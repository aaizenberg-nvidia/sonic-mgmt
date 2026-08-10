"""
Functional injection test for tx-bandwidth-loss-monitor (zombie link).

Drives a real BW-loss event end-to-end via ``phy_diag.py`` (PPBMP_BW_LOSS
threshold lowering + PTER FEC-error injection on ``monitor_group=Tx_BW_loss``)
and asserts the NVOS-visible signals on ``nv show interface <port> link
phy detail``:

- ``linkdown-reason-code-local`` injected plane's slot == 45
  (BW_LOSS_THRESHOLD_EXCEEDED)
- ``linkdown-reason-status-local`` injected plane's slot ==
  ``BW_LOSS_THRESHOLD_EXCEEDED``
- ``unintentional-link-down-events`` counter incremented

Skips cleanly when ``phy_diag.py`` isn't deployed on the DUT (so normal
regression runs don't fail) and when the firmware silently rejects the PTER
arm (engineering-FW-only path).

Verified working on Taipan (Quantum3, FW 35.2016.4994-002).
"""

import logging
import pytest
from retry.api import retry_call

from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import (
    NvosConsts, PhyDiagConsts, TxBwLossMonitorConsts,
)
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port
from ngts.nvos_tools.infra.Fae import Fae
from ngts.nvos_tools.infra.IbInterfaceTool import IbInterfaceTool
from ngts.nvos_tools.infra.MultiPlanarTool import MultiPlanarTool
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.PhyDiagTool import PhyDiagTool
from ngts.nvos_tools.infra.RandomizationTool import RandomizationTool
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger()

# Retry budget for the FW-side BW-loss detection + NVOS publish.
EVENT_RETRIES = 30
EVENT_RETRY_DELAY_S = 2


def _read_phy_detail(port) -> dict:
    """Read ``nv show interface <port> link phy detail`` as a dict, using the
    same parser path as the rest of the interfaces tests."""
    return OutputParsingTool.parse_show_output_to_dict(
        port.interface.link.phy.detail.show()).get_returned_value()


def _latest_linkdown_signals(port, plane_idx=0):
    """Return ``(code, status, unintentional_count, detail)`` from NVOS's
    ``link phy detail`` for a single plane.

    ``linkdown-reason-code-local`` / ``-status-local`` are slash-joined
    **per plane**, not per event -- e.g. ``"23/23/23/22"`` on a 4-plane
    BlackMamba port means planes 1-3 read 23 and plane 4 reads 22. So the
    slot has to match the plane the injection was actually driven on;
    reading slot 0 while injecting on plane 4 silently observes an
    untouched plane. ``plane_idx`` is 0-based (plane N -> N-1).

    ``unintentional-link-down-events`` is a single aggregated counter for
    the whole port, so it is not sliced.
    """
    detail = _read_phy_detail(port)
    splitter = TxBwLossMonitorConsts.NVOS_PHY_DETAIL_PLANE_SPLITTER
    code = detail.get(TxBwLossMonitorConsts.NVOS_PHY_DETAIL_LINKDOWN_CODE_LOCAL,
                      "").split(splitter)[plane_idx]
    status = detail.get(TxBwLossMonitorConsts.NVOS_PHY_DETAIL_LINKDOWN_STATUS_LOCAL,
                        "").split(splitter)[plane_idx]
    count = int(detail.get(
        TxBwLossMonitorConsts.NVOS_PHY_DETAIL_UNINTENTIONAL_LINK_DOWN, "0"))
    return code, status, count, detail


def _assert_bw_loss_observed(port, baseline_count, plane_idx=0):
    """Assert NVOS has published a fresh BW-loss event on the injected plane.
    Raises ``AssertionError`` while the event hasn't landed yet; ``retry_call``
    polls this until it succeeds or the retry budget is exhausted."""
    code, status, count, _ = _latest_linkdown_signals(port, plane_idx)
    assert count > baseline_count and code == TxBwLossMonitorConsts.BW_LOSS_DIAG_CODE, \
        (f"waiting for BW-loss event on plane {plane_idx + 1}: code={code} "
         f"unintentional={count} (baseline={baseline_count})")
    return code, status, count


@pytest.fixture()
def phy_diag_deployed(engines):
    """Deploy ``phy_diag.py`` to the DUT for this test, and remove it after.

    Yields True when the tool is usable. The unpacked package is ~960 MB on
    the DUT's root overlay, so we always tear down what we deployed rather
    than leaving it for the next session on the setup. A pre-existing
    install (someone deployed it by hand) is left alone -- we only remove
    what this fixture put there.

    Teardown runs even when the test fails or the injection raises, since
    the yield-fixture teardown is unconditional.
    """
    preexisting = PhyDiagTool.is_available(engines.dut)
    deployed = PhyDiagTool.ensure_deployed(engines.dut, engines.sonic_mgmt)
    try:
        yield deployed
    finally:
        if deployed and not preexisting:
            PhyDiagTool.cleanup_deployed(engines.dut)


@pytest.mark.interface
@pytest.mark.multiplanar
def test_tx_bw_loss_injection_and_link_down(engines, devices, phy_diag_deployed):
    """
    End-to-end functional test: drive a real BW-loss event via direct PHY
    register access and verify NVOS publishes it correctly.

    Steps:

    0. **Ensure phy_diag.py is on the DUT** -- the ``phy_diag_deployed``
       fixture pushes the staged ``phy_package_lite`` tarball from the
       sonic-mgmt engine (which mounts ``/auto/sw_system_project``, unlike
       the NVOS DUT) and unpacks it into ``/tmp/phy_package`` on the DUT,
       removing it again at teardown. Skips the test only when the staged
       tarball isn't reachable from the sonic-mgmt host.

    1. **Pick a link-up port** -- random selection via
       ``RandomizationTool.select_random_port``; skip if no port is up
       (multi-planar setups may have only some ports wired).

    2. **Resolve the CR-space MST device + local_port** -- the BW-loss
       registers (PPBMC / PPBMP / PTER) aren't reachable via the
       ``pciconf`` MST variant on Quantum3, only via ``pci_cr``.
       ``IbInterfaceTool.get_mst_dev_name`` returns ``pciconf``;
       ``PhyDiagTool.cr_mst_dev_for`` swaps it. ``local_port`` is
       resolved via ``get_local_port_hex``.

    3. **Read baseline NVOS phy detail** -- capture the current value of
       ``unintentional-link-down-events`` so we can assert it increments
       after the injection.

    4. **PPBMC: arm the literal Tx_BW_loss monitor**
       (``monitor_cntl=0x20``), narrowing monitoring to that one type so the
       link-down is classified as code 45. Note the FW default ``0x24`` is
       bits 5+2 and already *includes* Tx_BW_loss (phy_diag decodes it as
       ``Symbol_FEC_monitor_Tx_BW_loss``), so this narrows rather than
       enables.

    5. **PPBMP: set the BW-loss alarm threshold to 1%**
       (``bw_loss_threshold=100``; the field is in 0.01% units). The value
       is derived from measurement, not guesswork -- on BlackMamba DGX
       (ASIC FW 35.2020.1094) the PTER injection below produces a Tx BW
       loss (Delta ``plr_xmit_retry_codes`` / Delta ``plr_xmit_codes``)
       that **varies per plane** with the plane's codeword rate: measured
       9.2% on a 48.5M codes/s plane but only ~2.1% on a 24.2M codes/s
       plane. An idle link measures exactly **0%** everywhere. The
       threshold must sit below the weakest plane's signal, above noise:

       * the FW default (``9500`` = 95%) is never crossed -- 10/10
         injections produced no event at all;
       * the original value (``1`` = 0.01%, NOT 1%) also trips on link
         re-training noise, so NVOS's attribution came out as a random
         mix of codes 45/22/2 run-to-run;
       * 500 (5%) tripped strong planes but sat above the weak planes'
         ~2.1% -- the verdict depended on the random plane pick;
       * at 100 (1%) both the weakest and strongest measured planes
         tripped in exactly 10s with code 45 /
         ``BW_LOSS_THRESHOLD_EXCEEDED`` and the counter incrementing.

       ``time_window`` is left at the FW default (no override) -- every
       validated run used it. The write persists across link flaps, so
       teardown restores FW defaults via ``clr``.

    6. **PTER: clear then arm Effective_BER FEC injection**
       (``error_type_admin=2, ber_mantissa=1, ber_exp=4,
       error_injection_time=0xFFFF``). The clear-first step is required
       on Quantum3 -- a fresh arm without clear gets ``status=2`` from
       the FW. We verify the response shows ``error_type_oper=1``
       (firmware armed); skip if it didn't (engineering-FW-only path).

    7. **Wait for NVOS to publish the BW-loss event** -- ``retry_call``
       polls ``nv show interface <port> link phy detail`` until either
       (a) ``unintentional-link-down-events`` has incremented AND the
       injected plane's slot of ``linkdown-reason-code-local`` reads
       ``"45"``, or (b) the retry budget (~60 s) is exhausted. The
       slash-joined reason strings are per-plane, so the slot read must be
       the plane the injection was driven on.

    8. **Assert NVOS-visible BW-loss signals** -- the first slot of
       ``linkdown-reason-code-local`` must be ``45`` and
       ``linkdown-reason-status-local`` must be
       ``BW_LOSS_THRESHOLD_EXCEEDED`` (uppercase form as NVOS reports).

    Cleanup:
        Stop the PTER injection (``error_type_admin=0``) and wait for the
        port to auto-recover to ``up``. The FW recovers the link on its
        own after a BW-loss event; no admin-up override is needed in the
        normal path. If recovery fails the wait raises -- surfacing the
        real product issue rather than masking it. PPBMC (and PPBMP, to undo
        older runs) are restored to FW defaults, since those writes persist
        across link flaps. The ``phy_diag_deployed`` fixture then removes
        ``/tmp/phy_package`` from the DUT.
    """
    if not phy_diag_deployed:
        pytest.skip(
            f"phy_diag.py not available at {PhyDiagConsts.PHY_DIAG_BIN} "
            f"and source tarball {PhyDiagConsts.PHY_DIAG_SOURCE_TARBALL} is not "
            f"reachable from the test process nor via the sonic-mgmt engine")

    with allure.step("Select a link-up port"):
        port_res = RandomizationTool.select_random_port(
            requested_ports_state=NvosConsts.LINK_STATE_UP)
        if not port_res.result:
            port_res.result.ignore_result()
            pytest.skip(f"no up port: {port_res.info}")
        port = port_res.get_returned_value()
        port_name = port.name
        logger.info(f"[inject] picked port={port_name}")

    with allure.step("Resolve CR-space MST device + local_port"):
        # Register-level work needs a plane-port name on a multi-planar device:
        # the lane-bmap is derived from the plane number, so an aggregated name
        # like "sw1p1" has no bmap (BlackMambaSwitch._get_lane_bmap would raise
        # on plane_number=None). Same resolution the up-once test does before
        # simulate_toggle_port_event.
        if devices.dut.num_of_plane_ports == 1:
            reg_port_name, plane_idx = port_name, 0
        else:
            reg_port_name = MultiPlanarTool.select_random_plane_port(
                Fae(port_name=port_name)).port.name
            # The phy-detail assertions read the slot for this plane, so the
            # injected plane and the observed slot must be the same one.
            plane_idx = Port.parse_port_name(reg_port_name)[4] - 1
        logger.info(f"[inject] register-level port={reg_port_name} plane_idx={plane_idx}")
        pciconf = IbInterfaceTool.get_mst_dev_name(engines.dut, port_name=reg_port_name)
        cr_mst = PhyDiagTool.cr_mst_dev_for(pciconf)
        local_port = int(
            IbInterfaceTool.get_local_port_hex(engines.dut, devices.dut, reg_port_name),
            16,
        )
        logger.info(f"[inject] cr_mst={cr_mst} local_port={local_port}")

    with allure.step("Read baseline NVOS phy detail"):
        _, _, baseline_count, _ = _latest_linkdown_signals(port, plane_idx)
        logger.info(f"[inject] baseline unintentional-link-down-events={baseline_count}")

    try:
        # Narrow monitoring to the literal Tx_BW_loss type so the link-down is
        # classified as code 45. (The FW default 0x24 already includes
        # Tx_BW_loss; this just removes the other monitors from the picture.)
        with allure.step("PPBMC: arm Tx_BW_loss monitor (monitor_cntl=0x20)"):
            PhyDiagTool.ppbmc_set_monitor_cntl(engines.dut, cr_mst, local_port)

        # Threshold at 1%: below the weakest measured per-plane injected loss
        # (~2.1%) and above the measured 0% idle noise -- see step 5 in the
        # docstring. Restored to FW defaults in teardown.
        with allure.step("PPBMP: set BW-loss alarm threshold to 1%"):
            out = PhyDiagTool.ppbmp_bw_loss_set_threshold(engines.dut, cr_mst, local_port)
            if "bad status" in out:
                pytest.skip(f"PPBMP write rejected by FW: {out[-200:]}")

        with allure.step("PTER: clear then arm Effective_BER injection"):
            PhyDiagTool.pter_clear(engines.dut, cr_mst, local_port)
            arm_out = PhyDiagTool.pter_arm(engines.dut, cr_mst, local_port)
            if not PhyDiagTool.is_pter_armed(arm_out):
                pytest.skip("PTER did not arm — likely engineering-FW-only path")

        with allure.step("Wait for NVOS to report BW-loss (retry-polled)"):
            code, status, count = retry_call(
                _assert_bw_loss_observed,
                fargs=[port, baseline_count, plane_idx],
                exceptions=AssertionError,
                tries=EVENT_RETRIES,
                delay=EVENT_RETRY_DELAY_S,
            )
            logger.info(f"[inject] event observed: code={code} status={status} count={count}")

        with allure.step("Assert NVOS-visible BW-loss signals"):
            ValidationTool.assert_expected_value(
                TxBwLossMonitorConsts.BW_LOSS_DIAG_CODE, code,
                TxBwLossMonitorConsts.NVOS_PHY_DETAIL_LINKDOWN_CODE_LOCAL)
            ValidationTool.assert_expected_value(
                TxBwLossMonitorConsts.NVOS_LINKDOWN_STATUS_BW_LOSS, status,
                TxBwLossMonitorConsts.NVOS_PHY_DETAIL_LINKDOWN_STATUS_LOCAL)

    finally:
        with allure.step("Cleanup: stop injection, restore registers, verify recovery"):
            PhyDiagTool.pter_clear(engines.dut, cr_mst, local_port)

            # PPBMC/PPBMP writes persist across link flaps, so a port left
            # modified would silently skew every later test that touches it.
            # Restore FW defaults even though this test now only writes PPBMC
            # -- the PPBMP restore also cleans up after older runs of this
            # test, which used to lower the threshold and never put it back.
            PhyDiagTool.ppbmc_restore_defaults(engines.dut, cr_mst, local_port)
            PhyDiagTool.ppbmp_bw_loss_restore_defaults(engines.dut, cr_mst, local_port)

            # The BW-loss event toggles the port; the firmware then auto-
            # recovers it. No admin-up step is needed in the normal path --
            # we just wait for the recovery to complete. If the port doesn't
            # come back up on its own that is a real product issue and the
            # wait_for_port_state failure surfaces it.
            port.interface.wait_for_port_state(
                NvosConsts.LINK_STATE_UP, timeout=60).verify_result()
