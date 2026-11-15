"""Small UI helpers for writing commands without importing the worker."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from pantry.models import Command, CommandType, Run
from pantry.store.base import Store


def enqueue_ui_command(
    store: Store,
    command_type: CommandType,
    payload: dict[str, Any] | None = None,
    *,
    command_id: str | None = None,
) -> Command:
    """Enqueue one idempotent UI command and return the stored command."""
    command = Command(
        command_id=command_id or f"ui-{uuid4().hex}",
        type=command_type,
        payload=payload or {},
    )
    return store.enqueue_command(command)


def command_status_row(command: Command) -> dict[str, str]:
    """Return the compact command status shape displayed by Streamlit."""
    return {
        "command_id": command.command_id,
        "type": command.type,
        "status": command.status,
        "error": command.error or "",
    }


def run_status_notice(run: Run) -> tuple[str, str] | None:
    """Return a prominent notice for paused or failed live runs."""
    failed_steps = [step for step in run.steps if step.status == "failed"]
    if failed_steps:
        detail = failed_steps[-1].detail
        error = detail.get("error") or detail.get("reason") or "See the timeline for details."
        return "error", f"Run encountered an error: {error}"
    if run.state == "PENDING_CONFIRMATION" or run.pending_confirmation is not None:
        return "warning", "Run is paused and requires human confirmation."
    if run.state == "GATED":
        return "warning", "Run is at the safety gate; no inventory change has been applied."
    return None
