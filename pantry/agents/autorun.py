# Bounded auto-run for the demo: advances the simulated clock one day at a time, up to max_days,
# pausing whenever a Run needs human confirmation. It never sleeps or spawns threads; the worker
# loop calls tick() once per iteration and routes START_AUTO_RUN / PAUSE_AUTO_RUN to handle().
from __future__ import annotations

from typing import TYPE_CHECKING

from pantry.models import AutoRunState, Command, Run, utc_now
from pantry.store.base import Store

if TYPE_CHECKING:
    from pantry.agents.worker import Worker

# Run states in which a Run is no longer being worked on by the orchestrator. APPLIED runs are
# committed by ADVANCE_CLOCK; REJECTED runs stay non-terminal in the Store but are finished here.
SETTLED_RUN_STATES: frozenset[str] = frozenset({"APPLIED", "RECORDED", "REJECTED"})

DEFAULT_MAX_DAYS = 3


def advance_command_id(day_iso: str) -> str:
    """Deterministic ADVANCE_CLOCK command id for a day, so a day is advanced at most once."""
    return f"autorun:advance:{day_iso}"


def _is_pending(run: Run) -> bool:
    return run.state == "PENDING_CONFIRMATION" or run.pending_confirmation is not None


class AutoRunner:
    """Drives ADVANCE_CLOCK automatically while the auto-run state is running."""

    def __init__(self, store: Store, worker: Worker, max_days: int = DEFAULT_MAX_DAYS) -> None:
        self.store = store
        self.worker = worker
        self.max_days = max_days

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    def handle(self, command: Command) -> dict[str, str]:
        """Apply START_AUTO_RUN or PAUSE_AUTO_RUN to the stored auto-run state."""
        state = self.store.get_auto_run()

        if command.type == "START_AUTO_RUN":
            raw = command.payload.get("max_days")
            max_days = int(raw) if raw is not None else self.max_days
            if max_days < 1:
                raise ValueError("max_days must be positive")
            state = state.model_copy(
                update={"mode": "running", "pause_reason": None, "max_days": max_days}
            )
        elif command.type == "PAUSE_AUTO_RUN":
            state = state.model_copy(update={"mode": "paused", "pause_reason": "manual"})
        else:
            raise ValueError(f"AutoRunner cannot handle command type {command.type!r}")

        return self._summary(self._save(state))

    # ------------------------------------------------------------------
    # Loop step
    # ------------------------------------------------------------------

    def tick(self) -> None:
        """Advance the day once if auto-run is running and today's work is finished."""
        state = self.store.get_auto_run()
        if state.mode != "running":
            return

        if state.days_completed >= state.max_days:
            self._save(state.model_copy(update={"mode": "off", "pause_reason": None}))
            return

        active = [r for r in self.store.list_active_runs() if r.state not in SETTLED_RUN_STATES or _is_pending(r)]

        # 1. A Run waiting for the user pauses auto-run; the user decides, then STARTs again.
        pending = [r for r in active if _is_pending(r)]
        if pending:
            run = pending[0]
            rule = run.pending_confirmation.rule if run.pending_confirmation else "unknown"
            self._pause(state, f"pending_confirmation:{run.run_id}:{rule}")
            return

        # 2. Today's triggers must be detected and every Run finished before the day can close.
        #    worker.tick() is idempotent (bus events dedupe per day), so calling it here only makes
        #    sure a day is never closed before its triggers were emitted.
        if self.worker.tick():
            return
        if active or self.store.list_bus_events(status="pending", limit=1):
            return

        today = self.store.get_today().isoformat()
        failed = [e for e in self.store.list_bus_events(status="failed", limit=1000) if e.dedupe_key.endswith(f":{today}")]
        if failed:
            self._pause(state, f"dispatch_failed:{failed[0].dedupe_key}")
            return

        # 3. The day is finished.
        days_completed = state.days_completed + 1
        if days_completed >= state.max_days:
            self._save(state.model_copy(update={"mode": "off", "pause_reason": None, "days_completed": days_completed}))
            return

        command_id = advance_command_id(today)
        existing = self.store.get_command(command_id)
        if existing is None:
            self.store.enqueue_command(Command(command_id=command_id, type="ADVANCE_CLOCK", payload={"days": 1}))
            self.worker.process_commands()
            existing = self.store.get_command(command_id)

        if existing is None or existing.status != "done":
            status = existing.status if existing else "missing"
            error = (existing.error if existing else None) or status
            self._pause(state, f"advance_failed:{today}:{error}")
            return

        # Re-read: process_commands may have handled a PAUSE_AUTO_RUN queued alongside.
        latest = self.store.get_auto_run()
        self._save(latest.model_copy(update={"days_completed": days_completed}))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _pause(self, state: AutoRunState, reason: str) -> None:
        self._save(state.model_copy(update={"mode": "paused", "pause_reason": reason}))

    def _save(self, state: AutoRunState) -> AutoRunState:
        return self.store.set_auto_run(state.model_copy(update={"updated_at": utc_now()}))

    @staticmethod
    def _summary(state: AutoRunState) -> dict[str, str]:
        return {
            "mode": state.mode,
            "days_completed": str(state.days_completed),
            "max_days": str(state.max_days),
            "pause_reason": state.pause_reason or "",
        }
