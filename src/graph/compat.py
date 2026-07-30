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
except ImportError:  # lightweight offline fallback for this generated sandbox
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

        def invoke(self, input_state, config=None):
            state = dict(input_state)
            thread_id = ((config or {}).get("configurable") or {}).get("thread_id", "default")
            if self.checkpointer and thread_id in self.checkpointer.states:
                previous = dict(self.checkpointer.states[thread_id])
                previous.update(state)
                state = previous
            current = self.graph.edges[START][0]
            steps = 0
            while current != END:
                update = self.graph.nodes[current](state) or {}
                state.update(update)
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
            return state
