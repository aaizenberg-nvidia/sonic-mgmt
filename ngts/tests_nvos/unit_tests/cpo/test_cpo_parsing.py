"""Offline parser tests against the HLD sample outputs.

Verifies that the Cpov2Consts field names match the HLD output shapes, that the
JSON parsing path used by the framework handles them, and that relationship
maps built from show outputs satisfy CpoTopology.assert_consistent.

Run offline (no setup) with:
    python -m pytest ngts/tests_nvos/unit_tests/cpo -c ngts/pytest.ini \
        -o filterwarnings=ignore --noconftest
"""

import copy
import json

from ngts.nvos_constants.constants_nvos import Cpov2Consts
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import IbInterfaceConsts
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.platform.Cpo import Cpo
from ngts.tests_nvos.unit_tests.cpo import sample_outputs as samples

TOPOLOGY = samples.TOPOLOGY


def _parse_json_round_trip(data: dict) -> dict:
    """Feed the fixture through the same JSON parsing the framework show() uses."""
    return OutputParsingTool.parse_json_str_to_dictionary(json.dumps(data)).get_returned_value()


class TestCpoShowFields:
    def test_cpo_summary_round_trip(self):
        parsed = _parse_json_round_trip(samples.SHOW_PLATFORM_CPO)
        assert set(parsed.keys()) == set(TOPOLOGY.cpo_names())

    def test_cpo_detail_top_level_fields(self):
        detail = _parse_json_round_trip(samples.SHOW_PLATFORM_CPO_DETAIL)["cpo1"]
        for field in (
            Cpov2Consts.STATUS,
            Cpov2Consts.ERROR_STATUS,
            Cpov2Consts.IDENTIFIER,
            Cpov2Consts.FW_VERSION,
            Cpov2Consts.PORTS,
            Cpov2Consts.LASER_SOURCES,
            Cpov2Consts.THRESHOLDS,
            Cpov2Consts.OE,
            Cpov2Consts.CHANNEL,
        ):
            assert field in detail, f"missing {field} in cpo detail"

    def test_cpo_thresholds_fields(self):
        thresholds = samples.SHOW_PLATFORM_CPO_DETAIL["cpo1"][Cpov2Consts.THRESHOLDS]
        for measured in (
            Cpov2Consts.CH_LASER_SOURCE_INPUT_POWER,
            Cpov2Consts.CH_RX_POWER,
            Cpov2Consts.CH_TX_POWER,
        ):
            for field in (
                Cpov2Consts.HIGH_ALARM,
                Cpov2Consts.LOW_ALARM,
                Cpov2Consts.HIGH_WARNING,
                Cpov2Consts.LOW_WARNING,
            ):
                assert field in thresholds[measured]

    def test_cpo_channel_fields(self):
        detail = samples.SHOW_PLATFORM_CPO_DETAIL["cpo1"]
        assert len(detail[Cpov2Consts.CHANNEL]) == TOPOLOGY.channels_per_cpo
        channel = detail[Cpov2Consts.CHANNEL]["channel-1"]
        for field in (
            Cpov2Consts.CH_RX_POWER,
            Cpov2Consts.CH_TX_POWER,
            Cpov2Consts.CH_RX_LOS,
            Cpov2Consts.CH_TX_LOS,
            Cpov2Consts.CH_TX_FAULT,
            Cpov2Consts.CH_LASER_SOURCE_INPUT_POWER,
            Cpov2Consts.CH_FAULT_OPCODE,
            Cpov2Consts.CH_DP_STATE,
        ):
            assert field in channel, f"missing {field} in channel entry"
        for measured in (Cpov2Consts.CH_RX_POWER, Cpov2Consts.CH_TX_POWER):
            for field in (
                Cpov2Consts.POWER,
                Cpov2Consts.ALARM_STATUS,
                Cpov2Consts.ALARM_SEVERITY,
            ):
                assert field in channel[measured]

    def test_cpo_oe_fields(self):
        detail = samples.SHOW_PLATFORM_CPO_DETAIL["cpo1"]
        assert len(detail[Cpov2Consts.OE]) == TOPOLOGY.oe_per_cpo
        oe = detail[Cpov2Consts.OE]["oe1"]
        for field in (
            Cpov2Consts.IDENTIFIER,
            Cpov2Consts.OE_SERIAL_NUMBER,
            Cpov2Consts.OE_TEMPERATURE,
        ):
            assert field in oe


class TestLaserSourceShowFields:
    def test_summary_round_trip(self):
        parsed = _parse_json_round_trip(samples.SHOW_PLATFORM_LASER_SOURCE)
        assert set(parsed.keys()) == set(TOPOLOGY.els_names())

    def test_detail_top_level_fields(self):
        detail = samples.SHOW_PLATFORM_LASER_SOURCE_DETAIL["els1"]
        for field in (
            Cpov2Consts.DIAGNOSTICS_STATUS,
            Cpov2Consts.STATUS,
            Cpov2Consts.ERROR_STATUS,
            Cpov2Consts.ELS_VENDOR_DATE_CODE,
            Cpov2Consts.IDENTIFIER,
            Cpov2Consts.ELS_VENDOR_NAME,
            Cpov2Consts.ELS_VENDOR_REV,
            Cpov2Consts.ELS_VENDOR_PN,
            Cpov2Consts.ELS_VENDOR_SN,
            Cpov2Consts.FW_VERSION,
            Cpov2Consts.PARENT,
            Cpov2Consts.TEMPERATURE,
            Cpov2Consts.ELS_POWER_CONSUMPTION,
            Cpov2Consts.ELS_ICC_CURRENT,
            Cpov2Consts.THRESHOLD,
            Cpov2Consts.LASER,
        ):
            assert field in detail, f"missing {field} in laser-source detail"

    def test_threshold_fields(self):
        threshold = samples.SHOW_PLATFORM_LASER_SOURCE_DETAIL["els1"][Cpov2Consts.THRESHOLD]
        for severity in (Cpov2Consts.WARNING, Cpov2Consts.ALARM):
            for field in (Cpov2Consts.TX_POWER_UPPER, Cpov2Consts.TX_POWER_LOWER):
                assert field in threshold[severity]

    def test_per_laser_fields(self):
        detail = samples.SHOW_PLATFORM_LASER_SOURCE_DETAIL["els1"]
        assert len(detail[Cpov2Consts.LASER]) == TOPOLOGY.lasers_per_els
        laser = detail[Cpov2Consts.LASER]["laser-1"]
        for field in (
            Cpov2Consts.LASER_ENABLED,
            Cpov2Consts.LASER_OPER_STATUS,
            Cpov2Consts.LASER_ERROR_STATUS,
            Cpov2Consts.LASER_RAMPING_STATUS,
            Cpov2Consts.LASER_POWER_RESTRICTION,
            Cpov2Consts.LASER_AGE,
            Cpov2Consts.LASER_TARGET_OUTPUT_POWER,
            Cpov2Consts.LASER_MPD_CURRENT,
            Cpov2Consts.LASER_BIAS_CURRENT,
            Cpov2Consts.LASER_TEC_CURRENT,
            Cpov2Consts.LASER_TEC_VOLTAGE,
            Cpov2Consts.LASER_TEMPERATURE,
            Cpov2Consts.LASER_HEALTH,
            Cpov2Consts.TEC_HEALTH,
            Cpov2Consts.FREQUENCY_ERROR,
            Cpov2Consts.LASER_TX_POWER,
        ):
            assert field in laser, f"missing {field} in laser entry"

    def test_parent_points_to_owning_cpo(self):
        for els, detail in samples.SHOW_PLATFORM_LASER_SOURCE_DETAIL.items():
            assert detail[Cpov2Consts.PARENT] == TOPOLOGY.cpo_for_els(els)


class TestInterfaceCpoFields:
    def test_interface_cpo_fields(self):
        parsed = _parse_json_round_trip(samples.SHOW_INTERFACE_CPO_SW8P1S1)
        for field in (
            Cpov2Consts.PARENT,
            Cpov2Consts.STATUS,
            Cpov2Consts.PORTS,
            Cpov2Consts.OE,
            Cpov2Consts.CHANNEL,
        ):
            assert field in parsed


class TestFaeSystemCpoFields:
    def test_els_initialization_all_steps(self):
        for els, steps in samples.SHOW_FAE_ELS_INITIALIZATION.items():
            for step in Cpov2Consts.ALL_ACTIVATE_STEPS:
                assert step in steps, f"{els}: missing step {step}"

    def test_els_initialization_per_laser(self):
        per_laser = samples.SHOW_FAE_ELS_INITIALIZATION_PER_LASER["els1"][Cpov2Consts.ELS_INITIALIZATION]
        assert len(per_laser) == TOPOLOGY.lasers_per_els
        # per HLD, the per-laser breakdown keys lasers as laser1.. (no dash) and
        # reports 'fiber-tuning' (unlike the summary table's 'laser-tuning')
        for field in (
            Cpov2Consts.STEP_FIBER_CHECK,
            Cpov2Consts.INIT_FIBER_TUNING,
            Cpov2Consts.STEP_LASER_UP,
            Cpov2Consts.STEP_LASER_FINE_TUNE,
            Cpov2Consts.STEP_POWER_SETPOINT,
            Cpov2Consts.INIT_ERROR,
        ):
            assert field in per_laser["laser1"], f"missing {field}"

    def test_cpo_dump_state(self):
        assert Cpov2Consts.CPO_DUMP_STATE in samples.SHOW_FAE_SYSTEM_CPO


class TestTopologyMapsFromShowOutput:
    def test_split_names_string_and_list(self):
        assert Cpo.split_names("oe1, oe2,oe3") == ["oe1", "oe2", "oe3"]
        assert Cpo.split_names(["oe1", "oe2"]) == ["oe1", "oe2"]
        assert Cpo.split_names(None) == []
        assert Cpo.split_names("") == []

    def test_summary_output_is_consistent_with_topology(self):
        """The documented usage: assert_consistent(**build_topology_maps(...))."""
        maps = Cpo.build_topology_maps(samples.SHOW_PLATFORM_CPO, port_to_cpo=samples.PORT_TO_CPO)
        TOPOLOGY.assert_consistent(**maps).verify_result()

    def test_detail_output_is_consistent_with_topology(self):
        """Without port_to_cpo the helper omits the port maps, so the splat
        still forms a valid assert_consistent call."""
        maps = Cpo.build_topology_maps(samples.SHOW_PLATFORM_CPO_DETAIL)
        assert "cpo_to_ports" not in maps and "port_to_cpo" not in maps
        TOPOLOGY.assert_consistent(**maps).verify_result()

    def test_corrupted_output_is_detected(self):
        corrupted = copy.deepcopy(samples.SHOW_PLATFORM_CPO)
        # cpo2 claims an OE that belongs to cpo1
        corrupted["cpo2"][Cpov2Consts.OE].pop("oe5")
        corrupted["cpo2"][Cpov2Consts.OE]["oe1"] = {}
        maps = Cpo.build_topology_maps(corrupted)
        TOPOLOGY.assert_consistent(**maps).verify_result(should_succeed=False)

    def test_port_mismatch_is_detected(self):
        port_to_cpo = dict(samples.PORT_TO_CPO)
        port_to_cpo["sw1p1s1"] = "cpo3"  # contradicts cpo1's ports
        maps = Cpo.build_topology_maps(samples.SHOW_PLATFORM_CPO, port_to_cpo=port_to_cpo)
        TOPOLOGY.assert_consistent(**maps).verify_result(should_succeed=False)


class TestInterfaceLinkParsing:
    """Real `nv show interface <port> link` captures through the framework parser.

    This is the exact chain the CPO reset/link tests use on-DUT: raw JSON in,
    the nested `state` dict flattened to its single key.
    """

    def _parse_link(self, data: dict) -> dict:
        return OutputParsingTool.parse_show_interface_link_output_to_dictionary(json.dumps(data)).get_returned_value()

    def test_up_capture_state_is_flattened(self):
        parsed = self._parse_link(samples.SHOW_INTERFACE_SW_LINK_NVL5_UP)
        assert parsed[IbInterfaceConsts.LINK_STATE] == "up"
        assert parsed["physical-state"] == "LinkUp"

    def test_down_capture_state_and_pruned_fields(self):
        parsed = self._parse_link(samples.SHOW_INTERFACE_SW_LINK_NVL5_DOWN)
        assert parsed[IbInterfaceConsts.LINK_STATE] == "down"
        assert parsed["plr"]["margin-threshold"] is None
        for negotiated_field in ("lanes", "speed", "mtu", "op-vls"):
            assert negotiated_field not in parsed

    def test_nvl6_acp_capture_round_trip(self):
        parsed = self._parse_link(samples.SHOW_INTERFACE_ACP_LINK_NVL6)
        assert parsed[IbInterfaceConsts.LINK_STATE] == "up"
        assert parsed["fec"] == "octal-fec"
