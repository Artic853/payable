"""The mandate is the only thing standing between a confused agent and your money."""

import time

import pytest

from payable.mandate import MandateError, issue_mandate, verify_mandate
from payable.models import FailureCode


def test_valid_mandate_passes():
    mandate = issue_mandate("user:a", "agent:b", 500_000, ["keyboard"])
    verify_mandate(mandate, 400_000, "keyboard")  # does not raise


def test_tampered_amount_cap_is_rejected():
    """Raising the cap client-side must not raise the cap server-side."""
    mandate = issue_mandate("user:a", "agent:b", 100_000)
    mandate.max_amount_paise = 10_000_000  # attacker edits the field

    with pytest.raises(MandateError) as exc:
        verify_mandate(mandate, 5_000_000, "keyboard")
    assert exc.value.code is FailureCode.MANDATE_REJECTED


def test_amount_over_cap_is_rejected_as_over_budget():
    mandate = issue_mandate("user:a", "agent:b", 100_000)
    with pytest.raises(MandateError) as exc:
        verify_mandate(mandate, 100_001, "keyboard")
    assert exc.value.code is FailureCode.OVER_BUDGET


def test_expired_mandate_is_rejected():
    mandate = issue_mandate("user:a", "agent:b", 100_000, ttl_seconds=-1)
    with pytest.raises(MandateError) as exc:
        verify_mandate(mandate, 10_000, "keyboard")
    assert "expired" in exc.value.message


def test_category_outside_scope_is_rejected():
    mandate = issue_mandate("user:a", "agent:b", 100_000, ["keyboard"])
    with pytest.raises(MandateError) as exc:
        verify_mandate(mandate, 10_000, "monitor")
    assert exc.value.code is FailureCode.MANDATE_REJECTED


def test_empty_category_scope_allows_any_category():
    mandate = issue_mandate("user:a", "agent:b", 100_000, [])
    verify_mandate(mandate, 10_000, "monitor")


def test_signature_is_bound_to_the_signing_secret():
    mandate = issue_mandate("user:a", "agent:b", 100_000, secret="secret-one")
    with pytest.raises(MandateError):
        verify_mandate(mandate, 10_000, "keyboard", secret="secret-two")


def test_mandate_expiry_is_in_the_future_by_the_requested_ttl():
    before = time.time()
    mandate = issue_mandate("user:a", "agent:b", 1, ttl_seconds=600)
    assert mandate.expires_at >= before + 599
