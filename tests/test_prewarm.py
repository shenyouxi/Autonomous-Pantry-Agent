# Offline tests for scripts/prewarm.py with --fake semantics (FakeLLM, FakeLinkupClient, JsonStore.memory()).
# B's Planner has no plan cache yet, so the offline replay against B's real code is expected to fail (xfail,
# strict: it starts passing once B implements the cache). The PASS path and the "missing item" reporting are
# exercised with test-only cache-aware stand-ins for the Planner and the Resolver extractor, installed at
# runtime; B's files are not touched.
from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime, timezone

import pytest

from pantry.agents.planner import Planner
from pantry.models import PlanCacheEntry, PlannerOutput
from pantry.tracing import set_tracer
from scripts import prewarm


@pytest.fixture(autouse=True)
def isolate(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("CACHE_FIRST", "false")  # prewarm flips it; monkeypatch restores it
    yield
    set_tracer(None)


class CachingPlanner(Planner):
    """Stand-in for the spec's cache-first Planner: plan_cache keyed by prewarm.plan_key."""

    def __init__(self, inner: Planner, store) -> None:
        super().__init__(inner.llm)
        self.store = store
        self.event = None

    def plan(self, snapshot, retrieved_trace_ids=()):
        event = self.event
        constraints = [str(event.constraints.model_dump())]
        allergens = list(event.allergens or self.store.get_profile().allergens)
        key = prewarm.plan_key(snapshot, constraints, allergens)
        cached = self.store.get_plan_cache(key)
        if cached is not None:
            return PlannerOutput.model_validate(cached.plan.model_dump())
        plan = super().plan(snapshot, retrieved_trace_ids)
        self.store.put_plan_cache(
            PlanCacheEntry(
                key=key,
                snapshot_text=snapshot,
                constraints=constraints,
                allergens=allergens,
                plan=plan,
                called_at=datetime.now(timezone.utc),
            )
        )
        return plan


def cache_aware_context() -> prewarm.Context:
    """fake_context() whose workers get a cache-first Planner and an extractor cached per query."""
    ctx = prewarm.fake_context()
    base = ctx.make_worker
    extracted: dict[str, object] = {}

    def make_worker(cache_first: bool):
        store, worker = base(cache_first)
        orch = worker.orchestrator
        planner = CachingPlanner(orch.planner, store)
        orch.planner = planner
        execute = orch.execute

        def execute_with_event(event):
            planner.event = event
            return execute(event)

        orch.execute = execute_with_event

        def extractor(query, result):
            if query not in extracted:
                extracted[query] = orch._extract_resolver_output(query, result)
            return extracted[query]

        orch.resolver.extractor = extractor
        return store, worker

    ctx.make_worker = make_worker
    return ctx


def run(ctx: prewarm.Context, after_live=None) -> tuple[int, str]:
    lines: list[str] = []
    code = prewarm.run_prewarm(ctx, out=lines.append, after_live=after_live)
    return code, "\n".join(lines)


def test_live_run_fills_search_cache_and_memory_check_holds() -> None:
    ctx = prewarm.fake_context()
    seen = {}

    def capture(store, rounds):
        seen["store"], seen["rounds"] = store, rounds

    _, output = run(ctx, after_live=capture)

    assert "memory check: Day 1 trace retrieved on Day 2: yes" in output
    assert "cache entries written by the live run: search_cache=3" in output
    rounds = seen["rounds"]
    assert [(r.day, r.final_outcome) for r in rounds] == [(1, "accepted"), (2, "accepted"), (3, "rejected")]
    store = seen["store"]
    _, worker = ctx.make_worker(True)
    for r in rounds:
        for query in r.queries:
            entry = store.get_search_cache(prewarm.search_key(worker, query))
            assert entry is not None and entry.called_at is not None
    # Final reset: back to day 1 with no runs, caches kept.
    assert store.get_today() == ctx.start_date
    assert store.list_runs(limit=100) == []


@pytest.mark.xfail(
    strict=True,
    reason="B: Planner has no plan_cache and the Resolver extractor calls the LLM on cache hits",
)
def test_prewarm_fills_caches_and_offline_replay_passes() -> None:
    code, output = run(prewarm.fake_context())
    assert "PASS offline replay" in output, output
    assert code == 0


def test_offline_replay_failure_names_the_b_gap() -> None:
    code, output = run(prewarm.fake_context())
    assert code != 0
    assert "FAIL offline replay" in output
    assert "Day 1: plan_cache miss, snapshot_text:" in output
    assert "Planner called the LLM instead of serving plan_cache" in output


def test_replay_passes_with_cache_aware_planner_and_extractor() -> None:
    code, output = run(cache_aware_context())
    assert code == 0, output
    assert "PASS offline replay: Day 1: 2 cache hits, Day 2: 2 cache hits, Day 3: 2 cache hits" in output
    assert "cache entries created: search_cache=3, plan_cache=3" in output


@pytest.mark.parametrize("kind", ["search", "plan"])
def test_removed_cache_entry_fails_replay_and_names_it(kind: str) -> None:
    ctx = cache_aware_context()
    removed = {}

    def drop_one(store, rounds):
        day2 = next(r for r in rounds if r.day == 2)
        if kind == "search":
            query = day2.queries[0]
            _, worker = ctx.make_worker(False)
            del store._search_cache[prewarm.search_key(worker, query)]
            removed["line"] = f"Day 2: search_cache miss, query: {json.dumps(query)}"
        else:
            del store._plan_cache[prewarm.plan_key(day2.snapshot_text, day2.constraints, day2.allergens)]
            removed["line"] = f"Day 2: plan_cache miss, snapshot_text: {json.dumps(day2.snapshot_text)}"

    code, output = run(ctx, after_live=drop_one)

    assert code != 0
    assert "FAIL offline replay" in output
    assert removed["line"] in output, output
    assert "Day 1: ok" in output
    blocked = "Linkup search" if kind == "search" else "LLM chat_json(PlannerOutput)"
    assert f"Day 2: blocked {blocked}" in output
