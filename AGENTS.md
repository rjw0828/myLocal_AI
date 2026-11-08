# myLocalAI — Codex Project Instructions

## Project status and boundaries

- Base Framework v1.0 is the stable baseline, tagged `v1.0.0-base`. Treat the documented 215 passing tests as a historical release record, not a fixed requirement or a test result for the current checkout.
- Before changing behavior, inspect the relevant code and the current project documentation. The README and the current-status sections of `..\docs\Local_AI_Lab_WebControlConsole.md` describe the project; `..\docs\Local_AI_extension_plan.md` is a proposal, not implementation approval.
- Preserve the existing module boundaries: `app.py` (Flask API and sessions), `ai_controller.py` / `ollama_runtime.py` (Ollama), `storage\` (SQLite), `voice\` (STT/TTS), and `templates\` / `static\` (Web UI).
- Prefer small, compatible changes and existing dependencies. Reuse and evaluate suitable open-source projects before implementing substantial new functionality. Do not install dependencies, download or import external code, or transmit project/private data without explicit approval.
- Keep inference and user data local where practical. Do not expose services beyond loopback or add arbitrary command execution.
- MCP, Function/Tool Calling, and Extension directories are not part of the implemented Base Framework unless present in the current tree. Do not create speculative framework or extension directories; implement future features only when explicitly requested.
- Verify current files and behavior instead of relying on remembered or historical descriptions. Distinguish documentation, mock-based checks, local integration results, and user-reported device results.

## Repository and Git safety

- This repository is `F:\LLM\AI_Project\myLocalAI`; `F:\LLM\AI_Project` is its parent workspace, not a Git repository.
- At the start of work, identify the target repository and inspect its branch, HEAD, and working tree. Recheck before any Git write operation.
- `master` is the stable release branch. Use `feature/*` for features, `fix/*` for bug fixes, and `docs/*` for documentation or development-environment changes.
- Do not create or switch branches, commit, merge, or push without explicit user approval. Approval applies only to the stated repository and operation.
- Never run `reset`, `clean`, force-push, or other destructive Git operations automatically. Preserve pre-existing and concurrent user/agent changes; do not stage unrelated files.
- Before a merge, review the complete change set and run the relevant approved tests. Do not merge without explicit approval.

## Implementation and testing

- Keep changes within the requested scope. Do not modify application code, tests, databases, or other configuration as incidental cleanup.
- Use the repository's existing test framework and documented commands. Inspect `README.md` and relevant tests to select checks for the changed behavior; cover important edge cases and report the exact commands and outcomes.
- Do not run tests, builds, or hardware/integration checks when the user forbids them or they require unapproved external services, costly resources, or destructive effects.
- Never hide failures, weaken assertions, or remove tests to obtain a passing result. Clearly distinguish passed, failed, blocked, and not-run checks.
- For LLM, voice, and Ollama paths, state whether verification used mocks or a live local service/model. Never claim real-device or Edge verification unless it was actually performed.

## Project-specific test commands

Confirm these against the current `README.md` before use:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
node tests/test_voice_turn.js
node tests/test_voice_ui.js
```

The README records 215 tests for the v1.0 release (139 Python and 76 JavaScript/voice-model tests). Test inventory and counts may change; discover the current suite rather than requiring these historical counts.

## Task completion and handoff

- Summarize changed files, behavior, tests and checks (including those not run), unresolved issues, and the next step.
- When work is handed to another agent or the user, use the `project-handoff` skill. Keep the handoff concise and repository-specific; do not copy the README or plans.
