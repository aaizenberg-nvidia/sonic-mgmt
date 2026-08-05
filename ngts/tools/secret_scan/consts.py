"""
Constants + store config for the secret-scan request protocol.

Mirrors the request/response protocol of the TruffleHog secret-scan service
(see the trufflehog repo, docs/protocol.md). The protocol constants below MUST
stay in sync with the server's utils/config.py, and BASE must match the
server's SERVICE_BASE. Per-request metadata (project / release / notify / dut)
is handled by the client + CLI options (see client.py and conftest.py).
"""


class SecretScanConsts:
    # --- Protocol contract (keep in sync with the server config) ---
    REQUESTS_DIRNAME: str = 'requests'
    REQUEST_INFO_FILENAME: str = 'request_info.json'
    SUBMITTED_MARKER: str = 'SUBMITTED'
    # request_info.json schema version the server understands.
    REQUEST_SCHEMA_VERSION: str = '1.0.0'

    # --- Store base: shared NFS dir holding requests/ + responses/ ---
    #     (must equal the server's SERVICE_BASE).
    BASE: str = ('/auto/sw_system_project/NVOS_INFRA/security/'
                 'trufflehog-service')
