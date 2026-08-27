import logging
import re
import pytest
from time import sleep
from ngts.nvos_tools.infra.BmcCliTool import BmcCliTool
from ngts.tests_nvos.platform.bmc_helpers import (
    skip_if_no_bmc, validate_bmc_healthy, wait_for_bmc_ready,
)
from ngts.tools.test_utils import allure_utils as allure
cumulus_owner = "gpazbenitzha"
logger = logging.getLogger()
CL_SUPPORT_TIMEOUT = 600
BMC_RECOVERY_TIMEOUT = 300


@pytest.mark.bmc
@pytest.mark.cumulus
def test_cl_support_includes_bmc_dump(engines, devices, topology_obj):
    """Ensure BMC dump is included in cl-support AND bmc-cli collect-all works.

    cl-support archive BMC entries plus collect-all file step.
    """
    skip_if_no_bmc(devices, engines)

    with allure.step("Run cl-support to generate system dump"):
        output = engines.dut.run_cmd('sudo cl-support', timeout=CL_SUPPORT_TIMEOUT, validate=False)
        logger.info("cl-support output: %s", output)

    with allure.step("Extract archive path from cl-support output"):
        archive_path = None
        for line in output.splitlines():
            match = re.search(r'(/var/support/\S+\.txz)', line)
            if match:
                archive_path = match.group(1)
                break
        assert archive_path, "Could not find cl-support archive path in output:\n{}".format(output)
        logger.info("cl-support archive: %s", archive_path)

    with allure.step("List archive contents and verify BMC data is present"):
        # A cl-support archive holds ~40k entries; grep on the DUT so the SSH channel
        # carries only the matches. A full listing gets truncated and drops them.
        listing = engines.dut.run_cmd(
            f'tar tf {archive_path} | grep -i bmc', validate=False)
        bmc_entries = [entry for entry in listing.splitlines() if 'bmc' in entry.lower()]
        logger.info("BMC-related entries in archive: %s", bmc_entries)
        assert bmc_entries, (
            "cl-support archive does not contain any BMC-related files. "
            f"Archive: {archive_path}"
        )

    with allure.step("Cleanup generated cl-support archive"):
        engines.dut.run_cmd(f'rm -f {archive_path}', validate=False)

    dump_path = '/tmp/bmc_dump_test.json'
    cli = BmcCliTool(engines.dut)
    try:
        with allure.step("Also run bmc-cli collect-all and verify dump file"):
            result = cli.collect_all(dump_path)
            assert result is not None, "collect-all returned None"
            ls_output = engines.dut.run_cmd(f'ls -la {dump_path}', validate=False)
            assert 'bmc_dump_test.json' in ls_output, f"Dump file not found: {ls_output}"
            size_out = engines.dut.run_cmd(
                f"stat -c %s {dump_path} 2>/dev/null || echo 0", validate=False
            ).strip()
            assert size_out.isdigit() and int(size_out) > 0, \
                f"collect-all dump file missing/empty (size={size_out})"
            logger.info("Dump file size: %s bytes", size_out)
    finally:
        with allure.step("Cleanup collect-all dump file"):
            engines.dut.run_cmd(f'rm -f {dump_path}', validate=False)


@pytest.mark.bmc
@pytest.mark.disable_loganalyzer
@pytest.mark.cumulus
def test_cl_support_while_bmc_down(engines, devices, topology_obj):
    """Verify cl-support completes without hanging while BMC is down."""
    skip_if_no_bmc(devices, engines)

    with allure.step("Reset BMC to bring it down"):
        BmcCliTool(engines.dut).reset_bmc()

    with allure.step("Wait a few seconds then run cl-support while BMC is still recovering"):
        sleep(5)
        output = engines.dut.run_cmd('sudo cl-support', timeout=CL_SUPPORT_TIMEOUT, validate=False)
        logger.info("cl-support output (BMC down): %s", output)

    with allure.step("Verify cl-support produced an archive"):
        archive_found = any(
            re.search(r'\.txz', line) for line in output.splitlines()
        )
        assert archive_found, (
            "cl-support did not produce an archive while BMC was down. "
            "Output:\n{}".format(output)
        )

    with allure.step("Wait for BMC to recover"):
        wait_for_bmc_ready(engines, topology_obj, timeout=BMC_RECOVERY_TIMEOUT)

    with allure.step("Verify BMC is healthy after recovery"):
        validate_bmc_healthy(engines)


# SN28/29/30 (dump under stress / degraded / during FW burn) are tracked as
# REWRITE in the folded table — stub bodies removed from this MR so they do not
# count as green coverage. Re-land with real implementations in a follow-up.
