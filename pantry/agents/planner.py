from __future__ import annotations

from collections.abc import Sequence

from pantry.llm import LLM
from pantry.models import PlannerOutput


class Planner:
    def __init__(self, llm: LLM) -> None:
        self.llm = llm

    def plan(
        self,
        snapshot: str,
        retrieved_trace_ids: Sequence[str] = (),
    ) -> PlannerOutput:
        system = (
            "You are the pantry recipe planner. "
            "Return exactly one practical recipe using the available pantry items. "
            "Respect the snapshot's allergens and time constraint."
        )
        result = self.llm.chat_json(
            system,
            snapshot,
            PlannerOutput,
        )
        plan = PlannerOutput.model_validate(result)

        if retrieved_trace_ids and not plan.used_seed_trace_ids:
            plan = plan.model_copy(
                update={"used_seed_trace_ids": list(retrieved_trace_ids)}
            )

        return plan

