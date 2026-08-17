# User Profile — Schema and Contract

The persistent, cross-project record of **who the client is** — their standing preferences, baseline technical fluency, recurring opinionated areas, and how they want things rendered. The intake "grounds in the profile": the grilling session reads it to **calibrate** before the user says a word, so depth is spent where it pays and the user is not re-asked things they have already settled across past projects.

This artifact is the thing referenced by `role.md` ("structured conversation **grounded in the user profile**") and by `handbook.md` ("The tenth project is faster because the **user profile** has accumulated"). Before this schema, the profile was named but undefined. This defines it.

---

## What it is (one line)

A **living, append-mostly, cross-project** record of stable facts about the client that let the intake start warm instead of cold — a prior, not a cage.

It is **not**: a per-project artifact (that is the intent-spec), a transcript store, or an authority that overrides what the user says *this* time. The profile sets the **default**; the live request always wins on conflict.

---

## Where it lives

- **Path:** `operational/shared/user-profile.md` (one client, the only client — F-single-client). One file, persistent across all projects.
- **Owner:** L1 maintains it. The grilling session **reads** it (input), never writes it inline. Profile updates are a distinct, deliberate L1 act after a project, not a side-effect of intake.
- **Visibility:** readable by the grilling session at spawn (passed as an input, see `intake-session-template.md`). Not handed down past L1 — L2–L5 receive the per-project intent-spec, never the raw profile.
- **Lifecycle:** survives every project. The walking-skeleton dry-run starts it near-empty; it accretes a calibrated entry per opinionated area each time a project teaches L1 something durable about the client.

---

## Observable contract — what a well-formed profile MUST contain

A checker (or L1's own pre-intake read) treats the profile as well-formed iff each section below is present and each entry carries its required fields. Empty sections are legal (cold start) but must be present as headed, explicitly-empty sections — a *missing* section is malformed, an *empty* one is a valid cold-start state.

### 1. General preferences (standing, cross-project)

Free-form but each entry is a **stated preference + its scope**. Example rows:

| Preference | Scope | Source | Confidence |
|------------|-------|--------|------------|
| Ship a thin slice first, iterate | all projects | stated 2026-06-02 | high |
| Cheap-to-run beats feature-rich | infra/cost decisions | recurring across 2 projects | high |
| Hates being asked to choose a tech stack | stack decisions | observed, never stated | medium |

`Source` is one of: `stated` (user said it), `observed` (L1 inferred from behavior), `recurring` (held across ≥2 projects). `Confidence` gates how hard the intake leans on it: `high` → assume it and confirm only at reflect-back; `low` → probe it fresh.

### 2. Baseline technical fluency map

The **default** fluency by domain area, used to seed M58 render-depth **before** the per-project fluency capture refines it. One row per area the client has touched:

| Area | Baseline fluency | Render default | Evidence |
|------|------------------|----------------|----------|
| Payment flows | fluent at flow level; not at idempotency internals | technical for flow, plain for internals | payments project, 2026-06-02 |
| Infra / hosting | plain-language; cares about cost outcome not mechanism | plain | recurring |
| Datastores / schema | unknown | probe fresh | — |

`Render default` is exactly the vocabulary M58 / the gate consumes: `technical` (show the technical claim) or `plain` (show the plain-language implication). The per-project intake **overrides** this for the specific project but the profile supplies the starting point so the grilling session does not re-discover known fluency from scratch.

### 3. Recurring opinionated areas

Areas where the client has reliably had a stake across projects — so the intake knows *in advance* which forks to put in front of them and which to wave through:

| Area | Standing stance | Strength | Note |
|------|-----------------|----------|------|
| Running cost | keep it cheap | directional, strong | shows up every project |
| Test discipline | wants failure-path tests on anything money-touching | absolute | first surfaced in payments |
| UI polish | low priority early, "make it work first" | directional | |

`Strength`: `absolute` (a near-MNF stance), `directional` (a lean), `situational` (depends). This is a **prior on opinionated-vs-delegated**, refined by the per-project tradeoff-probing — not a replacement for it.

### 4. Communication / render preferences

How the client wants L1 to talk to them, and how much they want to see at gates:

| Dimension | Preference |
|-----------|------------|
| Gate render-depth default | "just tell me if it matches what I described" unless it's a money/safety area, then show me the mechanism |
| Challenge appetite | wants pushback when L1 has verified evidence; no reflexive "are you sure?" |
| Detail tolerance | low — outcomes and decisions, not process |
| Decision style | decides fast on forks; dislikes open-ended "what do you want?" questions |

These feed both the intake (how to run the conversation) and downstream result-shaping (how L1 packages deliverables).

---

## How the intake reads it to calibrate (observable behavior)

The grilling session, on receiving the profile as input, performs these calibrations **before** drilling — and a reviewer of the finished intent-spec can see the profile's fingerprint in it:

1. **Pre-seed the opinionated/delegated map.** Recurring opinionated areas (§3) start the project's opinionated-areas table pre-populated with `provisional` stance, to be confirmed or overridden by this project's tradeoff-probing. Observable: the intent-spec's opinionated-areas table cites the profile as the evidence source for any area not freshly probed this session.
2. **Pre-seed fluency / render-depth.** Each area's `Render at gate as` value defaults from §2 and is only re-derived if this project's probing contradicts it. Observable: areas the user never engaged this session still carry a non-empty fluency value, sourced "profile baseline."
3. **Suppress already-settled forks.** A fork the profile marks `high`-confidence settled (e.g. "cheap beats managed") is **not** re-litigated as a fresh probe — it is stated back as an assumption at reflect-back instead. Observable: the reflect-back script lists profile-derived assumptions ("you usually want X — still true?") separately from this-session decisions.
4. **Set the conversation register.** §4 sets challenge appetite, detail tolerance, and decision style for *how* the grilling session talks. Observable only in transcript tone, not in the artifact — but the artifact's render choices must not contradict §4.

**Conflict rule (load-bearing):** if the live request contradicts the profile, the **live request wins** and the contradiction is logged as a profile-update candidate. The profile is a prior, never an override. A grilling session that quietly used a stale profile preference over what the user said *this* time has failed its contract.

---

## How it is updated (closing the loop)

After a project's intake (or after delivery), L1 may append durable learnings:

- A newly-revealed standing preference → §1.
- A fluency level demonstrated this project → §2 (sharpens the baseline).
- An area the user turned out to be opinionated about repeatedly → promote to §3.
- A render/communication preference observed → §4.

Updates are **deliberate and attributed** (each entry carries its `Source`/`Evidence`), never silent. A single project does not promote a one-off to `recurring`; that requires ≥2 observations. This append-mostly discipline is what makes the tenth project faster than the first without letting a single noisy session poison the prior.

---

*Created: 2026-06-02 — defines the previously-named-but-undefined user-profile artifact the intake grounds in. Consumed by `operational/L1/intake-session-template.md` (input) and refined by per-project intake (M50/K45).*
