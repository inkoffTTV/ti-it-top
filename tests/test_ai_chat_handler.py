"""AI-помощник в боте: выход из режима по кнопке меню, лимиты, ошибки провайдера (без сети и Telegram)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiogram.dispatcher.event.bases import SkipHandler

import handlers.ai_chat as ai_chat
from services import ai_budget


class FakeState:
    def __init__(self):
        self.data = {}
        self.state = ai_chat.AIChatStates.chatting.state

    async def clear(self):
        self.state = None
        self.data = {}

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **kw):
        self.data.update(kw)

    async def get_state(self):
        return self.state


class FakeMsg:
    def __init__(self, text, uid=42):
        self.text = text
        self.from_user = SimpleNamespace(id=uid)
        self.chat = SimpleNamespace(id=uid)
        self.sent: list[str] = []
        self.edited: list[str] = []
        outer = self

        class _Bot:
            async def send_chat_action(self, *a, **k):
                return None

        self.bot = _Bot()

        class _Waiting:
            async def edit_text(self, text, **kw):
                outer.edited.append(text)

        self._waiting = _Waiting()

    async def answer(self, text, **kw):
        self.sent.append(text)
        return self._waiting


@pytest.fixture(autouse=True)
def _budget(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_budget, "PATH", tmp_path / "ai_usage.json")
    ai_chat._last_question.clear()


async def test_menu_button_leaves_ai_mode_and_passes_through():
    st = FakeState()
    with pytest.raises(SkipHandler):
        await ai_chat.ai_leave_by_menu(FakeMsg(ai_chat.BTN_MY_PROFILE), st)
    assert st.state is None


async def test_answer_and_limit(monkeypatch):
    monkeypatch.setenv("AI_BOT_USER_DAILY", "1")
    calls = []

    async def fake_answer(config, tg_id, question, history):
        calls.append(question)
        return "Ответ <b>без</b> HTML-инъекций"

    monkeypatch.setattr("services.ai_user_assistant.answer", fake_answer)
    st = FakeState()
    m = FakeMsg("Как подать заявку?")
    await ai_chat.ai_question(m, st)
    assert calls and "&lt;b&gt;" in m.edited[-1] and len(st.data["ai_history"]) == 2
    ai_chat._last_question.clear()
    m2 = FakeMsg("И ещё вопрос")
    await ai_chat.ai_question(m2, st)
    assert "лимит" in m2.sent[-1] and len(calls) == 1


async def test_provider_failure_refunds_question(monkeypatch):
    from services.ai_simple import AIUnavailable

    async def broken(*a, **k):
        raise AIUnavailable("429")

    monkeypatch.setattr("services.ai_user_assistant.answer", broken)
    m = FakeMsg("Когда турнир?")
    await ai_chat.ai_question(m, FakeState())
    assert "не отвечает" in m.edited[-1]
    assert ai_budget.usage()["bot_total"] == 0
