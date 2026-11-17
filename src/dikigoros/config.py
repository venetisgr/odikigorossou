"""Ρυθμίσεις από μεταβλητές περιβάλλοντος (βλ. ``.env.example``)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _json(name: str) -> dict:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Η {name} δεν είναι έγκυρο JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"Η {name} πρέπει να είναι αντικείμενο JSON")
    return value


def _str(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


@dataclass
class Settings:
    # Ευρετήριο
    index_path: Path = field(default_factory=lambda: Path(_str("DIKIGOROS_INDEX", str(ROOT / "data" / "index.db"))))

    # LLM: οποιοσδήποτε πάροχος με OpenAI-compatible /chat/completions.
    llm_base_url: str = field(default_factory=lambda: _str("LLM_BASE_URL", "https://api.tensorx.ai/v1"))
    llm_api_key: str = field(default_factory=lambda: _str("LLM_API_KEY"))
    llm_model: str = field(default_factory=lambda: _str("LLM_MODEL", "z-ai/glm-5.3-flash"))
    llm_router_model: str = field(default_factory=lambda: _str("LLM_ROUTER_MODEL"))
    llm_extra_body: dict = field(default_factory=lambda: _json("LLM_EXTRA_BODY"))
    llm_router_extra_body: dict = field(default_factory=lambda: _json("LLM_ROUTER_EXTRA_BODY"))
    llm_temperature: float = field(default_factory=lambda: _float("LLM_TEMPERATURE", 0.3))
    llm_max_tokens: int = field(default_factory=lambda: _int("LLM_MAX_TOKENS", 8000))
    llm_router_max_tokens: int = field(default_factory=lambda: _int("LLM_ROUTER_MAX_TOKENS", 4000))
    llm_timeout: float = field(default_factory=lambda: _float("LLM_TIMEOUT", 180))

    # Ανάκτηση
    max_sources: int = field(default_factory=lambda: _int("MAX_SOURCES", 12))
    max_candidates: int = field(default_factory=lambda: _int("MAX_CANDIDATES", 40))
    context_chars: int = field(default_factory=lambda: _int("CONTEXT_CHARS", 120_000))
    history_turns: int = field(default_factory=lambda: _int("HISTORY_TURNS", 4))

    # Ζωντανός έλεγχος στο αρχείο Global law της Lexifina (ισχύς σε ημερομηνία, ιστορικό).
    law_api_base_url: str = field(default_factory=lambda: _str("LAW_API_BASE_URL", "https://global-law.lexifina.com/v1"))
    law_api_key: str = field(default_factory=lambda: _str("LAW_API_KEY"))
    law_api_enabled: bool = field(default_factory=lambda: _str("LAW_API_ENABLED", "1") not in ("0", "false", "no"))
    law_api_per_minute: int = field(default_factory=lambda: _int("LAW_API_PER_MINUTE", 0))
    law_api_per_day: int = field(default_factory=lambda: _int("LAW_API_PER_DAY", 0))

    # Πρόσβαση και προστασία από κατάχρηση
    access_code: str = field(default_factory=lambda: _str("ACCESS_CODE"))
    ask_per_hour: int = field(default_factory=lambda: _int("ASK_PER_HOUR", 40))
    max_question_chars: int = field(default_factory=lambda: _int("MAX_QUESTION_CHARS", 6000))
    cors_origins: str = field(default_factory=lambda: _str("CORS_ORIGINS"))

    @property
    def llm_mode(self) -> str:
        """``fake`` (επίδειξη χωρίς LLM), ``live`` ή ``off`` (δεν έχει οριστεί κλειδί)."""
        if self.llm_base_url.lower() == "fake":
            return "fake"
        return "live" if self.llm_api_key else "off"

    @property
    def router_model(self) -> str:
        return self.llm_router_model or self.llm_model

    @property
    def law_api_limits(self) -> tuple[int, int]:
        """(ανά λεπτό, ανά ημέρα): τα όρια της Lexifina, χωρίς κλειδί ή με κλειδί."""
        minute = self.law_api_per_minute or (30 if self.law_api_key else 10)
        day = self.law_api_per_day or (2000 if self.law_api_key else 100)
        return minute, day
