"""Phase-6 behavioural-eval harness (design/BEHAVIOURAL-VALIDATION.md).

Spawns a REAL agent-under-test (jailed, dialog-free) in interactive tmux, drives a multi-turn
conversation against a COUNTERPART-SIMULATOR (an LLM playing the human/parent that answers the
agent's questions + escalations, driven by a scenario brief), captures the transcript + artifacts,
and scores against a rubric. The interactive-tmux transport is deliberate: it makes freezes/stalls
VISIBLE (capture-pane), which `-p` mode hides.
"""
