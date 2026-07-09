# Workflow Contract

## Purpose

Defines how Hermes Core models workflows, steps, dependencies, and transitions.

## Workflow representation

A workflow is the mission Hermes executes. Hermes separates the workflow definition from runtime execution state.

### Workflow definition

A workflow definition contains the declared process. It is immutable and does not own runtime history.

- `workflow_id`
- `name`
- `created_at`
- `steps`
- `transitions`
- `policy_refs`

### Workflow execution

A workflow execution tracks the runtime state of a workflow definition. It contains:

- `execution_id`
- `workflow_id`
- `status`
- `started_at`
- `completed_at`
- `active_executions`
- `completed_executions`
- `failed_executions`
- `produced_artifacts`
- `events`
- `policy_context`

## Workflow states

- `CREATED`
- `PLANNING`
- `EXECUTING`
- `WAITING_APPROVAL`
- `COMPLETED`
- `FAILED`

## Step model

Each workflow step is a single unit of execution assigned to a worker role.

Example step schema:

```yaml
- id: architect-1
  role: architect
  objective:
    description: "Design the OAuth architecture"
    acceptance_criteria:
      - "Supports refresh tokens"
      - "Matches project security constraints"
  depends_on: []
  outputs:
    - architecture_v1
```
```

Fields:

- `id` — stable step identifier
- `role` — worker capability
- `objective` — instruction package
- `depends_on` — artifact or step references
- `outputs` — expected artifact ids or types
- `constraints` — optional runtime boundaries

## Transition model

Hermes should schedule work based on observed events and artifact state rather than hardcoded step-to-step triggers.

Transition rules may include explicit priority to arbitrate conflicting actions.

Example transition rule:

```yaml
transitions:
  - priority: 100
    condition:
      event: ARTIFACT_CREATED
      artifact_type: production_change
    action:
      require_approval:
        role: human_operator
  - priority: 50
    condition:
      event: STEP_COMPLETED
      artifact_type: architecture
    action:
      schedule:
        role: developer
  - priority: 40
    condition:
      event: STEP_COMPLETED
      artifact_type: implementation_patch
    action:
      schedule:
        role: qa
```
```

This allows Hermes to react to events and capability requirements instead of rigid pipelines, with deterministic arbitration when multiple transitions apply.

## Flexible workflow structure

Hermes workflows should be graphs, not rigid linear pipelines.
Real operational work can include:

- explicit dependency graphs
- conditional transitions
- approval gates
- optional branches
- parallel work
- event-driven scheduling

A workflow should represent both a simple sequence and a dynamic operational process.

Example flexible workflow:

```yaml
workflow_id: "uuid"
name: "build_oauth"
created_at: "2026-07-07T12:00:00Z"
steps:
  - id: architect-1
    role: architect
    objective:
      description: "Define the OAuth architecture"
      acceptance_criteria:
        - "Support refresh token flow"
    depends_on: []
    outputs:
      - architecture_v1
  - id: developer-1
    role: developer
    objective:
      description: "Implement OAuth support"
      acceptance_criteria:
        - "All tests pass"
    depends_on:
      - architecture_v1
    outputs:
      - implementation_patch_v1
  - id: security-1
    role: security_auditor
    objective:
      description: "Review OAuth implementation for security"
      acceptance_criteria:
        - "No insecure auth flows"
    depends_on:
      - implementation_patch_v1
    outputs:
      - security_report_v1
  - id: qa-1
    role: qa
    objective:
      description: "Validate OAuth implementation"
      acceptance_criteria:
        - "No regressions introduced"
    depends_on:
      - implementation_patch_v1
    outputs:
      - qa_report_v1
transitions:
  - condition:
      event: STEP_COMPLETED
      artifact_type: architecture
    action:
      schedule:
        role: developer
  - condition:
      event: STEP_COMPLETED
      artifact_type: implementation_patch
    action:
      schedule:
        role: qa
  - condition:
      event: STEP_COMPLETED
      artifact_type: implementation_patch
    action:
      schedule:
        role: security_auditor
```
```

## Execution model

- Hermes schedules steps when dependencies are satisfied and transition conditions are met.
- Hermes reacts to events, artifact state, and policy outcomes.
- Steps may be conditional and can branch based on execution results.
- Hermes may insert approval steps or human gates as explicit workflow steps.
- Each step produces artifacts and may recommend next roles.
- Hermes owns workflow transitions and decides whether to follow worker recommendations.
