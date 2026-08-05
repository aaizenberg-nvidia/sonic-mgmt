"""
Helper functions for NV Bridge encryption tests.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable

from retry.api import retry_call

from devts.infra.tools.connection_tools.linux_ssh_engine import LinuxSshEngine

import ngts.tools.test_utils.allure_utils as allure
from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from ngts.ngts_types import EnginesT
from ngts.nvos_constants.constants_nvos import ClusterApps, ClusterConsts
from ngts.nvos_tools.infra.BaseComponent import BaseComponent
from ngts.nvos_tools.infra.DutUtilsTool import RebootParams
from ngts.nvos_tools.infra.NvBridgeTool import verify_nv_bridge_has_connection
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.ResultObj import ResultObj
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.nvos_tools.nmx.Cluster import Cluster
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.cluster.cluster_tools import ClusterTools
from ngts.tests_nvos.general.security.certificate.CertInfo import CertInfo
from ngts.tests_nvos.general.security.certificate.helpers import (
    verify_file_exists_in_dut,
)
from ngts.tests_nvos.general.security.helpers import (
    get_test_certs_dir_location,
    import_cas_safely,
    import_certs_safely,
    setup_certs_for_tests,
)
from ngts.tests_nvos.general.security.nmx_cert.constants import (
    ALTERNATE_CERTIFICATE,
    CA_CERTIFICATE,
    CERTIFICATE,
    CLUSTER_INTERNAL_JSON_PATH,
    CRL,
    DISABLED,
    ENCRYPTION,
    ITEM_NOT_EXIST_ERR,
    STATE,
    SYSTEM_INTERNAL_JSON_PATH,
    ClusterInternalJsonFields,
    Defaults,
    EncryptionMode,
    FieldsInShowOf,
    SystemInternalJsonFields,
)
from ngts.tests_nvos.system.gnmi.helpers import get_scp_player

logger = logging.getLogger(__name__)

# System internal certificate paths
SYSTEM_INTERNAL_CERT_PATH = "/etc/internal/cert/{}.crt"
SYSTEM_INTERNAL_KEY_PATH = "/etc/internal/cert/{}.key"
SYSTEM_INTERNAL_CACERT_PATH = "/etc/internal/ca_cert/{}.crt"
SYSTEM_INTERNAL_ALTERNATE_CERT_PATH = "/etc/internal/alt_cert/{}.crt"
SYSTEM_INTERNAL_ALTERNATE_KEY_PATH = "/etc/internal/alt_cert/{}.key"

# Cluster app internal certificate paths (different from manager paths)
CLUSTER_INTERNAL_CERT_PATH = "/etc/{}/int_cert/{}.crt"
CLUSTER_INTERNAL_KEY_PATH = "/etc/{}/int_cert/{}.key"
CLUSTER_INTERNAL_CACERT_PATH = "/etc/{}/int_ca_cert/{}.crt"
CLUSTER_INTERNAL_ALTERNATE_CERT_PATH = "/etc/{}/int_alt_cert/{}.crt"
CLUSTER_INTERNAL_ALTERNATE_KEY_PATH = "/etc/{}/int_alt_cert/{}.key"


@dataclass(frozen=True)
class BridgeSpiffeParams:
    """SPIFFE values for primary/alternate and system/cluster override use-cases."""

    primary_cert_spiffe: str
    alternate_cert_spiffe: str
    system_cert_spiffe: str | None = None
    cluster_cert_spiffe: str | None = None

    @property
    def system_primary_spiffe(self) -> str:
        return self.system_cert_spiffe or self.primary_cert_spiffe

    @property
    def cluster_primary_spiffe(self) -> str:
        return self.cluster_cert_spiffe or self.primary_cert_spiffe


@dataclass(frozen=True)
class BridgeSpiffeCerts:
    """SPIFFE certs for nv-bridge tests."""

    system_primary_cert: CertInfo
    system_alt_cert: CertInfo
    cluster_primary_cert: CertInfo
    cluster_alt_cert: CertInfo
    certs_dir: str
    params: BridgeSpiffeParams

    @property
    def all_certs(self) -> list[CertInfo]:
        return [self.system_primary_cert, self.system_alt_cert, self.cluster_primary_cert, self.cluster_alt_cert]


@dataclass(frozen=True)
class BridgeCrlCerts:
    """CRL-focused cert set for nv-bridge tests."""

    primary_cert: CertInfo
    alt_cert: CertInfo
    revoked_primary_cert: CertInfo
    revoked_alt_cert: CertInfo
    certs_dir: str

    @property
    def all_certs(self) -> list[CertInfo]:
        return [self.primary_cert, self.alt_cert, self.revoked_primary_cert, self.revoked_alt_cert]


def normalize_cluster_app_name(app_name: str) -> str:
    """Normalize cluster app aliases to canonical names."""
    if app_name in ClusterApps.ALL_APPS:
        return app_name
    if app_name.startswith(ClusterConsts.NMX_CONTROLLER_PREFIX):
        return ClusterApps.NMX_CONTROLLER
    if app_name.startswith(ClusterConsts.NMX_TELEMETRY_PREFIX):
        return ClusterApps.NMX_TELEMETRY
    raise ValueError(f"Unsupported cluster app name: {app_name}")


def is_cluster_enabled() -> bool:
    """Check if cluster is enabled."""
    with allure.step("check if cluster is enabled"):
        output = OutputParsingTool.parse_json_str_to_dictionary(Cluster().show()).get_returned_value()
        return output.get(STATE) != DISABLED


def verify_item_not_exist(output: str) -> bool:
    """Verify that command output indicates item does not exist."""
    return ITEM_NOT_EXIST_ERR in output


def verify_component_show(
    component: BaseComponent,
    required_fields: list[str],
    value_expectations: dict[str, str | None | int],
    expect_success: bool = True,
    dut_engine: LinuxSshEngine | None = None,
):
    """
    Verify show command output for a component.

    Args:
        component: The component to verify show output for
        required_fields: List of fields that must exist in output
        value_expectations: Dict of field names to expected values
        expect_success: Whether the show command should succeed
        dut_engine: Target engine. When None, uses TestToolkit default (engines.dut).
    """
    with allure.step(f"verify show: {component._resource_path}"):
        with allure.step("run show command"):
            output = component.show(should_succeed=expect_success, dut_engine=dut_engine)
            if not expect_success:
                return
        out = OutputParsingTool.parse_json_str_to_dictionary(output).verify_result()
        ValidationTool.verify_all_fields_value_exist_in_output_dictionary(out, required_fields, check_empty_values=False).verify_result()
        for field_name, expected in value_expectations.items():
            ValidationTool.verify_field_value_in_output(out, field_name, expected).verify_result()


def verify_system_internal_show(
    expect_cert: str = "",
    expect_cacert: str = "",
    expect_alt_cert: str = "",
    expect_encryption: str = Defaults.ENCRYPTION,
    expect_crl: str | None = None,
    dut_engine: LinuxSshEngine | None = None,
):
    """Verify system internal show command output.

    Args:
        expect_crl: When not None, the ``crl`` field is included in the
            expected fields and its value is validated against this argument.
            Use ``None`` (default) when CRL is not configured so existing
            callers stay unaffected.
    """
    required_fields = list(FieldsInShowOf.INTERNAL)
    value_expectations: dict[str, str | None | int] = {
        CERTIFICATE: expect_cert,
        CA_CERTIFICATE: expect_cacert,
        ENCRYPTION: expect_encryption,
        ALTERNATE_CERTIFICATE: expect_alt_cert,
    }
    if expect_crl is not None:
        required_fields.append(CRL)
        value_expectations[CRL] = expect_crl

    verify_component_show(
        System().internal,
        required_fields,
        value_expectations,
        dut_engine=dut_engine,
    )


def verify_system_internal_crl_show(expect_crl: str = "", dut_engine: LinuxSshEngine | None = None):
    """Verify system internal CRL show command output."""
    verify_component_show(
        System().internal.crl,
        FieldsInShowOf.CRL,
        {
            CRL: expect_crl,
        },
        dut_engine=dut_engine,
    )


def verify_system_internal_json(
    dut_engine: LinuxSshEngine,
    expected_cert: str = "",
    expected_cacert: str = "",
    expected_alt_cert: str = "",
    expected_encryption: str = Defaults.ENCRYPTION,
):
    """Verify system internal json output."""
    with allure.step("verify system internal json"):
        output: str = dut_engine.run_cmd(f"sudo cat {SYSTEM_INTERNAL_JSON_PATH}; echo")
        output = OutputParsingTool.parse_json_str_to_dictionary(output).verify_result()
        cert_path = SYSTEM_INTERNAL_CERT_PATH.format(expected_cert) if expected_cert else ""
        cert_key_path = SYSTEM_INTERNAL_KEY_PATH.format(expected_cert) if expected_cert else ""

        cacert_path = SYSTEM_INTERNAL_CACERT_PATH.format(expected_cacert) if expected_cacert else ""

        alt_cert_path = SYSTEM_INTERNAL_ALTERNATE_CERT_PATH.format(expected_alt_cert) if expected_alt_cert else ""
        alt_cert_key_path = SYSTEM_INTERNAL_ALTERNATE_KEY_PATH.format(expected_alt_cert) if expected_alt_cert else ""

        with allure.independent_step("verify certificate path"):
            assert output.get(SystemInternalJsonFields.CERTIFICATE, "") == cert_path, (
                f"certificate path is not expected: {output.get(SystemInternalJsonFields.CERTIFICATE, '')} != {cert_path}"
            )
        with allure.independent_step("verify certificate private key path"):
            cert_key_value = output.get(SystemInternalJsonFields.CERTIFICATE_PRIVATE_KEY, "")
            assert cert_key_value == cert_key_path, f"certificate private key path is not expected: {cert_key_value} != {cert_key_path}"
        with allure.independent_step("verify ca certificate path"):
            assert output.get(SystemInternalJsonFields.CA_CERTIFICATE, "") == cacert_path, (
                f"ca certificate path is not expected: {output.get(SystemInternalJsonFields.CA_CERTIFICATE, '')} != {cacert_path}"
            )
        with allure.independent_step("verify alternate certificate path"):
            alt_cert_value = output.get(SystemInternalJsonFields.ALTERNATE_CERTIFICATE, "")
            assert alt_cert_value == alt_cert_path, f"alternate certificate path is not expected: {alt_cert_value} != {alt_cert_path}"
        with allure.independent_step("verify alternate certificate private key path"):
            alt_cert_key_value = output.get(SystemInternalJsonFields.ALTERNATE_CERTIFICATE_PRIVATE_KEY, "")
            assert alt_cert_key_value == alt_cert_key_path, (
                f"alternate certificate private key path is not expected: {alt_cert_key_value} != {alt_cert_key_path}"
            )
        with allure.independent_step("verify encryption"):
            out_encryption = output.get(SystemInternalJsonFields.ENCRYPTION, "")
            assert out_encryption == expected_encryption, f"encryption is not expected: {out_encryption} != {expected_encryption}"


def verify_system_internal_spiffe_json(
    dut_engine: LinuxSshEngine,
    expected_cert_spiffe: str = "",
    expected_alt_cert_spiffe: str = "",
):
    """Verify system internal SPIFFE fields in json output."""
    with allure.step("verify system internal spiffe in json"):
        output: str = dut_engine.run_cmd(f"sudo cat {SYSTEM_INTERNAL_JSON_PATH}; echo")
        output = OutputParsingTool.parse_json_str_to_dictionary(output).verify_result()
        with allure.independent_step("verify certificate-spiffe"):
            cert_spiffe = output.get(SystemInternalJsonFields.CERTIFICATE_SPIFFE, "") or ""
            assert cert_spiffe == expected_cert_spiffe, f"certificate-spiffe is not expected: {cert_spiffe} != {expected_cert_spiffe}"
        with allure.independent_step("verify alternate-certificate-spiffe"):
            alt_cert_spiffe = output.get(SystemInternalJsonFields.ALTERNATE_CERTIFICATE_SPIFFE, "") or ""
            assert alt_cert_spiffe == expected_alt_cert_spiffe, (
                f"alternate-certificate-spiffe is not expected: {alt_cert_spiffe} != {expected_alt_cert_spiffe}"
            )


def verify_cluster_app_internal_json(  # noqa: PLR0913
    dut_engine: LinuxSshEngine,
    app_name: str,
    expected_cert: str = "",
    expected_cacert: str = "",
    expected_alt_cert: str = "",
    expected_encryption: str = Defaults.ENCRYPTION,
):
    """Verify cluster app internal json output."""
    with allure.step("verify cluster app internal json"):
        full_app_name = normalize_cluster_app_name(app_name)
        output: str = dut_engine.run_cmd(f"sudo cat {CLUSTER_INTERNAL_JSON_PATH}; echo")
        output = OutputParsingTool.parse_json_str_to_dictionary(output).verify_result()
        cert_path = CLUSTER_INTERNAL_CERT_PATH.format(full_app_name, expected_cert) if expected_cert else ""
        cert_key_path = CLUSTER_INTERNAL_KEY_PATH.format(full_app_name, expected_cert) if expected_cert else ""
        cacert_path = CLUSTER_INTERNAL_CACERT_PATH.format(full_app_name, expected_cacert) if expected_cacert else ""
        alt_cert_path = CLUSTER_INTERNAL_ALTERNATE_CERT_PATH.format(full_app_name, expected_alt_cert) if expected_alt_cert else ""
        alt_cert_key_path = CLUSTER_INTERNAL_ALTERNATE_KEY_PATH.format(full_app_name, expected_alt_cert) if expected_alt_cert else ""

        with allure.independent_step("verify certificate path"):
            cert_value = output.get(ClusterInternalJsonFields.CERTIFICATE.format(full_app_name), "")
            assert cert_value == cert_path, f"certificate path is not expected: {cert_value} != {cert_path}"
        with allure.independent_step("verify certificate private key path"):
            cert_key_value = output.get(
                ClusterInternalJsonFields.CERTIFICATE_PRIVATE_KEY.format(full_app_name),
                "",
            )
            assert cert_key_value == cert_key_path, f"certificate private key path is not expected: {cert_key_value} != {cert_key_path}"
        with allure.independent_step("verify ca certificate path"):
            cacert_value = output.get(ClusterInternalJsonFields.CA_CERTIFICATE.format(full_app_name), "")
            assert cacert_value == cacert_path, f"ca certificate path is not expected: {cacert_value} != {cacert_path}"
        with allure.independent_step("verify alternate certificate path"):
            alt_cert_value = output.get(
                ClusterInternalJsonFields.ALTERNATE_CERTIFICATE.format(full_app_name),
                "",
            )
            assert alt_cert_value == alt_cert_path, f"alternate certificate path is not expected: {alt_cert_value} != {alt_cert_path}"
        with allure.independent_step("verify alternate certificate private key path"):
            alt_cert_key_value = output.get(
                ClusterInternalJsonFields.ALTERNATE_CERTIFICATE_PRIVATE_KEY.format(full_app_name),
                "",
            )
            assert alt_cert_key_value == alt_cert_key_path, (
                f"alternate certificate private key path is not expected: {alt_cert_key_value} != {alt_cert_key_path}"
            )
        with allure.independent_step("verify encryption"):
            out_encryption = output.get(ClusterInternalJsonFields.ENCRYPTION.format(full_app_name), "")
            assert out_encryption == expected_encryption, f"encryption is not expected: {out_encryption} != {expected_encryption}"


def verify_cluster_app_internal_spiffe_json(
    dut_engine: LinuxSshEngine,
    app_name: str,
    expected_cert_spiffe: str = "",
    expected_alt_cert_spiffe: str = "",
):
    """Verify cluster app internal SPIFFE fields in json output."""
    with allure.step("verify cluster app internal spiffe in json"):
        full_app_name = normalize_cluster_app_name(app_name)
        output: str = dut_engine.run_cmd(f"sudo cat {CLUSTER_INTERNAL_JSON_PATH}; echo")
        output = OutputParsingTool.parse_json_str_to_dictionary(output).verify_result()
        with allure.independent_step("verify internal spiffe"):
            cert_spiffe = output.get(ClusterInternalJsonFields.CERTIFICATE_SPIFFE.format(full_app_name), "") or ""
            assert cert_spiffe == expected_cert_spiffe, f"internal spiffe is not expected: {cert_spiffe} != {expected_cert_spiffe}"
        with allure.independent_step("verify internal alternate spiffe"):
            alt_cert_spiffe = output.get(ClusterInternalJsonFields.ALTERNATE_CERTIFICATE_SPIFFE.format(full_app_name), "") or ""
            assert alt_cert_spiffe == expected_alt_cert_spiffe, (
                f"internal alternate spiffe is not expected: {alt_cert_spiffe} != {expected_alt_cert_spiffe}"
            )


def verify_cluster_app_internal_show(  # noqa: PLR0913
    app_name: str,
    expect_cert: str = "",
    expect_cacert: str = "",
    expect_encryption: str = Defaults.ENCRYPTION,
    expect_alt_cert: str = "",
    expect_crl: str | None = None,
    expect_success: bool = True,
    dut_engine: LinuxSshEngine | None = None,
):
    """Verify cluster app internal show command output.

    Args:
        expect_crl: When not None, the ``crl`` field is included in the
            expected fields and its value is validated against this argument.
            Use ``None`` (default) when CRL is not configured so existing
            callers stay unaffected.
    """
    full_app_name = normalize_cluster_app_name(app_name)
    required_fields = list(FieldsInShowOf.INTERNAL)
    value_expectations: dict[str, str | None | int] = {
        CERTIFICATE: expect_cert,
        CA_CERTIFICATE: expect_cacert,
        ENCRYPTION: expect_encryption,
        ALTERNATE_CERTIFICATE: expect_alt_cert,
    }
    if expect_crl is not None:
        required_fields.append(CRL)
        value_expectations[CRL] = expect_crl

    verify_component_show(
        Cluster().apps.app_name[full_app_name].internal,
        required_fields,
        value_expectations,
        expect_success=expect_success,
        dut_engine=dut_engine,
    )


def verify_cluster_app_internal_crl_show(
    app_name: str,
    expect_crl: str = "",
    dut_engine: LinuxSshEngine | None = None,
):
    """Verify cluster app internal CRL show command output."""
    full_app_name = normalize_cluster_app_name(app_name)
    verify_component_show(
        Cluster().apps.app_name[full_app_name].internal.crl,
        FieldsInShowOf.CRL,
        {
            CRL: expect_crl,
        },
        dut_engine=dut_engine,
    )


def verify_system_internal_cert_files(
    dut_engine: LinuxSshEngine,
    expected_cert_id: str = "",
    expected_cacert_id: str = "",
    expected_alt_cert_id: str = "",
):
    """Verify system internal certificate files exist."""
    with allure.step("verify system internal cert files"):
        if expected_cert_id:
            verify_file_exists_in_dut(
                SYSTEM_INTERNAL_CERT_PATH.format(expected_cert_id),
                dut_engine,
                should_exist=True,
            )
            verify_file_exists_in_dut(
                SYSTEM_INTERNAL_KEY_PATH.format(expected_cert_id),
                dut_engine,
                should_exist=True,
            )
        if expected_alt_cert_id:
            verify_file_exists_in_dut(
                SYSTEM_INTERNAL_ALTERNATE_CERT_PATH.format(expected_alt_cert_id),
                dut_engine,
                should_exist=True,
            )
            verify_file_exists_in_dut(
                SYSTEM_INTERNAL_ALTERNATE_KEY_PATH.format(expected_alt_cert_id),
                dut_engine,
                should_exist=True,
            )
        if expected_cacert_id:
            verify_file_exists_in_dut(
                SYSTEM_INTERNAL_CACERT_PATH.format(expected_cacert_id),
                dut_engine,
                should_exist=True,
            )


def verify_cluster_app_internal_cert_files(
    app_name: str,
    dut_engine: LinuxSshEngine,
    expected_cert_id: str = "",
    expected_cacert_id: str = "",
    expected_alt_cert_id: str = "",
):
    """Verify cluster app internal certificate files exist."""
    full_app_name = normalize_cluster_app_name(app_name)
    with allure.step(f"verify {app_name} internal cert files"):
        if expected_cert_id:
            verify_file_exists_in_dut(
                CLUSTER_INTERNAL_CERT_PATH.format(full_app_name, expected_cert_id),
                dut_engine,
                should_exist=True,
            )
            verify_file_exists_in_dut(
                CLUSTER_INTERNAL_KEY_PATH.format(full_app_name, expected_cert_id),
                dut_engine,
                should_exist=True,
            )
        if expected_alt_cert_id:
            verify_file_exists_in_dut(
                CLUSTER_INTERNAL_ALTERNATE_CERT_PATH.format(full_app_name, expected_alt_cert_id),
                dut_engine,
                should_exist=True,
            )
            verify_file_exists_in_dut(
                CLUSTER_INTERNAL_ALTERNATE_KEY_PATH.format(full_app_name, expected_alt_cert_id),
                dut_engine,
                should_exist=True,
            )
        if expected_cacert_id:
            verify_file_exists_in_dut(
                CLUSTER_INTERNAL_CACERT_PATH.format(full_app_name, expected_cacert_id),
                dut_engine,
                should_exist=True,
            )


def wait_for_cluster_app_update(cluster: Cluster, engine: LinuxSshEngine):
    """Wait for cluster app state update to propagate."""
    with allure.step("wait for cluster app update"):
        ClusterTools.wait_for_apps_to_be_in_wanted_state(
            cluster,
            cluster_expected_state="enabled",
            nmx_c_expected_state="up",
            engine=engine,
        )
        time.sleep(1)


def save_config_and_reboot(engines: EnginesT, settle_time: int = 0) -> None:
    """Persist config and force-reboot the DUT, waiting reliably for NVOS to become functional.

    The reboot is issued with ``RebootParams(topology_obj=...)`` so readiness is gated on the
    serial-console "System is ready" banner. Without a topology object, ``action_reboot`` falls
    back to a CLI probe (``wait_until_cli_is_up``) that a slow NVUE init can satisfy prematurely:
    a ``nv show system`` read-timeout is swallowed as success, yielding a false "ready" that is
    immediately followed by ``Error: NVOS CLI is unavailable`` during the health check. Gating on
    the serial banner avoids that race.

    Args:
        engines: Test engines (uses ``engines.dut``).
        settle_time: Optional extra seconds to sleep after NVOS is functional, to let cluster /
            nmx-c services finish converging before further commands (e.g. syslog error scans).
    """
    with allure.step("Save config and reboot DUT"):
        NvueGeneralCli.save_config(engines.dut)
        System().action_reboot(
            "force",
            engine=engines.dut,
            reboot_params=RebootParams(topology_obj=getattr(TestToolkit, "topology_obj", None)),
        ).verify_result()
        engines.dut.disconnect()
        if settle_time:
            time.sleep(settle_time)


def restore_internal_config_with_retry(
    restore_actions: list[Callable[[], ResultObj]],
    fallback_action: Callable[[], ResultObj],
    scope_name: str,
    retries: int = 3,
    delay: int = 10,
    fallback_settle_time: int = 0,
) -> None:
    """Restore internal-encryption config items robustly during test teardown.

    When a test fails to restore its internal config, the certificate/CA stays
    bound to the configuration. A bound certificate cannot be deleted, so it
    lingers on the DUT and the next test fails while re-importing a certificate/CA
    with the same name (the leftover CA no longer matches the freshly generated
    certificate).

    Each restore action is retried independently. Restoring an already-default
    value is idempotent, so retrying is safe. If any item still fails after all
    retries, a last-resort full-node restore is attempted so the DUT is left in a
    clean, cert-free state for the next test. The teardown only fails when this
    fallback also fails, so a genuinely broken teardown is not hidden while a
    recoverable one no longer cascades into unrelated tests.

    Args:
        restore_actions: Ordered restore functions to run first. Encryption must
            come before certificate/ca-certificate so the certs get unbound. Each
            function returns a ResultObj.
        fallback_action: Callable that restores the whole internal node at once,
            used only when per-item restore keeps failing.
        scope_name: Human-readable scope for logs/allure (e.g. "system" or an app
            name).
        retries: Attempts per restore action.
        delay: Seconds to wait between attempts.
        fallback_settle_time: Seconds to wait before the fallback restore, used to
            let the system settle when a known bug is active. 0 disables the wait.
    """
    def _restore_once(action: Callable[[], ResultObj]) -> None:
        action().verify_result()

    with allure.step(f"restore {scope_name} internal config (with retry)"):
        failed_items: list[str] = []
        for action in restore_actions:
            try:
                retry_call(_restore_once, fargs=[action], tries=retries, delay=delay, logger=logger)
            except AssertionError as e:
                logger.warning("Failed to restore a %s internal item after %d tries: %s", scope_name, retries, e)
                failed_items.append(str(e))

        if not failed_items:
            return

        with allure.step(f"fallback: full {scope_name} internal node restore"):
            logger.warning(
                "Per-item restore of %s internal config failed (%s). Falling back to full node restore.",
                scope_name, "; ".join(failed_items),
            )
            if fallback_settle_time:
                logger.info(
                    "Waiting %ds for %s to settle before fallback restore", fallback_settle_time, scope_name,
                )
                time.sleep(fallback_settle_time)
            retry_call(_restore_once, fargs=[fallback_action], tries=retries, delay=delay, logger=logger)


def verify_system_internal_has_empty_defaults(dut_engine: LinuxSshEngine | None = None):
    """Verify system internal show has default empty values."""
    verify_system_internal_show(
        expect_cert=Defaults.CERT,
        expect_cacert=Defaults.CACERT,
        expect_alt_cert=Defaults.ALTERNATE_CERTIFICATE,
        expect_encryption=Defaults.ENCRYPTION,
        dut_engine=dut_engine,
    )


def verify_cluster_app_internal_has_empty_defaults(
    app_name: str, dut_engine: LinuxSshEngine | None = None,
):
    """Verify cluster app internal show has default empty values."""
    verify_cluster_app_internal_show(
        app_name,
        expect_cert=Defaults.CERT,
        expect_cacert=Defaults.CACERT,
        expect_encryption=Defaults.ENCRYPTION,
        expect_alt_cert=Defaults.ALTERNATE_CERTIFICATE,
        dut_engine=dut_engine,
    )


def build_internal_bridge_cert(  # noqa: PLR0913
    cert_name: str,
    cert_info: str,
    dut_hostname: str,
    dut_ip: str,
    cert_cn: str,
    spiffe_uri: str = "",
) -> CertInfo:
    """Build CertInfo for nv-bridge internal certificate generation."""
    san_uris = [spiffe_uri] if spiffe_uri else []
    return CertInfo(
        cert_name,
        cert_info,
        "",
        "",
        "",
        "",
        dut_hostname,
        dut_ip,
        "",
        cert_cn,
        san_uris,
    )


def generate_internal_certs(engines: EnginesT, location_name: str, hostname: str) -> tuple[CertInfo, CertInfo]:
    certs_location = get_test_certs_dir_location(location_name, hostname)
    scp_player = get_scp_player(engines)
    certs_location, certs = setup_certs_for_tests(
        certs_dirname_prefix=certs_location,
        certs_names=["client", "server"],
        engines=engines,
        dut_hostname=hostname,
        scp_player=scp_player,
        dut_ip=engines.dut.ip,
        create_chain=False,
    )
    client_cert = certs[0]
    server_cert = certs[-1]
    return server_cert, client_cert


def import_internal_certs(engines: EnginesT, bridge_certs: list[CertInfo], bridge_ca_certs: list[CertInfo]) -> None:
    scp_player = get_scp_player(engines)

    with allure.step("import test certs"):
        import_certs_safely(bridge_certs, scp_player)
        import_cas_safely(bridge_ca_certs, scp_player)


def reset_cluster_app(cluster, app_name: str, engine: LinuxSshEngine) -> None:
    """
    Reset a cluster app's internal connections.

    Args:
        cluster: Cluster object
        app_name: Name of the app to reset (e.g., nmx-controller)
        engine: Engine to connect to
    """
    with allure.step(f"Reset cluster app {app_name} internal connections"):
        full_app_name = normalize_cluster_app_name(app_name)
        app = cluster.apps.app_name[full_app_name]
        app.internal.connections.action_reset(dut_engine=engine).verify_result()
        wait_for_cluster_app_update(cluster, engine)


def rotate_system_internal_and_verify(  # noqa: PLR0913
    system,
    cluster,
    app_name: str,
    dut_engine: LinuxSshEngine,
    cacert_name: str,
    current_cert: str,
    current_alt: str,
    encryption_mode: str,
    should_reset: bool = False,
) -> tuple[str, str]:
    """
    Rotate system internal certificate and verify connection.

    Args:
        system: System object
        cluster: Cluster object
        app_name: Name of the cluster app
        dut_engine: SSH engine to DUT
        cacert_name: CA certificate name
        current_cert: Current certificate name (before rotation)
        current_alt: Current alternate certificate name (before rotation)
        encryption_mode: Expected encryption mode
        should_reset: If we want to reset connection

    Returns:
        Tuple of (new_cert, new_alt) after rotation (swapped)
    """
    with allure.step("Rotate system internal certificate"):
        system.internal.certificate.action_rotate(dut_engine=dut_engine).verify_result()
        new_cert, new_alt = current_alt, current_cert
        with allure.step("Verify system cert swapped"):
            verify_system_internal_show(
                expect_cert=new_cert,
                expect_cacert=cacert_name,
                expect_alt_cert=new_alt,
                expect_encryption=encryption_mode,
                dut_engine=dut_engine,
            )
        if should_reset:
            reset_cluster_app(cluster, app_name, dut_engine)
            wait_for_cluster_app_update(cluster, dut_engine)
        verify_nv_bridge_has_connection(dut_engine, expect_connection=True)
    return new_cert, new_alt


def rotate_cluster_internal_and_verify(  # noqa: PLR0913
    app,
    cluster,
    app_name: str,
    dut_engine: LinuxSshEngine,
    cacert_name: str,
    current_cert: str,
    current_alt: str,
    encryption_mode: str,
    should_reset: bool = False,
) -> tuple[str, str]:
    """
    Rotate cluster internal certificate and verify connection.

    Args:
        app: Cluster app object
        cluster: Cluster object
        app_name: Name of the cluster app
        dut_engine: SSH engine to DUT
        cacert_name: CA certificate name
        current_cert: Current certificate name (before rotation)
        current_alt: Current alternate certificate name (before rotation)
        encryption_mode: Expected encryption mode
        should_reset: If we want to reset connection

    Returns:
        Tuple of (new_cert, new_alt) after rotation (swapped)
    """
    with allure.step("Rotate cluster internal certificate"):
        app.internal.certificate.action_rotate(dut_engine=dut_engine).verify_result()
        new_cert, new_alt = current_alt, current_cert
        wait_for_cluster_app_update(cluster, dut_engine)
        with allure.step("Verify cluster cert swapped"):
            verify_cluster_app_internal_show(
                app_name,
                expect_cert=new_cert,
                expect_cacert=cacert_name,
                expect_alt_cert=new_alt,
                expect_encryption=encryption_mode,
                dut_engine=dut_engine,
            )
        if should_reset:
            reset_cluster_app(cluster, app_name, dut_engine)
            wait_for_cluster_app_update(cluster, dut_engine)
        verify_nv_bridge_has_connection(dut_engine, expect_connection=True)
    return new_cert, new_alt


def nv_bridge_internal_factory_reset_no_params_check():
    """
    Factory reset checker for NV Bridge internal encryption (no-params variant).

    Pre-reset:
        Configure system internal with cert + CA + mTLS and verify the config is present.
    Post-reset:
        Verify system internal has default empty values (everything wiped).

    This is a generator following the factory-reset checker protocol:
        yield once between setup and verification.
    """
    from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
    from ngts.tests_nvos.general.security.nmx_cert.constants import EncryptionMode

    engines = TestToolkit.engines
    system = System()

    with allure.step("setup NV Bridge system internal encryption"):
        server_cert, _ = generate_internal_certs(engines, "nv_bridge_factory_reset", engines.dut.ip)
        import_internal_certs(engines, [server_cert], [server_cert])

        system.internal.certificate.action_update(server_cert.name).verify_result()
        system.internal.ca_certificate.action_update(server_cert.cacert_name).verify_result()
        system.internal.encryption.action_update(EncryptionMode.MTLS).verify_result()

        verify_system_internal_show(
            expect_cert=server_cert.name,
            expect_cacert=server_cert.cacert_name,
            expect_encryption=EncryptionMode.MTLS,
        )

    yield  # factory reset happens here

    with allure.step("verify NV Bridge internal config wiped after factory reset"):
        verify_system_internal_has_empty_defaults()


BRIDGE_CROSS_FUNC_REBOOT_SETTLE_TIME = 60


def update_cluster_internal_encryption(
    cluster: Cluster,
    app_name: str,
    encryption_mode: str,
    engine: LinuxSshEngine,
) -> None:
    """Update cluster app internal encryption and wait for the app to settle."""
    app = cluster.apps.app_name[app_name]
    app.internal.encryption.action_update(encryption_mode, dut_engine=engine).verify_result()
    wait_for_cluster_app_update(cluster, engine)


def configure_bridge_internal_mtls(
    system: System,
    cluster: Cluster,
    app_name: str,
    bridge_cert: CertInfo,
    engine: LinuxSshEngine,
) -> None:
    """Bind system/cluster internal certs, enable mTLS, and reset the app connection."""
    app = cluster.apps.app_name[app_name]
    with allure.step("Configure system + cluster internal mTLS"):
        system.internal.certificate.action_update(bridge_cert.name).verify_result()
        system.internal.ca_certificate.action_update(bridge_cert.cacert_name).verify_result()
        app.internal.certificate.action_update(bridge_cert.name).verify_result()
        app.internal.ca_certificate.action_update(bridge_cert.cacert_name).verify_result()
        system.internal.encryption.action_update(EncryptionMode.MTLS).verify_result()
        update_cluster_internal_encryption(cluster, app_name, EncryptionMode.MTLS, engine)
        reset_cluster_app(cluster, app_name, engine)
    verify_system_internal_show(
        expect_cert=bridge_cert.name,
        expect_cacert=bridge_cert.cacert_name,
        expect_encryption=EncryptionMode.MTLS,
        dut_engine=engine,
    )
    verify_cluster_app_internal_show(
        app_name,
        expect_cert=bridge_cert.name,
        expect_cacert=bridge_cert.cacert_name,
        expect_encryption=EncryptionMode.MTLS,
        dut_engine=engine,
    )
