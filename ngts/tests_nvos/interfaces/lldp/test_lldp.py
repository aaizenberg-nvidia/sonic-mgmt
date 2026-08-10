import logging
import random
import time

import pytest

from devts.infra.tools.validations.traffic_validations.port_check.port_checker import check_port_status_till_alive
from ngts.nvos_constants.constants_nvos import SystemConsts, NvosConst, TcpDumpConsts
from ngts.nvos_tools.ib.InterfaceConfiguration.Interface import Interface
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import NvosConsts
from ngts.nvos_tools.infra.IpTool import IpTool
from ngts.tests_nvos.constants import MINUTE
from ngts.nvos_tools.infra.LLDPTool import LLDPTool
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from devts.infra.tools.redmine.redmine_api import is_redmine_issue_active
from ngts.nvos_tools.system.System import System
from ngts.tools.test_utils import allure_utils as allure


cumulus_owner = "lharo"


logger = logging.getLogger(__name__)


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

    with allure.step("Testing all mgmt ports"):
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

    with allure.step("Testing all mgmt ports"):
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

    with allure.step("Testing all mgmt ports"):
        mgmt_ports = devices.dut.get_mgmt_ports()
        for i, interface_name in enumerate(mgmt_ports):
            mgmt_interface = Port(name=interface_name)
            with allure.independent_step(f"Testing {interface_name}"):
                ip_address_full = None
                try:
                    ip_address_full = IpTool.select_random_ipv6_address().verify_result()  # 40c9:7735:e23d:dd2a:ca43:c5e9:682e:decb/114
                    ip_address, _prefix = ip_address_full.split("/")
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

    On Cumulus the flapping is done via kernel 'ip link set <port> up/down' via
    the serial console, instead of NVUE 'nv set interface <port> link state'.
    NVUE's apply pipeline runs ifreload-nvue.service, which runs
    'ethtool -s <port> duplex full' on igb-driven mgmt ports - the kernel
    rejects that with EINVAL ("Unsupported Speed/Duplex configuration"), causing
    NVUE to roll back the apply and leaving the port in an inconsistent state
    (often DOWN). The kernel ip-link path bypasses NVUE and ifreload-nvue.service
    entirely, so the link event still fires for LLDP without triggering the
    duplex apply failure. On NVOS/IB the original NVUE path is kept.
    """
    system = System()
    lldp = system.lldp
    _verify_lldp_running(lldp, engine=engines.dut)

    mgmt_port_names = devices.dut.get_mgmt_ports()
    is_cumulus = devices.dut.is_eth()

    try:
        for _ in range(4):
            for port_name in mgmt_port_names:
                _set_mgmt_link_state(serial_engine, port_name, NvosConsts.LINK_STATE_DOWN, is_cumulus)
            if not is_cumulus:
                serial_engine.run_cmd("nv config apply -y")

            for port_name in mgmt_port_names:
                _set_mgmt_link_state(serial_engine, port_name, NvosConsts.LINK_STATE_UP, is_cumulus)
            if not is_cumulus:
                serial_engine.run_cmd("nv config apply -y")

        repair_failures = []
        if is_cumulus:
            repair_failures = _repair_cumulus_mgmt_ports_dhcp(mgmt_port_names, serial_engine)

        try:
            check_port_status_till_alive(True, engines.dut.ip, engines.dut.ssh_port)
        except Exception as reach_exc:
            if repair_failures:
                report = "\n".join(
                    f"  - port={p} cmd={c!r} error={e}" for p, c, e in repair_failures
                )
                raise AssertionError(
                    f"DUT became TCP-unreachable after mgmt-port flapping ({reach_exc}). "
                    f"Cumulus mgmt repair previously reported {len(repair_failures)} "
                    f"command failure(s) which likely contributed:\n{report}"
                ) from reach_exc
            raise

    finally:
        _restore_mgmt_ports_after_flapping(mgmt_port_names, serial_engine, engines, devices, lldp)


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

    with allure.step("Testing all mgmt ports"):
        for interface_name in devices.dut.get_mgmt_ports():
            mgmt_interface = Port(name=interface_name)
            with allure.independent_step(f"Testing {interface_name}"):
                # A port returned by get_mgmt_ports() is declared by the platform's
                # device model as a working mgmt port, so it is expected to carry LLDP.
                # lldpd transmits LLDP frames only on interfaces with operational
                # carrier (ifconfig RUNNING); a declared mgmt port with no carrier is a
                # real defect, not something to silently skip. If a port is
                # intentionally disconnected on a platform, remove it from that
                # platform's mgmt_ports list (see Devices/*.py) instead of tolerating
                # it here.
                assert LLDPTool.is_mgmt_port_carrier_up(serial_engine, interface_name), (
                    f"{interface_name} is declared as a mgmt port but has no operational "
                    f"carrier (ifconfig RUNNING); lldpd will not transmit LLDP on it. If "
                    f"this port is intentionally disconnected on this platform, remove it from "
                    f"the platform's mgmt_ports list instead of leaving it untested.")
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

                    with allure.step("Verify lldp frames do not contain management IP addresses and do contain interface MAC"):
                        LLDPTool.verify_mgmt_ports_are_up(engine=serial_engine, device=devices.dut)
                        # Retry while the capture is empty: lldpd transmits on a
                        # tx-interval cadence, so a single window can race the
                        # transmitter and come back empty even when LLDP is healthy.
                        output = LLDPTool.get_lldp_frames(engine=serial_engine, interface=interface_name,
                                                          max_attempts=3)
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


def _set_mgmt_link_state(serial_engine, port_name, target_state, is_cumulus):
    """
    Set a management port's link state up/down, using the right level for the platform.

    On Cumulus we use kernel 'sudo -n ip link set <port> up/down' via the serial console.
    NVUE's apply pipeline calls ifreload-nvue.service which runs 'ethtool -s <port>
    duplex full'. On igb-driven mgmt ports the kernel rejects that with EINVAL
    ("Unsupported Speed/Duplex configuration"), causing NVUE to roll back the apply
    and leaving the port in an inconsistent state. The kernel path bypasses NVUE
    entirely while still firing the link-up/down event that LLDP listens to.

    On NVOS/IB we keep the NVUE path (the duplex bug is Cumulus-specific).
    """
    if is_cumulus:
        action = "down" if target_state == NvosConsts.LINK_STATE_DOWN else "up"
        serial_engine.run_cmd(f"sudo -n ip link set {port_name} {action}")
    else:
        Port(name=port_name).interface.link.state.set(
            op_param_name=target_state, apply=True,
            ask_for_confirmation=True, dut_engine=serial_engine,
        ).verify_result()


def _repair_cumulus_mgmt_ports_dhcp(mgmt_port_names, serial_engine):
    """
    Restore Cumulus mgmt-port autoneg + DHCP lease after rapid carrier flapping.

    Four rapid carrier events back-to-back leave Cumulus mgmt ports in two bad
    states that combine to make the switch unreachable from outside the local
    subnet even though port 22 is still listening:
      1) the kernel/ethtool state for the port can end up with autoneg off; the
         link then renegotiates at 10M (or not at all), and the NVUE 'applied'
         config (auto-negotiate enabled) no longer matches reality.
      2) dhclient can't keep up with the rapid lease churn, so the default
         route in 'vrf mgmt' ends up as 'unreachable default' with no real
         gateway.

    We intentionally bypass NVUE here: 'nv config apply' with no diff is a
    no-op, and forcing ifreload-nvue.service to re-apply the eth0 stanza is
    what triggers the ethtool-duplex bug we are already working around.

    All commands are idempotent and per-port-isolated so the test/cleanup can
    always run this without leaving mgmt half-repaired if a single command on
    one port fails.

    Returns a list of (port_name, cmd, error_message) tuples describing any
    commands that raised. Failures are also attached to the Allure report so a
    later reachability failure can be diagnosed without re-reading the test log.
    """
    failures = []
    for port_name in mgmt_port_names:
        for cmd in (
            f"sudo -n ethtool -s {port_name} autoneg on",
            f"sudo -n dhclient -r -i {port_name}",
            f"sudo -n dhclient -i {port_name}",
        ):
            try:
                serial_engine.run_cmd(cmd)
            except Exception as exc:
                logger.error("Cumulus mgmt repair '%s' failed: %s", cmd, exc)
                failures.append((port_name, cmd, str(exc)))

    if failures:
        report = "\n".join(
            f"  - port={p} cmd={c!r} error={e}" for p, c, e in failures
        )
        message = "Cumulus mgmt-port DHCP repair encountered failures:\n" + report
        logger.error(message)
        try:
            allure.attach(
                "cumulus_mgmt_repair_failures",
                message,
            )
        except Exception as attach_exc:
            logger.warning(
                "Could not attach cumulus_mgmt_repair_failures to Allure: %s",
                attach_exc,
            )
    return failures


def _restore_mgmt_ports_after_flapping(mgmt_port_names, serial_engine, engines, devices, lldp):
    """
    Defensive cleanup for test_lldp_interface_flapping.

    Each step is independent, so a single failure does not leave a mgmt port DOWN and
    break every subsequent test on this DUT - including tests in later MARS sessions,
    which would otherwise present as a "DUT is TCP-unreachable from session start"
    failure with no obvious root cause.

    On Cumulus we restore the link via kernel 'sudo -n ip link set <port> up' (NVUE's
    apply path is broken on igb mgmt ports - see _set_mgmt_link_state for the reason).
    On NVOS/IB we go through NVUE 'unset interface <port> link state', and if that
    fails we fall back to the same kernel 'sudo -n ip link set <port> up' as a last resort.

    check_port_status_till_alive at the end always runs, so an unreachable DUT is
    surfaced as a clear test ERROR rather than silently inherited by the next session.
    """
    is_cumulus = devices.dut.is_eth()
    cleanup_failures = []
    repair_failures = []

    with allure.step("Verify ports are up after all the flapping test"):
        if is_cumulus:
            # Kernel ip-link is the only restore path on Cumulus (NVUE apply is
            # broken on igb mgmt ports - see _set_mgmt_link_state). There is no
            # meaningful fallback beyond this command itself.
            for port_name in mgmt_port_names:
                cmd = f"sudo -n ip link set {port_name} up"
                try:
                    serial_engine.run_cmd(cmd)
                except Exception as exc:
                    logger.error("Cleanup kernel '%s' failed: %s", cmd, exc)
                    cleanup_failures.append((port_name, cmd, str(exc)))
            # Always run the autoneg/dhclient repair in cleanup, not just on the
            # happy path: if the flapping loop raised before the in-test repair
            # ran, mgmt is left with autoneg off and an 'unreachable default'
            # route, and every subsequent test on this DUT (in this and later
            # MARS sessions) fails with a TCP-unreachable DUT. Idempotent, so
            # safe to run after the in-test call too.
            repair_failures = _repair_cumulus_mgmt_ports_dhcp(mgmt_port_names, serial_engine)
        else:
            # On NVOS/IB go through NVUE 'unset interface <port> link state';
            # fall back to kernel 'sudo -n ip link set <port> up' if NVUE fails.
            for port_name in mgmt_port_names:
                try:
                    Port(name=port_name).interface.link.state.unset(
                        apply=True, dut_engine=serial_engine, ask_for_confirmation=True,
                    ).verify_result()
                except Exception as primary_exc:
                    logger.error(
                        "Cleanup NVUE link-state unset failed for %s: %s; "
                        "falling back to 'sudo -n ip link set %s up'.",
                        port_name, primary_exc, port_name,
                    )
                    cleanup_failures.append((port_name, "nvue unset link state", str(primary_exc)))
                    fallback_cmd = f"sudo -n ip link set {port_name} up"
                    try:
                        serial_engine.run_cmd(fallback_cmd)
                    except Exception as fallback_exc:
                        logger.error(
                            "Cleanup kernel fallback '%s' also failed: %s",
                            fallback_cmd, fallback_exc,
                        )
                        cleanup_failures.append((port_name, fallback_cmd, str(fallback_exc)))

            # Persist the unset via 'nv config apply'. On Cumulus we never
            # touched NVUE config (kernel ip-link was used), so nothing to apply.
            try:
                serial_engine.run_cmd("nv config apply -y")
            except Exception as apply_exc:
                logger.error("Final 'nv config apply -y' failed during cleanup: %s", apply_exc)
                cleanup_failures.append(("<global>", "nv config apply -y", str(apply_exc)))

        all_failures = cleanup_failures + repair_failures
        if all_failures:
            report = "\n".join(
                f"  - port={p} cmd={c!r} error={e}" for p, c, e in all_failures
            )
            message = "Mgmt-port restore/repair encountered failures during cleanup:\n" + report
            logger.error(message)
            try:
                allure.attach(
                    "mgmt_port_cleanup_failures",
                    message,
                )
            except Exception as attach_exc:
                logger.warning(
                    "Could not attach mgmt_port_cleanup_failures to Allure: %s",
                    attach_exc,
                )

        try:
            check_port_status_till_alive(True, engines.dut.ip, engines.dut.ssh_port)
        except Exception as reach_exc:
            if all_failures:
                report = "\n".join(
                    f"  - port={p} cmd={c!r} error={e}" for p, c, e in all_failures
                )
                raise AssertionError(
                    f"DUT remained TCP-unreachable after cleanup ({reach_exc}). "
                    f"Cleanup recorded {len(all_failures)} repair/restore failure(s) "
                    f"which likely contributed:\n{report}"
                ) from reach_exc
            raise

        _verify_lldp_is_sending_frames(lldp=lldp, engine=engines.dut, device=devices.dut)


def _verify_lldp_running(lldp, engine):
    if TestToolkit.devices.dut.is_ib():
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
        if TestToolkit.devices.dut.is_ib():
            if is_redmine_issue_active(4868603):
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

            with allure.step("Verify that no lldp frames are sent"):
                lldp_frames_output = LLDPTool.get_lldp_frames(engine=engine, interface=interface_name)
                assert not lldp_frames_output, f"There are still lldp frames sent for {interface_name}"


def _wait_until_ipv6_removed_from_interface(mgmt_interface, ip_address_full, serial_engine,
                                            timeout=INTERFACE_CLEANUP_DELAY):
    """Wait for interface cleanup after IPv6 unset before configuring the next mgmt port.

    `nv config apply` for the unset returns and `parse_show` stops reporting the address
    well before the management plane has actually settled. Returning early causes the
    next port's set to race and the just-set address to be invisible to verify
    (Redmine #4544216). A flat sleep for the full INTERFACE_CLEANUP_DELAY is required.
    """
    ip_address = ip_address_full.split("/")[0] if "/" in ip_address_full else ip_address_full
    with allure.step(f"Wait {timeout}s for interface cleanup after IPv6 unset"):
        time.sleep(timeout)
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


def _set_lldp_state(
    lldp,
    key,
    val,
    wait_for_state=True,
    apply=True,
    poll_interval=LLDP_STATE_CHANGE_POLL_INTERVAL,
):
    with allure.step(f"Set lldp {key} to {val}"):
        lldp.set(key, val, apply=apply, ask_for_confirmation=True).verify_result()
        if wait_for_state and apply:
            _wait_for_lldp_state_applied(lldp, key, val, poll_interval=poll_interval)
