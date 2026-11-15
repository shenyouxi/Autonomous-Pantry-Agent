from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Sequence

from pantry.llm import LLM
from pantry.models import PlanCacheEntry, PlannerOutput
from pantry.store.base import Store


PLANNER_SYSTEM_PROMPT = (
    "You are the pantry recipe planner. "
    "Return exactly one practical recipe using the available pantry items. "
    "Respect the snapshot's allergens and time constraint.\n"
    "Rules:\n"
    "- Plan for exactly the number of servings given by servings=N in the snapshot, and set servings to N.\n"
    "- Build the dish around the expiring item(s) listed in expiring=.\n"
    "- Use ONLY ingredients listed in inventory=, and never more than the quantity listed there.\n"
    "- Use EXACTLY the inventory unit for every ingredient (g, ml or pcs). Never use clove, cloves, piece, "
    "medium, handful, tbsp or tsp; convert to the inventory unit instead.\n"
    "- Salt, pepper, water and cooking oil are always available and do not need to be in the inventory.\n"
    "- You may use at most ONE ingredient that is missing from the inventory or insufficient in quantity, "
    "and it must be minor (a sauce or garnish component, not the main ingredient)."
)


class Planner:
    def __init__(
        self,
        llm: LLM,
        store: Store | None = None,
        *,
        cache_first: bool | None = None,
    ) -> None:
        self.llm = llm
        self.store = store
        self.cache_first = (
            cache_first
            if cache_first is not None
            else os.getenv("CACHE_FIRST", "").strip().lower()
            in {"1", "true", "yes", "on"}
        )

    @staticmethod
    def plan_cache_key(
        snapshot_text: str,
        constraints: Sequence[str],
        allergens: Sequence[str],
    ) -> str:
        raw = json.dumps(
            {
                "snapshot_text": snapshot_text,
                "constraints": list(constraints),
                "allergens": sorted(allergens),
            },
            sort_keys=True,
        )
        return hashlib.sha256(raw.encode()).hexdigest()

    # Compatibility alias for cache/prewarm tooling.
    _cache_key = plan_cache_key

    def plan(
        self,
        snapshot: str,
        retrieved_trace_ids: Sequence[str] = (),
        *,
        constraints: Sequence[str] = (),
        allergens: Sequence[str] = (),
    ) -> PlannerOutput:
        key = self.plan_cache_key(snapshot, constraints, allergens)

        if self.store is not None and self.cache_first:
            cached = self.store.get_plan_cache(key)
            if cached is not None:
                plan = PlannerOutput.model_validate(cached.plan)
                if retrieved_trace_ids and not plan.used_seed_trace_ids:
                    plan = plan.model_copy(
                        update={"used_seed_trace_ids": list(retrieved_trace_ids)}
                    )
                return plan

        result = self.llm.chat_json(PLANNER_SYSTEM_PROMPT, snapshot, PlannerOutput)
        plan = PlannerOutput.model_validate(result)

        if retrieved_trace_ids and not plan.used_seed_trace_ids:
            plan = plan.model_copy(
                update={"used_seed_trace_ids": list(retrieved_trace_ids)}
            )

        if self.store is not None:
            from datetime import datetime, timezone

            self.store.put_plan_cache(
                PlanCacheEntry(
                    key=key,
                    snapshot_text=snapshot,
                    constraints=list(constraints),
                    allergens=list(allergens),
                    plan=plan,
                    called_at=datetime.now(timezone.utc),
                )
            )

        return plan
