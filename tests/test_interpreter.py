import pytest

from src.interpreter import (
    MODEL_ID,
    Intent,
    LLMInterpreter,
    RuleBasedInterpreter,
    default_interpreter,
)


class FakeMessage:
    def __init__(self, text: str):
        self.content = [type("Block", (), {"text": text})()]


class FakeClient:
    """Records the request and replays a canned response."""

    def __init__(self, text: str = '{"intent": "REVIEW", "subject_id": "S-100"}'):
        self.text = text
        self.calls = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.text, Exception):
            raise self.text
        return FakeMessage(self.text)


def test_model_id_is_a_valid_pinned_version():
    """A dotted '4.5' is not a valid model ID and 404s on every call."""
    assert MODEL_ID == "claude-haiku-4-5-20251001"
    assert "latest" not in MODEL_ID


def test_llm_interpreter_sends_the_pinned_model():
    client = FakeClient()
    decision = LLMInterpreter(client=client).classify("review S-100")
    assert client.calls[0]["model"] == MODEL_ID
    assert decision.intent == Intent.REVIEW
    assert decision.subject_id == "S-100"


@pytest.mark.parametrize(
    "text",
    [
        'Here is the classification:\n{"intent": "REVIEW"}',
        '```json\n{"intent": "REVIEW"}\n```',
        '{"intent": "REVIEW"}',
    ],
)
def test_prose_and_fences_around_the_json_are_tolerated(text):
    decision = LLMInterpreter(client=FakeClient(text)).classify("review S-100")
    assert decision.intent == Intent.REVIEW


def test_malformed_response_degrades_instead_of_raising():
    decision = LLMInterpreter(client=FakeClient("I cannot help with that.")).classify("rollback ACT-ABC123")
    assert decision.intent == Intent.ROLLBACK_ACTION
    assert decision.action_id == "ACT-ABC123"


def test_api_failure_degrades_instead_of_raising():
    client = FakeClient(RuntimeError("connection reset"))
    decision = LLMInterpreter(client=client).classify("review S-100")
    assert decision.intent == Intent.REVIEW


def test_ids_are_recovered_when_the_model_omits_them():
    client = FakeClient('{"intent": "APPROVE_ACTION"}')
    decision = LLMInterpreter(client=client).classify("approve ACT-DEADBEEF")
    assert decision.action_id == "ACT-DEADBEEF"


def test_unknown_intent_label_does_not_raise():
    decision = LLMInterpreter(client=FakeClient('{"intent": "NONSENSE"}')).classify("hello")
    assert decision.intent == Intent.UNKNOWN


def test_default_interpreter_is_offline_without_a_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert isinstance(default_interpreter(), RuleBasedInterpreter)


def test_default_interpreter_uses_the_llm_when_configured(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-a-real-key")
    assert isinstance(default_interpreter(), LLMInterpreter)
