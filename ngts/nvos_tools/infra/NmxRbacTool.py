import logging
import time
from typing import Tuple, List, Dict

from ngts.nvos_constants.constants_nvos import ClusterConsts
from ngts.nvos_tools.infra.GrpcCmdBuilder import GrpcCmdBuilder
from ngts.nvos_tools.nmx.Cluster import Cluster
from ngts.nvos_tools.nmx.Apps import ClusterApp
from ngts.tests_nvos.general.security.certificate.CertInfo import CertInfo
from ngts.tests_nvos.general.security.helpers import import_cas_safely, import_certs_safely
from ngts.tests_nvos.general.security.nmx_cert.constants import EncryptionMode
from ngts.tests_nvos.general.security.nmx_cert.helpers import enable_cluster_app_manager_state
from ngts.tests_nvos.general.security.security_test_tools.tool_classes.UserInfo import UserInfo
from ngts.tests_nvos.helpers.general_helpers import run_cmd, run_ssh_cmd_with_rc
from ngts.tests_nvos.general.security.api_server.mtls.spiffe_id.constants import BAD_RESPONSE_KEYWORDS
from ngts.tests_nvos.system.gnmi.helpers import get_scp_player
from ngts.tools.test_utils import allure_utils as allure
from devts.infra.tools.connection_tools.linux_ssh_engine import LinuxSshEngine
from ngts.tools.test_utils.nvos_general_utils import generate_scp_uri_using_player


class NmxRbacTool:
    def __init__(self, cluster: Cluster, engine: LinuxSshEngine, cluster_app: ClusterApp):
        self.cluster = cluster
        self.engine = engine
        self.cluster_app = cluster_app

    def import_rbac_file(self, rbac_file_name: str, rbac_file_path: str):
        rbac_scp_path = generate_scp_uri_using_player(get_scp_player(self.engine), rbac_file_path)
        with allure.step("Import rbac file"):
            self.cluster.rbac.file[rbac_file_name].action_import(import_path=rbac_scp_path, dut_engine=self.engine).verify_result()
        with allure.step("Verify imported rbac file is stored in '/etc/cluster_infra/rbac/'"):
            self._verify_cmd_success(f"ls /etc/cluster_infra/rbac/{rbac_file_name}.yaml")
        with allure.step("Verify rbac file is imported successfully"):
            rbac_file_output = self.cluster.rbac.files.show_files(dut_engine=self.engine)
            assert rbac_file_name in rbac_file_output, f"Rbac {rbac_file_name} file is not imported successfully"

    def delete_rbac_file(self, rbac_file_name: str):
        with allure.step("Delete rbac file"):
            self.cluster.rbac.file[rbac_file_name].action_delete(dut_engine=self.engine).verify_result()
        with allure.step("Verify rbac file is deleted successfully"):
            self._verify_cmd_success(f"ls /etc/cluster_infra/rbac/{rbac_file_name}.yaml", should_succeed=False)
        with allure.step("Verify rbac file is deleted successfully"):
            assert rbac_file_name not in self.cluster.rbac.files.show_files(dut_engine=self.engine), f"Rbac {rbac_file_name} file is not deleted successfully"

    def delete_all_rbac_files(self):
        with allure.step("Delete all rbac files"):
            files = self.cluster.rbac.files.show_files(dut_engine=self.engine)
            for file_name in files.keys():
                self.delete_rbac_file(file_name)
        with allure.step("Verify all rbac files are deleted successfully"):
            assert not self.cluster.rbac.files.show_files(dut_engine=self.engine), "Rbac files are not deleted successfully"

    def update_rbac_file(self, rbac_file_name: str, should_succeed: bool = True):
        with allure.step("Update rbac file"):
            self.cluster_app.rbac.file.action_update(rbac_file_name).verify_result(should_succeed=should_succeed)
        if should_succeed:
            with allure.step("Verify rbac file is updated successfully"):
                rbac_file_output = self.cluster_app.rbac.file.parse_show()[ClusterConsts.NMX_RBAC_FILE]
                assert rbac_file_name in rbac_file_output, f"Rbac {rbac_file_name} file is not updated successfully"

    def restore_rbac_file(self):
        with allure.step("Restore rbac file"):
            self.cluster_app.rbac.file.action_restore().verify_result()
        with allure.step("Verify rbac file is restored successfully"):
            rbac_file_output = self.cluster_app.rbac.file.parse_show()[ClusterConsts.NMX_RBAC_FILE]
            assert not rbac_file_output, "Rbac file is not restored successfully, the content is not empty"

    def update_rbac_mode(self, mode: str):
        with allure.step("Update rbac mode"):
            self.cluster_app.rbac.mode.action_update(mode=mode).verify_result()
        with allure.step("Verify rbac mode is updated successfully"):
            assert mode in self.cluster_app.rbac.mode.show(), f"Rbac mode is not updated successfully"

    def restore_rbac_mode(self):
        with allure.step("Restore rbac mode"):
            self.cluster_app.rbac.mode.action_restore().verify_result()
        with allure.step("Verify rbac mode is restored successfully"):
            assert "disabled" in self.cluster_app.rbac.mode.show(), "Rbac mode is not restored successfully"

    def run_app_client(self, host: str, user: UserInfo, client_cert: CertInfo, client_cacert: CertInfo, expect_success: bool = True):
        with allure.step(f"Run app client on {host} for {self.cluster_app.app_name}"):
            if self.cluster_app.app_name == ClusterConsts.NMX_CONTROLLER:
                self.run_controller_client(host=host, user=user, client_cert=client_cert, client_cacert=client_cacert, expect_success=expect_success)
            elif self.cluster_app.app_name == ClusterConsts.NMX_TELEMETRY:
                self.run_telemetry_client(host=host, user=user, client_cert=client_cert, client_cacert=client_cacert, expect_success=expect_success)

    def run_telemetry_client(self, host: str, user: UserInfo, client_cert: CertInfo, client_cacert: CertInfo, expect_success: bool = True):
        endpoint = 'TelemetryService.Hello'
        port = ClusterConsts.NMX_TELEMETRY_ENVOY_PORT
        proto_path = ClusterConsts.NMX_TELEMETRY_PROTO_PATH
        self._run_grpc_client(host=host, endpoint=endpoint, user=user, client_cert=client_cert, client_cacert=client_cacert, port=port, expect_success=expect_success, proto_path=proto_path)

    def run_controller_client(self, host: str, user: UserInfo, client_cert: CertInfo, client_cacert: CertInfo, expect_success: bool = True):
        endpoint = 'nmx_c.NMX_Controller.Hello'
        port = ClusterConsts.NMX_CONTROLLER_ENVOY_PORT
        proto_path = ClusterConsts.NMX_CONTROLLER_PROTO_PATH
        self._run_grpc_client(host=host, endpoint=endpoint, user=user, client_cert=client_cert, client_cacert=client_cacert, port=port, expect_success=expect_success, proto_path=proto_path)

    def prepare_nmx_certs(self, server_certs: List[CertInfo], client_cas: List[CertInfo], encryption_mode: str = EncryptionMode.MTLS) -> Tuple[CertInfo, CertInfo]:
        scp_player = get_scp_player(self.engine)

        server_cert: CertInfo = server_certs[0]
        client_ca_cert: CertInfo = client_cas[0]

        with allure.step("import test certs"):
            import_certs_safely(server_certs, scp_player)
            import_cas_safely(client_cas, scp_player)

        with allure.step("Enable cluster port and setup security mode by binding test certs"):
            enable_cluster_app_manager_state(self.cluster_app.manager)
            self.cluster_app.manager.certificate.action_update(server_cert.name).verify_result()
            self.cluster_app.manager.ca_certificate.action_update(client_ca_cert.cacert_name).verify_result()
            self.cluster_app.manager.encryption.action_update(encryption_mode).verify_result()
        return server_cert, client_ca_cert

    def unbind_nmx_certs(self):
        """
        Release the certs bound by :meth:`prepare_nmx_certs` from the app manager.

        ``nv action delete system security certificate`` refuses to remove a cert that an application still
        references ("is currently being used by the following applications"), so the binding has to be
        released before the certs can be deleted.

        Encryption is released first: mTLS requires a bound cert/CA pair, so releasing those while the mode
        is still mTLS is rejected. Each field is released independently and failures are only logged - a
        factory reset may already have cleared the binding, this runs in teardown where raising would mask
        the real failure, and one field failing must not leave the others bound.
        """
        manager = self.cluster_app.manager
        fields_to_release = (("encryption", manager.encryption),
                             ("certificate", manager.certificate),
                             ("ca_certificate", manager.ca_certificate))
        with allure.step(f"Unbind certs from {self.cluster_app.app_name} manager"):
            for field_name, field in fields_to_release:
                failure = None
                try:
                    result = field.action_restore().ignore_result()
                    if not result.result:
                        failure = result.info or "the restore action reported failure without details"
                except Exception as e:
                    failure = e
                if failure:
                    logging.warning(f"Failed to release {field_name} on {self.cluster_app.app_name} manager: {failure}")

    def _run_grpc_client(self, host: str, endpoint: str, user: UserInfo, client_cert: CertInfo, client_cacert: CertInfo, port: int = None, expect_success: bool = True, proto_path: str = ""):
        payload: Dict[str, str] = {"gatewayId": "sasha",
                                   "major_version": "PROTO_MSG_MAJOR_VERSION", "minor_version": "PROTO_MSG_MINOR_VERSION"}
        grpc = GrpcCmdBuilder(host, port)
        if user:
            grpc.user_creds(user.username, user.password)
        if client_cert:
            grpc.cert(client_cert.private, client_cert.public)
        if client_cacert:
            grpc.ca(client_cacert.cacert)
        if endpoint:
            grpc.endpoint(endpoint)
        if payload:
            grpc.payload(payload)
        if proto_path:
            grpc.proto(proto_path)
        grpc_cmd = grpc.build()
        self._verify_result(client_cmd=grpc_cmd, expect_success=expect_success)

    def _verify_result(self, client_cmd: str, expect_success: bool):
        time.sleep(0.1)
        logging.info(f'running request:\n{client_cmd}\nexpect: {expect_success}')
        try:
            out = run_cmd(client_cmd)
            assert all(msg not in out for msg in BAD_RESPONSE_KEYWORDS), out
            logging.info('show succeeded')
            actual_success = True
        except Exception as e:
            logging.info('show failed')
            actual_success = False
            out = e
        assert actual_success == expect_success, f'show result not as expected. expected: {expect_success}. actual: {actual_success}\ncmd: {client_cmd}\nerr:\n{out}'

    def _verify_cmd_success(self, cmd: str, should_succeed: bool = True):
        with allure.step("Verify the command is successful"):
            cmd_output, exit_code = run_ssh_cmd_with_rc(self.engine, cmd)
        if should_succeed:
            assert exit_code == 0, f"The command should be successful: {cmd}"
        else:
            assert exit_code != 0, f"The command should fail: {cmd}"
        return cmd_output
