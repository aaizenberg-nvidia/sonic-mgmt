import logging
import time

from ngts.nvos_constants.constants_nvos import ClusterApps, ClusterConsts
from ngts.nvos_tools.infra.CrlValidator import ClientConfig, CrlValidator
from ngts.nvos_tools.infra.CurlCmdBuilder import CurlCmdBuilder
from ngts.nvos_tools.infra.GrpcCmdBuilder import GrpcCmdBuilder
from ngts.nvos_tools.infra.NvBridgeTool import NvBridgeTool
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.nmx.Cluster import Cluster
from ngts.tests_nvos.general.security.certificate.CertInfo import CertInfo
from ngts.tests_nvos.general.security.nmx_cert.constants import CA_CERTIFICATE, CERTIFICATE, NV_BRIDGE_SERVER_PORT, EncryptionMode
from ngts.tests_nvos.general.security.nmx_cert.helpers import (
    disable_cluster,
    disable_cluster_app_manager_state,
    enable_cluster,
    enable_cluster_app_manager_state,
)
from ngts.tests_nvos.system.gnmi.GnmiClient import GnmicCmdBuilder
from ngts.tests_nvos.system.gnmi.helpers import verify_gnmi_client_tools_installed
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger(__name__)


class ApiCrlValidator(CrlValidator):
    """API/REST CRL validator."""

    # Allow nginx to finish reload after mTLS/CRL apply before client traffic.
    _API_SETTLE_AFTER_APPLY_SEC = 5

    @property
    def _feature_resource(self):
        return self.system.api

    def _bind_mtls_certs(self, server_cert: CertInfo, client_ca: CertInfo) -> None:
        engines = TestToolkit.engines
        assert engines is not None
        self._feature_resource.set(CERTIFICATE, server_cert.name).verify_result()
        self._feature_resource.mtls.set(CA_CERTIFICATE, client_ca.cacert_name).verify_result()
        self._feature_resource._general_cli_wrapper.apply_config(engines.dut)
        time.sleep(self._API_SETTLE_AFTER_APPLY_SEC)

    def _do_bind_crl(self, crl_name: str, ask_confirm: bool, should_succeed: bool) -> None:
        self._feature_resource.mtls.set("crl", crl_name, apply=True, ask_for_confirmation=ask_confirm).verify_result(
            should_succeed=should_succeed
        )

    def _post_bind_crl(self) -> None:
        time.sleep(self._API_SETTLE_AFTER_APPLY_SEC)

    def _verify_crl_bound(self, crl_name: str) -> None:
        output = self._feature_resource.mtls.parse_show()
        assert "crl" in output, "No crl found in output"
        assert crl_name in output["crl"], f"Expected CRL '{crl_name}' not found in show output"

    def _do_unbind_crl(self) -> None:
        self._feature_resource.mtls.unset("crl", apply=True).verify_result()
        time.sleep(self._API_SETTLE_AFTER_APPLY_SEC)

    def _do_cleanup(self) -> None:
        self._feature_resource.unset(apply=True).verify_result()

    def run_client(self, config: ClientConfig | None = None):
        if config is None:
            config = ClientConfig()

        method = "GET"
        resource = self.system.version.get_resource_path()

        cmd_builder = CurlCmdBuilder(method, self.host, resource)
        if config.user:
            cmd_builder.user_creds(config.user.username, config.user.password)
        if config.run_insecure or not config.client_cert:
            cmd_builder.insecure()
        if config.client_cert:
            cmd_builder.client_cert(config.client_cert.private, config.client_cert.public)
        if config.client_cacert:
            cmd_builder.cacert(config.client_cacert.cacert)
        curl_cmd = cmd_builder.build()
        self.verify_result(curl_cmd, config.expect_success)


class GnmiCrlValidator(CrlValidator):
    """gNMI CRL validator."""

    @property
    def _feature_resource(self):
        return self.system.gnmi_server

    def _bind_mtls_certs(self, server_cert: CertInfo, client_ca: CertInfo) -> None:
        engines = TestToolkit.engines
        assert engines is not None
        self._feature_resource.set(CERTIFICATE, server_cert.name).verify_result()
        self._feature_resource.mtls.set(CA_CERTIFICATE, client_ca.cacert_name).verify_result()
        self._feature_resource._general_cli_wrapper.apply_config(engines.dut)

    def _do_bind_crl(self, crl_name: str, ask_confirm: bool, should_succeed: bool) -> None:
        self._feature_resource.mtls.set("crl", crl_name, apply=True, ask_for_confirmation=ask_confirm).verify_result(
            should_succeed=should_succeed
        )

    def _post_bind_crl(self) -> None:
        time.sleep(5)

    def _verify_crl_bound(self, crl_name: str) -> None:
        output = self._feature_resource.mtls.parse_show()
        assert "crl" in output, "No crl found in output"
        assert crl_name in output["crl"], f"Expected CRL '{crl_name}' not found in show output"

    def _do_unbind_crl(self) -> None:
        self._feature_resource.mtls.unset("crl", apply=True).verify_result()
        time.sleep(5)

    def _do_cleanup(self) -> None:
        self._feature_resource.unset(apply=True).verify_result()

    def run_client(self, config: ClientConfig | None = None):
        if config is None:
            config = ClientConfig()

        verify_gnmi_client_tools_installed()
        gnmic = GnmicCmdBuilder(self.ip).capabilities()
        if config.run_insecure or not config.client_cert:
            gnmic.skip_verify()
        if config.user:
            gnmic.user_creds(config.user.username, config.user.password)
        if config.client_cert:
            gnmic.cert(config.client_cert.private, config.client_cert.public)
        if config.client_cacert:
            gnmic.ca(config.client_cacert.cacert)

        self.verify_result(gnmic.build(), config.expect_success)


class NmxCrlValidator(CrlValidator):
    """NMX CRL validator base class."""

    def __init__(self, host: str, ip: str, app_name: str, port: int, proto_path: str = ""):
        super().__init__(host, ip)
        self.app_name = app_name
        self.port = port
        self.cluster = Cluster()
        self.proto_path = proto_path
        verify_gnmi_client_tools_installed()

    @property
    def _nmx_app(self):
        return self.cluster.apps.app_name[self.app_name]

    def _bind_mtls_certs(self, server_cert: CertInfo, client_ca: CertInfo) -> None:
        nmx_app = self._nmx_app

        with allure.step("Enable cluster and setup mtls by binding test certs"):
            enable_cluster()
            enable_cluster_app_manager_state(nmx_app.manager)
            nmx_app.manager.certificate.action_update(server_cert.name).verify_result()
            nmx_app.manager.ca_certificate.action_update(client_ca.cacert_name).verify_result()
            nmx_app.manager.encryption.action_update(EncryptionMode.MTLS).verify_result()

    def _do_bind_crl(self, crl_name: str, ask_confirm: bool, should_succeed: bool) -> None:
        self._nmx_app.manager.crl.action_update(crl_name).verify_result(should_succeed=should_succeed)

    def _post_bind_crl(self) -> None:
        pass

    def _verify_crl_bound(self, crl_name: str) -> None:
        nmx_app = self._nmx_app
        with allure.independent_step("verify crl shown in nmx app manager output"):
            output = nmx_app.manager.parse_show()
            assert "crl" in output, "No crl found in output"
        with allure.independent_step("verify crl shown in nmx app manager crl output"):
            output = nmx_app.manager.crl.parse_show()
            assert "crl" in output, "No crl found in output"
            assert crl_name in output["crl"], f"Expected CRL '{crl_name}' not found in show output"

    def _do_unbind_crl(self) -> None:
        self._nmx_app.manager.crl.action_restore().verify_result()

    def _do_cleanup(self) -> None:
        app_manager = self._nmx_app.manager
        app_manager.encryption.action_restore().verify_result()
        app_manager.crl.action_restore().verify_result()
        app_manager.certificate.action_restore().verify_result()
        app_manager.ca_certificate.action_restore().verify_result()
        disable_cluster_app_manager_state(app_manager)
        disable_cluster()

    def run_client(self, config: ClientConfig | None = None):
        if config is None:
            config = ClientConfig()

        match self.app_name:
            case ClusterApps.NMX_CONTROLLER:
                endpoint = "nmx_c.NMX_Controller.Hello"
            case ClusterApps.NMX_TELEMETRY:
                endpoint = "TelemetryService.Hello"
            case _:
                raise ValueError(f"Invalid app name: {self.app_name}")

        port = config.port or self.port

        payload = {
            "gatewayId": "sasha",
            "major_version": "PROTO_MSG_MAJOR_VERSION",
            "minor_version": "PROTO_MSG_MINOR_VERSION",
        }
        grpc = GrpcCmdBuilder(self.ip, port)
        if config.user:
            grpc.user_creds(config.user.username, config.user.password)
        if config.client_cert:
            grpc.cert(config.client_cert.private, config.client_cert.public)
        if config.client_cacert:
            grpc.ca(config.client_cacert.cacert)
        if endpoint:
            grpc.endpoint(endpoint)
        if payload:
            grpc.payload(payload)
        if self.proto_path:
            grpc.proto(self.proto_path)
        grpc_cmd = grpc.build()
        self.verify_result(grpc_cmd, config.expect_success)


class NmxControllerCrlValidator(NmxCrlValidator):
    """NMX Controller CRL validator."""

    def __init__(self, host: str, ip: str):
        super().__init__(host, ip, ClusterApps.NMX_CONTROLLER, ClusterConsts.NMX_CONTROLLER_ENVOY_PORT)


class NmxTelemetryCrlValidator(NmxCrlValidator):
    """NMX Telemetry CRL validator."""

    def __init__(self, host: str, ip: str):
        super().__init__(
            host, ip, ClusterApps.NMX_TELEMETRY, ClusterConsts.NMX_TELEMETRY_ENVOY_PORT, ClusterConsts.NMX_TELEMETRY_PROTO_PATH
        )


class BridgeInternalCrlValidator(CrlValidator):
    """Base CRL validator for nv-bridge internal resources."""

    def __init__(self, host: str, ip: str):
        super().__init__(host, ip)
        self.cluster = Cluster()

    @property
    def _internal_resource(self):
        raise NotImplementedError

    @property
    def _internal_crl_resource(self):
        raise NotImplementedError

    @staticmethod
    def _verify_expected_crl(output: dict, crl_name: str, context: str):
        assert "crl" in output, f"No crl found in {context} output"
        crl_value = output["crl"]
        if isinstance(crl_value, list):
            assert crl_name in crl_value, f"Expected CRL '{crl_name}' not found in {context} output: {crl_value}"
            return
        assert str(crl_value) == crl_name or crl_name in str(crl_value), (
            f"Expected CRL '{crl_name}' not found in {context} output: {crl_value}"
        )

    def _do_bind_crl(self, crl_name: str, ask_confirm: bool, should_succeed: bool) -> None:
        self._internal_crl_resource.action_update(crl_name).verify_result(should_succeed=should_succeed)

    def _post_bind_crl(self) -> None:
        pass

    def _verify_crl_bound(self, crl_name: str) -> None:
        with allure.independent_step("verify crl shown in internal show output"):
            self._verify_expected_crl(self._internal_resource.parse_show(), crl_name, "internal")
        with allure.independent_step("verify crl shown in internal crl show output"):
            self._verify_expected_crl(self._internal_crl_resource.parse_show(), crl_name, "internal-crl")

    def _do_unbind_crl(self) -> None:
        self._internal_crl_resource.action_restore().verify_result()

    def run_client(self, config: ClientConfig | None = None):
        if config is None:
            config = ClientConfig()

        port = config.port or NV_BRIDGE_SERVER_PORT
        bridge_tool = NvBridgeTool(self.ip, port)
        client_cert = config.client_cert or self.configured_server_cert
        client_cacert = config.client_cacert or self.configured_client_ca
        plaintext = config.run_insecure and not client_cert and not client_cacert
        bridge_tool.run_bridge_hello(
            client_cert=client_cert,
            client_cacert=client_cacert,
            plaintext=plaintext,
            expect_success=config.expect_success,
        )


class BridgeSystemInternalCrlValidator(BridgeInternalCrlValidator):
    """CRL validator for system internal nv-bridge scope."""

    @property
    def _internal_resource(self):
        return self.system.internal

    @property
    def _internal_crl_resource(self):
        return self.system.internal.crl

    def _bind_mtls_certs(self, server_cert: CertInfo, client_ca: CertInfo) -> None:
        internal = self.system.internal
        internal.certificate.action_update(server_cert.name).verify_result()
        internal.ca_certificate.action_update(client_ca.cacert_name).verify_result()
        internal.encryption.action_update(EncryptionMode.MTLS).verify_result()

    def _do_cleanup(self) -> None:
        internal = self.system.internal
        internal.encryption.action_restore().verify_result()
        internal.crl.action_restore().verify_result()
        internal.certificate.action_restore().verify_result()
        internal.ca_certificate.action_restore().verify_result()
        internal.alternate_certificate.action_restore().verify_result()


class BridgeClusterInternalCrlValidator(BridgeInternalCrlValidator):
    """CRL validator for cluster app internal nv-bridge scope."""

    SUPPORTED_APPS = [ClusterApps.NMX_CONTROLLER, ClusterApps.NMX_TELEMETRY]

    def __init__(self, host: str, ip: str, app_name: str):
        app_name = self._normalize_app_name(app_name)
        super().__init__(host, ip)
        self.app_name = app_name

    @classmethod
    def _normalize_app_name(cls, app_name: str) -> str:
        if app_name in cls.SUPPORTED_APPS:
            return app_name
        if app_name.startswith(ClusterConsts.NMX_CONTROLLER_PREFIX):
            return ClusterApps.NMX_CONTROLLER
        if app_name.startswith(ClusterConsts.NMX_TELEMETRY_PREFIX):
            return ClusterApps.NMX_TELEMETRY
        raise ValueError(
            f"Unsupported cluster app '{app_name}'. Supported apps: {cls.SUPPORTED_APPS}, "
            f"prefixes: {ClusterConsts.NMX_CONTROLLER_PREFIX}, {ClusterConsts.NMX_TELEMETRY_PREFIX}"
        )

    @property
    def _cluster_app(self):
        return self.cluster.apps.app_name[self.app_name]

    @property
    def _internal_resource(self):
        return self._cluster_app.internal

    @property
    def _internal_crl_resource(self):
        return self._cluster_app.internal.crl

    def _bind_mtls_certs(self, server_cert: CertInfo, client_ca: CertInfo) -> None:
        enable_cluster()
        internal = self._internal_resource
        internal.certificate.action_update(server_cert.name).verify_result()
        internal.ca_certificate.action_update(client_ca.cacert_name).verify_result()
        internal.encryption.action_update(EncryptionMode.MTLS).verify_result()
        time.sleep(1)

    def _do_cleanup(self) -> None:
        internal = self._internal_resource
        internal.encryption.action_restore().verify_result()
        internal.crl.action_restore().verify_result()
        internal.certificate.action_restore().verify_result()
        internal.ca_certificate.action_restore().verify_result()
        internal.alternate_certificate.action_restore().verify_result()
