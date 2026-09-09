from __future__ import annotations

import logging
import pytest

from ngts.tools.test_utils import allure_utils as allure
from ngts.helpers import system_helpers
from ngts.ngts_types import EnginesT

logger = logging.getLogger(__name__)

pytest_plugins = (
    "ngts.common.plugins.valgrind.plugin",
    'ngts.common.plugins.nvos_run_policy',
)

# Docker side-channel used by NVOS valgrind-compatible builds.
_VALGRIND_FLAG_FILE = "/etc/valgrind_run"

# These are all services on croc/mamba where we successfully run valgrind
_SERVICE_LIST: tuple[str, ...] = (  # cspell:disable
    'aaastatsd.service',
    'configmgrd.service',
    'containerd.service',
    'countermgrd.service',
    'cron.service',
    'dbus.service',
    'featured.service',
    'getty@tty1.service',
    'haveged.service',
    'health-statsd.service',
    'hostcfgd.service',
    'hw-management-sync.service',
    'nginx-authenticator.service',
    'nginx.service',
    'nvued.service',
    'pam-auth.service',
    'portsyncmgrd.service',
    'rasdaemon.service',
    'rsyslog.service',
    'serial-getty@ttyS0.service',
    'smartmontools.service',
    'ssh.service',
    'statemgrd.service',
    'stats-reportd.service',
)  # cspell:enable

_DOCKERS_LIST: tuple[str, ...] = (  # cspell:disable
    'pmon',
    'lldp',
)  # cspell:enable


def pytest_addoption(parser: pytest.Parser):
    group = parser.getgroup("Valgrind")
    group.addoption('--vg-services', nargs='*', default=_SERVICE_LIST, help='List of services to run valgrind on')
    group.addoption('--vg-dockers', nargs='*', default=_DOCKERS_LIST, help='List of dockers to run valgrind on')


@pytest.fixture
def valgrind_services(request: pytest.FixtureRequest):
    def _ensure_service_suffix(service: str) -> str:
        if not service.endswith('.service'):
            return f"{service}.service"
        return service

    return list(map(_ensure_service_suffix, request.config.getoption('--vg-services')))


@pytest.fixture
def valgrind_dockers(request: pytest.FixtureRequest):
    return request.config.getoption('--vg-dockers')


@pytest.fixture(scope="session", autouse=True)
def ensure_valgrind_compatible_build(request: pytest.FixtureRequest, engines: EnginesT) -> None:
    """
    Abort the whole pytest session early when the DUT build is not valgrind-compatible.

    A valgrind-compatible build exposes `/etc/valgrind_run` inside the target NVOS containers.
    When the file is missing, running the valgrind enable/disable flows is meaningless, so we
    stop the whole MARS session immediately via `pytest.exit()`.
    """
    dockers: list[str] = list(request.config.getoption("--vg-dockers") or [])
    if not dockers:
        return

    sudo_engine = system_helpers.PrefixEngine(engines.dut, "sudo")
    check_docker_cmd = f"docker exec %s sh -c 'test -f {_VALGRIND_FLAG_FILE} && echo __PRESENT__ || echo __MISSING__'"
    missing: list[str] = []
    details: list[str] = []

    with allure.step("Check if valgrind flag file is present in dockers"):
        for docker in dockers:
            with allure.independent_step(f"Check if valgrind flag file is present in {docker}"):
                if "__MISSING__" in (out := sudo_engine.run_cmd(check_docker_cmd % docker, validate=True)):
                    missing.append(docker)
                    details.append(f"{docker}: {out.strip()}")
                    logger.error(out)

    if missing:
        msg = "Build is not valgrind compatible: missing %s in docker(s): %s\n%s"
        logger.error(msg, _VALGRIND_FLAG_FILE, ', '.join(missing), '\n'.join(details))
        pytest.exit(msg, 1)
