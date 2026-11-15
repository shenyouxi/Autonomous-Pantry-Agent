import logging
from datetime import date

import pytest

from pantry.agents.orchestrator import OneRunOrchestrator
from pantry.agents.units import normalise_quantity
from pantry.agents.worker import Worker
from pantry.llm import FakeLLM
from pantry.models import Ingredient, PlannerOutput, Run, RunStep
from pantry.store.json_store import JsonStore


# ---------------------------------------------------------------------------
# normalise_quantity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "quantity, unit, pantry_unit, expected",
    [
        (2, "cloves", "pcs", (2.0, "pcs")),  # garlic
        (0.5, "piece", "pcs", (0.5, "pcs")),  # lemon
        (1, "tbsp", "ml", (15.0, "ml")),  # olive oil
        (1, "tsp", "ml", (5.0, "ml")),
        (3, " PCS ", "pcs", (3.0, "pcs")),
        (1, "Whole", "pcs", (1.0, "pcs")),
        (1, "each", "pcs", (1.0, "pcs")),
        (1.5, "kg", "g", (1500.0, "g")),
        (0.25, "L", "ml", (250.0, "ml")),
        (200, "grams", "g", (200.0, "g")),
        (1, "gram", "g", (1.0, "g")),
        (100, "ml", "ml", (100.0, "ml")),
    ],
)
def test_normalise_quantity_converts(quantity, unit, pantry_unit, expected):
    assert normalise_quantity(quantity, unit, pantry_unit) == expected


@pytest.mark.parametrize(
    "unit, pantry_unit",
    [
        ("tbsp", "g"),  # parsley: no density guessing
        ("tsp", "g"),
        ("cloves", "g"),
        ("g", "ml"),
        ("ml", "pcs"),
        ("handful", "g"),
        ("", "g"),
        (None, "pcs"),
    ],
)
def test_normalise_quantity_returns_none_when_not_convertible(unit, pantry_unit):
    assert normalise_quantity(1, unit, pantry_unit) is None


# ---------------------------------------------------------------------------
# The four real mismatches from the crashed real-LLM run
# ---------------------------------------------------------------------------

REAL_PLAN = {
    "dish_name": "Garlic lemon pasta",
    "servings": 1,
    "est_minutes": 15,
    "ingredients": [
        {"name": "garlic", "amount": 2, "unit": "cloves"},
        {"name": "lemon", "amount": 0.5, "unit": "piece"},
        {"name": "parsley", "amount": 1, "unit": "tbsp"},
        {"name": "olive oil", "amount": 1, "unit": "tbsp"},
    ],
    "steps": ["Cook"],
}

PANTRY = [
    Ingredient(item_name="garlic", quantity=5, unit="pcs", expiry_date=date(2026, 11, 30)),
    Ingredient(item_name="lemon", quantity=2, unit="pcs", expiry_date=date(2026, 11, 30)),
    Ingredient(item_name="parsley", quantity=30, unit="g", expiry_date=date(2026, 11, 30)),
    Ingredient(item_name="olive oil", quantity=500, unit="ml", expiry_date=date(2027, 6, 1)),
]


def _store_with_planned_run() -> tuple[JsonStore, Run]:
    store = JsonStore.memory()
    store.seed_pantry(PANTRY)
    run, _ = store.create_run(
        Run(run_id="run:units", trigger="EXPIRY_IMMINENT", item_name="garlic", idempotency_key="k:units")
    )
    run = store.save_checkpoint(
        run.run_id,
        "PLANNED",
        {"plan": PlannerOutput.model_validate(REAL_PLAN).model_dump()},
        RunStep(name="Planner", status="ok"),
    )
    return store, run


def test_reservation_events_normalise_real_plan_units(caplog):
    store, run = _store_with_planned_run()
    worker = Worker(store, OneRunOrchestrator(store, FakeLLM()))

    with caplog.at_level(logging.WARNING, logger="pantry.agents.worker"):
        events = worker._reservation_events(run)  # must not raise

    reserved = {event.item_name: (event.delta, event.unit) for event in events}
    assert reserved == {
        "garlic": (2.0, "pcs"),
        "lemon": (0.5, "pcs"),
        "olive oil": (15.0, "ml"),
    }
    assert "parsley" not in reserved
    warning = " ".join(r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
    assert "parsley" in warning and "tbsp" in warning and "'g'" in warning


def test_gap_check_normalises_units_and_excludes_unconvertible(caplog):
    plan = PlannerOutput.model_validate(REAL_PLAN)
    with caplog.at_level(logging.WARNING, logger="pantry.agents.orchestrator"):
        gaps = OneRunOrchestrator._gaps(plan, PANTRY, allergens=[])

    assert gaps == []
    warning = " ".join(r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
    assert "parsley" in warning and "tbsp" in warning


def test_gap_check_reports_insufficient_in_pantry_unit():
    plan = PlannerOutput.model_validate(
        {**REAL_PLAN, "ingredients": [{"name": "olive oil", "amount": 1, "unit": "l"}]}
    )
    gaps = OneRunOrchestrator._gaps(plan, PANTRY, allergens=[])

    assert len(gaps) == 1
    assert gaps[0].kind == "INSUFFICIENT"
    assert gaps[0].needed == 500.0
    assert gaps[0].unit == "ml"
