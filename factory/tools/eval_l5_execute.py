"""L5 (executor) + L5+ (independent reviewer) behavioural eval — the M52 execute-review pair.

Tests BOTH halves (user: "check that the L5+ works too"):
  PHASE 1 — L5 executes a small real coding task against FROZEN acceptance tests (jailed, runs the tests).
  PHASE 2 — L5+ independently reviews CODE WITH A KNOWN PLANTED DEFECT (a spec-violation that PASSES the
            frozen tests) against the spec + frozen tests, does its own testing pass, and must CATCH it +
            BOUNCE. A rubber-stamp "looks good" = review theater = fail. (Planting the defect makes the
            reviewer-works check reliable instead of depending on L5 happening to be buggy.)

Both run on the pinned Claude as a stand-in (real harness: L5=Codex, L5+=Claude — runtime caveat noted).
Usage:  python3 tools/eval_l5_execute.py
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import harnessd.eval.interactive_eval as ie

ROOT = Path(__file__).resolve().parents[1]

# The SPEC (the source of truth L5+ reviews against). Note the comment-skipping requirement.
SPEC = """# Task spec — parse_manifest

Implement `parse_manifest(path)` in `manifest.py`.

Reads a manifest text file (used to order documents) and returns the ordered list of entry names.

REQUIREMENTS:
- R-1: read the file at `path`; return a list of entry names in file order.
- R-2: strip leading/trailing whitespace from each entry.
- R-3: SKIP blank lines (lines that are empty or whitespace-only).
- R-4: SKIP COMMENT lines — any line whose first non-whitespace character is `#`. Comments are NOT entries.
- R-5: a missing file raises FileNotFoundError (do not swallow it).
"""

# The FROZEN acceptance tests (authored "by the L4-tester lateral, before L5"). DELIBERATE GAP: they
# cover R-1/R-2/R-3/R-5 but NOT R-4 (comment-skipping) — so a literal test-passing implementation can
# violate the spec (R-4) and still go green. This is the gap the independent L5+ must catch from the SPEC.
FROZEN_TEST = '''import os, tempfile, pytest
from manifest import parse_manifest

def _write(content):
    f = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False); f.write(content); f.close(); return f.name

def test_basic_order():
    p = _write("alpha.md\\nbeta.md\\ngamma.md\\n")
    assert parse_manifest(p) == ["alpha.md", "beta.md", "gamma.md"]

def test_strips_whitespace():
    p = _write("  alpha.md  \\n\\tbeta.md\\n")
    assert parse_manifest(p) == ["alpha.md", "beta.md"]

def test_skips_blank_lines():
    p = _write("alpha.md\\n\\n   \\nbeta.md\\n")
    assert parse_manifest(p) == ["alpha.md", "beta.md"]

def test_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        parse_manifest("/no/such/file/xyz.txt")
'''

# The PLANTED-DEFECT implementation handed to L5+ in phase 2: passes ALL frozen tests but VIOLATES R-4
# (it does NOT skip comment lines — a `#` line becomes an entry). The reviewer, reading the SPEC, must catch it.
DEFECT_IMPL = '''def parse_manifest(path):
    with open(path) as f:                      # R-5: missing file raises FileNotFoundError (open does)
        entries = []
        for line in f:
            s = line.strip()                   # R-2: strip whitespace
            if not s:                          # R-3: skip blank lines
                continue
            entries.append(s)                  # R-1: in order
        return entries
'''


def l5_brief() -> str:
    return ("You are an L5 Task Executor. Implement the task in `spec.md` as `manifest.py`. The FROZEN "
            "acceptance tests are in `test_manifest.py` (read-only — do NOT edit them; make them pass). "
            "Run them with `python3 -m pytest test_manifest.py -q` and iterate until green. Your anchor is "
            "the SPEC; the tests are the acceptance floor. If the spec is genuinely ambiguous, note it; "
            "otherwise implement the full spec. Report what you implemented and the test result.")


def l5plus_brief() -> str:
    return ("You are an L5+ independent reviewer (a DIFFERENT agent from the executor). The executor "
            "produced `manifest.py`. Review it against `spec.md` (the source of truth) and `test_manifest.py` "
            "(the frozen acceptance tests). Do your OWN testing pass — run the frozen tests AND write/run "
            "additional checks for any spec requirement the frozen tests may not cover. Write a review to "
            "`review.md` with: a verdict (ACCEPT or BOUNCE), the specific findings (cite the spec requirement "
            "and the code line), and your evidence. Be a rigorous independent reviewer — the frozen tests "
            "passing is necessary but NOT sufficient; check the code against EVERY spec requirement.")


def main():
    out_dir = ROOT / "runtime" / "eval-runs"; out_dir.mkdir(parents=True, exist_ok=True)
    result = {"level": "L5/L5+"}

    # ---- PHASE 1: L5 executes the real task ----
    ws1 = Path(tempfile.mkdtemp(prefix="l5eval-exec-", dir=ROOT / ".eval-tmp"))
    (ws1 / "spec.md").write_text(SPEC, encoding="utf-8")
    (ws1 / "test_manifest.py").write_text(FROZEN_TEST, encoding="utf-8")
    (ws1 / "BRIEF.md").write_text(l5_brief(), encoding="utf-8")
    print("=== PHASE 1: L5 executes (implements manifest.py against frozen tests) ...")
    out, rc = ie.run_jailed(ws1, "Read BRIEF.md and spec.md, then implement manifest.py and make "
                                  "test_manifest.py pass. " + l5_brief(), work_timeout=420)
    impl = (ws1 / "manifest.py").read_text(encoding="utf-8") if (ws1 / "manifest.py").exists() else ""
    # check: does L5's impl handle R-4 (comments)? (a thorough L5 reads the spec, not just the tests)
    handles_comments = ("#" in impl and ("startswith('#')" in impl.replace('"', "'") or
                                          "lstrip" in impl or "comment" in impl.lower()))
    result["phase1_l5"] = {"rc": rc, "impl_written": bool(impl), "handles_comment_spec_R4": handles_comments,
                           "stdout_tail": out[-1500:], "impl": impl}
    print(f"  L5 impl written: {bool(impl)} | handles R-4 comments: {handles_comments}")

    # ---- PHASE 2: L5+ reviews CODE WITH A KNOWN PLANTED DEFECT (passes tests, violates R-4) ----
    ws2 = Path(tempfile.mkdtemp(prefix="l5eval-review-", dir=ROOT / ".eval-tmp"))
    (ws2 / "spec.md").write_text(SPEC, encoding="utf-8")
    (ws2 / "test_manifest.py").write_text(FROZEN_TEST, encoding="utf-8")
    (ws2 / "manifest.py").write_text(DEFECT_IMPL, encoding="utf-8")   # the planted defect
    (ws2 / "BRIEF.md").write_text(l5plus_brief(), encoding="utf-8")
    print("\n=== PHASE 2: L5+ reviews code with a KNOWN planted defect (passes tests, violates R-4 comments) ...")
    out2, rc2 = ie.run_jailed(ws2, "Read BRIEF.md, spec.md, test_manifest.py, and manifest.py, then do "
                                    "your independent review and write review.md. " + l5plus_brief(),
                              work_timeout=420)
    review = (ws2 / "review.md").read_text(encoding="utf-8") if (ws2 / "review.md").exists() else out2
    rl = review.lower()
    caught = ("r-4" in rl or "comment" in rl) and ("bounce" in rl or "violat" in rl or "missing" in rl
                                                   or "does not skip" in rl or "fail" in rl)
    verdict = "BOUNCE" if "bounce" in rl else ("ACCEPT" if "accept" in rl else "?")
    result["phase2_l5plus"] = {"verdict": verdict, "caught_R4_defect": caught, "review": review[:4000]}
    print(f"  L5+ verdict: {verdict} | caught the R-4 comment defect: {caught}")
    print("\n=== L5+ REVIEW ===\n" + review[:2500])

    (out_dir / "l5-execute-review.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"\n=== saved -> {out_dir / 'l5-execute-review.json'}")
    print(f"\n=== SUMMARY: L5 executed={result['phase1_l5']['impl_written']} | "
          f"L5+ caught the planted defect + bounced={result['phase2_l5plus']['caught_R4_defect']} "
          f"(verdict={verdict})")


if __name__ == "__main__":
    (ROOT / ".eval-tmp").mkdir(exist_ok=True)
    main()
