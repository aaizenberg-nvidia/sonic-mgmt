from typing import Optional, Union

from ngts.ngts_types import DevicesT, OperationalAppliedT
from ngts.nvos_constants import constants_nvos as consts_nv
from ngts.nvos_tools.ib.InterfaceConfiguration import Port
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import IbInterfaceConsts
from ngts.nvos_tools.infra import Fae, ValidationTool
from ngts.tests_nvos.helpers.interfaces import interface_helpers
from ngts.tests_nvos.interfaces.nvl_port import helpers as nvl_helpers
from ngts.tools.test_utils import allure_utils as allure

# phy-role/constant-role are plain fields under `link`, so both revs come from one parse of the
# whole link component rather than from a dedicated sub-component.
RoleT = Union[str, list[str]]

# a port that is down or still training reports no role; the fw publishes it once the link is up
NO_ROLE_VALUES: tuple[Optional[str], ...] = (None, consts_nv.NvosConst.NOT_AVAILABLE)


def get_fae_objs(linked_ports_pair: tuple[str, str]) -> tuple[Fae.Fae, Fae.Fae]:
    return tuple(Fae.Fae(port_name=port) for port in linked_ports_pair)


def parse_roles(fae: Fae.Fae) -> OperationalAppliedT:
    """Read the link component of a port in both revs; phy-role/constant-role are fields on it."""
    return fae.interface.link.parse_show_operational_applied()


def verify_roles(fae: Fae.Fae, expected: OperationalAppliedT) -> None:
    """Verify the port's link output contains `expected` in both revs."""
    with allure.step(f"Verify roles on {fae.port.name}"):
        ValidationTool.ValidationTool.compare_nested_dictionary_content(
            parse_roles(fae), expected,
        ).verify_result()


def wait_for_links(devices: DevicesT, fae_objs: list[Fae.Fae]) -> None:
    """
    Wait for every given port to link up, then verify its diagnostics.

    Setting a role on one end flaps both ends of the link, so every port that is about to be read
    must be waited on - not just the ones that were configured. Reading a role from a port that is
    still down yields 'N/A'.
    """
    unique_names: list[str] = []
    for fae in fae_objs:
        if fae.port.name not in unique_names:
            unique_names.append(fae.port.name)
    interface_helpers.wait_and_verify_link(
        [Port.Port(name) for name in unique_names],
        timeout=nvl_helpers.acp_link_up_timeout(devices),
    )


def is_device_set_simplex(fae_objs: tuple[Fae.Fae, Fae.Fae]) -> bool:
    """Whether the linked pair is running in simplex, where the fw reports no phy-role."""
    try:
        nvl_helpers.validate_ports_state_and_speed(
            IbInterfaceConsts.XDR_SLOW_SPEED, [fae.port.name for fae in fae_objs], "acp",
        )
    except AssertionError:
        return False
    for fae in fae_objs:
        roles: OperationalAppliedT = parse_roles(fae)
        for rev in (consts_nv.ConfState.OPERATIONAL, consts_nv.ConfState.APPLIED):
            value = roles[rev].get(consts_nv.PhyRoleConsts.PHY_ROLE)
            assert value in NO_ROLE_VALUES, (
                f"Port {fae.port.name} {rev} {consts_nv.PhyRoleConsts.PHY_ROLE} value is {value} "
                f"- expected no role while the device is set to simplex"
            )
    return True


def role_case(
    devices: DevicesT,
    roles_to_set: Optional[list[tuple[Fae.Fae, str, str]]] = None,
    expected_values: Optional[list[tuple[Fae.Fae, str, RoleT, RoleT]]] = None,
) -> Optional[dict[str, OperationalAppliedT]]:
    """
    Set roles on the given ports and/or verify the resulting operational+applied values.

    :param roles_to_set: (fae, role name, value) triplets to configure, applied one by one.
    :param expected_values: (fae, role name, expected operational, expected applied) tuples. Either
        expectation may be a list, meaning "any of these".
    :return: the parsed link output per port name, or None when there was nothing to verify.
    """
    if roles_to_set is not None:
        for fae, role, value in roles_to_set:
            with allure.step(f"Set {role} to {value} on {fae.port.name}"):
                fae.interface.link.set(
                    op_param_name=role, op_param_value=value, apply=True, ask_for_confirmation=True,
                ).verify_result()
    if expected_values is None:
        if roles_to_set is not None:
            wait_for_links(devices, [fae for fae, _, _ in roles_to_set])
        return None

    # wait on every port that was touched or is about to be read, not just the configured ones
    involved: list[Fae.Fae] = [fae for fae, _, _ in (roles_to_set or [])]
    involved += [fae for fae, _, _, _ in expected_values]
    wait_for_links(devices, involved)

    with allure.step("Get ports roles output"):
        roles_outputs: dict[str, OperationalAppliedT] = {}
        for fae, _, _, _ in expected_values:
            if fae.port.name not in roles_outputs:
                roles_outputs[fae.port.name] = parse_roles(fae)
    for fae, role, expected_operational, expected_applied in expected_values:
        with allure.step(f"Verify {role} output on {fae.port.name}"):
            _verify_role(
                roles_outputs[fae.port.name], fae.port.name, role, expected_operational, expected_applied,
            )
    return roles_outputs


def verify_phy_role_default(devices: DevicesT, fae_port_1: Fae.Fae, fae_port_2: Fae.Fae) -> None:
    """Verify both ports carry a default phy-role, and that the two ends negotiated opposite roles."""
    consts = consts_nv.PhyRoleConsts
    with allure.step("Verify default phy-role on both ports"):
        roles_outputs = role_case(
            devices,
            expected_values=[
                (fae, consts.PHY_ROLE, list(consts.PHY_ROLE_OPERATIONAL_DEFAULT_VALUES),
                 consts.PHY_ROLE_APPLIED_DEFAULT)
                for fae in (fae_port_1, fae_port_2)
            ],
        )
        assert roles_outputs is not None, "Roles outputs are None"
        assert_roles_are_opposite(roles_outputs, fae_port_1.port.name, fae_port_2.port.name)


def assert_roles_are_opposite(
    roles_outputs: dict[str, OperationalAppliedT], port_name_1: str, port_name_2: str,
) -> None:
    """Verify the two link ends negotiated opposite operational phy-roles."""
    phy_role = consts_nv.PhyRoleConsts.PHY_ROLE
    role_1 = roles_outputs[port_name_1][consts_nv.ConfState.OPERATIONAL][phy_role]
    role_2 = roles_outputs[port_name_2][consts_nv.ConfState.OPERATIONAL][phy_role]
    assert role_1 != role_2, (
        f"Both ports operational {phy_role} value is {role_1} "
        f"- expected the two link ends to negotiate opposite roles"
    )


def cleanup_phy_role(devices: DevicesT, fae_objs: tuple[Fae.Fae, ...]) -> None:
    """Unset phy-role and constant-role on every port, wait for the links, and verify the defaults."""
    consts = consts_nv.PhyRoleConsts
    with allure.step("Cleanup: unset phy-role and constant-role, wait for link, verify defaults"):
        for fae in fae_objs:
            for role in (consts.PHY_ROLE, consts.CONSTANT_ROLE):
                fae.interface.link.unset(op_param=role, apply=True, ask_for_confirmation=True).verify_result()
        verify_phy_role_default(devices, *fae_objs)
        for fae in fae_objs:
            verify_roles(fae, {
                consts_nv.ConfState.OPERATIONAL: {consts.CONSTANT_ROLE: consts.CONSTANT_ROLE_DEFAULT},
                consts_nv.ConfState.APPLIED: {consts.CONSTANT_ROLE: consts.CONSTANT_ROLE_DEFAULT},
            })


def _verify_role(
    roles: OperationalAppliedT, port_name: str, role: str, expected_operational: RoleT, expected_applied: RoleT,
) -> None:
    # asserted directly rather than through compare_nested_dictionary_content, whose failure message
    # names only the field - not the port, the actual value or the expected one
    for rev, expected in (
        (consts_nv.ConfState.OPERATIONAL, expected_operational),
        (consts_nv.ConfState.APPLIED, expected_applied),
    ):
        allowed = expected if isinstance(expected, list) else [expected]
        actual = roles[rev].get(role)
        assert actual in allowed, (
            f"Port {port_name} {rev} {role} value is {actual} - expected "
            f"{f'one of {allowed}' if len(allowed) > 1 else allowed[0]}"
        )
