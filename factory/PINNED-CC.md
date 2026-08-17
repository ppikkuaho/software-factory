# Pinned Claude Code — the harness substrate

The L1-L5 harness runs L1-L4 (and L5+) on a **pinned, vanilla, isolated** Claude Code — separate from any daily/patched CC (which auto-updates and carries Life-OS patches/hooks/injections). The whole point of pinning is a frozen, known surface we can patch (H40) and trust to behave identically across runs.

## What's pinned (2026-06-04)
- **Version:** `2.1.152` — the `stable` dist-tag at pin time. Chosen for a conservative, frozen surface, not newest features.
- **Install (gitignored, reproducible):**
  ```bash
  npm install --prefix .cc-pinned @anthropic-ai/claude-code@2.1.152
  ```
  Native binary: `.cc-pinned/node_modules/@anthropic-ai/claude-code/bin/claude.exe` (Mach-O arm64, ~214 MB).
- **Launch (isolated):** `.cc-pinned/claude-pinned.sh` — sets `CLAUDE_CONFIG_DIR=.cc-pinned/config` (clean config: no inherited hooks/MCP/injections), `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, `DISABLE_AUTOUPDATER=1`. Auth via `CLAUDE_CODE_OAUTH_TOKEN` (env, **verified present in the binary**) — not by sharing the patched `~/.claude`.
- **`.cc-pinned/` is gitignored** (node_modules + config); this file is the tracked record + reinstall recipe.

## Opus 5 explicit-ID validation (2026-07-24)

The harness deliberately **keeps the proven 2.1.152 pin** and changes only its explicit model
mapping to `opus-5.0 → claude-opus-5`.

- Anthropic's Claude Code 2.1.219 changelog is the first model-aware release surface to name
  `claude-opus-5`. At discovery time, immutable 2.1.219 carried npm's mutable `latest`/`next`
  dist-tags while `stable` still pointed to 2.1.211.
- Claude Code 2.1.218 nevertheless passed the explicit server model ID through successfully. A
  bounded follow-up on the already trusted 2.1.152 pin also completed a real OAuth-only Opus 5
  turn. Its init `model`, assistant `message.model`, and result `modelUsage` key all agreed on
  `claude-opus-5`.
- The 2.1.152 result schema does not emit the newer `canonicalModel`/`provider` fields seen on
  2.1.218. The server's assistant `message.model` is the attribution surface available on this pin;
  no harness code parses or requires the newer fields.
- Re-pinning to 2.1.219 or later remains an available future option if a model-aware CLI feature or
  its richer result schema becomes load-bearing. Nothing is owed merely because npm later moves a
  dist-tag: the harness pins immutable versions and explicit model IDs, and its own production boot
  plus full suite are the stability authority.

## Reasoning-effort validation (2026-07-28)

Pinned Claude Code 2.1.152 exposes the native flag `--effort <level>` with the
exact accepted tier set `low`, `medium`, `high`, `xhigh`, `max`. No translation
is required for the harness calibration: `--effort xhigh --version` and
`--effort high --version` both exit 0, while an invalid tier exits 1 with the
accepted set named. These are parser-only probes; no model call was made.

The generated seat argv now supplies the `LevelConfig.reasoning_effort` value
explicitly. L1/L2/L3 execution seats use `xhigh`; every other Claude seat uses
`high`. The runtime default is never relied upon.

## Useful env vars discovered in the binary
`CLAUDE_CONFIG_DIR`, `CLAUDE_CODE_OAUTH_TOKEN` (+ `_FILE_DESCRIPTOR`), `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`, `CLAUDE_CODE_SUBAGENT_MODEL`, `CLAUDE_CODE_AUTO_COMPACT_WINDOW`, `CLAUDE_CODE_SESSION_KIND`, `CLAUDE_CODE_ENTRYPOINT`, `CLAUDE_CODE_TMPDIR`, `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB`, `CLAUDE_CODE_REMOTE`.
