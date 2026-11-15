"""Deterministic conversion of plan/substitute units into the pantry item's unit.

No density guessing: a volume unit is never converted into a weight unit or vice
versa. When a conversion is not possible the function returns None and the caller
must exclude that ingredient (and log a warning) instead of failing.
"""
from __future__ import annotations

_COUNT_ALIASES = frozenset({"clove", "cloves", "piece", "pieces", "pc", "pcs", "whole", "each"})

# unit -> (pantry unit, factor)
_FACTORS: dict[str, tuple[str, float]] = {
    "g": ("g", 1.0),
    "gram": ("g", 1.0),
    "grams": ("g", 1.0),
    "kg": ("g", 1000.0),
    "ml": ("ml", 1.0),
    "l": ("ml", 1000.0),
    "tbsp": ("ml", 15.0),
    "tsp": ("ml", 5.0),
}


def normalise_quantity(quantity: float, unit: str | None, pantry_unit: str) -> tuple[float, str] | None:
    """Return (quantity, pantry_unit) expressed in the pantry unit, or None if not convertible."""
    target = (pantry_unit or "").strip().lower()
    source = (unit or "").strip().lower()

    if source == target:
        return float(quantity), pantry_unit
    if target == "pcs" and source in _COUNT_ALIASES:
        return float(quantity), pantry_unit

    conversion = _FACTORS.get(source)
    if conversion is not None and conversion[0] == target:
        return float(quantity) * conversion[1], pantry_unit
    return None
