import logging
import random
from contextlib import contextmanager
import pytest

from ngts.tools.test_utils import allure_utils as allure
from ngts.nvos_tools.nmx.Cluster import Cluster
from ngts.nvos_tools.nmx.Sdn import Sdn
from ngts.nvos_tools.platform.Platform import Platform
from ngts.nvos_constants.constants_nvos import OutputFormat
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_constants.constants_nvos import ChassisLocationConsts
from ngts.tests_nvos.cluster.cluster_consts import ClusterConsts
from ngts.tests_nvos.cluster.cluster_tools import ClusterTools, disabled_access_ports
from ngts.tests_nvos.constants import MINUTE
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit


logger = logging.getLogger()

# Error message constants for tray maintenance state tests
# NMX-C reports a non-existent/out-of-range tray id as NMX_ST_BADPARAM
# (it does not return a distinct "not found" code), so this is the expected
# error for any invalid tray-id failure.
ERR_NMX_TRAY_ID_BADPARAM = "NMX_ST_BADPARAM"
ERR_INVALID_TRAY_ID = "is not a 'sdn-tray-id'"


@pytest.fixture(scope="module", autouse=True)
def skip_if_standalone(standalone_system):
    """Skip tests if system is standalone (requires non-standalone system for single aperture support)."""
    if standalone_system:
        pytest.skip("Single aperture support tests require non-standalone system (mini-oberon setup)")


@contextmanager
def cluster_enabled(setup_name, devices):
    """Enable cluster and wait for NMX-C, then disable on exit."""
    cluster = Cluster()
    with allure.step("Enable cluster and wait for NMX-C to be ready"):
        ClusterTools.start_cluster(cluster, setup_name, OutputFormat.json, devices=devices)
        ClusterTools.wait_until_app_expected_status(cluster, ClusterConsts.NMX_CONTROLLER, 'ok')
    try:
        yield cluster
    finally:
        with allure.step("Disable cluster after test"):
            ClusterTools.stop_cluster(cluster, OutputFormat.json)


@pytest.fixture(scope="module")
def get_chassis_info():
    """
    Fixture to get chassis serial number and slot ID.
    First tries to get from 'nv show platform chassis-location'.
    If not available, generates chassis_mapping config and extracts from there.

    Returns:
        dict: {
            'slot-number': str,
            'chassis-sn': str or None,
        }
    """
    chassis_info = {
        ChassisLocationConsts.SLOT_NUM: None,
        ChassisLocationConsts.CHAS_SN: None,
    }

    with allure.step("Get chassis location from 'nv show platform chassis-location'"):
        platform = Platform()
        chassis_location_output = OutputParsingTool.parse_show_output_to_dict(
            platform.chassis_location.show()).get_returned_value()
        chassis_info[ChassisLocationConsts.SLOT_NUM] = chassis_location_output.get(ChassisLocationConsts.SLOT_NUM)
        chassis_info[ChassisLocationConsts.CHAS_SN] = chassis_location_output.get(ChassisLocationConsts.CHAS_SN)
    return chassis_info


@disabled_access_ports
@pytest.mark.nmx
@pytest.mark.timeout(15 * MINUTE, func_only=True)
def test_update_maintenance_state_up(engines, devices, random_api, get_chassis_info, setup_name):
    """
    Test Objective:
    Verify that a switch tray on the local chassis can be brought back to maintenance-state up using the
    simplified command syntax (without chassis-sn).

    Precondition:
    - Cluster state enabled and NMX-C is running
    - Non standalone system
    """
    TestToolkit.tested_api = random_api

    with cluster_enabled(setup_name, devices):
        slot_number = get_chassis_info[ChassisLocationConsts.SLOT_NUM]

        with allure.step("Create Sdn object"):
            sdn = Sdn()

        trays_output = OutputParsingTool.parse_show_output_to_dict(
            sdn.trays.show()).get_returned_value()
        tray_ids = list(trays_output.keys())
        # Only switch trays support maintenance actions — compute trays reject them
        # ('Tray admin state is not supported'). This test keeps to switch trays.
        remote_tray_ids = _filter_switch_tray_candidates(
            [tray_id for tray_id in tray_ids if str(tray_id) != str(slot_number)], trays_output)
        if remote_tray_ids:
            remote_tray_id = random.choice(remote_tray_ids)
            with allure.step("Run 'nv action update sdn trays <slot-id> maintenance-state up' for same chassis different tray"):
                sdn.trays.action_update_maintenance_state(tray_id=remote_tray_id, maintenance_state='up').verify_result()

        with allure.step("Run 'nv action update sdn trays <slot-number> maintenance-state up' for local tray"):
            sdn.trays.action_update_maintenance_state(tray_id=slot_number, maintenance_state='up').verify_result()


@disabled_access_ports
@pytest.mark.nmx
@pytest.mark.timeout(15 * MINUTE, func_only=True)
def test_error_flow_single_aperture_support(engines, devices, random_api, get_chassis_info, setup_name):
    """
    Test Objective:
    Verify proper error handling for invalid slot ID and / or invalid chassis-sn

    Precondition:
    - Cluster state enabled and NMX-C is running
    - Non standalone system
    """
    TestToolkit.tested_api = random_api
    output_format = OutputFormat.json

    with cluster_enabled(setup_name, devices) as cluster:
        chassis_sn = get_chassis_info[ChassisLocationConsts.CHAS_SN]

        with allure.step("Create Sdn object"):
            sdn = Sdn()

        with allure.step("Verify cluster state is enabled and NMX-C is running"):
            ClusterTools.validate_cluster_enabled(cluster, output_format)

        with allure.step("Verify bad flow commands for single aperture support"):
            with allure.independent_step("Attempt with non-existent slot (999)"):
                sdn.trays.action_update_maintenance_state(tray_id='999').verify_result(should_succeed=False, expected_value=ERR_NMX_TRAY_ID_BADPARAM)

            with allure.independent_step("Attempt with negative slot (-1)"):
                sdn.trays.action_update_maintenance_state(tray_id='-1').verify_result(should_succeed=False, expected_value=ERR_INVALID_TRAY_ID)

            with allure.independent_step(f"Attempt with fake slot number ({chassis_sn}.aaa)"):
                sdn.trays.action_update_maintenance_state(tray_id=f"{chassis_sn}.aaa").verify_result(should_succeed=False, expected_value=ERR_INVALID_TRAY_ID)

            with allure.independent_step("Attempt with chassis SN without slot"):
                sdn.trays.action_update_maintenance_state(tray_id=chassis_sn).verify_result(should_succeed=False, expected_value=ERR_NMX_TRAY_ID_BADPARAM)

            with allure.independent_step("Attempt with a invalid format of the slot number (.1)"):
                sdn.trays.action_update_maintenance_state(tray_id='.1').verify_result(should_succeed=False, expected_value=ERR_INVALID_TRAY_ID)

        with allure.step("Verify NMX-C is still running after all error tests"):
            ClusterTools.validate_cluster_enabled(cluster, output_format)


def _filter_switch_tray_candidates(tray_ids, trays_output):
    """Drop compute trays — only switch trays support maintenance actions.

    Since the 6623822 fix (0.8 >= 25.03.0722) the trays view lists compute trays with
    maintenance-state 'N/A' (update/restore on them are rejected with the dedicated
    'Tray admin state is not supported' message), so the view itself tells the tray
    types apart — no partition parsing needed. Compute-tray rejection coverage lives
    in tray_admin_state's test_tray_bad_params.
    """
    return [
        tray_id for tray_id in tray_ids
        if trays_output.get(str(tray_id), {}).get(ClusterConsts.MAINTENANCE_STATE) !=
        ClusterConsts.COMPUTE_TRAY_MAINTENANCE_STATE]
