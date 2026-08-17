# Inter-Level Communication

**Status:** current process design, reconciled 2026-07-24.

This document defines transport, addressing, message contracts, visibility, reporting, questions,
and contract amendment. `operational/shared/comms-protocol.md` is the agent-facing procedure;
`TRANSPORTS.md` owns delivery mechanics; `WORKSPACE-SCHEMA.md` owns paths.

## One primitive

Every parent↔child communication edge uses one sender-owned durable message:

```text
messages/<message-id>.json
```

The record names the sender address and owner token, direct-edge recipient, event type, artifact
pointer, creation identity, and whether an answer is required. The referenced artifact holds the
substantive product, decision account, evidence, or proposal; the message remains a small durable
pointer plus enough context to explain why the recipient should read it. The recipient's
`.inbox.<seat>.jsonl` row is a recoverable delivery pointer, not a second source of truth.

Addressing uses the one hierarchical-path spine plus a role suffix:

```text
proj/payments/gateway#exec
proj/payments/gateway#review
```

Addresses belong to work seats, not process instances, and survive collapse or respawn. Routine
traffic is direct parent↔child. A cross-parent concern goes to the common ancestor; a parent may
still issue an authorized downward instruction inside its own subtree.

## Questions, answers, and arbitration

`needs_answer: true` makes an ordinary message an open question. The question names the decision
needed and the durable evidence or options. The asker can then park because its wait is derivable
from the ledger. An answer is another message that names the question; committing the answer closes
the question atomically and wakes the asker.

Arbitration is a queryable tag on an open question when the direct recipient should adjudicate
rather than merely clarify. It is not another message kind or relay. The receiving owner evaluates
the evidence independently; a subordinate's recommendation is useful signal, not delegated
decision authority.

## Messages persuade; owners amend

A message can report, propose, clarify, ask, answer, or call attention to evidence. It cannot alter
a frozen brief, acceptance package, intent spec, interface, or other contract. The owner of the
canonical contract home makes a change through the explicit immutable revision channel. The notary
stamps the new version and the daemon sends an ordinary `contract_amendment` message to affected
holders. Each holder re-reads and explicitly rebinds through its fenced `contract-rebind/` record.

Contract receipts make stale holders visible. Staleness currently wakes the holder but is not a
turn-end, submission, or gate blocker; that enforcement decision remains owner-open.

## Event wake contract

The daemon owns the global event→wake table; senders have no wake knob.

| Event | Wake behavior |
|---|---|
| direct-edge message or answer | wake recipient |
| contract amendment | wake affected holder |
| gate verdict, plan-alignment decision, human answer | wake recipient |
| ordinary child completion before cohort ready | append silently |
| `product` or `review_check` cohort barrier transition | one wake to cohort owner |

Delivery is fenced to the current owner, verified against runtime turn evidence, retried from the
durable message/question/verdict state, and safe under duplication. A missed keystroke costs
latency, never the record. Parked seats do not poll; the event that resolves their ledger-derivable
wait wakes them.

## Visibility and payload discipline

The need-to-know read graph is derived from the same path spine:

- own subtree;
- parent direct node surface;
- same-parent siblings' direct node surfaces;
- L1 whole-tree read exception.

The strong-form local-file blinders in `SECURITY.md` make that graph physical. Visibility is not
authority: siblings can publish interfaces without commanding one another, while parents own
decisions inside their subtree.

Messages point to artifacts; they do not duplicate file bodies, reports, screenshots, or logs.
Attachments live in the appropriate owned node and are referenced by exact path and, where frozen,
stamp/receipt identity.

## Reporting and escalation

Reporting is event-driven, never periodic. Meaningful products, decisions, questions, amendments,
and verdicts create message records. “Still working” chatter is absent. Ordinary child completion
is intentionally quiet until its cohort barrier crosses.

When ambiguity affects the outcome, the seat asks before mutating the affected product. A
decision-ready question states what happened, what was tried, evidence, options, and recommendation.
The seat then parks on the open question. “Escalate, do not decide” converts an underspecified
boundary into an observable stop instead of plausible but wrong downstream work.

## Compatibility boundary

`handoffs/*.json`, `coordination-note`, `coordination-decision`, `answer-down`, and
`.signal ESCALATED` are legacy read-side compatibility inputs while older live seats drain. They
are not authored for new work and do not define a parallel cargo or terminal taxonomy. Canonical
direct-edge messages and open questions are the only current parent↔child write surface.

*Created: 2026-03-17. Reconciled: 2026-07-24 — canonical messages/questions, wake table, cohort
barriers, and owner-home contract revision.*
