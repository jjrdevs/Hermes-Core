# Tool Contract

## Purpose

Defines how external resources and tools are represented and authorized in Hermes Core.

## Tool shape

A tool describes an external capability Hermes can access.

Example tool schema:

```yaml
tool:
  name: filesystem
  capabilities:
    - read_files
    - write_files
  permissions:
    scope:
      - repository/
  audit:
    required: true
```

## Fields

- `name` — stable tool identifier
- `capabilities` — actions the tool can perform
- `permissions` — scope restrictions and allowed targets
- `audit` — whether access requires auditing
- `metadata` — optional tool-specific information

## Purpose

This contract answers:

- What can Hermes touch?
- What capabilities does the tool expose?
- What scope restrictions apply?
- What auditing is required?

## Example tools

- `filesystem`
- `git`
- `test_runner`
- `deployment`
- `api_client`
- `database`

## Usage

- Workers request tool capabilities.
- Hermes resolves authorized tools based on policy and permissions.
- Tools are invoked through Hermes, not directly by workers.

## Policy integration

Tool access is subject to Hermes policies.
Example:

```yaml
policy:
  name: production_filesystem_access
  rules:
    - action: write_files
      tool: filesystem
      permissions:
        scope:
          - /workspace/project
      requires:
        - human_approval
```

This contract makes external capabilities explicit and auditable.
