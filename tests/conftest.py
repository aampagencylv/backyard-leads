"""pytest config — register the asyncio marker so we don't need a plugin
config file. Stub out the audit log writer so tests don't try to hit a
real DB.
"""
import asyncio
import pytest


# Bridge async tests to the runner without requiring pytest-asyncio
# (keeps the test deps lightweight). If pytest-asyncio is installed,
# its marker handler takes precedence; otherwise our wrapper runs.
@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem):
    if asyncio.iscoroutinefunction(pyfuncitem.function):
        loop = asyncio.new_event_loop()
        try:
            funcargs = pyfuncitem.funcargs
            testargs = {arg: funcargs[arg] for arg in pyfuncitem._fixtureinfo.argnames}
            loop.run_until_complete(pyfuncitem.function(**testargs))
        finally:
            loop.close()
        return True


@pytest.fixture(autouse=True)
def stub_audit_log(monkeypatch):
    """The audit log writer hits the DB — stub it out so guard tests can
    run without a live database. Tests still verify the guards fire."""
    async def _noop(**kwargs):
        return None
    monkeypatch.setattr("app.services.email_sender._log_outbound_audit", _noop, raising=False)
