"""Unit-only coverage for NVUE DUT-port filtering; no lab devices are accessed."""

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

import pytest


NGTS_ROOT = Path(__file__).resolve().parents[3]


def _module(name, **attributes):
    module = ModuleType(name)
    for attribute, value in attributes.items():
        setattr(module, attribute, value)
    return module


class _PerformanceCommon:
    pass


class _TestIssue(Exception):
    pass


def _identity_retry(**kwargs):
    del kwargs
    return lambda function: function


DEPENDENCY_STUBS = {
    "allure": _module("allure"),
    "yaml": _module("yaml"),
    "retry": _module("retry", retry=_identity_retry),
    "retry.api": _module("retry.api", retry_call=Mock()),
    "jinja2": _module("jinja2", Environment=Mock(), FileSystemLoader=Mock()),
    "devts.infra.tools.exceptions.test_issue": _module(
        "devts.infra.tools.exceptions.test_issue", TestIssue=_TestIssue),
    "devts.infra.tools.exceptions.real_issue": _module(
        "devts.infra.tools.exceptions.real_issue", RealIssue=Exception),
    "ngts.nvos_tools.infra.SendCommandTool": _module(
        "ngts.nvos_tools.infra.SendCommandTool", SendCommandTool=Mock()),
    "ngts.cli_wrappers.nvue.nvue_json_utils": _module(
        "ngts.cli_wrappers.nvue.nvue_json_utils", parse_nvue_json_cli_output=Mock()),
    "ngts.constants.constants": _module(
        "ngts.constants.constants", BugHandlerConst=Mock(), ResultUploaderConst=Mock()),
    "ngts.constants.performance_constants": _module(
        "ngts.constants.performance_constants",
        MongoDbConsts=Mock(),
        PerfConsts=SimpleNamespace(DEFAULT_PERF_TEMPLATES_DIR=""),
        Cl_Consts=SimpleNamespace(CL_HOME_DIR="", CL_PYTHON_PATH=""),
        ValidationConsts=Mock()),
    "ngts.cli_wrappers.common.performance_clis_common": _module(
        "ngts.cli_wrappers.common.performance_clis_common", PerformanceCommon=_PerformanceCommon),
    "ngts.helpers.performance.nvue_port_count": _module(
        "ngts.helpers.performance.nvue_port_count",
        get_nvue_data_physical_ports_count=Mock(),
        get_nvue_expected_nexthops=Mock(),
        get_nvue_physical_ports_count=Mock()),
    "ngts.helpers.performance.sensors_power_parse": _module(
        "ngts.helpers.performance.sensors_power_parse", build_controllers_info_dicts_list=Mock()),
    "ngts.helpers.performance.traffic_helpers": _module(
        "ngts.helpers.performance.traffic_helpers",
        generate_ip_address_list=Mock(),
        is_ipv6=Mock(),
        address_calculator=Mock()),
    "ngts.helpers.performance.port_selection": _module(
        "ngts.helpers.performance.port_selection",
        resolve_symmetric_cascade=Mock(),
        set_resolved_excluded_dut_ports=Mock(),
        get_resolved_excluded_dut_ports=Mock(),
        clear_resolved_excluded_ports=Mock(),
        record_port_selection_debug=Mock()),
}


with patch.dict(sys.modules, DEPENDENCY_STUBS):
    performance_spec = importlib.util.spec_from_file_location(
        "test_nvue_performance_clis",
        NGTS_ROOT / "cli_wrappers" / "nvue" / "nvue_performance_clis.py")
    performance_module = importlib.util.module_from_spec(performance_spec)
    performance_spec.loader.exec_module(performance_module)


@pytest.mark.parametrize("bonus_ports, bonus_port_in_output", [
    (["swp65"], "swp65s0"),
    (["swp65", "swp66"], "swp66s3"),
    (["swp65s0", "swp65s1"], "swp65"),
])
def test_get_dut_ports_returns_only_front_panel_ports_in_input_order(
        bonus_ports, bonus_port_in_output):
    """Verify physical JSON is filtered by interface name and platform bonus ports."""
    cli = performance_module.NvuePerformanceCli.__new__(performance_module.NvuePerformanceCli)
    cli.engine = Mock()
    cli.cli_obj = SimpleNamespace(interface=Mock())
    cli.cli_obj.interface.get_bonus_ports.return_value = bonus_ports
    cli.execute_cmd = Mock(return_value=json.dumps({
        "usb0": {},
        "swp2": {},
        "eth0": {"type": "swp"},
        "swp1s3": {},
        "lo": {},
        bonus_port_in_output: {},
        "docker0": {},
        "swp7": {"type": "ethernet"},
    }))

    assert cli.get_dut_ports() == ["swp2", "swp1s3", "swp7"]


def test_get_dut_ports_sdk_ports_behavior_is_unchanged():
    """Verify the SDK path returns connected ports without reading physical JSON."""
    cli = performance_module.NvuePerformanceCli.__new__(performance_module.NvuePerformanceCli)
    cli.engine = Mock()
    cli.cli_obj = SimpleNamespace(interface=Mock())
    cli.cli_obj.interface.get_bonus_ports.return_value = []
    cli.get_player_ports = Mock(return_value={"connected_ports": [4097, 4098]})
    cli.execute_cmd = Mock()

    assert cli.get_dut_ports(sdk_ports=True) == [4097, 4098]
    cli.execute_cmd.assert_not_called()
    cli.cli_obj.interface.get_bonus_ports.assert_not_called()
