---
name: project-handoff
description: Prepare a concise, evidence-based handoff of myLocalAI work to another agent or the user, including repository and Git state, file ownership, validation, unresolved issues, and next scope. Use when pausing, switching agents, or completing a multi-step task.
---

# Project handoff

## Purpose

Transfer only the context needed to continue work safely in the correct myLocalAI repository, without repeating project documentation.

## Use when

- Work is being transferred between Codex, Copilot, another agent, or the user.
- A multi-step task is paused or needs a clearly bounded next step.

## Inputs

- Task identifier and objective (if supplied).
- Repository path, branch, HEAD, and baseline commit/tag.
- Current worktree state, changed files, ownership, and authorization boundaries.
- Validation commands/results, unresolved errors, and remaining work.

Verify Git state with read-only commands before reporting it. If state cannot be checked, say so and identify it as unverified. Do not include secrets or private conversation content.

## Procedure

1. State the task objective and exact repository root.
2. Record current branch, HEAD, relevant baseline commit/tag, and worktree status as observed; do not infer remote freshness.
3. List only task-related changed files and summarize their purpose. Note files actively owned by another person or agent and avoid overlapping edits.
4. Record test/check commands and outcomes exactly; separate passed, failed, blocked, and not run, and distinguish mocks from live service/device checks.
5. List unresolved errors, decisions, approval status, and explicit prohibitions.
6. Define the next agent's bounded task and completion criteria. Do not claim approval for any Git or file operation that was not granted.
7. Keep the handoff short; link to existing README/plans rather than copying their contents.
8. Before saving a handoff document in the project, ask the user to confirm its path and whether it should be Git-tracked. Do not create or commit a handoff artifact without that confirmation.

## Output

Use this Markdown template:

```markdown
# Handoff: <short task title>

- Task ID: <value or not provided>
- Goal: <objective>
- Repository: <absolute path>
- Branch / HEAD: <observed branch and commit>
- Baseline: <commit or tag, if relevant>
- Working tree: <clean or concise status>
- Owner / file ownership: <agent/person and scope>
- Changes: <files and short purpose, or none>
- Validation: <commands and passed/failed/blocked/not-run outcomes>
- Unresolved: <issues, risks, or none known>
- Approvals / restrictions: <granted and still-required approvals>
- Next scope: <bounded task>
- Completion criteria: <observable conditions>
```

## Safety rules

- Report only verified state as fact; label user-reported or historical information separately.
- Never stage, discard, overwrite, commit, merge, push, or switch branches as part of handoff.
- Do not include credentials, tokens, private data, or unnecessary copied documentation.
- Ask before writing the handoff to a project path; clarify Git tracking intent.

## Completion criteria

The receiving party can identify the exact repository and worktree, understand what is and is not verified, avoid conflicting file ownership, and proceed within the stated approvals and scope.
