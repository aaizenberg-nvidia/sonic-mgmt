import pytest
import logging

from ngts.nvos_tools.Devices.IbDevice import RosalindSwitch
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port
from ngts.nvos_tools.nmx.Cluster import Cluster
from ngts.nvos_tools.nmx.Sdn import Sdn
from ngts.nvos_constants.constants_nvos import LinkTrainingConsts, OutputFormat
from ngts.tests_nvos.cluster.cluster_tools import ClusterTools, summarize_ports
from ngts.tests_nvos.cluster.cluster_consts import ClusterConsts
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger()


@pytest.fixture(scope="module")
def cluster_and_sdn(setup_name, devices, standalone_system):
    """Enable cluster, yield (cluster, sdn), then disable cluster."""
    if standalone_system:
        pytest.skip("Tray admin state tests require non-standalone setup (multi-switch rack)")
    cluster = Cluster()
    sdn = Sdn()
    ClusterTools.start_cluster(cluster, setup_name, OutputFormat.json, devices=devices)
    ClusterTools.wait_until_app_expected_status(cluster, ClusterConsts.NMX_CONTROLLER, 'ok')
    yield cluster, sdn
    cluster.unset(apply=True)
    ClusterTools.wait_for_apps_to_be_in_wanted_state(
        cluster, cluster_expected_state='disabled', nmx_c_expected_state='down')


# chassis_info and tray_topology_config live in ngts/tests_nvos/cluster/conftest.py
# so that cluster-level tests (e.g. test_cluster_reboot) can use them too.
@pytest.fixture(scope="module")
def expected_tray_count(tray_topology_config):
    """
    Expected len(nv show sdn trays) == switch_nodes from lab config.

    Since 0.8 NMX-C filters compute trays out of the trays view entirely (compute-tray
    admin state is not released to customers), so only switch trays are listed —
    observed on rtf2: compute trays 1/2 hidden, switch tray 3 shown, while the
    partition view still shows all GPUs (computes remain discovered and on-fabric).
    """
    return tray_topology_config['switch_nodes']


@pytest.fixture(scope="module")
def ltx_force_max_iterations(cluster_and_sdn, tray_topology_config, devices):
    """
    Arm LTX force-max-iterations on all access ports for the tray admin-state flows.

    Coverage for the 6480024 class (NVL6 PHY LTX retry broke only under nmx_diag):
    with the knob armed, every tray-admin-driven retrain (down->diag, restore after
    'down') walks ALL link-training iterations instead of finishing on the first
    successful round (~1min -> ~5min per bring-up), stressing the LTX/admin-state
    interaction. Arming is hitless — it only affects future training, so the
    link-up snapshot taken by get_acp_ports_for_tray is not disturbed.

    Yields True when armed; tests use this to widen their port-verify retry budget
    (see TRAY_PORT_VERIFY_TRIES_LTX_FORCED in helpers.py).
    """
    if not isinstance(devices.dut, RosalindSwitch):
        logger.info("force-max-iterations is an NVL6/Rosalind knob — not arming on this device")
        yield False
        return
    ports_range = Port(summarize_ports(devices.dut.nvl_access_ports_list))
    with allure.step(f"Enable LTX force-max-iterations on all access ports ({ports_range.name})"):
        ports_range.interface.link.link_training.set(
            op_param_name=LinkTrainingConsts.FORCE_MAX_ITERATIONS,
            op_param_value=LinkTrainingConsts.ForceMaxIterations.ENABLED.value,
            apply=True, ask_for_confirmation=True).verify_result()
    yield True
    with allure.step(f"Cleanup: unset LTX force-max-iterations on all access ports ({ports_range.name})"):
        ports_range.interface.link.link_training.unset(
            apply=True, ask_for_confirmation=True).verify_result()
