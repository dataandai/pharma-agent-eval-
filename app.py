from pathlib import Path

import streamlit as st

from src.data.repositories import SandboxRepository
from src.graph.builder import build_agent_graph, initial_state, run_turn

ROOT = Path(__file__).resolve().parent

st.set_page_config(page_title="Revenue Leakage Agent", page_icon="🧾", layout="centered")
st.title("🧾 Revenue Leakage Agent — Mock Demo")
st.caption("LLM boundary is mocked offline; financial logic, approval, execution and audit are deterministic.")

if "agent_graph" not in st.session_state:
    st.session_state.agent_graph = build_agent_graph(ROOT)
if "agent_state" not in st.session_state:
    st.session_state.agent_state = initial_state()

with st.sidebar:
    st.subheader("Mock plans")
    st.code("P-100 missing invoice\nP-200 already remediated\nP-300 underbilling\nP-400 overbilling\nP-500 contract change")
    if st.button("Reset sandbox", use_container_width=True):
        SandboxRepository(ROOT).reset()
        st.session_state.agent_graph = build_agent_graph(ROOT)
        st.session_state.agent_state = initial_state()
        st.rerun()

for message in st.session_state.agent_state.get("messages", []):
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

pending = st.session_state.agent_state.get("pending_actions", [])
if pending:
    st.subheader("Pending approval")
    for proposal in pending:
        cols = st.columns([3, 1, 1])
        cols[0].markdown(
            f"`{proposal['action_id']}` — **{proposal['action_type']}** "
            f"{proposal.get('amount') or ''} {proposal.get('currency') or ''}"
        )
        if cols[1].button("Approve", key=f"approve-{proposal['action_id']}"):
            st.session_state.agent_state = run_turn(
                st.session_state.agent_graph,
                st.session_state.agent_state,
                f"approve {proposal['action_id']}",
            )
            st.rerun()
        if cols[2].button("Reject", key=f"reject-{proposal['action_id']}"):
            st.session_state.agent_state = run_turn(
                st.session_state.agent_graph,
                st.session_state.agent_state,
                f"reject {proposal['action_id']}",
            )
            st.rerun()

if prompt := st.chat_input("Try: investigate P-100"):
    st.session_state.agent_state = run_turn(
        st.session_state.agent_graph,
        st.session_state.agent_state,
        prompt,
    )
    st.rerun()
