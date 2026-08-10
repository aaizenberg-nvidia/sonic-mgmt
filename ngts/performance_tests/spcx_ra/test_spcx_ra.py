"""Unified Spectrum-X RA performance tests.

This module owns:

  - ``ChipBandwidth``        : chip-specific details for a lanes-per-sub-port
                               scenario (port-split, per-port speed, per-port
                               packet count, OS / Redmine skip overrides).
  - ``LanesConfig``          : a lanes-per-sub-port scenario plus the
                               ``chip_dict`` mapping of ``ChipBandwidth`` entries.
                               The same lane count is reached via different
                               port-splits across ASICs (SPC4/5: 100 Gbps/lane;
                               SPC6: 200 Gbps/lane).
  - ``FLAP_SCENARIO_NAMES``  : names of the link-flap sub-tests.
  - ``_SpcXRATestBase``      : the SPCX-RA test methods + shared helpers.
                               Underscore-prefixed so pytest doesn't collect it.
  - ``TestSpcXRA_Nx_lanes``  : four per-lane-count subclasses; each carries
                               its own ``CONFIG = LanesConfig(...)``.

Autouse fixtures in ``conftest.py`` read ``self.CONFIG`` (via duck typing) and
pick the active per-chip entry at runtime.
"""
import functools
import logging
import random
from dataclasses import dataclass, field
from typing import ClassVar, Dict, Optional

import allure
import pytest

from infra.tools.exceptions.test_issue import TestIssue
from infra.tools.redmine.redmine_api import is_redmine_issue_active
from ngts.cli_wrappers.nvue.nvue_cli import NvueCli
from ngts.cli_wrappers.nvue.nvue_performance_clis import get_swp_parent_port_names
from ngts.constants.constants import CliType, InfraConst
from ngts.constants.performance_constants import (
    MongoDbConsts, PerfConsts, SPCXRAConsts, ValidationConsts, BwFairnessThreshold)
from ngts.helpers.performance.performance_counter_helpers import (
    should_validate_performance_counters)
from ngts.helpers.performance.performance_db_helpers import (
    add_test_mongo_metadata, get_perf_test_name)
from ngts.helpers.performance.performance_setup_helpers import (
    ValidationConfig, apply_test_configuration, configure_incremental_dips_on_tg,
    get_obj_method, restore_basic_configuration, run_traffic,
    run_validation, set_ports_admin_state, skip_test_on_unsupported_chip_type,
    skip_test_on_unsupported_os, validate_perf_dut_ingress_buffer_mode,
    validate_perf_dut_rebalancer_buffer_mode, validate_traffic_results)
from ngts.helpers.performance.traffic_helpers import validate_bw_per_ports
from ngts.performance_tests.spcx_ra.conftest import (
    TESTS_SCENARIO, compute_flap_validation_overrides, get_spcx_ra_leaf_traffic)

logger = logging.getLogger()

# Names of the link-flap scenarios. Each must resolve to a method on
# _SpcXRATestBase via get_obj_method().
FLAP_SCENARIO_NAMES = ["port_hiccup", "port_repeated_toggle", "toggle_multiple_ports"]

# Per-chip "default split": SPC6 physical ports are pre-split into 2 sub-ports
# at the hardware level, so a lane count that SPC4/5 reach with ``port_split``
# needs ``2 * port_split`` on SPC6. NVUE ``link breakout Nx`` is absolute
# (relative to the physical port, not to the current breakout), so this
# multiplier must be applied before the value reaches the NVUE templates -
# see LanesConfig.effective_split() / to_template_args(absolute_breakout=True).
# DVS keeps ``port_split`` as-is: SPC6 DVS already boots at that default
# breakout, and SDK ``ports_split(split_to=N)`` is relative to current ports.
CHIP_DEFAULT_SPLIT: Dict[str, int] = {"SPC4": 1, "SPC5": 1, "SPC6": 2}


# ======================================================== configuration model

@dataclass(frozen=True)
class ChipBandwidth:
    """Chip-specific details for a lanes-per-sub-port scenario.

    Attributes:
        port_split: Number of sub-ports each physical port is split into
            on this chip, relative to the chip's default breakout
            (``CHIP_DEFAULT_SPLIT``). Different chips can reach the same
            lanes-per-sub-port via different splits (e.g. 4 lanes/sub-port =
            SPC4/5 at ``split=2`` but SPC6 at ``split=1``, because SPC4/5 are
            100 Gbps/lane and SPC6 is 200 Gbps/lane). The absolute breakout
            configured on the DUT is :meth:`LanesConfig.effective_split`.
        port_speed_bps: Per-port line rate in bits per second, as a string
            (e.g. ``"800000000"``). Kept as a string for backwards-compat
            with the Jinja templates.
        traffic_packets_per_port: Number of unique packets the traffic
            generator should produce per port. Selected per chip because
            the right value tracks per-port speed (200G/400G/800G/...).
        unsupported_os: If non-None, the test is skipped on this chip when
            running on the given CLI/OS family.
        ibm_bw_skip_redmine_issue: If non-None, the IBM-test BW check is
            suppressed on this chip while the given Redmine issue is open
            (e.g. SPC4 x1=800G has Bug SW #4348288 — 800G AR is "won't fix"
            — yet the other validations on that test still run).
        skip_first_iteration_redmine_issue: If non-None, the first counters
            sampling iteration is skipped on this chip while the given
            Redmine issue is open (e.g. SPC6 x1=200G with Bug SW #5144323).
        link_phy_speed: Optional NVUE ``link.speed`` override (e.g. ``"200G"``)
            for Cumulus max-breakout mloop ports. When set, jinja skips the
            default link-phy include on data ports and applies this speed on
            mloop ports.
    """

    port_split: int
    port_speed_bps: str
    traffic_packets_per_port: int
    unsupported_os: Optional[CliType] = None
    ibm_bw_skip_redmine_issue: Optional[int] = None
    skip_first_iteration_redmine_issue: Optional[int] = None
    link_phy_speed: Optional[str] = None


@dataclass(frozen=True)
class LanesConfig:
    """A lanes-per-sub-port test scenario across ASIC generations.

    The same lanes-per-sub-port (1, 2, 4, 8) is reached via different
    port-splits on different chips because lane speed differs:

        SPC4 / SPC5 (100 Gbps/lane, default breakout 1):
            8 lanes -> 800G, split=1
            4 lanes -> 400G, split=2
            2 lanes -> 200G, split=4
            1 lane  -> 100G, split=8
        SPC6 (200 Gbps/lane, default breakout 2):
            4 lanes -> 800G, split=1
            2 lanes -> 400G, split=2
            1 lane  -> 200G, split=4

    ``split`` above is relative to the chip's default breakout;
    :meth:`effective_split` converts it to the absolute NVUE breakout.

    Each ``TestSpcXRA_Nx_lanes`` class pins itself by assigning a
    ``LanesConfig`` to its ``CONFIG`` class attribute. A chip not
    present in ``chip_dict`` means the test class is skipped on that chip
    (no lab setup at that lane count).

    Attributes:
        lanes_per_subport: Number of lanes each sub-port spans (1, 2, 4, 8).
        chip_dict: Mapping of chip type (e.g. ``"SPC4"``) to per-chip details.
    """

    lanes_per_subport: int
    chip_dict: Dict[str, ChipBandwidth] = field(default_factory=dict)

    # ------------------------------------------------------- lookup

    def for_chip(self, chip_type: str) -> Optional[ChipBandwidth]:
        """Return the per-chip details for ``chip_type``.

        Args:
            chip_type: Chip type identifier (e.g. ``"SPC4"``, ``"SPC6"``).

        Returns:
            The ``ChipBandwidth`` registered under ``chip_type``, or ``None``
            when ``chip_type`` is not in ``chip_dict`` (meaning the test
            should be skipped on this chip).
        """
        return self.chip_dict.get(chip_type)

    # ------------------------------------------------------- derived splits

    def effective_split(self, chip_type: str) -> int:
        """Return the absolute breakout factor to configure on ``chip_type``.

        ``ChipBandwidth.port_split`` is expressed relative to the chip's
        default breakout, while NVUE ``link breakout Nx`` is absolute with
        respect to the physical port. On chips with a default pre-split
        (SPC6: 2) the two differ, so the configured value must be
        ``CHIP_DEFAULT_SPLIT[chip_type] * port_split`` - e.g. 2 lanes/sub-port
        on SPC6 is ``port_split=2`` but ``breakout 4x`` (400G sub-ports of an
        1600G parent). Feeding ``port_split`` straight to the templates leaves
        the ports at twice the intended speed while the traffic generator is
        shaped for the intended one.

        Args:
            chip_type: Chip type identifier.

        Returns:
            The breakout factor to render into the NVUE templates.
        """
        return CHIP_DEFAULT_SPLIT.get(chip_type, 1) * self.chip_dict[chip_type].port_split

    # ------------------------------------------------------- derived names

    def name(self, chip_type: str) -> str:
        """Build the scenario name from the chip's split and per-chip speed.

        Args:
            chip_type: Chip type identifier.

        Returns:
            A string like ``"x1_800G"`` for ``port_split=1`` on SPC6 in the
            4-lanes class.
        """
        chip_speed_config = self.chip_dict[chip_type]
        speed_gbps = int(chip_speed_config.port_speed_bps) // 1_000_000_000
        return f"x{chip_speed_config.port_split}_{speed_gbps}G"

    def mongo_conf_name_spine(self, chip_type: str) -> str:
        """Build the spine-test Mongo CONF_NAME entry for ``chip_type``.

        Args:
            chip_type: Chip type identifier.

        Returns:
            A string like ``"x2_800G_spine"``.
        """
        return f"{self.name(chip_type)}_spine"

    def mongo_conf_name_leaf(self, chip_type: str) -> str:
        """Build the leaf-test Mongo CONF_NAME entry for ``chip_type``.

        Args:
            chip_type: Chip type identifier.

        Returns:
            A string like ``"x2_800G_leaf"``.
        """
        return f"{self.name(chip_type)}_leaf"

    def allure_title_suffix(self, chip_type: str) -> str:
        """Build the bracketed allure-title suffix for ``chip_type``.

        Args:
            chip_type: Chip type identifier.

        Returns:
            A suffix such as
            ``"[400G, real_split=4, test_split=4]"`` for chips with no
            default pre-split, or
            ``"[400G, real_split=8, test_split=4]"`` for chips that have one.
            Currently only SPC6 has a default pre-split, so its real split is
            ``2 * port_split``.
        """
        chip_speed_config = self.chip_dict[chip_type]
        # ``port_speed_bps`` is actually in Kbps despite the field name —
        # the Jinja templates feed it directly into ``port-max-rate``.
        # Kbps -> Gbps: divide by 1,000,000.
        speed_gbps = int(chip_speed_config.port_speed_bps) // 1_000_000
        return (f"[{speed_gbps}G, real_split={self.effective_split(chip_type)}, "
                f"test_split={chip_speed_config.port_split}]")

    # ------------------------------------------------------- builders

    def to_template_args(self, chip_type: str, is_ipv6: bool,
                         absolute_breakout: bool = False) -> dict:
        """Render this config as the ``conf_args`` dict the Jinja templates consume.

        Args:
            chip_type: Chip type identifier; selects the per-chip entry.
            is_ipv6: Whether the test runs against IPv6 instead of IPv4.
            absolute_breakout: When True (NVUE/Cumulus), emit
                :meth:`effective_split` because ``link breakout Nx`` is
                absolute vs the physical port. When False (DVS default),
                emit ``port_split`` as-is: SPC6 DVS already boots at the
                chip default breakout
                (``SPC6_DVS_CUSTOM_CONFIG_FILE`` / 128-port 2x), and SDK
                ``ports_split(split_to=N)`` is relative to the current
                ports — multiplying again would double-split.

        Returns:
            A dictionary that mirrors the legacy ``conf_args`` schema
            consumed by the spcx_ra Jinja templates and CLI helpers.
        """
        chip_speed_config = self.chip_dict[chip_type]
        is_ipv4 = not is_ipv6
        split = (self.effective_split(chip_type) if absolute_breakout
                 else chip_speed_config.port_split)
        conf_args = {
            "auto_buffer_mode": "True" if chip_type == "SPC6" else "False",
            "congestion_thresh_lo": (PerfConsts.LOW_AR_THRESHOLD_SPC6 if chip_type == "SPC6"
                                     else PerfConsts.LOW_AR_THRESHOLD),
            "two_sided_ar": True,
            "is_ipv6": is_ipv6,
            "is_ipv4": is_ipv4,
            "chip_type": chip_type,
            # Incremental-DIP fan-out (MultiDipsTG) for the leaf test. The start
            # DIPs mirror dut.jinja's ``dip_left_to_right`` so the TG's DIP-increment
            # ACL matches the leaf IPs the DUT actually routes to.
            "use_incremental_dips": False,
            "dip_left_to_right_start_ipv4_list": ["10.0.1.0"] if is_ipv4 else [],
            "dip_left_to_right_start_ipv6_list": ["192:168:5:1:1:1:2:0"] if is_ipv6 else [],
            "split_left": split,
            "split_right": split,
            "host": PerfConsts.RIGHT_TG_ALIAS,
            "spine": PerfConsts.LEFT_TG_ALIAS,
            "shaper_value": 0.975,
            "scenario": TESTS_SCENARIO,
            "packet_size": PerfConsts.PACKET_SIZE_4K,
            "left_num_packets": chip_speed_config.traffic_packets_per_port,
            "right_num_packets": chip_speed_config.traffic_packets_per_port,
            "speed": chip_speed_config.port_speed_bps,
            "params": None,
        }
        if chip_speed_config.link_phy_speed:
            conf_args["link_phy_speed"] = chip_speed_config.link_phy_speed
        return conf_args

    def ibm_bandwidth_threshold(self, chip_type: str) -> Optional[dict]:
        """Build the IBM-test BW threshold dict for this config + chip.

        Args:
            chip_type: Chip type identifier; selects the per-chip entry.

        Returns:
            A nested dict of ``{port_side: {direction: threshold}}`` ratios,
            or ``None`` when the chip's ``ibm_bw_skip_redmine_issue`` is
            currently active (e.g. while Bug SW #4348288 is open the SPC4
            x1=800G IBM test skips its BW check).
        """
        chip_speed_config = self.chip_dict[chip_type]
        skip_issue = chip_speed_config.ibm_bw_skip_redmine_issue
        if skip_issue is not None and is_redmine_issue_active([skip_issue])[0]:
            return None
        ibm_bw_threshold = SPCXRAConsts.get_min_line_rate_bw_threshold_ibm(chip_type)
        return {
            "left_ports": {ValidationConsts.TX: ibm_bw_threshold,
                           ValidationConsts.RX: ibm_bw_threshold},
            "right_ports": {ValidationConsts.TX: ibm_bw_threshold,
                            ValidationConsts.RX: ibm_bw_threshold},
        }


# ======================================================================== base

def _with_lanes_allure_title(method):
    """Decorator: prepend the speed/split suffix to the allure report title.

    ``allure.dynamic.title()`` only takes effect when called during the test
    call phase, not from an autouse fixture's setup. Wrapping each test
    method gives us the right phase.
    """
    @functools.wraps(method)
    def wrapper(self, request, *args, **kwargs):
        allure.dynamic.title(
            f"{request.node.name} {self.CONFIG.allure_title_suffix(self.chip_type)}")
        return method(self, request, *args, **kwargs)
    return wrapper


@pytest.mark.parametrize("basic_setup_configuration",
                         [InfraConst.IPV4, InfraConst.IPV6], indirect=True)
class _SpcXRATestBase:
    """Base class holding the SPCX-RA test methods and per-test helpers.

    Subclasses set ``CONFIG = LanesConfig(...)``. The autouse fixtures
    in ``conftest.py`` read ``self.CONFIG`` and attach the per-instance test
    state (``self.players``, ``self.cli_object``, ``self.conf_args``,
    ``self.traffic_jsons``, ...).

    The leading underscore stops pytest from collecting this base class.
    """

    CONFIG: ClassVar[Optional[LanesConfig]] = None

    # ================================================================ tests

    @allure.description('AR enabled + auto buffer mode (rebalancer) on. Verify port utilization.')
    @_with_lanes_allure_title
    def test_ar_perf_max_bandwidth_rebalancer_enabled(self, request):
        """Verify port utilization with AR + auto buffer mode (rebalancer) on.

        Args:
            request: pytest ``FixtureRequest`` (provides the test name).
        """
        skip_test_on_unsupported_chip_type(self.chip_type, "SPC4")
        skip_test_on_unsupported_chip_type(self.chip_type, "SPC5")
        test_name = get_perf_test_name(request)
        packet_size = PerfConsts.PACKET_SIZE_4K

        with allure.step("Validate rebalancer (auto buffer mode) is active on the DUT"):
            if self.chip_type == "SPC6" and isinstance(self.cli_object, NvueCli):
                validate_perf_dut_rebalancer_buffer_mode(self.players)

        self._run_spine_traffic(packet_size)
        self._validate(
            test_name, packet_size,
            skip_first_counters_iteration=(self.chip_type == "SPC6" and
                                           isinstance(self.cli_object, NvueCli)))

    @allure.description('AR enabled + IBM enabled. Verify port utilization at minimum line rate.')
    @_with_lanes_allure_title
    def test_ar_perf_max_bandwidth_ibm_rebalancer_disabled(self, request):
        """Verify port utilization with AR + IBM enabled and the rebalancer disabled.

        Args:
            request: pytest ``FixtureRequest``.
        """
        skip_test_on_unsupported_chip_type(self.chip_type, "SPC6")
        test_name = get_perf_test_name(request)
        packet_size = PerfConsts.PACKET_SIZE_4K

        with allure.step("Validate ingress buffer mode (IBM) on DUT"):
            if isinstance(self.cli_object, NvueCli):
                validate_perf_dut_ingress_buffer_mode(self.players)

        self._run_spine_traffic(packet_size)
        self._validate(
            test_name, packet_size,
            bw_threshold=self.CONFIG.ibm_bandwidth_threshold(self.chip_type),
            skip_first_counters_iteration=True)

    @allure.description('AR enabled on one side (leaf scenario). Verify port utilization.')
    @_with_lanes_allure_title
    def test_ar_perf_max_bandwidth_leaf(self, request):
        """Verify port utilization with AR enabled on one side only (leaf).

        Args:
            request: pytest ``FixtureRequest``.
        """
        test_name = get_perf_test_name(request)
        packet_size = PerfConsts.PACKET_SIZE_4K
        add_test_mongo_metadata(
            test_name,
            {MongoDbConsts.CONF_NAME: self.CONFIG.mongo_conf_name_leaf(self.chip_type)})

        self._reconfigure_as_one_sided_ar()
        try:
            if self.conf_args["use_incremental_dips"]:
                with allure.step("Create incremental dips on the spine traffic generator"):
                    configure_incremental_dips_on_tg(
                        self.players, players_aliases=[PerfConsts.LEFT_TG_ALIAS])
            leaf_traffic_jsons = get_spcx_ra_leaf_traffic(
                self.players, self.conf_args,
                use_incremental_dips=self.conf_args["use_incremental_dips"],
                incremental_dip_num_packets=self.CONFIG.chip_dict[self.chip_type].traffic_packets_per_port)

            with allure.step(f"Run {packet_size}B packet Traffic on all the ports"):
                run_traffic(self.players, self.scenario, leaf_traffic_jsons)

            self._wait_for_nexthop_resolution()
            self._validate(test_name, packet_size, skip_first_counters_iteration=True)
        finally:
            self._restore_two_sided_ar()

    @pytest.mark.parametrize("flap_scenario", FLAP_SCENARIO_NAMES)
    @allure.description('Verify traffic converges to initial state after an interface flap.')
    @_with_lanes_allure_title
    def test_ar_perf_link_flap(self, request, flap_scenario):
        """Verify traffic converges to the initial state after an interface flap.

        Args:
            request: pytest ``FixtureRequest``.
            flap_scenario: One of ``FLAP_SCENARIO_NAMES``; resolved to a
                same-named method on ``self`` via
                :func:`get_obj_method` and invoked between the traffic and
                validation phases.
        """
        test_name = get_perf_test_name(request)
        packet_size = PerfConsts.PACKET_SIZE_4K
        self._run_spine_traffic(packet_size, wait_for_nexthop=False)
        ports_down = get_obj_method(self, flap_scenario)(test_name)
        allure.step(f"Ports flipped: {ports_down}")
        self._wait_for_nexthop_resolution()

        flap_overrides = compute_flap_validation_overrides(
            ports_down=ports_down,
            side_ports=self.cli_object.performance.get_right_left_ports_dict(),
            bw_baseline=SPCXRAConsts.DUT_TX_UTIL_AUTO_TH_DICT,
            tc_occ_baseline=PerfConsts.OCC_TH_DICT)
        samples_params = dict(PerfConsts.SAMPLES_PARAMS)
        if flap_scenario == "port_hiccup":
            # Example: enable expensive collectors only for this flap scenario.
            samples_params[PerfConsts.COLLECT_TC_LATENCY_ENV_VAR] = "True"
            samples_params[PerfConsts.COLLECT_SDK_DUMP_ENV_VAR] = "True"
        self._validate(
            test_name, packet_size, skip_first_counters_iteration=True,
            samples_params_dict=samples_params,
            **flap_overrides)

    @allure.description('Verify traffic converges to initial state after a cold reboot.')
    @_with_lanes_allure_title
    def test_ar_perf_reload_reboot(self, request):
        """Verify traffic converges to the initial state after a cold reboot.

        Skipped on DVS — the DVS topology doesn't support DUT reboot.

        Args:
            request: pytest ``FixtureRequest``.
        """
        skip_test_on_unsupported_os(cli_obj=self.cli_object, unsupported_os=CliType.DVS)
        test_name = get_perf_test_name(request)
        packet_size = PerfConsts.PACKET_SIZE_4K
        self._run_spine_traffic(packet_size, wait_for_nexthop=False)

        with allure.step("Rebooting the DUT"):
            self.cli_object.general.reboot(
                self.dut_engine, save_config=True, wait_after_ping=240)

        self._wait_for_nexthop_resolution()
        self._validate(test_name, packet_size, skip_first_counters_iteration=True)

    # ============================================================== helpers

    def _run_spine_traffic(self, packet_size: int, wait_for_nexthop: bool = True) -> None:
        """Run the spine-side traffic JSON on all configured players.

        Args:
            packet_size: Frame size in bytes (used only for the Allure step label).
            wait_for_nexthop: When ``True`` (default), wait for nexthop
                resolution immediately after the traffic starts. Set to
                ``False`` when a perturbation step (flap, reboot) needs to
                run before the wait.
        """
        with allure.step(f"Run {packet_size}B packet Traffic on all the ports"):
            run_traffic(self.players, self.scenario, self.traffic_jsons)
        if wait_for_nexthop:
            self._wait_for_nexthop_resolution()

    def _wait_for_nexthop_resolution(self) -> None:
        """
        Wait for nexthop resolution while the gating Redmine issue is open.
        """
        with allure.step("Wait for nexthop resolution"):
            self.cli_object.performance.wait_for_nexthop_resolution(
                self.conf_args, timeout=PerfConsts.TIMEOUT_FOR_NEXTHOP_RESOLUTION)

    def _reconfigure_as_one_sided_ar(self) -> None:
        """Reconfigure the DUT for the leaf scenario (one-sided AR).

        Sets ``two_sided_ar`` to ``False`` in ``self.conf_args`` then
        re-applies the test configuration on every DUT-side player.
        """
        self._apply_ar_mode(two_sided=False)

    def _restore_two_sided_ar(self) -> None:
        """Restore the DUT to the default two-sided AR test configuration.

        Mirror of :meth:`_reconfigure_as_one_sided_ar`. Used as the leaf
        test's teardown so subsequent tests in the class (flap, reboot)
        don't run their two-sided traffic JSONs against a DUT that is
        still configured one-sided.
        """
        self._apply_ar_mode(two_sided=True)

    def _apply_ar_mode(self, *, two_sided: bool) -> None:
        """Re-apply the DUT test configuration with the given AR mode.

        Mutates ``self.conf_args['two_sided_ar']`` in place, restores the
        DUT-side players to the saved baseline, and re-applies the test
        configuration. Used by :meth:`_reconfigure_as_one_sided_ar` and
        :meth:`_restore_two_sided_ar`.
        """
        self.conf_args["two_sided_ar"] = two_sided
        restore_basic_configuration(
            players=self.players, players_aliases=PerfConsts.PERF_SETUP_DUT_ALIASES)
        apply_test_configuration(
            players=self.players, players_aliases=PerfConsts.PERF_SETUP_DUT_ALIASES,
            scenario=self.scenario, conf_args=self.conf_args)

    def _validate(self, test_name: str, packet_size: int, *,
                  bw_threshold=SPCXRAConsts.DUT_TX_UTIL_AUTO_TH_DICT,
                  tc_occ_threshold=PerfConsts.OCC_TH_DICT,
                  skip_first_counters_iteration: bool = False,
                  run_validate_counters: bool = True,
                  samples_params_dict=None,
                  step: Optional[str] = None) -> None:
        """Build a ``ValidationConfig`` and run it inside an Allure step.

        Args:
            test_name: Test identifier captured from the pytest request.
            packet_size: Frame size used to build the Allure step label.
            bw_threshold: Override for the BW threshold; defaults to
                ``SPCXRAConsts.DUT_TX_UTIL_AUTO_TH_DICT``. Pass ``None`` to
                disable the BW validation.
            tc_occ_threshold: Override for the TC-occupancy threshold;
                defaults to ``PerfConsts.OCC_TH_DICT``. Pass ``None`` to
                disable the TC-occupancy validation.
            skip_first_counters_iteration: Forwarded to ``ValidationConfig``;
                useful after reboot / flap when the first sample is unstable.
                Also forced on when the chip's ``skip_first_iteration_redmine_issue``
                is active.
            run_validate_counters: When ``False``, the per-sample DUT
                counter check is skipped. Used by the flap test when the
                L/R down-port counts are unequal: the oversubscribed
                bottleneck side legitimately accrues non-zero drop / error
                counters during the flap window.
            samples_params_dict: Optional TrafficValidator sample/env overrides
                (e.g. ``COLLECT_TC_LATENCY``, ``COLLECT_SDK_DUMP``). Defaults to
                ``PerfConsts.SAMPLES_PARAMS``.
            step: Override for the Allure step label; defaults to
                ``"Verifying the traffic for packet size {packet_size}"``.
        """
        chip_bw = self.CONFIG.for_chip(self.chip_type)
        skip_issue = chip_bw.skip_first_iteration_redmine_issue if chip_bw else None
        if skip_issue is not None and is_redmine_issue_active([skip_issue])[0]:
            skip_first_counters_iteration = True

        step = step or f"Verifying the traffic for packet size {packet_size}"
        with allure.step(step):
            run_validation(ValidationConfig(
                players=self.players,
                test_name=test_name,
                scenario=self.scenario,
                chip_type=self.chip_type,
                packet_size=packet_size,
                bw_threshold=bw_threshold,
                bw_fairness_threshold_per_port_group=(
                    BwFairnessThreshold.get_bw_fairness_threshold_per_port_group(bw_threshold)),
                tc_occ_threshold=tc_occ_threshold,
                run_validate_counters=run_validate_counters,
                run_validate_performance_counters=should_validate_performance_counters(
                    self.cli_object),
                power_threshold=self.power_thresholds_by_chip_type,
                skip_first_counters_iteration=skip_first_counters_iteration,
                samples_params_dict=(samples_params_dict
                                     if samples_params_dict is not None
                                     else PerfConsts.SAMPLES_PARAMS),
            ))

    # ---------- link-flap scenarios (resolved via get_obj_method(self, name))

    def port_hiccup(self, test_name: str) -> list:
        """Shut down then bring up a single randomly-chosen DUT port.

        Args:
            test_name: Test identifier (unused here; kept to match the
                callable signature shared by the three flap scenarios).

        Returns:
            The list of DUT ports that were administratively down during
            the flap (always one port for this scenario).
        """
        [port] = random.sample(self._unique_dut_ports(), 1)
        self._toggle_port(port)
        return [port]

    def port_repeated_toggle(self, test_name: str) -> list:
        """Toggle a single randomly-chosen DUT port ten times in a row.

        Args:
            test_name: Test identifier (unused; kept for signature symmetry).

        Returns:
            The single DUT port that was repeatedly toggled.
        """
        [port] = random.sample(self._unique_dut_ports(), 1)
        with allure.step(f"Toggle {port} 10 times"):
            for _ in range(10):
                self._toggle_port(port)
        return [port]

    def toggle_multiple_ports(self, test_name: str) -> list:
        """Shut down a random subset of DUT ports, validate, then bring them back up.

        Args:
            test_name: Test identifier; used to look up the traffic results
                during the zero-BW validation phase.

        Returns:
            The DUT ports that were administratively down during the inner
            validation phase. The L/R distribution of this list is what
            the caller uses to decide whether the post-flap state is
            symmetric.
        """
        ports = self._unique_dut_ports()
        num_ports_to_shutdown = random.randrange(2, 10)
        ports_to_shutdown = random.sample(ports, num_ports_to_shutdown)

        with allure.step(f"Shutting down ports: {ports_to_shutdown}"):
            set_ports_admin_state(
                self.players, port_list=ports_to_shutdown, port_state="down")

        self._validate_zero_bw_on_down_ports(test_name, ports_to_shutdown)

        with allure.step(f"Bringing up ports: {ports_to_shutdown}"):
            set_ports_admin_state(
                self.players, port_list=ports_to_shutdown, port_state="up")
        self._recover_admin_down_ports_on_cumulus()

        return ports_to_shutdown

    # ----------- internal flap helpers

    def _unique_dut_ports(self) -> list:
        """Return the DUT's port list with duplicates removed.

        Returns:
            A list of DUT ports as reported by the performance CLI, with
            duplicates filtered out.
        """
        return list(set(self.cli_object.performance.get_dut_ports()))

    def _toggle_port(self, port) -> None:
        """Shut down then bring up a single port.

        Args:
            port: The port identifier to toggle.
        """
        with allure.step(f"Shutting down port: {port}"):
            set_ports_admin_state(self.players, port_list=[port], port_state="down")
        with allure.step(f"Bringing up port: {port}"):
            set_ports_admin_state(self.players, port_list=[port], port_state="up")
        self._recover_admin_down_ports_on_cumulus()

    def _recover_admin_down_ports_on_cumulus(self) -> None:
        """Re-assert admin up on Cumulus ports that stayed down after a flap.

        On Cumulus a flapped port can remain admin down even though
        :func:`set_ports_admin_state` already issued
        ``nv set interface <ports> link state up``. Leaving it down starves the
        remaining validations of a port the traffic pattern expects. Recover it
        the same way ``unsplit_all_ports`` does: bring every existing ``swp``
        interface up one by one and wait for NVUE to report them admin up.

        No-op on DVS and SONiC, where these NVUE helpers do not exist.
        """
        if not isinstance(self.cli_object, NvueCli):
            return

        bonus_parent_ports = set(get_swp_parent_port_names(
            self.cli_object.interface.get_bonus_ports(self.dut_engine)))
        down_ports = self.cli_object.performance.get_down_swp_ports(
            bonus_parent_ports, require_oper_up=False)
        if not down_ports:
            return

        with allure.step(f"Ports still admin down after the flap: {down_ports}. Bringing them up"):
            logger.info(f"Ports still admin down after the flap: {down_ports}")
            self.cli_object.interface.bring_all_existing_swp_ports_up()
            self.cli_object.performance.wait_for_all_swp_ports_admin_up()

    def _validate_zero_bw_on_down_ports(self, test_name: str,
                                        ports_to_shutdown: list) -> None:
        """Verify that all down ports show 0% B/W utilization in the traffic JSONs.

        Args:
            test_name: Test identifier used to look up the traffic-result JSONs.
            ports_to_shutdown: The DUT-port list that was administratively
                shut down before this validation runs.

        Raises:
            TestIssue: If any of the down ports show non-zero B/W in any of
                the collected traffic samples.
        """
        sdk_ports = self.cli_object.performance.get_sdk_ports(ports_to_shutdown)
        validation_jsons = validate_traffic_results(
            self.players, test_name, self.scenario, PerfConsts.SAMPLES_PARAMS)

        violations = []
        for result in validation_jsons:
            with allure.step("Verifying the B/W utilization is 0% on down ports"):
                validate_bw_per_ports(
                    result['traffic_json'], bw_threshold=0,
                    ports_list=sdk_ports, violations_list=violations)

        if violations:
            raise TestIssue("\n".join(violations))


# =================================================== per-lane-count classes
#
# Four classes, one per lanes-per-sub-port. Each class is chip-aware: the
# port_split needed to reach a given lane count depends on the chip's
# parent-port lane count and per-lane speed.
#
# ``port_split`` below is relative to the chip's default breakout. On NVUE the
# absolute breakout rendered into templates is
# ``CHIP_DEFAULT_SPLIT[chip] * port_split`` (see LanesConfig.effective_split /
# to_template_args(absolute_breakout=True)). On DVS, ``port_split`` is passed
# through unchanged because SPC6 DVS already boots at the default breakout and
# SDK ``ports_split`` is relative to the current ports.
#
#   SPC4 / SPC5 (8-lane 800G parent, 100 Gbps/lane, default breakout 1):
#       8 lanes -> 800G, split=1 -> breakout 1x
#       4 lanes -> 400G, split=2 -> breakout 2x
#       2 lanes -> 200G, split=4 -> breakout 4x
#       1 lane  -> 100G, split=8 -> breakout 8x
#   SPC6 (8-lane 1600G parent, 200 Gbps/lane, default breakout 2):
#       4 lanes -> 800G, split=1 -> NVUE breakout 2x / DVS no further split
#       2 lanes -> 400G, split=2 -> NVUE breakout 4x / DVS relative split 2
#       1 lane  -> 200G, split=4 -> NVUE breakout 8x / DVS relative split 4
#
# Per-port packet count tracks per-port speed (200G -> 6, 400G -> 8,
# 800G -> 20, ...). If a chip is absent from ``chip_dict`` the test class is
# skipped on that chip — no lab setup at that lane count.

class TestSpcXRA_8x_lanes(_SpcXRATestBase):
    """Each sub-port spans 8 lanes (SPC4: 800G/split=1)."""

    CONFIG = LanesConfig(
        lanes_per_subport=8,
        chip_dict={
            "SPC4": ChipBandwidth(
                port_split=1,
                port_speed_bps="800000000",
                traffic_packets_per_port=SPCXRAConsts.PACKET_NUM_800G_x1["SPC4"],
            ),
            # SPC5 entry intentionally absent: 800G not supported:
            # https://p4hw-swarm.nvidia.com/view/hw/doc/engr/nswitch/flamingo/arch/IAS/released/SPC4.5_IAS.html#_port_permutations
            # https://nvidia.sharepoint.com/:w:/r/sites/NBU-sales/WW-Ethernet-Switch/Documents/Switch%20Product/z%20Internal/Internal%20-%20MRD/SN5640%20Bison/MRD%20SN5640%20Bison.docx?d=wf031087d688b437d89137cc93ba16009&csf=1&web=1&e=gM5w7l
            # SPC6 entry absent: 8 lanes/sub-port would be 1600G (not in the lab setup).
        }
    )


class TestSpcXRA_4x_lanes(_SpcXRATestBase):
    """Each sub-port spans 4 lanes (SPC4/5: 400G/split=2, SPC6: 800G/split=1)."""

    CONFIG = LanesConfig(
        lanes_per_subport=4,
        chip_dict={
            "SPC4": ChipBandwidth(
                port_split=2,
                port_speed_bps="400000000",
                traffic_packets_per_port=SPCXRAConsts.PACKET_NUM_400G_x2["SPC4"],
            ),
            "SPC5": ChipBandwidth(
                port_split=2,
                port_speed_bps="400000000",
                traffic_packets_per_port=SPCXRAConsts.PACKET_NUM_400G_x2["SPC5"],
            ),
            "SPC6": ChipBandwidth(
                port_split=1,
                port_speed_bps="800000000",
                traffic_packets_per_port=SPCXRAConsts.PACKET_NUM_800G_x1["SPC6"],
            ),
        },
    )


class TestSpcXRA_2x_lanes(_SpcXRATestBase):
    """Each sub-port spans 2 lanes (SPC4/5: 200G/split=4, SPC6: 400G/split=2)."""

    CONFIG = LanesConfig(
        lanes_per_subport=2,
        chip_dict={
            "SPC4": ChipBandwidth(
                port_split=4,
                port_speed_bps="200000000",
                traffic_packets_per_port=SPCXRAConsts.PACKET_NUM_200G_x4["SPC4"],
            ),
            "SPC5": ChipBandwidth(
                port_split=4,
                port_speed_bps="200000000",
                traffic_packets_per_port=SPCXRAConsts.PACKET_NUM_200G_x4["SPC5"],
            ),
            "SPC6": ChipBandwidth(
                port_split=2,
                port_speed_bps="400000000",
                traffic_packets_per_port=SPCXRAConsts.PACKET_NUM_400G_x2["SPC6"],
            ),
        },
    )


class TestSpcXRA_1x_lanes(_SpcXRATestBase):
    """Each sub-port spans 1 lane (SPC5: 100G/split=8, SPC6: 200G/split=4)."""

    CONFIG = LanesConfig(
        lanes_per_subport=1,
        chip_dict={
            "SPC5": ChipBandwidth(
                port_split=8,
                port_speed_bps="100000000",
                # 100G/port -> reuse the 200G packet count (lowest defined tier).
                traffic_packets_per_port=SPCXRAConsts.PACKET_NUM_200G_x4["SPC5"],
            ),
            "SPC6": ChipBandwidth(
                port_split=4,
                port_speed_bps="200000000",
                traffic_packets_per_port=SPCXRAConsts.PACKET_NUM_200G_x4["SPC6"],
                skip_first_iteration_redmine_issue=5144323,
                # Cumulus max-breakout mloop ports need an explicit phy speed
                # (same pattern as Yehuda's SPC6 x8 NVUE bring-up).
                link_phy_speed="200G",
            ),
        },
    )
