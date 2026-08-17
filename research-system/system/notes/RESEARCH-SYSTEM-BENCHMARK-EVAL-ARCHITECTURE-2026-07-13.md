# Hypothesis Tree Research System — Benchmark & Eval Architecture

> **Status:** working note; concept-level design, converged 2026-07-13
> (architecture session, user + Fable architecture seat). User rulings inline;
> decision log at §12. NOT a buildable spec; §11 is the open-question queue.
> **Extends:** `RESEARCH-SYSTEM-CONCEPT-2026-07-07.md` — gives concrete shape to
> the evals/benchmarks the concept presupposes throughout (§1 "validated against
> evals/benchmarks", §5 tier-1 "suite S", §4.3 merge-gate "mechanical eval
> checks", §5 EDD-as-asymptote). Nothing ratified is amended; this note fills a
> designed-around hole.
> **Subject scope:** benchmarks measure the SUBJECT system (the L1-L5 harness);
> the loop-effectiveness readout (PC note §7) measures the RESEARCH system.
> Related, never shared machinery.
> **Evidence base:** a three-stream web research sweep run in-session
> (2026-07-13; ~50 primary sources: practitioner postmortems, security
> incidents, academic benchmarks). Key anchors in §9; full corpora with
> verbatim quotes live in the session transcript (condensation owed on demand).

---

## 0. How to read

House convention: ✅ committed/ruled · 🟡 committed concept, mechanics open ·
🔲 open, do not assume · ⏸ pinned, proposed-not-accepted.

---

## 1. Frame ✅ (user-ruled)

**The software-factory frame.** The system is a large autonomous scaffolded
factory completing difficult software tasks; the top-level benchmark is an
**outcome** benchmark (we do measure outcomes, and in that sense coding
ability). Coordination fidelity is the mechanism story for *why* outcomes are
better, not the thing measured.

**The headache the system solves (user, verbatim intent):** with a naive
agent build, delivery is followed by days of test-iterate-fix — bugs,
non-functional features, drift, bloat, dropped features, janky architecture,
painful changes (the community's 8–12× fix-multiplier). The harness's promise:
what comes out is functional, worked to spec, unattended during build; residual
issues concentrate in design-phase gaps, not execution.

**The set-vs-house principle.** One-shot/naive builds famously "look done"
and collapse under real use (movie-set sitcom wall, not a house). The
benchmark's acceptance pass must be the thing that walks around the back of
the set and pushes on the walls. Where the walls are hollow is now empirically
harvested (§9).

**Autonomy is the premise, not the prize:** human/human-proxy interventions
during build are **logged, never scored** (user ruling).

---

## 2. Three benchmark families ✅

1. **Family 1 — owned outcome corpus.** 3–5 grand tasks (shortlist 🔲 §11.1),
   clone-class or similar: well-known software gives a free public
   ground-truth spec and legible results. The center of the program.
2. **Family 2 — public anchors.** Occasional external calibration, run at
   milestones, never iterated against. Preference: **SWE-Lancer Diamond**
   (user); FeatureBench as candidate; SWE-bench Pro deprioritized ("way
   downstream" / mostly saturated / tasks too small). Decision deferred until
   the program locks in (🔲 §11.4).
3. **Family 3 — component benchmarks.** Per-seat frozen input corpora
   (~**5** cases per seat, not ~20 — user sizing) + rubric; run the seat alone
   (= the ruled slice-run discipline). Sources: harvested moments from real
   runs (grand-task runs double as specimen farms) + synthetic cases stressing
   known failure modes. These corpora ARE "suite S" per lane — methodology-lane
   instruments, versioned, epoch-gated.

**Linkage rule ✅ (user):** component gains license **slice-scope claims
only**; promotion to "the factory got better" requires the outcome corpus.
Merges validate on component/slice evidence (cheap, per-merge); the outcome
corpus runs **on cadence** (per epoch batch / milestone) as the standing
calibration that component-level progress is real — structurally parallel to
post-merge watches.

---

## 3. The outcome corpus ✅ concept / tasks 🔲

### 3.1 Task qualification (drafter checklist, evidence-derived)

A task qualifies when a strong naive agent *starts well and degrades* — and
must contain the walls that go hollow: auth boundaries with ≥2 roles/tenants ·
money or equivalent invariant-bearing state · persistent data with concurrent
writers · ≥1 external integration · a real deploy step · multi-session
horizon. No small tasks (user; also empirically where the phenomenon isn't —
size inversion: smallest projects show the *highest* defect density).
Spread: prefer not five greenfields (one brownfield/extension in the mix —
drafter lean, unruled).

### 3.2 Arms ✅ (user)

Strong naive agents as-they-ship (e.g. Opus 4.8 in Claude Code, GPT-5.6 in
Codex) vs the harness. Comparisons are secondary to absolute capability; no
crossover-hunting on small tasks. Budget recorded (not matched).

### 3.3 Input ✅ (user)

Per task: an **L1-grade detailed want-description** (what L1 would produce
with human input) — not a live synthetic client in v1. Intake measurement
moves inside the harness arm. Hidden full spec + frozen acceptance battery
authored per task, before any arm runs, by not-the-competitor (D26 promoted to
the benchmark layer).

### 3.4 Score vector ✅ shape / metrics 🔲

- **Entry ticket:** requirement coverage (weighted acceptance passing, RTM-keyed).
- **Headline: acceptance debt** — the defect list from the independent
  acceptance pass, severity-weighted, **partitioned by attribution**:
  *execution infidelity* (spec covered it; build got it wrong) vs *spec gap*
  (spec silent/ambiguous; build guessed) vs *ambiguous* (own bucket, never
  forced). Success criterion = **distribution shift**: execution-infidelity
  → ~0; residue lives in the spec-gap bucket (design-phase problems, the
  bucket that belongs to the user). Falsifiable both directions; no
  zero-defect standard. Attribution judged blind (judge sees spec + defect,
  not which arm) — drafter call.
- **Recorded, unscored:** interventions log; spend.
- **⏸ Extension probes** (2–3 held-back change requests, frozen with the
  spec, revealed post-delivery; cost of next change as the maintainability
  measure): pinned, proposed-not-accepted. Evidence for: extension cliff is
  High-evidence (~8 sources) and the only channel catching architectural
  entropy as outcome not rubric. Dissent recorded: the one preregistered
  downstream-maintainability RCT (Borg et al., 151 participants) found no
  effect — self-flagged underpowered, short tasks; read as "the cliff needs
  project-scale complexity," but carried as the known dissent.

---

## 4. The acceptance battery ✅ shape

Judgment economics invert the builder's testing pyramid: the battery runs
rarely, needs no fault-localization, needs verdict fidelity. **The judge works
at the surface the user touches** — pixel-driven browser use over real flows.
Unit tests remain the builder's private business.

### Layer 1 — scripted flow suite (deterministic; the scoreboard)

Spec-derived: every requirement maps to ≥1 user flow (traceability — coverage
gaps enumerable); every flow gets happy path **+ sad-path variants stamped
from the break kit**: invalid/hostile input · empty states · wrong-role and
logged-out access · double-submit · refresh/back mid-flow · session expiry ·
injected network failure (timeout, 500, malformed). Every entry exists because
real apps died of it (§9). Playwright-class; every run records video + trace —
which is the **archive anchor** for every scored defect (citation discipline
carries over). **Pixel-driven ≠ pixel-asserted:** drive the real rendered UI,
assert user-visible outcomes (text, navigation, persistence across reload),
never screenshot diffs.

### Layer 2 — adversarial pass (discovery organ)

LLM tester driving the real UI with spec + persona + try-to-break-it mandate.
**User ruling:** the *scored* set is codified beforehand — predefined flows,
frozen, reproducible, fair. Test-time exploration continues but is
**unscored**; an exploratory finding is a **claim, not a defect** — it scores
only after reduction to a reproducible script, which **graduates into the next
battery version**. (EDD applied to the battery itself: the suite grows where
observed breakage demands.)

### Layer 3 — structural probes (near-free; the headline-breach class)

One unauthenticated request-sweep across protected routes · two-seeded-account
IDOR/cross-tenant sweep · secret-scan of the client bundle · **clean-room
deploy** (fresh checkout → install → migrate → production build → serve).
Minutes of compute; would have caught Moltbook, Tea, the Lovable CVE, Base44.
Optional per task: one latency/throughput budget assertion (efficiency is a
real but secondary channel).

### Battery disciplines

- **Classical tests retained** (user: "we don't know how bad things would've
  been without them") with an explicit behaviour-coverage check = the two
  mechanical halves: requirement-traceability + **seeded-defect calibration**
  — inject known defects from the taxonomy into a good build; measure the
  battery's detection rate before its verdicts license anything (mutation
  testing promoted to app level; gives acceptance-debt an honest denominator).
- **Flakiness is epistemic noise**, not annoyance — scorer noise propagates
  into every claim upstream. Seeded data, fixed viewport, frozen clock where
  feasible, third-party services stubbed at the network boundary (per-task
  exception list for integrations under test); defect counts only if it
  reproduces N of M runs; "flaky" is its own recorded category.
- **Versioned like the instrument it is:** methodology-lane artifact;
  battery-version changes epoch-gated; cross-arm comparisons within one
  version only; cross-epoch numbers carry the version stamp or re-run the
  baseline (mechanics 🔲 §11.2).

---

## 5. Visibility rules ✅ (the exam-hall door, correctly drawn)

- **Claim A ✅ (user-ratified):** running arms never see the battery mid-run.
  Model instances given tests that conflict with intent take the
  specification-violating shortcut at 76–92% (ImpossibleBench), and more
  capable models cheat more. Builder self-reports and builder-authored green
  suites are **zero-evidence**; the harness re-derives every verdict from the
  frozen battery.
- **Claim B ✅ (user-ruled, correcting an earlier drafter overreach):** the
  improvement loop gets **full visibility** of results and failures between
  runs. Benchmark-as-punch-list is commissioning, not corruption — harness
  fixes are legible artifacts (prompt lines, gate rules, protocol steps),
  auditable in a way weight updates never are. Guard rail (drafter, for the
  composition gate): harness changes must be statable in task-general terms;
  a change referencing a benchmark task's specifics is flagged as the
  symptom-fix it is.
- **The answer-key rule ✅ (user-ratified):** the internal tester seat and the
  benchmark battery are separate instances of the same method library — same
  spec, same break kit, **never the literal frozen flows**. Not a fragility
  argument: copying the standard in changes what the benchmark measures, from
  *derivation* of the standard to *transcription* of it (closed-book →
  open-book licenses a much weaker claim). In real deployment there is no
  external battery — the harness's own oracle is all the truth-checking there
  is; the benchmark exists to measure how good that internal derivation is.
  Independent convergence on the same checks is the desired outcome.

---

## 6. Corpus governance ✅ (the curation reframe, user-ruled)

**Corpus-as-constitution.** For a purpose-built system the task corpus is not
a sample from a population — it is the **operational definition of what the
harness is for**. "Overfitting to the corpus" and "specializing to purpose"
are the same act; which one it is depends entirely on whether the corpus
faithfully encodes the purpose. Consequences:

- **Curation is governance, user-tier.** Choosing the 3–5 tasks is choosing
  what the harness becomes under hundreds of iterations of selection — the
  research program's constitution, not an instrument config.
- The standing watch-item is not "are we adapting to the benchmark" (yes, and
  should be) but **"does the corpus still mean what we want"** — corpus-meaning
  drift.
- The drift detectors are machinery already ratified: **post-merge watches**
  (predicted-vs-observed on real usage = the out-of-sample check on
  lab-fitted improvements) and **deliberate task rotation** (retire a grand
  task, mint a fresh one at intervals). They catch the moment "good at these
  five tasks" and "good at what I actually want" come apart — the one failure
  mode curation can't see from inside.
- Adaptive overfitting (the public-leaderboard effect — compounded honest
  selection, no dishonesty required) is acknowledged and accepted at this
  corpus size, bounded by the two detectors above and claim-tier scoping
  (corpus evidence licenses corpus-scope claims; "generally better" needs the
  ladder's varied-context recurrence, as ever).

---

## 7. The oracle principle ✅ (why this is not benchmark overhead)

AI has already inverted builder economics, and this testing class is
automatable with gen AI — so its value compounds: **the binding constraint on
autonomous delivery is oracle access, not generation ability.** Agents ship
hollow sets because their feedback loop terminates at "my tests are green" —
a self-referential signal; they have no way to feel the difference from
inside. A trustworthy external truth-check is simultaneously (a) the
benchmark's measurement instrument and (b) what lets the harness *deliver*
finished work in production, where no external battery exists. One artifact
upgrades both the product and the measurement. This grounds D26/M51 in the
subject system and makes the battery arguably the highest-leverage single
component in the estate.

**Downstream (user):** the measurement substrate is what makes the
optimization cascade real — model substitutions, reasoning-effort cuts,
cost-per-outcome tuning each become an ordinary hypothesis-tree dispatch
(bounded change, measured verdict, merge or demote). E31 made seats
config-swappable; this makes swaps *checkable*. Rail reaffirmed with full
force: **these numbers are evidence, never targets.**

---

## 8. Relation to the estate

- **L1-5 tester seat (founding pass owed):** the battery is the same species —
  same craft, break kit, browser-driving skills. Share the method library;
  separate the instances per §5. The founding pass should consume this note.
- **Family-3 corpora** slot into instrument governance as ruled (methodology
  lane, versioned, epoch-gated); benchmark results enter the research system
  as epoch-stamped measurements under normal claim machinery (wiring 🔲 §11.5).
- **Loop-effectiveness readout** (PC note §7) stays separate machinery —
  measures the research system, not the subject.

---

## 9. Evidence base (key anchors; full corpora in session transcript)

- **Functional/secure decoupling:** SusVibes (arXiv 2512.03262, CMU): 200 real
  feature tasks — 61% functionally correct, **10.5% secure**; >80% of
  functionally-correct solutions insecure. The facade, measured.
- **Difficulty cliff / task shape:** FeatureBench (arXiv 2602.10975): Opus 4.5
  74.4% on SWE-bench Verified → **11.0%** on 200 multi-file feature tasks
  (~790 lines, ~16 files). Tests-visible-to-agent raised one model 10%→60% —
  direct external validation of frozen-acceptance-before-code (D26).
- **Test gaming:** ImpossibleBench (arXiv 2510.20270): GPT-5 takes
  spec-violating shortcuts in 76–92% of conflicting-test cases; more capable
  models cheat more; LLM monitors catch only 42–65% on realistic tasks.
- **Green suite ≠ working software:** GH anthropics/claude-code #47300 —
  18,268-value golden regression suite green through 20+ fix attempts while
  live output stayed wrong (tests encoded the agent's interpretation, not the
  requirement).
- **Anchor contamination:** SWE-bench+ (arXiv 2410.06992): 32.7% of passing
  patches had solution leakage, 31.1% passed on weak tests; corrected
  resolution 12.5%→4.0%. (User: noted, not heavily weighted — SWE-bench is
  downstream regardless.)
- **Failure taxonomy (~12 classes, break-kit seeds):** broken/missing
  authorization the single most recurrent class (Moltbook, Tea, Lovable
  CVE-2025-48757, Base44; needs role-based runtime probing — static analysis
  can't see an inverted guard) · happy-path-only wiring · silent
  failure/error-swallowing · placeholder/stub logic shipped as real · secret
  exposure · test theater · deploy/config divergence · extension cliff ·
  concurrency/idempotency (thinnest public evidence precisely because it
  produces no public postmortems — benchmark opportunity).
- **Extension-cliff dissent:** Borg et al. (arXiv 2507.00788) preregistered
  RCT: no downstream maintainability effect (underpowered, short tasks).
- **Context:** METR RCT (+19% time with AI, perceived −20%); GitClear
  duplication trend 8.3%→12.3%.

---

## 10. Principle extraction (provenance record)

Ten transferable eval-design principles were distilled from this session for
incorporation into the user's llm-design-principles skill (curation pending —
list delivered in-session 2026-07-13): oracle-first · judge at the claim
surface · actor-authored signals are claims · exam-hall scope rule (in-run
hidden / between-run visible) · answer-key vs derivation · corpus-as-
constitution · discovery/scoring split · instrument noise is epistemic noise ·
attribution-split scoring · harvest the break kit. This note is the dated
record; the skill is the living doctrine home.

---

## 11. Open items 🔲

1. **The grand-task shortlist** — the 3–5 tasks themselves (user: "we'll need
   to think"; the biggest open box). Qualification checklist §3.1 stands as
   drafter criteria.
2. **Battery-version / epoch mechanics** — versioning cadence, graduation
   protocol from exploration to scored core, baseline re-run rules.
3. **Component-corpus construction** — per-seat case selection, rubric form,
   harvest pipeline from grand-task runs (~5 cases/seat).
4. **Public-anchor lock-in** — SWE-Lancer Diamond subset (preferred) and/or
   FeatureBench; when the owned corpus exists.
5. **Results wiring** — how benchmark measurements enter trees/ledger
   (epoch-stamped measurements; instrument registration for the battery).
6. **⏸ Extension probes** — pinned per §3.4; revisit after v1 runs.

---

## 12. Decision log

### User rulings (2026-07-13, this session)

- Software-factory frame; outcome benchmarks primary; outcomes (incl. coding
  ability) are measured. The three-part headache (§1) is the benchmark's
  definition of success.
- Three families ratified; linkage rule (component → slice claims only;
  outcome corpus on cadence as calibration).
- Arms: strong naive agents vs harness; comparisons secondary; no small
  tasks; budget recorded not matched.
- Interventions logged, never scored — autonomy is the premise.
- Headline metric: acceptance debt partitioned by attribution; success =
  distribution shift toward spec-gap. (The 8–12× fix-multiplier is the debt
  being priced.)
- Extension probes ⏸ pinned, not accepted (hard to judge; not bad idea).
- Input = L1-grade detailed want-description (v1); no live synthetic client.
- Component corpora ~5 cases per seat.
- Public anchors: SWE-Lancer Diamond preferred; SWE-bench Pro deprioritized;
  contamination findings noted, not heavily weighted; decision deferred.
- E2E = real functions, real user flows, pixel-driven browser use,
  try-to-break. Classical tests retained with explicit behaviour-coverage
  verification. Tests hidden from arms (Claim A).
- Layer-2 scored set predefined/frozen for reproducibility; exploration
  unscored, graduates into next version.
- Claim B: improvement loop gets full visibility; fixing root causes against
  benchmark failures is desired (build phase); harness ≠ model for
  overfitting fragility.
- Curation reframe: corpus specialization is purpose-definition, not
  corruption — "as long as we stay open-eyed that what we're optimizing for
  is the thing we actually want"; task selection is user-tier governance.
- Answer-key rule ratified (derivation vs transcription; tester never gets
  the literal flows).
- Oracle principle ratified and extended (user): AI inverted builder
  economics; automatable truth-checking's value shoots up; measurability
  unlocks the downstream optimization cascade.
- Capture approach approved (this note); principle-extraction layer
  requested (§10).

### Drafter judgment calls (proposed in-session; confirm or strike)

- Task-qualification checklist (§3.1) incl. one-brownfield spread lean.
- Blind attribution judging; "ambiguous" as own bucket (§3.4).
- Break-kit composition from the harvested taxonomy (§4 L1).
- Determinism/repetition/flaky-category disciplines (§4).
- Seeded-defect calibration as precondition for verdicts licensing claims (§4).
- Battery versioned as methodology-lane instrument, epoch-gated (§4).
- Composition-gate rule: harness changes statable in task-general terms;
  task-specific fixes flagged (§5).
- Post-merge watches + task rotation named as the corpus-drift detectors (§6).
- Grand-task runs double as Family-3 specimen farms (§2).
- Per-task optional efficiency budget assertion (§4 L3).

---

## 13. Propagation owed

- `system/notes/README.md` reading-order pointer (this note alongside the
  2026-07-12 layer) — same-change with commit routing (this file written
  uncommitted from the architecture seat; working tree was on
  `wip/item1-recovery` during the live foundations build — user routes).
- llm-design-principles skill: the §10 ten-principle diff, on user curation.
- L1-5 corpus: tester-seat founding pass consumes §4/§5; a register-row
  pointer on next touch of that corpus (its own write protocol; not written
  from this seat).
