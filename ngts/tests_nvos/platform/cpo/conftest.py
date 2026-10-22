import pytest

from ngts.tests_nvos.system.gnmi.helpers import verify_gnmi_client_tools_installed
from ngts.tests_nvos.system.telemetry.telemetry_watch import gnmi_server_enabled


@pytest.fixture(scope="session", autouse=True)
def gnmi_ready(engines):
    verify_gnmi_client_tools_installed()
    with gnmi_server_enabled():
        yield
