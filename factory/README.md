# The Factory — L1–L5 build hierarchy

This directory is the build hierarchy inside **Software Factory**; `../research-system/` is the research system that studies and improves it.

A hierarchy of LLM agents that turns a user's intent into built, verified software — and keeps what gets built faithful to what was meant, all the way down to the code.

Models already write good code, so the bottleneck in building software with them has shifted from execution to coordination and fidelity: directing many efforts in parallel, and keeping what gets built faithful to what was meant. One person can run one project by hand; they cannot direct, keep coherent, and quality-check twenty at once, nor guarantee that a long autonomous build still matches what they asked for. This system enforces a separation of concerns across five levels of abstraction, each doing a genuinely different kind of thinking, so the person sets and guards intent while the hierarchy carries the work. Three things come out of that:

**1 — Higher-quality architecture and code.** It architects the way a senior architect actually does, not ad-hoc. There's a real method underneath: carve a system by where its connections are thin — where change is naturally isolated — not by drawing arbitrary boxes; keep complexity hidden behind narrow, stable interfaces; point dependencies toward the stable core. "Deep modules" is used as a quality rubric that pressure-tests a design, never as the carving rule itself. The result is codebases with clean seams that stay coherent as they grow.

**2 — Long-horizon, high-difficulty autonomous execution.** Large autonomous builds usually degrade because error compounds over a long horizon with nothing to catch it. Here, nothing runs without a validated plan; work is decomposed until each unit is small and independently verifiable; every level reviews composition and fidelity at its own altitude; and tests are frozen before code, so the work is anchored to them rather than the reverse. A genuinely hard task can run end-to-end with accuracy held up by structure instead of hope.

**3 — Alignment and fidelity.** The first priority is that the thing built is the thing meant — and stays that way from intent to shipped code. Intent is captured precisely: the intake probes tradeoffs to find where the user actually has opinions (people reveal them at a fork, not when asked "do you care?"), and records how technically fluent they are per area, so the system knows what to decide for them and what to bring back. Every requirement then carries a stable ID that threads through the whole system — design element, test, branch, and review all trace to it. The gate, the per-level independent reviews, and that traceability spine exist for one purpose: to kill drift.

## The five levels

A separation by the kind of thinking each does, not a rank ladder:

- **L1 — System Orchestrator** — captures the user's intent, guards it for the life of the project, routes work, and is the only level the user talks to.
- **L2 — Project Architect** — designs the shape of the solution: where the module boundaries fall, the interfaces between them, the decisions that are expensive to reverse.
- **L3 — Module Designer** — takes one module and designs it in depth, then manages its construction.
- **L4 — Workstream Coordinator** — breaks a module into concrete tasks and authors the acceptance tests they'll be judged against.
- **L5 — Task Executor** — writes the actual code against frozen tests, paired with an independent reviewer (L5+) that checks it.

Direction flows down as minimal "short-email" briefs (what, not how); results flow up as compressed reports. Raw work never moves up, and clean context is preserved at every boundary.

## How it works, end to end

Work runs as two cycles joined by a single hard gate — design, then build — never one big waterfall.

The **design cycle** produces a validated plan and not a line of code. Intake turns the user's intent into a precise, tagged, traceable spec. The architect proposes the structure. The module designers detail each area in a single coordinated round, renegotiating interfaces against real constraints. And — critically — the tests and review rubrics are written here, before any code, by agents that aren't the ones who'll do the work.

The **plan-alignment gate** is the heart of the system, at the seam between designing and building. It reads the whole assembled plan against the original intent — something per-level reviews structurally can't do — and catches the three ways a plan drifts even when every local step looked fine: dropped requirements, unrequested additions, and requirements technically present but subtly wrong. It even inspects its own first translation (turning the user's prose into requirements), because that's where drift enters upstream of every other check. A human gives a warm sign-off on a triangulated view; nothing builds until it passes.

The **build cycle** begins only on PASS: executors write code against the now-frozen plan, every level's output checked by independent review before it moves up.

## What makes it distinctive

- **Tests before code, by not-the-coder.** Tests written after the fact get bent to fit the code; written first, from the spec, by someone else, the code must serve them. (Corollary, proven in simulation: tests anchor only what they assert — so an independent reviewer is load-bearing, catching the fidelity gaps tests miss.)
- **One spine.** A single hierarchical scheme is the requirement ID, the agent's address, the workspace path, the git branch, the rubric location, and the visibility graph — decided once, it serves all of them.
- **Cross-model by design.** Opus 5.0 for the generative/architecture levels; GPT-5.6 Sol (via Codex) for execution, where literal precision is the strength. Failures escalate rather than silently degrade.
- **Documentation is memory.** Every level can be killed and respawned from its artifacts; truth lives in documents, coordinated over a lightweight bus. The system also keeps a workspace to observe itself and propose its own improvements.
- **Walking-skeleton first.** A thin end-to-end thread proves the connections before the full build commits to them.

The methodology isn't invented — it's borrowed from how architecture firms and consultancies actually turn a client's intent into a built thing, instantiated with agents instead of people.

## Repository layout

This directory holds both the specification corpus and the runtime built from it.

```text
design/         the governing specification corpus; design/INDEX.md is its map
operational/    what each agent loads at spawn: L1–L5, review seats, and shared protocols
harnessd/       the run-scoped daemon, CLI, supervision, gates, and state store
tools/          rendering, evaluation, and launch-audit instruments
tests/          the factory test suite
dry-run/        a worked end-to-end example through design, alignment, and a built slice
.claude/skills/l1-l5-harness/  the explicit operator skill
```

Good entry points: `design/ARCHITECTURE.md` (the system design), `design/PLAN-ALIGNMENT-GATE.md` (the central fidelity mechanism), `harnessd/daemon.py` (the runtime spine), and `dry-run/` (the worked example).

## Running the tests

Run the suite with `python3 -m pytest`. On macOS, the suite automatically selects a short temporary root for AF_UNIX sockets; use the explicit fallback below only if a local environment overrides that behavior:

```bash
TMPDIR=/tmp python3 -m pytest
```

## Operating a build

The supported operator surface is the explicit-invocation Claude Code project skill at
`.claude/skills/l1-l5-harness/SKILL.md`. Claude Code discovers it automatically in this repository.
Invoke `/l1-l5-harness` with `start`, `status`, `attach`, `promote`, or another documented
`harnessctl` verb; the skill keeps `harnessctl` as the state boundary.

Before the first intake, copy `operational/shared/user-profile.template.md` to `operational/shared/user-profile.md` and fill it in; the destination is a gitignored, per-deployment profile.

The direct one-command terminal path starts an on-demand per-run daemon, spawns L1, and attaches:

```bash
python3 -m harnessd.harnessctl start \
  --build-id <unique-build-id> \
  --intake-file <operator-intake.md>
```

The daemon is protected by launchd only while that build is live. Crashes restart in recovery-only
mode; an empty/all-terminal binding ledger ends the process cleanly and boots its per-run launchd
job out. Nothing is installed at login. The rendered job freezes absolute daemon tool paths. A
startup refusal distinguishes a live-but-socketless daemon from a crash loop and includes the
launchd restart count plus a bounded stderr tail; the empty-stderr hang form gives the exact macOS
Documents-folder permission repair for the rendered Python interpreter.

Watch the whole live or completed journey from the same operator surface:

```bash
python3 -m harnessd.harnessctl view --build-id <build-id> --format terminal
python3 -m harnessd.harnessctl view --build-id <build-id> --format json
python3 -m harnessd.harnessctl journey --build-id <build-id>
```

`view` joins ledger and artifact facts on demand. `journey` produces a self-contained HTML/SVG
supervision + dependency DAG at
`<runtime-root>/.harnessd/views/journey.html`. Both work after the run-scoped daemon exits; use
`journey --stdout` for a no-write capture. No daemon maintains a competing aggregate or dashboard
file.

For optional discovery outside this repository, link the versioned source rather than copying it:

```bash
mkdir -p ~/.claude/skills
ln -sfn "$(git rev-parse --show-toplevel)/.claude/skills/l1-l5-harness" \
  ~/.claude/skills/l1-l5-harness
```

The skill resolves commands back into this checkout through that symlink; it does not treat an
unrelated current project as the harness repository.

### Pinned Codex auth

The pinned Codex runtime and the user's normal Codex CLI share one OAuth token lineage. Set up or
repair the pinned auth link from this repository with:

```bash
python3 tools/repair_pinned_codex_auth.py
```

The command links `.codex-pinned/config/auth.json` to the default `~/.codex/auth.json`, backing up
any displaced regular pinned auth file under the ignored `.codex-pinned/auth-backups/` directory.
It is safe to rerun when the link is already correct. If auth expires or is revoked, run `codex
login` normally against the default `~/.codex` home; do not log in with `CODEX_HOME` pointed at the
pinned config, because Codex may replace the link and recreate two competing refresh-token
lineages. Then rerun the repair command if the link was replaced. Worker seats still receive
isolated access/id snapshots and no usable refresh token.

## Status

The design is complete and hardened against a full end-to-end simulation — which built a real vertical slice (17/17 tests passing, a genuine cross-model handoff) and surfaced the gaps, now closed.

The runtime is built and running supervised live builds. A run-scoped daemon boots the hierarchy on pinned Claude Code/Codex runtimes, reads runtime-native turn state with evidence fallback, and enforces the spec's gates deterministically at three chokepoints: no under-equipped agent spawns (pieces-present gate), no DONE is accepted without its return contract, and no delivery leaves without intake-confirmation and a derived destination (promote gate). First live runs have delivered working software end-to-end — including the full refuse → self-heal → re-sign loop firing unattended at both leaf and root. Current work: scoring behavioral adherence run-by-run against the spec and closing the gaps it surfaces; the commit history is the build log.
