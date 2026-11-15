# Offline tests for scripts/linkup_demo.py: FakeLLM, FakeLinkupClient and JsonStore.memory(); no network.
from __future__ import annotations

from datetime import date

import pytest

from pantry.llm import FakeLLM
from pantry.models import Ingredient
from pantry.store.json_store import JsonStore
from pantry.tools.linkup import FakeLinkupClient
from scripts import linkup_demo

RESOLVER_OUTPUT = {
    "substitute_name": "flax egg",
    "components": [
        {"name": "flour", "amount": 30, "unit": "g"},
        {"name": "water", "amount": 90, "unit": "ml"},
        {"name": "flaxseed", "amount": 2, "unit": "tbsp"},
    ],
    "ratio_text": "1 tbsp flax + 3 tbsp water per egg",
    "source_url": "https://example.com/fake-cooking",
    "confidence": "medium",
}


def _store() -> JsonStore:
    store = JsonStore.memory(start_date=date(2026, 11, 14))
    store.seed_pantry(
        [
            Ingredient(item_name="flour", quantity=250, unit="g", expiry_date=date(2027, 1, 1)),
            Ingredient(item_name="milk", quantity=500, unit="ml", reserved=500, expiry_date=date(2026, 11, 20)),
        ]
    )
    return store


def _snapshot(store: JsonStore):
    items = [(i.item_name, i.quantity, i.reserved) for i in store.list_items()]
    return items, len(store.list_runs(limit=1000)), len(store.list_traces(limit=1000)), len(store.list_events())


def _run(store, llm, search, cache_first=False):
    lines: list[str] = []
    code = linkup_demo.run_demo(
        store, llm, search, item="eggs", amount=2, unit="pcs", cache_first=cache_first, out=lines.append
    )
    return code, lines


def test_demo_prints_substitute_components_and_sources_without_writing():
    store = _store()
    before = _snapshot(store)
    llm = FakeLLM()
    llm.register("ResolverOutput", RESOLVER_OUTPUT)

    code, lines = _run(store, llm, FakeLinkupClient())
    text = "\n".join(lines)

    assert code == 0
    assert "gap: eggs | 2 | pcs | MISSING" in text
    assert "inventory=flour 250 g" in text and "milk" not in text.split("context:")[1].splitlines()[0]
    assert "substitute: flax egg" in text
    assert "  flour | 30 | g | yes" in text
    assert "  water | 90 | ml | yes (staple)" in text
    assert "  flaxseed | 2 | tbsp | no (not in pantry)" in text
    assert "Fake cooking source - https://example.com/fake-cooking" in text
    assert "store writes: search_cache=1" in text
    assert _snapshot(store) == before


def test_second_run_with_cache_first_hits_search_cache():
    store = _store()
    llm = FakeLLM()
    llm.register("ResolverOutput", RESOLVER_OUTPUT)
    _run(store, llm, FakeLinkupClient())

    class NoSearch:
        def search(self, query, depth="standard"):
            raise AssertionError("Linkup must not be called on a cache hit")

    code, lines = _run(store, llm, NoSearch(), cache_first=True)
    assert code == 0
    assert any("search_cache hit" in line for line in lines)


def test_linkup_failure_exits_non_zero():
    class Down:
        def search(self, query, depth="standard"):
            raise ConnectionError("unreachable")

    llm = FakeLLM()
    llm.register("ResolverOutput", RESOLVER_OUTPUT)
    code, lines = _run(_store(), llm, Down())
    assert code == linkup_demo.EXIT_CALL
    assert lines[-1].startswith("FAIL Linkup search failed")


def test_llm_failure_exits_non_zero():
    code, lines = _run(_store(), FakeLLM(), FakeLinkupClient())  # nothing registered -> chat_json raises
    assert code == linkup_demo.EXIT_CALL
    assert lines[-1].startswith("FAIL LLM extraction failed")


def test_read_only_store_blocks_other_writes():
    ro = linkup_demo.ReadOnlyStore(_store())
    with pytest.raises(linkup_demo.StoreWriteBlocked):
        ro.apply_events([], "k")
    assert ro.get_item("flour") is not None


def test_main_fails_cleanly_when_clients_unavailable(monkeypatch, capsys):
    def boom():
        raise linkup_demo.LinkupUnavailable("Linkup unavailable: RuntimeError: LINKUP_API_KEY is missing.")

    monkeypatch.setattr(linkup_demo, "build_real_clients", boom)
    assert linkup_demo.main([]) == linkup_demo.EXIT_SETUP
    assert "FAIL Linkup unavailable" in capsys.readouterr().out
