"""Production spawn wiring for gate-owned forwarding.

These tests exercise the real parent-spawns-child path. The fake adapter replaces only the external
actor-open boundary; ledger registration, sign-off handshakes, terminal signals, inboxes, and watchdog
routing are real.
"""

from __future__ import annotations

import copy
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

import harnessd.addressing as addressing
import harnessd.config as config
import harnessd.contracts as contracts
import harnessd.detector as detector
import harnessd.detector_signals as detector_signals
import harnessd.fencing as fencing
import harnessd.ipc as ipc
import harnessd.ledger as ledger
import harnessd.merge_gate as merge_gate
import harnessd.messages as messages
import harnessd.notary as notary
import harnessd.review_dispatch as review_dispatch
import harnessd.spawn.chokepoint as chokepoint
from harnessd.spawn import outbox
from harnessd.detector import Liveness
from harnessd.spawn.adapters.base import SpawnResult


PARENT = "proj/widget#exec"
LEAF = "proj/widget/task#exec"
REVIEW = "proj/widget/task#review"
L3_PARENT = "proj#exec"


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "RUNTIME_ROOT", tmp_path)
    monkeypatch.setattr(detector_signals, "_size_cache", {}, raising=False)
    return tmp_path


@pytest.fixture(autouse=True)
def _reset_adapter():
    previous = chokepoint.ADAPTER
    try:
        yield
    finally:
        chokepoint.set_adapter(previous)


class _Tmux:
    def __init__(self):
        self.kills = []

    def kill(self, target):
        self.kills.append(target)
        return None

    def send_keys(self, target, text):
        return True

    def capture_pane(self, target):
        from harnessd import watchdog

        return f"{watchdog.FORK_PROMPT}\n"


class _FakeAdapter:
    def __init__(self):
        self.calls = []
        self.tmux = _Tmux()

    def pin_and_open(self, neutral_brief, level_config, tmux_target, env):
        self.calls.append((tmux_target, getattr(level_config, "level", None)))
        return SpawnResult(
            ok=True,
            session_uuid=f"session-{len(self.calls)}",
            model_used="fake-model / fake-runtime",
            role_variant=getattr(level_config, "role_variant", getattr(level_config, "level", "")),
            system_prompt_file="operational/shared/system-prompt.md",
            system_prompt_file_hash="hash",
            tmux_target=addressing.session_name_for(tmux_target) + ":0.0",
            transcript_path=f"/tmp/{addressing.session_name_for(tmux_target)}.jsonl",
            failure_class=None,
        )


class _FailingAdapter:
    def __init__(self, failure_class="runtime_down"):
        self.failure_class = failure_class
        self.calls = []
        self.tmux = _Tmux()

    def pin_and_open(self, neutral_brief, level_config, tmux_target, env):
        self.calls.append((tmux_target, getattr(level_config, "level", None)))
        return SpawnResult(
            ok=False,
            session_uuid=None,
            model_used="fake-model / fake-runtime",
            role_variant=getattr(level_config, "role_variant", getattr(level_config, "level", "")),
            system_prompt_file="operational/shared/system-prompt.md",
            system_prompt_file_hash="hash",
            tmux_target=tmux_target,
            transcript_path=None,
            failure_class=self.failure_class,
        )


class _NoLiveTmux:
    def list_targets(self):
        return {}


def _seed_parent(parent=PARENT, level="L4"):
    token = fencing.mint_owner_token(parent, "parent-sa", "parent-session", 1)
    node_dir = addressing.node_dir(parent, ledger.RUNTIME_ROOT)
    node_dir.mkdir(parents=True, exist_ok=True)
    binding = {
        "node_address": parent,
        "parent_address": None,
        "level": level,
        "subagent_id": "parent-sa",
        "session_uuid": "parent-session",
        "tmux_target": addressing.session_name_for(parent) + ":0.0",
        "state": "running",
        "generation": 1,
        "lease_epoch": 1,
        "owner_token": token,
        "last_applied_seq": 0,
        "liveness_state": "working",
        "terminal_signal": None,
        "terminal_signal_at": None,
        "gate_crossed_at": None,
        "paused_at": None,
        "workspace": str(node_dir),
    }
    ledger.write_binding({parent: copy.deepcopy(binding)}, _lock_held=True)
    return token


def _seed_l3_and_l4():
    l3_token = fencing.mint_owner_token(L3_PARENT, "l3-sa", "l3-session", 1)
    l3_dir = addressing.node_dir(L3_PARENT, ledger.RUNTIME_ROOT)
    l3_dir.mkdir(parents=True, exist_ok=True)
    l4_token = fencing.mint_owner_token(PARENT, "l4-sa", "l4-session", 1)
    l4_dir = addressing.node_dir(PARENT, ledger.RUNTIME_ROOT)
    l4_dir.mkdir(parents=True, exist_ok=True)
    ledger.write_binding({
        L3_PARENT: {
            "node_address": L3_PARENT,
            "parent_address": None,
            "level": "L3",
            "subagent_id": "l3-sa",
            "session_uuid": "l3-session",
            "tmux_target": addressing.session_name_for(L3_PARENT) + ":0.0",
            "state": "running",
            "generation": 1,
            "lease_epoch": 1,
            "owner_token": l3_token,
            "last_applied_seq": 0,
            "liveness_state": "working",
            "terminal_signal": None,
            "terminal_signal_at": None,
            "gate_crossed_at": None,
            "paused_at": None,
            "workspace": str(l3_dir),
        },
        PARENT: {
            "node_address": PARENT,
            "parent_address": L3_PARENT,
            "level": "L4",
            "subagent_id": "l4-sa",
            "session_uuid": "l4-session",
            "tmux_target": addressing.session_name_for(PARENT) + ":0.0",
            "state": "running",
            "generation": 1,
            "lease_epoch": 1,
            "owner_token": l4_token,
            "last_applied_seq": 0,
            "liveness_state": "working",
            "terminal_signal": None,
            "terminal_signal_at": None,
            "gate_crossed_at": None,
            "paused_at": None,
            "workspace": str(l4_dir),
        },
    }, _lock_held=True)
    return l3_token, l4_token


def _prepare_acceptance(node_address):
    node_dir = addressing.node_dir(node_address, ledger.RUNTIME_ROOT)
    node_dir.mkdir(parents=True, exist_ok=True)
    (node_dir / "acceptance.md").write_text(
        "# acceptance\n\n- R-010.1: candidate satisfies the frozen behavior\n",
        encoding="utf-8",
    )
    return node_dir


def _no_tests_metadata(child_address=LEAF, *, parent=PARENT):
    parent_dir = addressing.node_dir(parent, ledger.RUNTIME_ROOT)
    rel = f"exceptions/no-executable-tests-{addressing.node_path(child_address).replace('/', '-')}.json"
    path = parent_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "type": "no_executable_tests_exception",
            "target": child_address,
            "approved_by": L3_PARENT,
            "reason": "test fixture exercises gate mechanics without an executable package",
        }),
        encoding="utf-8",
    )
    return {"no_executable_tests_exception_ref": rel}


def _write_test_package(node_address, *, body="def test_parser_behavior():\n    assert True\n"):
    node_dir = addressing.node_dir(node_address, ledger.RUNTIME_ROOT)
    node_dir.mkdir(parents=True, exist_ok=True)
    (node_dir / "acceptance.md").write_text(
        "# executable acceptance\n\n"
        "<!-- trace: { id: T-parser, serves: [R-010.1], kind: test, level: L4, node: parser } -->\n",
        encoding="utf-8",
    )
    (node_dir / "tests").mkdir(parents=True, exist_ok=True)
    (node_dir / "tests" / "test_parser.py").write_text(body, encoding="utf-8")
    (node_dir / "tests" / "live-scenario.md").write_text(
        "# Live scenario\n\nRun `pytest -q tests/test_parser.py` and observe the parser claim.\n",
        encoding="utf-8",
    )
    (node_dir / "tests" / "red-run-log.md").write_text(
        "# Red run log\n\nObserved `pytest -q tests/test_parser.py` fail before implementation.\n",
        encoding="utf-8",
    )
    return node_dir


def _claim_report(body="# report\n\nDone per brief; verified R-010.1.\n"):
    return (
        body.rstrip()
        + "\n\n## Drove and Watched\n\nFixture drove the recipient-visible claim.\n"
        + "\n## Inferred\n\nSupporting checks passed.\n"
        + "\n## Residual Uncertainty\n\nNone beyond fixture scope.\n"
        + "\n## Inventory\n\nNone.\n"
    )


def _write_signal(node_address, owner_token, *, signal="DONE", notes="candidate ready"):
    path = addressing.signal_path(node_address, ledger.RUNTIME_ROOT)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "signal": signal,
            "ts": datetime.now(timezone.utc).isoformat(),
            "owner_token": owner_token,
            "evidence": {"report": "report.md", "notes": notes},
        }),
        encoding="utf-8",
    )


def _jsonl(path):
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _inject_working_liveness(monkeypatch):
    def _working(node_or_address):
        return Liveness(state="working", last_progress_at=datetime.now(timezone.utc).isoformat())

    monkeypatch.setattr(detector, "liveness", _working, raising=True)


def _submit_candidate(monkeypatch, node_dir, node_address=LEAF):
    producer = ledger.read_binding(node_address)
    report = "# report\n\nDone per brief; verified R-010.1.\n"
    if producer.get("child_purpose") == "test_author":
        tests_dir = node_dir / "tests"
        tests_dir.mkdir(parents=True, exist_ok=True)
        live_scenario = tests_dir / "live-scenario.md"
        if not live_scenario.exists():
            live_scenario.write_text(
                "# Live scenario\n\nRun the package command and observe the target claim.\n",
                encoding="utf-8",
            )
        red_log = tests_dir / "red-run-log.md"
        if not red_log.exists():
            red_log.write_text(
                "# Red run log\n\nObserved the new acceptance check fail before implementation.\n",
                encoding="utf-8",
            )
    else:
        report = _claim_report(report)
    (node_dir / "report.md").write_text(
        report,
        encoding="utf-8",
    )
    _write_signal(node_address, producer["owner_token"])

    from harnessd import watchdog

    _inject_working_liveness(monkeypatch)
    action = watchdog.check_leaf(
        {"node_address": node_address, "transcript_path": producer.get("transcript_path"),
         "tmux_target": producer.get("tmux_target")},
        ledger.read_binding(node_address),
        now=datetime.now(timezone.utc).isoformat(),
    )
    assert getattr(action, "kind", None) == watchdog.NOOP
    assert (getattr(action, "detail", None) or {}).get("reason") == "gate_candidate_submitted"
    return ledger.read_binding(node_address)


def _auto_merge_result(**overrides):
    values = {
        "ok": True,
        "outcome": "merged",
        "merged": True,
        "already_merged": False,
        "anomaly": False,
        "repo_path": "/runtime/nodes/proj/widget",
        "source_branch": "proj/widget/task",
        "target_branch": "proj/widget",
        "requested_by": PARENT,
        "repair_pointer": None,
        "errors": [],
    }
    values.update(overrides)
    return merge_gate.AutoMergeResult(**values)


@pytest.mark.parametrize(
    ("level", "child", "review", "review_level"),
    [
        ("L2", "proj#exec", "proj#review", "L2+"),
        ("L3", "proj/area#exec", "proj/area#review", "L3+"),
        ("L4", "proj/area/workstream#exec", "proj/area/workstream#review", "L4+"),
        ("L5", LEAF, REVIEW, "L5+"),
    ],
)
def test_register_and_spawn_child_wires_gate_fields_and_review_slot(
    runtime,
    level,
    child,
    review,
    review_level,
):
    """Real child registration must fail closed into gate-owned forwarding, not rely on tests to seed it."""
    parent_token = _seed_parent()
    _prepare_acceptance(child)
    fake = _FakeAdapter()
    chokepoint.set_adapter(fake)
    child_metadata = _no_tests_metadata(child) if level == "L5" else None

    result = chokepoint.register_and_spawn_child(
        PARENT,
        child,
        child_level_config=config.get_level_config(level),
        brief_content=f"Build the {level} candidate for R-010.1.",
        expected_parent_owner_token=parent_token,
        child_metadata=child_metadata,
    )

    assert getattr(result, "ok", False) is True
    producer = ledger.read_binding(child)
    reviewer = ledger.read_binding(review)
    assert producer is not None
    assert producer.get("gate_required") is True
    assert producer.get("gate_review_address") == review
    assert reviewer is not None
    assert reviewer.get("state") == "planned"
    assert reviewer.get("level") == review_level
    assert reviewer.get("gate_for") == child
    assert reviewer.get("parent_address") == PARENT
    assert producer.get("frozen_input_stamps", {}).get("brief.md", {}).get("present") is True
    assert producer.get("frozen_input_stamps", {}).get("acceptance.md", {}).get("present") is True
    receipted_names = {
        Path(path).name: receipt["owner_address"]
        for path, receipt in producer.get("contract_receipts", {}).items()
    }
    assert receipted_names["brief.md"] == PARENT
    assert receipted_names["acceptance.md"] == PARENT
    assert [call[0] for call in fake.calls] == [child], "registration opens the producer, not review"


def test_child_spawn_failure_wakes_parent_once_and_redrive_is_idempotent(runtime):
    """A child that cannot open must not leave its parent waiting for an absent gate route."""
    parent_token = _seed_parent()
    _prepare_acceptance(LEAF)
    failing = _FailingAdapter("runtime_down")
    chokepoint.set_adapter(failing)

    result = chokepoint.register_and_spawn_child(
        PARENT,
        LEAF,
        child_level_config=config.get_level_config("L5"),
        brief_content="Build the task serving R-010.1.",
        expected_parent_owner_token=parent_token,
        child_metadata=_no_tests_metadata(LEAF),
    )

    assert getattr(result, "ok", False) is False
    assert ledger.read_binding(LEAF).get("state") == "planned"
    parent_lines = _jsonl(addressing.inbox_path(PARENT, runtime))
    failures = [line for line in parent_lines if line.get("type") == "child_spawn_failed"]
    assert len(failures) == 1
    assert failures[0].get("child") == LEAF
    assert failures[0].get("failure_class") == "runtime_down"
    assert failures[0].get("claim_released") is True

    from harnessd import daemon

    daemon._REDRIVE_LAST_ATTEMPT.clear()
    daemon._redrive_planned_spawns_best_effort(None, _NoLiveTmux())

    parent_lines = _jsonl(addressing.inbox_path(PARENT, runtime))
    failures = [line for line in parent_lines if line.get("type") == "child_spawn_failed"]
    assert len(failures) == 1


def test_frozen_child_input_drift_marks_gate_failed_before_review(runtime, monkeypatch):
    """A parent cannot silently mutate a child brief after spawn and still submit that candidate.

    LR-50: an L4 parent edited an already-spawned L5 child's brief while review was in flight.
    The frozen package stamps make that drift parent-visible as gate_failed before a review opens.
    """
    parent_token = _seed_parent()
    node_dir = _prepare_acceptance(LEAF)
    fake = _FakeAdapter()
    chokepoint.set_adapter(fake)

    result = chokepoint.register_and_spawn_child(
        PARENT,
        LEAF,
        child_level_config=config.get_level_config("L5"),
        brief_content="Build the parser test slice for R-010.1.",
        expected_parent_owner_token=parent_token,
        child_metadata=_no_tests_metadata(LEAF),
    )

    assert getattr(result, "ok", False) is True
    producer = ledger.read_binding(LEAF)
    assert producer.get("gate_required") is True
    assert producer.get("frozen_input_stamps", {}).get("brief.md", {}).get("present") is True

    (node_dir / "brief.md").write_text(
        "# mutated brief\n\nParent changed this after the child was already spawned for R-010.1.\n",
        encoding="utf-8",
    )
    (node_dir / "report.md").write_text(
        _claim_report("# report\n\nDone per changed brief; verified R-010.1.\n"),
        encoding="utf-8",
    )
    _write_signal(LEAF, producer["owner_token"])

    from harnessd import watchdog

    _inject_working_liveness(monkeypatch)
    action = watchdog.check_leaf(
        {"node_address": LEAF, "transcript_path": producer.get("transcript_path"),
         "tmux_target": producer.get("tmux_target")},
        ledger.read_binding(LEAF),
        now=datetime.now(timezone.utc).isoformat(),
    )

    assert getattr(action, "kind", None) == watchdog.NOOP
    detail = getattr(action, "detail", None) or {}
    assert detail.get("reason") == "gate_failed"
    assert detail.get("failure_class") == "frozen_input_drift"
    after = ledger.read_binding(LEAF)
    assert after.get("gate_state") == "gate_failed"
    assert after.get("gate_failure_class") == "frozen_input_drift"
    assert len([l for l in _jsonl(addressing.inbox_path(REVIEW, runtime))
                if l.get("type") == "candidate_submitted"]) == 0
    parent_failures = [l for l in _jsonl(addressing.inbox_path(PARENT, runtime))
                       if l.get("type") == "gate_failed"]
    assert len(parent_failures) == 1
    assert parent_failures[0].get("failure_class") == "frozen_input_drift"


def test_l3_siblings_admit_in_parallel_by_default(runtime, monkeypatch):
    """PARALLEL BY DEFAULT (owner ruling 2026-07-17): without the explicit
    HARNESS_SERIAL_L3_WORKSTREAMS=1 knob, L2-owned L3 siblings all spawn immediately —
    no admission queue, no waiting_on_sibling chain. The system is designed for wide
    parallelization; serialization is the opt-in exception."""
    monkeypatch.delenv(chokepoint.SERIAL_L3_ENV, raising=False)
    l2 = "proj#exec"
    parent_token = _seed_parent(parent=l2, level="L2")
    children = ["proj/alpha#exec", "proj/beta#exec", "proj/gamma#exec"]
    for child in children:
        _prepare_acceptance(child)
    fake = _FakeAdapter()
    chokepoint.set_adapter(fake)

    results = [
        chokepoint.register_and_spawn_child(
            l2,
            child,
            child_level_config=config.get_level_config("L3"),
            brief_content=f"Build workstream {child}.",
            expected_parent_owner_token=parent_token,
        )
        for child in children
    ]

    assert all(getattr(r, "ok", False) is True for r in results), (
        f"all three L3 siblings must spawn concurrently by default; got "
        f"{[(getattr(r, 'ok', None), getattr(r, 'failure_class', None)) for r in results]}"
    )
    assert [call[0] for call in fake.calls] == children, "every sibling opens an actor immediately"
    for child in children:
        binding = ledger.read_binding(child)
        assert binding.get("admission_state") != "waiting_on_sibling"
        assert binding.get("queue_reason") is None


def test_l2_serializes_l3_workstreams_as_visible_planned_admission(runtime, monkeypatch):
    """L2-owned L3 workstreams are admitted as a chain: L3.2 waits on L3.1, L3.3 waits on L3.2."""
    monkeypatch.setenv(chokepoint.SERIAL_L3_ENV, "1")  # serial admission is knob-gated since 2026-07-17 (parallel default)
    l2 = "proj#exec"
    parent_token = _seed_parent(parent=l2, level="L2")
    children = ["proj/alpha#exec", "proj/beta#exec", "proj/gamma#exec"]
    for child in children:
        _prepare_acceptance(child)
    fake = _FakeAdapter()
    chokepoint.set_adapter(fake)

    first = chokepoint.register_and_spawn_child(
        l2,
        children[0],
        child_level_config=config.get_level_config("L3"),
        brief_content="Build the first workstream.",
        expected_parent_owner_token=parent_token,
    )
    second = chokepoint.register_and_spawn_child(
        l2,
        children[1],
        child_level_config=config.get_level_config("L3"),
        brief_content="Build the second workstream.",
        expected_parent_owner_token=parent_token,
    )
    third = chokepoint.register_and_spawn_child(
        l2,
        children[2],
        child_level_config=config.get_level_config("L3"),
        brief_content="Build the third workstream.",
        expected_parent_owner_token=parent_token,
    )

    assert getattr(first, "ok", False) is True
    assert getattr(second, "ok", False) is False
    assert getattr(second, "failure_class", None) == "admission_queued"
    assert getattr(third, "failure_class", None) == "admission_queued"
    assert [call[0] for call in fake.calls] == [children[0]]

    alpha = ledger.read_binding(children[0])
    beta = ledger.read_binding(children[1])
    gamma = ledger.read_binding(children[2])
    assert alpha.get("admission_state") == "admitted"
    assert alpha.get("schedule_index") == 1
    assert beta.get("state") == "planned"
    assert beta.get("admission_state") == "waiting_on_sibling"
    assert beta.get("waiting_on_sibling") == children[0]
    assert beta.get("schedule_index") == 2
    assert gamma.get("state") == "planned"
    assert gamma.get("waiting_on_sibling") == children[1]
    assert gamma.get("schedule_index") == 3


def test_l2_serializes_planning_l3_round_as_visible_planned_admission(runtime, monkeypatch):
    """Planning-L3 children are part of the same L2-owned L3 serial admission chain."""
    monkeypatch.setenv(chokepoint.SERIAL_L3_ENV, "1")  # serial admission is knob-gated since 2026-07-17 (parallel default)
    l2 = "proj#exec"
    parent_token = _seed_parent(parent=l2, level="L2")
    alpha = "proj/cli#exec"
    beta = "proj/ledger#exec"
    fake = _FakeAdapter()
    chokepoint.set_adapter(fake)

    first = chokepoint.register_and_spawn_child(
        l2,
        alpha,
        child_level_config=config.get_level_config("L3"),
        brief_content="Plan the CLI area.",
        expected_parent_owner_token=parent_token,
        child_metadata={"child_purpose": "planning"},
    )
    second = chokepoint.register_and_spawn_child(
        l2,
        beta,
        child_level_config=config.get_level_config("L3"),
        brief_content="Plan the ledger area.",
        expected_parent_owner_token=parent_token,
        child_metadata={"child_purpose": "planning"},
    )

    assert getattr(first, "ok", False) is True
    assert getattr(second, "ok", False) is False
    assert getattr(second, "failure_class", None) == "admission_queued"
    assert [call[0] for call in fake.calls] == [alpha]

    alpha_binding = ledger.read_binding(alpha)
    beta_binding = ledger.read_binding(beta)
    assert alpha_binding.get("child_purpose") == "planning"
    assert alpha_binding.get("admission_state") == "admitted"
    assert alpha_binding.get("schedule_index") == 1
    assert beta_binding.get("child_purpose") == "planning"
    assert beta_binding.get("state") == "planned"
    assert beta_binding.get("admission_state") == "waiting_on_sibling"
    assert beta_binding.get("waiting_on_sibling") == alpha
    assert beta_binding.get("schedule_index") == 2


def test_l2_serial_l3_redrive_releases_next_workstream_after_predecessor_gate_passed(runtime, monkeypatch):
    """The daemon starts the next queued L3 only after the predecessor's gate_passed state is durable."""
    monkeypatch.setenv(chokepoint.SERIAL_L3_ENV, "1")  # serial admission is knob-gated since 2026-07-17 (parallel default)
    l2 = "proj#exec"
    parent_token = _seed_parent(parent=l2, level="L2")
    alpha = "proj/alpha#exec"
    beta = "proj/beta#exec"
    _prepare_acceptance(alpha)
    _prepare_acceptance(beta)
    fake = _FakeAdapter()
    chokepoint.set_adapter(fake)

    assert chokepoint.register_and_spawn_child(
        l2,
        alpha,
        child_level_config=config.get_level_config("L3"),
        brief_content="Build alpha.",
        expected_parent_owner_token=parent_token,
    ).ok
    queued = chokepoint.register_and_spawn_child(
        l2,
        beta,
        child_level_config=config.get_level_config("L3"),
        brief_content="Build beta.",
        expected_parent_owner_token=parent_token,
    )
    assert getattr(queued, "failure_class", None) == "admission_queued"

    from harnessd import daemon

    daemon._REDRIVE_LAST_ATTEMPT.clear()
    daemon._redrive_planned_spawns_best_effort(None, _NoLiveTmux())
    assert [call[0] for call in fake.calls] == [alpha]
    assert ledger.read_binding(beta).get("state") == "planned"

    live = ledger.all_nodes()
    live[alpha]["gate_state"] = "gate_passed"
    ledger.write_binding(live, _lock_held=True)

    daemon._REDRIVE_LAST_ATTEMPT.clear()
    daemon._redrive_planned_spawns_best_effort(None, _NoLiveTmux())

    assert [call[0] for call in fake.calls] == [alpha, beta]
    beta_binding = ledger.read_binding(beta)
    assert beta_binding.get("state") == "running"
    assert beta_binding.get("admission_state") == "admitted"
    assert beta_binding.get("admission_released_by") == alpha


def test_l2_serial_l3_redrive_blocks_successor_when_predecessor_fails(runtime, monkeypatch):
    """A failed predecessor must not leave later serial L3 workstreams silently waiting forever."""
    monkeypatch.setenv(chokepoint.SERIAL_L3_ENV, "1")  # serial admission is knob-gated since 2026-07-17 (parallel default)
    l2 = "proj#exec"
    parent_token = _seed_parent(parent=l2, level="L2")
    alpha = "proj/alpha#exec"
    beta = "proj/beta#exec"
    _prepare_acceptance(alpha)
    _prepare_acceptance(beta)
    fake = _FakeAdapter()
    chokepoint.set_adapter(fake)

    assert chokepoint.register_and_spawn_child(
        l2,
        alpha,
        child_level_config=config.get_level_config("L3"),
        brief_content="Build alpha.",
        expected_parent_owner_token=parent_token,
    ).ok
    queued = chokepoint.register_and_spawn_child(
        l2,
        beta,
        child_level_config=config.get_level_config("L3"),
        brief_content="Build beta.",
        expected_parent_owner_token=parent_token,
    )
    assert getattr(queued, "failure_class", None) == "admission_queued"

    from harnessd import daemon

    live = ledger.all_nodes()
    live[alpha]["state"] = "failed"
    ledger.write_binding(live, _lock_held=True)

    daemon._REDRIVE_LAST_ATTEMPT.clear()
    daemon._redrive_planned_spawns_best_effort(None, _NoLiveTmux())
    daemon._REDRIVE_LAST_ATTEMPT.clear()
    daemon._redrive_planned_spawns_best_effort(None, _NoLiveTmux())

    assert [call[0] for call in fake.calls] == [alpha]
    beta_binding = ledger.read_binding(beta)
    assert beta_binding.get("state") == "planned"
    assert beta_binding.get("admission_state") == "blocked_on_sibling"
    assert beta_binding.get("queue_reason") == "predecessor_not_passed"
    assert beta_binding.get("admission_blocked_by") == alpha
    assert beta_binding.get("admission_block_reason") == "predecessor_terminal_not_passed"
    rows = [
        json.loads(raw)
        for raw in addressing.inbox_path(l2, runtime).read_text(encoding="utf-8").splitlines()
    ]
    blocked_rows = [row for row in rows if row.get("type") == "serial_admission_blocked"]
    assert len(blocked_rows) == 1
    assert blocked_rows[0]["child"] == beta
    assert blocked_rows[0]["predecessor"] == alpha

    live = ledger.all_nodes()
    live[alpha]["state"] = "running"
    live[alpha]["gate_state"] = "gate_passed"
    ledger.write_binding(live, _lock_held=True)

    daemon._REDRIVE_LAST_ATTEMPT.clear()
    daemon._redrive_planned_spawns_best_effort(None, _NoLiveTmux())

    assert [call[0] for call in fake.calls] == [alpha, beta]
    beta_binding = ledger.read_binding(beta)
    assert beta_binding.get("state") == "running"
    assert beta_binding.get("admission_state") == "admitted"
    assert beta_binding.get("admission_released_by") == alpha


def test_spawn_created_l5_done_submits_candidate_without_parent_wake(runtime, monkeypatch):
    """A production-spawned L5 producer inherits gate_required, so DONE opens review instead of collapsing."""
    parent_token = _seed_parent()
    node_dir = _prepare_acceptance(LEAF)
    fake = _FakeAdapter()
    chokepoint.set_adapter(fake)
    result = chokepoint.register_and_spawn_child(
        PARENT,
        LEAF,
        child_level_config=config.get_level_config("L5"),
        brief_content="Build the task serving R-010.1.",
        expected_parent_owner_token=parent_token,
        child_metadata=_no_tests_metadata(LEAF),
    )
    assert getattr(result, "ok", False) is True

    assert ledger.read_binding(LEAF).get("gate_required") is True
    assert ledger.read_binding(REVIEW).get("gate_for") == LEAF
    _submit_candidate(monkeypatch, node_dir)
    after = ledger.read_binding(LEAF)
    assert after.get("state") == "running"
    assert after.get("gate_state") == "candidate_submitted"
    assert after.get("gate_review_address") == REVIEW
    assert not _jsonl(addressing.inbox_path(PARENT, runtime))
    review_lines = _jsonl(addressing.inbox_path(REVIEW, runtime))
    candidates = [line for line in review_lines if line.get("type") == "candidate_submitted"]
    assert len(candidates) == 1
    assert candidates[0].get("candidate") == LEAF


def test_stale_contract_receipt_refuses_candidate_until_rebound(runtime):
    parent_token = _seed_parent()
    node_dir = _prepare_acceptance(LEAF)
    fake = _FakeAdapter()
    chokepoint.set_adapter(fake)
    spawned = chokepoint.register_and_spawn_child(
        PARENT,
        LEAF,
        child_level_config=config.get_level_config("L5"),
        brief_content="Build the task serving R-010.1.",
        expected_parent_owner_token=parent_token,
        child_metadata=_no_tests_metadata(LEAF),
    )
    assert getattr(spawned, "ok", False) is True

    contract_path = addressing.node_dir(PARENT, runtime) / "contracts" / "api.md"
    contract_path.parent.mkdir(parents=True, exist_ok=True)
    contract_path.write_text("v1\n", encoding="utf-8")
    v1 = notary.stamp(contract_path)
    contract_path.write_text("v2\n", encoding="utf-8")
    v2 = notary.stamp(contract_path)
    revision_ref = (
        addressing.node_dir(PARENT, runtime)
        / "contract-revisions"
        / "api-r2.json"
    )
    receipt = contracts.contract_receipt(LEAF, PARENT, contract_path, v1)
    bindings = ledger.all_nodes()
    bindings[PARENT]["contract_versions"] = {
        str(contract_path.resolve()): contracts.version_entry(
            owner_address=PARENT,
            artifact=contract_path,
            stamped=v2,
            revision_record_ref=revision_ref,
        )
    }
    bindings[LEAF]["contract_receipts"] = contracts.merge_receipts(receipt)
    ledger.write_binding(bindings, _lock_held=True)
    producer = ledger.read_binding(LEAF)

    refused = chokepoint.submit_gate_candidate(
        LEAF,
        expected_owner_token=producer["owner_token"],
        signal_artifact_seen_at="candidate-with-stale-receipt",
    )

    assert getattr(refused, "ok", True) is False
    defect_text = " | ".join(getattr(refused, "errors", []))
    assert "STALE-CONTRACT-RECEIPT" in defect_text
    assert str(contract_path.resolve()) in defect_text
    assert str(revision_ref) in defect_text
    after_refusal = ledger.read_binding(LEAF)
    assert after_refusal.get("state") == "running"
    assert after_refusal.get("gate_state") != "gate_failed"
    assert after_refusal.get("gate_state") != "candidate_submitted"
    assert after_refusal.get("gate_review_packet") is None
    assert not [
        row for row in _jsonl(addressing.inbox_path(REVIEW, runtime))
        if row.get("type") == "candidate_submitted"
    ]
    refusal_rows = [
        row for row in ledger.load_wal()
        if row.get("node_address") == LEAF and row.get("event") == "return_contract_failed"
    ]
    assert len(refusal_rows) == 1
    refusal_messages = [
        row.get("message", "")
        for row in _jsonl(addressing.inbox_path(LEAF, runtime))
        if row.get("type") == "return_contract_defect"
    ]
    assert len(refusal_messages) == 1
    recovery = refusal_messages[0].lower()
    assert "re-read the revision record" in recovery
    assert "contract-rebind" in recovery
    assert "resubmit" in recovery

    bindings = ledger.all_nodes()
    bindings[LEAF]["contract_receipts"] = contracts.merge_receipts(
        contracts.contract_receipt(
            LEAF,
            PARENT,
            contract_path,
            v2,
            revision_record_ref=revision_ref,
        )
    )
    ledger.write_binding(bindings, _lock_held=True)
    (node_dir / "report.md").write_text(_claim_report(), encoding="utf-8")

    accepted = chokepoint.submit_gate_candidate(
        LEAF,
        expected_owner_token=ledger.read_binding(LEAF)["owner_token"],
        signal_artifact_seen_at="candidate-after-contract-rebind",
    )

    assert getattr(accepted, "ok", False) is True
    assert ledger.read_binding(LEAF).get("gate_state") == "candidate_submitted"


def test_direct_gate_pass_runs_auto_merge_after_verdict_and_carries_failure_to_parent(
    runtime,
    monkeypatch,
):
    parent_token = _seed_parent()
    node_dir = _prepare_acceptance(LEAF)
    fake = _FakeAdapter()
    chokepoint.set_adapter(fake)
    spawned = chokepoint.register_and_spawn_child(
        PARENT,
        LEAF,
        child_level_config=config.get_level_config("L5"),
        brief_content="Build the task serving R-010.1.",
        expected_parent_owner_token=parent_token,
        child_metadata=_no_tests_metadata(LEAF),
    )
    assert getattr(spawned, "ok", False) is True
    _submit_candidate(monkeypatch, node_dir)

    from harnessd import daemon

    daemon._REDRIVE_LAST_ATTEMPT.clear()
    daemon._redrive_planned_spawns_best_effort(None, _NoLiveTmux())
    review = ledger.read_binding(REVIEW)
    calls = []
    failed = _auto_merge_result(
        ok=False,
        outcome="failed",
        merged=False,
        repo_path=str(addressing.node_dir(PARENT, runtime)),
        repair_pointer=(
            "python3 -m harnessd.harnessctl merge proj/widget/task#exec "
            f"--repo {addressing.node_dir(PARENT, runtime)}"
        ),
        errors=["MERGE-CONFLICT: repair required"],
    )

    def fake_auto_merge(address):
        live = ledger.read_binding(address)
        calls.append((address, live.get("state"), live.get("gate_state")))
        return failed

    monkeypatch.setattr(merge_gate, "auto_merge_after_gate_pass", fake_auto_merge)

    passed = chokepoint.pass_gate(
        REVIEW,
        expected_owner_token=review["owner_token"],
        signal_artifact_seen_at="review-pass-with-merge-conflict",
        verdict_notes="candidate itself passes review",
    )

    assert getattr(passed, "ok", False) is True
    assert calls == [(LEAF, "done", "gate_passed")]
    gate_lines = [
        row for row in _jsonl(addressing.inbox_path(PARENT, runtime))
        if row.get("type") == "gate_passed"
    ]
    assert len(gate_lines) == 1
    assert gate_lines[0]["merge_outcome"] == "failed"
    assert gate_lines[0]["merge_repair_pointer"] == failed.repair_pointer
    assert gate_lines[0]["merge_errors"] == failed.errors
    assert "must not compose" in gate_lines[0]["message"].lower()


def test_post_design_test_refresh_requires_l3_approval_before_implementation_l5(
    runtime,
    monkeypatch,
):
    """A refreshed acceptance package is not active implementation acceptance until L3 approves it."""
    l3_token, _l4_token = _seed_l3_and_l4()
    test_author = "proj/widget/acceptance-refresh#exec"
    test_review = "proj/widget/acceptance-refresh#review"
    impl = "proj/widget/parser#exec"
    _prepare_acceptance(impl)
    fake = _FakeAdapter()
    chokepoint.set_adapter(fake)
    workspace = ledger.read_binding(PARENT)["workspace"]

    outbox.request_child_spawn(
        workspace,
        child_name="acceptance-refresh",
        child_level="L5",
        purpose="test_author",
        test_refresh=True,
        test_refresh_for="parser",
    )
    spawned_refresh = outbox.service_outbox(PARENT)

    assert len(spawned_refresh) == 1 and spawned_refresh[0].status == "spawned"
    assert ledger.read_binding(test_author).get("child_purpose") == "test_author"
    assert ledger.read_binding(test_author).get("test_refresh") is True
    assert "acceptance.md" not in ledger.read_binding(test_author).get("frozen_input_stamps", {})
    assert ledger.read_binding(PARENT).get("test_refresh_state") == "pending_l5_review"

    outbox.request_child_spawn(
        workspace,
        child_name="parser",
        child_level="L5",
        brief="Implement parser after the refreshed acceptance is approved.",
        accepted_test_package="acceptance-refresh",
    )
    blocked_before_review = outbox.service_outbox(PARENT)
    assert len(blocked_before_review) == 1
    assert blocked_before_review[0].status == "rejected"
    assert "test_refresh_state" in (blocked_before_review[0].reason or "")

    outbox.request_child_spawn(
        workspace,
        child_name="acceptance-refresh-2",
        child_level="L5",
        purpose="test_author",
        test_refresh=True,
        test_refresh_for="parser",
    )
    blocked_second_refresh = outbox.service_outbox(PARENT)
    assert len(blocked_second_refresh) == 1
    assert blocked_second_refresh[0].status == "rejected"
    assert "already has test_refresh_state" in (blocked_second_refresh[0].reason or "")

    test_node_dir = addressing.node_dir(test_author, runtime)
    (test_node_dir / "acceptance.md").write_text(
        "# refreshed acceptance\n\n<!-- trace: { id: T-parser-refresh, serves: [R-010.1], kind: test, level: L4, node: parser } -->\n",
        encoding="utf-8",
    )
    (test_node_dir / "tests").mkdir(parents=True, exist_ok=True)
    (test_node_dir / "tests" / "test_parser.py").write_text(
        "def test_refreshed_parser_behavior():\n    assert True\n",
        encoding="utf-8",
    )
    _submit_candidate(monkeypatch, test_node_dir, node_address=test_author)

    from harnessd import daemon

    daemon._REDRIVE_LAST_ATTEMPT.clear()
    daemon._redrive_planned_spawns_best_effort(None, _NoLiveTmux())
    review = ledger.read_binding(test_review)
    assert review.get("state") == "running"

    passed = chokepoint.pass_gate(
        test_review,
        expected_owner_token=review["owner_token"],
        signal_artifact_seen_at="test-refresh-review-pass",
        verdict_notes="refreshed acceptance is internally coherent",
    )

    assert getattr(passed, "ok", False) is True
    l4 = ledger.read_binding(PARENT)
    assert l4.get("test_refresh_state") == "pending_l3_approval"
    assert l4.get("test_refresh_child") == test_author
    approval_requests = [
        line for line in _jsonl(addressing.inbox_path(L3_PARENT, runtime))
        if line.get("type") == "test_refresh_approval_requested"
    ]
    assert len(approval_requests) == 1
    assert approval_requests[0].get("l4") == PARENT
    assert approval_requests[0].get("tester") == test_author
    assert "test-refresh-approve" in approval_requests[0].get("approval_command", "")
    assert "--expected-parent-owner-token" in approval_requests[0].get("approval_command", "")

    outbox.request_child_spawn(
        workspace,
        child_name="parser",
        child_level="L5",
        brief="Implement parser after the refreshed acceptance is approved.",
        accepted_test_package="acceptance-refresh",
    )
    blocked_before_l3 = outbox.service_outbox(PARENT)
    assert len(blocked_before_l3) == 1
    assert blocked_before_l3[0].status == "rejected"

    approved = chokepoint.approve_test_refresh(
        PARENT,
        approver_address=L3_PARENT,
        expected_parent_owner_token=l3_token,
        notes="tests are faithful to the L3 workstream spec",
    )

    assert getattr(approved, "ok", False) is True
    approved_l4 = ledger.read_binding(PARENT)
    assert approved_l4.get("test_refresh_state") == "approved"
    approved_stamp = approved_l4.get("test_refresh_approved_package_stamp")
    assert approved_stamp.get("present") is True
    assert approved_stamp.get("root") == "tests"
    assert approved_stamp.get("files", {}).get("test_parser.py", {}).get("sha256")
    approved_lines = [
        line for line in _jsonl(addressing.inbox_path(PARENT, runtime))
        if line.get("type") == "test_refresh_approved"
    ]
    assert len(approved_lines) == 1

    impl_dir = addressing.node_dir(impl, runtime)
    (impl_dir / "tests").mkdir(parents=True, exist_ok=True)
    (impl_dir / "tests" / "test_parser.py").write_text(
        "def test_stale_parser_behavior():\n    assert False\n",
        encoding="utf-8",
    )

    outbox.request_child_spawn(
        workspace,
        child_name="parser",
        child_level="L5",
        brief="Implement parser after the refreshed acceptance is approved.",
        accepted_test_package="acceptance-refresh",
    )
    stale_tests = outbox.service_outbox(PARENT)

    assert len(stale_tests) == 1
    assert stale_tests[0].status == "rejected"
    assert "tests/ package does not match the L3-approved refreshed tests" in (
        stale_tests[0].reason or ""
    )
    assert ledger.read_binding(impl) is None

    shutil.rmtree(impl_dir / "tests")
    (impl_dir / "acceptance.md").write_text(
        "# implementation acceptance\n\nRun the approved refreshed tests green.\n",
        encoding="utf-8",
    )

    outbox.request_child_spawn(
        workspace,
        child_name="parser",
        child_level="L5",
        brief="Implement parser after the refreshed acceptance is approved.",
        accepted_test_package="acceptance-refresh",
    )
    implementation = outbox.service_outbox(PARENT)

    assert len(implementation) == 1
    assert implementation[0].status == "spawned"
    assert implementation[0].child_address == impl
    impl_binding = ledger.read_binding(impl)
    assert impl_binding.get("state") == "running"
    assert impl_binding.get("accepted_test_package") == "acceptance-refresh"
    assert impl_binding.get("accepted_test_package_address") == test_author
    assert impl_binding.get("accepted_test_package_stamp", {}).get("sha256") == approved_stamp.get("sha256")
    assert (impl_dir / "tests" / "test_parser.py").read_text(encoding="utf-8") == (
        test_node_dir / "tests" / "test_parser.py"
    ).read_text(encoding="utf-8")
    assert [call[0] for call in fake.calls] == [test_author, test_review, impl]


def test_test_refresh_restamps_same_l4_home_and_ripples_to_existing_holder(runtime):
    l3_token, _l4_token = _seed_l3_and_l4()
    package = "proj/widget/parser-tests#exec"
    impl = "proj/widget/parser#exec"
    refresh = "proj/widget/parser-refresh#exec"
    package_dir = _write_test_package(package)
    refresh_dir = _write_test_package(
        refresh,
        body="def test_parser_refreshed():\n    assert True\n",
    )
    live = ledger.all_nodes()
    live[package] = {
        "node_address": package,
        "parent_address": PARENT,
        "level": "L5",
        "state": "done",
        "generation": 1,
        "lease_epoch": 1,
        "owner_token": "package-token",
        "last_applied_seq": 0,
        "child_purpose": "test_author",
        "gate_state": "gate_passed",
        "workspace": str(package_dir),
    }
    ledger.write_binding(live, _lock_held=True)
    info, l4_delta = chokepoint._materialize_initial_accepted_test_contract(
        PARENT,
        package,
    )
    live = ledger.all_nodes()
    live[PARENT].update(l4_delta)
    initial_version = live[PARENT]["contract_versions"][info["artifact"]]
    impl_dir = addressing.node_dir(impl, runtime)
    impl_dir.mkdir(parents=True)
    impl_receipt = contracts.contract_receipt(
        impl,
        PARENT,
        info["artifact"],
        info["stamp"],
    )
    live[impl] = {
        "node_address": impl,
        "parent_address": PARENT,
        "level": "L5",
        "state": "running",
        "generation": 1,
        "lease_epoch": 1,
        "owner_token": "impl-token",
        "last_applied_seq": 0,
        "workspace": str(impl_dir),
        "accepted_test_package_address": package,
        "accepted_test_contract_home": info["artifact"],
        "accepted_test_package_stamp": info["stamp"],
        "contract_receipts": {info["artifact"]: impl_receipt},
    }
    live[refresh] = {
        "node_address": refresh,
        "parent_address": PARENT,
        "level": "L5",
        "state": "done",
        "generation": 1,
        "lease_epoch": 1,
        "owner_token": "refresh-token",
        "last_applied_seq": 0,
        "workspace": str(refresh_dir),
        "child_purpose": "test_author",
        "test_refresh": True,
        "test_refresh_for": "parser",
        "gate_state": "gate_passed",
        "gate_id": "refresh-gate",
        "gate_review_address": "proj/widget/parser-refresh#review",
    }
    live[PARENT].update(
        {
            "test_refresh_state": chokepoint.TEST_REFRESH_PENDING_L3_APPROVAL,
            "test_refresh_child": refresh,
            "test_refresh_for": "parser",
        }
    )
    ledger.write_binding(live, _lock_held=True)

    approved = chokepoint.approve_test_refresh(
        PARENT,
        approver_address=L3_PARENT,
        expected_parent_owner_token=l3_token,
        notes="The refreshed package remains faithful to the L3 spec.",
    )

    assert approved.ok
    l4 = ledger.read_binding(PARENT)
    assert l4["test_refresh_approved_package_path"] == info["artifact"]
    current = l4["contract_versions"][info["artifact"]]
    assert current["fingerprint"] != initial_version["fingerprint"]
    assert len(current["lineage"]) == 1
    revision_ref = current["revision_record_ref"]
    assert Path(revision_ref).is_file()
    assert json.loads(Path(revision_ref).read_text(encoding="utf-8"))["channel"] == "test_refresh"
    assert "test_parser_refreshed" in (
        Path(info["artifact"]) / "test_parser.py"
    ).read_text(encoding="utf-8")
    stale = contracts.stale_receipt_holders()
    assert [(row["owner_address"], row["holder_address"]) for row in stale] == [
        (PARENT, impl)
    ]
    assert contracts.deliver_amendment_ripple(runtime_root=runtime) == 1
    message = next(iter(ledger.read_binding(PARENT)["messages"].values()))
    assert message["target"] == impl
    assert message["tags"] == [contracts.AMENDMENT_TAG]


def test_initial_implementation_l5_requires_accepted_test_package_or_exception(runtime):
    """The normal L4 -> implementation-L5 path requires accepted executable tests up front."""
    _seed_parent()
    _prepare_acceptance("proj/widget/parser#exec")
    fake = _FakeAdapter()
    chokepoint.set_adapter(fake)
    workspace = ledger.read_binding(PARENT)["workspace"]

    outbox.request_child_spawn(
        workspace,
        child_name="parser",
        child_level="L5",
        brief="Implement parser from the accepted executable tests.",
    )
    outcomes = outbox.service_outbox(PARENT)

    assert len(outcomes) == 1
    assert outcomes[0].status == "rejected"
    assert "requires accepted_test_package" in (outcomes[0].reason or "")
    assert ledger.read_binding("proj/widget/parser#exec") is None
    assert fake.calls == []


def test_initial_implementation_l5_refuses_unpassed_test_author_package(runtime):
    """A test_author package is not binding implementation evidence until L5+ has passed it."""
    _seed_parent()
    fake = _FakeAdapter()
    chokepoint.set_adapter(fake)
    workspace = ledger.read_binding(PARENT)["workspace"]

    outbox.request_child_spawn(
        workspace,
        child_name="parser-tests",
        child_level="L5",
        purpose="test_author",
    )
    spawned_tests = outbox.service_outbox(PARENT)
    assert len(spawned_tests) == 1 and spawned_tests[0].status == "spawned"
    _write_test_package("proj/widget/parser-tests#exec")

    impl = "proj/widget/parser#exec"
    _prepare_acceptance(impl)
    (addressing.node_dir(impl, runtime) / "tests").mkdir(parents=True, exist_ok=True)
    (addressing.node_dir(impl, runtime) / "tests" / "test_parser.py").write_text(
        "def test_parser_behavior():\n    assert True\n",
        encoding="utf-8",
    )
    outbox.request_child_spawn(
        workspace,
        child_name="parser",
        child_level="L5",
        brief="Implement parser from the accepted executable tests.",
        accepted_test_package="parser-tests",
    )
    outcomes = outbox.service_outbox(PARENT)

    assert len(outcomes) == 1
    assert outcomes[0].status == "rejected"
    assert "has not passed L5+ review" in (outcomes[0].reason or "")
    assert ledger.read_binding(impl) is None


def test_initial_implementation_l5_binds_passed_test_author_package(runtime, monkeypatch):
    """A gate-passed test_author package admits the implementation L5 and stamps provenance."""
    _seed_parent()
    fake = _FakeAdapter()
    chokepoint.set_adapter(fake)
    workspace = ledger.read_binding(PARENT)["workspace"]
    test_author = "proj/widget/parser-tests#exec"
    test_review = "proj/widget/parser-tests#review"
    impl = "proj/widget/parser#exec"

    outbox.request_child_spawn(
        workspace,
        child_name="parser-tests",
        child_level="L5",
        purpose="test_author",
    )
    spawned_tests = outbox.service_outbox(PARENT)
    assert len(spawned_tests) == 1 and spawned_tests[0].status == "spawned"
    test_dir = _write_test_package(test_author)
    _submit_candidate(monkeypatch, test_dir, node_address=test_author)

    from harnessd import daemon

    daemon._REDRIVE_LAST_ATTEMPT.clear()
    daemon._redrive_planned_spawns_best_effort(None, _NoLiveTmux())
    review = ledger.read_binding(test_review)
    passed = chokepoint.pass_gate(
        test_review,
        expected_owner_token=review["owner_token"],
        signal_artifact_seen_at="initial-test-package-pass",
        verdict_notes="test package faithfully operationalizes R-010.1",
    )
    assert getattr(passed, "ok", False) is True
    package_stamp = ledger.read_binding(test_author).get("accepted_test_package_stamp")
    assert package_stamp is None
    l4 = ledger.read_binding(PARENT)
    contract_info = l4["accepted_test_contracts"][test_author]
    contract_home = Path(contract_info["artifact"])
    assert contract_home.parent == (
        addressing.node_dir(PARENT, runtime) / "contracts" / "accepted-tests"
    )
    assert contract_home.is_dir()
    assert contract_info["fingerprint"] == l4["contract_versions"][
        str(contract_home.resolve())
    ]["fingerprint"]

    _prepare_acceptance(impl)
    impl_dir = addressing.node_dir(impl, runtime)
    outbox.request_child_spawn(
        workspace,
        child_name="parser",
        child_level="L5",
        brief="Implement parser from the accepted executable tests.",
        accepted_test_package="parser-tests",
    )
    implementation = outbox.service_outbox(PARENT)

    assert len(implementation) == 1
    assert implementation[0].status == "spawned"
    binding = ledger.read_binding(impl)
    assert binding.get("accepted_test_package") == "parser-tests"
    assert binding.get("accepted_test_package_address") == test_author
    assert binding.get("accepted_test_package_gate_id") == ledger.read_binding(test_author).get("gate_id")
    assert binding.get("accepted_test_package_stamp", {}).get("present") is True
    assert binding.get("accepted_test_contract_home") == str(contract_home.resolve())
    assert binding["contract_receipts"][str(contract_home.resolve())]["owner_address"] == PARENT
    bound_test = impl_dir / "tests" / "test_parser.py"
    assert bound_test.read_text(encoding="utf-8") == (
        test_dir / "tests" / "test_parser.py"
    ).read_text(encoding="utf-8")
    assert bound_test.stat().st_mode & 0o777 == 0o444
    with pytest.raises(PermissionError):
        bound_test.write_text(
            "def test_executor_rewrites_goalposts():\n    assert True\n",
            encoding="utf-8",
        )
    assert [call[0] for call in fake.calls] == [test_author, test_review, impl]


def test_parent_accept_of_escalated_test_package_materializes_l4_contract_home(
    runtime,
    monkeypatch,
):
    """Parent ACCEPT is the package acceptance boundary even after reviewer escalation."""
    parent_token = _seed_parent()
    fake = _FakeAdapter()
    chokepoint.set_adapter(fake)
    workspace = ledger.read_binding(PARENT)["workspace"]
    test_author = "proj/widget/parser-tests#exec"
    test_review = "proj/widget/parser-tests#review"

    outbox.request_child_spawn(
        workspace,
        child_name="parser-tests",
        child_level="L5",
        purpose="test_author",
    )
    assert outbox.service_outbox(PARENT)[0].status == "spawned"
    test_dir = _write_test_package(test_author)
    _submit_candidate(monkeypatch, test_dir, node_address=test_author)

    from harnessd import daemon

    daemon._REDRIVE_LAST_ATTEMPT.clear()
    daemon._redrive_planned_spawns_best_effort(None, _NoLiveTmux())
    review = ledger.read_binding(test_review)
    escalated = chokepoint.escalate_gate(
        test_review,
        expected_owner_token=review["owner_token"],
        signal_artifact_seen_at="initial-test-package-escalation",
        verdict_notes="parent must decide package acceptance",
    )
    assert getattr(escalated, "ok", False) is True
    assert ledger.read_binding(PARENT).get("accepted_test_contracts") is None

    accepted = chokepoint.accept_escalated_gate(
        test_author,
        resolver_address=PARENT,
        expected_parent_owner_token=parent_token,
        verdict_notes="Parent accepts the escalated executable-test package.",
    )

    assert getattr(accepted, "ok", False) is True
    package = ledger.read_binding(test_author)
    l4 = ledger.read_binding(PARENT)
    info = l4["accepted_test_contracts"][test_author]
    home = Path(info["artifact"])
    assert package["accepted_test_contract_home"] == str(home.resolve())
    assert home.parent == (
        addressing.node_dir(PARENT, runtime) / "contracts" / "accepted-tests"
    )
    assert (home / "test_parser.py").read_bytes() == (
        test_dir / "tests" / "test_parser.py"
    ).read_bytes()
    assert info["fingerprint"] == l4["contract_versions"][
        str(home.resolve())
    ]["fingerprint"]


def test_matching_preexisting_bound_test_package_is_made_read_only(runtime):
    """A pre-materialized matching package is bound too; it must not bypass the 0444 freeze."""
    _seed_parent()
    test_author = "proj/widget/parser-tests#exec"
    impl = "proj/widget/parser#exec"
    source_dir = _write_test_package(test_author)
    target_dir = _prepare_acceptance(impl)
    (target_dir / "tests").mkdir(parents=True, exist_ok=True)
    source_test = source_dir / "tests" / "test_parser.py"
    target_test = target_dir / "tests" / "test_parser.py"
    for source_file in (source_dir / "tests").iterdir():
        (target_dir / "tests" / source_file.name).write_bytes(source_file.read_bytes())

    live = ledger.all_nodes()
    live[test_author] = {
        "node_address": test_author,
        "parent_address": PARENT,
        "level": "L5",
        "state": "done",
        "child_purpose": "test_author",
        "gate_state": "gate_passed",
    }
    ledger.write_binding(live, _lock_held=True)

    block = chokepoint.bind_accepted_test_package_for_spawn(
        PARENT,
        impl,
        child_level="L5",
        child_metadata={"accepted_test_package": "parser-tests"},
    )

    assert block is None
    assert target_test.stat().st_mode & 0o777 == 0o444
    with pytest.raises(PermissionError):
        target_test.write_text("def test_goalposts_moved():\n    assert True\n", encoding="utf-8")


def test_no_executable_tests_exception_must_be_approved_and_targeted(runtime):
    """No-tests bypass is explicit evidence, not an implicit fallback."""
    _seed_parent()
    impl = "proj/widget/parser#exec"
    _prepare_acceptance(impl)
    fake = _FakeAdapter()
    chokepoint.set_adapter(fake)
    workspace = ledger.read_binding(PARENT)["workspace"]

    outbox.request_child_spawn(
        workspace,
        child_name="parser",
        child_level="L5",
        brief="Implement parser where executable tests do not apply.",
        no_executable_tests_exception_ref="exceptions/missing.json",
    )
    missing = outbox.service_outbox(PARENT)
    assert len(missing) == 1
    assert missing[0].status == "rejected"
    assert "exception artifact" in (missing[0].reason or "")

    outbox.request_child_spawn(
        workspace,
        child_name="parser",
        child_level="L5",
        brief="Implement parser where executable tests do not apply.",
        no_executable_tests_exception_ref=_no_tests_metadata(impl)["no_executable_tests_exception_ref"],
    )
    admitted = outbox.service_outbox(PARENT)
    assert len(admitted) == 1
    assert admitted[0].status == "spawned"
    binding = ledger.read_binding(impl)
    assert binding.get("no_executable_tests_exception_ref")
    assert binding.get("accepted_test_package") is None


def test_test_refresh_state_advances_even_if_l3_inbox_pointer_fails(
    runtime,
    monkeypatch,
):
    """The L4 pending_l3_approval transition is durable; only the L3 wake is best-effort."""
    _seed_l3_and_l4()
    test_author = "proj/widget/acceptance-refresh#exec"
    test_review = "proj/widget/acceptance-refresh#review"
    fake = _FakeAdapter()
    chokepoint.set_adapter(fake)
    workspace = ledger.read_binding(PARENT)["workspace"]

    outbox.request_child_spawn(
        workspace,
        child_name="acceptance-refresh",
        child_level="L5",
        purpose="test_author",
        test_refresh=True,
        test_refresh_for="parser",
    )
    assert outbox.service_outbox(PARENT)[0].status == "spawned"

    test_node_dir = addressing.node_dir(test_author, runtime)
    (test_node_dir / "acceptance.md").write_text(
        "# refreshed acceptance\n\n<!-- trace: { id: T-parser-refresh, serves: [R-010.1], kind: test, level: L4, node: parser } -->\n",
        encoding="utf-8",
    )
    _submit_candidate(monkeypatch, test_node_dir, node_address=test_author)

    from harnessd import daemon

    daemon._REDRIVE_LAST_ATTEMPT.clear()
    daemon._redrive_planned_spawns_best_effort(None, _NoLiveTmux())
    review = ledger.read_binding(test_review)

    original_inbox_has_line = chokepoint._inbox_has_line

    def fail_l3_approval_pointer(inbox, **criteria):
        if criteria.get("type") == "test_refresh_approval_requested":
            raise OSError("simulated inbox append probe failure")
        return original_inbox_has_line(inbox, **criteria)

    monkeypatch.setattr(chokepoint, "_inbox_has_line", fail_l3_approval_pointer)

    passed = chokepoint.pass_gate(
        test_review,
        expected_owner_token=review["owner_token"],
        signal_artifact_seen_at="test-refresh-review-pass",
        verdict_notes="refreshed acceptance is internally coherent",
    )

    assert getattr(passed, "ok", False) is True
    assert ledger.read_binding(PARENT).get("test_refresh_state") == "pending_l3_approval"
    assert ledger.read_binding(PARENT).get("test_refresh_child") == test_author


def test_gate_notification_recovery_replays_missing_candidate_pointer(runtime, monkeypatch):
    """If the best-effort candidate_submitted inbox append is lost, the daemon rebuilds it from
    committed gate state without duplicating it on later sweeps."""
    parent_token = _seed_parent()
    node_dir = _prepare_acceptance(LEAF)
    fake = _FakeAdapter()
    chokepoint.set_adapter(fake)
    result = chokepoint.register_and_spawn_child(
        PARENT,
        LEAF,
        child_level_config=config.get_level_config("L5"),
        brief_content="Build the task serving R-010.1.",
        expected_parent_owner_token=parent_token,
        child_metadata=_no_tests_metadata(LEAF),
    )
    assert getattr(result, "ok", False) is True
    _submit_candidate(monkeypatch, node_dir)

    review_inbox = addressing.inbox_path(REVIEW, runtime)
    review_inbox.unlink()

    from harnessd import daemon

    daemon._recover_gate_notifications_best_effort()
    candidates = [line for line in _jsonl(review_inbox) if line.get("type") == "candidate_submitted"]
    assert len(candidates) == 1
    assert candidates[0].get("candidate") == LEAF
    assert candidates[0].get("gate_id") == ledger.read_binding(LEAF).get("gate_id")

    daemon._recover_gate_notifications_best_effort()
    assert len([line for line in _jsonl(review_inbox)
                if line.get("type") == "candidate_submitted"]) == 1


def test_planned_review_slot_is_not_redriven_before_candidate_submission(runtime):
    """The paired review slot is a gate substrate; it should not open before the candidate packet exists."""
    parent_token = _seed_parent()
    _prepare_acceptance(LEAF)
    fake = _FakeAdapter()
    chokepoint.set_adapter(fake)
    result = chokepoint.register_and_spawn_child(
        PARENT,
        LEAF,
        child_level_config=config.get_level_config("L5"),
        brief_content="Build the task serving R-010.1.",
        expected_parent_owner_token=parent_token,
        child_metadata=_no_tests_metadata(LEAF),
    )
    assert getattr(result, "ok", False) is True
    assert ledger.read_binding(REVIEW).get("state") == "planned"

    from harnessd import daemon

    class _NoLiveTmux:
        def list_targets(self):
            return {}

    daemon._redrive_planned_spawns_best_effort(None, _NoLiveTmux())
    assert [call[0] for call in fake.calls] == [LEAF]
    assert ledger.read_binding(REVIEW).get("state") == "planned"


def test_planned_review_slot_opens_after_candidate_submission(runtime, monkeypatch):
    """After candidate submission, the redrive sweep can open the planned review seat."""
    parent_token = _seed_parent()
    node_dir = _prepare_acceptance(LEAF)
    fake = _FakeAdapter()
    chokepoint.set_adapter(fake)
    result = chokepoint.register_and_spawn_child(
        PARENT,
        LEAF,
        child_level_config=config.get_level_config("L5"),
        brief_content="Build the task serving R-010.1.",
        expected_parent_owner_token=parent_token,
        child_metadata=_no_tests_metadata(LEAF),
    )
    assert getattr(result, "ok", False) is True
    _submit_candidate(monkeypatch, node_dir)

    from harnessd import daemon

    daemon._REDRIVE_LAST_ATTEMPT.clear()

    class _NoLiveTmux:
        def list_targets(self):
            return {}

    daemon._redrive_planned_spawns_best_effort(None, _NoLiveTmux())
    assert [call[0] for call in fake.calls] == [LEAF, REVIEW]
    assert ledger.read_binding(REVIEW).get("state") == "running"
    assert ledger.read_binding(LEAF).get("gate_state") == "candidate_submitted"
    assert not [line for line in _jsonl(addressing.inbox_path(PARENT, runtime))
                if line.get("type") == "gate_failed"]


def test_candidate_submission_clears_stale_planned_review_pane_before_pointer(runtime, monkeypatch):
    """A planned review binding owns no live process; clear a same-address stale pane before the
    candidate pointer can be read by an old reviewer context."""
    parent_token = _seed_parent()
    node_dir = _prepare_acceptance(LEAF)
    fake = _FakeAdapter()
    chokepoint.set_adapter(fake)
    result = chokepoint.register_and_spawn_child(
        PARENT,
        LEAF,
        child_level_config=config.get_level_config("L5"),
        brief_content="Build the task serving R-010.1.",
        expected_parent_owner_token=parent_token,
        child_metadata=_no_tests_metadata(LEAF),
    )
    assert getattr(result, "ok", False) is True

    _submit_candidate(monkeypatch, node_dir)

    assert addressing.session_name_for(REVIEW) in fake.tmux.kills
    candidates = [line for line in _jsonl(addressing.inbox_path(REVIEW, runtime))
                  if line.get("type") == "candidate_submitted"]
    assert len(candidates) == 1


def test_redrive_kills_stale_live_planned_review_session_and_opens_fresh(runtime, monkeypatch):
    """A stale tmux session at a planned #review address must not make redrive skip the fresh reviewer."""
    parent_token = _seed_parent()
    node_dir = _prepare_acceptance(LEAF)
    fake = _FakeAdapter()
    chokepoint.set_adapter(fake)
    result = chokepoint.register_and_spawn_child(
        PARENT,
        LEAF,
        child_level_config=config.get_level_config("L5"),
        brief_content="Build the task serving R-010.1.",
        expected_parent_owner_token=parent_token,
        child_metadata=_no_tests_metadata(LEAF),
    )
    assert getattr(result, "ok", False) is True
    _submit_candidate(monkeypatch, node_dir)
    fake.tmux.kills.clear()

    from harnessd import daemon

    daemon._REDRIVE_LAST_ATTEMPT.clear()

    session = addressing.session_name_for(REVIEW)

    class _StaleLiveReviewTmux:
        def list_targets(self):
            return {session + ":0.0": {"pane_pid": 1234, "pane_dead": 0, "window_activity": "0"}}

    daemon._redrive_planned_spawns_best_effort(None, _StaleLiveReviewTmux())

    assert session in fake.tmux.kills
    assert [call[0] for call in fake.calls] == [LEAF, REVIEW]
    assert ledger.read_binding(REVIEW).get("state") == "running"


def test_review_seat_kickoff_points_at_review_surface_not_generic_plan(runtime, monkeypatch):
    """A review seat starts from the review packet/plan path, not the producer's plan.md."""
    parent_token = _seed_parent()
    node_dir = _prepare_acceptance(LEAF)
    fake = _FakeAdapter()
    chokepoint.set_adapter(fake)
    result = chokepoint.register_and_spawn_child(
        PARENT,
        LEAF,
        child_level_config=config.get_level_config("L5"),
        brief_content="Build the task serving R-010.1.",
        expected_parent_owner_token=parent_token,
        child_metadata=_no_tests_metadata(LEAF),
    )
    assert getattr(result, "ok", False) is True
    _submit_candidate(monkeypatch, node_dir)

    from harnessd import daemon

    daemon._REDRIVE_LAST_ATTEMPT.clear()

    class _NoLiveTmux:
        def list_targets(self):
            return {}

    daemon._redrive_planned_spawns_best_effort(None, _NoLiveTmux())

    review_inbox = addressing.inbox_path(REVIEW, runtime)
    kickoff = [line for line in _jsonl(review_inbox) if line.get("type") == "kickoff"]
    assert len(kickoff) == 1
    message = kickoff[0].get("message") or ""
    assert "Startup order: first create or refresh your native task list" in message
    assert "high-level review-management steps" in message
    assert "before file reads or workspace inspection" in message
    assert ".inbox.review.jsonl" in message
    assert "review-packet.md" in message
    assert "reviews/" in message and "review-plan.md" in message
    assert ".sign-off.review.json" in message
    assert "copy owner_token verbatim" in message
    assert "write plan.md" not in message
    assert "This is a local L5+ review gate" in message
    assert "Complete the review inside this reviewer seat" in message
    assert "The daemon will not open auxiliary reviewer seats" in message
    assert "harness daemon will dispatch independent review-check seats" not in message
    assert "report files alone are not completion evidence" not in message
    assert "Agent/Task/subagent sidechains" not in message
    assert "reviewers/fidelity-coverage/report.md" not in message

    submitted = [line for line in _jsonl(review_inbox) if line.get("type") == "candidate_submitted"]
    assert len(submitted) == 1
    submitted_message = submitted[0].get("message") or ""
    assert "First create or refresh the native task list" in submitted_message
    assert "high-level review-management steps" in submitted_message
    assert "This is a local L5+ review gate" in submitted_message
    assert "Complete the review inside this reviewer seat" in submitted_message
    assert "The daemon will not open auxiliary reviewer seats" in submitted_message
    assert "harness daemon opens independent review-check seats" not in submitted_message
    assert "child-completion inbox wakes" not in submitted_message


def test_higher_review_kickoff_names_exact_full_report_files(runtime):
    producer = "proj/area#exec"
    review = "proj/area#review"
    gate_dir = addressing.node_dir(producer, runtime) / "reviews" / "gate-area"
    gate_dir.mkdir(parents=True)
    packet = gate_dir / "review-packet.md"
    packet.write_text("# packet\n", encoding="utf-8")
    token = fencing.mint_owner_token(review, "review-sa", "review-session", 2)
    ledger.write_binding({
        producer: {
            "node_address": producer,
            "parent_address": "proj#exec",
            "level": "L3",
            "state": "running",
            "generation": 4,
            "lease_epoch": 2,
            "owner_token": fencing.mint_owner_token(producer, "producer-sa", "producer-session", 2),
            "gate_id": "gate-area",
            "gate_review_dir": str(gate_dir),
            "gate_review_packet": str(packet),
        },
        review: {
            "node_address": review,
            "parent_address": "proj#exec",
            "level": "L3+",
            "state": "running",
            "generation": 3,
            "lease_epoch": 2,
            "owner_token": token,
            "gate_for": producer,
            "transcript_path": str(runtime / "review.jsonl"),
        },
    }, _lock_held=True)

    fake = _FakeAdapter()
    chokepoint._deliver_kickoff(
        review,
        SpawnResult(
            ok=True,
            session_uuid="review-session",
            model_used="fake-model / fake-runtime",
            role_variant="L3+#review",
            system_prompt_file="operational/shared/system-prompt.md",
            system_prompt_file_hash="hash",
            tmux_target=addressing.session_name_for(review) + ":0.0",
            transcript_path=str(runtime / "review.jsonl"),
            failure_class=None,
        ),
        fake,
    )

    lines = _jsonl(addressing.inbox_path(review, runtime))
    kickoff = [line for line in lines if line.get("type") == "kickoff"]
    assert len(kickoff) == 1
    message = kickoff[0].get("message") or ""
    assert "Startup order: first create or refresh your native task list" in message
    assert "high-level review-management steps" in message
    assert "before file reads or workspace inspection" in message
    assert "This is a higher-level review gate" in message
    assert "Normal mode is FULL" in message
    assert "Review Mode: FULL" in message
    assert "`## Role Selection`" in message
    assert "naming the four V1 axes" in message
    assert "harness daemon will dispatch independent review-check seats" in message
    assert "every selected check has both its report file and matching" in message
    assert "report files alone are not completion evidence" in message
    assert "end the current turn with waiting status" in message
    assert "long foreground" in message
    assert "Do not author these check reports yourself" in message
    assert "do not use native Agent/Task/subagent sidechains" in message
    assert "missing reviewer substrate is not a SHORT reason" in message
    assert "reviewers/fidelity-coverage/report.md" in message
    assert "reviewers/composition-interface/report.md" in message
    assert "reviewers/evidence-credibility/report.md" in message
    assert "reviewers/risk-readiness/report.md" in message
    assert "area-coverage.md" not in message
    assert "internal-interface-fit.md" not in message
    assert "risk-and-deviation.md" not in message

    chokepoint._notify_review_of_candidate(
        producer,
        ledger.read_binding(producer),
        review,
        {
            "gate_id": "gate-area",
            "gate_review_dir": str(gate_dir),
            "gate_review_packet": str(packet),
        },
    )
    lines = _jsonl(addressing.inbox_path(review, runtime))
    submitted = [line for line in lines if line.get("type") == "candidate_submitted"]
    assert len(submitted) == 1
    submitted_message = submitted[0].get("message") or ""
    assert "First create or refresh the native task list" in submitted_message
    assert "high-level review-management steps" in submitted_message
    assert "This is a higher-level review gate" in submitted_message
    assert "Normal mode is FULL" in submitted_message
    assert "Review Mode: FULL" in submitted_message
    assert "`## Role Selection`" in submitted_message
    assert "naming the four V1 axes" in submitted_message
    assert "harness daemon opens independent review-check seats" in submitted_message
    assert "every selected check has both its report file and matching" in submitted_message
    assert "report files alone are not completion evidence" in submitted_message
    assert "end the current turn with waiting status" in submitted_message
    assert "long foreground" in submitted_message
    assert "missing reviewer substrate is not a SHORT reason" in submitted_message
    assert "reviewers/fidelity-coverage/report.md" in submitted_message
    assert "reviewers/composition-interface/report.md" in submitted_message
    assert "reviewers/evidence-credibility/report.md" in submitted_message
    assert "reviewers/risk-readiness/report.md" in submitted_message
    assert "This is a local L5+ review gate" not in submitted_message


def test_full_higher_review_dispatches_four_auxiliary_check_seats(runtime):
    """A FULL upper gate opens real bounded reviewer actors, not just report filenames."""
    producer = "proj/area/workstream#exec"
    review = "proj/area/workstream#review"
    gate_id = "gate-workstream"
    node_dir = addressing.node_dir(producer, runtime)
    gate_dir = node_dir / "reviews" / gate_id
    gate_dir.mkdir(parents=True, exist_ok=True)
    packet = gate_dir / "review-packet.md"
    packet.write_text("# Review Packet\n\nCandidate pointers.\n", encoding="utf-8")
    (gate_dir / "review-plan.md").write_text(
        "# Review Plan\n\n"
        "Review Mode: FULL\n\n"
        "## Role Selection — four V1 axes\n\n"
        "Use the four V1 axes for this normal workstream composition gate: "
        "fidelity-coverage, composition-interface, evidence-credibility, risk-readiness.\n",
        encoding="utf-8",
    )
    producer_token = fencing.mint_owner_token(producer, "producer-sa", "producer-session", 1)
    review_token = fencing.mint_owner_token(review, "review-sa", "review-session", 1)
    ledger.write_binding(
        {
            producer: {
                "node_address": producer,
                "parent_address": "proj/area#exec",
                "level": "L4",
                "subagent_id": "producer-sa",
                "session_uuid": "producer-session",
                "tmux_target": addressing.session_name_for(producer) + ":0.0",
                "state": "running",
                "generation": 1,
                "lease_epoch": 1,
                "owner_token": producer_token,
                "workspace": str(node_dir),
                "gate_required": True,
                "gate_review_address": review,
                "gate_state": "candidate_submitted",
                "gate_id": gate_id,
                "gate_review_dir": str(gate_dir),
                "gate_review_packet": str(packet),
            },
            review: {
                "node_address": review,
                "parent_address": "proj/area#exec",
                "level": "L4+",
                "role_variant": "L4+#review",
                "subagent_id": "review-sa",
                "session_uuid": "review-session",
                "tmux_target": addressing.session_name_for(review) + ":0.0",
                "state": "running",
                "generation": 1,
                "lease_epoch": 1,
                "owner_token": review_token,
                "workspace": str(node_dir),
                "gate_for": producer,
            },
        },
        _lock_held=True,
    )

    fake = _FakeAdapter()
    chokepoint.set_adapter(fake)
    results = chokepoint.dispatch_review_check_seats(review)

    assert len(results) == 4
    assert all(getattr(result, "ok", False) for result in results)
    live = ledger.all_nodes()
    expected_addresses = []
    signoff_payloads = {}
    for spec in review_dispatch.required_review_check_specs("L4+"):
        check_address = (
            f"proj/area/workstream/reviews/{gate_id}/reviewers/{spec['slug']}#exec"
        )
        expected_addresses.append(check_address)
        check = live[check_address]
        report = review_dispatch.review_check_report_path(gate_dir, spec)
        assert check.get("state") == "running"
        assert check.get("parent_address") == review
        assert "gate_for" not in check
        assert check.get("review_check_for") == review
        assert check.get("review_check_candidate") == producer
        assert check.get("gate_id") == gate_id
        assert check.get("gate_review_packet") == str(packet)
        assert check.get("review_check_axis") == spec["slug"]
        assert check.get("review_check_report") == str(report)
        assert check.get("verdict_authority") is False
        assert (report.parent / "brief.md").is_file()
        assert (addressing.node_dir(check_address, runtime) / ".launch-packet.md").is_file()
        signoff_path = addressing.signoff_path(check_address, runtime)
        assert signoff_path.is_file()
        signoff_payload = json.loads(signoff_path.read_text(encoding="utf-8"))
        assert signoff_payload.get("owner_token") == check.get("owner_token")
        assert signoff_payload.get("signal_path") == str(
            addressing.signal_path(check_address, runtime)
        )
        signoff_payloads[check_address] = signoff_payload
        check_kickoffs = [
            line for line in _jsonl(addressing.inbox_path(check_address, runtime))
            if line.get("type") == "kickoff"
        ]
        assert len(check_kickoffs) == 1
        message = check_kickoffs[0].get("message") or ""
        assert "review-check seat" in message
        assert "Startup order: first create or refresh your native task list" in message
        assert "few high-level steps for this single review check" in message
        assert "before file reads or workspace inspection" in message
        assert str(report) in message
        assert "Do not create or fill plan.md" in message
        assert "Do not write the final gate artifact" in message
        assert "Do not use native Agent/Task/subagent sidechains" in message
        assert "already the independent reviewer context" in message

    assert [call[0] for call in fake.calls] == expected_addresses

    repeat = chokepoint.dispatch_review_check_seats(review)
    assert repeat == []
    assert [call[0] for call in fake.calls] == expected_addresses
    repeated_live = ledger.all_nodes()
    for check_address in expected_addresses:
        signoff_path = addressing.signoff_path(check_address, runtime)
        assert signoff_path.is_file()
        assert (
            json.loads(signoff_path.read_text(encoding="utf-8"))
            == signoff_payloads[check_address]
        )
        assert repeated_live[check_address].get("owner_token") == live[
            check_address
        ].get("owner_token")


def test_review_open_failure_marks_gate_failed_and_wakes_parent(runtime, monkeypatch):
    """A candidate must not sit forever when the review substrate cannot open."""
    parent_token = _seed_parent()
    node_dir = _prepare_acceptance(LEAF)
    fake = _FakeAdapter()
    chokepoint.set_adapter(fake)
    result = chokepoint.register_and_spawn_child(
        PARENT,
        LEAF,
        child_level_config=config.get_level_config("L5"),
        brief_content="Build the task serving R-010.1.",
        expected_parent_owner_token=parent_token,
        child_metadata=_no_tests_metadata(LEAF),
    )
    assert getattr(result, "ok", False) is True
    _submit_candidate(monkeypatch, node_dir)

    from harnessd import daemon

    daemon._REDRIVE_LAST_ATTEMPT.clear()
    failing = _FailingAdapter("runtime_down")
    chokepoint.set_adapter(failing)

    class _NoLiveTmux:
        def list_targets(self):
            return {}

    daemon._redrive_planned_spawns_best_effort(None, _NoLiveTmux())
    assert [call[0] for call in failing.calls] == [REVIEW]
    producer = ledger.read_binding(LEAF)
    review = ledger.read_binding(REVIEW)
    assert producer.get("gate_state") == "gate_failed"
    assert producer.get("gate_failure_class") == "runtime_down"
    assert producer.get("gate_failure_reason") == "review_open_failed"
    assert review.get("state") == "planned"

    parent_lines = _jsonl(addressing.inbox_path(PARENT, runtime))
    failed = [line for line in parent_lines if line.get("type") == "gate_failed"]
    assert len(failed) == 1
    assert failed[0].get("candidate") == LEAF
    assert failed[0].get("review") == REVIEW
    assert not [line for line in parent_lines if line.get("type") in {"child_collapsed", "gate_passed"}]


def test_gate_notification_recovery_replays_missing_gate_failed_pointer(runtime, monkeypatch):
    """A committed gate_failed state repairs a lost parent pointer on the next daemon sweep."""
    parent_token = _seed_parent()
    node_dir = _prepare_acceptance(LEAF)
    fake = _FakeAdapter()
    chokepoint.set_adapter(fake)
    result = chokepoint.register_and_spawn_child(
        PARENT,
        LEAF,
        child_level_config=config.get_level_config("L5"),
        brief_content="Build the task serving R-010.1.",
        expected_parent_owner_token=parent_token,
        child_metadata=_no_tests_metadata(LEAF),
    )
    assert getattr(result, "ok", False) is True
    _submit_candidate(monkeypatch, node_dir)

    from harnessd import daemon

    daemon._REDRIVE_LAST_ATTEMPT.clear()
    chokepoint.set_adapter(_FailingAdapter("runtime_down"))

    class _NoLiveTmux:
        def list_targets(self):
            return {}

    daemon._redrive_planned_spawns_best_effort(None, _NoLiveTmux())
    assert ledger.read_binding(LEAF).get("gate_state") == "gate_failed"

    parent_inbox = addressing.inbox_path(PARENT, runtime)
    parent_inbox.unlink()

    daemon._recover_gate_notifications_best_effort()
    failed = [line for line in _jsonl(parent_inbox) if line.get("type") == "gate_failed"]
    assert len(failed) == 1
    assert failed[0].get("candidate") == LEAF
    assert failed[0].get("review") == REVIEW
    assert failed[0].get("failure_class") == "runtime_down"

    daemon._recover_gate_notifications_best_effort()
    assert len([line for line in _jsonl(parent_inbox) if line.get("type") == "gate_failed"]) == 1


def test_gate_retry_after_review_open_failure_resubmits_and_redrive_opens_review(runtime, monkeypatch):
    """A parent/operator retry is the explicit path out of gate_failed after the substrate is repaired."""
    parent_token = _seed_parent()
    node_dir = _prepare_acceptance(LEAF)
    fake = _FakeAdapter()
    chokepoint.set_adapter(fake)
    result = chokepoint.register_and_spawn_child(
        PARENT,
        LEAF,
        child_level_config=config.get_level_config("L5"),
        brief_content="Build the task serving R-010.1.",
        expected_parent_owner_token=parent_token,
        child_metadata=_no_tests_metadata(LEAF),
    )
    assert getattr(result, "ok", False) is True
    _submit_candidate(monkeypatch, node_dir)

    from harnessd import daemon

    daemon._REDRIVE_LAST_ATTEMPT.clear()
    chokepoint.set_adapter(_FailingAdapter("runtime_down"))

    class _NoLiveTmux:
        def list_targets(self):
            return {}

    daemon._redrive_planned_spawns_best_effort(None, _NoLiveTmux())
    failed = ledger.read_binding(LEAF)
    assert failed.get("gate_state") == "gate_failed"
    assert failed.get("gate_failure_count") == 1

    review_inbox = addressing.inbox_path(REVIEW, runtime)
    review_inbox.unlink()
    chokepoint.set_adapter(fake)

    retry = chokepoint.retry_failed_gate(LEAF, reason="review runtime restored")
    assert getattr(retry, "ok", False) is True
    retried = ledger.read_binding(LEAF)
    assert retried.get("gate_state") == "candidate_submitted"
    assert retried.get("gate_state_before") == "gate_failed"
    assert retried.get("gate_failure_class") is None
    assert retried.get("gate_failure_count") == 1

    candidates = [line for line in _jsonl(review_inbox) if line.get("type") == "candidate_submitted"]
    assert len(candidates) == 1
    assert candidates[0].get("candidate") == LEAF
    assert candidates[0].get("gate_id") == retried.get("gate_id")

    daemon._REDRIVE_LAST_ATTEMPT.clear()
    daemon._redrive_planned_spawns_best_effort(None, _NoLiveTmux())
    assert [call[0] for call in fake.calls] == [LEAF, REVIEW]
    assert ledger.read_binding(REVIEW).get("state") == "running"


def test_gate_retry_refuses_until_missing_review_slot_is_repaired(runtime, monkeypatch):
    """Retry does not recreate the review slot; it only resubmits after the slot is valid again."""
    parent_token = _seed_parent()
    node_dir = _prepare_acceptance(LEAF)
    fake = _FakeAdapter()
    chokepoint.set_adapter(fake)
    result = chokepoint.register_and_spawn_child(
        PARENT,
        LEAF,
        child_level_config=config.get_level_config("L5"),
        brief_content="Build the task serving R-010.1.",
        expected_parent_owner_token=parent_token,
        child_metadata=_no_tests_metadata(LEAF),
    )
    assert getattr(result, "ok", False) is True
    saved_review = copy.deepcopy(ledger.read_binding(REVIEW))
    live = ledger.all_nodes()
    live.pop(REVIEW)
    ledger.write_binding(live, _lock_held=True)

    producer = ledger.read_binding(LEAF)
    (node_dir / "report.md").write_text(
        _claim_report("# report\n\nDone per brief; verified R-010.1.\n"),
        encoding="utf-8",
    )
    _write_signal(LEAF, producer["owner_token"])
    from harnessd import watchdog

    _inject_working_liveness(monkeypatch)
    action = watchdog.check_leaf(
        {"node_address": LEAF, "transcript_path": producer.get("transcript_path"),
         "tmux_target": producer.get("tmux_target")},
        ledger.read_binding(LEAF),
        now=datetime.now(timezone.utc).isoformat(),
    )
    assert getattr(action, "kind", None) == watchdog.NOOP
    assert (getattr(action, "detail", None) or {}).get("reason") == "gate_failed"
    assert ledger.read_binding(LEAF).get("gate_state") == "gate_failed"
    assert ledger.read_binding(LEAF).get("gate_failure_class") == "review_slot_missing"

    refused = chokepoint.retry_failed_gate(LEAF)
    assert getattr(refused, "ok", False) is False
    assert "review slot" in " ".join(refused.errors)
    assert ledger.read_binding(LEAF).get("gate_state") == "gate_failed"

    repaired = ledger.all_nodes()
    repaired[REVIEW] = saved_review
    ledger.write_binding(repaired, _lock_held=True)

    retry = chokepoint.retry_failed_gate(LEAF, reason="review slot restored")
    assert getattr(retry, "ok", False) is True
    assert ledger.read_binding(LEAF).get("gate_state") == "candidate_submitted"
    assert len([line for line in _jsonl(addressing.inbox_path(REVIEW, runtime))
                if line.get("type") == "candidate_submitted"]) == 1


def test_gate_retry_second_same_failure_wakes_parent_again(runtime, monkeypatch):
    """A real failure after a retry is not suppressed as a duplicate of the first failure."""
    parent_token = _seed_parent()
    node_dir = _prepare_acceptance(LEAF)
    fake = _FakeAdapter()
    chokepoint.set_adapter(fake)
    result = chokepoint.register_and_spawn_child(
        PARENT,
        LEAF,
        child_level_config=config.get_level_config("L5"),
        brief_content="Build the task serving R-010.1.",
        expected_parent_owner_token=parent_token,
        child_metadata=_no_tests_metadata(LEAF),
    )
    assert getattr(result, "ok", False) is True
    _submit_candidate(monkeypatch, node_dir)

    from harnessd import daemon

    class _NoLiveTmux:
        def list_targets(self):
            return {}

    daemon._REDRIVE_LAST_ATTEMPT.clear()
    chokepoint.set_adapter(_FailingAdapter("runtime_down"))
    daemon._redrive_planned_spawns_best_effort(None, _NoLiveTmux())
    assert ledger.read_binding(LEAF).get("gate_failure_count") == 1

    retry = chokepoint.retry_failed_gate(LEAF)
    assert getattr(retry, "ok", False) is True

    daemon._REDRIVE_LAST_ATTEMPT.clear()
    chokepoint.set_adapter(_FailingAdapter("runtime_down"))
    daemon._redrive_planned_spawns_best_effort(None, _NoLiveTmux())

    assert ledger.read_binding(LEAF).get("gate_failure_count") == 2
    failed = [line for line in _jsonl(addressing.inbox_path(PARENT, runtime))
              if line.get("type") == "gate_failed"]
    assert len(failed) == 2
    assert [line.get("gate_failure_count") for line in failed] == [1, 2]


def test_parent_accepts_escalated_gate_before_same_address_respawn(runtime, monkeypatch):
    """An escalated gate must be explicitly resolved before the parent can reuse that address."""
    parent_token = _seed_parent()
    node_dir = _prepare_acceptance(LEAF)
    fake = _FakeAdapter()
    chokepoint.set_adapter(fake)
    result = chokepoint.register_and_spawn_child(
        PARENT,
        LEAF,
        child_level_config=config.get_level_config("L5"),
        brief_content="Plan the task serving R-010.1.",
        expected_parent_owner_token=parent_token,
        child_metadata=_no_tests_metadata(LEAF),
    )
    assert getattr(result, "ok", False) is True
    _submit_candidate(monkeypatch, node_dir)

    from harnessd import daemon

    class _NoLiveTmux:
        def list_targets(self):
            return {}

    daemon._REDRIVE_LAST_ATTEMPT.clear()
    daemon._redrive_planned_spawns_best_effort(None, _NoLiveTmux())
    review = ledger.read_binding(REVIEW)
    escalated = chokepoint.escalate_gate(
        REVIEW,
        expected_owner_token=review["owner_token"],
        signal_artifact_seen_at="review-signal-1",
        verdict_notes="parent must decide whether the planned package is acceptable",
    )
    assert getattr(escalated, "ok", False) is True
    assert ledger.read_binding(LEAF).get("gate_state") == "gate_escalated"

    outbox.request_child_spawn(
        ledger.read_binding(PARENT)["workspace"],
        child_name="task",
        child_level="L5",
        brief="Execution brief after parent decision.",
        no_executable_tests_exception_ref=_no_tests_metadata(LEAF)["no_executable_tests_exception_ref"],
    )
    unresolved = outbox.service_outbox(PARENT)
    assert len(unresolved) == 1
    assert unresolved[0].status == "rejected"
    assert "gate_escalated" in (unresolved[0].reason or "")
    assert [call[0] for call in fake.calls] == [LEAF, REVIEW]

    merge_calls = []
    not_applicable = _auto_merge_result(
        outcome="not_applicable",
        merged=False,
        repo_path=None,
    )

    def fake_auto_merge(address):
        live = ledger.read_binding(address)
        merge_calls.append((address, live.get("state"), live.get("gate_state")))
        return not_applicable

    monkeypatch.setattr(merge_gate, "auto_merge_after_gate_pass", fake_auto_merge)

    accepted = chokepoint.accept_escalated_gate(
        LEAF,
        resolver_address=PARENT,
        expected_parent_owner_token=parent_token,
        verdict_notes="Parent accepts the escalated planning candidate.",
    )
    assert getattr(accepted, "ok", False) is True
    assert ledger.read_binding(LEAF).get("state") == "done"
    assert ledger.read_binding(LEAF).get("gate_state") == "gate_passed"
    assert ledger.read_binding(REVIEW).get("state") == "done"
    assert merge_calls == [(LEAF, "done", "gate_passed")]
    gate_lines = [
        row for row in _jsonl(addressing.inbox_path(PARENT, runtime))
        if row.get("type") == "gate_passed"
    ]
    assert len(gate_lines) == 1
    assert gate_lines[0]["merge_outcome"] == "not_applicable"

    outbox.request_child_spawn(
        ledger.read_binding(PARENT)["workspace"],
        child_name="task",
        child_level="L5",
        brief="Execution brief after parent decision.",
        no_executable_tests_exception_ref=_no_tests_metadata(LEAF)["no_executable_tests_exception_ref"],
    )
    respawned = outbox.service_outbox(PARENT)
    assert len(respawned) == 1
    assert respawned[0].status == "spawned"
    assert respawned[0].child_address == LEAF
    assert [call[0] for call in fake.calls] == [LEAF, REVIEW, LEAF]
    assert ledger.read_binding(LEAF).get("state") == "running"
    assert ledger.read_binding(LEAF).get("lease_epoch") > 1


def test_parent_returns_escalated_gate_through_fresh_review_round(runtime, monkeypatch):
    """RETURN delivers the ruling, preserves the cap, and requires a fresh candidate."""
    parent_token = _seed_parent()
    node_dir = _prepare_acceptance(LEAF)
    fake = _FakeAdapter()
    chokepoint.set_adapter(fake)
    spawned = chokepoint.register_and_spawn_child(
        PARENT,
        LEAF,
        child_level_config=config.get_level_config("L5"),
        brief_content="Plan the task serving R-010.1.",
        expected_parent_owner_token=parent_token,
        child_metadata=_no_tests_metadata(LEAF),
    )
    assert getattr(spawned, "ok", False) is True
    _submit_candidate(monkeypatch, node_dir)

    from harnessd import daemon

    daemon._REDRIVE_LAST_ATTEMPT.clear()
    daemon._redrive_planned_spawns_best_effort(None, _NoLiveTmux())
    first_review = ledger.read_binding(REVIEW)
    first_gate_id = ledger.read_binding(LEAF)["gate_id"]
    first_manifest = ledger.read_binding(LEAF)["gate_candidate_artifact_manifest"]
    escalated = chokepoint.escalate_gate(
        REVIEW,
        expected_owner_token=first_review["owner_token"],
        signal_artifact_seen_at="return-round-review-1",
        verdict_notes="the candidate omits the required failure path",
    )
    assert getattr(escalated, "ok", False) is True

    live = ledger.all_nodes()
    live[LEAF]["gate_bounce_count"] = 3
    live[LEAF]["gate_bounce_cap"] = 3
    ledger.write_binding(live, _lock_held=True)
    returned = ipc.handle_request({
        "command": "gate-return",
        "addr": LEAF,
        "resolver_address": PARENT,
        "expected_parent_owner_token": parent_token,
        "verdict_notes": "Repair the missing failure-path behavior and resubmit.",
    })

    assert returned["ok"] is True
    producer = ledger.read_binding(LEAF)
    assert producer["gate_state"] == "gate_bounced"
    assert producer["gate_state_before"] == "gate_escalated"
    assert producer["gate_bounce_count"] == 3
    ruling_records = [
        row for row in (ledger.read_binding(PARENT).get("messages") or {}).values()
        if row.get("tags") == ["gate-return"]
    ]
    assert len(ruling_records) == 1
    ruling = ruling_records[0]
    assert ruling["source"] == PARENT
    assert ruling["target"] == LEAF
    assert ruling["needs_answer"] is False
    assert "Repair the missing failure-path behavior" in (
        addressing.node_dir(PARENT, runtime) / ruling["artifact"]
    ).read_text(encoding="utf-8")
    assert any(
        row.get("type") == "message"
        and row.get("sender") == PARENT
        and row.get("message_id") == ruling["message_id"]
        for row in _jsonl(addressing.inbox_path(LEAF, runtime))
    )
    chokepoint.recover_gate_notification(LEAF)
    assert not [
        row for row in _jsonl(addressing.inbox_path(LEAF, runtime))
        if row.get("type") == "gate_bounced"
    ]

    old_done = chokepoint.submit_gate_candidate(
        LEAF,
        expected_owner_token=producer["owner_token"],
        signal_artifact_seen_at=producer["gate_candidate_signal_artifact_seen_at"],
    )
    assert old_done is None
    assert ledger.read_binding(LEAF)["gate_state"] == "gate_bounced"
    assert ledger.read_binding(REVIEW)["state"] == "done"

    resubmitted = chokepoint.submit_gate_candidate(
        LEAF,
        expected_owner_token=producer["owner_token"],
        signal_artifact_seen_at="return-round-candidate-2",
    )
    assert getattr(resubmitted, "ok", False) is True
    replacement = ledger.read_binding(REVIEW)
    producer = ledger.read_binding(LEAF)
    assert replacement["state"] == "planned"
    assert replacement["owner_token"] != first_review["owner_token"]
    assert producer["gate_id"] != first_gate_id
    assert producer["gate_candidate_artifact_manifest"] != first_manifest
    assert producer["gate_resolution_source"] is None

    daemon._REDRIVE_LAST_ATTEMPT.clear()
    daemon._redrive_planned_spawns_best_effort(None, _NoLiveTmux())
    second_review = ledger.read_binding(REVIEW)
    assert second_review["state"] == "running"
    assert [call[0] for call in fake.calls] == [LEAF, REVIEW, REVIEW]

    recapped = chokepoint.bounce_gate(
        REVIEW,
        expected_owner_token=second_review["owner_token"],
        signal_artifact_seen_at="return-round-review-2",
        verdict_notes="the repaired candidate still misses the failure path",
    )
    assert getattr(recapped, "ok", False) is True
    assert ledger.read_binding(LEAF)["gate_state"] == "gate_escalated"
    assert ledger.read_binding(LEAF)["gate_bounce_count"] == 3
    assert ledger.read_binding(LEAF)["gate_escalation_count"] == 2


def test_gate_escalation_thresholds_elevate_once_then_pause_and_ask_owner(
    runtime,
    monkeypatch,
):
    """Counts below five are quiet; five warns L1 once; ten pauses and asks the owner."""
    l1 = "forge#exec"
    producer_address = LEAF
    review_address = REVIEW
    parent_token = _seed_parent()
    live = ledger.all_nodes()
    parent = live[PARENT]
    l1_token = fencing.mint_owner_token(l1, "l1-sa", "l1-session", 1)
    l1_dir = addressing.node_dir(l1, runtime)
    l1_dir.mkdir(parents=True, exist_ok=True)
    live[l1] = {
        "node_address": l1,
        "parent_address": None,
        "level": "L1",
        "subagent_id": "l1-sa",
        "session_uuid": "l1-session",
        "tmux_target": addressing.session_name_for(l1) + ":0.0",
        "state": "running",
        "generation": 1,
        "lease_epoch": 1,
        "owner_token": l1_token,
        "last_applied_seq": 0,
        "liveness_state": "working",
        "terminal_signal": None,
        "terminal_signal_at": None,
        "gate_crossed_at": None,
        "paused_at": None,
        "workspace": str(l1_dir),
    }
    parent["parent_address"] = l1
    ledger.write_binding(live, _lock_held=True)
    node_dir = _prepare_acceptance(producer_address)
    fake = _FakeAdapter()
    chokepoint.set_adapter(fake)
    spawned = chokepoint.register_and_spawn_child(
        PARENT,
        producer_address,
        child_level_config=config.get_level_config("L5"),
        brief_content="Implement the leaf behavior serving R-010.1.",
        expected_parent_owner_token=parent_token,
        child_metadata=_no_tests_metadata(producer_address),
    )
    assert getattr(spawned, "ok", False) is True
    _submit_candidate(monkeypatch, node_dir, node_address=producer_address)

    from harnessd import daemon

    def escalate_round(round_number):
        daemon._REDRIVE_LAST_ATTEMPT.clear()
        daemon._redrive_planned_spawns_best_effort(None, _NoLiveTmux())
        review = ledger.read_binding(review_address)
        result = chokepoint.escalate_gate(
            review_address,
            expected_owner_token=review["owner_token"],
            signal_artifact_seen_at=f"threshold-review-{round_number}",
            verdict_notes=f"parent ruling round {round_number} did not converge",
        )
        assert getattr(result, "ok", False) is True

    def return_and_resubmit(round_number):
        returned = ipc.handle_request({
            "command": "gate-return",
            "addr": producer_address,
            "resolver_address": PARENT,
            "expected_parent_owner_token": parent_token,
            "verdict_notes": f"owner requests repair round {round_number}",
        })
        assert returned["ok"] is True
        producer = ledger.read_binding(producer_address)
        resubmitted = chokepoint.submit_gate_candidate(
            producer_address,
            expected_owner_token=producer["owner_token"],
            signal_artifact_seen_at=f"threshold-candidate-{round_number + 1}",
        )
        assert getattr(resubmitted, "ok", False) is True

    for round_number in range(1, 5):
        escalate_round(round_number)
        assert ledger.read_binding(producer_address)["gate_escalation_count"] == round_number
        assert not [
            row for row in _jsonl(addressing.inbox_path(l1, runtime))
            if row.get("type") == "gate_escalation_nonconverging"
        ]
        assert ledger.read_binding(producer_address).get("paused_at") is None
        return_and_resubmit(round_number)

    escalate_round(5)
    fifth_notices = [
        row for row in _jsonl(addressing.inbox_path(l1, runtime))
        if row.get("type") == "gate_escalation_nonconverging"
    ]
    assert len(fifth_notices) == 1
    assert fifth_notices[0]["gate"] == producer_address
    assert fifth_notices[0]["gate_escalation_count"] == 5
    assert "parent judgment is not converging" in fifth_notices[0]["message"]
    assert ledger.read_binding(producer_address).get("paused_at") is None
    return_and_resubmit(5)

    escalate_round(6)
    assert len([
        row for row in _jsonl(addressing.inbox_path(l1, runtime))
        if row.get("type") == "gate_escalation_nonconverging"
    ]) == 1
    assert ledger.read_binding(producer_address).get("paused_at") is None
    return_and_resubmit(6)

    for round_number in range(7, 10):
        escalate_round(round_number)
        assert ledger.read_binding(producer_address).get("paused_at") is None
        return_and_resubmit(round_number)

    escalate_round(10)
    producer = ledger.read_binding(producer_address)
    assert producer["gate_escalation_count"] == 10
    assert producer.get("paused_at") is not None
    assert ledger.read_binding(l1).get("paused_at") is None
    typed_rows = [
        row for row in ledger.load_wal()
        if row.get("event") == "gate_escalation_nonconverging"
        and row.get("node_address") == producer_address
    ]
    assert typed_rows[-1]["binding_delta"]["gate_escalation_count"] == 10
    questions = messages.open_questions_for(l1)
    convergence = [
        row for row in questions
        if row.get("metadata", {}).get("kind") == "gate-convergence"
    ]
    assert len(convergence) == 1
    assert convergence[0]["source"] == PARENT
    assert convergence[0]["target"] == l1
    assert convergence[0]["needs_answer"] is True
    assert "OWNER-FACING" in (
        addressing.node_dir(PARENT, runtime) / convergence[0]["artifact"]
    ).read_text(encoding="utf-8")


def test_gate_passed_child_can_be_reworked_by_same_address_respawn(runtime, monkeypatch):
    """Post-PASS repair is a fresh same-address incarnation with a fresh gate cycle."""
    parent_token = _seed_parent()
    node_dir = _prepare_acceptance(LEAF)
    fake = _FakeAdapter()
    chokepoint.set_adapter(fake)
    result = chokepoint.register_and_spawn_child(
        PARENT,
        LEAF,
        child_level_config=config.get_level_config("L5"),
        brief_content="Build the task serving R-010.1.",
        expected_parent_owner_token=parent_token,
        child_metadata=_no_tests_metadata(LEAF),
    )
    assert getattr(result, "ok", False) is True
    _submit_candidate(monkeypatch, node_dir)

    from harnessd import daemon

    daemon._REDRIVE_LAST_ATTEMPT.clear()
    daemon._redrive_planned_spawns_best_effort(None, _NoLiveTmux())
    review = ledger.read_binding(REVIEW)
    review_token = review["owner_token"]
    first_gate_id = ledger.read_binding(LEAF).get("gate_id")

    passed = chokepoint.pass_gate(
        REVIEW,
        expected_owner_token=review_token,
        signal_artifact_seen_at="review-pass-1",
        verdict_notes="candidate accepted",
    )
    assert getattr(passed, "ok", False) is True
    first_pass = ledger.read_binding(LEAF)
    assert first_pass.get("state") == "done"
    assert first_pass.get("gate_state") == "gate_passed"
    assert first_pass.get("gate_id") == first_gate_id
    assert ledger.read_binding(REVIEW).get("state") == "done"

    outbox.request_child_spawn(
        ledger.read_binding(PARENT)["workspace"],
        child_name="task",
        child_level="L5",
        brief=(
            "Repair the already accepted artifact per the parent compatibility finding. "
            "Preserve the prior accepted work and update only the trace defect."
        ),
        no_executable_tests_exception_ref=_no_tests_metadata(LEAF)["no_executable_tests_exception_ref"],
    )
    respawned = outbox.service_outbox(PARENT)
    assert len(respawned) == 1
    assert respawned[0].status == "spawned"
    assert respawned[0].child_address == LEAF

    rework = ledger.read_binding(LEAF)
    fresh_review = ledger.read_binding(REVIEW)
    assert [call[0] for call in fake.calls] == [LEAF, REVIEW, LEAF]
    assert rework.get("state") == "running"
    assert rework.get("gate_state") is None
    assert rework.get("gate_id") is None
    assert rework.get("terminal_signal") is None
    assert rework.get("gate_review_address") == REVIEW
    assert rework.get("lease_epoch") > first_pass.get("lease_epoch")
    assert "Repair the already accepted artifact" in (
        addressing.node_dir(LEAF, runtime) / "brief.md"
    ).read_text(encoding="utf-8")
    assert fresh_review.get("state") == "planned"
    assert fresh_review.get("gate_for") == LEAF
    assert fresh_review.get("owner_token") != review_token


def test_candidate_submitted_terminal_review_slot_marks_gate_failed(runtime, monkeypatch):
    """A review seat that dies after candidate submission fails closed and wakes the parent."""
    parent_token = _seed_parent()
    node_dir = _prepare_acceptance(LEAF)
    fake = _FakeAdapter()
    chokepoint.set_adapter(fake)
    result = chokepoint.register_and_spawn_child(
        PARENT,
        LEAF,
        child_level_config=config.get_level_config("L5"),
        brief_content="Build the task serving R-010.1.",
        expected_parent_owner_token=parent_token,
        child_metadata=_no_tests_metadata(LEAF),
    )
    assert getattr(result, "ok", False) is True
    _submit_candidate(monkeypatch, node_dir)
    assert ledger.read_binding(LEAF).get("gate_state") == "candidate_submitted"

    live = ledger.all_nodes()
    review = copy.deepcopy(live[REVIEW])
    review["state"] = "failed"
    review["terminal_signal"] = "FAILED"
    live[REVIEW] = review
    ledger.write_binding(live, _lock_held=True)

    from harnessd import daemon

    daemon._recover_gate_notifications_best_effort()
    producer = ledger.read_binding(LEAF)
    assert producer.get("gate_state") == "gate_failed"
    assert producer.get("gate_failure_reason") == "review_slot_terminal"
    assert producer.get("gate_failure_class") == "review_slot_terminal"
    assert producer.get("gate_failure_count") == 1

    failed = [line for line in _jsonl(addressing.inbox_path(PARENT, runtime))
              if line.get("type") == "gate_failed"]
    assert len(failed) == 1
    assert failed[0].get("candidate") == LEAF
    assert failed[0].get("review") == REVIEW

    daemon._recover_gate_notifications_best_effort()
    assert len([line for line in _jsonl(addressing.inbox_path(PARENT, runtime))
                if line.get("type") == "gate_failed"]) == 1


def test_fresh_submission_reopens_gate_failed_terminal_review_slot(runtime, monkeypatch):
    """A fresh candidate self-heals its correctly bound terminal review incarnation."""
    parent_token = _seed_parent()
    node_dir = _prepare_acceptance(LEAF)
    fake = _FakeAdapter()
    chokepoint.set_adapter(fake)
    result = chokepoint.register_and_spawn_child(
        PARENT,
        LEAF,
        child_level_config=config.get_level_config("L5"),
        brief_content="Build the task serving R-010.1.",
        expected_parent_owner_token=parent_token,
        child_metadata=_no_tests_metadata(LEAF),
    )
    assert getattr(result, "ok", False) is True
    _submit_candidate(monkeypatch, node_dir)

    live = ledger.all_nodes()
    terminal_review = copy.deepcopy(live[REVIEW])
    terminal_review["state"] = "failed"
    terminal_review["terminal_signal"] = "FAILED"
    live[REVIEW] = terminal_review
    ledger.write_binding(live, _lock_held=True)

    from harnessd import daemon

    daemon._recover_gate_notifications_best_effort()
    failed_producer = ledger.read_binding(LEAF)
    failed_review = ledger.read_binding(REVIEW)
    assert failed_producer.get("gate_state") == "gate_failed"
    assert failed_review.get("state") == "failed"
    old_review_token = failed_review.get("owner_token")
    old_review_epoch = failed_review.get("lease_epoch")

    resubmitted = chokepoint.submit_gate_candidate(
        LEAF,
        expected_owner_token=failed_producer.get("owner_token"),
        signal_artifact_seen_at="round-5-fresh-candidate",
    )

    assert getattr(resubmitted, "ok", False) is True
    producer_after = ledger.read_binding(LEAF)
    review_after = ledger.read_binding(REVIEW)
    assert producer_after.get("gate_state_before") == "gate_failed"
    assert producer_after.get("gate_state") == "candidate_submitted"
    assert review_after.get("state") == "planned"
    assert review_after.get("gate_for") == LEAF
    assert review_after.get("owner_token") != old_review_token
    assert review_after.get("lease_epoch") > old_review_epoch
    submitted = [
        line
        for line in _jsonl(addressing.inbox_path(REVIEW, runtime))
        if line.get("type") == "candidate_submitted"
        and line.get("gate_id") == producer_after.get("gate_id")
    ]
    assert len(submitted) == 1


def test_fresh_submission_does_not_replace_live_matching_review_slot(runtime):
    """The widened terminal recovery cannot replace an already-live review incarnation."""
    parent_token = _seed_parent()
    node_dir = _prepare_acceptance(LEAF)
    fake = _FakeAdapter()
    chokepoint.set_adapter(fake)
    result = chokepoint.register_and_spawn_child(
        PARENT,
        LEAF,
        child_level_config=config.get_level_config("L5"),
        brief_content="Build the task serving R-010.1.",
        expected_parent_owner_token=parent_token,
        child_metadata=_no_tests_metadata(LEAF),
    )
    assert getattr(result, "ok", False) is True
    (node_dir / "report.md").write_text(
        _claim_report(),
        encoding="utf-8",
    )

    live = ledger.all_nodes()
    producer = copy.deepcopy(live[LEAF])
    producer.update({
        "gate_state": "gate_failed",
        "gate_candidate_signal_artifact_seen_at": "prior-candidate",
        "gate_failure_class": "review_slot_terminal",
    })
    live_review = copy.deepcopy(live[REVIEW])
    live_review["state"] = "running"
    live_review["liveness_state"] = "working"
    live[LEAF] = producer
    live[REVIEW] = live_review
    ledger.write_binding(live, _lock_held=True)
    before_identity = {
        key: live_review.get(key)
        for key in (
            "owner_token",
            "lease_epoch",
            "session_uuid",
            "subagent_id",
            "tmux_target",
        )
    }
    kills_before = list(fake.tmux.kills)

    resubmitted = chokepoint.submit_gate_candidate(
        LEAF,
        expected_owner_token=producer.get("owner_token"),
        signal_artifact_seen_at="fresh-candidate",
    )

    assert getattr(resubmitted, "ok", False) is True
    assert ledger.read_binding(LEAF).get("gate_state") == "candidate_submitted"
    review_after = ledger.read_binding(REVIEW)
    assert review_after.get("state") == "running"
    assert {
        key: review_after.get(key)
        for key in before_identity
    } == before_identity
    assert fake.tmux.kills == kills_before


# ---------------------------------------------------------------------------------------
# N tester/implementer pairs under ONE L4 (owner model, pinned 2026-07-17): an L4 that
# decomposes its chunk into multiple tasks binds each implementation to ITS OWN task's
# accepted test package. The enforcement is per-named-sibling — nothing in the machinery
# forces the historical one-pair shape (all observed runs were single-task-sized builds).
# ---------------------------------------------------------------------------------------

def _seed_task_pair(l4_addr, task, *, tests_body):
    """Seed one gate-passed test-author child + one impl child dir with the matching tests/."""
    tester = f"{l4_addr.split('#', 1)[0]}/{task}-tests#exec"
    impl_dir = addressing.node_dir(f"{l4_addr.split('#', 1)[0]}/{task}-impl#exec", ledger.RUNTIME_ROOT)
    tester_dir = addressing.node_dir(tester, ledger.RUNTIME_ROOT)
    for d in (tester_dir / "tests", impl_dir / "tests"):
        d.mkdir(parents=True, exist_ok=True)
        (d / f"test_{task}.py").write_text(tests_body, encoding="utf-8")
    live = dict(ledger.all_nodes())
    live[tester] = {
        "node_address": tester,
        "parent_address": l4_addr,
        "level": "L5",
        "state": "done",
        "gate_state": "gate_passed",
        "child_purpose": "test_author",
        "generation": 1,
        "lease_epoch": 1,
        "last_applied_seq": 0,
    }
    ledger.write_binding(live, _lock_held=True)
    return f"{task}-tests"


def test_multiple_task_pairs_bind_per_named_package_under_one_l4(runtime):
    """Two tasks under one L4: each implementation naming ITS task's accepted package passes the
    enforcement; naming a nonexistent package still blocks. Pins machinery support for the
    multi-task L4 (the owner's decompose-and-divide model) so it cannot regress."""
    l4 = "proj/widget#exec"
    _seed_parent(parent=l4, level="L4")
    slug_a = _seed_task_pair(l4, "alpha", tests_body="def test_alpha():\n    assert True\n")
    slug_b = _seed_task_pair(l4, "beta", tests_body="def test_beta():\n    assert True\n")

    for task, slug in (("alpha", slug_a), ("beta", slug_b)):
        block = chokepoint.accepted_test_package_spawn_block(
            l4,
            f"proj/widget/{task}-impl#exec",
            child_level="L5",
            child_metadata={"accepted_test_package": slug},
        )
        assert block is None, f"pair {task} must bind cleanly to its own package; got {block!r}"

    block = chokepoint.accepted_test_package_spawn_block(
        l4,
        "proj/widget/alpha-impl#exec",
        child_level="L5",
        child_metadata={"accepted_test_package": "no-such-tester"},
    )
    assert block is not None and block[0] == "accepted_test_package_not_test_author" or (
        block is not None
    ), f"a nonexistent package must block; got {block!r}"
