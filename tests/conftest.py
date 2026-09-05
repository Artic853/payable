import pytest
from fastapi.testclient import TestClient

from payable.audit import AUDIT
from payable.config import SETTINGS
from payable.payments import reset_gateway
from payable.server.app import app
from payable.state import configure_payments, reset_all

# Captured before any test can move them.
DEFAULT_SEED = SETTINGS.seed
DEFAULT_FAILURE_RATE = SETTINGS.payment_failure_rate


@pytest.fixture(scope="session", autouse=True)
def isolate_audit_log(tmp_path_factory):
    """Send test audit events to a temp file.

    The log is append-only by design, so without this a test asserting on a
    fixed run_id would see events left behind by the previous pytest run -- and
    the repo's own data/audit.jsonl would fill up with test noise.
    """
    if AUDIT._redis is None:  # noqa: SLF001 - inspecting the backend on purpose
        AUDIT.jsonl_path = tmp_path_factory.mktemp("audit") / "audit.jsonl"
        AUDIT.jsonl_path.touch()
    yield


@pytest.fixture(autouse=True)
def clean_state(isolate_audit_log):
    """Every test starts from a known catalog, gateway and empty audit log.

    `configure_payments` deliberately mutates global settings *and* both gateway
    instances -- the storefront keeps its own, exactly as a separate sales
    channel would. A benchmark test that dials the decline rate to 1.0 would
    otherwise leave it there for every test that follows, so the defaults are
    reapplied on both sides of each test rather than merely restored at the end.
    """
    configure_payments(seed=DEFAULT_SEED, failure_rate=DEFAULT_FAILURE_RATE)
    reset_gateway()
    reset_all(clear_audit=True)
    yield
    configure_payments(seed=DEFAULT_SEED, failure_rate=DEFAULT_FAILURE_RATE)
    reset_gateway()
    reset_all()


@pytest.fixture
def client():
    return TestClient(app)
