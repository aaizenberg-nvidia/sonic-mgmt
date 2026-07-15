import contextlib
import logging
import shlex
from functools import partial

import pytest
import retry
from devts.infra.tools.connection_tools.pexpect_serial_engine import PexpectSerialEngine

from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from ngts.ngts_types import CleanUpT, DevicesT, EnginesT, TopologyT
from ngts.nvos_constants.constants_nvos import ActionConsts, ClusterAppsLogLevels, NvosConst, OutputFormat, SystemConsts
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import NvosConsts
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.SecureBootTool import SecureBootTool
from ngts.nvos_tools.infra.Tools import Tools
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.nvos_tools.nmx.Cluster import Cluster
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.cluster.cluster_consts import ClusterConsts
from ngts.tests_nvos.cluster.cluster_tools import ClusterTools
from ngts.tests_nvos.constants import MINUTE
from ngts.tests_nvos.general.security.security_test_tools.tool_classes.AuthVerifier import SshAuthVerifier
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger(__name__)

NMX_CONTROLLER = 'nmx-controller'
NMX_TELEMETRY = 'nmx-telemetry'


@pytest.fixture(autouse=True, scope='module')
def _disable_devts_config_save(engines: EnginesT) -> None:
    with engines.dut.disable_config_save():
        yield


@pytest.fixture(autouse=True)
def _ztp_cleanup(engines: EnginesT) -> None:
    yield
    System().ztp.action_abort_ztp().verify_result()
    engines.dut.run_cmd('sudo rm -f /host/ztp/ztp_data_local.json')


@pytest.mark.ztp
@pytest.mark.system
def test_show_ztp_command(engines: EnginesT, serial_engine: PexpectSerialEngine):
    """
    Test flow:
        1. Check default ztp values
        2. Validate ztp logs with nv show system log command and with serial connection
        3. Check ztp log file exist, ztp logs inside
        4. Config save and check ztp go to inactive
        5. Config save enabled, verify changes
        6. Ztp unset and verify values
    """
    system = System(None)
    _run_system_ztp_with_empty_config(engines, system)

    with allure.step("Wait until ztp values fields changed"):
        _wait_until_ztp_values_fields_changed(system, SystemConsts.ZTP_OUTPUT_FIELDS, SystemConsts.ZTP_DEFAULT_VALUES, tries=5, delay=2)

    with allure.step("Run nv show system log command and check ztp logs inside"):
        show_output = system.log.file.show_log(param="| grep ztp")
        ValidationTool.verify_expected_output(show_output, 'ztp').verify_result()

    with allure.step("Run nv show system log command and check ztp logs inside"):
        serial_engine.serial_engine.expect("ztp", timeout=30)

    with allure.step("Check ztp log file exist"):
        wc_output = engines.dut.run_cmd(f'wc -c {SystemConsts.ZTP_DEFAULT_LOG_FILE}')
        assert SystemConsts.ZTP_DEFAULT_LOG_FILE in wc_output, 'ZTP log file not exist'

    with allure.step("Save configuration"):
        system.security.password_hardening.set(SystemConsts.USERNAME_PASSWORD_HARDENING_STATE, SystemConsts.USER_STATE_DISABLED)
        NvueGeneralCli.apply_config(engines.dut)
        NvueGeneralCli.save_config(engines.dut)

    with allure.step("Wait until ztp values fields changed"):
        _wait_until_ztp_values_fields_changed(system, SystemConsts.ZTP_OUTPUT_FIELDS, SystemConsts.ZTP_AFTER_CONFIG_SAVE_VALUES)

    with allure.step("Run nv set system ztp config-save enabled"):
        system.ztp.set('config-save', 'enabled').verify_result(True)
        NvueGeneralCli.apply_config(engines.dut)

    with allure.step("Run show ztp after save and verify values"):
        system_ztp_output = system.ztp.parse_show()

        with allure.step("Verify config save value"):
            ValidationTool.verify_field_value_in_output(system_ztp_output, 'config-save', 'enabled').verify_result()

    with allure.step("Run nv unset system ztp"):
        system.ztp.unset().verify_result(True)
        NvueGeneralCli.apply_config(engines.dut)

    _run_system_ztp_with_empty_config(engines, system)

    with allure.step("Wait until ztp values fields changed"):
        _wait_until_ztp_values_fields_changed(system, SystemConsts.ZTP_OUTPUT_FIELDS, SystemConsts.ZTP_DEFAULT_VALUES)


@pytest.mark.ztp
@pytest.mark.system
def test_ztp_json(engines: EnginesT):
    """
    Test flow:
        1. Check default ztp values
        2. Download bad format json and check error in the log
        3. Run positive json file and check status changed
        4. Run negative ping and check ztp failed
        5. Run json with halt-on-failure param
        6. Run json with restart-on-failure param
    """
    system = System(None)

    _run_system_ztp_with_empty_config(engines, system)
    _wait_until_ztp_values_fields_changed(system, SystemConsts.ZTP_OUTPUT_FIELDS, SystemConsts.ZTP_DEFAULT_VALUES, tries=5, delay=2)

    with allure.step("Download dummy json file"):
        system.ztp.action_abort_ztp().verify_result()
        _download_ztp_json_config(engines, SystemConsts.DUMMY_JSON)

        with allure.step("Run nv action run system ztp"):
            _apply_empty_config_and_save(engines)
            system.ztp.action_run_ztp().verify_result()

        with allure.step("Validate ztp error in ztp log file"):
            _validate_ztp_log_file(
                engines, string_to_validate='occurred while processing ZTP JSON file /host/ztp/ztp_data_local.json')

    with allure.step("Download positive json file"):
        system.ztp.action_abort_ztp().verify_result()
        _download_ztp_json_config(engines, SystemConsts.POSITIVE_JSON)

        with allure.step("Run nv action run system ztp"):
            _apply_empty_config_and_save(engines)
            system.ztp.action_run_ztp().verify_result()

        with allure.step("Run show ztp and verify default values"):
            _wait_until_ztp_status(system, SystemConsts.ZTP_STATUS_RUNNING)
            _wait_until_ztp_status(system, SystemConsts.ZTP_STATUS_SUCCESS)

    with allure.step("Download negative ping json file"):
        system.ztp.action_abort_ztp().verify_result()
        _download_ztp_json_config(engines, SystemConsts.NEGATIVE_PING_JSON)

        _run_system_ztp_with_empty_config(engines, system)

        with allure.step("Run show ztp and verify default values"):
            _wait_until_ztp_status(system, SystemConsts.ZTP_STATUS_FAILED)

    with allure.step("Download json file with halt on failure parameter"):
        system.ztp.action_abort_ztp().verify_result()
        _download_ztp_json_config(engines, SystemConsts.NEGATIVE_HALT_ON_FAILURE_JSON)

        _run_system_ztp_with_empty_config(engines, system)

        with allure.step("Run show ztp and verify default values"):
            _wait_until_ztp_status(system, SystemConsts.ZTP_STATUS_RUNNING)
            _wait_until_ztp_step_status(system, '01-connectivity-check', SystemConsts.ZTP_STATUS_FAILED)
            _wait_until_ztp_status(system, SystemConsts.ZTP_STATUS_FAILED)

    with allure.step("Download json file with restart on failure parameter"):
        system.ztp.action_abort_ztp().verify_result()
        _download_ztp_json_config(engines, SystemConsts.NEGATIVE_RESTART_ON_FAILURE_JSON)

        _run_system_ztp_with_empty_config(engines, system)

        with allure.step("Run show ztp and verify default values"):
            _wait_until_ztp_status(system, SystemConsts.ZTP_STATUS_FAILED)

        with allure.step("Run nv show system log command and check ztp logs inside"):
            show_output = system.log.file.show_log(param="| grep ztp")
            ValidationTool.verify_expected_output(show_output, 'Waiting for 300 seconds before restarting ZTP').verify_result()

    with allure.step("Run nv abort run system ztp and delete json file"):
        system.ztp.action_abort_ztp().verify_result()
        engines.dut.run_cmd('sudo rm -f /host/ztp/ztp_data_local.json')


@pytest.mark.ztp
@pytest.mark.system
def test_ztp_image(engines: EnginesT, devices: DevicesT):
    """
    Test flow:
        1. Check default ztp values
        2. Apply image json file
        3. Verify image installed
        4. Verify image uninstalled
    """
    system = System(None)

    _run_system_ztp_with_empty_config(engines, system)
    _wait_until_ztp_values_fields_changed(system, SystemConsts.ZTP_OUTPUT_FIELDS, SystemConsts.ZTP_DEFAULT_VALUES, tries=5, delay=2)

    with allure.step("Download image json file"):
        image_json = devices.dut.ztp_dev_json if SecureBootTool.is_dev_system(TestToolkit.engines.dut) else devices.dut.ztp_prod_json
        system.ztp.action_abort_ztp().verify_result()
        _download_ztp_json_config(engines, image_json)

        _run_system_ztp_with_empty_config(engines, system)

        with allure.step("Check ztp status for image test"):
            with allure.step("Check ztp status for download and install image"):
                _wait_until_ztp_step_status(system, '01-image', SystemConsts.ZTP_STATUS_SUCCESS, tries=100, delay=5)
                output_dictionary = system.image.parse_show()
                assert output_dictionary['partition1']['build-id'] != output_dictionary['partition2']['build-id'], 'Image not installed'

            with allure.step("Check ztp status for uninstall image"):
                _wait_until_ztp_step_status(system, '02-image', SystemConsts.ZTP_STATUS_SUCCESS)
                output_dictionary = system.image.parse_show()
                assert output_dictionary['partition2']['build-id'] is None, 'Image not uninstalled'


def _restore_port_up(selected_port: Port) -> None:
    with allure.step(f"Toggle port {selected_port.name} back up"):
        selected_port.interface.link.state.set(op_param_name=NvosConsts.LINK_STATE_UP, apply=True).verify_result()
        selected_port.interface.wait_for_port_state(state=NvosConsts.LINK_STATE_UP).verify_result()


@pytest.mark.ztp
@pytest.mark.system
def test_ztp_startup_file_commands_list(engines: EnginesT, register_cleanup: CleanUpT):
    """
    Test flow:
        1. Check default ztp values
        2. Download json file with dummy ip inside
        3. Set description to random interface
        4. Apply json file with clear config false and check interface description exist
        5. Apply json file with config save true
        6. Apply json file with clear config true and check interface description empty
    """
    system = System(None)
    empty_description = ""
    abcd_description = "abcd"

    # Try to find a DOWN port first, if none available use an UP port
    result = Tools.RandomizationTool.select_random_port(requested_ports_state=NvosConsts.LINK_STATE_DOWN)
    selected_port: Port
    if result.result:
        selected_port = result.get_returned_value()
    else:
        result.ignore_result()  # It's acceptable that no DOWN ports were found
        # No DOWN port available, select an UP port and toggle it down
        selected_port = Tools.RandomizationTool.select_random_port(requested_ports_state=NvosConsts.LINK_STATE_UP).get_returned_value()
        with allure.step(f"Toggle port {selected_port.name} down for test"):
            selected_port.interface.link.state.set(op_param_name=NvosConsts.LINK_STATE_DOWN, apply=True).verify_result()
            selected_port.interface.wait_for_port_state(state=NvosConsts.LINK_STATE_DOWN).verify_result()
            # NOTE: ensure that at the end of the test, the port will be back up
            register_cleanup(partial(_restore_port_up, selected_port))

    selected_port.update_output_dictionary()
    TestToolkit.update_tested_ports([selected_port])

    _run_system_ztp_with_empty_config(engines, system)
    _wait_until_ztp_values_fields_changed(system, SystemConsts.ZTP_OUTPUT_FIELDS, SystemConsts.ZTP_DEFAULT_VALUES, tries=5, delay=2)

    with allure.step("Download json file with wrong ip"):
        system.ztp.action_abort_ztp().verify_result()
        _download_ztp_json_config(engines, SystemConsts.STARTUP_FILE_WRONG_IP)

        _run_system_ztp_with_empty_config(engines, system)

        with allure.step("Check ztp status"):
            _wait_until_ztp_step_status(system, '01-startup-file', SystemConsts.ZTP_STATUS_FAILED)

    with allure.step('Run show command on selected port and verify that description field is set'):
        selected_port.interface.set(NvosConst.DESCRIPTION, abcd_description, apply=True).verify_result()
        selected_port.update_output_dictionary()
        _validate_interface_description_field(selected_port, abcd_description, True)

    with allure.step("Download clear config false json file"):
        system.ztp.action_abort_ztp().verify_result()
        _download_ztp_json_config(engines, SystemConsts.STARTUP_FILE_CLEAR_CONFIG_FALSE)

        with allure.step("Run nv action run system ztp"):
            _run_system_ztp_with_empty_config(engines, system)

            with allure.step("Check ztp status"):
                _wait_until_ztp_step_status(system, '01-startup-file', SystemConsts.ZTP_STATUS_SUCCESS)

            with allure.step('Check interface description exist'):
                selected_port.update_output_dictionary()
                _validate_interface_description_field(selected_port, abcd_description, True)

    with allure.step("Download config save true startup json file"):
        system.ztp.action_abort_ztp().verify_result()
        _download_ztp_json_config(engines, SystemConsts.STARTUP_FILE_CLEAR_CONFIG_TRUE)

        with allure.step("Run nv action run system ztp"):
            _run_system_ztp_with_empty_config(engines, system)

            with allure.step("Check ztp status"):
                _wait_until_ztp_step_status(system, '01-startup-file', SystemConsts.ZTP_STATUS_SUCCESS)

            with allure.step('Check interface description exist'):
                selected_port.update_output_dictionary()
                _validate_interface_description_field(selected_port, empty_description, False)

    with allure.step("Download clear config true startup json file"):
        system.ztp.action_abort_ztp().verify_result()
        _download_ztp_json_config(engines, SystemConsts.STARTUP_FILE_SAVE_CONFIG_TRUE)

        with allure.step("Run nv action run system ztp"):
            _run_system_ztp_with_empty_config(engines, system)

            with allure.step("Check ztp status"):
                _wait_until_ztp_step_status(system, '01-startup-file', SystemConsts.ZTP_STATUS_SUCCESS)

    with allure.step("Download clear config true startup json file"):
        system.ztp.action_abort_ztp().verify_result()
        _download_ztp_json_config(engines, SystemConsts.STARTUP_FILE_INTERACTIVE_COMMANDS)

        with allure.step("Run nv action run system ztp"):
            _run_system_ztp_with_empty_config(engines, system)

            with allure.step("Check ztp status"):
                _wait_until_ztp_step_status(system, '01-startup-file', SystemConsts.ZTP_STATUS_FAILED)


@pytest.mark.ztp
@pytest.mark.system
def test_ztp_connectivity_check(engines: EnginesT):
    """
    Test flow:
        1. Check default values for ztp
        2. Apply json file with ipv4 and ipv6
        3. Apply json file with dummy ipv4, ipv6
    """
    system = System(None)

    with allure.step("Run nv action run system ztp"):
        _run_system_ztp_with_empty_config(engines, system)
        _wait_until_ztp_values_fields_changed(system, SystemConsts.ZTP_OUTPUT_FIELDS, SystemConsts.ZTP_DEFAULT_VALUES, tries=5, delay=2)

    with allure.step("Download ping ipv4 and ipv6 json file"):
        system.ztp.action_abort_ztp().verify_result()
        _download_ztp_json_config(engines, SystemConsts.CONNECTIVITY_IPV4_IPV6)

        with allure.step("Run nv action run system ztp"):
            _apply_empty_config_and_save(engines)
            system.ztp.action_run_ztp().verify_result()

            with allure.step("Check ztp status"):
                _wait_until_ztp_status(system, SystemConsts.ZTP_STATUS_SUCCESS)
                _wait_until_ztp_step_status(system, '01-connectivity-check', SystemConsts.ZTP_STATUS_SUCCESS)

    with allure.step("Download negative ip json file"):
        system.ztp.action_abort_ztp().verify_result()
        _download_ztp_json_config(engines, SystemConsts.NEGATIVE_CONNECTIVITY)

        with allure.step("Run nv action run system ztp"):
            _apply_empty_config_and_save(engines)
            system.ztp.action_run_ztp().verify_result()

            with allure.step("Check ztp status"):
                _wait_until_ztp_status(system, SystemConsts.ZTP_STATUS_RUNNING)
                _wait_until_ztp_step_status(system, '01-connectivity-check', SystemConsts.ZTP_STATUS_FAILED)
                _wait_until_ztp_step_status(system, '02-commands-list', SystemConsts.ZTP_STATUS_SUCCESS)
                _wait_until_ztp_step_status(system, '03-connectivity-check', SystemConsts.ZTP_STATUS_FAILED)
                _wait_until_ztp_status(system, SystemConsts.ZTP_STATUS_FAILED)


@pytest.mark.ztp
@pytest.mark.system
def test_ztp_json_complex(engines: EnginesT, devices: DevicesT):
    """
    Test flow:
        1. Check default values for ztp
        2. Apply complex json file
        3. Validate all ztp stages
    """
    system = System(None)

    _run_system_ztp_with_empty_config(engines, system)
    _wait_until_ztp_values_fields_changed(system, SystemConsts.ZTP_OUTPUT_FIELDS, SystemConsts.ZTP_DEFAULT_VALUES, tries=5, delay=2)

    with allure.step("Download complex json file"):
        image_json = devices.dut.ztp_complex_dev_json if SecureBootTool.is_dev_system(
            TestToolkit.engines.dut) else devices.dut.ztp_complex_prod_json
        system.ztp.action_abort_ztp().verify_result()
        _download_ztp_json_config(engines, image_json)

        with allure.step("Run nv action run system ztp"):
            _apply_empty_config_and_save(engines)
            system.ztp.action_run_ztp().verify_result()

            with allure.step("Check ztp status"):
                _wait_until_ztp_status(system, SystemConsts.ZTP_STATUS_RUNNING)
                _wait_until_ztp_step_status(system, '01-image', SystemConsts.ZTP_STATUS_SUCCESS, tries=90, delay=3)
                _wait_until_ztp_step_status(system, '02-image', SystemConsts.ZTP_STATUS_SUCCESS)
                _wait_until_ztp_step_status(system, '03-connectivity-check', SystemConsts.ZTP_STATUS_SUCCESS)
                _wait_until_ztp_step_status(system, '04-connectivity-check', SystemConsts.ZTP_STATUS_FAILED)
                _wait_until_ztp_step_status(system, '05-startup-file', SystemConsts.ZTP_STATUS_FAILED)
                _wait_until_ztp_step_status(system, '06-connectivity-check', SystemConsts.ZTP_STATUS_SUCCESS)
                _wait_until_ztp_status(system, SystemConsts.ZTP_STATUS_FAILED)


@pytest.mark.ztp
@pytest.mark.system
def test_ztp_nmx_negative(
    engines: EnginesT,
    devices: DevicesT,
    setup_name: str,
    has_loopbox: bool,
    standalone_system: bool,
    register_cleanup: CleanUpT,
):
    """
    Test flow:
        1. Check default values for ztp
        2. Download and run ztp positive json, when cluster is disabled
        3. Download and run ztp json with no exist file, when cluster is disabled
        4. Start cluster
        5. Download and run ztp json with incorrect commands
        6. Stop nmx-controller
        7. Download and run ztp positive json
        8. Stop nmx-telemetry
        9. Download and run positive ztp json
        10. Cleanup
    """
    system = System(None)
    cluster = Cluster()
    register_cleanup(partial(ClusterTools.stop_cluster, cluster))

    _run_system_ztp_with_empty_config(engines, system)
    _wait_until_ztp_values_fields_changed(system, SystemConsts.ZTP_OUTPUT_FIELDS, SystemConsts.ZTP_DEFAULT_VALUES, tries=5, delay=2)

    shared_kwargs = {
        "engines": engines,
        "system": system,
        "step": '1-nmx-commands-list',
        "step_status_code": SystemConsts.ZTP_STATUS_FAILED,
        "ztp_status_code": SystemConsts.ZTP_STATUS_SUCCESS,
    }

    with allure.step("Download ztp nmx positive command list, cluster disabled"):
        _download_file_and_run_ztp(file=SystemConsts.NMX_POSITIVE_JSON, **shared_kwargs)

    with allure.step("Download ztp nmx not exist file"):
        _run_system_ztp_with_empty_config(engines, system)
        _download_file_and_run_ztp(file=SystemConsts.NMX_NOT_EXIST_FILE_JSON, **shared_kwargs)

    with allure.step("Start cluster"):
        ClusterTools.start_cluster(cluster, setup_name, devices=devices)

        with allure.step("Verify cluster enabled"):
            ClusterTools.validate_cluster_enabled(cluster)

    with allure.step("Download ztp nmx json, with incorrect commands inside"):
        _download_file_and_run_ztp(file=SystemConsts.NMX_BAD_COMMANDS, **shared_kwargs, run_ztp=False)

    with allure.step("Disable nmx controller and run positive ztp"):
        ClusterTools.stop_app(cluster, ClusterConsts.NMX_CONTROLLER)

        with allure.step("Download ztp nmx positive, when nmx controller disabled"):
            _download_file_and_run_ztp(file=SystemConsts.NMX_POSITIVE_JSON, **shared_kwargs, run_ztp=False)

        with allure.step("Enable nmx controller"):
            ClusterTools.start_app(cluster, ClusterConsts.NMX_CONTROLLER, has_loopbox, standalone_system)

    with allure.step("Disable nmx telemetry and run positive ztp"):
        ClusterTools.stop_app(cluster, ClusterConsts.NMX_TELEMETRY)

        with allure.step("Download ztp nmx positive, when nmx controller disabled"):
            _run_system_ztp_with_empty_config(engines, system)
            _download_file_and_run_ztp(file=SystemConsts.NMX_POSITIVE_JSON, **shared_kwargs)


@pytest.mark.ztp
@pytest.mark.system
def test_ztp_nmx_positive(engines: EnginesT, devices: DevicesT, setup_name: str, register_cleanup: CleanUpT):
    """
    Test flow:
        1. Check default values for ztp
        2. Start cluster
        3. Download and run positive ztp json
        4. Verify changes
        5. Cleanup
    """
    output_format = OutputFormat.json
    system = System(None)
    cluster = Cluster()
    register_cleanup(partial(ClusterTools.stop_cluster, cluster))

    _run_system_ztp_with_empty_config(engines, system)
    _wait_until_ztp_values_fields_changed(system, SystemConsts.ZTP_OUTPUT_FIELDS, SystemConsts.ZTP_DEFAULT_VALUES, tries=5, delay=2)

    with allure.step("Start cluster"):
        ClusterTools.start_cluster(cluster, setup_name, devices=devices)

    with allure.step("Download ztp nmx positive command list, cluster enabled"):
        # _run_system_ztp_with_empty_config(engines, system)
        _download_file_and_run_ztp(
            engines,
            system,
            SystemConsts.NMX_POSITIVE_JSON,
            '1-nmx-commands-list',
            SystemConsts.ZTP_STATUS_SUCCESS,
            SystemConsts.ZTP_STATUS_SUCCESS,
            run_ztp=False,
        )

        with allure.step("Verify log level of apps changed"):
            ClusterTools.verify_log_level(ClusterAppsLogLevels.INFO, ClusterConsts.NMX_CONTROLLER, output_format, cluster)
            ClusterTools.verify_log_level(ClusterAppsLogLevels.INFO, ClusterConsts.NMX_TELEMETRY, output_format, cluster)


@pytest.mark.ztp
@pytest.mark.system
@pytest.mark.timeout(6 * MINUTE, func_only=True)
def test_ztp_provisioning_script_negative(engines: EnginesT):
    """
    Test flow:
        1. Check default values for ztp
        2. Apply json file with script with interactive commands
        3. Apply json file with negative provisioning script
        4. Apply json file with script bad extension
        5. Apply json file with loop and timeout
    """
    system = System(None)

    _run_system_ztp_with_empty_config(engines, system)

    _wait_until_ztp_values_fields_changed(system, SystemConsts.ZTP_OUTPUT_FIELDS, SystemConsts.ZTP_DEFAULT_VALUES)

    shared_kwargs = {
        "engines": engines,
        "system": system,
        "step": '01-provisioning-script',
        "step_status_code": SystemConsts.ZTP_STATUS_FAILED,
        "ztp_status_code": SystemConsts.ZTP_STATUS_FAILED,
    }

    with allure.step("Download provisioning script with interactive commands"):
        _download_file_and_run_ztp(file=SystemConsts.SCRIPT_INTERACTIVE, **shared_kwargs)

    with allure.step("Download negative provisioning script"):
        _download_file_and_run_ztp(file=SystemConsts.SCRIPT_NEGATIVE, **shared_kwargs, run_ztp=False)

    with allure.step("Download provisioning with bad extension"):
        _download_file_and_run_ztp(file=SystemConsts.SCRIPT_BAD_FILE, **shared_kwargs, run_ztp=False)

    with allure.step("Download provisioning script with loop and timeout"):
        _download_file_and_run_ztp(file=SystemConsts.SCRIPT_LOOP_TIMEOUT, **shared_kwargs, run_ztp=False)


@pytest.mark.ztp
@pytest.mark.system
def test_ztp_provisioning_script_positive(engines: EnginesT):
    """
    Test flow:
        1. Check default values for ztp
        2. Apply json file with positive script
        3. Apply json file with python script
    """
    system = System(None)

    _run_system_ztp_with_empty_config(engines, system)

    _wait_until_ztp_values_fields_changed(system, SystemConsts.ZTP_OUTPUT_FIELDS, SystemConsts.ZTP_DEFAULT_VALUES)

    with allure.step("Running positive ztp provisioning script"):
        _download_file_and_run_ztp(engines, system, SystemConsts.SCRIPT_POSITIVE, '01-provisioning-script')

    _run_system_ztp_with_empty_config(engines, system)

    with allure.step("Download provisioning python script"):
        _download_file_and_run_ztp(engines, system, SystemConsts.SCRIPT_POSITIVE_PYTHON, '01-provisioning-script')


@pytest.mark.ztp
@pytest.mark.system
def test_ztp_provisioning_script_parameters_positive(engines: EnginesT):
    """
    Verify the provisioning-script "parameters" object is forwarded to the downloaded
    script as named command-line arguments (name value), each value paired with its name.

    Test flow:
        1. Check default values for ztp
        2. Apply json with mixed-type parameters, verify the script received the exact
           argument list: flags verbatim, numbers stringified, booleans lowercase, values
           paired with names, delivered in alphabetical order of names
        3. Apply json with the maximum (20) parameters, verify all are forwarded
    """
    system = System(None)

    _run_system_ztp_with_empty_config(engines, system)
    _wait_until_ztp_values_fields_changed(system, SystemConsts.ZTP_OUTPUT_FIELDS, SystemConsts.ZTP_DEFAULT_VALUES)

    with allure.step("Run provisioning script with mixed-type parameters"):
        system.ztp.action_abort_ztp().verify_result()
        _remove_recorder_output(engines)
        _download_ztp_json_config(engines, SystemConsts.SCRIPT_PARAMS_POSITIVE)
        log_mark = _ztp_log_mark(engines)
        _run_system_ztp_with_empty_config(engines, system)

        with allure.step("Check ztp status"):
            _wait_until_ztp_step_status(system, SystemConsts.PROVISIONING_SCRIPT_STEP, SystemConsts.ZTP_STATUS_SUCCESS)
            _wait_until_ztp_status(system, SystemConsts.ZTP_STATUS_SUCCESS)

        with allure.step("Verify the script received the parameters as named CLI arguments"):
            # Names sorted alphabetically (ZTP serializes the per-stage input.json with sorted keys);
            # numbers stringified; boolean rendered lowercase; single-dash short option passed verbatim.
            # Covers single-dash single-char ("-n", "-s") and double-dash single-char ("--s") forms.
            expected_args = [
                '--nvos_image', '/images/nvos-3.10.bin',
                '--retries', '3',
                '--role', 'leaf',
                '--s', 'alpha',
                '--site', 'dc1',
                '--verbose', 'true',
                '-n', '5',
                '-s', 'bravo',
            ]
            _verify_recorder_args(engines, expected_args)

        with allure.step("Verify ztp.log logs the parameter count + names, but not the values"):
            _validate_param_names_logged(
                engines,
                SystemConsts.PARAMS_LOG_PASSING_POSITIVE,
                forbidden_values=['/images/nvos-3.10.bin', 'dc1', 'leaf', 'true', 'alpha', 'bravo'],
                since_line=log_mark,
            )

    with allure.step(f"Run provisioning script with the maximum ({SystemConsts.ZTP_PARAMS_EXPECTED_MAX}) parameters"):
        system.ztp.action_abort_ztp().verify_result()
        _remove_recorder_output(engines)
        _download_ztp_json_config(engines, SystemConsts.SCRIPT_PARAMS_MAX)
        log_mark = _ztp_log_mark(engines)
        _run_system_ztp_with_empty_config(engines, system)

        with allure.step("Check ztp status"):
            _wait_until_ztp_step_status(system, SystemConsts.PROVISIONING_SCRIPT_STEP, SystemConsts.ZTP_STATUS_SUCCESS)
            _wait_until_ztp_status(system, SystemConsts.ZTP_STATUS_SUCCESS)

        with allure.step(f"Verify all {SystemConsts.ZTP_PARAMS_EXPECTED_MAX} parameters were forwarded, each paired with its name"):
            args = _read_recorder_args(engines)
            expected_token_count = SystemConsts.ZTP_PARAMS_EXPECTED_MAX * 2
            assert len(args) == expected_token_count, \
                f'expected {expected_token_count} tokens ({SystemConsts.ZTP_PARAMS_EXPECTED_MAX} name/value pairs), got {len(args)}: {args}'

        with allure.step("Verify ztp.log logs the parameter count + names, but not the values"):
            _validate_param_names_logged(
                engines,
                SystemConsts.PARAMS_LOG_PASSING_MAX,
                forbidden_values=['v01', 'v20'],
                since_line=log_mark,
            )


@pytest.mark.ztp
@pytest.mark.system
def test_ztp_provisioning_script_parameters_python_positive(engines: EnginesT):
    """
    Verify the "parameters" feature is script-language-agnostic: ZTP forwards each parameter
    as a standard `name value` argv pair, and a Python provisioning script parses them natively
    with argparse. Uses a minimal parameter set (one long flag + one short flag).

    The Python recorder writes back the argparse-PARSED namespace (not raw argv), so an exact
    match proves Python consumed the ZTP-forwarded flags/values correctly.

    Test flow:
        1. Check default values for ztp
        2. Apply json whose url points to the Python (argparse) recorder, with a minimal parameters set
        3. Verify the script parsed the flags/values correctly and ztp.log logs the count + names (no values)
    """
    system = System(None)

    _run_system_ztp_with_empty_config(engines, system)
    _wait_until_ztp_values_fields_changed(system, SystemConsts.ZTP_OUTPUT_FIELDS, SystemConsts.ZTP_DEFAULT_VALUES)

    with allure.step("Run a Python (argparse) provisioning script with a minimal parameters set"):
        system.ztp.action_abort_ztp().verify_result()
        _remove_recorder_output(engines)
        _download_ztp_json_config(engines, SystemConsts.SCRIPT_PARAMS_PYTHON_MIN)
        log_mark = _ztp_log_mark(engines)
        _run_system_ztp_with_empty_config(engines, system)

        with allure.step("Check ztp status"):
            _wait_until_ztp_step_status(system, SystemConsts.PROVISIONING_SCRIPT_STEP, SystemConsts.ZTP_STATUS_SUCCESS)
            _wait_until_ztp_status(system, SystemConsts.ZTP_STATUS_SUCCESS)

        with allure.step("Verify the Python (argparse) script parsed the forwarded flags/values"):
            # Recorder emits the PARSED namespace (name value, names sorted); exact match => argparse consumed them.
            expected_args = [
                '--role', 'leaf',
                '-n', '5',
            ]
            _verify_recorder_args(engines, expected_args)

        with allure.step("Verify ztp.log logs the parameter count + names, but not the values"):
            _validate_param_names_logged(
                engines,
                SystemConsts.PARAMS_LOG_PASSING_PYTHON_MIN,
                forbidden_values=['leaf'],
                since_line=log_mark,
            )


@pytest.mark.ztp
@pytest.mark.system
def test_ztp_provisioning_script_parameters_backward_compat(engines: EnginesT):
    """
    Verify backward compatibility: when the provisioning-script section has no "parameters"
    key, or an empty "parameters" object, the script runs with zero arguments (unchanged
    behavior). Also covers the upgrade contract (existing json without parameters is unaffected).

    Test flow:
        1. Check default values for ztp
        2. Apply json with no parameters key, verify the script ran with zero arguments
        3. Apply json with an empty parameters object, verify the script ran with zero arguments
    """
    system = System(None)

    _run_system_ztp_with_empty_config(engines, system)
    _wait_until_ztp_values_fields_changed(system, SystemConsts.ZTP_OUTPUT_FIELDS, SystemConsts.ZTP_DEFAULT_VALUES)

    backward_compat_cases = [
        (SystemConsts.SCRIPT_PARAMS_NO_KEY, "no parameters key"),
        (SystemConsts.SCRIPT_PARAMS_EMPTY, "empty parameters object"),
    ]

    for fixture, description in backward_compat_cases:
        with allure.step(f"Backward-compat: {description} -> script runs with zero arguments"):
            system.ztp.action_abort_ztp().verify_result()
            _remove_recorder_output(engines)
            _download_ztp_json_config(engines, fixture)
            _run_system_ztp_with_empty_config(engines, system)

            with allure.step("Check ztp status"):
                _wait_until_ztp_step_status(system, SystemConsts.PROVISIONING_SCRIPT_STEP, SystemConsts.ZTP_STATUS_SUCCESS)
                _wait_until_ztp_status(system, SystemConsts.ZTP_STATUS_SUCCESS)

            with allure.step("Verify the script was executed with zero arguments"):
                args = _read_recorder_args(engines)
                assert args == [], f'expected zero arguments for "{description}", got: {args}'


@pytest.mark.ztp
@pytest.mark.system
@pytest.mark.timeout(10 * MINUTE, func_only=True)
def test_ztp_provisioning_script_parameters_negative(engines: EnginesT):
    """
    Verify fail-fast validation of a malformed "parameters" object: the provisioning-script
    stage fails (before downloading the script) for each validation error category, and the
    script is never executed (the recorder output file is never created).

    Test flow (one sub-case per validation error category):
        1. parameters is not a JSON object
        2. a parameter name is empty
        3. a parameter name is not a valid option flag (no dash / whitespace after "--" /
           numeric after "--" / single-dash multi-char)
        4. a parameter value is not a scalar (list, or JSON null)
        5. more than 20 parameters
        6. malformed JSON literal (bare TRUE) -> ZTP JSON load fails before parameter validation
    """
    system = System(None)

    _run_system_ztp_with_empty_config(engines, system)
    _wait_until_ztp_values_fields_changed(system, SystemConsts.ZTP_OUTPUT_FIELDS, SystemConsts.ZTP_DEFAULT_VALUES)

    negative_cases = [
        (SystemConsts.SCRIPT_PARAMS_NOT_OBJECT, "parameters is not a JSON object", SystemConsts.PARAMS_ERR_NOT_OBJECT),
        (SystemConsts.SCRIPT_PARAMS_EMPTY_NAME, "empty parameter name", SystemConsts.PARAMS_ERR_EMPTY_NAME),
        (SystemConsts.SCRIPT_PARAMS_INVALID_FLAG, "parameter name is not a valid option flag", SystemConsts.PARAMS_ERR_INVALID_FLAG),
        (SystemConsts.SCRIPT_PARAMS_WHITESPACE_NAME, 'whitespace-only name after "--"', SystemConsts.PARAMS_ERR_INVALID_FLAG),
        (SystemConsts.SCRIPT_PARAMS_NUMERIC_NAME, 'numeric name after "--" ("--1")', SystemConsts.PARAMS_ERR_INVALID_FLAG),
        (SystemConsts.SCRIPT_PARAMS_SHORT_MULTICHAR, 'single-dash multi-char name ("-ss")', SystemConsts.PARAMS_ERR_INVALID_FLAG),
        (SystemConsts.SCRIPT_PARAMS_NON_SCALAR, "parameter value is not a scalar", SystemConsts.PARAMS_ERR_NON_SCALAR),
        (SystemConsts.SCRIPT_PARAMS_NULL_VALUE, "parameter value is null (non-scalar)", SystemConsts.PARAMS_ERR_NON_SCALAR),
        (SystemConsts.SCRIPT_PARAMS_TOO_MANY, "more than 20 parameters", SystemConsts.PARAMS_ERR_TOO_MANY),
    ]

    for fixture, description, expected_error in negative_cases:
        with allure.step(f"Negative: {description} -> stage fails before download"):
            system.ztp.action_abort_ztp().verify_result()
            _remove_recorder_output(engines)
            _download_ztp_json_config(engines, fixture)
            log_mark = _ztp_log_mark(engines)
            _run_system_ztp_with_empty_config(engines, system)

            with allure.step("Check the provisioning-script stage failed"):
                _wait_until_ztp_step_status(system, SystemConsts.PROVISIONING_SCRIPT_STEP, SystemConsts.ZTP_STATUS_FAILED)
                _wait_until_ztp_status(system, SystemConsts.ZTP_STATUS_FAILED)

            with allure.step("Verify the descriptive failure reason is logged in ztp.log"):
                _validate_ztp_log_file(engines, expected_error, since_line=log_mark)

            with allure.step("Verify fail-fast: the script was never downloaded/executed"):
                _assert_recorder_output_absent(engines)

    # Malformed JSON literal (bare TRUE) fails during ZTP JSON parsing, *before* the parameters
    # validator runs. ZTP does not reach a terminal 'failed' stage here (it logs a load error and
    # retries), so this case is verified via the ztp.log load-failure line + recorder absence.
    with allure.step('Negative: invalid JSON boolean literal (TRUE) -> ZTP JSON load fails, script never runs'):
        system.ztp.action_abort_ztp().verify_result()
        _remove_recorder_output(engines)
        _download_ztp_json_config(engines, SystemConsts.SCRIPT_PARAMS_INVALID_BOOL)
        log_mark = _ztp_log_mark(engines)
        _run_system_ztp_with_empty_config(engines, system)

        with allure.step("Verify ztp.log reports the JSON load failure"):
            _wait_for_ztp_log_line(engines, SystemConsts.PARAMS_ERR_JSON_LOAD, since_line=log_mark)

        with allure.step("Verify fail-fast: the script was never downloaded/executed"):
            _assert_recorder_output_absent(engines)

        # Stop ZTP's retry loop and remove the malformed local JSON so it does not affect later runs.
        system.ztp.action_abort_ztp().verify_result()
        engines.dut.run_cmd('sudo rm -f /host/ztp/ztp_data_local.json')


@pytest.mark.ztp
@pytest.mark.system
def test_ztp_hashed_password(engines: EnginesT, topology_obj: TopologyT):
    """
    Test flow:
        1. Check default values for ztp
        2. Apply json file with hashed password script
        3. Verify that SSH authentication is successful with hashed password
    """
    system = System(None)

    _run_system_ztp_with_empty_config(engines, system)

    _wait_until_ztp_values_fields_changed(system, SystemConsts.ZTP_OUTPUT_FIELDS, SystemConsts.ZTP_DEFAULT_VALUES)

    with allure.step("Running hashed password ztp provisioning script"):
        _download_file_and_run_ztp(engines, system, SystemConsts.SCRIPT_HASHED_PASSWORD, '01-startup-file', SystemConsts.ZTP_STATUS_SUCCESS)

    with allure.step("Verify that SSH authentication is successful with hashed password"):
        with SshAuthVerifier(username='sasha', password='sasha', engines=engines, topology_obj=topology_obj) as ssh_connection:
            ssh_connection.verify_authentication(True)


@pytest.mark.ztp
@pytest.mark.system
def test_ztp_fetch_asic_debug_config(engines: EnginesT):
    """
    Test flow:
        1. Check default values for ztp
        2. Apply json file with asic-debug-config (ZTP fetches YAML from URL in server JSON).
        3. Verify config fetched
    """
    system = System(None)

    _run_system_ztp_with_empty_config(engines, system)

    _wait_until_ztp_values_fields_changed(system, SystemConsts.ZTP_OUTPUT_FIELDS, SystemConsts.ZTP_DEFAULT_VALUES, tries=2, delay=1)

    with allure.step("Running positive ztp asic-debug-fetch script"):
        _download_file_and_run_ztp(engines, system, SystemConsts.SCRIPT_ASIC_DEBUG_CONFIG, '01-fetch-asic-debug-config-file')

    with allure.step("Verify file fetched successfully"):
        output_dictionary = system.asic_debug_config.parse_show('files')
        ValidationTool.verify_expected_output(output_dictionary, SystemConsts.PASS_ASIC_DEBUG_CONFIG_ZTP).verify_result()

    with allure.step("Delete asic-debug-config yaml"):
        system.asic_debug_config.action(ActionConsts.DELETE, additional_params={'files': f'{SystemConsts.PASS_ASIC_DEBUG_CONFIG_ZTP}'})


@retry.retry(Exception, tries=3, delay=2)
def _download_ztp_json_config(engines: EnginesT, json: str = '') -> str:
    engines.dut.run_cmd('sudo rm -f /host/ztp/ztp_data_local.json')
    file_url = f'{SystemConsts.HTTP_SERVER}{SystemConsts.VERIFICATION_ZTP_PATH}{json}'
    return engines.dut.run_cmd(
        f'sudo curl {file_url} -o /host/ztp/ztp_data_local.json',
        validate=True,
    )


def _download_file_and_run_ztp(
    engines: EnginesT,
    system: System,
    file: str = '',
    step: str = '',
    step_status_code: SystemConsts = SystemConsts.ZTP_STATUS_SUCCESS,
    ztp_status_code: SystemConsts = SystemConsts.ZTP_STATUS_SUCCESS,
    run_ztp: bool = True,
) -> None:
    with allure.step("Download json file"):
        _download_ztp_json_config(engines, file)

    run_ztp_step = allure.step("Run nv action run system ztp") if run_ztp else contextlib.nullcontext()
    with run_ztp_step:
        if run_ztp:
            system.ztp.action_run_ztp().verify_result()

        with allure.step("Check ztp status"):
            _wait_until_ztp_step_status(system, step, step_status_code)
            _wait_until_ztp_status(system, ztp_status_code)


def _apply_empty_config_and_save(engines: EnginesT) -> None:
    """Apply empty config and save so next ZTP run is not bypassed (config already applied/saved)."""
    engines.dut.disconnect()  # force reconnect to the dut
    NvueGeneralCli.apply_config(engine=engines.dut, rev_id='empty', option='-y')
    NvueGeneralCli.save_config(engine=engines.dut)


def _run_system_ztp_with_empty_config(engines: EnginesT, system: System) -> None:
    with allure.step("Run nv action run system ztp"):
        _apply_empty_config_and_save(engines)
        system.ztp.action_run_ztp().verify_result()


def _validate_ztp_log_file(engines: EnginesT, string_to_validate: str = '', since_line: int = 0) -> None:
    # Only search lines appended after `since_line` so stale entries from earlier runs can't match.
    # Use fixed-string grep (-F) with an end-of-options guard (--) and a shell-quoted pattern so
    # regex/shell metacharacters in the log string (e.g. '.', '[', '(', quotes) match literally and
    # never break the remote command. The Python `in` assertion below is the authoritative check.
    pattern = shlex.quote(string_to_validate)
    output = engines.dut.run_cmd(
        f'tail -n +{since_line + 1} {SystemConsts.ZTP_DEFAULT_LOG_FILE} | grep -F -- {pattern}')
    assert string_to_validate in output, 'String not in ztp log'


def _ztp_log_mark(engines: EnginesT) -> int:
    """Snapshot the current ztp.log line count, so later greps can be scoped to lines added after now."""
    output = engines.dut.run_cmd(f'cat {SystemConsts.ZTP_DEFAULT_LOG_FILE} 2>/dev/null | wc -l').strip()
    return int(output) if output.isdigit() else 0


def _wait_for_ztp_log_line(engines: EnginesT, string_to_validate: str = '', since_line: int = 0,
                           tries: int = 30, delay: int = 2) -> None:
    """Poll ztp.log (scoped to lines after `since_line`) until `string_to_validate` appears."""
    @retry.retry(AssertionError, tries=tries, delay=delay)
    def _inner():
        _validate_ztp_log_file(engines, string_to_validate, since_line=since_line)

    with allure.step(f"Waiting for ztp.log to contain {string_to_validate!r}"):
        _inner()


def _validate_param_names_logged(engines: EnginesT, expected_names_line: str, forbidden_values: list[str] = None,
                                 since_line: int = 0) -> None:
    """Assert /var/log/ztp.log logs the parameter count + names, and (optionally) that no values leaked."""
    with allure.step("Verify parameter count+names logged (no values) in ztp.log"):
        # Only search lines appended after `since_line` so stale entries from earlier runs can't match.
        # Fixed-string grep (-F) with an end-of-options guard (--) and a shell-quoted pattern, so
        # metacharacters/quotes in the expected line match literally and never break the remote command.
        pattern = shlex.quote(expected_names_line)
        log_line = engines.dut.run_cmd(
            f'tail -n +{since_line + 1} {SystemConsts.ZTP_DEFAULT_LOG_FILE} | grep -F -- {pattern} | tail -n 1')
        assert expected_names_line in log_line, \
            f'expected parameter info log not found in {SystemConsts.ZTP_DEFAULT_LOG_FILE}: {expected_names_line!r}'
        for value in (forbidden_values or []):
            assert value not in log_line, \
                f'parameter VALUE {value!r} leaked into the log line (must log names only): {log_line!r}'


def _remove_recorder_output(engines: EnginesT) -> None:
    """Remove the recorder output file and confirm it is gone, so each run starts from a known-clean
    state. Verifying the removal prevents a stale file (e.g. from a crashed/aborted prior run) from
    being misread as the current run's output - the determinism risk with a fixed output path."""
    engines.dut.run_cmd(f'sudo rm -f {SystemConsts.ZTP_PARAMS_RECORDER_OUTPUT}')
    check = engines.dut.run_cmd(
        f'test -e {SystemConsts.ZTP_PARAMS_RECORDER_OUTPUT} && echo EXISTS || echo MISSING')
    assert 'MISSING' in check, \
        f'failed to clean recorder output {SystemConsts.ZTP_PARAMS_RECORDER_OUTPUT} before the run; ' \
        f'a stale file would make results non-deterministic'


def _read_recorder_args(engines: EnginesT) -> list[str]:
    """Read the argv tokens the recorder provisioning script received (one token per line)."""
    output = engines.dut.run_cmd(f'sudo cat {SystemConsts.ZTP_PARAMS_RECORDER_OUTPUT}').strip()
    return output.splitlines() if output else []


def _verify_recorder_args(engines: EnginesT, expected_args: list[str]) -> None:
    with allure.step("Read recorder output and compare with expected arguments"):
        actual_args = _read_recorder_args(engines)
        allure.attach("recorder-args", str(actual_args))
        assert actual_args == expected_args, \
            f'script received unexpected arguments.\nexpected: {expected_args}\nactual:   {actual_args}'


def _assert_recorder_output_absent(engines: EnginesT) -> None:
    """Assert the recorder output file was never created (fail-fast: script never executed)."""
    output = engines.dut.run_cmd(f'test -f {SystemConsts.ZTP_PARAMS_RECORDER_OUTPUT} && echo EXISTS || echo MISSING')
    assert 'MISSING' in output, \
        f'{SystemConsts.ZTP_PARAMS_RECORDER_OUTPUT} exists -> the script was executed despite invalid parameters (fail-fast violated)'


@retry.retry(Exception, tries=30, delay=2)
def _wait_until_ztp_status(system: System, ztp_status: str = '') -> None:
    with allure.step(f"Waiting for ztp status changed to status {ztp_status}"):
        ztp_output = system.ztp.parse_show()
        allure.attach("ztp-output.json", ztp_output)
        assert 'status' in ztp_output, f"'status' not in ztp output: {ztp_output}"
        assert ztp_output['status'] == ztp_status, f'ztp status not changed to {ztp_status}'


def _wait_until_ztp_step_status(system: System, ztp_step: str = '', ztp_status: str = '', tries: int = 30, delay: int = 2) -> None:
    @retry.retry(AssertionError, tries=tries, delay=delay)
    def _retry_decorator(system_obj: System, ztp_step_name: str = '', ztp_status_name: str = ''):
        with allure.step(f"Waiting for ztp status changed to status {ztp_status_name}"):
            ztp_output = system_obj.ztp.parse_show()
            allure.attach("ztp-output.json", ztp_output)
            assert 'stage' in ztp_output, f"'stage' not in ztp output: {ztp_output}"
            assert ztp_step_name in ztp_output['stage'], f"{ztp_step_name!r} not in ztp output: {ztp_output['stage']}"
            assert 'status' in ztp_output['stage'][ztp_step_name], f"'status' not in ztp output: {ztp_output['stage'][ztp_step_name]}"

            current_status = ztp_output['stage'][ztp_step_name]['status']
            final_statuses = (SystemConsts.ZTP_STATUS_SUCCESS, SystemConsts.ZTP_STATUS_FAILED)

            if current_status in final_statuses and current_status != ztp_status_name:
                # Stop retrying: final ZTP states will not change anymore.
                raise RuntimeError(f'expected ztp status is {ztp_status_name!r}, but got {current_status!r}')

            assert current_status == ztp_status_name, f'ztp status not changed to {ztp_status_name}'

    _retry_decorator(system, ztp_step, ztp_status)


def _validate_interface_description_field(selected_port: Port, description_value: str, should_be_equal: bool = True) -> None:
    with allure.step('Check that interface description field matches the expected value'):
        output_dictionary = selected_port.show_output_dictionary
        if NvosConst.DESCRIPTION in output_dictionary.keys():
            Tools.ValidationTool.verify_field_value_in_output(output_dictionary, NvosConst.DESCRIPTION,
                                                              description_value).verify_result(should_be_equal)


def _wait_until_ztp_values_fields_changed(
    system: System,
    ztp_output_fields: list[str],
    ztp_output_values: list[str],
    tries: int = 30,
    delay: int = 3,
) -> None:
    @retry.retry(Exception, tries=tries, delay=delay)
    def _retry_decorator(system_obj: System, ztp_step_name: str = '', ztp_status_name: str = ''):
        with allure.step("Run show ztp and verify default values"):
            system_ztp_output = system_obj.ztp.parse_show()
            allure.attach("ztp-output.json", system_ztp_output)

        with allure.step("Verify default values and fields"):
            ValidationTool.validate_fields_values_in_output(ztp_step_name, ztp_status_name, system_ztp_output).verify_result()

    _retry_decorator(system, ztp_output_fields, ztp_output_values)
