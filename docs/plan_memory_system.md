# Implementation Plan: Memory System

## Objective

Add a practical memory layer that helps the agent retain useful context across turns and runs without polluting the prompt with irrelevant or stale material. This is a high-value feature, but it is also one of the hardest to get right.

## Why this matters

The current core runtime already carries context through [engine/runtime_service.py](../engine/runtime_service.py) and [engine/models.py](../engine/models.py), but that is not the same thing as a real memory system. A true memory system must decide:

- what to remember
- what to forget
- what should be surfaced now
- what should remain hidden from the current prompt

That is where much of the quality difference between a toy agent and a useful one appears.

## Design goals

1. Preserve useful facts across runs and sessions.
2. Avoid prompt poisoning from too much context.
3. Keep memory retrieval explicit and inspectable.
4. Make the system safe for long-running, multi-step workflows.

## Proposed architecture

### 1. Memory store abstraction

Introduce a memory abstraction that supports:

- write operations for facts, summaries, and decisions
- retrieval by relevance or recency
- retention policies
- scope tags such as run, session, project, or task

This should be a separate layer from the workflow runtime so it can evolve independently.

### 2. Memory selection policy

A memory system should not blindly inject everything it knows. Add selection rules that decide which memory fragments are relevant to the current request. The selection process should consider:

- recent activity
- task similarity
- explicit user instructions
- known constraints
- recency and confidence

### 3. Memory summarization

Store compact summaries rather than raw conversation history whenever possible. This helps keep the prompt bounded while preserving long-term context.

### 4. Memory visibility and control

The agent or user should be able to inspect and manage memory. That can start with:

- list memory entries
- clear stale entries
- mark entries as private or public
- attach memory to specific runs

## Implementation phases

### Phase 1 — Introduce a memory abstraction

- add a memory storage interface and a simple in-process implementation
- bind it to the runtime context and run lifecycle
- persist memory entries alongside run metadata

Acceptance criteria:

- the system can store and retrieve memory entries
- memory entries are scoped to a run or session

### Phase 2 — Add retrieval and selection heuristics

- select the most relevant memory fragments for each new step
- limit the number of inserted memory items
- rank by recency, relevance, and confidence

Acceptance criteria:

- the prompt receives a bounded and useful memory slice
- the system does not explode context size with irrelevant history

### Phase 3 — Add summarization and retention policy

- compress older details into higher-level summaries
- expire stale memory items based on age or relevance decay
- keep important facts even when details are old

Acceptance criteria:

- memory remains useful over time
- old context does not dominate the current prompt

### Phase 4 — Add memory events and observability

- emit events when memory is stored, retrieved, or evicted
- expose memory usage via run or session metadata

Acceptance criteria:

- the agent can explain why it is using memory
- debugging memory behavior is practical

## Files to change

- [engine/models.py](../engine/models.py)
- [engine/runtime.py](../engine/runtime.py)
- [engine/runtime_service.py](../engine/runtime_service.py)
- [engine/storage.py](../engine/storage.py)
- [engine/api.py](../engine/api.py)

## Tests

Add tests for:

- storing and retrieving memory entries
- limiting prompt size from memory injection
- retention and summarization behavior
- memory scoping across runs and sessions
- restart recovery of memory state

## Estimated effort

- 2 to 6 weeks depending on how much sophistication is desired
- the first useful version should be much smaller than a full memory research system

## Definition of done

This work is complete when the agent can retain useful context over time without overwhelming the prompt, and when memory usage is explicit, inspectable, and recoverable.
