# Human Contract

## Purpose

Defines how humans interact with Hermes Core as actors in the operational loop.

## Role of humans

Humans provide intent, constraints, validation, and corrections.
They do not directly manipulate workflow execution state.

## Human actions

Humans can:

- submit objectives and goals
- define or adjust constraints
- approve or reject actions
- contribute artifacts
- modify policies
- correct workflow interpretation

## Human event model

Humans generate events such as:

- `HUMAN_GOAL_SUBMITTED`
- `HUMAN_CONSTRAINT_UPDATED`
- `HUMAN_APPROVAL_GRANTED`
- `HUMAN_APPROVAL_DENIED`
- `HUMAN_ARTIFACT_SUBMITTED`
- `HUMAN_POLICY_OVERRIDE`

## Human actor shape

Example human actor reference:

```json
{
  "actor_type": "human",
  "actor_id": "user_123",
  "name": "Alice",
  "role": "engineering_manager"
}
```

## Human vs worker

- Humans are not workers.
- Humans are sources of intent, constraints, and oversight.
- Workers are execution capabilities selected by Hermes.

## Interaction contract

Human actions are handled by Hermes through structured inputs, approvals, and policy decisions.

Example interaction:

```json
{
  "type": "HUMAN_APPROVAL",
  "workflow_id": "uuid",
  "execution_id": "execution_83281",
  "actor": {
    "actor_type": "human",
    "actor_id": "user_123",
    "name": "Alice"
  },
  "decision": "approved",
  "notes": "Ready to deploy after security review."
}
```

## Principles

- Hermes records human input as events.
- Hermes evaluates human decisions against policies.
- Humans can influence but do not directly override the runtime state outside Hermes.
