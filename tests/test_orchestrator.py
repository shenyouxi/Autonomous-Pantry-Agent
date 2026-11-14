from datetime import date

from pantry.agents.orchestrator import OneRunOrchestrator
from pantry.llm import FakeLLM
from pantry.models import Constraints, Ingredient, Severity, TriggerEvent
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
