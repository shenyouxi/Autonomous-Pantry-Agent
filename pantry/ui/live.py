"""Small UI helpers for writing commands without importing the worker."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from pantry.models import Command, CommandType
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
