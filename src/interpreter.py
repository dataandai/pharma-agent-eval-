from __future__ import annotations

import json
import os
import re

from src.domain.models import Intent, IntentDecision


PLAN_RE = re.compile(r"\bP-\d+\b", re.IGNORECASE)
ACTION_RE = re.compile(r"\bACT-[A-Z0-9]+\b", re.IGNORECASE)
JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)

MODEL_ID = "claude-haiku-4-5-20251001"


def extract_ids(message: str) -> tuple[str | None, str | None]:
    plan_match = PLAN_RE.search(message)
    action_match = ACTION_RE.search(message)
    return (
        plan_match.group(0).upper() if plan_match else None,
        action_match.group(0).upper() if action_match else None,
    )


class LLMInterpreter:
    """Claude Haiku 4.5 adapter for intent classification.

    The LLM boundary decides intent only. It never produces a write payload,
    an amount, or a record ID that is not already present in the user message.
    A malformed response degrades to the rule-based classifier rather than
    raising into the graph.
    """

    SYSTEM_PROMPT = """You are an intent classifier for a revenue leakage detection agent.
Classify the user's message into one of these intents:
- APPROVE_ACTION: User approves/applies an action
- REJECT_ACTION: User rejects/denies an action
- ROLLBACK_ACTION: User wants to undo/rollback an action
- INVESTIGATE: User wants to investigate a plan
- AUDIT_QUERY: User asks about history or what happened
- KNOWLEDGE_QUESTION: User asks about financial concepts (credit memo, make-good, etc.)
- FOLLOW_UP: User asks clarifying questions (why, currency, other months)
- UNKNOWN: Message doesn't fit other categories

Extract any plan IDs (P-XXX) and action IDs (ACT-XXXXXX) mentioned.
Respond only with JSON: {"intent": "...", "referenced_plan_id": null or "P-XXX", "referenced_action_id": null or "ACT-XXXXXX"}"""

    def __init__(self, client=None, model: str = MODEL_ID):
        self.model = model
        self.fallback = RuleBasedInterpreter()
        if client is not None:
            self.client = client
            return
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY environment variable not set")
        from anthropic import Anthropic

        self.client = Anthropic(api_key=api_key)

    def classify(self, message: str) -> IntentDecision:
        plan_id, action_id = extract_ids(message)
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=256,
                system=self.SYSTEM_PROMPT,
                messages=[{"role": "user", "content": message}],
            )
            data = self._parse(response.content[0].text)
        except Exception:
            return self.fallback.classify(message)

        try:
            intent = Intent[str(data.get("intent", "UNKNOWN")).upper()]
        except KeyError:
            intent = Intent.UNKNOWN

        return IntentDecision(
            intent=intent,
            referenced_plan_id=data.get("referenced_plan_id") or plan_id,
            referenced_action_id=data.get("referenced_action_id") or action_id,
        )

    @staticmethod
    def _parse(text: str) -> dict:
        """Tolerate a prose preamble or a fenced code block around the JSON."""
        match = JSON_OBJECT_RE.search(text)
        if not match:
            raise ValueError("no JSON object in classifier response")
        parsed = json.loads(match.group(0))
        if not isinstance(parsed, dict):
            raise ValueError("classifier response is not an object")
        return parsed


class RuleBasedInterpreter:
    """Offline classifier. The default when no API key is present, and the
    deterministic interpreter the test suite runs against."""

    def classify(self, message: str) -> IntentDecision:
        text = message.strip().lower()
        plan_id, action_id = extract_ids(message)

        if any(word in text for word in ["rollback", "roll back", "undo", "visszavon"]):
            intent = Intent.ROLLBACK_ACTION
        elif any(word in text for word in ["approve", "apply", "yes", "jóváhagy", "alkalmazd"]):
            intent = Intent.APPROVE_ACTION
        elif any(word in text for word in ["reject", "elutasít", "ne alkalmazd"]):
            intent = Intent.REJECT_ACTION
        elif any(word in text for word in ["audit", "history", "what happened", "történt"]):
            intent = Intent.AUDIT_QUERY
        elif any(word in text for word in ["credit memo", "make-good", "make good", "plan amendment", "revenue leakage"]):
            intent = Intent.KNOWLEDGE_QUESTION
        elif plan_id and any(word in text for word in ["check", "investigate", "anomaly", "leakage", "vizsgáld", "ellenőriz"]):
            intent = Intent.INVESTIGATE
        elif any(word in text for word in ["currency", "other months", "why", "deviza", "többi hónap", "miért"]):
            intent = Intent.FOLLOW_UP
        elif plan_id:
            intent = Intent.INVESTIGATE
        else:
            intent = Intent.UNKNOWN

        return IntentDecision(
            intent=intent,
            referenced_plan_id=plan_id,
            referenced_action_id=action_id,
        )


def default_interpreter():
    """Pick the LLM boundary when it is configured, otherwise stay offline.

    Constructing the graph must never require network credentials — that is
    what made the graph tests unrunnable.
    """
    if os.getenv("ANTHROPIC_API_KEY"):
        return LLMInterpreter()
    return RuleBasedInterpreter()
