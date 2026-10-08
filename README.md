# octopus

Research implementation for task-driven testing of Android applications across
multiple devices. The runtime observes device state, generates structured task
candidates, executes grounded actions, and verifies outcomes using state and
peer-event evidence.

This repository contains the implementation and automated tests. Experiment
results, paper drafts, APKs, credentials, and local temporary files are excluded.
Project configuration variables use the `OCTOPUS_` prefix; see `.env.example`.

## Setup

Use Python 3.10 or newer. Install Android platform tools and ensure `adb` is on
your PATH. Connect the Android devices or emulators you intend to test.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` to select your provider and model and supply your own API key.
The OpenAI-compatible client supports the `openai` and `gemini` provider modes.
Do not commit `.env` or API keys.

The current snapshot was checked on Windows with Python 3.13.12. Exact package
versions from that environment are recorded in `requirements-lock.txt`; use
`python -m pip install -r requirements-lock.txt` to install those versions.

Confirm devices are available with `adb devices`. Configure each device for
uiautomator2 before running an experiment.

## Run

Run commands from the repository root (the directory containing this README).

```powershell
.\.venv\Scripts\python.exe -m octopus --help

# Replace the device IDs and package with your experiment configuration.
.\.venv\Scripts\python.exe -m octopus --dip emulator-5554 emulator-5556 --app-package org.example --run-seconds 3600
```

Omitting `--task` enables automatic task generation. Use `--task` to provide an
initial task. `--multi-task` continues with generated tasks after the initial
episode. `--apk` can install an APK on the selected devices; APK-based method and
class coverage requires AndroidLog instrumentation points. The log tag
`MADROID_COVERAGE` is retained to read existing instrumented APKs.

Optional action, LLM-call, and token budgets are available through `--max-actions`,
`--max-llm-calls`, and `--max-total-tokens`.

## Evaluation and ablations

Results are written under `logs/<app>/<run_id>/`. See
[logging documentation](docs/evaluation.md) for metric definitions, coverage
denominators, task deduplication, and detailed trace options. Method/class
coverage measures instrumentation targets; unavailable denominators are reported
as unavailable rather than zero. Real defects require manual confirmation.

Use the same devices, app, model, and budget when comparing:

| Option | Behavior |
| --- | --- |
| `--candidate-count 1` | Generate one task candidate |
| `--no-relevance` | Disable current-screen relevance ranking |
| `--no-diversity` | Disable the repetition penalty and task-history prompt context |
| `--no-effect-feedback` | Disable structured effect prediction feedback |
| `--verification-rounds 0` | Disable additional verification observations |

## Tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

The automated tests exercise task validation, memory, evidence verification,
execution budgets, and evaluation output using mocks and local fixtures.
They do not replace experiments on actual Android devices.

## Code layout

- `octopus/__main__.py`: command-line entry point.
- `octopus/session.py`: campaign lifecycle and device orchestration.
- `octopus/task_dispatcher.py` and `octopus/device_agent.py`: task coordination and actions.
- `octopus/task_planner.py`, `action_executor.py`, and `outcome_verifier.py`: task planning, execution, and verification.
- `octopus/services/`: Android control, model access, memory, coverage, and tracing.
- `tests/`: automated regression tests.
- `docs/architecture.md`: implementation contract and current design choices.
