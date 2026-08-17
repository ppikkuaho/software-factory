"""Q7 — Opus 5 model refresh contract.

The proven Claude Code 2.1.152 pin is intentionally retained: it serves the explicit
``claude-opus-5`` id and attributes the response to that model.  Q7 changes every established
Opus seat, but does not pre-empt the separately calibrated semantic-cell/product-probe registries.
"""

from pathlib import Path

import harnessd.config as config


ROOT = Path(__file__).resolve().parents[1]
ESTABLISHED_OPUS_SEATS = ("L1", "L2", "L3", "L4", "L2+", "L3+", "L4+", "L5+")


def test_keeps_proven_claude_code_pin_and_maps_exact_opus5_id():
    assert config.PINNED_BINARY_VERSION == "2.1.152"
    assert config.CC_MODEL_FLAGS == {"opus-5.0": "claude-opus-5"}


def test_every_established_opus_seat_moves_to_opus5():
    for seat in ESTABLISHED_OPUS_SEATS:
        level_config = config.LEVEL_CONFIGS[seat]
        assert level_config.model == "opus-5.0", seat
        assert level_config.runtime == "claude-code", seat


def test_sol_executor_does_not_move():
    l5 = config.LEVEL_CONFIGS["L5"]
    assert (l5.model, l5.runtime) == ("gpt-5.6-sol", "codex")


def test_canonical_runtime_map_names_opus5_and_kept_pin():
    model_map = (ROOT / "operational/shared/runtime-and-model-map.md").read_text(encoding="utf-8")
    assert "claude-opus-5" in model_map
    assert "Opus 5.0" in model_map
    assert "2.1.152" in model_map
