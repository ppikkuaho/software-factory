"""Increment 0 Done-test (clause 1): the harnessd package + its Increment-0 modules import cleanly.

Scope per IMPLEMENTATION-PLAN.md "Increment 0 — repo skeleton + config seats":
the package skeleton plus `config.py` and `states.py` are the Increment-0 modules.
Later-increment modules (ledger, executor, ...) are intentionally NOT imported here.

These tests fail RED until the builder creates the package + the two modules.
"""

import importlib

import pytest


def test_harnessd_package_imports():
    """The `harnessd` package itself imports (the repo skeleton exists)."""
    pkg = importlib.import_module("harnessd")
    assert pkg is not None
    # A package, not a stray top-level module.
    assert hasattr(pkg, "__path__"), "harnessd must be an importable package (have __path__)"


def test_harnessd_is_a_real_package_not_just_a_namespace_dir():
    """RED-until-built guard: the bare `harnessd/` directory (holding only the plan doc)
    already resolves as an IMPLICIT namespace package on modern Python, so a plain
    `import harnessd` is not a meaningful "skeleton exists" signal. Require a real
    regular package — i.e. a `harnessd/__init__.py` the builder must add (the §3 module
    table lists `harnessd/` as a code package, not a namespace dir). Namespace packages
    have `__file__ is None` / no `__init__` spec; regular packages have an __init__ module."""
    pkg = importlib.import_module("harnessd")
    spec = pkg.__spec__
    assert spec is not None, "harnessd must have an import spec"
    # Implicit namespace packages report origin == 'namespace' (and __file__ is None).
    assert getattr(pkg, "__file__", None) is not None, (
        "harnessd is an implicit namespace dir, not a real package — "
        "the builder must add harnessd/__init__.py"
    )
    assert spec.origin not in (None, "namespace"), (
        f"harnessd resolved as a namespace package (origin={spec.origin!r}); "
        "a real harnessd/__init__.py is required"
    )


def test_states_module_imports():
    """`harnessd.states` (Increment-0 module) imports cleanly."""
    states = importlib.import_module("harnessd.states")
    assert states is not None


def test_config_module_imports():
    """`harnessd.config` (Increment-0 module) imports cleanly."""
    config = importlib.import_module("harnessd.config")
    assert config is not None


@pytest.mark.parametrize("modname", ["harnessd", "harnessd.states", "harnessd.config"])
def test_increment0_modules_each_import(modname):
    """Each Increment-0 import surface module imports without error (no partial skeleton)."""
    mod = importlib.import_module(modname)
    assert mod is not None
