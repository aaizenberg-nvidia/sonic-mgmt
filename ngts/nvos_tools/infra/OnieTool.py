import datetime
import re

from ngts.tests_nvos.general.ONIE.constants import OnieConsts, ProvisionConsts
from ngts.tools.test_utils import allure_utils as allure
import allure
import logging

from ngts.constants.constants import PlayersAliases
from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from ngts.nvos_tools.infra.BiosTools.BiosFactory import BiosFactory
from ngts.nvos_tools.infra.DutUtilsTool import DutUtilsTool
from devts.infra.tools.validations.traffic_validations.port_check.port_checker import check_port_status_till_alive
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit

logger = logging.getLogger()


class OnieTool:

    @staticmethod
    def is_opn(topology_obj):
        """
        Determine if the DUT is an OPN device by checking the 'opn' field in topology.

        @param topology_obj: topology object containing players and attributes
        @return: bool - True if OPN, False otherwise
        """
        for host in topology_obj.players:
            if host in PlayersAliases.duts_list:
                opn_attr = topology_obj.players[host]['attributes'].noga_query_data['attributes']['Specific'].get('opn',
                                                                                                                  '')
                return opn_attr.strip().lower() == 'yes'
        raise ValueError("Could not determine if device is OPN – no matching DUT found.")

    @staticmethod
    def is_ipn(topology_obj):
        """
        Determine if the DUT is an IPN device — logically not OPN.

        @param topology_obj: topology object containing players and attributes
        @return: bool - True if IPN, False otherwise
        """
        return not OnieTool.is_opn(topology_obj)

    @staticmethod
    def get_device_type(topology_obj) -> str:
        """
        Determine device type from topology ('OPN' or 'IPN').

        :param topology_obj: Topology object with DUT attributes.
        :return: 'OPN' or 'IPN'
        """
        return OnieConsts.OPN if OnieTool.is_opn(topology_obj) else OnieConsts.IPN

    @staticmethod
    def fetch_onie_updater(serial_engine, image_url):
        """
        Downloads the ONIE updater file using wget and checks for success.

        @param serial_engine: Serial connection engine object
        @param image_url: URL of the ONIE updater to download
        @raises Exception: if wget fails to download the file
        """
        with allure.step(f'Downloading ONIE updater from {image_url}'):
            out, index = serial_engine.run_cmd(
                f'wget {image_url}',
                expected_value=["100%", OnieConsts.WGET_ERROR],
                timeout=30
            )

            if index == 0:
                logger.info("ONIE updater downloaded successfully.")
            elif index == 1:
                error_msg = "Failed for wget error"
                raise Exception(error_msg)
            else:
                raise Exception("Unexpected response during ONIE updater fetch.")

    @staticmethod
    def run_onie_updater(serial_engine):
        """
        Runs the ONIE updater tool and verifies outcome.

        @param serial_engine: Serial connection engine object
        @raises Exception: if update fails or CMS verification error is detected
        """
        expected_patterns = [OnieConsts.UPDATE_SUCCESS_PATTERN, OnieConsts.CMS_VERIFICATION_ERROR]
        with allure.step('Update ONIE via onie-updater'):
            out, index = serial_engine.run_cmd(
                OnieConsts.ONIE_UPDATE_COMMAND,
                expected_value=expected_patterns,
                timeout=30
            )

            if index == 0:
                logger.info(f'Successfully matched pattern: "{expected_patterns[0]}"')
            elif index == 1:
                error_msg = (
                    "CMS verification failed during ONIE update. "
                    "You may have downloaded an incompatible ONIE image.\n"
                    "👉 Please verify the OPN value in NOGA and ensure you're using the correct image.\n\n"
                )
                raise Exception(error_msg)
            else:
                raise Exception(f"Unexpected update result.")

    @staticmethod
    def get_onie_updater_path(topology_obj) -> str:
        """
        Get the ONIE updater image path based on device type.

        :param topology_obj: Topology object with DUT attributes.
        :return: ONIE updater image URL
        """
        dev_type = OnieTool.get_device_type(topology_obj)
        logger.info(f"Selected ONIE updater path for {dev_type}: {OnieConsts.ONIE_FILES_DICT[dev_type]}")
        return OnieConsts.ONIE_FILES_DICT[dev_type]

    @staticmethod
    def get_provisioning_url(topology_obj) -> str:
        """
        Return the provisioning URL based on device type (OPN/IPN).
        """
        dev_type = OnieTool.get_device_type(topology_obj)
        version = ProvisionConsts.VERSIONS_DICT[dev_type]['version']
        logger.info(f"Selected provisioning version: {version}")
        return ProvisionConsts.VERSIONS_DICT[dev_type]['provisioning_url']

    @staticmethod
    def run_provisioning(serial_engine, filename):
        serial_engine.run_cmd("cd /tmp", expected_value=["/tmp"])
        serial_engine.run_cmd(f"tar -xzf {filename}", expected_value='.*')
        serial_engine.run_cmd("./sedutil_init.sh", expected_value="completed successfully", timeout=60)

    @staticmethod
    def get_onie_version_name_for_pxe(topology_obj) -> str:
        """
        Get the ONIE PXE menu version string based on device type.

        :param topology_obj: Topology object with DUT attributes.
        :return: ONIE version string (e.g., 'ONIE_r5.3.0015-115200')
        """
        dev_type = OnieTool.get_device_type(topology_obj)
        logger.info(f"Selected PXE version for {dev_type}: {OnieConsts.ONIE_VERSIONS_PXE_DICT[dev_type]}")
        return OnieConsts.ONIE_VERSIONS_PXE_DICT[dev_type]

    @staticmethod
    def extract_onie_updater_result(output):
        lines = output.strip().splitlines()
        results = []
        for line in lines:
            if line.startswith('onie'):
                parts = [p.strip() for p in line.split('|')]
                if len(parts) == 4:
                    results.append({
                        'name': parts[0],
                        'version': parts[1],
                        'result': parts[2],
                        'date': parts[3],
                    })
        return results

    @staticmethod
    def verify_onie_update(engine):
        with allure.step('Run mlnx-onie-fw-update.sh and verify latest update'):
            output = engine.run_cmd("sudo mlnx-onie-fw-update.sh")
            results = OnieTool.extract_onie_updater_result(output)

            assert results, "No update results found in output"

            last_update = results[-1]
            assert last_update['name'] == OnieConsts.ONIE_UPDATER_FILE, \
                f"Unexpected file name in last update: {last_update['name']}"

            assert last_update['result'].lower() == 'success', \
                f"Update result was not successful: {last_update['result']}"

            now = datetime.datetime.now().strftime('%Y-%m-%d')
            assert now in last_update['date'], \
                f"Update date '{last_update['date']}' does not match current date '{now}'"
