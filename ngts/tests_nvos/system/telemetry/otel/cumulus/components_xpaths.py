"""Components gNMI XPath builder — reproduces the SSIM3 expected XPath set for
``components_top_level_subscription``.

Source parity: cl-cafe ``cafe/components/telemetry.py`` (``ComponentsXPaths*`` hierarchy)
and cl-tests ``components_top_level_subscription`` (per-type component enumeration + bulk
XPath expansion).

Only the XPath strings and ``top_level`` are consumed by the top-level subscription test, so
this port keeps just those. SSIM3's value-comparison metadata (show command / show key) is
intentionally dropped — the top-level subscription verifies *XPath coverage* per timestamp,
never values.

The SSIM3 ``ComponentsXPaths.populate_gnmi_xpaths`` is called once per discovered component
(with its ``component_type`` and, for transceivers, its ``transceiver_type`` /
``firmware-present`` metadata); every ``{"xpath": ...}`` / ``additional_xpaths`` descriptor it
sets becomes an expected leaf. ``build_components_top_level_xpaths`` reproduces exactly this
expansion for the discovered ``components_by_type`` map.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from ngts.tests_nvos.system.telemetry.otel.cumulus.system_xpaths import (
    get_all_supported_xpaths,
    get_all_variables,
)

logger = logging.getLogger(__name__)


class ComponentType:
    """gNMI component types enumerated by SSIM3 ``components_top_level_subscription``."""

    PSU = "PSU"
    FAN = "FAN"
    TEMP = "TEMP"
    TRANSCEIVER = "TRANSCEIVER"
    ASIC = "ASIC"
    CHASSIS = "CHASSIS"
    STORAGE = "STORAGE"
    CPU = "CPU"
    LEAKAGE = "LEAKAGE"

    ALL: Tuple[str, ...] = (
        PSU, FAN, TEMP, TRANSCEIVER, ASIC, CHASSIS, STORAGE, CPU, LEAKAGE,
    )
    # Types that skip the common name / oper-status / description state leaves.
    NO_BASIC_STATE: Tuple[str, ...] = (
        TRANSCEIVER, ASIC, CHASSIS, STORAGE, CPU, LEAKAGE,
    )


COMPONENT_TYPES = ComponentType.ALL

# SSIM3 ``get_bad_absent_component_basic_xpaths`` — basic state leaves streamed even for
# BAD / ABSENT PSU and TEMP components (FAN does NOT stream when absent/bad).
BASIC_LEAVES: Tuple[str, ...] = ("state/name", "state/description", "state/oper-status")
# A BAD / ABSENT PSU additionally streams ``state/type`` (observed on the DUT: PSU2 streams
# name / description / oper-status / type), unlike a BAD / ABSENT TEMP sensor.
PSU_BAD_ABSENT_LEAVES: Tuple[str, ...] = BASIC_LEAVES + ("state/type",)

_TRANSCEIVER_CHANNEL_RANGE = range(1, 9)


# ========================================================================
# ComponentsXPaths class hierarchy (SSIM3 parity, xpath-only)
# ========================================================================

class ComponentsXPaths:
    """SSIM3 ``ComponentsXPaths`` (NVUE 1.9) — xpath-only."""

    def __init__(self):
        self.component_type = None
        self.component_name = None
        self.temp_description = None
        self.top_level = "/components/"

    def _normalize_names(self, component_name, component_type):
        if component_type == ComponentType.TEMP and isinstance(component_name, dict):
            self.component_name = list(component_name.keys())[0]
            self.temp_description = list(component_name.values())[0]
        elif component_type == ComponentType.TRANSCEIVER:
            if isinstance(component_name, dict) and "name" in component_name:
                self.component_name = component_name["name"]["component_name"]
            else:
                self.component_name = component_name
        else:
            self.component_name = component_name

    def populate_gnmi_xpaths(self, component_name, component_type=ComponentType.PSU, channel_index=1, **kwargs):
        self.component_type = component_type
        self._normalize_names(component_name, component_type)

        base_path = f"/components/component[name={self.component_name}]"
        self.top_level = "/components/"

        if self.component_type not in ComponentType.NO_BASIC_STATE:
            self.name = {"xpath": f"{base_path}/state/name"}
            self.oper_status = {"xpath": f"{base_path}/state/oper-status"}
            self.description = {"xpath": f"{base_path}/state/description"}
        elif self.component_type == ComponentType.LEAKAGE:
            self.name = {"xpath": f"{base_path}/state/name"}
            self.oper_status = {"xpath": f"{base_path}/state/oper-status"}

        if self.component_type == ComponentType.PSU:
            psu_base_path = f"{base_path}/power-supply/state"
            self.psu_capacity = {"xpath": f"{psu_base_path}/capacity"}
            self.psu_output_current = {"xpath": f"{psu_base_path}/output-current"}
            self.psu_output_voltage = {"xpath": f"{psu_base_path}/output-voltage"}
            self.psu_output_power = {"xpath": f"{psu_base_path}/output-power"}
        elif self.component_type == ComponentType.FAN:
            fan_base_path = f"{base_path}/fan/state"
            self.fan_speed = {"xpath": f"{fan_base_path}/speed"}
        elif self.component_type == ComponentType.TEMP:
            temp_base_path = f"{base_path}/state/temperature"
            self.temp_instant = {"xpath": f"{temp_base_path}/instant"}
            self.temp_alarm_status = {"xpath": f"{temp_base_path}/alarm-status"}
        elif self.component_type == ComponentType.TRANSCEIVER:
            transceiver_base_xpath = f"{base_path}/transceiver"
            transceiver_type = None
            if isinstance(component_name, dict) and "transceiver_type" in component_name:
                transceiver_type = component_name["transceiver_type"]
            if transceiver_type == "fiber":
                self.transceiver_input_power = {
                    "xpath": f"{transceiver_base_xpath}/physical-channels/channel[index={channel_index}]/state/input-power/instant",
                    "additional_xpaths": [f"{transceiver_base_xpath}/physical-channels/channel[index={x}]/state/input-power/instant" for x in _TRANSCEIVER_CHANNEL_RANGE],
                }
                self.transceiver_laser_bias_current = {
                    "xpath": f"{transceiver_base_xpath}/physical-channels/channel[index={channel_index}]/state/laser-bias-current/instant",
                    "additional_xpaths": [f"{transceiver_base_xpath}/physical-channels/channel[index={x}]/state/laser-bias-current/instant" for x in _TRANSCEIVER_CHANNEL_RANGE],
                }
                self.transceiver_output_power = {
                    "xpath": f"{transceiver_base_xpath}/physical-channels/channel[index={channel_index}]/state/output-power/instant",
                    "additional_xpaths": [f"{transceiver_base_xpath}/physical-channels/channel[index={x}]/state/output-power/instant" for x in _TRANSCEIVER_CHANNEL_RANGE],
                }
                self.transceiver_temperature = {"xpath": f"{base_path}/state/temperature/instant"}


class ComponentsXPaths110(ComponentsXPaths):
    """SSIM3 ``ComponentsXPaths110`` (NVUE 1.10) — xpath-only."""

    def populate_gnmi_xpaths(self, component_name, component_type=ComponentType.PSU, channel_index=1, expect_alarm=False, **kwargs):
        super().populate_gnmi_xpaths(component_name, component_type, channel_index, **kwargs)
        self.supported_prefixes = ["/components/component[name=*]"]

        if self.component_type == ComponentType.PSU:
            psu_base_path = f"/components/component[name={self.component_name}]/power-supply/state"
            self.psu_input_current = {"xpath": f"{psu_base_path}/input-current"}
            self.psu_input_voltage = {"xpath": f"{psu_base_path}/input-voltage"}
            self.psu_input_power = {"xpath": f"{psu_base_path}/input-power"}
        elif self.component_type == ComponentType.TEMP:
            if isinstance(component_name, dict):
                component_name_key = list(component_name.keys())[0]
            else:
                component_name_key = component_name
            temp_base_path = f"/components/component[name={component_name_key}]/state/temperature"
            self._add_temp_stats(temp_base_path)
            if expect_alarm:
                self.temp_alarm_threshold = {"xpath": f"{temp_base_path}/alarm-threshold"}
                self.temp_alarm_severity = {"xpath": f"{temp_base_path}/alarm-severity"}
        elif self.component_type == ComponentType.TRANSCEIVER:
            self._add_transceiver_common(component_name, channel_index, firmware_detail=False)
        elif self.component_type == ComponentType.ASIC:
            asic_name = component_name["component_name"]
            self.asic_firmware_version = {"xpath": f"/components/component[name={asic_name}]/state/firmware-version"}
        elif self.component_type == ComponentType.CHASSIS:
            chassis_name = component_name["component_name"]
            chassis_base_path = f"/components/component[name={chassis_name}]/state"
            self.chassis_last_reboot_reason = {"xpath": f"{chassis_base_path}/last-reboot-reason"}
            self.chassis_last_reboot_time = {"xpath": f"{chassis_base_path}/last-reboot-time"}

    def _add_temp_stats(self, temp_base_path):
        self.temp_avg = {"xpath": f"{temp_base_path}/avg"}
        self.temp_min = {"xpath": f"{temp_base_path}/min"}
        self.temp_max = {"xpath": f"{temp_base_path}/max"}
        self.temp_min_time = {"xpath": f"{temp_base_path}/min-time"}
        self.temp_max_time = {"xpath": f"{temp_base_path}/max-time"}
        self.temp_interval = {"xpath": f"{temp_base_path}/interval"}

    def _add_transceiver_common(self, component_name, channel_index, *, firmware_detail):
        if isinstance(component_name, dict) and "name" in component_name:
            transceiver_name = component_name["name"]["component_name"]
        else:
            transceiver_name = component_name
        base_path = f"/components/component[name={transceiver_name}]"
        transceiver_base_path = f"{base_path}/transceiver"

        transceiver_type = None
        if isinstance(component_name, dict) and "transceiver_type" in component_name:
            transceiver_type = component_name["transceiver_type"]
        firmware_present = component_name.get("firmware-present", True) if isinstance(component_name, dict) else True

        if transceiver_type == "fiber":
            include_all, include_copper_fiber, include_optical = True, True, True
        elif transceiver_type == "copper":
            include_all, include_copper_fiber, include_optical = True, True, False
        elif transceiver_type == "inactive":
            include_all, include_copper_fiber, include_optical = True, False, False
        else:
            include_all, include_copper_fiber, include_optical = False, False, False

        if include_all:
            self.transceiver_enabled = {"xpath": f"{transceiver_base_path}/state/enabled"}
            self.transceiver_form_factor = {"xpath": f"{transceiver_base_path}/state/form-factor"}
            self.transceiver_present = {"xpath": f"{transceiver_base_path}/state/present"}

        if include_copper_fiber:
            if firmware_present:
                self.transceiver_firmware_version = {"xpath": f"{base_path}/state/firmware-version"}
            # SSIM3 parity: ethernet-pmd is expected for BOTH copper and fiber modules. Some devices
            # (e.g. passive-copper DACs / SP6 QSFP28) do not stream it — that is a known device-side
            # gap tracked separately, NOT filtered here (filtering would be a false pass vs SSIM3).
            self.transceiver_ethernet_pmd = {"xpath": f"{transceiver_base_path}/state/ethernet-pmd"}
            self.transceiver_serial_no = {"xpath": f"{transceiver_base_path}/state/serial-no"}
            self.transceiver_vendor = {"xpath": f"{transceiver_base_path}/state/vendor"}
            self.transceiver_vendor_part = {"xpath": f"{transceiver_base_path}/state/vendor-part"}
            self.transceiver_vendor_rev = {"xpath": f"{transceiver_base_path}/state/vendor-rev"}
            self.transceiver_date_code = {"xpath": f"{transceiver_base_path}/state/date-code"}

        if include_optical:
            self.transceiver_supply_voltage = {"xpath": f"{transceiver_base_path}/state/supply-voltage/instant"}
            self.transceiver_rx_cdr_lol = {
                "xpath": f"{transceiver_base_path}/physical-channels/channel[index={channel_index}]/state/rx-cdr-lol",
                "additional_xpaths": [f"{transceiver_base_path}/physical-channels/channel[index={x}]/state/rx-cdr-lol" for x in _TRANSCEIVER_CHANNEL_RANGE],
            }
            self.transceiver_rx_los = {
                "xpath": f"{transceiver_base_path}/physical-channels/channel[index={channel_index}]/state/rx-los",
                "additional_xpaths": [f"{transceiver_base_path}/physical-channels/channel[index={x}]/state/rx-los" for x in _TRANSCEIVER_CHANNEL_RANGE],
            }
            self.transceiver_tx_los = {
                "xpath": f"{transceiver_base_path}/host-lanes/lane[lane-number={channel_index}]/state/tx-los",
                "additional_xpaths": [f"{transceiver_base_path}/host-lanes/lane[lane-number={x}]/state/tx-los" for x in _TRANSCEIVER_CHANNEL_RANGE],
            }
            self.transceiver_tx_cdr_lol = {
                "xpath": f"{transceiver_base_path}/host-lanes/lane[lane-number={channel_index}]/state/tx-cdr-lol",
                "additional_xpaths": [f"{transceiver_base_path}/host-lanes/lane[lane-number={x}]/state/tx-cdr-lol" for x in _TRANSCEIVER_CHANNEL_RANGE],
            }
            self.transceiver_input_power = {
                "xpath": f"{transceiver_base_path}/physical-channels/channel[index={channel_index}]/state/input-power/instant",
                "additional_xpaths": [f"{transceiver_base_path}/physical-channels/channel[index={x}]/state/input-power/instant" for x in _TRANSCEIVER_CHANNEL_RANGE],
            }
            self.transceiver_laser_bias_current = {
                "xpath": f"{transceiver_base_path}/physical-channels/channel[index={channel_index}]/state/laser-bias-current/instant",
                "additional_xpaths": [f"{transceiver_base_path}/physical-channels/channel[index={x}]/state/laser-bias-current/instant" for x in _TRANSCEIVER_CHANNEL_RANGE],
            }
            self.transceiver_output_power = {
                "xpath": f"{transceiver_base_path}/physical-channels/channel[index={channel_index}]/state/output-power/instant",
                "additional_xpaths": [f"{transceiver_base_path}/physical-channels/channel[index={x}]/state/output-power/instant" for x in _TRANSCEIVER_CHANNEL_RANGE],
            }
            self.transceiver_temperature = {"xpath": f"{base_path}/state/temperature/instant"}
            transceiver_temp_base_path = f"{base_path}/state/temperature"
            self._add_temp_stats(transceiver_temp_base_path)


class ComponentsXPaths111(ComponentsXPaths110):
    """SSIM3 ``ComponentsXPaths111`` (NVUE 1.11) — xpath-only.

    SSIM3's 111 re-implements the base logic (it does not call ``super().populate_gnmi_xpaths``),
    which is why FAN gains direction/max-speed/min-speed and the common/PSU/TEMP/TRANSCEIVER leaves
    are set explicitly. This port mirrors that: 111 is the effective floor for the 112/113/114 chain.
    """

    def populate_gnmi_xpaths(self, component_name, component_type=ComponentType.PSU, channel_index=1, expect_alarm=False, **kwargs):
        self.component_type = component_type
        self._normalize_names(component_name, component_type)
        self.channel_index = channel_index
        self.expect_alarm = expect_alarm

        base_path = f"/components/component[name={self.component_name}]"
        self.top_level = "/components/"

        if self.component_type not in ComponentType.NO_BASIC_STATE:
            self.name = {"xpath": f"{base_path}/state/name"}
            self.oper_status = {"xpath": f"{base_path}/state/oper-status"}
            self.description = {"xpath": f"{base_path}/state/description"}
        elif self.component_type == ComponentType.LEAKAGE:
            self.name = {"xpath": f"{base_path}/state/name"}
            self.oper_status = {"xpath": f"{base_path}/state/oper-status"}

        self.supported_prefixes = ["/components/component[name=*]"]

        if self.component_type == ComponentType.PSU:
            psu_base_path = f"{base_path}/power-supply/state"
            self.psu_input_current = {"xpath": f"{psu_base_path}/input-current"}
            self.psu_input_voltage = {"xpath": f"{psu_base_path}/input-voltage"}
            self.psu_input_power = {"xpath": f"{psu_base_path}/input-power"}
            self.psu_capacity = {"xpath": f"{psu_base_path}/capacity"}
            self.psu_output_current = {"xpath": f"{psu_base_path}/output-current"}
            self.psu_output_voltage = {"xpath": f"{psu_base_path}/output-voltage"}
            self.psu_output_power = {"xpath": f"{psu_base_path}/output-power"}
        elif self.component_type == ComponentType.FAN:
            fan_base_path = f"{base_path}/fan/state"
            self.fan_speed = {"xpath": f"{fan_base_path}/speed"}
            self.fan_direction = {"xpath": f"{fan_base_path}/direction"}
            self.fan_max_speed = {"xpath": f"{fan_base_path}/max-speed"}
            self.fan_min_speed = {"xpath": f"{fan_base_path}/min-speed"}
        elif self.component_type == ComponentType.TEMP:
            if isinstance(component_name, dict):
                component_name_key = list(component_name.keys())[0]
            else:
                component_name_key = component_name
            temp_base_path = f"/components/component[name={component_name_key}]/state/temperature"
            self.temp_instant = {"xpath": f"{temp_base_path}/instant"}
            self.temp_alarm_status = {"xpath": f"{temp_base_path}/alarm-status"}
            self._add_temp_stats(temp_base_path)
            if expect_alarm:
                self.temp_alarm_threshold = {"xpath": f"{temp_base_path}/alarm-threshold"}
                self.temp_alarm_severity = {"xpath": f"{temp_base_path}/alarm-severity"}
        elif self.component_type == ComponentType.TRANSCEIVER:
            self._add_transceiver_common(component_name, channel_index, firmware_detail=True)
        elif self.component_type == ComponentType.ASIC:
            asic_name = component_name["component_name"]
            self.asic_firmware_version = {"xpath": f"/components/component[name={asic_name}]/state/firmware-version"}
        elif self.component_type == ComponentType.CHASSIS:
            chassis_name = component_name["component_name"]
            chassis_base_path = f"/components/component[name={chassis_name}]/state"
            self.chassis_last_reboot_reason = {"xpath": f"{chassis_base_path}/last-reboot-reason"}
            self.chassis_last_reboot_time = {"xpath": f"{chassis_base_path}/last-reboot-time"}
        elif self.component_type == ComponentType.STORAGE:
            self._add_storage(component_name)

    def _add_storage(self, component_name):
        # STORAGE descriptor is either a plain device name or ``{"name": ..., "ata_identify": bool}``.
        if isinstance(component_name, dict):
            name = component_name.get("name")
            ata_identify = bool(component_name.get("ata_identify", True))
        else:
            name = component_name
            ata_identify = True
        prefix = f"/components/component[name={name}]/storage/state/counters"
        state = f"/components/component[name={name}]/state"
        # rotation-rate-rpm / write-cache come from ATA IDENTIFY: only real SATA/ATA disks expose
        # them. NVMe and USB mass-storage (for example a removable USB disk) do not stream them.
        if ata_identify and not (isinstance(name, str) and name.startswith("nvme")):
            self.storage_rotation_rate_rpm = {"xpath": f"{prefix}/rotation-rate-rpm"}
            self.storage_write_cache = {"xpath": f"{prefix}/write-cache"}
            self.storage_write_cache_enabled = {"xpath": f"{prefix}/write-cache-enabled"}
        self.storage_discard_seconds = {"xpath": f"{prefix}/discard-seconds"}
        self.storage_discard_sectors = {"xpath": f"{prefix}/discard-sectors"}
        self.storage_discard_completed = {"xpath": f"{prefix}/discard-completed"}
        self.storage_discard_merged = {"xpath": f"{prefix}/discard-merged"}
        self.storage_flush_req_seconds = {"xpath": f"{prefix}/flush-req-seconds"}
        self.storage_flush_req = {"xpath": f"{prefix}/flush-req"}
        self.storage_io_ops_in_progress = {"xpath": f"{prefix}/io-ops-in-progress"}
        self.storage_io_seconds = {"xpath": f"{prefix}/io-seconds"}
        self.storage_io_weighted_seconds = {"xpath": f"{prefix}/io-weighted-seconds"}
        self.storage_read_bytes = {"xpath": f"{prefix}/read-bytes"}
        self.storage_read_seconds = {"xpath": f"{prefix}/read-seconds"}
        self.storage_read_ops = {"xpath": f"{prefix}/read-ops"}
        self.storage_read_merged = {"xpath": f"{prefix}/read-merged"}
        self.storage_write_seconds = {"xpath": f"{prefix}/write-seconds"}
        self.storage_write_ops = {"xpath": f"{prefix}/write-ops"}
        self.storage_write_merged = {"xpath": f"{prefix}/write-merged"}
        self.storage_write_bytes = {"xpath": f"{prefix}/write-bytes"}
        self.sda_name = {"xpath": f"{state}/name"}
        self.sda_serial_no = {"xpath": f"{state}/serial-no"}
        self.sda_part_no = {"xpath": f"{state}/part-no"}
        # SSIM3 uses the fixed 'cumulus-linux' component for software-version.
        self.sda_software_version = {"xpath": "/components/component[name=cumulus-linux]/state/software-version"}


class ComponentsXPaths112(ComponentsXPaths111):
    """SSIM3 ``ComponentsXPaths112`` (NVUE 1.12) — adds transceiver serial-no for active modules."""

    def populate_gnmi_xpaths(self, component_name, component_type=ComponentType.PSU, channel_index=1, expect_alarm=False, **kwargs):
        super().populate_gnmi_xpaths(component_name, component_type, channel_index, expect_alarm, **kwargs)
        if component_type == ComponentType.TRANSCEIVER:
            transceiver_type = None
            if isinstance(component_name, dict) and "transceiver_type" in component_name:
                transceiver_type = component_name["transceiver_type"]
            if transceiver_type != "inactive":
                transceiver_base_path = f"/components/component[name={self.component_name}]/transceiver"
                self.transceiver_serial_number = {"xpath": f"{transceiver_base_path}/state/serial-no"}


class ComponentsXPaths113(ComponentsXPaths112):
    """SSIM3 ``ComponentsXPaths113`` (NVUE 1.13) — fiber PHY-diag channels/thresholds + CHASSIS/CPU/PSU state."""

    def populate_gnmi_xpaths(self, component_name, component_type=ComponentType.PSU, channel_index=1, expect_alarm=False, **kwargs):
        super().populate_gnmi_xpaths(component_name, component_type, channel_index, expect_alarm, **kwargs)

        if component_type == ComponentType.TRANSCEIVER:
            transceiver_type = component_name.get("transceiver_type") if isinstance(component_name, dict) else None
            if transceiver_type == "fiber":
                transceiver_base_path = f"/components/component[name={self.component_name}]/transceiver"
                threshold_base_path = f"{transceiver_base_path}/thresholds/threshold[severity=CRITICAL]/state"
                self.transceiver_rx_cdr_lol = {
                    "xpath": f"{transceiver_base_path}/physical-channels/channel[index={channel_index}]/state/rx-cdr-lol",
                    "additional_xpaths": [f"{transceiver_base_path}/physical-channels/channel[index={x}]/state/rx-cdr-lol" for x in _TRANSCEIVER_CHANNEL_RANGE],
                }
                self.transceiver_rx_los = {
                    "xpath": f"{transceiver_base_path}/physical-channels/channel[index={channel_index}]/state/rx-los",
                    "additional_xpaths": [f"{transceiver_base_path}/physical-channels/channel[index={x}]/state/rx-los" for x in _TRANSCEIVER_CHANNEL_RANGE],
                }
                self.transceiver_tx_los = {
                    "xpath": f"{transceiver_base_path}/host-lanes/lane[lane-number={channel_index}]/state/tx-los",
                    "additional_xpaths": [f"{transceiver_base_path}/host-lanes/lane[lane-number={x}]/state/tx-los" for x in _TRANSCEIVER_CHANNEL_RANGE],
                }
                self.transceiver_tx_cdr_lol = {
                    "xpath": f"{transceiver_base_path}/host-lanes/lane[lane-number={channel_index}]/state/tx-cdr-lol",
                    "additional_xpaths": [f"{transceiver_base_path}/host-lanes/lane[lane-number={x}]/state/tx-cdr-lol" for x in _TRANSCEIVER_CHANNEL_RANGE],
                }
                self.module_temperature_upper = {"xpath": f"{threshold_base_path}/module-temperature-upper"}
                self.module_temperature_lower = {"xpath": f"{threshold_base_path}/module-temperature-lower"}
                self.supply_voltage_upper = {"xpath": f"{threshold_base_path}/supply-voltage-upper"}
                self.supply_voltage_lower = {"xpath": f"{threshold_base_path}/supply-voltage-lower"}
                self.output_power_upper = {"xpath": f"{threshold_base_path}/output-power-upper"}
                self.output_power_lower = {"xpath": f"{threshold_base_path}/output-power-lower"}
                self.input_power_upper = {"xpath": f"{threshold_base_path}/input-power-upper"}
                self.input_power_lower = {"xpath": f"{threshold_base_path}/input-power-lower"}
                self.laser_bias_current_upper = {"xpath": f"{threshold_base_path}/laser-bias-current-upper"}
                self.laser_bias_current_lower = {"xpath": f"{threshold_base_path}/laser-bias-current-lower"}

        if component_type == ComponentType.CHASSIS:
            chassis_name = component_name.get("component_name", "CHASSIS") if isinstance(component_name, dict) else component_name
            self._add_hw_info_state(chassis_name, prefix="chassis")
        elif component_type == ComponentType.CPU:
            cpu_name = component_name.get("component_name", "CPU") if isinstance(component_name, dict) else component_name
            self._add_hw_info_state(cpu_name, prefix="cpu")
        elif component_type == ComponentType.PSU:
            psu_name = component_name.get("component_name", "PSU") if isinstance(component_name, dict) else component_name
            self._add_hw_info_state(psu_name, prefix="psu")

    def _add_hw_info_state(self, name, *, prefix):
        base_path = f"/components/component[name={name}]/state"
        setattr(self, f"{prefix}_name", {"xpath": f"{base_path}/name"})
        setattr(self, f"{prefix}_serial_no", {"xpath": f"{base_path}/serial-no"})
        setattr(self, f"{prefix}_part_no", {"xpath": f"{base_path}/part-no"})
        setattr(self, f"{prefix}_model_name", {"xpath": f"{base_path}/model-name"})
        setattr(self, f"{prefix}_hardware_version", {"xpath": f"{base_path}/hardware-version"})
        setattr(self, f"{prefix}_type", {"xpath": f"{base_path}/type"})


class ComponentsXPaths114(ComponentsXPaths113):
    """SSIM3 ``ComponentsXPaths114`` (NVUE 1.14) — adds integrated-circuit utilization resources."""

    def populate_gnmi_xpaths(self, component_name, component_type=ComponentType.PSU, channel_index=1, expect_alarm=False, **kwargs):
        super().populate_gnmi_xpaths(component_name, component_type, channel_index, expect_alarm, **kwargs)
        resource_name = kwargs.get("resource_name") or "*"
        base_path = "/components/component/integrated-circuit/utilization/resources"
        self.integrated_circuit_utilization_resources_used = {"xpath": f"{base_path}/resource[name={resource_name}]/state/used"}
        self.integrated_circuit_utilization_resources_free = {"xpath": f"{base_path}/resource[name={resource_name}]/state/free"}
        self.integrated_circuit_utilization_resources_max_limit = {"xpath": f"{base_path}/resource[name={resource_name}]/state/max-limit"}
        self.integrated_circuit_utilization_resources_high_watermark = {"xpath": f"{base_path}/resource[name={resource_name}]/state/high-watermark"}
        self.integrated_circuit_utilization_resources_last_high_watermark = {"xpath": f"{base_path}/resource[name={resource_name}]/state/last-high-watermark"}


# ========================================================================
# XPath enumeration + builder API (SSIM3 parity)
# ========================================================================

# Map ``python3-nvue`` major.minor -> ComponentsXPaths class (cafe ComponentSelector).
COMPONENTS_XPATH_CLASS_BY_NVUE = {
    "1.9": ComponentsXPaths,
    "1.10": ComponentsXPaths110,
    "1.11": ComponentsXPaths111,
    "1.12": ComponentsXPaths112,
    "1.13": ComponentsXPaths113,
    "1.14": ComponentsXPaths114,
}


def resolve_components_xpath_class(nvue_pkg_version=None):
    """Return the ``ComponentsXPaths*`` class for a ``python3-nvue`` version (default 113)."""
    if nvue_pkg_version:
        parts = str(nvue_pkg_version).split(".")
        if len(parts) >= 2:
            key = f"{parts[0]}.{parts[1]}"
            if key in COMPONENTS_XPATH_CLASS_BY_NVUE:
                return COMPONENTS_XPATH_CLASS_BY_NVUE[key]
    logger.warning(
        "python3-nvue version %r missing/unparseable; using ComponentsXPaths113",
        nvue_pkg_version,
    )
    return ComponentsXPaths113


def _component_xpaths_for(cls, component_name, component_type) -> Set[str]:
    obj = cls()
    obj.populate_gnmi_xpaths(component_name, component_type=component_type)
    xpaths: Set[str] = set()
    for attr_name in get_all_variables(obj):
        descriptor = getattr(obj, attr_name, None)
        if isinstance(descriptor, dict) and "xpath" in descriptor:
            xpaths.update(get_all_supported_xpaths(descriptor))
    return xpaths


def _transceiver_component_name(descriptor) -> Optional[str]:
    if isinstance(descriptor, dict):
        name = descriptor.get("name")
        if isinstance(name, dict):
            return name.get("component_name")
    return None


def build_components_top_level_xpaths(
    components_by_type: Dict[str, Sequence],
    *,
    bad_absent_components: Optional[Any] = None,
    nvue_pkg_version=None,
    xpath_class=None,
) -> Tuple[List[str], str]:
    """Reproduce SSIM3 ``components_top_level_subscription`` expected-xpath discovery.

    ``components_by_type`` maps a component type (``PSU``/``FAN``/.../``LEAKAGE``) to the list of
    per-component descriptors that SSIM3 ``return_component_list`` yields (a plain name for
    PSU/FAN/STORAGE/CPU/LEAKAGE, a ``{name: description}`` dict for TEMP, a
    ``{"name": {...}, "transceiver_type": ..., "firmware-present": ...}`` dict for TRANSCEIVER,
    and a ``{"component_name": ...}`` dict for ASIC/CHASSIS).

    Mirrors SSIM3 post-processing: integrated-circuit leaves are dropped from the expected set
    (they are received-noise-filtered instead), transceiver ``firmware-version`` leaves are
    removed for modules that report no module firmware, and basic state leaves for BAD/ABSENT
    PSU/TEMP components are merged back in. Returns ``(sorted_all_xpaths, top_level_path)``.

    ``bad_absent_components`` may be a ``{name: [state_leaf, ...]}`` mapping (each BAD/ABSENT
    component with the exact basic leaves it streams — PSU also streams ``state/type``) or, for
    backwards compatibility, a plain list of names (each gets ``BASIC_LEAVES``).
    """
    cls = xpath_class or resolve_components_xpath_class(nvue_pkg_version)
    all_xpaths: Set[str] = set()
    transceivers_without_firmware: Set[str] = set()

    for component_type in COMPONENT_TYPES:
        for descriptor in components_by_type.get(component_type, []) or []:
            if component_type == ComponentType.TRANSCEIVER and isinstance(descriptor, dict):
                name_val = _transceiver_component_name(descriptor)
                if name_val and not bool(descriptor.get("firmware-present", True)):
                    transceivers_without_firmware.add(name_val)
            all_xpaths.update(_component_xpaths_for(cls, descriptor, component_type))

    # SSIM3 drops integrated-circuit leaves from expected (received-noise-filtered instead).
    all_xpaths = {xp for xp in all_xpaths if "integrated-circuit" not in xp}

    # SSIM3 filter_expected_components_xpaths: modules with no module firmware do not stream it.
    if transceivers_without_firmware:
        fw_suffix = "/state/firmware-version"
        drop = {
            f"/components/component[name={name}]{fw_suffix}"
            for name in transceivers_without_firmware
        }
        all_xpaths -= drop

    # SSIM3 get_bad_absent_component_basic_xpaths: BAD/ABSENT PSU & TEMP still stream basic leaves
    # (PSU also streams state/type — see PSU_BAD_ABSENT_LEAVES).
    if bad_absent_components:
        if isinstance(bad_absent_components, dict):
            bad_absent_items = bad_absent_components.items()
        else:
            bad_absent_items = ((name, BASIC_LEAVES) for name in bad_absent_components)
        for name, leaves in bad_absent_items:
            base = f"/components/component[name={name}]"
            for leaf in leaves:
                all_xpaths.add(f"{base}/{leaf}")

    top_level = cls().top_level  # "/components/"
    return sorted(all_xpaths), top_level
