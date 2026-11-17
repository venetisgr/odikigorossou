from pathlib import Path

import pytest

from dikigoros.config import Settings
from dikigoros.indexer import build
from dikigoros.llm import FakeLLM
from dikigoros.store import Store

FIXTURES = Path(__file__).parent / "fixtures" / "greece-law"


@pytest.fixture(scope="session")
def index_path(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("index") / "index.db"
    stats = build(FIXTURES, out, workers=1, quiet=True)
    assert stats["acts"] == 6 and not stats["errors"]
    return out


@pytest.fixture(scope="session")
def store(index_path) -> Store:
    return Store(index_path)


@pytest.fixture()
def settings(index_path, monkeypatch) -> Settings:
    for name in ("LLM_API_KEY", "ACCESS_CODE", "LAW_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LLM_BASE_URL", "fake")
    monkeypatch.setenv("DIKIGOROS_INDEX", str(index_path))
    return Settings()


class ScriptedLLM(FakeLLM):
    """Ψεύτικο μοντέλο με προκαθορισμένες απαντήσεις ανά στάδιο."""

    def __init__(self, plan=None, select=None, answer="Απάντηση [1]."):
        super().__init__()
        self.plan, self.select, self.answer = plan, select, answer

    async def complete_json(self, system, user, *, stage=""):
        self.calls.append((stage, user))
        scripted = self.plan if stage == "plan" else self.select
        if scripted is None:
            return await FakeLLM.complete_json(self, system, user, stage=stage)
        if isinstance(scripted, Exception):
            raise scripted
        return scripted.pop(0) if isinstance(scripted, list) else scripted

    async def stream(self, messages):
        self.calls.append(("write", messages))
        if isinstance(self.answer, Exception):
            raise self.answer
        for i in range(0, len(self.answer), 7):
            yield self.answer[i:i + 7]
