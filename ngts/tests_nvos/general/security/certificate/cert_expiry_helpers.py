"""Helpers for TLS certificate expiry monitoring tests."""
import logging
import re
import shlex
import time
from typing import Dict, Optional
from uuid import uuid4

from retry import retry

from ngts.nvos_constants.constants_nvos import ApiType, ClusterApps
from ngts.nvos_tools.infra.DutUtilsTool import wait_until_cli_is_up
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.nmx.Cluster import Cluster
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.general.security.certificate.CertInfo import CertInfo
from ngts.tests_nvos.general.security.certificate.constants import CertExpiryConsts, CertShowFields
from ngts.tests_nvos.general.security.helpers import (
    get_test_certs_dir_location,
    import_cas_safely,
    prepare_tmp_test_certs,
)
from ngts.tests_nvos.general.security.nmx_cert.constants import EncryptionMode
from ngts.tests_nvos.general.security.nv_bridge.helpers import (
    is_cluster_enabled,
    restore_internal_config_with_retry,
)
from ngts.tests_nvos.general.security.test_api_server_security.constants import (
    API_INSTALLED,
    CERTIFICATE,
)
from ngts.tests_nvos.system.clock.ClockConsts import ClockConsts
from ngts.tests_nvos.system.gnmi.constants import GNMI_INSTALLED
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger(__name__)

CLUSTER_APP_NAME = ClusterApps.NMX_CONTROLLER
LogOffsets = Dict[str, int]
MONITORED_LOG_PATHS = (CertExpiryConsts.SYSLOG_PATH, CertExpiryConsts.NVUED_LOG_PATH)


def safe_is_cluster_enabled() -> bool:
    """Like ``is_cluster_enabled``, but False if show fails."""
    try:
        return is_cluster_enabled()
    except Exception:
        return False


class ExpiryMonitoredService:
    """In-use cert surface tracked in the operational store.

    :param requires_cluster: the surface only exists while the cluster runs, so it is skipped on
        platforms without NMX and triggers lazy cluster bring-up.
    """

    def __init__(self, key: str, installed_label: Optional[str] = None, requires_cluster: bool = False):
        self.key = key
        self.installed_label = installed_label
        self.requires_cluster = requires_cluster

    def bind(self, system: System, cert: CertInfo, engines, scp_player):
        raise NotImplementedError

    def unbind(self, system: System, engines):
        raise NotImplementedError

    def resource(self, system: System):
        return None

    @property
    def supports_rebind_in_place(self) -> bool:
        """True if a new cert can be bound while a previous cert is still bound."""
        return True


class SetBasedService(ExpiryMonitoredService):
    """API / gNMI bind via set/unset certificate."""

    def __init__(self, key: str, resource_getter, installed_label: str):
        super().__init__(key, installed_label)
        self._resource_getter = resource_getter

    def resource(self, system: System):
        return self._resource_getter(system)

    def bind(self, system: System, cert: CertInfo, engines, scp_player):
        # assume-yes: near-expiry bind prompts and would hang apply otherwise
        with allure.step(f'bind cert "{cert.name}" to {self.key}'):
            self.resource(system).set(
                CERTIFICATE, cert.name, apply=True, ask_for_confirmation=True).verify_result()

    def unbind(self, system: System, engines):
        with allure.step(f'unbind cert from {self.key}'):
            self.resource(system).unset(CERTIFICATE, apply=True, ask_for_confirmation=True).verify_result()


class ActionUpdateService(ExpiryMonitoredService):
    """Cluster / system-internal: action update (+ optional mTLS); restore via action restore."""

    def __init__(self, key: str, resource_getter, set_encryption: bool = True,
                 restore_resource: bool = False, requires_cluster: bool = False):
        super().__init__(key, requires_cluster=requires_cluster)
        self._resource_getter = resource_getter
        self._set_encryption = set_encryption
        self._restore_resource = restore_resource

    @property
    def supports_rebind_in_place(self) -> bool:
        # while encryption is mtls the DUT rejects "action update certificate" and demands
        # alternate-certificate + rotate, so the surface has to be released before rebinding
        return not self._set_encryption

    def bind(self, system: System, cert: CertInfo, engines, scp_player):
        with allure.step(f'bind cert "{cert.name}" to {self.key}'):
            # keep an existing CA: the DUT rejects deleting a CA that any application still uses,
            # so re-import would break rebinding a cert that is bound to another surface
            import_cas_safely([cert], scp_player, override_existing=False)
            resource = self._resource_getter(system)
            resource.certificate.action_update(cert.name).verify_result()
            resource.ca_certificate.action_update(cert.cacert_name).verify_result()
            if self._set_encryption:
                resource.encryption.action_update(EncryptionMode.MTLS).verify_result()

    def unbind(self, system: System, engines):
        with allure.step(f'restore {self.key} certificate binding'):
            resource = self._resource_getter(system)
            if self._restore_resource:
                restore_actions = [resource.action_restore]
            else:
                restore_actions = []
                if self._set_encryption:
                    restore_actions.append(resource.encryption.action_restore)
                for name in ('certificate', 'ca_certificate'):
                    sub_resource = getattr(resource, name, None)
                    if sub_resource is not None:
                        restore_actions.append(sub_resource.action_restore)
            restore_internal_config_with_retry(
                restore_actions=restore_actions,
                fallback_action=resource.action_restore,
                scope_name=self.key,
            )


def _cluster_manager(_system: System):
    return Cluster().apps.app_name[CLUSTER_APP_NAME].manager


def _cluster_internal(_system: System):
    return Cluster().apps.app_name[CLUSTER_APP_NAME].internal


API_SERVICE = SetBasedService('api', lambda system: system.api, API_INSTALLED)
GNMI_SERVICE = SetBasedService('gnmi-server', lambda system: system.gnmi_server, GNMI_INSTALLED)
CLUSTER_MANAGER_SERVICE = ActionUpdateService('cluster-manager', _cluster_manager, requires_cluster=True)
CLUSTER_INTERNAL_SERVICE = ActionUpdateService('cluster-internal', _cluster_internal, restore_resource=True,
                                               requires_cluster=True)
SYSTEM_INTERNAL_SERVICE = ActionUpdateService('system-internal', lambda system: system.internal)
MONITORED_SERVICES = {service.key: service for service in
                      (API_SERVICE, GNMI_SERVICE,
                       CLUSTER_MANAGER_SERVICE, CLUSTER_INTERNAL_SERVICE, SYSTEM_INTERNAL_SERVICE)}


class BoundServiceRegistry:
    """Tracks bound surfaces for teardown restore.

    A surface is registered *before* it is mutated and de-registered only after its restore
    succeeded, so a partially applied bind or a failed restore is still retried by the later
    module-level teardown instead of leaking into the next test.
    """

    def __init__(self):
        self._bound: Dict[str, ExpiryMonitoredService] = {}

    def bind(self, system: System, service: ExpiryMonitoredService, cert: CertInfo, engines, scp_player):
        if self.is_bound(service) and not service.supports_rebind_in_place:
            self.unbind(system, service, engines)
        self._bound[service.key] = service
        service.bind(system, cert, engines, scp_player)

    def unbind(self, system: System, service: ExpiryMonitoredService, engines):
        """Restore the surface and forget it. Raises if the restore failed."""
        service.unbind(system, engines)
        self._bound.pop(service.key, None)

    def unbind_many(self, system: System, services, engines):
        """Restore every given surface; attempt all, then raise if any restore failed."""
        failures = {}
        for service in services:
            try:
                self.unbind(system, service, engines)
            except Exception as error:
                failures[service.key] = error
                logger.warning('failed to unbind %s: %s', service.key, error)
        assert not failures, f'failed to restore bound cert surface(s): {failures}'

    def unbind_all(self, system: System, engines, cluster_only: bool = False):
        services = [service for service in self._bound.values()
                    if service.requires_cluster or not cluster_only]
        self.unbind_many(system, services, engines)

    def is_bound(self, service: ExpiryMonitoredService) -> bool:
        return service.key in self._bound


@retry(AssertionError, tries=12, delay=5)
def _assert_cert_left_oper_store(engines, cert_name: str, service_key: str):
    assert not cert_in_oper_store(engines, cert_name), \
        f'"{cert_name}" still referenced in the operational store after unbind from {service_key}'


def validate_unbind(engines, system: System, cert: CertInfo, service: ExpiryMonitoredService):
    """Assert cert left oper-store and is not installed."""
    with allure.step(f'validate cert "{cert.name}" is unbound from {service.key}'):
        _assert_cert_left_oper_store(engines, cert.name, service.key)
        verify_cert_not_installed(system, cert.name, service)


def backup_expiry_feature_env(engines) -> str:
    """Copy /etc/default/nvued aside and return the backup path.

    The path is unique per call, so a backup left behind by an interrupted run can never be
    restored on top of a later run.
    """
    backup_file = f'{CertExpiryConsts.NVUED_ENV_FILE}.{CertExpiryConsts.ENV_BACKUP_SUFFIX}.{uuid4().hex[:8]}'
    with allure.step(f'backup {CertExpiryConsts.NVUED_ENV_FILE} to {backup_file}'):
        engines.dut.run_cmd(f'sudo touch {CertExpiryConsts.NVUED_ENV_FILE}')
        engines.dut.run_cmd(f'sudo cp -a {CertExpiryConsts.NVUED_ENV_FILE} {backup_file}')
    return backup_file


def set_expiry_feature_env(engines, enabled: bool = True,
                           interval_seconds: int = CertExpiryConsts.TEST_INTERVAL_SECONDS,
                           warning_days: int = CertExpiryConsts.DEFAULT_WARNING_DAYS):
    """Upsert enable/interval/warning in /etc/default/nvued, then restart nvued."""
    env_values = {
        CertExpiryConsts.ENV_ENABLE: (CertExpiryConsts.ENV_ENABLE_YES if enabled
                                      else CertExpiryConsts.ENV_ENABLE_NO),
        CertExpiryConsts.ENV_INTERVAL_SECONDS: str(interval_seconds),
        CertExpiryConsts.ENV_WARNING_DAYS: str(warning_days),
    }
    with allure.step(f'configure cert-expiry env: {env_values}'):
        engines.dut.run_cmd(f'sudo touch {CertExpiryConsts.NVUED_ENV_FILE}')
        for key, value in env_values.items():
            engines.dut.run_cmd(f"sudo sed -i '/^{key}=/d' {CertExpiryConsts.NVUED_ENV_FILE}")
            engines.dut.run_cmd(
                f'echo {shlex.quote(f"{key}={value}")} | sudo tee -a {CertExpiryConsts.NVUED_ENV_FILE}')
        restart_nvued(engines)


def restore_expiry_feature_env(engines, backup_file: str):
    """Restore /etc/default/nvued from ``backup_file`` and restart nvued."""
    with allure.step('restore cert-expiry feature env'):
        engines.dut.run_cmd(
            f'sudo test -f {backup_file} '
            f'&& sudo mv -f {backup_file} {CertExpiryConsts.NVUED_ENV_FILE} || true')
        restart_nvued(engines)


def restart_nvued(engines):
    """Restart nvued; wait until active and NVUE CLI ready."""
    with allure.step(f'restart {CertExpiryConsts.NVUED_SERVICE}'):
        engines.dut.run_cmd(f'sudo systemctl restart {CertExpiryConsts.NVUED_SERVICE}')
        _wait_nvued_active(engines)
        wait_until_cli_is_up(engines.dut)
        _wait_nvue_not_initializing(engines)


@retry(Exception, tries=20, delay=2)
def _wait_nvued_active(engines):
    status = engines.dut.run_cmd(f'systemctl is-active {CertExpiryConsts.NVUED_SERVICE}').strip()
    assert status == 'active', f'{CertExpiryConsts.NVUED_SERVICE} is not active yet (status: {status})'


@retry(Exception, tries=30, delay=2)
def _wait_nvue_not_initializing(engines):
    # wait_until_cli_is_up does not cover 'System is initializing'
    output = engines.dut.run_cmd('nv show system', validate=False)
    assert 'System is initializing' not in output, f'NVUE still initializing after nvued restart:\n{output}'


def generate_expiry_test_cert(cert_name: str, engines, dut_hostname, expiration_days: int,
                              dut_ip: Optional[str] = None) -> CertInfo:
    """Generate a short-lived leaf cert via ``prepare_tmp_test_certs``."""
    dest_dir = get_test_certs_dir_location('cert-expiry', dut_hostname)
    return prepare_tmp_test_certs(
        [cert_name], dest_dir, engines, dut_hostname, dut_ip, expiration_days=expiration_days)[cert_name]


def _dut_epoch(engines) -> int:
    return int(engines.dut.run_cmd("date '+%s'").strip().splitlines()[-1])


def _set_dut_clock_to_epoch(engines, system: System, epoch: int):
    local_time = engines.dut.run_cmd(f"date -d '@{epoch}' '+%Y-%m-%d %H:%M:%S'").strip()
    system.datetime.action_change(local_time).verify_result()


def _set_ntp_state(system: System, state: str):
    system.ntp.set(ClockConsts.STATE, state, apply=True).verify_result()
    time.sleep(CertExpiryConsts.NTP_TOGGLE_SETTLE_SECONDS)


def advance_dut_clock_days(engines, system: System, days: int):
    """Disable NTP and set DUT clock ``days`` ahead of host time."""
    with allure.step(f'advance DUT clock by {days} day(s)'):
        _set_ntp_state(system, ClockConsts.DISABLED)
        _set_dut_clock_to_epoch(engines, system,
                                int(time.time()) + days * CertExpiryConsts.SECONDS_PER_DAY)


def is_dut_clock_advanced(engines, min_days: int = CertExpiryConsts.CLOCK_ADVANCED_MIN_DAYS) -> bool:
    return (_dut_epoch(engines) - int(time.time())) / CertExpiryConsts.SECONDS_PER_DAY >= min_days


def ensure_dut_clock_advanced(engines, system: System, days: int) -> bool:
    """Re-advance clock if a lifecycle event restored wall time. True when re-advanced."""
    if is_dut_clock_advanced(engines):
        return False
    advance_dut_clock_days(engines, system, days)
    return True


def restore_dut_clock(engines, system: System):
    """Set DUT clock to host time and re-enable NTP."""
    with allure.step('restore DUT clock to real time'):
        _set_ntp_state(system, ClockConsts.DISABLED)
        _set_dut_clock_to_epoch(engines, system, int(time.time()))
        _set_ntp_state(system, ClockConsts.ENABLED)


def verify_dut_clock_restored(engines, system: System):
    """Assert NTP enabled and DUT clock within restore tolerance of host time."""
    with allure.step('verify DUT clock restored'):
        ntp_state = OutputParsingTool.parse_json_str_to_dictionary(
            system.ntp.show()).get_returned_value().get(ClockConsts.STATE)
        assert ntp_state == ClockConsts.ENABLED, \
            f'expected NTP enabled after clock restore, got state={ntp_state!r}'
        drift = abs(_dut_epoch(engines) - int(time.time()))
        assert drift <= CertExpiryConsts.CLOCK_RESTORE_TOLERANCE_SECONDS, \
            f'DUT clock is still {drift}s away from real time after restore'


def get_cert_expiry_status(system: System, cert_name: str) -> Optional[str]:
    """Return expiring-soon/expired status, or None."""
    out = OutputParsingTool.parse_json_str_to_dictionary(
        system.security.certificate.cert_id[cert_name].show()).get_returned_value()
    known = (CertExpiryConsts.STATUS_EXPIRING_SOON, CertExpiryConsts.STATUS_EXPIRED)
    status = out.get(CertExpiryConsts.STATUS_FIELD)
    if status in known:
        return status
    for value in out.values():
        if isinstance(value, str) and value in known:
            return value
    return None


@retry(AssertionError, tries=12, delay=5)
def _assert_show_status(system: System, cert_name: str, expected_status: Optional[str]):
    actual = get_cert_expiry_status(system, cert_name)
    if expected_status is None:
        assert actual is None, f'expected no expiry status for "{cert_name}" but got: {actual}'
    else:
        assert actual == expected_status, \
            f'unexpected expiry status for "{cert_name}": expected {expected_status}, got {actual}'


def verify_show_status(system: System, cert_name: str, expected_status: Optional[str],
                       installed_service: Optional[ExpiryMonitoredService] = None, verify_api: bool = True):
    """Assert CLI show status (optional OpenAPI + installed service)."""
    with allure.step(f'verify show status of "{cert_name}" == {expected_status}'):
        _assert_show_status(system, cert_name, expected_status)
    if verify_api:
        previous_api = TestToolkit.tested_api
        TestToolkit.tested_api = ApiType.OPENAPI
        try:
            api_status = get_cert_expiry_status(System(), cert_name)
        finally:
            TestToolkit.tested_api = previous_api
        assert api_status == expected_status, \
            f'API expiry status for "{cert_name}" differs: expected {expected_status}, got {api_status}'
    if installed_service is not None and installed_service.installed_label:
        with allure.step(f'verify "{cert_name}" is installed on {installed_service.key}'):
            out = OutputParsingTool.parse_json_str_to_dictionary(
                system.security.certificate.cert_id[cert_name].installed.show()).get_returned_value()
            assert installed_service.installed_label in str(out), \
                f'"{cert_name}" not installed on {installed_service.installed_label}\ninstalled: {out}'


def verify_cert_not_installed(system: System, cert_name: str,
                              service: Optional[ExpiryMonitoredService] = None):
    """Assert cert is not installed (whole map, or one service label)."""
    label = service.installed_label if service is not None else None
    target = service.key if service is not None else 'any service'
    with allure.step(f'verify "{cert_name}" is no longer installed on {target}'):
        out = OutputParsingTool.parse_json_str_to_dictionary(
            system.security.certificate.cert_id[cert_name].show()).get_returned_value()
        installed = out.get(CertShowFields.INSTALLED) or {}
        if not isinstance(installed, dict):
            installed = {}
        if label:
            assert label not in str(installed), \
                f'"{cert_name}" still installed on {label}\ninstalled: {installed}'
        else:
            assert not installed, f'"{cert_name}" still reports installations\ninstalled: {installed}'


def _log_fragment_for_level(cert_name: str, level: str) -> str:
    if level == CertExpiryConsts.LEVEL_WARNING:
        return CertExpiryConsts.warning_log_fragment(cert_name)
    if level == CertExpiryConsts.LEVEL_ERROR:
        return CertExpiryConsts.expired_log_fragment(cert_name)
    raise ValueError(f'unsupported log level: {level}')


def _parse_int(output: str) -> int:
    try:
        return int(output.splitlines()[-1].strip()) if output.strip() else 0
    except (ValueError, IndexError):
        return 0


def get_log_offsets(engines) -> LogOffsets:
    """Snapshot monitored log sizes for later ``since`` filtering."""
    offsets = {}
    for log_path in MONITORED_LOG_PATHS:
        output = engines.dut.run_cmd(f'sudo stat -c %s {log_path} 2>/dev/null || echo 0').strip()
        offsets[log_path] = _parse_int(output)
    return offsets


def _count_scheduler_alert_in_log(engine, cert_name: str, level: str, log_path: str,
                                  since: Optional[LogOffsets] = None) -> int:
    fragment = _log_fragment_for_level(cert_name, level)
    start_offset = since.get(log_path, 0) if since else 0
    if start_offset <= 0:
        since_cmd = f'sudo cat {log_path} 2>/dev/null'
    else:
        since_cmd = (
            f'if [ "$(sudo stat -c %s {log_path} 2>/dev/null || echo 0)" -lt {start_offset} ]; '
            f'then sudo cat {log_path} 2>/dev/null; '
            f'else sudo tail -c +{start_offset + 1} {log_path} 2>/dev/null; fi')
    cmd = (
        f'({since_cmd}) '
        f'| grep -F {shlex.quote(CertExpiryConsts.SCHEDULER_LOG_TAG)} '
        f'| grep -F {shlex.quote(level)} '
        f'| grep -F {shlex.quote(fragment)} '
        f'| wc -l'
    )
    return _parse_int(engine.run_cmd(cmd))


def verify_expiry_alert(engines, cert_name: str, level: str, present: bool = True, check_nvued_log: bool = True,
                        since: Optional[LogOffsets] = None):
    """Assert/refute scheduler alert in syslog (+ nvued.log best-effort)."""
    fragment = _log_fragment_for_level(cert_name, level)
    verb = 'present' if present else 'absent'
    with allure.step(f'verify {level} alert for "{cert_name}" {verb} in syslog'):
        count = _count_scheduler_alert_in_log(
            engines.dut, cert_name, level, CertExpiryConsts.SYSLOG_PATH, since)
        if present:
            assert count > 0, (
                f'expected {level} {CertExpiryConsts.SCHEDULER_LOG_TAG} alert mentioning "{fragment}" '
                f'in syslog but none found')
        else:
            assert count == 0, (
                f'did not expect {level} alert mentioning "{fragment}" in syslog but found {count}')
    if not check_nvued_log:
        return
    with allure.step(f'verify {level} alert for "{cert_name}" in nvued.log (best-effort)'):
        exists = engines.dut.run_cmd(
            f'test -f {CertExpiryConsts.NVUED_LOG_PATH} && echo yes || echo no').strip().endswith('yes')
        if not exists:
            logger.warning('nvued.log not found - skipping nvued.log assertion')
            return
        count = _count_scheduler_alert_in_log(
            engines.dut, cert_name, level, CertExpiryConsts.NVUED_LOG_PATH, since)
        if present:
            assert count > 0, (
                f'expected {level} alert mentioning "{fragment}" in nvued.log but none found')
        else:
            assert count == 0, (
                f'did not expect {level} alert mentioning "{fragment}" in nvued.log but found {count}')


@retry(AssertionError, tries=18, delay=10)
def _poll_for_expiry_alert(engines, cert_name: str, level: str, check_nvued_log: bool,
                           since: Optional[LogOffsets]):
    verify_expiry_alert(engines, cert_name, level, present=True, check_nvued_log=check_nvued_log, since=since)


def wait_for_expiry_alert(engines, cert_name: str, level: str, check_nvued_log: bool = True,
                          since: Optional[LogOffsets] = None):
    """Poll until alert appears; dump diagnostics on timeout."""
    try:
        _poll_for_expiry_alert(engines, cert_name, level, check_nvued_log, since)
    except AssertionError:
        dump_expiry_monitor_diagnostics(engines, cert_name)
        raise


def wait_for_scheduler_trigger(engines) -> LogOffsets:
    """Snapshot logs, sleep one scan interval; return offsets for ``since``."""
    offsets = get_log_offsets(engines)
    with allure.step(f'wait {CertExpiryConsts.TEST_INTERVAL_SECONDS}s for scheduler'):
        time.sleep(CertExpiryConsts.TEST_INTERVAL_SECONDS)
    return offsets


def dump_expiry_monitor_diagnostics(engines, cert_name: str = ''):
    """Attach env/logs/oper-store dump for missing-alert triage."""
    probes = [
        ('DUT date', 'date'),
        ('nvued env file', f'sudo cat {CertExpiryConsts.NVUED_ENV_FILE} 2>&1 || true'),
        ('nvued unit EnvironmentFiles', 'systemctl show nvued -p EnvironmentFiles -p Environment 2>&1 || true'),
        ('nvued effective env (proc)',
         'sudo tr "\\0" "\\n" < /proc/$(pgrep -f nvued | head -1)/environ 2>/dev/null '
         '| grep -i certificate || true'),
        (f'syslog {CertExpiryConsts.SCHEDULER_LOG_TAG}',
         f'sudo grep -F {shlex.quote(CertExpiryConsts.SCHEDULER_LOG_TAG)} {CertExpiryConsts.SYSLOG_PATH} '
         f'2>/dev/null | tail -50 || true'),
        ('nvued.log tail', f'sudo tail -50 {CertExpiryConsts.NVUED_LOG_PATH} 2>/dev/null || true'),
        ('certs oper store', f'sudo cat {CertExpiryConsts.CERTS_OPER_STORE} 2>/dev/null || true'),
    ]
    if cert_name:
        probes.append((f'syslog "{cert_name}"',
                       f'sudo grep -F {shlex.quote(cert_name)} {CertExpiryConsts.SYSLOG_PATH} '
                       f'2>/dev/null | tail -50 || true'))
    report = []
    for title, cmd in probes:
        try:
            output = engines.dut.run_cmd(cmd)
        except Exception as error:
            output = f'<probe failed: {error}>'
        report.append(f'===== {title} =====\n$ {cmd}\n{output}\n')
    with allure.step('dump cert-expiry monitor diagnostics'):
        allure.attach('cert-expiry monitor diagnostics', '\n'.join(report))


def cert_in_oper_store(engines, cert_name: str) -> bool:
    """True if leaf id is in oper-store (ignores ``ca-of-<name>``)."""
    output = engines.dut.run_cmd(f'sudo cat {CertExpiryConsts.CERTS_OPER_STORE} 2>/dev/null || true')
    return bool(re.search(rf'(?<!ca-of-)(?<![A-Za-z0-9_-]){re.escape(cert_name)}(?![A-Za-z0-9_-])', output))
