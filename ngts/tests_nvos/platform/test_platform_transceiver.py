import logging
import random
import re
import time

import pytest
from retry import retry

from ngts.constants.constants import GnmiConsts
from ngts.nvos_constants.constants_nvos import ApiType, PlatformConsts
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import NvosConsts
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port
from ngts.nvos_tools.infra.IbInterfaceTool import IbInterfaceTool
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.Tools import Tools
from ngts.nvos_tools.infra.TransceiverSysfsTool import TransceiverSysfsTool
from ngts.nvos_tools.platform.Platform import Platform
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.helpers.redmine_helpers import is_bug_active
from ngts.tests_nvos.platform.constants import TransceiversConsts
from ngts.tests_nvos.interfaces.test_ib_interface_configuration import wait_for_port_to_become_active
from ngts.tests_nvos.platform.helpers import _pre_port_config, _post_port_config
from ngts.tests_nvos.system.gnmi.GnmiClient import GnmiClient
from ngts.tests_nvos.system.gnmi.constants import GnmiMode, GnmiConstants, GnmicErr
from ngts.tests_nvos.system.gnmi.helpers import _is_gnmi_unavailable, is_gnmi_failure, verify_msg_not_in_out_or_err
from ngts.tests_nvos.system.gnmi.mapping.helpers import parse_gnmic_flat_path
from ngts.tools.test_utils.nvos_general_utils import loganalyzer_ignore
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger()


MODULE_STATUS_DICT = {"Inserted": {"N/A", "Power budget exceeded", "Long range for non - Mellanox cable or module",
                                   "Bit I2C stuck", "Unsupported cable", "High temperature", "Enforce part number list",
                                   "Bad EEPROM", "Bad cable", "PMD type not enabled",
                                   "PCIE system power slot exceeded"},
                      "Removed": {"N/A"}}


@pytest.mark.platform
@pytest.mark.transceiver
def test_transceiver_status(engines, random_api):
    """
    The test will check default field and values for transceiver module_status and error.

    flow:
    1. Check module and error_status for plugged module
    2. Check module and error_status for unplugged module
    3. Validate gNMI physical-channels field-set for both module types
    """

    with allure.step("Create platform object"):
        platform = Platform()

    plugged_module = _get_module_with_status(platform, PlatformConsts.INSERTED)
    unplugged_module = _get_module_with_status(platform, PlatformConsts.REMOVED)
    up_ports = _get_ports_for_module(plugged_module)
    down_ports = _get_ports_for_module(unplugged_module)

    _verify_link_state_up(up_ports)
    _verify_transceiver_status(platform, transceiver_id=plugged_module, expected_module_status=PlatformConsts.INSERTED)

    _verify_transceiver_status(platform, transceiver_id=unplugged_module, expected_module_status=PlatformConsts.REMOVED)
    _verify_link_state_down(down_ports)

    with allure.step(f"supported-cable-ib-speed: present + well-formed on {plugged_module}, "
                     f"absent on {unplugged_module}"):
        plugged_output = _show_transceiver_json(plugged_module)
        assert _IB_SPEED_FIELD in plugged_output, \
            f"{_IB_SPEED_FIELD} missing on cabled port {plugged_module}, keys: {sorted(plugged_output)}"
        _assert_supported_cable_ib_speed_wellformed(plugged_output[_IB_SPEED_FIELD].strip())
        _assert_supported_cable_ib_speed_matches_module(plugged_output, plugged_module)

        unplugged_output = _show_transceiver_json(unplugged_module)
        assert _IB_SPEED_FIELD not in unplugged_output, \
            f"{_IB_SPEED_FIELD} must be absent on empty port {unplugged_module}, " \
            f"got: {unplugged_output.get(_IB_SPEED_FIELD)!r}"

    with allure.step('gNMI validation - physical-channels telemetry field-set'):
        gnmi_client = GnmiClient(
            engines.dut.ip, GnmiConsts.GNMI_DEFAULT_PORT,
            engines.dut.username, engines.dut.password,
            cmd_time=60, verify_tools_installed=True,
        )
        _, err = gnmi_client.gnmic_capabilities(skip_cert_verify=True)
        if _is_gnmi_unavailable(err):
            pytest.skip(f"gNMI server is unavailable on {engines.dut.ip}:{GnmiConsts.GNMI_DEFAULT_PORT}: {err.strip()}")
        validate_gnmi_transceiver_physical_channel_fields(gnmi_client, plugged_module, PlatformConsts.INSERTED)
        validate_gnmi_transceiver_physical_channel_fields(gnmi_client, unplugged_module, PlatformConsts.REMOVED)


@pytest.mark.check_log_size
@pytest.mark.check_disk_usage
@pytest.mark.platform
@pytest.mark.transceiver
def test_transceiver_status_unplug(engines, devices, random_api):
    """
    The test will check if the module_status changes to Removed after simulating unplug event.

    flow:
    1. Verify module is plugged
    2. Unplug selected module
    3. Check module and error_status for unplugged module
    4. Plug module back
    """

    platform = Platform()
    desired_state = NvosConsts.LINK_STATE_UP

    with allure.step(f"Get module with state {desired_state}"):
        module_under_test, ports, mst_dev_name, module_index = _get_module_with_desire_state(
            engines, devices, platform, desired_state)

    baseline_output = _show_transceiver_json(module_under_test)
    baseline_ib_speed = baseline_output.get(_IB_SPEED_FIELD, '').strip() if _IB_SPEED_FIELD in baseline_output else None
    if baseline_ib_speed is not None:
        _assert_supported_cable_ib_speed_wellformed(baseline_ib_speed)
        logger.info(f"Baseline {_IB_SPEED_FIELD}={baseline_ib_speed!r} on {module_under_test}")

    try:
        _verify_transceiver_status(platform, transceiver_id=module_under_test, expected_module_status='Inserted')
        show_ports_output = _pre_port_config(ports)
        IbInterfaceTool.simulate_unplug_module_event(engines.dut, devices.dut, module_index, mst_dev_name, 8)
        _verify_link_state_down(ports)
        _verify_transceiver_status(platform, transceiver_id=module_under_test, expected_module_status='Removed')

        with allure.step(f"supported-cable-ib-speed absent while {module_under_test} is unplugged"):
            unplugged_out = _show_transceiver_json(module_under_test)
            assert _IB_SPEED_FIELD not in unplugged_out, \
                f"{_IB_SPEED_FIELD} should be absent after unplug, " \
                f"got: {unplugged_out.get(_IB_SPEED_FIELD)!r}"
        # read eeprom values and config interface run show fae interface before and after unplug/plug .. add configurations (think about it .. at least delayed recovery)

    finally:
        IbInterfaceTool.simulate_plugin_module_event(engines.dut, devices.dut, module_index, mst_dev_name, 50)
        _verify_link_state_up(ports)
        wait_for_port_to_become_active(ports[0])
        _post_port_config(show_ports_output, ports)
        _verify_link_state_up(ports)
        _verify_transceiver_status(platform, transceiver_id=module_under_test, expected_module_status='Inserted')

    if baseline_ib_speed is not None:
        with allure.step(f"supported-cable-ib-speed restored to baseline {baseline_ib_speed!r} after re-plug"):
            post_plug = _show_transceiver_json(module_under_test)
            assert _IB_SPEED_FIELD in post_plug, \
                f"{_IB_SPEED_FIELD} did not reappear after re-plug on {module_under_test}"
            assert post_plug[_IB_SPEED_FIELD].strip() == baseline_ib_speed, \
                f"{_IB_SPEED_FIELD} changed across plug cycle: " \
                f"baseline={baseline_ib_speed!r} after={post_plug[_IB_SPEED_FIELD]!r}"


@pytest.mark.check_log_size
@pytest.mark.check_disk_usage
@pytest.mark.platform
@pytest.mark.transceiver
def test_transceiver_status_with_reboot(engines, devices, random_api):
    """
    The test will check if the value of module_status is reset after reboot.

    flow:
    1. Verify module is plugged
    2. Unplug selected module
    3. Check module and error_status for unplugged module
    4. Reboot the system
    5. Verify module is plugged
    """

    with allure.step("Create System and platform object"):
        platform = Platform()
        system = System()

    desired_state = NvosConsts.LINK_STATE_UP
    with allure.step(f"Get module with state {desired_state}"):
        module_under_test, ports, mst_dev_name, module_index = _get_module_with_desire_state(
            engines, devices, platform, desired_state)

    try:
        _verify_transceiver_status(platform, transceiver_id=module_under_test, expected_module_status='Inserted')

        IbInterfaceTool.simulate_unplug_module_event(engines.dut, devices.dut, module_index, mst_dev_name, 8)
        _verify_link_state_down(ports)
        _verify_transceiver_status(platform, transceiver_id=module_under_test, expected_module_status='Removed')

        sleep_time_seconds = 60
        with allure.step(f"Reboot the system and sleep {sleep_time_seconds} seconds"):
            system.reboot.action_reboot(engine=engines.dut, device=devices.dut)
            time.sleep(sleep_time_seconds)

        _verify_transceiver_status(platform, transceiver_id=module_under_test, expected_module_status='Inserted')

    finally:
        IbInterfaceTool.simulate_plugin_module_event(engines.dut, devices.dut, module_index, mst_dev_name, 50)
        _verify_link_state_up(ports)
        _verify_transceiver_status(platform, transceiver_id=module_under_test, expected_module_status='Inserted')


# ---------------------------------------------------------------------------
# supported-cable-ib-speed leaf (Redmine #4660953)
# TP: /auto/sysgwork/itkoren/test_plans/transceiver_supported_ib_speed_TP.md
# ---------------------------------------------------------------------------

_IB_SPEED_FIELD = PlatformConsts.TRANSCEIVER_SUPPORTED_CABLE_IB_SPEED
_IB_SPEED_NAMES = PlatformConsts.TRANSCEIVER_SUPPORTED_CABLE_IB_SPEED_NAMES
_IB_SPEED_TOKEN_RE = re.compile(
    r'^(' + '|'.join(_IB_SPEED_NAMES) + r')(,(' + '|'.join(_IB_SPEED_NAMES) + r'))*$')


def _show_transceiver_json(module_name):
    return OutputParsingTool.parse_json_str_to_dictionary(
        Platform().transceiver.show(module_name)).get_returned_value()


def _assert_supported_cable_ib_speed_matches_module(transceiver_output, module_name):
    """When the tested transceiver's vendor-pn is a known module family (per
    PlatformConsts.TRANSCEIVER_IB_SPEED_RULES_BY_PN), assert:
      - every required speed for that family appears in the observed tokens, and
      - no forbidden speed appears (catches FW spurious-bit bugs like the
        Wolverine2-Arcus2 fdr,xdr regression, SW #6453983 / Redmine #4660953).
    Unlisted vendor-pns fall back to the well-formed structure check only."""
    pn = transceiver_output.get(PlatformConsts.TRANSCEIVER_VENDOR_PN, '').strip()
    rules = PlatformConsts.TRANSCEIVER_IB_SPEED_RULES_BY_PN.get(pn)
    if rules is None:
        logger.info(f"{module_name}: vendor-pn={pn!r} not in ib-speed rules map, "
                    f"well-formed structure check only")
        return
    actual_tokens = set(transceiver_output[_IB_SPEED_FIELD].strip().split(','))
    missing = rules['required'] - actual_tokens
    spurious = rules['forbidden'] & actual_tokens
    problems = []
    if missing:
        problems.append(f"missing required speed(s) {sorted(missing)!r}")
    if spurious:
        problems.append(f"unexpected forbidden speed(s) {sorted(spurious)!r}")
    assert not problems, \
        f"{_IB_SPEED_FIELD}={sorted(actual_tokens)!r} on {module_name} " \
        f"(vendor-pn={pn!r}) violates rules for this module family: " + "; ".join(problems)


def _assert_supported_cable_ib_speed_wellformed(value):
    """Non-empty, non-N/A, comma-joined known IB-speed tokens in ascending order, no dups."""
    na = PlatformConsts.TRANSCEIVER_SUPPORTED_CABLE_IB_SPEED_NA
    assert value and value != na, f"{_IB_SPEED_FIELD} unexpectedly '{value}' for a cabled port"
    assert _IB_SPEED_TOKEN_RE.match(value), \
        f"{_IB_SPEED_FIELD}='{value}' is not a comma-joined list of {_IB_SPEED_NAMES}"
    tokens = value.split(',')
    order = [_IB_SPEED_NAMES.index(t) for t in tokens]
    assert order == sorted(order), f"{_IB_SPEED_FIELD}='{value}' tokens not in ascending order"
    assert len(tokens) == len(set(tokens)), f"{_IB_SPEED_FIELD}='{value}' contains duplicates"


@pytest.mark.platform
@pytest.mark.transceiver
def test_transceiver_supported_ib_speed_na_and_error_paths(engines):
    """TP §6.1 - defensive decode paths driven against the production SfpSysfsParser
    inside pmon (crafted supported_cable_speed values), plus /var/log/syslog check
    for the expected WARNING(s). Uses TransceiverSysfsTool so the pmon wiring is
    reusable and off the test's main path.

    Note: the crafted inputs are invalid on purpose, so pmon is *expected* to log
    WARNINGs about them - the test asserts they appear. They are not a failure
    signal, so the injection is wrapped in loganalyzer_ignore() to keep
    loganalyzer from flagging the test's own deliberate noise.
    """
    with allure.step("Empty cage -> supported-cable-ib-speed leaf absent (NVUE surface)"):
        empty_module = _get_module_with_status(Platform(), PlatformConsts.REMOVED)
        empty_output = _show_transceiver_json(empty_module)
        assert _IB_SPEED_FIELD not in empty_output, \
            f"{_IB_SPEED_FIELD} must be absent for empty port {empty_module}, " \
            f"got: {empty_output.get(_IB_SPEED_FIELD)!r}"

    with allure.step("Mark syslog with a sentinel for warning check"):
        marker = TransceiverSysfsTool.mark_syslog(engines.dut)

    with allure.step("Drive SfpSysfsParser.get_supported_cable_ib_speed inside pmon"):
        # Feeding unparseable / unknown-bit values makes pmon log WARNINGs by design.
        # Ignore them here so loganalyzer does not fail the test on noise it asked for;
        # the "Verify ... WARNING(s) logged" step below still asserts they were emitted.
        with loganalyzer_ignore():
            results = TransceiverSysfsTool.drive_supported_cable_ib_speed_decoder(engines.dut)
        logger.info("Defensive-decode results:")
        for r in results:
            logger.info(f"  input={r['input']!r:>16}  expected={r['expected']!r:<10}  "
                        f"got={r['got']!r:<10}  # {r['desc']}")

    with allure.step("Assert every defensive case returned the expected value"):
        failures = []
        for r in results:
            ok = r["got"] == r["expected"]
            step_name = (f"[{'PASS' if ok else 'FAIL'}] input={r['input']!r} "
                         f"-> got={r['got']!r} (expected {r['expected']!r}) - {r['desc']}")
            with allure.independent_step(step_name):
                if not ok:
                    failures.append(r)
                    logger.error(step_name)
        assert not failures, "Defensive decode mismatches:\n" + "\n".join(
            f"  input={r['input']!r} expected={r['expected']!r} got={r['got']!r} ({r['desc']})"
            for r in failures)

    with allure.step("Verify xcvrd/platform WARNING(s) logged for the bad values"):
        warnings = TransceiverSysfsTool.grep_supported_cable_speed_warnings(engines.dut, marker)
        logger.info(f"Found {len(warnings)} post-marker warning line(s) in /var/log/syslog:")
        for line in warnings:
            with allure.independent_step(f"syslog: {line}"):
                logger.info(line)
        assert warnings, (
            "Expected at least one WARNING about supported_cable_speed after driving bad values "
            f"(marker={marker!r})")


@pytest.mark.platform
@pytest.mark.transceiver
def test_transceivers_and_ports(engines, devices, nv_command, random_api):
    """
    The test verifies all expected modules (by device) exists in transceivers detail output.

    flow:
    1. Verify all expected modules exists in transceivers detail output
    2. Verify connected transceivers count matches system status
    """

    transceivers_list = devices.dut.transceiver_list

    with allure.step("Verify all connected transceivers and ports"):
        connected_transceivers_output = OutputParsingTool.parse_json_str_to_dictionary(
            nv_command.platform.transceiver.show()).get_returned_value()
        nv_connected_transceivers = set(connected_transceivers_output.keys())

        with allure.independent_step("Verify all expected transceiver modules are detected"):
            transceivers_output = [name for name, transceiver in OutputParsingTool.parse_json_str_to_dictionary(
                nv_command.platform.transceiver.show('detail')).get_returned_value().items()]
            for transceiver in transceivers_list:
                assert transceiver in transceivers_output, f"{transceiver} is missing in transceivers output"

        with allure.independent_step("Verify number of connected transceivers matches system status"):
            system_transceiver_status = IbInterfaceTool.get_connected_transceivers_dict(engines.dut, transceivers_list)

            system_connected_transceivers = {transceiver for transceiver, is_connected in system_transceiver_status.items() if is_connected}

            Tools.ValidationTool.validate_set_equal(actual=system_connected_transceivers, expected=nv_connected_transceivers).verify_result()

        with allure.independent_step("Verify ports are UP for all connected cables"):
            selected_up_ports = Tools.RandomizationTool.select_random_ports(
                requested_ports_state=NvosConsts.LINK_STATE_UP, requested_ports_type=devices.dut.switch_type.lower(),
                num_of_ports_to_select=0).get_returned_value()

            ports = [port.name for port in selected_up_ports]

            find_missing_ports(nv_connected_transceivers, ports)


def find_missing_ports(connected_transceivers, ports):
    """
    Behavior:
        - A transceiver is included in the result ONLY if BOTH "swp1" and "swp2" are down.
        - transceivers with at least one of the ports (p1 or p2) is up.
        - We do not validate FNM ports (transceivers whose ID starts with "fnm" are ignored).

    :param connected_transceivers:
    :param ports:
    :return:
    """
    missing = {}
    for dev in connected_transceivers:
        if "fnm" in dev:
            continue

        expected_p1 = f"{dev}p1"
        expected_p2 = f"{dev}p2"

        has_p1 = expected_p1 in ports
        has_p2 = expected_p2 in ports

        # report only if both are missing
        if not has_p1 and not has_p2:
            missing[dev] = ["p1", "p2"]

    assert not missing, f"Missing per device: {missing}"


def _verify_transceiver_status(platform, transceiver_id, expected_module_status='Inserted',
                               expected_error_status='N/A'):
    with allure.step(f"Check transceiver {transceiver_id} status - expected"):
        with allure.step("Check status and error-status exists in nv show platform transceiver"):
            transceiver_output = OutputParsingTool.parse_json_str_to_dictionary(
                platform.transceiver.show(transceiver_id)).get_returned_value()
            fields_to_check = [PlatformConsts.TRANSCEIVER_STATUS, PlatformConsts.TRANSCEIVER_ERROR_STATUS]
            Tools.ValidationTool.verify_field_exist_in_json_output(transceiver_output, fields_to_check). \
                verify_result()

        with allure.step(f"Check {PlatformConsts.TRANSCEIVER_STATUS} has correct value {expected_module_status}"):
            Tools.ValidationTool.verify_field_value_in_output(output_dictionary=transceiver_output,
                                                              field_name=PlatformConsts.TRANSCEIVER_STATUS,
                                                              expected_value=expected_module_status).verify_result()

        with allure.step("Verify error status exists"):
            module_status = transceiver_output[PlatformConsts.TRANSCEIVER_STATUS].strip()
            error_status = transceiver_output[PlatformConsts.TRANSCEIVER_ERROR_STATUS].strip()
            assert error_status in MODULE_STATUS_DICT[
                module_status], f"module-error-status is in not allowed state: {error_status}"

        with allure.step(f"Check {PlatformConsts.TRANSCEIVER_ERROR_STATUS} has correct value {expected_error_status}"):
            if not is_bug_active(4323183):
                Tools.ValidationTool.verify_field_value_in_output(output_dictionary=transceiver_output,
                                                                  field_name=PlatformConsts.TRANSCEIVER_ERROR_STATUS,
                                                                  expected_value=expected_error_status).verify_result()


def _verify_transceiver_fields(platform, transceiver_id, expected_module_status='Inserted',
                               expected_error_status='N/A'):
    with allure.step("Check all fields in nv show platform transceiver <transceiver>"):
        transceiver_output = OutputParsingTool.parse_json_str_to_dictionary(
            platform.transceiver.show(transceiver_id)).get_returned_value()
        fields_to_check = [PlatformConsts.TRANSCEIVER_STATUS, PlatformConsts.TRANSCEIVER_ERROR_STATUS]
        Tools.ValidationTool.verify_field_exist_in_json_output(transceiver_output, fields_to_check). \
            verify_result()

    with allure.step(f"Check {PlatformConsts.TRANSCEIVER_STATUS} has correct value {expected_module_status}"):
        Tools.ValidationTool.verify_field_value_in_output(output_dictionary=transceiver_output,
                                                          field_name=PlatformConsts.TRANSCEIVER_STATUS,
                                                          expected_value=expected_module_status).verify_result()

    with allure.step("Verify error status exists"):
        module_status = transceiver_output[PlatformConsts.TRANSCEIVER_STATUS].strip()
        error_status = transceiver_output[PlatformConsts.TRANSCEIVER_ERROR_STATUS].strip()
        assert error_status in MODULE_STATUS_DICT[
            module_status], f"module-error-status is in not allowed state: {error_status}"

    with allure.step(f"Check {PlatformConsts.TRANSCEIVER_ERROR_STATUS} has correct value {expected_error_status}"):
        if not is_bug_active(4323183):
            Tools.ValidationTool.verify_field_value_in_output(output_dictionary=transceiver_output,
                                                              field_name=PlatformConsts.TRANSCEIVER_ERROR_STATUS,
                                                              expected_value=expected_error_status).verify_result()


def validate_gnmi_transceiver_physical_channel_fields(
    client, transceiver_id, module_status, expected_field_set=None,
):
    """Assert gnmic returns exactly the expected field-set under
    `components/component[name=<id>]/transceiver/physical-channels/channel[index=1]/`
    (no more, no fewer); names only, values are not checked.

    Inserted modules must return the full leaf-set; Removed modules must return nothing.

    `expected_field_set` lets callers override the Inserted expectation per module type
    (e.g. ELS vs OE vs SW physical-channels surface differs). When None, falls back to
    `GnmiConstants.EXPECTED_TRANSCEIVER_PHYSICAL_CHANNEL_FIELDS` (the SW set), preserving
    existing caller behavior. The Removed branch is unaffected by this argument.
    """
    if module_status == PlatformConsts.INSERTED:
        expected = (expected_field_set
                    if expected_field_set is not None
                    else GnmiConstants.EXPECTED_TRANSCEIVER_PHYSICAL_CHANNEL_FIELDS)
    else:
        expected = set()
    with allure.independent_step(f'Validate gNMI physical-channels field-set for {transceiver_id} ({module_status})'):
        container_path = (
            f'components/component[name={transceiver_id}]/transceiver/physical-channels/channel[index=1]'
        ).strip('/')
        out, err, _, _ = client.gnmic_subscribe(
            prefix='',
            path=container_path,
            mode=GnmiMode.ONCE,
            flat=True,
            skip_cert_verify=True,
            cmd_time=30,
            wait_till_done=False,
        )
        verify_msg_not_in_out_or_err(GnmicErr.AUTH_FAIL, out, err)
        assert not is_gnmi_failure(err), (
            f"gnmic subscribe failed for transceiver {transceiver_id} ({module_status}):\n"
            f"  stderr: {err}\n"
            f"  stdout: {out}"
        )
        match_prefix = container_path + '/'
        actual = set()
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            full_path = parse_gnmic_flat_path(line)
            if not full_path:
                continue
            full_path = full_path.lstrip('/')
            if full_path.startswith(match_prefix):
                actual.add(full_path[len(match_prefix):])
        Tools.ValidationTool.validate_set_equal(actual=actual, expected=expected).verify_result()


@retry(Exception, tries=15, delay=1)
def _verify_link_state_up(up_ports):
    with allure.step("Verify link for any of the up ports is up"):
        link_states = [
            OutputParsingTool.parse_json_str_to_dictionary(port.interface.link.state.show()).get_returned_value()
            for port in up_ports
        ]
        port_names = [port.name for port in up_ports]
        with allure.step(f"Verify link for any of the {port_names} is {NvosConsts.LINK_STATE_UP}"):
            for link_state in link_states:
                if not link_state:
                    assert False, "Link state is empty should be up or down"
                # At least one of the ports should be up for inserted transceiver.
                if NvosConsts.LINK_STATE_UP in link_state:
                    return
            assert False, f"None of the ports are {NvosConsts.LINK_STATE_UP}"


def _verify_link_state_down(down_ports):
    with allure.step("Verify all down ports are down"):
        link_states = [
            OutputParsingTool.parse_json_str_to_dictionary(port.interface.link.state.show()).get_returned_value()
            for port in down_ports
        ]
        port_names = [port.name for port in down_ports]
        with allure.step(f"Verify all {port_names} are down"):
            for link_state in link_states:
                if not link_state:
                    assert False, "Link state is empty should be up or down"
                if NvosConsts.LINK_STATE_DOWN not in link_state:
                    assert False, "The link state is up for removed transceiver"


def _get_module_with_status(platform, status):
    with allure.step(f"Find {status} module"):
        detail = ""
        if TestToolkit.tested_api == ApiType.NVUE:
            detail = "detail"
        transceivers = [name for name, transceiver in
                        OutputParsingTool.parse_json_str_to_dictionary(
                            platform.transceiver.show(detail)).get_returned_value().items() if
                        ((TransceiversConsts.TRANSCEIVERS_SW in name) or (TransceiversConsts.TRANSCEIVERS_ELS in name)) and
                        transceiver[PlatformConsts.TRANSCEIVER_STATUS] == status]
        if not transceivers:
            pytest.skip(f"No {status} transceivers found for setup")
        return random.choice(transceivers)


def _get_ports_for_module(module_name):
    with allure.step(f"Get ports for module {module_name}"):
        ports = Port.get_list_of_ports()
        if TransceiversConsts.TRANSCEIVERS_ELS in module_name:
            platform = Platform()
            port_mapping = OutputParsingTool.parse_json_str_to_dictionary(
                platform.transceiver.show(module_name)).get_returned_value()['port-mapping'].keys()
            ports_for_module = [port for port in ports if port.name in port_mapping]
        else:
            ports_for_module = [port for port in ports if f"{module_name}p" in port.name]
        return ports_for_module


def _count_module_index(module_name, device):
    module_index = int(''.join(c for c in module_name if c.isdigit()))
    if TransceiversConsts.TRANSCEIVERS_ELS in module_name:
        module_index += 71  # [els1,...,els18] -> [module_index 72,...,module_index 89]
    else:
        module_index -= 1
        if device.module_offset:
            offset = device.module_offset
            module_index %= offset
    return module_index


def _get_module_with_desire_state(engines, devices, platform, desired_state):
    with allure.step(f"Get module with state {desired_state}"):
        module_under_test = _get_module_with_status(platform, PlatformConsts.INSERTED)
        assert module_under_test, f"No module with state {desired_state} found"
        ports = _get_ports_for_module(module_under_test)
        assert ports, "Should be at least one port for module"
        mst_dev_name = IbInterfaceTool.get_mst_dev_name(engine=engines.dut, module_name=module_under_test,
                                                        port_name=ports[0].name)
        module_index = _count_module_index(module_under_test, devices.dut)

    return module_under_test, ports, mst_dev_name, module_index
