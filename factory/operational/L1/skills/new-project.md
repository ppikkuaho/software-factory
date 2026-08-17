# Skill: New Project Creation

Use when the user wants to start a new project or any substantive piece of work.

---

## When to Use

The user has described an idea through conversation and you've determined:
- It needs its own project (not a one-off task)
- The vision is articulated enough to hand to L2 (or you're about to finish articulating it)
- You know what kind of project this is (what domain, what type of work)

## The Process

### 1. Complete the Vision Capture (Intake)

Before creating anything, complete the structured intake (M50/K45). **You OWN this deliverable but you DISPATCH the heavy elicitation** — you don't run the multi-turn drilling in your own context (route-don't-execute applies to intake; see `role.md` → "Owns the spec, dispatches the elicitation").

1. **Outcomes-first conversation** — what does success look like? what problem is this solving?
2. **Tradeoff-probing** — show real forks to detect opinionated vs. delegated areas; capture technical fluency per area for gate render-depth calibration (M58).
3. **Dispatch the parallel grilling session** — spawn it via `operational/L1/intake-session-template.md`, passing the user's raw request + the user profile (`operational/shared/user-profile-schema.md`). The intensive elicitation runs in that separate, throwaway session; **only the finished intent-spec returns to you** (not its transcript). L1's context stays clean. (The artifact is the **intent-spec**, not an "SDD" — that term was a cascade-wide-methodology misnomer at the intake boundary; see `operational/shared/intent-spec-contract.md`.)
4. **Ingest, verify, and own the tagged intent spec** — verify the returned spec against `operational/shared/intent-spec-contract.md` before accepting it; bounce it back if malformed. Every requirement tagged `decided` / `delegated` / `deferred`.

Checklist:

- [ ] User's idea is understood — what they want to build, who it serves, what problem it solves
- [ ] Opinionated vs. delegated areas mapped (via tradeoff-probing, not survey questions)
- [ ] Technical fluency captured per opinionated area (drives render-depth at plan-alignment gate)
- [ ] Deep areas worked through in the dispatched parallel grilling session (spawned via `intake-session-template.md`); only the finished intent-spec returned
- [ ] You know what success looks like from the user's perspective
- [ ] You know any constraints or preferences the user has declared
- [ ] You know what the user's priority overrides are (if any)
- [ ] Returned intent-spec verified against `operational/shared/intent-spec-contract.md` (all seven elements present and well-formed)
- [ ] Tagged intent spec produced (decided/delegated/deferred)
- [ ] L1 scope check: this is technical work, not business model/monetization/go-to-market

### 2. Create the Project Workspace

Create the folder structure:

```
projects/{project-name}/
  client-brief/
    raw-request.md    ← exact initial user request; frozen with intent confirmation
    intent-spec.md    ← tagged intent spec (decided/delegated/deferred)
    vision.md
    priorities.md
    adrs/             ← ADRs will be added here by L2 and during planning
```

L2 creates everything else at boot. You only create the project root and client brief.

### 3. Write the Client Brief

**`client-brief/raw-request.md`** — the user's exact initial request bytes, copied from the start
intake row without paraphrase. Freeze it beside the intent-spec at reflect-back confirmation; it is
the surviving independent input for the once-per-intent atomization audit.

**`client-brief/intent-spec.md`** — the tagged intent spec (founding reference; immutable once settled). This is the artifact the grilling session returns; you do not author it inline. Its **canonical schema and return contract are `operational/shared/intent-spec-contract.md`** — verify the returned spec against that before accepting. The reference instance is `dry-run/intent-spec.md`. It must contain all contract elements:

1. **Outcomes table** — stable `Outcome ID` (`O-NNN`) rows, concrete in the user's terms,
   each with verbatim intent span(s). These IDs are the final fidelity playback roster.
2. **Hierarchically-IDed requirements table** — every row carrying `Tag` (`decided`/`delegated`/`deferred`) · `Priority` · `MNF` flag · `Parent`-or-`serves` · `Fluency` (`technical`/`plain`) · **`Reflect-back status`** (`pending`→`confirmed`). IDs are minted **only** here.
3. **Intent Journeys table** — stable `Journey ID` (`J-NNN`) rows for positive/negative
   recipient-visible paths, linked to live requirements and any exact MNF obligation, each with
   starting state, `Action through the face`, observable result, and `Reflect-back status`.
4. **Per-area opinionated/delegated + technical-fluency map** + the logged tradeoff probes (drives gate render-depth, M58).
5. **Must-never-fail decomposition** — each MNF split into atomic, individually-testable obligations, each with the forbidden failure + distinct mechanism + the **negative test the gate will require** + safe-failure direction.
6. **Verbatim ID→intent-span map** with `[L1-derived]` flags on any requirement L1 surfaced that the user did not state.
7. **A trace-block per requirement** — the root of the downstream flow-down chain the RTM-builder harvests.
8. **Reflect-back script + confirmation status**, explicitly playing back every journey.
9. **Delivery destination** — one resolvable filesystem path or git remote, or an explicit
   in-place/no-external-delivery mark.

**Source of truth.** The **intent-spec is the single source of truth** for the project — canonical for opinionated/delegated areas, priorities, and every captured requirement. `vision.md` and `priorities.md` (if kept) are **thin, human-readable views derived from it** — they exist for narrative readability, hold no authority the intent-spec lacks, and **defer to the intent-spec on any divergence**. Do not let them drift into a competing record: where a reader needs the authoritative answer, it is the intent-spec. There is one source of truth, and it is `intent-spec.md`.

**`client-brief/vision.md`** — a thin human-readable view of the user's vision, derived from and deferring to the intent-spec:

```markdown
# Vision — {Project Name}

## What We're Building
[What the product/system is, in concrete terms]

## Who It Serves
[Who uses this, what their situation is]

## The Problem
[What problem this solves, why it matters]

## What Success Looks Like
[How the user will know this succeeded — concrete outcomes, not vague goals]

## Features / Capabilities
[What the thing does — the specific capabilities the user described]

## Constraints
[Budget, timeline, technical constraints, platform requirements, anything the user specified as fixed]

## Open Areas
[Things that came up as needing definition but were delegated to L2's judgment]
```

**`client-brief/priorities.md`** — a thin human-readable view of user triage and overrides, derived from and deferring to the intent-spec (the intent-spec's tags + priority + opinionated/delegated map are canonical; this file is a readable digest of them):

```markdown
# Priorities — {Project Name}

## User-Defined Depth
[Which areas the user collaborated deeply on, and what was decided]

## Delegated Areas
[Which areas the user delegated — "tech stack: your call", etc.]

## Priority Overrides
[Any specific priority statements that override domain defaults.
Example: "Performance matters more than polish." "Ship fast, iterate later."
These flow through the entire project — L2 passes them to L3s, L3s to L4s.]

## What the User Wants to Review
[What level of detail the user wants to see when the concept comes back.
"Show me everything" vs "just tell me if it matches what I described" vs specific areas they want to review.]
```

Both files are immutable once written. They are **derived views**, not the founding reference — the **intent-spec is the single source of truth and the founding reference** for the entire project; these two defer to it on any divergence.

### 4. Configure L2

**`projects/{project-name}/L2-config.md`**:

```markdown
# L2 Configuration — {Project Name}

## Role Identity
{The professional role L2 should adopt for this project.
Example: "technical architect for a personal finance web app"
Example: "solution designer for a multiplayer game"
Example: "research lead for an ML training pipeline"}

## Domain Context
{What kind of project this is, what domain expertise applies.
Example: "Fintech — prioritize data integrity, security, regulatory awareness."
Example: "Game development — prioritize performance, player experience, asset pipeline."}

## User Priorities
{Inherited from client-brief/priorities.md. The specific overrides that L2 must respect.}

## Project Workspace
projects/{project-name}/
```

### 5. Spawn L2

Using `operational/L2/spawn-template.md`, fill in the variables:
- `{{PROJECT_NAME}}` — the project name
- `{{ROLE_IDENTITY}}` — from L2-config.md
- `{{USER_PRIORITIES}}` — from priorities.md
- `{{DOMAIN_DEFAULTS}}` — from L2-config.md domain context
- `{{RUNTIME}}` — from `operational/shared/runtime-and-model-map.md` (L2 = Opus 5.0 on Claude Code)

Spawn L2 pointing at the project workspace.

### 6. Update Portfolio

- Update `L1/portfolio.md`: add new project entry (name, status: "L2 spawned — concept pending", L2 config path, workspace path)
- Append to `L1/log.md`: project creation entry
- If this came from a conversation thread, update the thread

---

## Checklist Before Spawning

- [ ] Intake complete (step 1 checklist all checked) — grilling session dispatched, intent-spec returned and verified against the contract
- [ ] `client-brief/raw-request.md` preserves the exact start intake and is frozen with the intent-spec
- [ ] `client-brief/intent-spec.md` in place (contract-valid per `operational/shared/intent-spec-contract.md`: IDs + tags + priority + confirmed intent journeys + MNF decomposition + fluency map + ID→span map + trace-blocks + reflect-back status + delivery destination)
- [ ] `client-brief/vision.md` written
- [ ] `client-brief/priorities.md` written
- [ ] `L2-config.md` written with role identity + domain context + user priorities
- [ ] `operational/L2/spawn-template.md` variables filled (including `{{RUNTIME}}`)
- [ ] `portfolio.md` updated with new project
- [ ] Log entry appended

---

*Skill version: 2026-07-24 — added load-bearing confirmed intent journeys to the canonical intake and spawn checklist.*
*Skill version: 2026-06-02b — intake now dispatches the grilling session via `intake-session-template.md` (L1 owns, doesn't produce inline); intent-spec template replaced by reference to canonical `intent-spec-contract.md` (seven elements: IDs/tags/priority/MNF-decomposition/fluency/ID→span-map/trace-blocks/reflect-back); retired "SDD" misnomer; added contract-verification step; profile input via `user-profile-schema.md`.*
