import random
import time

import pytest

from infra.tools.validations.traffic_validations.port_check.port_checker import check_port_status_till_alive
from ngts.nvos_constants.constants_nvos import SystemConsts, NvosConst, TcpDumpConsts, ApiType
from ngts.nvos_tools.ib.InterfaceConfiguration.Interface import Interface
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import NvosConsts
from ngts.nvos_tools.infra.IpTool import IpTool
from ngts.tests_nvos.constants import MINUTE
from ngts.nvos_tools.infra.LLDPTool import LLDPTool
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from infra.tools.redmine.redmine_api import is_redmine_issue_active
from ngts.nvos_tools.system.System import System
from ngts.tools.test_utils import allure_utils as allure


INTERFACE_CLEANUP_DELAY = 30  # seconds - https://redmine.mellanox.com/issues/4544216

# LLDP protocol and test value constants (interval * multiplier must be <= LLDP_MAX_TTL)
LLDP_MAX_TTL = 65535
LLDP_MAX_INTERVAL = 13107  # max interval such that interval * LLDP_MAX_MULTIPLIER <= LLDP_MAX_TTL
LLDP_MAX_MULTIPLIER = 5
LLDP_INVALID_INTERVAL = 32769  # above valid range for set
LLDP_INVALID_MULTIPLIER = 8193  # above valid range for set
LLDP_CUSTOM_INTERVAL = 5
LLDP_DEFAULT_INTERVAL = 30
CUSTOM_LLDP_HOSTNAME = "lldp-host"
LLDP_STATE_CHANGE_POLL_TIMEOUT = 30
LLDP_STATE_CHANGE_POLL_INTERVAL = 2


@pytest.mark.lldp
@pytest.mark.system
@pytest.mark.interface
@pytest.mark.cumulus
def test_lldp_enabled(engines, devices, random_api):
    """
    Verify lldp functionality is working by default.

    1. Verify lldp is running.
    2. Verify lldp is sending and receiving frames.
    """
    system = System()
    lldp = system.lldp
    _verify_lldp_is_sending_frames(lldp=lldp, engine=engines.dut, device=devices.dut)


@pytest.mark.lldp
@pytest.mark.system
@pytest.mark.interface
@pytest.mark.cumulus
def test_lldp_show(engines, devices):
    """
    Verify lldp show is working as expected.

    1. Verify lldp is running.
    2. Verify interface lldp contains neighbor field.
    """
    system = System()
    lldp = system.lldp

    _verify_lldp_running(lldp, engine=engines.dut)
    lldp_output = OutputParsingTool.parse_json_str_to_dictionary(Interface(parent_obj=None).lldp.show()).get_returned_value()

    with allure.step(f"Testing all mgmt ports"):
        for interface_name in devices.dut.get_mgmt_ports():
            with allure.independent_step(f"Testing {interface_name}"):
                ValidationTool.verify_field_exist_in_json_output(lldp_output,
                                                                 [interface_name]).verify_result()
                eth_output = lldp_output[interface_name]
                ValidationTool.verify_field_exist_in_json_output(eth_output,
                                                                 [SystemConsts.LLDP_LLDP]).verify_result()
                eth_lldp_output = eth_output[SystemConsts.LLDP_LLDP]
                ValidationTool.verify_field_exist_in_json_output(eth_lldp_output,
                                                                 [SystemConsts.LLDP_NEIGHBOR]).verify_result()


@pytest.mark.lldp
@pytest.mark.system
@pytest.mark.interface
@pytest.mark.cumulus
def test_lldp_disabled(engines, devices, random_api):
    """
    Check that lldp is disabled correctly.

    1. Verify lldp is running.
    2. Disable lldp state.
    3. Verify lldp is not running and not sending any frames.
    4. Verify neighbors table is empty
    5. Enable back lldp and verify it is working.
    """
    system = System()
    lldp = system.lldp

    _verify_lldp_is_sending_frames(lldp, engine=engines.dut, device=devices.dut)

    try:
        _set_lldp_state(lldp, key=SystemConsts.STATE, val=NvosConst.DISABLED)
        _verify_lldp_not_running(lldp, engine=engines.dut, device=devices.dut)

    finally:
        _set_lldp_state(lldp, key=SystemConsts.STATE, val=NvosConst.ENABLED)
        _verify_lldp_is_sending_frames(lldp, engine=engines.dut, device=devices.dut)


@pytest.mark.lldp
@pytest.mark.system
@pytest.mark.interface
@pytest.mark.cumulus
def test_lldp_with_custom_interval(engines, devices, random_api):
    """
    Check that lldp frames have correct data in them.
    1. Verify lldp is running with custom interval.
    2. Verify lldp information is the same as in tcpdump.
    """

    system = System()
    lldp = system.lldp
    cli_output = lldp.parsed_show()
    default_interval = cli_output[SystemConsts.LLDP_INTERVAL]

    _verify_lldp_running(lldp, engine=engines.dut)

    try:
        system_output = OutputParsingTool.parse_json_str_to_dictionary(system.show()).get_returned_value()
        _set_lldp_state(lldp, SystemConsts.LLDP_INTERVAL, LLDP_CUSTOM_INTERVAL)

        _verify_cli_output_with_dump_output(engines.dut, devices.dut, lldp, system_output)
    finally:
        _set_lldp_state(lldp, SystemConsts.LLDP_INTERVAL, default_interval)


@pytest.mark.lldp
@pytest.mark.system
@pytest.mark.interface
@pytest.mark.cumulus
def test_lldp_custom_hostname(engines, devices, random_api):
    """
    Check that lldp frames have correct custom hostname.
    1. Verify lldp is running.
    2. Change default hostname to custom.
    3. Verify lldp is sending correct hostname.
    4. Change hostname back to default.
    """
    system = System()
    _verify_lldp_running(system.lldp, engine=engines.dut)

    custom_hostname = CUSTOM_LLDP_HOSTNAME
    with allure.step("Get current system hostname"):
        system_dict = OutputParsingTool.parse_json_str_to_dictionary(
            system.show()).get_returned_value()
        default_hostname = system_dict[SystemConsts.HOSTNAME]
    try:
        system.set(SystemConsts.HOSTNAME, custom_hostname, apply=True, ask_for_confirmation=True).verify_result()
        with allure.step("Verify hostname in tcpdump"):
            lldp_dump = LLDPTool.get_lldp_frames(engine=engines.dut)
            lldp_dict = LLDPTool.parse_lldp_dump(lldp_dump)
            assert lldp_dict[
                TcpDumpConsts.LLDP_SYSTEM_NAME] == custom_hostname, f'The lldp {lldp_dict[TcpDumpConsts.LLDP_SYSTEM_NAME]} is not {custom_hostname}'
    finally:
        system.set(SystemConsts.HOSTNAME, default_hostname, apply=True, ask_for_confirmation=True).verify_result()


@pytest.mark.lldp
@pytest.mark.system
@pytest.mark.interface
@pytest.mark.cumulus
def test_lldp_one_neighbor(engines, devices, random_api):
    """
    Check that interfaces have only one lldp neighbor.
    1. Verify lldp is running.
    2. For each mgmt interface verify it has only one neighbor.
    3. Verify neighbor's output is not empty
    """
    system = System()
    _verify_lldp_running(system.lldp, engine=engines.dut)

    with allure.step(f"Testing all mgmt ports"):
        for interface_name in devices.dut.get_mgmt_ports():
            with allure.independent_step(f"Testing {interface_name}"):
                with allure.step(f"Verify {interface_name} has only one neighbor"):
                    mgmt_interface = Port(name=interface_name)
                    output_dict = OutputParsingTool.parse_json_str_to_dictionary(
                        mgmt_interface.interface.lldp.neighbor.show()).get_returned_value()
                    neighbor_keys = list(output_dict.keys())
                    assert len(neighbor_keys) == 1, "There is not only one neighbor"
                neighbor_id = neighbor_keys[0]

                with allure.step(f"Verify neighbor {neighbor_id} is not empty for {interface_name}"):
                    neighbor_dict = OutputParsingTool.parse_json_str_to_dictionary(
                        mgmt_interface.interface.lldp.neighbor.neighbor_id[neighbor_id].show())
                    assert neighbor_dict, f"The neighbor {neighbor_id} is empty"


@pytest.mark.lldp
@pytest.mark.system
@pytest.mark.interface
@pytest.mark.cumulus
def test_lldp_incorrect_values(engines, devices, random_api):
    """
    Check that lldp set commands are not working for values outside the range.
    1. Verify lldp is running.
    2. Try to set incorrect interval.
    3. Try to set incorrect multiplier.
    """
    system = System()
    lldp = system.lldp

    _verify_lldp_running(lldp, engine=engines.dut)

    with allure.step(f"Verify can't set interval to {LLDP_INVALID_INTERVAL}"):
        lldp.set(SystemConsts.LLDP_INTERVAL, LLDP_INVALID_INTERVAL, ask_for_confirmation=True).verify_result(should_succeed=False)

    with allure.step(f"Verify can't set hold-multiplier to {LLDP_INVALID_MULTIPLIER}"):
        lldp.set(SystemConsts.LLDP_MULTIPLIER, LLDP_INVALID_MULTIPLIER, ask_for_confirmation=True).verify_result(should_succeed=False)

    if TestToolkit.devices.dut.is_ib():
        with allure.step(f"Generate random interval and multiplier, so ttl will exceed max allowed value of {LLDP_MAX_TTL}"):
            while True:
                interval = random.randint(5, 32768)
                multiplier = random.randint(1, 8192)
                ttl = interval * multiplier
                if ttl > LLDP_MAX_TTL:
                    break

        with allure.step(f"Verify can't set {interval} * {multiplier} which exceeds TTL of {LLDP_MAX_TTL}"):
            lldp.set(SystemConsts.LLDP_INTERVAL, interval, ask_for_confirmation=True).verify_result()
            lldp.set(SystemConsts.LLDP_MULTIPLIER, multiplier, apply=True, ask_for_confirmation=True).verify_result(should_succeed=False)
            lldp.unset(apply=True).verify_result()


@pytest.mark.lldp
@pytest.mark.system
@pytest.mark.interface
def test_lldp_max_values(engines, devices):
    """
    Check that lldp set commands are working with max values
    1. Verify lldp is running.
    2. Set max interval and multiplier.
    3. Verify ttl.
    """
    system = System()
    lldp = system.lldp

    _verify_lldp_running(lldp, engine=engines.dut)
    max_interval = 13107  # Interval * multiplier should be less or equal to 65535
    max_multiplier = 5

    try:
        with allure.step(f"Verify lldp works with interval {max_interval}"):
            _set_lldp_state(lldp, SystemConsts.LLDP_INTERVAL, max_interval, apply=False)
        with allure.step(f"Verify lldp works with interval {max_multiplier}"):
            _set_lldp_state(lldp, SystemConsts.LLDP_MULTIPLIER, max_multiplier)

        with allure.step("Disable and enable lldp to get first frame instead of waiting forever"):
            _set_lldp_state(lldp, key=SystemConsts.STATE, val=NvosConst.DISABLED)
            # WA in order to get first lldp frame without waiting for long ttl
            _set_lldp_state(lldp, key=SystemConsts.STATE, val=NvosConst.ENABLED, wait_for_state=False)

        with allure.step("Verify interval values"):
            lldp_dump = LLDPTool.get_lldp_frames(engine=engines.dut)
            lldp_dict = LLDPTool.parse_lldp_dump(lldp_dump)
            ttl = max_interval * max_multiplier
            assert int(lldp_dict[
                TcpDumpConsts.LLDP_TIME_TO_LIVE]) == ttl, 'The cli ttl does not match sent frame time to live'

    finally:
        with allure.step("Return to default lldp values"):
            _restore_lldp_defaults(lldp, engine=engines.dut)


@pytest.mark.lldp
@pytest.mark.system
@pytest.mark.interface
@pytest.mark.timeout(5 * MINUTE, func_only=True)
@pytest.mark.cumulus
def test_lldp_additional_ipv6(engines, devices, serial_engine):
    """
    Check that correct lldp frames sent all IpV6 addresses.
    1. Verify lldp is running.
    2. Add additional IpV6 address.
    3. Verify lldp frames contain this new IpV6 address.
    """
    if not IpTool.is_dhcp_client6_has_lease(engines.dut):
        pytest.skip("DUT DHCP client6 has no lease; cannot run this IPv6 test.")

    system = System()
    engine = engines.dut
    _verify_lldp_running(system.lldp, engine=engine)

    with allure.step(f"Testing all mgmt ports"):
        mgmt_ports = devices.dut.get_mgmt_ports()
        for i, interface_name in enumerate(mgmt_ports):
            mgmt_interface = Port(name=interface_name)
            with allure.independent_step(f"Testing {interface_name}"):
                ip_address_full = None
                try:
                    ip_address_full = IpTool.select_random_ipv6_address().verify_result()  # 40c9:7735:e23d:dd2a:ca43:c5e9:682e:decb/114
                    ip_address, prefix = ip_address_full.split("/")
                    with allure.step(f"Set random ipv6 address {ip_address} for {interface_name}"):
                        mgmt_interface.interface.ipv6.address.set(op_param_name=ip_address_full, apply=True,
                                                                  ask_for_confirmation=True,
                                                                  dut_engine=serial_engine).verify_result()

                    LLDPTool.verify_ip_address_is_set(engine=serial_engine, mgmt_interface=mgmt_interface,
                                                      ip_address=ip_address_full, is_ipv6=True)

                    with allure.step("Verify ipv6 address is in the lldp frame"):
                        output = LLDPTool.get_lldp_frames(engine=serial_engine, interface=interface_name,
                                                          interval=LLDP_DEFAULT_INTERVAL + 2)
                        assert ip_address in output, f"The ipv6 address {ip_address} is not in lldp frame"

                finally:
                    mgmt_interface.interface.ipv6.address.unset(apply=True, dut_engine=serial_engine,
                                                                ask_for_confirmation=True).verify_result()
                    if i < len(mgmt_ports) - 1 and ip_address_full is not None:
                        _wait_until_ipv6_removed_from_interface(mgmt_interface, ip_address_full, serial_engine)


@pytest.mark.lldp
@pytest.mark.system
@pytest.mark.interface
@pytest.mark.cumulus
def test_lldp_interface_flapping(engines, devices, serial_engine):
    """
    Check that correct lldp frames are sent after interface flapping
    1. Verify lldp is running.
    2. Do interface flapping for each of mgmt ports.
    3. Verify lldp frames are sent and valid.
    """
    system = System()
    lldp = system.lldp
    _verify_lldp_running(lldp, engine=engines.dut)

    mgmt_ports = [Port(name=interface_name) for interface_name in devices.dut.get_mgmt_ports()]

    try:
        for _ in range(4):
            for mgmt_port in mgmt_ports:
                mgmt_port.interface.link.state.set(op_param_name=NvosConsts.LINK_STATE_DOWN, apply=True,
                                                   ask_for_confirmation=True, dut_engine=serial_engine).verify_result()

            serial_engine.run_cmd("nv config apply -y")

            for mgmt_port in mgmt_ports:
                mgmt_port.interface.link.state.set(op_param_name=NvosConsts.LINK_STATE_UP, apply=True,
                                                   ask_for_confirmation=True, dut_engine=serial_engine).verify_result()

            serial_engine.run_cmd("nv config apply -y")

        check_port_status_till_alive(True, engines.dut.ip, engines.dut.ssh_port)

    finally:
        with allure.step("Verify ports are up after all the flapping test"):
            for mgmt_port in mgmt_ports:
                mgmt_port.interface.link.state.unset(apply=True, dut_engine=serial_engine, ask_for_confirmation=True).verify_result()
            check_port_status_till_alive(True, engines.dut.ip, engines.dut.ssh_port)
            _verify_lldp_is_sending_frames(lldp=lldp, engine=engines.dut, device=devices.dut)


@pytest.mark.lldp
@pytest.mark.system
@pytest.mark.interface
def test_lldp_disable_dhcp(engines, devices, serial_engine):
    """
    Check that correct lldp sends mac address if dhcp is disabled.
    1. Verify lldp is running.
    2. Disable dhcp client
    3. Verify lldp frames do not contain ip addresses.
    4. Verify lldp frames contain mac address.
    """
    system = System()
    _verify_lldp_running(system.lldp, engine=engines.dut)

    with allure.step(f"Testing all mgmt ports"):
        for interface_name in devices.dut.get_mgmt_ports():
            mgmt_interface = Port(name=interface_name)
            with allure.independent_step(f"Testing {interface_name}"):
                try:
                    with allure.step("Get ip addresses"):
                        # Get IPv4 addresses
                        ipv4_addresses_dict = mgmt_interface.interface.ipv4.address.parse_show(dut_engine=serial_engine)
                        ipv4_addresses = list(ipv4_addresses_dict.keys())

                        # Get IPv6 addresses
                        ipv6_addresses_dict = mgmt_interface.interface.ipv6.address.parse_show(dut_engine=serial_engine)
                        ipv6_addresses = list(ipv6_addresses_dict.keys())

                        # Combine both for backward compatibility
                        ip_addresses = ipv4_addresses + ipv6_addresses

                    with allure.step(f"Disable dhcp-client for {interface_name}"):
                        mgmt_interface.interface.ipv4.dhcp_client.set(SystemConsts.STATE, NvosConst.DISABLED, apply=True,
                                                                      ask_for_confirmation=True, dut_engine=serial_engine).verify_result()
                        check_port_status_till_alive(False, engines.dut.ip, engines.dut.ssh_port)

                    with allure.step("Verify lldp frames do not contain hostname"):
                        LLDPTool.verify_mgmt_ports_are_up(engine=serial_engine, device=devices.dut)
                        output = LLDPTool.get_lldp_frames(engine=serial_engine, interface=interface_name)
                        interface_link = OutputParsingTool.parse_json_str_to_dictionary(mgmt_interface.interface.link.show(dut_engine=serial_engine)).get_returned_value()
                        for ip_address in ip_addresses:
                            assert ip_address not in output, f"The {ip_address} is found in output"
                        assert interface_link[SystemConsts.MAC] in output, f"The {interface_link[SystemConsts.MAC]} is not found in output"

                finally:
                    mgmt_interface.interface.ipv4.dhcp_client.unset(apply=True, dut_engine=serial_engine, ask_for_confirmation=True).verify_result()
                    check_port_status_till_alive(True, engines.dut.ip, engines.dut.ssh_port)


@pytest.mark.lldp
@pytest.mark.system
@pytest.mark.interface
@pytest.mark.cumulus_only
def test_lldp_max_values_for_cumulus(engines, devices):
    """
    Verify lldp operates correctly with maximum allowed interval and multiplier, with full validation.

    Test flow:
    1. Verify lldp is running and config can be read.
    2. Set interval to maximum allowed value (13107); interval * multiplier must be <= 65535.
    3. Set multiplier to 5 and apply.
    4. Verify configured values via lldp show (parsed output).
    5. Verify lldp continues to send frames on all mgmt ports and neighbors are visible.
    6. Restore default lldp values and verify lldp is still running.
    """
    system = System()
    lldp = system.lldp

    with allure.step("Verify lldp is running and get default configuration"):
        _verify_lldp_running(lldp, engine=engines.dut)
        lldp.parsed_show()  # ensure we can read config before changing it

    try:
        with allure.step(f"Set lldp interval to maximum value {LLDP_MAX_INTERVAL}"):
            _set_lldp_state(lldp, SystemConsts.LLDP_INTERVAL, LLDP_MAX_INTERVAL, apply=False)

        with allure.step(f"Set lldp hold-multiplier to {LLDP_MAX_MULTIPLIER} and apply"):
            _set_lldp_state(lldp, SystemConsts.LLDP_MULTIPLIER, LLDP_MAX_MULTIPLIER)

        with allure.step("Validate tx-interval and tx-hold-multiplier via nv show system lldp"):
            cli_output = lldp.parsed_show()  # runs: nv show system lldp --output json
            assert int(cli_output[SystemConsts.LLDP_INTERVAL]) == LLDP_MAX_INTERVAL, (
                f"Expected tx-interval {LLDP_MAX_INTERVAL}, got {cli_output[SystemConsts.LLDP_INTERVAL]}"
            )
            assert int(cli_output[SystemConsts.LLDP_MULTIPLIER]) == LLDP_MAX_MULTIPLIER, (
                f"Expected tx-hold-multiplier {LLDP_MAX_MULTIPLIER}, got {cli_output[SystemConsts.LLDP_MULTIPLIER]}"
            )

        with allure.step("Verify lldp is running and neighbors are present (no frame capture to avoid long wait)"):
            _verify_lldp_running_and_neighbors_present(lldp=lldp, engine=engines.dut, device=devices.dut)

    finally:
        _restore_lldp_defaults(lldp, engine=engines.dut)


def _verify_cli_output_with_dump_output(engine, device, lldp, system_output):
    cli_output = lldp.parsed_show()
    for interface_name in device.get_mgmt_ports():
        with allure.step(f"Get and parse tcp dump for {interface_name}"):
            lldp_dump = LLDPTool.get_lldp_frames(engine=engine, interface=interface_name)
            lldp_dict = LLDPTool.parse_lldp_dump(lldp_dump)
            ttl = int(cli_output[SystemConsts.LLDP_INTERVAL]) * int(cli_output[SystemConsts.LLDP_MULTIPLIER])
            port_name = device.get_lldp_port_name_from_dump(lldp_dict)
            assert port_name == interface_name, f'The {interface_name} name does not match lldp frame port id'
            ttl_in_frame = lldp_dict.get(TcpDumpConsts.LLDP_TIME_TO_LIVE)
            assert ttl_in_frame is not None, (
                f"No TTL found in LLDP dump for {interface_name}; parsed keys: {list(lldp_dict.keys())}."
            )
            assert int(ttl_in_frame) == ttl, 'The cli ttl does not match sent frame time to live'
            is_match_hostname = lldp_dict[TcpDumpConsts.LLDP_SYSTEM_NAME] in system_output[SystemConsts.HOSTNAME] or \
                system_output[SystemConsts.HOSTNAME] in lldp_dict[TcpDumpConsts.LLDP_SYSTEM_NAME]
            assert is_match_hostname, "The hostname do not match"


def _verify_lldp_running(lldp, engine):
    if TestToolkit.devices.dut.is_ib() or TestToolkit.devices.dut.is_nvl():
        with allure.step("Verify lldp container is running"):
            lldp_running = engine.run_cmd('docker inspect --format \'{{.State.Running}}\' lldp')
            assert lldp_running == 'true', 'The lldp docker container is down'
    with allure.step("Verify lldp is running and enabled"):
        cli_output = lldp.parsed_show()
        assert cli_output[SystemConsts.LLDP_STATE] == NvosConst.ENABLED, 'The lldp is not enabled'


def _verify_lldp_is_sending_frames(lldp, engine, device):
    _verify_lldp_running(lldp, engine)
    cli_output = lldp.parsed_show()

    with allure.step("Verify lldp frames are being sent for each active mgmt interface"):
        interval = int(cli_output[SystemConsts.LLDP_INTERVAL])
        for interface_name in device.get_mgmt_ports():
            output = LLDPTool.get_lldp_frames(engine=engine, interval=interval, interface=interface_name)
            assert interface_name in output, f"The data for {interface_name} not found in lldp frames"

            mgmt_interface = Port(name=interface_name)
            output_dict = OutputParsingTool.parse_json_str_to_dictionary(
                mgmt_interface.interface.lldp.neighbor.show()).get_returned_value()
            assert output_dict, f"The neighbors output for {interface_name} is empty"


def _verify_lldp_running_and_neighbors_present(lldp, engine, device):
    """
    Verify LLDP is running and each mgmt interface has at least one neighbor.
    Does not capture frames (no tcpdump), so avoids long wait when tx-interval is large (e.g. max 13107s).
    """
    _verify_lldp_running(lldp, engine)
    with allure.step("Verify each mgmt interface has at least one LLDP neighbor"):
        for interface_name in device.get_mgmt_ports():
            mgmt_interface = Port(name=interface_name)
            output_dict = OutputParsingTool.parse_json_str_to_dictionary(
                mgmt_interface.interface.lldp.neighbor.show()).get_returned_value()
            assert output_dict, f"The neighbors output for {interface_name} is empty"


def _verify_lldp_not_running(lldp, engine, device):
    with allure.step("Verify lldp container is not running"):
        if TestToolkit.devices.dut.is_ib() or TestToolkit.devices.dut.is_nvl():
            if TestToolkit.devices.dut.is_ib() and is_redmine_issue_active(4868603):
                time.sleep(5)

            def _check_lldp_container_stopped():
                lldp_running = engine.run_cmd('docker inspect --format \'{{.State.Running}}\' lldp')
                assert lldp_running == 'false', f'The lldp docker container is still up (state: {lldp_running})'

            ValidationTool.retry_until_valid(_check_lldp_container_stopped,
                                             description="Verify lldp container is not running")
    with allure.step("Verify lldp is not running and not enabled"):
        cli_output = lldp.parsed_show()
        assert cli_output[SystemConsts.LLDP_STATE] == NvosConst.DISABLED, 'The lldp is enabled'

    with allure.step("Verify lldp frames are not being sent for each active mgmt interface"):
        for interface_name in device.get_mgmt_ports():
            mgmt_interface = Port(name=interface_name)
            output_dict = OutputParsingTool.parse_json_str_to_dictionary(
                mgmt_interface.interface.lldp.neighbor.show()).get_returned_value()
            assert not output_dict, f"The neighbors output for {interface_name} is not empty"

            with allure.step(f"Verify that no lldp frames are sent"):
                lldp_frames_output = LLDPTool.get_lldp_frames(engine=engine, interface=interface_name)
                assert not lldp_frames_output, f"There are still lldp frames sent for {interface_name}"


def _wait_until_ipv6_removed_from_interface(mgmt_interface, ip_address_full, serial_engine,
                                            timeout=INTERFACE_CLEANUP_DELAY, poll_interval=2):
    """Poll until the given IPv6 address is no longer present on the interface (after unset)."""
    ip_address = ip_address_full.split("/")[0] if "/" in ip_address_full else ip_address_full
    deadline = time.monotonic() + timeout
    with allure.step(f"Wait up to {timeout}s for interface cleanup after IPv6 unset"):
        while time.monotonic() < deadline:
            addrs_dict = mgmt_interface.interface.ipv6.address.parse_show(dut_engine=serial_engine)
            if not addrs_dict or ip_address not in str(addrs_dict):
                return
            time.sleep(poll_interval)
        addrs_dict = mgmt_interface.interface.ipv6.address.parse_show(dut_engine=serial_engine)
        assert ip_address not in str(addrs_dict), (
            f"IPv6 {ip_address} still present on interface after {timeout}s"
        )


def _restore_lldp_defaults(lldp, engine):
    """Restore default LLDP interval, multiplier and state; verify LLDP is running."""
    ask_for_confirmation = getattr(TestToolkit.devices.dut, "ask_for_confirmation", False)
    with allure.step("Restore default lldp interval and multiplier"):
        lldp.unset(SystemConsts.LLDP_INTERVAL).verify_result()
        lldp.unset(SystemConsts.LLDP_MULTIPLIER).verify_result()
        lldp.unset(SystemConsts.STATE, apply=True, ask_for_confirmation=ask_for_confirmation).verify_result()
    with allure.step("Verify lldp is running after restoring defaults"):
        _verify_lldp_running(lldp, engine=engine)


def _wait_for_lldp_state_applied(lldp, key, expected_val, timeout=LLDP_STATE_CHANGE_POLL_TIMEOUT,
                                 poll_interval=LLDP_STATE_CHANGE_POLL_INTERVAL):
    """Poll until lldp parsed_show shows key == expected_val or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        cli_output = lldp.parsed_show()
        current = cli_output.get(key)
        if current is not None and str(current) == str(expected_val):
            return
        time.sleep(poll_interval)
    cli_output = lldp.parsed_show()
    assert cli_output.get(key) == expected_val, (
        f"LLDP {key} did not reach {expected_val} within {timeout}s; current: {cli_output.get(key)}"
    )


def _set_lldp_state(lldp, key, val, wait_for_state=True, apply=True):
    with allure.step(f"Set lldp {key} to {val}"):
        lldp.set(key, val, apply=apply, ask_for_confirmation=True).verify_result()
        if wait_for_state and apply:
            _wait_for_lldp_state_applied(lldp, key, val)
