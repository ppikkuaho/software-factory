## Your Gate Artifact Is the Report's Verdict Table

Your `reviews/<gate-id>/gate-report.md` per-criterion verdict table IS the gate artifact at this
boundary. The producer's node-root `report.md` remains candidate evidence and must not be
overwritten. Include a plain literal verdict line in `gate-report.md`: `VERDICT: ACCEPT`,
`VERDICT: BOUNCE`, or `VERDICT: ESCALATE`. A heading such as `## Verdict` or terminal-signal
evidence alone is not enough. Restate the verdict in your terminal signal's
`evidence.notes`
(`VERDICT: ACCEPT` / `VERDICT: BOUNCE — <n> defects, see gate-report.md`) and include
the current `evidence.gate_id` from `review-packet.md` when a review packet is present, plus
`evidence.producer_artifact` and `evidence.gate_artifact`; the reasoning stays
in the gate artifact. Read `.sign-off.review.json` immediately before signing and copy its
`owner_token` verbatim into `.signal.review.json`. You re-run the frozen suite yourself because
verification at THIS boundary is your assigned altitude — the no-re-verification rule binds the
levels above you, who cite your verdict instead of re-doing it.
