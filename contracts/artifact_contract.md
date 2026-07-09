# Artifact Contract

## Purpose

Defines the immutable object Hermes Core uses to represent work products, decisions, and outputs.

## Artifact identity

Each artifact must include:

- `artifact_id` — stable immutable identity
- `artifact_type` — classification such as `requirements`, `architecture`, `code_patch`, `test_report`, `decision_record`
- `version` — revision counter or explicit revision label
- `created_at` — timestamp
- `created_by` — reference to the execution or actor that produced the artifact

The artifact identity is independent from the version. The same `artifact_id` can have multiple revisions over time.

artifact_id identifies the conceptual artifact lineage.

version identifies an immutable revision within that lineage.

Example:

artifact_id:
oauth_architecture

versions:

v1
v2
v3

Each version has:

content_hash
artifact_hash

## Artifact shape

Example artifact schema:

```json
{
  "artifact_id": "8f92b31",
  "artifact_type": "architecture",
  "version": 3,
  "artifact_hash": "sha256:xxxxxxxx...",
  "title": "OAuth design",
  "content": "...",
  "created_by": {
    "type": "execution",
    "id": "execution_83281"
  },
  "created_at": "2026-07-07T12:00:00Z",
  "inputs": ["requirements_4f3a2d"],
  "parent_version": 2,
  "status": "completed",
  "decision_record": {
    "objective": "Design OAuth architecture",
    "constraints": [
      "Existing clients must remain compatible",
      "Must remain stateless"
    ],
    "evidence_refs": [
      "requirements_4f3a2d",
      "security_policy_7c1b0f"
    ],
    "alternatives": [
      {
        "option": "Session authentication",
        "rejected_reason": "Requires server-side state"
      },
      {
        "option": "OAuth provider delegation",
        "rejected_reason": "Adds external dependency"
      }
    ]
  },
  "metadata": {
    "confidence": 0.86,
    "source": "ollama"
  }
}
```

## Fields

- `title` — human-readable artifact label
- `content` — payload, which may be text or structured JSON
- `content_hash` — digest of the artifact payload/content only
- `artifact_hash` — digest of the complete immutable artifact record, including content, metadata, and decision_record
- `created_by` — reference to the execution or actor that produced the artifact
- `inputs` — references to artifact ids used as inputs
- `parent_version` — previous revision number for the same artifact identity
- `status` — lifecycle state: `pending`, `completed`, `failed`, `blocked`
- `decision_record` — high-level record of why the artifact exists
- `metadata` — optional provider/runtime hints

## Rules

- Artifacts are immutable once created.
- New revisions create new artifact versions under the same `artifact_id`.
- Hermes Core owns artifact persistence and lineage.
- The artifact represents the result; the decision record captures why it exists.
- `artifact_hash` is a digest of the complete immutable artifact record.
- `content_hash` is a digest of only the artifact payload/content.
- Metadata changes should not imply content changes.
- Artifact versions remain immutable and lineage is preserved through `parent_version`.
- Decision records stay high-level and should not include raw model chain-of-thought.
- Workers may suggest artifact metadata and decision records, but Hermes validates and stores artifacts.

## Examples

- `requirements_v1`
- `architecture_v1`
- `code_patch_v3`
- `test_report_v2`
- `decision_record_v4`
