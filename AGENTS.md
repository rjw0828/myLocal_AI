# Local AI Lab — Codex Project Instructions

## Project goal
Build a modular, privacy-first local AI system on an existing Windows PC. Prioritize learning, experimentation, and measurable progress over adding many features at once.

## Hardware and runtime
- OS: Windows 10 22H2 (build 19045)
- CPU: AMD Ryzen 5 3600
- RAM: 32 GB
- GPU: AMD Radeon RX 9060 XT, 16 GB VRAM
- LLM runtime: Ollama (already installed)
- Working model: `qwen3.5:9b` (same model content may also be tagged `qwen3.5:9b-q4_K_M`)
- Ollama API: `http://127.0.0.1:11434`
- Model storage: `F:\LLM\Models`
- Python controller: `ai_controller.py` (already implemented; inspect its actual contents before changes)

## Non-negotiable principles
1. Zero additional cost: prefer free/open-source software, existing equipment, and no paid APIs.
2. Local-first and privacy-conscious: local inference, STT, TTS, and memory wherever practical. Do not send project data to external services unless explicitly approved.
3. Preserve normal PC usability: measure RAM/VRAM/CPU/GPU impact and responsiveness; avoid unnecessary background workloads.
4. Implement incrementally: one narrowly scoped feature, test, then proceed.
5. Modular design: controller, LLM client, tools, memory, and I/O should be replaceable independently.
6. Safety: never run destructive commands, alter important files, expose network services, or install new dependencies without explaining and obtaining approval where relevant.
7. Keep changes small and readable; prefer existing project conventions over wholesale rewrites.

## Verified vs. unverified
- Previously observed: Ollama ran Qwen3.5 9B with 100% GPU placement reported by `ollama ps`; reported generation speed was about 41 tokens/s in one test.
- `100% GPU` in Ollama means the model is placed on GPU, NOT continuous 100% utilization.
- Do not assume the current process, available VRAM, or dependencies are unchanged; verify when necessary.
- Do not assume `ai_controller.py` has a particular structure, library, or test suite. Read the file first.

## Current development priorities
1. **Next task: Flask HTTP Controller.** Reuse the working `ai_controller.py` wherever possible.
   - `GET /health`: controller health, with clear distinction between controller up and Ollama reachable.
   - `POST /chat`: forward prompts to local Ollama and return responses.
   - Session-specific conversation history; ensure session separation and a way to reset.
   - Default `think: false`; allow a deliberately controlled option if appropriate.
   - Report request elapsed time and generation metrics when Ollama provides them.
   - Handle connection failures, invalid JSON, and model errors clearly.
   - Bind to `127.0.0.1` only; never open to LAN by default.
2. Tool registry and personas: after HTTP API is tested. Tool execution must use allowlists and validation, not arbitrary shell commands.
3. Voice pipeline: local STT (e.g. faster-whisper) and TTS (e.g. Piper) after core API is stable.
4. Long-term memory: SQLite, with explicit distinction between retrieval-based memory and model training.
5. Peripherals: Bluetooth audio, Xbox controller, phone, optional StackChan.
6. Learning experiments: only as a separate later phase.

## Coding and test workflow
- Before implementing, summarize current files and propose the smallest change plan.
- Make explicit which files will be added or modified.
- Keep configuration centralized; use environment variables for paths/ports if useful, with safe localhost defaults.
- Use Python typing and simple, maintainable functions. Avoid unnecessary frameworks and dependencies.
- Ensure API failures return suitable HTTP status codes and structured JSON; do not leak internal stack traces in HTTP responses.
- Add basic tests or documented smoke-test commands for success and error paths.
- Provide runnable commands for Windows PowerShell/CMD, not Unix-only instructions.
- Never claim hardware or integration tests passed unless actually run. If not runnable here, state exactly what the user should verify on the target PC.
- Preserve `.gitignore` for secrets, virtual environments, logs, local data, and model binaries; do not commit private conversations or tokens.
- Do not auto-install packages, download models, publish repositories, deploy, or expose local ports without user approval.

## Response and collaboration style
- Explain decisions in Korean and use concise incremental steps.
- Relate architecture to embedded/firmware concepts when helpful (modules, interfaces, state machines, resource management).
- Distinguish observed facts, assumptions, and recommendations.
- At the end of each task summarize: modified files, test outcomes, outstanding issues, and recommended next step.

## First instruction to Codex
Inspect the repository and `ai_controller.py`. **Do not change files yet.** Explain how to add a minimal Flask API with `/health` and `/chat`, maintaining local-only binding and existing Ollama behavior. List proposed files, dependencies, test commands, and open questions. Wait for approval before implementing.
