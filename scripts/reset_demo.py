# Restores the demo to day 1 (DEMO_START_DATE) on the STORE_BACKEND store; safe to run repeatedly.
# Keeps search_cache, plan_cache and seed traces; clears runs, bus events, commands, ledger, feedback, shopping list.
from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pantry.config import get_settings  # noqa: E402
from pantry.store.base import Store  # noqa: E402
from pantry.store.factory import make_store  # noqa: E402


def reset_store(store: Store, start_date: date, backend: str, out: Callable[[str], None] = print) -> int:
    """Reset store to day 1, print the summary and return 0 when the inventory invariants hold."""
    counts = store.reset_demo(start_date)

    problems = store.check_invariants()
    seeds = len(store.list_traces(is_seed=True, limit=10_000))
    out(f"Reset backend={backend} to day 1 ({store.get_today().isoformat()})")
    out(f"  restored pantry items: {counts.get('pantry_items', 0)}")
    for name in sorted(k for k in counts if k != "pantry_items"):
        out(f"  cleared {name}: {counts[name]}")
    out(f"  kept seed traces: {seeds}; search_cache and plan_cache untouched")
    if counts.get("pantry_items", 0) == 0:
        out("  warning: no pantry baseline found; run python -m pantry.seed first")
    if problems:
        out("FAIL invariant violations after reset:")
        for problem in problems:
            out(f"  - {problem}")
        return 1
    out("PASS inventory invariants hold")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reset the pantry demo to day 1.")
    parser.add_argument("--backend", choices=["mongo", "json"], help="override STORE_BACKEND")
    args = parser.parse_args(argv)

    settings = get_settings()
    backend = args.backend or settings.store_backend
    store = make_store(backend, settings)
    return reset_store(store, settings.demo_start_date, backend)


if __name__ == "__main__":
    sys.exit(main())
