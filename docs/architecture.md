# octopus implementation contract

octopus tests an Android app as a continuous sequence of structured tasks. Device state is
preserved between tasks so later tasks can reuse accounts, permissions, files, contacts, and
screens established earlier in the session.

## Closed loop

1. Observe every available device and retrieve Exploration Memory and Task Memory.
2. Generate up to five structured candidates in one model call and validate their schema locally.
3. Rank by current-screen relevance minus historical screen/task repetition. Keep unselected
   candidates with their source screen and generate fresh candidates when the screen changes.
4. Execute one grounded primitive at a time on exactly one active device.
5. Verify frozen conditions without an LLM, optionally refresh the participant with missing
   evidence, commit persistent evidence, and clear only Step Memory.

The paper is a design reference. This implementation uses token cosine similarity instead of
Sentence-BERT, one candidate-generation call instead of beam expansion, and rule-based evidence
checks instead of a visual LLM oracle. Repetition is the maximum product of screen similarity
and task similarity over previous completed attempts. No new model dependency is required.
Cached alternatives are reconsidered on returning to their source screen; there is no automatic
DFS navigation or path replay.

## Task schema

Each task contains a global goal, participating devices, a local goal for each participant, the
first device, grounding information, and verification conditions. Conditions support
`foreground_activity`, `visible`, and `peer_event`. A task is multi-device only when its result
depends on an observable peer effect. The planner never emits a GUI action sequence.

## Runtime rules

Device agents select exactly one of:

```text
[tap] [component]
[input] [component] [value]
[back]
[switch] [device_number] [message]
[end_task]
[nop]
```

Targets are resolved against the current XML before execution. Two consecutive grounding
failures or three consecutive grounded GUI actions without task-relevant progress terminate the
attempt. `nop` waits for a matching peer event and does not consume the GUI-action budget.
Peer events follow `pending -> claimed -> consumed` or `pending -> expired`, and are consumed at
most once.

With effect feedback enabled, the operator returns JSON containing `objective`, `action` (one
primitive above), and `expected_effect` (a structured condition). The runtime checks that effect
on the indicated participant and records observed support or unresolved evidence in Step Memory.
Predictions never establish task completion. Bare action responses remain accepted.

State conditions require two consecutive matching observations of the specified device. Event
conditions use events recorded since this task started, regardless of routing status. Verification
refreshes only a participant with unmet conditions, up to two extra observations (0.2 s wait each),
within the remaining session time. Missing/unstable evidence stays unknown; persistently unmatched
conditions fail. Verification returns `success`, `failure`, or `unknown` and makes no model call.

## Ablations

Use the same app, devices, model, and budget for each variant. Each task result records `method`.

| CLI option | Change |
| --- | --- |
| `--candidate-count 1` | Generate a single candidate instead of five |
| `--no-relevance` | Remove current-screen relevance from ranking |
| `--no-diversity` | Remove repetition penalty and task-history prompt context |
| `--no-effect-feedback` | Use bare actions and existing GUI/peer progress checks |
| `--verification-rounds 0` | Disable additional evidence collection; retain the same outcome conditions |

Options can be combined. `octopus.experiment_config.MethodOptions` exposes the same switches to Python callers.

## Main modules

- `octopus/__main__.py`: minimal command-line entry point.
- `octopus/session.py`: campaign lifecycle and device-worker orchestration.
- `octopus/task_dispatcher.py`: converts an already planned task record into device-local goals.
- `octopus/device_agent.py`: produces and grounds one strict runtime action.
- `octopus/app_lifecycle.py`: package resolution, installation, launch, and activity discovery.
- `octopus/event_bridge.py`: accessibility/logcat collection and peer-event routing.
- `octopus/action_executor.py`: grounded action execution, scheduling, and progress tracking.
- `octopus/task_planner.py`: one-call task generation and screen-scoped candidate selection.
- `octopus/reporting.py`: run paths, statistics, and result serialization.
- `octopus/session_state.py`: budgets, execution ledger, and switch state.
- `octopus/task_schema.py`: task schema, fingerprints, validation, and deterministic selection.
- `octopus/outcome_verifier.py`: rule-based outcome verification.
- `octopus/ui_context.py`: UI hierarchy reduction and state signatures.
- `octopus/services/memory_store.py`: Step, Exploration, Task, and peer-event memory.

## Evaluation output

See `docs/evaluation.md` for metric definitions and output files. Detailed traces and exploration
exports are off by default (`--detailed-log` enables them). Compact task records, per-run metrics,
and an incremental coverage CSV are always retained. Method/class coverage uses instrumentation
targets; absent static denominators are unavailable, not zero.
