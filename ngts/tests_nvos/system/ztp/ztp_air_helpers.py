import logging

from ngts.tools.test_utils import allure_utils as allure
from ngts.nvos_constants.constants_nvos import SystemConsts
from ngts.nvos_tools.infra.DutUtilsTool import DutUtilsTool
from devts.infra.tools.general_constants.air_constants import NvidiaAirConstants

logger = logging.getLogger(__name__)

DHCPD_CONF_PATH = '/etc/dhcp/dhcpd.conf'
DHCPD_CONF_BACKUP_PATH = '/etc/dhcp/dhcpd.conf.bak.ztp_test'
ZTP_JSON_PATH_ON_OOB = '/home/ubuntu/ztp_data.json'
ZTP_JSON_NFS_URL = f'{SystemConsts.HTTP_SERVER}{SystemConsts.VERIFICATION_ZTP_PATH}{SystemConsts.POSITIVE_JSON}'
OOB_SERVER_IP = NvidiaAirConstants.NVIDIA_AIR_OOB_MGMT_SERVER_IP
OOB_USER = NvidiaAirConstants.NVIDIA_AIR_OOB_MGMT_SERVER_USER

# Seconds to wait after factory reset before SSH-ing in.
# SSH connections trigger config changes that cancel ZTP.
ZTP_CONFIGURE_TIME = 330

ZTP_JOURNAL_SUCCESS_PATTERN = 'ZTP successfully completed'
ZTP_JOURNAL_FAILURE_PATTERN = r'ZTP failed'


def remove_all_ztp_data_files(dut_engine) -> None:
    """Remove all ZTP files from the DUT."""
    with allure.step("Remove all ZTP files"):
        dut_engine.run_cmd('sudo rm -rf  /host/ztp/ztp_data*')


def follow_ztp_journal_until_done(dut_engine, raise_on_failure: bool = True, timeout=300, since_boot: bool = False):
    """
    Follow the ZTP journal stream and return as soon as success or failure is detected.

    :param dut_engine: SSH engine for the DUT
    :param raise_on_failure: if True, raise AssertionError on ZTP failure
    :param timeout:    max seconds to wait before giving up
    :param since_boot: if True, read from the start of the current boot
    :raises TimeoutError: if ZTP does not complete within *timeout* seconds
    :raises AssertionError: if ZTP reports failure
    """
    patterns = [ZTP_JOURNAL_SUCCESS_PATTERN, ZTP_JOURNAL_FAILURE_PATTERN]
    with allure.step(f'Follow ZTP journal, waiting for: {patterns!r}'):
        i, output = DutUtilsTool.follow_journal_until_pattern(dut_engine, 'ztp', patterns, timeout=timeout,
                                                              since_boot=since_boot)
        if raise_on_failure and i == patterns.index(ZTP_JOURNAL_FAILURE_PATTERN):
            raise AssertionError(f'ZTP failed per journal:\n{output}')
        logger.info(f'ZTP completed successfully.')


def download_ztp_json_to_oob(oob_engine):
    """Download ZTP JSON from NFS HTTP server to the oob-mgmt-server."""
    with allure.step(f"Download ZTP JSON from {ZTP_JSON_NFS_URL} to OOB server"):
        oob_engine.run_cmd(f'curl -o {ZTP_JSON_PATH_ON_OOB} {ZTP_JSON_NFS_URL}')
        oob_engine.run_cmd(f'cat {ZTP_JSON_PATH_ON_OOB}')


def backup_dhcpd_configs(oob_engine):
    """Backup dhcpd.conf before ZTP modifications."""
    with allure.step("Backup original dhcpd.conf"):
        oob_engine.run_cmd(f'sudo cp {DHCPD_CONF_PATH} {DHCPD_CONF_BACKUP_PATH}')


def configure_dhcpd_for_ztp(oob_engine):
    """
    Configure DHCP server on oob-mgmt-server for ZTP discovery.

    Adds global ZTP options (tftp-server-name, bootfile-name) to dhcpd.conf,
    then restarts isc-dhcp-server.
    """
    scp_url = f'scp://{OOB_USER}:{oob_engine.password}@{OOB_SERVER_IP}{ZTP_JSON_PATH_ON_OOB}'

    ztp_options = (
        f'# ZTP options added by test_system_ztp_air\n'
        f'option tftp-server-name "{OOB_SERVER_IP}";\n'
        f'option bootfile-name "{scp_url}";'
    )

    with allure.step("Add ZTP options to dhcpd.conf"):
        oob_engine.run_cmd(
            f"sudo tee -a {DHCPD_CONF_PATH} <<'ZTP_EOF'\n{ztp_options}\nZTP_EOF"
        )

    with allure.step("Validate dhcpd.conf syntax"):
        oob_engine.run_cmd(f'sudo dhcpd -t -cf {DHCPD_CONF_PATH}')
        # TODO  VERIFY BY OUTPUT

    with allure.step("Restart DHCP server"):
        oob_engine.run_cmd('sudo systemctl restart isc-dhcp-server')
        status = oob_engine.run_cmd('sudo systemctl is-active isc-dhcp-server')
        assert 'active' in status, f'DHCP server failed to start: {status}'


def restore_dhcpd_configs(oob_engine):
    """Restore dhcpd.conf from backup, restart DHCP, and clean up."""
    with allure.step("Restore original dhcpd.conf"):
        oob_engine.run_cmd(f'sudo cp {DHCPD_CONF_BACKUP_PATH} {DHCPD_CONF_PATH}')
        oob_engine.run_cmd(f'sudo rm -f {DHCPD_CONF_BACKUP_PATH}')
    with allure.step("Restart DHCP server and clean up"):
        oob_engine.run_cmd('sudo systemctl restart isc-dhcp-server')
        oob_engine.run_cmd(f'rm -f {ZTP_JSON_PATH_ON_OOB}')
