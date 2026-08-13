from typing import Optional

import pytest

from ngts.ngts_types import DevicesT, OperationalAppliedT
from ngts.nvos_constants import constants_nvos as consts_nv
from ngts.nvos_tools.ib.InterfaceConfiguration import Port
from ngts.nvos_tools.infra import BaseComponent, Fae, ValidationTool
from ngts.tests_nvos.helpers.interfaces import interface_helpers
from ngts.tests_nvos.helpers.redmine_helpers import is_bug_active
from ngts.tests_nvos.interfaces.nvl_port import helpers as nvl_helpers
from ngts.tools.test_utils import allure_utils as allure


def expected_operational_applied(values: dict[str, str]) -> OperationalAppliedT:
    """Build an OperationalAppliedT where both revs are expected to carry the same values."""
    return {
        consts_nv.ConfState.OPERATIONAL: dict(values),
        consts_nv.ConfState.APPLIED: dict(values),
    }


LOW_POWER_STATE_DEFAULTS: OperationalAppliedT = expected_operational_applied(
    {consts_nv.NvosConst.STATE: consts_nv.LowPowerConsts.STATE_DEFAULT},
)


def fae_low_power_defaults() -> OperationalAppliedT:
    """Expected fae low-power params when nothing is configured."""
    return expected_operational_applied(
        {name: field["default"] for name, field in consts_nv.LowPowerConsts.FAE_FIELDS.items()},
    )


def agreed_function_expected(set_dict: dict[str, str]) -> dict[str, str]:
    """
    The values the fw agrees on when the peer end is left at its defaults.

    With peq-recal-force-period=use-agreed-function the two link ends negotiate: the shorter
    peq-duration and the longer peq-recal-period win.
    """
    consts = consts_nv.LowPowerConsts
    expected: dict[str, str] = {}
    if consts.PEC_RECAL_FORCE_PERIOD in set_dict:
        expected[consts.PEC_RECAL_FORCE_PERIOD] = set_dict[consts.PEC_RECAL_FORCE_PERIOD]
    if consts.PEC_DURATION in set_dict:
        expected[consts.PEC_DURATION] = min(
            set_dict[consts.PEC_DURATION], consts.FAE_FIELDS[consts.PEC_DURATION]["default"],
        )
    if consts.PEC_RECAL_PERIOD in set_dict:
        expected[consts.PEC_RECAL_PERIOD] = max(
            set_dict[consts.PEC_RECAL_PERIOD], consts.FAE_FIELDS[consts.PEC_RECAL_PERIOD]["default"],
        )
    return expected


def should_skip_if_low_power_not_supported() -> bool:
    return is_bug_active(4831699) or is_bug_active(4850191)


def get_linked_ports_objs(devices: DevicesT, linked_ports_pair: tuple[str, str]) -> tuple[Port.Port, Port.Port]:
    """
    Return the port objects carrying the low-power knobs for a linked pair.

    QTM3 exposes low-power only under the fae path, so the port is reached through `Fae.port`,
    while QTM4 uses the regular interface path.
    """
    if nvl_helpers.is_qtm3_device(devices):
        return tuple(Fae.Fae(port_name=port).port for port in linked_ports_pair)
    elif nvl_helpers.is_qtm4_device(devices):
        return tuple(Port.Port(name=port) for port in linked_ports_pair)
    else:
        pytest.fail("Unsupported device type - expected a QTM3 or QTM4 NVL device")


def verify_low_power(low_power_component: BaseComponent.BaseComponent, expected: OperationalAppliedT) -> None:
    """Verify the low-power component's operational+applied output contains `expected`."""
    with allure.step(f"Verify low power params of {low_power_component.get_resource_path()}"):
        ValidationTool.ValidationTool.compare_nested_dictionary_content(
            low_power_component.parse_show_operational_applied(), expected,
        ).verify_result()


def low_power_state_case(
    devices: DevicesT,
    ports_to_set: Optional[list[tuple[Port.Port, str]]] = None,
    expected_values: Optional[list[tuple[Port.Port, str, str]]] = None,
) -> None:
    """
    Set the low-power state on the given ports and/or verify the resulting operational+applied state.

    :param ports_to_set: (port, state) pairs to configure, applied one by one.
    :param expected_values: (port, expected operational, expected applied) triplets to verify.
    """
    if ports_to_set is not None:
        for port, value in ports_to_set:
            with allure.step(f"Set {consts_nv.NvosConst.STATE} to {value} on {port.name}"):
                port.interface.link.low_power.set(
                    op_param_name=consts_nv.NvosConst.STATE,
                    op_param_value=value,
                    apply=True,
                    ask_for_confirmation=True,
                ).verify_result()
        _wait_and_verify(devices, [port for port, _ in ports_to_set])
    if expected_values is not None:
        if ports_to_set is None:
            # verify-only call: nothing was toggled, so check the ports being asserted on
            _wait_and_verify(devices, [port for port, _, _ in expected_values])
        with allure.step("Verify low power states"):
            for port, expected_operational, expected_applied in expected_values:
                with allure.step(f"Verify port {port.name}"):
                    verify_low_power(
                        port.interface.link.low_power,
                        {
                            consts_nv.ConfState.OPERATIONAL: {consts_nv.NvosConst.STATE: expected_operational},
                            consts_nv.ConfState.APPLIED: {consts_nv.NvosConst.STATE: expected_applied},
                        },
                    )


def cleanup_low_power(devices: DevicesT, ports: tuple[Port.Port, ...]) -> None:
    """Unset the low-power component on every port, wait for the links, and verify the defaults."""
    with allure.step("Cleanup: unset low-power component, wait for link, verify defaults"):
        for port in ports:
            port.interface.link.low_power.unset(apply=True, ask_for_confirmation=True).verify_result()
        _wait_and_verify(devices, list(ports))
        for port in ports:
            with allure.step(f"Verify port {port.name}"):
                verify_low_power(port.interface.link.low_power, LOW_POWER_STATE_DEFAULTS)


def _wait_and_verify(devices: DevicesT, ports: list[Port.Port]) -> None:
    """Wait for the ports to link up and verify their diagnostics, skipping repeated ports."""
    unique_ports, seen = [], set()
    for port in ports:
        if port.name not in seen:
            seen.add(port.name)
            unique_ports.append(port)
    interface_helpers.wait_and_verify_link(
        unique_ports, timeout=nvl_helpers.acp_link_up_timeout(devices),
    )
