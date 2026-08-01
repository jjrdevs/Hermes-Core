# Memory layer implementation plan

## Goal

Add a persistent memory system that lets Hermes remember prior tasks, notes, facts, and failures so it can build on previous work instead of starting from scratch each time.

## Why this matters

Hermes already has event and artifact persistence, but that is mostly workflow state. For long-running or recurring work, the system also needs semantic memory: a way to recall what it learned, what failed before, and which patterns worked well.

## Proposed architecture

### Core components

- Memory entry: a structured record with content, type, source, timestamps, and relevance.
- Ingestion pipeline: writes new memory entries from events, notes, artifacts, and tool output.
- Retrieval layer: searches memory by task type, topic, or context.
- Memory context builder: assembles the relevant memories into the active task context.
- Retention policy: removes or summarizes stale or low-value memories.

## Implementation steps

### Phase 1: Define the memory schema

- Introduce a schema for:
  - notes
  - task summaries
  - implementation lessons
  - known constraints
  - earlier failures
- Store memory entries in SQLite or a simple JSON-backed local store for version 1.
- Avoid introducing a full vector database or a third-party memory system until retrieval quality clearly demands it.

### Phase 2: Ingest useful signals

- Automatically capture information from:
  - workflow summaries
  - tool outputs
  - verification failures
  - artifact metadata
  - user notes
- Avoid storing everything; focus on reusable facts and lessons.

### Phase 3: Add retrieval and context assembly

- Create a retrieval path that can search memory by:
  - topic
  - project
  - task type
  - recent recency
- Add a context builder that injects the most relevant memories into the next execution step.

### Phase 4: Add memory-driven behavior

- Use memory to:
  - avoid repeating the same failed approach
  - recall prior implementation patterns
  - attach relevant notes to new tasks
  - build a short-term working memory for active runs

### Phase 5: Add retention and summarization

- Keep memory size bounded.
- Periodically summarize older entries into compact notes.
- Allow the user to clear, pin, or manually edit memory entries.

## Deliverables

- Memory schema and persistence layer
- Ingestion pipeline from runtime events and tool output
- Retrieval layer and context assembly
- Retention and summarization policy
- A simple interface for manual memory updates

## Acceptance criteria

- The system can store a structured note or lesson from a completed run.
- A later run can retrieve that memory and use it to guide its next action.
- The memory layer remains bounded and does not grow indefinitely without summarization.

## Risks and mitigations

- Risk: memory becomes noisy and unhelpful.
  - Mitigation: store only high-value facts and summarize aggressively.
- Risk: retrieval quality is poor.
  - Mitigation: start with keyword + metadata retrieval before adding embeddings.
