from __future__ import annotations
import logging
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
from pantry.agents.autorun import AutoRunner
from pantry.agents.units import normalise_quantity

logger = logging.getLogger(__name__)

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

                    trace = self.store.get_trace_by_run(run.run_id)
                    if trace is not None and trace.risk_status == "none":
                        self._approve(
                            Command(
                                command_id=f"auto-approve:{run.run_id}",
                                type="APPROVE",
                                payload={"run_id": run.run_id},
                            )
                        )
                        run = self.store.get_run(run.run_id) or run

                self.store.set_bus_event_status(
                    bus_event.dedupe_key,
                    "dispatched",
                )
                results.append(run)
            except Exception:
                # One bad Run must not kill the worker. The bus event is marked
                # "failed" (so it is never listed as pending again and the Run is
                # not retried); the Run itself is left in whatever state it reached.
                logger.exception(
                    "dispatch failed for bus event %r", bus_event.dedupe_key
                )
                try:
                    self.store.set_bus_event_status(
                        bus_event.dedupe_key,
                        "failed",
                    )
                except Exception:
                    logger.exception(
                        "could not mark bus event %r failed", bus_event.dedupe_key
                    )

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
        resolving = run.checkpoints.get("RESOLVING", {})
        resolved_rows = resolving.get("resolved", [])

        substitutions = {}
        for row in resolved_rows:
            gap = row.get("gap", {})
            substitute = row.get("substitute", {})
            substitutions[str(gap.get("name", "")).strip().lower()] = (
                substitute.get("components", [])
            )

        events: list[InventoryEvent] = []

        for ingredient in plan.ingredients:
            name = ingredient.name.strip().lower()
            components = substitutions.get(name)

            if components is not None:
                for component in components:
                    component_name = str(component["name"])
                    item = self.store.get_item(component_name)
                    if item is None and rules.is_staple(component_name):
                        logger.warning(
                            "run %s: not reserving substitute %r for %r, "
                            "staple not tracked in pantry",
                            run.run_id, component_name, name,
                        )
                        continue
                    if item is None:
                        raise ValueError(
                            f"Cannot approve run {run.run_id}: "
                            f"missing substitute item {component_name!r}"
                        )

                    amount = component.get("amount")
                    unit = component.get("unit") or item.unit
                    if amount is None or amount <= 0:
                        continue
                    normalised = normalise_quantity(amount, unit, item.unit)
                    if normalised is None:
                        logger.warning(
                            "run %s: not reserving substitute %r, cannot convert "
                            "%r to pantry unit %r",
                            run.run_id, item.item_name, unit, item.unit,
                        )
                        continue
                    amount = normalised[0]

                    events.append(
                        InventoryEvent(
                            event_id="",
                            item_name=item.item_name,
                            type="RESERVED",
                            delta=float(amount),
                            unit=item.unit,
                            estimated=True,
                            run_id=run.run_id,
                            note=f"reserved substitute for {name}",
                        )
                    )
                continue

            item = self.store.get_item(ingredient.name)
            if item is None:
                raise ValueError(
                    f"Cannot approve run {run.run_id}: "
                    f"missing pantry item {ingredient.name!r}"
                )

            normalised = normalise_quantity(
                ingredient.amount, ingredient.unit, item.unit
            )
            if normalised is None:
                logger.warning(
                    "run %s: not reserving %r, cannot convert %r to pantry unit %r",
                    run.run_id, item.item_name, ingredient.unit, item.unit,
                )
                continue
            amount = normalised[0]

            if amount > 0:
                events.append(
                    InventoryEvent(
                        event_id="",
                        item_name=item.item_name,
                        type="RESERVED",
                        delta=amount,
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
        if command.type in {"START_AUTO_RUN", "PAUSE_AUTO_RUN"}:
            return self.autorun.handle(command)
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
                logger.exception(
                    "command %r (%s) failed", command.command_id, command.type
                )
                try:
                    processed.append(
                        self.store.mark_command(
                            command.command_id,
                            "failed",
                            error=f"{type(exc).__name__}: {exc}",
                        )
                    )
                except Exception:
                    logger.exception(
                        "could not mark command %r failed", command.command_id
                    )

        return processed

    def execute_event(self, event: TriggerEvent) -> Run:
        return self.orchestrator.execute(event)


def build_worker(settings=None):
    from pantry.config import get_settings
    from pantry.llm import FakeLLM, LLMConfigError, OpenNextLLM
    from pantry.store.factory import make_store
    from pantry.tools.linkup import FakeLinkupClient, LinkupClient

    settings = settings or get_settings()
    store = make_store(None, settings)

    try:
        llm = OpenNextLLM(settings)
    except LLMConfigError:
        llm = FakeLLM(embed_model=settings.embed_model)

    try:
        search_client = LinkupClient(settings.linkup_api_key)
    except RuntimeError:
        search_client = FakeLinkupClient()

    orchestrator = OneRunOrchestrator(
        store,
        llm,
        search_client=search_client,
    )
    worker = Worker(store, orchestrator)
    worker.autorun = AutoRunner(store, worker)
    return store, worker


def main() -> None:
    import time
    from pantry.config import get_settings

    settings = get_settings()
    _, worker = build_worker(settings)

    while True:
        worker.process_commands()
        worker.tick()
        worker.dispatch_pending()
        worker.autorun.tick()
        time.sleep(settings.tick_seconds)


if __name__ == "__main__":
    main()
