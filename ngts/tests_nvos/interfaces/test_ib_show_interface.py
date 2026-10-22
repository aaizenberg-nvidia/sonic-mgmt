import json
import logging
import re
import random
import time
import os

from ngts.tools.test_utils import allure_utils as allure
import pytest
from devts.infra.tools.connection_tools.linux_ssh_engine import LinuxSshEngine
from ngts.nvos_tools.Devices.IbDevice import JulietNonScaleoutSwitch
from ngts.nvos_tools.ib.InterfaceConfiguration.Interface import Interface
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import NvosConsts, IbInterfaceConsts, NvlInterfaceConsts
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.RandomizationTool import RandomizationTool
from ngts.nvos_tools.infra.Tools import Tools
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.nvos_constants.constants_nvos import ApiType, NvosConst, PlatformConsts
from ngts.nvos_tools.infra.Fae import Fae
from ngts.nvos_tools.infra.ResultObj import ResultObj
from ngts.tools.test_utils import allure_utils as allure
from ngts.tests_nvos.general.security.conftest import create_ssh_login_engine
from ngts.tests_nvos.general.telemetry.ib.helpers import get_plane_port_oid
from devts.infra.tools.general_constants.constants import DefaultConnectionValues
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.PhotonicsTool import PhotonicsTool
from ngts.nvos_tools.system.System import System

logger = logging.getLogger()


@pytest.mark.ib_interfaces
@pytest.mark.nvos_ci
@pytest.mark.ib
@pytest.mark.air
@pytest.mark.simx
def test_ib_show_interface(engines, devices, random_api):
    """
    Run show interface command and verify the required fields are exist
    command: nv show interface <name>

    flow:
    1. Select a random port (status of which is up)
    2. Run 'nv show interface <name>' on selected port
    3. Verify the required fields are presented in the output
    """
    try:
        selected_port = Tools.RandomizationTool.select_random_port(requested_ports_type=devices.dut.switch_type.lower()).get_returned_value()
    except Exception:
        pytest.skip("Device does not have any connectivity")

    TestToolkit.update_tested_ports([selected_port])

    with allure.step('Run show command on selected port and verify that each field has an appropriate '
                     'value according to the state of the port'):
        with allure.step('run show interface'):
            output_dictionary = Tools.OutputParsingTool.parse_show_interface_output_to_dictionary(
                selected_port.interface.show()).get_returned_value()
        with allure.step('validate fields values'):
            validate_one_port_show_output(output_dictionary, devices.dut.switch_type.lower(),
                                          devices.dut.asic_type in NvosConst.QTM3_AND_NEWER,
                                          is_cpo=hasattr(devices.dut, 'els_list'),
                                          dut=devices.dut, port_name=selected_port.name)

    with allure.step(f'Check interface primary ASIC for port {selected_port.name}'):
        fae = Fae(port_name=selected_port.name)
        output_dictionary = Tools.OutputParsingTool.parse_show_interface_output_to_dictionary(
            fae.interface.show()).get_returned_value()
        assert IbInterfaceConsts.PRIMARY_ASIC in output_dictionary.keys(), \
            f"{IbInterfaceConsts.PRIMARY_ASIC} field not found for port {selected_port.name}"
        assert int(output_dictionary[IbInterfaceConsts.PRIMARY_ASIC]) in range(0, devices.dut.asic_amount), \
            f"IbInterfaceConsts.PRIMARY_ASIC should be in range of 0-{devices.dut.asic_amount}, " \
            f"but for port {selected_port.name} - " \
            f"{IbInterfaceConsts.PRIMARY_ASIC}={output_dictionary[IbInterfaceConsts.PRIMARY_ASIC]}"


@pytest.mark.ib_interfaces
@pytest.mark.parametrize('test_api', [ApiType.OPENAPI])
@pytest.mark.simx
def test_ib_show_interface_all_state_up(engines, devices, start_sm, test_api):
    """
    Run show interface command and verify the required fields are exist
    command: nv show interface

    flow:
    1. Run 'nv show interface'
    2. Select a random port from the output in 'up' state
    3. Verify the required fields are presented in the output
    4. Change the port state to 'down'
    5. Verify the port state as down
    6. Change the port state to 'up'
    7. Verify the port state as up

    """

    output_dictionary = Tools.OutputParsingTool.parse_show_all_interfaces_output_to_dictionary(
        Port.show_interface()).get_returned_value()

    result = Tools.RandomizationTool.select_random_port(requested_ports_state="up",
                                                        requested_ports_logical_state=NvosConsts.LINK_LOG_STATE_ACTIVE,
                                                        requested_ports_type=devices.dut.switch_type.lower())
    if not result.result:
        return

    selected_port = result.returned_value

    TestToolkit.update_tested_ports([selected_port])

    assert selected_port.name in output_dictionary.keys(), "selected port can't be found in the output"

    output_dictionary = Tools.OutputParsingTool.parse_show_interface_output_to_dictionary(
        selected_port.interface.show()).get_returned_value()

    with allure.step('Run show command on selected port and verify that each field has an appropriate '
                     'value according to the state of the port'):
        validate_one_port_in_show_all_ports(output_dictionary, devices.dut.switch_type.lower())

    try:
        with allure.step('Set the state of selected port to "down"'):
            selected_port.interface.link.state.set(op_param_name=NvosConsts.LINK_STATE_DOWN, apply=True,
                                                   ask_for_confirmation=True).verify_result()
            selected_port.interface.wait_for_port_state(state=NvosConsts.LINK_STATE_DOWN).verify_result()

            output_dictionary = Tools.OutputParsingTool.parse_show_interface_output_to_dictionary(
                selected_port.interface.show()).get_returned_value()

            with allure.step('Run show command on selected port and verify that each field has an appropriate '
                             'value according to the state of the port'):
                validate_one_port_in_show_all_ports(output_dictionary, devices.dut.switch_type.lower(), False)

            with allure.step('Set the state of selected port to "up"'):
                selected_port.interface.link.state.set(op_param_name=NvosConsts.LINK_STATE_UP, apply=True,
                                                       ask_for_confirmation=True).verify_result()
                selected_port.interface.wait_for_port_state(NvosConsts.LINK_STATE_UP,
                                                            logical_state='Active').verify_result()
            time.sleep(5)
            output_dictionary = Tools.OutputParsingTool.parse_show_interface_output_to_dictionary(
                selected_port.interface.show()).get_returned_value()

            with allure.step('Run show command on selected port and verify that each field has an appropriate '
                             'value according to the state of the port'):
                validate_one_port_in_show_all_ports(output_dictionary, devices.dut.switch_type.lower(), True)
    finally:
        with allure.step('Set the state of selected port to "up"'):
            selected_port.interface.link.state.set(op_param_name=NvosConsts.LINK_STATE_UP, apply=True,
                                                   ask_for_confirmation=True).verify_result()
            selected_port.interface.wait_for_port_state(NvosConsts.LINK_STATE_UP,
                                                        logical_state='Active').verify_result()


@pytest.mark.ib_interfaces
@pytest.mark.simx
@pytest.mark.nvos_chipsim_ci
def test_ib_show_interface_all_state_down(engines, devices, has_loopbox, setup_name):
    """
    Run show interface command and verify the required fields are exist
    command: nv show interface

    flow:
    1. Run 'nv show interface'
    2. Select a random port from the output in 'down' state
    3. Verify the required fields are presented in the output
    """

    if has_loopbox and (devices.dut.nvl_trunk_ports_list == []):
        pytest.skip("Cannot run test System with loopboxes, and no trunk links (NSO)")

    output_dictionary = Tools.OutputParsingTool.parse_show_all_interfaces_output_to_dictionary(
        Port.show_interface()).get_returned_value()

    selected_port = Tools.RandomizationTool.select_random_port(requested_ports_state="down",
                                                               requested_ports_logical_state=None,
                                                               requested_ports_type=devices.dut.switch_type.lower()).get_returned_value()
    TestToolkit.update_tested_ports([selected_port])

    assert selected_port.name in output_dictionary.keys(), "selected port can't be found in the output"

    output_dictionary = Tools.OutputParsingTool.parse_show_interface_output_to_dictionary(
        selected_port.interface.show()).get_returned_value()

    with allure.step('Run show command on selected port and verify that each field has an appropriate '
                     'value according to the state of the port'):
        validate_one_port_in_show_all_ports(output_dictionary, devices.dut.switch_type.lower(), False)
        link_physical_port_state = output_dictionary[IbInterfaceConsts.LINK][IbInterfaceConsts.LINK_PHYSICAL_PORT_STATE]
        assert link_physical_port_state in [IbInterfaceConsts.LINK_PHYSICAL_PORT_STATE_POLLING,
                                            IbInterfaceConsts.LINK_PHYSICAL_PORT_STATE_DISABLED,
                                            IbInterfaceConsts.LINK_PHYSICAL_PORT_STATE_CONFIGURATION_TRAINING], \
            "Link physical port state {} isn't as we expected".format(link_physical_port_state)


@pytest.mark.ib_interfaces
@pytest.mark.nvos_ci
@pytest.mark.ib
@pytest.mark.simx
def test_ib_show_interface_name_link(engines, devices, random_api):
    """
    Run show interface command and verify the required fields exist
    Command: nv show interface <name> link

    flow:
    1. Select a random port (status of which is up)
    2. Run 'nv show interface <name> link' on selected port
    3. Verify the required fields are presented in the output
    4. Verify state based on logical & physical state
    """

    try:
        selected_port = Tools.RandomizationTool.select_random_port(requested_ports_type=devices.dut.switch_type.lower()).get_returned_value()
    except Exception:
        pytest.skip("Device does not have any connectivity")

    TestToolkit.update_tested_ports([selected_port])

    with allure.step('Run show command on selected port and verify that each field has an appropriate '
                     'value according to the state of the port'):
        output_dictionary = Tools.OutputParsingTool.parse_show_interface_link_output_to_dictionary(
            selected_port.interface.link.show()).get_returned_value()

        validate_link_fields(output_dictionary, devices.dut.switch_type.lower())
        verify_expected_link_state(output_dictionary)


@pytest.mark.ib_interfaces
@pytest.mark.air
@pytest.mark.simx
def test_ib_show_interface_name_stats(engines, devices, random_api):
    """
    Run show interface command and verify the required fields exist
    Command: nv show interface <name> link stats

    flow:
    1. Select a random port (status of which is up)
    2. Run 'nv show interface <name> link stats' on selected port
    3. Verify the required fields are presented in the output
    """
    try:
        selected_port = Tools.RandomizationTool.select_random_port(requested_ports_type=devices.dut.switch_type.lower()).get_returned_value()
    except Exception:
        pytest.skip("Device does not have any connectivity")

    TestToolkit.update_tested_ports([selected_port])

    with allure.step('Run show command on selected port and verify that each field has an appropriate '
                     'value according to the state of the port'):
        output_dictionary = Tools.OutputParsingTool.parse_show_interface_stats_output_to_dictionary(
            selected_port.interface.counters.show()).get_returned_value()

        validate_stats_fields(output_dictionary, devices.dut.asic_type in NvosConst.QTM3_AND_NEWER)


@pytest.mark.ib_interfaces
@pytest.mark.ib
@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
@pytest.mark.simx
def test_show_interface_filter(engines, test_api):
    """
    Run show interface command with filter flag and verify the required fields are exist
    command: nv show interface -- filter "<filter>=<value>"

    flow:
    1. Run show interface without filter
    2. Select filter type and value from existing output
    3. Create expected dictionary according to the selected filter
    4. Run show interface with the selected filter
    5. Compare between filtered output dictionary to expected dictionary (at least one should be found)
    6. Run show interface with an empty filter (returns all data)
    7. Compare between filtered output dictionary to the full dictionary
    8. Run show interface with existing filter but value not exist
    9. Run show interface with a filter that does not exist
    """
    interface = Interface(parent_obj=None)
    TestToolkit.tested_api = test_api

    with allure.step('Run show interface without filter'):
        output_dict = Tools.OutputParsingTool.parse_show_all_interfaces_output_to_dictionary(
            interface.show()).get_returned_value()

    with allure.step('Select filter type and value from existing output'):
        random_key = RandomizationTool.select_random_value(list(output_dict.keys())).get_returned_value()
        filter_name = RandomizationTool.select_random_value(list(output_dict[random_key].keys())).get_returned_value()
        value = output_dict[random_key][filter_name]

    with allure.step('Create expected dictionary according to the selected filter'):
        filtered_expected = {}
        for key in output_dict.keys():
            if filter_name in output_dict[key].keys():
                if value == output_dict[key][filter_name]:
                    filtered_expected.update({key: output_dict[key]})

    with allure.step('Run show interface with the selected filter'):
        output_dict_filtered = Tools.OutputParsingTool.parse_show_all_interfaces_output_to_dictionary(
            interface.filter(filter_name=filter_name, value=value).get_returned_value()).verify_result()

    with allure.step('Compare between filtered output dictionary to expected dictionary'
                     '(at least one should be found)'):
        res_obj = ValidationTool.compare_nested_dictionary_content(output_dict_filtered, filtered_expected)
        if test_api == ApiType.OPENAPI:
            res_obj.ignore_result()
        else:
            res_obj.verify_result()

    with allure.step('Run show interface with an empty filter (returns all data)'):
        output_dict_filtered = Tools.OutputParsingTool.parse_show_all_interfaces_output_to_dictionary(
            interface.filter().get_returned_value()).get_returned_value()

    with allure.step('Run show interface without filter'):
        output_dict = Tools.OutputParsingTool.parse_show_all_interfaces_output_to_dictionary(
            interface.show()).get_returned_value()

    with allure.step('Compare between filtered output dictionary to the full dictionary'):
        res_obj = ValidationTool.compare_nested_dictionary_content(output_dict_filtered, output_dict)
        if test_api == ApiType.OPENAPI:
            res_obj.ignore_result()
        else:
            res_obj.verify_result()

    with allure.step('Run show interface with existing filter but value not exist'):
        value = 'value_not_exists'
        output_dict_filtered = interface.filter(filter_name=filter_name, value=value).verify_result()
        assert output_dict_filtered.startswith('{}'), f"expected empty dict - got {output_dict_filtered}"

    with allure.step('Run show interface with a filter that does not exist'):
        filter_name = 'filter_not_exist'
        output_dict_filtered = interface.filter(filter_name=filter_name, value=value).verify_result(False)
        assert re.search(r'No match found for filter depth of \d+\.', output_dict_filtered)


@pytest.mark.ib_interfaces
@pytest.mark.ib
@pytest.mark.simx
def test_validate_discard_counters_fields(engines, random_api):
    """
    Run show interface command and verify the required fields are exist
    command: nv show interface <name>

    flow:
    1. Select a random port
    2. Get the OID of the port
    3. Validate that in-drops and out-drops fields are present in show output
    4. Validate that in-drops and out-drops fields are present in Sonic DB
    5. Validate that in-drops and out-drops fields are present in GNMI
    """
    plane_suffix = "pl1"
    server = 'fit-build-240'
    server_user = os.getenv("BUILD_SERVER_USER")
    server_password = os.getenv("BUILD_SERVER_PASSWORD")
    gnmi_engine = LinuxSshEngine(server, server_user, server_password)
    system = System()
    system_show = OutputParsingTool.parse_json_str_to_dictionary(system.show()).get_returned_value()
    host = system_show['hostname']
    selected_port = Tools.RandomizationTool.select_random_port(requested_ports_state=None).get_returned_value()
    TestToolkit.update_tested_ports([selected_port])
    port = selected_port.name
    port_oid = get_port_oid(engines, port, plane_suffix)
    logging.info("Selected Port:{}, OID:{}".format(port, port_oid))

    with allure.step('Validate that in-drops and out-drops fields are present in show output'):
        output_dictionary = Tools.OutputParsingTool.parse_show_interface_output_to_dictionary(
            selected_port.interface.show()).get_returned_value()
        assert IbInterfaceConsts.LINK_STATS_IN_DROPS in output_dictionary["counters"].keys(), \
            "{} field not available in show interface".format(IbInterfaceConsts.LINK_STATS_IN_DROPS)
        assert IbInterfaceConsts.LINK_STATS_OUT_DROPS in output_dictionary["counters"].keys(), \
            "{} field not available in show interface".format(IbInterfaceConsts.LINK_STATS_OUT_DROPS)

    with allure.step('Validate that in-drop counter fields are present in Sonic-DB'):
        validate_field_from_sonic_db(engines, port_oid, IbInterfaceConsts.PC_VL15_DROPPED_F)
        validate_field_from_sonic_db(engines, port_oid, IbInterfaceConsts.RCV_DISCARD_EXTERNAL_CONTAIN)
        validate_field_from_sonic_db(engines, port_oid, IbInterfaceConsts.TOTAL_IN_DROPS)

    with allure.step('Validate that out-drop counter fields are present in Sonic-DB'):
        validate_field_from_sonic_db(engines, port_oid, IbInterfaceConsts.PC_XMT_DISCARDS_F)
        validate_field_from_sonic_db(engines, port_oid, IbInterfaceConsts.XMT_DISCARD_EXTERNAL_CONTAIN)
        validate_field_from_sonic_db(engines, port_oid, IbInterfaceConsts.TOTAL_OUT_DROPS)

    with allure.step('Validate that total-in-drops and total-out-drops fields are present via GNMI'):
        validate_field_from_gnmi(gnmi_engine, host, port, "in-discards")
        validate_field_from_gnmi(gnmi_engine, host, port, "out-discards")


@pytest.mark.ib_interfaces
@pytest.mark.ib
@pytest.mark.simx
def test_validate_total_counters_in_out_drops(engines, random_api):
    """
    Validate that total in and out drops counters are a sum of two other counters

    flow:
    1. Select a random port (status of which is up)
    2. Get the OID of the port
    3. Validate total in drop counters are a sum of two counters in Sonic-DB
    4. Validate total out drop counters are a sum of two counters in Sonic-DB
    """
    plane_suffix = "pl1"
    selected_port = Tools.RandomizationTool.select_random_port(requested_ports_state=None).get_returned_value()
    TestToolkit.update_tested_ports([selected_port])
    port = selected_port.name
    port_oid = get_port_oid(engines, port, plane_suffix)
    logging.info("Selected Port:{}, OID:{}".format(port, port_oid))

    with allure.step('Validate total in drop counters are a sum of two counters in Sonic-DB'):
        pc_vl15_dropped_f = validate_field_from_sonic_db(engines, port_oid, IbInterfaceConsts.PC_VL15_DROPPED_F)
        rcv_discard_ext_cont = validate_field_from_sonic_db(engines, port_oid, IbInterfaceConsts.
                                                            RCV_DISCARD_EXTERNAL_CONTAIN)
        total_in_drops = pc_vl15_dropped_f + rcv_discard_ext_cont

        output = validate_field_from_sonic_db(engines, port_oid, IbInterfaceConsts.TOTAL_IN_DROPS)
        assert total_in_drops == output, f"Total in drops is not equal to sum of " \
            f"{IbInterfaceConsts.PC_VL15_DROPPED_F} and " \
            f"{IbInterfaceConsts.RCV_DISCARD_EXTERNAL_CONTAIN}"

    with allure.step('Validate total out drop counters are a sum of two counters in Sonic-DB'):
        pc_xmt_discards_f = validate_field_from_sonic_db(engines, port_oid, IbInterfaceConsts.PC_XMT_DISCARDS_F)
        xmt_discard_ext_cont = validate_field_from_sonic_db(engines, port_oid, IbInterfaceConsts.
                                                            XMT_DISCARD_EXTERNAL_CONTAIN)
        total_out_drops = pc_xmt_discards_f + xmt_discard_ext_cont

        output = validate_field_from_sonic_db(engines, port_oid, IbInterfaceConsts.TOTAL_OUT_DROPS)
        assert total_out_drops == output, f"Total out drops is {output} instead of {total_out_drops} which is not " \
            f"equal to sum of {IbInterfaceConsts.PC_XMT_DISCARDS_F} and " \
            f"{IbInterfaceConsts.XMT_DISCARD_EXTERNAL_CONTAIN}"


@pytest.mark.ib_interfaces
@pytest.mark.ib
@pytest.mark.simx
def test_validate_total_in_out_counters_show_db_gnmi(engines, random_api):
    """
    Validate total in drop and out drop counters are same across show, Sonic DB and GNMI

    flow:
    1. Select a random port
    2. Get the OID of the port
    3. Retrieve the required fields: in-drops and out-drops are present from the show output
    4. Retrieve the required fields: in-drops and out-drops are present from the sonic DB
    5. Retrieve the required fields: in-drops and out-drops are present from GNMI
    6. Compare in/out drops counters across show CLI, Sonic DB and GNMI server
    """
    plane_suffix = "pl1"
    server = 'fit-build-240'
    server_user = os.getenv("BUILD_SERVER_USER")
    server_password = os.getenv("BUILD_SERVER_PASSWORD")
    gnmi_engine = LinuxSshEngine(server, server_user, server_password)
    system = System()
    system_show = OutputParsingTool.parse_json_str_to_dictionary(system.show()).get_returned_value()
    host = system_show['hostname']
    in_drop_mismatch_err = ""
    out_drop_mismatch_err = ""
    selected_port = Tools.RandomizationTool.select_random_port(requested_ports_state=None).get_returned_value()
    TestToolkit.update_tested_ports([selected_port])
    port = selected_port.name
    logging.info("Selected Port:{}".format(port))
    port_oid = get_port_oid(engines, selected_port.name, plane_suffix)

    with allure.step('Retrieve in-drops and out-drops fields from show output'):
        output_dictionary = Tools.OutputParsingTool.parse_show_interface_output_to_dictionary(
            selected_port.interface.show()).get_returned_value()
        in_drops_show = output_dictionary["counters"][IbInterfaceConsts.LINK_STATS_IN_DROPS]
        out_drops_show = output_dictionary["counters"][IbInterfaceConsts.LINK_STATS_OUT_DROPS]

    with allure.step('Retrieve in-drops and out-drops fields from Sonic-DB'):
        in_drops_sonic_db = validate_field_from_sonic_db(engines, port_oid, IbInterfaceConsts.TOTAL_IN_DROPS)
        out_drops_sonic_db = validate_field_from_sonic_db(engines, port_oid, IbInterfaceConsts.TOTAL_OUT_DROPS)

    with allure.step('Validate that in-drops and out-drops fields are present via GNMI'):
        in_drops_gnmi = validate_field_from_gnmi(gnmi_engine, host, port, "in-discards")
        out_drops_gnmi = validate_field_from_gnmi(gnmi_engine, host, port, "out-discards")

    with allure.step('Compare in/out drops counters across show CLI, Sonic DB and GNMI server'):
        if (in_drops_show != in_drops_sonic_db) or (in_drops_show != in_drops_gnmi):
            in_drop_mismatch_err = "In drop counter mismatch: show:{}, DB:{}, GNMI:{}".format(
                in_drops_show, in_drops_sonic_db, in_drops_gnmi)
        if (out_drops_show != out_drops_sonic_db) | (out_drops_show != out_drops_gnmi):
            out_drop_mismatch_err = "Out drop counter mismatch: show:{}, DB:{}, GNMI:{}".format(
                out_drops_show, out_drops_sonic_db, out_drops_gnmi)
        assert not ((in_drop_mismatch_err != "") or (out_drop_mismatch_err != "")), "{}, {}".format(
            in_drop_mismatch_err, out_drop_mismatch_err)


def get_port_oid(engines, port_name, plane_suffix):
    # Use the shared plane-port -> OID resolver: it tries the plane name and the
    # IB-name form in COUNTERS_PORT_NAME_MAP before falling back to the FAE
    # `plane-ports` "key" trick, so it survives Aports that don't expose
    # `plane-ports` in `nv show fae interface <aport>`.
    plane_port_name = port_name + plane_suffix
    oid_key = get_plane_port_oid(engines, plane_port_name)  # "oid:0x..."
    return oid_key.split(":", 1)[1] if oid_key.startswith("oid:") else oid_key


def validate_field_from_sonic_db(engines, port_oid, field_name):
    output_sonic_db = engines.dut.run_cmd(f'sonic-db-cli COUNTERS_DB hget "COUNTERS:oid:{port_oid}" "{field_name}"')
    assert output_sonic_db != "", f"Field {field_name} not present in sonic db for port oid {port_oid}"
    value = int(output_sonic_db)
    logger.info(f"Sonic DB Port OID:{port_oid}, Field:{field_name}, Value:{value}")
    return value


def validate_field_from_gnmi(gnmi_engine, host, port, field):
    gnmi_cmd = f'gnmic -a {host} --port 9339 subscribe --path "interfaces/interface[name={port}]/state/' \
        f'counters/{field}" --target nvos -u admin -p admin --mode once --skip-verify --format flat'
    output = gnmi_engine.run_cmd(gnmi_cmd)
    value = int(output.split(":")[-1].strip())
    logging.info("Port: {}, Field {}: {}".format(port, field, value))
    return value


def _validate_cpo_mappings(output_dictionary, dut, port_name):
    """Validate els-mapping and oe-mapping values against the device's known mappings."""
    with allure.step(f'Validate CPO mappings for port {port_name}'):
        expected_els, _, _ = PhotonicsTool.get_els_for_traffic_ports(dut, [port_name])
        if expected_els is None:
            logging.warning(f"Port {port_name} not found in any ELS port mapping, skipping CPO mapping validation")
            return

        # Validate els-mapping
        ValidationTool.verify_field_value_in_output(
            output_dictionary, PlatformConsts.TRANSCEIVER_ELS_MAPPING, expected_els
        ).verify_result()

        # Validate oe-mapping
        expected_oes = set(PhotonicsTool.get_oe_list_for_els(dut, expected_els))
        actual_oes = set(output_dictionary.get(PlatformConsts.TRANSCEIVER_OE_MAPPING, {}).keys())
        ValidationTool.validate_set_equal(actual_oes, expected_oes).verify_result()

        logging.info(f"Port {port_name}: els-mapping={expected_els}, oe-mapping={actual_oes}")


def validate_interface_fields(output_dictionary, is_cpo=False, dut=None, port_name=None):
    with allure.step('Check that the following fields exist in the output: type, link'):
        logging.info('Check that the following fields exist in the output: type, link')
        field_to_check = [IbInterfaceConsts.TYPE, IbInterfaceConsts.LINK]
        Tools.ValidationTool.verify_field_exist_in_json_output(output_dictionary, field_to_check).verify_result()

    if is_cpo:
        with allure.step('Check CPO fields: els-mapping, oe-mapping'):
            logging.info('Check that els-mapping and oe-mapping fields exist in the output')
            cpo_fields = [PlatformConsts.TRANSCEIVER_ELS_MAPPING, PlatformConsts.TRANSCEIVER_OE_MAPPING]
            Tools.ValidationTool.verify_field_exist_in_json_output(output_dictionary, cpo_fields).verify_result()

        if dut and port_name:
            _validate_cpo_mappings(output_dictionary, dut, port_name)


def validate_link_fields(output_dictionary, switch_type, port_up=True):
    with allure.step('Check that all expected fields under link field exist in the output'):
        logging.info('Check that all expected fields under link field exist in the output')
        field_to_check = [IbInterfaceConsts.LINK_STATE,
                          # IbInterfaceConsts.LINK_IB_SUBNET,
                          IbInterfaceConsts.LINK_SUPPORTED_LANES,
                          IbInterfaceConsts.LINK_MAX_SUPPORTED_MTU,
                          # IbInterfaceConsts.LINK_SUPPORTED_IB_SPEEDS,
                          # IbInterfaceConsts.LINK_SUPPORTED_SPEEDS,
                          IbInterfaceConsts.LINK_LOGICAL_PORT_STATE,
                          IbInterfaceConsts.LINK_PHYSICAL_PORT_STATE,
                          IbInterfaceConsts.LINK_VL_ADMIN_CAPABILITIES]
        if switch_type == "ib":
            field_to_check.insert(1, IbInterfaceConsts.LINK_IB_SUBNET)  # Insert at the desired position
            field_to_check.append(IbInterfaceConsts.LINK_CONNECTION_MODE)

        Tools.ValidationTool.verify_field_exist_in_json_output(output_dictionary, field_to_check).verify_result()

        field_to_check = [IbInterfaceConsts.LINK_MTU,
                          # IbInterfaceConsts.LINK_SPEED,
                          # IbInterfaceConsts.LINK_IB_SPEED,
                          IbInterfaceConsts.LINK_OPERATIONAL_VLS]

        if switch_type == "ib" and \
           output_dictionary[IbInterfaceConsts.LINK_CONNECTION_MODE] == IbInterfaceConsts.XDR and \
           output_dictionary[IbInterfaceConsts.LINK_LOGICAL_PORT_STATE] == IbInterfaceConsts.LINK_LOGICAL_PORT_STATE_ACTIVE:
            field_to_check.insert(1, IbInterfaceConsts.LINK_SPEED)

        Tools.ValidationTool.verify_field_exist_in_json_output(output_dictionary,
                                                               field_to_check, port_up).verify_result()
        # Will be changed
        field_to_check = [IbInterfaceConsts.LINK_LANES]
        res = Tools.ValidationTool.verify_field_exist_in_json_output(output_dictionary, field_to_check, port_up)
        logging.warning(res.info)


def validate_stats_fields(output_dictionary, is_qtm3_or_newer=False):
    with allure.step('Check that all expected fields under link-stats field exist in the output'):
        logging.info('Check that all expected fields under link-stats field exist in the output')
        fields_to_check = [IbInterfaceConsts.LINK_STATS_IN_BYTES,
                           IbInterfaceConsts.LINK_STATS_IN_DROPS,
                           IbInterfaceConsts.LINK_STATS_IN_ERRORS,
                           # Note: in-symbol-errors is not at top level, it's under ib.errors.symbol-errors
                           IbInterfaceConsts.LINK_STATS_IN_PKTS,
                           IbInterfaceConsts.LINK_STATS_OUT_BYTES,
                           IbInterfaceConsts.LINK_STATS_OUT_DROPS,
                           IbInterfaceConsts.LINK_STATS_OUT_ERRORS,
                           IbInterfaceConsts.LINK_STATS_OUT_PKTS,
                           IbInterfaceConsts.LINK_STATS_OUT_WAIT]
        if is_qtm3_or_newer:
            logging.info('Add expected fields for Quantum3 device')
            # QTM3 fields at top level
            fields_to_check.extend(IbInterfaceConsts.LINK_STATS_QNT3_TOP_LEVEL)
            # QTM3 fields under 'link' dictionary
            if 'link' in output_dictionary:
                Tools.ValidationTool.verify_field_exist_in_json_output(
                    output_dictionary['link'],
                    IbInterfaceConsts.LINK_STATS_QNT3_UNDER_LINK
                ).verify_result()
            # Access nested error counters from ib.errors structure
            icrc_errors_rcv = output_dictionary.get('ib', {}).get('errors', {}).get('icrc-errors', {}).get('receive', 0)
            tx_parity_errors_rcv = output_dictionary.get('ib', {}).get('errors', {}).get('tx-parity-errors', {}).get('receive', 0)
            verify_non_negative_counters({'icrc-errors-receive': icrc_errors_rcv,
                                         'tx-parity-errors-receive': tx_parity_errors_rcv})
        Tools.ValidationTool.verify_field_exist_in_json_output(output_dictionary, fields_to_check).verify_result()


def validate_one_port_show_output(output_dictionary, switch_type, is_qtm3_or_newer=False, is_cpo=False,
                                  dut=None, port_name=None):
    validate_interface_fields(output_dictionary, is_cpo=is_cpo, dut=dut, port_name=port_name)

    validate_link_fields(output_dictionary[IbInterfaceConsts.LINK], switch_type)

    # counters is at the top level of output_dictionary, not under link
    validate_stats_fields(output_dictionary[IbInterfaceConsts.LINK_STATS], is_qtm3_or_newer)


def validate_one_port_in_show_all_ports(output_dictionary, switch_type, port_up=True):
    field_to_check = [IbInterfaceConsts.TYPE, IbInterfaceConsts.LINK]
    Tools.ValidationTool.verify_field_exist_in_json_output(output_dictionary, field_to_check).verify_result()

    validate_link_fields(output_dictionary[IbInterfaceConsts.LINK], switch_type, port_up)


def verify_expected_link_state(output_dictionary):
    link_physical_port_state = output_dictionary[IbInterfaceConsts.LINK_PHYSICAL_PORT_STATE]
    link_logical_port_state = output_dictionary[IbInterfaceConsts.LINK_LOGICAL_PORT_STATE]
    if link_physical_port_state in \
            [IbInterfaceConsts.LINK_PHYSICAL_PORT_STATE_POLLING,
             IbInterfaceConsts.LINK_PHYSICAL_PORT_STATE_DISABLED]:

        Tools.ValidationTool.validate_fields_values_in_output(
            output_dict=output_dictionary,
            expected_fields=[IbInterfaceConsts.LINK_LOGICAL_PORT_STATE, IbInterfaceConsts.LINK_STATE],
            expected_values=[IbInterfaceConsts.LINK_LOGICAL_PORT_STATE_DOWN, NvosConsts.LINK_STATE_DOWN]) \
            .verify_result()

    elif link_physical_port_state == IbInterfaceConsts.LINK_PHYSICAL_PORT_STATE_LINK_UP:

        Tools.ValidationTool.verify_field_value_in_output(
            output_dictionary=output_dictionary,
            field_name=IbInterfaceConsts.LINK_STATE,
            expected_value=NvosConsts.LINK_STATE_UP).verify_result()

        assert link_logical_port_state in [IbInterfaceConsts.LINK_LOGICAL_PORT_STATE_ACTIVE,
                                           IbInterfaceConsts.LINK_LOGICAL_PORT_STATE_INITIALIZE], \
            "Link logical port state {} isn't as we expected".format(link_logical_port_state)

    else:
        raise Exception("Link physical port state {} isn't as we expected".format(link_physical_port_state))


def verify_non_negative_counters(link_stats_dict):
    for field, counter in link_stats_dict.items():
        assert counter >= 0, f"counter isn't as we expected.\n we got: {field}={counter}"


def extract_non_dict_keys(output_dict):
    keys = []

    for key, value in output_dict.items():
        if not isinstance(value, dict):  # Only add key if value is not a dict
            keys.append(key)

    return keys


@pytest.mark.ib_interfaces
@pytest.mark.nvos_ci
@pytest.mark.ib
@pytest.mark.air
@pytest.mark.simx
def test_ib_interface_counters_cli_structure(engines, devices, random_api):
    """
    Test to verify CLI structure for IB interface counters

    Requirements for IB interfaces:
    1. Check interface type is 'ib'
    2. Under 'nv show interface <name> counters', 'ib' and 'link' options should be present
    3. 'nvl' should NOT be present under counters
    4. Under 'nv show interface <name> counters ib', there should be 3 options: errors, drops, fast-recovery

    Flow:
    1. Select a random IB port
    2. Verify interface type is 'ib'
    3. Get available options under 'nv show interface <name> counters'
    4. Verify 'ib' and 'link' are present, and 'nvl' is NOT present
    5. Get available options under 'nv show interface <name> counters ib'
    6. Verify errors, drops, and fast-recovery are present
    """

    if devices.dut.switch_type == NvosConst.IB_SWITCH_TYPE:
        verify_ib_interface_counters_cli_structure_ib(devices)
    else:
        verify_ib_interface_counters_cli_structure_nvl(devices)


def verify_ib_interface_counters_cli_structure_nvl(devices):
    with allure.step('Select a random port'):
        selected_port = Tools.RandomizationTool.select_random_port(
            requested_ports_state=None,
            requested_ports_type=NvlInterfaceConsts.NVL_PORT_TYPE).get_returned_value()

        TestToolkit.update_tested_ports([selected_port])
        logger.info(f"Selected NVL port: {selected_port.name}")

    with allure.step('Verify NVL interface counters'):
        counters_output = selected_port.interface.counters.show()
        counters_dict = Tools.OutputParsingTool.parse_json_str_to_dictionary(counters_output).get_returned_value()
        logger.info(f"Counters output: {counters_dict}")

        expectations = [
            (IbInterfaceConsts.NVL, True),
            (IbInterfaceConsts.IB_PORT_TYPE, False),
            (IbInterfaceConsts.LINK, True)]

        for key, should_exist in expectations:
            is_present = key in counters_dict
            presence_text = "present" if should_exist else "NOT be present"
            assert is_present == should_exist, \
                f"Expected '{key}' option to {presence_text} under counters, but got: {counters_dict}"

    with allure.step('Verify NVL interface counters link'):
        counters_output = selected_port.interface.counters.link.show()
        counters_dict = Tools.OutputParsingTool.parse_json_str_to_dictionary(counters_output).get_returned_value()
        ValidationTool.verify_field_exist_in_json_output(counters_dict, IbInterfaceConsts.LINK_STATS_QNT3_UNDER_LINK).verify_result()

    with allure.step('Verify NVL interface counters nvl'):
        counters_output = selected_port.interface.counters.nvl.show()
        counters_dict = Tools.OutputParsingTool.parse_json_str_to_dictionary(counters_output).get_returned_value()
        ValidationTool.verify_field_exist_in_json_output(counters_dict, IbInterfaceConsts.NVL_COUNTERS_NVL_FIELDS).verify_result()
        for sub_counters in IbInterfaceConsts.NVL_COUNTERS_NVL_FIELDS:
            Tools.OutputParsingTool.parse_json_str_to_dictionary(
                selected_port.interface.counters.nvl.show(op_param=sub_counters)).verify_result()


def verify_ib_interface_counters_cli_structure_ib(devices):
    with allure.step('Select a random port'):
        selected_port = Tools.RandomizationTool.select_random_port(
            requested_ports_type=IbInterfaceConsts.IB_PORT_TYPE).get_returned_value()

        TestToolkit.update_tested_ports([selected_port])
        logger.info(f"Selected {devices.dut.switch_type} port: {selected_port.name}")

    with allure.step('Verify interface type is "ib"'):
        output_dictionary = Tools.OutputParsingTool.parse_show_interface_output_to_dictionary(
            selected_port.interface.show()
        ).get_returned_value()

        interface_type = output_dictionary.get(IbInterfaceConsts.TYPE, None)
        assert interface_type == IbInterfaceConsts.IB_PORT_TYPE, \
            f"Expected interface type to be '{IbInterfaceConsts.IB_PORT_TYPE}', but got '{interface_type}'"
        logger.info(f"Verified interface type: {interface_type}")

    with allure.step('Check available options under "nv show interface <name> counters"'):
        # Get counters output to check available options using framework method
        counters_output = selected_port.interface.counters.show()

        try:
            counters_dict = Tools.OutputParsingTool.parse_json_str_to_dictionary(counters_output).get_returned_value()
        except Exception as e:
            pytest.fail(f"Failed to parse counters output: {e}")

        # Get top-level keys (these represent available options)
        available_options = list(counters_dict.keys())
        logger.info(f"Available options under counters: {available_options}")

        with allure.step('Verify only "ib" and "link" options are present'):
            # Check that 'ib' and 'link' are present
            assert IbInterfaceConsts.IB_PORT_TYPE in available_options, \
                f"Expected 'ib' option to be present under counters, but got: {available_options}"
            assert IbInterfaceConsts.LINK in available_options, \
                f"Expected 'link' option to be present under counters, but got: {available_options}"
            logger.info("Verified 'ib' and 'link' options are present")

        with allure.step('Verify "nvl" option is NOT present'):
            assert NvlInterfaceConsts.NVL_PORT_TYPE not in available_options, \
                f"'nvl' option should NOT be present for IB interfaces, but found in: {available_options}"
            logger.info("Verified 'nvl' option is not present")

    with allure.step('Check available options under "nv show interface <name> counters ib"'):
        # Get IB counters output to check available sub-options using framework method
        ib_counters_output = selected_port.interface.counters.ib.show()

        try:
            ib_counters_dict = Tools.OutputParsingTool.parse_json_str_to_dictionary(ib_counters_output).get_returned_value()
        except Exception as e:
            pytest.fail(f"Failed to parse IB counters output: {e}")

        # Get top-level keys under 'ib' counters
        ib_options = list(ib_counters_dict.keys())
        logger.info(f"Available options under 'counters ib': {ib_options}")

        with allure.step('Verify "errors", "drops", and "fast-recovery" options are present'):
            expected_options = IbInterfaceConsts.IB_COUNTERS_EXPECTED_OPTIONS

            for option in expected_options:
                assert option in ib_options, \
                    f"Expected '{option}' option to be present under 'counters ib', but got: {ib_options}"

            logger.info(f"Verified all expected options are present: {expected_options}")

    logger.info("Test completed successfully: IB interface counters CLI structure is correct")


@pytest.mark.ib_interfaces
@pytest.mark.nvos_ci
@pytest.mark.ib
@pytest.mark.simx
def test_show_non_ib_interface_missing_counters(engines, devices, random_api):
    """
    Test to verify that non-IB interfaces (ib0, eth0, eth1) do not have ib/nvl/link sub-options under counters

    Requirements for non-IB interfaces:
    1. Under 'nv show interface <name> counters', should have actual counter fields (in-pkts, out-pkts, etc.)
    2. 'ib', 'nvl', and 'link' sub-options should NOT be present under counters

    Flow:
    1. Iterate through non-IB interfaces (ib0, eth0, eth1)
    2. For each interface that exists on the device:
       - Verify counters show actual counter fields
       - Verify 'ib', 'nvl', and 'link' sub-options are NOT present
    """

    non_ib_interfaces = ['ib0', 'eth0', 'eth1']

    with allure.step('Verify non-IB interfaces (ib0, eth0, eth1) do not have ib/nvl/link sub-options'):
        for interface_name in non_ib_interfaces:
            with allure.step(f'Check interface {interface_name}'):
                # Create Interface object for the interface using framework
                interface_obj = Interface(parent_obj=None, port_name=interface_name)

                # Try to get interface information using framework method
                try:
                    interface_info_output = interface_obj.show()
                    interface_info = Tools.OutputParsingTool.parse_json_str_to_dictionary(interface_info_output).get_returned_value()
                    logger.info(f"Interface {interface_name} exists on device")
                except Exception as e:
                    logger.info(f"Interface {interface_name} does not exist on this device or error occurred: {e}, skipping")
                    continue

                # Get counters output using framework method
                try:
                    counters_output = interface_obj.counters.show()
                    counters_dict = Tools.OutputParsingTool.parse_json_str_to_dictionary(counters_output).get_returned_value()
                except Exception as e:
                    logger.info(f"Counters not available for interface {interface_name}: {e}, skipping")
                    continue

                # Verify counters output is not empty (should contain actual counter data)
                if not counters_dict:
                    logger.warning(f"Counters output for {interface_name} is empty")
                    continue

                # Get top-level keys
                available_keys = list(counters_dict.keys())
                logger.info(f"Available keys under counters for {interface_name}: {available_keys}")

                # Verify actual counter fields are present
                expected_counter_fields = ['in-pkts', 'out-pkts', 'in-bytes', 'out-bytes']
                found_counter_fields = [field for field in expected_counter_fields if field in available_keys]

                assert len(found_counter_fields) > 0, \
                    f"Expected to find counter fields like {expected_counter_fields} for {interface_name}, " \
                    f"but got: {available_keys}"

                logger.info(f"Verified {interface_name} has actual counter fields: {found_counter_fields}")

                # Verify ib/nvl/link sub-options are NOT present
                forbidden_options = IbInterfaceConsts.NON_IB_COUNTERS_FORBIDDEN_OPTIONS

                for option in forbidden_options:
                    assert option not in available_keys, \
                        f"'{option}' sub-option should NOT be present under counters for {interface_name} interface, " \
                        f"but found in: {available_keys}"

                logger.info(f"Verified {interface_name} counters do not contain forbidden sub-options: {forbidden_options}")

    logger.info("Test completed successfully: Non-IB interface counters CLI structures are correct")
