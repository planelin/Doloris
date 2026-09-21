# Contributing to Doloris (ドロリス)

Thank you for your interest in contributing to **Doloris**!

## Architecture Overview

Doloris is an autonomous task supervision, watchdog, self-healing, and desktop-companion engine for long-running AI coding agents (Codex / Claude).
The system follows a decoupled design:
- **L1 Orchestrator / Watchdog** (`afk_supervisor/`):
  - Manages worker process lifecycles, heartbeats, hang detection, and safe handoffs.
  - Implements 3 modes:
    - Mode 1: Fork Headless (`doloris fork` / `afk2.cmd`): Safe pause of parent task, fork child thread, keep App alive.
    - Mode 2: Kill & Resume (`doloris resume` / `afk.cmd`): Safe app shutdown, headless resume in same thread.
    - Mode 3: Dual-Head GUI (`doloris gui` / `afk3.cmd`): UI Automation direct injection into desktop app.
  - Collects structured evidence and evaluates delivery acceptance.
- **L2 Delegated Intelligence** (`afk_supervisor/l2/`):
  - Resolves questions/decisions posed by workers without requiring human presence.
  - Automatically diagnoses and repairs common local/environment errors.
- **Desktop Companion & BYOP (Phase 2)**:
  - Bring Your Own Pet (BYOP) sprite mapping protocol and desktop mascot overlay engine (`doloris_app/`).
  - Procedural vector rendering and dynamic multi-state animation support.

## Development Setup

1. **Prerequisites**:
   - Python 3.10+
   - Windows 10/11 (for UI Automation & Windows process integration)
   - PowerShell 5.1+

2. **Zero External Dependencies**:
   Doloris core is built entirely using Python's standard library. No `pip install` of third-party runtime libraries is required! (Optional: `pip install Pillow` for Desktop Mascot GUI).

3. **Editable Installation**:
   ```bash
   pip install -e .
   ```

## Running Tests

All unit and regression tests are located in `tests/`.
We provide a hermetic isolated test runner that runs without touching real apps or acquiring real workspace locks:

```bash
# Run isolated regression harness
python -B -X utf8 tests/run_isolated.py

# Run full unittest suite
python -m unittest
```

Before submitting a Pull Request, make sure all tests pass:
```text
Ran 200 tests in ~10s
OK (failures=0, errors=0)
```

## Guidelines

- **Zero-Dependency Core**: Maintain zero external runtime dependencies for the core supervisor.
- **Safe Boundary Checks**: Never bypass single-writer locks or file silence assumptions. All handoffs must be verified against structured rollout events.
- **Evidence-Based Acceptance**: Completion must be backed by tangible evidence (deliverables, checklists, test executions), not superficial completion claims.
- **Cross-Codepage Cleanliness**: Keep batch scripts (`*.cmd`) in pure ASCII to prevent issues with GBK/UTF-8 codepage mismatches on Windows command prompts.
