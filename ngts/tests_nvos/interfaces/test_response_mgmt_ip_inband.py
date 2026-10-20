import pytest
import time
import logging
from ngts.nvos_constants.constants_nvos import ApiType, DatabaseConst, IpConsts, UfmMadConsts
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import NvosConsts
from ngts.nvos_tools.infra.DatabaseTool import DatabaseTool
from ngts.nvos_tools.infra.Fae import Fae
from ngts.nvos_tools.infra.IpTool import IpTool
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.RandomizationTool import RandomizationTool
from ngts.nvos_tools.infra.RegisterTool import RegisterTool
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.tools.test_utils import allure_utils as allure


logger = logging.getLogger(__name__)


@pytest.mark.interfaces
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
def test_configure_feature_state(engines, devices, start_sm, test_api):
    """
    Validate configuring feature state by using the fae commands:
    - show fae ib ufm-mad command
    - set/unset ib ufm-mad state command

    Test flow:
    1. Choose mgmt port (eth0|eth1) and get its addresses
    2. Verify feature state enabled and IP address configured
    3. Disable ufm-mad feature
    4. Validate ufm-mad state disabled and IP address is empty
    5. Enable feature (by set command)
    6. Verify feature state enabled and IP address configured
    7. Disable ufm-mad feature
    8. Enable feature (by unset command)
    9. Verify feature state enabled and IP address configured
    10. Set feature state to default
    """
    TestToolkit.tested_api = test_api
    engines_dut = engines.dut
    devices_dut = devices.dut
    ib_host_engine = getattr(engines, devices.dut.ib_host_player)
    fae = Fae()

    try:
        with allure.step("Renew interface eth0 and eth1 ipv6 dhcp-client"):
            engines_dut.run_cmd('nv action renew interface eth0 ipv6 dhcp-client')
            engines_dut.run_cmd('nv action renew interface eth1 ipv6 dhcp-client')
            time.sleep(30)

        with allure.step("Choose mgmt port (eth0|eth1) and get its addresses"):
            mgmt_port, mgmt_ip_dict, port_name = choose_mgmt_port(engines_dut, devices_dut)

        with allure.step("Verify feature state enabled and IP address configured"):
            verify_ufm_mad_configuration(fae, engines_dut, port_name, devices_dut, ib_host_engine,
                                         UfmMadConsts.State.ENABLED.value, mgmt_ip_dict[UfmMadConsts.IPV4],
                                         mgmt_ip_dict[UfmMadConsts.IPV6])

        with allure.step("Disable ufm-mad feature"):
            fae.ib.ufm_mad.set(op_param_name=UfmMadConsts.STATE, op_param_value=UfmMadConsts.State.DISABLED.value,
                               apply=True, ask_for_confirmation=True).verify_result()

        with allure.step("Validate ufm-mad state disabled and IP address is empty"):
            verify_ufm_mad_configuration(fae, engines_dut, port_name, devices_dut, ib_host_engine,
                                         UfmMadConsts.State.DISABLED.value)

        with allure.step("Enable feature (by set command)"):
            fae.ib.ufm_mad.set(op_param_name=UfmMadConsts.STATE, op_param_value=UfmMadConsts.State.ENABLED.value,
                               apply=True).verify_result()

        with allure.step("Verify feature state enabled and IP address configured"):
            verify_ufm_mad_configuration(fae, engines_dut, port_name, devices_dut, ib_host_engine,
                                         UfmMadConsts.State.ENABLED.value, mgmt_ip_dict[UfmMadConsts.IPV4],
                                         mgmt_ip_dict[UfmMadConsts.IPV6])
            time.sleep(UfmMadConsts.CONFIG_TIME)

        with allure.step("Disable ufm-mad feature"):
            fae.ib.ufm_mad.set(op_param_name=UfmMadConsts.STATE, op_param_value=UfmMadConsts.State.DISABLED.value,
                               apply=True).verify_result()

        with allure.step("Enable feature (by unset command)"):
            fae.ib.ufm_mad.unset(op_param=UfmMadConsts.STATE, apply=True).verify_result()

        with allure.step("Verify feature state enabled and IP address configured"):
            verify_ufm_mad_configuration(fae, engines_dut, port_name, devices_dut, ib_host_engine,
                                         UfmMadConsts.State.ENABLED.value, mgmt_ip_dict[UfmMadConsts.IPV4],
                                         mgmt_ip_dict[UfmMadConsts.IPV6])

    finally:
        with allure.step("Set feature state to default"):
            fae.ib.ufm_mad.unset(op_param=UfmMadConsts.STATE, apply=True).verify_result()


@pytest.mark.interfaces
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
def test_configure_mgmt_port_ipv4(engines, devices, topology_obj, prepare_traffic, test_api, serial_engine):
    """
    Validate configuring management port ipv4 address in several cases

    Test flow:
    1. Choose mgmt port (eth0|eth1) and read its IPv4/IPv6 addresses
    2. Verify UFM-MAD is enabled and both addresses are advertised
    3. Disable UFM-MAD
    4. Configure a static IPv4 address on the mgmt port
    5. Verify UFM-MAD is disabled and advertised IPv4/IPv6 are empty
    6. Verify State DB table reflects "disabled + empty addresses"
    7. Enable UFM-MAD (set state ENABLED)
    8. Verify UFM-MAD is enabled and the configured IPs are advertised (static IPv4 + IPv6 SLAAC)
    9. Verify State DB table reflects "enabled + addresses present"
    10. Unset the static IPv4 address while UFM-MAD is enabled (revert IPv4 to the original mgmt IP)
    11. Verify UFM-MAD stays enabled and IPv4/IPv6 match the current/original mgmt IP values
    12. Verify State DB table reflects the updated IPv4 address
    13. Disable the IPv4 DHCP client (UFM-MAD remains enabled)
    14. Verify IPv4 is NOT advertised while IPv6 remains advertised
    15. Verify State DB table reflects "enabled + IPv4 empty + IPv6 present"
    16. Restore defaults (mgmt port address and UFM-MAD state)
    """
    TestToolkit.tested_api = test_api
    engines_dut = engines.dut
    devices_dut = devices.dut
    ib_host_engine = getattr(engines, devices.dut.ib_host_player)
    fae = Fae()
    mgmt_port = None

    try:
        with allure.step("Choose mgmt port (eth0|eth1) and get its addresses"):
            mgmt_port, mgmt_ip_dict, port_name = choose_mgmt_port(engines_dut, devices_dut)

            verify_ufm_mad_configuration(fae, serial_engine, port_name, devices_dut, ib_host_engine,
                                         UfmMadConsts.State.ENABLED.value, mgmt_ip_dict[UfmMadConsts.IPV4],
                                         mgmt_ip_dict[UfmMadConsts.IPV6])

        with allure.step("Disable ufm-mad feature"):
            fae.ib.ufm_mad.set(op_param_name=UfmMadConsts.STATE, op_param_value=UfmMadConsts.State.DISABLED.value,
                               apply=True).verify_result()

        with allure.step("Update mgmt port ipv4 address (static ip)"):
            mgmt_port.interface.ipv4.address.set(op_param_name=UfmMadConsts.STATIC_IPV4, apply=True,
                                                 ask_for_confirmation=True).verify_result()

        # Connection in SSH is lost, continue in serial port
        with allure.step("Validate ufm-mad state disabled and IP address is empty"):
            verify_ufm_mad_configuration(fae, serial_engine, port_name, devices_dut,
                                         ib_host_engine, UfmMadConsts.State.DISABLED.value)

        with allure.step("Verify State DB:UFM-MAD value"):
            verify_ufm_mad_db_table(engine=serial_engine, state=UfmMadConsts.State.DISABLED.value, port_name=port_name)

        with allure.step("Enable feature"):
            fae.ib.ufm_mad.set(op_param_name=UfmMadConsts.STATE, op_param_value=UfmMadConsts.State.ENABLED.value,
                               apply=True, dut_engine=serial_engine).verify_result()
            time.sleep(UfmMadConsts.CONFIG_TIME)

        with allure.step("Validate ufm-mad state enabled and IP static address is configured"):
            verify_ufm_mad_configuration(fae, serial_engine, port_name, devices_dut, ib_host_engine,
                                         UfmMadConsts.State.ENABLED.value, UfmMadConsts.STATIC_IPV4,
                                         mgmt_ip_dict[UfmMadConsts.IPV6_SLAAC])

        with allure.step("Verify State DB:UFM-MAD value"):
            verify_ufm_mad_db_table(serial_engine, UfmMadConsts.State.ENABLED.value, port_name,
                                    UfmMadConsts.STATIC_IPV4, mgmt_ip_dict[UfmMadConsts.IPV6_SLAAC])

        with allure.step("Update mgmt port ipv4 address when ufm-mad state is enabled"):
            mgmt_port.interface.ipv4.address.unset(dut_engine=serial_engine, apply=True).verify_result()
            time.sleep(UfmMadConsts.CONFIG_TIME)

        # Connection in SSH is back, continue in engines.dut
        with allure.step("Validate ufm-mad state enabled and IP address is configured"):
            verify_ufm_mad_configuration(fae, engines_dut, port_name, devices_dut, ib_host_engine,
                                         UfmMadConsts.State.ENABLED.value, mgmt_ip_dict[UfmMadConsts.IPV4],
                                         mgmt_ip_dict[UfmMadConsts.IPV6])

        with allure.step("Verify State DB:UFM-MAD value"):
            verify_ufm_mad_db_table(engines_dut, UfmMadConsts.State.ENABLED.value, port_name,
                                    mgmt_ip_dict[UfmMadConsts.IPV4], mgmt_ip_dict[UfmMadConsts.IPV6])

        dhcp_disable_test_steps("IPv4", mgmt_port.interface.ipv4, serial_engine, port_name, devices_dut, ib_host_engine, mgmt_ip_dict, fae)
    finally:
        with allure.step("Re-enable DHCP client for IPv4"):
            mgmt_port.interface.ipv4.dhcp_client.unset(apply=True, ask_for_confirmation=True, dut_engine=serial_engine).verify_result()
            time.sleep(UfmMadConsts.CONFIG_TIME)  # for the next test - wait for the DHCP client to be disabled.

        with allure.step("Set to default mgmt port address and ufm-mad feature state"):
            mgmt_port.interface.ipv4.address.unset(dut_engine=serial_engine).verify_result()
            fae.ib.ufm_mad.unset(op_param=UfmMadConsts.STATE, apply=True, dut_engine=serial_engine).verify_result()


@pytest.mark.interfaces
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
def test_configure_mgmt_port_ipv6(engines, devices, topology_obj, prepare_traffic, test_api, serial_engine):
    """
    Validate configuring management port ipv6 address in several cases

    KNOWN ISSUE (Redmine #4839922): Disabling IPv6 DHCP client also clears IPv4 from UFM-MAD.
    Steps 13-14 account for this known issue.

    Test flow:
    1. Choose mgmt port (eth0|eth1) and read its IPv4/IPv6 addresses
    2. Disable UFM-MAD
    3. Configure a static IPv6 address on the mgmt port
    4. Verify UFM-MAD is disabled and advertised IPv4/IPv6 are empty
    5. Verify State DB table reflects "disabled + empty addresses"
    6. Enable UFM-MAD (set state ENABLED)
    7. Verify UFM-MAD is enabled and the configured IPv6 (static) is advertised
    8. Verify State DB table reflects "enabled + IPv6 present"
    9. Unset the static IPv6 address while UFM-MAD is enabled (revert IPv6 to the original mgmt IP)
    10. Verify UFM-MAD stays enabled and IPv4/IPv6 match the current/original mgmt IP values
    11. Verify State DB table reflects the updated IPv4/IPv6 addresses
    12. Disable the IPv6 DHCP client (UFM-MAD remains enabled)
    13. Verify IPv4 is NOT advertised (known issue #4839922), but IPv6 SLAAC remains
    14. Verify State DB table reflects "enabled + IPv4 empty + IPv6 SLAAC present"
    15. Restore defaults (mgmt port address and UFM-MAD state)
    """
    TestToolkit.tested_api = test_api
    engines_dut = engines.dut
    devices_dut = devices.dut
    ib_host_engine = getattr(engines, devices.dut.ib_host_player)
    fae = Fae()
    mgmt_port = None

    try:
        with allure.step("Choose mgmt port (eth0|eth1) and get its addresses"):
            mgmt_port, mgmt_ip_dict, port_name = choose_mgmt_port(engines_dut, devices_dut)

        with allure.step("Disable ufm-mad feature"):
            fae.ib.ufm_mad.set(op_param_name=UfmMadConsts.STATE, op_param_value=UfmMadConsts.State.DISABLED.value,
                               apply=True).verify_result()

        with allure.step("Update mgmt port ipv6 address (static ip)"):
            mgmt_port.interface.ipv6.address.set(op_param_name=UfmMadConsts.STATIC_IPV6, apply=True,
                                                 ask_for_confirmation=True).verify_result()

        # Connection in SSH is lost, continue in serial port
        with allure.step("Validate ufm-mad state disabled and IP address is empty"):
            verify_ufm_mad_configuration(fae, serial_engine, port_name, devices_dut, ib_host_engine,
                                         UfmMadConsts.State.DISABLED.value)

        with allure.step("Verify State DB:UFM-MAD value"):
            verify_ufm_mad_db_table(engine=serial_engine, state=UfmMadConsts.State.DISABLED.value, port_name=port_name)

        with allure.step("Enable feature"):
            fae.ib.ufm_mad.set(op_param_name=UfmMadConsts.STATE, op_param_value=UfmMadConsts.State.ENABLED.value,
                               apply=True, dut_engine=serial_engine).verify_result()
            time.sleep(UfmMadConsts.CONFIG_TIME)

        with allure.step("Validate ufm-mad state enabled and IP static address is configured"):
            verify_ufm_mad_configuration(fae, serial_engine, port_name, devices_dut, ib_host_engine,
                                         UfmMadConsts.State.ENABLED.value, ipv6=UfmMadConsts.STATIC_IPV6)

        with allure.step("Verify State DB:UFM-MAD value"):
            verify_ufm_mad_db_table(serial_engine, UfmMadConsts.State.ENABLED.value, port_name=port_name,
                                    ipv6=UfmMadConsts.STATIC_IPV6)

        with allure.step("Update mgmt port ipv6 address when ufm-mad state is enabled"):
            mgmt_port.interface.ipv6.address.unset(dut_engine=serial_engine, apply=True).verify_result()
            time.sleep(UfmMadConsts.CONFIG_TIME)

        # Connection in SSH is back, continue in engines.dut
        with allure.step("Validate ufm-mad state enabled and IP address is configured"):
            verify_ufm_mad_configuration(fae, engines_dut, port_name, devices_dut, ib_host_engine,
                                         UfmMadConsts.State.ENABLED.value, mgmt_ip_dict[UfmMadConsts.IPV4],
                                         mgmt_ip_dict[UfmMadConsts.IPV6])

        with allure.step("Verify State DB:UFM-MAD value"):
            verify_ufm_mad_db_table(engines_dut, UfmMadConsts.State.ENABLED.value, port_name,
                                    mgmt_ip_dict[UfmMadConsts.IPV4], mgmt_ip_dict[UfmMadConsts.IPV6])

        dhcp_disable_test_steps("IPv6", mgmt_port.interface.ipv6, serial_engine, port_name, devices_dut, ib_host_engine, mgmt_ip_dict, fae)
    finally:
        with allure.step("Re-enable DHCP client for IPv6"):
            mgmt_port.interface.ipv6.dhcp_client.unset(apply=True, ask_for_confirmation=True, dut_engine=serial_engine).verify_result()
            time.sleep(UfmMadConsts.CONFIG_TIME)

        with allure.step("Set to default mgmt port address and ufm-mad feature state"):
            mgmt_port.interface.ipv6.address.unset(apply=True, ask_for_confirmation=True, dut_engine=serial_engine).verify_result()
            fae.ib.ufm_mad.unset(op_param=UfmMadConsts.STATE, apply=True, ask_for_confirmation=True, dut_engine=serial_engine).verify_result()


@pytest.mark.interfaces
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
def test_configure_mgmt_port_link_state(engines, devices, topology_obj, prepare_traffic, test_api, serial_engine):
    """
    Validate configuring management port link state in several cases

    Test flow:
    1. Choose mgmt port (eth0|eth1) and get its addresses
    2. Disable ufm-mad feature when link state is up
    3. Validate ufm-mad state disabled and IP address is empty
    4. Enable ufm-mad feature when link state is up
    5. Validate ufm-mad state enabled and IP addresses are configured
    6. Set mgmt port link state to down
    7. Disable ufm-mad feature when link state is down
    8. Validate ufm-mad state disabled and IP addressed are empty
    9. Enable ufm-mad feature when link state is down
    10. Validate ufm-mad state enabled and IP addressed are empty
    11. Set mgmt port link state to up and enable ufm-mad feature state
    """
    TestToolkit.tested_api = test_api
    engines_dut = engines.dut
    devices_dut = devices.dut
    ib_host_engine = getattr(engines, devices.dut.ib_host_player)
    fae = Fae()

    try:
        with allure.step("Choose mgmt port (eth0|eth1) and get its addresses"):
            mgmt_port, mgmt_ip_dict, port_name = choose_mgmt_port(engines_dut, devices_dut)

        with allure.step("Disable ufm-mad feature when link state is up"):
            fae.ib.ufm_mad.set(op_param_name=UfmMadConsts.STATE, op_param_value=UfmMadConsts.State.DISABLED.value,
                               apply=True).verify_result()

        with allure.step("Validate ufm-mad state disabled and IP address is empty"):
            verify_ufm_mad_configuration(fae, engines_dut, port_name, devices_dut,
                                         ib_host_engine, UfmMadConsts.State.DISABLED.value)

        with allure.step("Enable ufm-mad feature when link state is up"):
            fae.ib.ufm_mad.set(op_param_name=UfmMadConsts.STATE, op_param_value=UfmMadConsts.State.ENABLED.value,
                               apply=True).verify_result()
            time.sleep(UfmMadConsts.CONFIG_TIME)

        with allure.step("Validate ufm-mad state enabled and IP addresses are configured"):
            verify_ufm_mad_configuration(fae, engines_dut, port_name, devices_dut, ib_host_engine,
                                         UfmMadConsts.State.ENABLED.value, mgmt_ip_dict[UfmMadConsts.IPV4],
                                         mgmt_ip_dict[UfmMadConsts.IPV6])

        with allure.step("Set mgmt port link state to down"):
            mgmt_port.interface.link.state.set(op_param_name=NvosConsts.LINK_STATE_DOWN, apply=True,
                                               ask_for_confirmation=True).verify_result()

        # Connection in SSH is lost, continue in serial port
        with allure.step("Disable ufm-mad feature when link state is down"):
            fae.ib.ufm_mad.set(op_param_name=UfmMadConsts.STATE, op_param_value=UfmMadConsts.State.DISABLED.value,
                               apply=True, dut_engine=serial_engine).verify_result()

        with allure.step("Validate ufm-mad state disabled and IP addressed are empty"):
            verify_ufm_mad_configuration(fae, serial_engine, port_name, devices_dut, ib_host_engine,
                                         UfmMadConsts.State.DISABLED.value)

        with allure.step("Enable ufm-mad feature when link state is down"):
            fae.ib.ufm_mad.set(op_param_name=UfmMadConsts.STATE, op_param_value=UfmMadConsts.State.ENABLED.value,
                               apply=True, dut_engine=serial_engine).verify_result()

        with allure.step("Validate ufm-mad state enabled and IP addressed are empty"):
            verify_ufm_mad_configuration(fae, serial_engine, port_name, devices_dut, ib_host_engine,
                                         UfmMadConsts.State.ENABLED.value)

    finally:
        with allure.step("Set mgmt port link state to up and enable ufm-mad feature state"):
            mgmt_port.interface.link.state.set(op_param_name=NvosConsts.LINK_STATE_UP, ask_for_confirmation=True,
                                               apply=True, dut_engine=serial_engine).verify_result()
            time.sleep(UfmMadConsts.CONFIG_TIME)
            fae.ib.ufm_mad.unset(op_param=UfmMadConsts.STATE, apply=True, dut_engine=serial_engine).verify_result()


@pytest.mark.interfacel
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
def test_fae_invalid_commands(test_api):
    """
    Check set fae command with invalid param value.

    Test flow:
    1. nv set fae ib ufm-mad <invalid state>
    """

    TestToolkit.tested_api = test_api

    with allure.step("Validate set fae interface link lanes with invalid lanes"):
        Fae().ib.ufm_mad.set(op_param_name=UfmMadConsts.STATE, op_param_value='invalid_state',
                             apply=True).verify_result(should_succeed=False)


def choose_mgmt_port(dut_engine, devices):
    port_name = RandomizationTool.select_random_value(devices.mgmt_ports).get_returned_value()
    mgmt_port = Port(port_name)
    mgmt_ip_dict = get_mgmt_port_ip_addresses(mgmt_port, dut_engine)
    return mgmt_port, mgmt_ip_dict, port_name


def get_mgmt_port_ip_addresses(mgmt_port, dut_engine):
    with allure.step(f"Get mgmt port {mgmt_port.name} ip addresses"):
        # Get IPv4 addresses - returns dict like {"10.7.148.248/21": {}}
        ipv4_addresses_show = OutputParsingTool.parse_json_str_to_dictionary(
            mgmt_port.interface.ipv4.address.show(dut_engine=dut_engine)).get_returned_value()

        # Get IPv6 addresses - returns dict like {"fdfd:fdfd:7:145::1000:405d/128": {}}
        ipv6_addresses_show = OutputParsingTool.parse_json_str_to_dictionary(
            mgmt_port.interface.ipv6.address.show(dut_engine=dut_engine)).get_returned_value()

    ip_address = {}

    # Extract IPv4 address (first key from the dictionary)
    if ipv4_addresses_show and len(ipv4_addresses_show) > 0:
        ipv4_key = list(ipv4_addresses_show.keys())[0]
        ip_address.update({UfmMadConsts.IPV4: ipv4_key})
    else:
        ip_address.update({UfmMadConsts.IPV4: ""})

    # Extract IPv6 addresses
    # Use length-based heuristic: SLAAC addresses (EUI-64) are longer strings than static addresses
    # (which use :: compression). UFM-MAD advertises the static (shorter) address.
    if ipv6_addresses_show and len(ipv6_addresses_show) > 0:
        ipv6_keys = list(ipv6_addresses_show.keys())
        if len(ipv6_keys) >= 2:
            # Use length to distinguish: shorter string = static (IPV6), longer = SLAAC
            if len(ipv6_keys[0]) > len(ipv6_keys[1]):
                ip_address.update({UfmMadConsts.IPV6: ipv6_keys[1]})        # shorter = static
                ip_address.update({UfmMadConsts.IPV6_SLAAC: ipv6_keys[0]})  # longer = SLAAC
            else:
                ip_address.update({UfmMadConsts.IPV6: ipv6_keys[0]})        # shorter = static
                ip_address.update({UfmMadConsts.IPV6_SLAAC: ipv6_keys[1]})  # longer = SLAAC
        elif len(ipv6_keys) == 1:
            ip_address.update({UfmMadConsts.IPV6: ipv6_keys[0]})
            ip_address.update({UfmMadConsts.IPV6_SLAAC: ipv6_keys[0]})
    else:
        ip_address.update({UfmMadConsts.IPV6: ""})
        ip_address.update({UfmMadConsts.IPV6_SLAAC: ""})

    return ip_address


def verify_ufm_mad_configuration(fae, dut_engine, port_name, devices_dut, ib_host_engine, state, ipv4='', ipv6=''):
    with allure.step("Validate ufm-mad state in show command"):
        ufm_mad_show = OutputParsingTool.parse_json_str_to_dictionary(
            fae.ib.ufm_mad.show(dut_engine=dut_engine)).get_returned_value()
        assert ufm_mad_show[UfmMadConsts.STATE] == state, f"Ufm-Mad state should be {state}"
        ufm_mad_show = OutputParsingTool.parse_json_str_to_dictionary(
            fae.ib.ufm_mad.show(UfmMadConsts.ADVERTISED_ADDRESSED, dut_engine=dut_engine)).get_returned_value()

    with (allure.step("Run MAD request from traffic server")):
        output = IpTool.send_ufm_mad(ib_host_engine, UfmMadConsts.NVMAD_PATH, port_name[-1]).get_returned_value()
        mads_response = IpTool.parse_mad_output(output)
        ipv4_res = mads_response[IpConsts.IPV4]
        ipv6_res = mads_response[IpConsts.IPV6]

    with allure.step("Get MAD from IBSNI register"):
        output = RegisterTool.get_mst_register_value(dut_engine, devices_dut.mst_dev_name[0], UfmMadConsts.IBSNI_REGISTER)
        ibsni_response = parse_ibsni_register(output, port_name)
        ipv4_reg = ibsni_response[IpConsts.IPV4]
        ipv6_reg = ibsni_response[IpConsts.IPV6]

    with allure.step("Verify ip addresses via: show command, MAD request from traffic server, and IBSNI register"):
        if ipv4:
            with allure.independent_step('verify ipv4'):
                assert ufm_mad_show[port_name][UfmMadConsts.IPV4_PREFIX] == ipv4, \
                    f"ipv4 should be {ipv4}, but it is {ufm_mad_show[port_name][UfmMadConsts.IPV4_PREFIX]}"
            with allure.independent_step('verify ufm mad ipv4'):
                assert ipv4_res == ipv4.split('/')[0], f"ufm mad ipv4 response is: {ipv4_res}, " \
                    f"but expected: {ipv4.split('/')[0]}"
            with allure.independent_step('verify IBSNI ipv4'):
                assert ipv4_reg == ipv4.split('/')[0], f"IBSNI ipv4 response is: {ipv4_reg}, " \
                    f"but expected: {ipv4.split('/')[0]}"
        else:
            with allure.independent_step(f'verify {UfmMadConsts.IPV4_PREFIX}'):
                assert UfmMadConsts.IPV4_PREFIX not in ufm_mad_show[port_name].keys(), \
                    f"{UfmMadConsts.IPV4_PREFIX} should be empty, but it is: " \
                    f"{ufm_mad_show[port_name][UfmMadConsts.IPV4_PREFIX]}"
            with allure.independent_step('verify ufm mad ipv4 response'):
                assert ipv4_res == UfmMadConsts.MAD_NO_IPV4, f"ufm mad ipv4 response is: {ipv4_res}, " \
                    f"but expected: {UfmMadConsts.MAD_NO_IPV4}"
            with allure.independent_step('verify IBSNI ipv4 response'):
                assert ipv4_reg == UfmMadConsts.MAD_NO_IPV4, f"IBSNI ipv4 response is: {ipv4_reg}, " \
                    f"but expected: {UfmMadConsts.MAD_NO_IPV4}"
        if ipv6:
            with allure.independent_step('verify ipv6'):
                assert ufm_mad_show[port_name][UfmMadConsts.IPV6_PREFIX] == ipv6, \
                    f"ipv6 should be {ipv6}, but it is {ufm_mad_show[port_name][UfmMadConsts.IPV6_PREFIX]}"
            with allure.independent_step('verify ufm mad ipv6 response'):
                assert ipv6_res == ipv6.split('/')[0], f"ufm mad ipv6 response is: {ipv6_res}, " \
                    f"but expected: {ipv6.split('/')[0]}"
            with allure.independent_step('verify IBSNI ipv6 response'):
                assert ipv6_reg == ipv6.split('/')[0], f"IBSNI ipv6 response is: {ipv6_reg}, " \
                    f"but expected: {ipv6.split('/')[0]}"
        else:
            with allure.independent_step(f'verify {UfmMadConsts.IPV6_PREFIX}'):
                assert UfmMadConsts.IPV6_PREFIX not in ufm_mad_show[port_name].keys(), \
                    f"{UfmMadConsts.IPV6_PREFIX} should be empty, but it is: " \
                    f"{ufm_mad_show[port_name][UfmMadConsts.IPV6_PREFIX]}"
            with allure.independent_step('verify ufm mad ipv6 response'):
                assert ipv6_res == UfmMadConsts.MAD_NO_IPV6, f"ufm mad ipv6 response is: {ipv6_res}, " \
                    f"but expected: {UfmMadConsts.MAD_NO_IPV6}"
            with allure.independent_step('verify IBSNI ipv6 response'):
                assert ipv6_reg == UfmMadConsts.MAD_NO_IPV6, f"IBSNI ipv6 response is: {ipv6_reg}, " \
                    f"but expected: {UfmMadConsts.MAD_NO_IPV6}"


def verify_ufm_mad_db_table(engine, state, port_name, ipv4='', ipv6=''):
    table_eth = DatabaseTool.sonic_db_cli_hgetall(engine=engine, asic="", db_name=DatabaseConst.STATE_DB_NAME,
                                                  table_name=UfmMadConsts.UFM_MAD_TABLE_ETH_TEMPLATE.format(
                                                      port_name=port_name))
    table_general = DatabaseTool.sonic_db_cli_hgetall(engine=engine, asic="", db_name=DatabaseConst.STATE_DB_NAME,
                                                      table_name=UfmMadConsts.UFM_MAD_TABLE_GENERAL)
    reg_state = table_general.split(': ')[1].replace("'", "").replace("}", "")
    ValidationTool.compare_values(reg_state, state).verify_result()
    if ipv4:
        with allure.independent_step('ipv4'):
            reg_ipv4 = table_eth.split(',')[0].split(': ')[1].replace("'", "")
            ValidationTool.compare_values(reg_ipv4, ipv4).verify_result()
    if ipv6:
        with allure.independent_step('ipv6'):
            reg_ipv6 = table_eth.split(',')[1].split(': ')[1].replace("'", "").replace("}", "")
            ValidationTool.compare_values(reg_ipv6, ipv6).verify_result()


def parse_ibsni_register(ibsni_value, port_name):
    """
    @Summary:
        This function will parse the IBSNI register value and return a dict of the required fields.
        IPV4, IPV6 And their netmasks.
    @param ibsni_value: The IBSNI register value. for example:
        Sending access register...

        Field Name    | Data
        ===========================
        ipv4_0        | 0x0a07945e
        netmask_0     | 0xfffff800
        ipv4_1        | 0x00000000
        netmask_1     | 0x00000000
        ipv4_2        | 0x00000000
        netmask_2     | 0x00000000
        ipv4_3        | 0x00000000
        netmask_3     | 0x00000000
        ipv6[0]       | 0xfdfdfdfd
        ipv6[1]       | 0x00070145
        ipv6[2]       | 0x00000000
        ipv6[3]       | 0x1000419f
        netmask[0]    | 0xffffffff
        netmask[1]    | 0xffffffff
        netmask[2]    | 0xffffffff
        netmask[3]    | 0xffffffff
        ipv6[0]       | 0x00000000
        ipv6[1]       | 0x00000000
        ipv6[2]       | 0x00000000
        ipv6[3]       | 0x00000000
        netmask[0]    | 0x00000000
        netmask[1]    | 0x00000000
        netmask[2]    | 0x00000000
        netmask[3]    | 0x00000000
        ipv6[0]       | 0x00000000
        ipv6[1]       | 0x00000000
        ipv6[2]       | 0x00000000
        ipv6[3]       | 0x00000000
        netmask[0]    | 0x00000000
        netmask[1]    | 0x00000000
        netmask[2]    | 0x00000000
        netmask[3]    | 0x00000000
        ipv6[0]       | 0x00000000
        ipv6[1]       | 0x00000000
        ipv6[2]       | 0x00000000
        ipv6[3]       | 0x00000000
        netmask[0]    | 0x00000000
        netmask[1]    | 0x00000000
        netmask[2]    | 0x00000000
        netmask[3]    | 0x00000000
        ipv4_0        | 0x0a07945f
        netmask_0     | 0xfffff800
        ipv4_1        | 0x00000000
        netmask_1     | 0x00000000
        ipv4_2        | 0x00000000
        netmask_2     | 0x00000000
        ipv4_3        | 0x00000000
        netmask_3     | 0x00000000
        ipv6[0]       | 0xfdfdfdfd
        ipv6[1]       | 0x00070145
        ipv6[2]       | 0x00000000
        ipv6[3]       | 0x1000427e
        netmask[0]    | 0xffffffff
        netmask[1]    | 0xffffffff
        netmask[2]    | 0xffffffff
        netmask[3]    | 0xffffffff
        ipv6[0]       | 0x00000000
        ipv6[1]       | 0x00000000
        ipv6[2]       | 0x00000000
        ipv6[3]       | 0x00000000
        netmask[0]    | 0x00000000
        netmask[1]    | 0x00000000
        netmask[2]    | 0x00000000
        netmask[3]    | 0x00000000
        ipv6[0]       | 0x00000000
        ipv6[1]       | 0x00000000
        ipv6[2]       | 0x00000000
        ipv6[3]       | 0x00000000
        netmask[0]    | 0x00000000
        netmask[1]    | 0x00000000
        netmask[2]    | 0x00000000
        netmask[3]    | 0x00000000
        ipv6[0]       | 0x00000000
        ipv6[1]       | 0x00000000
        ipv6[2]       | 0x00000000
        ipv6[3]       | 0x00000000
        netmask[0]    | 0x00000000
        netmask[1]    | 0x00000000
        netmask[2]    | 0x00000000
        netmask[3]    | 0x00000000
        ===========================
    @param port_name: mgmt port ('eth0' or 'eth1')
    @return: Dict:
        {'ipv4': '10.7.144.154',
        'ipv4_netmask': '255.255.248.0',
        'ipv6': 'fdfd:fdfd:7:145::1000:484c',
        'ipv6_netmask': 'ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff'}
    """
    mad_output_list = ibsni_value.split('\n')
    if port_name == UfmMadConsts.MGMT_PORT1:
        # Find the second 'ipv4_0' which marks start of eth1 section
        # (more robust than hardcoded offset which can break if output format changes)
        found_first = False
        eth1_section_found = False
        for i, line in enumerate(mad_output_list):
            if UfmMadConsts.IPV4_PREF in line:
                if found_first:
                    del mad_output_list[:i]
                    eth1_section_found = True
                    break
                found_first = True
        if not eth1_section_found:
            # Log first N lines to aid debugging
            preview_lines = '\n'.join(mad_output_list[:20])
            raise ValueError(
                f"eth1 section not found in MAD output - expected second '{UfmMadConsts.IPV4_PREF}' marker. "
                f"Found first marker: {found_first}. MAD output preview (first 20 lines):\n{preview_lines}"
            )
    ips_dict = {}
    ipv6 = '0x'
    ipv6_netmask = '0x'
    for out in mad_output_list:
        if UfmMadConsts.IPV4_PREF in out:
            out = out.split("|")[-1]
            ips_dict[UfmMadConsts.IPV4] = IpTool.hex_to_ipv4(out)
        elif UfmMadConsts.IPV4_NETMASK_PREF in out:
            out = out.split("|")[-1]
            ips_dict[UfmMadConsts.IPV4_NETMASK] = IpTool.hex_to_ipv4(out)
        elif UfmMadConsts.IPV6 in out:
            out1 = out.split("|")[-1]
            ipv6 += out1.split("x")[-1]
            if UfmMadConsts.LAST_IP_INDEX in out:
                ips_dict[UfmMadConsts.IPV6] = IpTool.hex_to_ipv6(ipv6)
        elif UfmMadConsts.IPV6_NETMASK_PREF in out:
            out1 = out.split("|")[-1]
            ipv6_netmask += out1.split("x")[-1]
            if UfmMadConsts.LAST_IP_INDEX in out:
                ips_dict[UfmMadConsts.IPV6_NETMASK] = IpTool.hex_to_ipv6(ipv6_netmask)
        if len(ips_dict) == UfmMadConsts.NUMBER_OF_ADDRESSES_IN_MAD_RESPONSE:
            break
    return ips_dict


def dhcp_disable_test_steps(ip_version, ipv_obj, serial_engine, port_name, devices_dut, ib_host_engine, mgmt_ip_dict, fae):
    """
    Tests that disabling DHCP client for one IP family removes that IP from UFM-MAD
    while preserving the other IP family's address.

    KNOWN ISSUE (Redmine #4839922): When disabling IPv6 DHCP client, IPv4 address is
    also cleared from UFM-MAD advertisement (should remain but doesn't). IPv6 SLAAC
    address correctly persists (independent of DHCP client). Test expectations are
    adjusted to match current behavior.

    Args:
        ip_version: "IPv4" or "IPv6" - the IP family whose DHCP client we're disabling
        ipv_obj: The interface.ipv4 or interface.ipv6 object to operate on
        serial_engine: Engine to run commands (serial port since SSH may be lost)
        port_name: Management port name (eth0 or eth1)
        devices_dut: Device under test object
        ib_host_engine: HA engines for MAD requests
        mgmt_ip_dict: Dictionary containing original management IP addresses
        fae: Fae object for UFM-MAD operations

    Test Flow:
        1. Disable DHCP client for the specified IP family
        2. Verify UFM-MAD shows the expected IP addresses (accounting for known issue)
        3. Verify State DB reflects the same expectations
    """
    # Build expectations for both IPv4 and IPv6 DHCP disable scenarios
    # NOTE: Both cases currently have the same expectations due to known issue #4839922
    if ip_version == "IPv6":
        # KNOWN ISSUE (Redmine #4839922): Disabling IPv6 DHCP client also clears IPv4
        # Expected behavior: DHCPv6 cleared, SLAAC remains, IPv4 stays
        # Actual behavior: IPv4 gets cleared (bug), SLAAC persists correctly
        expected_ipv4 = ""  # IPv4 cleared due to known issue
        expected_ipv6 = mgmt_ip_dict[UfmMadConsts.IPV6_SLAAC]  # SLAAC persists
    else:  # IPv4
        # Disabling IPv4 DHCP: IPv4 cleared (expected), SLAAC persists
        expected_ipv4 = ""  # IPv4 cleared as expected
        expected_ipv6 = mgmt_ip_dict[UfmMadConsts.IPV6_SLAAC]  # SLAAC persists

    with allure.step(f"Disable DHCP client for {ip_version}"):
        ipv_obj.dhcp_client.set(
            op_param_name=UfmMadConsts.STATE, op_param_value=UfmMadConsts.State.DISABLED.value,
            apply=True, dut_engine=serial_engine, ask_for_confirmation=True
        ).verify_result()
        time.sleep(UfmMadConsts.CONFIG_TIME)

    with allure.step(f"Verify UFM-MAD: {ip_version} is NOT advertised, other IP family unchanged"):
        verify_ufm_mad_configuration(
            fae=fae, dut_engine=serial_engine, port_name=port_name,
            devices_dut=devices_dut, ib_host_engine=ib_host_engine,
            state=UfmMadConsts.State.ENABLED.value,
            ipv4=expected_ipv4, ipv6=expected_ipv6
        )

    with allure.step("Verify State DB:UFM-MAD reflects correct advertised addresses"):
        verify_ufm_mad_db_table(
            engine=serial_engine, state=UfmMadConsts.State.ENABLED.value, port_name=port_name,
            ipv4=expected_ipv4, ipv6=expected_ipv6
        )
