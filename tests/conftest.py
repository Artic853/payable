import pytest
from fastapi.testclient import TestClient

from payable.audit import AUDIT
from payable.payments import reset_gateway
from payable.server.app import app
from payable.state import reset_all


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
    """Every test starts from the catalog as it is on disk and an empty log."""
    reset_gateway()
    reset_all(clear_audit=True)
    yield
    # Tests that swap in a custom gateway must not leak it into the next test.
    reset_gateway()
    reset_all()


@pytest.fixture
def client():
    return TestClient(app)
