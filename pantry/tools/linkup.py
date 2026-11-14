from __future__ import annotations

import os
from typing import Any, TypedDict

from dotenv import load_dotenv
from linkup import LinkupClient as SdkLinkupClient


class Source(TypedDict):
    title: str
    url: str
    content: str


class LinkupResult(TypedDict):
    answer: str
    sources: list[Source]


def _value(item: Any, key: str, default: str = "") -> str:
    if isinstance(item, dict):
        return str(item.get(key, default) or default)
    return str(getattr(item, key, default) or default)


def _normalize_source(item: Any) -> Source:
    return {
        "title": _value(item, "title") or _value(item, "name"),
        "url": _value(item, "url"),
        "content": _value(item, "content") or _value(item, "snippet"),
    }


class LinkupClient:
    def __init__(self, api_key: str | None = None) -> None:
        load_dotenv(".env")
        api_key = api_key or os.getenv("LINKUP_API_KEY")
        if not api_key:
            raise RuntimeError("LINKUP_API_KEY is missing. Check your .env file.")
        self._client = SdkLinkupClient(api_key=api_key)

    def search(self, query: str, depth: str = "standard") -> LinkupResult:
        response = self._client.search(
            query=query,
            depth=depth,
            output_type="sourcedAnswer",
        )
        answer = _value(response, "answer")
        sources = response.get("sources", []) if isinstance(response, dict) else getattr(response, "sources", [])
        return {"answer": answer, "sources": [_normalize_source(item) for item in sources]}


class FakeLinkupClient:
    def search(self, query: str, depth: str = "standard") -> LinkupResult:
        return {
            "answer": f"Fake answer for: {query}",
            "sources": [
                {
                    "title": "Fake cooking source",
                    "url": "https://example.com/fake-cooking",
                    "content": "This is deterministic fake source content.",
                }
            ],
        }
