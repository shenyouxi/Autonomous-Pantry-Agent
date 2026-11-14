from datetime import date

import pytest

from pantry.agents.orchestrator import OneRunOrchestrator
from pantry.agents.worker import Worker
from pantry.llm import FakeLLM
from pantry.models import Command, Ingredient, Trace
from pantry.store.json_store import JsonStore


def test_worker_dispatches_one_pending_event_to_one_run():
    store = JsonStore.memory()
    store.seed_pantry([
        Ingredient(item_name="milk", quantity=1, unit="ml", expiry_date=date(2026, 11, 15)),
    ])
    llm = FakeLLM()
    llm.register(
        "PlannerOutput",
        {
            "dish_name": "Milk dish",
            "servings": 1,
            "est_minutes": 10,
            "ingredients": [{"name": "milk", "amount": 1, "unit": "ml"}],
            "steps": ["Prepare milk"],
            "rationale": "Use the expiring item.",
        },
    )
    store.recompute_priorities()
    from pantry import rules

    events = rules.detect_triggers(store.list_items(), store.get_today())
    assert events
    assert store.emit_bus_event(events[0]) is True

    worker = Worker(store, OneRunOrchestrator(store, llm))
    runs = worker.dispatch_pending()

    assert len(runs) == 1
    assert runs[0].state == "GATED"
    assert len(store.list_runs()) == 1
    assert store.list_bus_events(status="pending") == []

    worker.dispatch_pending()
    assert len(store.list_runs()) == 1


def test_worker_marks_bus_event_failed_when_processing_raises():
    store = JsonStore.memory()
    store.seed_pantry([
        Ingredient(
            item_name="milk",
            quantity=1,
            unit="ml",
            expiry_date=date(2026, 11, 15),
        ),
    ])

    store.recompute_priorities()

    from pantry import rules

    events = rules.detect_triggers(store.list_items(), store.get_today())
    assert events
    assert store.emit_bus_event(events[0]) is True

    llm = FakeLLM()
    orchestrator = OneRunOrchestrator(store, llm)
    worker = Worker(store, orchestrator)

    def fail_execute(event):
        raise RuntimeError("simulated orchestration failure")

    orchestrator.execute = fail_execute

    with pytest.raises(RuntimeError, match="simulated orchestration failure"):
        worker.dispatch_pending()

    failed_events = store.list_bus_events(status="failed")

    assert len(failed_events) == 1
    assert failed_events[0].dedupe_key == events[0].dedupe_key
    assert store.list_bus_events(status="pending") == []


def test_worker_processes_feedback_command_and_returns_done():
    store = JsonStore.memory()
    trace = Trace(
        trace_id="trace:feedback-test",
        run_id="run:feedback-test",
        day_index=0,
        trigger="EXPIRY_IMMINENT",
        snapshot_text="feedback test",
        final_outcome="pending_confirmation",
    )
    store.save_trace(trace)

    store.enqueue_command(
        Command(
            command_id="cmd-feedback",
            type="FEEDBACK",
            payload={
                "trace_id": "trace:feedback-test",
                "outcome": "accepted",
                "op_id": "op-feedback",
                "source": "user",
            },
        )
    )

    worker = Worker(store, OneRunOrchestrator(store, FakeLLM()))
    processed = worker.process_commands()

    assert len(processed) == 1
    assert processed[0].command_id == "cmd-feedback"
    assert processed[0].status == "done"
    assert store.get_trace("trace:feedback-test").final_outcome == "accepted"
    assert store.poll_commands() == []


def test_worker_marks_unsupported_command_failed():
    store = JsonStore.memory()
    store.enqueue_command(
        Command(command_id="cmd-approve", type="APPROVE", payload={})
    )

    worker = Worker(store, OneRunOrchestrator(store, FakeLLM()))
    processed = worker.process_commands()

    assert len(processed) == 1
    assert processed[0].command_id == "cmd-approve"
    assert processed[0].status == "failed"
    assert "not implemented" in (processed[0].error or "")
    assert store.poll_commands() == []
