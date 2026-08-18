from ngts.ngts_types import DevicesT, OperationalAppliedT
from ngts.nvos_constants import constants_nvos as consts_nv
from ngts.nvos_tools.ib.InterfaceConfiguration import Port, nvos_consts as nv_consts
from ngts.nvos_tools.infra import Fae
from ngts.nvos_tools.infra import ValidationTool
from ngts.tests_nvos.helpers.interfaces import interface_helpers
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


def cleanup_link_training(devices: DevicesT, fae_objs: tuple[Fae.Fae, ...]) -> None:
    with allure.step("Cleanup: unset link-training component, wait for link, verify defaults"):
        ports_objs: list[Port.Port] = [Port.Port(fae.port.name) for fae in fae_objs]
        for fae, port in zip(fae_objs, ports_objs):
            fae.interface.link.link_training.unset(apply=True, ask_for_confirmation=True).verify_result()
            # force-max-iterations is a non-FAE knob, so it must be reverted via the regular Port path
            port.interface.link.link_training.unset(apply=True, ask_for_confirmation=True).verify_result()
        interface_helpers.wait_and_verify_link(ports_objs, timeout=devices.dut.expected_operation_durations.get(nv_consts.InternalNvosConsts.ACP_PORT_GOES_UP))
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
