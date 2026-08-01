from __future__ import annotations

from typing import Any, Dict


def execute(url: str, *, max_chars: int = 2000) -> Dict[str, Any]:
    """Return a lightweight, deterministic page-extraction payload for local workflows."""
    return {
        "url": url,
        "max_chars": max_chars,
        "title": "Synthetic page",
        "content": f"Synthetic extracted content from {url}",
        "metadata": {
            "source": "local-capability",
            "char_count": min(max_chars, len(f"Synthetic extracted content from {url}")),
        },
    }
