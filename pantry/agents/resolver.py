from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, Protocol

from pantry.models import Gap, ResolverAction, ResolverOutput, SearchCacheEntry
from pantry.store.base import Store


class SearchClient(Protocol):
    def search(self, query: str, depth: str = "standard") -> dict: ...


class Resolver:
    """Resolve one missing ingredient through configurable search caching."""

    def __init__(
        self,
        store: Store,
        search_client: SearchClient,
        *,
        extractor: Callable[[str, dict], ResolverOutput] | None = None,
        depth: str = "standard",
        cache_first: bool | None = None,
    ) -> None:
        self.store = store
        self.search_client = search_client
        self.extractor = extractor or self._default_extract
        self.depth = depth
        self.cache_first = (
            cache_first
            if cache_first is not None
            else (
                os.getenv("CACHE_FIRST", "").strip().lower()
                in {"1", "true", "yes", "on"}
                if os.getenv("CACHE_FIRST", "").strip()
                else True
            )
        )

    @staticmethod
    def _normalise_query(query: str) -> str:
        return re.sub(r"\\s+", " ", query).strip().lower()

    @classmethod
    def _cache_key(cls, query: str, depth: str) -> str:
        raw = json.dumps(
            {
                "query": cls._normalise_query(query),
                "depth": depth,
            },
            sort_keys=True,
        )
        return hashlib.sha256(raw.encode()).hexdigest()

    @staticmethod
    def _default_extract(query: str, result: dict) -> ResolverOutput:
        name = query.split(" substitute for ", 1)[-1].strip()
        return ResolverOutput(
            substitute_name=name,
            components=[{"name": name}],
            source_url=(result.get("sources") or [{}])[0].get("url"),
            confidence="low",
        )

    @staticmethod
    def build_query(gap: Gap, snapshot_text: str = "") -> str:
        context = f" Context: {snapshot_text.strip()}" if snapshot_text.strip() else ""
        return (
            f"Find a safe cooking substitute for {gap.name}, amount "
            f"{gap.needed} {gap.unit}. Return a practical substitute and "
            f"its component ratio.{context}"
        )

    def resolve_gap(
        self,
        gap: Gap,
        snapshot_text: str = "",
    ) -> tuple[ResolverOutput, ResolverAction]:
        query = self.build_query(gap, snapshot_text)
        key = self._cache_key(query, self.depth)
        cached = self.store.get_search_cache(key) if self.cache_first else None

        if cached is not None:
            result = dict(cached.response)
            raw_output = result.get("_resolver_output")
            if raw_output is not None:
                output = ResolverOutput.model_validate(raw_output)
            else:
                output = self.extractor(query, result)
                result["_resolver_output"] = output.model_dump(mode="json")
                self.store.put_search_cache(
                    cached.model_copy(update={"response": result})
                )

            return output, ResolverAction(
                tool="cache",
                query=query,
                answer=str(result.get("answer", "")),
                sources=list(result.get("sources", [])),
                cached_at=cached.called_at,
            )

        result = dict(self.search_client.search(query=query, depth=self.depth))
        output = self.extractor(query, result)
        result["_resolver_output"] = output.model_dump(mode="json")
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

        return output, ResolverAction(
            tool="linkup",
            query=query,
            answer=str(result.get("answer", "")),
            sources=list(result.get("sources", [])),
        )
