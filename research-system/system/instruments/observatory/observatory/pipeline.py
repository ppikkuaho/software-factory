"""Observatory pipeline (C1 §3) — the on-demand per-run pass.

Wires the three layers: L1 mechanical screens (always) -> L2 triage (one claude -p,
unless --skip-llm) -> L3 deep dives (claude -p per above-minor flag, unless
--skip-llm). Emits the report card, copies the L1/L2/L3 artifacts to the out dir,
and merge-updates the mechanical statistics store. The trace-only work dir is the
LLM sandbox (the raw session bundle is never added to it).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import tempfile

from . import audit as audit_mod
from . import report, screens as screens_mod, statistics, triage as triage_mod
from .deepdive import run_deep_dives
from .spine import load_spine
from .triage import above_minor_targets, run_triage


def _repo_root():
    cwd = os.getcwd()
    if os.path.isdir(os.path.join(cwd, "system")) and os.path.isfile(
        os.path.join(cwd, "AGENTS.md")
    ):
        return cwd
    d = os.path.dirname(os.path.abspath(__file__))
    while True:
        if os.path.isdir(os.path.join(d, "system")) and os.path.isfile(
            os.path.join(d, "AGENTS.md")
        ):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return cwd
        d = parent


def _sha256_file(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _digest_provenance(digest_path):
    """Best-effort read of a digest's frontmatter for provenance stamping."""
    prov = {"path": digest_path, "sha256": _sha256_file(digest_path)}
    try:
        with open(digest_path, encoding="utf-8") as fh:
            text = fh.read(4000)
    except OSError:
        return prov
    m = re.search(r"prompt_template:\s*\n(?:.*\n)*?\s*sha256:\s*([0-9a-f]{64})", text)
    if m:
        prov["template_sha256"] = m.group(1)
    for key in ("model", "generation_path"):
        mm = re.search(rf"^{key}:\s*(.+)$", text, re.MULTILINE)
        if mm:
            prov[key] = mm.group(1).strip()
    return prov


def _resolve_digest(work_dir, digest_path, model, skip_llm):
    """Return (digest_prov_dict, generated_bool). Generates via the trace reader's
    digest pipeline only if none is supplied and none sits in the work dir."""
    if skip_llm:
        return {}, False
    if digest_path:
        return _digest_provenance(digest_path), False
    wd_digest = os.path.join(work_dir, "digest.md")
    if os.path.isfile(wd_digest):
        return _digest_provenance(wd_digest), False
    # generate one over the trace (counts a claude -p call)
    from trace_reader.digests import build_digest

    build_digest(work_dir, model=model)
    return _digest_provenance(wd_digest), True


def _uncreated_temp_dir(session_id):
    """Choose a temp work path without creating it.

    Audit mode must validate every destination before the first mkdir.  The
    normal tempfile probes candidates by creating a file, so audit mode selects
    from the same environment variables and conventional directories using
    metadata only.  The extraction layer creates the validated candidate later.
    """
    candidates = [
        os.environ[name]
        for name in ("TMPDIR", "TEMP", "TMP")
        if os.environ.get(name)
    ]
    candidates.extend(("/tmp", "/var/tmp", "/usr/tmp"))
    try:
        candidates.append(os.getcwd())
    except OSError:
        pass

    base = None
    for raw_candidate in candidates:
        candidate = os.path.abspath(raw_candidate)
        if os.path.isdir(candidate) and os.access(
            candidate, os.W_OK | os.X_OK
        ):
            base = candidate
            break
    if base is None:
        raise FileNotFoundError("no usable observatory temporary directory")

    for _ in range(100):
        candidate = os.path.join(base, f"ht-observe-{session_id}-{secrets.token_hex(8)}")
        if not os.path.lexists(candidate):
            return candidate
    raise FileExistsError("could not select an unused observatory work directory")


def run_observatory(bundle_path, out_dir=None, work_dir=None, skip_llm=False,
                    digest_path=None, model=None, generator=None, stats_path=None,
                    k=15, audit_runtime=None):
    bundle_path = os.path.abspath(bundle_path)
    session_id = screens_mod.session_id_from_bundle(bundle_path)
    root = _repo_root()
    if out_dir is None:
        out_dir = os.path.join(root, "readout", "observatory", session_id)
    if stats_path is None:
        stats_path = os.path.join(root, "readout", "statistics.json")

    runtime_audit = None
    if audit_runtime is not None:
        # Capture and validate the complete join before creating output/work/stats
        # paths or giving a generator any chance to run.
        runtime_audit = audit_mod.capture_runtime_audit(audit_runtime, bundle_path)
        if work_dir is None:
            work_dir = _uncreated_temp_dir(session_id)
        audit_mod.validate_write_destinations(
            runtime_audit,
            out_dir=os.path.abspath(out_dir),
            work_dir=os.path.abspath(work_dir),
            stats_path=os.path.abspath(stats_path),
        )
    elif work_dir is None:
        work_dir = tempfile.mkdtemp(prefix=f"ht-observe-{session_id}-")
    os.makedirs(out_dir, exist_ok=True)

    call_count = 0

    # --- L1 mechanical screens (always) ---
    screens, orient_result, relevant_globs, subject_cwd = \
        screens_mod.extract_and_screen(
            bundle_path, work_dir, k=k, runtime_audit=runtime_audit
        )

    # --- spine ---
    spine_entries, spine_source, spine_path = load_spine(root)

    # --- digest (L2 substrate) ---
    digest_prov, digest_generated = _resolve_digest(work_dir, digest_path, model, skip_llm)
    if digest_generated:
        call_count += 1

    triage_record = None
    deep_dive_records = []
    targets = []
    if not skip_llm:
        # --- L2 triage (one call) ---
        triage_record = run_triage(work_dir, digest_prov["path"], spine_entries,
                                   spine_source, generator=generator, model=model)
        call_count += 1
        targets = above_minor_targets(triage_record["result"])
        # --- L3 deep dives (one call per above-minor flag; no cap, P-1) ---
        deep_dive_records = run_deep_dives(work_dir, targets, generator=generator, model=model)
        call_count += len(deep_dive_records)

    # --- report card ---
    meta = {
        "skip_llm": skip_llm,
        "digest": digest_prov,
        "extra_notes": [
            f"Spine loaded from {'the committed file' if spine_source == 'file' else 'the handoff §5 fallback seed'}"
            + (f" ({spine_path})" if spine_path else "") + ".",
            f"claude -p calls this pass: {call_count} "
            f"({'digest+' if digest_generated else ''}"
            f"{'0 (skip-llm)' if skip_llm else f'1 triage + {len(deep_dive_records)} deep dive(s)'}).",
        ],
    }
    card_text, card_meta = report.build_report_card(
        screens, spine_entries, spine_source, triage_record, deep_dive_records,
        targets, meta)
    card_path = report.write_report_card(out_dir, card_text)

    # --- archive the layer artifacts alongside the card ---
    shutil.copyfile(os.path.join(work_dir, "screens.json"),
                    os.path.join(out_dir, "screens.json"))
    if triage_record is not None:
        with open(os.path.join(out_dir, "triage.json"), "w", encoding="utf-8") as fh:
            fh.write(json.dumps(triage_record, ensure_ascii=False, indent=2) + "\n")
    for i, r in enumerate(deep_dive_records, 1):
        with open(os.path.join(out_dir, f"deep-dive-{i}.json"), "w", encoding="utf-8") as fh:
            fh.write(json.dumps(r, ensure_ascii=False, indent=2) + "\n")

    # --- statistics store (mechanical-only, idempotent) ---
    statistics.merge_update(stats_path, screens)

    return {
        "session_id": session_id,
        "out_dir": out_dir,
        "work_dir": work_dir,
        "report_card": card_path,
        "screens_path": os.path.join(out_dir, "screens.json"),
        "statistics_path": stats_path,
        "spine_source": spine_source,
        "relevant_globs": relevant_globs,
        "subject_cwd": subject_cwd,
        "skip_llm": skip_llm,
        "claude_call_count": call_count,
        "digest_generated": digest_generated,
        "targets_above_minor": len(targets),
        "deep_dives": len(deep_dive_records),
        "anchor_unanchored": len(card_meta["anchor_warnings"]["unanchored"]),
        "screens": screens,
        "triage_record": triage_record,
        "deep_dive_records": deep_dive_records,
        "runtime_audit": runtime_audit.provenance if runtime_audit is not None else None,
    }
