"""Κλήσεις σε LLM μέσω OpenAI-compatible ``/chat/completions`` (TensorX, OVHcloud, vLLM, …).

Δύο είδη κλήσεων: ``complete_json`` (planner, επιλογή άρθρων) και ``stream`` (συντάκτης).
Η σκέψη του μοντέλου (``reasoning_content``, μπλοκ ``<think>``) δεν προωθείται ποτέ.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import AsyncIterator

import httpx

from .config import Settings

_THINK = re.compile(r"<think>.*?</think>", re.S)
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.M)
RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class LLMError(RuntimeError):
    """Ο πάροχος δεν απάντησε ή απάντησε κάτι που δεν διαβάζεται."""


def parse_json(text: str) -> dict:
    """Το πρώτο αντικείμενο JSON μέσα στην απάντηση, ανεκτικά (code fences, σκέψη, σχόλια γύρω)."""
    text = _FENCE.sub("", _THINK.sub("", text or "")).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise LLMError("Η απάντηση του μοντέλου δεν περιέχει JSON") from None
        try:
            value = json.loads(text[start:end + 1])
        except json.JSONDecodeError as exc:
            raise LLMError(f"Μη έγκυρο JSON από το μοντέλο: {exc}") from None
    if not isinstance(value, dict):
        raise LLMError("Το μοντέλο δεν επέστρεψε αντικείμενο JSON")
    return value


class ThinkFilter:
    """Αφαιρεί τα μπλοκ ``<think>…</think>`` από ροή κειμένου που φτάνει σε κομμάτια."""

    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self) -> None:
        self.buffer = ""
        self.inside = False

    def feed(self, chunk: str) -> str:
        self.buffer += chunk
        out = []
        while self.buffer:
            if self.inside:
                end = self.buffer.find(self.CLOSE)
                if end < 0:
                    self.buffer = self.buffer[-len(self.CLOSE):]
                    break
                self.buffer = self.buffer[end + len(self.CLOSE):]
                self.inside = False
                continue
            start = self.buffer.find(self.OPEN)
            if start >= 0:
                out.append(self.buffer[:start])
                self.buffer = self.buffer[start + len(self.OPEN):]
                self.inside = True
                continue
            # Κρατάμε πίσω μια πιθανή αρχή «<think» που κόπηκε στη μέση.
            keep = next((n for n in range(len(self.OPEN) - 1, 0, -1)
                         if self.buffer.endswith(self.OPEN[:n])), 0)
            out.append(self.buffer[:len(self.buffer) - keep])
            self.buffer = self.buffer[len(self.buffer) - keep:]
            break
        return "".join(out)

    def flush(self) -> str:
        rest, self.buffer = ("" if self.inside else self.buffer), ""
        return rest


class LLM:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(settings.llm_timeout, connect=20))

    async def aclose(self) -> None:
        await self._client.aclose()

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.settings.llm_api_key}", "Content-Type": "application/json"}

    def _url(self) -> str:
        return self.settings.llm_base_url.rstrip("/") + "/chat/completions"

    async def complete_json(self, system: str, user: str, *, stage: str = "") -> dict:
        """Μία κλήση που πρέπει να επιστρέψει αντικείμενο JSON· μία επανάληψη αν δεν διαβάζεται."""
        s = self.settings
        body = {
            "model": s.router_model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0.1,
            "max_tokens": s.llm_router_max_tokens,
            "response_format": {"type": "json_object"},
            **s.llm_router_extra_body,
        }
        last: Exception | None = None
        for attempt in range(3):
            try:
                resp = await self._client.post(self._url(), headers=self._headers(), json=body)
            except httpx.HTTPError as exc:
                last = LLMError(f"Σφάλμα σύνδεσης με τον πάροχο LLM: {type(exc).__name__}")
                await asyncio.sleep(1 + attempt)
                continue
            if resp.status_code == 400 and "response_format" in body:
                body.pop("response_format")  # ο πάροχος δεν υποστηρίζει JSON mode: το ζητά μόνο το prompt
                continue
            if resp.status_code in RETRY_STATUS:
                last = LLMError(f"Ο πάροχος LLM απάντησε {resp.status_code}")
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            if resp.status_code >= 400:
                raise LLMError(f"Ο πάροχος LLM απάντησε {resp.status_code}: {resp.text[:300]}")
            try:
                message = resp.json()["choices"][0]["message"]
                return parse_json(message.get("content") or "")
            except (LLMError, KeyError, IndexError, ValueError) as exc:
                last = exc if isinstance(exc, LLMError) else LLMError("Απρόσμενη μορφή απάντησης από τον πάροχο")
        raise last or LLMError("Ο πάροχος LLM δεν απάντησε")

    async def stream(self, messages: list[dict]) -> AsyncIterator[str]:
        """Το κείμενο της απάντησης σε κομμάτια, όπως φτάνει."""
        s = self.settings
        body = {
            "model": s.llm_model,
            "messages": messages,
            "temperature": s.llm_temperature,
            "max_tokens": s.llm_max_tokens,
            "stream": True,
            **s.llm_extra_body,
        }
        think = ThinkFilter()
        for attempt in range(2):
            sent = False
            try:
                async with self._client.stream("POST", self._url(), headers=self._headers(), json=body) as resp:
                    if resp.status_code in RETRY_STATUS and attempt == 0:
                        await resp.aread()
                        await asyncio.sleep(2)
                        continue
                    if resp.status_code >= 400:
                        detail = (await resp.aread()).decode("utf-8", "replace")[:300]
                        raise LLMError(f"Ο πάροχος LLM απάντησε {resp.status_code}: {detail}")
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            choices = json.loads(data).get("choices") or []
                        except json.JSONDecodeError:
                            continue
                        if not choices:
                            continue
                        piece = (choices[0].get("delta") or {}).get("content")
                        if piece:
                            text = think.feed(piece)
                            if text:
                                sent = True
                                yield text
                    tail = think.flush()
                    if tail:
                        yield tail
                    return
            except httpx.HTTPError as exc:
                if sent or attempt == 1:
                    raise LLMError(f"Η ροή από τον πάροχο LLM διακόπηκε: {type(exc).__name__}") from exc
                await asyncio.sleep(2)
        raise LLMError("Ο πάροχος LLM δεν απάντησε")


class FakeLLM:
    """Λειτουργία επίδειξης χωρίς πάροχο (``LLM_BASE_URL=fake``) και βάση των tests.

    Δεν «απαντά» νομικά: σχεδιάζει μία αναζήτηση με την ίδια την ερώτηση, κρατά τις πρώτες
    υποψήφιες διατάξεις και τις απαριθμεί, ώστε να φαίνεται όλη η ροή και το UI.
    """

    def __init__(self, settings: Settings | None = None):
        self.settings = settings
        self.calls: list[tuple[str, str]] = []

    async def aclose(self) -> None:
        return None

    async def complete_json(self, system: str, user: str, *, stage: str = "") -> dict:
        self.calls.append((stage, user))
        if stage == "plan":
            question = user.rsplit("ΕΡΩΤΗΣΗ:", 1)[-1].strip()
            return {"in_scope": True, "question": question, "searches": [{"q": question}], "citations": []}
        numbers = [int(n) for n in re.findall(r"^\[(\d+)\]", user, re.M)]
        return {"selected": numbers[:5], "more_searches": []}

    async def stream(self, messages: list[dict]) -> AsyncIterator[str]:
        self.calls.append(("write", messages[-1]["content"]))
        docs = re.findall(r'<document n="(\d+)" title="([^"]*)"', "\n".join(m["content"] for m in messages))
        if not docs:
            text = "**Λειτουργία επίδειξης.** Δεν βρέθηκαν σχετικές διατάξεις στο ευρετήριο."
        else:
            lines = [f"- {title} [{n}]" for n, title in docs]
            text = ("**Λειτουργία επίδειξης (χωρίς μοντέλο).** Δεν έχει οριστεί πάροχος LLM, οπότε δεν "
                    "συντάσσεται απάντηση. Οι διατάξεις που εντόπισε η αναζήτηση:\n\n" + "\n".join(lines))
        for i in range(0, len(text), 24):
            await asyncio.sleep(0.005)
            yield text[i:i + 24]


def make_llm(settings: Settings):
    return FakeLLM(settings) if settings.llm_mode == "fake" else LLM(settings)
