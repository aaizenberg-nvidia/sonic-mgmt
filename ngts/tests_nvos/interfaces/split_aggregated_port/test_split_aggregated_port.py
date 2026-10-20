import pytest
import time

from ngts.nvos_constants.constants_nvos import DatabaseConst
from ngts.nvos_tools.infra.Fae import Fae
from ngts.nvos_tools.infra.MultiPlanarTool import MultiPlanarTool
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.nvos_constants.constants_nvos import SystemConsts
from ngts.nvos_tools.system.System import System
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import *
from ngts.nvos_tools.infra.Tools import Tools
from ngts.tools.test_utils.allure_utils import step as allure_step
from ngts.nvos_constants.constants_nvos import LinkDetectionConsts


logger = logging.getLogger()


@pytest.mark.interfaces
@pytest.mark.multiplanar
@pytest.mark.simx_xdr
@pytest.mark.system_profile_cleanup
def test_interface_aggregated_port_split(engines, devices, random_api, players, interfaces, start_sm, setup_name):
    """
    validate all show fae interface commands.

    Test flow:
    1. Change platform json
    2. Check aggregated port lanes
    3. Change system profile to breakout mode
    4. Split a port
    5. Do set command for a port
    6. Redis validation
    7. Run traffic and check counters
    8. Clear counters and validate
    9. Unset a port
    """

    system = System(None)

    with allure_step("Select random aggregated port and validate planarized ports"):
        selected_fae_aggregated_port = MultiPlanarTool.select_random_aggregated_port(devices.dut, setup_name)
        fae_interface_output = OutputParsingTool.parse_show_interface_output_to_dictionary(
            selected_fae_aggregated_port.port.interface.show()).get_returned_value()
        # [TBD] doesn't work on simulation, need to verify on real system
        # ValidationTool.compare_values(fae_interface_output['planarized-ports'],
        #                               devices.dut.aggregated_port_planarized_ports).verify_result()

    with allure_step('Change system profile to breakout'):
        with allure_step('Verify changed values'):
            system_profile_output = OutputParsingTool.parse_json_str_to_dictionary(system.profile.show()) \
                .get_returned_value()
            values_to_verify = [SystemConsts.PROFILE_STATE_ENABLED, 1792,
                                SystemConsts.PROFILE_STATE_ENABLED, SystemConsts.PROFILE_STATE_DISABLED,
                                SystemConsts.DEFAULT_NUM_SWIDS]
            ValidationTool.validate_fields_values_in_output(SystemConsts.PROFILE_OUTPUT_FIELDS,
                                                            values_to_verify,
                                                            system_profile_output).verify_result()
            logging.info("All expected values were found")

    with allure_step("Check traffic port up"):
        # Need to provide some good way to find loopback ports. Is it ibnetdiscover?
        split_ports = MultiPlanarTool._get_split_ports(port="swA11p1")

    with allure_step("Split splitter port"):
        parent_port = split_ports[0]
        parent_port.interface.link.set(op_param_name=LinkDetectionConsts.CONNECTION_MODE,
                                       op_param_value=LinkDetectionConsts.CONNECTION_MODE_XDR,
                                       apply=True, ask_for_confirmation=True).verify_result()
        parent_port.interface.link.set(op_param_name='breakout',
                                       op_param_value=IbInterfaceConsts.LINK_BREAKOUT_XDR,
                                       apply=True, ask_for_confirmation=True).verify_result()

    with allure_step("Get split ports"):
        child_ports = MultiPlanarTool._get_split_child_ports(parent_port)

    with allure_step("Validate split port going to up"):
        child_port = Port(name=devices.dut.child_aggregated_port)
        Port.wait_for_port_state(child_port, "up")

    with allure_step("Change mtu on child port and check changes"):
        child_ports[0].interface.link.set(op_param_name='mtu', op_param_value=512, apply=True,
                                          ask_for_confirmation=True).verify_result()
        time.sleep(5)

        with allure_step("Verify changed values on child port"):
            child_ports[0].interface.wait_for_port_state(NvosConsts.LINK_STATE_UP, sleep_time=8).verify_result()
            output_dictionary = Tools.OutputParsingTool.parse_show_interface_link_output_to_dictionary(
                child_ports[0].interface.link.show()).get_returned_value()
            Tools.ValidationTool.verify_field_value_in_output(output_dictionary=output_dictionary,
                                                              field_name='mtu',
                                                              expected_value='512').verify_result()

    with allure_step("Redis validation"):
        redis_cli_output = Tools.DatabaseTool.sonic_db_cli_hget(engine=engines.dut, asic="",
                                                                db_name=DatabaseConst.CONFIG_DB_NAME,
                                                                db_config="IB_PORT\\|{0}".format('Infiniband72'),
                                                                param="planarized_ports")
        assert redis_cli_output != 2, "On split port planarized ports not 2"

        redis_cli_output = Tools.DatabaseTool.sonic_db_cli_hget(engine=engines.dut, asic="",
                                                                db_name=DatabaseConst.CONFIG_DB_NAME,
                                                                db_config="IB_PORT\\|{0}".format('Infiniband80'),
                                                                param="planarized_ports")
        assert redis_cli_output != 2, "On split port planarized ports not 2"

        redis_cli_output = Tools.DatabaseTool.sonic_db_cli_hget(engine=engines.dut, asic="",
                                                                db_name=DatabaseConst.CONFIG_DB_NAME,
                                                                db_config="IB_PORT\\|{0}".format('Infiniband72'),
                                                                param="planarized_ports")
        assert redis_cli_output != '(empty array)', "Planarized port not exist in redis db"

    with allure_step("Run traffic and check counters"):
        Tools.TrafficGeneratorTool.send_ib_traffic(players, interfaces, setup_name, True).verify_result()

        with allure_step("Check counters before split, should be not 0"):
            output_dictionary = Tools.OutputParsingTool.parse_show_interface_stats_output_to_dictionary(
                child_ports[0].interface.counters.show()).get_returned_value()
            assert output_dictionary[IbInterfaceConsts.LINK_STATS_IN_PKTS] == output_dictionary[
                IbInterfaceConsts.LINK_STATS_OUT_PKTS]

    with allure_step("Clear counters and validate"):
        child_ports[0].interface.action_clear_counter_for_all_interfaces(engines.dut).verify_result()

        with allure_step("Check counters after clear counters, should be 0"):
            output_dictionary = Tools.OutputParsingTool.parse_show_interface_stats_output_to_dictionary(
                child_ports[0].interface.counters.show()).get_returned_value()
            assert output_dictionary[IbInterfaceConsts.LINK_STATS_IN_PKTS] == output_dictionary[
                IbInterfaceConsts.LINK_STATS_OUT_PKTS]
