# End-to-end three-day demo (seed_data/demo_script.json) driven only through the public Worker API and
# Store commands: enqueue_command + process_commands + tick + dispatch_pending, like the real worker loop.
# Offline: JsonStore.memory(), FakeLLM, FakeLinkupClient and MLflow tracing disabled.
#
# Differences from demo_script.json forced by the small test fixture (tests/fixtures/seed_data):
# - the fixture pantry has no spinach or cream, so every day is triggered by milk (EXPIRY_IMMINENT);
# - milk expires at offset 3, so one setup ADVANCE_CLOCK moves it into the 2-day window (as in
#   tests/test_three_rounds.py); Day 1 is therefore START + 1;
# - the fixture profile already lists "nuts", so Day 3 needs no SET_CONSTRAINT (not implemented yet).
from __future__ import annotations

import inspect
from collections.abc import Iterator
from datetime import timedelta
from itertools import count
from pathlib import Path

import pytest

from pantry.agents.orchestrator import OneRunOrchestrator
from pantry.agents.resolver import Resolver
from pantry.agents.worker import Worker
from pantry.llm import FakeLLM
from pantry.models import Command, Run, Trace
from pantry.seed import seed_store
from pantry.store.base import Store
from pantry.store.json_store import JsonStore
from pantry.tools.linkup import FakeLinkupClient, LinkupResult
from pantry.tracing import MlflowTracer, set_tracer
from tests.conftest import START

TRIGGER = "EXPIRY_IMMINENT"
Ledger = dict[str, tuple[float, float]]

# Day 1 mirrors round 1: an expiring-milk recipe with 100 ml of missing cream (a MISSING gap for the Resolver).
DAY1_PLAN = {
    "dish_name": "creamy milk pancakes",
    "servings": 2,
    "est_minutes": 20,
    "ingredients": [
        {"name": "milk", "amount": 300, "unit": "ml"},
        {"name": "heavy cream", "amount": 100, "unit": "ml"},
        {"name": "flour", "amount": 200, "unit": "g"},
        {"name": "eggs", "amount": 2, "unit": "pcs"},
    ],
    "steps": ["Whisk the batter.", "Cook on a hot pan."],
    "rationale": "Uses the expiring milk.",
}
# Day 2 mirrors round 2: a similar cream gap (75 ml), which should recall the Day 1 decision.
DAY2_PLAN = {
    "dish_name": "creamy baked custard",
    "servings": 2,
    "est_minutes": 25,
    "ingredients": [
        {"name": "milk", "amount": 200, "unit": "ml"},
        {"name": "heavy cream", "amount": 75, "unit": "ml"},
        {"name": "eggs", "amount": 2, "unit": "pcs"},
        {"name": "sugar", "amount": 50, "unit": "g"},
    ],
    "steps": ["Whisk.", "Bake."],
    "rationale": "Uses the expiring milk again.",
}
# Day 3 mirrors round 3: a walnut pesto candidate that hits the user's nut allergy.
DAY3_PLAN = {
    "dish_name": "walnut pesto pasta",
    "servings": 2,
    "est_minutes": 15,
    "ingredients": [
        {"name": "milk", "amount": 100, "unit": "ml"},
        {"name": "walnuts", "amount": 50, "unit": "g"},
    ],
    "steps": ["Blend the pesto.", "Toss with pasta."],
    "rationale": "Quick use of milk.",
}
# Canned extraction answer for a Resolver that uses the LLM (round 1 substitution_candidate).
RESOLVER_OUTPUT = {
    "substitute_name": "milk and butter",
    "components": [
        {"name": "milk", "amount": 75, "unit": "ml"},
        {"name": "butter", "amount": 25, "unit": "g"},
    ],
    "ratio_text": "3 parts milk to 1 part melted butter; cooking only, cannot be whipped.",
    "source_url": "https://example.com/fake-cooking",
    "confidence": "high",
}

MISSING_RESOLVER_WIRING = (
    "B wiring missing: OneRunOrchestrator takes no resolver/search client and execute() never calls "
    "Resolver.resolve_gap, so the Day 1 MISSING gap (heavy cream) gets no resolver_actions and APPROVE "
    "fails with \"missing pantry item 'heavy cream'\"; non-risk plans are also not auto-accepted."
)


class SpyLinkup(FakeLinkupClient):
    def __init__(self) -> None:
        self.queries: list[str] = []

    def search(self, query: str, depth: str = "standard") -> LinkupResult:
        self.queries.append(query)
        return super().search(query, depth)


@pytest.fixture(autouse=True)
def no_mlflow() -> Iterator[None]:
    """Disable MLflow so the test never reaches a tracking server."""
    set_tracer(MlflowTracer(""))
    yield
    set_tracer(None)


def build_orchestrator(store: Store, llm: FakeLLM, linkup: SpyLinkup) -> OneRunOrchestrator:
    """Construct the orchestrator, passing the Resolver and the Linkup fake through whichever keyword
    parameters it accepts (none today); B's files are not modified."""
    params = inspect.signature(OneRunOrchestrator.__init__).parameters
    offered = {
        "resolver": Resolver(store, linkup),
        "search_client": linkup,
        "linkup_client": linkup,
        "linkup": linkup,
    }
    kwargs = {name: value for name, value in offered.items() if name in params}
    return OneRunOrchestrator(store, llm, **kwargs)


class Harness:
    """Drives the system the way the worker loop and the operator CLI do."""

    def __init__(self, store: Store, worker: Worker) -> None:
        self.store = store
        self.worker = worker
        self._ids = count(1)

    def command(self, type_: str, **payload: object) -> Command:
        command_id = f"cmd-e2e-{next(self._ids)}"
        self.store.enqueue_command(Command(command_id=command_id, type=type_, payload=payload))
        processed = {c.command_id: c for c in self.worker.process_commands()}
        assert command_id in processed, f"worker did not process {type_} {command_id}"
        return processed[command_id]

    def run_day(self, label: str) -> Run:
        """tick + dispatch_pending; the day must produce exactly one milk Run."""
        emitted = self.worker.tick()
        assert [(e.trigger_type, e.item_name) for e in emitted] == [(TRIGGER, "milk")], (
            f"[{label} / tick] expected one milk {TRIGGER} bus event, got "
            f"{[(e.trigger_type, e.item_name) for e in emitted]}"
        )
        before = {r.run_id for r in self.store.list_runs(limit=1000)}
        runs = self.worker.dispatch_pending()
        new = [r for r in self.store.list_runs(limit=1000) if r.run_id not in before]
        assert len(runs) == 1 and len(new) == 1, f"[{label} / dispatch] expected exactly one new Run, got {len(new)}"
        run = new[0]
        assert (run.trigger, run.item_name) == (TRIGGER, "milk"), f"[{label} / dispatch] wrong run {run.trigger}:{run.item_name}"
        return run

    def trace(self, run: Run, label: str) -> Trace:
        trace = self.store.get_trace_by_run(run.run_id)
        assert trace is not None, f"[{label} / trace] no decision trace for {run.run_id}"
        return trace


def stored(store: Store) -> Ledger:
    return {item.item_name: (item.quantity, item.reserved) for item in store.list_items()}


def assert_consistent(store: Store, label: str) -> None:
    problems = store.check_invariants()
    assert problems == [], f"[{label} / invariants] {problems}"
    assert store.replay_ledger() == stored(store), f"[{label} / replay] ledger replay differs from stored quantities"


def reserved_by_run(store: Store, run_id: str) -> dict[str, float]:
    """Outstanding RESERVED amount per item for a run (reversed reservations excluded)."""
    events = store.list_events(run_id=run_id)
    reversed_ids = {e.reverses_event_id for e in events if e.reverses_event_id}
    totals: dict[str, float] = {}
    for event in events:
        if event.type == "RESERVED" and event.event_id not in reversed_ids and not event.reverses_event_id:
            totals[event.item_name] = totals.get(event.item_name, 0.0) + event.delta
    return totals


def accept_day(h: Harness, run: Run, label: str) -> dict[str, float]:
    """Auto-accept if the worker already did it, otherwise APPROVE; reservations must then exist."""
    run = h.store.get_run(run.run_id) or run
    if run.state != "RECORDED":
        approve = h.command("APPROVE", run_id=run.run_id)
        assert approve.status == "done", f"[{label} / APPROVE] command failed: {approve.error}"
    run = h.store.get_run(run.run_id)
    assert run is not None and run.state == "RECORDED" and run.pending_confirmation is None, (
        f"[{label} / APPROVE] run not recorded: state={run.state if run else None}"
    )
    reservations = reserved_by_run(h.store, run.run_id)
    assert reservations, f"[{label} / reserve] no RESERVED events for {run.run_id}"
    assert "heavy cream" not in reservations, f"[{label} / reserve] the missing gap item was reserved"
    for name, amount in reservations.items():
        assert h.store.get_item(name).reserved >= amount, f"[{label} / reserve] {name} reservation not in stock"
    assert h.trace(run, label).final_outcome == "accepted", f"[{label} / trace] outcome is not accepted"
    assert_consistent(h.store, f"{label} after accept")
    return reservations


def advance_and_commit(h: Harness, run: Run, reservations: dict[str, float], label: str) -> None:
    """ADVANCE_CLOCK commits the applied run (reserved -> consumed) and then advances the date."""
    before_day = h.store.get_today()
    before = stored(h.store)
    clock = h.command("ADVANCE_CLOCK", days=1)
    assert clock.status == "done", f"[{label} / ADVANCE_CLOCK] command failed: {clock.error}"
    assert h.store.get_today() == before_day + timedelta(days=1), f"[{label} / ADVANCE_CLOCK] date did not advance"
    committed = h.store.get_run(run.run_id)
    assert committed is not None and committed.committed, f"[{label} / ADVANCE_CLOCK] run was not committed"
    consumed = [e for e in h.store.list_events(run_id=run.run_id) if e.type == "CONSUMED"]
    assert {e.item_name for e in consumed} == set(reservations), f"[{label} / ADVANCE_CLOCK] CONSUMED events mismatch"
    after = stored(h.store)
    for name, (quantity, reserved) in before.items():
        used = reservations.get(name, 0.0)
        assert after[name] == (quantity - used, reserved - used), (
            f"[{label} / ADVANCE_CLOCK] {name}: expected {(quantity - used, reserved - used)}, got {after[name]}"
        )
    assert_consistent(h.store, f"{label} after ADVANCE_CLOCK")


def test_three_day_demo_through_worker(seed_dir: Path) -> None:
    store = JsonStore.memory(start_date=START)
    llm = FakeLLM()
    seed_store(store, llm, seed_dir, START)
    llm.register("ResolverOutput", RESOLVER_OUTPUT)
    linkup = SpyLinkup()
    h = Harness(store, Worker(store, build_orchestrator(store, llm, linkup)))
    assert_consistent(store, "seed")

    # Setup: move milk (offset 3) into the 2-day urgency window through the worker.
    setup = h.command("ADVANCE_CLOCK", days=1)
    assert setup.status == "done", f"[setup / ADVANCE_CLOCK] {setup.error}"
    assert store.get_today() == START + timedelta(days=1)
    assert_consistent(store, "setup")

    # ---------------- Day 1: milk EXPIRY_IMMINENT, cream gap resolved via Linkup ----------------
    llm.register("PlannerOutput", DAY1_PLAN)
    day1 = h.run_day("Day 1")
    assert len(store.list_runs(limit=1000)) == 1, "[Day 1 / dispatch] Day 1 must produce exactly one Run"
    gaps = (store.get_run(day1.run_id).checkpoints.get("GAP_CHECKED") or {}).get("gaps", [])
    assert [(g["name"], g["kind"]) for g in gaps] == [("heavy cream", "MISSING")], f"[Day 1 / GapCheck] gaps={gaps}"
    day1_trace = h.trace(day1, "Day 1")
    linkup_actions = [a for a in day1_trace.resolver_actions if a.tool == "linkup"]
    assert linkup_actions and linkup.queries, (
        f"[Day 1 / Resolver] the MISSING gap was not resolved via Linkup: resolver_actions="
        f"{[a.tool for a in day1_trace.resolver_actions]}, linkup calls={len(linkup.queries)}"
    )
    assert linkup_actions[0].sources and linkup_actions[0].sources[0]["url"], "[Day 1 / Resolver] no cited source"
    assert day1_trace.risk_status == "none", f"[Day 1 / Gate] unexpected risk {day1_trace.risk_status}"
    day1_reserved = accept_day(h, day1, "Day 1")
    advance_and_commit(h, day1, day1_reserved, "Day 1")

    # ---------------- Day 2: recall the Day 1 decision ----------------
    llm.register("PlannerOutput", DAY2_PLAN)
    day2 = h.run_day("Day 2")
    day2_trace = h.trace(day2, "Day 2")
    retrieved_ids = [r.trace_id for r in day2_trace.retrieved_traces]
    assert day1_trace.trace_id in retrieved_ids, (
        f"[Day 2 / vector_search] Day 1 trace {day1_trace.trace_id} not retrieved: {retrieved_ids}"
    )
    assert any(a.tool in {"linkup", "cache"} for a in day2_trace.resolver_actions), (
        "[Day 2 / Resolver] the cream gap was not resolved"
    )
    day2_reserved = accept_day(h, day2, "Day 2")
    advance_and_commit(h, day2, day2_reserved, "Day 2")

    # ---------------- Day 3: allergen gate pauses, REJECT is recorded ----------------
    llm.register("PlannerOutput", DAY3_PLAN)
    before_day3 = stored(store)
    day3_date = store.get_today()
    day3 = h.run_day("Day 3")
    day3 = store.get_run(day3.run_id)
    pending = day3.pending_confirmation
    assert pending is not None and pending.rule == "allergen", f"[Day 3 / Gate] pending={pending}"
    # The worker treats state PENDING_CONFIRMATION or a stored pending_confirmation as paused.
    assert day3.state in {"PENDING_CONFIRMATION", "GATED"}, f"[Day 3 / Gate] state={day3.state}"
    assert h.trace(day3, "Day 3").risk_status == "allergen", "[Day 3 / Gate] trace risk_status is not allergen"
    assert store.list_events(run_id=day3.run_id) == [], "[Day 3 / Gate] inventory touched before confirmation"

    blocked = h.command("ADVANCE_CLOCK", days=1)
    assert blocked.status == "failed" and store.get_today() == day3_date, (
        "[Day 3 / ADVANCE_CLOCK] clock advanced while a run was pending confirmation"
    )

    reject = h.command("REJECT", run_id=day3.run_id)
    assert reject.status == "done", f"[Day 3 / REJECT] command failed: {reject.error}"
    day3 = store.get_run(day3.run_id)
    assert day3.state == "REJECTED" and day3.pending_confirmation is None, f"[Day 3 / REJECT] state={day3.state}"
    day3_trace = h.trace(day3, "Day 3")
    assert day3_trace.final_outcome == "rejected", f"[Day 3 / REJECT] outcome={day3_trace.final_outcome}"
    assert store.list_events(run_id=day3.run_id) == [], "[Day 3 / REJECT] rejected run wrote ledger events"
    assert stored(store) == before_day3, "[Day 3 / REJECT] inventory changed by a rejected run"
    assert_consistent(store, "Day 3 after REJECT")

    # The rejected decision stays findable for audit and memory, with its embedding kept ...
    rejected = [t for t in store.list_traces(is_seed=False, limit=1000) if t.final_outcome == "rejected"]
    assert [t.trace_id for t in rejected] == [day3_trace.trace_id], "[Day 3 / memory] rejected trace not listed"
    assert store.get_trace(day3_trace.trace_id).embedding, "[Day 3 / memory] rejected trace has no embedding"
    # ... but, per the Store.search_traces contract, it is never recalled as a positive example.
    probe = llm.embed([day3_trace.snapshot_text])[0]
    recalled = [r.trace_id for r in store.search_traces(probe, TRIGGER, k=10)[0]]
    assert day3_trace.trace_id not in recalled and day1_trace.trace_id in recalled, f"[Day 3 / memory] {recalled}"

    # Closing ADVANCE_CLOCK: nothing is consumed for the rejected run.
    closing = h.command("ADVANCE_CLOCK", days=1)
    assert closing.status == "done", f"[Day 3 / closing ADVANCE_CLOCK] {closing.error}"
    assert not [e for e in store.list_events(run_id=day3.run_id) if e.type == "CONSUMED"]
    assert stored(store) == before_day3, "[Day 3 / closing ADVANCE_CLOCK] inventory deducted for a rejected run"
    assert_consistent(store, "Day 3 closing")
