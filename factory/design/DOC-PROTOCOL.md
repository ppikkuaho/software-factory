# DOC-PROTOCOL — Documentation Schema & Write Protocol

**The canonical rules for writing in this repo.** Read before creating, moving, superseding, or renaming any document. `design/INDEX.md` says WHAT is current; this file says WHERE things go and HOW change happens. Rules here change only via the amendment protocol at the bottom — never by drift.

*Created 2026-07-07 out of the estate audit (`working-notes/ESTATE-AUDIT-2026-07-06.md`), which found the docs honest but disoriented: no claim was false, but supersession, indexing, and entry points had rotted structurally.*

## Principles

The hygiene standard this protocol serves (ratified 2026-07-06, owner + lead):

1. **Single source of truth** — every fact has one authoritative home; everything else points at it.
2. **Clear tiers** — canonical / working / historical are distinguishable at a glance.
3. **Status ledger** — built vs designed vs deprecated is answerable from dated, indexed surfaces.
4. **Clean VCS** — work is committed at burst end; nothing load-bearing lives only uncommitted or outside the repo.
5. **Supersession discipline** — replaced docs get a dated banner pointing forward; nothing is silently deleted; git history is the archive.
6. **Load-bearing orientation** — a cold reader reconstructs what / where / what-next in minutes, from the entry points alone.
7. **Minimum viable state** — the fewest living surfaces that satisfy 1–6. Every living surface is a standing promise that stateless agents must keep without being reminded; the writers here ARE stateless agents, and noise compounds. When in doubt, don't add a surface.

**The write-path corollary (the owner's core diagnosis):** discipline must be legible to a cold agent *at the moment of writing*. A rule recorded where nobody looks gets skipped — measured in this repo in June 2026, when a bolded "do not skip" index rule was skipped anyway and 25 files sat unindexed and uncommitted for 15 days. The cure is delivery, not automation: the root `AGENTS.md`/`CLAUDE.md` pair auto-loads into every session and points here. Keep that chain intact.

## Schema — what lives where

| Surface | What it is | Write rules |
|---|---|---|
| `README.md` | Front door: what the system is | Rarely changes; keep the opening true |
| `AGENTS.md` + `CLAUDE.md` (root) | The delivery pair: auto-loads into every session and points here; `CLAUDE.md` is a symlink of `AGENTS.md` | Keep pointer-thin; edit only via the amendment protocol; break the symlink only if tool-specific instructions must diverge |
| `design/` | The specs; `design/INDEX.md` is the ONE governing map | New or superseded design doc ⇒ update INDEX **in the same change** |
| `design/DOC-PROTOCOL.md` | This file | Amendment protocol below |
| `operational/` | Agent-facing runtime docs | Governed by `design/DOC-SYSTEM.md` (shared blocks + `tools/render_blocks.py` + `tests/test_doc_blocks.py`). Never hand-edit inside block markers |
| `harnessd/`, `tools/`, `tests/` | The code and its instruments | Code change ⇒ run the touched modules' tests |
| Root files (`PINNED-CC.md`, …) | Repository-wide records | Adding a NEW root file requires an amendment here |
| `~/l1-l5-workspaces/` | Deployment state the harness builds — not repo contents | The daemon writes here; humans and maintenance agents don't |

## Write protocol

- **Creating a doc:** place it per the schema, and list it in its governing index in the SAME change. A doc on disk but not indexed is invisible — this repo's measured #1 rot mode.
- **Superseding:** dated banner on the old doc pointing forward, and move its INDEX entry to the superseded section. Never silently delete; git is the archive.
- **Status:** status lives in `design/INDEX.md`, dated. Don't scatter dated `Status:` headers across living docs — they rot in days (measured: 15 of 25 stale).
- **Evidence pointers:** never point a living doc at `/tmp`, session scratchpads, or other mortal locations. Put evidence in a durable, reviewed location before citing it.
- **Burst-end ritual** — ending a work burst means both, in order:
  1. Write or refresh the current session bridge note (state, decisions, resume point).
  2. Commit everything. No pushes without an explicit owner ask.

## Enforcement

Three channels, all behavioral — by owner ruling there are no mechanical gates on this layer:

- **Docs:** the root `AGENTS.md`/`CLAUDE.md` pair auto-loads into every session and points here — the rules arrive at the moment of writing.
- **Practice:** the burst-end ritual above.
- **Owner:** on returning after time away, the first act is a short orientation-health review: check (a) every `design/*.md` is either listed in `design/INDEX.md` or carries a supersession banner; (b) any dated status claim that contradicts git history. Report mismatches before doing any work.

`tests/test_doc_blocks.py` guards ONLY the `operational/` shared blocks, and a green run there means mechanical conformance, never doc health. Nothing mechanical guards THIS layer — a green suite says nothing about orientation.

## Amendment protocol

The rules in this file change only deliberately: edit this file, owner reviews. If a rule keeps getting violated, that is data about the rule — bring it to the owner rather than silently forking conventions.
