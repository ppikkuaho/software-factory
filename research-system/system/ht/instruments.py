"""Instrument + epoch legality (B4 §9; U1 — a REAL mechanism, not a stub check).

Reads system/instruments/registry.json. A claim's stamped epoch must satisfy
0 <= epoch <= tree.epoch. If the claim names an instrument 'suite@version', the
registry must contain that id+version AND the epoch must fall within that suite's
legality window [from, to] (to=null == open-ended). The registry ships empty, so
in Phase 0 any --instrument is rejected until a suite is installed — the rejection
test installs a synthetic suite to prove the mechanism fires both ways.
"""

from __future__ import annotations

from . import jsonio
from .errors import HtError
from .paths import Root


def check_epoch(epoch: int, tree_epoch: int) -> None:
    if not (0 <= epoch <= tree_epoch):
        raise HtError(
            f"epoch stamp {epoch} outside legal range 0..{tree_epoch} "
            f"(tree.epoch) (B4 §9 epoch legality)"
        )


def parse_instrument(spec: str) -> tuple[str, str]:
    if "@" not in spec:
        raise HtError(
            f"instrument '{spec}' must be 'suite@version' (B4 §9 instrument legality)"
        )
    suite_id, version = spec.rsplit("@", 1)
    if not suite_id or not version:
        raise HtError(
            f"instrument '{spec}' must be 'suite@version' (B4 §9 instrument legality)"
        )
    return suite_id, version


def check_instrument(root: Root, spec: str, epoch: int) -> None:
    suite_id, version = parse_instrument(spec)
    registry = jsonio.load(root.registry)
    for suite in registry.get("suites", []):
        if suite["id"] == suite_id and suite["version"] == version:
            frm = suite["epochs"]["from"]
            to = suite["epochs"]["to"]
            if epoch < frm or (to is not None and epoch > to):
                to_str = "inf" if to is None else str(to)
                raise HtError(
                    f"instrument '{spec}' not legal at epoch {epoch} "
                    f"(suite epochs {frm}..{to_str}) (B4 §9 instrument legality)"
                )
            return
    raise HtError(
        f"instrument '{spec}' absent from registry "
        f"(no suite id+version match) (B4 §9 instrument legality)"
    )
