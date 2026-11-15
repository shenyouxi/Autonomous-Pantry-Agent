# Unit tests for pantry/rules.py: priority formula, triggers, expired exclusion, dedupe, names, allergens, snapshot.
# Pure functions only; no network, no .env.
from __future__ import annotations

from datetime import date, timedelta

import pytest

from pantry import rules
from pantry.models import Ingredient

TODAY = date(2026, 11, 14)


def make_item(name: str, quantity: float, days: int | None, safety: float = 0, unit: str = "g", **kw) -> Ingredient:
    expiry = None if days is None else TODAY + timedelta(days=days)
    return Ingredient(item_name=name, quantity=quantity, unit=unit, expiry_date=expiry, safety_stock=safety, **kw)


# --- dates and priority ------------------------------------------------------


def test_days_left_accepts_dates_and_iso_strings():
    assert rules.days_left(date(2026, 11, 16), TODAY) == 2
    assert rules.days_left("2026-11-13", "2026-11-14") == -1
    assert rules.days_left(None, TODAY) is None


@pytest.mark.parametrize("days,expected", [(5, 0.0), (6, 0.0), (3, 0.4), (2, 0.6), (0, 1.0), (-3, 1.0), (None, 0.0)])
def test_expiry_u_is_clamped(days, expected):
    assert rules.expiry_u(days) == pytest.approx(expected)


def test_low_stock_u_uses_physical_quantity():
    assert rules.low_stock_u(200, 200) == 1.0
    assert rules.low_stock_u(201, 200) == 0.0


def test_priority_score_formula():
    milk = make_item("milk", 1000, 2, safety=200, unit="ml")
    assert rules.priority_score(milk, TODAY) == pytest.approx(0.7 * 0.6)
    low = make_item("milk", 100, 2, safety=200, unit="ml")
    assert rules.priority_score(low, TODAY) == pytest.approx(0.7 * 0.6 + 0.3)
    no_expiry = make_item("salt", 10, None, safety=50)
    assert rules.priority_score(no_expiry, TODAY) == pytest.approx(0.3)


def test_reservations_do_not_affect_priority_or_low_stock():
    item = make_item("flour", 300, 100, safety=200, reserved=250)
    assert rules.low_stock_u(item.quantity, item.safety_stock) == 0.0
    assert rules.trigger_types(item, TODAY) == []
    assert rules.priority_score(item, TODAY) == 0.0


# --- triggers ----------------------------------------------------------------


@pytest.mark.parametrize("days,fires", [(3, False), (2, True), (1, True), (0, True), (-1, False)])
def test_expiry_imminent_window(days, fires):
    item = make_item("milk", 1000, days, safety=200, unit="ml")
    assert ("EXPIRY_IMMINENT" in rules.trigger_types(item, TODAY)) is fires


def test_stock_depleted_fires_at_or_below_safety_stock():
    assert rules.trigger_types(make_item("eggs", 2, 14, safety=2, unit="pcs"), TODAY) == ["STOCK_DEPLETED"]
    assert rules.trigger_types(make_item("eggs", 1, 14, safety=2, unit="pcs"), TODAY) == ["STOCK_DEPLETED"]
    assert rules.trigger_types(make_item("eggs", 3, 14, safety=2, unit="pcs"), TODAY) == []


@pytest.mark.parametrize("days", [14, None])
def test_stock_depleted_does_not_fire_for_zero_stock(days):
    item = make_item("heavy cream", 0, days, safety=100, unit="ml")
    assert rules.trigger_types(item, TODAY) == []
    assert rules.detect_triggers([item], TODAY) == []


def test_zero_stock_item_does_not_fire_expiry_imminent():
    item = make_item("milk", 0, 1, safety=200, unit="ml")
    assert rules.trigger_types(item, TODAY) == []


def test_expiry_imminent_needs_available_stock():
    zero = make_item("eggs", 0, 0, safety=2, unit="pcs")
    fully_reserved = make_item("milk", 500, 0, safety=200, unit="ml", reserved=500)
    stocked = make_item("butter", 250, 0, safety=50)
    events = rules.detect_triggers([zero, fully_reserved, stocked], TODAY)
    assert [(e.trigger_type, e.item_name) for e in events] == [("EXPIRY_IMMINENT", "butter")]
    assert events[0].dedupe_key == "EXPIRY_IMMINENT:butter:2026-11-14"


def test_stock_depleted_dedupe_key_is_stable_per_day():
    item = make_item(" Eggs ", 1, 14, safety=2, unit="pcs")
    first = rules.detect_triggers([item], TODAY)
    again = rules.detect_triggers([item], TODAY)
    assert [e.dedupe_key for e in first] == ["STOCK_DEPLETED:eggs:2026-11-14"]
    assert [e.dedupe_key for e in again] == [e.dedupe_key for e in first]
    next_day = rules.detect_triggers([item], TODAY + timedelta(days=1))
    assert [e.dedupe_key for e in next_day] == ["STOCK_DEPLETED:eggs:2026-11-15"]


@pytest.mark.parametrize("label", ["use_by", "best_before", "estimated_quality", "none"])
def test_expired_items_never_trigger_and_are_not_plannable(label):
    expired = make_item("milk", 0, -1, safety=200, unit="ml", date_label=label)
    fresh = make_item("eggs", 10, 14, safety=2, unit="pcs")
    assert rules.trigger_types(expired, TODAY) == []
    assert rules.detect_triggers([expired], TODAY) == []
    assert [i.item_name for i in rules.plannable_items([expired, fresh], TODAY)] == ["eggs"]


def test_detect_triggers_builds_bus_events_with_severity_and_dedupe_key():
    items = [
        make_item("milk", 1000, 0, safety=200, unit="ml"),
        make_item("eggs", 1, 14, safety=2, unit="pcs"),
        make_item("butter", 250, 1, safety=50),
        make_item("flour", 1000, 180, safety=200),
        make_item("heavy cream", 0, 30, safety=100, unit="ml"),
    ]
    events = {(e.trigger_type, e.item_name): e for e in rules.detect_triggers(items, TODAY)}
    assert set(events) == {
        ("EXPIRY_IMMINENT", "milk"),
        ("STOCK_DEPLETED", "eggs"),
        ("EXPIRY_IMMINENT", "butter"),
    }
    assert events[("EXPIRY_IMMINENT", "milk")].severity == "high"
    assert events[("STOCK_DEPLETED", "eggs")].severity == "low"
    assert events[("EXPIRY_IMMINENT", "butter")].severity == "low"
    assert events[("EXPIRY_IMMINENT", "milk")].dedupe_key == "EXPIRY_IMMINENT:milk:2026-11-14"
    assert all(e.status == "pending" for e in events.values())


def test_dedupe_key_is_stable_per_day():
    assert rules.dedupe_key("EXPIRY_IMMINENT", " Milk ", "2026-11-14") == rules.dedupe_key(
        "EXPIRY_IMMINENT", "milk", TODAY
    )
    assert rules.dedupe_key("EXPIRY_IMMINENT", "milk", TODAY) != rules.dedupe_key(
        "EXPIRY_IMMINENT", "milk", TODAY + timedelta(days=1)
    )


# --- names and staples -------------------------------------------------------


def test_normalize_name_uses_aliases():
    assert rules.normalize_name("  Double   Cream ") == "heavy cream"
    assert rules.normalize_name("All-Purpose Flour") == "flour"
    assert rules.normalize_name("egg") == "eggs"
    assert rules.normalize_name("lait", {"lait": "milk"}) == "milk"
    assert rules.normalize_name("tomato") == "tomato"


def test_item_alias_map_and_staples():
    item = make_item("heavy cream", 100, 5, unit="ml", aliases=["Thick Cream"])
    assert rules.normalize_name("thick cream", rules.item_alias_map([item])) == "heavy cream"
    assert rules.is_staple("Sea Salt")
    assert rules.is_staple("water")
    assert not rules.is_staple("milk")


# --- allergens ---------------------------------------------------------------


def test_allergen_map_has_expected_groups():
    assert {"nuts", "peanut", "dairy", "egg", "gluten", "soy", "shellfish", "fish", "sesame"} <= set(rules.ALLERGEN_MAP)


@pytest.mark.parametrize(
    "name,groups",
    [
        ("almond milk", {"nuts"}),
        ("Almond Flour", {"nuts"}),
        ("walnuts", {"nuts"}),
        ("milk", {"dairy"}),
        ("oat milk", set()),
        ("peanut butter", {"peanut"}),
        ("nutmeg", set()),
        ("eggplant", set()),
        ("rice flour", set()),
        ("flour", {"gluten"}),
        ("shellfish", {"shellfish"}),
        ("anchovies", {"fish"}),
    ],
)
def test_allergen_groups_word_boundary_matching(name, groups):
    assert rules.allergen_groups(name) == groups


def test_check_allergens_against_user_groups():
    assert rules.check_allergens("almond milk", ["nuts"]) == ["nuts"]
    assert rules.check_allergens(["oat milk", "sugar"], ["nuts", "dairy"]) == []
    assert rules.check_allergens(["butter", "almond flour"], ["Tree Nuts", "milk"]) == ["dairy", "nuts"]
    assert rules.matches_allergen("kiwi slices", ["kiwi"])
    assert not rules.matches_allergen("kiwifruit", ["kiwi"])


# --- snapshot ----------------------------------------------------------------


def test_snapshot_text_uses_relative_days_and_excludes_expired():
    items = [
        make_item("milk", 1000, 2, safety=200, unit="ml"),
        make_item("eggs", 10, 14, safety=2, unit="pcs"),
        make_item("flour", 1000, 180, safety=200),
        make_item("sugar", 500, 365, safety=100),
        make_item("butter", 250, 20, safety=50),
        make_item("yogurt", 200, -1, unit="g"),
    ]
    text = rules.build_snapshot_text("EXPIRY_IMMINENT", items, TODAY, 25, ["nuts"])
    assert text == (
        "trigger=EXPIRY_IMMINENT; expiring=milk(2d); have=butter,eggs,flour,sugar; constraint=25min; allergens=nuts; "
        "inventory=butter 250 g; eggs 10 pcs; flour 1000 g; milk 1000 ml; sugar 500 g; servings=2"
    )
    assert "2026" not in text and "yogurt" not in text


def test_snapshot_text_is_identical_across_shifted_rounds():
    def round_items(start: date) -> list[Ingredient]:
        return [
            Ingredient(item_name="milk", quantity=1000, unit="ml", expiry_date=start + timedelta(days=2)),
            Ingredient(item_name="eggs", quantity=10, unit="pcs", expiry_date=start + timedelta(days=14)),
        ]

    later = TODAY + timedelta(days=30)
    a = rules.build_snapshot_text("EXPIRY_IMMINENT", round_items(TODAY), TODAY, 25, ["nuts"])
    b = rules.build_snapshot_text("EXPIRY_IMMINENT", round_items(later), later, 25, ["nuts"])
    assert a == b


def test_snapshot_text_lists_depleted_items():
    items = [make_item("eggs", 1, 14, safety=2, unit="pcs"), make_item("flour", 1000, 180, safety=200)]
    text = rules.build_snapshot_text("STOCK_DEPLETED", items, TODAY, 25, [])
    assert text == (
        "trigger=STOCK_DEPLETED; expiring=none; depleted=eggs; have=flour; constraint=25min; allergens=none; "
        "inventory=eggs 1 pcs; flour 1000 g; servings=2"
    )


def test_snapshot_text_appends_available_inventory_deterministically():
    items = [
        make_item("penne", 500, 30, reserved=250),
        make_item("milk", 700, 1, unit="ml"),
        make_item("eggs", 6, 3, unit="pcs"),
        make_item("ham", 100, 5, reserved=100),
        make_item("yogurt", 200, -1),
    ]
    text = rules.build_snapshot_text("EXPIRY_IMMINENT", items, TODAY, 25, [])
    assert text.startswith("trigger=EXPIRY_IMMINENT; expiring=milk(1d); have=eggs,ham,penne; constraint=25min;")
    assert text.endswith("; inventory=eggs 6 pcs; milk 700 ml; penne 250 g; servings=2")
    assert text == rules.build_snapshot_text("EXPIRY_IMMINENT", list(reversed(items)), TODAY, 25, [])
