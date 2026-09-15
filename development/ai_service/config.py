from __future__ import annotations

from dataclasses import dataclass
import os


@dataclass(frozen=True)
class Settings:
    api_token: str | None
    mode: str
    gemini_api_key: str | None
    gemini_itinerary_model: str

    @classmethod
    def from_env(cls) -> "Settings":
        mode = os.getenv("AUDIGO_MODE", "demo").lower()
        if mode not in {"demo", "live"}:
            raise RuntimeError("AUDIGO_MODE must be either demo or live")
        api_token = os.getenv("AUDIGO_API_TOKEN")
        if api_token and len(api_token) < 32:
            raise RuntimeError("AUDIGO_API_TOKEN must be at least 32 characters")
        return cls(
            api_token=api_token,
            mode=mode,
            gemini_api_key=os.getenv("GEMINI_API_KEY"),
            gemini_itinerary_model=os.getenv(
                "GEMINI_ITINERARY_MODEL", "gemini-3.5-flash-lite"
            ),
        )
