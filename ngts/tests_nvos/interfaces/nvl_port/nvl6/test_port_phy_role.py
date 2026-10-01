import random
import pytest

from ngts.ngts_types import CleanUpT, DevicesT, OperationalAppliedT
from ngts.nvos_constants import constants_nvos as consts_nv
from ngts.nvos_tools.ib.InterfaceConfiguration import Port, nvos_consts as nv_consts
from ngts.nvos_tools.infra import Fae
from ngts.nvos_tools.infra import NvosTestToolkit as TestToolkit
from ngts.tests_nvos.helpers.interfaces.nvl_port.nvl6 import phy_role_helpers
from ngts.tests_nvos.interfaces.nvl_port import helpers as nvl_helpers
from ngts.tools.test_utils import allure_utils as allure


@pytest.fixture(scope="session")
def fae_objs(linked_ports_pair: tuple[str, str]) -> tuple[Fae.Fae, Fae.Fae]:
    return phy_role_helpers.get_fae_objs(linked_ports_pair)


@pytest.mark.parametrize('test_api', [random.choice(consts_nv.ApiType.ALL_TYPES)])
def test_port_phy_role(
    devices: DevicesT,
    fae_objs: tuple[Fae.Fae, Fae.Fae],
    test_api: consts_nv.ApiType,
    register_cleanup: CleanUpT,
):
    """
    Test phy role functionality on NVL ports.
    This test verifies the phy role feature on QTM4 devices, which allows a port to be configured to
    a specific role (primary, secondary, or auto). The test validates that the port's role is set correctly
    and remains stable across port state flips.
    Test Steps:
        1. Verify default phy-role and constant-role on both ports
        2. Set first port to primary role
        3. Set first port to secondary role
        4. Set first port to auto role
        5. Unset both ports
    """
    if not nvl_helpers.is_qtm4_device(devices):
        pytest.skip("Phy role is only supported on QTM4 devices")
    if phy_role_helpers.is_device_set_simplex(fae_objs):
        pytest.skip("Phy role is not supported on devices set to simplex")

    TestToolkit.TestToolkit.tested_api = test_api
    consts = consts_nv.PhyRoleConsts
    fae_port_1, fae_port_2 = fae_objs

    register_cleanup(lambda: phy_role_helpers.cleanup_phy_role(devices, fae_objs))

    with allure.step("Verify default phy-role and constant-role on both ports"):
        phy_role_helpers.verify_phy_role_default(devices, fae_port_1, fae_port_2)
    with allure.step("Set first port to primary role"):
        phy_role_helpers.role_case(
            devices,
            roles_to_set=[(fae_port_1, consts.PHY_ROLE, consts.PhyRole.PRIMARY.value)],
            expected_values=[
                (fae_port_1, consts.PHY_ROLE, consts.PhyRole.PRIMARY.value,
                 [consts.PhyRole.PRIMARY.value, consts.PhyRole.FW_DEFAULT.value]),
                (fae_port_2, consts.PHY_ROLE, consts.PhyRole.SECONDARY.value,
                 [consts.PhyRole.SECONDARY.value, consts.PhyRole.FW_DEFAULT.value]),
            ],
        )
    with allure.step("Set first port to secondary role"):
        phy_role_helpers.role_case(
            devices,
            roles_to_set=[(fae_port_1, consts.PHY_ROLE, consts.PhyRole.SECONDARY.value)],
            expected_values=[
                (fae_port_1, consts.PHY_ROLE, consts.PhyRole.SECONDARY.value,
                 [consts.PhyRole.SECONDARY.value, consts.PhyRole.FW_DEFAULT.value]),
                (fae_port_2, consts.PHY_ROLE, consts.PhyRole.PRIMARY.value,
                 [consts.PhyRole.PRIMARY.value, consts.PhyRole.FW_DEFAULT.value]),
            ],
        )
    with allure.step("Set first port to primary and second port to secondary"):
        phy_role_helpers.role_case(
            devices,
            roles_to_set=[
                (fae_port_1, consts.PHY_ROLE, consts.PhyRole.PRIMARY.value),
                (fae_port_2, consts.PHY_ROLE, consts.PhyRole.SECONDARY.value),
            ],
            expected_values=[
                (fae_port_1, consts.PHY_ROLE, consts.PhyRole.PRIMARY.value, consts.PhyRole.PRIMARY.value),
                (fae_port_2, consts.PHY_ROLE, consts.PhyRole.SECONDARY.value, consts.PhyRole.SECONDARY.value),
            ],
        )
    with allure.step("Set first port to auto role"):
        phy_role_helpers.role_case(
            devices,
            roles_to_set=[(fae_port_1, consts.PHY_ROLE, consts.PhyRole.AUTO.value)],
            expected_values=[
                (fae_port_1, consts.PHY_ROLE, consts.PhyRole.PRIMARY.value, consts.PhyRole.AUTO.value),
                (fae_port_2, consts.PHY_ROLE, consts.PhyRole.SECONDARY.value, consts.PhyRole.SECONDARY.value),
            ],
        )
    with allure.step("unset both ports"):
        for fae in fae_objs:
            fae.interface.link.unset(
                op_param=consts.PHY_ROLE, apply=True, ask_for_confirmation=True,
            ).verify_result()
        phy_role_helpers.verify_phy_role_default(devices, fae_port_1, fae_port_2)


@pytest.mark.parametrize('test_api', consts_nv.ApiType.ALL_TYPES)
def test_port_constant_role(
    devices: DevicesT,
    fae_objs: tuple[Fae.Fae, Fae.Fae],
    test_api: consts_nv.ApiType,
    register_cleanup: CleanUpT,
):
    """
    Test constant role functionality on NVL ports.
    This test verifies the constant-role feature on QTM4 devices, which allows a port to maintain
    its PHY role even after link state changes. The test validates that when constant-role is enabled,
    the port's role remains stable across port state flips.

    With phy-role set to auto on one end and left at fw-default on the other, neither end is pinned,
    so the fw is free to hand out either assignment when the link renegotiates. The roles are
    therefore observed once after constant-role is enabled and then asserted to be unchanged - which
    is what constant-role promises - rather than predicted from the values read before the change.

    Test Steps:
        1. Verify default phy-role and constant-role on both ports
        2. Set first port to auto role and enable constant-role, record the negotiated roles
        3. Verify the recorded roles stick after port state flip (down/up)
        4. Unset constant-role on both ports, verify the roles still stick
    """
    if not nvl_helpers.is_qtm4_device(devices):
        pytest.skip("Constant role is only supported on QTM4 devices")
    if phy_role_helpers.is_device_set_simplex(fae_objs):
        pytest.skip("Phy role is not supported on devices set to simplex")

    TestToolkit.TestToolkit.tested_api = test_api
    consts = consts_nv.PhyRoleConsts
    fae_port_1, fae_port_2 = fae_objs
    port_1 = Port.Port(fae_port_1.port.name)

    register_cleanup(lambda: phy_role_helpers.cleanup_phy_role(devices, fae_objs))

    with allure.step("Verify default phy-role and constant-role on both ports"):
        phy_role_helpers.verify_phy_role_default(devices, fae_port_1, fae_port_2)
        phy_role_helpers.role_case(
            devices,
            expected_values=[
                (fae, consts.CONSTANT_ROLE, consts.CONSTANT_ROLE_DEFAULT, consts.CONSTANT_ROLE_DEFAULT)
                for fae in fae_objs
            ],
        )
    with allure.step("Set first port to auto role and enable constant-role"):
        roles_after_enable = phy_role_helpers.role_case(
            devices,
            roles_to_set=[
                (fae_port_1, consts.PHY_ROLE, consts.PhyRole.AUTO.value),
                (fae_port_1, consts.CONSTANT_ROLE, consts.ConstantRole.ENABLED.value),
            ],
            expected_values=[
                # neither end is pinned, so only assert the role is a valid negotiated one
                (fae_port_1, consts.PHY_ROLE, consts.PhyRole.operational(), consts.PhyRole.AUTO.value),
                (fae_port_1, consts.CONSTANT_ROLE,
                 consts.ConstantRole.ENABLED.value, consts.ConstantRole.ENABLED.value),
                (fae_port_2, consts.PHY_ROLE, consts.PhyRole.operational(), consts.PHY_ROLE_APPLIED_DEFAULT),
                (fae_port_2, consts.CONSTANT_ROLE, consts.CONSTANT_ROLE_DEFAULT, consts.CONSTANT_ROLE_DEFAULT),
            ],
        )
        assert roles_after_enable is not None, "Roles outputs are None"
        phy_role_helpers.assert_roles_are_opposite(
            roles_after_enable, fae_port_1.port.name, fae_port_2.port.name,
        )
        # from here on the exact negotiated values are what must not change
        sticky_expectations = _pin_expectations(fae_objs, roles_after_enable)
    with allure.step("Verify role sticks after port state flip"):
        port_1.interface.link.state.set(
            nv_consts.NvosConsts.LINK_STATE_DOWN, apply=True, ask_for_confirmation=True,
        ).verify_result()
        port_1.interface.wait_for_port_state(nv_consts.NvosConsts.LINK_STATE_DOWN).verify_result()
        port_1.interface.link.state.set(
            nv_consts.NvosConsts.LINK_STATE_UP, apply=True, ask_for_confirmation=True,
        ).verify_result()
        phy_role_helpers.role_case(devices, expected_values=sticky_expectations)
    with allure.step("Unset constant-role"):
        for fae in fae_objs:
            fae.interface.link.unset(
                op_param=consts.CONSTANT_ROLE, apply=True, ask_for_confirmation=True,
            ).verify_result()
        phy_role_helpers.role_case(devices, expected_values=sticky_expectations)


def _pin_expectations(
    fae_objs: tuple[Fae.Fae, ...], roles_outputs: dict[str, OperationalAppliedT],
) -> list[tuple[Fae.Fae, str, str, str]]:
    """Turn the observed phy-role/constant-role values into expectations that must not change."""
    consts = consts_nv.PhyRoleConsts
    return [
        (fae, role,
         roles_outputs[fae.port.name][consts_nv.ConfState.OPERATIONAL][role],
         roles_outputs[fae.port.name][consts_nv.ConfState.APPLIED][role])
        for fae in fae_objs
        for role in (consts.PHY_ROLE, consts.CONSTANT_ROLE)
    ]
