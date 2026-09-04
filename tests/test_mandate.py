"""The mandate is the only thing standing between a confused agent and your money."""

import time

import pytest

from payable.mandate import (
    ALG_ED25519,
    ALG_HMAC,
    ED25519_AVAILABLE,
    MandateError,
    PrincipalKeyring,
    issue_mandate,
    verify_mandate,
)
from payable.models import FailureCode

pytestmark = pytest.mark.skipif(
    not ED25519_AVAILABLE, reason="asymmetric mandates require `cryptography`"
)


@pytest.fixture
def keyring():
    """A keyring per test, so enrolment in one cannot affect another."""
    return PrincipalKeyring()


def sign(keyring, principal="user:a", amount=500_000, categories=None, **kwargs):
    return issue_mandate(
        principal=principal,
        agent_id="agent:b",
        max_amount_paise=amount,
        allowed_categories=categories,
        keyring=keyring,
        **kwargs,
    )


# -- the happy path --------------------------------------------------------

def test_valid_mandate_passes(keyring):
    mandate = sign(keyring, categories=["keyboard"])
    assert mandate.alg == ALG_ED25519
    verify_mandate(mandate, 400_000, "keyboard", keyring=keyring)  # does not raise


def test_issuing_enrols_the_principals_public_key(keyring):
    sign(keyring, principal="user:new")
    assert keyring.knows("user:new")
    # Only the public half is exportable.
    assert len(bytes.fromhex(keyring.export_public_keys()["user:new"])) == 32


# -- forgery ---------------------------------------------------------------

def test_tampered_amount_cap_is_rejected(keyring):
    """Raising the cap client-side must not raise the cap server-side."""
    mandate = sign(keyring, amount=100_000)
    mandate.max_amount_paise = 10_000_000  # attacker edits the field

    with pytest.raises(MandateError) as exc:
        verify_mandate(mandate, 5_000_000, "keyboard", keyring=keyring)
    assert exc.value.code is FailureCode.MANDATE_REJECTED
    assert "signature invalid" in exc.value.message


def test_tampered_category_scope_is_rejected(keyring):
    mandate = sign(keyring, categories=["cable"])
    mandate.allowed_categories = ["monitor"]

    with pytest.raises(MandateError):
        verify_mandate(mandate, 10_000, "monitor", keyring=keyring)


def test_a_mandate_cannot_be_reassigned_to_another_principal(keyring):
    mandate = sign(keyring, principal="user:a")
    keyring.enrol("user:victim")
    mandate.principal = "user:victim"

    with pytest.raises(MandateError):
        verify_mandate(mandate, 10_000, "keyboard", keyring=keyring)


def test_another_principals_key_cannot_sign_for_you(keyring):
    """A signature only authorizes spending by the principal that made it."""
    mandate = sign(keyring, principal="user:attacker")
    keyring.enrol("user:victim")
    mandate.principal = "user:victim"

    with pytest.raises(MandateError):
        verify_mandate(mandate, 10_000, "keyboard", keyring=keyring)


def test_unknown_principal_has_no_key_to_verify_against(keyring):
    mandate = sign(keyring, principal="user:a")
    keyring.forget("user:a")

    with pytest.raises(MandateError) as exc:
        verify_mandate(mandate, 10_000, "keyboard", keyring=keyring)
    assert "no registered public key" in exc.value.message


def test_garbage_signature_is_rejected_not_crashed(keyring):
    mandate = sign(keyring)
    mandate.signature = "not-hex"
    with pytest.raises(MandateError):
        verify_mandate(mandate, 10_000, "keyboard", keyring=keyring)


# -- algorithm downgrade ---------------------------------------------------

def test_symmetric_mandate_is_refused_for_an_enrolled_principal(keyring):
    """The attack the asymmetric scheme exists to stop.

    The HMAC secret has a documented default. If the merchant accepted a
    symmetric mandate for a principal that has enrolled a public key, anyone
    holding that secret could spend as them.
    """
    keyring.enrol("user:a")
    forged = issue_mandate(
        principal="user:a", agent_id="agent:evil", max_amount_paise=9_999_999,
        alg=ALG_HMAC, secret="dev-only-mandate-secret", keyring=keyring,
    )
    assert forged.alg == ALG_HMAC

    with pytest.raises(MandateError) as exc:
        verify_mandate(forged, 9_000_000, "monitor", keyring=keyring)
    assert exc.value.code is FailureCode.MANDATE_REJECTED
    assert ALG_ED25519 in exc.value.message


def test_symmetric_mandates_still_work_for_unenrolled_principals(keyring):
    """The dev fallback stays usable where no public key exists."""
    mandate = issue_mandate(
        principal="user:dev-only", agent_id="agent:b", max_amount_paise=100_000,
        alg=ALG_HMAC, secret="shared", keyring=keyring,
    )
    verify_mandate(mandate, 50_000, "keyboard", secret="shared", keyring=keyring)


def test_swapping_the_alg_field_invalidates_the_signature(keyring):
    """`alg` is inside the signed payload, so it cannot be edited in flight."""
    mandate = issue_mandate(
        principal="user:dev-only", agent_id="agent:b", max_amount_paise=100_000,
        alg=ALG_HMAC, secret="shared", keyring=keyring,
    )
    mandate.alg = ALG_ED25519
    with pytest.raises(MandateError):
        verify_mandate(mandate, 50_000, "keyboard", secret="shared", keyring=keyring)


# -- the business rules ----------------------------------------------------

def test_amount_over_cap_is_rejected_as_over_budget(keyring):
    mandate = sign(keyring, amount=100_000)
    with pytest.raises(MandateError) as exc:
        verify_mandate(mandate, 100_001, "keyboard", keyring=keyring)
    assert exc.value.code is FailureCode.OVER_BUDGET


def test_spending_exactly_the_cap_is_allowed(keyring):
    mandate = sign(keyring, amount=100_000)
    verify_mandate(mandate, 100_000, "keyboard", keyring=keyring)


def test_expired_mandate_is_rejected(keyring):
    mandate = sign(keyring, ttl_seconds=-1)
    with pytest.raises(MandateError) as exc:
        verify_mandate(mandate, 10_000, "keyboard", keyring=keyring)
    assert "expired" in exc.value.message


def test_category_outside_scope_is_rejected(keyring):
    mandate = sign(keyring, categories=["keyboard"])
    with pytest.raises(MandateError) as exc:
        verify_mandate(mandate, 10_000, "monitor", keyring=keyring)
    assert exc.value.code is FailureCode.MANDATE_REJECTED


def test_empty_category_scope_allows_any_category(keyring):
    mandate = sign(keyring, categories=[])
    verify_mandate(mandate, 10_000, "monitor", keyring=keyring)


def test_mandate_expiry_is_in_the_future_by_the_requested_ttl(keyring):
    before = time.time()
    mandate = sign(keyring, ttl_seconds=600)
    assert mandate.expires_at >= before + 599
