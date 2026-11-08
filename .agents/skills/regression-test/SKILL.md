---
name: regression-test
description: Plan and perform authorized regression checks for changes in myLocalAI, including identifying affected behavior, relevant Python and JavaScript tests, edge cases, and results. Use after implementation or when asked to assess regression risk; do not run checks forbidden by the user.
---

# Regression test

## Purpose

Verify that an authorized change preserves relevant myLocalAI behavior and report evidence without treating historical test counts as a pass condition.

## Use when

- Code or behavior has changed and needs regression validation.
- The user requests a test plan or a scoped assessment of regression risks.

## Inputs

- Intended change, acceptance criteria, and any explicit test/build restrictions.
- Target repository path and current working-tree state.
- Changed files or the proposed change set.

## Procedure

1. Confirm the repository root, current branch, HEAD, and working tree. Do not alter Git state.
2. Identify the changed files and trace their relevant callers, consumers, data flow, and module boundaries.
3. Read the current `README.md`, test support, and relevant tests to identify the exact documented commands and existing test categories. Do not assume the release-era test count or command remains current.
4. Map changed behavior to focused tests first. Include relevant edge cases such as invalid input, errors/timeouts, session isolation and reset, persistence/conflicts, cancellation/concurrency, and UI late/failure responses as applicable; do not run irrelevant checks.
5. Check the user's authorization and restrictions. Run only permitted checks. Do not install dependencies, start external services, use real models/devices, or perform destructive data operations unless expressly authorized.
6. Report commands exactly, distinguish passed/failed/blocked/not run, and state whether service/model checks used mocks or live local resources. If a failure occurs, preserve its evidence and do not weaken or remove tests to make the suite pass.

## Project command references

The README currently documents these commands; re-read it before execution:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
node tests/test_voice_turn.js
node tests/test_voice_ui.js
```

The README records a historical v1.0 baseline of 215 tests (139 Python and 76 JavaScript/voice-model tests). This is context only, not a fixed threshold or proof of the current test inventory.

## Output

Summarize:

- Change scope and affected behavior.
- Selected tests/checks and why they cover the scope.
- Exact commands and outcomes, or why a check was not run.
- Edge cases covered and remaining regression risks.
- Mock versus live Ollama/STT/TTS/device verification.

## Safety rules

- Respect explicit test/build prohibitions and user-data protections.
- Do not change files, tests, databases, or Git state as part of validation unless separately requested and authorized.
- Never claim a test, integration, or device check passed unless it was actually run and observed.

## Completion criteria

The relevant authorized checks have been reported accurately, or the remaining validation gaps and reasons are explicit and actionable.
