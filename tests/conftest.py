"""pytest config — stub the audit log writer so tests don't try to hit a
real DB unless they explicitly opt in. pytest-asyncio (mode=auto in
pytest.ini) handles the async-test bridge.

Anything that needs a real test database uses the db_session /
bmp_world fixtures in tests/fixtures.py, which spin up an in-memory
SQLite per-test.
"""
import os
import sys

import pytest

# Make `app` importable when running pytest from anywhere
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def stub_audit_log(monkeypatch):
    """The outbound audit log writer hits the DB — stub it out so guard
    tests can run without a live database. Tests still verify the guards
    fire; the audit-write itself is a side effect, not the contract."""
    async def _noop(**kwargs):
        return None
    monkeypatch.setattr("app.services.email_sender._log_outbound_audit", _noop, raising=False)
