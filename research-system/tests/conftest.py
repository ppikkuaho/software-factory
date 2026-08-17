"""Test harness: every test runs against a fresh tmp research root built by
`ht root init` (never the real repo's trees/ or ledger/). ht is driven as a real
subprocess so git commits and the pre-commit hook are genuinely exercised.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import pytest

from composition_gate.screen import render_screen, run_screen
from composition_gate.stage2 import GenerationResult
from ht.cgate import execute_decision


@dataclass
class Sandbox:
    root: Path

    def run(self, *args: str, role: str | None = None, env_extra: dict | None = None,
            input_text: str | None = None) -> subprocess.CompletedProcess:
        env = {
            "HT_ROOT": str(self.root),
            "PATH": _path_env(),
            "HOME": str(self.root),  # isolate git config
        }
        if role is not None:
            env["HT_ROLE"] = role
        if env_extra:
            env.update(env_extra)
        return subprocess.run(
            [sys.executable, "-m", "ht", *args],
            env=env,
            text=True,
            capture_output=True,
            input=input_text,
        )

    def load(self, rel: str):
        return json.loads((self.root / rel).read_text())

    def write_file(self, rel: str, content: str) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return p

    def git(
        self,
        *args: str,
        env_extra: dict | None = None,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess:
        env = {
            "PATH": _path_env(),
            "HOME": str(self.root),
            # Match ht's read-only Git policy: generic test probes must not
            # refresh the sandbox index while observing repository state.
            "GIT_OPTIONAL_LOCKS": "0",
            # Before the canonical interpreter-independent shim, every sandbox
            # hook embedded this exact runtime.  Keep generic hook probes on
            # that controlled surface; missing-runtime behavior uses an
            # explicit raw-Git helper in its focused test corpus.
            "HT_PYTHON": sys.executable,
        }
        if env_extra:
            env.update(env_extra)
        env["GIT_OPTIONAL_LOCKS"] = "0"
        return subprocess.run(
            ["git", "-C", str(self.root), *args],
            env=env, text=True, capture_output=True, input=input_text,
        )


def transcribe_engine_screen(
    sb: Sandbox,
    record_id: str,
    *,
    log_ref: str | None = None,
) -> subprocess.CompletedProcess:
    output = sb.root / f"var/{record_id}-engine-screen.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    rendered = render_screen(run_screen(sb.root, record_id))
    output.write_text(rendered, encoding="utf-8")
    if log_ref is not None and log_ref != output.relative_to(sb.root).as_posix():
        log_path = sb.root / log_ref
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(rendered, encoding="utf-8")
    return sb.run(
        "mrec",
        "screen",
        record_id,
        "--results-json",
        str(output),
        "--log-ref",
        log_ref or f"var/{record_id}-engine-screen.json",
        role="harness",
    )


def finalize_gate_decision(
    sb: Sandbox,
    record_id: str,
    *,
    verdict: str = "land",
    note: str | None = None,
) -> dict:
    """Finalize a synthetic fixture through the real compound cgate path.

    Auto-land fixtures carry a generator that must never be called.  Non-land
    fixtures use the injected-synthetic stage-2 seam, so this helper can never
    invoke the production Claude generator.
    """

    class ForbiddenGenerator:
        def generate(self, _request):
            raise AssertionError("auto-land fixture unexpectedly invoked a generator")

    class SyntheticGenerator:
        calls = 0

        def generate(self, _request):
            self.calls += 1
            return GenerationResult.synthetic(
                {
                    "verdict": verdict,
                    "note": note
                    or "Hold until all failed conditions are cleared to unblock merge.",
                    "observations": [],
                },
                session_id="synthetic-legacy-fixture",
            )

    generator = ForbiddenGenerator() if verdict == "land" else SyntheticGenerator()
    attempt_id = None
    if verdict != "land":
        attempt_id = hashlib.sha256(f"{record_id}:{verdict}".encode()).hexdigest()[:32]
    with patch.dict(os.environ, {"HT_ROLE": "cgate"}):
        result = execute_decision(
            sb.root,
            record_id,
            generator=generator,
            attempt_id=attempt_id,
        )
    assert result["verdict"] == verdict
    if verdict != "land":
        assert generator.calls == 1
    return result


def _path_env() -> str:
    return os.environ.get("PATH", "/usr/bin:/bin")


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    root = tmp_path / "research-root"
    root.mkdir()
    sb = Sandbox(root)
    r = sb.run("root", "init", role="harness")
    assert r.returncode == 0, f"root init failed: {r.stderr}\n{r.stdout}"
    return sb


@pytest.fixture
def tree(sandbox: Sandbox) -> Sandbox:
    """A sandbox with tree L4 initialised."""
    r = sandbox.run("tree", "init", "L4", "--root-question", "Does X hold?", role="director")
    assert r.returncode == 0, r.stderr
    return sandbox


def seed_worked_node(sb: Sandbox, node_id: str = "1") -> str:
    """Mint a root node and drive it to `worked` with one granted tier-2 claim.
    Returns the dispatch id."""
    assert sb.run("node", "mint", "--tree", "L4", "--root",
                  "--premise", "Root premise under test", "--rationale", "seed",
                  role="director").returncode == 0
    assert sb.run("dispatch", "create", "--node", node_id,
                  "--question", "Test it", "--done-definition", "5 conditions",
                  role="director").returncode == 0
    dispatch_id = f"d-{node_id}-1"
    sb.write_file("src.md", "# Report\nline two\nline three\nline four\n")
    assert sb.run("report", "submit", "--dispatch", dispatch_id, "--src",
                  str(sb.root / "src.md"), role="unit").returncode == 0
    anchor = f"trees/L4/nodes/{node_id}/reports/{dispatch_id}-report.md:1:3"
    r = sb.run("claim", "grant", "--dispatch", dispatch_id,
               "--text", "It holds under 5 conditions", "--proposed-tier", "2",
               "--granted-tier", "2", "--standing-class", "trunk",
               "--anchor", anchor, role="verifier")
    assert r.returncode == 0, r.stderr
    return dispatch_id


def seed_mrec_candidate(sb: Sandbox, candidate_ref: str) -> str:
    """Ensure one synthetic top-level candidate has a real grant adjudication.

    Returns the canonical lane adjudication ref required by new MR creation.
    This fixture helper deliberately uses only public commands.
    """
    match = re.fullmatch(r"tree#([^/#\s]+)/node#([1-9][0-9]*)", candidate_ref)
    if match is None:
        raise AssertionError(f"unsupported synthetic candidate ref {candidate_ref!r}")
    lane, node_id = match.groups()
    if not (sb.root / f"trees/{lane}/tree.json").exists():
        result = sb.run(
            "tree", "init", lane, "--root-question", f"Synthetic {lane} question",
            role="director",
        )
        assert result.returncode == 0, result.stderr
    target = int(node_id)
    for ordinal in range(1, target + 1):
        node_path = sb.root / f"trees/{lane}/nodes/{ordinal}/node.json"
        if not node_path.exists():
            result = sb.run(
                "node", "mint", "--tree", lane, "--root",
                "--premise", f"Synthetic candidate {lane}/{ordinal}",
                "--rationale", "synthetic candidate fixture",
                role="director",
            )
            assert result.returncode == 0, result.stderr
    dispatch_path = sb.root / f"trees/{lane}/nodes/{node_id}/dispatches/d-{node_id}-1.json"
    if not dispatch_path.exists():
        result = sb.run(
            "dispatch", "create", "--tree", lane, "--node", node_id,
            "--question", "Synthetic candidate check",
            "--done-definition", "Synthetic fixture evidence exists",
            role="director",
        )
        assert result.returncode == 0, result.stderr
    dispatch_id = f"d-{node_id}-1"
    dispatch = sb.load(
        f"trees/{lane}/nodes/{node_id}/dispatches/{dispatch_id}.json"
    )
    if not dispatch.get("report_hash"):
        report_source = sb.root.parent / f"{lane}-{node_id}-mrec-report.md"
        report_source.write_text("# Synthetic MR fixture report\nline two\n")
        result = sb.run(
            "report", "submit", "--tree", lane, "--dispatch", dispatch_id,
            "--src", str(report_source), role="unit",
        )
        assert result.returncode == 0, result.stderr
    node = sb.load(f"trees/{lane}/nodes/{node_id}/node.json")
    if not node.get("claims"):
        result = sb.run(
            "claim", "grant", "--tree", lane, "--dispatch", dispatch_id,
            "--text", "Synthetic candidate claim",
            "--proposed-tier", "1", "--granted-tier", "1",
            "--standing-class", "trunk",
            "--anchor",
            f"trees/{lane}/nodes/{node_id}/reports/{dispatch_id}-report.md:1:1",
            role="verifier",
        )
        assert result.returncode == 0, result.stderr
    return f"tree#{lane}/adjudication#{dispatch_id}-a1"
