# Implementation Plan: Evaluation and Self-Improvement Loop

## Objective

Introduce a lightweight evaluation layer that lets Hermes Core or the surrounding agent loop judge whether a run is progressing, failing, or should be revised. This is the later-stage capability that turns an agent from “works on tasks” into “improves its own behavior over time.”

## Why this matters

The earlier steps improve execution, planning, and safety. This step adds the ability to judge outcomes and adapt. That is what makes the system feel more autonomous and less brittle.

## Design goals

1. Evaluate progress with a simple and reliable signal.
2. Keep the loop lightweight enough to run in normal workflows.
3. Avoid overfitting to a complex research-grade self-improvement system too early.
4. Make evaluation actionable rather than just descriptive.

## Proposed architecture

### 1. Outcome evaluation

After each major execution chunk, evaluate:

- did the expected artifact appear?
- did the tool outcome meet the expected contract?
- is there evidence of progress toward the goal?
- is the current plan still viable?

This can be implemented as a small rubric with structured checks rather than a fully generative evaluator.

### 2. Failure classification

Classify each failure into categories:

- transient tool failure
- plan mismatch
- policy violation
- missing input
- blocked by approval
- dead-end strategy

This classification drives the next action.

### 3. Recovery and revision policy

Depending on the evaluation result:

- retry with a different tactic
- ask for clarification
- escalate for approval
- mark the run as blocked
- end successfully

### 4. Learning loop

The most basic version of self-improvement can store simple learned patterns such as:

- which tool combinations tend to work
- which prompts or plan structures tend to fail
- which recovery strategies are effective

This should remain lightweight and should not require a large training pipeline.

## Implementation phases

### Phase 1 — Add evaluation hooks

- define an evaluation result structure in [engine/models.py](../engine/models.py)
- add evaluation points after tool execution and after subgoal completion
- emit evaluation events into the run history

Acceptance criteria:

- every major step can be evaluated
- evaluation results are persisted and inspectable

### Phase 2 — Add failure classification and recovery policy

- classify failures into a small fixed set of categories
- route each category to a recovery action
- preserve recovery decisions in the run event stream

Acceptance criteria:

- failed runs can move into a recovery path instead of simply failing hard
- the runtime can explain why it chose a particular recovery strategy

### Phase 3 — Add self-revision support

- let the planner revise the next action based on the evaluation result
- support a small number of revision attempts before blocking the run

Acceptance criteria:

- the loop can recover from mild execution issues without user intervention
- repeated failures trigger a controlled stop rather than infinite retries

### Phase 4 — Add lightweight learning memory

- log successful and failed strategies
- reuse successful patterns in future runs where appropriate
- keep the memory bounded and explainable

Acceptance criteria:

- the system improves over time in a narrow and observable way
- the learning loop does not create hidden or unstable behavior

## Files to change

- [engine/models.py](../engine/models.py)
- [engine/runtime.py](../engine/runtime.py)
- [engine/runtime_service.py](../engine/runtime_service.py)
- [engine/storage.py](../engine/storage.py)
- [workers/model_adapter.py](../workers/model_adapter.py)

## Tests

Add tests for:

- evaluation after successful tool execution
- evaluation after failed tool execution
- recovery policy for each failure class
- revision after a failed plan step
- persistence of evaluation history across restart

## Estimated effort

- 2 to 4 weeks for a basic evaluation loop
- 4 to 8 weeks for a more sophisticated self-improvement system

## Definition of done

This work is complete when the runtime can evaluate progress, classify failures, select recovery actions, and use that information to improve future behavior in a controlled and explainable way.
