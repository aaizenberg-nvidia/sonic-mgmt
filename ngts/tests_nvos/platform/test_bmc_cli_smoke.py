"""
SN19 — read-only bmc-cli smoke (one parametrized case).

No burns, resets, or password changes.
write-protect is not a committed CL/SPC6 command (no NVUE, NOS-invisible
ASIC flash WP via MFT/Redfish, FS status Not supported / needs FR) so it
is not in this table.
"""
import logging

import pytest

from ngts.nvos_tools.infra.BmcCliTool import BmcCliTool
from ngts.tests_nvos.platform.bmc_helpers import skip_if_no_bmc
from ngts.tools.test_utils import allure_utils as allure

cumulus_owner = "gpazbenitzha"
logger = logging.getLogger()

BMC_CLI_SMOKE = [
    "info",
    "thermal",
    "power",
    "firmware-detail",
    "inventory",
    "console-get",
    "eeprom",
    "boot-policy",
    "password-policy",
    "erot",
    "attestation-list",
    "debug-token-status",
    "sensor-dump",
    "sensor-dump-file",
    "ping",
    "help",
    "reset-help",
    "firmware-update-help",
]


def _assert_no_login_failure(output, cmd):
    text = str(output or "")
    assert text.strip(), f"{cmd} returned empty output"
    assert "Login failed" not in text, f"{cmd} got login failure: {text[:300]}"


def _assert_json_payload(result, cmd):
    assert isinstance(result, (dict, list)) and result, (
        f"{cmd}: expected non-empty JSON dict/list, got {type(result)} — {result!r}"
    )


@pytest.mark.bmc
@pytest.mark.platform
@pytest.mark.skip_clear_config
@pytest.mark.cumulus
@pytest.mark.parametrize("subcmd", BMC_CLI_SMOKE)
def test_bmc_cli_smoke(engines, devices, subcmd):
    """Read-only / help-surface smoke for the bmc-cli command set (SN19)."""
    skip_if_no_bmc(devices, engines)
    cli = BmcCliTool(engines.dut)

    with allure.step(f"bmc-cli smoke: {subcmd}"):
        if subcmd == "info":
            _assert_json_payload(cli.system_info(), subcmd)
            return
        if subcmd == "thermal":
            _assert_json_payload(cli.thermal_status(), subcmd)
            return
        if subcmd == "power":
            _assert_json_payload(cli.power_status(), subcmd)
            return
        if subcmd == "firmware-detail":
            _assert_json_payload(cli.firmware_list(), subcmd)
            return
        if subcmd == "inventory":
            _assert_json_payload(cli.inventory(), subcmd)
            return
        if subcmd == "console-get":
            out = cli.console_get()
            _assert_no_login_failure(out, subcmd)
            assert "cpu" in out.lower() or "bmc" in out.lower(), (
                f"console get unexpected: {out[:300]}"
            )
            return
        if subcmd == "eeprom":
            out = cli.eeprom()
            _assert_no_login_failure(out, subcmd)
            assert "eeprom" in out.lower(), f"eeprom missing marker: {out[:300]}"
            return
        if subcmd == "boot-policy":
            out = cli.boot_policy()
            _assert_no_login_failure(out, subcmd)
            assert "boot policy" in out.lower(), (
                f"boot-policy missing marker: {out[:300]}"
            )
            return
        if subcmd == "password-policy":
            out = cli.password_policy()
            _assert_no_login_failure(out, subcmd)
            assert "minimum length" in out.lower(), (
                f"password-policy missing 'Minimum Length': {out[:300]}"
            )
            return
        if subcmd == "erot":
            out = cli.erot_status("MGX_ERoT_BMC_0")
            _assert_no_login_failure(out, subcmd)
            assert "erot status" in out.lower() or "background_copy" in out.lower(), (
                f"erot status missing marker: {out[:300]}"
            )
            return
        if subcmd == "attestation-list":
            out = cli.attestation_list()
            _assert_no_login_failure(out, subcmd)
            assert "erot" in out.lower(), (
                f"attestation list missing ERoT: {out[:300]}"
            )
            return
        if subcmd == "debug-token-status":
            out = cli.debug_token_status()
            _assert_no_login_failure(out, subcmd)
            assert "debug token" in out.lower() or "installed" in out.lower(), (
                f"debug-token status missing marker: {out[:300]}"
            )
            return
        if subcmd == "sensor-dump":
            out = cli.sensor_dump()
            _assert_no_login_failure(out, subcmd)
            return
        if subcmd == "sensor-dump-file":
            path = "/tmp/bmc_smoke_sensor_dump.txt"
            try:
                engines.dut.run_cmd(f"rm -f {path}", validate=False)
                out = cli.sensor_dump(output=path)
                _assert_no_login_failure(out, subcmd)
                size = engines.dut.run_cmd(
                    f"stat -c %s {path} 2>/dev/null || echo 0", validate=False
                ).strip()
                assert size.isdigit() and int(size) > 0, (
                    f"sensor-dump --output file missing/empty (size={size})"
                )
            finally:
                engines.dut.run_cmd(f"rm -f {path}", validate=False)
            return
        if subcmd == "ping":
            out = cli.ping(timeout=15)
            _assert_no_login_failure(out, subcmd)
            assert "reachable" in out.lower(), f"ping did not report reachable: {out[:300]}"
            return
        if subcmd == "help":
            out = cli.run("--help")
            _assert_no_login_failure(out, subcmd)
            assert "usage" in out.lower() or "bmc-cli" in out.lower(), (
                f"--help missing usage: {out[:300]}"
            )
            return
        if subcmd == "reset-help":
            # Auth'd --help, not _reset_cmd (that path actually issues a reset).
            out = cli.run("reset --help")
            _assert_no_login_failure(out, subcmd)
            low = out.lower()
            assert "cpu-reset" in low or "bmc-reset" in low or "power-cycle" in low, (
                f"reset --help missing reset types: {out[:400]}"
            )
            return
        if subcmd == "firmware-update-help":
            out = cli.run("--help")
            _assert_no_login_failure(out, subcmd)
            low = out.lower()
            assert "firmware-update" in low or "firmware-update-multipart" in low or (
                "firmware" in low and "update" in low
            ), f"bmc-cli --help does not list firmware-update: {out[:400]}"
            return

        pytest.fail(f"unhandled smoke subcmd: {subcmd}")
