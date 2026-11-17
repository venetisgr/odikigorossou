"""Η υπηρεσία: API + στατικό frontend σε ένα container.

    uvicorn dikigoros.server:app --host 0.0.0.0 --port 8000

Δεν αποθηκεύει τίποτα: οι συζητήσεις μένουν στον browser του χρήστη και στέλνονται
ξανά με κάθε ερώτηση.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import re
import time
import urllib.parse
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from . import __version__
from .config import Settings
from .lawapi import LawAPI, LawAPIError
from .llm import make_llm
from .pipeline import Options, Pipeline
from .store import KIND_LABEL, KINDS, Store

log = logging.getLogger("dikigoros")
WEB = Path(__file__).parent / "web"
KEEPALIVE_S = 15
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class Message(BaseModel):
    role: str
    content: str = Field(max_length=40_000)


class AskRequest(BaseModel):
    question: str
    history: list[Message] = Field(default_factory=list, max_length=40)
    kinds: list[str] = Field(default_factory=list, max_length=4)
    acts: list[str] = Field(default_factory=list, max_length=30)
    as_of: str = ""
    previous_sources: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("as_of")
    @classmethod
    def _date(cls, value: str) -> str:
        value = value.strip()
        if value and not _DATE.match(value):
            raise ValueError("Η ημερομηνία γράφεται ως ΕΕΕΕ-ΜΜ-ΗΗ")
        return value


class RateLimiter:
    """Όριο ερωτήσεων ανά διεύθυνση την ώρα, σε μνήμη (αρκεί για μία μηχανή)."""

    def __init__(self, per_hour: int):
        self.per_hour = per_hour
        self.hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str) -> int | None:
        """``None`` αν επιτρέπεται· αλλιώς σε πόσα δευτερόλεπτα να ξαναδοκιμάσει."""
        if self.per_hour <= 0:
            return None
        now = time.time()
        q = self.hits[key]
        while q and now - q[0] > 3600:
            q.popleft()
        if len(q) >= self.per_hour:
            return int(3600 - (now - q[0])) + 1
        q.append(now)
        if len(self.hits) > 20_000:
            self.hits.clear()
        return None


def client_ip(request: Request) -> str:
    return (request.headers.get("fly-client-ip")
            or (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
            or (request.client.host if request.client else "unknown"))


def sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def create_app(settings: Settings | None = None, store: Store | None = None, llm=None,
               law: LawAPI | None = None) -> FastAPI:
    settings = settings or Settings()
    state: dict = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state["store"] = store or Store(settings.index_path)
        state["llm"] = llm or make_llm(settings)
        state["law"] = law or LawAPI(settings)
        state["pipeline"] = Pipeline(settings, state["store"], state["llm"], state["law"])
        log.info("ευρετήριο: %s", state["store"].meta())
        yield
        await state["llm"].aclose()
        await state["law"].aclose()

    app = FastAPI(title="Ο Δικηγόρος σου", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None)
    if settings.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=[o.strip() for o in settings.cors_origins.split(",")],
                           allow_methods=["GET", "POST"], allow_headers=["*"])
    limiter = RateLimiter(settings.ask_per_hour)
    live_limiter = RateLimiter(max(20, settings.ask_per_hour))

    def auth(request: Request) -> None:
        if not settings.access_code:
            return
        # Ο κωδικός ταξιδεύει percent-encoded, ώστε να μπορεί να έχει ελληνικούς χαρακτήρες.
        given = urllib.parse.unquote(request.headers.get("x-access-code", ""))
        if not hmac.compare_digest(given.encode(), settings.access_code.encode()):
            raise HTTPException(401, "Απαιτείται κωδικός πρόσβασης")

    # ------------------------------------------------------------ δημόσια

    @app.get("/health")
    async def health() -> dict:
        meta = state["store"].meta()
        return {"ok": True, "version": __version__, "llm": settings.llm_mode,
                "units": int(meta.get("units", 0)), "index_built_at": meta.get("built_at")}

    @app.get("/api/meta")
    async def meta() -> dict:
        s: Store = state["store"]
        info = s.meta()
        return {
            "version": __version__,
            "auth_required": bool(settings.access_code),
            "llm": settings.llm_mode,
            "model": settings.llm_model if settings.llm_mode == "live" else None,
            "index": {"built_at": info.get("built_at"), "source": info.get("source"),
                      "source_commit": (info.get("source_commit") or "")[:12],
                      "acts": int(info.get("acts", 0)), "units": int(info.get("units", 0)),
                      "by_kind": info["by_kind"]},
            "kinds": [{"id": k, "label": KIND_LABEL[k]} for k in KINDS],
            "catalogue": [{"id": a["id"], "short": a["short"], "title": a["title"], "kind": a["kind"],
                           "consolidation": a["consolidation"], "ocr": bool(a["ocr"]), "note": a["note"],
                           "n_units": a["n_units"]} for a in s.catalogue()],
            "live_check": state["law"].enabled,
            "limits": {"ask_per_hour": settings.ask_per_hour, "question_chars": settings.max_question_chars},
        }

    # ------------------------------------------------------------ με κωδικό

    @app.post("/api/login", dependencies=[Depends(auth)])
    async def login() -> dict:
        return {"ok": True}

    @app.post("/api/ask", dependencies=[Depends(auth)])
    async def ask(body: AskRequest, request: Request):
        question = body.question.strip()
        if not question:
            raise HTTPException(422, "Γράψτε μια ερώτηση")
        if len(question) > settings.max_question_chars:
            raise HTTPException(422, f"Η ερώτηση ξεπερνά τους {settings.max_question_chars} χαρακτήρες")
        if settings.llm_mode == "off":
            raise HTTPException(503, "Δεν έχει οριστεί πάροχος LLM (LLM_API_KEY). Η αναζήτηση διατάξεων λειτουργεί.")
        wait = limiter.check(client_ip(request))
        if wait:
            raise HTTPException(429, f"Πολλές ερωτήσεις. Δοκιμάστε ξανά σε {wait // 60 + 1} λεπτά.",
                                headers={"Retry-After": str(wait)})
        options = Options(kinds=[k for k in body.kinds if k in KINDS], acts=body.acts, as_of=body.as_of)
        history = [m.model_dump() for m in body.history]
        pipeline: Pipeline = state["pipeline"]

        async def produce(queue: asyncio.Queue) -> None:
            try:
                async for event in pipeline.run(question, history, options, body.previous_sources):
                    await queue.put(event)
            except Exception:  # ό,τι κι αν συμβεί, ο χρήστης βλέπει μήνυμα αντί για ροή που κόπηκε
                log.exception("σφάλμα στη ροή απάντησης")
                await queue.put({"type": "error", "message": "Κάτι πήγε στραβά κατά την απάντηση. Δοκιμάστε ξανά."})
            await queue.put(None)

        async def stream():
            queue: asyncio.Queue = asyncio.Queue()
            task = asyncio.create_task(produce(queue))
            try:
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), KEEPALIVE_S)
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
                        continue
                    if event is None:
                        break
                    yield sse(event)
            finally:
                task.cancel()

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/search", dependencies=[Depends(auth)])
    async def search(q: str = Query(min_length=2, max_length=400), kinds: str = "", acts: str = "",
                     limit: int = Query(20, ge=1, le=50)) -> dict:
        s: Store = state["store"]
        kind_list = [k for k in kinds.split(",") if k in KINDS]
        act_list = [a for a in (s.resolve_act(x) for x in acts.split(",") if x) if a]
        hits = await asyncio.to_thread(s.search, q, kind_list or None, act_list or None, limit)
        for hit in hits:
            hit.pop("text", None)
        return {"query": q, "hits": hits}

    @app.get("/api/unit", dependencies=[Depends(auth)])
    async def unit(uid: str = Query(max_length=300)) -> dict:
        s: Store = state["store"]
        found = s.unit(uid)
        if not found:
            raise HTTPException(404, "Η διάταξη δεν βρέθηκε")
        found.update(s.neighbours(found))
        found["live_available"] = state["law"].enabled and found["path"].endswith(".md")
        return found

    @app.get("/api/act", dependencies=[Depends(auth)])
    async def act(id: str = Query(max_length=200)) -> dict:
        s: Store = state["store"]
        act_id = s.resolve_act(id)
        if not act_id:
            raise HTTPException(404, "Η πράξη δεν βρέθηκε")
        return {"act": s.act(act_id), "toc": s.toc(act_id)}

    @app.get("/api/live", dependencies=[Depends(auth)])
    async def live(request: Request, uid: str = Query(max_length=300)) -> dict:
        """Η κατάσταση της διάταξης σήμερα στο αρχείο Global law (ισχύς, τελευταία αλλαγή, ιστορικό)."""
        s: Store = state["store"]
        law: LawAPI = state["law"]
        found = s.unit(uid)
        if not found:
            raise HTTPException(404, "Η διάταξη δεν βρέθηκε")
        if not found["path"].endswith(".md"):
            raise HTTPException(422, "Η διάταξη αυτή δεν παρακολουθείται ανά άρθρο στο αρχείο Global law")
        wait = live_limiter.check(client_ip(request))
        if wait:
            raise HTTPException(429, "Πολλοί έλεγχοι. Δοκιμάστε αργότερα.", headers={"Retry-After": str(wait)})
        try:
            return await law.status(found["path"])
        except LawAPIError as exc:
            raise HTTPException(429 if exc.limited else 502, str(exc)) from exc

    # ------------------------------------------------------------ frontend

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code, headers=exc.headers)

    @app.get("/")
    async def index():
        return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=WEB), name="static")
    return app


app = create_app()
