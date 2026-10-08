import logging
import pytest
from ngts.nvos_tools.infra.BmcCliTool import BmcCliTool
from ngts.tests_nvos.platform.bmc_helpers import skip_if_no_bmc
from ngts.tools.test_utils import allure_utils as allure
cumulus_owner = "gpazbenitzha"
logger = logging.getLogger()


def _bmc_cli(engines):
    """Create a BmcCliTool with sudo prefix for console commands."""
    return BmcCliTool(engines.dut)


@pytest.mark.bmc
@pytest.mark.cumulus
def test_bmc_console_mux_roundtrip(engines, devices):
    """Console mux: one basic toggle, then rapid roundtrips."""
    skip_if_no_bmc(devices, engines)

    bmc_cli = _bmc_cli(engines)

    with allure.step("Read initial console target"):
        original_target = bmc_cli.console_get().strip().lower()
        logger.info("Original console target: %s", original_target)

    try:
        with allure.step("Basic toggle: switch console to BMC then CPU"):
            bmc_cli.console_set("bmc")
            current = bmc_cli.console_get().strip().lower()
            logger.info("Console target after set bmc: %s", current)
            assert "bmc" in current, (
                f"Expected console target 'bmc' after set, got '{current}'"
            )

            bmc_cli.console_set("cpu")
            current = bmc_cli.console_get().strip().lower()
            logger.info("Console target after set cpu: %s", current)
            assert "cpu" in current, (
                f"Expected console target 'cpu' after set, got '{current}'"
            )

        for i in range(2):
            with allure.step(f"Rapid roundtrip {i + 1}/2: set bmc, set cpu"):
                bmc_cli.console_set("bmc")
                result = bmc_cli.console_get()
                assert 'bmc' in result.lower(), f"Expected 'bmc', got: {result}"

                bmc_cli.console_set("cpu")
                result = bmc_cli.console_get()
                assert 'cpu' in result.lower(), f"Expected 'cpu', got: {result}"
    finally:
        with allure.step("Restore console target to CPU"):
            bmc_cli.console_set("cpu")
            logger.info("Console target restored to cpu")


@pytest.mark.bmc
@pytest.mark.cumulus
def test_console_set_invalid_target(engines, devices):
    """'--target invalid' should produce a clean error."""
    skip_if_no_bmc(devices, engines)

    bmc_cli = _bmc_cli(engines)
    with allure.step("Attempt to set console to invalid target"):
        result = bmc_cli.console_set("invalid_target_xyz")

    with allure.step("Verify error response"):
        result_lower = str(result).lower()
        assert any(w in result_lower for w in ['error', 'invalid', 'fail', 'usage']), \
            f"Expected error for invalid target, got: {result}"


@pytest.mark.bmc
@pytest.mark.cumulus
def test_console_not_in_nvue(engines, devices):
    """Verify the console mux is intentionally not part of the NVUE tree.

    Product guarantee: UART mux is bmc-cli-only on CL;
    ``nv show platform bmc console`` must not succeed as a real show.
    """
    skip_if_no_bmc(devices, engines)

    with allure.step("Attempt NVUE serial console command (must not succeed)"):
        output = engines.dut.run_cmd(
            'nv show platform bmc console', validate=False
        )
        logger.info("NVUE console command output: %s", str(output)[:300])

    with allure.step("Assert NVUE rejects / does not expose console mux"):
        text = str(output).lower()
        reject_tokens = (
            'error', 'invalid', 'unknown', 'not found', 'no such',
            'unrecognized', 'not recognized', 'usage', 'failed',
            'ambiguous', 'incomplete',
        )
        looks_rejected = any(tok in text for tok in reject_tokens)
        # Empty / whitespace-only is also acceptable (no tree / no data).
        looks_empty = not text.strip()
        assert looks_rejected or looks_empty, (
            "Expected NVUE console mux command to fail or return empty "
            f"(console mux must stay outside the NVUE tree), got: {str(output)[:300]}"
        )
