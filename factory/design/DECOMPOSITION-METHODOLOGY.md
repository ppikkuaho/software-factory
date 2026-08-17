# AI Architecture — Decomposition Methodology

The carving method L2 and L3 use to turn intent into a buildable structure. This is the *single home* for how the system decides where one thing ends and the next begins — module boundaries, interfaces, dependency direction, and build order. DESIGN-PRINCIPLES.md carries only a principle-altitude summary and points here; PROJECT-PLANNING.md references this doc rather than restating it. (This corrects the prior false pointer in ARCHITECTURE.md item 15, which claimed the method lived in PROJECT-PLANNING.md — it didn't.)

For V1 the carving target is **software-building**. The method is general, but its worked examples and the quality rubric below are tuned to building software systems; broader task types are a post-V1 destination, not a V1 claim.

**Where this sits in the cascade.** Decomposition is the core cognitive work of the design cycle (see PLAN-ALIGNMENT-GATE.md). L2 carves the project into a module portfolio + interface contracts; planning-L3s (L3&) carve each module into a buildable design; L4 carves each module's workstreams into tasks. The same method runs at each level, on a smaller scope. This doc is the method; QUALITY-GATE.md reviews the *output* of applying it; PLAN-ALIGNMENT-GATE.md validates the *assembled whole* against intent before any of it is built.

A note on what is backbone vs. what is rubric, because it is the central correction this doc makes (L47):

- **The backbone — how you actually carve (B5)** — is **C4 nesting + DDD seam-finding + the SDD fidelity spine + hexagonal ports** (WBS + vertical slices at execution). That is the load-bearing method.
- **Deep modules is the *quality / review rubric*** — the test you apply to a proposed carving to judge whether it's good, *not* the procedure that generates the carving. The vocabulary stays; its job is demoted from backbone to rubric (B4, L47).

Mixing those two up — using "make modules deep" as if it were a decomposition algorithm — produces the classic failure of staring at a depth ratio with no method for finding the seams. Find seams with the backbone; *then* score them with the rubric.

---

## Part I — The Backbone (how you carve)

### 1. C4 Nesting Is the Frame

The decomposition is hierarchical and self-similar: system → containers → components → code, each level a zoom of the one above. C4 gives the altitudes; the level structure (L2 carves the system, L3 carves a container/module, L4 carves components into tasks) maps onto C4 zoom levels. Each zoom is *the same carving question asked at a smaller scope* — which is why one method serves every level.

The C4 frame also fixes what an artifact at each altitude must answer. A higher-altitude carving names boxes and the lines between them and stops; it does *not* reach down and specify the inside of a box, because that's the next zoom's job and the next agent's context. This is the structural form of subsidiarity (Part I.10) and of pointer-not-payload briefs (see PLAN-ALIGNMENT-GATE.md): each level resolves only its own altitude and defers the inside downward with constraints.

**Implication:** Every design artifact declares its C4 altitude. An L2 artifact is a container diagram with contracts, not a class design. An artifact that mixes altitudes (a system map that suddenly specifies a function signature) is a carving smell — it means a boundary wasn't trusted to hold.

### 2. Carve at the Seams, Not the Boxes — Connections Over Boxes (B6)

Boxes are cheap; the connections between them are what cost you. A decomposition is good or bad *because of its interfaces*, not because of its boxes. So carve where the connection is **thin** — where the least information has to cross — and you get a boundary that's cheap to define, cheap to hold, and cheap to change behind.

The unit of judgment is therefore the *cut*, not the *region*. When evaluating two candidate decompositions, compare the interfaces they imply: the one whose boundaries carry less traffic, fewer shared assumptions, and narrower contracts is the better carving even if its boxes look less tidy.

**Implication:** Draw the lines first, then the boxes. When a boundary's interface is fat — many methods, shared mutable state, leaking internals — that boundary is in the wrong place. Move it to where the cut is thin.

### 3. DDD Is the Seam-Finder (B7)

Where *is* the thin cut? DDD answers it: **carve by co-change and where the language changes.** Two concerns belong on the same side of a boundary when they change together (high coupling, carve to *contain* the change) and on opposite sides when the ubiquitous language shifts — when "order" means one thing to fulfillment and another to billing, that linguistic seam is a bounded-context boundary. The target of every cut is to **isolate change**: a well-placed boundary means a future change lands inside one module and stops at its interface.

Co-change is the empirical signal (things that historically change together); language-shift is the conceptual signal (the same word means different things on each side). When they agree, you've found a strong seam. When they disagree, prefer the language seam and watch the co-change for drift.

DDD is the **carving sub-method inside L2's architect-process** (see runtime-and-model-map.md and the architect-process in the consolidation log) — it finds the bounded contexts; it does not by itself decide which decisions are architecturally significant or which to defer. It's a seam-finder, not the whole architecture.

**Implication:** Justify every module boundary in DDD terms: *what changes together that this contains, and what language-shift it sits on.* A boundary that can't be justified either way is arbitrary and should be redrawn or dissolved.

### 4. The SDD Fidelity Spine

Spec-Driven Development supplies the fidelity spine that threads the whole cascade: the spec at each altitude is the testable claim the level below must satisfy, and the chain of specs is the trace from intent down to code. The WBS 100%-rule applies — a decomposition must account for *all* of the parent's scope, no more and no less — which is exactly the forward/backward coverage the plan-alignment gate later verifies mechanically. Carving and traceability are the same act: every child boundary you draw carries a slice of the parent's spec, tagged with the requirement ID it serves (see PLAN-ALIGNMENT-GATE.md, Requirements Traceability).

This is why decomposition can't be a private act of taste. Each cut emits a trace-block; the union of cuts must cover the parent's spec exactly (100%-rule) with no orphan boxes (backward coverage) and no dropped scope (forward coverage). The fidelity spine is what makes the gate's coverage checks possible at all.

**Implication:** A carving is only finished when every piece of the parent spec lands in exactly one child, and every child traces up to some piece of the parent spec. Uncovered scope = a gap; uncovered box = scope creep. Both are carving defects, caught at design time.

### 5. Hexagonal Ports Define the Boundaries

Once a seam is found, it is realized as a **port**: the core defines the socket (the interface it needs), and the outside world supplies adapters. Domain logic depends only on ports it owns; infrastructure, transports, and peers are adapters plugged into those ports. This is what keeps the dependency arrow pointing the right way (Part I.6) and what makes the same brief runnable across runtimes (the runtime envelope is an adapter — see runtime-and-model-map.md / E32).

Ports also separate the two things people conflate: the **contract** (the port — what's exchanged, the semantic interface) and the **transport** (the adapter — how it's carried). Carve and stabilize the contract; let the transport be swappable. (See Part III on transport-vs-contract and COMMUNICATION.md, where the same split governs the bus.)

**Implication:** Every module exposes ports it defines, not interfaces imposed by its callers. If a module's interface is shaped by who calls it rather than by what it is, the dependency arrow is backwards — invert it (define the port in the core, adapt on the outside).

### 6. Dependencies Point Toward Stability — the Sun (B8)

Arrange modules so dependencies flow **toward stability**. The stable, abstract, slow-to-change things are the "sun" everything orbits; the volatile, concrete, fast-changing things are on the outside depending inward. **Never put something volatile at the center** — if the thing everyone depends on changes constantly, every change radiates outward through the whole system.

Read this off the dependency graph: high **fan-in** (many depend on it) demands high stability; high **fan-out** (it depends on many) marks a volatile leaf, which is exactly where volatility *should* live. A volatile high-fan-in node is the single worst structural defect — it's a volatile sun.

**Implication:** Before locking a decomposition, draw the dependency arrows and find the center of gravity. If anything volatile has high fan-in, the carving is unstable — extract the stable abstraction the dependents actually need (a port), let them depend on *that*, and push the volatile concretion to a low-fan-in leaf.

### 7. Rich Inside, Thin Across — Fractal Membranes (B10)

Every boundary is a **membrane**: rich behavior inside, thin contract across. A module hides a lot and exposes a little. This holds **fractally** — it's true of the system boundary, of each container, of each component, of each function. At every zoom the same shape recurs: dense internal cohesion, narrow external surface.

This is the same membrane DESIGN-PRINCIPLES P5 (Context Isolation via Process Boundaries) describes for *agents* — context rich inside a process, compressed across the boundary. Module membranes and agent membranes are the same idea at different layers: the workspace tree, the module tree, and the agent tree are one hierarchical spine (see WORKSPACE-SCHEMA.md and the one-path-spine unification), so a thin module interface and a thin inter-level contract reinforce each other.

**Implication:** At every boundary, ask "how much is crossing?" If a lot crosses, the membrane is too permeable — either the boundary is misplaced (Part I.2) or too much internal detail is leaking out (tighten the port). The interface should be the smallest thing that lets the outside use the inside.

### 8. Conway: Module ≠ Work ≠ Org — and the Substrate (B14)

Three decompositions are *different* and must not be conflated:

- the **module** decomposition (how the running system is structured),
- the **work** decomposition (how the build is sequenced — WBS, tasks),
- the **org** decomposition (which agents own what).

Conway warns that the org you spawn will imprint its shape on the system you build, so align them deliberately rather than by accident — but they are not the same carving and a clean module boundary is not automatically a clean work package or a clean ownership boundary.

**Cross-cutting concerns get a named context — the substrate (B14, resolved).** When a concern (money, IDs, events, audit, idempotency, the base data model) cuts across many feature areas, do *not* smear it through them. Name it explicitly as a **platform/foundation context — the substrate** — a stable core that L2 establishes **before** the feature areas, built first via the walking skeleton (Part II.9). The substrate is **not a peer feature module**; it's the sun (Part I.6) the features orbit. This is the resolution of B14: the foundation = a substrate L2 stands up first, the stable core every later area depends inward toward.

**Implication:** Maintain the three decompositions as distinct views. When a concern won't sit cleanly in any feature module because it's everywhere, that's the signal to lift it into the named substrate context and build it first — not to duplicate it across modules or bolt it onto whichever module touched it first.

---

## Part II — Build Order (how you sequence the carving into existence)

### 9. Walking Skeleton First; Design ≠ Build; Build by Dependency/Risk; Contract-First (B15)

Carving produces a *design*; building turns it into a system. Keep these distinct (design ≠ build is the design-cycle / build-cycle separation in PLAN-ALIGNMENT-GATE.md — design is gated *before* any building starts).

Build order, once the gate clears the plan:

- **Walking skeleton first.** Build the thinnest end-to-end thread that exercises every major boundary — one trivial path through the whole structure — before fleshing out any module. It proves the *connections* (the expensive part, Part I.2) early and de-risks the architecture. The substrate (Part I.8) is what the skeleton stands on, so the skeleton is also how the foundation gets built first.
- **The walking skeleton is a de-risking *spike*, not a gated increment.** It runs early, ungated, to prove the connections — distinct from the gated execution that follows. Keep the two cleanly delineated: the skeleton answers "do the boundaries connect?"; gated execution answers "is each module built right?" (M55). Don't let the skeleton smuggle in real feature work, and don't subject the spike to the full gate.
- **Build core-out, by dependency and risk.** After the skeleton, flesh out from the stable center toward the volatile edges, sequencing by what's depended-upon (build it before its dependents) and by what's risky (build the scary part early, while it's cheap to be wrong).
- **Contract-first.** Freeze the port (the contract) before building either side of it, so the two sides can proceed in parallel against a stable interface. Interfaces are **fluid during the planning cascade and frozen for execution** (see Part III.13 and the provisional-interface hardening in the architect-process): L2 proposes coarse ports, planning-L3s pressure-test and renegotiate them upward, the walking skeleton runs *early on the provisional ports* (surfacing gaps while they are still cheap to change — not validating settled ones), they are **candidate-locked** at the compatibility review, and they **freeze only after the plan-alignment gate PASSes** — never at the close of the cascade.
- **Pressure-test the contract against execution reality before freeze — enums and ports especially.** A concept-altitude interface contract is not freezable on the strength of a prose disclaimer that it is "provisional." Before it freezes, the contract — and in particular its **enums and its ports** — must be *exercised* against the real execution surface it will meet: the async flows it must carry and the real keyspaces/identifiers it must address. Two concrete failure modes the pressure-test exists to catch: (a) an **outcome enum that cannot express a real runtime state** — e.g. an order-outcome enum with only `accepted`/`rejected` that has no way to say "accepted, pending confirmation," a state the async confirmation flow actually produces; and (b) a **missing port** — e.g. no intent→order mapping port for an inbound webhook to resolve which order an event belongs to. Both are invisible to prose review and surface only when the contract is run. The exercising agents are the **walking skeleton** (drives the thinnest end-to-end thread through the enum's states and the ports' calls) and the **planning-L3 renegotiation** (which feeds discovered gaps back up the cascade per Part III.13). **Observable rule:** an interface whose enums cannot represent the real execution states it will encounter, or whose ports are missing for a flow that must traverse the boundary, is **not freezable** — the gate must see a contract that has been exercised against async flows and real keyspaces, not one merely annotated "provisional." A contract that has not been pressure-tested in this way is treated as still reflect-back-pending and must not be frozen.

**Implication:** Never build a module fully before the skeleton proves its boundaries connect. Sequence construction by dependency and risk, not by what's easy or interesting. Freeze contracts before building against them — but only *after* their enums and ports have been exercised against real async flows and keyspaces by the walking skeleton and planning-L3 renegotiation, never on the strength of a "provisional" label alone. Renegotiate contracts *up* the cascade during planning, never silently during execution.

---

## Part III — Interfaces, Routing, and Ownership

### 10. Interfaces = Core-Defined Sockets + Many Adapters; Talk Only Through Interfaces (B16)

A module's interface is a **socket it defines** (Part I.5), and the world plugs **many adapters** into it. Components talk to each other **only through declared interfaces** — never by reaching into internals, sharing mutable state, or assuming another module's implementation. This is what makes the membrane real rather than nominal.

**Transport vs. contract, again:** the *contract* is the socket (semantic, stable); the *transport* is how a call is carried (in-process call, bus message, network hop — swappable). A shared transport (e.g. the bus, see COMMUNICATION.md) is *optional plumbing*, not part of any module's contract. Don't bake the transport into the contract; a module shouldn't know or care whether its caller is local or remote.

**Implication:** If module A knows anything about module B beyond B's declared interface, the boundary is breached — fix it. Keep transport choices out of contracts so transports can change without renegotiating interfaces.

### 11. Last Responsible Moment + Subsidiarity (the deferral rule)

Decide **cross-module and expensive** things **now**; defer **module-internal and domain-deep** things **downward, with constraints**. This is the architect-process deferral rule (the L2 methodology — see the consolidation log and runtime-and-model-map.md) expressed as a carving discipline: a higher level resolves only what it's uniquely positioned to resolve (the boundaries and contracts that bind the pieces together) and pushes the inside down to the level that will load the domain depth to do it well.

A deferred decision is **not dropped** — it goes down as a *constraint* in the child's spec (the D26 rubric the child is held to: a frozen, per-unit, read-only-to-the-executor artifact in the work node). "Defer with constraints" is what keeps deferral from becoming abdication.

**Implication:** For each decision in front of you, ask "is this cross-boundary/expensive-to-reverse, or internal/cheap-to-defer?" Resolve the former; emit the latter as a constraint in the child's frozen rubric. Don't resolve domain-internal details you'd have to load a whole domain to do well — that's the child's altitude.

### 12. Router vs. Direct (B17)

**Default to direct calls through interfaces.** A router (a hop that decides where a message goes) earns its place **only at boundaries** — process / network / runtime boundaries, or where dispatch is genuinely **dynamic or asynchronous** (you don't know the target at carve time, or the call must be decoupled in time). Everywhere else, an indirection just adds a place for bugs to hide and a contract to maintain.

When a router *is* warranted, **keep it dumb.** A router routes — it looks at an address and forwards. It does not own data, transform payloads, make business decisions, or accumulate logic. The moment a router starts *deciding* things beyond where to send a message, it has become a hidden module with no clear boundary.

This is the carving-side rule that pairs with COMMUNICATION.md's bus: the bus is the runtime router (dynamic, async, cross-process) and it is deliberately dumb — it carries pointers/nudges, while truth lives in docs.

**Implication:** Don't introduce a router for an in-process, statically-known call — call the interface directly. Do introduce one at a runtime/network/async boundary — and then guard it: if the router grows logic, extract that logic into a real module and shrink the router back to forwarding.

### 13. Hub-as-Router OK vs. Hub-as-Central-Store Anti-Pattern (B18)

A **hub that routes** is fine (it's a dumb router, Part III.12). A **hub that owns everyone's data** — a central store every module reads and writes through — is an anti-pattern: it becomes a volatile high-fan-in sun (Part I.6), couples every module to every other through shared state, and destroys the membranes.

The discipline:

- **Separate routing from ownership.** Routing is "where does this go"; ownership is "who holds the truth for this." A hub may do the first; it must never become the second.
- **Single source of truth.** Each piece of state has exactly one owning module. Others get it through that owner's interface, not by reading a shared blob. (This is the same single-source-of-truth that makes docs-as-truth work in COMMUNICATION.md / F33.)
- **Separate queries from commands.** Reads and writes are different shapes with different consistency and different audiences; carving them apart keeps interfaces honest and ownership clear.

**Implication:** When you see a module that everyone reads and writes, stop — that's a central-store hub forming. Give each piece of state a single owner, route through interfaces, and split query paths from command paths. A hub may forward; it may not hoard.

---

## Part IV — The Quality / Review Rubric (deep modules — score, don't generate)

These are the tests you apply to a candidate carving produced by Parts I–III. They are the **rubric, not the backbone** (B4, L47): they tell you whether a carving is good, not how to find it. They are also what QUALITY-GATE.md's architectural-fit and interface-contract dimensions check at the L2/L3 gates.

### 14. The Depth-Ratio Test; Collapse Shallow Levels (B9)

A **deep module** has a large ratio of hidden internal complexity to exposed interface surface — much capability behind a small contract. A **shallow module** exposes nearly as much as it hides; its interface is as big as its insides, so the boundary buys you almost nothing.

Score each module by its depth ratio (roughly: how much is inside vs. how wide is the contract). **Collapse shallow levels** — a module or a hierarchy level whose interface ≈ its contents is pure overhead; dissolve it and inline its contents into its neighbor or parent. (This is the module-level twin of DESIGN-PRINCIPLES P15 variable-depth and P3 "if a level adds no kind-of-thinking, it shouldn't exist" — and of the threshold-gated planning-L3 split, M53, where trivial modules may use one L3 owner for both design and execution management while keeping the L4/L5 implementation spine.)

**Implication:** For every module and every nesting level, ask "does this boundary hide more than it exposes?" If not, collapse it. Depth is the payoff of a boundary; a boundary with no depth is a tax.

### 15. Boundary-Pays-For-Itself — Glob vs. Confetti (B11)

Every boundary has a cost (a contract to define, hold, and cross) and must **pay for itself** in isolation bought. Two opposite failures:

- **Glob** — too few boundaries: one giant module that hides nothing because everything's tangled inside it. No membranes, no isolation, change radiates everywhere.
- **Confetti** — too many boundaries: the logic shredded into so many tiny modules that the *connections* (Part I.2) dominate, and you can't follow a single behavior without hopping through twenty interfaces.

The right granularity is where each boundary buys more isolation than it costs in interface. Below that, you're confetti; above it, you're glob.

**Implication:** Count the boundaries against the isolation they buy. If a boundary isolates no independent change, it's confetti — remove it. If a module isolates nothing because it's everything, it's a glob — split it at the next thin seam. Carve to the granularity where boundaries pay rent.

### 16. Cohesion = Name It Honestly; the Utils Magnet (B12)

A module is cohesive when you can **name it honestly** — one true name that describes everything inside it and excludes everything outside it. If the honest name is "stuff" or "helpers" or a list joined by "and," the module isn't cohesive; it's a bag.

The canonical failure is the **utils magnet**: a `utils`/`common`/`shared` module that attracts anything nobody bothered to home properly, growing into a high-fan-in junk drawer that couples the whole system. The honest distinction: a genuine utility is **generic, not merely infrequent** — it's used by many because it's truly general (string formatting, a math primitive), not dumped there because it was hard to place. Infrequent-but-domain-specific code belongs in its domain module, however small.

**Implication:** Name-test every module: one honest name that fits all of it and none of what's outside. Watch every `utils`/`shared`/`common` module as a standing risk — audit it for things that have a real home, and admit only the genuinely-generic. An "and" in a module's honest name is a split signal.

### 17. Misfits Are Information — the Taxonomy (B13)

When a piece of work won't fit cleanly into the carving, the misfit is **information about the carving**, not noise to suppress by jamming it somewhere. Read the misfit:

- **Doesn't fit any module** → a missing boundary; the carving lacks a context this work needs (often the substrate, Part I.8).
- **Fits several modules / spans them** → a cross-cutting concern; lift it into a named context (substrate or a new bounded context) rather than duplicating it.
- **Forces a fat interface to fit** → the boundary is misplaced; the thin cut is elsewhere (Part I.2).
- **Keeps changing the same two modules together** → those two should be one (co-change, Part I.3).
- **Lands in the utils magnet** → it has a real home you haven't named yet (Part IV.16).

**Implication:** Treat every "where does this go?" struggle as a diagnostic, not an annoyance. The thing that won't fit is telling you where the carving is wrong. Re-carve in response to misfits instead of absorbing them into the nearest module and hiding the signal.

---

## How This Doc Relates to the Others

- **DESIGN-PRINCIPLES.md** — carries a short principle-altitude summary of this method (deep-modules-as-rubric; carve-at-seams; dependencies-toward-stability; build-skeleton-first) and points here for the procedure. The principle-level statements (P3 kinds-of-thinking, P5 membranes, P15 variable depth, P17 right-size) are the *why*; this doc is the *how*.
- **PROJECT-PLANNING.md** — references this doc as L2/L3's carving method instead of restating it (correcting ARCHITECTURE.md item 15's stale pointer).
- **PLAN-ALIGNMENT-GATE.md** — consumes the output of this method: the fidelity spine (Part I.4) is what its forward/backward coverage and RTM verify; design ≠ build (Part II.9) is its dual-cycle split; the frozen contracts (Part II.9, III.13) are what it locks at the gate.
- **runtime-and-model-map.md** — the architect-process (significant-decisions / LRM / subsidiarity / defer-with-constraints / patterns / spikes) is the wrapper L2 runs this method inside; DDD (Part I.3) is the carving sub-method within it. The hexagonal port (Part I.5) is also the seam where the cross-runtime brief's runtime envelope plugs in (E32).
- **WORKSPACE-SCHEMA.md** — the module tree this method produces *is* the workspace tree / agent-address tree / git-branch tree (the one hierarchical-path spine). A module boundary, a work node, an agent address, and a rubric location are the same boundary at different layers.
- **COMMUNICATION.md** — the transport-vs-contract split (Part I.5, III.10) and the dumb-router rule (Part III.12–13) are the carving-side basis for the bus: the bus is the optional, dumb, async transport; module contracts and docs-as-truth are the durable layer.
- **QUALITY-GATE.md** — the Part IV rubric is what the L2/L3 gate's architectural-fit and interface dimensions score; the gate reviews carvings, it doesn't produce them.
- **agent-definition-principles.md** — the membrane idea (Part I.7) is shared between module boundaries and agent boundaries; both are thin-across, rich-inside.

---

*Created: 2026-06-02*
*Source: consolidation of the B-series (B4–B18) + L47 from the 2026-06-02 design conversation; see working-notes/consolidation-plan-2026-06-02.md (WS1).*
*Status: Living document — the single home for the system's decomposition method. Backbone = C4 + DDD + SDD + hexagonal ports; deep-modules = quality rubric.*
