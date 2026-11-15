from pantry.models import Run, RunStep, Trace
from pantry.store.json_store import JsonStore
from pantry.ui.live import command_status_row, enqueue_ui_command, run_status_notice


def test_ui_enqueues_real_run_command() -> None:
    store = JsonStore.memory()

    command = enqueue_ui_command(
        store,
        "REJECT",
        {"run_id": "run-123"},
        command_id="ui-test-reject",
    )

    assert command.payload == {"run_id": "run-123"}
    assert store.get_command("ui-test-reject") == command


def test_ui_enqueues_feedback_with_real_trace_id() -> None:
    store = JsonStore.memory()
    trace = Trace(
        trace_id="trace:run-123",
        run_id="run-123",
        day_index=0,
        trigger="EXPIRY_IMMINENT",
        snapshot_text="milk expires soon",
        final_outcome="pending_confirmation",
    )
    store.save_trace(trace)

    command = enqueue_ui_command(
        store,
        "FEEDBACK",
        {"trace_id": trace.trace_id, "outcome": "accepted", "source": "user"},
        command_id="ui-test-feedback",
    )

    assert command.payload["trace_id"] == "trace:run-123"
    assert command_status_row(command) == {
        "command_id": "ui-test-feedback",
        "type": "FEEDBACK",
        "status": "pending",
        "error": "",
    }


def test_ui_enqueues_bounded_auto_run() -> None:
    store = JsonStore.memory()

    command = enqueue_ui_command(
        store,
        "START_AUTO_RUN",
        {"max_days": 3},
        command_id="ui-test-auto-run",
    )

    assert command.type == "START_AUTO_RUN"
    assert command.payload == {"max_days": 3}


def _run(state: str, *, steps: list[RunStep] | None = None) -> Run:
    return Run(
        run_id=f"run-{state.lower()}",
        trigger="EXPIRY_IMMINENT",
        item_name="live item",
        state=state,
        steps=steps or [],
        idempotency_key=f"test-{state.lower()}",
    )


def test_ui_highlights_gated_and_pending_runs() -> None:
    assert run_status_notice(_run("GATED")) == (
        "warning",
        "Run is at the safety gate; no inventory change has been applied.",
    )
    assert run_status_notice(_run("PENDING_CONFIRMATION")) == (
        "warning",
        "Run is paused and requires human confirmation.",
    )


def test_ui_surfaces_failed_timeline_step() -> None:
    run = _run(
        "GATED",
        steps=[RunStep(name="approve", status="failed", detail={"error": "unit mismatch"})],
    )

    assert run_status_notice(run) == (
        "error",
        "Run encountered an error: unit mismatch",
    )
