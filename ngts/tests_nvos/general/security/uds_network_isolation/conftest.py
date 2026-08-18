"""Fixtures for UDS Network Isolation tests."""
import logging
from typing import Callable, Dict, Iterator, List

import pytest

from devts.infra.tools.connection_tools.linux_ssh_engine import LinuxSshEngine

from ngts.nvos_tools.Devices.IbDevice import (
    BlackMambaSwitch,
    CrocodileSwitch,
    CrocodileSimxSwitch,
)
from ngts.nvos_tools.nmx.Manager import Manager
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.general.security.nmx_cert import helpers as nmx_helpers
from ngts.tests_nvos.general.security.nmx_rbac.conftest import (  # noqa: F401  (re-export fixtures for pytest discovery)
    enable_cluster,
    verify_grpc_tools_installed,
)
from ngts.tests_nvos.general.security.security_test_tools.constants import AaaConsts
from ngts.tests_nvos.general.security.security_test_tools.security_test_utils import set_local_users
from ngts.tests_nvos.general.security.security_test_tools.tool_classes.UserInfo import UserInfo
from ngts.tools.test_utils import allure_utils as allure
from ngts.tools.test_utils.switch_recovery import generate_strong_password

from ngts.tests_nvos.general.security.uds_network_isolation import constants as uds_constants
from ngts.tests_nvos.general.security.uds_network_isolation.helpers import (
    parse_ss_listeners,
    extract_listening_ports,
    install_nmap,
    is_nmap_installed,
    run_ss_tulpn,
    run_ss_unix,
)

logger = logging.getLogger(__name__)


# ── Setup-type detection ─────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def is_xdr(devices) -> bool:
    """True when running on an XDR setup (Crocodile / BlackMamba)."""
    return isinstance(devices.dut, (CrocodileSwitch, CrocodileSimxSwitch, BlackMambaSwitch))


@pytest.fixture(scope="session")
def component_list(is_xdr) -> List[uds_constants.ComponentPaths]:
    """UDS component list appropriate for this setup."""
    return uds_constants.XDR_COMPONENTS if is_xdr else uds_constants.NVLINK_COMPONENTS


@pytest.fixture(scope="session")
def published_ports(is_xdr) -> Dict[int, str]:
    """Published (external) TCP port allowlist for this setup."""
    return uds_constants.PUBLISHED_PORTS_XDR if is_xdr else uds_constants.PUBLISHED_PORTS_NVLINK


@pytest.fixture(scope="session")
def internal_probe_ports(is_xdr) -> Dict[int, str]:
    """Internal ports that must NOT be exposed."""
    return (
        uds_constants.INTERNAL_PROBE_PORTS_XDR if is_xdr
        else uds_constants.INTERNAL_PROBE_PORTS_NVLINK
    )


@pytest.fixture(scope="session")
def scanner_engine(engines) -> LinuxSshEngine:
    """External host used for nmap scans, with nmap installed."""
    engine = getattr(engines, "sonic_mgmt", None) or getattr(engines, "server", None)
    if engine is None:
        pytest.skip("No external engine (sonic-mgmt or server) available to run nmap")

    with allure.step("Verify nmap installation on scanner host"):
        if is_nmap_installed(engine):
            return engine

    for attempt in range(1, 4):
        with allure.step(f"attempt {attempt}: install nmap on scanner host"):
            install_nmap(engine)
        with allure.step("verify nmap is installed"):
            if is_nmap_installed(engine):
                return engine

    raise AssertionError("failed to install nmap on scanner host")


# ── UDS path list (flattened from components) ────────────────────────────────

@pytest.fixture(scope="session")
def uds_paths(component_list) -> List[str]:
    """Flat list of all UDS paths applicable to this setup."""
    paths = []
    for comp in component_list:
        paths.extend(comp.uds_paths)
    return paths


# ── Monitor user ─────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def monitor_user(engines, devices) -> Iterator[UserInfo]:
    """Create a local monitor-role user for the test session; remove on teardown."""
    monitor_role = AaaConsts.MONITOR
    user = UserInfo(
        username=uds_constants.MONITOR_USER,
        password=generate_strong_password(),
        role=monitor_role,
    )
    logger.info("Creating local monitor user: %s", user.username)
    set_local_users(engines, [user], apply=True)

    yield user

    with allure.step(f"Remove monitor user '{user.username}' after test session"):
        try:
            System().aaa.user.user_id[user.username].unset(apply=True).verify_result()
            logger.info("Removed monitor user: %s", user.username)
        except Exception:
            logger.warning("Failed to remove monitor user: %s", user.username, exc_info=True)


@pytest.fixture(scope="session")
def monitor_engine(engines, monitor_user) -> Iterator[LinuxSshEngine]:
    """SSH engine connected as the monitor user."""
    engine = LinuxSshEngine(engines.dut.ip, monitor_user.username, monitor_user.password)
    yield engine
    try:
        engine.disconnect()
    except Exception:
        logger.debug("Failed to disconnect monitor engine", exc_info=True)


# ── Cluster app manager state (auto-restore) ─────────────────────────────────

@pytest.fixture(scope="function")
def enable_app_manager(enable_cluster) -> Iterator[Callable[[Manager], None]]:
    """Enable per-app cluster managers and restore their state on teardown.

    The shared ``enable_cluster`` fixture only stops the cluster container; it
    does NOT restore per-app NVUE manager config (e.g. ``nv set cluster app
    name nmx-c manager state enabled``). Without an explicit restore, manager
    state set by one test leaks into subsequent tests in the same session.

    Yields a callable ``enable(manager)`` that:
      * sets ``manager.state`` to enabled (via ``enable_cluster_app_manager_state``);
      * registers the manager so it is restored via
        ``restore_cluster_app_manager_state`` when the fixture tears down.

    The fixture depends on ``enable_cluster`` so the restore step runs **before**
    the cluster is stopped (pytest tears down dependents first).
    """
    enabled: List[Manager] = []

    def _enable(manager: Manager) -> None:
        nmx_helpers.enable_cluster_app_manager_state(manager)
        enabled.append(manager)

    yield _enable

    for manager in enabled:
        with allure.step("Restore cluster app manager state"):
            try:
                nmx_helpers.restore_cluster_app_manager_state(manager)
            except Exception:
                # Don't mask test results on cleanup failure, but make it loud.
                logger.error(
                    "Failed to restore cluster app manager state for %r",
                    manager, exc_info=True,
                )


# ── Cleanup fixture (resource-leak detection) ────────────────────────────────

@pytest.fixture(scope="session", autouse=True)
def resource_leak_check(engines):
    """Snapshot listeners before the suite; diff after to catch leaked sockets.

    Logs an error and attaches a warning to the allure report if new listening
    sockets appear after the test suite. Does not assert, to avoid masking
    real test failures.
    """
    dut: LinuxSshEngine = engines.dut

    with allure.step("Capture baseline ss snapshots before test suite"):
        baseline_tcp = run_ss_tulpn(dut)
        baseline_unix = run_ss_unix(dut)
        baseline_ports = extract_listening_ports(parse_ss_listeners(baseline_tcp))
        logger.info("Baseline TCP listener ports: %s", sorted(baseline_ports))

    yield

    with allure.step("Compare ss snapshots after test suite for leaked sockets"):
        post_tcp = run_ss_tulpn(dut)
        post_unix = run_ss_unix(dut)
        post_ports = extract_listening_ports(parse_ss_listeners(post_tcp))

        new_ports = post_ports - baseline_ports
        allure.attach("Baseline ss -tulpn", baseline_tcp)
        allure.attach("Baseline ss -x (unix sockets)", baseline_unix)
        allure.attach("Post-test ss -tulpn", post_tcp)
        allure.attach("Post-test ss -x (unix sockets)", post_unix)
        if new_ports:
            msg = f"Resource leak detected: new listening ports appeared after tests: {sorted(new_ports)}"
            logger.error(msg)
            allure.attach("RESOURCE LEAK", msg)
