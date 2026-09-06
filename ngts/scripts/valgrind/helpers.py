from __future__ import annotations

from typing import Self, Generator
from pathlib import Path
import logging
import base64
import time
import re

from ngts.cli_wrappers.common.general_clis_common import GeneralCliCommon
from devts.infra.tools.connection_tools.linux_ssh_engine import LinuxSshEngine
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.tools.test_utils import allure_utils as allure
from devts.infra.tools.exceptions.test_issue import TestIssue
from ngts.nvos_tools.system.Health import HealthConsts
import retry

logger = logging.getLogger(__name__)

_PARSE_SYSTEMD_EXEC_START = re.compile(r'([^\s=]+)=(.*?)(?= ?; [^\s=]+=|$)')
_VALGRIND_CONF_FILE = Path(__file__).parent / 'valgrind.conf'
assert _VALGRIND_CONF_FILE.exists(), 'valgrind.conf not found'
VALGRIND_DIR = '/var/log/valgrind'
# The image pins apt to a Debian snapshot older than the packages the rootfs ships, so the
# snapshot's -dbg packages can never satisfy their exact-version dependency on a runtime
# package. Add a live mirror for the duration of the install, then drop it again.
_DBG_MIRROR_LIST = '/etc/apt/sources.list.d/valgrind-dbg.list'
_DBG_MIRROR_LINES = (
    'deb [arch=amd64] http://debian-archive.trafficmanager.net/debian trixie main',
    'deb [arch=amd64] http://debian-archive.trafficmanager.net/debian trixie-updates main',
)


class ServiceNotApplicableError(Exception):
    """Raised when a requested service is not currently applicable to a Valgrind run."""


class ValgrindPkg:
    def __init__(self, engine: LinuxSshEngine):
        self._engine = engine
        self._general_cli = GeneralCliCommon(engine=engine)
        self._log = logger.getChild(self.__class__.__name__)

    def install(self) -> None:
        """
        Installs valgrind package if it is not already installed.
        :param engine: the engine to use.
        """
        with allure.step("Install valgrind package"):
            if self._general_cli.which('valgrind'):
                self._log.info('Valgrind package is already installed, skipping...')
            else:
                self._engine.run_cmd(f'rm -f {_DBG_MIRROR_LIST}')
                for line in _DBG_MIRROR_LINES:
                    self._engine.run_cmd(f"echo '{line}' | sudo tee -a {_DBG_MIRROR_LIST}")
                try:
                    self._general_cli.apt_update()
                    self._general_cli.apt_install('valgrind', '-y')
                    self.get_process_path('valgrind')  # sanity check
                    self._general_cli.apt_install('python3-dbg', '-y')
                finally:
                    self._engine.run_cmd(f'rm -f {_DBG_MIRROR_LIST}')

    def get_process_path(self, process: str) -> str:
        """
        Returns the absolute path of a process, found by the 'which' command.
        :param process: the process of which to get the absolute path.
        :return: the absolute path of the process.
        :raise Exception: if the process was not found.
        """
        with allure.step(f"Get path of {process}"):
            path = self._general_cli.which(process)
            if not path:
                raise Exception(f'Process {process} not found')
            return path

    def clear_valgrind_dir(self, dockers: list[str] = []) -> Self:
        """
        Clears the valgrind dir.
        :param engine: the engine to use, may use a PrefixEngine with prefix 'sudo'
            to act on the host, or with prefix 'docker exec <container>'
            to act on a Docker container.
        """
        with allure.step(f'Clear valgrind dir at {VALGRIND_DIR}'):
            self._general_cli.rm(VALGRIND_DIR, flags='-rf')
            self._general_cli.mkdir(VALGRIND_DIR, flags='-p')
            for docker in dockers:
                self._general_cli.mkdir(f"{VALGRIND_DIR}/{docker}", flags='-p')
            self._general_cli.chmod_by_mode(VALGRIND_DIR, '2775', flags='-R')
        return self


class BaseManager:
    def __init__(self, engine: LinuxSshEngine):
        self._log = logger.getChild(self.__class__.__name__)
        self._name = self.__class__.__name__.replace('Manager', '').lower()

        self._engine = engine
        self._services_paths: dict[str, Path] = {}

    def enable_all(self, services: list[str]) -> None:
        service_failed_to_configure: list[str] = []
        service_skipped: dict[str, str] = {}
        for service in sorted(set(services)):
            with allure.independent_step(f"Enable valgrind for {service}"):
                try:
                    self._configure(service)
                except ServiceNotApplicableError as error:
                    service_skipped[service] = str(error)
                    self._log.warning("Skipping valgrind for %s: %s", service, error)
                except Exception as e:
                    self._log.error(f"Failed to enable valgrind for {service}: {e}")
                    service_failed_to_configure.append(service)
                    raise

        if service_skipped:
            allure.attach(
                "Skipped Valgrind services",
                "\n".join(f"{service}: {reason}" for service, reason in sorted(service_skipped.items())),
            )

        if service_failed_to_configure:
            logger.error(
                "Failed to edit the following services, check log for details: %s",
                ' '.join(service_failed_to_configure)
            )
            self._handle_failed_services(service_failed_to_configure)

            assert len(service_failed_to_configure) < len(services), "All services failed to enable"

        configured_services = set(services) - set(service_failed_to_configure) - set(service_skipped)
        self._restart_services(sorted(configured_services), failed_to_enable=True)

    def disable_all(self, services: list[str]) -> Generator[str, None, None]:
        """
        Disables valgrind for all services, and yields the services that were disabled.
        :param services: the services to disable valgrind for.
        :return: a generator of services that were disabled.
        """

        for service in sorted(set(services)):
            with allure.independent_step(f"Disable valgrind for {service}"):
                yield service

    def _configure(self, service: str) -> None:
        raise NotImplementedError

    def _un_configure(self, service: str) -> None:
        raise NotImplementedError

    def _handle_failed_services(self, services: list[str]) -> None:
        raise NotImplementedError

    def _restart_services(self, services: list[str], /, *, failed_to_enable: bool = False) -> None:
        ...


class DockerManager(BaseManager):
    def disable_all(self, services: list[str]) -> None:
        for service in super().disable_all(services):
            self._un_configure(service)

    def _configure(self, service: str) -> None:
        """
        Starts a service.
        """
        self._log.info(f"Starting service {service}")
        # If currently enabled, disable first to get a clean start
        if self._is_enabled(service):
            logger.info(f'Valgrind is already enabled for {service}; disabling before enabling anew')
            # why we need to disable before re-enabling?
            # in order to make valgrind stop the currently writing to the logs files, and start writing to new ones.
            self._disable(service)

        self._enable(service)

        if not self._wait_for_flag_file_value(service, '/etc/valgrind_run', '1', timeout_s=90):
            raise TestIssue(f'Timed out waiting for {service} to report valgrind enabled (1)')

        self._verify(service)

    def _un_configure(self, service: str) -> None:
        """
        Disables valgrind for a service.
        """
        if not self._is_enabled(service):
            self._log.warning(f"Valgrind is not currently enabled for {service}; proceeding with disable flow anyway")
        else:
            self._disable(service)

        self._verify(service)

    def _is_enabled(self, service: str) -> bool:
        with allure.step(f"Checking if {service=} is enabled"):
            try:
                out: str = self._engine.run_cmd(f'docker exec {service} cat /etc/valgrind_run')
                return out.strip() == '1'
            except Exception:
                return False

    def _disable(self, service: str) -> None:
        with allure.step(f"Disabling service {service}"):
            self._engine.run_cmd(f'sonic-db-cli CONFIG_DB hdel "FEATURE|{service}" "run_valgrind"')

            try:
                self._engine.run_cmd(f'docker stop --time 180 {service} || true')
            except Exception:
                self._log.warning(f"Failed to stop {service} container; proceeding with disable flow anyway")

            self._engine.run_cmd(f'systemctl reset-failed {service} || true')
            self._engine.run_cmd(f'systemctl restart {service}', validate=True)
            if not self._wait_for_flag_file_value(service, '/etc/valgrind_run', '0', timeout_s=60):
                raise TestIssue(f'Timed out waiting for {service} to report valgrind disabled (0)')

    def _enable(self, service: str) -> None:
        with allure.step(f"Enable service {service}"):
            # Enable flag and restart {service}
            self._engine.run_cmd(f'sonic-db-cli CONFIG_DB hset "FEATURE|{service}" "run_valgrind" "true"')
            self._engine.run_cmd(f'systemctl reset-failed {service} || true')
            self._engine.run_cmd(f'systemctl restart {service}', validate=True)
            if not self._wait_for_flag_file_value(service, '/etc/valgrind_run', '1', timeout_s=60):
                raise TestIssue(f'Timed out waiting for {service} to report valgrind enabled (1)')

    def _wait_for_flag_file_value(
        self,
        container: str,
        path: str,
        expected_value: str,
        timeout_s: int = 90,
        poll_s: float = 2.0
    ) -> bool:
        """
        Poll a file inside a docker container until its content equals expected_value.
        Returns True on success, False on timeout.
        """
        deadline = time.time() + timeout_s
        last_error: str | None = None
        while time.time() < deadline:
            try:
                out: str = self._engine.run_cmd(f"docker exec {container} cat {path}")
                if out.strip() == expected_value:
                    return True
            except BaseException as ex:  # noqa: BLE001
                last_error = str(ex)
            time.sleep(poll_s)
        if last_error:
            logger.debug("wait_for_flag_file_value last error: %s", last_error)
        return False

    def _verify(self, service: str) -> None:
        """ Verifies that a service is actually up and running. """
        with allure.step(f"Verifying {service} status"):
            try:
                is_active = self._engine.run_cmd(f"systemctl is-active '{service}'").strip()
                running = self._engine.run_cmd("docker inspect -f '{{.State.Running}}' %s" % service).strip()
            except BaseException as ex:  # noqa: BLE001
                raise TestIssue(f"Failed to verify '{service}' status: {ex}")

            if is_active != 'active' or running != 'true':
                raise TestIssue(
                    f"'{service}' not healthy after enabling valgrind: "
                    f"systemd={is_active}, docker_running={running}"
                )

    def _handle_failed_services(self, failed_to_enable_docker: list[str]) -> list[str]:
        with allure.step(f"Disabling failed to enable dockers ({', '.join(failed_to_enable_docker)})"):
            for docker in failed_to_enable_docker:
                with allure.independent_step(f"Disable valgrind for {docker}"):
                    self._un_configure(docker)


class ServiceManager(BaseManager):
    _VALGRIND_CONF = None

    def __init__(self, engine: LinuxSshEngine):
        super().__init__(engine)
        self._general_cli = GeneralCliCommon(engine=engine)

    def disable_all(self, services: list[str]) -> None:
        not_configured_services: set[str] = set()
        restore_errors: dict[str, str] = {}

        for service in super().disable_all(services):
            try:
                self.restore_service(service)
            except FileNotFoundError:
                # Likely never configured or already restored earlier; not a real issue
                not_configured_services.add(service)
                self._log.warning("No configuration found for %s; skipping restore", service)
            except Exception as ex:
                # not sure if we should raise here, or just log and continue
                restore_errors[service] = str(ex)
                self._log.error("Restore failed for %s: %s", service, ex)

        if restore_errors:
            allure.attach(
                'restore-errors',
                '\n'.join(f"{svc}: {msg}" for svc, msg in sorted(restore_errors.items()))
            )
        if not_configured_services:
            allure.attach('not-configured-services', ' '.join(sorted(not_configured_services)))

        configured_services = set(services) - not_configured_services
        if configured_services:
            try:
                self._general_cli.systemctl_restart(configured_services, daemon_reload=True)
            except TestIssue as ex:
                # Do not abort; let the failed scan path below handle recovery
                self._log.error("systemctl_restart raised: %s", ex)

        # Proactively scan for services that ended up failed and restore them
        if failed_after := self._get_failed_systemctl():
            with allure.step("Scan for failed services after restart and restore"):
                for service in failed_after:
                    try:
                        self.restore_service(service)
                    except FileNotFoundError:
                        self._log.warning("No backup found for %s during failed-scan restore; skipping", service)
                    except Exception as ex:  # noqa: BLE001
                        self._log.error("Restore during failed-scan failed for %s: %s", service, ex)
                self._general_cli.systemctl_restart(tuple(failed_after), daemon_reload=True)

    @property
    def _valgrind_conf(self) -> str:
        if self._VALGRIND_CONF is None:
            self._VALGRIND_CONF = _VALGRIND_CONF_FILE.read_text()
        return self._VALGRIND_CONF

    @staticmethod
    def _get_dropin_dir(service: str) -> Path:
        ''' Returns the path to the drop-in directory for a service. '''
        return Path(f"/etc/systemd/system/{service}.d/valgrind.conf")

    def _is_dropin_exists(self, service: str) -> bool:
        ''' Checks if the drop-in file exists. '''
        return self._engine.run_cmd(f"test -f '{self._get_dropin_dir(service).as_posix()}' && echo yes || echo no").strip() == "yes"

    def _get_service_current_state(self, service: str) -> tuple[str, str, str]:
        """Return the service load, active, and sub states."""
        result: str = self._engine.run_cmd(
            f"systemctl show {service} --property=LoadState --property=ActiveState --property=SubState --no-pager",
            validate=False,
        )
        state = {}
        for line in result.splitlines():
            key, separator, value = line.partition("=")
            if separator:
                state[key.strip()] = value.strip()

        required_fields = ("LoadState", "ActiveState", "SubState")
        missing_fields = [field for field in required_fields if field not in state]
        if missing_fields:
            raise RuntimeError(
                f"Could not determine service state for {service}; missing {', '.join(missing_fields)} in output: {result!r}"
            )

        return state["LoadState"], state["ActiveState"], state["SubState"]

    def _handle_failed_services(self, services: list[str]) -> None:
        """Remove partial Valgrind configuration without masking the original failure."""
        cleanup_errors: dict[str, str] = {}
        with allure.step(f"Clean failed Valgrind services: {', '.join(services)}"):
            for service in services:
                try:
                    if self._is_dropin_exists(service):
                        self.restore_service(service, failed_to_enable=True)
                except Exception as error:
                    cleanup_errors[service] = str(error)
                    self._log.error("Failed to clean Valgrind configuration for %s: %s", service, error)

        if cleanup_errors:
            allure.attach(
                "Valgrind service cleanup errors",
                "\n".join(f"{service}: {error}" for service, error in sorted(cleanup_errors.items())),
            )

    def _configure(self, service: str) -> None:
        """
        Create a systemd drop-in for `service` that wraps its ExecStart
        with /usr/local/bin/valgrind-wrapper.sh.
        """
        # service is e.g. "syseepromd.service"
        svc_name = service.rsplit(".service", maxsplit=1)[0]

        load_state, active_state, sub_state = self._get_service_current_state(service)
        if load_state != "loaded" or active_state != "active" or sub_state != "running":
            raise ServiceNotApplicableError(
                f"service is not running (load={load_state}, active={active_state}, sub={sub_state})"
            )

        service_command = self._get_service_systemd_exec_argv(service).replace(" --daemon", "")

        with allure.step("Render drop-in content"):
            conf_content = self._valgrind_conf.format(
                service_name=svc_name,
                service_command=service_command,
            )
            allure.attach(f"Valgrind configuration for {service}", conf_content)

        with allure.step("Create drop-in"):
            dropin = self._get_dropin_dir(service)

            with allure.step("Encode drop-in content to base64"):
                conf_b64 = base64.b64encode(conf_content.encode("utf-8")).decode("ascii")

            with allure.step("Create drop-in directory and write file via base64"):
                cmd = (
                    f"sh -c 'mkdir -p {dropin.parent.as_posix()} && "
                    f"printf %s {conf_b64} | base64 -d > {dropin.as_posix()}'"
                )
                self._engine.run_cmd(cmd, validate=False)
                if not self._is_dropin_exists(service):
                    raise TestIssue(f"Failed to write drop-in file for {service}: {dropin}")

    def _get_service_systemd_exec_argv(self, service: str) -> str:
        """
        Returns the ExecStart command for a service.
        """
        with allure.step(f"Get ExecStart command for {service}"):
            with allure.step("Get ExecStart command from systemd"):
                raw: str = self._engine.run_cmd(
                    f"systemctl show {service} -P ExecStart --no-pager --value",
                    validate=False,
                ).strip()

                if not raw:
                    raise RuntimeError(f"Could not get ExecStart for {service}")

            with allure.step("Parse ExecStart command"):
                # systemctl may output multiple ExecStart= lines; take the last non-empty one
                lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
                exec_struct = lines[-1]

                # exec_struct looks like:
                # { path=/usr/sbin/rsyslogd ; argv[]=/usr/sbin/rsyslogd -n ; ignore_errors=no ; ... }
                if not (exec_struct.startswith("{") and exec_struct.endswith("}")):
                    raise RuntimeError(f"Unexpected ExecStart format for {service}: {exec_struct!r}")

            with allure.step("Find argv[] in ExecStart command"):
                for key, value in _PARSE_SYSTEMD_EXEC_START.findall(exec_struct):
                    if key == "argv[]":
                        if grep := re.match(r'(/usr/sbin/nginx )(.*)(-g )([^-$]+)(.*)', value):
                            # e.g. return "/usr/sbin/nginx daemon on; master_process on; -c /etc/nginx/nginx_auth.conf"
                            # the regex will wraps around the -g flag value quotes. => -g 'daemon on; master_process on;'
                            # each {} is mapped to () in the above regex.
                            return "{}{}{}{!r} {}".format(*grep.groups())
                        return value
                raise RuntimeError(f"Could not find argv[] in ExecStart for {service}: {exec_struct!r}")

    def _restart_services(self, services: list[str], /, *, failed_to_enable: bool = False) -> None:
        self._log.debug(f"Services to restart: {services}")

        try:
            if services:
                with allure.step(f"Restarting services: {services}"):
                    self._general_cli.systemctl_restart(services, daemon_reload=True)
        except TestIssue as e:
            # Extract failing services, restore, attach journal, and retry those
            failed_services = sorted(re.findall(r'Job for (\S+) failed', str(e), re.IGNORECASE))
            if failed_services:
                with allure.step("Attempting to restore services that failed to run with valgrind"):
                    for service in failed_services:
                        self.restore_service(service, failed_to_enable=failed_to_enable)
                        try:
                            fail_log = self._engine.run_cmd(
                                f"journalctl --no-pager -xu '{service}'", validate=True
                            )
                        except BaseException as ex:  # noqa: BLE001
                            fail_log = "Failed to collect journal: " + str(ex)
                        allure.attach(service + ".log", fail_log)
                    self._general_cli.systemctl_restart(tuple(failed_services), daemon_reload=True)

        # Proactively scan for services that ended up failed and restore them
        if failed_after := self._get_failed_systemctl():
            with allure.step("Scan for failed services after restart and restore"):
                for service in failed_after:
                    self.restore_service(service)
                self._general_cli.systemctl_restart(tuple(failed_after), daemon_reload=True)

    def _get_failed_systemctl(self) -> list[str]:
        with allure.step("Get failed systemctl"):
            result = self._engine.run_cmd("systemctl --failed")
            # ● banner-config.service loaded failed failed start Update banner config based on configdb
            allure.attach("systemctl_failed", result)
            return re.findall(r'● (\S+\.service) loaded failed failed start', result)

    def restore_service(self, service: str, /, *, failed_to_enable: bool = False) -> None:
        with allure.step(f"Restoring service {service}"):
            dropin = self._get_dropin_dir(service)

            if failed_to_enable:
                with allure.step('Show valgrind drop-in file'):
                    dropin_content = self._engine.run_cmd(f'cat {dropin}')
                    allure.attach(f"{service}-dropin.conf", dropin_content)

            self._log.info(f"Restoring service {service}: removing {dropin}")

            # Check if drop-in exists
            if not self._is_dropin_exists(service):
                self._log.info(f"No valgrind drop-in found for {service} at {dropin}, nothing to restore")
                raise FileNotFoundError(f"No valgrind drop-in found for {service} at {dropin}")

            # Remove drop-in (and drop-in dir if empty) under root shell
            cmd = """sudo sh -c 'rm -f "$1" && rmdir --ignore-fail-on-non-empty "$2" 2>/dev/null' -- '{dropin_file}' '{dropin_dir}'"""
            self._engine.run_cmd(cmd.format(dropin_file=dropin.as_posix(), dropin_dir=dropin.parent.as_posix()), validate=True)


def eval_system_ready(engine: LinuxSshEngine, reason: str = '', check_health: bool = True) -> None:
    """
    Waits for systemd to have no pending jobs and critical targets are active.
    Adds diagnostics on failure but does not raise unless jobs persist beyond
    a generous timeout.
    """
    @retry.retry(Exception, tries=12, delay=5)  # 1 minute
    def wait_until_ready():
        output = OutputParsingTool.parse_json_str_to_dictionary(
            engine.run_cmd('nv show system -o json')
        ).get_returned_value()
        assert output['status'].lower() == 'system is ready', "system status should be 'system is ready'"

    label = f'system-stable {reason}'.strip()
    with allure.step(f'Wait for system stability: {label}'):
        wait_until_ready()

    if check_health:
        eval_system_health(engine)


def eval_system_health(engine: LinuxSshEngine) -> None:
    @retry.retry(AssertionError, tries=20, delay=30)
    def wait_until_health_status_change_after_reboot():
        sys_health = engine.run_cmd('nv show system health -o json')
        assert "NVOS CLI is unavailable" not in sys_health, "NVOS CLI is unavailable"
        output = OutputParsingTool.parse_json_str_to_dictionary(sys_health).get_returned_value()
        assert output[HealthConsts.STATUS] == HealthConsts.OK, f"health should be {HealthConsts.OK}"

    with allure.step("Evaluate system health"):
        wait_until_health_status_change_after_reboot()
