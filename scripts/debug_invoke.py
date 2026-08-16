import sys
from pathlib import Path
sys.path.insert(0, str(Path('.').resolve()))
from src.graph.builder import build_agent_graph, initial_state, run_turn
from src.interpreter import RuleBasedInterpreter
from src.sandbox import Sandbox
from src.graph.compat import Command

root = Path('.')
interpreter = RuleBasedInterpreter()
graph = build_agent_graph(root, interpreter=interpreter, data_dir=root / 'data')
state = run_turn(graph, initial_state('t-debug'), 'review S-013')
print('pending:', len(state['pending_actions']))
routine = next(d for d in state['pending_actions'] if d['confirmation_level']=='plain')
print('id', routine['action_id'])
state_after = run_turn(graph, state, f"approve {routine['action_id']}")
print('after approve gate_passed:', state_after.get('gate_passed'))
# now directly invoke resume via graph.invoke
res = graph.invoke(Command(resume='yes'), config={'configurable': {'thread_id': state['thread_id']}})
print('raw invoke result keys:', list(res.keys()))
print('applied_actions in result:', res.get('applied_actions'))
print('sandbox version', Sandbox(root).version())
print('ledger len', len(Sandbox(root).read(routine['target_ledger'])))
