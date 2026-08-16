"""Drive the actual Streamlit app.

A server answering HTTP 200 on `/` proves the socket is open, not that the app
renders -- Streamlit serves the same static shell whether or not the script
raised. These tests run `app.py` through Streamlit's own harness and assert on
what a user would see.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

AppTest = pytest.importorskip("streamlit.testing.v1").AppTest


@pytest.fixture()
def app(monkeypatch, tmp_path):
    """A fresh app against a disposable sandbox, never the repo's own."""
    import shutil

    shutil.copytree(ROOT / "data", tmp_path / "data")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    return AppTest.from_file(str(ROOT / "app.py"), default_timeout=90).run()


def rendered(at) -> str:
    return " ".join(block.value for block in at.markdown)


def test_the_app_renders_without_raising(app):
    assert not app.exception, app.exception
    assert app.chat_input, "no chat input rendered"


def test_a_review_renders_findings_and_drafts_nothing_visible(app):
    app.chat_input[0].set_value("review S-005").run()
    assert not app.exception, app.exception

    body = rendered(app)
    assert "2.5.3" in body, "the immediate-hazard citation is missing from the UI"
    assert "ACT-" in body, "no drafted action was shown"
    assert "log_deviation" in body


def test_the_approval_card_shows_what_a_human_needs(app):
    app.chat_input[0].set_value("review S-005").run()
    ids = re.findall(r"ACT-[A-Z0-9]{10}", rendered(app))
    assert ids

    app.chat_input[0].set_value(f"approve {ids[0]}").run()
    assert not app.exception, app.exception

    assert "Approval required" in [s.value for s in app.subheader]
    card = " ".join(i.value for i in app.info)
    assert card, "the card shows no calculation"
    body = rendered(app)
    assert "Will write to" in body
    assert "sandbox/" in body


def test_a_token_gated_action_offers_a_field_not_a_button(app):
    """Classifying an important deviation must not be one click away."""
    app.chat_input[0].set_value("review S-005").run()
    ids = re.findall(r"ACT-[A-Z0-9]{10}", rendered(app))

    app.chat_input[0].set_value(f"approve {ids[0]}").run()
    labels = [t.label for t in app.text_input]
    buttons = [b.label for b in app.button]

    assert "Confirmation" in labels
    assert "Approve" not in buttons, "a token-gated action must not have an Approve button"
    assert "Submit confirmation" in buttons


def test_a_follow_up_question_does_not_crash_the_app(app):
    """Site-level observations carry no verdict, and the summary path used to
    hand `None` to sorted() alongside strings."""
    app.chat_input[0].set_value("review SITE-03").run()
    assert not app.exception, app.exception

    app.chat_input[0].set_value("how many of each?").run()
    assert not app.exception, app.exception
    assert rendered(app)


def test_the_knowledge_registry_answers_in_the_ui(app):
    app.chat_input[0].set_value("what is an important deviation?").run()
    assert not app.exception, app.exception
    assert "significantly affect" in rendered(app)
