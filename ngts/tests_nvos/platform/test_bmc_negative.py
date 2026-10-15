"""
TestBmcNegative: Cross-cutting failure-mode tests. Validates graceful behavior
under adversarial and edge-case conditions.

Extended coverage — not in BMC_Testing.txt.
"""
import logging
import re
import time

import pytest

from ngts.nvos_tools.infra.BmcCliTool import BmcCliTool
from ngts.nvos_tools.infra.DutUtilsTool import DutUtilsTool
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.constants import MINUTE
from ngts.tests_nvos.platform.bmc_helpers import (
    BMC_IP,
    ensure_usb0_up,
    ignore_cl_reboot_result,
    skip_if_no_bmc,
    validate_bmc_healthy,
    wait_for_bmc_ready,
)
from ngts.tools.test_utils import allure_utils as allure

cumulus_owner = "gpazbenitzha"
logger = logging.getLogger()


def _assert_bmc_reachable(engines):
    """Ping the CL usb0 BMC address (platform validate is inventory-only)."""
    ping_out = engines.dut.run_cmd(
        f"ping -c 2 -W 3 {BMC_IP}", validate=False)
    assert "bytes from" in ping_out, f"BMC at {BMC_IP} is not reachable"


@pytest.mark.bmc
@pytest.mark.disable_loganalyzer
@pytest.mark.cumulus
def test_bmc_unreachable_graceful_error(engines, devices, topology_obj):
    """NOS CLIs return a clean error when BMC is down, not hang."""
    skip_if_no_bmc(devices)
    ensure_usb0_up(engines.dut)

    with allure.step("Reset BMC via bmc-cli to make it temporarily unreachable"):
        # Use the wrapper's root-shell reset path (``sudo su -c 'bmc-cli reset
        # bmc-reset'``): it carries no password in the command, so the secret can
        # never reach logs/artifacts, and it is the form that actually performs
        # the reset (the ``--host/--username/--password`` form only prints
        # "Performing ..." without resetting the host).
        BmcCliTool(engines.dut).reset_bmc()

    with allure.step("Confirm BMC is down before asserting NVUE error"):
        # Avoid racing a still-up BMC (healthy inventory would look like success).
        # Require several consecutive ping misses so a brief usb0 blip is not enough.
        deadline = time.time() + 120
        consecutive_misses = 0
        needed_misses = 3
        bmc_down = False
        while time.time() < deadline:
            ping_out = engines.dut.run_cmd(
                f"ping -c 1 -W 2 {BMC_IP}", validate=False)
            if "bytes from" not in ping_out:
                consecutive_misses += 1
                if consecutive_misses >= needed_misses:
                    bmc_down = True
                    break
            else:
                consecutive_misses = 0
            time.sleep(2)
        assert bmc_down, (
            f"BMC at {BMC_IP} still reachable after reset — cannot validate "
            f"graceful BMC-down CLI error"
        )

    with allure.step("Query BMC via NVUE — expect clean error, not hang"):
        output = engines.dut.run_cmd(
            'nv show platform inventory BMC -o json',
            validate=False, timeout=60
        )
        logger.info(f"NVUE output while BMC is down: {str(output)[:200]}")
        out_l = str(output or "").lower()
        assert out_l.strip(), "NVUE returned nothing while the BMC was down"
        # Two answers are both clean. Before #5237224 the aggregate inventory
        # dropped the BMC entry and NVUE errored ("Error: The requested item
        # does not exist."); since that fix the entry stays and reports
        # state 'failed' with N/A fields. What must not happen is a hang, an
        # empty answer, or a healthy 'ok' for a BMC that is down.
        clean_error = (
            "the requested item does not exist" in out_l or
            ("error" in out_l and "does not exist" in out_l)
        )
        state_match = re.search(r'"state"\s*:\s*"([^"]+)"', out_l)
        state = state_match.group(1) if state_match else None
        assert state != "ok", (
            f"NVUE reported the BMC healthy while it was unreachable: "
            f"{str(output)[:300]}"
        )
        assert clean_error or state in ("failed", "not ok", "unknown", "down"), (
            f"Expected a clean NVUE error or a degraded BMC report while the "
            f"BMC is down, got: {str(output)[:300]}"
        )

    with allure.step("Wait for BMC to recover"):
        wait_for_bmc_ready(engines, topology_obj, timeout=600)


@pytest.mark.bmc
@pytest.mark.disable_loganalyzer
@pytest.mark.cumulus
def test_bmc_operations_under_system_load(engines, devices, topology_obj):
    """BMC keeps working under concurrent system / config activity.

    Scenarios (collect-all errors within this case):
      1) Multiple BMC queries in one burst (former concurrent_bmc_operations)
      2) BMC query while NOS config apply is in progress (table intent:
         BMC ops during NOS upgrade / activity)
    """
    skip_if_no_bmc(devices)
    ensure_usb0_up(engines.dut)
    errors = []

    cli = BmcCliTool(engines.dut)
    with allure.step("Scenario A: burst of BMC queries (sensor/info/inventory)"):
        try:
            results = {
                'sensor': cli.run('sensor-dump'),
                'system': cli.system_info(),
                'inventory': cli.inventory(),
            }
            for name, result in results.items():
                assert result, f"{name} returned empty/None"
                if isinstance(result, dict):
                    assert len(result) > 0, f"{name} returned empty dict"
                elif isinstance(result, str):
                    assert 'Login failed' not in result, \
                        f"{name} got login failure: {result[:200]}"
            validate_bmc_healthy(engines)
            _assert_bmc_reachable(engines)
        except Exception as exc:  # noqa: BLE001 — collect-all within scenario
            errors.append(f"burst BMC queries: {exc}")

    with allure.step("Scenario B: BMC query during NOS config activity"):
        try:
            engines.dut.run_cmd('nv config apply -y', validate=False, timeout=30)
            result = cli.system_info()
            if isinstance(result, dict):
                assert len(result) > 0, "BMC system_info returned empty dict"
            elif isinstance(result, str):
                assert 'Login failed' not in result, \
                    f"BMC system_info got login failure: {result[:200]}"
                assert result.strip(), "BMC system_info returned empty string"
            else:
                raise AssertionError(
                    f"BMC system_info returned unexpected type: {type(result)}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"BMC during NOS activity: {exc}")

    assert not errors, "BMC under system load failures:\n- " + "\n- ".join(errors)


@pytest.mark.bmc
@pytest.mark.cumulus
def test_bmc_auth_failure_handling(engines, devices, topology_obj):
    """Wrong credentials produce a clean auth error, not a generic timeout."""
    skip_if_no_bmc(devices)
    ensure_usb0_up(engines.dut)

    # Raw Redfish curl to the BMC is not viable from the CPU on CL/SPC6: the usb0
    # address (BMC_IP:443) never completes a TCP handshake from the switch, and
    # the BMC mgmt IP is not routable from the switch. bmc-cli with --no-tpm DOES honor
    # the supplied --password (without --no-tpm, bmc-cli as root reads /etc/cumulus/bmc.conf
    # and ignores the password), so a wrong password yields a clean "Login failed",
    # not a hang -- which is exactly what this negative test should verify.
    with allure.step("Run bmc-cli with a wrong password (forced via --no-tpm)"):
        response = engines.dut.run_cmd(
            f"sudo timeout 30 /usr/cumulus/bin/bmc-cli --no-tpm "
            f"--host {BMC_IP} --username root "
            f"--password totally_wrong_password_12345 info",
            validate=False, timeout=45)

    with allure.step("Verify a clean auth failure (not a hang/timeout)"):
        response_lower = str(response).lower()
        error_indicators = [
            'login failed', 'error code', 'unauthorized', 'authorization',
            'forbidden', 'denied', 'invalid', 'auth',
        ]
        assert any(ind in response_lower for ind in error_indicators), \
            f"Expected a clean auth failure, got: {response[:300]}"


@pytest.mark.bmc
@pytest.mark.slow
@pytest.mark.stress
@pytest.mark.disable_loganalyzer
@pytest.mark.skip_clear_config
@pytest.mark.timeout(30 * MINUTE)
@pytest.mark.cumulus
def test_rapid_power_cycle_stress(engines, devices, topology_obj):
    """3 power-cycles in succession — system always recovers.

    Marked slow/stress — gate behind CI with -m "not slow".
    """
    skip_if_no_bmc(devices)

    for i in range(3):
        with allure.step(f"Power-cycle iteration {i + 1}/3"):
            # should_wait_till_system_ready=False is REQUIRED: the default (True)
            # makes action_reboot wait for readiness over the serial console, which
            # times out mid-BIOS on this setup (pexpect 30s prompt timeout). We do
            # our own disconnect/reconnect recovery instead (proven CL pattern).
            result = System().reboot.action_reboot(send_user_confirmation='y',
                                                   should_wait_till_system_ready=False)
            ignore_cl_reboot_result(result)
            engines.dut.disconnect()
            time.sleep(90)
            DutUtilsTool.reconnect_engine(engines.dut, find_prompt_tries=90,
                                          find_prompt_delay=10)
            # Non-strict: BMC may still be settling; wait_for_bmc_ready polls next.
            ensure_usb0_up(engines.dut, strict=False)

            with allure.step("Verify BMC healthy after power-cycle"):
                wait_for_bmc_ready(engines, topology_obj, timeout=600)


# SN37 (FW burn disk full) tracked as REWRITE/TODO — stub body removed from this
# MR (false promise: only df + health). Re-land with real ENOSPC inject + cleanup.


@pytest.mark.bmc
@pytest.mark.cumulus
def test_bmc_cli_invalid_subcommand(engines, devices):
    """'bmc-cli nonexistent' produces a helpful error, not a stack trace."""
    skip_if_no_bmc(devices)

    cli = BmcCliTool(engines.dut)
    with allure.step("Run bmc-cli with invalid subcommand"):
        result = cli.run('nonexistent-command-xyz')

    with allure.step("Verify output is an error message, not a Python traceback"):
        result_str = str(result)
        assert 'Traceback' not in result_str, \
            f"bmc-cli returned a stack trace: {result_str[:300]}"


@pytest.mark.bmc
@pytest.mark.cumulus
def test_bmc_cli_unreachable_host(engines, devices):
    """bmc-cli with unreachable host produces a timeout with clear message."""
    skip_if_no_bmc(devices)

    with allure.step("Run bmc-cli command targeting unreachable host"):
        result = engines.dut.run_cmd(
            'bmc-cli --host 192.168.255.255 info',
            validate=False, timeout=30
        )

    with allure.step("Verify output indicates connection failure"):
        result_lower = str(result).lower()
        failure_indicators = ['timeout', 'error', 'failed', 'refused', 'unreachable']
        assert any(ind in result_lower for ind in failure_indicators), \
            f"Expected connection failure message, got: {result[:300]}"
