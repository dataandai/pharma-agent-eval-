"""The LLM boundary.

It classifies intent and extracts identifiers that are already present in the
message. It never produces a figure, a classification, or a write payload --
every number the user sees comes from a tool result, and every write goes
through the gate.

Two implementations behind one interface: the Claude adapter, and a
deterministic rule-based one that is the default when no API key is configured
and the one the test suite runs against.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
try:
    # Python 3.11+ provides StrEnum
    from enum import StrEnum
except Exception:  # pragma: no cover - provides compatibility for older Pythons
    from enum import Enum

    class StrEnum(str, Enum):
        pass

MODEL_ID = "claude-haiku-4-5-20251001"

SUBJECT_RE = re.compile(r"\b[Ss][-_]?\d{2,3}\b")
SITE_RE = re.compile(r"\bSITE[-_]?\d{1,2}\b", re.IGNORECASE)
ACTION_RE = re.compile(r"\bACT-[A-Z0-9]+\b", re.IGNORECASE)
JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


class Intent(StrEnum):
    REVIEW = "review"
    FOLLOW_UP = "follow_up"
    KNOWLEDGE_QUESTION = "knowledge_question"
    APPROVE_ACTION = "approve_action"
    REJECT_ACTION = "reject_action"
    ROLLBACK_ACTION = "rollback_action"
    AUDIT_QUERY = "audit_query"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class IntentDecision:
    intent: Intent
    subject_id: str | None = None
    site_id: str | None = None
    action_id: str | None = None


def extract_ids(message: str) -> tuple[str | None, str | None, str | None]:
    subject = SUBJECT_RE.search(message or "")
    site = SITE_RE.search(message or "")
    action = ACTION_RE.search(message or "")

    subject_id = None
    if subject:
        digits = re.sub(r"\D", "", subject.group(0))
        subject_id = f"S-{int(digits):03d}"
    site_id = None
    if site:
        digits = re.sub(r"\D", "", site.group(0))
        site_id = f"SITE-{int(digits):02d}"
    return subject_id, site_id, (action.group(0).upper() if action else None)


class RuleBasedInterpreter:
    """Deterministic and offline. The default without an API key."""

    def classify(self, message: str) -> IntentDecision:
        text = (message or "").strip().lower()
        subject_id, site_id, action_id = extract_ids(message or "")

        if any(word in text for word in
               ("rollback", "roll back", "undo", "reverse", "visszavon")):
            intent = Intent.ROLLBACK_ACTION
        elif any(word in text for word in ("reject", "decline", "no", "elutasít")):
            intent = Intent.REJECT_ACTION
        elif "approve" in text or text in ("yes", "ok", "igen", "rendben") \
                or (action_id and "confirm" in text):
            intent = Intent.APPROVE_ACTION
        elif any(word in text for word in
                 ("audit", "history", "what happened", "who approved", "történt")):
            intent = Intent.AUDIT_QUERY
        elif any(word in text for word in
                 ("review", "check", "assess", "deviation", "findings", "vizsgáld",
                  "ellenőriz")) or subject_id or site_id:
            intent = Intent.REVIEW
        elif any(word in text for word in
                 ("what is", "what does", "explain", "mean", "mit jelent")):
            intent = Intent.KNOWLEDGE_QUESTION
        elif any(word in text for word in ("why", "how", "which", "miért")):
            intent = Intent.FOLLOW_UP
        else:
            intent = Intent.UNKNOWN

        return IntentDecision(intent, subject_id, site_id, action_id)


class LLMInterpreter:
    """Claude adapter. Degrades to the rule-based classifier rather than
    raising into the graph."""

    SYSTEM_PROMPT = """You classify a message to a clinical trial protocol deviation agent.

Return one intent:
- REVIEW: assess a subject, a site, or the study for protocol deviations
- FOLLOW_UP: a clarifying question about findings already shown
- KNOWLEDGE_QUESTION: what a term means (important deviation, CAPA, visit window...)
- APPROVE_ACTION: approves a drafted action
- REJECT_ACTION: rejects a drafted action
- ROLLBACK_ACTION: undo an applied action
- AUDIT_QUERY: asks what happened, who approved what
- UNKNOWN: none of the above

Extract identifiers ONLY if they appear literally in the message: subject (S-001),
site (SITE-01), action (ACT-XXXXXXXX). Never invent one.

Respond with JSON only:
{"intent": "...", "subject_id": null, "site_id": null, "action_id": null}"""

    def __init__(self, client=None, model: str = MODEL_ID):
        self.model = model
        self.fallback = RuleBasedInterpreter()
        if client is not None:
            self.client = client
            return
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY environment variable not set")
        try:
            from anthropic import Anthropic
            self.client = Anthropic(api_key=api_key)
        except Exception:
            # Allow tests to enable the LLM interpreter without having the
            # `anthropic` package installed; fall back to rule-based behavior.
            self.client = None

    def classify(self, message: str) -> IntentDecision:
        subject_id, site_id, action_id = extract_ids(message or "")
        try:
            response = self.client.messages.create(
                model=self.model, max_tokens=256, system=self.SYSTEM_PROMPT,
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
            subject_id=data.get("subject_id") or subject_id,
            site_id=data.get("site_id") or site_id,
            action_id=data.get("action_id") or action_id,
        )

    @staticmethod
    def _parse(text: str) -> dict:
        match = JSON_OBJECT_RE.search(text)
        if not match:
            raise ValueError("no JSON object in classifier response")
        parsed = json.loads(match.group(0))
        if not isinstance(parsed, dict):
            raise ValueError("classifier response is not an object")
        return parsed


def default_interpreter():
    """Building the graph must never require network credentials."""
    if os.getenv("ANTHROPIC_API_KEY"):
        return LLMInterpreter()
    return RuleBasedInterpreter()
