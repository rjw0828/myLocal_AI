---
name: opensource-research
description: Research and compare existing open-source projects before implementing a substantial new feature or adding a dependency. Use when evaluating libraries, services, runtimes, or tools for myLocalAI; do not use for routine changes that need no new capability.
---

# Open-source research

## Purpose

Determine whether an existing open-source solution is a better fit than direct implementation while preserving myLocalAI's local-first architecture and Windows constraints.

## Use when

- A proposed feature may be provided by an existing library, application, runtime, or service.
- A new third-party dependency or external service is being considered.

Do not treat research as approval to install, download, integrate, or modify project files.

## Inputs

- Feature requirements and acceptance criteria.
- Current project constraints and relevant architecture, verified from the repository.
- Any known hardware, offline, license, privacy, or deployment constraints.

Ask for missing decision-critical requirements rather than assuming them.

## Procedure

1. Inspect the relevant project code and documentation to understand existing capabilities and integration boundaries.
2. Identify a small set of credible candidate projects and cite primary sources for project status, release information, license, platform support, and installation/runtime requirements.
3. Compare each candidate against:
   - Feature fit and maturity.
   - License and usage obligations.
   - Maintenance signals and release/activity recency, with the observation date.
   - Windows 10, Python, and current dependency/runtime compatibility.
   - Local/offline operation, network requirements, and privacy implications.
   - Additional costs, including paid services or usage limits.
   - CPU, RAM, GPU, and storage requirements; mark unavailable measurements as unknown.
   - Integration effort and fit with the existing Controller, Storage, Voice, and Web UI boundaries.
4. Compare adoption with extending existing code or implementing the smallest suitable solution. State trade-offs and uncertainty; do not infer compatibility from a platform badge alone.
5. Recommend adopt, investigate further, or implement directly, and list the evidence and conditions that could change the recommendation.

## Output

Provide a concise report with:

- Requirement and evaluation date.
- Candidate comparison table (project, fit, license, maintenance/platform evidence, offline/cost/resource notes, integration effort).
- Recommendation and rationale, including direct-implementation comparison.
- Risks, unknowns, and items requiring user confirmation.
- Primary-source URLs for consequential claims.

## Safety rules

- Use public information only; never send project source, credentials, private data, or conversation content to external services.
- Do not install packages, download models or source, run candidate code, add dependencies, or modify project files without explicit approval.
- Do not present unverified license, compatibility, maintenance, performance, or offline claims as facts. Mark them unverified.

## Completion criteria

The decision is supported by cited evidence, trade-offs and explicit unknowns, and no installation or code change has been performed without authorization.
