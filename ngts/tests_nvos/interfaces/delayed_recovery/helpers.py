import pytest

from ngts.cli_wrappers.nvue import nvue_general_clis as gen_clis
from ngts.nvos_constants.constants_nvos import ConfState
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import NvosConsts, InternalNvosConsts
from ngts.nvos_tools.infra import NvosTestToolkit as TestToolkit
import ngts.nvos_tools.infra.Tools as Tools
from ngts.nvos_tools.infra.Fae import Fae
from ngts.nvos_tools.infra.RegressionConfigurations import RegressionLinks
from ngts.tools.test_utils import allure_utils as allure
from ngts.tests_nvos.interfaces.nvl_port import helpers as nvl_helpers
from ngts.ngts_types import DevicesT, EnginesT


def apply_config():
    with allure.step("Apply delayed recovery configuration"):
        return gen_clis.NvueGeneralCli.apply_config(
            TestToolkit.TestToolkit.engines.dut,
            ask_for_confirmation=True,
        )


def wait_for_delayed_recovery_ports(fae_ports, timeout=InternalNvosConsts.DEFAULT_TIMEOUT):
    with allure.step("Wait for delayed recovery ports to be up"):
        for fae_port in fae_ports:
            fae_port.port.interface.wait_for_port_state(
                NvosConsts.LINK_STATE_UP,
                timeout=timeout,
            ).verify_result()


def validate_expected_values(fae_port, expected_values):
    with allure.step("Validate expected values"):
        output = Tools.OutputParsingTool.parse_json_str_to_dictionary(fae_port.interface.link.delayed_recovery.show()).verify_result()
        Tools.ValidationTool.compare_dictionaries(output, expected_values).verify_result()


def _stringify_values(values):
    if isinstance(values, dict):
        return {
            key: _stringify_values(value)
            for key, value in values.items()
        }
    return str(values)


def validate_expected_values_by_rev(fae_port, expected_values_by_rev):
    with allure.step(f"Validate delayed recovery values for {fae_port.port.name}"):
        actual_values_by_rev = {
            rev: _stringify_values(fae_port.interface.link.delayed_recovery.parse_show(rev=rev))
            for rev in expected_values_by_rev
        }
        Tools.ValidationTool.compare_nested_dictionary_content(
            actual_values_by_rev,
            expected_values_by_rev,
        ).verify_result()


def unset_delayed_recovery(fae_ports, apply=True):
    with allure.step("Unset delayed recovery configuration"):
        for fae_port in fae_ports:
            fae_port.interface.link.delayed_recovery.unset().verify_result()
        if apply:
            apply_config()
            wait_for_delayed_recovery_ports(fae_ports)


def set_delayed_recovery_values(fae_port, delayed_recovery_values):
    with allure.step(f"Set delayed recovery values for {fae_port.port.name}"):
        for field, value in delayed_recovery_values.items():
            fae_port.interface.link.delayed_recovery.set(field, value).verify_result()


def delayed_recovery_expected(applied, operational):
    return {
        ConfState.APPLIED: applied,
        ConfState.OPERATIONAL: operational,
    }


def get_connected_ports(devices: DevicesT, engines: EnginesT, setup_name: str):
    with allure.step("find connected ports"):
        if devices.dut.switch_type.lower() == "nvl":
            selected_port, selected_peer_port = (
                Fae(port_name=port) for port in nvl_helpers.get_linked_ports_pair(devices, engines)
            )
        else:
            # On IB (XDR) switches the connected pair is a loopback link (two switch ports
            # cabled to each other) taken from the setup connectivity map. peer-side force
            # scenarios need both ends of one link, and no-force scenarios are unaffected by
            # a loopback pair because peer-state-force defaults to enabled on both ends.
            loopback_ports = RegressionLinks.get_filtered_ports_list(setup_name, is_loopback=True)
            if not loopback_ports:
                pytest.skip(
                    f"delayed-recovery peer tests need a connected switch-switch (loopback) port "
                    f"pair; none found for setup {setup_name}")
            port_name, peer_name = next(iter(loopback_ports.items()))
            selected_port, selected_peer_port = Fae(port_name=port_name), Fae(port_name=peer_name)

        return selected_port, selected_peer_port
