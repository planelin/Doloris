# Contributing to AFK Supervisor

Thank you for your interest in contributing to AFK Supervisor!

## Architecture Overview

AFK is a task supervision and self-healing engine for long-running AI coding agents (Codex / Claude).
The system follows a two-tier design:
- **L1 Orchestrator / Watchdog** (`afk_supervisor/`):
  - Manages worker process lifecycles, heartbeats, hang detection, and safe handoffs.
  - Implements 3 modes:
    - Mode 1 (`afk.cmd`): Kill & Resume (safe app shutdown, headless resume in the same thread).
    - Mode 2 (`afk2.cmd`): Fork Headless (safe pause of parent, fork child thread, headless execution).
    - Mode 3 (`afk3.cmd`): Dual-Head GUI (UI Automation direct injection into desktop app).
  - Collects structured evidence and evaluates delivery acceptance.
- **L2 Delegated Intelligence** (`afk_supervisor/l2/`):
  - Resolves questions/decisions posed by workers without requiring human presence.
  - Automatically diagnoses and repairs common local/environment errors.

## Development Setup

1. **Prerequisites**:
   - Python 3.10+
   - Windows 10/11 (for UI Automation & Windows process integration)
   - PowerShell 5.1+

2. **Zero External Dependencies**:
   AFK is built entirely using Python's standard library. No `pip install` of third-party runtime libraries is required!

3. **Editable Installation**:
   ```bash
   pip install -e .
   ```

## Running Tests

All unit and regression tests are located in `tests/`.
We provide a hermetic isolated test runner that runs without touching real apps or acquiring real workspace locks:

```bash
python -B -X utf8 tests/run_isolated.py
```

Before submitting a Pull Request, make sure all tests pass:
```text
Ran 197 tests in ~8s
OK (failures=0, errors=0)
```

## Guidelines

- **Zero-Dependency Core**: Maintain zero external runtime dependencies. Rely on Python standard library modules.
- **Safe Boundary Checks**: Never bypass single-writer locks or file silence assumptions. All handoffs must be verified against structured rollout events.
- **Evidence-Based Acceptance**: Completion must be backed by tangible evidence (deliverables, checklists, test executions), not superficial completion claims.
- **Cross-Codepage Cleanliness**: Keep batch scripts (`*.cmd`) in pure ASCII to prevent issues with GBK/UTF-8 codepage mismatches on Windows command prompts.
