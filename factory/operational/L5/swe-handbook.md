# Software Engineering Practices Handbook

Compiled from professional software engineering resources. Loaded at spawn as a reference for craft execution. This is how good engineers write code — not rules to memorize, but principles to internalize.

**Spec-faithfulness is the #1 criterion at L5.** Code elegance is real and worth pursuing, but it is secondary. The first question on every implementation decision is: *does this code do exactly what the spec says?* A plain solution that passes all acceptance tests is a success. A beautiful solution that misses the spec is a failure. All the principles below operate within that constraint.

---

## The Compass: Four Rules of Simple Design

*From Kent Beck, documented by Martin Fowler.*

In priority order:

1. **Passes the tests.** Whatever else is true about the code, it must work as intended. Tests guarantee this.
2. **Reveals intention.** Code exists for human readers. Your purpose should be clear to someone encountering the code for the first time.
3. **No duplication.** Everything stated once and only once. Duplication is a signal that an abstraction is missing.
4. **Fewest elements.** Remove anything that doesn't serve the first three rules. Don't add elements for speculative future needs.

When in doubt about a design decision, check these four in order.

---

## Code Structure

### Functions

- **Small.** A function should do one thing and fit on a screen.
- **Do one thing.** If you can describe the function with "it does X *and* Y," it does too much.
- **Descriptive names.** The name should tell you what the function does without reading the body.
- **Few arguments.** Prefer fewer. Each argument increases cognitive load and testing combinations.
- **No side effects.** A function that claims to do one thing but quietly changes something else is lying about what it does.
- **No flag arguments.** A boolean parameter that changes behavior means the function does two things. Split it.

### Naming

- **Choose descriptive and unambiguous names.** A name should tell you why something exists, what it does, and how it's used.
- **Make meaningful distinctions.** If names differ, they should mean different things. `data` and `info` as suffixes add nothing.
- **Use pronounceable, searchable names.** You will talk about this code and search for it.
- **Replace magic numbers with named constants.** The number means nothing; the name means everything.
- **No encodings or prefixes.** Hungarian notation and type prefixes are noise.

### Source Organization

- **Separate concepts vertically.** Blank lines between distinct ideas.
- **Related code should appear together.** Functions that call each other should be close.
- **Declare variables close to usage.** Not at the top of the function — near where they're used.
- **Place functions in the downward direction.** The caller above the callee. Read like a narrative.
- **Keep lines short.** If a line needs scrolling, break it.

---

## Design Principles

### Complexity is the Enemy

*From John Ousterhout, "A Philosophy of Software Design."*

Complexity manifests as:
- **Change amplification** — a simple change requires modifications in many places.
- **Cognitive load** — how much a developer must know to work with the code.
- **Unknown unknowns** — it's not clear which code needs to change or what you need to know.

Root causes are **dependencies** (one piece can't be understood without another) and **obscurity** (important information isn't obvious).

### Deep Modules, Not Shallow Ones — Code-Altitude Quality Rubric

Deep modules is a **code-quality rubric** for the work you write. It is not a system decomposition methodology — for how the system as a whole is carved into areas, modules, and boundaries, see `design/DECOMPOSITION-METHODOLOGY.md` (that's what L2/L3 use; it is the system-level decomposition backbone). At L5 your concern is the quality of the code in front of you, and deep-modules gives you three concrete tests for that.

**The rubric:**

- **Depth-ratio test.** A module's value = capability it hides / interface complexity it exposes. A shallow module — complex interface, little internal capability — is a net cost to the system. Ask: does this module's interface pay for itself? If the caller needs to know almost as much about the internals as if they'd just written it themselves, the abstraction isn't earning its keep.
- **Glob-vs-confetti.** Prefer one module that hides substantial capability over many tiny fragments whose accumulated interfaces, connections, and boilerplate cost more than the "simplicity" of each piece. Confetti modules (many tiny, shallow things) inflate cognitive load. When you find yourself creating a module whose entire job is to call one other module, ask whether it should exist at all.
- **Boundary-pays-for-itself.** Every boundary you introduce has a carrying cost: a new interface to understand, a seam to maintain, an indirection to trace. A boundary earns its keep when it hides a real decision behind it — information hiding. If crossing the boundary requires knowing what's on the other side anyway, the boundary isn't hiding anything and doesn't pay for itself.

**The reconciliation with small functions:** keep individual functions focused and small, but organize them into modules that are deep — hiding substantial capability behind a simple interface. Small functions *within* deep modules.

(Vocabulary from Ousterhout, *A Philosophy of Software Design*. L47 — criterion corrected from "backbone" to "code-quality rubric"; system decomposition backbone lives in `design/DECOMPOSITION-METHODOLOGY.md`.)

### Information Hiding

Each module should encapsulate design decisions. When a decision changes, only one module needs to change. If a design decision leaks across multiple modules, you've created hidden dependencies.

**Information leakage** — when knowledge spreads between modules without obvious connections — is the most dangerous form. Watch for it.

### Define Errors Out of Existence

Instead of throwing exceptions for edge cases, redesign the interface so the edge case isn't exceptional. If `unset(variable)` throws when the variable doesn't exist, redefine it as "ensure the variable no longer exists." Now the "error" is just normal operation.

Exceptions contribute disproportionately to complexity. Handle them by making them unnecessary, not by adding more error-handling code.

---

## Comments

Code speaks for the WHAT. Comments speak for the WHY.

- **Don't comment what the code does.** If the code needs a comment to explain what it does, the code isn't clear enough. Rename, extract, simplify.
- **Do comment why it does it that way.** Design intent, alternatives considered, constraints that shaped the decision. Code cannot express reasoning — comments can.
- **Don't be redundant.** A comment that restates the code is noise.
- **Don't comment out code.** Delete it. Version control remembers.
- **Use comments as warnings.** If something will break under certain conditions, say so.

Write comments before or during implementation — not after. They clarify your thinking about the abstraction, not just document it.

---

<!-- block:claim-proof-account v1 -->
## Build to the Claim — Fix It, Drive It, Account for It, Stop

For implementation work, your claim is the smallest recipient-visible increment named by your
decision-complete brief. Do not silently “fix the claim yourself.” If the claim, its boundary, or
its acceptance is ambiguous, send L4 a direct-edge message with `needs_answer: true` and hold the
affected work until the answer lands.

The artifact's **face** is part of the claim: commands, options, documented examples, emitted
fields, errors, and existing entry points are promises the recipient will act on. Keep that face no
larger than the claim you can prove completely.

Prove the claim where it lives. Drive the real artifact through the package's live-scenario
invocation on a disposable instance and observe the recipient-visible result. Green tests,
compilation, types, and code reading are evidence inputs; they are not substitutes for watching the
artifact do the promised thing. Re-walk existing entry points against every new state this
increment creates.

Stop when the claim is proven. Reopen it only when a promise on the artifact's face is broken or an
existing public entry point misbehaves on a state this increment created. Every other finding is
**inventory owned by L4**: record it; do not fix it in this diff. In `report.md`, keep the account
explicit under the exact headings `## Drove and Watched`, `## Inferred`,
`## Residual Uncertainty`, and `## Inventory`.
<!-- /block:claim-proof-account -->

## Testing

- **One assert per test.** Each test verifies one thing. When it fails, you know exactly what broke.
- **Readable.** Tests are documentation. Someone reading a test should understand the expected behavior.
- **Fast.** Slow tests don't get run. Run them constantly.
- **Independent.** Tests should not depend on each other or on execution order.
- **Repeatable.** Same result every time, in every environment. No randomness, no external dependencies.
- **Test edges, not just the happy path.** Boundaries, empty inputs, unexpected types, error conditions. The happy path is easy; the edges are where bugs live.

---

## Error Handling

- **Handle errors explicitly.** Don't swallow exceptions silently. Either handle them meaningfully or let them propagate with context.
- **Fail fast.** When something is wrong, detect it as early as possible. Don't let bad state propagate.
- **Add context to errors.** An error message should tell you what went wrong, where, and what was expected. "Error" is not a message.
- **Don't use exceptions for control flow.** Exceptions are for exceptional conditions, not for normal branching.

---

## Code Smells

Signs that the code needs attention:

- **Rigidity.** A small change causes a cascade of changes elsewhere. The code resists modification.
- **Fragility.** The code breaks in unexpected places when you change something. Unrelated things are coupled.
- **Immobility.** You can't reuse a piece elsewhere because it's entangled with its context.
- **Needless complexity.** Structure or code that serves no current purpose. "We might need this later" — you won't, and if you do, you'll build it differently.
- **Needless repetition.** The same logic in multiple places. A missing abstraction.
- **Opacity.** The code is hard to understand. It takes effort to figure out what it does or why.

---

## Self-Review Before Submitting

*Adapted from Google Engineering Practices.*

Before reporting work complete, review your own code against these dimensions:

1. **Design** — Does the code structure make sense? Are the interactions between pieces logical?
2. **Functionality** — Does it do what the brief asked for? Consider edge cases and error conditions.
3. **Complexity** — Can it be understood quickly by someone else? Is anything over-engineered?
4. **Tests** — Are they correct, meaningful, and covering the right things? Would they fail if the code broke?
5. **Naming** — Do names communicate purpose clearly?
6. **Comments** — Do they explain WHY, not WHAT? Are they present at decision points?
7. **Style** — Does it follow conventions.md and the formatter output?

---

## Sources

- Kent Beck, *Four Rules of Simple Design* (via Martin Fowler): https://martinfowler.com/bliki/BeckDesignRules.html
- Robert C. Martin, *Clean Code* (summary): https://gist.github.com/wojteklu/73c6914cc446146b8b533c0988cf8d29
- John Ousterhout, *A Philosophy of Software Design* (summary): https://carstenbehrens.com/a-philosophy-of-software-design-summary/
- Google Engineering Practices: https://google.github.io/eng-practices/

---

*Compiled: 2026-03-26*
*Updated: 2026-06-02 — Added spec-faithfulness as #1 criterion (preamble). Updated "Deep Modules, Not Shallow Ones" from simple note to full code-quality rubric (depth-ratio test, glob-vs-confetti, boundary-pays-for-itself); clarified it is a code-altitude quality rubric, not the system decomposition backbone (which lives in design/DECOMPOSITION-METHODOLOGY.md). B4/B9/B11/B12/L47.*
