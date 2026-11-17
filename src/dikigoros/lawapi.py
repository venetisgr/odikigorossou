"""Πελάτης του αρχείου «Global law» της Lexifina (https://global-law.lexifina.com).

Χρησιμοποιείται μόνο για ό,τι δεν έχει το τοπικό ευρετήριο: το κείμενο μιας διάταξης όπως
ίσχυε σε μια ημερομηνία, και την κατάσταση/ιστορικό της σήμερα. Η αναζήτηση γίνεται τοπικά,
γιατί η υπηρεσία έχει όρια (χωρίς κλειδί: 10 κλήσεις το λεπτό και 100 την ημέρα ανά διεύθυνση,
μία κάθε φορά). Οι απαντήσεις κρατιούνται σε μνήμη.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import deque

import httpx

from .config import Settings

_PART = re.compile(r"^=== (\S+?) \((.*?)\) ===$", re.M)
CACHE_TTL = 6 * 3600
CACHE_MAX = 2000


class LawAPIError(RuntimeError):
    def __init__(self, message: str, limited: bool = False):
        super().__init__(message)
        self.limited = limited


class LawAPI:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(100, connect=15))
        self._lock = asyncio.Lock()  # η υπηρεσία δέχεται μία κλήση κάθε φορά ανά διεύθυνση
        self._calls: deque[float] = deque()
        self._cache: dict[str, tuple[float, dict]] = {}

    @property
    def enabled(self) -> bool:
        return self.settings.law_api_enabled

    async def aclose(self) -> None:
        await self._client.aclose()

    def budget(self) -> dict:
        """Πόσες κλήσεις απομένουν με βάση όσες έκανε αυτή η διεργασία."""
        minute, day = self.settings.law_api_limits
        now = time.time()
        while self._calls and now - self._calls[0] > 86400:
            self._calls.popleft()
        last_minute = sum(1 for t in self._calls if now - t < 60)
        return {"per_minute": minute, "per_day": day,
                "left_minute": max(0, minute - last_minute), "left_day": max(0, day - len(self._calls))}

    async def call(self, tool: str, **args) -> dict:
        """Καλεί ένα εργαλείο και επιστρέφει το ``data`` της απάντησης."""
        if not self.enabled:
            raise LawAPIError("Ο ζωντανός έλεγχος είναι απενεργοποιημένος")
        key = tool + "|" + repr(sorted(args.items()))
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < CACHE_TTL:
            return hit[1]
        async with self._lock:
            left = self.budget()
            if not left["left_day"]:
                raise LawAPIError("Εξαντλήθηκε το ημερήσιο όριο κλήσεων προς το αρχείο Global law", limited=True)
            if not left["left_minute"]:
                raise LawAPIError("Πολλές κλήσεις προς το αρχείο Global law· δοκιμάστε σε ένα λεπτό", limited=True)
            headers = {"Content-Type": "application/json"}
            if self.settings.law_api_key:
                headers["Authorization"] = f"Bearer {self.settings.law_api_key}"
            url = f"{self.settings.law_api_base_url.rstrip('/')}/{tool}"
            self._calls.append(time.time())
            try:
                resp = await self._client.post(url, headers=headers, json=args)
            except httpx.HTTPError as exc:
                raise LawAPIError(f"Το αρχείο Global law δεν απάντησε ({type(exc).__name__})") from exc
        if resp.status_code == 429:
            raise LawAPIError("Το αρχείο Global law απάντησε ότι ξεπεράστηκε το όριο κλήσεων", limited=True)
        try:
            payload = resp.json()
        except ValueError:
            raise LawAPIError(f"Απρόσμενη απάντηση από το αρχείο Global law ({resp.status_code})") from None
        if resp.status_code >= 400 or "data" not in payload:
            raise LawAPIError(str(payload.get("error") or f"Σφάλμα {resp.status_code}")[:300])
        data = payload["data"]
        data["_tree"] = payload.get("tree", "")
        if len(self._cache) >= CACHE_MAX:
            self._cache.pop(next(iter(self._cache)))
        self._cache[key] = (time.time(), data)
        return data

    async def status(self, path: str) -> dict:
        """Η κατάσταση μιας διάταξης σήμερα: ισχύει; ποιος νόμος την άλλαξε τελευταίος; ιστορικό."""
        data = await self.call("read_law", path=path, include_text=False)
        cite = data.get("cite") or {}
        return {
            "path": path,
            "in_force": data.get("in_force"),
            "summary": data.get("summary", ""),
            "last_change": data.get("last_change"),
            "next_change": data.get("next_change"),
            "history": data.get("history") or [],
            "changes_count": data.get("changes_count"),
            "latest": (data.get("latest") or {}).get("summary", ""),
            "official_url": cite.get("official_url") or "",
            "pincite": cite.get("pincite") or "",
            "caveats": data.get("caveats") or [],
            "tree": data.get("_tree", ""),
        }

    async def read_as_of(self, paths: list[str], date: str) -> dict[str, dict]:
        """Τα κείμενα έως 20 διατάξεων όπως ίσχυαν στην ημερομηνία· ``{path: {in_force, text, law}}``."""
        paths = list(dict.fromkeys(paths))[:20]
        if not paths:
            return {}
        if len(paths) == 1:
            one = await self.call("read_law", path=paths[0], date=date, max_chars=100_000)
            return {paths[0]: {
                "in_force": bool(one.get("in_force")),
                "text": one.get("text") or "",
                "law": (one.get("last_change") or {}).get("law") or "",
                "since": (one.get("last_change") or {}).get("in_force") or "",
            }}
        data = await self.call("read_law", paths=paths, date=date, max_chars=100_000)
        out: dict[str, dict] = {}
        for item in data.get("provisions") or []:
            change = item.get("last_change") or {}
            out[item["path"]] = {"in_force": bool(item.get("in_force")), "text": "",
                                 "law": change.get("law") or "", "since": change.get("in_force") or ""}
        text = data.get("text") or ""
        marks = list(_PART.finditer(text))
        for i, m in enumerate(marks):
            end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
            if m.group(1) in out:
                out[m.group(1)]["text"] = text[m.end():end].strip()
        return out
