"""
Test-suite-local conftest for gNMI-for-IB plane-port and extended-telemetry tests.

Provides:
- `gnmi_client`: a function-scoped GnmiClient backed by a session client that
  pre-installs gnmic / grpcurl on the test player exactly once and authenticates
  with a dedicated throw-away local-admin user;
- `setup_topology`: a session-scoped lightweight view of the DUT's plane-port
  layout (plane count comes from `devices.dut.num_of_plane_ports`);
- `_planeport_safety_teardown`: an autouse function-scoped teardown that
  defensively `nv unset system plane-port state` between tests, in addition to
  the standard session `clear_config` chain.

This conftest grows one fixture at a time as each plane-port test passes review.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import pytest

from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port
from ngts.nvos_tools.infra.MultiPlanarTool import MultiPlanarTool
from ngts.nvos_tools.infra.RegressionConfigurations import PlanePortConnectivity
from ngts.tests_nvos.system.gnmi.GnmiClient import GnmiClient
from ngts.tests_nvos.system.reboot_telemetry_helpers import gnmi_client_for_dut
from ngts.tests_nvos.general.security.security_test_tools import security_test_utils
from ngts.tests_nvos.general.security.security_test_tools.constants import AaaConsts
from ngts.tests_nvos.general.security.security_test_tools.tool_classes.UserInfo import UserInfo
from ngts.tools.test_utils import switch_recovery

from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.general.telemetry.ib import helpers as ibh
from ngts.tests_nvos.general.telemetry.ib.constants import NvuePaths

logger = logging.getLogger(__name__)


# ============================================================================
# Topology view
# ============================================================================


@dataclass
class Topology:
    """Lightweight view of the DUT's plane-port layout + lab connectivity."""
    setup_name: str
    num_of_plane_ports: int
    _connectivity: Dict[str, dict] = field(default_factory=dict)
    fabric_description: str = ""

    def planes_for(self, aport_name: str) -> List[Port]:
        """Return Port objects for every plane belonging to `aport_name`."""
        return MultiPlanarTool.enumerate_plane_ports(aport_name, self.num_of_plane_ports)

    def all_aports(self) -> List[str]:
        """All IB Aport names present in the connectivity JSON."""
        return ibh.filter_aport_names(list(self._connectivity.keys()))

    def all_planarized_ports(self) -> List[str]:
        """Ports that get per-plane COUNTERS rows: IB Aports + the FNM fabric port."""
        names = list(self._connectivity.keys())
        return ibh.filter_aport_names(names) + ibh.filter_fabric_port_names(names)

    def expected_plane_count(self) -> int:
        """Total number of plane-port rows across all planarized ports on the DUT."""
        # sym-mgr writes a COUNTERS row per plane of every planarized port,
        # including the FNM fabric port (fnm1pl1..N), so the FNM port is counted
        # alongside the IB Aports rather than dropped as a non-Aport name.
        return len(self.all_planarized_ports()) * self.num_of_plane_ports

    def inter_switch_partner(
        self, dut_aport_name: str, dut_hostname: str
    ) -> Optional[Tuple[str, str]]:
        """Return ``(partner_hostname, partner_port)`` for an inter-switch link, or None."""
        return PlanePortConnectivity.find_inter_switch_partner(
            self._connectivity, self.fabric_description, dut_aport_name, dut_hostname
        )

    def loopback_partner(self, aport_name: str) -> Optional[str]:
        """Same-switch loopback peer port (live NVUE name) for `aport_name`, or None.

        `aport_name` may be either the live NVUE name (``sw122p1``) or the
        ibdiagnet aggregated label (``sw122p0``); both resolve to the same entry.
        """
        body = self._connectivity.get(aport_name)
        if body is None:
            # Accept a live NVUE name by mapping back to the ibdiagnet label.
            body = self._connectivity.get(re.sub(r"p1$", "p0", aport_name))
        if not isinstance(body, dict) or not body.get("loopback"):
            return None
        peer = str(body.get("connected_to") or "").strip()
        return ibh.connectivity_label_to_nvue(peer) if peer else None

    def link_up_loopback_aports(self) -> List[Tuple[str, str]]:
        """`(aport, loopback_peer)` pairs (live NVUE names) for loopback links that
        are physically up and expose the full plane set (both ends live on this DUT).

        Connectivity keys are ibdiagnet aggregated labels (``sw122p0``); they are
        mapped to NVUE interface names (``sw122p1``) before being returned.
        """
        pairs: List[Tuple[str, str]] = []
        for name, body in self._connectivity.items():
            if not isinstance(body, dict) or not body.get("loopback"):
                continue
            peer = str(body.get("connected_to") or "").strip()
            if not peer:
                continue
            if "LINK UP" not in str(body.get("physical_state") or "").upper():
                continue
            if len(body.get("planes") or {}) < self.num_of_plane_ports:
                continue
            pairs.append(
                (ibh.connectivity_label_to_nvue(name), ibh.connectivity_label_to_nvue(peer))
            )
        return pairs


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture(scope="session")
def _gnmi_local_admin(devices) -> UserInfo:
    """
    Stable credentials for the throw-away local admin user that authenticates
    gNMI for this suite. Generated once per session so the username/password
    stay constant across re-provisioning (see `gnmi_client`).
    """
    return UserInfo(
        username=AaaConsts.LOCALADMIN,
        password=switch_recovery.generate_strong_password(),
        role=devices.dut.aaa_admin_role,
    )


@pytest.fixture(scope="session")
def _gnmi_client_session(engines, devices, _gnmi_local_admin) -> GnmiClient:
    """Session-scoped gnmic client pinned to a dedicated local-admin user."""
    client = gnmi_client_for_dut(engines.dut, devices.dut)
    client.username = _gnmi_local_admin.username
    client.password = _gnmi_local_admin.password
    try:
        yield client
    finally:
        try:
            security_test_utils.cleanup_local_users(engines, [_gnmi_local_admin])
        except Exception as exc:  # noqa: BLE001
            logger.warning("gNMI local-admin cleanup failed: %s (non-fatal)", exc)


@pytest.fixture(scope="function")
def gnmi_client(engines, _gnmi_client_session, _gnmi_local_admin) -> GnmiClient:
    """Per-test gNMI client; re-provisions the local-admin user (idempotent) and returns the shared session client."""
    security_test_utils.set_local_users(engines, [_gnmi_local_admin], apply=True)
    return _gnmi_client_session


@pytest.fixture(scope="session")
def setup_topology(setup_name, devices, engines) -> Topology:
    """Plane-port topology view; plane count from the DUT, connectivity from the lab JSON."""
    connectivity, fabric_description = PlanePortConnectivity.load(setup_name, engines)
    return Topology(
        setup_name=setup_name,
        num_of_plane_ports=getattr(devices.dut, "num_of_plane_ports", 1),
        _connectivity=connectivity,
        fabric_description=fabric_description,
    )


@pytest.fixture(autouse=True)
def _skip_without_hfnm(request, topology_obj):
    """Skip `requires_hfnm`-marked tests on setups with no real HFNM in the topology."""
    # engines.hfnm is unreliable here: the global conftest falls back to the HA
    # engine when no FNM host exists, so it is always set. Topology membership is
    # the authoritative signal (matches the global conftest's HFNM-vs-HA logic).
    # Autouse + function-scoped so it runs before the requested start_sm fixture.
    if request.node.get_closest_marker("requires_hfnm") and "hfnm" not in topology_obj.players:
        pytest.skip(
            "requires a real HFNM in the topology to start the Subnet Manager and "
            "bring IB ports Active (no 'hfnm' player; the HA fallback cannot run the SM)"
        )


@pytest.fixture(autouse=True)
def _planeport_safety_teardown(engines, request):
    """Reset the plane-port knob to default (disabled) after each test."""
    yield
    if "skip_clear_config" in request.keywords:
        return
    try:
        result = System().plane_port.unset(
            op_param=NvuePaths.KEY_STATE, apply=True, ask_for_confirmation='-y'
        )
        if not result.result:
            result.ignore_result()
            logger.warning("plane-port safety teardown unset skipped (knob not present?): %s",
                           result.info)
    except Exception as exc:  # noqa: BLE001
        logger.warning("plane-port safety teardown failed: %s (non-fatal)", exc)
