# Planner decomposition contract violation

## Summary

The planner currently accepts degenerate plans that violate the intended decomposition contract: a single sub-task is accepted, and plans without a `verify` sub-task are also accepted.

This is inconsistent with the planner prompt and runtime expectation that a task should decompose into 2–5 focused sub-tasks and always include at least one verify step.

## Current behavior

A raw planner response like the following is currently treated as valid:

```json
[
  {
    "type": "code",
    "capability": "code",
    "goal": "write a function",
    "instructions": "do it"
  }
]
```

The planner then emits a single child packet and reports success even though this violates the contract.

## Expected behavior

The planner should reject plans that:

- have fewer than 2 sub-tasks
- have no sub-task with `capability == "verify"`

and trigger the standard reflect path instead of silently emitting a degenerate plan.

## Evidence

- [skills/planner_v1.md](../skills/planner_v1.md) says: prefer 3 sub-tasks and always include at least one verify sub-task.
- [src/contracts.py](../src/contracts.py) wraps single task objects into a one-item list and treats them as valid.
- [src/agents/planner.py](../src/agents/planner.py) then emits those packets without additional validation.

## Impact

This allows weak planner output to pass through the system, producing trivially decomposed tasks that skip the intended verification loop and reduce the quality of the swarm's execution path.
