from __future__ import annotations
from pantry import rules
from pantry.models import BusEvent
from datetime import date
from typing import Any

from pantry.agents.orchestrator import OneRunOrchestrator
from pantry.models import (
    Command,
    InventoryEvent,
    PlannerOutput,
    Run,
    RunStep,
    TriggerEvent,
)
from pantry.store.base import Store
from pantry.tracing import save_decision_trace, trace_id_for_run


class Worker:
    """Dispatch bus events and process worker commands."""

    def __init__(self, store: Store, orchestrator: OneRunOrchestrator) -> None:
        self.store = store
        self.orchestrator = orchestrator

    def _event_context(self, bus_event) -> TriggerEvent:
        profile = self.store.get_profile()
        today = self.store.get_today()
        return self.orchestrator.sentinel.build_context(
            trigger=bus_event.trigger_type,
            item_name=bus_event.item_name,
            severity=bus_event.severity,
            dedupe_key=bus_event.dedupe_key,
            items=self.store.list_items(),
            today=today.isoformat() if isinstance(today, date) else str(today),
            constraints=profile.constraints,
            allergens=list(profile.allergens),
        )
    def tick(self) -> list[BusEvent]:
        """Detect today's triggers and emit each new event once."""
        today = self.store.get_today()
        detected = rules.detect_triggers(
            self.store.list_items(),
            today,
        )

        emitted: list[BusEvent] = []
        for event in detected:
            if self.store.emit_bus_event(event):
                emitted.append(event)

        return emitted
    def dispatch_pending(self, limit: int = 50) -> list[Run]:
        results: list[Run] = []

        for bus_event in self.store.list_bus_events(status="pending", limit=limit):
            try:
                event = self._event_context(bus_event)
                run, created = self.orchestrator.create_run(event)

                if created:
                    run = self.orchestrator.execute(event)

                self.store.set_bus_event_status(
                    bus_event.dedupe_key,
                    "dispatched",
                )
                results.append(run)
            except Exception:
                try:
                    self.store.set_bus_event_status(
                        bus_event.dedupe_key,
                        "failed",
                    )
                finally:
                    raise

        return results

    @staticmethod
    def _run_id(command: Command) -> str:
        run_id = command.payload.get("run_id")
        if not run_id:
            raise ValueError("command payload requires run_id")
        return str(run_id)

    def _trace_id(self, run_id: str) -> str:
        return trace_id_for_run(run_id)

    def _planned_output(self, run) -> PlannerOutput:
        checkpoint = run.checkpoints.get("PLANNED")
        if not checkpoint or "plan" not in checkpoint:
            raise ValueError(f"Run {run.run_id} has no PLANNED checkpoint")
        return PlannerOutput.model_validate(checkpoint["plan"])

    def _reservation_events(self, run) -> list[InventoryEvent]:
        plan = self._planned_output(run)
        events: list[InventoryEvent] = []

        for ingredient in plan.ingredients:
            item = self.store.get_item(ingredient.name)
            if item is None:
                raise ValueError(
                    f"Cannot approve run {run.run_id}: "
                    f"missing pantry item {ingredient.name!r}"
                )

            if ingredient.unit != item.unit:
                raise ValueError(
                    f"Cannot approve run {run.run_id}: unit mismatch for "
                    f"{item.item_name!r}"
                )

            if ingredient.amount <= 0:
                continue

            events.append(
                InventoryEvent(
                    event_id="",
                    item_name=item.item_name,
                    type="RESERVED",
                    delta=ingredient.amount,
                    unit=item.unit,
                    estimated=ingredient.estimated,
                    run_id=run.run_id,
                    note=f"reserved for {plan.dish_name}",
                )
            )

        if not events:
            raise ValueError(f"Run {run.run_id} has no reservable ingredients")

        return events

    def _refresh_trace(self, run_id: str, outcome: str) -> None:
        trace = self.store.get_trace_by_run(run_id)
        if trace is None:
            raise ValueError(f"No decision trace found for run {run_id}")

        updated = trace.model_copy(update={"final_outcome": outcome})
        save_decision_trace(self.store, updated)

    def _approve(self, command: Command) -> dict[str, str]:
        run_id = self._run_id(command)
        run = self.store.get_run(run_id)

        if run is None:
            raise ValueError(f"Unknown run {run_id}")

        trace_id = self._trace_id(run_id)

        if run.state == "RECORDED":
            return {"trace_id": trace_id}

        if run.state == "APPLIED":
            self.store.save_feedback(
                trace_id,
                "accepted",
                f"auto-accept:{run_id}",
                "auto",
            )
            return {"trace_id": trace_id}

        events = self._reservation_events(run)
        self.store.apply_events(events, idempotency_key=run_id)

        self.store.save_checkpoint(
            run_id,
            "APPLIED",
            {"event_ids": [event.event_id for event in events]},
            RunStep(
                name="ApplyInventory",
                status="ok",
                detail={"event_count": len(events)},
            ),
        )
        self.store.set_pending_confirmation(run_id, None)

        self.store.save_feedback(
            trace_id,
            "accepted",
            f"auto-accept:{run_id}",
            "auto",
        )

        self.store.save_checkpoint(
            run_id,
            "RECORDED",
            {"trace_id": trace_id, "outcome": "accepted"},
            RunStep(
                name="Recorded",
                status="ok",
                detail={"trace_id": trace_id},
            ),
        )
        self._refresh_trace(run_id, "accepted")

        return {"trace_id": trace_id}

    def _reject(self, command: Command) -> dict[str, str]:
        run_id = self._run_id(command)
        run = self.store.get_run(run_id)

        if run is None:
            raise ValueError(f"Unknown run {run_id}")

        trace_id = self._trace_id(run_id)

        if run.state == "RECORDED" and run.committed:
            raise ValueError(
                f"Run {run_id} is already committed and cannot be undone"
            )

        if run.state == "APPLIED" or (
            run.state == "RECORDED" and "APPLIED" in run.checkpoints
        ):
            self.store.reverse_run_events(run_id)

        self.store.set_pending_confirmation(run_id, None)
        self.store.save_checkpoint(
            run_id,
            "REJECTED",
            {"trace_id": trace_id, "outcome": "rejected"},
            RunStep(
                name="Rejected",
                status="ok",
                detail={"trace_id": trace_id},
            ),
        )

        self.store.save_feedback(
            trace_id,
            "rejected",
            f"user-reject:{command.command_id}",
            "user",
        )
        self._refresh_trace(run_id, "rejected")

        return {"trace_id": trace_id}

    def _advance_clock(self, command: Command) -> dict[str, str]:
        pending = [
            run
            for run in self.store.list_active_runs()
            if run.pending_confirmation is not None
            or run.state == "PENDING_CONFIRMATION"
        ]
        if pending:
            raise ValueError(
                "Cannot advance clock while a run is pending confirmation: "
                + ", ".join(run.run_id for run in pending)
            )

        for run in self.store.list_uncommitted_applied_runs():
            self.store.commit_run(run.run_id)

        days = int(command.payload.get("days", 1))
        if days < 1:
            raise ValueError("days must be positive")

        today = self.store.advance_day(days)
        return {"today": today.isoformat()}

    def _dispatch_command(self, command: Command) -> dict[str, str]:
        if command.type == "FEEDBACK":
            trace_id = str(command.payload["trace_id"])
            outcome = command.payload["outcome"]
            op_id = str(command.payload.get("op_id", command.command_id))
            source = command.payload.get("source", "user")
            feedback = self.store.save_feedback(
                trace_id,
                outcome,
                op_id,
                source,
            )
            return {"trace_id": feedback.trace_id}

        if command.type == "APPROVE":
            return self._approve(command)

        if command.type in {"REJECT", "UNDO_RUN"}:
            return self._reject(command)

        if command.type == "ADVANCE_CLOCK":
            return self._advance_clock(command)

        raise NotImplementedError(
            f"Command type {command.type!r} is not implemented yet"
        )

    def process_commands(self, limit: int = 20) -> list[Command]:
        processed: list[Command] = []

        for command in self.store.poll_commands(limit):
            try:
                self._dispatch_command(command)
                processed.append(
                    self.store.mark_command(command.command_id, "done")
                )
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
        return self.orchestrator.execute(event)
