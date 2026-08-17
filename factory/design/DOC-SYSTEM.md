# Doc System — Shared Blocks, Templates, and Per-Node Schemas

How the harness's agent-facing documentation is kept deliberate instead of accidental: cross-level
duties live ONCE as versioned shared blocks rendered into the per-level role docs between markers;
a machine-readable registry says who carries what at which version; an idempotent render/check tool
plus a pytest checker fail RED on omission and drift; standard artifacts get templates; and every
level gets a per-node file schema so a stateless successor knows where everything is in any node of
its level. Process-design layer (DOCUMENT-HIERARCHY level 4). Settled with the user 2026-06-12 from
the Run-2 defect evidence; placed in `design/` (not `working-notes/`) because the shape is ratified
and load-bearing, not provisional.

---

## THE CRITICAL CONSTRAINT — read this before trusting a green checker

**The checker encodes MECHANICAL CONFORMANCE ONLY.** A green `tests/test_doc_blocks.py` means
exactly: no block is missing from a registered carrier, no rendered copy has drifted from its
source, no version is stale, no marker is unregistered. It does **not** mean the documentation is
healthy, well-written, behaviorally effective, or even pointed at the right duties. **Tests cannot
encode the desired behavior.** Run-2 proved the inverse direction too: four testers and a reviewer
whose docs DID carry the report duty still signed DONE without reports — perfectly "conformant"
prose lost to completion bias until the runtime gate bounced them. Documentation HEALTH belongs to
the judgment layer:

- **periodic doc review** — a human or delegated reviewer reading the rendered docs as an agent
  would, asking "does this still produce the behavior we want, at the right strength, in the right
  place?";
- **above all, behavioral evidence from live runs** — the RUN-ADHERENCE-AUDIT instrument
  (`working-notes/RUN-ADHERENCE-AUDIT-2026-06-11.md`). Run-2's defect distribution (which seats got
  bounced, for what, despite which doc language) IS doc-health data. The audit, not the checker, is
  where "the docs work" gets decided.

Do not let this system's existence imply otherwise. The checker prevents drift/omission
*accidents*; it cannot detect a duty that is uniformly present and uniformly ineffective.

---

## The problem (Run-2 evidence, 2026-06-12)

The per-level docs (`operational/L1..L5,L5+/{role,soul,config}.md` + `operational/shared/*`) carry
duties that are SHARED across levels — write `report.md` at DONE, cite given requirement IDs as
bare references, trace-stanza conventions, sign-off discipline, harness behavior — but coverage was
accidental:

- **Uneven by accident, not by adaptation.** L5's role doc stated the report duty three times; L3's
  stated it ZERO times. Both Run-2 execution-L3s (L3) signed DONE without reports and were bounced by the
  E2 runtime gate (`harnessd/return_contract.py`) — the gate held, but the doc layer never told L3
  the duty existed (LIVE-RUN findings, "~01:05 E2 refused markdown-L3's DONE (MISSING-REPORT)").
- **Exhortation loses to completion bias — structure beats prose.** Seats whose docs DID carry the
  duty (testers, a reviewer) were bounced anyway. The cure is structural (a checklist whose last
  items are the duties, a template that is a form to fill), not louder prose.
- **No template for ANY standard artifact.** Every agent improvised its report/plan format; the
  healed shapes converged anyway (typed headers, pointer-not-payload) — convergence we were paying
  for in tokens and bounce loops instead of supplying up front.
- **No per-node file schema.** Sibling nodes at the same level disagreed about what files exist
  (Run-2: `assembly/ws-b` lacked the `status.md` + `composition-report.md` its siblings ws-a/ws-c
  carried; the `assembly` L3 node lacked the `log.md`/`reviews/` the `markdown` L3 node had; review
  seats were named `review/`, `builder-review/`, `coder-review/` across one run). Bad for stateless
  respawn — a successor must know where everything is in any node of its level — and for cross-node
  legibility.
- **Hand-splicing does not scale.** The LR-13 gate-output contracts were landed by a one-off script
  (`working-notes/splice_gate_contracts.py`, the prior art) into three docs. As shared duties
  multiply, hand-splicing each into 6+ level docs — and keeping per-level adaptations deliberate
  rather than drifted — becomes unmanageable.

## The mechanism

### 1. Shared blocks — single sources under `operational/shared/blocks/`

Each cross-level duty lives ONCE as a content file:

```
operational/shared/blocks/
  registry.json              # the machine-readable doc registry (below)
  plan-first.md              # base content, verbatim carriers
  report-contract.md
  trace-discipline.md
  gate-output-contract.L1.md # named per-level ADAPTATIONS (no base — every carrier adapts)
  gate-output-contract.L2.md
  gate-output-contract.L4.md
  gate-output-contract.L5+.md
```

Per-level docs carry **rendered copies** between HTML-comment markers:

```
<!-- block:report-contract v1 -->
…rendered copy of the source content…
<!-- /block:report-contract -->
```

Everything OUTSIDE markers is that level's own craft — **never touched by tooling**. The marker
pair is the entire tool-owned surface of a role doc.

**Adaptations are first-class.** A level may carry an adapted variant (a `<block-id>.<level>.md`
source file) instead of the verbatim base — but the registry must SAY so. An adaptation is a
deliberate, named, versioned divergence; an unregistered difference is drift and fails red. The
`gate-output-contract` block is the existence proof: its duty (your gate produces a named
altitude-specific artifact; never re-run lower-level verification) is shared, but its artifact is
different at every altitude, so every carrier is an adaptation and there is no base file.

### 2. The doc registry — `operational/shared/blocks/registry.json`

Per block: the landed version, the source file (base), and the carriers — which level docs carry
it, verbatim (`"variant": null`) or as a named per-level adaptation (`"variant":
"gate-output-contract.L2.md"`), plus the anchor heading the render tool splices before when the
block is not yet landed. It also lists the template files the blocks depend on, so a deleted
template fails red.

**Format decision: JSON, single source.** One machine-clean file consumed by both the tool and the
pytest checker; pretty-printed, so it diffs fine for humans. A markdown table was rejected because
it would need its own parser and could itself drift; "both, md rendered from json" was rejected
because the registry is small enough that a second rendering is sync surface with no reader who
needs it.

### 3. The render/check tool — `tools/render_blocks.py`

Idempotent operator script, two modes (prior art: `working-notes/splice_gate_contracts.py`):

- **`--check`** (default): walk the registry, fail red (exit 1) with typed defects on: missing
  block in a registered carrier (`MISSING-BLOCK`), stale marker version (`STALE-VERSION`), content
  drift inside markers (`CONTENT-DRIFT`), markers in operational docs the registry doesn't know
  (`UNKNOWN-MARKER`), leftover pre-registry markers (`LEGACY-MARKER`), duplicate/unclosed markers,
  missing sources/templates.
- **`--render`**: re-render every carrier's in-marker content from its source (healing drift,
  stamping the registry version); splice a not-yet-landed block immediately before its registered
  anchor. Touches NOTHING outside markers. Running it twice is a no-op.

**Rendering decision: marker-splice, not full-file generation.** Role docs are hand-craft outside
markers; generating whole files was rejected by default — the tool owns only the marked regions.

**Drift-detection decision: literal comparison against the source block, not hash-pinning.** Literal
comparison gives diffable failures and keeps the source file as the single place content lives;
hashes would add registry bookkeeping per edit and yield opaque failures. The `vN` stamp in the
marker is kept anyway because it distinguishes *stale render* (source was bumped, doc wasn't
re-rendered — fails STALE-VERSION even before content is compared) from *in-place tampering*
(CONTENT-DRIFT), and it gives a human reading the doc a legibility cue.

**Run it only between runs.** Role docs are read in place by live agents (the splice prior-art
rule); never re-render mid-run.

### 4. The checker test — `tests/test_doc_blocks.py`

Part of the pytest suite (run with `--basetemp=/tmp/docsys`). The real-corpus check is the suite
gate; mutant tests drive the checker against deliberately-broken temp copies of the corpus (a
removed block, drifted content, a stale version, an unknown marker, a deleted template) and assert
each defect is caught AND NAMED — the pieces-present pattern: a silent gap becomes a loud,
front-loaded failure. The test module's docstring restates the critical constraint above; so does
the tool's.

### 5. Templates — `operational/shared/templates/`

Every standard artifact a block requires is backed by a template:

- `report-template.md` — the one-page report form: typed header (From/To/Type/Status — the shape
  Run-2 agents converged on unprompted, see the healed L2 report), Outcome / What was done /
  Requirement IDs discharged (bare references) / Verification evidence (pointers, not payload) /
  Deviations & concerns / Sign-off checklist. Pointer-not-payload doctrine
  (`operational/shared/comms-protocol.md`).
- `plan-template.md` — minimal: goal line + checklist + the standing final three items (fill
  report.md per template → verify requirement-ID citations → sign off).

Templates are forms, not specimens: short enough that filling them is cheaper than improvising.

### 6. Per-node file schemas — `design/WORKSPACE-SCHEMA.md` § "Per-Node File Schemas"

The WORKSPACE-SCHEMA philosophy extended down to "what files does EVERY node of a level contain,"
grounded in the actual Run-2 trees (`~/l1-l5-workspaces/build-site1/nodes/` — pre-2026-07-07 path was `~/Documents/l1-l5-workspaces/…`, still resolving via compat symlink).
**Placement decision:** extended WORKSPACE-SCHEMA itself rather than a sibling doc — that document
already owns workspace structure, key artifacts, and per-level workspace needs; a sibling would
split the workspace truth across two files for no reader's benefit. The brief sanctions exactly
this extension.

**Schema conformance is checked EVAL-SIDE only** — by the run-adherence audit — **NEVER as a
runtime gate.** The E2 runtime floor stays exactly the three deterministic checks in
`harnessd/return_contract.py` (report present, IDs cited where given for L5-class, stanzas valid
where present). The schema names the management skeleton; code and creative artifacts keep their
freedom.

## The initial content (landed through the mechanism, from live-run findings)

| Block | v | Carriers | Duty |
|---|---|---|---|
| `plan-first` | 3 | all six role docs, verbatim | first act = high-level native task list; bounded orientation; durable `plan.md` next with final three items fixed; read preinstantiated forms through the file-read tool before editing; keep task tool and file aligned; successor inherits the file |
| `report-contract` | 3 | five role docs verbatim; **L5+ a named adaptation** | `report.md` required at DONE at every level (the E2 floor refuses without it); shared template; bare requirement-ID citations even when local artifacts declare serving artifact IDs. The L5+ variant (`report-contract.L5+.md`): a reviewer **verifies** requirement IDs rather than discharging them, follows the `report-template.L5+.md` adaptation, and owes the citation duty for its OWN report — both Run-2 L5+ reviewers tripped the E2 citation check because the L5+ bundle carried no such duty. |
| `trace-discipline` | 2 | all six role docs, verbatim | stanzas declared once in declaring artifacts; downstream docs cite bare IDs; given IDs never re-minted. v2 adds **declaration ownership follows artifact ownership** (you declare only IDs YOU mint, in YOUR artifacts; children mint strictly-deeper sub-IDs) — the law behind Run-2's DUP-ID bounces, codifying the healed behavior (testers renumbering to deeper sub-IDs), not inventing. |
| `gate-output-contract` | 6 | L1, L2, L3, L4, L5+ role docs — all adaptations | the LR-13 cure, migrated from the legacy `<!-- gate-output-contract (LR-13) -->` markers into the registry scheme. v4 applies the gate-owned-forwarding invariant from `GATE-LIFECYCLE.md`: the executor submits only a candidate; the review gate owns parent-visible completion. L3 now has the missing `area-composition-review.md` gate artifact. v5 records the 2026-06-16 ruling that zero-L3 product execution is retired: L2 product work passes through an L3 owner, while a trivial area may still combine planning and execution-management in one L3. v6 records that ACCEPT at higher gates requires direct lower execution child gate-pass evidence and that L3 does not substitute inline implementation for L4/L5 work. |

Templates adapt the same way blocks do: `report-template.L5+.md` is the first registered template
adaptation ("Requirement IDs discharged" → "Requirement IDs verified (per-criterion verdicts)",
per QUALITY-GATE M52 — the verdict table is the reviewer's gate artifact). The registry entry
names the base it adapts and the reason; an unreasoned adaptation is a fork and fails the check.

This closes both L3 gaps now known to be load-bearing: L3 carries `report-contract` so it cannot
silently omit `report.md`, and it carries `gate-output-contract` so its cross-workstream integration
judgment has a named gate artifact (`area-composition-review.md`) instead of disappearing into the
producer's report.

## Operating procedure

**Adding a shared duty:** author the block file under `operational/shared/blocks/` → register it
(carriers + anchors + version 1) → `python3 tools/render_blocks.py --render` → read every landing
in place (rendered ≠ reviewed) → run the suite → commit content + registry + rendered docs
together.

**Changing a duty:** edit the source block, bump the registry version, re-render, re-read, commit.
A bumped source without a re-render fails STALE-VERSION/CONTENT-DRIFT in CI — that is the point.

**Adapting per level:** copy the base to `<block-id>.<level>.md`, adapt, point that carrier's
`variant` at it. The divergence is now named and versioned instead of drifted.

**Retiring:** remove the carrier entry, delete the marker region from the doc (the one sanctioned
manual touch of a marked region), re-check.

## What the judgment layer should watch (the checker structurally cannot)

- **Uniform ineffectiveness** — a duty present everywhere and obeyed nowhere (Run-2's pre-gate
  report behavior). Only run evidence shows this.
- **Tone/strength decay relative to surroundings** — a block that was load-bearing when landed can
  become wallpaper as the craft prose around it grows; rendered copies are identical by
  construction, salience is not.
- **Wrong altitude** — a verbatim block whose phrasing fits L5 but reads as noise at L1; the cure
  is a registered adaptation, but only a reader notices the need.
- **Redundancy friction** — blocks overlap with levels' own prose (L5's role doc already carried
  report language before the block landed). Overlap is acceptable; contradiction is not; only
  reading catches the moment overlap becomes contradiction.
- **Anchor erosion** — markers keep blocks intact, but if the craft sections around them are
  rewritten, a block can end up in a context that changes its meaning.
- **Template fit** — agents filling the report template badly (rote checklists, payload pasted
  into "Verification evidence") is invisible to the checker; it is exactly what the run-adherence
  audit's report-quality rows are for.
- **Citation completeness** — the E2 runtime floor deliberately stays at "≥1 given ID cited"
  (confirmed when this system landed); whether a report accounts for EVERY given ID is the L5+
  review's and the audit's job, not the walker's. Do not "fix" the floor upward — completeness is
  a judgment call (deferred/escalated IDs are legitimate), exactly what the deterministic layer
  must not adjudicate.

---

*Created: 2026-06-12 — from the Run-2 doc-coverage findings (LIVE-RUN-2026-06-11-FINDINGS.md LR-13/LR-14 + the E2 firings) and the user-settled design shape. Prior art: `working-notes/splice_gate_contracts.py`, `working-notes/GATE-OUTPUT-CONTRACTS-DRAFT.md`.*
