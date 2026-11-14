from datetime import date

from pantry.agents.orchestrator import OneRunOrchestrator
from pantry.agents.worker import Worker
from pantry.llm import FakeLLM
from pantry.models import Ingredient
from pantry.store.json_store import JsonStore


def test_worker_dispatches_one_pending_event_to_one_run():
    store = JsonStore.memory()
    store.seed_pantry([
        Ingredient(item_name="milk", quantity=1, unit="ml", expiry_date=date(2026, 11, 15)),
    ])
    llm = FakeLLM()
    llm.register(
        "PlannerOutput",
        {
            "dish_name": "Milk dish",
            "servings": 1,
            "est_minutes": 10,
            "ingredients": [{"name": "milk", "amount": 1, "unit": "ml"}],
            "steps": ["Prepare milk"],
            "rationale": "Use the expiring item.",
        },
    )
    store.recompute_priorities()
    from pantry import rules

    events = rules.detect_triggers(store.list_items(), store.get_today())
    assert events
    assert store.emit_bus_event(events[0]) is True

    worker = Worker(store, OneRunOrchestrator(store, llm))
    runs = worker.dispatch_pending()

    assert len(runs) == 1
    assert runs[0].state == "GATED"
    assert len(store.list_runs()) == 1
    assert store.list_bus_events(status="pending") == []

    worker.dispatch_pending()
    assert len(store.list_runs()) == 1
