"""
ZTP BAT test for AIR platform.

Tests the full DHCP-based ZTP discovery flow that is only possible on AIR,
where we control the oob-mgmt-server and can configure it as a DHCP + SCP server.

Flow:
    1. Configure oob-mgmt-server DHCP with ZTP options (bootfile-name, tftp-server-name)
    2. Download ZTP JSON from NFS to oob-mgmt-server
    3. Reboot DUT to trigger DHCP-based ZTP discovery
    4. Verify ZTP completes successfully
"""
import logging
import time

import pytest
from retry.api import retry_call

from ngts.nvos_constants.constants_nvos import SystemConsts
from ngts.tests_nvos.system.ztp.ztp_air_helpers import (
    ZTP_CONFIGURE_TIME,
    download_ztp_json_to_oob,
    backup_dhcpd_configs,
    configure_dhcpd_for_ztp,
    restore_dhcpd_configs,
    follow_ztp_journal_until_done, remove_all_ztp_data_files,
)
from ngts.tests_nvos.system.ztp.ztp_helpers import (
    apply_empty_config_and_save,
    wait_until_ztp_status,
    wait_until_ztp_values_fields_changed,
)
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger(__name__)


@pytest.fixture(scope='module')
def configure_oob_for_ztp(engines):
    """
    Configure oob-mgmt-server as a ZTP server (DHCP + SCP).

    Setup:
        1. Download ZTP JSON from NFS to oob-mgmt-server
        2. Backup dhcpd.conf
        3. Add ZTP options to dhcpd.conf
        4. Validate and restart DHCP server

    Teardown:
        Restore original dhcpd.conf, restart DHCP.
    """
    oob_engine = engines.oob_mgmt_server

    with allure.step("Configure OOB server for ZTP"):
        download_ztp_json_to_oob(oob_engine)
        backup_dhcpd_configs(oob_engine)
        configure_dhcpd_for_ztp(oob_engine)

    yield

    with allure.step("Cleanup OOB ZTP configuration"):
        restore_dhcpd_configs(oob_engine)


@pytest.mark.ztp
@pytest.mark.system
@pytest.mark.air
@pytest.mark.reboot
def test_ztp_dhcp_discovery_on_air(topology_obj, engines, devices, nv_command, configure_oob_for_ztp):
    """
    Test ZTP DHCP-based discovery flow on AIR platform.

    This test verifies the full ZTP flow that occurs during initial device provisioning:
        1. OOB-mgmt-server is configured as DHCP+SCP server with ZTP options
        2. DUT is rebooted to trigger ZTP discovery
        3. DUT obtains ZTP JSON URL via DHCP option bootfile-name
        4. DUT downloads and executes the ZTP JSON
        5. ZTP completes successfully

    This flow cannot be tested in physical labs because it requires
    control over the DHCP server on the device's management network.
    """
    try:
        with allure.step("Abort ZTP and remove data files"):
            nv_command.system.ztp.action_abort_ztp().verify_result()
            remove_all_ztp_data_files(engines.dut)
            wait_until_ztp_values_fields_changed(
                nv_command.system,
                [SystemConsts.ZTP_STATUS],
                [SystemConsts.ZTP_CONFIG_SAVE_STATUS],
            )

        with allure.step("Factory reset DUT to trigger ZTP discovery"):
            nv_command.system.factory_default.action_reset(param='force', wait_for_functional=False).verify_result()

        with allure.step(f"Wait {ZTP_CONFIGURE_TIME}s for ZTP configure phase"):
            logger.info(f"Sleeping {ZTP_CONFIGURE_TIME}s to avoid cancelling ZTP flow")
            time.sleep(ZTP_CONFIGURE_TIME)

        with allure.step("Reconnect SSH to DUT"):
            retry_call(engines.dut.run_cmd, fargs=[''], fkwargs={'timeout': 30},
                       tries=10, delay=10, logger=logger)

        with allure.step("Follow ZTP journal until success"):
            follow_ztp_journal_until_done(engines.dut, since_boot=True)

        with allure.step("Verify ZTP status"):
            wait_until_ztp_status(nv_command.system, SystemConsts.ZTP_STATUS_SUCCESS, is_on_air=True)

    except Exception as e:
        logger.error(f"ZTP DHCP discovery test failed: {e}")
        try:
            ztp_show = nv_command.system.ztp.show()
            logger.info(f"ZTP show output at failure:\n{ztp_show}")
            ztp_log = engines.dut.run_cmd('sudo tail -100 /var/log/ztp.log', validate=False)
            logger.info(f"ZTP log tail:\n{ztp_log}")
        except Exception as diag_err:
            logger.warning(f"Could not collect diagnostics: {diag_err}")
        raise
    finally:
        try:
            nv_command.system.ztp.action_abort_ztp().ignore_result()
            remove_all_ztp_data_files(engines.dut)
            apply_empty_config_and_save(engines)
        except Exception as cleanup_err:
            logger.warning(f"Cleanup failed: {cleanup_err}")
