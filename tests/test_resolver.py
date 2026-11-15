from pantry.agents.resolver import Resolver
from pantry.models import Gap
from pantry.store.json_store import JsonStore
from pantry.tools.linkup import FakeLinkupClient


def test_resolver_searches_and_caches_result():
    store = JsonStore.memory()
    calls = []

    class Client:
        def search(self, query, depth="standard"):
            calls.append((query, depth))
            return FakeLinkupClient().search(query, depth)

    resolver = Resolver(store, Client(), cache_first=True)
    gap = Gap(name="egg", needed=1, unit="pcs", kind="MISSING")

    first, first_action = resolver.resolve_gap(gap)
    second, second_action = resolver.resolve_gap(gap)

    assert len(calls) == 1
    assert first_action.tool == "linkup"
    assert second_action.tool == "cache"
    assert first.substitute_name
    assert second.substitute_name == first.substitute_name


def test_resolver_uses_injected_extractor():
    store = JsonStore.memory()
    resolver = Resolver(
        store,
        FakeLinkupClient(),
        extractor=lambda query, result: __import__("pantry.models", fromlist=["ResolverOutput"]).ResolverOutput(
            substitute_name="yogurt",
            components=[{"name": "yogurt", "amount": 1, "unit": "g"}],
            confidence="high",
        ),
    )

    output, action = resolver.resolve_gap(Gap(name="egg", needed=1, unit="pcs", kind="MISSING"))

    assert output.substitute_name == "yogurt"
    assert action.tool == "linkup"
