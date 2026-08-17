# Role Resolution — How a Role Is Resolved at Boot

The canonical spec for **how a spawned agent becomes who it is** at boot. It reconciles the H40
boot model (resolved 2026-06-05) with the write-jail (SECURITY §1.4) and the spawn chokepoint
(DAEMON §6): the system prompt is one shared minimal constant; the **role arrives as documents the
agent reads in place** under the read-allow graph. No per-level system prompt, no inline-flatten.

> **2026-07-13:** L5 unified onto the Claude Code standing-seat pattern (Sol main loop) — the Codex-seat resolution path below is dormant capability, retained for any future non-Claude runtime. See the working-notes ruling note `L5-RUNTIME-UNIFICATION-RULING-2026-07-13.md`.

Authoritative sources, do not contradict: `operational/shared/agent-definition-principles.md` §4
(the reframe), `operational/shared/system-prompt.md` (the promoted shared prompt),
`working-notes/DEFERRED-REGISTER.md` Decision A (write-jail) + Decision B (this reframe),
`DAEMON.md` §6 (spawn chokepoint) + §3.2 (binding schema), `SECURITY.md` §1.4 (the read graph).

> **Supersedes** the old "`--system-prompt-file` = per-level `role.md`" model wherever it still
> appears (DAEMON §6.2 line 930, §3.2 `role_file`, SECURITY §1.4 / §356 / §504, IMPLEMENTATION-PLAN
> adapter + tests). Those bake `role.md` into the prompt; this doc is what they reconcile *to*.
> Reconcile, don't redesign — no new mechanism is introduced beyond what §4 + Decision B fixed.

---

## 1. The split — shared minimal prompt (constant) vs role-as-delivered-documents

H40 asked: the Claude Code base prompt frames the model as a deferential *coding assistant*, which
fights every non-coding seat (L1 Orchestrator, L2 Architect, a reviewer). **Resolved: don't fight
the base framing — replace it, and deliver the role separately.** Two distinct pieces, never folded
into one:

- **THE SYSTEM PROMPT (constant, shared).** `--system-prompt-file` is **always**
  `operational/shared/system-prompt.md` — the ONE shared minimal prompt, byte-identical for L1–L5.
  It exists now (promoted 2026-06-05). It is **not** a per-level file and **not** `role.md`. It
  *replaces* Claude Code's base coding-assistant block (base block 2), keeps the 57-char identity
  line and the default 24-tool set, and stays OAuth/interactive-safe. Its opening line points the
  agent at its own node: *"your role, scope, and current task are delivered as documents in your
  workspace — read those first."* The SWE-craft framing is gone at the source, so role docs no
  longer have to *compensate* for an assistant default fighting them.
- **THE ROLE (delivered as documents the agent READS at boot).** Who the agent is and what it is
  doing are **files it reads in place** — never strings baked into the prompt, never flattened into
  a bundle. Two regions:
  - **Node-local:** the instantiated spawn brief (the filled `spawn-template`, written into the
    node) carries identity (address, level, role-variant) **and** its load-manifest (the
    "Identity — Load These Documents" list), plus the frozen read-only `acceptance.md`. The brief is
    the per-spawn delivery vehicle; it lives in the node.
  - **Read-allowed harness docs:** the per-level `operational/L{n}/{soul,role,config}.md` and the
    always-loaded `operational/shared/*` contract docs, read **in place at their harness-root
    paths** — not copied, not inlined.

**Why the split (three reasons, all load-bearing):**

1. **Token-saving + no-out-of-role-pull** — the shared prompt is deliberately bare so it carries no
   SWE/coding-assistant framing to pull a non-coding seat out of role, and the same file serves
   every level (no reason to fork it).
2. **Write-jail plus declared-read grants make refs resolve without broad harness access** —
   Decision A confines *writes* to the node. Strong-form blinders grant only the exact
   load-manifest/identity/launch paths and visible resolved references (§3), so declared docs
   resolve in place without opening arbitrary `design/` or `operational/` files. H40 also confirmed
   role-content *outside* the system prompt bites hard.

This applies to **Claude (Opus) seats**, which have a base prompt to replace. A **Codex-harness seat** would run
with no Claude base prompt; its literalness concern is handled in the brief
(`runtime-and-model-map.md`) — but that path is **dormant since the 2026-07-13 L5 unification**: L5 now
runs GPT-5.6 Sol on the Claude Code harness and follows the Claude-seat path above. The
role-as-documents half applies to **both** — a Codex seat would also read its role + brief from its node.

---

## 2. The per-seat LOAD-MANIFEST

What each seat reads at boot. The manifest lives in the brief's "Identity — Load These Documents"
list; the chokepoint assembles it from the binding's `role_variant` (§4). Three tiers:

| Tier | Documents | Loaded |
|---|---|---|
| **Shared — always** | `operational/shared/system-prompt.md` *(this is the **prompt**, not a read-doc)* | every level, as `--system-prompt-file` |
| | `operational/shared/comms-protocol.md`, `agent-lifecycle.md`, `runtime-and-model-map.md` | every level — each header says "loaded at boot for all levels" |
| | `operational/shared/agent-definition-principles.md` | definition-authoring levels (L1–L4) — its header scopes itself; **L5 omits it** |
| | `operational/shared/git-protocol.md` | levels that own code movement and gate evidence — its header scopes itself |
| **Per-level** | `operational/L{n}/soul.md` (one-line pointer), `role.md` (boundaries/outputs), `config.md` (self-monitoring) | the agent's own level |
| | **L1 extras:** `handbook.md`, `intake-session-template.md` | L1 |
| | **L3 extra:** `planning-template.md` | L3 (planning seat) |
| | **L5 extra:** `swe-handbook.md` | L5 |
| **Node-local** | the filled spawn brief (identity + this manifest + resolved decisions) | per-spawn, written into the node |
| | frozen read-only `acceptance.md` (the rubric, authored before the work) | per-spawn, at the node |
| | per-project `conventions.md` / `README.md` / append-only `log.md` (read-only reference) | per-project, per F34 read scope |

**Seat-variants select different manifests.** The `role_variant` field (`#exec`, `#review`,
and the future first-class `#test`) selects **which** per-level docs + bundle the chokepoint
assembles into the brief. An `L5#exec` reads the L5 executor bundle; an `L5+#review` reviewer (a
separate Opus seat) reads the reviewer manifest against the *same* frozen `acceptance.md`. Same
shared prompt for all of them; different read-set. Current V1 exception/refresh acceptance
authoring does not open a live `#test` seat; it uses an L5 test-author child plus L5+ review.

The manifest is **authored** in each level's `spawn-template.md` "Identity — Load These Documents" /
"Read before anything else" sections — those lists ARE the per-seat manifest (e.g. L5's lists
`soul/role/config/swe-handbook` + brief + frozen `acceptance.md`). This doc is the canonical index;
the templates are the per-seat instances.

### Context Layers At Spawn

There are three distinct context layers:

1. **Auto-loaded identity.** For Claude-Code seats, the adapter composes the shared prompt plus the
   selected `soul.md` / `role.md` / `config.md` trio into `.identity-prompt.md` before the first model
   token. For a Codex-harness seat (dormant since the 2026-07-13 unification; none active), the boot prompt would list the same trio as required first-reads. This prevents
   the role from depending on agent diligence.
2. **Boot-read task package.** The node-local `brief.md`, frozen `acceptance.md`, and load-manifest
   documents are the agent's ordinary working context. They should contain everything the role normally
   needs to begin correctly.
3. **Readable lookup surface.** The path-derived visibility graph permits the parent's and known
   same-parent siblings' **direct node surfaces**, not their nested subtrees. Exact visible
   reference-map targets extend that surface when a concrete dependency, interface question,
   ambiguity, or integration check calls for them. They are not a request to ingest every readable
   file at startup.

This split is intentional. The system should prepare the agent's immediate context deterministically,
then leave a bounded lookup surface for legitimate situational context. Agent prompts and templates
should make the boot package feel like the normal starting point and sibling/parent reads feel like
targeted lookup, not default exploration.

---

## 3. Reference resolution — read-in-place against the harness root

> **AMENDED 2026-06-12 (user ruling, LR-4 cure — identity auto-load):** the per-level IDENTITY
> TRIO (`soul.md`/`role.md`/`config.md`, `brief.identity_docs`) no longer rides on agent
> diligence. The Claude-Code adapter FLATTENS it (shared prompt first, provenance headers per
> doc) into a per-spawn composed system prompt (`<workspace>/.identity-prompt.md`) that argv
> points at; the Codex adapter (dormant since the 2026-07-13 unification) would list the trio as explicit first-reads in its boot prompt (its
> native system message stays — user decision). Evidence: run-1's L1 booted without its
> identity (LR-4); doc-presence alone proved insufficient against completion bias in run-2.
> Everything below — the manifest, read-in-place, no-flatten — REMAINS TRUE for the shared
> protocol docs and all cross-refs: they are a reference library the agent reads and re-reads
> in place; only the identity trio is delivered in-context at boot.

Every manifest doc and every cross-ref inside it (`design/…`, `operational/…`) is a path **relative
to the harness root**, which the node reads in place. The mechanics:

- **Declared read-allow, write-jail.** The node can read its own subtree, parent/sibling direct
  surfaces, exact manifest/launch documents, and the visible resolved targets recorded in
  `.reference-map.json`; it cannot write outside its own subtree. Maintainer-only `hidden`
  reference entries are deliberately not grants.
- **No broad flatten and no broad harness grant.** A declared role cross-reference resolves through
  its visible reference-map target and is read in place. An undeclared `design/*.md` path remains
  closed. When the declaration is a symlink, both logical and real target paths are granted.
- **The chokepoint makes the root resolvable.** The spawn chokepoint (DAEMON §6) launches the actor
  with the harness root reachable from the node's working directory, so the relative paths in the
  manifest and its visible references land on exact files under the read-allow graph. The physical
  grant is finalized only after the identity/launch/reference surfaces are materialized.
- **The shared prompt path is itself read-allowed.** `operational/shared/system-prompt.md` must be
  readable at boot — the Claude Code process reads it to honor `--system-prompt-file`. SECURITY §1.4
  lists "the shared system prompt" in the read-allow set for this reason.

See `SECURITY.md` §1.4 for the authoritative read-allow / write-deny enumeration and §2.3 for the
seatbelt profile that enforces it. This doc does not duplicate the profile; it specifies what the
profile must leave readable for role resolution to work.

---

## 4. Wiring — how this rides the spawn chokepoint

This resolution rides the single spawn chokepoint; it does not add a code path. At spawn (DAEMON
§6.1 **STEP 2**) the adapter reads the level config and **assembles the runtime-neutral brief +
load-manifest** from the binding's `role_variant`. The Claude-Code adapter (§6.2) then composes a
per-spawn `.identity-prompt.md`: the shared prompt first, followed by the selected level identity
trio (`soul.md`, `role.md`, `config.md`) under provenance headers. It passes
`--system-prompt-file` = that composed identity bundle — **not** a bare per-level role doc and no
longer the bare shared prompt — then boots the pinned binary with the isolation env. The broader
protocol docs named in the manifest are still read in place by the agent (§3); only the identity trio
is flattened to remove the LR-4 diligence failure. The binding (DAEMON §3.2) splits the old single
field accordingly:

- `role_file: "L5/role.md"` (the old "`--system-prompt-file` passed at spawn") is **SPLIT** into:
  - **`system_prompt_file`** — records the concrete prompt file the runtime loaded. For Claude-Code
    seats this is the per-spawn composed `.identity-prompt.md`; its content begins with
    `operational/shared/system-prompt.md`, so the shared prompt remains the constant base.
  - **`role_variant: "L5#exec"`** (or `"L4"`, `"L5+#review"`, `"L3#plan"`, …) — per-binding; selects
    **which** load-manifest + per-level role docs the chokepoint assembles into the brief. This is
    the field that varies by seat.

Genesis (§7) boots L1 the same way; resume (§6.4) re-assembles the manifest into a delta brief. See
DAEMON for the chokepoint mechanics — this doc points at it, it is not duplicated here.

---

## 5. Invariants preserved (do not weaken)

This reframe changes *what* `--system-prompt-file` points at and splits one binding field. It changes
**nothing** below; all of these remain true after it:

- **OAuth-only.** Never `--bare` (reads auth strictly from `ANTHROPIC_API_KEY`, breaks an OAuth
  subscription token), never `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`. `--bare` stays forbidden.
- **Never `--append-system-prompt`** (keeps the SWE block) / **never `--agents`** (does not inject
  persona). The shared prompt *replaces* base block 2 via `--system-prompt-file`; nothing is appended.
- **Write-jail.** Writes confined to the node subtree + the per-session `CLAUDE_CODE_TMPDIR`,
  redirected tool-caches, and `CLAUDE_CONFIG_DIR`. Harness code, the ledger, secrets, and `~` are
  unreachable-for-write (SECURITY §1.3 / §2.3, Decision A).
- **Read-allow graph (F34).** Own subtree + siblings' published surface + parent + the
  role/shared/design docs + the shared system prompt. The role docs are now **READ-documents** read
  in place — not "the `--system-prompt-file` role doc." This is what makes refs resolve without
  flatten (§3).
- **Gate firewall on resume.** Never `--resume` a session across a quality-gate boundary
  (DAEMON §6.4, LOCKED) — unaffected by this reframe; left intact.

---

*Created: 2026-06-05. Sources: agent-definition-principles §4 (H40 resolved); DEFERRED-REGISTER Decisions A + B; DAEMON §6 + §3.2; SECURITY §1.4; system-prompt.md (promoted 2026-06-05).*
