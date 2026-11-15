from __future__ import annotations

import logging
from collections.abc import Sequence
import inspect
from datetime import date
from uuid import uuid4
from pantry.agents.resolver import Resolver
from pantry.agents.units import normalise_quantity
from pantry.tools.linkup import FakeLinkupClient
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
    ResolverOutput,
)
from pantry.rules import clean_name, is_staple, matches_allergen
from pantry.store.base import Store
from pantry.tracing import (
    save_decision_trace,
    trace_id_for_run,
    trace_step,
)

logger = logging.getLogger(__name__)


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
        resolver: Resolver | None = None,
        search_client=None,
        cache_first: bool | None = None,
        day_index: int = 0,
    ) -> None:
        self.store = store
        self.llm = llm
        self.planner = planner or Planner(llm, store, cache_first=cache_first)
        self.sentinel = sentinel or Sentinel()
        if resolver is None:
            self.resolver = Resolver(
                store,
                search_client or FakeLinkupClient(),
                extractor=self._extract_resolver_output,
                cache_first=cache_first,
            )
        else:
            self.resolver = resolver
            self.resolver.extractor = self._extract_resolver_output
        self.day_index = day_index

    def _extract_resolver_output(
        self,
        query: str,
        result: dict,
    ) -> ResolverOutput:
        system = (
            "You are a cooking substitution extractor. "
            "Extract one practical substitute and its component amounts "
            "from the sourced answer. Return only the requested schema."
        )
        user = (
            f"Query: {query}\n"
            f"Answer: {result.get('answer', '')}\n"
            f"Sources: {result.get('sources', [])}"
        )
        return ResolverOutput.model_validate(
            self.llm.chat_json(system, user, ResolverOutput)
        )

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
            elif item is not None:
                normalised = normalise_quantity(ingredient.amount, ingredient.unit, item.unit)
                if normalised is None:
                    logger.warning(
                        "gap check: skipping %r, cannot convert %r to pantry unit %r",
                        name, ingredient.unit, item.unit,
                    )
                    continue
                amount, unit = normalised
                if item.quantity < amount:
                    gaps.append(
                        Gap(
                            name=name,
                            needed=round(amount - item.quantity, 6),
                            unit=unit,
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
            planner_args = (
                snapshot_text,
                [item.trace_id for item in retrieved],
            )
            planner_kwargs = {
                "constraints": [str(event.constraints.model_dump())],
                "allergens": allergens,
            }
            if "constraints" in inspect.signature(self.planner.plan).parameters:
                plan = self.planner.plan(*planner_args, **planner_kwargs)
            else:
                plan = self.planner.plan(*planner_args)
            step.set(dish_name=plan.dish_name, servings=plan.servings)
            step.checkpoint(
                "PLANNED",
                {"plan": plan.model_dump(), "retrieved_trace_ids": [item.trace_id for item in retrieved]},
            )

        gaps = self._gaps(plan, event.items_ranked, allergens)
        with trace_step(self.store, run_id, "GapCheck") as step:
            step.set(gaps=len(gaps))
            step.checkpoint("GAP_CHECKED", {"gaps": [gap.model_dump() for gap in gaps]})
            resolver_actions = []
        missing_gaps = [gap for gap in gaps if gap.kind == "MISSING"]

        if missing_gaps:
            with trace_step(self.store, run_id, "Resolver") as step:
                step.set(gaps=len(missing_gaps))
                resolved = []

                for gap in missing_gaps:
                    output, action = self.resolver.resolve_gap(
                        gap,
                        snapshot_text,
                    )
                    resolver_actions.append(action)
                    resolved.append(
                        {
                            "gap": gap.model_dump(),
                            "substitute": output.model_dump(),
                        }
                    )

                step.checkpoint(
                    "RESOLVING",
                    {"resolved": resolved},
                )
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
        resolver_actions=resolver_actions,
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
