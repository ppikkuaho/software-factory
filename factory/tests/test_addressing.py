"""The ONE canonical address→workspace mapping — NESTED (the one-spine), the root of the layout fix.

The flat `-`/`__`-collapse (my FORK-BRIEF-LANDING misread of "collapsed-address") put every node in its
OWN flat dir, so a coordinator's WORKROOT did NOT contain its children — breaking the canonical write
model (ARCHITECTURE.md:122 "each level writes within its own workspace and creates child workspaces
within it"; IMPLEMENTATION-PLAN "write-jailed to its node SUBTREE"). Nesting by PATH restores it: a
child's dir sits UNDER its parent's, so the existing "write your WORKROOT subtree" jail rule gives a
coordinator write access to its children's nodes — with NO jail-rule change.

The `#seat` (exec/review) is NOT a path segment (that would break child-nesting). Two seats share the
node WORKSPACE (the reviewer reads the executor's work in place); only the per-ACTOR metadata files
(the sign-off signal, the wake inbox) are seat-qualified so they don't clobber each other.
"""

from pathlib import Path

import harnessd.addressing as addressing


def test_seat_is_split_off_the_path():
    assert addressing.split_address("proj/widget/parser#exec") == ("proj/widget/parser", "exec")
    assert addressing.split_address("proj/widget/parser#review") == ("proj/widget/parser", "review")
    # no '#' -> the default exec seat
    assert addressing.split_address("proj/widget/parser") == ("proj/widget/parser", "exec")


def test_node_dir_is_nested_by_path_seat_stripped(tmp_path):
    d = addressing.node_dir("proj/widget/parser#exec", tmp_path)
    assert d == tmp_path / "nodes" / "proj" / "widget" / "parser"
    # exec and review share the SAME node workspace (two actors, one node)
    assert addressing.node_dir("proj/widget/parser#review", tmp_path) == d


def test_child_dir_nests_under_parent_dir(tmp_path):
    """THE load-bearing property: a child's node dir is a SUBPATH of its parent's — so the parent's
    WORKROOT (its own node dir) contains it, and the subtree-write-jail lets the parent seed it."""
    parent = addressing.node_dir("proj/widget#exec", tmp_path)
    child = addressing.node_dir("proj/widget/parser#exec", tmp_path)
    assert str(child).startswith(str(parent) + "/"), "child dir must nest UNDER the parent dir"


def test_per_seat_metadata_files_do_not_collide(tmp_path):
    """exec and review share the node dir, so their per-actor metadata (sign-off signal, wake inbox)
    MUST be seat-qualified — else the L5/L5+ pair would clobber each other's sign-off."""
    sig_exec = addressing.signal_path("proj/widget/parser#exec", tmp_path)
    sig_review = addressing.signal_path("proj/widget/parser#review", tmp_path)
    assert sig_exec != sig_review, "exec and review need distinct signal files"
    assert sig_exec.parent == sig_review.parent, "but both live in the one shared node dir"
    assert "exec" in sig_exec.name and "review" in sig_review.name
    inbox_exec = addressing.inbox_path("proj/widget/parser#exec", tmp_path)
    inbox_review = addressing.inbox_path("proj/widget/parser#review", tmp_path)
    assert inbox_exec != inbox_review and inbox_exec.parent == inbox_review.parent


def test_l1_root_is_a_single_top_segment(tmp_path):
    d = addressing.node_dir("root#exec", tmp_path)
    assert d == tmp_path / "nodes" / "root"


def test_subtree_containment_a_parent_workroot_holds_its_child(tmp_path):
    """Stated as the jail property: the realpath of a child node dir is under the realpath of the
    parent node dir, so render_profile's `(allow file-write* (subpath WORKROOT))` covers the child."""
    parent_workroot = addressing.node_dir("proj/payments/gateway#exec", tmp_path).resolve()
    child_workroot = addressing.node_dir("proj/payments/gateway/stripe-client#exec", tmp_path).resolve()
    assert child_workroot.is_relative_to(parent_workroot), \
        "the child node dir must be inside the parent's WORKROOT subtree (the seed-able subtree)"
    # a COUSIN is NOT under this parent (no cross-subtree write)
    cousin = addressing.node_dir("proj/checkout/cart#exec", tmp_path).resolve()
    assert not cousin.is_relative_to(parent_workroot)
