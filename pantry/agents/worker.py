from __future__ import annotations

from datetime import date

from pantry.agents.orchestrator import OneRunOrchestrator
from pantry.models import Command, Run, TriggerEvent
from pantry.store.base import Store


class Worker:
    """Dispatch pending bus events into independent one-Run executions.

    Command handling, approval/rejection, and clock advancement are intentionally
    separate follow-up steps. This class establishes the idempotent event-to-Run
    boundary first.
    """

    def __init__(self, store: Store, orchestrator: OneRunOrchestrator) -> None:
        self.store = store
        self.orchestrator = orchestrator

    def _event_context(self, bus_event) -> TriggerEvent:
        profile = self.store.get_profile()
        today = self.store.get_today()
        constraints = profile.constraints
        allergens = list(profile.allergens)
        items = self.store.list_items()
        return self.orchestrator.sentinel.build_context(
            trigger=bus_event.trigger_type,
            item_name=bus_event.item_name,
            severity=bus_event.severity,
            dedupe_key=bus_event.dedupe_key,
            items=items,
            today=today.isoformat() if isinstance(today, date) else str(today),
            constraints=constraints,
            allergens=allergens,
        )

    def dispatch_pending(self, limit: int = 50) -> list[Run]:
        """Process pending bus events once; duplicate events cannot create duplicate Runs."""
        results: list[Run] = []
        for bus_event in self.store.list_bus_events(status="pending", limit=limit):
            try:
                event = self._event_context(bus_event)
                run, created = self.orchestrator.create_run(event)
                if created:
                    run = self.orchestrator.execute(event)
                self.store.set_bus_event_status(bus_event.dedupe_key, "dispatched")
                results.append(run)
            except Exception:
                # Keep failed processing visible; preserve the original error.
                try:
                    self.store.set_bus_event_status(bus_event.dedupe_key, "failed")
                finally:
                    raise
        return results

    def _dispatch_command(self, command: Command) -> dict[str, str]:
        """Execute one supported command and return its small result payload."""
        if command.type == "FEEDBACK":
            trace_id = str(command.payload["trace_id"])
            outcome = command.payload["outcome"]
            op_id = str(command.payload.get("op_id", command.command_id))
            source = command.payload.get("source", "user")
            feedback = self.store.save_feedback(trace_id, outcome, op_id, source)
            return {"trace_id": feedback.trace_id}

        raise NotImplementedError(
            f"Command type {command.type!r} is not implemented yet"
        )

    def process_commands(self, limit: int = 20) -> list[Command]:
        """Process pending commands once and persist each command status."""
        processed: list[Command] = []

        for command in self.store.poll_commands(limit):
            try:
                self._dispatch_command(command)
                processed.append(self.store.mark_command(command.command_id, "done"))
            except Exception as exc:
                processed.append(
                    self.store.mark_command(
                        command.command_id,
                        "failed",
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )

        return processed

    def execute_event(self, event: TriggerEvent) -> Run:
        """Execute one already-built trigger context."""
        return self.orchestrator.execute(event)
