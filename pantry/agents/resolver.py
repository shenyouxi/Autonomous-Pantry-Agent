from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Protocol

from pantry.models import Gap, ResolverAction, ResolverOutput, SearchCacheEntry
from pantry.store.base import Store


class SearchClient(Protocol):
    def search(self, query: str, depth: str = "standard") -> dict: ...


class Resolver:
    """Resolve one missing ingredient through cache, then an injected search client."""

    def __init__(
        self,
        store: Store,
        search_client: SearchClient,
        *,
        extractor: Callable[[str, dict], ResolverOutput] | None = None,
        depth: str = "standard",
    ) -> None:
        self.store = store
        self.search_client = search_client
        self.extractor = extractor or self._default_extract
        self.depth = depth

    @staticmethod
    def _cache_key(query: str, depth: str) -> str:
        raw = json.dumps({"query": query.strip(), "depth": depth}, sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()

    @staticmethod
    def _default_extract(query: str, result: dict) -> ResolverOutput:
        """Deterministic fallback extraction used until an LLM extractor is injected."""
        return ResolverOutput(
            substitute_name=query.split(" substitute for ", 1)[-1].strip(),
            components=[{"name": query.split(" substitute for ", 1)[-1].strip()}],
            source_url=(result.get("sources") or [{}])[0].get("url"),
            confidence="low",
        )

    @staticmethod
    def build_query(gap: Gap, snapshot_text: str = "") -> str:
        context = f" Context: {snapshot_text.strip()}" if snapshot_text.strip() else ""
        return (
            f"Find a safe cooking substitute for {gap.name}, amount {gap.needed} {gap.unit}."
            f" Return a practical substitute and its component ratio.{context}"
        )

    def resolve_gap(self, gap: Gap, snapshot_text: str = "") -> tuple[ResolverOutput, ResolverAction]:
        query = self.build_query(gap, snapshot_text)
        key = self._cache_key(query, self.depth)
        cached = self.store.get_search_cache(key)
        if cached is not None:
            result = cached.response
            output = self.extractor(query, result)
            action = ResolverAction(
                tool="cache",
                query=query,
                answer=str(result.get("answer", "")),
                sources=list(result.get("sources", [])),
                cached_at=cached.called_at,
            )
            return output, action

        result = self.search_client.search(query=query, depth=self.depth)
        called_at = datetime.now(timezone.utc)
        self.store.put_search_cache(
            SearchCacheEntry(
                key=key,
                tool="linkup",
                query=query,
                depth=self.depth,
                response=result,
                called_at=called_at,
            )
        )
        output = self.extractor(query, result)
        action = ResolverAction(
            tool="linkup",
            query=query,
            answer=str(result.get("answer", "")),
            sources=list(result.get("sources", [])),
        )
        return output, action
