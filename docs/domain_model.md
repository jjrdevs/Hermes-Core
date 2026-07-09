# Hermes Domain Model

## Purpose

Document the core nouns and their relationships before implementation.

## Core entities

### Workflow

A workflow is the mission Hermes executes. Hermes separates workflow definitions from workflow execution state.

Workflow definitions contain:
- `workflow_id`
- `name`
- `created_at`
- `steps`
- `transitions`
- `policy_refs`

Workflow executions contain runtime state such as:
- `execution_id`
- `workflow_id`
- `started_at`
- `status`
- `active_executions`
- `completed_executions`
- `failed_executions`
- `produced_artifacts`
- `events`
- `policy_context`

States:
- `CREATED`
- `PLANNING`
- `EXECUTING`
- `WAITING_APPROVAL`
- `COMPLETED`
- `FAILED`

### Execution / Step

A step is a single execution unit inside a workflow.

Fields:
- `execution_id`
- `workflow_id`
- `step_id`
- `role`
- `objective`
- `started_at`
- `completed_at`
- `status`
- `inputs`
- `outputs`
- `recommendations`

Statuses:
- `PENDING`
- `RUNNING`
- `COMPLETED`
- `FAILED`
- `BLOCKED`
- `AWAITING_INPUT`

### Worker

A worker is a capability implementation that performs a role.

A worker:
- receives a `WorkerRequest`
- produces a `WorkerResponse`
- creates artifacts
- may recommend the next role or approval

Workers do not:
- schedule themselves
- manage workflow state
- persist artifacts directly

### Artifact

Everything Hermes produces is modeled as an immutable artifact.

Fields:
- `artifact_id`
- `artifact_type`
- `version`
- `title`
- `content`
- `content_hash`
- `artifact_hash`
- `created_by`
- `created_at`
- `inputs`
- `parent_version`
- `status`
- `decision_record`
- `metadata`

Rules:
- artifacts are immutable once stored
- new revisions create new artifact versions
- lineage is tracked through `inputs` and `parent_version`
- `content_hash` verifies payload immutability
- `artifact_hash` verifies the complete artifact record

### Event

An event records a state change or operation.

Fields:
- `event_id`
- `event_type`
- `timestamp`
- `workflow_id`
- `execution_id` (optional)
- `payload`

Common event types:
- `WORKFLOW_CREATED`
- `STEP_SCHEDULED`
- `WORKER_STARTED`
- `WORKER_COMPLETED`
- `ARTIFACT_CREATED`
- `WORKFLOW_TRANSITIONED`
- `APPROVAL_REQUESTED`
- `APPROVAL_GRANTED`

## Relationships

- A `Workflow` contains one or more `Step` executions.
- A `Step` is executed by a `Worker` with a given `role`.
- A `Worker` produces one or more `Artifact`s.
- `Artifact`s may depend on prior artifacts via lineage.
- `Event`s record changes to workflows, executions, and artifacts.

## Example sequence

1. Create workflow `build_oauth`.
2. Schedule architect step.
3. Architect worker produces `architecture_v1` artifact.
4. Hermes schedules developer step after dependency satisfied.
5. Developer worker produces `implementation_patch_v1` artifact.
6. Hermes requests QA approval or schedules QA step.
7. Workflow completes when final step finishes.
