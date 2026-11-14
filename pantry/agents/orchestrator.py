from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from uuid import uuid4

from pantry.agents.planner import Planner
from pantry.agents.sentinel import Sentinel
from pantry.llm import LLM
from pantry.models import (
    ConfirmationRequest,
    Gap,
    Ingredient,
    Run,
    Trace,
    TriggerEvent,
)
from pantry.rules import clean_name, is_staple, matches_allergen
from pantry.store.base import Store
from pantry.tracing import (
    save_decision_trace,
    trace_id_for_run,
    trace_step,
)


class OneRunOrchestrator:
    """Execute one trigger through planning and the safety gate.

    This first version intentionally stops at confirmation. It does not reserve
    inventory or call the Resolver yet.
    """

    def __init__(
        self,
        store: Store,
        llm: LLM,
        *,
        planner: Planner | None = None,
        sentinel: Sentinel | None = None,
        day_index: int = 0,
    ) -> None:
        self.store = store
        self.llm = llm
        self.planner = planner or Planner(llm)
        self.sentinel = sentinel or Sentinel()
        self.day_index = day_index

    def create_run(self, event: TriggerEvent) -> tuple[Run, bool]:
        run = Run(
            run_id=f"run:{uuid4().hex}",
            trigger=event.trigger,
            item_name=event.item_name,
            idempotency_key=event.dedupe_key,
        )
        return self.store.create_run(run)

    @staticmethod
    def _gaps(plan, items: Sequence[Ingredient], allergens: Sequence[str]) -> list[Gap]:
        available = {clean_name(item.item_name): item for item in items}
        gaps: list[Gap] = []
        for ingredient in plan.ingredients:
            name = clean_name(ingredient.name)
            if matches_allergen(name, allergens):
                gaps.append(Gap(name=name, needed=ingredient.amount, unit=ingredient.unit, kind="MISSING"))
                continue
            item = available.get(name)
            if item is None and not is_staple(name):
                gaps.append(Gap(name=name, needed=ingredient.amount, unit=ingredient.unit, kind="MISSING"))
            elif item is not None and item.quantity < ingredient.amount:
                gaps.append(
                    Gap(
                        name=name,
                        needed=round(ingredient.amount - item.quantity, 6),
                        unit=ingredient.unit,
                        kind="INSUFFICIENT",
                    )
                )
        return gaps

    def execute(self, event: TriggerEvent) -> Run:
        run, _ = self.create_run(event)
        run_id = run.run_id
        profile = self.store.get_profile()
        allergens = list(event.allergens or profile.allergens)
        snapshot_text = event.snapshot_text

        with trace_step(self.store, run_id, "Context") as step:
            step.set(trigger=event.trigger, item_name=event.item_name)
            step.checkpoint("CONTEXT_BUILT", {"snapshot_text": snapshot_text})

        with trace_step(self.store, run_id, "Embedding") as step:
            embedding = self.llm.embed([snapshot_text])[0]
            step.set(dimensions=len(embedding))

        with trace_step(self.store, run_id, "vector_search") as step:
            retrieved, degraded = self.store.search_traces(embedding, event.trigger, k=3)
            step.set(hits=len(retrieved))
            step.checkpoint(
                "SEEDS_RETRIEVED",
                {"embedding_dimensions": len(embedding), "hits": len(retrieved)},
            )
            if degraded:
                step.degrade("vector search degraded to local fallback")

        with trace_step(self.store, run_id, "Planner") as step:
            plan = self.planner.plan(snapshot_text, [item.trace_id for item in retrieved])
            step.set(dish_name=plan.dish_name, servings=plan.servings)
            step.checkpoint(
                "PLANNED",
                {"plan": plan.model_dump(), "retrieved_trace_ids": [item.trace_id for item in retrieved]},
            )

        gaps = self._gaps(plan, event.items_ranked, allergens)
        with trace_step(self.store, run_id, "GapCheck") as step:
            step.set(gaps=len(gaps))
            step.checkpoint("GAP_CHECKED", {"gaps": [gap.model_dump() for gap in gaps]})

        risk_status = "allergen" if any(
            matches_allergen(clean_name(ingredient.name), allergens)
            for ingredient in plan.ingredients
        ) else "none"

        trace = Trace(
            trace_id=trace_id_for_run(run_id),
            run_id=run_id,
            day_index=self.day_index,
            trigger=event.trigger,
            snapshot_text=snapshot_text,
            embedding=embedding,
            constraints=[str(event.constraints.model_dump())],
            plan=plan,
            retrieved_traces=retrieved,
            risk_status=risk_status,
            final_outcome="pending_confirmation",
        )

        with trace_step(self.store, run_id, "Gate") as step:
            step.set(gaps=len(gaps))
            step.checkpoint("GATED", {"gaps": [gap.model_dump() for gap in gaps]})

        save_decision_trace(self.store, trace)
        self.store.set_pending_confirmation(
            run_id,
            ConfirmationRequest(
                action="confirm_plan",
                evidence=f"Proposed meal: {plan.dish_name}",
                rule=trace.risk_status,
            ),
        )
        return self.store.get_run(run_id) or run
