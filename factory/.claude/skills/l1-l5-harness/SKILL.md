---
name: l1-l5-harness
description: Operate an L1-L5 harness build through its run-scoped daemon and harnessctl control boundary.
argument-hint: "<start|status|view|journey|attach|show|tree|fidelity-playback|promote|decision-or-repair-verb> [arguments]"
disable-model-invocation: true
---

# L1-L5 harness operator

Use this skill only when the operator deliberately invokes it. Interpret the first token of
`$ARGUMENTS` as the requested action. If the action or target is missing, perform only `status` and
ask for the missing choice; never guess a mutation, verdict, address, owner token, delivery
destination, or acceptance reference.

## Control boundary

Run from the harness repository root. When the skill is project-local:

```bash
cd "$(git rev-parse --show-toplevel)"
python3 -m harnessd.harnessctl <verb> ...
```

When the skill was discovered through the optional personal symlink while the session is in some
other checkout, resolve the harness checkout through that link first:

```bash
harness_skill_source="$(readlink "$HOME/.claude/skills/l1-l5-harness")"
cd "$(cd "$harness_skill_source/../../.." && pwd -P)"
python3 -m harnessd.harnessctl <verb> ...
```

Every state action goes through `python3 -m harnessd.harnessctl` and daemon IPC. Never edit runtime state files,
node binding data, run-ledger data, sign-off files, gates, receipts, or lifecycle
markers as a substitute for a verb. `start` is the single pre-daemon exception implemented inside
`harnessctl`; it touches launchd lifecycle artifacts only and never build state.

Global `--socket <path>` goes before the verb. Otherwise the helpers resolve
`$HARNESSD_SOCKET`, `$HARNESS_RUNTIME_ROOT`, or the commissioned
`~/l1-l5-workspaces/<build-id>` path. Exit codes are: `0` success, `2` command/precondition refusal,
and `3` daemon/lifecycle transport failure. Preserve the complete structured error in the report.

Before a mutation, use `status` or `show` to collect the current address, state, generation, and
owner-token fences. Never reuse a fence after another state change.

## Start, watch, and enter a build

### start

The normal one-command path starts the run-scoped daemon, waits for L1, and attaches to L1:

```bash
python3 -m harnessd.harnessctl start \
  --build-id <unique-build-id> \
  --intake-file <operator-intake.md>
```

Use `--runtime-root <absolute-path>` only for an explicit non-default run home. Use
`--intake "<text>"` for a short intake. `--wait-seconds <n>` changes only the bounded readiness
wait.

To commission mixed L3 module review-panel arms for an experiment, repeat
`--review-panel-arm '<module-address-glob>=<axis>[,<axis>...]'`. Declarations are matched against
the canonical L3 producer address in command-line order; the first match wins. The accepted axes
are `fidelity-coverage`, `composition-interface`, `evidence-credibility`, `risk-readiness`, and
`broad`. For example:

```bash
python3 -m harnessd.harnessctl start \
  --build-id <unique-build-id> \
  --intake-file <operator-intake.md> \
  --review-panel-arm 'forge-queue/queue-core#exec=composition-interface,risk-readiness' \
  --review-panel-arm 'forge-queue/persistence#exec=broad'
```

Omit the flag for the unchanged four-axis L3 module panel. This parameter does not alter L4
workstream panels or the separately calibrated L2+ product probes.

`start` refuses if the daemon, its lifetime lock, or its protective launchd job is already live.
Do not work around that refusal; inspect `status`. The generated job exists only for this run,
restarts crashes in recovery mode, and is booted out after definitive idle.

On readiness failure, preserve the complete structured response. `daemon_crashed` includes the
launchd restart count and bounded stderr traceback tail. `daemon_hung` means the process is alive
but its socket never appeared; when stderr is empty, the response names the exact rendered Python
path and the macOS Documents-folder grant repair. A Homebrew Python upgrade can move the Cellar
binary path and orphan that grant. `l1_not_ready` means daemon IPC answered but L1 did not become
live before the bound.

Claude Code's Bash tool may not expose an interactive controlling terminal. In that case, keep the
same start boundary but split only the final UI step:

```bash
python3 -m harnessd.harnessctl start \
  --build-id <unique-build-id> \
  --intake-file <operator-intake.md> \
  --no-attach
python3 -m harnessd.harnessctl attach --build-id <unique-build-id>
```

The first form remains the direct one-command terminal path.

Owner-final fidelity is the default for every run. A commissioning run may authorize one named
operator delegate only by declaring all three inputs at launch:

```bash
HARNESS_FIDELITY_PLAYBACK_AUTHORITY=operator-delegate \
HARNESS_FIDELITY_PLAYBACK_DELEGATE="<operator-label>" \
HARNESS_FIDELITY_PLAYBACK_DELEGATION_REASON="<commissioning reason>" \
python3 -m harnessd.harnessctl start \
  --build-id <unique-build-id> \
  --intake-file <operator-intake.md>
```

Never infer delegation from who launched the daemon, a test destination, or an operator being
present. The declared delegate is commissioning-only and is recorded distinctly from the owner in
the binding, WAL, answer artifact, views, and promotion response.

### status and tree

```bash
python3 -m harnessd.harnessctl status --build-id <build-id>
python3 -m harnessd.harnessctl tree
```

`status` reports the existing supervision tree or a definitively idle run. `tree` is the raw live
daemon tree helper for a configured socket.

### whole-build view and journey

Use the generated-on-read join when the operator needs the whole journey rather than the lightweight
live daemon tree:

```bash
python3 -m harnessd.harnessctl view --build-id <build-id> --format terminal
python3 -m harnessd.harnessctl view --build-id <build-id> --format json
python3 -m harnessd.harnessctl journey --build-id <build-id>
python3 -m harnessd.harnessctl journey --build-id <build-id> --stdout
```

`view` joins the binding ledger with current turn/checklist/question/contract/gate/barrier/posture
facts and writes nothing. `journey` renders the large self-contained HTML/SVG supervision +
dependency DAG. Its default output is
`<runtime-root>/.harnessd/views/journey.html`, outside every agent/gate node tree; `--stdout`
writes no file. An explicit `--output` is accepted only outside `<runtime-root>/nodes/`.

Both verbs work after the run-scoped daemon has exited, so use them for dead-run and terminal-run
postmortems too. Always invoke them through `harnessctl`; do not reconstruct the join by directly
reading or editing ledger/node state. The view is a timestamped capture of concurrently changing
artifacts, not a frozen control-plane checkpoint.

### attach

Attach to L1 by default, or name another live address:

```bash
python3 -m harnessd.harnessctl attach --build-id <build-id>
python3 -m harnessd.harnessctl attach <address> --build-id <build-id>
python3 -m harnessd.harnessctl attach <address> --build-id <build-id> --print-only
```

`attach` obtains the current tmux target through `show`; it never reconstructs a target from an
address or reads binding files.

## Read and inspect

```bash
python3 -m harnessd.harnessctl show <address>
python3 -m harnessd.harnessctl tree
python3 -m harnessd.harnessctl next-seq
python3 -m harnessd.harnessctl validate
python3 -m harnessd.harnessctl reconcile-inspect
```

- `show` is the node truth and fence source.
- `next-seq` reads the next WAL sequence without reserving it.
- `validate` runs the whole-ledger admission scan.
- `reconcile-inspect` is dry inspection; it does not apply recovery.

## Spawn and lifecycle control

Spawn a new child under its live parent with a decision-complete brief:

```bash
python3 -m harnessd.harnessctl spawn <child-address> \
  --parent <parent-address> \
  --level <L2|L3|L4|L5> \
  --brief <brief.md> \
  --expected-owner-token <parent-owner-token>
```

Claim an already-planned node by omitting `--parent` and supplying the current
`--expected-state`, `--expected-generation`, and `--expected-owner-token` where available.

Direct lifecycle repair is exceptional:

```bash
python3 -m harnessd.harnessctl transition <address> \
  --expected-state <state> \
  --expected-generation <generation> \
  --expected-owner-token <token> \
  --target-state <state> \
  --event <typed-event>
python3 -m harnessd.harnessctl pause <address>
python3 -m harnessd.harnessctl resume <address>
python3 -m harnessd.harnessctl kill <address> \
  --expected-owner-token <token> \
  --terminal-signal <FAILED|DIED_INFRA|DIED_METHODOLOGY|DEAD>
python3 -m harnessd.harnessctl service-outbox
python3 -m harnessd.harnessctl service-outbox --node <address>
```

Do not use `transition` to bypass a named gate, receipt, intent-revision, or completion verb.

When the daemon reports a frozen run (a durable `run_frozen` event plus a bus-lite notice to the
director sessions), acknowledge it within the ack window to stop the ladder before it notifies the
user and pauses the run. The verb is run-level and carries no address:

```bash
python3 -m harnessd.harnessctl escalation-ack
```

Acknowledging means you are handling the freeze. It closes the episode durably; it does not unfreeze
anything, and it refuses when no escalation is pending.

## Messages, questions, and decisions

Post an address-owned message:

```bash
python3 -m harnessd.harnessctl message <sender-address> \
  --to <recipient-address> \
  --message-id <stable-id> \
  --summary "<pointer summary>" \
  [--needs-answer] [--tag <tag>] \
  (--text "<content>" | --file <message.md>)
```

When answering a prior question, also pass `--answers-asker <address>` and
`--answers-message-id <id>`.

Direction matters:

```bash
# Owner answers the one current final-fidelity playback question.
python3 -m harnessd.harnessctl answer <L1-project-address> \
  --question-id <fidelity-playback-question-id> \
  --decision <confirm|reject> \
  (--text "<owner note; reject requires the repair reason>" | --file <answer.md>)

# Explicit commissioning delegate form. Both fields must match the launch-time declaration.
python3 -m harnessd.harnessctl answer <L1-project-address> \
  --question-id <fidelity-playback-question-id> \
  --decision <confirm|reject> \
  --authority operator-delegate \
  --actor "<operator-label>" \
  (--text "<delegate note; reject requires the repair reason>" | --file <answer.md>)

# Human/operator answers a current L1 plan-alignment elevation. Obtain the exact question id
# from `status`, `view`, or `show`; one command decides exactly one finding.
python3 -m harnessd.harnessctl answer <L1-address> \
  --question-id <plan-alignment-question-id> \
  --decision <confirm|reject> \
  (--text "<answer note>" | --file <answer.md>)

# Retained human-channel form for a current human question outside plan alignment.
python3 -m harnessd.harnessctl answer <address> (--text "<answer>" | --file <answer.md>)

python3 -m harnessd.harnessctl plan-alignment-decision <L2-address> \
  --decision <pass|fail> \
  (--text "<decision>" | --file <decision.md>)

python3 -m harnessd.harnessctl coordination-decision <child-address> \
  --handoff-id <id> \
  --decision <ack|approve|reject|revise|guidance> \
  (--text "<decision>" | --file <decision.md>)

python3 -m harnessd.harnessctl coordination-note <child-address> \
  --handoff-id <id> \
  --kind <phase_ready|scope_issue|plan_gap|interface_issue|acceptance_gap|approval_request|guidance_request|status_notice> \
  [--summary "<summary>"] \
  (--text "<note>" | --file <note.md>)
```

Use the general `message` primitive for new parent↔child questions. The specialized decision verbs
remain valid for their already-recorded gate/handoff state; never invent a row or marker by hand.

### REPAIR / COMPATIBILITY — legacy shim

For new parent↔child questions, message the child through the canonical primitive; ordinary answer
messages close canonical questions. Use this exact legacy shim only to repair or inspect an old run
whose parent-to-child question state predates canonical answers:

```bash
python3 -m harnessd.harnessctl answer-down <child-address> \
  (--text "<decision>" | --file <decision.md>)
```

## Gates, test refresh, and intent revision

```bash
python3 -m harnessd.harnessctl gate-retry <producer-address> \
  --expected-owner-token <producer-token>

python3 -m harnessd.harnessctl gate-accept <producer-address> \
  --resolver <parent-address> \
  --expected-parent-owner-token <parent-token> \
  --notes "<why the escalated gate is accepted>"

python3 -m harnessd.harnessctl gate-return <producer-address> \
  --resolver <parent-address> \
  --expected-parent-owner-token <parent-token> \
  --notes "<the ruling the producer must repair before a fresh submission>"

python3 -m harnessd.harnessctl test-refresh-approve <L4-address> \
  --approver <L3-address> \
  --expected-parent-owner-token <parent-token> \
  --notes "<accepted refresh>"

python3 -m harnessd.harnessctl intent-revise <L1-address> \
  --target <direct-L2-address> \
  --candidate-ref <confirmed-revision-file> \
  --reason "<client-confirmed reason>" \
  --expected-owner-token <L1-owner-token>
```

- `gate-retry` is for a repaired `gate_failed` producer, not a speculative rerun.
- `gate-accept` resolves an escalated review at the owning parent.
- `gate-return` sends the parent's ruling through the canonical message primitive and returns the
  producer to `gate_bounced`; only a fresh candidate opens a fresh review incarnation. It does not
  reset the bounce or escalation history.
- `test-refresh-approve` follows the accepted L5 test-author/L5+ refresh path.
- `intent-revise` requires the explicit confirmed revision record; never mutate a frozen intent
  spec directly.

## Merge repair and owner-final delivery

Gate PASS automatically runs the sanctioned source-to-parent merge. Use the explicit `merge` verb
only to repair an automatic merge outcome reported as failed:

```bash
python3 -m harnessd.harnessctl merge <source-address> \
  --repo <git-worktree> \
  [--target-branch <branch>] \
  [--requested-by <parent-address>]
```

L1's fidelity judgment is preliminary. The normal owner-final flow is deliberate and ordered:

```bash
# 1. After L1 writes the complete preliminary artifact, freeze one pointer-only owner question.
python3 -m harnessd.harnessctl fidelity-playback <L1-project-address>

# 2. Read the exact question id/status without reconstructing state.
python3 -m harnessd.harnessctl show <L1-project-address>
python3 -m harnessd.harnessctl view --build-id <build-id> --format terminal

# 3. The owner confirms or rejects through `answer` (exact invocations above).

# 4. Only after CONFIRM, deliberately promote. Answering never copies or pushes as a side effect.
python3 -m harnessd.harnessctl promote <L1-project-address> \
  --decision accept \
  --acceptance-ref <frozen-intent-spec> \
  [--delivery-source <node-relative-product-dir>] \
  [--note "<owner-confirmed playback context>"]
```

The daemon requires a complete `Preliminary Verdict: accept` artifact and the current immutable
owner-confirmed playback answer. The promotion response names
`OWNER-CONFIRMED-FIDELITY-PLAYBACK`; an explicitly commissioned delegate instead names
`COMMISSIONING-DELEGATE-CONFIRMED-FIDELITY-PLAYBACK`, never owner confirmation.

A reject closes the owner question, wakes L1, and routes the owner's stated reason through one
canonical repair message to the single live direct L2 child. It does not promote. If that direct
target is absent or ambiguous, the answer remains durable and the command fails loudly for L1 to
repair the topology.

The explicit promotion command is:

```bash
python3 -m harnessd.harnessctl promote <project-address> \
  --decision accept \
  --acceptance-ref <frozen-intent-spec> \
  [--delivery-source <node-relative-product-dir>] \
  [--note "<fidelity verdict context>"]
```

`--decision reject` is retained as a compatibility no-op; it is not the owner playback decision.
Use
`--delivery-destination <destination> --delivery-kind <filesystem-path|git-remote|in-place>` only
for the explicit repair of a prior delivery failure, never to override the intake-captured
destination on a first promotion.

## Installation and versioning

The source of truth is the committed project skill:

```text
.claude/skills/l1-l5-harness/SKILL.md
```

Claude Code discovers it automatically when started in this repository (restart the session if the
top-level `.claude/skills` directory did not exist when that session began).

For optional machine-wide discovery from other working directories, link to the repository source
rather than copying it:

```bash
mkdir -p ~/.claude/skills
ln -sfn "$(git rev-parse --show-toplevel)/.claude/skills/l1-l5-harness" \
  ~/.claude/skills/l1-l5-harness
```

The symlink preserves this repository as the single versioned source of truth and prevents copy
drift. Outside the checkout, resolve the repository through that symlink as shown under Control
boundary; do not run `git rev-parse` against an unrelated current project.
