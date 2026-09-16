"""mTLS certificate and NVUE setup for gNMI dial-out coexistence (SSIM pre_suite parity)."""

from __future__ import annotations

import logging
import os
import shlex
import tempfile
import time
from dataclasses import dataclass
from typing import Optional, Tuple

import pytest

import ngts.tools.test_utils.allure_utils as allure
from devts.infra.tools.linux_tools.linux_tools import scp_file
from ngts.constants.constants import GnmiConsts
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.general.security.mtls.generic_testing.constants import CA_CERTIFICATE
from ngts.tests_nvos.system.gnmi.constants import CERTIFICATE
from ngts.tests_nvos.system.grpc_tunnel.constants import GrpcTunnelConstants
from ngts.tests_nvos.system.telemetry.otel.cumulus.tls import (
    TlsCertMaterial,
    _assert_nvue_ca_active_on_dut,
    _delete_dut_ca_certificate_if_present,
    _import_ca_certificate_on_dut,
    _log_staged_ca_validity_on_dut,
    _run_cert_script,
    _upload_local_file_to_dut,
    install_ca_on_dut,
)

logger = logging.getLogger(__name__)

_CERTS_SCRIPT_DIR = os.path.join(os.path.dirname(__file__), 'certs_gen_scripts')

GNMI_DIALOUT_CA_NAME = 'gnmic_ca'
GNMI_DIALOUT_CLIENT_CERT = 'dut_client'
GNMI_DIALOUT_GNMIC_CLIENT_CERT = 'gnmic_client'
GNMI_DIALOUT_SERVER_CERT = 'gnmi'
GNMI_DIALOUT_REMOTE_CERT_DIR = '/tmp/ngts_gnmi_dialout_mtls'
# Match GrpcTunnelServer.DEFAULT_TARGET_WAIT; 45s is too short for DUT docker dial-out startup.
GNMI_DIALOUT_TARGET_WAIT = '300s'
GNMI_DIALOUT_RETRY_INTERVAL = 45
GNMI_DIALOUT_TARGET_NAME = ':9339'

_CERT_IMPORT_RETRIES = 3
_CERT_IMPORT_RETRY_DELAY_SEC = 2
_COEXISTENCE_TUNNEL_NAMES = (
    'coexistence_interface',
    'coexistence_qos_prefix',
    'coexistence_interface_onchange',
    'coexistence_system',
    'coexistence_component',
    'coexistence_component_cpu',
    'coexistence_component_psu',
    'coexistence_lldp',
    'coexistence_bgp',
    # Dedicated smoke top-level tunnels (kept distinct so the module-scoped smoke session that
    # defers cleanup does not collide with the Interface quality-of-service and Component/System collections).
    'coexistence_smoke_interface_top',
    'coexistence_smoke_system_top',
    'coexistence_smoke_component_top',
)


@dataclass(frozen=True)
class GnmiDialoutMtlsMaterial:
    """Runner-local PEM paths and sonic-mgmt remote paths after upload."""

    local_dir: str
    local_gnmic_ca_crt: str
    local_gnmic_ca_key: str
    local_gnmic_target_crt: str
    local_gnmic_target_key: str
    local_dut_ca_crt: str
    local_dut_ca_key: str
    local_dut_client_crt: str
    local_dut_client_key: str
    local_gnmic_client_crt: str
    local_gnmic_client_key: str
    local_gnmi_server_crt: str
    local_gnmi_server_key: str
    remote_cert_dir: str
    remote_gnmic_target_crt: str
    remote_gnmic_target_key: str
    remote_dut_ca_crt: str
    remote_gnmic_client_crt: str
    remote_gnmic_client_key: str


def _assert_local_file(path: str) -> None:
    if not os.path.isfile(path):
        pytest.fail('Expected TLS artifact missing after generation: %r' % (path,))


def generate_gnmi_dialout_mtls_material(
    *,
    collector_ip: str,
    dut_ip: str,
) -> GnmiDialoutMtlsMaterial:
    """Generate SSIM-style dial-out mTLS material locally (``gen_tls_certs_*`` parity)."""
    if not collector_ip or not dut_ip:
        pytest.fail('generate_gnmi_dialout_mtls_material requires collector_ip and dut_ip')

    cert_dir = tempfile.mkdtemp(prefix='ngts-gnmi-dialout-mtls-')
    with allure.step('Generate gNMI dial-out mTLS certs locally (SSIM gen_tls_certs parity)'):
        _run_cert_script(
            'ca_cert_generate.sh',
            cert_dir,
            '-ca-cert-file-pref',
            'gnmic_ca',
        )
        _run_cert_script(
            'entity_server_cert_generate.sh',
            cert_dir,
            '-ca-cert-file-pref',
            'gnmic_ca',
            '--target-subj-alt-name-ip-addr',
            collector_ip,
            '--target-cert-file-name-prefix',
            'gnmic_target',
        )
        _run_cert_script(
            'entity_client_cert_generate.sh',
            cert_dir,
            '-ca-cert-file-pref',
            'gnmic_ca',
            '-client-cert-file-pref',
            GNMI_DIALOUT_GNMIC_CLIENT_CERT,
            '-client-san-ip',
            collector_ip,
        )
        _run_cert_script(
            'ca_cert_generate.sh',
            cert_dir,
            '-ca-cert-file-pref',
            'dut_ca',
        )
        _run_cert_script(
            'entity_server_cert_generate.sh',
            cert_dir,
            '-ca-cert-file-pref',
            'dut_ca',
            '--target-subj-alt-name-ip-addr',
            dut_ip,
            '--target-cert-file-name-prefix',
            GNMI_DIALOUT_SERVER_CERT,
        )
        _run_cert_script(
            'entity_client_cert_generate.sh',
            cert_dir,
            '-ca-cert-file-pref',
            'dut_ca',
            '-client-cert-file-pref',
            GNMI_DIALOUT_CLIENT_CERT,
            '-client-san-ip',
            dut_ip,
        )

    local_gnmic_ca_crt = os.path.join(cert_dir, 'gnmic_ca.crt')
    local_gnmic_ca_key = os.path.join(cert_dir, 'gnmic_ca.key')
    local_gnmic_target_crt = os.path.join(cert_dir, 'gnmic_target.crt')
    local_gnmic_target_key = os.path.join(cert_dir, 'gnmic_target.key')
    local_dut_ca_crt = os.path.join(cert_dir, 'dut_ca.crt')
    local_dut_ca_key = os.path.join(cert_dir, 'dut_ca.key')
    local_dut_client_crt = os.path.join(cert_dir, '%s.crt' % (GNMI_DIALOUT_CLIENT_CERT,))
    local_dut_client_key = os.path.join(cert_dir, '%s.key' % (GNMI_DIALOUT_CLIENT_CERT,))
    local_gnmic_client_crt = os.path.join(cert_dir, '%s.crt' % (GNMI_DIALOUT_GNMIC_CLIENT_CERT,))
    local_gnmic_client_key = os.path.join(cert_dir, '%s.key' % (GNMI_DIALOUT_GNMIC_CLIENT_CERT,))
    local_gnmi_server_crt = os.path.join(cert_dir, '%s.crt' % (GNMI_DIALOUT_SERVER_CERT,))
    local_gnmi_server_key = os.path.join(cert_dir, '%s.key' % (GNMI_DIALOUT_SERVER_CERT,))
    for path in (
        local_gnmic_ca_crt,
        local_gnmic_ca_key,
        local_gnmic_target_crt,
        local_gnmic_target_key,
        local_dut_ca_crt,
        local_dut_ca_key,
        local_dut_client_crt,
        local_dut_client_key,
        local_gnmic_client_crt,
        local_gnmic_client_key,
        local_gnmi_server_crt,
        local_gnmi_server_key,
    ):
        _assert_local_file(path)

    remote_cert_dir = GNMI_DIALOUT_REMOTE_CERT_DIR
    return GnmiDialoutMtlsMaterial(
        local_dir=cert_dir,
        local_gnmic_ca_crt=local_gnmic_ca_crt,
        local_gnmic_ca_key=local_gnmic_ca_key,
        local_gnmic_target_crt=local_gnmic_target_crt,
        local_gnmic_target_key=local_gnmic_target_key,
        local_dut_ca_crt=local_dut_ca_crt,
        local_dut_ca_key=local_dut_ca_key,
        local_dut_client_crt=local_dut_client_crt,
        local_dut_client_key=local_dut_client_key,
        local_gnmic_client_crt=local_gnmic_client_crt,
        local_gnmic_client_key=local_gnmic_client_key,
        local_gnmi_server_crt=local_gnmi_server_crt,
        local_gnmi_server_key=local_gnmi_server_key,
        remote_cert_dir=remote_cert_dir,
        remote_gnmic_target_crt='%s/gnmic_target.crt' % (remote_cert_dir,),
        remote_gnmic_target_key='%s/gnmic_target.key' % (remote_cert_dir,),
        remote_dut_ca_crt='%s/dut_ca.crt' % (remote_cert_dir,),
        remote_gnmic_client_crt='%s/gnmic_client.crt' % (remote_cert_dir,),
        remote_gnmic_client_key='%s/gnmic_client.key' % (remote_cert_dir,),
    )


def upload_collector_mtls_certs(collector_engine, material: GnmiDialoutMtlsMaterial) -> None:
    """Stage gnmic tunnel-server TLS material on sonic-mgmt (SSIM ``gnmic_*.yaml`` cert paths)."""
    cert_dir_q = shlex.quote(material.remote_cert_dir)
    with allure.step('Upload gNMI dial-out mTLS certs to sonic-mgmt (%s)' % (getattr(collector_engine, 'ip', 'collector'),)):
        collector_engine.run_cmd(
            'bash -lc %s'
            % (shlex.quote('sudo rm -rf %s && sudo mkdir -p %s' % (cert_dir_q, cert_dir_q)),),
        )
        uploads = (
            (material.local_gnmic_target_crt, material.remote_gnmic_target_crt),
            (material.local_gnmic_target_key, material.remote_gnmic_target_key),
            (material.local_dut_ca_crt, material.remote_dut_ca_crt),
            (material.local_gnmic_client_crt, material.remote_gnmic_client_crt),
            (material.local_gnmic_client_key, material.remote_gnmic_client_key),
        )
        for local_path, remote_path in uploads:
            scp_file(collector_engine, local_path, remote_path, download_from_remote=False)
        for remote_path in (
            material.remote_gnmic_target_crt,
            material.remote_gnmic_target_key,
            material.remote_dut_ca_crt,
            material.remote_gnmic_client_crt,
            material.remote_gnmic_client_key,
        ):
            if remote_path not in collector_engine.run_cmd(
                'ls %s 2>&1' % (shlex.quote(remote_path),),
                validate=False,
                print_output=False,
            ):
                pytest.fail('Failed to stage collector TLS material: %r' % (remote_path,))


def _dut_staging_paths(dut, cert_name: str) -> Tuple[str, str]:
    """Stage PEMs under the DUT user home dir (mlx ``file://`` import is unreliable)."""
    username = getattr(dut, 'username', None) or 'cumulus'
    home = '/home/%s' % (username,)
    return '%s/ngts_%s.crt' % (home, cert_name), '%s/ngts_%s.key' % (home, cert_name)


def _resign_entity_cert_on_dut(
    dut,
    *,
    staged_crt: str,
    staged_key: str,
    ca_crt_local: str,
    ca_key_local: str,
    san_ip: str,
    client_cert: bool,
) -> None:
    """Re-sign entity PEM on the DUT so ``notBefore`` matches DUT wall clock (``tls.py`` CA parity)."""
    staged_ca_crt = '/tmp/ngts_dut_ca_resign.crt'
    staged_ca_key = '/tmp/ngts_dut_ca_resign.key'
    eku = 'clientAuth' if client_cert else 'serverAuth'
    crt_q = shlex.quote(staged_crt)
    key_q = shlex.quote(staged_key)
    ca_crt_q = shlex.quote(staged_ca_crt)
    ca_key_q = shlex.quote(staged_ca_key)
    with allure.step('Re-sign entity certificate on DUT (mlx lab clock-skew fix)'):
        dut.run_cmd(
            'rm -f %s %s %s'
            % (crt_q, shlex.quote(staged_ca_crt), shlex.quote(staged_ca_key)),
            validate=False,
        )
        _upload_local_file_to_dut(dut, ca_key_local, staged_ca_key)
        _upload_local_file_to_dut(dut, ca_crt_local, staged_ca_crt)
        dut.run_cmd('chmod 600 %s' % (ca_key_q,), validate=False)
        dut.run_cmd('chmod 644 %s' % (ca_crt_q,), validate=False)
        script = (
            'set -euo pipefail\n'
            'cat > /tmp/ngts_entity_ext.cnf <<\'EOF\'\n'
            '[v3_ca]\n'
            'basicConstraints = CA:FALSE\n'
            'keyUsage = digitalSignature, keyEncipherment\n'
            'extendedKeyUsage = critical, %s\n'
            'subjectAltName = IP:%s\n'
            'EOF\n'
            'openssl req -key %s -new -sha256 -out /tmp/ngts_entity.csr '
            '-subj "/C=US/ST=CA/L=Santa Clara/O=NVIDIA Corporation/OU=NBU/CN=%s"\n'
            'openssl x509 -req -in /tmp/ngts_entity.csr -sha256 -days 365 '
            '-CA %s -CAkey %s -CAcreateserial '
            '-out %s -extensions v3_ca -extfile /tmp/ngts_entity_ext.cnf\n'
            'chmod 644 %s\n'
            'rm -f /tmp/ngts_entity.csr /tmp/ngts_entity_ext.cnf /tmp/ngts_dut_ca.srl %s %s\n'
        ) % (eku, san_ip, key_q, san_ip, ca_crt_q, ca_key_q, crt_q, crt_q, ca_crt_q, ca_key_q)
        dut.run_cmd('bash -lc ' + shlex.quote(script), validate=False)
        if staged_crt not in dut.run_cmd(
            'ls %s 2>&1' % (crt_q,),
            validate=False,
            print_output=False,
        ):
            pytest.fail('Entity cert re-sign failed; missing %r on DUT' % (staged_crt,))


def _import_dut_entity_certificate(
    dut,
    cert_name: str,
    *,
    local_key: str,
    ca_crt_local: str,
    ca_key_local: str,
    san_ip: str,
    client_cert: bool = False,
) -> None:
    """Import NVUE entity certificate via localhost SCP (SSIM / ``enable_gnmi_server_with_cert`` parity).

    mlx NVUE rejects ``file://`` for entity cert private keys; ``scp://user:pass@127.0.0.1:/path`` works.
    Entity certs are re-signed on the DUT first so NVUE validity matches switch wall clock.
    """
    staged_crt, staged_key = _dut_staging_paths(dut, cert_name)
    username = getattr(dut, 'username', None) or 'cumulus'
    password = getattr(dut, 'password', None) or ''
    with allure.step('Import DUT certificate %r' % (cert_name,)):
        dut.run_cmd(
            'rm -f %s %s' % (shlex.quote(staged_crt), shlex.quote(staged_key)),
            validate=False,
        )
        dut.run_cmd(
            'sudo nv action delete system security certificate %s' % (cert_name,),
            validate=False,
        )
        _upload_local_file_to_dut(dut, local_key, staged_key)
        dut.run_cmd('chmod 600 %s' % (shlex.quote(staged_key),), validate=False)
        _resign_entity_cert_on_dut(
            dut,
            staged_crt=staged_crt,
            staged_key=staged_key,
            ca_crt_local=ca_crt_local,
            ca_key_local=ca_key_local,
            san_ip=san_ip,
            client_cert=client_cert,
        )
        dut.run_cmd('chmod 644 %s' % (shlex.quote(staged_crt),), validate=False)
        import_cmd = (
            'sudo nv action import system security certificate %s '
            "uri-public-key 'scp://%s:%s@127.0.0.1:%s' "
            "uri-private-key 'scp://%s:%s@127.0.0.1:%s'"
            % (cert_name, username, password, staged_crt, username, password, staged_key)
        )
        import_out = ''
        for attempt in range(1, _CERT_IMPORT_RETRIES + 1):
            import_out = dut.run_cmd(import_cmd, validate=False, print_output=False)
            if 'Succeeded in importing' in import_out:
                break
            logger.warning('certificate import attempt %d/%d did not succeed yet', attempt, _CERT_IMPORT_RETRIES)
            time.sleep(_CERT_IMPORT_RETRY_DELAY_SEC)
        if 'Succeeded in importing' not in import_out:
            pytest.fail('Certificate import failed for %r: %r' % (cert_name, import_out.strip()))
        dut.run_cmd(
            'rm -f %s %s' % (shlex.quote(staged_crt), shlex.quote(staged_key)),
            validate=False,
        )


def _release_stale_gnmi_dialout_security(dut) -> None:
    """Drop prior-run grpc-tunnel/gnmi-server refs so CA/certs can be replaced.

    A failed prior run can leave ``gnmi-server`` mTLS bound to ``gnmic_ca``, which blocks
    ``nv action delete system security ca-certificate gnmic_ca`` and makes re-import fail
    with "already exists".
    """
    system = System()
    with allure.step('Release stale gNMI dial-out security bindings on DUT'):
        for tunnel_name in _COEXISTENCE_TUNNEL_NAMES:
            dut.run_cmd(
                'sudo systemctl stop nv-grpctunneld@%s' % (tunnel_name,),
                validate=False,
            )
            try:
                system.grpc_tunnel.server.tunnel_name[tunnel_name].unset(apply=False)
            except Exception as exc:
                logger.debug('grpc-tunnel unset %s: %s', tunnel_name, exc)
        try:
            system.gnmi_server.mtls.unset(apply=False)
        except Exception as exc:
            logger.debug('gnmi-server mtls unset: %s', exc)
        try:
            system.gnmi_server.unset(
                apply=True, ask_for_confirmation=True, dut_engine=dut
            ).verify_result()
        except Exception as exc:
            logger.debug('gnmi-server unset: %s', exc)
            dut.run_cmd('nv unset system gnmi-server -y', validate=False)
            dut.run_cmd('nv config apply -y', validate=False)
        try:
            show = system.gnmi_server.parse_show(dut_engine=dut) or {}
        except Exception as exc:
            logger.debug('gnmi-server show after unset: %s', exc)
            show = {}
        if not isinstance(show, dict):
            show = {}
        mtls = show.get('mtls') if isinstance(show.get('mtls'), dict) else {}
        if mtls.get(CA_CERTIFICATE) == GNMI_DIALOUT_CA_NAME or show.get(
            CERTIFICATE
        ) == GNMI_DIALOUT_SERVER_CERT:
            logger.warning(
                'gnmi-server still bound after unset (leaks into the next module): %s',
                str(show)[:400],
            )
        time.sleep(_CERT_IMPORT_RETRY_DELAY_SEC)
        for cert_name in (GNMI_DIALOUT_CLIENT_CERT, GNMI_DIALOUT_SERVER_CERT):
            dut.run_cmd(
                'sudo nv action delete system security certificate %s' % (cert_name,),
                validate=False,
            )
        for attempt in range(1, _CERT_IMPORT_RETRIES + 1):
            delete_out = dut.run_cmd(
                'nv action delete system security ca-certificate %s' % (GNMI_DIALOUT_CA_NAME,),
                validate=False,
                print_output=False,
            )
            if 'Succeeded in deleting' in delete_out:
                break
            if 'does not exist' in delete_out.lower():
                break
            logger.warning(
                'gnmic_ca delete attempt %d/%d: %s',
                attempt,
                _CERT_IMPORT_RETRIES,
                delete_out.strip()[:200],
            )
            time.sleep(_CERT_IMPORT_RETRY_DELAY_SEC)


def _install_gnmic_ca_on_dut(dut, material: GnmiDialoutMtlsMaterial) -> None:
    """Import ``gnmic_ca`` on the DUT (SSIM ``copy_certs_between_client_dut`` direct-import parity).

    SSIM imports the collector-generated ``gnmic_ca.crt`` as-is. mlx-lab clock skew may reject a
    runner-generated CA whose ``notBefore`` is still in the future on the switch; fall back to
    :func:`install_ca_on_dut` re-sign on the DUT only when direct import fails.
    """
    from ngts.tests_nvos.system.telemetry.otel.cumulus.data_collection import (
        cleanup_stale_nvue_censor_files,
    )

    staged = '/tmp/ngts_gnmic_ca.crt'
    with allure.step("Import CA certificate %r on DUT (SSIM direct import)" % (GNMI_DIALOUT_CA_NAME,)):
        cleanup_stale_nvue_censor_files(dut)
        _delete_dut_ca_certificate_if_present(dut, GNMI_DIALOUT_CA_NAME)
        dut.run_cmd('sudo rm -f %s' % (shlex.quote(staged),), validate=False)
        _upload_local_file_to_dut(dut, material.local_gnmic_ca_crt, staged)
        _log_staged_ca_validity_on_dut(dut, staged)
        import_out = _import_ca_certificate_on_dut(dut, GNMI_DIALOUT_CA_NAME, staged)
        if (
            'Succeeded in importing' in import_out and
            GNMI_DIALOUT_CA_NAME in import_out and
            'are expired' not in import_out.lower()
        ):
            _assert_nvue_ca_active_on_dut(dut, GNMI_DIALOUT_CA_NAME)
            return
        if 'Succeeded in importing' in import_out:
            logger.warning(
                'Direct gnmic_ca import reported NVUE expiry warning; '
                're-signing on DUT (mlx clock-skew fix). nv_out=%r',
                import_out.strip()[:300],
            )
            _delete_dut_ca_certificate_if_present(dut, GNMI_DIALOUT_CA_NAME)
        else:
            logger.warning(
                'Direct gnmic_ca import failed (%r); falling back to DUT re-sign (mlx clock-skew fix)',
                import_out.strip()[:200],
            )
    with allure.step("Re-sign and import CA certificate %r on DUT (mlx clock-skew fix)" % (GNMI_DIALOUT_CA_NAME,)):
        install_ca_on_dut(
            dut,
            TlsCertMaterial(
                local_dir=material.local_dir,
                local_ca_crt=material.local_gnmic_ca_crt,
                local_ca_key=material.local_gnmic_ca_key,
                local_server_crt=material.local_gnmi_server_crt,
                local_server_key=material.local_gnmi_server_key,
                remote_cert_dir=material.remote_cert_dir,
                remote_server_crt=material.remote_gnmic_target_crt,
                remote_server_key=material.remote_gnmic_target_key,
            ),
            ca_name=GNMI_DIALOUT_CA_NAME,
        )


def install_gnmi_dialout_certs_on_dut(
    dut,
    material: GnmiDialoutMtlsMaterial,
    *,
    dut_ip: str,
) -> None:
    """Exchange CAs and import DUT client + gNMI server certs (SSIM ``copy_certs_between_client_dut`` parity)."""
    from ngts.tests_nvos.system.telemetry.otel.cumulus.data_collection import (
        cleanup_stale_nvue_censor_files,
    )

    with allure.step('Install gNMI dial-out mTLS certificates on DUT'):
        cleanup_stale_nvue_censor_files(dut)
        _release_stale_gnmi_dialout_security(dut)
        _install_gnmic_ca_on_dut(dut, material)
        _import_dut_entity_certificate(
            dut,
            GNMI_DIALOUT_CLIENT_CERT,
            local_key=material.local_dut_client_key,
            ca_crt_local=material.local_dut_ca_crt,
            ca_key_local=material.local_dut_ca_key,
            san_ip=dut_ip,
            client_cert=True,
        )
        _import_dut_entity_certificate(
            dut,
            GNMI_DIALOUT_SERVER_CERT,
            local_key=material.local_gnmi_server_key,
            ca_crt_local=material.local_dut_ca_crt,
            ca_key_local=material.local_dut_ca_key,
            san_ip=dut_ip,
            client_cert=False,
        )
        cleanup_stale_nvue_censor_files(dut)


def configure_dut_gnmi_server_mtls(dut, *, dut_ip: str) -> None:
    """Enable local gNMI target for grpc-tunnel (SSIM ``configure_gnmic_mtls`` hydra=False parity)."""
    system = System()
    with allure.step('Configure gnmi-server mTLS on DUT (SSIM configure_gnmic_mtls parity)'):
        system.gnmi_server.set(GnmiConsts.GNMI_STATE_FIELD, GnmiConsts.GNMI_STATE_ENABLED, apply=False)
        system.gnmi_server.set('listening-address', dut_ip, apply=False)
        system.gnmi_server.set(CERTIFICATE, GNMI_DIALOUT_SERVER_CERT, apply=False)
        system.gnmi_server.mtls.set(CA_CERTIFICATE, GNMI_DIALOUT_CA_NAME, apply=True, ask_for_confirmation=True,
                                    dut_engine=dut,).verify_result()
        logger.info('gnmi-server enabled with certificate=%s mtls ca=%s', GNMI_DIALOUT_SERVER_CERT, GNMI_DIALOUT_CA_NAME)


def configure_dut_grpc_tunnel_mtls(
    dut,
    collector_ip: str,
    tunnel_name: str,
    listen_port: int,
    *,
    retry_interval: int = GNMI_DIALOUT_RETRY_INTERVAL,
) -> None:
    """Full NVUE grpc-tunnel server config (SSIM ``configure_grpc_tunnel_vx`` parity)."""
    with allure.step('Configure grpc-tunnel server %s with mTLS' % (tunnel_name,)):
        system = System()
        tunnel = system.grpc_tunnel.server.set_new_tunnel(tunnel_name=tunnel_name)
        tunnel.set(op_param_name=GrpcTunnelConstants.ADDRESS, op_param_value=collector_ip)
        tunnel.set(op_param_name=GrpcTunnelConstants.PORT, op_param_value=str(listen_port))
        tunnel.set(op_param_name=GrpcTunnelConstants.TARGET_NAME, op_param_value=GNMI_DIALOUT_TARGET_NAME)
        tunnel.set(op_param_name=GrpcTunnelConstants.TARGET_TYPE, op_param_value='gnmi-gnoi')
        tunnel.set(op_param_name=GrpcTunnelConstants.CA_CERTIFICATE, op_param_value=GNMI_DIALOUT_CA_NAME)
        tunnel.set(op_param_name=GrpcTunnelConstants.CERTIFICATE, op_param_value=GNMI_DIALOUT_CLIENT_CERT)
        tunnel.set(op_param_name=GrpcTunnelConstants.RETRY_INTERVAL, op_param_value=str(retry_interval))
        tunnel.set(op_param_name=GrpcTunnelConstants.STATE, op_param_value=GnmiConsts.GNMI_STATE_ENABLED, apply=True, ask_for_confirmation=True,
                   dut_engine=dut,).verify_result()


def resolve_dut_ip(dut) -> str:
    """Return the DUT management IP used for gNMI SAN and listening-address."""
    dut_ip = getattr(dut, 'ip', None)
    if dut_ip:
        return str(dut_ip).split('/')[0]
    pytest.fail('Could not resolve DUT IP for gNMI dial-out mTLS setup')
