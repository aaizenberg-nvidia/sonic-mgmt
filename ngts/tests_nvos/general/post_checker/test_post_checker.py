import logging
import pytest
import os

from devts.infra.tools.connection_tools.serial_engine_factory import SerialEngineFactory

from devts.infra.tools.validations.traffic_validations.port_check import port_checker
from ngts.nvos_tools.infra.TrafficGeneratorTool import TrafficGeneratorTool
from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from ngts.nvos_tools.infra.ConnectionTool import ConnectionTool
from ngts.nvos_tools.infra.DutUtilsTool import DutUtilsTool
from ngts.ngts_types import TopologyT, EnginesT, DevicesT
from ngts.tools.test_utils import allure_utils as allure
from ngts.constants.constants import FatalStateConsts
from ngts.tools.test_utils import nvos_config_utils
from ngts.tools.test_utils import switch_recovery
from ngts.nvos_tools.system.System import System

logger = logging.getLogger(__name__)


@pytest.mark.general
@pytest.mark.disable_loganalyzer
@pytest.mark.no_log_test_wrapper
@pytest.mark.no_cli_coverage_run
def test_post_checker(
    engines: EnginesT,
    topology_obj: TopologyT,
    devices: DevicesT,
    default_config_yml_path: str,
    root_dir: str,
    dumps_folder: str,
    setup_name: str,
    security_post_checker: bool,
    pytestconfig: pytest.Config,
):
    """
    Post checker flow:
        1. Check if ssh port is open and we can connect to it
        2. If no connection generate sysdump
        3. Check if we have uncleaned configuration after test, clean it if exist
        4. Check if ssh port is open and we can connect to it
        5. If no connection generate sysdump
        6. Check if we have uncleaned configuration after test, clean it if exist
        7. If after cleanup we still have uncleaned configuration, perform reboot
        8. Check if ssh port is open, if not perform reboot
        9. Upload sysdump to shared location
        10. Check system in fatal state, if yes, stop regression
    """
    no_dut_reboot = getattr(pytestconfig, "_no_dut_reboot", False)
    no_techsupport_on_fail = getattr(pytestconfig, "_no_techsupport_on_fail", False)

    try:
        if security_post_checker:
            switch_recovery.check_switch_connectivity(topology_obj, engines, allow_dut_reboot=not no_dut_reboot)

        with allure.step('Check dut is up'):
            if not ConnectionTool.ping_device(engines.dut.ip, num_of_retries=1):
                if no_dut_reboot:
                    # This is a hack to stop the regression when the DUT is not up and --no-dut-reboot is set
                    pytest.exit("DUT is not up, and --no-dut-reboot is set, stopping regression", returncode=42)
                NvueGeneralCli(engines.dut).remote_reboot(topology_obj)

            res_obj = DutUtilsTool.wait_for_nvos_to_become_functional(engines.dut)

            if res_obj.result:
                with allure.step("Clear config"):
                    nvos_config_utils.clear_conf(
                        engine=engines.dut,
                        device=devices.dut,
                        config_yml=default_config_yml_path,
                        root_dir=root_dir,
                    )
            else:
                logging.info("Try to clear the config using serial console")

                with allure.step("Create serial engine"):
                    system = System()
                    # post_checker.copy_file is Mellanox/netmiko-only, so build a
                    # MellanoxSerialEngine explicitly regardless of how the
                    # topology factory dispatched at build time. Fails loudly
                    # on AIR (no serial_connection_command) — Phase 2.
                    attributes = topology_obj.players['dut_serial']['attributes'].noga_query_data['attributes']
                    serial_engine = SerialEngineFactory.create_mellanox(attributes)

                with allure.step("Try to generate techsupport using serial console"):
                    # no-techsupport-on-fail flag is set in pytest_configure, pytest_addoption, _configure_skip_gen_techsupport_on_fail
                    if no_techsupport_on_fail:
                        logger.warning("Skipping techsupport generation on test failure")
                    else:
                        generate_techsupport(dumps_folder, system, serial_engine)

                with allure.step("Clear config using serial console"):
                    nvos_config_utils.clear_conf(
                        engine=serial_engine,
                        device=devices.dut,
                        config_yml=default_config_yml_path,
                        root_dir=root_dir,
                    )

                with allure.step('Check connection and perform reboot if needed'):
                    logger.info('Check port status, should be up after cleanup')
                    if not check_if_dut_port_is_open(engines):
                        logger.info('System not up after cleanup, performing reboot to revive system')
                        if no_dut_reboot:
                            # This is a hack to stop the regression when the system is not up after cleanup and --no-dut-reboot is set
                            pytest.exit("System not up after cleanup, and --no-dut-reboot is set, stopping regression", returncode=42)
                        NvueGeneralCli(engines.dut).remote_reboot(topology_obj)
                        DutUtilsTool.wait_for_nvos_to_become_functional(engines.dut).verify_result()

                with allure.step('Check fatal state'):
                    fatal_state_result = check_system_in_fatal_state(serial_engine, FatalStateConsts.FATAL_FILE)
                    if fatal_state_result == FatalStateConsts.FATAL_STATE_RETURN_CODE:
                        pytest.exit(f"System in fatal state, return code {FatalStateConsts.FATAL_STATE_RETURN_CODE}",
                                    returncode=FatalStateConsts.FATAL_STATE_RETURN_CODE)

        if not security_post_checker:
            TrafficGeneratorTool.bring_up_traffic_containers(engines, setup_name)

    except BaseException as err:
        logging.error(f"Exception during post checker: {err}")
        raise err


def check_if_dut_port_is_open(engines):
    try:
        port_checker.check_port_status_till_alive(True, engines.dut.ip, engines.dut.ssh_port, tries=3, delay=2)
        return True
    except BaseException:
        return False


def check_system_in_fatal_state(engines, file_path):
    result = engines.dut.run_cmd(f"sudo test -f {file_path} && echo exists || echo not_exists")
    if result.strip() == "exists":
        return FatalStateConsts.FATAL_STATE_RETURN_CODE


def generate_techsupport(dumps_folder, system, serial_engine):

    with allure.step('Generating a sysdump'):
        tar_file, duration = system.techsupport.action_generate(engine=serial_engine)
        logger.info("Dump was created at: {}".format(tar_file))
        tarball_file_name = str(tar_file.replace('/var/dump/', ''))

    with allure.step('Copy dump: {} to log folder {}'.format(tarball_file_name, dumps_folder)):
        dest_file = dumps_folder + '/sysdump_' + tarball_file_name
        logger.info('Copy dump {} to log folder {}'.format(tar_file, dumps_folder))
        serial_engine.copy_file(source_file=tar_file, dest_file=dest_file, file_system='/', direction='get',
                                overwrite_file=True, verify_file=False)
        os.chmod(dest_file, 0o777)
        logger.info('Dump file location: {}'.format(dest_file))
