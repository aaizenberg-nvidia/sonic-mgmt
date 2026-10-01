from ngts.ngts_types import DevicesT, OperationalAppliedT
from ngts.nvos_constants import constants_nvos as consts_nv
from ngts.nvos_tools.ib.InterfaceConfiguration import Port, nvos_consts as nv_consts
from ngts.nvos_tools.infra import Fae
from ngts.nvos_tools.infra import ValidationTool
from ngts.tests_nvos.helpers import redmine_helpers
from ngts.tests_nvos.helpers.interfaces import interface_helpers
from ngts.tests_nvos.interfaces.nvl_port import helpers as nvl_port_helpers
from ngts.tools.test_utils import allure_utils as allure

FAE_LINK_TRAINING_DEFAULTS: OperationalAppliedT = {
    consts_nv.ConfState.OPERATIONAL: {
        consts_nv.LinkTrainingConsts.FEC_MEASURE_MODE: consts_nv.LinkTrainingConsts.FEC_MEASURE_MODE_OPERATIONAL_DEFAULT.value,
        consts_nv.LinkTrainingConsts.FEC_MEASURE_FAIL_ACTION: consts_nv.LinkTrainingConsts.FEC_MEASURE_FAIL_ACTION_OPERATIONAL_DEFAULT.value,
    },
    consts_nv.ConfState.APPLIED: {
        consts_nv.LinkTrainingConsts.FEC_MEASURE_MODE: consts_nv.LinkTrainingConsts.FEC_MEASURE_MODE_APPLIED_DEFAULT.value,
        consts_nv.LinkTrainingConsts.FEC_MEASURE_FAIL_ACTION: consts_nv.LinkTrainingConsts.FEC_MEASURE_FAIL_ACTION_APPLIED_DEFAULT.value,
    },
}

LINK_TRAINING_DEFAULTS: OperationalAppliedT = {
    consts_nv.ConfState.OPERATIONAL: {
        consts_nv.LinkTrainingConsts.FORCE_MAX_ITERATIONS: consts_nv.LinkTrainingConsts.FORCE_MAX_ITERATIONS_OPERATIONAL_DEFAULT.value,
    },
    consts_nv.ConfState.APPLIED: {
        consts_nv.LinkTrainingConsts.FORCE_MAX_ITERATIONS: consts_nv.LinkTrainingConsts.FORCE_MAX_ITERATIONS_APPLIED_DEFAULT.value,
    },
}


def get_access_ports_range(devices: DevicesT) -> Port.Port:
    """
    All access ports of the DUT as a single Port range object, e.g. Port('acp1-288').

    Same construction as the `access_ports` fixture in ngts/tests_nvos/interfaces/conftest.py, for
    callers that have no access to that fixture - e.g. the upgrade feature checkers.
    """
    acp: str = nv_consts.NvlInterfaceConsts.ACP_PORT_TYPE
    port_indices: list[int] = [int(name.replace(acp, '')) for name in devices.dut.nvl_access_ports_list]
    return Port.Port(f'{acp}{min(port_indices)}-{max(port_indices)}')


def cleanup_link_training(devices: DevicesT, access_ports: Port.Port, fae_objs: tuple[Fae.Fae, ...],
                          down_access_ports: list[str]) -> None:
    """
    Restore the link-training defaults on the whole access-port range and verify the links recover.

    :param access_ports: the access-port range object, e.g. Port('acp1-288')
    :param down_access_ports: names of the ports that were already down BEFORE the test ran. The
        snapshot has to be taken by the caller: read here it would be taken after the test already
        mutated the range, so a port the test itself broke would be excluded from its own recovery
        wait. Ports with no link partner never come up and would otherwise hold the budget open.
    """
    with allure.step(f"Cleanup: unset link-training on {access_ports.name}, wait for link, verify defaults"):
        # Link-training is programmed per ASIC, so a stale value on any port sharing an ASIC with
        # the tested pair bleeds into the pair on its next retrain. Unset the whole access-port
        # range rather than only the tested ports - NVUE takes 'acp1-288' as one interface token,
        # so this is one unset + one apply per path, not one per port.
        Fae.Fae(port_name=access_ports.name).interface.link.link_training.unset(
            apply=True, ask_for_confirmation=True).verify_result()
        # force-max-iterations is a non-FAE knob, so it must be reverted via the regular Port path
        access_ports.interface.link.link_training.unset(
            apply=True, ask_for_confirmation=True).verify_result()

        timeout: int = devices.dut.expected_operation_durations.get(
            nv_consts.InternalNvosConsts.ACP_PORT_GOES_UP)

        if redmine_helpers.is_bug_active(5266643):
            access_ports.interface.link.state.set(
                op_param_name=nv_consts.NvosConsts.LINK_STATE_DOWN,
                apply=True, ask_for_confirmation=True).verify_result()
            nvl_port_helpers.wait_for_acp_ports_state(
                access_ports, nv_consts.NvosConsts.LINK_STATE_DOWN, timeout=timeout, exclude_ports=down_access_ports)
            access_ports.interface.link.state.set(
                op_param_name=nv_consts.NvosConsts.LINK_STATE_UP,
                apply=True, ask_for_confirmation=True).verify_result()

        ports_objs: list[Port.Port] = [Port.Port(fae.port.name) for fae in fae_objs]
        # Wait broadly, verify diagnostics narrowly: the wait covers every port in the range from
        # one show per poll, while diagnostics are read per port and so are checked on the tested
        # pair only.
        interface_helpers.wait_and_verify_link_on_range(
            access_ports,
            ports_objs,
            timeout=timeout,
            exclude_ports=down_access_ports,
        )
        with allure.step("Verify link-training params"):
            for fae in fae_objs:
                with allure.step(f"Verify port {fae.port.name}"):
                    actual: OperationalAppliedT = fae.interface.link.link_training.parse_show_operational_applied()
                    ValidationTool.ValidationTool.compare_nested_dictionary_content(
                        actual, FAE_LINK_TRAINING_DEFAULTS,
                    ).verify_result()
            for port in ports_objs:
                with allure.step(f"Verify port {port.name}"):
                    actual: OperationalAppliedT = port.interface.link.link_training.parse_show_operational_applied()
                    ValidationTool.ValidationTool.compare_nested_dictionary_content(
                        actual, LINK_TRAINING_DEFAULTS,
                    ).verify_result()
