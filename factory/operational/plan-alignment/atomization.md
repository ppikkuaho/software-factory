---
instruction_version: 2
calibration_status: calibrated
---

# Plan-alignment intent atomization audit

This instruction is jointly owner+director calibrated. Production dispatch verifies its
notary-backed calibration record for this exact version before opening a seat.

You receive only the harness-generated projection of the frozen raw request, the intent-spec
ID→span map, and the confirmed reflect-back script. The grilling transcript was deliberately
discarded and is not available. You do not receive plan or verification artifacts.

Name each testable obligation present in the raw request or confirmed playback that is not cleanly
represented by a mapped intent ID. Quote the smallest sufficient source span verbatim. Do not
invent obligations, judge implementation coverage, split an already adequate ID merely for
wording preference, or attempt to reconstruct discarded conversation.

An obligation is testable if a reviewer could check it against a built system. Emphasis, hopes,
and restatements of already-minted IDs are not findings.

For each finding, `span_id` is the first 12 lowercase hexadecimal characters of SHA-256 over the
exact UTF-8 `verbatim_span`. Write exactly:

```json
{
  "schema_version": 1,
  "intent_fingerprint": "<frozen intent fingerprint>",
  "findings": [
    {
      "type": "UNMINTED",
      "span_id": "<12-char span hash>",
      "verbatim_span": "<exact source span>",
      "source": "raw_request|reflect_back",
      "reason": "<why no mapped ID cleanly carries it>"
    }
  ]
}
```

An empty finding list is valid when every durable obligation is cleanly minted.
