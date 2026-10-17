# Octopus

**Towards Adaptive GUI Testing: Dynamic Task Planning and Execution for Mobile Apps**

Octopus is an LLM-powered approach to adaptive GUI testing of Android apps. It
adapts **what to test** by identifying semantic tasks from the current GUI, and
**how to test** by coordinating the devices needed to exercise those tasks.
Exploration results inform subsequent task planning through a graph-based memory.

This repository presents the approach and results described in the accompanying
manuscript, together with the current Python implementation. See
[implementation scope](docs/implementation-scope.md) for the correspondence
between the paper and this code snapshot.

## Table of Contents

- [Getting Started](#getting-started)
- [Adaptive GUI Testing](#adaptive-gui-testing)
- [Approach](#approach)
  - [Semantic Task Planning](#semantic-task-planning)
  - [Adaptive Task Execution](#adaptive-task-execution)
  - [Exploration State Management](#exploration-state-management)
- [Evaluation](#evaluation)
  - [RQ1: Performance of Octopus](#rq1-performance-of-octopus)
  - [RQ2: Comparison with Existing Approaches](#rq2-comparison-with-existing-approaches)
  - [RQ3: Real-world App Testing](#rq3-real-world-app-testing)
- [Repository Structure](#repository-structure)
- [Documentation and Tests](#documentation-and-tests)

## Getting Started

Use Python 3.10 or newer, Android platform tools with `adb` on your PATH, and
connected Android devices or emulators. The packaged code has been checked on
Windows with Python 3.13.12.

From the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Set your provider, model, and API key in `.env`. Check device connectivity with
`adb devices`, then run:

```powershell
# Replace the device IDs and package name with your experiment configuration.
.\.venv\Scripts\python.exe -m octopus --dip emulator-5554 emulator-5556 --app-package org.example --run-seconds 3600
```

The current runtime uses the connected devices supplied through `--dip`.
See the [setup and usage guide](docs/getting-started.md) for environment setup,
provider configuration, task options, and coverage prerequisites.

## Adaptive GUI Testing

An app screen can expose several meaningful functionalities. A tester might first
share a photo or inspect a contact, then send a message that requires another
participant, and finally explore reactions and replies made available by that
interaction. Octopus organizes exploration around such tasks and their changing
interaction requirements.

![Adaptive task-driven exploration in Teams](figures/motivating-example.jpg)

*Figure 1 from the manuscript: an example of adaptive task-driven exploration in Teams.*

## Approach

The paper describes three cooperating phases:

![Overview of Octopus](figures/overview.jpg)

*Figure 2 from the manuscript: semantic task planning, adaptive task execution,
and exploration state management.*

### Semantic Task Planning

Octopus derives candidate tasks from the current GUI and selects a representative
task using its relevance to the visible functionality and its novelty relative to
earlier exploration. The paper describes prefix expansion through beam search and
semantic similarity scoring for this selection process.

![Semantic task planning](figures/task-planning.jpg)

*Figure 3 from the manuscript: expanding task prefixes and assessing candidate tasks.*

Current code: [task planner](octopus/task_planner.py),
[task schema and ranking](octopus/task_schema.py).
The current snapshot uses one-call candidate generation and token-based scoring;
details are recorded in [implementation scope](docs/implementation-scope.md).

### Adaptive Task Execution

The selected task determines the participating devices, their roles, and the
intended outcomes. Execution translates the task into grounded GUI actions and
coordinates interactions when progress depends on another participant.

![Adaptive task execution](figures/task-execution.jpg)

*Figure 4 from the manuscript: identifying execution requirements and selecting
the next action from the current GUI.*

Current code: [task dispatcher](octopus/task_dispatcher.py),
[device agent](octopus/device_agent.py),
[action executor](octopus/action_executor.py), and
[event bridge](octopus/event_bridge.py).

### Exploration State Management

The paper uses GUI observations, interaction events, and available system logs to
assess task outcomes and identify potential bugs. Its exploration memory connects
GUI states and task executions to inform subsequent exploration.

Current code: [outcome verifier](octopus/outcome_verifier.py),
[memory store](octopus/services/memory_store.py), and
[session state](octopus/session_state.py). This snapshot verifies structured
conditions with rules and records outcomes as `success`, `failure`, or `unknown`.
Confirmed bugs require separate validation.

See [architecture and runtime behavior](docs/architecture.md) for the implemented
task format, action primitives, memory lifecycle, and configuration switches.

## Evaluation

The following results are reported in the supplied manuscript, rather than
measurements newly produced by this repository package. The study evaluates
**59 Android apps from 15 categories**, with **60 minutes per run** and
**three independent runs** for each experimental configuration.

### RQ1: Performance of Octopus

**How effective is Octopus in automatically exploring app functionalities through
semantic task-driven exploration?**

The paper compares the complete approach with three ablation variants. The table
below reproduces the average columns of its Table 1. All values are percentages.

| Approach | Activity coverage | Method coverage | Class coverage | Task success rate |
| --- | ---: | ---: | ---: | ---: |
| Without Task Selection | 57.89 | 33.95 | 43.51 | 74.65 |
| Without Adaptive Execution | 59.32 | 30.63 | 40.21 | 76.31 |
| Without Exploration Memory | 56.18 | 29.12 | 38.56 | 79.04 |
| **Octopus** | **63.78** | **37.85** | **46.01** | **82.75** |

The complete approach has the highest reported average on all four metrics.
The code's available configuration switches are documented in
[architecture](docs/architecture.md#ablations); they are not one-to-one
implementations of the three paper ablations.

### RQ2: Comparison with Existing Approaches

**How does Octopus compare with existing state-of-the-art automated app testing approaches?**

The seven baselines are Android Monkey, DroidBot, APE, Humanoid, Fastbot2,
DroidAgent, and LLMDroid. The manuscript reports the following final average
coverage for Octopus and the strongest overall baseline, DroidAgent:

| Approach | Activity coverage | Method coverage | Class coverage |
| --- | ---: | ---: | ---: |
| DroidAgent | 60.33% | 30.40% | 43.21% |
| **Octopus** | **63.78%** | **37.85%** | **46.01%** |
| Absolute improvement | +3.45 pp | +7.45 pp | +2.80 pp |

Here, `pp` means percentage points. Values are transcribed from Section 3.2 of
the manuscript; this table presents final coverage, not within-run trajectories.

### RQ3: Real-world App Testing

**How useful is Octopus in identifying real-world app issues through automated exploration?**

The paper reports **42 detected bugs out of 65 manually confirmed bugs**, with
**50.60% precision**, **64.62% recall**, and **56.76% F1-score**.

| Bug category | Detected / confirmed | Recall |
| --- | ---: | ---: |
| Crash / abnormal termination | 11 / 11 | 100.00% |
| Functional failure | 15 / 26 | 57.69% |
| Incorrect output | 4 / 5 | 80.00% |
| State inconsistency | 3 / 8 | 37.50% |
| Other bugs | 9 / 15 | 60.00% |
| **Total** | **42 / 65** | **64.62%** |

![Examples of app bugs illustrated in the manuscript](figures/bug-examples.png)

*Examples from Figure 6: a crash in Douyin Lite, peer-discovery failure in
Warpinator, unsuccessful search in Deku-SMS, inconsistent poll-creator labels in
Jitsi, and divergent shared-list state in KitchenOwl.*

For result-file formats and measurement definitions in the current code, see
[evaluation outputs](docs/evaluation.md). Image and result sources are listed in
[figure provenance](figures/README.md).

## Repository Structure

```text
README.md                  Project overview, method, and paper-reported results
docs/
  getting-started.md        Installation, configuration, and execution
  architecture.md           Implemented task and execution model
  evaluation.md             Output files and metric definitions
  implementation-scope.md  Paper-to-code correspondence
figures/                    Figures extracted from the supplied manuscript
octopus/
  __main__.py               Command-line entry point
  session.py                Testing session orchestration
  task_planner.py           Task generation and selection
  task_dispatcher.py        Task assignment across participants
  device_agent.py           Grounded device decisions
  action_executor.py        Action execution and feedback
  outcome_verifier.py       Structured outcome verification
  services/                 Android, model, memory, coverage, and tracing
tests/                      Automated regression tests
```

## Documentation and Tests

- [Setup and usage](docs/getting-started.md)
- [Architecture](docs/architecture.md)
- [Evaluation outputs](docs/evaluation.md)
- [Implementation scope](docs/implementation-scope.md)
- [Figure and result sources](figures/README.md)

Run the automated tests from the repository root:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

These tests exercise local logic, mocked execution, and result serialization.
Real-device experiments are needed to validate end-to-end testing behavior.
