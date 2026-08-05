"""
CLI options + fixtures for the secret-scan request client
(ngts.tools.secret_scan).

Options are declared here rather than in the shared rootdir conftest: pytest
loads a conftest as an "initial" plugin (so its pytest_addoption runs) whenever
the invoked test path is inside the conftest's directory - which is exactly how
MARS runs this tool (by --test_script path). The per-option fixtures below let
the test consume the values without touching request.config directly.

--project / --release_name are required; --dut_version / --dut_name /
--notify are optional overrides (read from the DUT when omitted).
setup_name (rootdir fixture) and session_id (the autouse fixture from the
ngts.tools.infra plugin) are consumed directly - not redefined here.

pytest_configure fetches the DUT/server creds from Vault for a local run;
--mars_run turns that off (see the note there).
"""
import logging
from typing import Optional

import pytest

logger = logging.getLogger()


def pytest_addoption(parser) -> None:
    parser.addoption('--project', action='store', required=True,
                     help='request project / store bucket (required)')
    parser.addoption('--release_name', action='store', required=True,
                     help='release name being tested, e.g. 25.01.0630 (req)')
    parser.addoption('--dut_version', action='store', default=None,
                     help='DUT sw version override (else read from the DUT)')
    parser.addoption('--dut_name', action='store', default=None,
                     help='DUT name override (else read from the DUT)')
    parser.addoption('--notify', action='store', default=None,
                     help='comma-separated e-mails to notify')
    parser.addoption('--mars_run', action='store_true', default=False,
                     help='run is driven by MARS: the creds are already in '
                          'the environment, so skip the Vault fetch')


@pytest.fixture
def project(request) -> str:
    return request.config.getoption('--project')


@pytest.fixture
def release_name(request) -> str:
    return request.config.getoption('--release_name')


@pytest.fixture
def dut_version(request) -> Optional[str]:
    return request.config.getoption('--dut_version')


@pytest.fixture
def dut_name(request) -> Optional[str]:
    return request.config.getoption('--dut_name')


@pytest.fixture
def notify(request) -> Optional[str]:
    return request.config.getoption('--notify')


def pytest_configure(config) -> None:
    """
    Export the DUT / server creds from Vault - local runs only.

    The ngts topology is built in pytest_sessionstart (the allure_report
    plugin), which reads EVERY player's user/password from the environment -
    not just the DUT's, but all players in the setup (servers, VMs, ...). A
    fixture runs too late (test setup happens after sessionstart), so this is
    a pytest_configure hook: it runs before any pytest_sessionstart.

    Under MARS the load_secrets pre-session step has already exported the
    creds, and the fetch must not run there (the player has no `vault` binary,
    so it would raise) - hence --mars_run, which MARS passes to say so
    explicitly rather than have us guess from the environment. The fetch is
    also best-effort: a failure is logged, never fatal, so a host without
    Vault still gets to the point where the missing cred is reported clearly.
    """
    if config.getoption('--mars_run'):
        logger.debug('secret_scan: MARS run, creds already exported')
        return
    try:
        from ngts.nvos_tools.infra.VaultClient import VaultClient
        VaultClient.fetch_and_export_secrets()
    except Exception as exc:
        err_msg = str(exc)
        logger.warning(f'secret_scan: Vault fetch skipped ({err_msg}); '
                       f'relying on the existing environment')
