"""TLS certificate generation and DUT CA import for secured OTLP tests."""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
import tempfile
import time
from dataclasses import dataclass
from typing import Optional

import pytest

import ngts.tools.test_utils.allure_utils as allure
from devts.infra.tools.linux_tools.linux_tools import scp_file

from ngts.nvos_constants.constants_nvos import ConfState, TelemetryConsts
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.system.telemetry.otel.constants import OtelCollectorConst
from ngts.tests_nvos.system.telemetry.otel.cumulus.constants import CumulusOtelConst
from ngts.tests_nvos.system.telemetry.otel.otel_collector import OtelCollector

logger = logging.getLogger(__name__)

_CERTS_SCRIPT_DIR = os.path.join(os.path.dirname(__file__), "certs_gen_scripts")
_CA_IMPORT_RETRIES = 3
_CA_IMPORT_RETRY_DELAY_SEC = 2
_SCP_UPLOAD_RETRIES = 5


@dataclass(frozen=True)
class TlsCertMaterial:
    """Local runner paths plus remote collector paths after upload."""

    local_dir: str
    local_ca_crt: str
    local_ca_key: str
    local_server_crt: str
    local_server_key: str
    remote_cert_dir: str
    remote_server_crt: str
    remote_server_key: str


def _run_cert_script(script_name: str, cwd: str, *args: str) -> None:
    """Run an SSIM ``certs_gen_scripts`` helper (``gen_tls_certs_mgmt`` parity)."""
    script_path = os.path.join(_CERTS_SCRIPT_DIR, script_name)
    if not os.path.isfile(script_path):
        pytest.fail(f"TLS cert script missing: {script_path!r}")
    if not os.access(script_path, os.X_OK):
        os.chmod(script_path, 0o755)
    result = subprocess.run(
        [script_path, *args],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    stderr = (result.stderr or "").lower()
    sig_ok = ("signature ok" in stderr) or ("self-signature ok" in stderr)
    if not sig_ok and result.returncode != 0:
        pytest.fail(
            f"{script_name} failed (rc={result.returncode}): "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )


def generate_tls_certs_locally(
    *,
    san_ip: Optional[str] = None,
    san_dns: Optional[str] = None,
) -> TlsCertMaterial:
    """Generate CA + server cert on the pytest runner (SSIM ``gen_tls_certs_mgmt`` parity).

    SSIM ``OtelMgmtVrfWithTLSConfig.configure_topo_post_boot`` runs
    ``ca_cert_generate.sh`` and ``entity_server_cert_generate.sh`` locally, then
    ``copy_server_cert_and_key`` / ``copy_ca_cert``. MLX NGTS lab uses collector IP
    as SAN (``--target-subj-alt-name-ip-addr``); SSIM sim uses hydra FQDN DNS SAN.
    """
    if not san_ip and not san_dns:
        pytest.fail("generate_tls_certs_locally requires san_ip and/or san_dns")

    cert_dir = tempfile.mkdtemp(prefix="ngts-otel-tls-")
    server_args = ["--target-cert-file-name-prefix", "otelc"]
    if san_dns:
        server_args.extend(["--target-subj-alt-name-dns", san_dns])
    if san_ip:
        server_args.extend(["--target-subj-alt-name-ip-addr", san_ip])

    with allure.step("Generate TLS certs locally on test runner (SSIM gen_tls_certs_mgmt parity)"):
        _run_cert_script("ca_cert_generate.sh", cert_dir)
        _run_cert_script("entity_server_cert_generate.sh", cert_dir, *server_args)

    local_ca_crt = os.path.join(cert_dir, "ca.crt")
    local_ca_key = os.path.join(cert_dir, "ca.key")
    local_server_crt = os.path.join(cert_dir, "otelc.crt")
    local_server_key = os.path.join(cert_dir, "otelc.key")
    for path in (local_ca_crt, local_ca_key, local_server_crt, local_server_key):
        if not os.path.isfile(path):
            pytest.fail(f"Expected TLS artifact missing after generation: {path!r}")

    return TlsCertMaterial(
        local_dir=cert_dir,
        local_ca_crt=local_ca_crt,
        local_ca_key=local_ca_key,
        local_server_crt=local_server_crt,
        local_server_key=local_server_key,
        remote_cert_dir=CumulusOtelConst.OTEL_TLS_CERT_DIR,
        remote_server_crt=CumulusOtelConst.OTEL_TLS_SERVER_CRT,
        remote_server_key=CumulusOtelConst.OTEL_TLS_SERVER_KEY,
    )


def upload_server_certs_to_collector(collector: OtelCollector, material: TlsCertMaterial) -> None:
    """Upload server cert/key to the collector host (SSIM ``copy_server_cert_and_key`` parity)."""
    sudo = collector._sudo_prefix  # noqa: SLF001 — shared remote layout with OtelCollector
    cert_dir_q = shlex.quote(material.remote_cert_dir)
    server_crt_q = shlex.quote(material.remote_server_crt)
    server_key_q = shlex.quote(material.remote_server_key)

    with allure.step(f"Upload server TLS certs to collector ({collector.ip})"):
        collector.engine.run_cmd(
            f"bash -lc {shlex.quote(f'{sudo}rm -rf {cert_dir_q} && {sudo}mkdir -p {cert_dir_q}')}",
        )
        scp_file(
            collector.engine,
            material.local_server_crt,
            material.remote_server_crt,
            download_from_remote=False,
        )
        scp_file(
            collector.engine,
            material.local_server_key,
            material.remote_server_key,
            download_from_remote=False,
        )
        collector.engine.run_cmd(
            f"bash -lc {shlex.quote(f'{sudo}chmod 644 {server_crt_q} {server_key_q}')}",
        )
        for remote_path in (material.remote_server_crt, material.remote_server_key):
            if remote_path not in collector.engine.run_cmd(
                f"ls {shlex.quote(remote_path)} 2>&1",
                validate=False,
                print_output=False,
            ):
                pytest.fail(f"Failed to stage server TLS material on collector: {remote_path!r}")


def _upload_local_file_to_dut(dut, local_path: str, remote_path: str) -> None:
    """Upload a file to the DUT (SSIM ``device.put`` / NGTS ``copy_file`` parity)."""
    remote_dir = os.path.dirname(remote_path) or "/tmp"
    remote_name = os.path.basename(remote_path)
    last_err = None
    for attempt in range(1, _SCP_UPLOAD_RETRIES + 1):
        try:
            dut.copy_file(
                source_file=local_path,
                dest_file=remote_name,
                file_system=remote_dir,
                direction="put",
            )
        except Exception as exc:
            last_err = exc
            logger.warning(
                "SCP upload attempt %d/%d failed for %r: %s",
                attempt,
                _SCP_UPLOAD_RETRIES,
                local_path,
                exc,
            )
            time.sleep(_CA_IMPORT_RETRY_DELAY_SEC * attempt)
            continue
        if remote_name in dut.run_cmd(
            f"ls {shlex.quote(remote_path)} 2>&1",
            validate=False,
            print_output=False,
        ):
            return
        last_err = RuntimeError(f"remote verify failed for {remote_path!r}")
        logger.warning(
            "SCP verify attempt %d/%d failed for %r",
            attempt,
            _SCP_UPLOAD_RETRIES,
            remote_path,
        )
        time.sleep(_CA_IMPORT_RETRY_DELAY_SEC * attempt)
    pytest.fail(
        f"Failed to upload {local_path!r} to DUT {remote_path!r} "
        f"after {_SCP_UPLOAD_RETRIES} attempts: {last_err!r}"
    )


def _nvue_ca_certificate_present(dut, ca_name: str) -> bool:
    """True when NVUE still holds ``ca_name`` (CA store or on-disk nvue dir)."""
    nvue_ca_dir = "/usr/local/share/ca-certificates/nvue"
    listing = dut.run_cmd(
        f"ls -1 {shlex.quote(nvue_ca_dir)} 2>/dev/null",
        validate=False,
        print_output=False,
    )
    if f"{ca_name}.crt" in (listing or "").splitlines():
        return True
    show = dut.run_cmd(
        f"nv show system security ca-certificate {ca_name} -o json 2>&1",
        validate=False,
        print_output=False,
    )
    return not ("Error:" in show or "does not exist" in (show or "").lower())


def _delete_dut_ca_certificate_if_present(dut, ca_name: str) -> None:
    """Remove NVUE CA cert if present (SSIM ``OtelMgmtVrfWithTLSConfig`` pre-import delete).

    Retries: while a gnmi-server or grpc-tunnel still references the CA, ``nv action
    delete`` is a no-op and the next import fails with "already exists".
    """
    for attempt in range(1, _CA_IMPORT_RETRIES + 1):
        if not _nvue_ca_certificate_present(dut, ca_name):
            return
        logger.info("Deleting pre-existing NVUE CA certificate %r (try %d)", ca_name, attempt)
        dut.run_cmd(
            f"nv action delete system security ca-certificate {ca_name}",
            validate=False,
        )
        time.sleep(_CA_IMPORT_RETRY_DELAY_SEC)
        if not _nvue_ca_certificate_present(dut, ca_name):
            return
        dut.run_cmd(
            f"nv unset system security ca-certificate {ca_name} -y && nv config apply -y",
            validate=False,
            print_output=False,
        )
        time.sleep(_CA_IMPORT_RETRY_DELAY_SEC)


def _regenerate_ca_crt_on_dut(dut, *, ca_key_path: str, ca_crt_path: str) -> None:
    """Re-sign ``ca.crt`` on the DUT so NVUE validity uses DUT wall clock.

    Runner-local generation matches SSIM, but mlx-lab runners can be seconds ahead of
    the DUT. NVUE rejects a CA whose ``notBefore`` is still in the future on the switch.
    """
    key_q = shlex.quote(ca_key_path)
    crt_q = shlex.quote(ca_crt_path)
    with allure.step("Re-sign CA certificate on DUT (mlx lab clock-skew fix)"):
        script = f"""
set -euo pipefail
cat > /tmp/otel-ca-ext.cnf <<'CAEOF'
[v3_ca]
basicConstraints = CA:TRUE
CAEOF
openssl req -key {key_q} -new -sha256 -out /tmp/otel-ca.csr \\
  -subj "/C=US/ST=CA/L=Santa Clara/O=NVIDIA Corporation/OU=NBU/CN=ca"
openssl x509 -signkey {key_q} -in /tmp/otel-ca.csr -sha256 -req -days 365 \\
  -out {crt_q} -extensions v3_ca -extfile /tmp/otel-ca-ext.cnf
chmod 644 {crt_q}
rm -f /tmp/otel-ca.csr /tmp/otel-ca-ext.cnf
"""
        dut.run_cmd("bash -lc " + shlex.quote(script), validate=False)


def _log_staged_ca_validity_on_dut(dut, staged_path: str) -> None:
    """Log staged PEM and DUT clock for triage."""
    quoted = shlex.quote(staged_path)
    dates = dut.run_cmd(
        f"openssl x509 -in {quoted} -noout -dates 2>&1",
        validate=False,
        print_output=False,
    )
    dut_now = dut.run_cmd("date -u 2>&1", validate=False, print_output=False)
    logger.info("DUT staged CA dates: %s (dut_now=%s)", dates.strip(), dut_now.strip())


def _import_ca_certificate_on_dut(dut, ca_name: str, staged_path: str) -> str:
    """SSIM ``OtelMgmtVrfWithTLSConfig.configure_topo_post_boot`` import with retry."""
    import_cmd = (
        f"nv action import system security ca-certificate {ca_name} "
        f"uri file://127.0.0.1{staged_path}"
    )
    import_out = ""
    for attempt in range(1, _CA_IMPORT_RETRIES + 1):
        import_out = dut.run_cmd(import_cmd, validate=False, print_output=False)
        if "Succeeded in importing" in import_out:
            break
        logger.warning("CA import attempt %d/%d did not succeed yet", attempt, _CA_IMPORT_RETRIES)
        if "already exists" in (import_out or "").lower():
            _delete_dut_ca_certificate_if_present(dut, ca_name)
        time.sleep(_CA_IMPORT_RETRY_DELAY_SEC)
    return import_out


def _assert_ca_import_succeeded(import_out: str, ca_name: str) -> None:
    """Require NVUE import success; expiry warnings fail (``nv config apply`` rejects them)."""
    if "Succeeded in importing" not in import_out or ca_name not in import_out:
        pytest.fail(f"CA import failed: {import_out.strip()!r}")
    if "are expired" in import_out.lower():
        pytest.fail(
            f"NVUE reported expired CA certificate(s) during import of {ca_name!r}: "
            f"{import_out.strip()!r}"
        )


def _assert_nvue_ca_active_on_dut(dut, ca_name: str) -> None:
    """Confirm NVUE CA store shows current validity before OTLP TLS ``nv config apply``."""
    show = dut.run_cmd(
        f"nv show system security ca-certificate {ca_name} -o json 2>&1",
        validate=False,
        print_output=False,
    )
    if "Error:" in show or "does not exist" in show.lower():
        pytest.fail(f"NVUE CA {ca_name!r} missing after import: {show.strip()!r}")
    parsed = OutputParsingTool.parse_json_str_to_dictionary(show).get_returned_value()
    if not isinstance(parsed, dict):
        pytest.fail(f"Could not parse NVUE CA show JSON: {show.strip()!r}")
    valid_to = parsed.get("valid-to")
    valid_from = parsed.get("valid-from")
    if not valid_to:
        pytest.fail(f"NVUE CA {ca_name!r} show missing valid-to: {parsed!r}")
    now_epoch = dut.run_cmd("date +%s 2>&1", validate=False, print_output=False).strip()
    valid_to_epoch = dut.run_cmd(
        f"date -d {shlex.quote(str(valid_to))} +%s 2>&1",
        validate=False,
        print_output=False,
    ).strip()
    if not valid_to_epoch.isdigit() or not now_epoch.isdigit() or int(valid_to_epoch) <= int(now_epoch):
        pytest.fail(
            f"NVUE CA {ca_name!r} is expired "
            f"(valid-from={valid_from!r}, valid-to={valid_to!r}, dut_now_epoch={now_epoch!r})"
        )
    logger.info(
        "NVUE CA %r active on DUT (valid-from=%s, valid-to=%s)",
        ca_name,
        valid_from,
        valid_to,
    )


def install_ca_on_dut(
    dut,
    material: TlsCertMaterial,
    *,
    ca_name: str = CumulusOtelConst.OTEL_TLS_CA_NAME,
) -> None:
    """Stage CA on the DUT and import via ``nv action import system security ca-certificate``.

    SSIM ``copy_ca_cert`` + ``OtelMgmtVrfWithTLSConfig.configure_topo_post_boot`` flow,
    plus a mlx-lab DUT re-sign so ``notBefore`` matches DUT wall clock.
    """
    staged = CumulusOtelConst.OTEL_TLS_DUT_CA_STAGING
    staged_key = CumulusOtelConst.OTEL_TLS_DUT_CA_KEY_STAGING
    from ngts.tests_nvos.system.telemetry.otel.cumulus.data_collection import (
        cleanup_stale_nvue_censor_files,
        settle_dut_ssh_after_module_transition,
    )

    with allure.step(f"Import CA certificate {ca_name!r} on DUT"):
        cleanup_stale_nvue_censor_files(dut)
        settle_dut_ssh_after_module_transition(dut, settle_sec=3)
        _delete_dut_ca_certificate_if_present(dut, ca_name)
        dut.run_cmd(f"sudo rm -f {shlex.quote(staged)} {shlex.quote(staged_key)}", validate=False)
        _upload_local_file_to_dut(dut, material.local_ca_key, staged_key)
        dut.run_cmd(f"sudo chmod 600 {shlex.quote(staged_key)}", validate=False)
        _regenerate_ca_crt_on_dut(dut, ca_key_path=staged_key, ca_crt_path=staged)
        dut.run_cmd(f"sudo rm -f {shlex.quote(staged_key)}", validate=False)
        _log_staged_ca_validity_on_dut(dut, staged)
        import_out = _import_ca_certificate_on_dut(dut, ca_name, staged)
        _assert_ca_import_succeeded(import_out, ca_name)
        _assert_nvue_ca_active_on_dut(dut, ca_name)


def configure_collector_tls(
    collector: OtelCollector,
    material: TlsCertMaterial,
    *,
    max_megabytes: int = OtelCollectorConst.PRIMARY_FILE_EXPORT_MAX_MB,
    install_if_missing: bool = True,
) -> None:
    """Write TLS collector YAML and restart the collector process."""
    from ngts.tests_nvos.system.telemetry.otel.constants import _otel_collector_config_yaml_tls

    collector.config_yaml = _otel_collector_config_yaml_tls(
        OtelCollectorConst.OTLP_GRPC_PORT,
        collector.output_json_path,
        max_megabytes,
        1,
        cert_file=material.remote_server_crt,
        key_file=material.remote_server_key,
        bind_addr=OtelCollectorConst.OTLP_GRPC_BIND_ADDR,
    )
    collector.ensure_running(install_if_missing=install_if_missing)


def _certificate_from_show(doc: dict) -> Optional[str]:
    """Return certificate id from NVUE show JSON (``certificate`` or legacy ``cert-id``)."""
    if not isinstance(doc, dict):
        return None
    cert = doc.get(TelemetryConsts.CERTIFICATE) or doc.get("cert-id")
    return str(cert).strip() if cert else None


def assert_otlp_grpc_certificate_applied(
    dut,
    *,
    destination_id: Optional[str] = None,
    expected: str = CumulusOtelConst.OTEL_TLS_CA_NAME,
) -> None:
    """Assert OTLP TLS ``certificate`` matches ``expected`` (SSIM cert-id check parity).

    SSIM ``Test_Otel_Default_Vrf_Secured`` reads ``get_system_telemetry_export_otlp_grpc()``
    (gRPC-level ``cert-id``). SSIM ``Test_Otel_Mgmt_Vrf_Secured`` reads
    ``get_system_telemetry_export_otlp_grpc_destination(destination_id=…)`` only.

    Cumulus lab NVUE rejects ``nv show … --operational`` on these OTLP resources; use
    ``--applied`` (same effective config after ``nv config apply`` in suite setup).
    """
    system = System()
    grpc = system.telemetry.export.otlp.grpc
    show_rev = ConfState.APPLIED

    with allure.step("Verify OTLP TLS certificate is applied"):
        if destination_id:
            if destination_id not in grpc.destination.resources_dict:
                grpc.destination.set_resource(destination_id).verify_result()
            dest_raw = grpc.destination.resources_dict[destination_id].show(
                dut_engine=dut, rev=show_rev
            )
            dest_parsed = OutputParsingTool.parse_json_str_to_dictionary(dest_raw).get_returned_value()
            dest_cert = _certificate_from_show(dest_parsed if isinstance(dest_parsed, dict) else {})
            if dest_cert != expected:
                pytest.fail(
                    f"destination {destination_id!r} certificate={dest_cert!r}, expected {expected!r}"
                )
            logger.info("destination %s certificate=%s verified", destination_id, dest_cert)
        else:
            raw = grpc.show(dut_engine=dut, rev=show_rev)
            parsed = OutputParsingTool.parse_json_str_to_dictionary(raw).get_returned_value()
            grpc_cert = _certificate_from_show(parsed if isinstance(parsed, dict) else {})
            if grpc_cert != expected:
                pytest.fail(f"OTLP gRPC certificate={grpc_cert!r}, expected {expected!r}")
            logger.info("OTLP gRPC certificate=%s verified", grpc_cert)
