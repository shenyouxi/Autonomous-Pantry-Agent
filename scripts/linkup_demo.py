# Stage demo of the Resolver: one MISSING gap -> real Linkup search -> real LLM extraction -> pantry check.
# Usage: python scripts/linkup_demo.py [--item eggs] [--amount 2] [--unit pcs] [--no-cache]
# Builds the clients like build_worker() but never falls back to fakes: if the LLM or Linkup cannot be built
# or called, it exits non-zero. The store is wrapped read-only; only the Resolver's own search_cache write
# (put_search_cache) is let through. No runs, traces, events or inventory changes are written.
from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pantry import rules  # noqa: E402
from pantry.agents.orchestrator import OneRunOrchestrator  # noqa: E402
from pantry.agents.units import normalise_quantity  # noqa: E402
from pantry.agents.worker import Worker  # noqa: E402
from pantry.models import Gap  # noqa: E402
from pantry.store.base import Store  # noqa: E402

Out = Callable[[str], None]
MAX_SOURCES = 5

EXIT_SETUP = 2  # store, LLM or Linkup could not be built
EXIT_CALL = 3  # Linkup or the LLM failed during the call
EXIT_WRITE = 4  # something tried to write to the store beyond search_cache


class DemoError(RuntimeError):
    """A clear, user-facing failure; the message never contains settings values."""


class LinkupUnavailable(DemoError):
    pass


class LLMUnavailable(DemoError):
    pass


class StoreWriteBlocked(DemoError):
    pass


# ---------------------------------------------------------------------------
# guards
# ---------------------------------------------------------------------------

_READ_PREFIXES = ("get_", "list_", "search_traces", "check_invariants", "replay_ledger")
_ALLOWED_WRITES = {"put_search_cache"}


class ReadOnlyStore:
    """Store proxy: reads pass through, put_search_cache passes through, every other method raises."""

    def __init__(self, inner: Store) -> None:
        self._inner = inner
        self.search_cache_writes = 0

    def put_search_cache(self, entry) -> None:
        self.search_cache_writes += 1
        self._inner.put_search_cache(entry)

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._inner, name)
        if not callable(attr) or name.startswith(_READ_PREFIXES) or name in _ALLOWED_WRITES:
            return attr

        def blocked(*args: Any, **kwargs: Any) -> Any:
            raise StoreWriteBlocked(f"linkup_demo is read-only; blocked store.{name}()")

        return blocked


class GuardedSearch:
    """Turns any search client failure into LinkupUnavailable and counts real calls."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls = 0

    def search(self, query: str, depth: str = "standard") -> dict:
        self.calls += 1
        try:
            return self._inner.search(query=query, depth=depth)
        except Exception as exc:  # noqa: BLE001 - reported as a clear failure, no fallback
            raise LinkupUnavailable(f"Linkup search failed: {type(exc).__name__}: {exc}") from exc


class GuardedLLM:
    """Turns chat_json failures into LLMUnavailable; everything else passes through."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls = 0

    def chat_json(self, system: str, user: str, schema) -> dict:
        self.calls += 1
        try:
            return self._inner.chat_json(system, user, schema)
        except Exception as exc:  # noqa: BLE001 - reported as a clear failure, no fallback
            raise LLMUnavailable(f"LLM extraction failed: {type(exc).__name__}: {exc}") from exc

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


# ---------------------------------------------------------------------------
# real clients (same construction as build_worker, minus the silent fallbacks)
# ---------------------------------------------------------------------------


def build_real_clients():
    from pantry.config import get_settings
    from pantry.llm import OpenNextLLM
    from pantry.store.factory import make_store
    from pantry.tools.linkup import LinkupClient

    try:
        settings = get_settings()
    except Exception as exc:  # noqa: BLE001
        raise DemoError(f"settings could not be loaded: {type(exc).__name__}") from exc
    try:
        store = make_store(None, settings)
        store.get_today()  # forces a round trip so an unreachable Mongo fails here
    except Exception as exc:  # noqa: BLE001
        raise DemoError(f"{settings.store_backend} store unavailable: {type(exc).__name__}") from exc
    try:
        llm = OpenNextLLM(settings)
    except Exception as exc:  # noqa: BLE001 - LLMConfigError lists missing names only
        raise LLMUnavailable(f"LLM unavailable: {type(exc).__name__}: {exc}") from exc
    try:
        search_client = LinkupClient(settings.linkup_api_key)
    except Exception as exc:  # noqa: BLE001 - message says the key is missing, never its value
        raise LinkupUnavailable(f"Linkup unavailable: {type(exc).__name__}: {exc}") from exc
    return settings, store, llm, search_client


# ---------------------------------------------------------------------------
# demo
# ---------------------------------------------------------------------------


def inventory_context(orchestrator: OneRunOrchestrator, store: Store) -> str:
    """inventory=name available unit; ... from current stock, via OneRunOrchestrator._resolver_context."""
    return orchestrator._resolver_context("inventory=", store.list_items())


def pantry_status(worker: Worker, store: Store, component: dict) -> str:
    """Worker._component_satisfiable verdict plus a short read-only explanation."""
    ok = worker._component_satisfiable(component)
    name = str(component.get("name", ""))
    item = store.get_item(name)
    if item is None:
        detail = "staple" if rules.is_staple(name) else "not in pantry"
    else:
        available = item.quantity - item.reserved
        detail = f"pantry has {available:g} {item.unit} available as {item.item_name}"
        amount = component.get("amount")
        if amount and amount > 0 and normalise_quantity(amount, component.get("unit") or item.unit, item.unit) is None:
            detail += f"; cannot convert {component.get('unit')} to {item.unit}"
    return f"{'yes' if ok else 'no'} ({detail})"


def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def run_demo(
    store: Store,
    llm: Any,
    search_client: Any,
    *,
    item: str,
    amount: float,
    unit: str,
    cache_first: bool,
    out: Out = print,
) -> int:
    ro_store = ReadOnlyStore(store)
    guarded_llm = GuardedLLM(llm)
    guarded_search = GuardedSearch(search_client)
    orchestrator = OneRunOrchestrator(ro_store, guarded_llm, search_client=guarded_search, cache_first=cache_first)
    worker = Worker(ro_store, orchestrator)

    gap = Gap(name=item, needed=amount, unit=unit, kind="MISSING")
    context = inventory_context(orchestrator, ro_store)
    out(f"gap: {gap.name} | {gap.needed:g} | {gap.unit} | {gap.kind}")
    out(f"context: {context}")

    try:
        output, action = orchestrator.resolver.resolve_gap(gap, context)
    except LinkupUnavailable as exc:
        out(f"FAIL {exc}")
        return EXIT_CALL
    except LLMUnavailable as exc:
        out(f"FAIL {exc}")
        return EXIT_CALL
    except StoreWriteBlocked as exc:
        out(f"FAIL {exc}")
        return EXIT_WRITE

    if action.tool == "cache":
        out(f"resolver: search_cache hit (cached {action.cached_at}); Linkup and LLM not called")
    else:
        out(f"resolver: live Linkup search ({guarded_search.calls} call), LLM extraction ({guarded_llm.calls} call)")
    out(f"substitute: {output.substitute_name} (confidence {output.confidence})")
    if output.ratio_text:
        out(f"ratio: {output.ratio_text}")
    out("components (name | amount | unit | in pantry):")
    for component in output.components:
        c = component.model_dump()
        out(f"  {c['name']} | {_fmt(c.get('amount'))} | {_fmt(c.get('unit'))} | {pantry_status(worker, ro_store, c)}")

    sources = [s for s in action.sources if isinstance(s, dict) and s.get("url")][:MAX_SOURCES]
    if not sources and output.source_url:
        sources = [{"title": "", "url": output.source_url}]
    out("sources:")
    for s in sources:
        out(f"  {s.get('title') or '(untitled)'} - {s['url']}")
    if not sources:
        out("  (none returned)")
    out(f"store writes: search_cache={ro_store.search_cache_writes}, nothing else")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage demo of the Resolver on one MISSING ingredient.")
    parser.add_argument("--item", default="eggs")
    parser.add_argument("--amount", type=float, default=2)
    parser.add_argument("--unit", default="pcs")
    parser.add_argument("--no-cache", action="store_true", help="skip the search_cache read (force a live call)")
    args = parser.parse_args(argv)
    if args.amount < 0:
        parser.error("--amount must be >= 0")

    try:
        settings, store, llm, search_client = build_real_clients()
    except DemoError as exc:
        print(f"FAIL {exc}")
        return EXIT_SETUP
    cache_first = settings.cache_first and not args.no_cache
    print(
        f"mode: real (LLM={type(llm).__name__} model={llm.model}, search={type(search_client).__name__}, "
        f"store={settings.store_backend} read-only, CACHE_FIRST={'true' if cache_first else 'false'})"
    )
    return run_demo(
        store, llm, search_client,
        item=args.item, amount=args.amount, unit=args.unit, cache_first=cache_first,
    )


if __name__ == "__main__":
    sys.exit(main())
