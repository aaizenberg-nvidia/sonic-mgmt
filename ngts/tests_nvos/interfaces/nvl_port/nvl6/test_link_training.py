import pytest
import random

from ngts.ngts_types import CleanUpT, DevicesT
from ngts.nvos_constants import constants_nvos as consts_nv
from ngts.nvos_tools.ib.InterfaceConfiguration import Port, nvos_consts as nv_consts
from ngts.nvos_tools.infra import Fae
from ngts.nvos_tools.infra import NvosTestToolkit as TestToolkit
from ngts.nvos_tools.infra import ValidationTool
from ngts.tests_nvos.helpers.interfaces import interface_helpers
from ngts.tests_nvos.helpers.interfaces.nvl_port.nvl6 import link_training_helpers
from ngts.tests_nvos.interfaces.nvl_port import helpers
from ngts.tools.test_utils import allure_utils as allure


@pytest.fixture(scope="session")
def fae_objs(linked_ports_pair: tuple[str, str]) -> tuple[Fae.Fae, Fae.Fae]:
    return tuple(Fae.Fae(port_name=port) for port in linked_ports_pair)


@pytest.mark.parametrize('test_api', [random.choice(consts_nv.ApiType.ALL_TYPES)])
def test_fae_link_training(devices: DevicesT, fae_objs: tuple[Fae.Fae, Fae.Fae], test_api: consts_nv.ApiType, register_cleanup: CleanUpT):
    """
    Test fec-measure-mode lifecycle on NVL6 link-training (FAE path).

    Verifies the fec-measure-mode field on QTM4/NVL6 devices through the full
    configuration lifecycle. The cross-port mismatch scenario lives in
    test_fae_link_training_negotiation.

    Test Steps:
        1. Verify default link-training params on both linked ports
        2. Set fec-measure-mode to 'enabled' on both ports, verify
        3. Set fec-measure-mode to 'disabled' on both ports, verify
        4. Unset link-training on both ports, verify defaults restored (cleanup)
    """
    helpers.skip_if_fec_measure_not_supported(devices)

    TestToolkit.TestToolkit.tested_api = test_api
    port_objs = [Port.Port(fae.port.name) for fae in fae_objs]

    register_cleanup(lambda: link_training_helpers.cleanup_link_training(devices, fae_objs))

    with allure.step("Verify default link-training params on both ports"):
        with allure.step("Verify link-training params"):
            for fae in fae_objs:
                with allure.step(f"Verify port {fae.port.name}"):
                    ValidationTool.ValidationTool.compare_nested_dictionary_content(
                        fae.interface.link.link_training.parse_show_operational_applied(),
                        link_training_helpers.FAE_LINK_TRAINING_DEFAULTS,
                    ).verify_result()

    with allure.step("Set fec-measure-mode to 'enabled' on both ports"):
        for fae in fae_objs:
            fae.interface.link.link_training.set(
                op_param_name=consts_nv.LinkTrainingConsts.FEC_MEASURE_MODE,
                op_param_value=consts_nv.LinkTrainingConsts.FecMeasureMode.ENABLED.value,
                apply=True,
                ask_for_confirmation=True,
            ).verify_result()
        interface_helpers.wait_and_verify_link(port_objs, timeout=nv_consts.InternalNvosConsts.NVL6_ACP_LINK_UP_TIMEOUT_LTX_ENABLED)
        with allure.step("Verify link-training params"):
            for fae in fae_objs:
                with allure.step(f"Verify port {fae.port.name}"):
                    ValidationTool.ValidationTool.compare_nested_dictionary_content(
                        fae.interface.link.link_training.parse_show_operational_applied(),
                        {
                            consts_nv.ConfState.OPERATIONAL: {
                                consts_nv.LinkTrainingConsts.FEC_MEASURE_MODE:
                                    consts_nv.LinkTrainingConsts.FecMeasureMode.ENABLED.value,
                            },
                            consts_nv.ConfState.APPLIED: {
                                consts_nv.LinkTrainingConsts.FEC_MEASURE_MODE:
                                    consts_nv.LinkTrainingConsts.FecMeasureMode.ENABLED.value,
                            },
                        },
                    ).verify_result()

    with allure.step("Set fec-measure-mode to 'disabled' on both ports"):
        for fae in fae_objs:
            fae.interface.link.link_training.set(
                op_param_name=consts_nv.LinkTrainingConsts.FEC_MEASURE_MODE,
                op_param_value=consts_nv.LinkTrainingConsts.FecMeasureMode.DISABLED.value,
                apply=True,
                ask_for_confirmation=True,
            ).verify_result()
        interface_helpers.wait_and_verify_link(port_objs, timeout=nv_consts.InternalNvosConsts.NVL6_ACP_LINK_UP_TIMEOUT_LTX_DISABLED)
        with allure.step("Verify link-training params"):
            for fae in fae_objs:
                with allure.step(f"Verify port {fae.port.name}"):
                    ValidationTool.ValidationTool.compare_nested_dictionary_content(
                        fae.interface.link.link_training.parse_show_operational_applied(),
                        {
                            consts_nv.ConfState.OPERATIONAL: {
                                consts_nv.LinkTrainingConsts.FEC_MEASURE_MODE:
                                    consts_nv.LinkTrainingConsts.FecMeasureMode.DISABLED.value,
                            },
                            consts_nv.ConfState.APPLIED: {
                                consts_nv.LinkTrainingConsts.FEC_MEASURE_MODE:
                                    consts_nv.LinkTrainingConsts.FecMeasureMode.DISABLED.value,
                            },
                        },
                    ).verify_result()


@pytest.mark.parametrize('test_api', [random.choice(consts_nv.ApiType.ALL_TYPES)])
def test_fae_link_training_fail_action(
    devices: DevicesT,
    fae_objs: tuple[Fae.Fae, Fae.Fae],
    test_api: consts_nv.ApiType,
    register_cleanup: CleanUpT,
):
    """
    Test setting the same fec-measure-fail-action on both linked ports (FAE path).

    fec-measure-mode is enabled by default, so the ports are already in LTX - no enable
    step is needed. For each operational fail-action, set the same value on both ports and
    verify operational/applied reflect it.
    """
    helpers.skip_if_fec_measure_not_supported(devices)

    TestToolkit.TestToolkit.tested_api = test_api
    port_objs = [Port.Port(fae.port.name) for fae in fae_objs]

    register_cleanup(lambda: link_training_helpers.cleanup_link_training(devices, fae_objs))

    for action in consts_nv.LinkTrainingConsts.FecMeasureFailAction.operational():
        with allure.step(f"Set both ports to fail-action={action}"):
            for fae in fae_objs:
                fae.interface.link.link_training.set(
                    op_param_name=consts_nv.LinkTrainingConsts.FEC_MEASURE_FAIL_ACTION,
                    op_param_value=action,
                    apply=True,
                    ask_for_confirmation=True,
                ).verify_result()
            interface_helpers.wait_and_verify_link(port_objs, timeout=nv_consts.InternalNvosConsts.NVL6_ACP_LINK_UP_TIMEOUT_LTX_ENABLED)
            with allure.step("Verify link-training params"):
                for fae in fae_objs:
                    with allure.step(f"Verify port {fae.port.name}"):
                        ValidationTool.ValidationTool.compare_nested_dictionary_content(
                            fae.interface.link.link_training.parse_show_operational_applied(),
                            {
                                consts_nv.ConfState.OPERATIONAL: {
                                    consts_nv.LinkTrainingConsts.FEC_MEASURE_FAIL_ACTION: action,
                                },
                                consts_nv.ConfState.APPLIED: {
                                    consts_nv.LinkTrainingConsts.FEC_MEASURE_FAIL_ACTION: action,
                                },
                            },
                        ).verify_result()


@pytest.mark.parametrize('test_api', [random.choice(consts_nv.ApiType.ALL_TYPES)])
def test_fae_link_training_negotiation(
    devices: DevicesT,
    fae_objs: tuple[Fae.Fae, Fae.Fae],
    test_api: consts_nv.ApiType,
    register_cleanup: CleanUpT,
):
    """
    Test cross-port negotiation on NVL6 link-training (FAE path).

    fec-measure-mode is enabled by default (ports already in LTX), so no enable step is
    needed. Verifies that when the two linked ends disagree, the operational value is the
    negotiated winner. Fail-action is checked first so it runs while both ends still have
    fec-measure-mode enabled (the fec-measure-mode mismatch then disables one end):
        1. fec-measure-fail-action mismatch (two random distinct actions): operational on
           both ports = the higher-priority action.
        2. fec-measure-mode mismatch (port1 enabled, port2 disabled): operational on both
           ports = enabled.
    """
    helpers.skip_if_fec_measure_not_supported(devices)

    TestToolkit.TestToolkit.tested_api = test_api
    fae_port_1, fae_port_2 = fae_objs
    port_objs = [Port.Port(fae.port.name) for fae in fae_objs]

    register_cleanup(lambda: link_training_helpers.cleanup_link_training(devices, fae_objs))

    low_action, high_action = sorted(
        random.sample(consts_nv.LinkTrainingConsts.FecMeasureFailAction.operational(), 2),
        key=lambda fail_action: consts_nv.LinkTrainingConsts.FAIL_ACTION_PRIORITY[fail_action],
    )
    with allure.step(f"Fail-action mismatch: {fae_port_1.port.name}={low_action}, {fae_port_2.port.name}={high_action} (expect operational={high_action})"):
        fae_port_1.interface.link.link_training.set(
            op_param_name=consts_nv.LinkTrainingConsts.FEC_MEASURE_FAIL_ACTION,
            op_param_value=low_action,
            apply=True,
            ask_for_confirmation=True,
        ).verify_result()
        fae_port_2.interface.link.link_training.set(
            op_param_name=consts_nv.LinkTrainingConsts.FEC_MEASURE_FAIL_ACTION,
            op_param_value=high_action,
            apply=True,
            ask_for_confirmation=True,
        ).verify_result()
        interface_helpers.wait_and_verify_link(port_objs, timeout=nv_consts.InternalNvosConsts.NVL6_ACP_LINK_UP_TIMEOUT_LTX_ENABLED)
        with allure.step(f"Verify port {fae_port_1.port.name}"):
            ValidationTool.ValidationTool.compare_nested_dictionary_content(
                fae_port_1.interface.link.link_training.parse_show_operational_applied(),
                {
                    consts_nv.ConfState.OPERATIONAL: {consts_nv.LinkTrainingConsts.FEC_MEASURE_FAIL_ACTION: high_action},
                    consts_nv.ConfState.APPLIED: {consts_nv.LinkTrainingConsts.FEC_MEASURE_FAIL_ACTION: low_action},
                },
            ).verify_result()
        with allure.step(f"Verify port {fae_port_2.port.name}"):
            ValidationTool.ValidationTool.compare_nested_dictionary_content(
                fae_port_2.interface.link.link_training.parse_show_operational_applied(),
                {
                    consts_nv.ConfState.OPERATIONAL: {consts_nv.LinkTrainingConsts.FEC_MEASURE_FAIL_ACTION: high_action},
                    consts_nv.ConfState.APPLIED: {consts_nv.LinkTrainingConsts.FEC_MEASURE_FAIL_ACTION: high_action},
                },
            ).verify_result()

    with allure.step("fec-measure-mode mismatch: port1 'enabled', port2 'disabled' (expect operational=enabled)"):
        fae_port_1.interface.link.link_training.set(
            op_param_name=consts_nv.LinkTrainingConsts.FEC_MEASURE_MODE,
            op_param_value=consts_nv.LinkTrainingConsts.FecMeasureMode.ENABLED.value,
            apply=True,
            ask_for_confirmation=True,
        ).verify_result()
        fae_port_2.interface.link.link_training.set(
            op_param_name=consts_nv.LinkTrainingConsts.FEC_MEASURE_MODE,
            op_param_value=consts_nv.LinkTrainingConsts.FecMeasureMode.DISABLED.value,
            apply=True,
            ask_for_confirmation=True,
        ).verify_result()
        interface_helpers.wait_and_verify_link(port_objs, timeout=nv_consts.InternalNvosConsts.NVL6_ACP_LINK_UP_TIMEOUT_LTX_ENABLED)
        with allure.step(f"Verify port {fae_port_1.port.name}"):
            ValidationTool.ValidationTool.compare_nested_dictionary_content(
                fae_port_1.interface.link.link_training.parse_show_operational_applied(),
                {
                    consts_nv.ConfState.OPERATIONAL: {
                        consts_nv.LinkTrainingConsts.FEC_MEASURE_MODE:
                            consts_nv.LinkTrainingConsts.FecMeasureMode.ENABLED.value,
                    },
                    consts_nv.ConfState.APPLIED: {
                        consts_nv.LinkTrainingConsts.FEC_MEASURE_MODE:
                            consts_nv.LinkTrainingConsts.FecMeasureMode.ENABLED.value,
                    },
                },
            ).verify_result()
        with allure.step(f"Verify port {fae_port_2.port.name}"):
            ValidationTool.ValidationTool.compare_nested_dictionary_content(
                fae_port_2.interface.link.link_training.parse_show_operational_applied(),
                {
                    consts_nv.ConfState.OPERATIONAL: {
                        consts_nv.LinkTrainingConsts.FEC_MEASURE_MODE:
                            consts_nv.LinkTrainingConsts.FecMeasureMode.ENABLED.value,
                    },
                    consts_nv.ConfState.APPLIED: {
                        consts_nv.LinkTrainingConsts.FEC_MEASURE_MODE:
                            consts_nv.LinkTrainingConsts.FecMeasureMode.DISABLED.value,
                    },
                },
            ).verify_result()


@pytest.mark.parametrize('test_api', [random.choice(consts_nv.ApiType.ALL_TYPES)])
def test_link_training_force_max_iterations(
    devices: DevicesT, fae_objs: tuple[Fae.Fae, Fae.Fae], test_api: consts_nv.ApiType, register_cleanup: CleanUpT,
):
    """
    Verify the non-FAE force-max-iterations knob can be configured on both linked ports.

    force-max-iterations is set on the regular interface path (not under fae):
        nv set interface <intf-id> link link-training force-max-iterations <disabled|enabled>

    Steps:
        1. fec-measure-mode is enabled by default, so the ports are already in LTX - no
           enable step is needed.
        2. Verify force-max-iterations default on both ports.
        3. For each force-max-iterations value, set it on both ports (non-FAE), wait for
           link-up, and verify operational/applied reflect the configured value.
           force-max-iterations=enabled runs all link-training iterations, so link-up
           can take up to NVL6_ACP_LINK_UP_TIMEOUT_LTX_ENABLED (375s).
    """
    helpers.skip_if_fec_measure_not_supported(devices)

    TestToolkit.TestToolkit.tested_api = test_api
    port_objs = [Port.Port(fae.port.name) for fae in fae_objs]

    register_cleanup(lambda: link_training_helpers.cleanup_link_training(devices, fae_objs))

    with allure.step("Verify force-max-iterations default on both ports"):
        for port in port_objs:
            with allure.step(f"Verify port {port.name}"):
                ValidationTool.ValidationTool.compare_nested_dictionary_content(
                    port.interface.link.link_training.parse_show_operational_applied(),
                    {
                        consts_nv.ConfState.OPERATIONAL: {
                            consts_nv.LinkTrainingConsts.FORCE_MAX_ITERATIONS:
                                consts_nv.LinkTrainingConsts.FORCE_MAX_ITERATIONS_OPERATIONAL_DEFAULT.value,
                        },
                        consts_nv.ConfState.APPLIED: {
                            consts_nv.LinkTrainingConsts.FORCE_MAX_ITERATIONS:
                                consts_nv.LinkTrainingConsts.FORCE_MAX_ITERATIONS_APPLIED_DEFAULT.value,
                        },
                    },
                ).verify_result()

    for value in consts_nv.LinkTrainingConsts.ForceMaxIterations.all():
        with allure.step(f"Set force-max-iterations={value} on both ports"):
            for port in port_objs:
                port.interface.link.link_training.set(
                    op_param_name=consts_nv.LinkTrainingConsts.FORCE_MAX_ITERATIONS,
                    op_param_value=value,
                    apply=True,
                    ask_for_confirmation=True,
                ).verify_result()
            link_up_timeout = nv_consts.InternalNvosConsts.NVL6_ACP_LINK_UP_TIMEOUT_LTX_ENABLED
            additional_diag = (
                nv_consts.IbInterfaceConsts.LINK_DIAGNOSTICS_LOW_BER
                if value == consts_nv.LinkTrainingConsts.ForceMaxIterations.ENABLED.value
                else None
            )
            interface_helpers.wait_and_verify_link(
                port_objs,
                timeout=link_up_timeout,
                additional_expected_link_diagnostic_status=additional_diag,
            )
            with allure.step("Verify link-training params"):
                for port in port_objs:
                    with allure.step(f"Verify port {port.name}"):
                        ValidationTool.ValidationTool.compare_nested_dictionary_content(
                            port.interface.link.link_training.parse_show_operational_applied(),
                            {
                                consts_nv.ConfState.OPERATIONAL: {
                                    consts_nv.LinkTrainingConsts.FORCE_MAX_ITERATIONS: value,
                                },
                                consts_nv.ConfState.APPLIED: {
                                    consts_nv.LinkTrainingConsts.FORCE_MAX_ITERATIONS: value,
                                },
                            },
                        ).verify_result()
