"""TLS certificate expiry monitoring tests (NVUE Core 1.14.0).

Show status (expiring-soon/expired) + scheduler WARNING/ERROR for in-use entity server certs.
Uses ``random_api`` + ``random_service`` / ``random_service_pair``. SimX skipped.
"""
import logging
import random

import pytest

from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from ngts.nvos_constants.constants_nvos import SystemConsts
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.nmx.Cluster import Cluster
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.cluster.cluster_tools import ClusterTools
from ngts.tests_nvos.general.security.certificate.cert_expiry_helpers import (
    MONITORED_SERVICES,
    BoundServiceRegistry,
    advance_dut_clock_days,
    backup_expiry_feature_env,
    cert_in_oper_store,
    ensure_dut_clock_advanced,
    generate_expiry_test_cert,
    is_dut_clock_advanced,
    restart_nvued,
    restore_dut_clock,
    restore_expiry_feature_env,
    safe_is_cluster_enabled,
    set_expiry_feature_env,
    validate_unbind,
    verify_cert_not_installed,
    verify_dut_clock_restored,
    verify_expiry_alert,
    verify_show_status,
    wait_for_expiry_alert,
    wait_for_scheduler_trigger,
)
from ngts.tests_nvos.general.security.certificate.constants import CertExpiryConsts
from ngts.tests_nvos.general.security.helpers import (
    delete_cas_safely,
    delete_certs_safely,
    import_cas_safely,
    import_certs_safely,
)
from ngts.tests_nvos.general.security.nv_bridge.helpers import save_config_and_reboot
from ngts.tests_nvos.system.gnmi.conftest import scp_player  # noqa: F401 - imported to expose the fixture
from ngts.tools.test_utils import allure_utils as allure
from ngts.tools.test_utils.nvos_general_utils import loganalyzer_ignore

logger = logging.getLogger(__name__)

# state -> (advance days, alert level, show status)
EXPIRY_STATE_SPEC = {
    CertExpiryConsts.STATUS_EXPIRING_SOON: (CertExpiryConsts.EXPIRING_SOON_ADVANCE_DAYS,
                                            CertExpiryConsts.LEVEL_WARNING,
                                            CertExpiryConsts.STATUS_EXPIRING_SOON),
    CertExpiryConsts.STATUS_EXPIRED: (CertExpiryConsts.EXPIRED_ADVANCE_DAYS,
                                      CertExpiryConsts.LEVEL_ERROR,
                                      CertExpiryConsts.STATUS_EXPIRED),
}


def _stop_cluster_quietly(cluster, engines):
    """Best-effort unset + stop (mirrors nv_bridge enable_cluster teardown)."""
    try:
        cluster.unset(apply=True, dut_engine=engines.dut).verify_result()
    except Exception as error:
        logger.warning('failed to unset cluster config: %s', error)
    try:
        ClusterTools.stop_cluster(cluster, engine=engines.dut)
    except Exception as error:
        logger.warning('failed to disable cluster: %s', error)


def _enable_cluster_for_expiry_tests(engines, devices, setup_name):
    """Stage nv-bridge node and start cluster; skip if nmxc does not come up."""
    cluster = Cluster()
    if safe_is_cluster_enabled():
        with allure.step('cluster already enabled - reset before enable'):
            _stop_cluster_quietly(cluster, engines)

    node_server = getattr(devices.dut, 'cur_mgmt_port_ip', None) or engines.dut.ip
    with allure.step(f'stage nv-bridge node primary server {node_server}'):
        cluster.node.primary.set_cluster_node(op_param_name=SystemConsts.NV_BRIDGE_NODE_SERVER,
                                              op_param_value=node_server, dut_engine=engines.dut)
    try:
        ClusterTools.start_cluster(cluster, setup_name, engine=engines.dut, devices=devices)
    except Exception as start_error:
        _stop_cluster_quietly(cluster, engines)
        pytest.skip(f'cluster/nmx-controller does not come up on setup {setup_name}: {start_error}')
    return cluster


def _ensure_cluster_if_needed(request, service):
    """Lazy cluster enable for cluster surfaces; re-enable if torn down between tests."""
    if not service.requires_cluster:
        return
    devices = request.getfixturevalue('devices')
    request.getfixturevalue('module_cluster')
    if not safe_is_cluster_enabled():
        engines = request.getfixturevalue('engines')
        setup_name = request.getfixturevalue('setup_name')
        with allure.step('re-enable cluster after clear_config'):
            _enable_cluster_for_expiry_tests(engines, devices, setup_name)


def _cleanup_bind_and_clock(engines, system, service, cert, registry):
    """Unbind if needed, then restore clock."""
    with allure.step(f'cleanup {service.key}: unbind + restore time'):
        try:
            if registry.is_bound(service):
                registry.unbind(system, service, engines)
                validate_unbind(engines, system, cert, service)
        finally:
            if is_dut_clock_advanced(engines):
                restore_dut_clock(engines, system)
                verify_dut_clock_restored(engines, system)


def _bind_advance_show_and_alert(engines, system, service, cert, scp_player, registry,
                                 advance_days, expected_status, expected_level, expect_alert=True):
    """Shared body: restart nvued -> bind -> advance clock -> show -> scheduler check."""
    restart_nvued(engines)
    registry.bind(system, service, cert, engines, scp_player)

    if advance_days:
        advance_dut_clock_days(engines, system, advance_days)
        restart_nvued(engines)  # re-arm scheduler after clock jump

    if expected_status is not None:
        verify_show_status(system, cert.name, expected_status, installed_service=service)

    offsets = wait_for_scheduler_trigger(engines)
    if expect_alert:
        wait_for_expiry_alert(engines, cert.name, expected_level, since=offsets)
    else:
        verify_expiry_alert(engines, cert.name, CertExpiryConsts.LEVEL_WARNING, present=False, since=offsets)
        verify_expiry_alert(engines, cert.name, CertExpiryConsts.LEVEL_ERROR, present=False, since=offsets)


def _import_cert_material(cert, scp_player):
    """Import leaf + CA before any bind, so binds never have to replace an in-use CA.

    Delete-before-import (the ``import_*_safely`` default) is required here: every cert is
    regenerated with a freshly created CA, so a leftover object of the same id from an aborted run
    would no longer match and must be replaced.
    """
    import_certs_safely([cert], scp_player)
    import_cas_safely([cert], scp_player)


def _restore_env_and_clock(engines, system, env_backup):
    """Restore the nvued env file and the DUT clock independently; raise if either failed."""
    failures = {}
    restores = (
        ('cert-expiry feature env', lambda: restore_expiry_feature_env(engines, env_backup)),
        ('DUT clock', lambda: restore_dut_clock(engines, system)),
    )
    for label, restore in restores:
        try:
            restore()
        except Exception as error:
            failures[label] = error
            logger.warning('failed to restore %s: %s', label, error)
    assert not failures, f'cert-expiry environment was not restored: {failures}'


@pytest.fixture(scope='module', autouse=True)
def skip_expiry_tests_on_simx(is_simx):
    """Skip on SimX (wall-clock + nvued restart)."""
    if is_simx:
        pytest.skip('cert-expiry tests change DUT wall clock and restart nvued - real switches only')


@pytest.fixture(scope='module', autouse=True)
def cert_expiry_env(engines, skip_expiry_tests_on_simx):
    """Enable feature (60s interval); normalize clock on enter/exit.

    The env file is backed up and the try/finally entered *before* the first mutation, so a setup
    failure between the env edit and the nvued restart still restores the DUT.
    """
    system = System()
    restore_dut_clock(engines, system)
    env_backup = backup_expiry_feature_env(engines)
    try:
        set_expiry_feature_env(engines, enabled=True, interval_seconds=CertExpiryConsts.TEST_INTERVAL_SECONDS,
                               warning_days=CertExpiryConsts.DEFAULT_WARNING_DAYS)
        yield
    finally:
        _restore_env_and_clock(engines, system, env_backup)


@pytest.fixture(scope='module')
def bound_services(engines):
    """Bind registry; restore leftovers on teardown."""
    registry = BoundServiceRegistry()
    yield registry
    registry.unbind_all(System(), engines)


@pytest.fixture(scope='module')
def module_cluster(engines, devices, setup_name, bound_services):
    """Enable cluster for cluster cert surfaces; disable on teardown."""
    cluster = _enable_cluster_for_expiry_tests(engines, devices, setup_name)
    yield cluster
    bound_services.unbind_all(System(), engines, cluster_only=True)
    _stop_cluster_quietly(cluster, engines)


def _available_monitored_services(request):
    """Surfaces supported on this DUT (cluster surfaces require has_nmx)."""
    devices = request.getfixturevalue('devices')
    has_nmx = getattr(devices.dut, 'has_nmx', False)
    return [service for service in MONITORED_SERVICES.values()
            if has_nmx or not service.requires_cluster]


def _select_random_services(request, count: int):
    """Draw ``count`` platform-supported surfaces; publish as Allure param."""
    pool = _available_monitored_services(request)
    if len(pool) < count:
        pytest.skip(
            f'need {count} monitored cert surface(s), only {len(pool)} available: '
            f'{[service.key for service in pool]}')
    services = random.sample(pool, count)
    selected = ', '.join(service.key for service in services)
    logger.info('randomly selected cert surface(s): %s', selected)
    allure.dynamic.parameter('cert surface(s)', selected)
    for service in services:
        _ensure_cluster_if_needed(request, service)
    return services


@pytest.fixture
def random_service(request, random_api):
    """Depends on ``random_api`` so selection/cluster bring-up runs under the tested API."""
    return _select_random_services(request, 1)[0]


@pytest.fixture
def random_service_pair(request, random_api):
    """Depends on ``random_api`` so selection/cluster bring-up runs under the tested API."""
    return _select_random_services(request, 2)


@pytest.fixture(scope='module')
def shared_expiry_cert(engines, dut_hostname, scp_player):
    """Module-scoped shared leaf cert."""
    cert_name = CertExpiryConsts.SHARED_CERT_NAME
    cert = generate_expiry_test_cert(cert_name, engines, dut_hostname, CertExpiryConsts.SHARED_CERT_LEAF_DAYS)
    _import_cert_material(cert, scp_player)
    yield cert
    try:
        delete_cas_safely([cert])
    except Exception as error:
        logger.warning('failed to delete test CAs: %s', error)
    try:
        delete_certs_safely([cert])
    except Exception as error:
        logger.warning('failed to delete test certs: %s', error)


@pytest.fixture
def expiry_cert_factory(engines, dut_hostname, scp_player):
    """Factory for per-test certs; deletes them on teardown."""
    created = []

    def _make(name, expiration_days, import_to_dut=True):
        cert = generate_expiry_test_cert(name, engines, dut_hostname, expiration_days)
        if import_to_dut:
            _import_cert_material(cert, scp_player)
        created.append(cert)
        return cert

    yield _make

    if not created:
        return
    try:
        delete_cas_safely(created)
    except Exception as error:
        logger.warning('failed to delete test CAs: %s', error)
    try:
        delete_certs_safely(created)
    except Exception as error:
        logger.warning('failed to delete test certs: %s', error)


@pytest.mark.security
@pytest.mark.certificate
@pytest.mark.system
@pytest.mark.parametrize('expiry_state', list(EXPIRY_STATE_SPEC))
def test_cert_expiry_status_and_alert(random_api, expiry_state, random_service, engines, scp_player,
                                      shared_expiry_cert, bound_services):
    """HLD TC-01: bind + clock-advance; verify show status and scheduler alert."""
    TestToolkit.tested_api = random_api
    service = random_service
    system = System()
    cert = shared_expiry_cert
    advance_days, expected_level, expected_status = EXPIRY_STATE_SPEC[expiry_state]

    with loganalyzer_ignore():
        try:
            _bind_advance_show_and_alert(engines, system, service, cert, scp_player, bound_services,
                                         advance_days, expected_status, expected_level, expect_alert=True)
        finally:
            _cleanup_bind_and_clock(engines, system, service, cert, bound_services)


@pytest.mark.security
@pytest.mark.certificate
@pytest.mark.system
def test_cert_expiry_feature_disabled(random_api, random_service, engines, scp_player, shared_expiry_cert,
                                      bound_services):
    """Feature off: show still reports expiring-soon; no scheduler WARNING/ERROR."""
    TestToolkit.tested_api = random_api
    service = random_service
    system = System()
    cert = shared_expiry_cert
    advance_days, _, expected_status = EXPIRY_STATE_SPEC[CertExpiryConsts.STATUS_EXPIRING_SOON]

    try:
        set_expiry_feature_env(engines, enabled=False)
        with loganalyzer_ignore():
            try:
                _bind_advance_show_and_alert(engines, system, service, cert, scp_player, bound_services,
                                             advance_days, expected_status, expected_level=None,
                                             expect_alert=False)
            finally:
                _cleanup_bind_and_clock(engines, system, service, cert, bound_services)
    finally:
        set_expiry_feature_env(engines, enabled=True, interval_seconds=CertExpiryConsts.TEST_INTERVAL_SECONDS,
                               warning_days=CertExpiryConsts.DEFAULT_WARNING_DAYS)


@pytest.mark.security
@pytest.mark.certificate
@pytest.mark.system
def test_cert_expiry_mixed_two_certs(random_api, random_service_pair, engines, scp_player, expiry_cert_factory,
                                     bound_services):
    """Per-cert independence across two random services (near-expiry vs healthy)."""
    TestToolkit.tested_api = random_api
    service_a, service_b = random_service_pair
    system = System()
    soon_cert = expiry_cert_factory(CertExpiryConsts.MIXED_SOON_CERT_NAME,
                                    CertExpiryConsts.EXPIRING_SOON_LEAF_DAYS)
    healthy_cert = expiry_cert_factory(CertExpiryConsts.MIXED_HEALTHY_CERT_NAME,
                                       CertExpiryConsts.HEALTHY_LEAF_DAYS)

    def _phase(label, soon_service, healthy_service):
        with allure.step(label):
            restart_nvued(engines)
            bound_services.bind(system, soon_service, soon_cert, engines, scp_player)
            bound_services.bind(system, healthy_service, healthy_cert, engines, scp_player)
            verify_show_status(system, soon_cert.name, CertExpiryConsts.STATUS_EXPIRING_SOON,
                               installed_service=soon_service)
            verify_show_status(system, healthy_cert.name, expected_status=None)
            offsets = wait_for_scheduler_trigger(engines)
            wait_for_expiry_alert(engines, soon_cert.name, CertExpiryConsts.LEVEL_WARNING, since=offsets)
            verify_expiry_alert(engines, healthy_cert.name, CertExpiryConsts.LEVEL_WARNING, present=False,
                                since=offsets)

    try:
        with loganalyzer_ignore():
            _phase(f'cross-service: {service_a.key}<-soon, {service_b.key}<-healthy', service_a, service_b)
            _phase(f'cross-service swap: {service_a.key}<-healthy, {service_b.key}<-soon', service_b, service_a)

            with allure.step(f'same-service ({service_b.key}): healthy -> no alert'):
                restart_nvued(engines)
                bound_services.bind(system, service_b, healthy_cert, engines, scp_player)
                verify_show_status(system, healthy_cert.name, expected_status=None)
                offsets = wait_for_scheduler_trigger(engines)
                verify_expiry_alert(engines, healthy_cert.name, CertExpiryConsts.LEVEL_WARNING, present=False,
                                    since=offsets)
                verify_expiry_alert(engines, healthy_cert.name, CertExpiryConsts.LEVEL_ERROR, present=False,
                                    since=offsets)

            with allure.step(f'same-service ({service_b.key}): rebind soon -> only it alerts'):
                restart_nvued(engines)
                bound_services.bind(system, service_b, soon_cert, engines, scp_player)
                verify_show_status(system, soon_cert.name, CertExpiryConsts.STATUS_EXPIRING_SOON,
                                   installed_service=service_b)
                verify_show_status(system, healthy_cert.name, expected_status=None)
                offsets = wait_for_scheduler_trigger(engines)
                wait_for_expiry_alert(engines, soon_cert.name, CertExpiryConsts.LEVEL_WARNING, since=offsets)
    finally:
        with loganalyzer_ignore():
            bound_services.unbind_many(system, (service_a, service_b), engines)
            for cert in (soon_cert, healthy_cert):
                assert not cert_in_oper_store(engines, cert.name), \
                    f'"{cert.name}" still referenced in the operational store after unbind'
                verify_cert_not_installed(system, cert.name)


@pytest.mark.security
@pytest.mark.certificate
@pytest.mark.system
def test_cert_expiry_persist_after_reboot(random_api, random_service, engines, scp_player, shared_expiry_cert,
                                          bound_services):
    """After save+reboot, expiring-soon show status and scheduler WARNING resume."""
    TestToolkit.tested_api = random_api
    service = random_service
    system = System()
    cert = shared_expiry_cert
    advance_days, expected_level, expected_status = EXPIRY_STATE_SPEC[CertExpiryConsts.STATUS_EXPIRING_SOON]

    try:
        with loganalyzer_ignore():
            _bind_advance_show_and_alert(engines, system, service, cert, scp_player, bound_services,
                                         advance_days, expected_status, expected_level, expect_alert=True)

            save_config_and_reboot(engines)

            with allure.step('after reboot: monitoring resumes'):
                if ensure_dut_clock_advanced(engines, system, advance_days):
                    restart_nvued(engines)
                verify_show_status(system, cert.name, expected_status, installed_service=service)
                offsets = wait_for_scheduler_trigger(engines)
                wait_for_expiry_alert(engines, cert.name, expected_level, since=offsets)
    finally:
        with loganalyzer_ignore():
            try:
                if bound_services.is_bound(service):
                    bound_services.unbind(system, service, engines)
                    NvueGeneralCli.save_config(engines.dut)
                    validate_unbind(engines, system, cert, service)
            finally:
                if is_dut_clock_advanced(engines):
                    restore_dut_clock(engines, system)
                    verify_dut_clock_restored(engines, system)
