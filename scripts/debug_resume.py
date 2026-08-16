from pathlib import Path
import sys
from pathlib import Path as P
# ensure repo root is importable when running from scripts/
sys.path.insert(0, str(P('.').resolve()))
from src.graph.builder import build_agent_graph, initial_state, run_turn, resume_turn
from src.interpreter import RuleBasedInterpreter
from src.sandbox import Sandbox

root = Path('.')
interpreter = RuleBasedInterpreter()
graph = build_agent_graph(root, interpreter=interpreter, data_dir=root / 'data')
state = run_turn(graph, initial_state('t-debug'), 'review S-013')
print('pending actions:', len(state['pending_actions']))
routine = next(d for d in state['pending_actions'] if d['confirmation_level']=='plain')
print('routine id', routine['action_id'], 'target ledger', routine['target_ledger'])
state = run_turn(graph, state, f"approve {routine['action_id']}")
print('after approve, gate_passed', state.get('gate_passed'))
state = resume_turn(graph, state, 'yes')
print('after resume, applied_actions', state.get('applied_actions'))
print('sandbox version', Sandbox(root).version())
print('ledger contents length:', len(Sandbox(root).read(routine['target_ledger'])))
