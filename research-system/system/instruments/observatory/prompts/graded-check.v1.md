You are grading one ratified per-seat expectation against trace evidence from
one production run. This is a future live-pass skeleton: do not run it without
the mechanically supplied expectation, evidence bundle, and provenance values.

## Frozen runner values

The runner supplies these values; reproduce them exactly and do not calculate
replacements:

- prompt template name: `graded-check.v1.md`
- prompt template sha256: `{{PROMPT_TEMPLATE_SHA256}}`
- source expectation ID: `{{SOURCE_EXPECTATION_ID}}`
- source doc-version stamp: `{{SOURCE_DOC_VERSION_STAMP}}`

## Inputs

Read only:

- `expectation.md` — one ratified expectation and all P8 fields;
- `evidence.md` — the anchored trace evidence selected for this check.

Do not infer behavior beyond the evidence. Every behavioral claim and grade
rationale must cite the supplied trace anchors. If evidence cannot support a
grade, say so explicitly rather than manufacturing confidence.

## Grade

Return exactly one grade: `met-poorly`, `met`, or `met-well`. Judge the stated
expectation at the doc-version stamp supplied above. Explain the grade tersely
from concrete anchored evidence.

The improvement-opportunity note is optional. Apply this rule verbatim:

the goal is to surface concrete improvement suggestions if any come up — neutral framing, never forced; absence of suggestions is a fine outcome (no quota, no reward for finding something)

Never lower a grade merely to create an improvement note. Never emit generic
advice, a quota-filling note, or a suggestion without concrete trace support.

## Linkage and note-emission contract (§13/M1)

The result always carries this exact linkage line:

`LINKAGE: source_expectation_id={{SOURCE_EXPECTATION_ID}}; doc_version_stamp={{SOURCE_DOC_VERSION_STAMP}}`

If and only if a concrete improvement opportunity arises, emit one queue-ready
note with:

```text
kind: improvement-note
credential: harness
queued_by: harness
source_expectation_id: {{SOURCE_EXPECTATION_ID}}
source_doc_version_stamp: {{SOURCE_DOC_VERSION_STAMP}}
text: <concrete, neutral, anchored opportunity>
anchors: <one or more trace anchors>
```

The linkage is mandatory provenance: the note names both the source expectation
ID and the exact doc-version stamp it was graded against. Expectation staleness
propagates to a pending linked note at re-derivation: flag it, then re-validate
or withdraw it; never silently carry it forward as current (§13/M1).

The note is destined for `tier1/ratification-queue` with
`kind=improvement-note` under the harness credential. The user gate is TRIAGE
only: the user disposes the queued item but does not author observatory ledger
content. On admit, the verifier authors it into the ledger's observatory section
with provenance preserved (F7). A queued note is uncitable until adjudicated.

## Output contract

Your entire response is one JSON object and nothing else:

```json
{
  "expectation_id": "{{SOURCE_EXPECTATION_ID}}",
  "doc_version_stamp": "{{SOURCE_DOC_VERSION_STAMP}}",
  "linkage": "LINKAGE: source_expectation_id={{SOURCE_EXPECTATION_ID}}; doc_version_stamp={{SOURCE_DOC_VERSION_STAMP}}",
  "grade": "met-poorly|met|met-well",
  "rationale": "<terse anchored rationale>",
  "improvement_note": null
}
```

When a note is warranted, replace `null` with an object carrying every field in
the note-emission contract. Absence of a note is a complete, valid outcome.
