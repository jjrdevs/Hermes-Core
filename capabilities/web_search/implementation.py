from __future__ import annotations

from typing import Any, Dict


def execute(query: str, *, max_results: int = 5) -> Dict[str, Any]:
    """Return a lightweight, deterministic web-search payload for local workflows.

    This intentionally avoids external network dependencies so the capability can be
    exercised in tests and local demos while still exposing a compatible shape.
    """
    return {
        "query": query,
        "max_results": max_results,
        "results": [
            {
                "title": f"Result {index}",
                "url": f"https://example.test/search/{index}",
                "snippet": f"Synthetic search result for {query}",
            }
            for index in range(1, max_results + 1)
        ],
    }
