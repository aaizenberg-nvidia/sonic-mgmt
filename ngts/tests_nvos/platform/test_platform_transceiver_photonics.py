"""Transceiver photonics tests for Taipan CPO systems (ELS/OE modules)."""

import logging
import random

import pytest

from ngts.constants.constants import GnmiConsts
from ngts.nvos_constants.constants_nvos import ApiType, PlatformConsts
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.PhotonicsTool import PhotonicsTool
from ngts.nvos_tools.infra.RegressionConfigurations import Configurations
from ngts.nvos_tools.infra.Tools import Tools
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.nvos_tools.platform.Platform import Platform
from ngts.tests_nvos.platform.constants import TransceiversConsts
from ngts.tests_nvos.platform.test_platform_transceiver import (
    validate_gnmi_transceiver_physical_channel_fields,
)
from ngts.tests_nvos.system.gnmi.constants import GnmiConstants
from ngts.tests_nvos.system.gnmi.GnmiClient import GnmiClient
from ngts.tests_nvos.system.gnmi.helpers import _is_gnmi_unavailable
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger()


def _build_gnmi_client_or_warn(engines):
    """Best-effort GnmiClient, built once and reused by the caller.

    Returns None and attaches an Allure warning on any failure (construction
    or an unreachable gNMI server); never skips or fails the test, so the
    photonics NVUE assertions stay decoupled from gNMI server health.
    """
    try:
        client = GnmiClient(
            engines.dut.ip, GnmiConsts.GNMI_DEFAULT_PORT,
            engines.dut.username, engines.dut.password,
            cmd_time=60, verify_tools_installed=True,
        )
        _, err = client.gnmic_capabilities(skip_cert_verify=True)
        if _is_gnmi_unavailable(err):
            allure.attach('gNMI unavailable WARNING',
                          f"gNMI server unavailable on {engines.dut.ip}: {err.strip()}")
            return None
        return client
    except Exception as exc:
        allure.attach('gNMI client WARNING',
                      f"GnmiClient construction failed: {exc}")
        return None


def _get_transceiver_lists(transceivers: list[str]) -> tuple[list[str], list[str]]:
    """Extract ELS and OE transceiver lists from full transceiver list."""
    els = [t for t in transceivers if TransceiversConsts.TRANSCEIVERS_ELS in t]
    oe = [t for t in transceivers if TransceiversConsts.TRANSCEIVERS_OE in t]
    return els, oe


def _parse_threshold(threshold_str: str) -> float:
    """Parse threshold value from string (e.g., '80.00 C' -> 80.0, '4.00 dBm' -> 4.0)."""
    return float(threshold_str.split()[0])


def _validate_against_thresholds(value: float, data: dict, name: str,
                                 high_key: str = 'high-alarm-threshold',
                                 low_key: str = 'low-alarm-threshold') -> None:
    """Validate value against dynamic thresholds from output."""
    if high_key in data:
        high_threshold = _parse_threshold(data[high_key])
        assert value <= high_threshold, f"{name}: {value} exceeds high-alarm-threshold {high_threshold}"
    if low_key in data:
        low_threshold = _parse_threshold(data[low_key])
        assert value >= low_threshold, f"{name}: {value} below low-alarm-threshold {low_threshold}"


@pytest.mark.platform
@pytest.mark.cpo
@pytest.mark.parametrize('test_api', ApiType.ALL_TYPES)
def test_show_transceiver_els(engines, devices, nv_command, test_api):
    """
    Verify ELS transceiver fields, port-mapping, oe-mapping, and fault-condition.

    flow:
    1. Verify all expected fields exist for a random ELS transceiver
    2. Verify port-mapping, oe-mapping, and fault-condition for each ELS
    """
    TestToolkit.tested_api = test_api

    els_list = devices.dut.els_list

    transceivers_els_to_port_mapping = devices.dut.els_port_mapping
    transceivers_els_to_oe_mapping = devices.dut.els_oe_mapping

    with allure.step("Get all transceiver information with single show_detailed call"):
        all_data = Tools.OutputParsingTool.parse_json_str_to_dictionary(
            nv_command.platform.transceiver.show_detailed()).get_returned_value()

    els_rand = random.choice(els_list)
    els_rand_output = all_data[els_rand]

    with allure.step("ELS transceiver validation steps"):
        with allure.independent_step(f"Verify fields and sub-dict structures for {els_rand}"):
            with allure.independent_step(f"Verify {els_rand} top-level fields"):
                expected_fields = set(TransceiversConsts.TRANSCEIVERS_FIELDS[TransceiversConsts.TRANSCEIVERS_ELS])
                actual_fields = set(els_rand_output.keys())
                ValidationTool.validate_set_equal(actual_fields, expected_fields).verify_result()

            with allure.independent_step(f"Verify {els_rand} temperature sub-dict"):
                ValidationTool.validate_subset_in_superset(
                    set(TransceiversConsts.ELS_TEMPERATURE_FIELDS), els_rand_output['temperature'].keys()
                ).verify_result()

            with allure.independent_step(f"Verify {els_rand} voltage sub-dict"):
                ValidationTool.validate_subset_in_superset(
                    set(TransceiversConsts.ELS_VOLTAGE_FIELDS), els_rand_output['voltage'].keys()
                ).verify_result()

            with allure.independent_step(f"Verify {els_rand} els-initialization sub-dict"):
                init_data = els_rand_output['els-initialization']
                assert init_data, f"{els_rand}: els-initialization is empty"
                for _laser_name, laser_data in init_data.items():
                    ValidationTool.validate_subset_in_superset(
                        set(TransceiversConsts.ELS_INIT_LASER_FIELDS), laser_data.keys()
                    ).verify_result()

            for ch_name, ch_data in els_rand_output['channel'].items():
                with allure.independent_step(f"Verify {els_rand} {ch_name} fields"):
                    ValidationTool.validate_subset_in_superset(
                        set(TransceiversConsts.ELS_CHANNEL_FIELDS), ch_data.keys()
                    ).verify_result()

        with allure.independent_step("Verify port-mapping, oe-mapping, fault-condition, els-oper-state for each ELS"):
            for els in els_list:
                els_output = all_data[els]

                with allure.independent_step(f"Verify {els} mappings"):
                    actual_ports = set(els_output[PlatformConsts.TRANSCEIVER_PORT_MAPPING].keys())
                    expected_ports = set(transceivers_els_to_port_mapping[els])
                    ValidationTool.validate_set_equal(actual_ports, expected_ports).verify_result()

                    actual_oes = set(els_output[PlatformConsts.TRANSCEIVER_OE_MAPPING].keys())
                    expected_oes = set(transceivers_els_to_oe_mapping[els])
                    ValidationTool.validate_set_equal(actual_oes, expected_oes).verify_result()

                    if els_output[PlatformConsts.TRANSCEIVER_STATUS] == PlatformConsts.INSERTED:
                        ValidationTool.verify_field_value_in_output(
                            els_output, PlatformConsts.TRANSCEIVER_FAULT_CONDITION, 'false'
                        ).verify_result()

                        ValidationTool.verify_field_value_in_output(
                            els_output, PlatformConsts.TRANSCEIVER_ELS_OPER_STATE,
                            PlatformConsts.TRANSCEIVER_ELS_OPER_STATE_LASER_ACTIVE
                        ).verify_result()

        with allure.independent_step("Verify ELS transceivers have unique serial numbers"):
            els_transceivers = {name: data for name, data in all_data.items()
                                if name.startswith(TransceiversConsts.TRANSCEIVERS_ELS)}
            els_serial_numbers = [data[TransceiversConsts.TRANSCEIVERS_VENDOR_SN] for name, data in els_transceivers.items()
                                  if TransceiversConsts.TRANSCEIVERS_VENDOR_SN in data]
            assert len(els_serial_numbers) == len(els_transceivers), \
                f"Not all ELS transceivers have serial numbers: {len(els_serial_numbers)}/{len(els_transceivers)}"
            unique_serials = set(els_serial_numbers)
            assert len(unique_serials) == len(els_transceivers), \
                f"ELS serial numbers are not unique: {len(unique_serials)} unique out of {len(els_transceivers)} ELS. " \
                f"Duplicates: {[sn for sn in els_serial_numbers if els_serial_numbers.count(sn) > 1]}"

        with allure.independent_step('gNMI ELS field-set validation'):
            gnmi_client = _build_gnmi_client_or_warn(engines)
            with allure.independent_step(f"Verify {els_rand} gNMI physical-channels field-set"):
                if gnmi_client is None:
                    allure.attach('gNMI ELS skipped', 'client unavailable')
                else:
                    # Use the actual ELS status since it may be Removed
                    els_status = els_rand_output[PlatformConsts.TRANSCEIVER_STATUS]
                    validate_gnmi_transceiver_physical_channel_fields(
                        gnmi_client, els_rand, els_status,
                        expected_field_set=GnmiConstants.EXPECTED_TRANSCEIVER_PHYSICAL_CHANNEL_FIELDS_ELS,
                    )


@pytest.mark.platform
@pytest.mark.cpo
@pytest.mark.parametrize('test_api', ApiType.ALL_TYPES)
def test_show_transceiver_oe(engines, devices, nv_command, test_api):
    """
    Verify OE transceiver fields, status, port-mapping, and els-mapping.

    flow:
    1. Verify all expected fields exist for a random OE transceiver
    2. Verify status=Inserted, port-mapping matches ELS mapping for each OE
    """
    TestToolkit.tested_api = test_api

    oe_list = [name for name in devices.dut.transceiver_list if TransceiversConsts.TRANSCEIVERS_OE in name]

    transceivers_els_to_port_mapping = devices.dut.els_port_mapping
    transceivers_els_to_oe_mapping = devices.dut.els_oe_mapping

    with allure.step("Get all transceiver information with single show_detailed call"):
        all_data = Tools.OutputParsingTool.parse_json_str_to_dictionary(
            nv_command.platform.transceiver.show_detailed()).get_returned_value()

    oe_rand = random.choice(oe_list)
    oe_rand_output = all_data[oe_rand]

    with allure.step("OE transceiver validation steps"):
        with allure.independent_step(f"Verify fields and sub-dict structures for {oe_rand}"):
            with allure.independent_step(f"Verify {oe_rand} top-level fields"):
                expected_fields = set(TransceiversConsts.TRANSCEIVERS_FIELDS[TransceiversConsts.TRANSCEIVERS_OE])
                actual_fields = set(oe_rand_output.keys())
                ValidationTool.validate_set_equal(actual_fields, expected_fields).verify_result()

            with allure.independent_step(f"Verify {oe_rand} temperature sub-dict"):
                ValidationTool.validate_subset_in_superset(
                    set(TransceiversConsts.OE_TEMPERATURE_FIELDS), oe_rand_output['temperature'].keys()
                ).verify_result()

            with allure.independent_step(f"Verify {oe_rand} voltage sub-dict"):
                ValidationTool.validate_subset_in_superset(
                    set(TransceiversConsts.OE_VOLTAGE_FIELDS), oe_rand_output['voltage'].keys()
                ).verify_result()

            for ch_name, ch_data in oe_rand_output['channel'].items():
                with allure.independent_step(f"Verify {oe_rand} {ch_name}"):
                    ValidationTool.validate_subset_in_superset(
                        set(TransceiversConsts.OE_CHANNEL_FIELDS), ch_data.keys()
                    ).verify_result()

                    # Validate rx-power sub-dict
                    ValidationTool.validate_subset_in_superset(
                        set(TransceiversConsts.OE_RX_POWER_FIELDS), ch_data['rx-power'].keys()
                    ).verify_result()

                    # Validate tx-power sub-dict
                    ValidationTool.validate_subset_in_superset(
                        set(TransceiversConsts.OE_TX_POWER_FIELDS), ch_data['tx-power'].keys()
                    ).verify_result()

        with allure.independent_step("Verify status and mappings for each OE"):
            for oe in oe_list:
                oe_output = all_data[oe]

                with allure.independent_step(f"Verify {oe}"):
                    ValidationTool.verify_field_value_in_output(
                        oe_output, PlatformConsts.TRANSCEIVER_STATUS, PlatformConsts.INSERTED
                    ).verify_result()

                    els_mapping = oe_output[PlatformConsts.TRANSCEIVER_ELS_MAPPING]
                    actual_ports = set(oe_output[PlatformConsts.TRANSCEIVER_PORT_MAPPING].keys())
                    expected_ports = set(transceivers_els_to_port_mapping[els_mapping])
                    ValidationTool.validate_set_equal(actual_ports, expected_ports).verify_result()

        with allure.independent_step('gNMI OE field-set validation'):
            gnmi_client = _build_gnmi_client_or_warn(engines)
            with allure.independent_step(f"Verify {oe_rand} gNMI physical-channels field-set"):
                if gnmi_client is None:
                    allure.attach('gNMI OE skipped', 'client unavailable')
                else:
                    validate_gnmi_transceiver_physical_channel_fields(
                        gnmi_client, oe_rand, PlatformConsts.INSERTED,
                        expected_field_set=GnmiConstants.EXPECTED_TRANSCEIVER_PHYSICAL_CHANNEL_FIELDS_OE,
                    )


@pytest.mark.platform
@pytest.mark.transceiver
@pytest.mark.parametrize('test_api', ApiType.ALL_TYPES)
def test_transceiver_els_diagnostics(engines, devices, nv_command, test_api):
    """
    Verify ELS transceiver diagnostics: temperature, voltage, and channel fields.

    flow:
    1. Select random ELS, verify temperature against high-alarm-threshold
    2. Verify voltage against high/low-alarm-thresholds from output
    3. Verify channel diagnostics fields exist
    """
    TestToolkit.tested_api = test_api
    platform = Platform()

    els_list, _ = _get_transceiver_lists(devices.dut.transceiver_list)
    if not els_list:
        pytest.skip("No ELS transceivers found")

    els_name = random.choice(els_list)

    with allure.step(f"Get ELS {els_name} diagnostics"):
        els_output = Tools.OutputParsingTool.parse_json_str_to_dictionary(
            platform.transceiver.show(els_name)).get_returned_value()

    with allure.step("Verify temperature against thresholds"):
        temp_data = els_output['temperature']
        ValidationTool.validate_subset_in_superset(
            set(TransceiversConsts.ELS_TEMPERATURE_FIELDS), temp_data.keys()
        ).verify_result()

        temp_value = _parse_threshold(temp_data['temperature'])
        _validate_against_thresholds(temp_value, temp_data, f"{els_name} temperature")
        logger.info(f"{els_name} temperature: {temp_value}C (max: {temp_data['high-alarm-threshold']})")

    with allure.step("Verify voltage against thresholds"):
        voltage_data = els_output['voltage']
        ValidationTool.validate_subset_in_superset(
            set(TransceiversConsts.ELS_VOLTAGE_FIELDS), voltage_data.keys()
        ).verify_result()

        voltage_value = _parse_threshold(voltage_data['voltage'])
        _validate_against_thresholds(voltage_value, voltage_data, f"{els_name} voltage")
        logger.info(f"{els_name} voltage: {voltage_value}V (range: {voltage_data['low-alarm-threshold']} - {voltage_data['high-alarm-threshold']})")

    with allure.step("Verify channel diagnostics fields"):
        channels = els_output['channel']
        assert channels, "No channels found"

        first_channel = next(iter(channels.values()))
        ValidationTool.validate_subset_in_superset(
            set(TransceiversConsts.ELS_CHANNEL_FIELDS), first_channel.keys()
        ).verify_result()

    with allure.step("Verify channel laser and tec-temp values"):
        for ch_name, ch_data in channels.items():
            with allure.independent_step(f"Verify {els_name} {ch_name} diagnostics"):
                laser_setpoint = ch_data.get('laser-setpoint')
                laser_power = ch_data.get('laser-power')
                tec_temp = ch_data.get('tec-temp')
                assert laser_setpoint is not None, f"{els_name} {ch_name}: missing 'laser-setpoint'"
                assert laser_power is not None, f"{els_name} {ch_name}: missing 'laser-power'"
                assert tec_temp is not None, f"{els_name} {ch_name}: missing 'tec-temp'"

                # Parse and validate power values (format: "135.83 mW / 21.33 dBm")
                setpoint_mw = float(laser_setpoint.split()[0])
                power_mw = float(laser_power.split()[0])
                assert setpoint_mw > 0, f"{els_name} {ch_name}: laser-setpoint {setpoint_mw} mW should be > 0"
                assert power_mw > 0, f"{els_name} {ch_name}: laser-power {power_mw} mW should be > 0"

                # Validate tec-temp is in expected range (~40C nominal, ±2C for HW variance)
                tec_temp_value = _parse_threshold(tec_temp)
                if not (TransceiversConsts.ELS_TEC_TEMP_MIN <= tec_temp_value <= TransceiversConsts.ELS_TEC_TEMP_MAX):
                    logger.warning(f"{els_name} {ch_name}: tec-temp {tec_temp_value}C outside expected "
                                   f"range {TransceiversConsts.ELS_TEC_TEMP_MIN}-{TransceiversConsts.ELS_TEC_TEMP_MAX}C "
                                   f"(may indicate HW issue)")

                logger.info(f"{els_name} {ch_name}: laser-setpoint={laser_setpoint}, "
                            f"laser-power={laser_power}, tec-temp={tec_temp_value}C")


def _parse_power_dbm(power_str: str) -> float:
    """Parse dBm value from power string (e.g., '0.54 mW / -2.68 dBm' -> -2.68)."""
    # Format: "0.54 mW / -2.68 dBm" - extract dBm value after "/"
    return float(power_str.split('/')[1].strip().split()[0])


@pytest.mark.platform
@pytest.mark.transceiver
@pytest.mark.parametrize('test_api', ApiType.ALL_TYPES)
def test_transceiver_oe_power_diagnostics(engines, devices, nv_command, test_api):
    """
    Verify OE transceiver diagnostics: temperature, voltage, and channel power/temperature.

    flow:
    1. Always: pick a random OE and validate module temp, voltage, and all channel fields
    2. If traffic ports configured: validate rx/tx power thresholds on traffic channels
    """
    TestToolkit.tested_api = test_api
    platform = Platform()

    _, oe_list = _get_transceiver_lists(devices.dut.transceiver_list)
    if not oe_list:
        pytest.skip("No OE transceivers found")

    # --- Part 1: Always validate a random OE (no traffic ports needed) ---
    oe_name = random.choice(oe_list)

    with allure.step(f"Get OE {oe_name} diagnostics"):
        oe_output = Tools.OutputParsingTool.parse_json_str_to_dictionary(
            platform.transceiver.show(oe_name)).get_returned_value()

    with allure.step(f"Verify {oe_name} temperature against thresholds"):
        temp_data = oe_output['temperature']
        temp_value = _parse_threshold(temp_data['temperature'])
        _validate_against_thresholds(temp_value, temp_data, f"{oe_name} temperature")
        logger.info(f"{oe_name} temperature: {temp_value}C "
                    f"(range: {temp_data.get('low-alarm-threshold', 'N/A')} - "
                    f"{temp_data.get('high-alarm-threshold', 'N/A')})")

    with allure.step(f"Verify {oe_name} voltage against thresholds"):
        voltage_data = oe_output['voltage']
        voltage_value = _parse_threshold(voltage_data['voltage'])
        _validate_against_thresholds(voltage_value, voltage_data, f"{oe_name} voltage")
        logger.info(f"{oe_name} voltage: {voltage_value}V "
                    f"(range: {voltage_data.get('low-alarm-threshold', 'N/A')} - "
                    f"{voltage_data.get('high-alarm-threshold', 'N/A')})")

    with allure.step(f"Verify {oe_name} all channel fields"):
        channels = oe_output['channel']
        assert channels, f"{oe_name}: no channels found"

        for ch_name, channel_data in channels.items():
            with allure.independent_step(f"Validate {oe_name} {ch_name}"):
                els_input_power = channel_data.get('els-input-power')
                assert els_input_power is not None, \
                    f"{oe_name} {ch_name}: missing 'els-input-power' field"

                oe_lane_temp = channel_data.get('oe-lane-temperature')
                assert oe_lane_temp is not None, \
                    f"{oe_name} {ch_name}: missing 'oe-lane-temperature' field"
                oe_lane_temp_value = _parse_threshold(oe_lane_temp)
                _validate_against_thresholds(
                    oe_lane_temp_value, temp_data, f"{oe_name} {ch_name} oe-lane-temperature"
                )

                logger.info(f"{oe_name} {ch_name}: els-input-power={els_input_power}, "
                            f"oe-lane-temperature={oe_lane_temp_value}C - VALID")

    # --- Part 2: Traffic-specific power threshold validation (if configured) ---
    traffic_ports = Configurations.traffic_ports.get(engines.dut.ip, [])
    if not traffic_ports:
        logger.info(f"No traffic ports configured for DUT {engines.dut.ip}, skipping power threshold validation")
        return

    els_name, matching_ports, _ = PhotonicsTool.get_els_for_traffic_ports(devices.dut, traffic_ports)
    if not els_name:
        logger.info(f"No ELS found for traffic ports {traffic_ports}, skipping power threshold validation")
        return

    traffic_oe_list = PhotonicsTool.get_oe_list_for_els(devices.dut, els_name)
    logger.info(f"ELS {els_name} with traffic ports {matching_ports}, OEs: {traffic_oe_list}")

    with allure.step(f"Validate power thresholds for traffic OEs: {traffic_oe_list}"):
        for traffic_oe_name in traffic_oe_list:
            traffic_oe_output = Tools.OutputParsingTool.parse_json_str_to_dictionary(
                platform.transceiver.show(traffic_oe_name)).get_returned_value()

            port_list = list(traffic_oe_output.get('port-mapping', {}).keys())
            channel_indices = PhotonicsTool.get_traffic_channel_indices(port_list, traffic_ports)

            if not channel_indices:
                logger.info(f"{traffic_oe_name}: no traffic ports in mapping, skipping")
                continue

            traffic_channels = traffic_oe_output.get('channel', {})
            for ch_idx in channel_indices:
                if not (channel_data := traffic_channels.get(f'channel-{ch_idx}')):
                    continue

                with allure.independent_step(f"Validate {traffic_oe_name} channel-{ch_idx} power"):
                    rx_data = channel_data['rx-power']
                    tx_data = channel_data['tx-power']

                    rx_dbm = _parse_power_dbm(rx_data['power'])
                    tx_dbm = _parse_power_dbm(tx_data['power'])

                    _validate_against_thresholds(rx_dbm, rx_data, f"{traffic_oe_name} ch-{ch_idx} rx-power")
                    _validate_against_thresholds(tx_dbm, tx_data, f"{traffic_oe_name} ch-{ch_idx} tx-power")

                    logger.info(f"{traffic_oe_name} ch-{ch_idx}: rx={rx_dbm} dBm, tx={tx_dbm} dBm - VALID")
