# SECURITY — Containment Floor for Spawned Autonomous Agents

> **Status:** v1 design. Establishes the minimal-but-effective, configurable, mac-native containment floor for agents spawned by the harness. Upgrades WORKSPACE-SCHEMA.md line 74's "filesystem-level ACLs are an available hardening" from convention to a **stated enforcement mechanism**.
>
> **Scope of this doc:** filesystem **writes** + **secret reads**. NOT capability. Spawned agents keep web/search, the full default tool set, and the ability to run code / tests / install deps. Network egress is **deferred-with-trigger**, not built here.
>
> **Register provenance:** V1 / Decision A flagged SECURITY.md as homeless. This doc claims the seat and names the owning build increment for every control so none stays homeless.
>
> **Verified on:** the user's primary dev machine, macOS 26.4 (build 25E246), arm64. All "VERIFIED" claims below were run live on this box.
>
> **Pinned-binary path (single source of truth):** the seatbelt wrapper and the §2.3 read-allow both reference ONE canonical realpath. DAEMON §6.2 line 926 currently spells it `.cc-pinned/node_modules/@anthropic-ai/claude-code/bin/claude.exe` — the `.exe` suffix on a Mach-O arm64 binary is almost certainly a stale Windows-ism; the real launcher on this box is the platform package binary `.cc-pinned/node_modules/@anthropic-ai/claude-code-darwin-arm64/claude` (Mach-O arm64, v2.1.152). **OWED back-edit:** reconcile DAEMON §6.2 line 926 to the resolved realpath. This doc, the §2.3 profile, and §7 reference that single path and do not re-spell it. The realpath is load-bearing: §2.4 canonicalization means a wrong/symlinked binary path either fails the spawn or silently read-denies the 214 MB binary under the jail.
>
> **AMENDED 2026-07-24 — strong-form blinders are physical:** the production spawn now applies
> the same external seatbelt wrapper to Claude Code and Codex. The existing write/secret floor is
> active in both production modes. `observe` is the rollout default and reports broad local reads;
> `enforce` replaces that broad read with the F34-derived allowset. There is no production `off`.
> This closes a discovered implementation divergence: containment had been designed and exercised
> in tests, but the production `LevelConfig` assemblers never requested it.

## Decisions (LOCKED 2026-06-05 — user evaluation)

- **Mechanism = Option A:** `sandbox-exec` (seatbelt) write+read-jail as the v1 DEFAULT (sandbox-exec confirmed present on this box), with the `securityd` mach-deny as the keychain floor. **Helper-UID is a config TOGGLE** — off by default; the configurable hardening for higher-stakes work, or forced on if commissioning gate 1 shows the CC binary needs `securityd` to boot. The VM tier stays reserved (`escalate_to_vm`, not built in v1).
- **OAuth token:** relocated OUT of the writable `CLAUDE_CONFIG_DIR`.
- **Default read posture:** cross-project read-deny ON by default for L2–L5.
- **Empirical validation deferred to build:** the 7 §8.1 commissioning gates (does CC boot under the seatbelt? does the mach-deny break its auth? do real installs land? …) are checked **when the spawn-chokepoint increment is built** — the seatbelt is literally the pane's launch command. The design is locked now; if the CC binary surprises us at build, the profile is adjusted then.

---

## 0. The constraints this design is built to (LOCKED, do not re-litigate)

These were evaluated and locked by the user on 2026-06-05. The design serves them; it does not reopen them.

1. **Run environment:** the user's **primary dev machine TODAY** (their files, `~/.ssh`, keychain, other projects all present → real confinement needed now). Moving to a **dedicated machine soon** → the design MUST be configurable to **relax** there.
2. **Default + per-process configurable:** ship a sane **default** profile, tunable at the start of each build/process. A policy with knobs, not one fixed setting. Not too tight (must not cripple agents), not too lenient.
3. **The jail is about FILESYSTEM WRITES + SECRET READS, not capability.** Agents keep web/search, full tools, and the ability to run code/tests/install deps. Network egress is **deferred** (accepted risk on a trusted box) — recorded, not built.
4. **Permission posture:** skip-permissions **inside** the jail (spawned CC auto-approves its own tool calls; containment bounds the blast radius). NOT `allowedTools`-restricted in v1. Revisitable.
5. **HARD mac-native constraint:** the pinned CC is a mac-native Mach-O binary run interactively in tmux. **A Linux Docker/OCI container is OFF THE TABLE** — it would require a different, non-pinned Linux CC and break H40. Containment MUST be mac-native: restricted helper OS user, `sandbox-exec`/seatbelt profile, a macOS VM, or a combination. Evaluate THESE.

---

## 1. Threat model + trust boundary

The corpus until now lacked an adversarial framing. This section supplies it.

### 1.1 Trust boundary

**TRUSTED (the control plane):**

- The **user**.
- The **harness code** (`harnessd/` and its daemon, the single-writer executor).
- The **ledgers** (the node state, the lease/owner-token records).
- The **exact role-and-contract bundle declared for this seat** — the documents named by the
  spawn load manifest, the composed identity/launch surfaces, and visible resolved references.
  An undeclared harness or design document is not trusted merely because it is read-only.

**UNTRUSTED-BY-DEFAULT (the data plane):**

- An **agent's actions beyond its own node workspace** — sibling nodes, parent/cousin nodes, the L1 portfolio tree, `harnessd/`, the design docs, the user's `~` and other projects.
- **Anything an agent reads during a build** — scraped pages, dependency source, test fixtures, tool output. A build can ingest attacker-influenced bytes through search/web/installs, which are intentionally open.

The agent's **own node workspace** and **below** is the only region it is trusted to mutate.

### 1.2 Adversary

The adversary is **a spawned agent that has gone off-policy**, by any of three routes that are operationally indistinguishable from the daemon's point of view:

- **Buggy** — a loop, a bad path join, a `rm -rf` with a wrong base.
- **Prompt-injected** — content read during the build (a scraped page, a malicious README, a poisoned dependency) steers the agent to act against the user.
- **`cd`'d-away** — the agent legitimately changes working directory and then writes/read-globs relative to the wrong root.

The adversary has, by design (constraint 4), **auto-approved tool calls** and the **full tool set** including Bash, web, and dep-install. It can run arbitrary code and compiled binaries. We do **not** rely on the agent's judgment or on `allowedTools` to bound it. **The containment is the structural bound.**

### 1.3 What the jail must structurally prevent

A spawned agent (buggy / injected / cd'd-away) MUST NOT be able to:

- **Write** outside its own node workspace: sibling/parent/cousin nodes, the L1 portfolio tree, `harnessd/` and the ledgers, the design docs, the user's home or other projects.
- **Read** the user's secrets: `~/.ssh` key material, the keychain, the OAuth token file itself, the credential stores under `~/.config`/`~/` (gh, gcloud, docker, npm, kube, netrc, git-credentials, shell histories), `.env`/secret-pattern files in sibling and other projects, inherited secret env vars.

> **Two guarantees here are MEASURED, not asserted — and one was a hole the first draft shipped.** The default profile in §2.3 now delivers all of the above (verified holes from the escape-path review are closed: the keychain is closed by a mach-service deny **and** the helper-UID kernel wall, not by the file-read deny which the review proved irrelevant; the broad credential set is denied, not just four home dirs; and `.env`/secret-pattern files outside the workspace are denied by a glob). Two caveats are bounded explicitly rather than over-claimed: **(a)** the keychain file-read deny alone does NOT protect the keychain (the `security` CLI reads via the `securityd` MACH service, not file IO — see §2.3 / §3.3); **(b)** same-UID seatbelt does NOT hide other same-UID processes' argv/env from this agent (`ps eww`, leaked `--token=…` on another pane's command line) — that residual is closed only by the `HELPER-UID` knob (§2.5, §3.3 residual-exposure note).

### 1.4 What MUST stay allowed (or role resolution / work breaks)

- **Own subtree:** all files at and below the seat's node.
- **Neighborhood direct surfaces:** files directly in the parent node and directly in every known
  same-parent sibling node. This is one directory level only; neither parent nor sibling subtrees
  are reopened. Cousins remain closed.
- **Declared documents:** the exact load-manifest paths, composed identity and launch surfaces,
  spec/acceptance pointers, and the visible `references[].resolved_paths[].absolute_path` entries
  in the seat's `.reference-map.json`. Maintainer-only `hidden` reference entries stay closed.
  Declaring a symlink grants both its logical path and resolved target (§2.4).
- **Runtime essentials:** measured system/toolchain roots, the pinned runtime packages, daemon hook
  package, exact git internals, and the seat's exact runtime-state root. Claude's shared config
  re-closes `projects/`, `tasks/`, and the shared `session-env/`, then re-opens only
  `session-env/<adapter-minted-session-uuid>` so Claude's own Bash wrapper can maintain its runtime
  directory without seeing another seat's. Codex gets only the minted worker `CODEX_HOME`, never
  the canonical refresh-token home.
- **Runtime control conduit:** network remains generally unchanged, but enforce profiles explicitly
  retain the exact daemon Unix socket (canonical path plus any logical runtime-root alias). Both
  ordinary `harnessctl` verbs and the immediate turn-hook adoption trigger use this one conduit.
  The binding ledger remains read-denied; hook callbacks append only node-local raw events and the
  daemon joins canonical global facts.
- **Writes** to the agent's own node subtree and the per-session `CLAUDE_CODE_TMPDIR` (SCALE §6).
- **Writes** to `CLAUDE_CONFIG_DIR` and (pending the §8.1 boot smoke-test) `~/.claude` — **CC writes its own lock/state/history there during a long-lived session; the jail must allow these or it bricks the interactive boot before any agent work happens** (§2.3 write-allow; the token-file read-deny is layered on top so a writable config still can't read the credential). This was a hole the first draft's §2.3 profile left open and §8.1 already predicted.
- **Writes** to the **redirected per-tool dependency caches** (npm/pip/go/cargo/nuget/yarn), pointed INTO the workspace by env vars set at the chokepoint (§2.3 tool-cache redirection). **Without this, a JS/Go/Rust/.NET build hard-fails on its VERY FIRST `npm install` / `go mod` / `cargo` fetch** (VERIFIED: those write per-user caches at `~/.npm`, `~/go/pkg/mod`, `~/.cargo` outside the workspace and abort with `EPERM`/`Operation not permitted` under a workspace-only write-jail; pip is the lucky exception that degrades to cache-disabled). Constraint 3 requires install/build to work, so this is first-class, not an afterthought.
- **Network**: search/web/dep-installs (constraint 3; §5).

L1 receives the whole nested node tree read-only because its canonical address path is exactly
`L1`; caller-supplied role text cannot grant that exception. The future optimizer-L1 exception is
documented but not implemented. The write-deny applies unchanged: god-view is never write or kill
authority.

### 1.5 Threat-model correction — the "0700 home already protects secrets" premise is FALSE on this box

The cluster prompt notes "Unix 0700 home dirs already block cross-user secret reads." **On a default macOS box that premise is only partly true and must be enforced, not assumed:**

| Path | Typical macOS perms | Consequence |
|---|---|---|
| `$HOME` | `drwxr-x---` **(0750, group staff)** | A helper user **in group staff** could traverse and read any group-readable file. NOT 0700. |
| `$HOME/Documents` | `drwx------` **(0700)** | Blocks a different non-staff UID from traversing into the harness tree at all. |
| `~/.ssh` | `drwx------` **(0700)** | SSH keys protected from a different UID by ownership alone. ✅ |
| `~/Library/Keychains/*` | `0600` | Keychain DB protected from a different UID by ownership alone. ✅ |
| harness repo root | `drwxr-xr-x` **(0755, world-readable)** | A helper user could read `harnessd/` + `design/` + `.cc-pinned/config/.oauth_token` unless tightened. |

**Net:** `~/.ssh` and the keychain DB are the only secrets the cross-UID (helper-user) wall already covers by default — **by file ownership, which matters because the keychain's secret-access path is `securityd` (mach), not file IO** (§2.3): a same-UID seatbelt's file-read deny does NOT cover the keychain, so for the keychain specifically the helper-UID wall is structurally stronger. The 0700-home claim is a **prerequisite to enforce** (a helper-user fork must `chmod 700 ~`, tighten the 0755 repo root, and protect the token file), **not a property to assume**. This shapes the mechanism fork in §2: the recommended default is the same-UID seatbelt jail (it does not depend on any of these perms being right for the FILE-path secrets), **but the keychain forces either a `(deny mach-lookup …)` clause that may break CC's boot (§8.1 gate 1) or falling back to HELPER-UID as the keychain floor** — the one place same-UID seatbelt cannot reach.

---

## 2. The write-jail — mechanism (FORK — for user review) + the configurable profile

### 2.1 Mechanism evaluation (the three mac-native candidates)

**Candidate A — `sandbox-exec` / seatbelt `.sb` profile (RECOMMENDED DEFAULT).**

A per-spawn seatbelt profile, templated from the node's one-spine address, wrapping the CC invocation at the spawn chokepoint.

- ✅ **Confines the entire process tree with zero per-child work** (VERIFIED): a nested bash, a `python3` subprocess, **and a compiled Mach-O `/bin/cp`** were all blocked from writing outside the work tree. Seatbelt policy is inherited across fork/exec, so CC's Bash tool, its node children, installed deps, and spawned test processes are all confined by **one** wrapper. This is the decisive property for an interactive CC that shells out freely.
- ✅ **Per-node-precise write-root** for free — the allowed-write subpath is the node's physical `L{n}/` path, computed from `node_address` exactly as WORKSPACE-SCHEMA line 74's path-scoping already is.
- ✅ **Stateless** — no machine-state mutation, no user creation, no `chmod` surgery. Same UID, so the pinned binary, role docs, and OAuth-token env injection "just work."
- ✅ **Network stays open for free** — the profile touches only `file-read*`/`file-write*`, never `network*` (VERIFIED).
- ⚠️ **`sandbox-exec` is officially DEPRECATED** in its own man page (~since 2017) but ships and runs on 26.4, root-owned `0755`, and is the substrate Apple's own app sandbox and Codex CLI use today. "Deprecated" = no public support promise, not "removed." **Recorded as an accepted-with-trigger risk (§8), not a v1 blocker.**
- ⚠️ **Same-UID secret-read hiding hits the realpath gotcha** (§2.4) — must canonicalize paths or the deny silently leaks.

**Candidate B — restricted helper OS user.**

Spawn the agent as a separate, unprivileged UID; rely on the kernel's cross-UID ownership wall for secret reads.

- ✅ **Stronger, kernel-level cross-UID isolation** for the user's own `~/.ssh`, keychain, and other projects — needs no path canonicalization, no symlink fragility.
- ❌ **Blocked by the perms wall** (§1.5): `$HOME` is 0750 and `$HOME/Documents` is **0700**, so a different non-staff UID **cannot even traverse into the harness tree** to reach the 214 MB pinned binary or the role docs. Fixing this means **relocating the harness outside the home dir** or adding ACLs/group membership along the entire path — materially invasive.
- ❌ **Coarse** — one helper user serves all nodes, so every node can write every other node's workspace *as that user* unless combined with seatbelt or per-node POSIX ACLs. Per-node helper users give true isolation but add user-management overhead at spawn/teardown.
- ❌ **One-time privileged setup** — `sysadminctl`/`dscl` are root-only. Plus `chmod 700 ~`, tighten the 0755 repo root, protect the token file. A real prerequisite, not a per-spawn cost — but a setup cost the default should not require.

**Candidate C — macOS VM.**

A full VM as the isolation boundary.

- ✅ Strongest isolation; the machine boundary *is* the jail.
- ❌ **Overkill for v1** on the primary machine. Reserved as the **escalation tier** (§2.5, the `ESCALATE-TO-VM` knob) for builds that ingest untrusted external input where a seatbelt-policy bug is unacceptable. On the coming dedicated machine the VM is unnecessary — the machine itself is the boundary and the knobs relax instead.

### 2.2 FORK — for user review

> **FORK: write-jail mechanism. RECOMMENDATION: Candidate A (seatbelt `sandbox-exec`) as the v1 default, with the helper-user (Candidate B) carved as an *optional* belt-and-suspenders read-floor toggle, and the VM (Candidate C) reserved as the `ESCALATE-TO-VM` knob target — not built in v1.**
>
> **Why A over B for the default:** the live tests are decisive. Seatbelt confines the whole process tree (including compiled binaries and installed deps) with zero per-child work and **zero host-permission surgery**; the helper-user path is blocked by the verified 0750/0700 walls and would force relocating the harness or rewriting ACLs. Seatbelt keeps the same UID, so the pinned binary, role docs, and OAuth env injection need no cross-user readability setup, no `launchctl asuser` / `sudo -u` plumbing, no tmux-server-ownership juggling. The seatbelt reproduces the helper-user's home-secret read-wall via explicit `(deny file-read* …)` clauses, at far lower setup cost — **for FILE-path secrets.**
>
> **The real tradeoff to weigh (why this is a genuine fork, not a foregone conclusion) — and the escape-path review SHARPENED it:**
> - Seatbelt-only is **lightest** but carries (a) the **deprecation risk** (§8), (b) the **same-UID realpath fragility** for secret-read denies (§2.4) — a mis-canonicalized path silently leaks a secret, and (c) **two things the seatbelt structurally cannot do that the helper-UID does for free:** the **keychain** (the `security` CLI reads via the `securityd` MACH service, not file IO, so a file-read deny is useless — the seatbelt needs a `(deny mach-lookup …)` that MAY break CC's own boot, §8.1 gate 1; the helper-UID covers the keychain by file ownership unconditionally), and **same-UID argv/process-table visibility** (a sibling agent's `--token=…` is visible via `ps eww`; only a separate UID hides it).
> - Helper-user **eliminates the realpath fragility AND covers the keychain AND closes cross-process argv** (all by kernel ownership, no profile) but is **invasive to stand up** on this box and **coarse** (one UID for all nodes) unless layered with seatbelt anyway.
> - The honest middle is **A as default, B available as a hardening toggle** (the `HELPER-UID` knob, §2.5) when a build wants the kernel read-wall in addition to the seatbelt write-jail. On the dedicated machine, both can relax.
>
> **Decision owner:** the user. Recommendation stands at **A-default, B-optional-toggle, C-as-escalation-knob** — BUT note the keychain caveat: if §8.1 gate 1 shows the pinned CC needs `securityd` to authenticate at boot, the global mach-deny can't ship and **`HELPER-UID` becomes the correct keychain floor by default**, not the seatbelt. If the user prefers the kernel read-wall from day one anyway (it also closes the keychain + argv gaps cleanly), flip `HELPER-UID` on by default and pay the one-time setup (`chmod 700 ~`, relocate-or-ACL the harness, create the UID).

### 2.3 The production seatbelt profile — write/secret floor plus read blinders

The production profile has one invariant floor and two read-rollout modes. Both modes retain the
verified deny-all write jail, cache redirection, keychain/credential denies, final OAuth-token
read/write deny, and open network. Only the broad local-file read rule differs:

```scheme
(version 1)
(allow default)                          ; tools, network, system services remain available

;; OBSERVE: production default while traces are commissioned
(allow (with report) file-read*)

;; ENFORCE instead begins with:
(deny file-read*)
(allow file-read*
  (subpath "<OWN_SUBTREE>")
  (literal "<EXACT_DECLARED_DOCUMENT>")
  (subpath "<MEASURED_RUNTIME_ROOT>")
  (regex #"^<PARENT_OR_SIBLING_NODE>/[^/]+$"))

;; BOTH MODES then apply the existing floor
(deny file-write*)
(allow file-write* ...)
(deny mach-lookup ...)
(deny file-read* ...)                    ; credential stores and secret-pattern files
(allow file-read* (subpath "<OWN_SUBTREE>"))
(allow file-read* (subpath "<EXACT_RUNTIME_STATE_ROOT>"))
(deny file-read* ...)                    ; Claude config inner carve-outs, when applicable
(deny file-read* (literal "<CONFIG>/.oauth_token"))
(deny file-write* (literal "<CONFIG>/.oauth_token"))
```

`observe` therefore does **not** mean unjailed: it enforces the already-ratified write, keychain,
credential, token, and environment floor while reporting allowed broad reads to the macOS unified
log. `enforce` is opt-in until supervised Claude and Codex traces plus an explicit owner/director
ruling promote it. Profile application is preflighted before the actor opens. An apply failure
refuses an `enforce` spawn; `observe` may continue only with a durable degraded posture.

**Tool-cache redirection (set at the chokepoint BEFORE launch — part of the default, not an afterthought).** So per-user package caches land INSIDE the jail instead of hard-failing:

```
NPM_CONFIG_CACHE=$WORKROOT/.cache/npm     PIP_CACHE_DIR=$WORKROOT/.cache/pip
GOMODCACHE=$WORKROOT/.cache/go            GOCACHE=$WORKROOT/.cache/gobuild
CARGO_HOME=$WORKROOT/.cargo               YARN_CACHE_FOLDER=$WORKROOT/.cache/yarn
NUGET_PACKAGES=$WORKROOT/.nuget
```

VERIFIED: `npm install --cache $WORKROOT/.npmcache` and `PIP_CACHE_DIR=$WORKROOT/…` both succeed RC=0 under the identical jail; the unredirected `npm install`/`go mod`/`cargo` all hard-fail. **Trade-off:** per-node caches lose cross-node sharing (slower cold installs) — acceptable for v1; a shared read-only cache mount is a later optimization.

- **Keychain (mach):** the `(deny mach-lookup …)` is the real keychain control. **VERIFIED** the old file-read deny was bypassable: under it, `security dump-keychain` enumerated 54 login-keychain items and `find-internet-password`/`find-generic-password` ran exit 0. The mach-deny is **gated on §8.1 gate 1** — if the pinned CC needs `securityd` for its own OAuth/keychain auth at boot, the mach-deny can't ship globally and the **HELPER-UID kernel wall (which covers the keychain by file ownership) becomes the keychain floor**, not the seatbelt.
- **SSH read — pick per knob (named decision, not a silent break):** the default denies all of `~/.ssh` (option a, accepted v1 break of `git+ssh` dep fetches; use HTTPS deps). If SSH-based fetch is a real need, swap the `~/.ssh` subpath deny for the narrowed key-material deny (option b): `(deny file-read* (regex #"^<HOME>/\.ssh/(id_|.*_key|identity)"))`, leaving `known_hosts`/`config` readable.
- **Network:** intentionally **no `(deny network*)`** clause — search/web/installs keep working
  (constraint 3; §5).
- **Reads:** broad and reported in `observe`; deny-by-default and reopened only from the derived
  physical allowset in `enforce`. Read-only status alone never grants a path.
- **`<WORKROOT>`** = the node's own subtree only (`project-{name}/L3/{area}/L4/{workstream}/L5/{task}/`, or the coordinator's owned subtree). Siblings/parent/cousins/L1-tree/`harnessd/`/`design/`/`~` are all write-DENY by virtue of the global `(deny file-write*)`.

### 2.4 HARD implementation requirement — realpath canonicalization (the #1 silent-hole risk)

> **Seatbelt matches the RESOLVED REAL PATH, not the symlink path. VERIFIED both directions:**
> - A `(deny file-read* (subpath "/tmp/secretdir"))` rule **silently failed** — the secret leaked, exit 0 — because `/tmp` is a symlink to `/private/tmp`. The same rule against the realpath `/private/tmp/secretdir` correctly denied it (exit 1).
> - Symmetrically, an `(allow file-write*)` rule written with a `/tmp` logical path **silently over-denied** a legitimate workspace write.

**Consequence (build requirement):** deny/write paths are realpath-canonicalized. A readable
symlink declaration is different: seatbelt checks the logical lookup and resolved target, so the
allowset retains **both**. Exact declared files do not reopen their containing directories;
subpaths and direct-surface roots receive only the ancestor metadata needed for traversal.

### 2.5 Production configuration and durable posture

`LevelConfig.blinders_mode` is the production input. Genesis and child-spawn assemblers resolve it
from `HARNESS_BLINDERS_MODE`: absent means `observe`, `enforce` opts into the strong read wall, and
every other value (including `off`) fails before spawn. The static registry and
`LevelConfig.for_level()` remain neutral only as a structural test seam; they are not production
launch assemblers.

Every binding records a secret-free `containment_posture`: version, mode, rendered-profile SHA-256,
the L1 exception bit, exact literal/subpath/direct-surface/runtime-state grants, runtime inner
denies and exact inner allows, daemon control-socket paths, and any degraded reason.
`permission_posture` separately records the runtime permission flags. Seatbelt's `(with report)`
events remain in the macOS unified log; per-read ledger ingestion is not part of this pass.

Helper-UID, VM escalation, network-deny, and a dedicated-host relax profile remain future controls.
They are not accepted `LevelConfig` inputs and are not claimed as wired.

### 2.6 Per-spawn derivation

The chokepoint finalizes the launch surface first, then derives the physical policy:

1. Resolve the node's canonical address and own workspace; only canonical `L1` receives god-view.
2. Read current bindings to derive the direct parent and known same-parent sibling node surfaces.
3. Add exact paths from the load manifest, identity/launch surfaces, spec and frozen acceptance,
   plus visible reference-map targets. Hidden reference entries are ignored.
4. Add measured system/toolchain roots, runtime packages, hook code, exact git internals, node
   temp/config, and the isolated git environment:
   `GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_NOSYSTEM=1`,
   `XDG_CONFIG_HOME=<WORKROOT>/.config`.
5. Finalize the exact runtime-state root. Claude re-denies its cross-seat state directories and,
   after the adapter mints the session UUID, re-opens only that exact `session-env/<uuid>` path.
   Codex finalizes only after minting the worker home.
6. Render, preflight, persist, wrap, and hash the profile through one runtime-neutral seam.

Direct parent/sibling access is an anchored one-level regex, so files published after profile
render remain visible without granting the subtree.

### 2.7 Uniform Claude Code and Codex application

Both runtime adapters apply the same external `sandbox-exec` profile around the unchanged inner
pane command. Native runtime protections are retained where present, but they do not create a
second policy or proof track. Codex finalizes the wrapper against the minted per-seat
`CODEX_HOME`; the canonical refresh-token home never enters its allowset. The retired
`containment_unsupported` refusal is not a valid production outcome.

---

## 3. Secret protection

### 3.1 The OAuth token — where it lives, how it reaches the agent

- **How it reaches the agent (clean path):** env-injected. The daemon's isolation env (DAEMON §6.2 lines 936-937) reads the token via a **token-file / `_FILE_DESCRIPTOR`** (`$(cat …)`), exporting it as `CLAUDE_CODE_OAUTH_TOKEN`, so **the literal credential never lands in the pane or the transcript**. The launcher comment is explicit: "export `CLAUDE_CODE_OAUTH_TOKEN` in the environment before calling this (don't bake it in)."
- **The on-disk tension (load-bearing, VERIFIED):** a `0600` copy of the token **exists on disk** at `.cc-pinned/config/.oauth_token`, and that file sits **inside** `CLAUDE_CONFIG_DIR` — the very directory the agent must read for clean-config boot. So "the token lives outside the jail's read scope" is **NOT true as laid out today**: the env-injected path is clean, but the on-disk file is reachable if the read scope includes the whole config dir.

### 3.2 Resolution (pick one; both are in-scope for the secret-protection fill)

> **Interaction with the now-WRITABLE config dir (§2.3):** because the §2.3 profile must make `CLAUDE_CONFIG_DIR` **writable** (CC writes its own lock/state there or the boot bricks), a token left inside a writable config dir could be read OR rewritten by the agent. So **option (a) relocate is now the preferred default** — moving the token OUT of the writable config dir is the clean separation. If (b) is used, the single-file read-deny must hold even though the surrounding dir is writable (the agent can write config but the literal token path stays read-denied AND ideally write-denied — add `(deny file-write* (literal "<CONFIG>/.oauth_token"))` if the file remains in place).

- **(a) Relocate** `.oauth_token` **OUT of `CLAUDE_CONFIG_DIR`** to a path above the jail's read+write root (cleanest — the agent reads/writes `config/` but the token isn't in it), **or**
- **(b) Single-file read-deny** `(deny file-read* (literal "<CONFIG>/.oauth_token"))` (+ the write-deny above) while allowing the rest of `config/` — seatbelt subpath/literal deny supports this (VERIFIED the deny-specific-file-while-allowing-siblings pattern works), and so does a helper-UID ACL. This is the clause already shown in §2.3.

> **Open question gating (b) → must be verified in H40 before relying on disk-deny:** does the pinned CC consume the token from the **env var alone** at spawn, or does it **re-read `config/.oauth_token` from disk** during the session? If it re-reads from disk, an env-only injection won't suffice and the file cannot simply be removed — a read-deny would break auth. **Empirical check belongs in the H40 vanilla-boot test.** Until then, prefer **(a) relocate** as the safer default (the binary's documented path is the env var).

### 3.3 The scrubbed spawn env — no inherited secret env vars

> **Correction (wiring honesty):** DAEMON §6.2 lines 935-938 specify a **named isolation-env set** (`CLAUDE_CONFIG_DIR`, `CLAUDE_CODE_OAUTH_TOKEN` via token-file, two disable flags) — it does **NOT** contain an `env -i` clean-slate clear. A grep of DAEMON for `env -i` returns zero hits. So the clean-slate scrub this section relies on is a **SECURITY-OWNED ADDITION**, not a pre-existing seat being merely wired. This matters: if the chokepoint sets only the named vars ON TOP OF the daemon's inherited environment (which on the primary box carries the user's shell secrets), inherited API keys DO leak into the agent. The "no inherited secret env vars" guarantee rests on `env -i` being actively added.

- **SECURITY UPGRADES DAEMON §6.2's named isolation-env set to an `env -i` clean-slate rebuild + named allowlist.** **VERIFIED:** under `env -i` a `FAKE_SECRET` came back empty, so the rebuilt env carries **only** the needed vars (`CLAUDE_CONFIG_DIR`, `CLAUDE_CODE_OAUTH_TOKEN`, `CLAUDE_CODE_TMPDIR`, the cache-redirection vars from §2.3, the disable-autoupdate/nonessential-traffic flags) — no inherited API keys, other-project secrets, or stray credentials. **OWED back-edit:** add `env -i` explicitly to DAEMON §6.2's isolation-env line, or the chokepoint owner will implement the named-set-only version and the scrub silently won't happen. Owning increment: the **spawn-chokepoint adapter** (same as the token read-deny).
- **`CLAUDE_CODE_SUBPROCESS_ENV_SCRUB`** appears in the binary strings (PINNED-CC.md) but its behavior is **NOT verified** — name it and set it so the token var is **not re-inherited by CC's own child processes** (Bash-run tools, dep installers) beyond the binary itself. **Commissioning gate owed (§8.1 gate 5):** empirically confirm it removes `CLAUDE_CODE_OAUTH_TOKEN` from a child `Bash` process (have the agent run `env | grep OAUTH` and assert empty). **If the flag is a no-op or misnamed on 2.1.152, every `Bash(curl …)` inherits the token and can exfiltrate it over the open network (§5)** — in that case the token-in-child-env path is an **accepted risk listed in §8 with its own trigger**, not silently assumed closed.

**Net secret-protection story:** token is **env-injected, not readable-from-disk-by-the-agent** (relocate or single-file deny); the inheritable env is **scrubbed clean** (`env -i` clean-slate, SECURITY-owned; subprocess-scrub flag pending §8.1 gate 5); role docs + read-only harness docs **stay readable** (verified compatible with deny-specific-secrets). `~/.ssh` key material covered by the §2.3 read-deny; **the keychain is covered by the §2.3 mach-service deny (NOT the file-read deny, which the escape-path review proved irrelevant) and, when `HELPER-UID` is on, by kernel file-ownership.**

> **Residual same-UID exposure (accepted-with-trigger, §8):** a same-UID seatbelt does NOT isolate the process-table/argv view. `env -i` scrubs the CHILD's OWN env but gives zero protection against this agent READING another same-UID process's secret-bearing argv (e.g. a tool invoked `--token=…` on a sibling agent's pane, visible via `ps eww`) or the user's other processes. A live `ps eww` scan returned no full-env hits this run (macOS gates full-env visibility cross-process), but secret-bearing **argv** remains same-UID-visible. This is exactly the gap the **`HELPER-UID` (per-node UID) knob** closes and the same-UID seatbelt structurally cannot — framed as accepted-with-trigger, not implicitly covered by `env -i`.

> **DEFER-with-trigger — full env-scrub beyond the jail:** scrubbing *every* potentially-sensitive var from the whole process environment beyond the token + the named set is **deferred**. Trigger to revisit: a measured leak of a non-OAuth secret into an agent process, or the move to handling untrusted external input. v1 ships `env -i` + the OAuth-only injection + the subprocess-scrub flag; it does not attempt an exhaustive env allowlist.

---

## 4. Permission posture — a named decision checked at spawn

### 4.1 The posture

- **Auth:** OAuth-token (subscription) via the env injection above.
- **System prompt:** `--system-prompt-file <node-workspace>/.identity-prompt.md` for Claude-Code seats — a per-spawn composed identity bundle whose first section is the ONE shared minimal prompt, followed by the selected `soul.md` / `role.md` / `config.md` identity trio (NOT a bare per-level file, NOT `role.md` alone; LR-4 amended H40 Decision B). This replaces base block 2, keeps the 57-char identity line, and keeps the **default 24-tool SWE set** incl. search/web/Bash/Edit/Write. **VERIFIED interactive + OAuth-compatible by H40; identity-autoload verified after LR-4.**
- **Role:** the identity trio is auto-loaded in the composed prompt; broader role/protocol context remains **documents the agent READS at boot** (per-level extras + the always-loaded shared contract docs), pointed at by the node-local spawn brief's load-manifest. The per-binding `role_variant` (DAEMON §3.2) selects WHICH identity/load-manifest bundle the chokepoint assembles.
- **Permissions:** **skip-permissions inside the jail** — the permission-bypass mode flag (the `--dangerously-skip-permissions` / `bypassPermissions` mode) so the unattended session auto-approves its own tool calls.
- **Tools:** **full default set incl. search**. NOT `allowedTools`-restricted in v1.
- **Containment, not capability, bounds the blast radius.** Auto-approval is safe **only because the seatbelt jail is the structural bound.** If the jail is ever disabled (the relax knob on the dedicated box), the auto-approve posture must be **re-evaluated together with it**.

### 4.2 `--bare` is REJECTED (do not "simplify" toward it)

H40 explicitly ruled out `--bare` (H40-FINDINGS §3/TL;DR rank 4): it strips to a 3-tool set `[Bash, Edit, Read]` **and** reads auth strictly from `ANTHROPIC_API_KEY`, **never OAuth/keychain**. It breaks both the OAuth-only subscription-token spawn and the full-tools-incl-search requirement. **Skip-permissions must be implemented WITHOUT `--bare`.** Recorded so a future maintainer doesn't regress toward it.

### 4.3 Posture is asserted + journaled at spawn (mirror the OAuth/model checks)

The corpus already trace-checks spawn-time invariants (TRANSPORTS §5.2: every child's `model_used == configured` else an L1 spawn-failure escalation must exist; the E32 model-pin check in DAEMON §6.3). SECURITY.md mirrors this: at the DAEMON §6.1/§6.2 chokepoint, **assert and journal a named "containment posture" record** alongside the OAuth-only assertion:

```
containment_posture := {
  version              : blinders-v1,
  mode                 : observe | enforce,
  profile_sha256       : <hash of the exact rendered .sb>,
  l1_god_view          : true | false,
  allow_literals       : [<exact declared/runtime files>],
  allow_subpaths       : [<own + measured runtime roots>],
  direct_surfaces      : [<parent/sibling node roots>],
  runtime_state_roots  : [<exact Claude config or Codex worker home>],
  runtime_inner_denies : [<Claude cross-seat state roots>],
  degraded             : true | false,
  degraded_reason      : <present only when degraded>
}
```

This is the binding's exact implemented, secret-free read-policy record. The sibling
`permission_posture` field records runtime permission behavior. Seatbelt's path events remain in
the OS unified log rather than being duplicated into the ledger.

> **Open item (spawn-chokepoint owner):** the **exact** permission-bypass flag spelling for the pinned 2.1.152 binary was not re-verified in H40 (H40 confirmed `--system-prompt-file`, `--append`, `--bare`, `--agents`). Confirm the precise flag / `settings.json` permission-mode value against `--help` and confirm it **composes with `--system-prompt-file` + OAuth**, the same way H40 confirmed the others.

---

## 5. Network / search posture — OPEN in v1, egress-control DEFERRED-with-trigger

- **v1 posture: OPEN.** The write-jail + secret-deny profile leaves **network egress fully open** — VERIFIED: with no `(deny network*)` clause, search/web/dep-installs all keep working under the jail with zero extra config. This is required by constraint 3 (agents keep web/search/installs).
- **DEFERRED-with-trigger.** Egress-control is a **single profile delta, no architecture change**: add `(deny network*)` + an allowlist of `(allow network-outbound (remote ip "…"))` entries. It is **not built in v1**.
- **Re-arm triggers (named):**
  - A build that **ingests untrusted external input** (scraped / attacker-controlled data) — pair with `ESCALATE-TO-VM` or the TIGHTEN read-deny knob.
  - Any future run on a **shared / hostile network**.
  - **NOTE:** moving to the dedicated machine is **NOT** a trigger to add egress control — that move **relaxes** (constraint 1). The dedicated box is *more* trusted, not less.
- **Accepted-risk record (honest about the rollout):** with open egress and full Bash/installs, a
  prompt-injected agent can exfiltrate anything it can read. In production-default `observe`, broad
  local reads remain open and reported, so the commissioning exposure is materially wider than
  the eventual F34 surface even though the write/secret floor is active. `enforce` shrinks the
  readable world to own subtree, direct neighborhood surfaces, exact declared docs, and measured
  runtime essentials. Neither mode prevents exfiltration of legitimately readable content. The
  default changes only after supervised Claude+Codex traces and an explicit owner/director ruling.

---

## 6. Fleet HALT — the human kill-switch (independent of L1 and the watchdog)

A single human-operated freeze/kill of the whole fleet, **independent of L1's judgment and of the watchdog's evidence gates.** It is the "third human-initiated authority" — it rides cluster ③'s human-control authority directly and needs **no evidence**; it just sets the flag the readers obey. **It is a thin operation over EXISTING seats — no new mechanism.**

### 6.1 Level 1 — FREEZE (the recommended panic button, reversible, non-destructive)

**HALT = set `paused_at` at the ROOT one-spine prefix, via the single-writer executor** (TRANSPORTS §5.3 pause-subtree primitive; the flag is DAEMON §3.2's `paused_at`).

Because the root is an ancestor of every node, the prefix check denies the whole fleet at **two already-seated enforcing readers**:

- **DAEMON §6.1 STEP 0** (lines 877-881): the chokepoint refuses to launch any child whose node-or-ancestor has `paused_at != null`. → **instantly freezes all new spawns** (and `--resume`/necro, which share the chokepoint, line 996).
- **WATCHDOG §3.4 step 0:** the recovery loop **skips prod/respawn/FAILED** for a paused node. → the watchdog stops "helpfully" respawning anything.

This is **independent of the watchdog's evidence/grace gates by design**, because the pause-gate is checked **FIRST** and short-circuits them. And it's a **non-destructive freeze** — WATCHDOG §3.4 step 0 + TRANSPORTS §5.3 make `paused_at` a "held quiet, intentional, not a stall" state; the watchdog must **not** mark a paused idle node FAILED. Freeze the whole fleet instantly and reversibly, then decide — without killing in-flight work or corrupting the ledger.

> **CRITICAL SCOPE — FREEZE is a spawn/respawn QUIESCING control, NOT an in-flight stop.** `paused_at` is obeyed by exactly two readers: the spawn chokepoint (refuses NEW spawns / `--resume` / necro) and the watchdog (skips respawn). **An agent ALREADY running in its tmux pane keeps executing tool calls, keeps writing to its workspace, and — with open egress (§5) — keeps exfiltrating; the freeze does nothing to it.** So FREEZE alone does NOT bound the blast radius of an actively-misbehaving RUNNING agent: it stops the fleet from GROWING and from self-healing, then you decide. To halt live action you must escalate to Level 2 (§6.2).
>
> **In-flight pause (the live-agent counterpart, if a non-destructive stop of a RUNNING agent is wanted):** `SIGSTOP` the pane's process group via the executor (reversible with `SIGCONT`) — this freezes a live agent's execution without killing it or desyncing the ledger, as the non-destructive complement to `paused_at`. Like the kill, it routes through the executor, never raw tmux. **v1:** `SIGSTOP`-live-pause is named here as the correct mechanism but is **gated to the same TRANSPORTS §5.3 control-surface increment** as the freeze; until built, the only in-flight stop is the destructive Level-2 CAS kill.

### 6.2 Level 2 — KILL (destructive override)

If freeze is insufficient: **CAS-guarded executor `transition` fan-out**, leaf-first, presenting `expected_owner_token` + `expected_generation` (DAEMON §4.1). This is the **same CAS that is the double-kill interlock** (TRANSPORTS §7.2 / WATCHDOG §5.4) — a second killer's stale token aborts. Only **L1 may force-reap at depth** (the emergency override, destructive, requires explicit confirmation, agent-lifecycle §135); the god-view layer is read-only and **cannot kill**.

### 6.3 The invariant

**A fleet HALT NEVER touches tmux directly.** Both freeze and kill route through the **single-writer executor**, so the ledger stays consistent. A raw `tmux kill` desyncs the ledger and is **explicitly forbidden**. The freeze is one executor op (instant, no per-node race); the kill is a CAS-fenced fan-out through the one serialization domain.

> The HALT is specified here as a **data-layer executor op addressable by root prefix**. The button rendering / GUI is deferred to the later GUI pass (TRANSPORTS §5.1/§5.3 specify data, not pixels).

---

## 7. Wiring + build-increment ownership

Each control names exactly where it attaches and which build increment owns it, so none stays homeless (the V1/Decision-A complaint that flagged this doc).

| # | Control | Attaches at | Owning build increment | Wiring note |
|---|---|---|---|---|
| **1** | Threat model + trust boundary | (design) | This doc | The adversarial framing the corpus lacked; consumed by every control below. |
| **2** | **Write jail + local-read blinders** | **DAEMON §6.2 in-role boot — the detached tmux PANE LAUNCH COMMAND.** The seatbelt prefix is part of the pane's launch command-line at session-create: `sandbox-exec -f <profile>.sb <env-i + binary + flags>` **IS** the detached pane's command (§7.1). Applied **after** the §6.1 CAS claim and launch-surface materialization. | The **spawn-chokepoint adapter** increment + 2026-07-24 blinders pass | Policy derives from canonical address, current bindings, exact launch/manifest/reference surfaces, and runtime essentials. Production always selects observe/enforce; the same jail is re-applied on resume/necro. |
| **2b** | Sibling/parent/cousin deny-reads + secret set derivation | Same chokepoint | **Same as visibility-graph derivation** (WORKSPACE-SCHEMA path-scoping increment) | Pure function of the one-spine address; no separate permissions file. |
| **2c** | **Codex external blinders wrapper** | DAEMON §6.3 Codex adapter | The **strong-form blinders pass** | Same external profile/proof seam as Claude; native protections remain, exact minted worker home only. |
| **3** | **Secret protection** (token relocate-or-single-file-deny + `env -i` clean-slate + keychain mach-deny + `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB`) | DAEMON §6.2 isolation env | The **spawn-chokepoint adapter** increment | **NOT merely wiring an existing seat:** DAEMON §6.2 specifies a named isolation-env set with NO `env -i` (grep=0) — SECURITY UPGRADES it to an `env -i` clean-slate rebuild (SECURITY-owned addition; OWED back-edit to DAEMON §6.2, §3.3). Adds the token read-deny/relocate and the keychain **mach-service** deny (file-read deny proven irrelevant, §2.3). Disk-deny gated on the §8.1 token re-read check; subprocess-scrub gated on §8.1 gate 5. |
| **4** | **Permission posture** (skip-perms-in-jail, full tools, journaled record) | DAEMON §6.1/§6.2; asserted like the E32 model-pin check in §6.3 | The **spawn-chokepoint adapter** increment | Named, journaled `containment_posture` record (§4.3); mirrors TRANSPORTS §5.2 `model_used` trace-checker. `--bare` rejected. |
| **5** | **Network/search posture** (OPEN v1; egress-control deferred) | The seatbelt profile (omission of `(deny network*)`) | **No build** in v1 — deferred. Re-arm = profile delta in the chokepoint increment when triggered | Accepted-risk record (§5); triggers = untrusted external input / hostile network. Dedicated-machine move is NOT a trigger. |
| **6** | **Fleet HALT** (root `paused_at` freeze + CAS kill + named `SIGSTOP` live-pause) | **DAEMON §6.1 STEP 0** (paused prefix-check) + the **single-writer kill path**; verb from **TRANSPORTS §5.3** human-control; third authority per **WATCHDOG §3.4 step 0** | The **TRANSPORTS §5.3 control-surface** increment | Root-prefix freeze + CAS human-kill fan-out (mostly existing seats). **FREEZE is spawn/respawn quiescing only — it does NOT stop a RUNNING agent** (§6.1); live stop = Level-2 CAS kill, or the named `SIGSTOP`-via-executor in-flight pause (same increment, gated). GUI deferred to the GUI pass. |
| **7** | **Per-session resource envelope** (RAM/FD rlimits) | Same spawn wrapper as the seatbelt (co-located, orthogonal control) | The **SCALE §6 commissioning-run** increment | `ulimit`/`setrlimit` in the spawn wrapper; whitelist the per-session `CLAUDE_CODE_TMPDIR` as the 2nd write-root so CC scratch works. **Mechanism stated; values come from the first pressure-up run** (unmeasured today). |

### 7.1 tmux-server-ownership wiring note (the concrete seam)

`sandbox-exec` wraps the **CC command inside the pane**, NOT the shared tmux server. The concrete seam: the seatbelt prefix is part of the **pane's launch command-line at session-create** — `tmux new-session -d -s harness:<addr> 'sandbox-exec -f <profile>.sb env -i <allowlist> <binary> <flags>'`. The whole `sandbox-exec … env -i … claude …` string IS the detached pane's command. It is NOT applied via `send-keys` after attach (which would leave a pre-jail window and risk a send-keys race). So one global tmux server still manages all panes while **each agent process is independently jailed** from the instant it launches, and `env -i` isolates that pane's env. The realpath-canonicalized binary path (header / §2.3) is the one inside this command.

---

## 8. Deferred items + accepted-with-trigger risks (the discipline ledger)

Minimal-but-effective, NOT gold-plated. What's deferred, and the trigger to revisit:

| Item | Status | Trigger to revisit | Owner |
|---|---|---|---|
| **Network egress control** | DEFERRED (accepted risk on trusted box) | Untrusted external input / shared-hostile network. NOT the dedicated-machine move. | User |
| **Full env-scrub beyond the jail** | DEFERRED | A measured non-OAuth secret leak into an agent, or untrusted-input handling | Spawn-chokepoint owner |
| **`sandbox-exec` deprecation** | ACCEPTED-WITH-TRIGGER (functional on 26.4; Apple/Codex depend on it) | A macOS bump that breaks the `.sb` grammar or removes the binary → escalate to the VM tier (Candidate C) or App-Sandbox entitlements | Spawn-chokepoint owner |
| **macOS VM tier** | NOT BUILT (reserved as `ESCALATE-TO-VM` knob target) | Untrusted external input on the primary machine; or `sandbox-exec` removal | User |
| **Helper-UID read-floor** | OPTIONAL TOGGLE (off by default) | A build wants the kernel cross-UID wall in addition to seatbelt; OR §8.1 gate 1 shows CC needs `securityd` (then HELPER-UID becomes the keychain floor) | User |
| **Strong-form F34 local-read enforcement** | BUILT; production defaults to `observe`, `enforce` is opt-in | Promote the default only after supervised Claude+Codex traces and explicit owner/director ruling | Owner / director |
| **Token in CC child-process env** | ACCEPTED-WITH-TRIGGER **only if §8.1 gate 5 shows `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB` is a no-op** | Gate 5 fails (token survives into `Bash` children) → token exfiltratable over §5 open egress | Spawn-chokepoint owner |
| **Cross-process argv/env visibility (same-UID)** | ACCEPTED-WITH-TRIGGER (seatbelt is same-UID) | Closed only by `HELPER-UID`; revisit when secret-bearing argv on a sibling pane is a real exposure | User |
| **Keychain via `securityd` mach path** | CLOSED by mach-deny OR HELPER-UID (NOT the file-read deny) | §8.1 gate 1 forces the choice; if CC needs securityd, HELPER-UID is the floor | Spawn-chokepoint owner |
| **Per-session RAM/FD ceiling VALUES** | MECHANISM stated, values unmeasured | First SCALE §6 commissioning pressure-up run | SCALE owner |

### 8.1 Commissioning checks owed before relying on the jail (the H40-adjacent gates)

The seatbelt mechanism is verified for bash/python/cp/network in isolation, but the **214 MB interactive CC binary was not yet booted under the profile.** Before v1 relies on the jail, run (analogous to WATCHDOG §8's empirical commissioning gates):

1. **CC-boots-under-seatbelt:** does the pinned CC run a long-lived tmux session under `sandbox-exec` with the §2.3 profile? The profile already adds the `(subpath "<CONFIG>")` + `(subpath "<HOME>/.claude")` write-allows this gate predicts CC needs — **confirm they suffice** and that the `(deny mach-lookup com.apple.SecurityServer/securityd)` keychain clause does NOT break CC's own boot. **Decision fork inside this gate:** if CC needs `securityd` to authenticate at boot, the global mach-deny CANNOT ship — fall back to `HELPER-UID` as the keychain floor (kernel file-ownership covers the keychain DB) and DROP the mach-deny from the seatbelt. Also confirm no other `(allow mach-lookup …)` is needed (updater self-check etc.).
2. **Inherited-confinement check:** run a real build and confirm a child `Bash(npm install)` is write-jailed too (the inheritance property, verified for cp/python/bash, re-confirmed for CC's actual subprocess spawning).
3. **Real dep-install lands under the jail (the "not too tight" gate):** with the §2.3 cache-redirection env set, confirm a real `npm install`, `pip install`, `go mod download`, and `cargo fetch` all SUCCEED (RC=0) inside the jail — they hard-fail without redirection. This is the bar constraint 2 sets ("must not cripple agents").
4. **Token-consumption check (§3.2):** does CC read the token from the env var alone, or re-read `config/.oauth_token` from disk? Decides relocate-vs-deny.
5. **Subprocess env-scrub check (§3.3):** have the jailed agent run `env | grep OAUTH` inside a `Bash` tool call and assert empty — i.e. confirm `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB` actually removes `CLAUDE_CODE_OAUTH_TOKEN` from CC's child processes on 2.1.152. **If it does NOT,** the token-in-child-env exfil path is OPEN under §5 egress → record as an accepted risk in §8 with its own trigger (do not assume closed).
6. **Keychain mach-deny holds (§2.3):** under the shipped profile, confirm `security dump-keychain` / `find-internet-password` are BLOCKED (the file-read deny alone left them exit-0; the mach-deny or the HELPER-UID wall must close them).
7. **Helper-UID + OAuth check** (only if that knob is turned on): confirm the pinned CC + env-injected OAuth works as a different UID with a writable `CLAUDE_CONFIG_DIR` and readable role/harness docs.

**Write-jail regression checklist (these VERIFIED-HELD on the box — a future profile edit must not silently re-open them).** The write jail is genuinely strong; lock it with a regression assertion so e.g. adding a broad `(allow mach-lookup)` for keychain doesn't accidentally re-enable launchd job submission:

- nested `sandbox-exec` with a permissive profile → blocked (`sandbox_apply: Operation not permitted`)
- `osascript do shell script` → inherits the sandbox, stays jailed
- LaunchAgent plist write → blocked; `launchctl submit` → no effect
- `crontab` install → blocked
- symlink-out-of-workspace write → blocked (seatbelt resolves the write target's realpath, §2.4)
- logical `/tmp/X` deny LEAKS while `/private/tmp/X` realpath deny BLOCKS → the §2.4 canonicalize-everything rule is load-bearing

---

## 9. Summary

- **Threat model:** trusted = user + harness + ledgers + role/design docs; untrusted = an agent's actions beyond its own node + anything it reads. Adversary = a buggy / prompt-injected / cd'd-away agent with auto-approved full tools. Containment, not judgment, is the bound. **The "0700-home already protects secrets" premise is FALSE on this box (home is 0750, repo root 0755) — a prerequisite to enforce, not assume.**
- **Write jail + blinders:** every production Claude/Codex pane is externally wrapped. `observe`
  reports broad reads while enforcing the existing write/secret/token floor; `enforce` reopens only
  the address/manifest/reference/runtime-derived world. The production input is
  `HARNESS_BLINDERS_MODE=observe|enforce`; there is no `off`.
- **Secrets:** token env-injected (never on the pane/transcript); the on-disk token is
  single-file-denied read and write, `env -i` is the clean-slate floor, and keychain access is
  blocked by the mach-service deny. Arbitrary role/design reads are not open: exact declared paths
  are grants.
- **Permission posture:** OAuth + `--system-prompt-file <node-workspace>/.identity-prompt.md` (shared prompt base + selected identity trio; broader protocol docs are read via the spawn brief's load-manifest) + skip-permissions + full tools incl. search, journaled as a named `containment_posture` record. `--bare` rejected.
- **Network:** OPEN in v1; egress-control deferred-with-trigger (one profile delta). Exfil surface narrowed by the cross-project read-jail to the agent's own readable set.
- **Fleet HALT:** root `paused_at` freeze (reversible, two existing readers obey) + CAS human-kill fan-out, all through the single-writer — never raw tmux. Independent of L1 and the watchdog. **FREEZE is spawn/respawn quiescing only; a RUNNING agent needs the Level-2 kill or the named `SIGSTOP` live-pause.**

**Forks for user review:**
1. **Write-jail mechanism (§2.2)** — RECOMMEND seatbelt-as-default, helper-UID-as-optional-toggle, VM-as-escalation-knob. Real tradeoff sharpened by the escape-path review: seatbelt's deprecation + same-UID realpath fragility + **its structural inability to cover the keychain (mach path) and same-UID argv** vs helper-user's invasive-but-kernel-strong read-wall that covers all three. User decides whether `HELPER-UID` is on from day one; **§8.1 gate 1 may force it as the keychain floor regardless** (if CC needs `securityd` to boot, the global mach-deny can't ship).
2. **OAuth token on disk (§3.2)** — relocate the file out of `CLAUDE_CONFIG_DIR` (safer default) **vs** single-file read-deny. Gated on the §8.1 check of whether CC re-reads the token from disk.

**What wires where (the one-liner):** every control attaches at the **DAEMON §6.1/§6.2 spawn chokepoint** (write-jail = the §6.2 detached-pane launch command `sandbox-exec -f … env -i … <binary>`; secret-scrub + posture-assertion ride the isolation env — `env -i` itself is a SECURITY-owned upgrade to DAEMON §6.2, not an inherited seat) **except** the fleet-HALT, which rides **TRANSPORTS §5.3** (root `paused_at` + CAS kill + named `SIGSTOP`) read by **DAEMON §6.1 STEP 0** and **WATCHDOG §3.4 step 0**, and the **resource envelope**, which co-locates in the spawn wrapper but is owned by **SCALE §6**. The write-jail + read-jail are the concrete upgrade of **WORKSPACE-SCHEMA lines 63-74** (read table + line-74 hardening).
