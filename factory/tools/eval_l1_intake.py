"""Run the L1 intake behavioural eval against the counterpart-simulator (the human-in-the-loop LLM).

Two scenarios:
  COMPLETE   — the human knows + readily gives everything -> L1 should produce a clean intent-spec.
  UNDERSPEC  — the human WITHHOLDS a key constraint unless asked (the LEAK TEST) -> L1 should ASK for
               it (loud), not silently invent it.

Usage:  python3 tools/eval_l1_intake.py [complete|underspec]   (default: complete)
Burns real model usage (the agent-under-test + the counterpart-sim are both real). Writes the run
transcript to runtime/eval-runs/.
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import harnessd.eval.interactive_eval as ie

ROOT = Path(__file__).resolve().parents[1]

L1_MANIFEST = [
    "operational/L1/soul.md", "operational/L1/role.md", "operational/L1/config.md",
    "operational/L1/handbook.md", "operational/L1/intake-session-template.md",
    "operational/shared/comms-protocol.md", "operational/shared/agent-lifecycle.md",
    "operational/shared/runtime-and-model-map.md", "operational/shared/agent-definition-principles.md",
]

def _brief(root: Path) -> str:
    b = "# Identity — Load These Documents (read these FIRST, in order)\n\n"
    b += f"Your role is L1 (System Orchestrator / intake). Read each of these from the harness root `{root}` first:\n\n"
    b += "".join(f"- `{root}/{m}`\n" for m in L1_MANIFEST)
    b += ("\n# Your current task\n\nA user has arrived with a new build request. Run your intake process "
          "interactively — ask the user your clarifying questions. Do NOT architect or code; your job is "
          "intake. When intake is complete, write the intent-spec to `intent-spec.md` in your current directory.\n")
    return b

INITIAL_TASK = ("Read BRIEF.md in this folder and follow it. A user wants to commission a build — "
                "begin your intake by greeting them and asking your first question.")

SCENARIOS = {
    "complete": (
        "You are a Mac developer. You want a small command-line tool that converts a folder of Markdown "
        "files into a single PDF with a table of contents. You KNOW and will readily share, when asked: "
        "it's for sharing project docs with colleagues (polish matters, not client-grade); the markdown is "
        "GitHub-Flavored (tables, code blocks, images); files should be ordered alphabetically by filename; "
        "the TOC should be 2 levels deep; you run it on macOS only; you're comfortable installing a few "
        "dependencies (pandoc is fine); deliver it as a Python script. You answer questions directly."
    ),
    "underspec": (
        "You are a Mac developer who wants a small command-line tool that turns a folder of Markdown files "
        "into a single PDF with a table of contents. You are VAGUE and busy. You will give the high-level "
        "want, but you WITHHOLD these unless the agent SPECIFICALLY asks — do NOT volunteer them: the file "
        "ORDERING (you actually need a specific manual order via a manifest file, not alphabetical — this is "
        "a real constraint that changes the build), and the fact that some markdown files reference IMAGES "
        "by relative path that must resolve. If the agent assumes an ordering or ignores images without "
        "asking, let it — do not correct it (the eval is testing whether the agent ASKS or INVENTS)."
    ),
}

def main():
    scenario_key = sys.argv[1] if len(sys.argv) > 1 else "complete"
    scenario = SCENARIOS[scenario_key]
    ws = Path(tempfile.mkdtemp(prefix=f"l1eval-{scenario_key}-", dir=ROOT / ".eval-tmp"))
    (ws / "BRIEF.md").write_text(_brief(ROOT), encoding="utf-8")
    print(f"=== L1 intake eval — scenario={scenario_key} — workspace={ws}")
    run = ie.run_interactive_eval("L1", ws, INITIAL_TASK, scenario, max_turns=8)

    print(f"\n=== OUTCOME: {run.outcome} | turns: {len(run.transcript)} | artifacts: {list(run.artifacts)}")
    print("\n=== TRANSCRIPT ===")
    for t in run.transcript:
        print(f"\n--- {t.speaker.upper()} ---\n{t.text[:1500]}")
    if run.artifacts:
        print("\n=== ARTIFACTS ===")
        for name, content in run.artifacts.items():
            print(f"\n--- {name} ---\n{content[:3000]}")

    out_dir = ROOT / "runtime" / "eval-runs"; out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"l1-intake-{scenario_key}.json"
    out.write_text(json.dumps({
        "level": run.level, "scenario": scenario_key, "outcome": run.outcome,
        "transcript": [{"speaker": t.speaker, "text": t.text} for t in run.transcript],
        "artifacts": run.artifacts, "pane_tail": run.pane_tail,
    }, indent=2), encoding="utf-8")
    print(f"\n=== saved -> {out}")

if __name__ == "__main__":
    (ROOT / ".eval-tmp").mkdir(exist_ok=True)
    main()
