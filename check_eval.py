import json
r = json.load(open('eval_test.json'))
m = r['metadata']
print(f'EVAL RESULTS: {m["passed"]}/{m["total_tests"]} passed ({100*m["pass_rate"]:.0f}%)')
