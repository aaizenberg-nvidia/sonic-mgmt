import pytest
import logging

from ngts.nvos_tools.nmx.Cluster import Cluster
from ngts.nvos_tools.nmx.Sdn import Sdn
from ngts.nvos_constants.constants_nvos import OutputFormat
from ngts.tests_nvos.cluster.cluster_tools import ClusterTools
from ngts.tests_nvos.cluster.cluster_consts import ClusterConsts

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
    """Expected len(nv show sdn trays) == switch_nodes + compute_nodes from lab config."""
    return tray_topology_config['switch_nodes'] + tray_topology_config['compute_nodes']
