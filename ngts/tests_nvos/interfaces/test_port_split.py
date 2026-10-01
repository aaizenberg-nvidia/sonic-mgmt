import logging
import pytest
from ngts.tools.test_utils import allure_utils as allure
import random
import time
from retry import retry
from ngts.nvos_tools.infra.Tools import Tools
from ngts.tests_nvos.constants import MINUTE
from ngts.nvos_tools.system.System import System
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_constants.constants_nvos import SystemConsts, DatabaseConst, EventConsts
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import IbInterfaceConsts
from ngts.cli_wrappers.nvue.nvue_system_clis import NvueSystemCli
from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import NvosConsts
from ngts.nvos_tools.ib.opensm.OpenSmTool import OpenSmTool
from retry.api import retry_call
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import PortRequirements
from ngts.nvos_tools.cli_coverage.operation_time import OperationTime
from ngts.tests_nvos.general.telemetry.ib import umf_churn_helpers as umf_churn
from ngts.tests_nvos.general.telemetry.ib.constants import (
    NVBUG_5768072,
    NVBUG_6152581,
    NVBUG_6152697,
    STUCK_SPLIT_LANES_RE,
    STUCK_SPLIT_STATE_DB_MARKER,
)
from ngts.nvos_tools.infra.ResultObj import IssueType, ResultObj

invalid_cmd_str = ['Invalid config', 'Error', 'command not found', 'Bad Request', 'Not Found', "unrecognized arguments",
                   "error: unrecognized arguments", "invalid choice", "Action failed", "Invalid Command",
                   "You do not have permission", "The requested item does not exist."]

MIN_PACKET_INCREASE = 5  # Minimum number of packets we expect to see increase

logger = logging.getLogger()


@pytest.mark.ib_interfaces
def test_ib_split_port_no_breakout_profile(engines, interfaces, start_sm, devices):
    """
    Test flow:
        1. Try to split port, eth0, ib0 in not breakout profile
        2. Change profile to breakout mode enabled
        3. Try to split port, eth0, ib0 in not breakout profile
        4. Try to split already splitted port
        5. Unset
        6. Assert no empty-IB-speed UMF ERROR during churn (NVBug 6152581 / UMF !191)
    """
    if not devices.dut.split_ports_supported:
        pytest.skip("Split is not supported on this setup")

    umf_marker = umf_churn.place_umf_churn_marker(engines, "split-no-breakout-profile")
    system = System(None)
    with allure.step("Verify default system profile"):
        system_profile_output = OutputParsingTool.parse_json_str_to_dictionary(system.profile.show()) \
            .get_returned_value()
        ValidationTool.validate_fields_values_in_output(SystemConsts.PROFILE_OUTPUT_FIELDS,
                                                        devices.dut.system_profile_default_values,
                                                        system_profile_output).verify_result()
        logging.info("All expected values were found")

    with allure.step("Try split splitter port in not breakout system profile"):
        split_ports, active_ports = _get_split_ports()
        for port in split_ports:
            port.interface.link.set(op_param_name='breakout', op_param_value=IbInterfaceConsts.LINK_BREAKOUT_HDR,
                                    apply=True, ask_for_confirmation=True).verify_result(False)
            port.interface.link.set(op_param_name='breakout', op_param_value=IbInterfaceConsts.LINK_BREAKOUT_NDR,
                                    apply=True, ask_for_confirmation=True).verify_result(False)
            NvueGeneralCli.detach_config(TestToolkit.engines.dut)
            output = NvueGeneralCli.diff_config(TestToolkit.engines.dut)
            assert not output, "config not detached"

    with allure.step("Try split eth0 and ib0 port in not breakout system profile"):
        mgmt_port = Port('eth0')
        ipoib_port = Port('ib0')
        mgmt_port.interface.link.set(op_param_name='breakout', op_param_value=IbInterfaceConsts.LINK_BREAKOUT_HDR,
                                     apply=True, ask_for_confirmation=True).verify_result(False)
        mgmt_port.interface.link.set(op_param_name='breakout', op_param_value=IbInterfaceConsts.LINK_BREAKOUT_NDR,
                                     apply=True, ask_for_confirmation=True).verify_result(False)
        NvueGeneralCli.detach_config(TestToolkit.engines.dut)
        output = NvueGeneralCli.diff_config(TestToolkit.engines.dut)
        assert not output, "config not detached"

        ipoib_port.interface.link.set(op_param_name='breakout', op_param_value=IbInterfaceConsts.LINK_BREAKOUT_HDR,
                                      apply=True, ask_for_confirmation=True).verify_result(False)
        ipoib_port.interface.link.set(op_param_name='breakout', op_param_value=IbInterfaceConsts.LINK_BREAKOUT_NDR,
                                      apply=True, ask_for_confirmation=True).verify_result(False)
        NvueGeneralCli.detach_config(TestToolkit.engines.dut)
        output = NvueGeneralCli.diff_config(TestToolkit.engines.dut)
        assert not output, "config not detached"

    with allure.step('Change system profile to breakout-mode enabled, adaptive-routing enabled'):
        if devices.dut.profile_change_supported:
            with allure.step("Enable adaptive-routing and enable breakout-mode "):
                system.profile.action_profile_change(params_dict={'adaptive-routing': 'enabled',
                                                                  'breakout-mode': 'enabled'})

        with allure.step("Start OpenSm"):
            OpenSmTool.start_open_sm(engines).verify_result()

        with allure.step('Verify changed values'):
            system_profile_output = OutputParsingTool.parse_json_str_to_dictionary(system.profile.show()) \
                .get_returned_value()
            values_to_verify = [SystemConsts.PROFILE_STATE_ENABLED, 1792,
                                SystemConsts.PROFILE_STATE_ENABLED, SystemConsts.PROFILE_STATE_DISABLED,
                                SystemConsts.DEFAULT_NUM_SWIDS]
            ValidationTool.validate_fields_values_in_output(SystemConsts.PROFILE_OUTPUT_FIELDS,
                                                            values_to_verify,
                                                            system_profile_output).verify_result()
            logging.info("All expected values were found")

    with allure.step("Try split eth0 and ib0 port in breakout system profile"):
        mgmt_port.interface.link.set(op_param_name='breakout', op_param_value=IbInterfaceConsts.LINK_BREAKOUT_HDR,
                                     apply=True, ask_for_confirmation=True).verify_result(False)
        mgmt_port.interface.link.set(op_param_name='breakout', op_param_value=IbInterfaceConsts.LINK_BREAKOUT_NDR,
                                     apply=True, ask_for_confirmation=True).verify_result(False)
        NvueGeneralCli.detach_config(TestToolkit.engines.dut)
        output = NvueGeneralCli.diff_config(TestToolkit.engines.dut)
        assert not output, "config not detached"

        ipoib_port.interface.link.set(op_param_name='breakout', op_param_value=IbInterfaceConsts.LINK_BREAKOUT_HDR,
                                      apply=True, ask_for_confirmation=True).verify_result(False)
        ipoib_port.interface.link.set(op_param_name='breakout', op_param_value=IbInterfaceConsts.LINK_BREAKOUT_NDR,
                                      apply=True, ask_for_confirmation=True).verify_result(False)
        NvueGeneralCli.detach_config(TestToolkit.engines.dut)
        output = NvueGeneralCli.diff_config(TestToolkit.engines.dut)
        assert not output, "config not detached"

    with allure.step("Check traffic port up"):
        port_names = [port.name for port in active_ports]
        retry_call(validate_list_ports_state, [port_names], exceptions=AssertionError, tries=6, delay=10)

    with allure.step("Split splitter port"):
        parent_port = split_ports[0]
        parent_port.interface.link.set(op_param_name='breakout', op_param_value=IbInterfaceConsts.LINK_BREAKOUT_XDR,
                                       apply=True, ask_for_confirmation=True).verify_result()

    with allure.step("Try split already splitted port"):
        list_of_all_ports = Port.get_list_of_ports()
        child_ports = []
        for port in list_of_all_ports:
            if parent_port.name in port.name and port.name[-2] == 's':
                child_ports.append(port)
        for child_port in child_ports:
            child_port.interface.link.set(op_param_name='breakout',
                                          op_param_value=IbInterfaceConsts.LINK_BREAKOUT_HDR, apply=True,
                                          ask_for_confirmation=True).verify_result(False)
            child_port.interface.link.set(op_param_name='breakout',
                                          op_param_value=IbInterfaceConsts.LINK_BREAKOUT_NDR, apply=True,
                                          ask_for_confirmation=True).verify_result(False)
            NvueGeneralCli.detach_config(TestToolkit.engines.dut)

    with allure.step("Unset parent port"):
        child_port.interface.link.unset(op_param='breakout', apply=True, ask_for_confirmation=True).verify_result()

    with allure.step(
        f"NVBug {NVBUG_6152581} / UMF !191: no empty-IB-speed ParseFloat ERROR during split churn"
    ):
        umf_churn.assert_no_empty_ib_speed_parse_errors(engines, umf_marker)


@pytest.mark.ib_interfaces
def test_ib_split_port_default_values(engines, interfaces, start_sm, devices):
    """
    Test flow:
        1. Change profile to breakout enabled
        2. Check parent port default value
        3. Split port
        4. Check parent and child default value
        5. Change child port values
        6. Negative testing for child port
        7. Unset parent port
        8. Check it returned to default values
    """
    if not devices.dut.split_ports_supported:
        pytest.skip("Split is not supported on this setup")

    system = System(None)
    with allure.step("Check traffic port up"):
        active_ports = Tools.RandomizationTool.get_random_active_port(number_of_values_to_select=0).get_returned_value()
        port_names = [port.name for port in active_ports]
        retry_call(validate_list_ports_state, [port_names], exceptions=AssertionError, tries=6, delay=10)

    with allure.step('Verify breakout values'):
        system_profile_output = OutputParsingTool.parse_json_str_to_dictionary(system.profile.show()) \
            .get_returned_value()
        ValidationTool.validate_fields_values_in_output(SystemConsts.PROFILE_OUTPUT_FIELDS,
                                                        devices.dut.system_profile_default_values,
                                                        system_profile_output).verify_result()
        logging.info("All expected values were found")

    with allure.step("Change default values for a parent port"):
        split_ports, active_ports = _get_split_ports()
        parent_port = split_ports[0]
        parent_port.interface.set(op_param_name='description', op_param_value='"parent"', apply=True).verify_result()

        with allure.step("Verify changes"):
            output_dictionary = Tools.OutputParsingTool.parse_show_interface_pluggable_output_to_dictionary(
                parent_port.show_interface(port_names=parent_port.name)).get_returned_value()
            Tools.ValidationTool.verify_field_value_in_output(output_dictionary=output_dictionary,
                                                              field_name=IbInterfaceConsts.DESCRIPTION,
                                                              expected_value='parent').verify_result()

    with allure.step("Split port, check default values for child and parent port"):
        parent_port.interface.link.set(op_param_name='breakout', op_param_value=IbInterfaceConsts.LINK_BREAKOUT_XDR,
                                       apply=True, ask_for_confirmation=True).verify_result()
        output_dictionary = Tools.OutputParsingTool.parse_show_interface_pluggable_output_to_dictionary(
            parent_port.show_interface(port_names=parent_port.name)).get_returned_value()
        Tools.ValidationTool.validate_fields_values_in_output(expected_fields=['link'],
                                                              expected_values=[{'breakout': '2x-xdr'}],
                                                              output_dict=output_dictionary).verify_result()

        with allure.step("Verify default values on child port"):
            with allure.step("Get child port"):
                list_of_all_ports = Port.get_list_of_ports()
                child_ports = []
                for port in list_of_all_ports:
                    if parent_port.name in port.name and port.name[-2] == 's':
                        child_ports.append(port)
            child_ports[0].interface.wait_for_port_state(NvosConsts.LINK_STATE_UP, sleep_time=30).verify_result()
            time.sleep(5)
            output_dictionary = Tools.OutputParsingTool.parse_show_interface_link_output_to_dictionary(
                child_ports[0].interface.link.show()).get_returned_value()
            Tools.ValidationTool.compare_values(output_dictionary[IbInterfaceConsts.LINK_MTU],
                                                IbInterfaceConsts.SPLIT_PORT_DEFAULT_MTU, True).verify_result()
            values_to_verify = [NvosConsts.LINK_STATE_UP, IbInterfaceConsts.SPLIT_PORT_CHILD_DEFAULT_LANES,
                                IbInterfaceConsts.SPLIT_PORT_DEFAULT_MTU]
            ValidationTool.validate_fields_values_in_output(['state', 'lanes', 'mtu'],
                                                            values_to_verify,
                                                            output_dictionary).verify_result()

    with allure.step("Change mtu, check changes for child port"):
        child_ports[0].interface.link.set(op_param_name='mtu', op_param_value=512, apply=True,
                                          ask_for_confirmation=True).verify_result()

        with allure.step("Verify changed values on child port"):
            child_ports[0].interface.wait_for_port_state(NvosConsts.LINK_STATE_UP, sleep_time=30).verify_result()
            child_ports[0].interface.wait_for_mtu_changed(512)

    with allure.step("Negative testing on child with lanes"):
        child_ports[0].interface.link.set(op_param_name='lanes', op_param_value='1X,2X,4X', apply=True,
                                          ask_for_confirmation=True).verify_result(False)
        child_ports[0].interface.link.set(op_param_name='lanes', op_param_value='1X,4X', apply=True,
                                          ask_for_confirmation=True).verify_result(False)
        NvueGeneralCli.detach_config(TestToolkit.engines.dut)

    with allure.step("Unset parent port"):
        parent_port.interface.link.unset(op_param='breakout', apply=True, ask_for_confirmation=True).verify_result()
        parent_port.interface.wait_for_port_state(NvosConsts.LINK_STATE_UP, sleep_time=30).verify_result()

    with allure.step("Check default values after unset parent port"):
        values_to_verify = [IbInterfaceConsts.SPLIT_PORT_DEFAULT_LANES, IbInterfaceConsts.DEFAULT_MTU,
                            IbInterfaceConsts.SPLIT_PORT_DEFAULT_VLS]
        parent_port.interface.wait_for_port_state(NvosConsts.LINK_STATE_UP, sleep_time=30).verify_result()
        parent_port.interface.wait_for_mtu_changed(IbInterfaceConsts.DEFAULT_MTU)
        output_dictionary = Tools.OutputParsingTool.parse_show_interface_link_output_to_dictionary(
            parent_port.interface.link.show()).get_returned_value()
        ValidationTool.validate_fields_values_in_output(['lanes', 'mtu', 'op-vls'],
                                                        values_to_verify,
                                                        output_dictionary).verify_result()


@pytest.mark.ib_interfaces
def test_split_port_counters(engines, players, interfaces, start_sm, devices, setup_name):
    """
    Test flow:
        1. Get split ports and clear counters for parent port
        2. Send traffic and verify counters increased on parent port
        3. Split port and get child ports
        4. Verify child ports are up and ready for traffic
        5. Send traffic and verify counters increased on s1 port
        6. Bring down s1 port and verify traffic still passes through s2
        7. Unset parent port and verify it returns to original state
        8. Send traffic again and verify it passes through the original parent port
    """
    if not devices.dut.split_ports_supported:
        pytest.skip("Split is not supported on this setup")

    with allure.step("Verify correct Noga setup"):
        assert engines.ha and engines.hb, "Traffic hosts details can't be found in Noga setup"

    with allure.step("Get split ports"):
        split_ports, active_ports = _get_split_ports()
        parent_port = split_ports[0]

    with allure.step("Clear counters before first traffic run"):
        parent_port.interface.action_clear_counter_for_interface(interface_name=parent_port.name).verify_result()
        logger.info(f"Cleared counters for parent port {parent_port.name}")

    with allure.step("Run traffic"):
        Tools.TrafficGeneratorTool.send_ib_traffic(players, interfaces, setup_name, True).verify_result()

    with allure.step("Check counters increased after first traffic run"):
        output_dictionary = Tools.OutputParsingTool.parse_show_interface_stats_output_to_dictionary(
            parent_port.interface.counters.show()).get_returned_value()
        logger.info(f"Parent port ({parent_port.name}) counters after traffic: in-pkts={output_dictionary['in-pkts']}, out-pkts={output_dictionary['out-pkts']}")

        # Check that counters increased from 0 (since we cleared them)
        assert output_dictionary['in-pkts'] >= MIN_PACKET_INCREASE, f"Parent port {parent_port.name} in-pkts should be at least {MIN_PACKET_INCREASE}, got {output_dictionary['in-pkts']}"
        assert output_dictionary['out-pkts'] >= MIN_PACKET_INCREASE, f"Parent port {parent_port.name} out-pkts should be at least {MIN_PACKET_INCREASE}, got {output_dictionary['out-pkts']}"

    with allure.step("Split port"):
        parent_port.interface.link.set(op_param_name='breakout', op_param_value=IbInterfaceConsts.LINK_BREAKOUT_XDR,
                                       apply=True, ask_for_confirmation=True).verify_result()

    with allure.step("Get child ports"):
        list_of_all_ports = Port.get_list_of_ports()
        child_ports = []
        for port in list_of_all_ports:
            if parent_port.name in port.name and port.name[-2] == 's':
                child_ports.append(port)

    with allure.step("Get baseline counters for child ports"):
        child_baseline_counters = []
        for child_port in child_ports:
            counters = Tools.OutputParsingTool.parse_show_interface_stats_output_to_dictionary(
                child_port.interface.counters.show()).get_returned_value()
            child_baseline_counters.append(counters)
            logger.info(f"Child port {child_port.name} baseline counters: in-pkts={counters['in-pkts']}, out-pkts={counters['out-pkts']}")

    with allure.step("Check traffic port up"):
        # Replace parent port with its child ports in the active ports list
        port_names = []
        for port in active_ports:
            if port.name == parent_port.name:  # If this is the parent port that was split
                port_names.extend([f"{port.name}s1", f"{port.name}s2"])
            else:
                port_names.append(port.name)
        retry_call(validate_list_ports_state, [port_names], exceptions=AssertionError, tries=6, delay=10)

    with allure.step("Run traffic"):
        Tools.TrafficGeneratorTool.send_ib_traffic(players, interfaces, setup_name, True).verify_result()

    with allure.step("Test first child port (s1)"):
        with allure.step("Run traffic and verify counters on s1"):
            Tools.TrafficGeneratorTool.send_ib_traffic(players, interfaces, setup_name, True).verify_result()
            output_dictionary = Tools.OutputParsingTool.parse_show_interface_stats_output_to_dictionary(
                child_ports[0].interface.counters.show()).get_returned_value()
            logger.info(f"Child port s1 ({child_ports[0].name}) counters after traffic: in-pkts={output_dictionary['in-pkts']}, out-pkts={output_dictionary['out-pkts']}")
            assert output_dictionary['in-pkts'] >= child_baseline_counters[0]['in-pkts'] + MIN_PACKET_INCREASE, \
                f"Child port s1 {child_ports[0].name} in-pkts should increase by at least {MIN_PACKET_INCREASE} from baseline {child_baseline_counters[0]['in-pkts']}, got {output_dictionary['in-pkts']}"
            assert output_dictionary['out-pkts'] >= child_baseline_counters[0]['out-pkts'] + MIN_PACKET_INCREASE, \
                f"Child port s1 {child_ports[0].name} out-pkts should increase by at least {MIN_PACKET_INCREASE} from baseline {child_baseline_counters[0]['out-pkts']}, got {output_dictionary['out-pkts']}"

        with allure.step("Set s1 to down"):
            toggle_port_state(child_ports[0], NvosConsts.LINK_STATE_DOWN, devices=devices)

    with allure.step("Test second child port (s2)"):
        with allure.step("Run traffic and verify counters on s2"):
            Tools.TrafficGeneratorTool.send_ib_traffic(players, interfaces, setup_name, True).verify_result()
            output_dictionary = Tools.OutputParsingTool.parse_show_interface_stats_output_to_dictionary(
                child_ports[1].interface.counters.show()).get_returned_value()
            logger.info(f"Child port s2 ({child_ports[1].name}) counters after traffic: in-pkts={output_dictionary['in-pkts']}, out-pkts={output_dictionary['out-pkts']}")
            assert output_dictionary['in-pkts'] >= child_baseline_counters[1]['in-pkts'] + MIN_PACKET_INCREASE, \
                f"Child port s2 {child_ports[1].name} in-pkts should increase by at least {MIN_PACKET_INCREASE} from baseline {child_baseline_counters[1]['in-pkts']}, got {output_dictionary['in-pkts']}"
            assert output_dictionary['out-pkts'] >= child_baseline_counters[1]['out-pkts'] + MIN_PACKET_INCREASE, \
                f"Child port s2 {child_ports[1].name} out-pkts should increase by at least {MIN_PACKET_INCREASE} from baseline {child_baseline_counters[1]['out-pkts']}, got {output_dictionary['out-pkts']}"

    with allure.step("Set s1 back to up"):
        toggle_port_state(child_ports[0], NvosConsts.LINK_STATE_UP, devices=devices)

    with allure.step("Unset parent port"):
        parent_port.interface.link.unset(op_param='breakout', apply=True, ask_for_confirmation=True).verify_result()

    with allure.step("Check traffic port up"):
        port_names = [port.name for port in active_ports]
        retry_call(validate_list_ports_state, [port_names], exceptions=AssertionError, tries=6, delay=10)

    with allure.step("Run traffic"):
        Tools.TrafficGeneratorTool.send_ib_traffic(players, interfaces, setup_name, True).verify_result()


@pytest.mark.ib_interfaces
def test_split_port_timings(engines, interfaces, start_sm, devices):
    """
    Test flow:
        1. Split port
        2. Check if child port will go up for less that 30 sec
        3. Unset split port
    """
    if not devices.dut.split_ports_supported:
        pytest.skip("Split is not supported on this setup")

    system = System()

    with allure.step("Snapshot events table baseline (highest event id before breakout)"):
        baseline_max_event_id = system.events.get_max_event_id()
        logger.info(f"Pre-breakout baseline: max event id = {baseline_max_event_id}, "
                    f"latest event = {system.events.get_last()}")

    with allure.step("Split port"):
        split_ports, active_ports = _get_split_ports()
        parent_port = split_ports[0]
        parent_port.interface.link.set(op_param_name='breakout', op_param_value=IbInterfaceConsts.LINK_BREAKOUT_XDR,
                                       apply=True, ask_for_confirmation=True).verify_result()

    with allure.step("Get alias from redis db for child port"):
        list_of_all_ports = Port.get_list_of_ports()
        child_ports = []
        for port in list_of_all_ports:
            if parent_port.name in port.name and port.name[-2] == 's':
                child_ports.append(port)

    with allure.step("Check if child port will go up for less that 30 sec"):
        child_ports[0].interface.wait_for_port_state(NvosConsts.LINK_STATE_UP, timeout=30).verify_result()

    with allure.step("Verify no unexpected 'Transceiver was inserted' event for child ports after breakout"):
        child_names = {p.name for p in child_ports}
        unexpected_insert_events = system.events.find_events(
            lambda ev: ev.get(EventConsts.RESOURCE) in child_names and
            "Transceiver was inserted" in ev.get(EventConsts.TEXT, ""),
            since_event_id=baseline_max_event_id)
        events_summary = "; ".join(
            f"id={eid} resource={ev.get(EventConsts.RESOURCE)} "
            f"text={ev.get(EventConsts.TEXT)!r} time={ev.get(EventConsts.TIME_CREATED, '')}"
            for eid, ev in unexpected_insert_events)
        assert not unexpected_insert_events, (
            f"Unexpected 'Transceiver was inserted' event(s) recorded for child ports after breakout "
            f"(no physical transceiver action occurred). Events: {events_summary}"
        )

    with allure.step("Unset parent port"):
        parent_port.interface.link.unset(op_param='breakout', apply=True, ask_for_confirmation=True).verify_result()


@pytest.mark.ib_interfaces
def test_split_port_n_times(engines, interfaces, start_sm, devices):
    """
    Test flow:
        1. Split/unsplit port n-times
        2. Check system stable
    """
    if not devices.dut.split_ports_supported:
        pytest.skip("Split is not supported on this setup")

    with allure.step("Split port"):
        split_ports, active_ports = _get_split_ports()
        parent_port = split_ports[0]

    with allure.step("Split/unsplit port n times and check log about that"):
        for _ in range(15):
            parent_port.interface.link.set(op_param_name='breakout',
                                           op_param_value=IbInterfaceConsts.LINK_BREAKOUT_XDR,
                                           apply=True, ask_for_confirmation=True).verify_result()
            parent_port.interface.link.unset(op_param='breakout', apply=True,
                                             ask_for_confirmation=True).verify_result()

    with allure.step("Check if we can do show for splitted interface"):
        Tools.OutputParsingTool.parse_show_all_interfaces_output_to_dictionary(
            parent_port.show_interface()).get_returned_value()


@pytest.mark.ib_interfaces
def test_split_all_ports_together(engines, interfaces, start_sm, devices):
    """
    Test flow:
        1. Get all ib ports
        2. Split it
        3. Check if show command for port work
        4. Get all ports
        5. Unset
        6. Assert 'nv config apply' after unset not rejected (NVBug 5768072)
        7. Assert no empty-IB-speed UMF ERROR during churn (NVBug 6152581 / UMF !191)
    """
    if not devices.dut.split_ports_supported:
        pytest.skip("Split is not supported on this setup")

    marker = TestToolkit.get_loganalyzer_marker(engines.dut)
    umf_marker = umf_churn.place_umf_churn_marker(engines, "split-all-ports-together")

    with allure.step("Get all up and down ports"):
        ports_down_state = Tools.RandomizationTool.select_random_ports(requested_ports_state=NvosConsts.LINK_STATE_DOWN,
                                                                       requested_ports_type="ib",
                                                                       num_of_ports_to_select=0).get_returned_value()
        ports_up_state = Tools.RandomizationTool.select_random_ports(requested_ports_state=NvosConsts.LINK_STATE_UP,
                                                                     requested_ports_type="ib",
                                                                     num_of_ports_to_select=0).get_returned_value()

    with allure.step("Split not connected ports"):
        for port_up in ports_down_state:
            port_up.interface.link.set(op_param_name='breakout', op_param_value=IbInterfaceConsts.LINK_BREAKOUT_XDR)\
                .verify_result()

    with allure.step("Split physical ports"):
        for port_down in ports_up_state:
            port_down.interface.link.set(op_param_name='breakout',
                                         op_param_value=IbInterfaceConsts.LINK_BREAKOUT_XDR).verify_result()
        NvueGeneralCli.apply_config(engines.dut, option='-y')

    with allure.step("Check if we can do show for splitted interface"):
        Tools.OutputParsingTool.parse_show_all_interfaces_output_to_dictionary(
            port_up.show_interface()).get_returned_value()

    with allure.step("Check if we can to get splitted ports"):
        _get_split_ports()

    with allure.step("Unset all ports"):
        NvueSystemCli.unset(TestToolkit.engines.dut, 'interface')
        TestToolkit.add_loganalyzer_marker(engines.dut, marker)
        apply_output = NvueGeneralCli.apply_config(
            engine=TestToolkit.engines.dut, option='--assume-yes'
        )

    with allure.step(
        f"NVBug {NVBUG_5768072}: 'nv config apply' after breakout unset must not be rejected"
    ):
        # 5768072 reproduces as: apply after 'nv unset interface' fails with
        # 'Invalid config [rev_id: N]: lanes NX is not supported' / 'failed
        # to get supported values from STATE_DB' because STATE_DB / lane
        # bookkeeping was not restored during the unset. Framework teardown
        # then recovers via 'nv config replace', so the rejection is silent
        # in the test body unless we assert on the apply output here.
        output_text = apply_output or ""
        hits = []
        if STUCK_SPLIT_LANES_RE.search(output_text):
            hits.append(STUCK_SPLIT_LANES_RE.pattern)
        if STUCK_SPLIT_STATE_DB_MARKER in output_text:
            hits.append(STUCK_SPLIT_STATE_DB_MARKER)
        ResultObj(
            not hits,
            f"NVBug {NVBUG_5768072}: 'nv config apply' after breakout unset "
            f"was rejected. Hits: {hits}. Full output: {output_text!r}",
            issue_type=IssueType.PossibleBug,
        ).verify_result()

    with allure.step(
        f"NVBug {NVBUG_6152581} / UMF !191: no empty-IB-speed ParseFloat ERROR during split churn"
    ):
        umf_churn.assert_no_empty_ib_speed_parse_errors(engines, umf_marker)


@pytest.mark.system_profile_cleanup
@pytest.mark.ib_interfaces
@pytest.mark.timeout(20 * MINUTE, func_only=True)
def test_split_all_ports(engines, interfaces, start_sm, devices):
    """
    Test flow:
        1. Get all ib ports
        2. Split it
        3. Check if show command for port work
        4. Get all ports
        5. Assert ALIAS_PORT_MAP consistency for live split ports (NVBug 6152697 / UMF !195)
        6. Unset
        7. Assert 'nv config apply' after unset not rejected (NVBug 5768072)
        8. Assert no UMF alias Data-index-out-of-range ERROR during churn (NVBug 6152697 / UMF !195)
    """
    if not devices.dut.split_ports_supported:
        pytest.skip("Split is not supported on this setup")

    marker = TestToolkit.get_loganalyzer_marker(engines.dut)
    umf_marker = umf_churn.place_umf_churn_marker(engines, "split-all-ports")

    with allure.step("Get all up and down ports"):
        ports_down_state = Tools.RandomizationTool.select_random_ports(requested_ports_state=NvosConsts.LINK_STATE_DOWN,
                                                                       requested_ports_type="ib",
                                                                       num_of_ports_to_select=0).get_returned_value()
        ports_up_state = Tools.RandomizationTool.select_random_ports(requested_ports_state=NvosConsts.LINK_STATE_UP,
                                                                     requested_ports_type="ib",
                                                                     num_of_ports_to_select=0).get_returned_value()

    with allure.step("Split not connected ports"):
        for port_up in ports_down_state:
            port_up.interface.link.set(op_param_name='breakout', op_param_value=IbInterfaceConsts.LINK_BREAKOUT_XDR,
                                       apply=True, ask_for_confirmation=True).verify_result()
            output_dictionary = Tools.OutputParsingTool.parse_show_interface_pluggable_output_to_dictionary(
                port_up.show_interface(port_names=port_up.name)).get_returned_value()
            Tools.ValidationTool.validate_fields_values_in_output(expected_fields=['link'],
                                                                  expected_values=[{'breakout': '2x-xdr'}],
                                                                  output_dict=output_dictionary).verify_result()

    with allure.step("Split physical ports"):
        for port_down in ports_up_state:
            port_down.interface.link.set(op_param_name='breakout',
                                         op_param_value=IbInterfaceConsts.LINK_BREAKOUT_XDR,
                                         apply=True, ask_for_confirmation=True).verify_result()
            output_dictionary = Tools.OutputParsingTool.parse_show_interface_pluggable_output_to_dictionary(
                port_down.show_interface(port_names=port_down.name)).get_returned_value()
            Tools.ValidationTool.validate_fields_values_in_output(expected_fields=['link'],
                                                                  expected_values=[{'breakout': '2x-xdr'}],
                                                                  output_dict=output_dictionary).verify_result()

    with allure.step("Check if we can do show for splitted interface"):
        Tools.OutputParsingTool.parse_show_all_interfaces_output_to_dictionary(
            port_up.show_interface()).get_returned_value()

    with allure.step("Check if we can to get splitted ports"):
        _get_split_ports()

    with allure.step(
        f"NVBug {NVBUG_6152697} / UMF !195: ALIAS_PORT_MAP consistent for live split ports"
    ):
        live_ib_up = Tools.RandomizationTool.select_random_ports(
            requested_ports_state=NvosConsts.LINK_STATE_UP,
            requested_ports_type="ib",
            num_of_ports_to_select=0,
        ).get_returned_value() or []
        live_names = umf_churn.sample_port_names([p.name for p in live_ib_up])
        if live_names:
            umf_churn.assert_alias_port_map_consistent(engines, live_names)

    with allure.step("Unset all ports"):
        NvueSystemCli.unset(TestToolkit.engines.dut, 'interface')
        TestToolkit.add_loganalyzer_marker(engines.dut, marker)
        apply_output = NvueGeneralCli.apply_config(
            engine=TestToolkit.engines.dut, option='--assume-yes'
        )

    with allure.step(
        f"NVBug {NVBUG_5768072}: 'nv config apply' after breakout unset must not be rejected"
    ):
        # 5768072 reproduces as: apply after 'nv unset interface' fails with
        # 'Invalid config [rev_id: N]: lanes NX is not supported' / 'failed
        # to get supported values from STATE_DB' because STATE_DB / lane
        # bookkeeping was not restored during the unset. Framework teardown
        # then recovers via 'nv config replace', so the rejection is silent
        # in the test body unless we assert on the apply output here.
        output_text = apply_output or ""
        hits = []
        if STUCK_SPLIT_LANES_RE.search(output_text):
            hits.append(STUCK_SPLIT_LANES_RE.pattern)
        if STUCK_SPLIT_STATE_DB_MARKER in output_text:
            hits.append(STUCK_SPLIT_STATE_DB_MARKER)
        ResultObj(
            not hits,
            f"NVBug {NVBUG_5768072}: 'nv config apply' after breakout unset "
            f"was rejected. Hits: {hits}. Full output: {output_text!r}",
            issue_type=IssueType.PossibleBug,
        ).verify_result()

    with allure.step(
        f"NVBug {NVBUG_6152697} / UMF !195: no alias Data-index-out-of-range ERROR during split churn"
    ):
        umf_churn.assert_no_alias_data_index_errors(engines, umf_marker)


@pytest.mark.ib_interfaces
def test_ib_split_port_stress(engines, interfaces, start_sm, devices):
    """
    Test flow:
        1. Stress system
        2. Check that we can split/unsplit
    """
    if not devices.dut.split_ports_supported:
        pytest.skip("Split is not supported on this setup")

    system = System(None)
    with allure.step('Change system profile to breakout'):
        if devices.dut.profile_change_supported:
            system.profile.action_profile_change(params_dict={'adaptive-routing': 'enabled',
                                                              'breakout-mode': 'enabled'})

        with allure.step("Start OpenSM"):
            OpenSmTool.start_open_sm(engines).verify_result()

        with allure.step('Verify changed values'):
            system_profile_output = OutputParsingTool.parse_json_str_to_dictionary(system.profile.show()) \
                .get_returned_value()
            values_to_verify = [SystemConsts.PROFILE_STATE_ENABLED, 1792,
                                SystemConsts.PROFILE_STATE_ENABLED, SystemConsts.PROFILE_STATE_DISABLED,
                                SystemConsts.DEFAULT_NUM_SWIDS]
            ValidationTool.validate_fields_values_in_output(SystemConsts.PROFILE_OUTPUT_FIELDS,
                                                            values_to_verify,
                                                            system_profile_output).verify_result()
            logging.info("All expected values were found")

    logging.info("*********** CLI Stress Test ({}) *********** ".format(engines.dut.ip))
    num_of_iterations = 10

    cmd = 'nv show system version'
    logging.info("Run " + cmd)
    _run_cmd_nvue(engines, [cmd], num_of_iterations)
    logging.info(cmd + " succeeded -----------------------------------")

    cmd = 'nv show interface eth0 link'
    logging.info("Run " + cmd)
    _run_cmd_nvue(engines, [cmd], num_of_iterations)
    logging.info(cmd + " succeeded -----------------------------------")

    cmds = ['nv show platform firmware', 'nv show interface eth0 link']
    logging.info("Run " + cmds[0] + " and " + cmds[1])
    _run_cmd_nvue(engines, cmds, num_of_iterations)
    logging.info(cmds[0] + "and" + cmds[1] + " succeeded -----------------------------------")

    with allure.step("Check traffic port up"):
        split_ports, active_ports = _get_split_ports()
        port_names = [port.name for port in active_ports]
        retry_call(validate_list_ports_state, [port_names], exceptions=AssertionError, tries=6, delay=10)

    with allure.step('Check that we can split/unsplit port during stress test'):
        with allure.step("Split splitter port"):
            parent_port = split_ports[0]
            parent_port.interface.link.set(op_param_name='breakout',
                                           op_param_value=IbInterfaceConsts.LINK_BREAKOUT_XDR,
                                           apply=True, ask_for_confirmation=True).verify_result()
        with allure.step("Unset parent port"):
            parent_port.interface.link.unset(op_param='breakout', apply=True,
                                             ask_for_confirmation=True).verify_result()


@pytest.mark.ib_interfaces
@pytest.mark.disable_loganalyzer
def test_split_port_redis_db_crash(engines, interfaces, start_sm, devices):
    """
    Test flow:
        1. Write to config db
        2. Check changes
        3. Check no system not crashed
    """
    if not devices.dut.split_ports_supported:
        pytest.skip("Split is not supported on this setup")

    system = System(None)
    with allure.step("Check traffic port up"):
        split_ports, active_ports = _get_split_ports()
        port_names = [port.name for port in active_ports]
        retry_call(validate_list_ports_state, [port_names], exceptions=AssertionError, tries=6, delay=10)

    with allure.step("Split splitter port"):
        parent_port = split_ports[0]
        parent_port.interface.link.set(op_param_name='breakout', op_param_value=IbInterfaceConsts.LINK_BREAKOUT_XDR,
                                       apply=True, ask_for_confirmation=True).verify_result()

    with allure.step("Get alias from redis db for child port"):
        list_of_all_ports = Port.get_list_of_ports()
        child_ports = []
        for port in list_of_all_ports:
            if parent_port.name in port.name and port.name[-2] == 's':
                child_ports.append(port)

        alias = Tools.DatabaseTool.sonic_db_cli_hget(engine=engines.dut, asic="", db_name=DatabaseConst.APPL_DB_NAME,
                                                     db_config="ALIAS_PORT_MAP:{}".format(child_ports[0].name),
                                                     param="name")

    with allure.step("Set mtu value through redis cli on a child port and validate"):
        random_mtu = random.randrange(256, 4096)
        redis_cli_output = Tools.DatabaseTool.sonic_db_cli_hget(engine=engines.dut, asic="",
                                                                db_name=DatabaseConst.CONFIG_DB_NAME,
                                                                db_config="IB_PORT\\|{0}".format(alias),
                                                                param="mtu {}".format(str(random_mtu)))

        assert redis_cli_output != 0, "Redis command failed"
        Tools.OutputParsingTool.parse_show_interface_link_output_to_dictionary(
            child_ports[0].interface.link.show()).get_returned_value()

    with allure.step("Unset parent port"):
        parent_port.interface.link.unset(op_param='breakout', apply=True, ask_for_confirmation=True).verify_result()

    if devices.dut.profile_change_supported:
        with allure.step('Change system profile to default'):
            system.profile.action_profile_change(
                params_dict={'adaptive-routing': 'enabled', 'adaptive-routing-groups': '2048', 'breakout-mode': 'disabled'})
            system_profile_output = OutputParsingTool.parse_json_str_to_dictionary(system.profile.show()) \
                .get_returned_value()
            ValidationTool.validate_fields_values_in_output(SystemConsts.PROFILE_OUTPUT_FIELDS,
                                                            devices.dut.system_profile_default_values,
                                                            system_profile_output).verify_result()
            logging.info("All values returned successfully")


def _run_cmd_nvue(engines, cmds_to_run, num_of_iterations):
    with allure.step("Run commands for {} iterations".format(num_of_iterations)):
        i = num_of_iterations
        try:
            for i in range(0, num_of_iterations):
                for cmd in cmds_to_run:
                    logging.info("Run {} iterations of {}".format(cmd, i))
                    output = engines.dut.run_cmd(cmd)
                    if any(msg in output for msg in invalid_cmd_str):
                        raise Exception("FAILED - " + output)
                i -= 1
        except BaseException as ex:
            raise Exception("Failed during iteration #{}: {}".format(i, str(ex)))


@retry(Exception, tries=4, delay=2)
def _get_split_ports():
    active_ports = Tools.RandomizationTool.get_random_active_port(0).get_returned_value()
    split_ports = []
    split_port_names = ["swA10p1", "swB10p1", "swA15p1", "swB15p1"]
    for port in active_ports:
        if port.name in split_port_names:
            split_ports.append(port)
    if not split_ports and not active_ports:
        raise Exception("Can't find any active port to split")
    return split_ports, active_ports


def validate_list_ports_state(expected_ports: list, state=NvosConsts.LINK_STATE_UP):
    port_requirements = PortRequirements()
    port_requirements.set_port_state(state)
    actual_ports = [port.name for port in Port.get_list_of_ports(port_requirements_object=port_requirements)]

    ValidationTool.validate_subset_in_superset(expected_ports, actual_ports).verify_result()


def toggle_port_state(selected_port, port_state, test_name='', devices=None):
    selected_port.interface.link.state.set(op_param_name=port_state, apply=True, ask_for_confirmation=True).verify_result()
    with allure.step("Wait till port {} is {}".format(selected_port, port_state)):
        res_obj, duration = OperationTime.save_duration('port goes {}'.format(port_state), '', test_name,
                                                        selected_port.interface.wait_for_port_state, port_state,
                                                        sleep_time=0.2)
        res_obj.verify_result()
        operation = 'port goes {}'.format(port_state)
        OperationTime.verify_operation_time(duration, operation, devices).verify_result()
