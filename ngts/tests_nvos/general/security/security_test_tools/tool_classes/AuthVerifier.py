from __future__ import annotations

from typing import Any
import os
import logging
import random
import string
from contextlib import contextmanager

from devts.infra.tools.connection_tools.serial_engine_factory import (
    PexpectSerialEngine,
)
from devts.infra.tools.general_constants.constants import DefaultConnectionValues
from devts.infra.tools.linux_tools.linux_tools import LinuxSshEngine, scp_file

from ngts.cli_wrappers.openapi.openapi_command_builder import OpenApiRequest
from ngts.nvos_constants.constants_nvos import ApiType, SystemConsts
from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from ngts.nvos_tools.infra.ConnectionTool import ConnectionTool
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.IpTool import IpTool
from ngts.nvos_tools.infra.SshCmdBuilder import SshCmdBuilder, SshPassCmdBuilder
from ngts.nvos_tools.infra.PexpectTool import PexpectTool
from ngts.tools.test_utils import allure_utils as allure
from ngts.nvos_tools.system.System import System
from ngts.ngts_types import EnginesT, TopologyT
from ..constants import AuthConsts, AuthMedium

logger = logging.getLogger(__name__)


class AuthVerifier:
    def __init__(self, username: str, password: str, engines: EnginesT, topology_obj: TopologyT):
        self.api = ApiType.NVUE
        self._log = logger.getChild(self.__class__.__name__)
        self.topology_obj: TopologyT = topology_obj
        self._log.info(f"Create proxy ssh engine for user: {username}")
        self.engine = LinuxSshEngine(engines.dut.ip, username, password)

    def __enter__(self) -> "AuthVerifier":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.cleanup()

    def __del__(self):
        try:
            self.cleanup()
        except Exception:
            pass

    def cleanup(self) -> None:
        """Release any long-lived transport opened by the verifier."""
        if (engine := getattr(self, "engine", None)) is None:
            return

        try:
            if callable(disconnect := getattr(engine, "disconnect", None)):
                disconnect()
            elif callable(close := getattr(engine, "close", None)):
                close()
        except Exception as err:
            self._log.info(
                "Failed to clean up auth verifier engine for user %s: %s",
                getattr(engine, "username", "unknown"),
                err,
            )
        finally:
            self.engine = None

    def change_test_api(self, api=None):
        api = self.api if api is None else api
        self._log.info(f"Change test api to: {api}")
        TestToolkit.tested_api = api

    def _build_auth_debug_snapshot(self) -> str:
        engine = getattr(self, "engine", None)
        try:
            serial_attrs: Any = self.topology_obj.players["dut_serial"]["attributes"].noga_query_data["attributes"]
            serial_specific = serial_attrs.get("Specific", {})
            serial_conn = serial_attrs.get("Topology Conn.", {})
        except Exception:
            serial_specific = {}
            serial_conn = {}

        password = getattr(engine, "password", None)
        return "\n".join(
            [
                f"verifier_class: {self.__class__.__name__}",
                f"api: {self.api}",
                f"tested_api: {getattr(TestToolkit, 'tested_api', 'n/a')}",
                f"engine_class: {type(engine).__name__ if engine else 'n/a'}",
                f"engine_ip: {getattr(engine, 'ip', 'n/a')}",
                f"engine_port: {getattr(engine, 'port', 'n/a')}",
                f"engine_username: {getattr(engine, 'username', 'n/a')}",
                f"engine_password_length: {len(password) if isinstance(password, str) else 'n/a'}",
                f"serial_ip: {serial_specific.get('ip', 'n/a')}",
                f"serial_conn_user: {serial_conn.get('CONN_USER', 'n/a')}",
            ]
        )

    def _attach_auth_failure_snapshot(self, err: Exception) -> None:
        snapshot = (
            f"{self._build_auth_debug_snapshot()}\n"
            f"exception_type: {type(err).__name__}\n"
            f"exception: {err}"
        )
        allure.attach(f"{self.__class__.__name__}_auth_failure_context", snapshot, log=False)

    def verify_authentication(self, expect_success=True):
        orig_test_api = TestToolkit.tested_api
        self.change_test_api()
        authentication_success = True
        try:
            self._log.info(
                "Verify authentication expect_success=%s ip=%s user=%s api=%s",
                expect_success,
                getattr(self.engine, "ip", "n/a"),
                getattr(self.engine, "username", "n/a"),
                self.api,
            )
            self._authenticate(expect_success)
        except Exception as e:
            self._attach_auth_failure_snapshot(e)
            self._log.info(f"Authentication failed\nException:\n{e}")
            authentication_success = False
        finally:
            self.change_test_api(orig_test_api)
            assert expect_success == authentication_success, "Authentication result not as expected"

    def _authenticate(self, expect_success):
        raise Exception("Method not implemented!")

    @staticmethod
    @contextmanager
    def _allure_step_expect_failure(step_msg: str, expect_success: bool):
        """Wrap an allure step so it exits cleanly on expected authentication failures.

        When *expect_success* is ``False`` and the body raises, the exception is
        captured, the allure step ``__exit__`` runs normally (keeping the step
        green in reports), and the exception is re-raised afterwards so the
        caller (``verify_authentication``) can still observe it.
        """
        caught = None
        with allure.step(step_msg):
            try:
                yield
            except Exception as exc:
                if expect_success:
                    raise
                caught = exc
        if caught:
            raise caught

    def verify_authorization(self, user_is_admin):
        orig_test_api = TestToolkit.tested_api
        self.change_test_api()
        try:
            system = System()
            with allure.step("Run show command. Expect success: True"):
                system.version.show(dut_engine=self.engine, check_engine_connectivity=False)
            with allure.step(f"Run set command. Expect success: {user_is_admin}"):
                system.message.set(
                    op_param_name=SystemConsts.PRE_LOGIN_MESSAGE,
                    op_param_value='"NVOS TESTS"',
                    dut_engine=self.engine,
                    check_engine_connectivity=False,
                ).verify_result(should_succeed=user_is_admin)

            with allure.step(f"Run unset command. Expect success: {user_is_admin}"):
                system.message.unset(
                    op_param=SystemConsts.PRE_LOGIN_MESSAGE, dut_engine=self.engine, check_engine_connectivity=False
                ).verify_result(should_succeed=user_is_admin)
        finally:
            with allure.step("cleanup after authorization check"):
                with allure.step("Clear global OpenApi changeset and payload"):
                    OpenApiRequest.clear_changeset_and_payload()
                if self.api == ApiType.NVUE:
                    with allure.step(f"detach config of user: {self.engine.username}"):
                        try:
                            NvueGeneralCli.detach_config(self.engine)
                        except Exception:
                            pass
                self.change_test_api(orig_test_api)


class SshAuthVerifier(AuthVerifier):
    def __init__(self, username: str, password: str, engines: EnginesT, topology_obj: TopologyT):
        super().__init__(username, password, engines, topology_obj)

    def _authenticate(self, expect_success):
        with self._allure_step_expect_failure(
            "For SSH - run some linux command on engine to trigger authentication", expect_success
        ):
            self.engine.run_cmd("id")


class OpenApiAuthVerifier(AuthVerifier):
    def __init__(self, username: str, password: str, engines: EnginesT, topology_obj: TopologyT):
        # Don't call super().__init__ to avoid creating SSH connection that generates accounting logs
        self._log = logger.getChild(self.__class__.__name__)
        self.api = ApiType.OPENAPI
        self.topology_obj = topology_obj
        self.username = username
        self.password = password
        self.engines = engines
        # Create a minimal engine-like object for OpenAPI that doesn't establish SSH
        self.engine = type('OpenApiEngine', (), {
            'ip': engines.dut.ip,
            'username': username,
            'password': password,
            'open_api_port': getattr(engines.dut, 'open_api_port', '443')
        })()

    def _authenticate(self, expect_success):
        with self._allure_step_expect_failure(
            "For OpenApi - run show command with OpenApi request to verify authentication", expect_success
        ):
            System().version.show(dut_engine=self.engine, check_engine_connectivity=False)


class RconAuthVerifier(AuthVerifier):
    def __init__(self, username: str, password: str, engines: EnginesT, topology_obj: TopologyT):
        super().__init__(username, password, engines, topology_obj)
        self._log.info(f"Create pexpect serial engine for user: {username}")
        self.engine: PexpectSerialEngine = ConnectionTool.create_serial_engine(
            topology_obj=topology_obj, ip=engines.dut.ip, username=username, password=password
        )

    def __del__(self):
        self.cleanup()

    def cleanup(self) -> None:
        if self.engine:
            serial_engine = getattr(self.engine, "serial_engine", None)
            if serial_engine is not None:
                try:
                    self._log.info("send ctrl+D to logout rcon session")
                    serial_engine.sendline("\x04")
                except Exception as err:
                    self._log.info("Failed to logout rcon auth verifier cleanly: %s", err)
            try:
                self.engine._close_serial_engine()
            except Exception as err:
                self._log.info("Failed to close rcon auth verifier engine: %s", err)
            finally:
                self.engine = None

    def _authenticate(self, expect_success):
        with self._allure_step_expect_failure(
            "For RCON - start rcon connection and force new login", expect_success
        ):
            assert isinstance(self.engine, PexpectSerialEngine), "engine should be pexpect serial engine"
            self.engine.create_serial_engine(disconnect_existing_login=True)
            self.engine.run_cmd_and_get_output("\r")


class ScpAuthVerifier(AuthVerifier):
    def __init__(self, username: str, password: str, engines: EnginesT, topology_obj: TopologyT):
        super().__init__(username, password, engines, topology_obj)

    def _authenticate(self, expect_success):
        with self._allure_step_expect_failure(
            f"Download a non-privileged file from the switch. Expect success: {expect_success}", expect_success
        ):
            self._verify_scp_download(
                switch_dir=AuthConsts.SWITCH_MONITORS_DIR, expect_success=expect_success, check_result_in_caller_func=True
            )

    def __verify_scp(self, src_path, dst_path, download_from_remote, expect_success, check_result_in_caller_func=False):
        scp_success = True
        try:
            scp_file(player=self.engine, src_path=src_path, dst_path=dst_path, download_from_remote=download_from_remote, print_output=True)
            self._log.info("SCP success")

            if download_from_remote:
                self._log.info("Remove downloaded file")
                os.remove(dst_path)
                self._log.info("Downloaded file successfully removed")
            else:
                self._log.info("Remove uploaded file")
                self.engine.run_cmd(f"rm -f {dst_path}")
                self._log.info("Uploaded file successfully removed")
        except Exception as e:
            self._log.info("SCP failed")
            if expect_success:
                self._log.info(f"Exception:\n{e}")
            scp_success = False
            if check_result_in_caller_func:
                raise e

        if not check_result_in_caller_func:
            assert scp_success == expect_success, f"SCP success ({scp_success}) status not as expected ({expect_success})"

    def _verify_scp_download(self, switch_dir, expect_success, switch_filename="", check_result_in_caller_func=False):
        with self._allure_step_expect_failure(
            f"Verify SCP download from the switch. Expect success: {expect_success}", expect_success
        ):
            src_filename = AuthConsts.SWITCH_SCP_DOWNLOAD_TEST_FILE_NAME if not switch_filename else switch_filename
            dst_filename = "".join([random.choice(string.ascii_lowercase) for _ in range(15)]) + ".txt"
            self.__verify_scp(
                src_path=f"{switch_dir}/{src_filename}",
                dst_path=f"{AuthConsts.SHARED_VERIFICATION_SCP_DIR}/{dst_filename}",
                download_from_remote=True,
                expect_success=expect_success,
                check_result_in_caller_func=check_result_in_caller_func,
            )

    def _verify_scp_upload(self, switch_dir, expect_success):
        with allure.step(f"Verify SCP upload to the switch. Expect success: {expect_success}"):
            self.__verify_scp(
                src_path=f"{AuthConsts.SHARED_VERIFICATION_SCP_DIR}/{AuthConsts.SHARED_VERIFICATION_SCP_UPLOAD_TEST_FILE_NAME}",
                dst_path=f"{switch_dir}/{AuthConsts.SHARED_VERIFICATION_SCP_UPLOAD_TEST_FILE_NAME}",
                download_from_remote=False,
                expect_success=expect_success,
            )

    def _verify_scp_download_and_upload(self, switch_dir, expect_success):
        switch_filename = AuthConsts.SWITCH_ROOT_FILE_NAME if switch_dir == AuthConsts.SWITCH_ROOT_DIR else ""
        self._verify_scp_download(switch_dir, expect_success, switch_filename=switch_filename)
        self._verify_scp_upload(switch_dir, expect_success)

    def verify_authorization(self, user_is_admin):
        with allure.step("Verify SCP with non privileged path on the switch. Expect success: True"):
            self._verify_scp_download_and_upload(AuthConsts.SWITCH_MONITORS_DIR, expect_success=True)

        with allure.step(f"Verify SCP with admin privileged path on the switch. Expect success: {user_is_admin}"):
            self._verify_scp_download_and_upload(AuthConsts.SWITCH_ADMINS_DIR, expect_success=user_is_admin)

        with allure.step("Verify SCP with root privileged path on the switch. Expect success: False"):
            self._verify_scp_download_and_upload(AuthConsts.SWITCH_ROOT_DIR, expect_success=False)


class PKAAuthVerifier(AuthVerifier):
    def __init__(
        self,
        username: str,
        private_key_path: str,
        hostname: str,
        password: str = None,
        engines: EnginesT = None,
        topology_obj: TopologyT = None,
    ):
        # Avoid AuthVerifier.__init__ creating an IPv4 LinuxSshEngine; PKA uses PexpectTool only.
        self._log = logger.getChild(self.__class__.__name__)
        self.api = ApiType.NVUE
        self.topology_obj: TopologyT = topology_obj
        self.engines = engines
        self.engine = None
        self.username = username
        self.private_key_path = private_key_path
        self.hostname = hostname
        self._dut_key_path = None
        self._dut_key_dir = None

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        try:
            self.cleanup()
        finally:
            self._remove_dut_key_dir()

    def __del__(self):
        try:
            self.cleanup()
            self._remove_dut_key_dir()
        except Exception:
            pass

    def _ensure_key_on_dut(self) -> str:
        """Copy the private key to the DUT so PKA ssh can run from the DUT (for IPv6 targets)."""
        if self._dut_key_path:
            return self._dut_key_path
        dut = self.engines.dut
        # Use a unique per-run directory so concurrent runs / stale leftovers can't
        # collide on a deterministic /tmp path (and so it can be cleaned up wholesale).
        suffix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
        remote_dir = f'/tmp/nvos_pka_{suffix}'
        dut.run_cmd(f'mkdir -m 700 -p {remote_dir}')
        remote_name = os.path.basename(self.private_key_path)
        remote_path = f'{remote_dir}/{remote_name}'
        dut.copy_file(
            source_file=self.private_key_path, file_system=remote_dir,
            dest_file=remote_name, direction='put',
        )
        dut.run_cmd(f'chmod 600 {remote_path}')

        # Cert-auth tests sign the key, producing an adjacent "<key>-cert.pub" that
        # OpenSSH auto-loads from beside the private key. Copy it too so the cert is
        # presented when ssh runs from the DUT. The plain PKA test has no cert, so
        # this is guarded by existence and leaves that flow unaffected.
        cert_source = f'{self.private_key_path}-cert.pub'
        if os.path.exists(cert_source):
            dut.copy_file(
                source_file=cert_source, file_system=remote_dir,
                dest_file=f'{remote_name}-cert.pub', direction='put',
            )
            dut.run_cmd(f'chmod 644 {remote_path}-cert.pub')

        self._dut_key_dir = remote_dir
        self._dut_key_path = remote_path
        return remote_path

    def _remove_dut_key_dir(self) -> None:
        """Remove the per-run key directory from the DUT.

        Called only at end of life (context exit / GC), never from the per-spawn
        ``cleanup()`` which runs mid-flow in ``_spawn_pka_engine`` while the key is
        still needed by the about-to-be-spawned ssh.
        """
        if not self._dut_key_dir or self.engines is None:
            return
        try:
            self.engines.dut.run_cmd(f'rm -rf {self._dut_key_dir}')
        except Exception as err:
            self._log.info("Failed to remove PKA key dir %s from DUT: %s", self._dut_key_dir, err)
        finally:
            self._dut_key_dir = None
            self._dut_key_path = None

    def _spawn_pka_engine(self) -> None:
        if IpTool.is_address_ipv6(self.hostname) and self.engines is not None:
            # sonic-mgmt has no IPv6 route and DUT forbids TCP forwarding, so run the PKA
            # ssh from the DUT against its own IPv6 mgmt address (reached over IPv4).
            dut = self.engines.dut
            remote_key = self._ensure_key_on_dut()
            inner_cmd = (
                SshCmdBuilder(self.username, self.hostname)
                .set_ssn()
                .ConnectTimeout(30)
                .use_auth_key(remote_key)
                .ForceTTY()
                .build()
            )
            ssh_cmd = (
                SshPassCmdBuilder(
                    dut.username, dut.password, dut.ip,
                    getattr(dut, 'ssh_port', 22), cmd_to_execute=inner_cmd,
                )
                .set_ssn()
                .build()
            )
        else:
            ssh_cmd = (
                SshCmdBuilder(self.username, self.hostname)
                .set_ssn()
                .ConnectTimeout(30)
                .use_auth_key(self.private_key_path)
                .build()
            )
        self.cleanup()
        self.engine = PexpectTool(spawn_cmd=ssh_cmd)

    def _authenticate(self, expect_success):
        with self._allure_step_expect_failure(f"SSH PKA authentication - {expect_success}", expect_success):
            self._log.info(f"Create PKA engine for user: {self.username}")
            self._spawn_pka_engine()
            timeout = 5 if not expect_success else None
            self.engine.expect(f"{self.username}@.*~", error_message="Expected login success, but failed", timeout=timeout)
            self.engine.expect(".*", timeout=timeout)

    def verify_authorization(self, user_is_admin):
        expected_msg = ".*" if user_is_admin else "Error: No permission to execute this command"
        timeout = 10 if not user_is_admin else None
        try:
            with allure.step("Run show command. Expect success: True"):
                self._spawn_pka_engine()
                self.engine.expect(DefaultConnectionValues.DEFAULT_PROMPTS, error_message="Expected login success, but failed")
                self.engine.sendline("nv show system")
                self.engine.expect(f"{self.username}@.*~")
            with allure.step(f"Run set command. Expect success: {user_is_admin}"):
                self.engine.sendline("nv set system message pre-login TESTS")
                self.engine.expect(expected_msg, timeout=timeout)
            with allure.step("cleanup between two sets"):
                self.engine.sendline("nv config detach")
            with allure.step(f"Run unset command. Expect success: {user_is_admin}"):
                self.engine.sendline("nv unset system message pre-login")
                self.engine.expect(expected_msg, timeout=timeout)
        finally:
            with allure.step("cleanup"):
                if self.engine is not None:
                    try:
                        self.engine.sendline("nv config detach")
                    except Exception as err:
                        self._log.info("Failed to detach config before closing PKA session: %s", err)


AUTH_VERIFIERS: dict[AuthMedium, type[AuthVerifier]] = {
    AuthMedium.SSH: SshAuthVerifier,
    AuthMedium.OPENAPI: OpenApiAuthVerifier,
    AuthMedium.RCON: RconAuthVerifier,
    AuthMedium.SCP: ScpAuthVerifier,
}
