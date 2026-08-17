"""Bundle discovery.

A *bundle* is the main session .jsonl plus the sibling subagent transcripts. Two
on-disk layouts are supported:

  * canonical (.claude live tree):   <dir>/<session-uuid>.jsonl
                                      <dir>/<session-uuid>/subagents/agent-*.jsonl
  * flattened (frozen fixtures):     <dir>/<name>.jsonl
                                      <dir>/subagents/agent-*.jsonl

Each subagent .jsonl may carry a sibling .meta.json sidecar whose `toolUseId`
joins it to a tool_use block in the main transcript. A missing subagents dir is
fine (prefix bundles / no subagents spawned).
"""

from __future__ import annotations

import glob
import json
import os
from dataclasses import dataclass


@dataclass
class SubagentFile:
    actor: str            # "agent-<id>" (the .jsonl stem)
    path: str             # absolute path to the .jsonl
    meta_path: str | None
    meta: dict | None     # parsed sidecar, or None
    tool_use_id: str | None


@dataclass
class Bundle:
    main_path: str
    subagents: list       # list[SubagentFile], sorted by actor name


def _subagents_dir(main_path: str) -> str | None:
    main_abs = os.path.abspath(main_path)
    d = os.path.dirname(main_abs)
    stem = os.path.splitext(os.path.basename(main_abs))[0]
    # canonical layout first, then flattened
    for cand in (os.path.join(d, stem, "subagents"), os.path.join(d, "subagents")):
        if os.path.isdir(cand):
            return cand
    return None


def _read_meta(meta_path: str) -> tuple[dict | None, str | None]:
    try:
        with open(meta_path) as fh:
            meta = json.load(fh)
    except Exception:
        return None, None
    # spawn join key spelled `toolUseId` in the sidecar (system rows use `toolUseID`)
    tid = meta.get("toolUseId") or meta.get("toolUseID")
    return meta, tid


def discover(main_path: str) -> Bundle:
    main_abs = os.path.abspath(main_path)
    subagents = []
    subdir = _subagents_dir(main_abs)
    if subdir is not None:
        for jsonl in sorted(glob.glob(os.path.join(subdir, "agent-*.jsonl"))):
            actor = os.path.splitext(os.path.basename(jsonl))[0]
            meta_path = os.path.splitext(jsonl)[0] + ".meta.json"
            if not os.path.isfile(meta_path):
                meta_path = None
            meta, tid = _read_meta(meta_path) if meta_path else (None, None)
            subagents.append(
                SubagentFile(
                    actor=actor,
                    path=os.path.abspath(jsonl),
                    meta_path=os.path.abspath(meta_path) if meta_path else None,
                    meta=meta,
                    tool_use_id=tid,
                )
            )
    subagents.sort(key=lambda s: s.actor)
    return Bundle(main_path=main_abs, subagents=subagents)
