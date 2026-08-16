from __future__ import annotations

try:  # pragma: no cover - exercised when langgraph is installed
    from langgraph.graph import END, START, StateGraph  # type: ignore
    try:
        from langgraph.checkpoint.memory import InMemorySaver as MemorySaver  # type: ignore
    except ImportError:
        from langgraph.checkpoint.memory import MemorySaver  # type: ignore
    from langgraph.types import Command, interrupt  # type: ignore
    USING_REAL_LANGGRAPH = True
    HAS_INTERRUPT = True
except ImportError:  # lightweight offline fallback
    START = "__start__"
    END = "__end__"
    USING_REAL_LANGGRAPH = False
    HAS_INTERRUPT = False

    def interrupt(payload):  # pragma: no cover - offline fallback
        raise RuntimeError("interrupt() requires langgraph")

    class Command:  # pragma: no cover - offline fallback
        def __init__(self, resume=None, **_):
            self.resume = resume

    class MemorySaver:
        def __init__(self):
            self.states = {}

    class StateGraph:
        def __init__(self, _schema=None):
            self.nodes = {}
            self.edges = {}
            self.conditionals = {}

        def add_node(self, name, function):
            self.nodes[name] = function

        def add_edge(self, source, target):
            self.edges.setdefault(source, []).append(target)

        def add_conditional_edges(self, source, route, mapping):
            self.conditionals[source] = (route, mapping)

        def compile(self, checkpointer=None):
            return _Compiled(self, checkpointer)

    class _Compiled:
        def __init__(self, graph, checkpointer):
            self.graph = graph
            self.checkpointer = checkpointer
            self._last_state = {}
            self._paused_at = None

        def invoke(self, input_state, config=None):
            # Accept either a mapping-like input or a Command-like object
            is_command = hasattr(input_state, "resume")
            resume_val = getattr(input_state, "resume") if is_command else None
            try:
                state = dict(input_state)
            except Exception:
                state = {}
            # If a Command-like object was provided, map its resume into
            # the user message so nodes see the reply.
            if is_command:
                state["user_message"] = resume_val

            thread_id = ((config or {}).get("configurable") or {}).get("thread_id", "default")
            # Merge with previous checkpointer state when available (resume path).
            if self.checkpointer and thread_id in self.checkpointer.states:
                previous = dict(self.checkpointer.states[thread_id])
                previous.update(state)
                state = previous
            # If this is a resume (Command) and we previously paused, start
            # execution at the paused node instead of re-running from START.
            current = self.graph.edges[START][0]
            if is_command and self._paused_at is not None:
                current = self._paused_at
            steps = 0
            while current != END:
                update = self.graph.nodes[current](state) or {}
                state.update(update)
                # Special-case the gate: when running the initial approve command
                # we should *pause* (emulate an interrupt) rather than consume the
                # user's approval token. A resume call (Command) will re-run the
                # graph with the saved state and the resume value mapped to
                # `user_message`, allowing the gate to proceed.
                if current == "gate" and not is_command:
                    # store snapshot and return early to simulate a pause
                    if self.checkpointer:
                        self.checkpointer.states[thread_id] = dict(state)
                    self._last_state = dict(state)
                    self._paused_at = current
                    return state
                # when resuming, clear paused marker before proceeding
                if current == "gate" and is_command and self._paused_at is not None:
                    self._paused_at = None
                if current in self.graph.conditionals:
                    route, mapping = self.graph.conditionals[current]
                    current = mapping[route(state)]
                else:
                    current = self.graph.edges.get(current, [END])[0]
                steps += 1
                if steps > 100:
                    raise RuntimeError("Fallback graph exceeded 100 steps")
            if self.checkpointer:
                self.checkpointer.states[thread_id] = dict(state)
            # remember last state for get_state()
            self._last_state = dict(state)
            return state

        def get_graph(self):
            """Return a lightweight graph view compatible with tests.

            The returned object exposes an `edges` iterable of objects with
            `source` and `target` attributes.
            """
            from types import SimpleNamespace

            edges = []
            for src, targets in self.graph.edges.items():
                for t in targets:
                    edges.append(SimpleNamespace(source=src, target=t))
            # include conditional edges
            for src, (_route, mapping) in getattr(self.graph, "conditionals", {}).items():
                for t in mapping.values():
                    edges.append(SimpleNamespace(source=src, target=t))
            return SimpleNamespace(edges=edges)

        def get_state(self, config=None):
            """Return a snapshot-like object with `tasks` containing
            interrupts whose `value` is the approval card (when available).
            This mirrors the minimal structure `pending_interrupt` expects.
            """
            from types import SimpleNamespace

            thread_id = ((config or {}).get("configurable") or {}).get(
                "thread_id", "default")
            state = (self.checkpointer.states.get(thread_id)
                     if self.checkpointer else self._last_state)
            tasks = []
            pending = state.get("pending_actions", []) if isinstance(state, dict) else []
            # Build interrupts for the pending actions; include the first one
            for draft in pending[:1]:
                card = dict(draft)
                # normalize keys to the approval card shape used elsewhere
                card.setdefault("action", draft.get("action_type"))
                card.setdefault("subject", draft.get("subject_id"))
                card.setdefault("site", draft.get("site_id"))
                card.setdefault("visit", draft.get("visit_id"))
                card.setdefault("protocol_version_governing_subject", draft.get("governing_version"))
                card.setdefault("will_write_to", f"sandbox/{draft.get('target_ledger')}.json")
                card.setdefault("proposed_classification", draft.get("proposed_classification"))
                card.setdefault("classification_reasoning", draft.get("classification_reasoning"))
                card.setdefault("classification_status", "PROPOSAL — the investigator decides, not this system")
                card.setdefault("evidence", draft.get("evidence") or [])
                card.setdefault("confirmation_required", draft.get("confirmation_level"))
                card.setdefault("required_token", draft.get("required_token"))
                tasks.append(SimpleNamespace(interrupts=[SimpleNamespace(value=card)]))
            return SimpleNamespace(tasks=tasks)
