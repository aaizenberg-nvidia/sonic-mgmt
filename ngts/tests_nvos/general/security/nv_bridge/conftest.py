"""
Conftest for NV Bridge encryption tests.
"""

import logging
import os

import pytest

import ngts.tools.test_utils.allure_utils as allure

from ngts.nvos_constants.constants_nvos import SystemConsts
from ngts.nvos_tools.infra.CertificateGenerator import CertificateGenerator
from ngts.nvos_tools.infra.NvCommand import NvCommand
from ngts.nvos_tools.nmx.Cluster import Cluster
from ngts.tests_nvos.cluster.cluster_consts import ClusterConsts
from ngts.tests_nvos.cluster.cluster_tools import ClusterTools
from ngts.tests_nvos.general.security.certificate.CertInfo import CertInfo
from ngts.tests_nvos.general.security.certificate.helpers import (
    delete_certificates,
    delete_crl,
)
from ngts.tests_nvos.general.security.crl.helpers import (
    BridgeClusterInternalCrlValidator,
    BridgeSystemInternalCrlValidator,
)
from ngts.tests_nvos.general.security.helpers import (
    generate_certs,
    get_test_certs_dir_location,
    import_crl_safely,
)
from ngts.tests_nvos.general.security.nv_bridge.helpers import (
    BridgeCrlCerts,
    BridgeSpiffeCerts,
    BridgeSpiffeParams,
    build_internal_bridge_cert,
    generate_internal_certs,
    import_internal_certs,
    is_cluster_enabled,
    restore_internal_config_with_retry,
)
from ngts.tests_nvos.helpers.redmine_helpers import is_bug_active
from ngts.tests_nvos.system.gnmi.helpers import get_scp_player, verify_gnmi_client_tools_installed

logger = logging.getLogger(__name__)

_BRIDGE_RESTORE_FIXTURES = frozenset({
    "restore_system_internal_config",
    "restore_cluster_app_internal_config",
})
_BRIDGE_CRL_FIXTURES = frozenset({
    "import_crl_bridge_certs",
    "bridge_crl_factory",
})


def _test_uses_internal_restore(request: pytest.FixtureRequest) -> bool:
    return bool(_BRIDGE_RESTORE_FIXTURES & set(request.fixturenames))


def _cleanup_imported_bridge_security_artifacts(request: pytest.FixtureRequest) -> None:
    """Delete imported certs/CAs/CRLs from the DUT security store."""
    with allure.step("cleanup imported bridge security artifacts"):
        delete_certificates()
        delete_certificates(ca=True)
        if _BRIDGE_CRL_FIXTURES & set(request.fixturenames):
            delete_crl()


@pytest.fixture
def _bridge_cert_cleanup_after_restore(request: pytest.FixtureRequest):
    """Delete imported security artifacts only after internal-config restore.

    Restore fixtures depend on this anchor so pytest tears them down first
    (unbind internal encryption config) and runs cert/CA/CRL deletion last.
    """
    yield
    if _test_uses_internal_restore(request):
        _cleanup_imported_bridge_security_artifacts(request)


@pytest.fixture(scope="session", autouse=True)
def verify_grpc_tools_installed():
    """Ensure grpcurl is installed on the test player before running bridge tests."""
    verify_gnmi_client_tools_installed()


@pytest.fixture(scope="session")
def bridge_spiffe_variants() -> dict[str, BridgeSpiffeParams]:
    """Reusable SPIFFE variants for bridge tests."""
    common_spiffe = "spiffe://nvbridge/default"
    return {
        "same_spiffe_pair": BridgeSpiffeParams(
            primary_cert_spiffe=common_spiffe,
            alternate_cert_spiffe=common_spiffe,
            system_cert_spiffe=common_spiffe,
            cluster_cert_spiffe=common_spiffe,
        ),
        "different_spiffe_pair": BridgeSpiffeParams(
            primary_cert_spiffe="spiffe://nvbridge/primary",
            alternate_cert_spiffe="spiffe://nvbridge/alternate",
        ),
        "cert_only_spiffe": BridgeSpiffeParams(
            primary_cert_spiffe=common_spiffe,
            alternate_cert_spiffe="",
        ),
        "alt_only_spiffe": BridgeSpiffeParams(
            primary_cert_spiffe="",
            alternate_cert_spiffe=common_spiffe,
        ),
        "cluster_system_primary_mismatch": BridgeSpiffeParams(
            primary_cert_spiffe=common_spiffe,
            alternate_cert_spiffe=common_spiffe,
            system_cert_spiffe="spiffe://nvbridge/system",
            cluster_cert_spiffe="spiffe://nvbridge/cluster",
        ),
    }


def _resolve_spiffe_params(
    raw_params: BridgeSpiffeParams | dict[str, str] | str,
    variants: dict[str, BridgeSpiffeParams],
) -> BridgeSpiffeParams:
    if isinstance(raw_params, BridgeSpiffeParams):
        return raw_params
    if isinstance(raw_params, str):
        if raw_params not in variants:
            raise ValueError(f"Unknown SPIFFE variant '{raw_params}'. Supported variants: {list(variants)}")
        return variants[raw_params]
    return BridgeSpiffeParams(**raw_params)


@pytest.fixture(scope="module")
def generate_internal_bridge_certs(engines, dut_hostname):
    """Generate required certificates for testing."""
    with allure.step("import test certificates"):
        server_cert, client_ca_cert = generate_internal_certs(engines, "internal_nv_bridge", dut_hostname)
    yield server_cert, client_ca_cert


@pytest.fixture(scope="function")
def import_certs(engines, generate_internal_bridge_certs, request: pytest.FixtureRequest):
    """Import required certificates for testing."""
    server_cert, client_ca_cert = generate_internal_bridge_certs
    with allure.step("import test certificates"):
        import_internal_certs(engines, [server_cert, client_ca_cert], [client_ca_cert, server_cert])
    yield server_cert, client_ca_cert
    if not _test_uses_internal_restore(request):
        _cleanup_imported_bridge_security_artifacts(request)


@pytest.fixture(scope="function")
def generate_certs_with_alt(engines, dut_hostname):
    certs_location = get_test_certs_dir_location("certs_with_alt", dut_hostname)
    cn = "nv-bridge-client"
    dn = dut_hostname
    ip = engines.dut.ip
    with allure.step("Generate 2 certificates (cert and alt-cert) signed by the same CA"):
        cert = CertInfo("cert", "certificate for test", "", "", "", "", dn, ip, "", f"{cn}")
        cert_alt = CertInfo("alt-cert", "alternate certificate for test", "", "", "", "", dn, ip, "", f"{cn}")
        bridge_certs = [cert, cert_alt]
        bridge_certs_dir = os.path.join(certs_location, "bridge_certs")
        generate_certs(bridge_certs_dir, bridge_certs)
    with allure.step("Generate other certificate signed by a different CA"):
        other_cert = CertInfo("other-cert", "other certificate for test", "", "", "", "", dn, ip, "", f"{cn}")
        other_cert_dir = os.path.join(certs_location, "other_certs")
        generate_certs(other_cert_dir, [other_cert])
    yield cert, cert_alt, other_cert


@pytest.fixture(scope="function")
def import_certs_with_alt(engines, generate_certs_with_alt, request: pytest.FixtureRequest):
    """Import required certificates for testing."""
    cert, cert_alt, other_cert = generate_certs_with_alt
    with allure.step("import test certificates"):
        import_internal_certs(engines, [cert, cert_alt, other_cert], [cert, other_cert])
    yield cert, cert_alt, other_cert
    if not _test_uses_internal_restore(request):
        _cleanup_imported_bridge_security_artifacts(request)


@pytest.fixture(scope="function")
def generate_spiffe_bridge_certs(
    engines, dut_hostname, bridge_spiffe_variants, request,
) -> BridgeSpiffeCerts:
    """
    Generate SPIFFE-aware cert set for nv-bridge.

    Supports indirect parameterization with:
    - BridgeSpiffeParams instance
    - dict with BridgeSpiffeParams field names
    - variant key from bridge_spiffe_variants fixture
    """
    raw_params = getattr(request, "param", bridge_spiffe_variants["same_spiffe_pair"])
    params = _resolve_spiffe_params(raw_params, bridge_spiffe_variants)

    certs_location = get_test_certs_dir_location("spiffe_bridge_certs", dut_hostname)
    certs_dir = os.path.join(certs_location, "bridge_spiffe")
    cert_cn = "nv-bridge-client"
    dut_ip = engines.dut.ip

    with allure.step("Generate SPIFFE certificates for system/cluster internal primary/alternate"):
        system_primary = build_internal_bridge_cert(
            cert_name="spiffe-system-cert",
            cert_info="system primary spiffe cert",
            dut_hostname=dut_hostname,
            dut_ip=dut_ip,
            cert_cn=f"{cert_cn}-sys",
            spiffe_uri=params.system_primary_spiffe,
        )
        system_alt = build_internal_bridge_cert(
            cert_name="spiffe-system-alt-cert",
            cert_info="system alternate spiffe cert",
            dut_hostname=dut_hostname,
            dut_ip=dut_ip,
            cert_cn=f"{cert_cn}-sys-alt",
            spiffe_uri=params.alternate_cert_spiffe,
        )
        cluster_primary = build_internal_bridge_cert(
            cert_name="spiffe-cluster-cert",
            cert_info="cluster primary spiffe cert",
            dut_hostname=dut_hostname,
            dut_ip=dut_ip,
            cert_cn=f"{cert_cn}-cluster",
            spiffe_uri=params.cluster_primary_spiffe,
        )
        cluster_alt = build_internal_bridge_cert(
            cert_name="spiffe-cluster-alt-cert",
            cert_info="cluster alternate spiffe cert",
            dut_hostname=dut_hostname,
            dut_ip=dut_ip,
            cert_cn=f"{cert_cn}-cluster-alt",
            spiffe_uri=params.alternate_cert_spiffe,
        )
        generate_certs(certs_dir, [system_primary, system_alt, cluster_primary, cluster_alt])

    return BridgeSpiffeCerts(
        system_primary_cert=system_primary,
        system_alt_cert=system_alt,
        cluster_primary_cert=cluster_primary,
        cluster_alt_cert=cluster_alt,
        certs_dir=certs_dir,
        params=params,
    )


@pytest.fixture(scope="function")
def import_spiffe_bridge_certs(engines, generate_spiffe_bridge_certs: BridgeSpiffeCerts, request: pytest.FixtureRequest):
    """Import SPIFFE cert fixture independently from CRL fixture flow."""
    with allure.step("import SPIFFE bridge certificates"):
        import_internal_certs(engines, generate_spiffe_bridge_certs.all_certs, generate_spiffe_bridge_certs.all_certs)
    yield generate_spiffe_bridge_certs
    if not _test_uses_internal_restore(request):
        _cleanup_imported_bridge_security_artifacts(request)


@pytest.fixture(scope="function")
def generate_crl_bridge_certs(engines, dut_hostname) -> BridgeCrlCerts:
    """Generate CRL-focused cert set independently from SPIFFE fixtures."""
    certs_location = get_test_certs_dir_location("crl_bridge_certs", dut_hostname)
    certs_dir = os.path.join(certs_location, "bridge_crl")
    cert_cn = "nv-bridge-client"
    dut_ip = engines.dut.ip

    with allure.step("Generate CRL certificates for active and revoked scenarios"):
        primary_cert = build_internal_bridge_cert(
            cert_name="crl-primary-cert",
            cert_info="crl primary cert",
            dut_hostname=dut_hostname,
            dut_ip=dut_ip,
            cert_cn=f"{cert_cn}-crl-primary",
        )
        alt_cert = build_internal_bridge_cert(
            cert_name="crl-alt-cert",
            cert_info="crl alternate cert",
            dut_hostname=dut_hostname,
            dut_ip=dut_ip,
            cert_cn=f"{cert_cn}-crl-alt",
        )
        revoked_primary_cert = build_internal_bridge_cert(
            cert_name="crl-revoked-primary-cert",
            cert_info="crl revoked primary cert",
            dut_hostname=dut_hostname,
            dut_ip=dut_ip,
            cert_cn=f"{cert_cn}-crl-revoked-primary",
        )
        revoked_alt_cert = build_internal_bridge_cert(
            cert_name="crl-revoked-alt-cert",
            cert_info="crl revoked alternate cert",
            dut_hostname=dut_hostname,
            dut_ip=dut_ip,
            cert_cn=f"{cert_cn}-crl-revoked-alt",
        )
        generate_certs(certs_dir, [primary_cert, alt_cert, revoked_primary_cert, revoked_alt_cert])

    return BridgeCrlCerts(
        primary_cert=primary_cert,
        alt_cert=alt_cert,
        revoked_primary_cert=revoked_primary_cert,
        revoked_alt_cert=revoked_alt_cert,
        certs_dir=certs_dir,
    )


@pytest.fixture(scope="function")
def import_crl_bridge_certs(engines, generate_crl_bridge_certs: BridgeCrlCerts, request: pytest.FixtureRequest):
    """Import CRL cert fixture flow independently from SPIFFE fixtures."""
    with allure.step("import CRL bridge certificates"):
        import_internal_certs(engines, generate_crl_bridge_certs.all_certs, generate_crl_bridge_certs.all_certs)
    yield generate_crl_bridge_certs
    if not _test_uses_internal_restore(request):
        _cleanup_imported_bridge_security_artifacts(request)


@pytest.fixture(scope="function")
def bridge_crl_factory(engines, request: pytest.FixtureRequest):
    """Generate + import CRL files for bridge tests using standard CRL flow."""
    certificate_generator = CertificateGenerator()
    scp_player = get_scp_player(engines)

    def _create_crl(  # noqa: PLR0913
        crl_name: str,
        cert_to_revoke: CertInfo,
        certs_dir: str = "",
        create_empty: bool = False,
        ca_dest: str = "",
        ca_name: str = "ca",
        revoke_cert_name: str = "cert.crt",
    ) -> str:
        # Derive certs_dir from the cert itself when not explicitly provided.
        # cert.public is <base_dir>/<cert_name>/cert.pem → parent of parent is <base_dir>.
        if not certs_dir:
            certs_dir = os.path.dirname(os.path.dirname(cert_to_revoke.public))

        # Resolve CA location from cert metadata when caller does not override it.
        # Bridge cert fixtures store the CA path in cert_to_revoke.cacert
        # (e.g. <certs_dir>/ca/ca.crt), while revoke_cert defaults expect CA in cert dir.
        resolved_ca_dest = ca_dest
        if not resolved_ca_dest and cert_to_revoke.cacert:
            resolved_ca_dest = os.path.dirname(cert_to_revoke.cacert)
        resolved_ca_name = ca_name
        if cert_to_revoke.cacert and not ca_dest and ca_name == "ca":
            resolved_ca_name = os.path.splitext(os.path.basename(cert_to_revoke.cacert))[0]

        crl_path = certificate_generator.revoke_cert(
            certs_dir,
            crl_name,
            cert_to_revoke.name,
            ca_dest=resolved_ca_dest,
            create_empty=create_empty,
            ca_name=resolved_ca_name,
            revoke_cert_name=revoke_cert_name,
        )
        import_crl_safely(crl_name, crl_path, scp_player)
        return crl_path

    yield _create_crl

    if not _test_uses_internal_restore(request):
        with allure.step("cleanup imported CRLs for bridge test"):
            delete_crl()


@pytest.fixture(scope="function")
def bridge_system_internal_crl_validator(dut_hostname, engines):
    """Bridge CRL validator bound to system internal scope."""
    validator = BridgeSystemInternalCrlValidator(
        host=dut_hostname, ip=engines.dut.ip,
    )
    yield validator
    validator.cleanup()


@pytest.fixture(scope="function")
def bridge_cluster_internal_crl_validator(dut_hostname, engines, restore_cluster_app_internal_config):
    """Bridge CRL validator bound to selected cluster app internal scope."""
    validator = BridgeClusterInternalCrlValidator(
        host=dut_hostname,
        ip=engines.dut.ip,
        app_name=restore_cluster_app_internal_config,
    )
    yield validator
    validator.cleanup()


@pytest.fixture(scope="function")
def enable_cluster(setup_name, engines):
    with allure.step("Enable cluster"):
        cluster = Cluster()
        cluster_tools = ClusterTools()
        cluster.node.primary.set_cluster_node(
            op_param_name=SystemConsts.NV_BRIDGE_NODE_SERVER,
            op_param_value=engines.dut.ip,
        )
        cluster_tools.start_cluster(cluster, setup_name)
    yield cluster
    with allure.step("Disable cluster"):
        cluster.unset().verify_result()
        cluster_tools.stop_cluster(cluster)


@pytest.fixture(scope="function")
def disable_cluster(setup_name):
    """Ensure cluster is disabled after the test."""
    cluster = Cluster()
    cluster_tools = ClusterTools()
    with allure.step("ensure cluster is disabled before the test"):
        if is_cluster_enabled():
            cluster_tools.stop_cluster(cluster)
    yield
    with allure.step("ensure cluster is disabled after the test"):
        if is_cluster_enabled():
            cluster_tools.stop_cluster(cluster)


@pytest.fixture
def restore_system_internal_config(nv_command: NvCommand, _bridge_cert_cleanup_after_restore):
    """Restore system internal config after test.

    Retries each restore item and falls back to a full node restore so a failed
    restore never leaves a certificate bound to the config (a bound certificate
    cannot be deleted, which would make the next test fail on cert/CA import).
    """
    yield
    with allure.step("restore system internal config"):
        internal = nv_command.system.internal
        restore_internal_config_with_retry(
            restore_actions=[
                internal.encryption.action_restore,
                internal.crl.action_restore,
                internal.certificate.action_restore,
                internal.ca_certificate.action_restore,
                internal.alternate_certificate.action_restore,
            ],
            fallback_action=internal.action_restore,
            scope_name="system",
            fallback_settle_time=100 if is_bug_active(4824684) else 0,
        )


@pytest.fixture(scope="function")
def restore_cluster_app_internal_config(_bridge_cert_cleanup_after_restore):
    """Restore cluster app internal config after test.

    Retries each restore item and falls back to a full node restore so a failed
    restore never leaves a certificate bound to the config (a bound certificate
    cannot be deleted, which would make the next test fail on cert/CA import).
    """
    yield ClusterConsts.NMX_CONTROLLER
    with allure.step("restore cluster app internal config"):
        app_name = ClusterConsts.NMX_CONTROLLER
        internal = Cluster().apps.app_name[app_name].internal
        restore_internal_config_with_retry(
            restore_actions=[
                internal.encryption.action_restore,
                internal.crl.action_restore,
                internal.certificate.action_restore,
                internal.alternate_certificate.action_restore,
                internal.ca_certificate.action_restore,
            ],
            fallback_action=internal.action_restore,
            scope_name=app_name,
            fallback_settle_time=100 if is_bug_active(4824684) else 0,
        )
