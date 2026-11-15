# Tests for scripts/send_command.py on JsonStore.memory(): each command type is enqueued with the
# payload Worker._dispatch_command expects, invalid arguments exit non-zero, and --list flags pending runs.
from __future__ import annotations

import pytest

from pantry.models import ConfirmationRequest, Run, Trace
from pantry.store.json_store import JsonStore
from scripts import send_command
from tests.conftest import START

RUN_ID = "run:cli-test"
TRACE_ID = "trace:run:cli-test"


@pytest.fixture
def store() -> JsonStore:
    store = JsonStore.memory(start_date=START)
    store.create_run(Run(run_id=RUN_ID, trigger="EXPIRY_IMMINENT", item_name="milk", idempotency_key="k1"))
    store.save_trace(
        Trace(trace_id=TRACE_ID, run_id=RUN_ID, day_index=0, trigger="EXPIRY_IMMINENT",
              snapshot_text="s", final_outcome="pending_confirmation")
    )
    return store


@pytest.mark.parametrize(
    ("argv", "expected_type", "expected_payload"),
    [
        (["ADVANCE_CLOCK"], "ADVANCE_CLOCK", {"days": 1}),
        (["ADVANCE_CLOCK", "--days", "2"], "ADVANCE_CLOCK", {"days": 2}),
        (["APPROVE", "--run-id", RUN_ID], "APPROVE", {"run_id": RUN_ID}),
        (["REJECT", "--run-id", RUN_ID], "REJECT", {"run_id": RUN_ID}),
        (["UNDO_RUN", "--run-id", RUN_ID], "UNDO_RUN", {"run_id": RUN_ID}),
        (
            ["FEEDBACK", "--trace-id", TRACE_ID, "--outcome", "rejected"],
            "FEEDBACK",
            {"trace_id": TRACE_ID, "outcome": "rejected", "source": "user"},
        ),
    ],
)
def test_each_command_is_enqueued_with_expected_payload(store, capsys, argv, expected_type, expected_payload):
    assert send_command.main(argv, store=store) == 0

    pending = store.poll_commands()
    assert len(pending) == 1
    assert pending[0].type == expected_type
    assert pending[0].payload == expected_payload
    assert pending[0].status == "pending"
    out = capsys.readouterr().out
    assert f"command_id={pending[0].command_id}" in out
    assert "status=pending" in out


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["APPROVE"],
        ["FEEDBACK", "--trace-id", TRACE_ID],
        ["FEEDBACK", "--outcome", "accepted"],
        ["ADVANCE_CLOCK", "--days", "0"],
        ["NOT_A_COMMAND"],
    ],
)
def test_invalid_arguments_exit_non_zero(store, argv):
    with pytest.raises(SystemExit) as info:
        send_command.main(argv, store=store)
    assert info.value.code != 0
    assert store.poll_commands() == []


def test_unknown_run_id_is_rejected(store, capsys):
    assert send_command.main(["APPROVE", "--run-id", "run:nope"], store=store) != 0
    assert store.poll_commands() == []
    assert "unknown run id" in capsys.readouterr().err


def test_list_highlights_pending_confirmation(store, capsys):
    store.create_run(Run(run_id="run:done", trigger="EXPIRY_IMMINENT", item_name="eggs", idempotency_key="k2"))
    store.set_pending_confirmation(
        RUN_ID, ConfirmationRequest(action="confirm_plan", evidence="walnut pesto", rule="allergen")
    )

    assert send_command.main(["--list"], store=store) == 0

    lines = capsys.readouterr().out.splitlines()
    pending_line = next(line for line in lines if RUN_ID in line)
    done_line = next(line for line in lines if "run:done" in line)
    assert "PENDING_CONFIRMATION" in pending_line and "rule=allergen" in pending_line
    assert "PENDING_CONFIRMATION" not in done_line
    assert store.poll_commands() == []
