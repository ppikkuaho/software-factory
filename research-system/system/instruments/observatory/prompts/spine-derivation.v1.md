You are deriving **candidate** L4 observatory expectations from a deliberately
bounded governing-document set. You are not ratifying them and you are not
editing the observatory spine. Your response is a provisional, uncitable
candidate file pending user ratification under RQ-7.

## Frozen runner values

The runner mechanically substitutes these values before invoking you. Reproduce
them exactly; do not calculate replacements:

- prompt template name: `spine-derivation.v1.md`
- prompt template sha256: `{{PROMPT_TEMPLATE_SHA256}}`
- subject-repo git revision: `{{SUBJECT_REPO_REV}}`

## Input contract

Read only the staged files listed in `SOURCE-MANIFEST.md`. The mandatory core is:

- `operational/L4/role.md`
- `operational/L4/config.md`
- the shared protocols directly referenced by those two documents and listed in
  the manifest
- `subject-repo-rev.txt`, which must equal `{{SUBJECT_REPO_REV}}`

All inputs are files from one captured subject-repo revision. Do not read the
subject repository, the research repository, the web, or any unstaged file.
The manifest is the source denominator: do not silently widen it.

## Task

Extract documented L4 obligations and target-quality behaviors, then convert
them into observable candidate expectations. Every candidate MUST carry every
P8 field using this exact grammar:

```text
id: SP-L4-<n>
statement: <one observable behavioral expectation>
source citation: <subject-repo-relative document> § <exact section heading> — "<verbatim source text>"
observable trace signal: <specific signal visible in a production trace or its anchored digest>
check-cost tier: mechanical | digest | deep-read
class: floor | aspiration
doc-version stamp: {{SUBJECT_REPO_REV}}
```

Use `floor` only when the cited document states a hard obligation. A floor is
binary and its violation is a defect. Use `aspiration` for target or quality
behavior; it is graded. Derived entries DEFAULT to `aspiration`. Never upgrade
guidance, preference, or an inferred best practice into a hard obligation.

Choose check cost honestly:

- `mechanical`: deterministic extraction or counting is sufficient;
- `digest`: an anchored behavioral digest is sufficient;
- `deep-read`: local-surface replay or comparable judgment is required.

## Citation discipline

The source citation must name the real staged document and its exact section
heading, followed by verbatim source text. NO obligation may appear without a
real document anchor. Do not paraphrase in the quoted source text, merge clauses
from different anchors, or fabricate a heading. A later fabricated-obligation
check will verify each citation against the staged source files.

## Active-set discipline

Propose **no more than 10** candidates in `## ACTIVE SET`. This is a budget cap,
not a target: use fewer when fewer merit repeated attention. Select a coherent,
high-value set without duplicating the seven global-floor concerns.

Place every other supported candidate in `## REFERENCE CATALOGUE`. Catalogue
entries keep the same complete P8 grammar but are not active. State that the
catalogue rotates into later active sets by the lane's current focus, subject to
the same budget and user ratification. Do not overstuff the active set by
renaming, grouping, or nesting multiple obligations inside one entry.

## Source-coverage rider

Immediately after the title, emit exactly one line with this grammar:

`SOURCE-COVERAGE: consumed=[<every staged governing doc actually consumed>]; deliberately-excluded=[<L4-relevant surfaces outside this bounded pass, with a short reason>]`

The consumed list and the deliberately excluded list are both mandatory. Be
honest about the denominator. Future passes widen coverage deliberately, never
silently. If a staged document could not be consumed, put it in the excluded
list with the reason rather than claiming coverage.

## Output contract

Your entire response is one Markdown document and nothing else. Begin with this
provenance frontmatter, reproducing runner values exactly:

```yaml
---
artifact: spine-candidates.L4.v1
prompt_template:
  name: spine-derivation.v1.md
  sha256: {{PROMPT_TEMPLATE_SHA256}}
doc_version_stamps:
  subject_repo: {{SUBJECT_REPO_REV}}
provenance: director-provisional-2026-07-13
standing: "candidate — uncitable, pending user ratification RQ-7"
---
```

Then emit exactly these surfaces, in order:

1. `# L4 spine candidates — v1`
2. the single required `SOURCE-COVERAGE:` line
3. `## ACTIVE SET` containing zero to ten complete candidates
4. `## REFERENCE CATALOGUE` containing all remaining complete candidates and
   the rotation-by-lane-focus note
5. `## Derivation notes` containing only ambiguities, overlaps with the global
   floor, and unsupported-looking source language deliberately not promoted

IDs must be unique and sequential across both candidate sections. Candidate
standing never becomes ratified through this generation step.
