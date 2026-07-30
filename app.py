"""Streamlit chat. Chat only -- there is no dashboard, by design.

The approval card is the point of this UI. It shows the human everything they
need to take responsibility for a classification: which subject, which visit,
which protocol version governs *that subject*, the calculation with its real
figures and record IDs, the proposed classification with its reasoning, and the
exact file that will be written.
"""

from pathlib import Path

import streamlit as st

from src.graph.builder import (
    build_agent_graph,
    initial_state,
    pending_interrupt,
    resume_turn,
    run_turn,
)
from src.sandbox import Sandbox

ROOT = Path(__file__).resolve().parent

st.set_page_config(page_title="Protocol Deviation Agent", page_icon="🧪",
                   layout="centered")
st.title("🧪 Protocol Deviation Agent")
st.caption("Detection is deterministic. The LLM classifies intent only. "
           "Nothing is written without a human at the gate.")

if "graph" not in st.session_state:
    st.session_state.graph = build_agent_graph(ROOT)
    st.session_state.state = initial_state("streamlit")

with st.sidebar:
    st.subheader("Try")
    st.code(
        "review S-004\n"
        "review S-009\n"
        "review SITE-02\n"
        "review S-005\n"
        "what is an important deviation?\n"
        "which records could you not assess?\n"
        "what happened?",
        language="text",
    )
    st.caption("`S-004` and `S-009` are late by the same four days and reach "
               "opposite verdicts. `S-005` is the immediate-hazard case.")

    actor = st.text_input("Reviewing as",
                          value="Dr A. Kovacs (Principal Investigator)")
    st.session_state.state["actor"] = actor

    if st.button("Reset sandbox", use_container_width=True):
        Sandbox(ROOT).reset()
        st.session_state.graph = build_agent_graph(ROOT)
        st.session_state.state = initial_state("streamlit")
        st.rerun()

    sandbox = Sandbox(ROOT)
    st.divider()
    st.caption("Sandbox")
    for ledger in ("deviation_entries", "site_queries", "capas",
                   "amendment_proposals", "escalations"):
        st.text(f"{ledger}: {len(sandbox.read(ledger))}")

for message in st.session_state.state.get("messages", []):
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

# ---- the approval card ----------------------------------------------------
card = pending_interrupt(st.session_state.graph, st.session_state.state)
if card:
    with st.container(border=True):
        st.subheader("Approval required")
        st.markdown(
            f"**Action** `{card['action_id']}` — `{card['action']}`  \n"
            f"**Subject** {card['subject'] or '—'} &nbsp;&nbsp; "
            f"**Visit** {card['visit'] or '—'} &nbsp;&nbsp; "
            f"**Site** {card['site'] or '—'}  \n"
            f"**Protocol version governing this subject** "
            f"{card['protocol_version_governing_subject'] or '—'}"
        )
        st.markdown("**Calculation**")
        st.info(card["calculation"])

        if card["proposed_classification"]:
            st.markdown(
                f"**Proposed classification:** `{card['proposed_classification']}`  \n"
                f"{card['classification_reasoning']}"
            )
            st.caption(card["classification_status"])

        for item in card["evidence"]:
            st.caption(f"Evidence — {item}")

        st.markdown(f"**Will write to** `{card['will_write_to']}`")

        if card["confirmation_required"] == "token":
            st.warning(
                "This action has downstream regulatory effect. Type the exact token "
                "to confirm."
            )
            st.code(card["required_token"], language="text")
            reply = st.text_input("Confirmation", key=f"tok-{card['action_id']}")
            if st.button("Submit confirmation", type="primary"):
                st.session_state.state = resume_turn(
                    st.session_state.graph, st.session_state.state, reply)
                st.rerun()
        else:
            columns = st.columns(2)
            if columns[0].button("Approve", type="primary",
                                 use_container_width=True):
                st.session_state.state = resume_turn(
                    st.session_state.graph, st.session_state.state, "yes")
                st.rerun()
            if columns[1].button("Reject", use_container_width=True):
                st.session_state.state = resume_turn(
                    st.session_state.graph, st.session_state.state, "no")
                st.rerun()

if prompt := st.chat_input("Try: review S-004"):
    st.session_state.state = run_turn(
        st.session_state.graph, st.session_state.state, prompt)
    st.rerun()
