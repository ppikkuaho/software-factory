# Plan-Alignment Gate — Design

**Truth status — 2026-07-24 (owner-docket Q4):** the readiness/decision handoff, deterministic
spine, and trimmed semantic-cell machinery are built. At each new readiness submission the harness reads requirement
tag/MNF metadata only from the current notary-checked intent-spec receipt, parses the one canonical
trace language, and gate-hard checks well-formedness, forward coverage, the per-MNF failure-path
presence rule, and backward coverage. It generates a content-addressed coverage report and returns
deterministic defects only to L2; passing this floor parks the semantic cell and does not wake L1.

The trimmed semantic cell is implemented with its production calibration interlock satisfied: two blind
reconstruction windows, one adversarial comparator doing both diffs and per-MNF adequacy
statements, one whole-portfolio coherence seat, and one atomization audit per frozen intent
fingerprint. All four unique instruction files are owner+director calibrated at exact version 2,
notary-receipted, and paired with explicit model/runtime rows. The reconstruction shape admits a
literal `undetermined` claim only when it names what is missing; the harness converts every such
claim into a required `UNDETERMINED-GAP` elevation routed by the frozen trace owner. Ambiguous
ownership is itself an unresolved required elevation, never a dropped report defect. Specificity
is an output contract/shape check, not a seat; parallel-L1 co-sign, the full
triangulated human package, and dwell-time monitoring are dropped. The human sees only elevated
drift/disagreement/contradiction/unclearable questions. Where the preserved rich design below still
describes a dropped seat or broader human surface, this current overlay and the 2026-07-24
owner-docket ruling govern.

**What this is:** the mechanism that validates the assembled plan against user intent *before* execution — the system's core anti-drift / alignment promise. Produced as a standalone design artifact to be reviewed before integrating.

---

## Plan-Alignment Gate — Final Design

### Purpose

The plan-alignment gate (**L/+** — the transition/cross-level review seat, ruled 2026-07-12; **LE+** wherever the slash would collide with address-path syntax) is the single hard checkpoint between the **design cycle** and the **build cycle**. Its auxiliary reviewers — the two reconstruction windows, the adversarial comparator, the specificity and coherence reviewers — are gate-scoped sub-seats under L/+, named, not notated. It validates the assembled, distributed plan against the user's **intent** — the tagged intake spec — *before* the first line of code is written.

It exists because the planning cascade is a chain of translations (intent → minted ID-set → L2 architecture → planning-L3 (L3&) module designs → workstream criteria/rubrics → build-cycle executable acceptance packages), each performed by a different agent or process step. **Per-level reviews already guarantee local fidelity** — each level checked the level below. The gate exists precisely because local fidelity at every step does *not* compose into global fidelity to intent (the telephone problem). So the gate reads the assembled *whole* against intent once, as a unit, catching the three drift classes that survive local review:

- **Gaps** — dropped requirements (caught by forward coverage).
- **Scope creep** — unrequested additions (caught by backward coverage).
- **Semantic drift** — requirements technically traced but subtly wrong (caught by two-window blind reconstruction + adversarial comparison; coverage is structurally blind to this).

**The translation the gate must NOT treat as axiomatic is its own first step.** The minting of intent prose into atomic requirement IDs is itself a single-agent translation by L1 — the same telephone risk — and it sits *upstream of every check in the gate*. If a compound intent ("share documents securely") collapses to one ID that drops sub-obligations ("encrypted at rest", "audit-logged"), every downstream check validates fidelity to a lossy ID-set and reports green. The gate therefore inspects the **prose→ID seam** explicitly (Check 0) rather than assuming it. This is the single most important change from the prior synthesis: the gate's #1 promise leaks at exactly the seam it used to never look at.

Catching all of this at plan-time is cheap; catching it after code is expensive. That asymmetry is the entire justification for the gate.

The gate's **core promise is faithfulness to intent**. Its honest limit, stated up front: coverage is verifiable, but the final fidelity call on a fuzzy intent is irreducibly human judgment. The gate's job is to make that human call *cheap, well-targeted, and not silently fed a single possibly-wrong machine summary* — not to pretend the judgment away with a number.

---

### Dual Cycles (design / build)

Two nested `Plan → Execute → Review` cycles share exactly one boundary, and **the gate IS that boundary**.

**DESIGN CYCLE** (produces a *validated plan*, never code):
- **Plan** — L1 + the parallel L1-session run the structured intake, producing the tagged intent spec. Minting now carries an **auditable ID→intent-span map** (see Requirements Traceability). L2 turns the spec into the module portfolio + interface contracts.
- **Execute** — planning-L3s design each module. They produce area designs, workstream definitions, interfaces, risks, and falsifiable acceptance criteria/rubrics. Execution-L4 later performs rolling task decomposition during the build cycle. Executable L5 acceptance packages are authored just-in-time by the normal M51 L5 `test_author` path before implementation L5 opens.
- **Review** — the plan-alignment gate. Its only success output is a signed **validated-plan artifact**: the frozen architecture doc + N module designs + M workstream plans/definitions + falsifiable acceptance criteria + rubrics + risk/deep-probe evidence where required, plus the generated RTM, the ID→intent-span map, and the gate's evidence bundle.

**THE GATE IS THE VERTEX OF THE V.** It is a hard big-bang gate on the whole assembled plan. No execution-L3 (L3) is spawned and the Codex/L5 harness is not unlocked until the gate emits **PASS**.

**The gate runs on CANDIDATE interfaces, not frozen ones — and freeze is the gate's post-PASS output, never a precondition.** The planning cascade's compatibility review produces *candidate* interface contracts (the compatibility-reviewed but not-yet-frozen set). The gate consumes those candidates. A checker enforces this ordering observably:

- **Interfaces presented to the gate carry status `candidate`, never `frozen`.** An interface contract whose status is already `frozen` at gate-entry is a structural FAIL (a frozen interface at the gate means something was frozen before the gate ran — the exact ordering bug this forbids). The gate's input manifest lists each interface contract with its status; any `frozen` entry fails Check 1 (tag well-formedness) before any judgment check runs.
- **No reflect-back-pending load-bearing requirement may enter the gate.** Every requirement the gate evaluates must carry a `reflect-back: confirmed` stamp (the user-facing reflect-back confirmation already happened upstream, during/after the planning cascade's compatibility review). A requirement still in `reflect-back: pending` that is load-bearing (any `decided`/`delegated` or must-never-fail) is a structural FAIL at Check 1 — the gate refuses to evaluate, and refuses to let anything be built on or frozen against, an unconfirmed foundation. `deferred` requirements are exempt and listed.
- **Freeze happens AFTER the gate PASSes, as the gate's output.** The signed validated-plan artifact is what carries the frozen interface contracts. The correct order is: **compatibility review produces candidate interfaces → reflect-back confirm → plan-alignment gate PASS → freeze (interfaces stamped `frozen` inside the validated-plan artifact).** Freeze is therefore observable as a state transition (`candidate` → `frozen`) that occurs only on the PASS edge; nothing downstream of intake may stamp an interface `frozen` before that edge.

**BUILD CYCLE** (produces validated code):
- A **fresh execution-L3** (distinct agent from the planning-L3) takes its frozen, gate-approved module design and drives L4 → L5 to write code.
- Execution-L4 decomposes workstreams into tasks and uses the normal M51 L5 `test_author` path to convert the frozen criteria into executable task acceptance before each implementation L5 opens. L5+ reviewers and the right-arm review gates verify code against those executable packages and the underlying frozen criteria/rubrics — these cannot be edited to match the code.

**The clean-context boundary is doubled.** Planning-L3 and execution-L3 are different agents AND the gate is a contextual firewall: the build cycle inherits only the frozen plan + RTM, never the design-cycle conversation. The reconstruction agents and the human both read the *frozen artifact*, never a live conversation.

**Where the gate sits, precisely:** the single transition edge from "plan exists and is internally assembled" to "plan is authorized for construction." A FAIL does not collapse the design cycle — it emits **typed, level-tagged defects**, only the affected branches re-plan, and the gate re-runs **incrementally over the touched subtree** (see Re-Gating). Plan changes after PASS are change-requests whose scope is computed from the RTM, not asserted by the requester (see Post-PASS Changes), and they re-enter the gate — never silent edits.

**Runtime handoff into the gate:** L2 enters this edge by writing a `plan-alignment-ready.json`
marker beside the assembled validated-plan package. The marker points to the package, a minimal
coverage sidecar, and a semantic manifest that physically partitions verification files from
construction modules. Verification files contain only `kind: test`; construction files contain no
tests; every trace-bearing Q3 artifact belongs to exactly one window. Each coverage-sidecar
artifact row is `{path, trace_ids}` and every MNF has a
`failure_path_criteria` row naming its in-package `kind: test`; the sidecar is an index, never a
second truth, so every named ID must exist in the exact named artifact or
`SIDECAR-DANGLING-<ref>` refuses. The bundle identity includes the marker, package, sidecar, listed
artifacts, and frozen intent fingerprint.

Before any ready-state write, the deterministic floor emits
`plan/plan-alignment-coverage.<bundle-sha256>.json`. `deferred` IDs are exempt but listed; valid
`DD-` records are excluded but listed. Structural/coverage defects return through the existing
marker-invalid path to L2 with the report pointer and no L1 wake. PASS records
`semantic_cell_pending` on the running L2 binding and still does not wake L1. The daemon
pre-registers the cohort, opens atomization plus both windows plus coherence through the normal
spawn chokepoint under exact-read enforce-mode blinders, and releases the comparator only after
both reconstruction reports are committed and shape-valid. One content-addressed evidence index
changes the state to ready and emits the single `design-submission` pointer with Q3/Q4 hashes and
the new/changed/cleared elevation delta. This is a normal nonterminal phase handoff, not L2
completion, not the product `L2#review` gate, and not an open blocking question. L1 owns triage and
the verdict, not seat dispatch or required-elevation selection, and returns it with
`harnessctl plan-alignment-decision <L2>#exec --decision pass|fail --file <verdict>`, which wakes L2
with a `plan_alignment_decision` pointer. PASS authorizes freeze and build-cycle opening; FAIL routes
repairs back to the design cycle.

The atomization input is frozen `client-brief/raw-request.md` plus the exact ID→intent-span map and
confirmed reflect-back section. Its stamped report is reused for the same intent fingerprint.
Same-intent repairs diff the prior stamped element/module index and re-run only changed dotted-ID
subtrees plus prior/current trace-graph neighbors. An intent revision resets the full cell; seat
prose is never topology. L1 authors one marker and one confirmable owner question per required
finding. `harnessctl answer <L1>#exec --question-id <id> --decision confirm|reject --file <note>`
closes it. PASS is gate-hard while a required item is missing, unanswered, rejected, or drifted.
With no required elevations, L1 PASS stands alone.

---

### Requirements Traceability

**ID minting — intent owns the namespace, AND minting is itself a reviewed translation.** Every atomic requirement in the tagged intent spec gets a stable ID at intake (`R-001`, `R-002`, …), carrying its tag (`decided` / `delegated` / `deferred`), its priority, its parent outcome, and a **must-never-fail** flag. IDs are minted **only** by L1 at intake — never downstream. But minting is no longer treated as ground truth:

- **Auditable ID→intent-span map (new, V1).** Per minted ID, intake must emit the **verbatim source-intent span(s)** that ID claims to carry. This makes the prose→ID translation inspectable instead of axiomatic.
- **Must-never-fails are decomposed to atomic, individually-testable obligations at intake (new, V1).** "Share securely" is not a single MNF; it is split into the concrete obligations it entails (auth-gated, encrypted-at-rest, audit-logged, …), each its own ID, each separately testable. The **user confirms the decomposition itself** during warm sign-off, not merely the outcome — because a compound MNF minted whole is the highest-stakes place for silent loss.

**Hierarchical, self-describing IDs.** When a requirement splits going down a level, the child ID is the parent dotted with a local index: `R-003 → R-003.2` (L2) `→ R-003.2.1` (L3) `→ R-003.2.1.4` (L4). The dotted prefix *is* the upward trace link; a parent is recoverable by truncation, so a child can never exist without a declared parent.

**Sanctioned element birth below intake, everywhere else is creep — and the two things the old `D-` prefix conflated are now split.** The prior design used `D-` for two colliding kinds of record. They are now distinct prefixes with distinct rules:

- **`DR-` = sanctioned derived-requirement.** A net-new *requirement-element* born below intake. It is legal **only** if it carries a **serves-link**: it names the intent ID(s) it *serves* and the engineering rationale (e.g. "R-017 implies a rate limiter → `DR-017a` serves R-017"). A `DR-` is the **only legal birth of a new requirement-element below intake**; any other route is creep. A `DR-` with no serves-link, or a serves-link naming a non-existent/dead intent ID, is itself a scope-creep defect.
- **`DD-` = design-decision record (ADR-class).** A captured engineering decision (e.g. "use Postgres advisory locks for the unique-order constraint → `DD-014`). A `DD-` is **NOT a requirement**: it imposes no new obligation to verify, it records *how* a chosen requirement is met. `DD-` records are **excluded from the scope-creep scan** — they are decisions, not unsanctioned requirements, so they neither need a serves-link nor count as untraced creep.

A new requirement-element (design element that introduces an obligation, task, or test) may be born in exactly two ways: (a) at intake, or (b) via a `DR-` record with a valid serves-link. Any such element whose tag resolves to neither an intake ID nor a sanctioned `DR-` record is, by definition, scope creep. `DD-` decision records sit outside this scan entirely.

#### Per-element trace-blocks — the machine-readable spine (concrete spec)

Traceability is carried by a **per-element trace-block**: one small, greppable, machine-readable stanza attached to *each element an artifact authors* — not by whole-file `intent_ids:` front-matter and not by prose "Intent IDs" columns in a table. This distinction is load-bearing and is the #1 fix this section makes: a dry-run finishing-pass found levels emitting file-level front-matter (`intent_ids: R-002, R-005, R-007.1`) and prose tables, so the RTM had to be **hand-built** from prose — destroying the "generated, complete-by-construction, never-maintained" property the whole gate depends on. The cure is to make the trace link a property of the *element*, in a fixed stanza shape the RTM-builder can grep with a single pattern.

An **element** is the smallest individually-traceable thing a level authors: an ADR, a module/area, an interface clause (a port, a request/response field, a contract invariant), a substrate primitive, a design element, a workstream, a task, an acceptance test, a rubric line.

**A module/area is traced as `kind: decision` (a carving decision), NOT `kind: requirement`.** An area typically discharges *several* intent requirements at once (a "search" area serves the search intent, the re-index intent, AND the access-control intent), so it has **no single parent intent** to dot a `kind: requirement` child under, and it imposes no obligation of its own — the obligations live in the elements *inside* it. So an area carries a `kind: decision` block (`DD-NNN`) with `serves: [the intent IDs the carving groups]` (informational, not a coverage claim). The single-intent **obligations inside** the area (interface clauses, invariants, substrate primitives, requirements that genuinely split down) each carry their own `kind: requirement` dotted child under the **one** intent they realize — where dotting is unambiguous. This dissolves the multi-parent problem (it lives at the area level, which is a decision; never at the requirement level) and is why a multi-intent area must **not** be forced into a dotted `kind: requirement` or a `DR-` (a `DR-`'s `<seed>` is an intent ID, never an area label). The RTM still recovers "which intents does area X serve" by rolling up the `node` field of the elements inside X — no area-level requirement-block is needed for completeness.

**The stanza.** Each element carries, immediately adjacent to it (the line directly under a heading, the trailing cell of a table row, or an inline tag on a list item), a single-line block of this exact shape:

```
<!-- trace: { id: R-007.1.2, serves: [R-007.1], kind: design, level: L3, node: payments/gateway } -->
```

Fields, all required unless noted:

| field | meaning | rule |
|---|---|---|
| `id` | the element's own minted ID (see minting rule below) | unique across the whole plan; for `kind: requirement` it is a dotted child of its parent; for `kind: derived` it is a `DR-` id; for `kind: decision` it is a `DD-` id |
| `serves` | list of the parent/intent IDs this element discharges | **required and non-empty** for `kind: derived` (the `DR-` serves-link). For a dotted requirement `id`, `serves` is *optional and redundant* — the parent is recoverable by prefix-truncation of `id` — but if present it must equal the truncation. For `kind: decision` it names the requirement(s) the decision is *about* (informational; not coverage). |
| `kind` | `requirement` \| `derived` \| `decision` \| `design` \| `test` \| `adr` | `derived` ⇒ a `DR-` element; `decision`/`adr` ⇒ a `DD-` element (excluded from the creep scan); `design`/`test`/`requirement` participate in coverage walks |
| `level` | the authoring level (`L1`\|`L2`\|`L3`\|`L4`\|`L5`) | used to attribute a missing-tag FAIL to the level that owed it. `L1` is the intake emitter — it stamps the root requirements the whole flow-down chain hangs from |
| `node` | the work-node path the element lives at (e.g. `payments/gateway`) | the same one-spine path that is the agent address / branch / rubric location (see WORKSPACE-SCHEMA.md); lets the RTM join element→node without inference |

The field set is **closed**: a stanza carries EXACTLY `{ id, serves, kind, level, node }` and **no other keys** — a non-canonical key (e.g. `reason`, `serves_also`) is a `MALFORMED-TRACE` reject (put rationale in the ADR/prose body, not the stanza). For any `kind: requirement` block the dotted `id` **must truncate to** its declared parent / served intent — `R-008.6` truncates to `R-008`, so declaring `parent/serves: R-008.1` against an `id` of `R-008.6` is a `TRACE-CONTRADICTION` (a child of `R-008.1` must be `R-008.1.N`).

The stanza is an HTML comment so it renders invisibly in prose docs yet greps trivially (`grep -o 'trace: {[^}]*}'`). In a table, the same JSON object may instead occupy a dedicated trailing `trace` column cell; the RTM-builder accepts both surface forms because both parse to the identical object. **No element may carry its trace data only as prose** (e.g. a bare "Intent IDs: R-005" bullet or a table column of bare IDs) — prose IDs are not a trace-block and Check 1 does not credit them.

#### Trace coverage vs. governance scope

The trace stanza is the coverage spine. It proves how authored elements connect
back to the requirement tree. It is not the only way an ADR, substrate decision,
or interface contract can govern downstream work.

Broad decisions and contracts carry **governance metadata outside the trace
stanza**. The trace stanza stays closed to `{ id, serves, kind, level, node }`;
scope metadata lives in the artifact frontmatter or decision/contract body. A
governing record may attach to:

- a path or subtree on the one-spine workspace address (`payments/**`);
- a named boundary between nodes (`payments -> ledger`);
- a port, contract, enum, invariant, or substrate surface.

For `kind: decision` / `kind: adr`, `serves` remains informational rather than a
coverage claim. It may name several affected requirements, or be empty when the
decision governs by path/boundary scope instead. True obligations introduced by
an ADR or contract still get their own `kind: requirement`, `kind: derived`, or
`kind: test` trace blocks. The plan-alignment and higher review gates therefore
consume two related indexes:

- the generated RTM/trace slice for requirement coverage;
- the generated governance slice for ADRs, substrate decisions, and interface
  contracts that apply by path scope or boundary intersection.

Worked example — the same ADR and interface clause the dry-run emitted as prose, now as trace-blocks:

```
# ADR-002 — Money is integer minor units + ISO-4217 currency
<!-- trace: { id: DD-002, serves: [R-005, R-007, R-008], kind: decision, level: L2, node: substrate } -->

| field  | type           | notes                              | trace                                                                                          |
|--------|----------------|------------------------------------|------------------------------------------------------------------------------------------------|
| amount | Money          | integer minor units; == order total| `{ id: R-005.1, serves: [R-005], kind: requirement, level: L2, node: payments/gateway }`        |

### CI-1 (contract invariant)
<!-- trace: { id: R-007.1.1, serves: [R-007.1], kind: requirement, level: L2, node: payments/gateway } -->
Two charge() calls with the same domain_idempotency_key produce at most one CHARGED.
```

#### Dotted-child-ID minting rule

Each level **mints the child IDs it authors under its parent's prefix, at the moment of authoring**:

- A requirement that splits going down a level takes the parent ID dotted with a **local index assigned in author order at that node**: `R-003` (intake) → `R-003.2` (L2 area) → `R-003.2.1` (L3 design element) → `R-003.2.1.4` (L4 task). The local index is unique among siblings under the same parent; gaps are allowed (deletions need not renumber), reuse is forbidden.
- **The dotted prefix IS the upward trace link.** A parent is recovered by truncating the last `.N` segment. Therefore a child requirement ID can never exist without a resolvable parent, and the RTM needs no separate "parent" field for dotted IDs — it truncates.
- **Minting is scoped to the authoring node**, so the same `R-003.2` subtree-prefix simultaneously scopes the workspace path, the branch, the rubric file, and the gate's re-run window (One Spine — see PROJECT-PLANNING.md and WORKSPACE-SCHEMA.md). Re-gating a FAIL is `grep`-by-prefix over `id`/`serves`.
- **`DR-` derived requirements** are minted as `DR-<seed><suffix>` (e.g. `DR-017a`) where `<seed>` is an intent ID they serve; they are *not* dotted children (they have no single parent) and instead carry their parent linkage in `serves`. **`DD-` decisions** are minted as a flat `DD-NNN` sequence (ADR numbering); they impose no obligation and never appear in coverage.
- IDs of `kind: requirement` are minted **only** by intake (root `R-NNN`) and by the level that performs the split (dotted children). No level may invent a non-dotted `R-` id; that is a structural FAIL (an orphan requirement with no parent).

#### Emission rule — a hard output contract, not hygiene

Every level **emits a trace-block for every element it authors**, as a non-optional clause of its return contract:

- **L1/intake** emits one per minted root requirement (`R-NNN`), `kind: requirement`, `level: L1`, carrying that requirement's verbatim intent-span — the roots the entire flow-down chain truncates back to.
- **L2** emits one per area/module, per substrate primitive, per ADR (`DD-`), and per interface clause (each port, each request/response field, each contract invariant).
- **planning-L3** emits one per design element it introduces inside its area.
- **L4** emits one per workstream and per task.
- **the acceptance authoring path** emits one per acceptance test and per rubric line (`kind: test`), keyed to the requirement it verifies.

Each level tags **only what it created**, at the moment it holds the context to know the link — never retroactively, never for a sibling's element. A level that receives an inherited ID it cannot place must **raise it (escalate up), not silently drop it**; a dropped ID surfaces downstream as a coverage gap with no owner, which is exactly what the emission rule exists to prevent. Decision-complete flow-down means each child node receives its responsible ID-set in its brief and is accountable for emitting trace-blocks discharging that set.

#### Enforcement spec — what the preflight / return-contract hook checks

The trace-block obligation is enforced by the **return-contract / preflight hook** (infrastructure to be built; this is the spec it implements). The hook runs at every level's output boundary and at gate entry, and it **REJECTS the artifact** (does not let the level report complete / does not admit it to the gate) on any of the following. All checks are deterministic and greppable — no model judgment:

1. **Every element has a trace-block.** The hook walks the artifact's elements (headings of the recognized kinds, table rows in a contract's field/invariant tables, list items in an acceptance list) and requires each to carry a parseable `trace:` stanza adjacent to it. A recognized element with no stanza is a **MISSING-TRACE-`<element>`** rejection, attributed to `level`. This is the check that makes "coverage looks fine because nobody tagged that module" impossible.
2. **Every stanza parses.** Malformed JSON-ish object, missing required field (`id`, `kind`, `level`, `node`, plus `serves` when `kind: derived`), or unknown `kind` ⇒ **MALFORMED-TRACE-`<id|line>`**.
3. **Parent resolves by prefix-truncation.** For `kind: requirement` with a dotted `id`, truncating the last segment must yield an `id` that exists somewhere in the assembled plan (or is a root `R-NNN` from the intent spec). No resolvable parent ⇒ **DANGLING-PARENT-`<id>`**. If `serves` is also present it must equal that truncation, else **TRACE-CONTRADICTION-`<id>`**.
4. **`DR-` carries a valid serves-link.** For `kind: derived`, `serves` must be non-empty and every id in it must resolve to a **live** intent/requirement ID (exists and is not `deferred`-dead). Empty serves ⇒ **DR-UNSERVED-`<id>`**; serves naming a non-existent/dead id ⇒ **DR-DEAD-SERVES-`<id>`**. (These are the scope-creep defects called out for `DR-` above, now mechanically detectable.)
5. **No duplicate IDs.** The same `id` minted on two elements ⇒ **DUP-ID-`<id>`**.
6. **No dangling references.** Any id appearing in a `serves` list that resolves to no minted element anywhere ⇒ **DANGLE-`<id>`**.
7. **`DD-` is fenced out of coverage.** A `kind: decision`/`adr` element is *not* required to carry a `serves`-coverage link and is *never* reported as creep; the hook only verifies its `id` is a well-formed unique `DD-NNN`.

A `PASS` from the hook is the precondition for a level to report complete and for the assembled plan to enter the gate. The hook's rejection list is itself emitted as typed defects keyed to `level`+`node` so the fix routes to the owner.

#### RTM harvest — generated, complete-by-construction

**The RTM is generated, never maintained.** At gate time the **RTM-builder** (L1-owned lateral, deterministic script) greps **every** trace-block out of the frozen artifacts with one pattern, parses each to its object, and **joins them mechanically**: by truncation (`id` → parent → … → root `R-NNN`) and by `serves` (for `DR-`), and against the ID→intent-span map for the root spans. Because each element already declares its `id`, `serves`, `kind`, `level`, and `node`, the join needs no inference and no prose-reading — the matrix is **complete-by-construction modulo missing tags, and missing tags are caught by Check 1 (hook check 1) before the build runs.** A trace-block that the hook admitted but the RTM cannot join is itself a detectable failure, not a silent hole.

Row shape, one row per leaf requirement ID:

`[Req-ID | parent | intent-span(s) | text | tag | source-outcome/must-never-fail | L2-element | L3-design-element | L4-workstream/task | acceptance-criterion/test-ID(s) | rubric-item(s) | verification-status]`

Each non-Req-ID column is filled by selecting the harvested elements whose `serves`/dotted-prefix reaches this requirement and whose `level`/`kind` match that column (e.g. the acceptance column = all `kind: test` verification elements serving this id). In V1, both design-cycle acceptance criteria and build-cycle executable acceptance tests use `kind: test`; a future schema may split criteria into a dedicated `kind: criterion`, but the gate does not need that split to enforce coverage. `DR-` derived requirements get their own rows with a **"serves"** column instead of a parent. `DD-` design-decision records are harvested into a **separate decision log**, not RTM requirement rows — they never appear in the forward/backward coverage walks. The matrix stores trace links, not judgments — it produces *candidate* findings that the judgment checks then adjudicate. It is versioned with the plan; the gate consumes the frozen snapshot. **All checks are keyed by dotted-ID prefix** (a `grep` over `id`/`serves`) so the gate can re-run over a subtree rather than the whole on a FAIL.

---

### The Gate

The gate runs a fixed-order sequence, split into two kinds. **Verifiable checks gate hard** (deterministic, immune to model-shared blind spots). **Judgment checks emit an evidence bundle**, never a pass/fail verdict — they assist the human gate rather than replace it. The gate **cannot PASS on a verdict alone**: it must emit evidence (the resolved RTM defect list, both reconstruction texts and their diff, the per-MNF adequacy statements, named contradiction-pairs). A vacuous or hand-wavy evidence bundle is a structural FAIL (and "vacuous" is now contract-checked, not merely "non-empty" — see Check 5) — this is the anti-rubber-stamp property.

| # | Check | Type | Who | What it does / gating behavior |
|---|-------|------|-----|--------------------------------|
| 0 | **Atomization completeness** *(new)* | Judgment (evidence) → routes to human, never cleared by L1 | Independent atomization auditor (NOT L1, clean context) | Reads the raw intake transcript/intent prose and the minted ID list + intent-span map side by side and answers one question: **"name every testable obligation in the prose that is NOT cleanly carried by some ID."** This is forward-coverage applied one level higher — at the prose→ID boundary the RTM otherwise treats as axiomatic. Output: typed `UNMINTED-<span>` findings. Because these implicate L1's own intake, they **cannot be cleared by L1** (Check 7) and surface to the human by construction. Any MNF span found under-decomposed is force-expanded in the human package. |
| 1 | **Tag well-formedness** | Verifiable (hard) | RTM-builder (deterministic script) | Re-runs the **per-element trace-block enforcement spec** (see Requirements Traceability → Enforcement spec) over the assembled plan: every recognized element carries a parseable adjacent `trace:` stanza; every dotted child ID resolves to a parent by prefix truncation; every `DR-` carries a live serves-link; no duplicate IDs, no dangling references; `DD-` fenced out of coverage; every minted root ID carries a non-empty intent-span. A missing trace-block from any level is a FAIL — prevents "coverage looks fine because nobody tagged that module." This is the gate-entry re-assertion of the same checks the preflight hook ran at each level's output boundary. |
| 2 | **Forward coverage / gap scan** | Verifiable (hard) | Coverage-analyzer (deterministic script over the ID graph) | Walk the RTM down: every `decided`/`delegated` (non-deferred) requirement must trace to ≥1 `kind: design` element AND ≥1 `kind: test` falsifiable acceptance criterion or executable acceptance test. Empty design = `UNCOVERED-<id>`; empty verification = `UNTESTED-<id>`. Every **must-never-fail** ID additionally requires its own sidecar row naming an in-package failure-path criterion that traces to it; absence or an invalid mapping = `MNF-NO-FAILURE-PATH-<id>` (a nonexistent/stanza-less reference is also `SIDECAR-DANGLING-<ref>`). Adequacy remains the Q4 comparator's judgment. Executable negative tests are produced by the M51 `test_author` path before implementation opens. `deferred` IDs are exempt but listed. |
| 3 | **Backward coverage / scope-creep scan** | Verifiable (hard) | Coverage-analyzer (deterministic script) | Walk the RTM up over **net-new elements only**: every requirement/design/test element must trace to ≥1 live intake requirement ID **or** to a sanctioned `DR-` record (which must itself carry a valid serves-link to a live intent ID). An element tracing to neither = `ORPHAN-<element>`; a missing/dead/deferred-only DR service = `DR-UNSERVED-<id>`. **`DD-` design-decision records are EXCLUDED from this scan** — the analyzer confirms and lists them but never treats a `DD-` as creep or demands a serves-link. `delegated` requirements are live coverage obligations; `deferred` requirements are exempt from forward coverage and are not live roots for built elements. |
| 4a | **Two-window blind reconstruction** *(strengthened)* | Judgment (evidence) | Two fresh reconstruction agents, clean context, no plan-authoring lineage, **forbidden the intent doc**; a **separate comparator** does the diff | **Window A reads only the verification artifacts** (design-cycle acceptance criteria + rubrics, plus executable suites where a risk/deep-probe path already produced them). **Window B reads only the construction artifacts** (design/plan prose). Each writes, in the user's domain/outcome language, **"a system built/verified by this would actually DO the following"** — behaviors, scope boundaries, what it refuses to do, assumptions baked in — committing its output **before any comparison**. **First diff A↔B:** where the two reconstructions of "what it does" DISAGREE, that is a criteria↔design drift (correlated mis-reading laundered through the verification plan) that neither window alone surfaces; emitted as `TEST-DESIGN-SPLIT` findings and surfaced to the human as explicit uncertainty, never collapsed to one confident playback. |
| 4b | **Adversarial comparison to intent** *(strengthened, pulled into V1)* | Judgment (evidence) | Adversarially-seeded comparator (separate agent; sees intent, never read the plan as authority) | Diffs both reconstructions against the tagged intent's outcomes + must-never-fails. **Seeded adversarially in V1, not deferred:** "assume a wrong-but-plausible realization exists; for each must-never-fail, state the realization that would technically pass these tests while violating intent." Emits typed semantic-drift findings (`DRIFT` / `SILENT-ASSUMPTION` / `SCOPE-SHIFT`), keyed to requirement IDs. The comparator hunts the gap rather than confirming consistency. Additionally, **per must-never-fail it reads the failure-path test and states in one line WHAT failure it exercises and HOW the assertion catches it**; an MNF test whose described mechanism is vacuous or tautological is a **FAIL** (this is the judgment half of the MNF check — presence is Check 2, adequacy is here). |
| 5 | **Evidence-specificity check** *(new)* | Verifiable heuristic + judgment | Specificity reviewer (different agent) + deterministic heuristic | Enforces that the reconstruction/comparison evidence is *falsifiable by contract*, not merely present. Each outcome and each must-never-fail must carry **one concrete behavioral claim that could be wrong** — an input→output pair, a refusal, or a boundary — not a topic label ("manages documents with appropriate controls"). Reconstructions failing to make per-outcome falsifiable claims are **rejected and re-run**, not passed. This applies the output-contract discipline to the evidence itself, closing "fluent vacuity satisfies evidence-mandatory PASS." |
| 6 | **Coherence / whole-portfolio cross-assumption scan** *(restored to a real V1 pass)* | Judgment (evidence; verifiable sub-parts) | Coherence reviewer (L2-altitude lateral, whole-portfolio read, structurally independent from the L3s that wrote the parts) | Enumerate cross-module **shared assumptions** — data shapes, ordering, auth/session state, error contracts — and flag conflicts. **Explicitly NOT keyed on shared requirement IDs**: the dangerous contradictions are between modules that independently assume incompatible things with *no* linking ID (each traces cleanly to its own requirement). Output: contradiction-pairs keyed to module pairs + the assumption in conflict. Pass scales with module count rather than folding into a single reconstruction read. *(Typed interface-skeleton compilation stays V2; the prose-level whole-portfolio assumption scan does not.)* |
| 7 | **L1 guard triage + package build** *(conflict-of-interest fenced)* | Judgment | L1 client-interface, with a narrow mandatory parallel-L1 co-sign | Triage all findings against the opinionated-areas map and decided/delegated/deferred tags; distinguish true failures from acceptable derivations (promote legitimate `DR-` items to approved-derived); tag each defect with a **routing target** (the level that introduced it) for local, incremental fixes; auto-route mechanical/uncontested findings to the owning level. **Two fences on L1 judging its own intake:** (a) any finding keyed to an **intake ID, a delegated/opinionated area, or an atomization (`UNMINTED`) span** that L1 wants to disposition as `accept`/`acceptable-derivation` **must be co-signed by the parallel L1-session** before it can be cleared without user view; (b) **atomization findings (Check 0) and any must-never-fail defect cannot be cleared by L1 at all** — they surface to the human by construction. This keeps L1's mechanical-routing value while removing its ability to bury self-implicating findings. Then build the digestible sign-off package. |
| 8 | **Human sign-off** | Judgment (irreducible) | The user | The one gate that cannot be delegated upward or automated away. PASS unlocks the build cycle. |

**Anti-theater properties baked into the checks (all ship in V1):**
- **Two-window blind reconstruction** — verification-artifact and construction-artifact reconstructions are diffed against *each other* first, so correlated test↔design drift surfaces as disagreement instead of being laundered through the tests.
- **Adversarial comparator in V1** — the comparator hunts the wrong-but-plausible realization rather than confirming consistency; the single non-adversarial comparator is no longer the weakest link.
- **Evidence falsifiable by contract** — reconstructions must make per-outcome claims that *could be wrong*; fluent-but-vacuous evidence is rejected and re-run, not passed.
- **MNF adequacy is judged, not just counted** — a failure-path test's described mechanism must be non-vacuous; deterministic-green presence is never relabeled "pinned."
- **Atomization treated as a reviewed translation** — the prose→ID seam is inspected by an independent auditor and its findings cannot be cleared by L1.
- **L1 cannot bury self-implicating findings** — intake/atomization/MNF dispositions are co-signed or surfaced to the human.
- **Behavior over citations** — reconstruction ignores ID tags and re-derives behavior, so a nominally-tagged-but-wrong link surfaces as drift.
- **Tests/rubrics authored from spec by not-the-worker** — the artifact anchoring a requirement is independent of the tagger and of the code.
- **Structural independence** — gate agents share no authoring lineage with planners; use a different model where available; seed the comparator adversarially.

---

### Re-Gating (incremental, in V1)

Whole-plan big-bang re-gating on every FAIL would make the gate cost-dominate the design cycle and become rational to skip under deadline — which is the exact skip the gate exists to forbid. So incremental re-gating is **V1, load-bearing, not a V2 optimization**:

- All checks are **keyed by dotted-ID prefix**. A FAIL re-runs coverage + reconstruction + the adversarial comparison **only over the touched subtree plus its coherence-neighbors** (modules sharing an assumption or a requirement ID).
- **First-round findings are batched into ONE verdict** with **one round of per-level kickbacks** — no kickback storms; iteration rounds are bounded.
- The **human is re-presented only the deltas since their last view**, never the whole package again.

Cost predictability is treated as an anti-theater property here: a gate that is economically irrational to run at full strength will be run at reduced strength.

---

### Human Sign-off & the Warm Intake

The user is shown a **single digestible package** — never the raw distributed plan — structured the way a design practice presents an as-designed review. The intake is the **signed brief**: because the user co-authored and signed the tagged intent spec (and, now, the must-never-fail *decomposition*), sign-off is a **warm diff against their own brief**, not a cold read of a foreign document. Most rows are green and collapsed; attention goes to the flagged items and the fuzzy zones.

The package, ordered by what the human is uniquely positioned to judge:

1. **The Playback (first, highest-signal) — triangulated, not a single summary.** For each outcome and must-never-fail, the human sees **three columns side by side**: the reconstruction's behavioral claim, the actual acceptance-test assertion(s) it's keyed to, and the intent span it's keyed to. A reconstruction error then shows up as a *mismatch between prose claim and concrete test*, visible rather than hidden inside smooth prose — the human is no longer signing off on a single, admittedly-noisy machine summary as their only input. Where the two-window reconstructions **disagreed**, that is surfaced explicitly as **"we are unsure what this will do"** rather than collapsed to one confident playback.
2. **The Findings Ledger (deltas only).** Each gap, scope-creep item, semantic-drift flag, atomization gap, silent `delegated`-area default, and surviving contradiction — one line of intent-language each, with a **proposed disposition** (`accept` / `fix-and-route-to-level-N` / `defer`). The user approves *decisions*, not raw diagnostics. Drift is phrased as a confirmable question: "the plan as written would do X for R-007; you asked for Y — confirm?"
3. **The Must-Never-Fail Roster.** The non-negotiables with their verifying (failure-path) tests **and the one-line adequacy statement from Check 4b** (what failure each test exercises and how it catches it), so the user confirms they are genuinely pinned — not merely present-and-tagged. MNFs are **always force-expanded, never collapsed.**
4. **The Residual.** An honest short list of judgment calls the system made inside `delegated` areas, anything it is genuinely unsure aligns, **any intent areas the atomization auditor rated thin/ambiguous**, and any two-window disagreements — so the human gate is pointed *at* the fuzzy zones rather than lulled by green checks.

**Green-collapse keys on tested-for-drift, not absence-of-flag.** A requirement may be collapsed only if it is **both fully covered AND carried an explicit, falsifiable, intent-keyed reconstruction claim that the adversarial comparator actively tried to break and could not**. Coverage completeness alone never licenses a collapse, because the gate's raison d'être is the cleanly-traced-but-semantically-wrong requirement — which would otherwise render green-and-collapsed in exactly the region the human is steered to skip. **Must-never-fails and any requirement on a thin/ambiguous intent span are force-expanded.** Absence of a flag is never rendered as presence of alignment.

A one-line provenance footer states which checks passed mechanically and which are judgment. `decided` requirements that traced cleanly *and survived the drift hunt* are shown collapsed and expand on request.

**Human-gate health monitoring (new, explicit residual instrument).** The system cannot engineer away the risk that a human signing off on warm diffs across many gate runs drifts toward "looks right, approve." It can at least *monitor* it: the gate tracks **sign-off dwell time and the expansion rate on flagged/force-expanded items** as a health signal that the human gate is still real. A collapse in dwell time / expansion rate is surfaced (to the user, and to optimizer-L1) as a warning that the irreducible gate may have gone slack — not as an automated override, but as the one observable proxy for the failure no machinery beneath the human can catch.

**The system never manufactures a fake alignment number.** Coverage, tag-completeness, atomization-presence, and contradiction detection are reported as verifiable. Fidelity is presented as a *reasoned recommendation with its evidence*, and the human renders the verdict. Collapsing this into an automated alignment percentage would launder a judgment call as a measurement and re-introduce the rubber-stamp the gate exists to prevent.

---

### Post-PASS Changes

Plan changes after PASS are change-requests, never silent edits — and **scope is computed from the RTM, not asserted by the requester** (whoever wants the change, often to unblock execution, is the party least neutral about calling it "local"):

- Any change touching an element **traces UP to its requirement ID(s)**; the affected subtree is the **union of those IDs' subtrees PLUS their coherence-neighbors PLUS any must-never-fail in scope** — computed from the trace graph, not from a human's scope estimate.
- The affected subtree re-enters the gate incrementally (per Re-Gating).
- **Any change that touches a must-never-fail or a user-confirmed delegated default re-enters the HUMAN gate**, not just the level gate — the human confirmed it, only the human releases it.

This makes "silent edit" structurally impossible by gating on the trace graph rather than on a scope estimate, closing the drift channel during the more expensive, higher-pressure build phase.

---

### The Honest Residual

- **Intent is fuzzy; the final fidelity call is irreducibly human.** Coverage proves structural completeness; it does not prove the plan *means* what the user intended. The user sign-off is the irreducible gate. The gate makes that decision cheap, well-targeted, and triangulated — it does not remove it.
- **The human gate degrades under repetition and volume — the single biggest residual.** Deltas-only re-presentation, three-column triangulation, force-expanded fuzzy zones, and surfaced two-window disagreement all *reduce* the rate of slack approvals; none *eliminate* it. The system therefore **monitors** the gate's health (dwell time, expansion rate) and surfaces degradation, accepting that a rubber-stamping human silently nullifies the core promise no matter how good the machinery beneath them is. This is named, instrumented, and not pretended away.
- **No automated alignment score.** Deliberately refused. A green number manufactures false confidence.
- **A wrong signed brief ships the wrong thing faithfully** — but the surface area is smaller now. Atomization-completeness (Check 0) catches *lossy* minting; MNF decomposition + user confirmation catches *compound-MNF* loss; coherence + reconstruction surface brief-*internal* contradictions ("R-007 and R-012 cannot both hold") that route UP to L1 to re-open intake; and intake/atomization findings cannot be cleared by L1. Fully residual: a coherent, non-contradictory, fully-atomized but *wrong* brief — where intake correctly captured what the user *said* and the user *misspoke* — is named honestly in the Residual section, not hidden.
- **Reconstruction is itself non-deterministic and can produce noisy false positives.** Mitigated by emitting *findings keyed to IDs*, not verdicts; the two-window split converts some noise into signal (real disagreement) rather than a single confident-but-wrong narrative; L1 triages; only confirmed divergences reach the user. Coverage/coherence carry the hard-gating load.

---

### MVP vs Later

**V1 — the non-negotiable core** (catches all three drift classes at plan-time *including drift introduced at minting*; keeps the human fidelity gate cheap, triangulated, and economically runnable):

1. Hierarchical requirement IDs minted at intake, with `decided`/`delegated`/`deferred` tags, the **must-never-fail** flag, **the verbatim ID→intent-span map**, and **MNFs decomposed to atomic obligations confirmed by the user**.
2. The per-level **trace-block output obligation**, enforced by the return-contract/preflight hook (a one-line parent-ID `trace:` stanza adjacent to each element — under its heading, in the row's trailing `trace` cell, or as an inline tag — never whole-file front-matter).
3. The **RTM-builder** generating one matrix mechanically from the frozen artifacts and the span map — generated report, not maintained document — **keyed by dotted-ID prefix** for subtree re-gating.
4. **Atomization-completeness (Check 0)**, **forward + backward coverage**, and **tag well-formedness** — the deterministic and prose→ID checks; coverage/tagging gate hard, atomization findings route to the human.
5. **Two-window blind reconstruction** (verification-artifact window + construction-artifact window, diffed against each other first) + **adversarially-seeded comparison to intent** + **per-MNF adequacy statements** — all V1, clean context, enforced via the spawn contract.
6. **Evidence-specificity check** — reconstruction evidence must be falsifiable-by-contract or it is re-run, not passed.
7. **A whole-portfolio coherence pass** (cross-module shared-assumption scan, NOT shared-ID-keyed) scaling with module count.
8. **L1 guard pass with the conflict-of-interest fences** — narrow mandatory parallel-L1 co-sign on intake/atomization/delegated dispositions; atomization and MNF findings un-clearable by L1.
9. **Incremental subtree re-gating**, batched one-verdict / one-round kickbacks, deltas-only human re-presentation.
10. **Warm human sign-off** as a hard lock on the build cycle, on the **three-column triangulated playback** + findings ledger + force-expanded MNF roster + residual, with **green-collapse keyed on tested-for-drift, not absence-of-flag**, and **human-gate health monitoring** running.

**Always-on even in V1** (cheap, load-bearing, anti-theater): two-window blind-then-compare ordering; adversarial comparator seeding; reconstruction reads behavior not citations; evidence falsifiable by contract; tests/rubrics from spec by not-the-worker; MNF adequacy judged not just counted; atomization treated as a reviewed translation; L1 cannot clear self-implicating findings; and **no automated alignment score**.

**Deferred — grown later by optimizer-L1, in priority order** (each covers a failure class the V1 core can run without; thicken precisely the check the first real run shows leaking):

1. **Interface-skeleton compiler** — force prose interfaces into typed stubs/schemas and machine-check them, converting future integration bugs into present verifiable defects. (The prose-level whole-portfolio coherence scan is V1; this is the typed hardening on top.)
2. **Multi-reconstruction voting** beyond the two windows; multi-model independence as standing policy on every gate seat (V1 uses a different model where available and seeds adversarially).
3. **`DR-` derived-requirement promotion workflow** richer than the V1 list-and-route (and richer `DD-` decision-log linkage); per-requirement risk weighting beyond the binary must-never-fail flag.
4. **Collapse/expand UI sophistication** and richer human-gate-health analytics (V1 tracks dwell time + expansion rate and surfaces degradation).
5. **Broadening the parallel-L1 co-sign** from the narrow intake/atomization/delegated fence to a wider second-opinion pass on more disposition classes, as cost allows.
