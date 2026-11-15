# Bounded auto-run (pantry/agents/autorun.py) driven like the worker loop: process_commands, tick,
# dispatch_pending, then AutoRunner.tick. Offline: JsonStore.memory(), FakeLLM, FakeLinkupClient,
# MLflow disabled. Reuses the plans and Harness from the three-day E2E test without changing it.
# The Worker does not route START_AUTO_RUN / PAUSE_AUTO_RUN yet, so those go through
# AutoRunner.handle directly; ADVANCE_CLOCK goes through the normal Store command path.
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from pantry.agents.autorun import AutoRunner, advance_command_id
from pantry.agents.worker import Worker
from pantry.llm import FakeLLM
from pantry.models import Command
from pantry.seed import seed_store
from pantry.store.json_store import JsonStore
from tests.conftest import START
from tests.test_e2e_three_days import (  # noqa: F401  (no_mlflow is an autouse fixture)
    DAY1_PLAN,
    DAY2_PLAN,
    DAY3_PLAN,
    RESOLVER_OUTPUT,
    Harness,
    SpyLinkup,
    assert_consistent,
    build_orchestrator,
    no_mlflow,
)

DAY1 = START + timedelta(days=1)
PLANS: dict[date, dict] = {DAY1: DAY1_PLAN, DAY1 + timedelta(days=1): DAY2_PLAN, DAY1 + timedelta(days=2): DAY3_PLAN}
MAX_STEPS = 20


class Loop:
    """One Harness plus an AutoRunner; step() is one iteration of the worker loop."""

    def __init__(self, seed_dir: Path) -> None:
        self.store = JsonStore.memory(start_date=START)
        self.llm = FakeLLM()
        seed_store(self.store, self.llm, seed_dir, START)
        self.llm.register("ResolverOutput", RESOLVER_OUTPUT)
        self.worker = Worker(self.store, build_orchestrator(self.store, self.llm, SpyLinkup()))
        self.h = Harness(self.store, self.worker)
        self.auto = AutoRunner(self.store, self.worker)
        # Same setup as the E2E test: move milk into the 2-day urgency window; Day 1 is START + 1.
        assert self.h.command("ADVANCE_CLOCK", days=1).status == "done"
        assert self.store.get_today() == DAY1

    def control(self, type_: str, **payload: object) -> dict[str, str]:
        return self.auto.handle(Command(command_id=f"ctl-{type_}", type=type_, payload=payload))

    def step(self) -> None:
        plan = PLANS.get(self.store.get_today())
        if plan is not None:
            self.llm.register("PlannerOutput", plan)
        self.worker.process_commands()
        self.worker.tick()
        self.worker.dispatch_pending()
        self.auto.tick()

    def run_until(self, done, label: str) -> None:
        for _ in range(MAX_STEPS):
            if done():
                return
            self.step()
        assert done(), f"[{label}] condition not reached in {MAX_STEPS} steps: {self.store.get_auto_run()}"

    def milk_runs(self) -> list:
        return sorted(self.store.list_runs(limit=1000), key=lambda r: r.created_at)


def paused_on_day3(loop: Loop) -> str:
    loop.control("START_AUTO_RUN")
    loop.run_until(lambda: loop.store.get_auto_run().mode != "running", "to Day 3 pause")
    state = loop.store.get_auto_run()
    runs = loop.milk_runs()
    assert len(runs) == 3, f"expected one Run per day, got {[(r.run_id, r.state) for r in runs]}"
    day3 = runs[-1]
    assert state.mode == "paused" and state.days_completed == 2, state
    assert state.pause_reason == f"pending_confirmation:{day3.run_id}:allergen", state.pause_reason
    assert loop.store.get_today() == DAY1 + timedelta(days=2)
    assert all(r.state == "RECORDED" and r.committed for r in runs[:2]), [(r.state, r.committed) for r in runs[:2]]
    assert day3.pending_confirmation is not None and day3.pending_confirmation.rule == "allergen"
    return day3.run_id


def test_auto_runs_days_1_and_2_then_pauses_on_allergen(seed_dir: Path) -> None:
    loop = Loop(seed_dir)
    paused_on_day3(loop)
    # Paused: further loop iterations change nothing.
    today = loop.store.get_today()
    for _ in range(3):
        loop.step()
    assert loop.store.get_today() == today and loop.store.get_auto_run().mode == "paused"
    assert_consistent(loop.store, "paused on Day 3")


def test_reject_then_restart_finishes_at_max_days(seed_dir: Path) -> None:
    loop = Loop(seed_dir)
    day3_id = paused_on_day3(loop)
    day3_date = loop.store.get_today()

    reject = loop.h.command("REJECT", run_id=day3_id)
    assert reject.status == "done", reject.error
    result = loop.control("START_AUTO_RUN")
    assert result["mode"] == "running" and result["pause_reason"] == ""

    loop.run_until(lambda: loop.store.get_auto_run().mode != "running", "to finish")
    state = loop.store.get_auto_run()
    assert state.mode == "off" and state.pause_reason is None and state.days_completed == 3, state
    assert loop.store.get_today() == day3_date, "auto-run must not advance past max_days"
    assert loop.store.get_run(day3_id).state == "REJECTED"
    assert len(loop.milk_runs()) == 3

    # Further ticks after finishing are no-ops.
    loop.step()
    assert loop.store.get_auto_run().days_completed == 3 and loop.store.get_today() == day3_date
    assert_consistent(loop.store, "after finish")


def test_pause_stops_progress_and_start_continues(seed_dir: Path) -> None:
    loop = Loop(seed_dir)
    loop.control("START_AUTO_RUN")
    loop.run_until(lambda: loop.store.get_auto_run().days_completed >= 1, "Day 1 done")
    paused = loop.control("PAUSE_AUTO_RUN")
    assert paused["mode"] == "paused" and paused["pause_reason"] == "manual"

    today = loop.store.get_today()
    for _ in range(5):
        loop.step()
    state = loop.store.get_auto_run()
    assert loop.store.get_today() == today and state.days_completed == 1 and state.mode == "paused"

    started = loop.control("START_AUTO_RUN")
    assert started["days_completed"] == "1", "START must keep days_completed"
    loop.run_until(lambda: loop.store.get_auto_run().mode != "running", "to Day 3 pause")
    state = loop.store.get_auto_run()
    assert state.mode == "paused" and state.days_completed == 2
    assert state.pause_reason.startswith("pending_confirmation:")
    assert_consistent(loop.store, "pause/resume")


def test_tick_twice_on_finished_day_advances_once(seed_dir: Path) -> None:
    loop = Loop(seed_dir)
    loop.control("START_AUTO_RUN")
    # Finish Day 1 without letting auto-run close it.
    loop.llm.register("PlannerOutput", DAY1_PLAN)
    loop.worker.tick()
    loop.worker.dispatch_pending()
    assert [r.state for r in loop.milk_runs()] == ["RECORDED"]

    loop.auto.tick()
    loop.auto.tick()
    assert loop.store.get_today() == DAY1 + timedelta(days=1)
    assert loop.store.get_auto_run().days_completed == 1
    advances = [cid for cid in (advance_command_id(DAY1.isoformat()), advance_command_id((DAY1 + timedelta(days=1)).isoformat()))
                if loop.store.get_command(cid) is not None]
    assert advances == [advance_command_id(DAY1.isoformat())]
    assert loop.milk_runs()[0].committed
    assert_consistent(loop.store, "double tick")


def test_full_sequence_keeps_ledger_consistent(seed_dir: Path) -> None:
    loop = Loop(seed_dir)
    day3_id = paused_on_day3(loop)
    assert_consistent(loop.store, "Day 3 pause")
    assert loop.h.command("REJECT", run_id=day3_id).status == "done"
    loop.control("START_AUTO_RUN")
    loop.run_until(lambda: loop.store.get_auto_run().mode == "off", "finish")
    assert loop.store.get_auto_run().days_completed == 3
    assert_consistent(loop.store, "full sequence")


def test_start_payload_max_days_and_unknown_command(seed_dir: Path) -> None:
    loop = Loop(seed_dir)
    assert loop.control("START_AUTO_RUN", max_days=1)["max_days"] == "1"
    loop.run_until(lambda: loop.store.get_auto_run().mode != "running", "max_days=1")
    state = loop.store.get_auto_run()
    assert state.mode == "off" and state.days_completed == 1 and loop.store.get_today() == DAY1
    with pytest.raises(ValueError):
        loop.auto.handle(Command(command_id="x", type="APPROVE", payload={}))
