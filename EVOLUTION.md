# Software Factory — how it evolved

This is the distilled design history of a private development repository whose raw trail — run
evidence, transcripts, and dated working notes — is not published. What survives is the motion: what
was believed, what broke, and what replaced it, between June and August 2026.

---

## Phase 1 — The parts that aren't agents aren't designed (June 2–8)

The project began with a mature semantic architecture and no runtime. Five levels, decomposition
logic, escalation vocabulary, review intent — all described, all resting on nothing. The first week
was spent discovering that a described hierarchy is not a system.

**2026-06-02 — Founded clean, prior art marked suspect.**
A design corpus was carried over from an earlier private workspace; the accompanying implementation
was not. Old code encoded hard-won lessons and subpar mechanics, so it would be mined for findings and
rewritten. Everything inherited was suspect by default and had to re-earn trust.

**2026-06-04 — Apparent architectural completeness was rejected.**
A gap review found no resident process, no authoritative state owner, no transport, no wake mechanism,
no recovery protocol, no admission gate, and no human-control path. The diagnosis that reorganized the
project: *the parts of the system that aren't agents are the parts that aren't designed.*

**2026-06-04 — One accountability invariant, one writer.**
Instead of agents coordinating by self-report, a single daemon became the only mutator of lifecycle
state: every session has exactly one owner, is reconciled within bounded time, and cannot be touched
by a stale one. That produced the binding ledger, intent-first write-ahead log, one spawn chokepoint,
and lease-epoch fencing. Liveness moved with it: silence stopped implying death, replaced by evidence
leases where time may open an investigation but never declare one.

**2026-06-06 — Children are prepared, not described.**
Inline briefs and flat child paths broke frozen acceptance, subtree authority, and provenance. The
parent now prepares a nested node carrying its brief and frozen acceptance; the spawn request carries
only identity and the daemon derives the pointers.

**2026-06-07 — "All components pass" is not "the system exists."**
Every module worked; there was no production entrypoint, no resident listener, and no watchdog call in
the loop. Module-level green was demoted as evidence, replaced by assembled, loop-level checks of
autonomous behavior.

---

## Phase 2 — First contact with real substrate (June 11–16)

Once it ran, failures moved to the seams the mocks had smoothed over — and then to the harder
question of who is allowed to declare work finished.

**2026-06-11 — Mocks stopped counting as proof, and security was deliberately demoted.**
Real terminals, transcripts, models, and authentication broke confidence built on simulated substrate,
precisely at the joints simulation had normalized. Mocks stayed for speed but ceased to be evidence
that a production seam works. Separately, aggressive confinement was making agents burn attention
rediscovering ordinary runtime facts; low-friction protections stayed, and behavior-distorting
confinement was deferred with named triggers.

**2026-06-15 — Forward progress became review-owned.**
The month's largest doctrine shift. A producer's `DONE` no longer reaches its parent: the producer
submits a candidate, an independent review seat alone owns PASS, BOUNCE, or ESCALATE, and only PASS
creates parent-visible completion. Separate reviewer tokens and gate identities make the verdict
unforgeable and unsuppressable by its subject.

**2026-06-15 — Upper gates stopped re-running lower tests.**
Higher levels had been treated as more thorough versions of the level below. They were re-derived as
altitude-specific: lower gates establish local correctness, upper gates judge whether accepted
children compose into what was asked for at that altitude. Repeating a lower check became an exception
requiring a named evidence gap.

**2026-06-16 — Candidate identity was frozen and review got its own custody.**
Reviewers sharing a producer's workspace had overwritten the evidence they were judging. Reviews moved
into gate-scoped directories with their own launch surfaces, candidates gained content hashes, and
repair after PASS became a fresh incarnation against a fresh gate rather than a nudge to a terminal
child.

**2026-06-16 — Credentials are not files.**
Copying authenticated homes across seats assumed static secrets; refresh tokens actually rotate as
single-use credentials. One daemon-owned refresher under lock became canonical, and auth failure was
reclassified as infrastructure contamination rather than agent misbehavior.

---

## Phase 3 — Behavior becomes the object of design (June 17–21)

With the spine in place, the interesting failures stopped being mechanical. Agents were doing
defensible things the system had never asked for, and corrections moved from state machines toward the
surfaces agents actually read.

**2026-06-17 — Four channels, not one.**
Gate-owned forwarding had been so strict that ordinary parent-child conversation vanished. Completion,
blockage, ordinary guidance, and phase readiness were separated into distinct transports. Coordination
can wake and steer; it can never pass, freeze, or bypass review.

**2026-06-17 — The plan-alignment gate became a real phase transition.**
A validated plan had no honest route upward: signing `DONE` claimed the project was finished, and
escalating called normal progress a failure. A dedicated readiness route now lets the architect stay
alive, publish a validated-plan package, and wake L1 for a decision. Only PASS freezes interfaces and
opens execution.

**2026-06-17 — Node-local documents are slices of one logical project document.**
Physical isolation had been mistaken for semantic independence: a parent's interface change could
silently invalidate a child whose own files never changed. Consumption edges and freshness states were
introduced passive-first, so the system could learn whether the signals were trustworthy before they
became gates.

**2026-06-18 — Acceptance authoring split, and the tester seat was founded.**
Planning had been assumed to produce executable suites. Instead: the module designer produces
falsifiable criteria, a dedicated test-author seat turns frozen criteria into an executable package,
an independent review accepts it, and only then does implementation open against it. The tester is an
ordinary role variant, not a repair path.

**2026-06-18 — Launch packets replaced document loading.**
Handing agents important documents wholesale had been equated with helping them; it produced
harness-operator behavior and wrong-altitude wandering. Each role now gets the smallest startup surface
that makes its normal behavior locally obvious, everything else behind a "use this when…" map. Packets
are generated and hashed, so behavioral prompt changes are auditable.

**2026-06-19 — Upper-gate review was decomposed into seats.**
Review had been artifact-decomposed but cognitively monolithic — one reviewer writing four files. The
gate lead became an orchestrator dispatching independent check seats over fidelity, composition,
evidence credibility, and risk, each with a narrow fresh context. The lead synthesizes and alone signs.

**2026-06-21 — The research design was challenged and shrunk.**
A first behavioral-research architecture had grown into a governed ontology of evidence, episodes,
findings, threads, and authority tiers. A frame challenge found it coherent and premature: the real
problem was institutional learning under rotating researchers. Rigor was concentrated at the
consequential boundaries — claiming an intervention worked, promoting a belief into doctrine, changing
an agent's lived surface — and ordinary observation was left cheap.

---

## Phase 4 — The research function splits off (July 6–13)

Self-improvement had been an in-repo workspace. It became its own system, on the argument that a
factory grading its own improvements is the shortest path to self-confirmation. Around the same time,
workspaces moved out of the ambient instruction path so spawned seats stopped inheriting a human's
environment, and a canonical document protocol replaced accumulated ad hoc entry points.

**2026-07-07 — The Hypothesis Tree Research System was founded.**
Not an archive and not a knowledge base: a loop that repeatedly changes the factory and merges only
improvements that survive evaluation. It was founded against six long-horizon failure modes — claim
inflation, unusable documentation, path-dependent navigation, amnesia, wrong-task drift, and premature
stopping. The unifying diagnosis: every "what next?" is decided from a compressed map, and a map that
loses its evidence links becomes self-confirming.

**2026-07-07 — Director, unit, verifier — three separated powers, and a claim ladder.**
The director reads only compressed state and dispatches one leaf; a unit executes on a branch against
an ex-ante rubric; a fresh independent verifier adjudicates and alone writes epistemic state. Above
that sits the claim ladder: point facts, episode claims, and pattern claims are licensed by different
evidence, not by increasing confidence. Over-generalization is demoted and returned as a new
hypothesis rather than rejected, and negative findings count as successful outcomes.

**2026-07-07 — Invariants in machinery, not prose.**
The comparison with prior systems concluded that Markdown rules leak. Ratified invariants became
schema and transition checks in a write-gate tool backed by a pre-commit hook: field-level authority,
legal transitions, resolvable anchors, claim-tier monotonicity, write-once archives. An invariant that
could not be mechanized had to come back for an explicit ruling rather than quietly become advice.

**2026-07-07 — The trace reader was built first, as the evidence plane.**
Raw transcripts had proven a poor reasoning surface; agents produced vibe-based analyses over huge
interleaved logs. Extraction was split into a mechanical action stream, as-transcribed reasoning, and
reconstructed experience, with an anchored post-hoc record kept distinct from an explicitly uncitable
live view. Branch-awareness was not theoretical — a file-order extractor had already mixed abandoned
timelines into the apparent record.

**2026-07-12 — Fractal seats, shared spine.**
The single-lane engine could not see whole-factory cascades. Three tiers were added: whole-system
coordination on top, a decomposition axis in the middle, per-seat research engines below. Directors
and verifiers replicate per lane; the evidence substrate, trunk, epoch counter, and merge
serialization stay shared. A composition gate got full authority from the first merge, because
individually sound changes can still interact badly.

**2026-07-13 — The benchmark architecture defined "improved."**
Success was reframed as the factory's actual promise: unattended delivery that is functional and
faithful, without days of repair. **Acceptance debt** became the headline outcome, measured by a frozen
battery that walks behind the movie set — requirement-traced flows with sad paths, adversarial
discovery scoring only on scripted reproduction, structural probes. The arms never see the battery
during a run; the improvement loop sees every failure between runs. Trustworthy oracle access, not
code generation, is the binding constraint.

---

## Phase 5 — Autopsy, then re-derivation from mental ownership (July 16–23)

A mechanism-by-mechanism autopsy of a failed run overturned several sensible-looking choices. The
substrate was then re-derived from a different question than the one that had produced it.

**2026-07-16 — The harness had been writing into its own gate surface.**
A run that had already been reviewed and accepted livelocked at a composition gate: generated aggregate
logs inside the node workspace produced both malformed trace findings and candidate drift. Aggregates
left candidate space entirely, and a circuit breaker was added so three identical refusals pause loudly
instead of spinning over a finished product. The same autopsy found that dozens of apparent runtime
failures were two short provider-error storms, so error rows became causal incidents.

**2026-07-16 — A runtime unification was reversed within three days.**
Executors had been unified onto a single standing seat on a proxied model, on the argument that model
diversity mattered and runtime diversity did not. Live evidence disagreed: execution went back to
native Codex and the proxy machinery was removed rather than parked.

**2026-07-17 — The organizing question changed from cost to mental ownership.**
Rather than asking what is cheap to automate, the substrate was re-derived by asking who must
understand and own each decision. That settled what the daemon automates, what agents judge, and what
stays visible to L1 or the owner — including that the circuit breaker escalates through L1, the human's
proxy, rather than paging the human directly.

**2026-07-23 — Liveness became a deterministic read, not an inference.**
Runtime-native turn state became the floor, joined with a per-level task-DAG read over ledger state and
owed artifacts; the old evidence-inference detector was demoted to fallback. Every turn must resolve to
produced work, a ledger-derived wait, or a loud prod.

**2026-07-23 — One message primitive, one contract home, one notary.**
Several overlapping handoff channels collapsed into a single parent-child message that always wakes,
with durable open questions landing on the recipient's owed list. Contracts got exactly one home and
are never restated. A notary — stamp, receipt, check — consolidated all five separate freezing
mechanisms, making "which bytes were accepted, by whom, under which revision" mechanically answerable.
Messages persuade; they never amend.

**2026-07-23 — Build-to-claim adopted as testing doctrine.**
Proof obligations moved into the acceptance package: scenario spine, red-run logs, prove-at-the-face,
account sections, reviewers driving the artifact. The warm-verifier component was dropped — frozen
contracts make cold reviewers safe. Diversity was redefined as *runtime* diversity: an Opus 5.0
reviewer over a Sol executor.

---

## Phase 6 — The build burst (July 24)

One dense day converted the re-derived physics into production paths and explicit judgment cells. Two
of the additions are worth naming on their own.

**2026-07-24 — Plan alignment gained a deterministic spine and a blind semantic cell.**
Mechanically: every live requirement must reach design and test nodes, must-never-fail requirements
need explicit failure paths, orphaned trace elements fail with typed defects. Semantically: atomization,
two independent reconstructions, coherence, and a delayed comparator run as isolated actors that cannot
see each other, elevating only real contradictions or disagreement.

**2026-07-24 — Final fidelity moved back to the owner; authored escalation was retired.**
L1's judgment became explicitly preliminary, and promotion now requires an immutable owner playback
answer over frozen intent, evidence, and judgment bytes. Separately, a blocked actor no longer signs a
terminal escalation: it writes the substantive artifact, sends a canonical question, and parks.

---

## Phase 7 — Supervised operation (July 25 – August 1)

The system was judged complete enough that more isolated implementation would not answer the only
remaining question: whether the whole hierarchy behaves coherently in a real build. Runs became
commissioning instruments, watched by a director whose job was to treat breakage as evidence. Two runs
carry this phase: the fourth, which was eventually retired for parts, and the fifth, which every
milestone from July 28 onward concerns.

**2026-07-25 — "Looks alive" stopped being evidence; enforcement became an instrument.**
In the fourth run, panes stayed alive while agents sat invisibly at permission choosers and the ledger
said *running*. The director's doctrine tightened to *derive, don't trust*. In parallel, containment
stopped being treated as a thing that causes failures — it reveals violations already present, and each
correction was pushed into an existing seam rather than given new machinery.

**2026-07-28 — The run got its own director, and new mechanisms lost the benefit of the doubt.**
Rather than drag a heavily repaired run through delivery, the fourth was harvested for findings and a
clean fifth commissioned. Live supervision was necessary, but having the steering director tail every
event recreated context overload, so an acting director now owns in-run judgment while the steering
director keeps altitude, priorities, and four exceptional boundaries. Alongside it, a standing rule:
every new mechanism is observed on its first live run and judged on whether it fired, helped, and
produced false positives. Build the smallest functional mechanism; expand only on lived evidence.

**2026-07-28 — Behavioral laws: field-proven discipline delivered where action happens.**
Several failures in the fifth run were not missing state machines but locally obvious actions no surface
had asked for — most tellingly, a seat that invented its own rule to read its inbox as the second-to-last
act before signing, which then prevented a real stale sign-off on the very next cycle. Eight such laws
went into the generated role surfaces. Only one got deterministic enforcement, because prose could not
safely carry that boundary. A behavioral law is not a subsystem.

**2026-07-28 — The frozen-bytes law, because correct guards fired too late.**
The same misunderstanding appeared at both of the fifth run's integrity boundaries: a parent editing
already-frozen child briefs after ruling by message, and a module editing submitted artifacts during
review. The substrate rejected both correctly — but only after burning a gate cycle. The fix was
documentation, not another freeze mechanism.

**2026-07-28 — Blocked-on-input was re-derived instead of timed out.**
A live seat spent hours at a permission chooser while appearing to be inside a tool call. Silence plus
an in-flight tool plus zero descendant CPU looked sufficient, but that also matches a legitimate network
wait. The final detector requires four agreeing facts, and the response is deliberately minimal: one
capability-reducing keystroke, write-ahead recorded so a crash cannot repeat it, then escalation rather
than a second attempt. Building it also exposed that generation-preserving log rows could be silently
skipped during replay — a class defect, repaired at the shared seam rather than routed around.

**2026-07-31 — Recovery must recover the observers, too.**
A host reboot destroyed the daemon, the terminal server, and every seat while the fifth run was parked.
Replay reconstructed the tree, frozen stamps, and pause state from an empty
process world — but respawned seats inherited stale turn-event cursors, leaving the control plane blind
to their new incarnations. Recovering actors is not enough.

**2026-08-01 — The escape hatch: hold and tell someone.**
A single silent client blocked the daemon's accept loop for fourteen hours, leaving the fifth run
nominally active while everything watching it went blind. Beyond bounding the read, this broke the
assumption that
an unattended run should continue as long as agents are alive. Seats now stop retrying the same substrate
failure, the daemon withholds new wakes while its own control socket is unhealthy, and true quiescence
escalates to a notification and a lossless pause. Hard budgets and rate caps were rejected — they fight
the goal. The strongest automatic action remains holding and telling someone.

**2026-08-01 — Real self-diagnosis, and the honest limit of that claim.**
Late in the fifth run, the hierarchy detected that L1 had replaced a frozen intent specification with a
delta, refused to propagate the corruption, recovered the original bytes from a frozen launch artifact,
verified them
against the recorded fingerprint, and documented the trap — unassisted. It also diagnosed that its own
review loop had no convergence pressure: every cycle added design detail and nothing forced the
transition to code. It raised that question itself. But director-owned seats still changed the harness
code. This is harness-assisted improvement, not self-modification.

---

## Where it stands

The design is complete and hardened. The runtime is built: a run-scoped daemon boots the hierarchy on
pinned Claude Code and Codex runtimes, reads runtime-native turn state, survives daemon and host loss,
and enforces the gates deterministically. Supervised live runs are real, and earlier runs have delivered
working software end to end.

What has not been proven is the thing the system exists for. The fifth commissioned run — the latest —
reached the end of the design phase across every module and produced no product code, because the review
architecture ratcheted elaboration rather than creating pressure to build. That finding — with model
tiering, a daemon-side progress predicate for seats that loop hotly while producing nothing, and the
untested build-side path — is the open work.

The research system is at v0: its state model, write gate, trace reader, federated ledger, composition
gate, and role packets are built; its macro architecture and benchmark program are designed. It is not
yet improving the factory. It watches, it can adjudicate, and it has not yet closed the loop.
