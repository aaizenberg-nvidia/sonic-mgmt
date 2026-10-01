import pytest

from ngts.nvos_constants.constants_nvos import SystemConsts, IpConsts
from ngts.nvos_tools.infra.IpTool import IpTool
from ngts.nvos_tools.infra.Tools import Tools
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import *
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_constants.constants_nvos import ApiType
from ngts.tools.test_utils import allure_utils as allure


cumulus_owner = "bravindranat"


logger = logging.getLogger()


@pytest.mark.cumulus
@pytest.mark.ib
@pytest.mark.simx
@pytest.mark.skynet
@pytest.mark.ib_interfaces
@pytest.mark.nvos_chipsim_ci
@pytest.mark.air
@pytest.mark.parametrize('test_api', [ApiType.OPENAPI])
def test_mgmt_show_interface(engines, test_api):
    """
    Run show interface eth0 command and verify the required fields are exist
    command: nv show interface eth0 link
    """
    TestToolkit.tested_api = test_api

    mgmt_port = Port()
    with allure.step('Run show command on mgmt port and verify that each field has an appropriate value'):
        output_dictionary = Tools.OutputParsingTool.parse_show_interface_output_to_dictionary(
            mgmt_port.interface.show()).get_returned_value()

        validate_interface_fields(output_dictionary)


@pytest.mark.cumulus
@pytest.mark.ib
@pytest.mark.simx
@pytest.mark.skynet
@pytest.mark.nvos_chipsim_ci
@pytest.mark.air
@pytest.mark.parametrize('test_api', [ApiType.OPENAPI])
def test_mgmt_show_interface_link(engines, test_api):
    """
    Run show interface eth0 link command and verify the required fields are exist
    only show cmds
    command0: nv show interface eth0 link
    command1: nv show interface eth0 link state
    command2: nv show interface eth0 link stats
    """
    TestToolkit.tested_api = test_api
    mgmt_port = Port()

    with allure.step('Run show command on selected port and verify that each field has an appropriate '
                     'value according to the state of the port'):
        output_dictionary = Tools.OutputParsingTool.parse_show_interface_link_output_to_dictionary(
            mgmt_port.interface.link.show()).get_returned_value()

        validate_link_fields(output_dictionary)
        verify_mac_address(IpTool.get_mac_address(engines.dut, mgmt_port.name), output_dictionary)


@pytest.mark.cumulus
@pytest.mark.ib
@pytest.mark.simx
@pytest.mark.skynet
@pytest.mark.air
@pytest.mark.parametrize('test_api', [ApiType.OPENAPI])
def test_ib_show_interface_stats(engines, test_api):
    """
    Run show interface command and verify the required fields exist
    Command: nv show interface <name> link stats
    """
    TestToolkit.tested_api = test_api
    mgmt_port = Port()

    with allure.step('Run show command on selected port and verify that each field has an appropriate '
                     'value according to the state of the port'):
        output_dictionary = Tools.OutputParsingTool.parse_show_interface_stats_output_to_dictionary(
            mgmt_port.interface.counters.show()).get_returned_value()

        validate_stats_fields(output_dictionary)


@pytest.mark.cumulus
@pytest.mark.ib
@pytest.mark.simx
@pytest.mark.skynet
@pytest.mark.nvos_chipsim_ci
@pytest.mark.air
@pytest.mark.parametrize('test_api', [ApiType.OPENAPI])
def test_ib_show_interface_ip(engines, test_api):
    """
    Run show interface command and verify the required fields exist
    Command: nv show interface <name> link stats

    flow:
    1. Select a random port (status of which is up)
    2. Run 'nv show interface <name> link stats' on selected port
    3. Verify the required fields are presented in the output
    """
    TestToolkit.tested_api = test_api
    mgmt_port = Port()

    with allure.step('IPV4 - Run show command on selected port and verify that each field has an appropriate '
                     'value according to the state of the port'):
        output_dictionary_ipv4 = Tools.OutputParsingTool.parse_show_interface_pluggable_output_to_dictionary(
            mgmt_port.interface.ipv4.show()).get_returned_value()

        validate_ip_fields(output_dictionary_ipv4, ipv4=True)

    with allure.step('IPV6 - Run show command on selected port and verify that each field has an appropriate '
                     'value according to the state of the port'):
        output_dictionary_ipv6 = Tools.OutputParsingTool.parse_show_interface_pluggable_output_to_dictionary(
            mgmt_port.interface.ipv6.show()).get_returned_value()

        validate_ip_fields(output_dictionary_ipv6, ipv4=False)


def check_dhcp(mgmt_port, ipv4=True):
    # Run the desired show command (ipv4 / ipv6)
    if ipv4:
        with allure.step('Run show command on dhcp-client (ipv4) of eth0 mgmt port'):
            output_json = mgmt_port.interface.ipv4.dhcp_client.show()
    else:  # ipv6
        with allure.step('Run show command on dhcp-client (ipv6) of eth0 mgmt port'):
            # Note: IPv6 uses dhcp_client, not dhcp_client6
            output_json = mgmt_port.interface.ipv6.dhcp_client.show()

    output_dict = Tools.OutputParsingTool.parse_json_str_to_dictionary(output_json).get_returned_value()

    # Verify the result - Updated fields to match new schema (removed deprecated has-lease and is-running)
    with allure.step('Verify required fields exist in the show output, and set to default value'):
        expected_fields = ['set-hostname', 'state']
        expected_values = ['enabled', 'enabled']
        Tools.ValidationTool.validate_fields_values_in_output(
            expected_fields=expected_fields, expected_values=expected_values,
            output_dict=output_dict).verify_result()


@pytest.mark.cumulus
@pytest.mark.ib
@pytest.mark.simx
@pytest.mark.skynet
@pytest.mark.interfaces
@pytest.mark.air
@pytest.mark.nvos_chipsim_ci
@pytest.mark.parametrize('test_api', [ApiType.OPENAPI])
def test_show_interface_ip_dhcp(engines, test_api):
    """
    Run show interface command and verify the required fields exist
    Command: nv show interface eth ip dhcp_client (and dhcp_client6)

    flow:
    1. Select eth0 port (status of which is up)
    2. Run 'nv show interface eth0 ip dhcp_client'
    3. Verify the required fields are presented in the output and set to default
    4. Run 'nv show interface eth0 ip dhcp_client6'
    5. Verify the required fields are presented in the output and set to default
    """
    TestToolkit.tested_api = test_api
    mgmt_port = Port('eth0')

    check_dhcp(mgmt_port=mgmt_port, ipv4=True)  # test ipv4
    check_dhcp(mgmt_port=mgmt_port, ipv4=False)  # same test on ipv6


def validate_interface_fields(output_dictionary):
    with allure.step('Check that the following fields exist in the output: type, link, ipv4, ipv6, ifindex'):
        logging.info('Check that the following fields exist in the output: type, link, ipv4, ipv6, ifindex')
        field_to_check = [IbInterfaceConsts.TYPE,
                          IbInterfaceConsts.LINK,
                          IbInterfaceConsts.IFINDEX,
                          IbInterfaceConsts.IPV4,
                          IbInterfaceConsts.IPV6,
                          IbInterfaceConsts.VRF,
                          IbInterfaceConsts.LINK_STATS,
                          IbInterfaceConsts.LLDP]
        Tools.ValidationTool.verify_field_exist_in_json_output(output_dictionary, field_to_check).verify_result()


def validate_link_fields(output_dictionary):
    with allure.step('Check that all expected fields under link field exist in the output'):
        logging.info('Check that all expected fields under link field exist in the output')
        field_to_check = [IbInterfaceConsts.LINK_MTU,
                          IbInterfaceConsts.LINK_SPEED,
                          IbInterfaceConsts.LINK_MAC,
                          IbInterfaceConsts.LINK_DUPLEX,
                          IbInterfaceConsts.LINK_STATE,
                          IbInterfaceConsts.LINK_AUTO_NEGOTIATE]
        Tools.ValidationTool.verify_field_exist_in_json_output(output_dictionary, field_to_check).verify_result()


def validate_stats_fields(output_dictionary):
    with allure.step('Check that all expected fields under link-stats field exist in the output'):
        logging.info('Check that all expected fields under link-stats field exist in the output')
        field_to_check = [IbInterfaceConsts.LINK_STATS_CARRIER_TRANSITION,
                          IbInterfaceConsts.LINK_STATS_IN_BYTES,
                          IbInterfaceConsts.LINK_STATS_IN_DROPS,
                          IbInterfaceConsts.LINK_STATS_IN_ERRORS,
                          IbInterfaceConsts.LINK_STATS_IN_PKTS,
                          IbInterfaceConsts.LINK_STATS_OUT_BYTES,
                          IbInterfaceConsts.LINK_STATS_OUT_DROPS,
                          IbInterfaceConsts.LINK_STATS_OUT_ERRORS,
                          IbInterfaceConsts.LINK_STATS_OUT_PKTS]
        Tools.ValidationTool.verify_field_exist_in_json_output(output_dictionary, field_to_check).verify_result()


def validate_ip_fields(output_dictionary, ipv4=True):
    with allure.step('Check that all expected fields under eth ip field exist in the output'):
        logging.info('Check that all expected fields under eth ip field exist in the output')
        if ipv4:
            field_to_check = [IbInterfaceConsts.IP_DHCP,
                              IbInterfaceConsts.ARPTIMEOUT,
                              IbInterfaceConsts.IP_ADDRESS,
                              IbInterfaceConsts.IP_GATEWAY]
        else:
            field_to_check = [IbInterfaceConsts.IP_DHCP,
                              IbInterfaceConsts.AUTOCONFIG]
        Tools.ValidationTool.verify_field_exist_in_json_output(output_dictionary, field_to_check).verify_result()


def verify_mac_address(expected_mac: str,
                       output_dictionary: dict):
    with allure.step('Verity that MAC address from nv show interface eth0 link matches expected value'):

        mac_address = output_dictionary[IbInterfaceConsts.LINK_MAC]
        assert mac_address == expected_mac, f"MAC address mismatch. Expected: {expected_mac}, Actual: {mac_address}"
