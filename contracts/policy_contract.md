# Policy Contract

## Purpose

Defines the rules Hermes Core uses to decide what can happen automatically, what requires approval, and what is forbidden.

## Policy model

A policy is a constraint on actions, resources, or workflow execution.

Example policy schema:

```yaml
- id: policy-001
  name: "Production access control"
  action: "modify_production_database"
  requires:
    - human_approval
  deny_if:
    - requester_role: developer
      without: ["security_auditor"]
```
```

## Policy types

- `requires` — actions needing approval or extra checks
- `deny_if` — forbidden conditions
- `allow_if` — explicit allow conditions
- `audit` — actions that must be logged

## Example policy

```yaml
- id: policy-002
  name: "Artifact publishing guard"
  action: "publish_artifact"
  conditions:
    - artifact_type: "production_manifest"
  requires:
    - human_approval
```
```

## Policy fields

- `id` — policy identifier
- `name` — human-readable label
- `action` — the action or event being governed
- `conditions` — optional filters on artifacts, roles, or workflow state
- `requires` — required approvals or checks
- `deny_if` — conditions that forbid the action
- `allow_if` — conditions that explicitly permit it

## Usage

- Hermesis evaluates policies before scheduling work or storing artifacts.
- Policy outcomes produce events like `POLICY_DENIED` or `APPROVAL_REQUIRED`.
- Policies are part of the operational control plane, not an afterthought.
