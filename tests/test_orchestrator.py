from datetime import date

import pytest

from pantry.agents.orchestrator import OneRunOrchestrator
from pantry.llm import FakeLLM
from pantry.models import (
    Constraints,
    Gap,
    Ingredient,
    PlannerOutput,
    ResolverAction,
    ResolverOutput,
    Severity,
    TriggerEvent,
)
from pantry.store.json_store import JsonStore


def test_one_run_reaches_gate_without_inventory_write():
    store = JsonStore.memory()
    store.seed_pantry([
        Ingredient(item_name="tomato", quantity=4, unit="pcs", expiry_date=date(2026, 11, 16)),
    ])
    llm = FakeLLM()
    llm.register(
        "PlannerOutput",
        {
            "dish_name": "Tomato plate",
            "servings": 1,
            "est_minutes": 10,
            "ingredients": [{"name": "tomato", "amount": 1, "unit": "pcs"}],
            "steps": ["Slice tomato"],
            "rationale": "Use the available tomato.",
            "used_seed_trace_ids": [],
        },
    )
    event = TriggerEvent(
        trigger="EXPIRY_IMMINENT",
        item_name="tomato",
        severity="low",
        dedupe_key="test:tomato:2026-11-14",
        today="2026-11-14",
        items_ranked=store.list_items(),
        constraints=Constraints(max_minutes=30),
        allergens=[],
        snapshot_text="tomato expires soon; max 30 minutes; no allergens",
    )

    result = OneRunOrchestrator(store, llm).execute(event)

    assert result.state == "GATED"
    assert result.pending_confirmation is not None
    assert result.steps
    trace = store.get_trace(f"trace:{result.run_id}")
    assert trace is not None
    assert trace.final_outcome == "pending_confirmation"
    assert trace.embedding
    assert store.list_events(run_id=result.run_id) == []

    repeated = OneRunOrchestrator(store, llm).execute(event)
    assert repeated.run_id == result.run_id
    assert len(store.list_runs()) == 1



class RecordingResolver:
    """Stand-in Resolver that records the gaps the orchestrator sends it."""

    def __init__(self) -> None:
        self.gaps: list[Gap] = []
        self.extractor = None

    def resolve_gap(self, gap: Gap, snapshot_text: str = ""):
        self.gaps.append(gap)
        output = ResolverOutput(
            substitute_name="flax egg",
            components=[{"name": "flaxseed", "amount": 7, "unit": "g"}],
            confidence="high",
        )
        return output, ResolverAction(tool="llm", query=f"substitute for {gap.name}")


EGG_PLAN = {
    "dish_name": "Omelette",
    "servings": 1,
    "est_minutes": 10,
    "ingredients": [{"name": "eggs", "amount": 2, "unit": "pcs"}],
    "steps": ["Whisk and cook"],
    "rationale": "Quick breakfast.",
    "used_seed_trace_ids": [],
}


def _egg_event(store: JsonStore) -> TriggerEvent:
    return TriggerEvent(
        trigger="EXPIRY_IMMINENT",
        item_name="eggs",
        severity="low",
        dedupe_key="test:eggs:2026-11-14",
        today="2026-11-14",
        items_ranked=store.list_items(),
        constraints=Constraints(max_minutes=30),
        allergens=[],
        snapshot_text="eggs; max 30 minutes; no allergens",
    )


@pytest.mark.parametrize("quantity,reserved", [(0, 0), (2, 2)])
def test_tracked_item_with_nothing_available_is_missing_and_goes_to_resolver(quantity, reserved):
    store = JsonStore.memory()
    store.seed_pantry([
        Ingredient(
            item_name="eggs", quantity=quantity, reserved=reserved, unit="pcs",
            expiry_date=date(2026, 11, 20),
        ),
    ])
    llm = FakeLLM()
    llm.register("PlannerOutput", EGG_PLAN)
    resolver = RecordingResolver()

    result = OneRunOrchestrator(store, llm, resolver=resolver).execute(_egg_event(store))

    assert resolver.gaps == [Gap(name="eggs", needed=2, unit="pcs", kind="MISSING")]
    trace = store.get_trace(f"trace:{result.run_id}")
    assert trace is not None and len(trace.resolver_actions) == 1


def test_tracked_item_with_partial_stock_stays_insufficient():
    plan = PlannerOutput.model_validate({**EGG_PLAN, "ingredients": [{"name": "eggs", "amount": 3, "unit": "pcs"}]})
    items = [Ingredient(item_name="eggs", quantity=1, unit="pcs", expiry_date=date(2026, 11, 20))]

    gaps = OneRunOrchestrator._gaps(plan, items, allergens=[])

    assert gaps == [Gap(name="eggs", needed=2.0, unit="pcs", kind="INSUFFICIENT")]
