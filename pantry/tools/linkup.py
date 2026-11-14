

class FakeLinkupClient:
    def search(self, query: str, depth: str = "standard") -> dict:
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
