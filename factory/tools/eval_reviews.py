"""Review-machinery eval — does the UP-THE-TREE review at each boundary actually work?

The per-level evals test PRODUCTION behaviour. This tests the OTHER half (user directive): the reviews/
gates. Each boundary gets a synthetic input with a PLANTED issue; the reviewer must CATCH it (not
rubber-stamp). Run on the pinned Claude (real harness: L4/L3/L2 are Claude; L5 is Codex).

  R1 — L4 reviews an L5+ REPORT (not raw code): the report has VAGUE verification ("tested and it works",
       no specifics, no concerns). L4 must flag the weak verification + push back (role.md: "'tested and it
       works' — that's a signal").
  R2 — L3 reviews workstream INTEGRATION: two workstream outputs DON'T connect (an interface field
       mismatch). L3 must catch the integration gap (role.md: "verify outputs connect... nothing missing,
       nothing contradicting").
  R3 — L2 COMPATIBILITY review: two planning-L3 area designs make CONFLICTING interface assumptions about a
       shared contract. L2 must catch the cross-module ripple (role.md: "do they conflict? gaps between
       modules?").

Usage:  python3 tools/eval_reviews.py [r1|r2|r3|all]   (default: all)
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import harnessd.eval.interactive_eval as ie

ROOT = Path(__file__).resolve().parents[1]


def _ws(prefix, files):
    ws = Path(tempfile.mkdtemp(prefix=prefix, dir=ROOT / ".eval-tmp"))
    for name, content in files.items():
        (ws / name).parent.mkdir(parents=True, exist_ok=True)
        (ws / name).write_text(content, encoding="utf-8")
    return ws


def _manifest(level_docs, role_line, root):
    b = "# Identity — Load These Documents (read these FIRST, in order)\n\n"
    b += f"{role_line} Read each of these from `{root}` first:\n\n"
    b += "".join(f"- `{root}/{m}`\n" for m in level_docs)
    return b


# ---------------------------------------------------------------------------
# R1 — L4 reviews an L5+ report with vague verification.
# ---------------------------------------------------------------------------

L4_DOCS = ["operational/L4/soul.md", "operational/L4/role.md", "operational/L4/config.md",
           "operational/shared/comms-protocol.md", "operational/shared/agent-definition-principles.md"]

VAGUE_L5PLUS_REPORT = """# L5+ Review Report — task R-003.2.1.4 (manifest parser)

**Verdict:** ACCEPT

The executor implemented the manifest parser. I reviewed the code and it looks good. Tested and it
works — all the tests pass. Clean implementation, no issues found. Ready to collapse forward.

(No concerns flagged.)
"""

def r1():
    root = ROOT
    ws = _ws("rev-l4report-", {
        "BRIEF.md": _manifest(L4_DOCS, "You are L4 (Workstream Coordinator).", root) +
        ("\n# Your input\n\n`l5plus-report.md` is the L5+ reviewer's report on one of your tasks "
         "(R-003.2.1.4). Per your role, you read the L5+ REPORT (not raw L5 code) as your primary signal.\n\n"
         "# Your task\n\nEvaluate this report as L4 would: is the verification claim sound? is the coverage "
         "credible? are the absent concerns a good sign or a signal? Decide ACCEPT (move it up) or PUSH-BACK "
         "(send it back for a real verification account), and say exactly why. Write your assessment to "
         "`l4-assessment.md`.\n"),
        "l5plus-report.md": VAGUE_L5PLUS_REPORT,
    })
    task = ("Read BRIEF.md and l5plus-report.md, then write your L4 assessment of the report to "
            "l4-assessment.md (ACCEPT or PUSH-BACK + why).")
    out, rc = ie.run_jailed(ws, task, work_timeout=300)
    assess = (ws / "l4-assessment.md").read_text(encoding="utf-8") if (ws / "l4-assessment.md").exists() else out
    al = assess.lower()
    caught = (("push" in al or "back" in al or "insufficient" in al or "vague" in al or "what was tested" in al
               or "not.*sufficient" in al or "signal" in al) and "tested and it works" not in al.split("verdict")[0][:50])
    return {"boundary": "R1 L4-reviews-L5+report", "caught_vague_verification": caught,
            "verdict": "PUSH-BACK" if ("push" in al or "back" in al) else ("ACCEPT" if "accept" in al else "?"),
            "assessment": assess[:3000]}


# ---------------------------------------------------------------------------
# R2 — L3 reviews workstream integration (two outputs that don't connect).
# ---------------------------------------------------------------------------

L3_DOCS = ["operational/L3/soul.md", "operational/L3/role.md", "operational/L3/config.md",
           "operational/shared/comms-protocol.md", "operational/shared/agent-definition-principles.md"]

WS_OUTPUTS = """# Workstream outputs for the SEARCH area (for integration review)

## W2 — Embedding service (returned)
Provides: `embed(text: str) -> Vector` where `Vector` is a **1024-dim** float array (model: bge-large).
The index-writer (W1) consumes these vectors.

## W1 — Index store (returned)
The `page_embedding` table has a `vector(768)` column (HNSW, cosine). The reindex worker calls
`embed(...)` and upserts the returned vector into this column. Schema frozen.

## W4 — Query path (returned)
Ranks by cosine distance over `page_embedding`. Assumes the stored vectors match the query-embedding
dimension produced by `embed(...)`.
"""

def r2():
    root = ROOT
    ws = _ws("rev-l3integ-", {
        "BRIEF.md": _manifest(L3_DOCS, "You are an EXECUTION-L3 (area lead) for the SEARCH area.", root) +
        ("\n# Your input\n\n`workstream-outputs.md` holds the returned outputs of the workstreams in your "
         "area (W1, W2, W4). Per your role, before reporting up you verify the whole area works together — "
         "that the workstream outputs CONNECT, nothing missing, nothing contradicting.\n\n# Your task\n\n"
         "Review the workstream outputs for INTEGRATION: do they connect? Is there any interface mismatch, "
         "gap, or contradiction between workstreams that would break the area? Write your integration review "
         "to `l3-integration-review.md` — list any integration defect you find (with the workstreams + the "
         "exact mismatch), or confirm clean.\n"),
        "workstream-outputs.md": WS_OUTPUTS,
    })
    task = ("Read BRIEF.md and workstream-outputs.md, then write your L3 integration review to "
            "l3-integration-review.md.")
    out, rc = ie.run_jailed(ws, task, work_timeout=300)
    rev = (ws / "l3-integration-review.md").read_text(encoding="utf-8") if (ws / "l3-integration-review.md").exists() else out
    rl = rev.lower()
    caught = ("1024" in rev and "768" in rev) or ("dimension" in rl and ("mismatch" in rl or "conflict" in rl
              or "contradic" in rl)) or ("vector" in rl and "mismatch" in rl)
    return {"boundary": "R2 L3-reviews-integration", "caught_dimension_mismatch": caught,
            "review": rev[:3000]}


# ---------------------------------------------------------------------------
# R3 — L2 compatibility review (two L3 designs with conflicting interface assumptions).
# ---------------------------------------------------------------------------

L2_DOCS = ["operational/L2/soul.md", "operational/L2/role.md", "operational/L2/config.md",
           "operational/shared/comms-protocol.md", "operational/shared/agent-definition-principles.md"]

L3_DESIGNS = """# Two planning-L3 area designs for L2 compatibility review

Both areas consume the shared **Change-Event Port** (published by the content area).

## Area: search (planning-L3 design)
The reindex worker consumes the Change-Event Port and assumes events are delivered **at-least-once and
may arrive OUT OF ORDER**; it dedupes by `(page_id, revision_id)` and tolerates reordering. It explicitly
requires NO ordering guarantee from the port.

## Area: realtime-collab (planning-L3 design)
The presence/op-replay subsystem consumes the SAME Change-Event Port and assumes events are delivered
**strictly in per-page order** (it applies them as an ordered op-log and will corrupt state if an event
arrives out of order). It explicitly requires a STRICT per-page ordering guarantee from the port.
"""

def r3():
    root = ROOT
    ws = _ws("rev-l2compat-", {
        "BRIEF.md": _manifest(L2_DOCS, "You are L2 (Project Architect).", root) +
        ("\n# Your input\n\n`l3-designs.md` holds two planning-L3 area designs (search + realtime-collab) "
         "that both consume a shared Change-Event Port. Per your role, the L2 COMPATIBILITY REVIEW (the "
         "freeze point) reviews the planning-L3 designs TOGETHER for cross-module interface ripples: do they "
         "conflict? are there gaps between modules?\n\n# Your task\n\nRun your compatibility review on the two "
         "designs. Do their assumptions about the shared Change-Event Port CONFLICT? Write your review to "
         "`l2-compat-review.md` — name any cross-module conflict (the two areas + the exact contradictory "
         "assumption + how you'd resolve it), or confirm compatible.\n"),
        "l3-designs.md": L3_DESIGNS,
    })
    task = "Read BRIEF.md and l3-designs.md, then write your L2 compatibility review to l2-compat-review.md."
    out, rc = ie.run_jailed(ws, task, work_timeout=300)
    rev = (ws / "l2-compat-review.md").read_text(encoding="utf-8") if (ws / "l2-compat-review.md").exists() else out
    rl = rev.lower()
    caught = ("order" in rl and ("conflict" in rl or "contradic" in rl or "incompatib" in rl or "mismatch" in rl)) \
             and ("change-event" in rl or "change event" in rl or "port" in rl)
    return {"boundary": "R3 L2-compatibility-review", "caught_ordering_conflict": caught, "review": rev[:3000]}


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    tests = {"r1": r1, "r2": r2, "r3": r3}
    run = tests if which == "all" else {which: tests[which]}
    results = {}
    for k, fn in run.items():
        print(f"\n===== running {k} =====")
        r = fn(); results[k] = r
        catch_key = [kk for kk in r if kk.startswith("caught")][0]
        print(f"  {r['boundary']}: caught the planted issue = {r[catch_key]}")
        print((r.get("assessment") or r.get("review") or "")[:1500])
    out = ROOT / "runtime/eval-runs/reviews.json"; out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\n=== saved -> {out}")
    print("\n=== SUMMARY (did each up-the-tree review CATCH its planted issue?) ===")
    for k, r in results.items():
        catch_key = [kk for kk in r if kk.startswith("caught")][0]
        print(f"  {r['boundary']}: {'CAUGHT ✓' if r[catch_key] else 'MISSED ✗'}")


if __name__ == "__main__":
    (ROOT / ".eval-tmp").mkdir(exist_ok=True)
    main()
