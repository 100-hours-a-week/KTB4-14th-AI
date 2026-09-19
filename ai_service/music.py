"""Verify a freely suggested song; never invent a DB ID or a YouTube video ID."""
from __future__ import annotations

from collections import OrderedDict
import time
import unicodedata
from urllib.parse import urlencode

import httpx
from pydantic import ValidationError

from ai_service.errors import ServiceUnavailable
from ai_service.schemas import MusicRecommendation, MusicSuggestion


def normalized(value: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKC", value).casefold() if c.isalnum())


class MusicCatalog:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.cache: OrderedDict[tuple[str, str], tuple[float, MusicRecommendation]] = OrderedDict()

    async def verify(self, suggestion: MusicSuggestion) -> MusicRecommendation | None:
        key = (normalized(suggestion.title), normalized(suggestion.artist))
        if not all(key):
            return None
        cached = self.cache.get(key)
        if cached and cached[0] > time.monotonic():
            self.cache.move_to_end(key)
            return cached[1].model_copy(deep=True)
        try:
            response = await self.client.get(
                "https://itunes.apple.com/search",
                params={"term": f"{suggestion.title} {suggestion.artist}", "media": "music",
                        "entity": "song", "country": "US", "limit": 20},
                timeout=10.0,
            )
            response.raise_for_status()
            records = response.json()["results"]
            if not isinstance(records, list):
                raise ValueError("invalid catalogue response")
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise ServiceUnavailable() from exc
        for record in records:
            if not isinstance(record, dict) or record.get("kind") != "song":
                continue
            title, artist = record.get("trackName"), record.get("artistName")
            if not isinstance(title, str) or not isinstance(artist, str):
                continue
            # Exact normalized title+artist prevents choosing a cover or live version.
            if (normalized(title), normalized(artist)) != key:
                continue
            try:
                song = MusicRecommendation(
                    title=title, artist=artist,
                    youtube_url="https://www.youtube.com/results?" + urlencode({"search_query": f"{artist} {title} official audio"}),
                )
            except ValidationError:
                continue
            self.cache[key] = (time.monotonic() + 600, song)
            self.cache.move_to_end(key)
            if len(self.cache) > 128:
                self.cache.popitem(last=False)
            return song.model_copy(deep=True)
        return None
