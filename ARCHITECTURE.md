# Architecture

## Responsibility split

| Concern | Owner |
|---|---|
| Intent and conversational references | LLM boundary / offline mock |
| ID matching, schedules, calculations | Deterministic Python |
| Proposal construction | Deterministic Python |
| Approval | Human chat action |
| Sandbox writes and rollback | Deterministic executor |
| Explanation | Structured evidence rendered to text |

## Graph

```text
START → classify_intent
  ├─ investigate → reconciliation → proposal(s) → END
  ├─ follow_up → answer from state → END
  ├─ knowledge → Key Concepts registry → END
  ├─ approve → strict gate → apply → verify → audit → END
  ├─ reject → update proposal → audit → END
  ├─ rollback → compensating action → audit → END
  └─ audit_query → exact structured filter → END
```

## Safety invariants

1. Structured financial records are matched by exact identifiers and periods.
2. The LLM boundary cannot produce arbitrary write payloads.
3. Proposal data is stored, versioned, and hashed before approval.
4. Approval references the exact stored proposal.
5. A stale sandbox version blocks execution.
6. `apply` and rollback are idempotent.
7. JSON files are written atomically.
8. Reconciliation runs after mutation.
9. Audit history is append-only; rollback does not erase history.
