# Improvement Workspace — System Improvement

A specification for a shared accumulation layer where system observations, recurring patterns, and improvement proposals can persist across sessions. It is not an agent; it defines reusable artifact formats that an implementation can expose to sessions and future improvement capabilities.

> **Communication reconciliation — 2026-07-24.** Current analysis reads canonical
> message/question/answer rows and the artifacts they point to. An ambiguity is resolved through an
> open question; if the resolution changes a frozen contract, the contract owner writes an
> immutable revision and affected holders re-read/rebind. Older bus and `.signal ESCALATED` rows
> remain historical evidence, not the current resolution primitive.

---

## Purpose

The system generates experience but has no native place to accumulate it. Individual sessions end, context compacts, and observations disappear. The Improvement Workspace defines a persistent artifact model for capturing what the system learns about itself.

Four things happen here:

1. **Observations get logged** — what's working, what keeps breaking, patterns noticed across sessions
2. **Proposals get drafted** — structured cases for changes to prompts, process, or architecture
3. **Outcomes get tracked** — what was tried, what changed, what the result was
4. **Patterns get surfaced** — recurring signals that individually look like noise but collectively point at something real

This workspace is an input to system evolution, not a decision-maker. Decisions happen through the normal design process: the governing design index, focused design sessions, and explicit changes to the affected specifications or operational documents. The Improvement Workspace feeds that process with grounded evidence.

---

## Artifact Structure

An implementation provides four collections: **observations** for raw signals,
**proposals** for structured change cases, **outcomes** for closed-loop results,
and **patterns** for synthesized clusters across observations. Their storage
location is deployment-defined; this public snapshot specifies the formats, not a
live workspace directory.

### Observation Log Format

Each observation is a lightweight entry:

```
Date: YYYY-MM-DD
Source: [level, domain, or session context]
Signal: [what happened — concrete, one paragraph]
Category: [failure | friction | surprise | working-well]
Linked proposal: [proposal ID if one was drafted, else "none"]
```

Observations do not need to be complete analyses. Raw signal is valuable. Patterns emerge from accumulation, not from individual entries being polished.

### Proposal Format

Proposals are structured cases for a specific change:

```
ID: IA-YYYY-NNN
Date: YYYY-MM-DD
Status: draft | under-review | accepted | rejected | deferred
Target: [what would change — specific doc, prompt, process, or config]
Trigger: [observation IDs or pattern notes that motivated this]
Proposal: [what change to make, stated precisely]
Expected effect: [what should improve and how you'd know]
Risk: [what could break or regress]
Decision: [leave blank until acted on]
Outcome: [leave blank until outcome is known]
```

### Outcome Records

When a proposal is accepted and implemented, an outcome record closes the loop:

```
Proposal: IA-YYYY-NNN
Implemented: YYYY-MM-DD
Change made: [link or description of what actually changed]
Observed result: [what happened — match against expected effect]
Residual: [what's still unresolved, if anything]
```

---

## Inputs to This Workspace

**Session observations** — an implementation may accept observation entries from any level. The admission bar is deliberately low: broken, surprising, or notably good behavior is useful raw signal.

**Audit trail and resurrection windows** — the 2-week resurrection window gives a defined period during which a paused or stalled workspace can be audited before being closed. Patterns that emerge during resurrection audits (what was the last state, what broke continuity, what caused the stall) are high-value inputs here.

**Post-mortem signals** — when a level fails to complete work, escalates unexpectedly, or produces output that required significant correction, that's an observation worth logging regardless of whether a proposal follows immediately.

**Design session conclusions** — when a design session resolves an open question, its key decision and rationale are valid observation-class inputs so future sessions can understand why the system is the way it is.

---

## Future: Improvement Agent Capability

An automated improvement capability that analyzes this workspace, surfaces proposal candidates, and validates outcome patterns is a plausible future direction; the sibling research system's **principal coordinator** represents that direction. Such a capability would operate against this workspace by reading observations, drafting proposals, and writing outcome records using the same formats.

That capability is not V1 scope and is not the same thing as the workspace. The artifact model remains useful as a passive accumulation layer regardless of whether an optimizer ever runs on top of it.

