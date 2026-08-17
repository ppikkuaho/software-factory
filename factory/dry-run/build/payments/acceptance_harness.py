"""Shared acceptance-test harness — FROZEN, read-only to the executor.

Helpers ONLY. No implementation of any payments component lives here. The executor
authors `payments_impl.build_system()`; these tests import it through `make_system()`.

Authored from L2/decisions/interfaces-locked.md before any build code exists (M51).
"""
import importlib
import unittest


def make_system(**kwargs):
    """Build a wired PaymentsSystem from the executor's `payments_impl` module.

    The executor MUST provide `payments_impl.build_system(**kwargs)`. kwargs are
    forwarded so a specific test can request, e.g., a real concurrent store.
    """
    try:
        impl = importlib.import_module("payments_impl")
    except ImportError as exc:  # pragma: no cover - guidance for the executor
        raise unittest.SkipTest(
            "payments_impl not found. The executor must provide "
            "payments_impl.build_system(); these frozen tests bind to it."
        ) from exc
    if not hasattr(impl, "build_system"):
        raise AssertionError(
            "payments_impl must expose build_system() returning a wired PaymentsSystem "
            "(see build/payments/acceptance.md)."
        )
    return impl.build_system(**kwargs)


# Canonical fixture values used across tests. Money is (minor_units, currency).
ORDER_A = "order-AAA"
ORDER_B = "order-BBB"
AMOUNT = (4999, "USD")          # $49.99, integer minor units (ADR-002 / R-002)
OTHER_AMOUNT = (1000, "USD")
TOKEN = "tok_visa_test"         # tokenized card ref; never a raw PAN (R-014)
RAW_PAN = "4242424242424242"    # used ONLY to assert it never reaches the gateway


def tag(result):
    """Return the variant tag of a SubmitResult/ChargeResult/WebhookResult.

    The contract is a tagged union; the executor may model it however it likes as
    long as the variant is recoverable as a string via `.tag` or `result['tag']`.
    """
    if hasattr(result, "tag"):
        return result.tag
    if isinstance(result, dict) and "tag" in result:
        return result["tag"]
    raise AssertionError(f"result {result!r} exposes no .tag / ['tag'] variant name")


def field(result, name):
    if hasattr(result, name):
        return getattr(result, name)
    if isinstance(result, dict) and name in result:
        return result[name]
    raise AssertionError(f"result {result!r} missing field {name!r}")
