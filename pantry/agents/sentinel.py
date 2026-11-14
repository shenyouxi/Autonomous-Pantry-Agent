from __future__ import annotations

from collections.abc import Sequence

from pantry.models import (
    Constraints,
    Ingredient,
    Severity,
    TriggerEvent,
    TriggerType,
)
from pantry.rules import (
    build_snapshot_text,
    plannable_items,
)


class Sentinel:
    """Build deterministic trigger context for one independent Run."""

    def build_context(
        self,
        *,
        trigger: TriggerType,
        item_name: str,
        severity: Severity,
        dedupe_key: str,
        items: Sequence[Ingredient],
        today: str,
        constraints: Constraints,
        allergens: Sequence[str] = (),
    ) -> TriggerEvent:
        ranked = plannable_items(items, today)
        snapshot = build_snapshot_text(
            trigger=trigger,
            items=ranked,
            today=today,
            max_minutes=constraints.max_minutes,
            allergens=allergens,
        )

        return TriggerEvent(
            trigger=trigger,
            item_name=item_name,
            severity=severity,
            dedupe_key=dedupe_key,
            today=today,
            items_ranked=ranked,
            constraints=constraints,
            allergens=list(allergens),
            snapshot_text=snapshot,
        )
