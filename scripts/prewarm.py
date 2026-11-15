# Prewarms search_cache and plan_cache for the three-day demo, then proves the demo replays offline.
# Steps: reset -> live three days (CACHE_FIRST=false) + memory check -> reset -> offline replay
# (CACHE_FIRST=true, every LLM chat and Linkup call raises) -> final reset. Exits non-zero on any failure.
# --fake uses FakeLLM, FakeLinkupClient and JsonStore.memory() with the E2E test's fixture and plans.
# Spec: docs/pantry_agent_v2_1_patch_A.md section 5. B's agents are only wrapped at runtime, never edited.
from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pantry.agents import orchestrator as orchestrator_mod  # noqa: E402
from pantry.agents import planner as planner_mod  # noqa: E402
from pantry.agents import resolver as resolver_mod  # noqa: E402
from pantry.agents import worker as worker_mod  # noqa: E402
from pantry.agents.planner import Planner  # noqa: E402
from pantry.agents.resolver import Resolver  # noqa: E402
from pantry.agents.worker import Worker  # noqa: E402
from pantry.llm import FakeLLM, OpenNextLLM  # noqa: E402
from pantry.models import Command, Run  # noqa: E402
from pantry.store.base import Store  # noqa: E402
from pantry.tools.linkup import FakeLinkupClient  # noqa: E402
from scripts.reset_demo import reset_store  # noqa: E402

Out = Callable[[str], None]
DAYS = (1, 2, 3)
REJECT_DAY = 3


class PrewarmError(RuntimeError):
    """A demo step did not behave as the runbook expects."""


class OfflineCallBlocked(RuntimeError):
    """Raised by the offline guards when the replay tries to reach a real LLM chat or Linkup."""


# ---------------------------------------------------------------------------
# context: which store / worker / clients a phase runs with
# ---------------------------------------------------------------------------


@dataclass
class Context:
    mode: str  # "real" or "fake"
    backend: str
    start_date: date
    # Returns (store, worker) for one phase; real mode calls build_worker(settings) with CACHE_FIRST set.
    make_worker: Callable[[bool], tuple[Store, Worker]]
    # Fake fixture only: ADVANCE_CLOCK days needed after reset before the Day 1 trigger fires.
    setup_advance_days: int = 0
    before_day: Callable[[int], None] = lambda day: None


def _settings_with_cache_first(cache_first: bool):
    from pantry.config import load_settings

    os.environ["CACHE_FIRST"] = "true" if cache_first else "false"
    return load_settings()


def real_context() -> Context:
    from pantry.agents.worker import build_worker

    settings = _settings_with_cache_first(False)

    def make_worker(cache_first: bool) -> tuple[Store, Worker]:
        return build_worker(_settings_with_cache_first(cache_first))

    return Context("real", settings.store_backend, settings.demo_start_date, make_worker)


def fake_context() -> Context:
    """Offline context: the E2E test's fixture pantry, its FakeLLM plan registrations and FakeLinkupClient."""
    from pantry.agents.orchestrator import OneRunOrchestrator
    from pantry.seed import seed_store
    from pantry.store.json_store import JsonStore
    from pantry.tracing import MlflowTracer, set_tracer
    from tests.conftest import FIXTURE_SEED_DIR, START
    from tests.test_e2e_three_days import DAY1_PLAN, DAY2_PLAN, DAY3_PLAN, RESOLVER_OUTPUT

    set_tracer(MlflowTracer(""))  # never reach a tracking server
    store = JsonStore.memory(start_date=START)
    llm = FakeLLM()
    seed_store(store, llm, FIXTURE_SEED_DIR, START)
    llm.register("ResolverOutput", RESOLVER_OUTPUT)
    linkup = FakeLinkupClient()
    plans = {1: DAY1_PLAN, 2: DAY2_PLAN, 3: DAY3_PLAN}

    def make_worker(cache_first: bool) -> tuple[Store, Worker]:
        os.environ["CACHE_FIRST"] = "true" if cache_first else "false"
        return store, Worker(store, OneRunOrchestrator(store, llm, search_client=linkup))

    return Context(
        "fake",
        "json (memory)",
        START,
        make_worker,
        setup_advance_days=1,  # fixture milk expires at offset 3, as in tests/test_e2e_three_days.py
        before_day=lambda day: llm.register("PlannerOutput", plans[day]),
    )


# ---------------------------------------------------------------------------
# cache keys (B's functions where they exist; the spec reading otherwise)
# ---------------------------------------------------------------------------


def search_key(worker: Worker, query: str) -> str:
    resolver = worker.orchestrator.resolver
    return Resolver._cache_key(query, resolver.depth)


def _b_plan_key_fn() -> Callable[..., str] | None:
    for name in ("plan_cache_key", "cache_key", "_cache_key", "_plan_key"):
        fn = getattr(Planner, name, None) or getattr(planner_mod, name, None)
        if callable(fn):
            return fn
    return None


def spec_plan_key(snapshot_text: str, constraints: list[str], allergens: list[str]) -> str:
    """Spec reading of hash(snapshot_text + constraints + allergens); used only while B has no key function."""
    raw = json.dumps(
        {"snapshot_text": snapshot_text, "constraints": list(constraints), "allergens": sorted(allergens)},
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def plan_key(snapshot_text: str, constraints: list[str], allergens: list[str]) -> str:
    fn = _b_plan_key_fn()
    if fn is not None:
        return fn(snapshot_text, constraints, allergens)
    return spec_plan_key(snapshot_text, constraints, allergens)


def b_side_gaps(ctx: Context, worker: Worker) -> list[str]:
    """What the spec needs from B (Planner/Resolver/build_worker) that the code does not provide."""
    gaps: list[str] = []
    planner_src = inspect.getsource(planner_mod)
    if "plan_cache" not in planner_src:
        gaps.append(
            "Planner (pantry/agents/planner.py) never reads or writes plan_cache: every Planner call goes to the "
            "LLM, so the plan cannot be replayed offline."
        )
    if _b_plan_key_fn() is None:
        gaps.append(
            "No plan cache key function in B's code (spec: hash(snapshot_text + constraints + allergens)); "
            "prewarm checks plan_cache with its own spec reading, which B must match."
        )
    sources = [inspect.getsource(m) for m in (planner_mod, resolver_mod, orchestrator_mod, worker_mod)]
    if not any("cache_first" in s or "CACHE_FIRST" in s for s in sources):
        gaps.append("CACHE_FIRST is not read by the Planner, Resolver, orchestrator or worker (Settings.cache_first is unused).")
    depth = worker.orchestrator.resolver.depth
    if Resolver._cache_key("Cream  Substitute ", depth) != Resolver._cache_key("cream substitute", depth):
        gaps.append(
            "Resolver._cache_key hashes {query: query.strip(), depth} without lowercasing or collapsing "
            "whitespace (spec: normalized query). Exact replays still hit; prewarm uses B's key as is."
        )
    if "cache" not in inspect.getsource(orchestrator_mod.OneRunOrchestrator._extract_resolver_output):
        gaps.append(
            "OneRunOrchestrator._extract_resolver_output calls the LLM chat on every gap, including search_cache "
            "hits; the extracted ResolverOutput is not cached."
        )
    if ctx.mode == "real":
        client = worker.orchestrator.resolver.search_client
        if isinstance(client, FakeLinkupClient):
            gaps.append(
                "build_worker() passes no search client, so OneRunOrchestrator defaults to FakeLinkupClient: the "
                "live run never calls real Linkup and search_cache holds fake answers."
            )
        if isinstance(worker.orchestrator.llm, FakeLLM):
            gaps.append("build_worker() fell back to FakeLLM (LLM settings incomplete); the live run used no real LLM.")
    try:
        import pantry.agents.autorun  # noqa: F401
    except ImportError:
        gaps.append("pantry/agents/autorun.py (AutoRunner) does not exist; prewarm steps the days by hand.")
    return gaps


# ---------------------------------------------------------------------------
# offline guards
# ---------------------------------------------------------------------------


@dataclass
class Blocked:
    day: int
    what: str
    detail: str


class OfflineLLM:
    """Raises on chat_json; embeddings are passed through untouched."""

    def __init__(self, inner, log: list[Blocked], day: list[int]) -> None:
        self._inner, self._log, self._day = inner, log, day

    def chat_json(self, system: str, user: str, schema) -> dict:
        self._log.append(Blocked(self._day[0], f"LLM chat_json({schema.__name__})", user))
        raise OfflineCallBlocked(f"LLM chat_json({schema.__name__}) called during the offline replay")

    def embed(self, texts):
        return self._inner.embed(texts)

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


class OfflineSearch:
    def __init__(self, inner, log: list[Blocked], day: list[int]) -> None:
        self._inner, self._log, self._day = inner, log, day

    def search(self, query: str, depth: str = "standard") -> dict:
        self._log.append(Blocked(self._day[0], "Linkup search", query))
        raise OfflineCallBlocked("Linkup search called during the offline replay")


def go_offline(worker: Worker, log: list[Blocked], day: list[int]) -> None:
    orch = worker.orchestrator
    orch.llm = OfflineLLM(orch.llm, log, day)
    orch.planner.llm = OfflineLLM(orch.planner.llm, log, day)
    orch.resolver.search_client = OfflineSearch(orch.resolver.search_client, log, day)


def embedding_network_note(worker: Worker) -> str:
    llm = worker.orchestrator.llm
    llm = getattr(llm, "_inner", llm)
    if isinstance(llm, OpenNextLLM):
        return "yes (OpenNextLLM.embed calls the embeddings endpoint; EMBED_PROVIDER=local is not implemented)"
    return f"no ({type(llm).__name__} computes embeddings locally)"


# ---------------------------------------------------------------------------
# driving the three days (same stepping as tests/test_e2e_three_days.py)
# ---------------------------------------------------------------------------


@dataclass
class Round:
    day: int
    run_id: str
    trigger: str
    item_name: str
    state: str
    dish: str = ""
    trace_id: str = ""
    final_outcome: str = ""
    snapshot_text: str = ""
    constraints: list[str] = field(default_factory=list)
    allergens: list[str] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    resolver_tools: list[str] = field(default_factory=list)
    retrieved: list[tuple[str, float]] = field(default_factory=list)


def _command(store: Store, worker: Worker, type_: str, **payload: object) -> Command:
    command_id = f"prewarm-{type_.lower()}-{uuid4().hex[:10]}"
    store.enqueue_command(Command(command_id=command_id, type=type_, payload=payload))
    for command in worker.process_commands():
        if command.command_id == command_id:
            if command.status != "done":
                raise PrewarmError(f"{type_} {payload or ''} failed: {command.error}")
            return command
    raise PrewarmError(f"worker did not process {type_} {command_id}")


def _round(store: Store, worker: Worker, day: int, run: Run) -> Round:
    trace = store.get_trace_by_run(run.run_id)
    rnd = Round(day, run.run_id, run.trigger, run.item_name, run.state)
    if trace is None:
        return rnd
    rnd.trace_id, rnd.final_outcome = trace.trace_id, trace.final_outcome
    rnd.dish = trace.plan.dish_name if trace.plan else ""
    rnd.snapshot_text, rnd.constraints = trace.snapshot_text, list(trace.constraints)
    rnd.allergens = list(store.get_profile().allergens)
    rnd.queries = [a.query for a in trace.resolver_actions if a.tool in {"linkup", "cache"}]
    rnd.resolver_tools = [a.tool for a in trace.resolver_actions]
    rnd.retrieved = [(r.trace_id, r.similarity) for r in trace.retrieved_traces]
    return rnd


def drive_three_days(ctx: Context, store: Store, worker: Worker, out: Out, day_ref: list[int]) -> tuple[list[Round], str | None]:
    """Run Day 1..3: tick + dispatch_pending, APPROVE/REJECT through commands, ADVANCE_CLOCK between days.
    Returns the rounds and the first error (None when all three days completed)."""
    rounds: list[Round] = []
    try:
        if ctx.setup_advance_days:
            _command(store, worker, "ADVANCE_CLOCK", days=ctx.setup_advance_days)
        for day in DAYS:
            day_ref[0] = day
            ctx.before_day(day)
            emitted = worker.tick()
            before = {r.run_id for r in store.list_runs(limit=10_000)}
            try:
                worker.dispatch_pending()
            except Exception as exc:  # noqa: BLE001 - reported per round
                return rounds, f"Day {day}: dispatch failed: {type(exc).__name__}: {exc}"
            new = [r for r in store.list_runs(limit=10_000) if r.run_id not in before]
            if not new:
                events = [(e.trigger_type, e.item_name) for e in emitted]
                return rounds, f"Day {day} ({store.get_today()}): no Run was created; tick emitted {events}"
            for run in new:
                run = store.get_run(run.run_id) or run
                if run.pending_confirmation is not None or run.state == "PENDING_CONFIRMATION":
                    if day != REJECT_DAY:
                        out(f"  warning: Day {day} run {run.run_id} paused at confirmation; sending REJECT")
                    _command(store, worker, "REJECT", run_id=run.run_id)
                elif run.state != "RECORDED":
                    _command(store, worker, "APPROVE", run_id=run.run_id)
                rnd = _round(store, worker, day, store.get_run(run.run_id) or run)
                rounds.append(rnd)
                out(
                    f"  Day {day} ({store.get_today()}): {rnd.trigger} {rnd.item_name} -> {rnd.dish or '?'}; "
                    f"state={rnd.state} outcome={rnd.final_outcome} resolver={rnd.resolver_tools or '-'}"
                )
            if day != DAYS[-1]:
                _command(store, worker, "ADVANCE_CLOCK", days=1)
    except PrewarmError as exc:
        return rounds, f"Day {day_ref[0]}: {exc}"
    return rounds, None


def memory_check(rounds: list[Round], out: Out) -> bool:
    day1 = {r.trace_id for r in rounds if r.day == 1 and r.trace_id}
    day2 = [r for r in rounds if r.day == 2]
    ok = bool(day1) and bool(day2) and all(any(t in day1 for t, _ in r.retrieved) for r in day2)
    out(f"memory check: Day 1 trace retrieved on Day 2: {'yes' if ok else 'no'}")
    for r in day2:
        for trace_id, similarity in r.retrieved:
            tag = " (Day 1)" if trace_id in day1 else ""
            out(f"  similarity {similarity:.4f} {trace_id}{tag}")
        if not r.retrieved:
            out("  Day 2 retrieved no traces")
    return ok


# ---------------------------------------------------------------------------
# offline replay
# ---------------------------------------------------------------------------


def missing_cache_entries(store: Store, worker: Worker, rounds: list[Round]) -> dict[int, list[str]]:
    """Per day, the plan snapshots and search queries recorded live that have no cache entry now."""
    missing: dict[int, list[str]] = {}
    for r in rounds:
        items = missing.setdefault(r.day, [])
        if store.get_plan_cache(plan_key(r.snapshot_text, r.constraints, r.allergens)) is None:
            items.append(f"plan_cache miss, snapshot_text: {json.dumps(r.snapshot_text)}")
        for query in r.queries:
            if store.get_search_cache(search_key(worker, query)) is None:
                items.append(f"search_cache miss, query: {json.dumps(query)}")
    return missing


def explain_blocked(b: Blocked) -> str:
    if b.what.endswith("(PlannerOutput)"):
        return "Planner called the LLM instead of serving plan_cache (plan cache not implemented by B)"
    if b.what.endswith("(ResolverOutput)"):
        return "Resolver extractor called the LLM on a search_cache hit (extraction is not cached by B)"
    return f"Linkup called for a query not in search_cache: {json.dumps(b.detail)}"


def replay_offline(ctx: Context, live: list[Round], out: Out) -> tuple[bool, Store]:
    store, worker = ctx.make_worker(True)
    log: list[Blocked] = []
    day_ref = [0]
    go_offline(worker, log, day_ref)
    out(f"embeddings need the network: {embedding_network_note(worker)}")

    static = missing_cache_entries(store, worker, live)
    rounds, error = drive_three_days(ctx, store, worker, out, day_ref)

    ok = error is None and not log and not any(static.values()) and {r.day for r in rounds} == set(DAYS)
    if ok:
        hits = {d: sum(1 + r.resolver_tools.count("cache") for r in rounds if r.day == d) for d in DAYS}
        out("PASS offline replay: " + ", ".join(f"Day {d}: {hits[d]} cache hits" for d in DAYS))
        return True, store

    out("FAIL offline replay")
    for day in DAYS:
        lines = list(static.get(day, []))
        lines += [f"blocked {b.what}: {explain_blocked(b)}" for b in log if b.day == day]
        if not any(r.day == day for r in rounds) and day_ref[0] < day:
            lines.append("not replayed (an earlier day failed)")
        for line in lines or ["ok"]:
            out(f"  Day {day}: {line}")
    if error:
        out(f"  stopped at {error}")
    return False, store


# ---------------------------------------------------------------------------
# main flow
# ---------------------------------------------------------------------------


def _reset(store: Store, ctx: Context, out: Out) -> None:
    if reset_store(store, ctx.start_date, ctx.backend, out) != 0:
        raise PrewarmError("reset failed its inventory invariants")


def _created(store: Store, worker: Worker, rounds: list[Round], since: datetime) -> tuple[int, int]:
    search = {search_key(worker, q) for r in rounds for q in r.queries}
    plans = {plan_key(r.snapshot_text, r.constraints, r.allergens) for r in rounds}
    s = sum(1 for k in search if (e := store.get_search_cache(k)) is not None and e.called_at >= since)
    p = sum(1 for k in plans if (e := store.get_plan_cache(k)) is not None and e.called_at >= since)
    return s, p


def run_prewarm(ctx: Context, out: Out = print, after_live: Callable[[Store, list[Round]], None] | None = None) -> int:
    store, worker = ctx.make_worker(False)
    llm = worker.orchestrator.llm
    out(f"store backend: {ctx.backend}")
    out(
        f"clients: {'fake' if ctx.mode == 'fake' else 'real'} mode; LLM={type(llm).__name__}, "
        f"search={type(worker.orchestrator.resolver.search_client).__name__}"
    )
    gaps = b_side_gaps(ctx, worker)

    try:
        out("\n[1/4] reset to day 1")
        _reset(store, ctx, out)

        out("\n[2/4] live three-day run (CACHE_FIRST=false)")
        live_start = datetime.now(timezone.utc)
        live, error = drive_three_days(ctx, store, worker, out, [0])
        if error:
            raise PrewarmError(f"live run: {error}")
        memory_ok = memory_check(live, out)
        created = _created(store, worker, live, live_start)
        out(f"cache entries written by the live run: search_cache={created[0]}, plan_cache={created[1]}")
        if after_live is not None:
            after_live(store, live)

        out("\n[3/4] reset, then offline replay (CACHE_FIRST=true, LLM chat and Linkup raise)")
        _reset(store, ctx, out)
        if gaps:
            out("B-side gaps that block or weaken the offline replay:")
            for gap in gaps:
                out(f"  - {gap}")
        replay_ok, store = replay_offline(ctx, live, out)
    except PrewarmError as exc:
        out(f"FAIL {exc}")
        memory_ok = replay_ok = False
        created = (0, 0)

    out("\n[4/4] final reset for the demo")
    try:
        _reset(store, ctx, out)
        reset_ok = True
    except PrewarmError as exc:
        out(f"FAIL {exc}")
        reset_ok = False

    out("\nsummary")
    out(f"  cache entries created: search_cache={created[0]}, plan_cache={created[1]}")
    out(f"  memory check: {'yes' if memory_ok else 'no'}")
    out(f"  offline replay: {'PASS' if replay_ok else 'FAIL'}")
    if gaps:
        out(f"  B-side gaps: {len(gaps)} (listed above)")
    return 0 if (memory_ok and replay_ok and reset_ok) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prewarm the demo caches and verify an offline replay.")
    parser.add_argument("--fake", action="store_true", help="FakeLLM, FakeLinkupClient and JsonStore.memory()")
    args = parser.parse_args(argv)
    try:
        ctx = fake_context() if args.fake else real_context()
    except Exception as exc:  # noqa: BLE001 - e.g. Mongo unreachable; never print settings values
        print(f"FAIL could not build the {'fake' if args.fake else 'real'} context: {type(exc).__name__}: {exc}")
        return 2
    return run_prewarm(ctx)


if __name__ == "__main__":
    sys.exit(main())
