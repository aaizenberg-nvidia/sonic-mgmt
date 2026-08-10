import logging
import time
import re
import pytest

from retry.api import retry_call
from devts.infra.tools.validations.traffic_validations.port_check.port_checker import check_port_status_till_alive
from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from ngts.nvos_constants.constants_nvos import SystemConsts, ActionConsts, NvosConst
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import *
from ngts.nvos_tools.infra.DutUtilsTool import DutUtilsTool
from ngts.nvos_tools.infra.HostMethods import HostMethods
from ngts.nvos_tools.infra.SendCommandTool import SendCommandTool
from ngts.nvos_tools.infra.Tools import Tools
from ngts.nvos_tools.system.System import System
from ngts.nvos_tools.platform.Platform import Platform
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import AutoNegotiateConsts
from multiprocessing import Process
from ngts.tools.test_utils import allure_utils as allure

from ngts.tests_nvos.helpers.redmine_helpers import is_bug_active
from ngts.tests_nvos.constants import MINUTE


cumulus_owner = "lharo"


logger = logging.getLogger()


@pytest.mark.eth0
@pytest.mark.system
@pytest.mark.timeout(4 * MINUTE, func_only=True)
def test_interface_eth0_enable_disable(engines, topology_obj, serial_engine):
    """
    Connect via serial port, verify eth0 enable by default, can be disabled and enable it back

    flow:
    1. Verify eth0 is up and can ping
    2. Disable interface, check it's down and not reachable
    3. Negative test it  with random value and verify error
    4. Unset it back and verify
    """

    try:
        mgmt_port_name = DutUtilsTool.get_engine_interface_name(engines.dut, topology_obj)
        mgmt_port = Port(mgmt_port_name)
        with allure.step('Run show command on mgmt port and verify that each field has an appropriate value'):
            output_dictionary = Tools.OutputParsingTool.parse_show_interface_link_output_to_dictionary(
                mgmt_port.interface.link.show(dut_engine=serial_engine)).get_returned_value()

            Tools.ValidationTool.verify_field_value_in_output(output_dictionary=output_dictionary,
                                                              field_name=IbInterfaceConsts.LINK_STATE,
                                                              expected_value=NvosConsts.LINK_STATE_UP).verify_result()

        with allure.step('Negative validation'):
            mgmt_port.interface.link.state.set(op_param_name='invalid_value', apply=False,
                                               dut_engine=serial_engine).verify_result(False)

            logger.info('Check port status, should be up')
            check_port_status_till_alive(True, engines.dut.ip, engines.dut.ssh_port, tries=15, delay=2)
            output_dictionary = Tools.OutputParsingTool.parse_show_interface_link_output_to_dictionary(
                mgmt_port.interface.link.show(dut_engine=serial_engine)).get_returned_value()

            Tools.ValidationTool.verify_field_value_in_output(output_dictionary=output_dictionary,
                                                              field_name=IbInterfaceConsts.LINK_STATE,
                                                              expected_value=NvosConsts.LINK_STATE_UP).verify_result()

        with allure.step('Set mgmt port down and check the state updated accordingly'):
            mgmt_port.interface.link.state.set(op_param_name=NvosConsts.LINK_STATE_DOWN, apply=True,
                                               ask_for_confirmation=True, dut_engine=serial_engine).verify_result()

            logger.info('Check port status, should be down')
            check_port_status_till_alive(False, engines.dut.ip, engines.dut.ssh_port, tries=15, delay=2)
            output_dictionary = Tools.OutputParsingTool.parse_show_interface_link_output_to_dictionary(
                mgmt_port.interface.link.show(dut_engine=serial_engine)).get_returned_value()

            Tools.ValidationTool.verify_field_value_in_output(output_dictionary=output_dictionary,
                                                              field_name=IbInterfaceConsts.LINK_STATE,
                                                              expected_value=NvosConsts.LINK_STATE_DOWN).verify_result()
    finally:
        with allure.step('Unset mgmt port and make sure the port state is up and reachable'):
            mgmt_port.interface.link.state.unset(apply=True, ask_for_confirmation=True,
                                                 dut_engine=serial_engine).verify_result()
            logger.info('Check port status, should be up')
            check_port_status_till_alive(True, engines.dut.ip, engines.dut.ssh_port, tries=15, delay=2)

            output_dictionary = Tools.OutputParsingTool.parse_show_interface_link_output_to_dictionary(
                mgmt_port.interface.link.show(dut_engine=serial_engine)).get_returned_value()

            Tools.ValidationTool.verify_field_value_in_output(output_dictionary=output_dictionary,
                                                              field_name=IbInterfaceConsts.LINK_STATE,
                                                              expected_value=NvosConsts.LINK_STATE_UP).verify_result()


@pytest.mark.timeout(8 * MINUTE, func_only=True)
@pytest.mark.cumulus
@pytest.mark.eth0
@pytest.mark.system
@pytest.mark.simx
def test_interface_eth0_speed_duplex_autoneg(engines, devices, topology_obj, serial_engine, is_in_mec):
    """
    Verify speed, duplex, autoneg configuration parameters can be changed

    flow:
    1. Check default values
    2. Try to set autoneg to off
    3. Negative testing for speed, duplex, autoneg
    4. Set duplex to half on default speed 1G
    5. Set speed to not default with supported duplex, verify changes via ping
    6. Set autoneg to on and validate changes
    7. Try to set all speeds from list supported with all supported duplex
    8. Unset speed, validate changes
    """

    mgmt_port_name = DutUtilsTool.get_engine_interface_name(engines.dut, topology_obj)
    mgmt_port = Port(mgmt_port_name)
    with allure.step('Run show command on mgmt port and verify default values'):
        output_dictionary = Tools.OutputParsingTool.parse_show_interface_link_output_to_dictionary(
            mgmt_port.interface.link.show()).get_returned_value()

        Tools.ValidationTool.verify_field_value_in_output(output_dictionary=output_dictionary,
                                                          field_name=IbInterfaceConsts.LINK_SPEED,
                                                          expected_value="1G")

        Tools.ValidationTool.verify_field_value_in_output(output_dictionary=output_dictionary,
                                                          field_name=IbInterfaceConsts.LINK_DUPLEX,
                                                          expected_value="full")

        Tools.ValidationTool.verify_field_value_in_output(output_dictionary=output_dictionary,
                                                          field_name=IbInterfaceConsts.LINK_AUTO_NEGOTIATE,
                                                          expected_value=AutoNegotiateConsts.State.ENABLED.value)

    with allure.step(f'Negative validation with auto neg, {IbInterfaceConsts.LINK_AUTO_NEGOTIATE} must be on with default 1G speed'):
        mgmt_port.interface.link.set(op_param_name='auto_negotiate', op_param_value=AutoNegotiateConsts.State.DISABLED.value,
                                     apply=True).verify_result(False)
        NvueGeneralCli.detach_config(TestToolkit.engines.dut)

    with allure.step('Negative validation with invalid value for duplex'):
        mgmt_port.interface.link.set(op_param_name='duplex', op_param_value='a', apply=False).verify_result(False)
        NvueGeneralCli.detach_config(TestToolkit.engines.dut)

    with allure.step('Negative validation with invalid value speed'):
        mgmt_port.interface.link.set(op_param_name='speed', op_param_value='50F', apply=False).verify_result(False)
        NvueGeneralCli.detach_config(TestToolkit.engines.dut)

    '''with allure.step('Negative validation with half duplex and default speed 1G'):
        mgmt_port.interface.link.duplex.set(value='half', apply=True).verify_result(False)
        NvueGeneralCli.detach_config(TestToolkit.engines.dut)'''

    with allure.step('Set all supported speeds with all supported duplex'):
        list_supported_speeds = devices.dut.supported_eth0_speeds
        list_supported_duplex = devices.dut.supported_eth0_duplex
        for speed in list_supported_speeds:
            # Only allow full and half duplex if the speed is "10M" or "100M"
            if speed in ["10M"]:
                applicable_duplex = list_supported_duplex
            else:
                applicable_duplex = ["full"]  # For other speeds, only "full" duplex is applicable

            for duplex in applicable_duplex:
                mgmt_port.interface.link.set(op_param_name='speed', op_param_value=speed, apply=True,
                                             ask_for_confirmation=True).verify_result()
                result = mgmt_port.interface.link.set(op_param_name='duplex', op_param_value=duplex,
                                                      apply=True, ask_for_confirmation=True)
                Port.wait_for_port_state(mgmt_port, "up")

                if not result:
                    SendCommandTool.execute_command(TestToolkit.GeneralApi[TestToolkit.tested_api].
                                                    apply_config, engines.dut, True).verify_result()

                wait_for_param_changed(mgmt_port, IbInterfaceConsts.LINK_SPEED, speed)
                wait_for_param_changed(mgmt_port, IbInterfaceConsts.LINK_DUPLEX, duplex)

    with allure.step('Set autoneg to off'):
        # Auto-negotiate cannot be disabled at 1G speed, so find a non-1G speed from the supported list
        non_1g_speeds = [s for s in devices.dut.supported_eth0_speeds if s != '1G']
        if not non_1g_speeds:
            pytest.skip("No speeds that support disabling auto-negotiate (all supported speeds are 1G)")

        target_speed = non_1g_speeds[0]
        mgmt_port.interface.link.set(op_param_name='speed', op_param_value=target_speed, apply=True,
                                     ask_for_confirmation=True).verify_result()
        Port.wait_for_port_state(mgmt_port, "up")
        wait_for_param_changed(mgmt_port, IbInterfaceConsts.LINK_SPEED, target_speed)

        # On MEC network, peer switch requires auto-neg so port goes down when disabled
        mgmt_port.interface.link.set(op_param_name=IbInterfaceConsts.LINK_AUTO_NEGOTIATE, op_param_value=AutoNegotiateConsts.State.DISABLED.value, apply=True,
                                     ask_for_confirmation=True).verify_result()
        if is_in_mec:
            # Port is down, use serial to verify and re-enable
            wait_for_param_changed(mgmt_port, IbInterfaceConsts.LINK_AUTO_NEGOTIATE, AutoNegotiateConsts.State.DISABLED.value, dut_engine=serial_engine)
        else:
            Port.wait_for_port_state(mgmt_port, "up")
            wait_for_param_changed(mgmt_port, IbInterfaceConsts.LINK_AUTO_NEGOTIATE, AutoNegotiateConsts.State.DISABLED.value)

    with allure.step('Run show command on mgmt port and verify default values after unset'):
        # Use serial to unset if port is down on MEC network
        mgmt_port.interface.link.unset(op_param=IbInterfaceConsts.LINK_AUTO_NEGOTIATE, apply=True, ask_for_confirmation=True,
                                       dut_engine=serial_engine if is_in_mec else None).verify_result()
        check_port_status_till_alive(True, engines.dut.ip, engines.dut.ssh_port)
        Port.wait_for_port_state(mgmt_port, "up")

        wait_for_param_changed(mgmt_port, IbInterfaceConsts.LINK_AUTO_NEGOTIATE, AutoNegotiateConsts.State.ENABLED.value)

        mgmt_port.interface.link.unset(op_param='duplex', apply=True, ask_for_confirmation=True).verify_result()

        check_port_status_till_alive(True, engines.dut.ip, engines.dut.ssh_port)
        wait_for_param_changed(mgmt_port, IbInterfaceConsts.LINK_DUPLEX, 'full')

        mgmt_port.interface.link.unset(op_param='speed', apply=True, ask_for_confirmation=True).verify_result()
        check_port_status_till_alive(True, engines.dut.ip, engines.dut.ssh_port)
        wait_for_param_changed(mgmt_port, IbInterfaceConsts.LINK_SPEED, "1G")


@pytest.mark.cumulus
@pytest.mark.eth0
@pytest.mark.system
@pytest.mark.simx
def test_interface_eth0_mtu(engines, topology_obj):
    """
    Verify default mtu configuration(1500), check that we can configure possible values(1280-9216),
    negative check(1279, 9000), check changes, unset it to default

    flow:
    1. Check default values
    2. Negative testing
    3. Configure possible mtu
    4. Unset mtu, check default value
    """

    mgmt_port_name = DutUtilsTool.get_engine_interface_name(engines.dut, topology_obj)
    mgmt_port = Port(mgmt_port_name)
    with allure.step('Run show command on mgmt port and verify default values'):
        wait_for_mtu_changed(mgmt_port, 1500)

    with allure.step('Negative validation with not supported for eth mtu 256'):
        mgmt_port.interface.link.set(op_param_name='mtu', op_param_value='256').verify_result(False)
        NvueGeneralCli.detach_config(TestToolkit.engines.dut)
        logger.info('Check port status, should be up')
        check_port_status_till_alive(True, engines.dut.ip, engines.dut.ssh_port)
    with allure.step('Negative validation with not supported for eth mtu 9218'):
        result_obj = mgmt_port.interface.link.set(op_param_name='mtu', op_param_value='9218', apply=False)
        assert "Valid range for mtu is" in result_obj.get_info(False), "Set of invalid mtu should fail"
        NvueGeneralCli.detach_config(TestToolkit.engines.dut)
        logger.info('Check port status, should be up')
        check_port_status_till_alive(True, engines.dut.ip, engines.dut.ssh_port)
        wait_for_mtu_changed(mgmt_port, 1500)
    with allure.step('Set validation with supported for eth mtu 9000'):
        mgmt_port.interface.link.set(op_param_name='mtu', op_param_value='9000',
                                     apply=True, ask_for_confirmation=True).verify_result()
        logger.info('Check port status, should be up')
        check_port_status_till_alive(True, engines.dut.ip, engines.dut.ssh_port)
        wait_for_mtu_changed(mgmt_port, 9000)

    with allure.step('Unset mtu validation'):
        mgmt_port.interface.link.unset(op_param='mtu', apply=True, ask_for_confirmation=True).verify_result()
        logger.info('Check port status, should be up')
        check_port_status_till_alive(True, engines.dut.ip, engines.dut.ssh_port)
        wait_for_mtu_changed(mgmt_port, 1500)


@pytest.mark.cumulus
@pytest.mark.eth0
@pytest.mark.system
@pytest.mark.simx
@pytest.mark.air
def test_interface_eth0_description(engines, topology_obj):
    """
    Verify default description on mgmt interface, configure, check changes,

    flow:
    1. Check default values
    2. Configure possible description, and check changes
    3. Negative testing
    4. Unset description, check default value
    """
    mgmt_port_name = DutUtilsTool.get_engine_interface_name(engines.dut, topology_obj)
    mgmt_port = Port(mgmt_port_name)
    with allure.step('Run show command on mgmt port and verify default description'):
        output_dictionary = Tools.OutputParsingTool.parse_show_interface_output_to_dictionary(
            mgmt_port.interface.show()).get_returned_value()

        assert IbInterfaceConsts.DESCRIPTION not in output_dictionary.keys(), \
            "Expected not to have description field after unset command, but we still have this field."

    with allure.step('Set description with spaces on mgmt port'):
        mgmt_port.interface.set(op_param_name='description', op_param_value='"{0} description"'.format(mgmt_port_name),
                                apply=True).verify_result()
        NvueGeneralCli.detach_config(TestToolkit.engines.dut)
        output_dictionary = Tools.OutputParsingTool.parse_show_interface_output_to_dictionary(
            mgmt_port.interface.show()).get_returned_value()

        Tools.ValidationTool.verify_field_value_in_output(output_dictionary=output_dictionary,
                                                          field_name=IbInterfaceConsts.DESCRIPTION,
                                                          expected_value='{0} description'.format(mgmt_port_name))

    with allure.step('Set possible description on mgmt port'):
        mgmt_port.interface.set(op_param_name='description', op_param_value='"nvosdescription"',
                                apply=True).verify_result()
        output_dictionary = Tools.OutputParsingTool.parse_show_interface_output_to_dictionary(
            mgmt_port.interface.show()).get_returned_value()

        Tools.ValidationTool.verify_field_value_in_output(output_dictionary=output_dictionary,
                                                          field_name=IbInterfaceConsts.DESCRIPTION,
                                                          expected_value='nvosdescription')

    with allure.step('Unset possible description on mgmt port'):
        mgmt_port.interface.unset(op_param='description', apply=True).verify_result()
        output_dictionary = Tools.OutputParsingTool.parse_show_interface_output_to_dictionary(
            mgmt_port.interface.show()).get_returned_value()

        assert IbInterfaceConsts.DESCRIPTION not in output_dictionary.keys(), \
            "Expected not to have description field after unset command, but we still have this field."


@pytest.mark.cumulus
@pytest.mark.eth0
@pytest.mark.system
def test_interface_eth0_ip_address(engines, topology_obj, serial_engine):
    """
    Verify can configure ipv address, switch ip updated by dhcp

    flow:
    1. Get ip/mask from switch, verify it’s reachable by ping
    2. Negative testing for ipv4 and prefix, instead of ip only “dhcp”
    3. Disable ipv4 dhcp, verify it’s disabled, we can’t ping
    4. Configure static ip for this switch, check it by show command, ping
    5. Unset ipv4, dhcp, validate in show command and ping
    """
    mgmt_port_name = DutUtilsTool.get_engine_interface_name(engines.dut, topology_obj)
    mgmt_port = Port(mgmt_port_name)
    switch_ip = engines.dut.ip
    try:
        with allure.step('Run show command on mgmt port and verify default description'):
            output_dictionary = Tools.OutputParsingTool.parse_show_interface_pluggable_output_to_dictionary(
                mgmt_port.interface.ipv4.show()).get_returned_value()
            validate_interface_ip_address(switch_ip, output_dictionary, True)

        with allure.step('Negative validation for {0} ip'.format(mgmt_port_name)):
            res = mgmt_port.interface.ipv4.address.set(op_param_name='aa', apply=False, ask_for_confirmation=True)
            res.ignore_result()
            assert not res.result or "is not a" in res.returned_value, \
                "The operation succeeded while it is expected to fail"

        with allure.step(f'Delete ipv4 address for {mgmt_port_name} using 0.0.0.0/0 and expect failure'):
            mgmt_port.interface.ipv4.address.set(op_param_name='0.0.0.0/0', apply=True).verify_result(False, "is not a valid interface IP address")
            NvueGeneralCli.detach_config(engines.dut)

        with allure.step('Disable dhcp, check mgmt port unreachable'):
            mgmt_port.interface.ipv4.dhcp_client.set(SystemConsts.STATE, NvosConst.DISABLED, apply=True,
                                                     ask_for_confirmation=True, dut_engine=serial_engine).verify_result()
            logger.info('Check port status, should be down')
            check_port_status_till_alive(False, engines.dut.ip, engines.dut.ssh_port, tries=15, delay=2)
            verify_mgmt_port_stays_unreachable(engines.dut.ip, engines.dut.ssh_port)

        with allure.step('Verify dhcp state is disabled'):
            output_dictionary = Tools.OutputParsingTool.parse_show_interface_pluggable_output_to_dictionary(
                mgmt_port.interface.ipv4.show(dut_engine=serial_engine)).get_returned_value()
            assert output_dictionary['dhcp-client']['state'] == 'disabled', "DHCP state is not disabled"
            logger.info('DHCP state is disabled')

        with allure.step('Select random ipv4 and set it'):
            ip_address = Tools.IpTool.select_random_ipv4_address().verify_result()
            mgmt_port.interface.ipv4.address.set(op_param_name=ip_address, apply=True, ask_for_confirmation=True, dut_engine=serial_engine).verify_result()
            logger.info('Check port status, should be down')
            check_port_status_till_alive(False, engines.dut.ip, engines.dut.ssh_port, tries=5, delay=2)
            validate_interface_ip_address(ip_address.split('/')[0], output_dictionary={}, validate_in=True,
                                          run_show=True, mgmt_port=mgmt_port, dut_engine=serial_engine)

    except Exception as e:
        logger.info("Received Exception during test_interface_eth0_ip_address: {}".format(e))
        raise e
    finally:
        with allure.step('Unset ipv4 and dhcp and check port reachable'):
            mgmt_port.interface.ipv4.address.unset(apply=True, ask_for_confirmation=True, dut_engine=serial_engine).verify_result()
            mgmt_port.interface.ipv4.dhcp_client.set(SystemConsts.STATE, NvosConst.ENABLED, apply=True,
                                                     ask_for_confirmation=True, dut_engine=serial_engine).verify_result()

            logger.info('Check port status, should be up')
            check_port_status_till_alive(True, engines.dut.ip, engines.dut.ssh_port, tries=15, delay=2)
            output_dictionary = Tools.OutputParsingTool.parse_show_interface_pluggable_output_to_dictionary(
                mgmt_port.interface.ipv4.show()).get_returned_value()
            validate_interface_ip_address(switch_ip, output_dictionary, True)


@pytest.mark.eth0
@pytest.mark.system
@pytest.mark.simx
@pytest.mark.air
def test_interface_eth0_show_dhcp(engines, topology_obj):
    """
    Verify all default fields in nv show interface eth0 ipv4 dhcp-client and ipv6 dhcp-client

    flow:
    1. Check all fields are exist in nv show interface eth0 ipv4 dhcp-client
    2. Check all fields exist in nv show interface eth0 ipv6 dhcp-client
    """
    mgmt_port_name = DutUtilsTool.get_engine_interface_name(engines.dut, topology_obj)
    mgmt_port = Port(mgmt_port_name)
    with allure.step('Run show command on mgmt port and verify default description'):
        # Test IPv4 interface
        output_dictionary_ipv4 = Tools.OutputParsingTool.parse_show_interface_pluggable_output_to_dictionary(
            mgmt_port.interface.ipv4.show()).get_returned_value()

        # Test IPv6 interface
        output_dictionary_ipv6 = Tools.OutputParsingTool.parse_show_interface_pluggable_output_to_dictionary(
            mgmt_port.interface.ipv6.show()).get_returned_value()

        with allure.step("Validate all expected fields in IPv4 show output"):
            Tools.ValidationTool.verify_field_exist_in_json_output(output_dictionary_ipv4, ["dhcp-client"]) \
                .verify_result()

        with allure.step("Validate all expected fields in IPv6 show output"):
            Tools.ValidationTool.verify_field_exist_in_json_output(output_dictionary_ipv6, ["dhcp-client"]) \
                .verify_result()
            logging.info("All expected fields were found")

        with allure.step("Test DHCP lease properties for IPv4"):
            # Test IPv4 lease properties
            lease_output_ipv4 = mgmt_port.interface.ipv4.dhcp_client.lease.show()
            assert lease_output_ipv4 is not None, "IPv4 DHCP lease should be accessible"

            # Check for key lease properties (if available)
            expected_lease_properties = [
                'ntp-servers', 'domain-search', 'domain-name-servers',
                'subnet-mask', 'server-name', 'routers', 'renew', 'rebind',
                'host-name', 'fixed-address', 'filename', 'expire',
                'domain-name', 'dhcp-message-type', 'dhcp-lease-time',
                'broadcast-address'
            ]

            for prop in expected_lease_properties:
                if prop in lease_output_ipv4:
                    logger.info(f" Found IPv4 lease property: {prop}")
                else:
                    logger.info(f" IPv4 lease property not present: {prop}")

        with allure.step("Test DHCP lease properties for IPv6"):
            # Test IPv6 lease properties
            lease_output_ipv6 = mgmt_port.interface.ipv6.dhcp_client.lease.show()
            assert lease_output_ipv6 is not None, "IPv6 DHCP lease should be accessible"

            # Check for key IPv6 lease properties (if available)
            expected_ipv6_lease_properties = [
                'ia-na', 'ntp-servers', 'name-servers', 'domain-search',
                'server-id', 'client-id'
            ]

            for prop in expected_ipv6_lease_properties:
                if prop in lease_output_ipv6:
                    logger.info(f" Found IPv6 lease property: {prop}")
                else:
                    logger.info(f" IPv6 lease property not present: {prop}")


@pytest.mark.cumulus
@pytest.mark.eth0
@pytest.mark.system
@pytest.mark.simx
def test_interface_eth0_dhcp_hostname(engines, topology_obj, serial_engine):
    """
    Verify switch receive hostname by dhcp

    flow:
    1. Check hostname received by dhcp, validate it’s same in show system and iblinkinfo command, field lease yes
    2. Disable dhcp, unset hostname, verify it’s nvos, is running no, no lease field
    3. Disable dhcp set-hostname, verify changed for ipv4 and ipv6 dhcp
    4. Enable dhcp, check we didn’t receive hostname
    5. Unset set-hostname and check we received hostname as we have on start of the test, configuration for ipv4 and ipv6 dhcp same, can ping
    """
    expect_timeout = 30
    mgmt_port_name = DutUtilsTool.get_engine_interface_name(engines.dut, topology_obj)
    mgmt_port = Port(mgmt_port_name)
    system = System()

    try:
        with allure.step('Run show ip dhcp command and check default values and dhcp hostname'):
            output_dictionary = Tools.OutputParsingTool.parse_show_interface_pluggable_output_to_dictionary(
                mgmt_port.interface.ipv4.dhcp_client.show()).get_returned_value()

            system_output = OutputParsingTool.parse_json_str_to_dictionary(system.show()).get_returned_value()

            dhcp_hostname = HostMethods.get_dhcp_hostname(topology_obj)
            assert dhcp_hostname, "No dhcp_hostname received from noga"

            # Check lease information instead of has-lease (new schema)
            with allure.step('Check DHCP lease information'):
                logger.info(f"dhcp hostname from noga: {dhcp_hostname}, hostname from show system command:{system_output['hostname']}")
                lease_output = mgmt_port.interface.ipv4.dhcp_client.lease.show()
                assert lease_output is not None, "DHCP lease information should be available"
                logger.info(" DHCP lease information is accessible")

            Tools.ValidationTool.verify_field_value_in_output(output_dictionary=output_dictionary,
                                                              field_name='set-hostname',
                                                              expected_value='enabled').verify_result()

            Tools.ValidationTool.verify_field_value_in_output(output_dictionary=output_dictionary,
                                                              field_name='state',
                                                              expected_value='enabled').verify_result()

            HostMethods.assert_system_hostname_matches_dhcp(system_output, dhcp_hostname)

        with allure.step('Disable dhcp and unset hostname, check port down and not reachable'):
            serial_engine.serial_engine.sendline("nv set interface {} ipv4 dhcp-client state disabled".format(mgmt_port_name))
            serial_engine.serial_engine.sendline("nv config apply")
            serial_engine.serial_engine.expect("Are you sure?", timeout=expect_timeout)
            serial_engine.serial_engine.sendline("y")
            serial_engine.serial_engine.expect("applied", timeout=expect_timeout)

            logger.info('Check port status, should be down')
            check_port_status_till_alive(False, engines.dut.ip, engines.dut.ssh_port)

            serial_engine.serial_engine.sendline("nv show interface {} ipv4 dhcp-client".format(mgmt_port_name))
            serial_engine.serial_engine.expect("state         disabled", timeout=expect_timeout)

        with allure.step('Disable dhcp set-hostname, check port down and not reachable'):
            serial_engine.serial_engine.sendline("nv set interface {} ipv4 dhcp-client set-hostname disabled".format(mgmt_port_name))
            serial_engine.serial_engine.sendline("nv config apply")
            serial_engine.serial_engine.expect("Are you sure?", timeout=expect_timeout)
            serial_engine.serial_engine.sendline("y")
            serial_engine.serial_engine.expect("applied", timeout=expect_timeout)

            logger.info('Check port status, should be down')
            check_port_status_till_alive(False, engines.dut.ip, engines.dut.ssh_port)
            serial_engine.serial_engine.sendline("nv show interface {} ipv4 dhcp-client".format(mgmt_port_name))
            serial_engine.serial_engine.expect("state         disabled", timeout=expect_timeout)
            serial_engine.serial_engine.sendline("nv show interface {} ipv6 dhcp-client".format(mgmt_port_name))
            serial_engine.serial_engine.expect("state         disabled", timeout=expect_timeout)

        with allure.step('Set hostname and enable dhcp, check hostname not changed, check port up'):
            serial_engine.serial_engine.sendline("nv set system hostname {}".format(SystemConsts.HOSTNAME))
            serial_engine.serial_engine.sendline("nv config apply")
            serial_engine.serial_engine.expect("Are you sure?", timeout=expect_timeout)
            serial_engine.serial_engine.sendline("y")
            serial_engine.serial_engine.expect("applied", timeout=expect_timeout)
            logger.info('Check port status, should be down')
            check_port_status_till_alive(False, engines.dut.ip, engines.dut.ssh_port)
            serial_engine.serial_engine.sendline("nv set interface {} ipv4 dhcp-client state enabled".format(mgmt_port_name))
            serial_engine.serial_engine.sendline("nv config apply")
            serial_engine.serial_engine.expect("Are you sure?", timeout=expect_timeout)
            serial_engine.serial_engine.sendline("y")
            serial_engine.serial_engine.expect("applied", timeout=expect_timeout)

            logger.info('Check port status, should be up')
            check_port_status_till_alive(True, engines.dut.ip, engines.dut.ssh_port)

            dhcp_output = Tools.OutputParsingTool.parse_show_interface_pluggable_output_to_dictionary(
                mgmt_port.interface.ipv4.dhcp_client.show()).get_returned_value()

            Tools.ValidationTool.verify_field_value_in_output(output_dictionary=dhcp_output, field_name='state',
                                                              expected_value='enabled').verify_result()
    finally:
        with allure.step('Unset dhcp, , check port up'):
            mgmt_port.interface.ipv4.dhcp_client.unset(apply=True, ask_for_confirmation=True).verify_result()
            logger.info('Check port status, should be up')
            check_port_status_till_alive(True, engines.dut.ip, engines.dut.ssh_port)

            dhcp_output = Tools.OutputParsingTool.parse_show_interface_pluggable_output_to_dictionary(
                mgmt_port.interface.ipv4.dhcp_client.show()).get_returned_value()

            Tools.ValidationTool.verify_field_value_in_output(output_dictionary=dhcp_output,
                                                              field_name='state',
                                                              expected_value='enabled').verify_result()
            Tools.ValidationTool.verify_field_value_in_output(output_dictionary=dhcp_output,
                                                              field_name='set-hostname',
                                                              expected_value='enabled').verify_result()

        with allure.step('Check hostname received by dhcp'):
            system.unset(op_param=SystemConsts.HOSTNAME, apply=True, ask_for_confirmation=True, dut_engine=serial_engine)
            wait_for_hostname_changed(system, dhcp_hostname)


@pytest.mark.cumulus
@pytest.mark.eth0
@pytest.mark.system
@pytest.mark.simx
@pytest.mark.air
def test_mgmt_interface_default(engines, topology_obj):
    """
    Verify default fields, stats, logs

    flow:
    1. Check default fields
    2. Check mgmt port ip is reachable
    3. Check logs
    4. Check stats
    """
    system = System(None)
    mgmt_port_name = DutUtilsTool.get_engine_interface_name(engines.dut, topology_obj)
    mgmt_port = Port(mgmt_port_name)

    with allure.step('Run show command on mgmt port and verify default values'):
        output_dictionary = Tools.OutputParsingTool.parse_show_interface_output_to_dictionary(
            mgmt_port.interface.show()).get_returned_value()

        field_to_check = [IbInterfaceConsts.TYPE, IbInterfaceConsts.LINK,
                          IbInterfaceConsts.IFINDEX, IbInterfaceConsts.IPV4, IbInterfaceConsts.IPV6]
        Tools.ValidationTool.verify_field_exist_in_json_output(output_dictionary, field_to_check).verify_result()

    with allure.step('Verify mgmt interface is reachable'):
        check_port_status_till_alive(True, engines.dut.ip, engines.dut.ssh_port)

    with allure.step('Check logs for mgmt interface exist'):
        show_output_lines = system.log.file.show(op_param=" | grep '" + mgmt_port_name + "'", output_format="").splitlines()
        assert len(show_output_lines) > 0, "Mgmt port {} not found in system log".format(mgmt_port_name)

    with allure.step('Check stats'):
        output_dictionary = Tools.OutputParsingTool.parse_show_interface_stats_output_to_dictionary(
            mgmt_port.interface.counters.show()).get_returned_value()
        field_to_check = [IbInterfaceConsts.LINK_STATS_CARRIER_TRANSITION, IbInterfaceConsts.LINK_STATS_IN_BYTES,
                          IbInterfaceConsts.LINK_STATS_IN_DROPS, IbInterfaceConsts.LINK_STATS_IN_ERRORS,
                          IbInterfaceConsts.LINK_STATS_IN_PKTS, IbInterfaceConsts.LINK_STATS_OUT_BYTES,
                          IbInterfaceConsts.LINK_STATS_OUT_DROPS, IbInterfaceConsts.LINK_STATS_OUT_ERRORS,
                          IbInterfaceConsts.LINK_STATS_OUT_PKTS]
        Tools.ValidationTool.verify_field_exist_in_json_output(output_dictionary, field_to_check).verify_result()


@pytest.mark.cumulus
@pytest.mark.eth0
@pytest.mark.system
@pytest.mark.simx
def test_mgmt_interface_dhcpv6_ztp(engines, topology_obj):
    """
    Test to verify ztp dhcpv6 vendor class bug https://redmine.mellanox.com/issues/3963391

    flow:
    1. Run tcpdump and catch 5 packets dhcpv6 ztp vendor class
    """
    mgmt_port_name = DutUtilsTool.get_engine_interface_name(engines.dut, topology_obj)

    with allure.step('Run tcpdump and catch dhcpv6 ztp vendor class'):
        tcpdump_output = Tools.IpTool.run_tcpdump(engines.dut, mgmt_port_name,
                                                  filter='port 546 or port 547 -e -c 5 -n -vv')
        match = re.search(r"(\d+) packets received by filter", tcpdump_output)
        packets_received = int(match.group(1))
        assert packets_received >= 5, f"Only {packets_received} packets received, less than 5"


@pytest.mark.eth0
@pytest.mark.system
@pytest.mark.simx
def test_mgmt_interface_dhcp_option_60(engines, topology_obj):
    """
    Test to verify availability of option 60(Vendor class) in dhcp

    flow:
    1. Start a subprocess to start monitoring tcpdump to validate option 60
    2. Renew DHCP so that packet transfers starts happening
    """

    platform = Platform()
    mgmt_port_name = DutUtilsTool.get_engine_interface_name(engines.dut, topology_obj)
    mgmt_port = Port(mgmt_port_name)
    output = OutputParsingTool.parse_json_str_to_dictionary(platform.show()).get_returned_value()
    product_name = output['product-name']
    hostname = engines.dut.run_cmd("hostname")
    regex = r"Hostname.*{}.*Vendor-Class.*{}".format(hostname, product_name)

    try:
        with allure.step("Create a separate process to run tcpdump and validate option 60 in packets"):
            tcpdump_process = Process(target=run_tcpdump_validate_option_60,
                                      args=(engines.dut, mgmt_port_name, regex))
            tcpdump_process.start()

        with allure.step("Test DHCP actions - renew only"):
            # Test renew action (existing functionality)
            mgmt_port.interface.ipv4.dhcp_client.action(ActionConsts.RENEW)

    finally:
        with allure.step("Combine with tcpdump process to finish gracefully"):
            tcpdump_process.join()
            assert tcpdump_process.exitcode == 0, "Vendor-Class Identifier (Option 60) not found in dhcp packets"


@pytest.mark.eth0
@pytest.mark.system
@pytest.mark.simx
def test_mgmt_interface_dhcp_option_60_conf_file(engines):
    """
    Test to verify availability of option 60(Vendor class identifier) in dhcp

    flow:
    1. Check vendor class identifier (DHCP option 60) is present in dh client conf file
    """

    with allure.step('Open dh client conf file and validate option 60'):
        dh_client_conf = engines.dut.run_cmd("cat " + SystemConsts.DH_CLIENT_CONF_FILE)
        assert "vendor-class-identifier" in dh_client_conf, "Vendor class identifier not present in dh client conf file"


@pytest.mark.eth0
@pytest.mark.system
def test_interface_eth0_dhcp_lease_properties(engines, topology_obj):
    """
    Test DHCP lease properties validation for both IPv4 and IPv6 on randomly selected eth interface

    Flow:
    1. Randomly select between eth0 and eth1
    2. Enable both IPv4 and IPv6 DHCP clients
    3. Verify all lease properties are accessible for both protocols
    4. Validate property types and structure for both IPv4 and IPv6
    """
    import random

    # Randomly select between eth0 and eth1
    available_ports = ['eth0', 'eth1']
    selected_port = random.choice(available_ports)
    logger.info(f"Randomly selected port: {selected_port}")

    mgmt_port_name = DutUtilsTool.get_engine_interface_name(engines.dut, topology_obj)
    mgmt_port = Port(mgmt_port_name)

    with allure.step(f'Enable IPv4 and IPv6 DHCP clients on {selected_port}'):
        mgmt_port.interface.ipv4.dhcp_client.set(
            op_param_name='state',
            op_param_value='enabled',
            apply=True
        ).verify_result()

        mgmt_port.interface.ipv6.dhcp_client.set(
            op_param_name='state',
            op_param_value='enabled',
            apply=True
        ).verify_result()

    with allure.step('Test IPv4 lease properties'):
        lease_output_ipv4 = mgmt_port.interface.ipv4.dhcp_client.lease.show()
        assert lease_output_ipv4 is not None, "IPv4 DHCP lease should be accessible"

        # Check for key IPv4 lease properties
        expected_ipv4_lease_properties = [
            'ntp-servers', 'domain-search', 'domain-name-servers',
            'subnet-mask', 'server-name', 'routers', 'renew', 'rebind',
            'host-name', 'fixed-address', 'filename', 'expire',
            'domain-name', 'dhcp-message-type', 'dhcp-lease-time',
            'broadcast-address'
        ]

        missing_properties = []
        for prop in expected_ipv4_lease_properties:
            if prop in lease_output_ipv4:
                logger.info(f" Found IPv4 lease property: {prop}")
            else:
                logger.info(f"IPv4 lease property not present: {prop}")
                missing_properties.append(prop)

        assert len(missing_properties) == 0, f"Missing IPv4 lease properties: {missing_properties}"

    with allure.step(f'Test IPv6 lease properties for {selected_port}'):
        lease_output_ipv6 = mgmt_port.interface.ipv6.dhcp_client.lease.show()
        assert lease_output_ipv6 is not None, "IPv6 DHCP lease should be accessible"

        # Check for key IPv6 lease properties (based on actual output)
        expected_ipv6_lease_properties = [
            'ia-na', 'server-id', 'client-id'
        ]

        missing_properties = []
        for prop in expected_ipv6_lease_properties:
            if prop in lease_output_ipv6:
                logger.info(f" Found IPv6 lease property: {prop}")
            else:
                logger.info(f" IPv6 lease property not present: {prop}")
                missing_properties.append(prop)

        assert len(missing_properties) == 0, f"Missing IPv6 lease properties: {missing_properties}"

    with allure.step('Verify both DHCP clients are running'):
        for ip_version in ['ipv4', 'ipv6']:
            with allure.independent_step(f"Testing {ip_version} DHCP client"):
                dhcp_output = Tools.OutputParsingTool.parse_show_interface_pluggable_output_to_dictionary(
                    getattr(mgmt_port.interface, ip_version).dhcp_client.show()).get_returned_value()
                Tools.ValidationTool.verify_field_value_in_output(
                    output_dictionary=dhcp_output,
                    field_name='state',
                    expected_value='enabled'
                ).verify_result()


def run_tcpdump_validate_option_60(dut, mgmt_port_name, regex):
    with allure.step('Run tcpdump and validate option 60'):
        retry_call(validate_dhcp_option_60_tcpdump, [dut, mgmt_port_name, regex],
                   exceptions=AssertionError, tries=5, delay=0)


def validate_dhcp_option_60_tcpdump(dut, mgmt_port_name, regex):
    tcpdump_output = Tools.IpTool.run_tcpdump(dut, mgmt_port_name, filter='port 67 or port 68 -c 10 -n -vv')
    # split packets
    tcp_dumps = tcpdump_output.split("Client-Ethernet-Address")
    for tcpdump in tcp_dumps:
        if re.search(regex, tcpdump, re.DOTALL):
            return
    assert False, "Vendor-Class Identifier (Option 60) not found in dhcp packets"


@retry(Exception, tries=6, delay=5)
def validate_interface_ip_address(address, output_dictionary, validate_in=True, run_show=False, mgmt_port=None, dut_engine=None):
    """

    :param address: ip address (could be ipv4 or ipv6)
    :param output_dictionary: the output after running nv show interface ib0 ip (ignored when run_show=True)
    :param validate_in: True after running set cmd, False after running unset
    :param run_show: when True, run show and parse inside (for retry with fresh data). Requires mgmt_port.
    :param mgmt_port: Port object for show when run_show=True
    :param dut_engine: engine for show when run_show=True (e.g. serial_engine)
    """
    with allure.step('check the address field is updated as expected'):
        if run_show:
            assert mgmt_port is not None, "mgmt_port required when run_show=True"
            output_dictionary = Tools.OutputParsingTool.parse_show_interface_pluggable_output_to_dictionary(
                mgmt_port.interface.ipv4.show(dut_engine=dut_engine)).get_returned_value()
        output_dictionary = str(output_dictionary['address'].keys())
        if validate_in:
            assert address in output_dictionary, "address not found: {add}".format(add=address)
        if not validate_in:
            assert address not in output_dictionary, "address found and should be deleted: {add}".format(add=address)


@retry(Exception, tries=10, delay=2)
def wait_for_mtu_changed(port_obj, mtu_to_verify):
    with allure.step("Waiting for mgmt port mtu changed to {}".format(mtu_to_verify)):
        output_dictionary = Tools.OutputParsingTool.parse_show_interface_link_output_to_dictionary(
            port_obj.interface.link.show()).get_returned_value()
        current_mtu = output_dictionary[IbInterfaceConsts.LINK_MTU]
        assert current_mtu == mtu_to_verify, "Current mtu {} is not as expected {}".format(current_mtu, mtu_to_verify)


@retry(Exception, tries=15, delay=2)
def wait_for_hostname_changed(system, dhcp_hostname):
    with (allure.step("Waiting for system hostname changed to {}".format(dhcp_hostname))):
        system_output = OutputParsingTool.parse_json_str_to_dictionary(system.show()).get_returned_value()
        HostMethods.assert_system_hostname_matches_dhcp(system_output, dhcp_hostname)


def verify_mgmt_port_stays_unreachable(dut_ip, dut_ssh_port, tries=5, delay=2):
    """
    Assert mgmt port stays unreachable for a short window (~35s).

    After disabling the mgmt-interface DHCP client, the port should stop being reachable
    and stay down. If `check_port_status_till_alive(True, ...)` ever returns within the
    window, the port came back up (DHCP client never actually stopped, or ZTP re-leased
    the same IP), that is the bug we want to catch.

    Per-iteration cost is ~7s (5s socket timeout when port is CLOSED + `delay`).
    """
    with allure.step(f"Verify mgmt port {dut_ip}:{dut_ssh_port} stays unreachable (~{tries * (5 + delay)}s)"):
        try:
            check_port_status_till_alive(True, dut_ip, dut_ssh_port, tries=tries, delay=delay)
        except Exception:
            return  # stayed CLOSED the whole window, DHCP truly disabled
        raise AssertionError(
            f"Mgmt port {dut_ip}:{dut_ssh_port} became reachable again after disabling DHCP. "
            "Applied state says 'disabled' but the DHCP client is still running"
        )


@retry(Exception, tries=25, delay=2)
def wait_for_param_changed(port_obj, param, param_to_verify, dut_engine=None):
    with allure.step(f"Waiting for {param} changed to {param_to_verify}"):
        output_dictionary = Tools.OutputParsingTool.parse_show_interface_link_output_to_dictionary(
            port_obj.interface.link.show(dut_engine=dut_engine)).get_returned_value()
        current_param = output_dictionary[f'{param}']
        assert current_param == param_to_verify, f"Current {current_param} is not as expected {param_to_verify}"


@pytest.mark.eth0
@pytest.mark.system
@pytest.mark.timeout(8 * MINUTE, func_only=True)
def test_interface_eth0_show_after_reboot(engines, topology_obj, serial_engine):
    """
    Verify eth0 interface is properly displayed after reboot with saved static IP config.

    flow:
    1. Disable DHCP, set static IP, save config
    2. Reboot the system
    3. Verify eth0 appears in interface show and lldp show
    4. Restore DHCP configuration
    """
    mgmt_port_name = DutUtilsTool.get_engine_interface_name(engines.dut, topology_obj)
    logger.info(f"mgmt port: {mgmt_port_name}")
    mgmt_port = Port(mgmt_port_name)

    try:
        with allure.step('Get current IP address'):
            ipv4_output = Tools.OutputParsingTool.parse_show_interface_pluggable_output_to_dictionary(
                mgmt_port.interface.ipv4.show()).get_returned_value()
            validate_interface_ip_address(engines.dut.ip, ipv4_output, True)
            switch_ip = [ip for ip in ipv4_output['address'] if engines.dut.ip in ip][0]

        with allure.step('Disable DHCP and set static IP'):
            mgmt_port.interface.ipv4.dhcp_client.set(
                op_param_name='state', op_param_value='disabled',
                dut_engine=serial_engine, apply=True, ask_for_confirmation=True).verify_result()
            check_port_status_till_alive(False, engines.dut.ip, engines.dut.ssh_port, tries=15, delay=2)
            mgmt_port.interface.ipv4.address.set(
                op_param_name=switch_ip, dut_engine=serial_engine,
                apply=True, ask_for_confirmation=True).verify_result()
            check_port_status_till_alive(True, engines.dut.ip, engines.dut.ssh_port, tries=15, delay=2)

        with allure.step('Verify static IP, save and reboot'):
            retry_call(lambda: validate_interface_ip_address(
                switch_ip, Tools.OutputParsingTool.parse_show_interface_pluggable_output_to_dictionary(
                    mgmt_port.interface.ipv4.show()).get_returned_value(), True),
                tries=10, delay=2)
            NvueGeneralCli.save_config(engine=engines.dut)
            System().reboot.action_reboot()
            _reconnect_serial(serial_engine)

            with allure.step('Wait for system to become ready after reboot'):
                retry_call(lambda: mgmt_port.interface.ipv4.show(dut_engine=serial_engine),
                           tries=60, delay=5)

            with allure.step('Verify mgmt interface shown after reboot'):
                interface = Interface(None)
                assert mgmt_port_name in interface.show(dut_engine=serial_engine), \
                    f"Mgmt port {mgmt_port_name} not found in interface show"
                assert mgmt_port_name in interface.lldp.show(dut_engine=serial_engine), \
                    f"Mgmt port {mgmt_port_name} not found in interface lldp show"

    finally:
        with allure.step('Restore DHCP configuration'):
            _reconnect_serial(serial_engine)
            mgmt_port.interface.ipv4.unset(
                dut_engine=serial_engine, apply=True, ask_for_confirmation=True).verify_result()
            check_port_status_till_alive(True, engines.dut.ip, engines.dut.ssh_port, tries=15, delay=2)
            ipv4_output = Tools.OutputParsingTool.parse_show_interface_pluggable_output_to_dictionary(
                mgmt_port.interface.ipv4.show()).get_returned_value()
            validate_interface_ip_address(engines.dut.ip, ipv4_output, validate_in=True, run_show=True, mgmt_port=mgmt_port)
            NvueGeneralCli.save_config(engine=serial_engine)


def _reconnect_serial(serial_engine):
    """Close stale serial connection and create a fresh one."""
    try:
        if serial_engine.serial_engine is not None:
            if serial_engine.serial_engine.logfile:
                serial_engine.serial_engine.logfile.close()
            serial_engine.serial_engine.close()
    except (OSError, IOError):
        pass
    serial_engine.serial_engine = None
    serial_engine.create_serial_engine(disconnect_existing_login=True)
