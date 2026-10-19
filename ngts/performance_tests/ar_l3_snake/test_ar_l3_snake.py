"""AR L3 snake DVS performance tests: four adjacency scenarios on 4 / 2 / 1 lanes per sub-port.

One-way inject from right_tg into the single snake head, traverse DUT body
ports with internal phys-loopback, exit on the left_tg snake tail. Validates
via TrafficValidator / ValidationConfig.

DVS only: the scenario has no NVUE or SONiC templates, so the split rendered
into the templates is the SDK-relative one and needs no absolute-breakout
conversion.

Test classes are keyed by lanes-per-sub-port, because the split that reaches a
given per-port speed differs per chip (SPC4/SPC5 are 100 Gbps/lane; SPC6 is
200 Gbps/lane and boots at a 2x default breakout, so the same lane count is
reached with half the split). The lanes table is also what gates the supported
chips: a chip missing from a lane config skips there. The snake is
characterized at 200G per port and up, which lands the matrix on SPC6 at 4 / 2
/ 1 lanes and SPC4 at 4 / 2 lanes - SPC4 at 1 lane would be 100G, so it skips.

Apart from that split and its seed-packet count, nothing in the configuration
differs per chip.

Every scenario runs the same replicated exact-prefix routes, the same
per-family DIP sweep, the same frame size and the same validation. What differs
is how each hop's adjacency is programmed on the DUT, and whether the injected
traffic is AR-eligible:

  - ``ar_ecmp`` with AR-ineligible traffic (S1): AR RIFs on vports and adaptive
    stateful ECMPs, driven by plain UDP that carries no BTH, with the AR
    classifier keyed on something the snake never sends. Every lookup still
    goes through an AR container; AR is never asked to steer.
  - ``ar_ecmp`` with AR-eligible traffic (S1-AR): the same containers, driven
    by RoCE whose BTH sets the AR bit, with the classifier keyed on that bit.
  - ``local_route`` (S2): local routes to connected neighbors, no ECMP.
  - ``lag_route`` (S3): S2 over a one-member PortChannel per snake port, so the
    RIF and every neighbor sit on a LAG instead of a physical port.

The matrix is scenario x lanes-per-sub-port, generated from the two tables at
the bottom of this module.
"""

import logging
from dataclasses import dataclass, field
from functools import wraps
from typing import ClassVar, Dict, Optional, Tuple

import allure

from ngts.constants.performance_constants import PerfConsts, ValidationConsts
from ngts.helpers.performance.performance_db_helpers import get_perf_test_name
from ngts.helpers.performance.performance_setup_helpers import (
    ValidationConfig, configure_incremental_dips_on_tg, run_traffic, run_validation)
from ngts.performance_tests.ar_l3_snake.conftest import TESTS_SCENARIO

logger = logging.getLogger()

# MultiNosTGConfig.pre_test turns this into the TG port ETS max shaper rate,
# in Gbps, as port_speed * value. 1.0 puts the ceiling at the whole port speed,
# so both the FWS generator and the port injecting into the snake run at line rate.
SNAKE_SHAPER_VALUE = 0.995
# Minimum utilization every snake port must sustain. It stays under the injected
# line rate because the on-wire overhead of each frame (preamble and
# inter-packet gap) is not counted as payload bandwidth.
SNAKE_MIN_BW_UTILIZATION = PerfConsts.DVS_SHAPER_VALUE
# The one frame size the snake runs at. The right_tg seed streams carry it and
# the FWS flood replicates it onto the wire.
SNAKE_PACKET_SIZE = 2750

# Adjacency scenario ids. These are passed to the DUT as ``snake_variant`` and
# must stay in sync with SNAKE_VARIANTS in ar_l3_snake_dut.py.
VARIANT_AR_ECMP = "ar_ecmp"
VARIANT_LOCAL_ROUTE = "local_route"
VARIANT_LAG_ROUTE = "lag_route"


@dataclass(frozen=True)
class ChipLanes:
    """What one chip needs to reach a lanes-per-sub-port scenario.

    Attributes:
        port_split: Number of sub-ports each physical port is split into, as the
            SDK takes it - relative to the breakout the chip already boots at.
        port_speed_gbps: Resulting per-port line rate, in Gbps. Naming only.
        seed_packets: Seed packets injected per route family, empirically tuned
            per port speed at SNAKE_PACKET_SIZE.
    """

    port_split: int
    port_speed_gbps: int
    seed_packets: int


@dataclass(frozen=True)
class SnakeLanesConfig:
    """One lanes-per-sub-port scenario across the chips that support it.

    A chip absent from ``chip_dict`` has no lab setup at this lane count, and
    the test class skips there.

    Attributes:
        lanes_per_subport: Lanes each sub-port spans (1, 2 or 4).
        chip_dict: Chip type (e.g. ``"SPC4"``) to its split and seed count.
    """

    lanes_per_subport: int
    chip_dict: Dict[str, ChipLanes] = field(default_factory=dict)

    def for_chip(self, chip_type: str) -> Optional[ChipLanes]:
        """Return the chip's entry, or None when the test should skip on it.

        Args:
            chip_type: Chip type identifier (e.g. ``"SPC4"``, ``"SPC6"``).

        Returns:
            The ``ChipLanes`` registered under ``chip_type``, or None.
        """
        return self.chip_dict.get(chip_type)

    def name(self, chip_type: str) -> str:
        """Build the scenario name from the chip's split and per-port speed.

        Args:
            chip_type: Chip type identifier.

        Returns:
            String like ``x2_400G``.
        """
        chip_lanes = self.chip_dict[chip_type]
        return f"x{chip_lanes.port_split}_{chip_lanes.port_speed_gbps}G"

    def mongo_conf_name(self, chip_type: str) -> str:
        """Build the Mongo CONF_NAME entry for this config and chip.

        Args:
            chip_type: Chip type identifier.

        Returns:
            String like ``x2_400G_ar_l3_snake``.
        """
        return f"{self.name(chip_type)}_{TESTS_SCENARIO}"

    def allure_title_suffix(self, chip_type: str) -> str:
        """Build the bracketed Allure-title suffix for this config and chip.

        Args:
            chip_type: Chip type identifier.

        Returns:
            A suffix such as ``"[400G, split=2, 4 lanes per sub-port]"``.
        """
        chip_lanes = self.chip_dict[chip_type]
        return (f"[{chip_lanes.port_speed_gbps}G, split={chip_lanes.port_split}, "
                f"{self.lanes_per_subport} lanes per sub-port]")

    def to_template_args(self, chip_type: str) -> dict:
        """Render the lane-dependent half of ``conf_args`` for the Jinja templates.

        The route model, the DIP sweep and the adjacency scenario are added by
        the conftest; everything here follows from the chip's split.

        Args:
            chip_type: Chip type identifier; selects the per-chip entry.

        Returns:
            dict: Lane-dependent conf_args for the ar_l3_snake templates.
        """
        chip_lanes = self.chip_dict[chip_type]
        return {
            "scenario": TESTS_SCENARIO,
            "chip_type": chip_type,
            # The route families sweep both address families regardless of this,
            # which only picks the TG's own link addressing.
            "is_ipv6": False,
            "split_left": chip_lanes.port_split,
            "split_right": chip_lanes.port_split,
            "shaper_value": SNAKE_SHAPER_VALUE,
            "packet_size": SNAKE_PACKET_SIZE,
            "right_num_packets": chip_lanes.seed_packets,
        }

    def snake_bandwidth_threshold(self) -> dict:
        """Minimum BW thresholds keyed by snake_heads, snake_tails, and snake_body.

        The snake is one-way, so the two edge ports carry traffic in a single
        direction: the head only receives the right_tg inject (its route egresses
        the next port, so head TX is 0) and the tail only transmits toward
        left_tg (it has no phys-loopback and no route, so tail RX is 0). Only the
        phys-looped body ports carry line rate in both directions.

        Returns:
            Nested dict for validate_bw keyed by port group name.
        """
        required_bw = SNAKE_MIN_BW_UTILIZATION
        return {
            "snake_heads": {ValidationConsts.TX: 0, ValidationConsts.RX: required_bw},
            "snake_tails": {ValidationConsts.TX: required_bw, ValidationConsts.RX: 0},
            "snake_body": {ValidationConsts.TX: required_bw, ValidationConsts.RX: required_bw},
        }


def _with_lanes_allure_title(method):
    """Prepend speed/split suffix to the Allure report title."""

    @wraps(method)
    def wrapper(self, request, *args, **kwargs):
        allure.dynamic.title(
            f"{request.node.name} {self.CONFIG.allure_title_suffix(self.chip_type)}")
        return method(self, request, *args, **kwargs)

    return wrapper


# SPC4/SPC5: 400G/split=2. SPC6: 800G/split=1.
CONFIG_4X_LANES = SnakeLanesConfig(
    lanes_per_subport=4,
    chip_dict={
        "SPC4": ChipLanes(port_split=2, port_speed_gbps=400, seed_packets=8),
        "SPC5": ChipLanes(port_split=2, port_speed_gbps=400, seed_packets=8),
        "SPC6": ChipLanes(port_split=1, port_speed_gbps=800, seed_packets=30),
    })
# SPC4/SPC5: 200G/split=4. SPC6: 400G/split=2.
CONFIG_2X_LANES = SnakeLanesConfig(
    lanes_per_subport=2,
    chip_dict={
        "SPC4": ChipLanes(port_split=4, port_speed_gbps=200, seed_packets=6),
        "SPC5": ChipLanes(port_split=4, port_speed_gbps=200, seed_packets=6),
        "SPC6": ChipLanes(port_split=2, port_speed_gbps=400, seed_packets=12),
    })
# SPC5: 100G/split=8. SPC6: 200G/split=4. No SPC4 entry, so 1 lane skips on SPC4.
CONFIG_1X_LANES = SnakeLanesConfig(
    lanes_per_subport=1,
    chip_dict={
        "SPC5": ChipLanes(port_split=8, port_speed_gbps=100, seed_packets=6),
        "SPC6": ChipLanes(port_split=4, port_speed_gbps=200, seed_packets=9),
    })


class _ArL3SnakeTestBase:
    """Shared setup and test flow of the replicated-route AR L3 snake lane configurations.

    The test cases live on the two subclasses below; this class holds what they
    share, so a generated class carries exactly one of them.

    Class attributes:
        CONFIG: The lanes-per-sub-port config the generated subclass pins.
        SNAKE_VARIANT: How each hop's adjacency is programmed on the DUT, one of
            VARIANT_AR_ECMP, VARIANT_LOCAL_ROUTE or VARIANT_LAG_ROUTE.
        AR_TRAFFIC: Whether the scenario is AR-eligible. True injects RoCE with
            the BTH AR bit and keys the DUT's AR classifier on it; False
            injects plain UDP with no BTH and keys the classifier on something
            the snake never sends. That pair is all that separates S1 from
            S1-AR - the routes, the neighbors and the AR containers are the
            same either way.
        ROUTE_FAMILIES: Names of the route families to run, out of
            ``conftest.ROUTE_FAMILY_SPECS``. Empty means all of them, which is
            what every generated class uses; pin it on a local subclass to sweep
            one family in isolation.
        FLOW_TARGET: Distinct 5-tuple flows the TG generates. 0 keeps one flow
            per DIP; a non-zero target adds a UDP SPORT sweep under the DIP sweep
            (SIP, DPORT and protocol stay fixed) so the flows land on distinct
            DUT AR flow-table entries.
    """

    CONFIG: ClassVar[Optional[SnakeLanesConfig]] = None
    SNAKE_VARIANT: ClassVar[str] = VARIANT_AR_ECMP
    AR_TRAFFIC: ClassVar[bool] = True
    ROUTE_FAMILIES: ClassVar[Tuple[str, ...]] = ()
    FLOW_TARGET: ClassVar[int] = 0

    def _run_snake_and_validate(self, request):
        """Install the TG sweep, inject the snake, check route hits and validate.

        Args:
            request: pytest FixtureRequest.
        """
        test_name = get_perf_test_name(request)
        if self.conf_args["sport_sweep"]:
            sport_sweep = self.conf_args["sport_sweep"]
            allure.dynamic.parameter("flows", sport_sweep["flows"])
            logger.info(f"TG generates {sport_sweep['flows']} flows: SPORT {sport_sweep['start']}.."
                        f"{sport_sweep['start'] + sport_sweep['count'] - 1} under every swept DIP")

        with allure.step("Install the per-family DIP sweep on right_tg"):
            configure_incremental_dips_on_tg(
                self.players, players_aliases=[PerfConsts.RIGHT_TG_ALIAS])

        with allure.step(f"Run {SNAKE_PACKET_SIZE}B one-way snake traffic on right_tg"):
            run_traffic(self.players, self.scenario, self.traffic_jsons, step="Running one-way snake traffic on right_tg",
                        players_aliases=[PerfConsts.RIGHT_TG_ALIAS])

        self._check_and_validate(test_name)

    def _check_and_validate(self, test_name):
        """Check route hits with traffic up, then validate it.

        Args:
            test_name: Perf-DB test name, from get_perf_test_name.
        """
        with allure.step("Verify the sampled replicated routes were actually hit"):
            self.cli_object.performance.verify_ar_l3_snake_route_hits()

        allure.dynamic.parameter("injected_frame_size_bytes", SNAKE_PACKET_SIZE)
        with allure.step(f"Validate snake traffic at {SNAKE_PACKET_SIZE}B frames"):
            run_validation(ValidationConfig(
                players=self.players,
                test_name=test_name,
                scenario=self.scenario,
                chip_type=self.chip_type,
                bw_threshold=self.CONFIG.snake_bandwidth_threshold(),
                tc_occ_threshold=None,
                run_validate_counters=True,
                power_threshold=self.power_thresholds_by_chip_type,
                skip_first_counters_iteration=False,
                samples_params_dict=PerfConsts.SAMPLES_PARAMS,
                validate_bw_rx=True,
            ))


class _ArL3SnakeMaxBandwidthBase(_ArL3SnakeTestBase):
    """The base snake case: one flow per swept DIP."""

    @_with_lanes_allure_title
    @allure.description(
        "One-way AR L3 snake with internal phys-loopback. Inject on right_tg "
        "heads, exit on left_tg tails, validate BW/counters via TrafficValidator.")
    def test_ar_l3_snake_max_bandwidth(self, request):
        """Run one-way snake traffic and validate with TrafficValidator.

        Args:
            request: pytest FixtureRequest.
        """
        self._run_snake_and_validate(request)


class _ArL3Snake100kFlowsBase(_ArL3SnakeTestBase):
    """The 100K-flow snake case: the same snake, driven by 100K distinct 5-tuples.

    Its own class rather than a second method of the base one, because the TG
    sweep is rendered at class setup and its ACL stays bound to the inject ports
    until the class restore.
    """

    FLOW_TARGET: ClassVar[int] = 100_000

    @_with_lanes_allure_title
    @allure.description(
        "One-way AR L3 snake driven by 100K distinct 5-tuple flows (DIP x UDP SPORT) "
        "to spread traffic across the DUT AR flow table; same gates as the base case.")
    def test_ar_l3_snake_100k_flows(self, request):
        """Run one-way snake traffic over 100K flows and validate with TrafficValidator.

        Args:
            request: pytest FixtureRequest.
        """
        self._run_snake_and_validate(request)


# The generated matrix: one test class per (scenario, lanes, case). Add a row to
# any table to widen it.
_LANE_CONFIGS = (("4x", CONFIG_4X_LANES), ("2x", CONFIG_2X_LANES), ("1x", CONFIG_1X_LANES))
# (class-name prefix, snake_variant, AR-eligible traffic, scenario id in the
# test plan). S1 and S1-AR build the same snake and differ only in AR
# eligibility; S1 keeps the prefix without a suffix, so existing MARS test lists and
# tools keep resolving. S2 and S3 have no ECMP container to steer, and keep
# injecting the AR-eligible RoCE they were characterized on.
_SNAKE_VARIANTS = (("TestArL3Snake", VARIANT_AR_ECMP, False, "S1"),
                   ("TestArL3SnakeAr", VARIANT_AR_ECMP, True, "S1-AR"),
                   ("TestArL3SnakeLocal", VARIANT_LOCAL_ROUTE, True, "S2"),
                   ("TestArL3SnakeLag", VARIANT_LAG_ROUTE, True, "S3"))
# (class-name suffix, test-case base, description). The empty suffix keeps the
# base case's existing class names.
_SNAKE_CASES = (("", _ArL3SnakeMaxBandwidthBase, "snake"),
                ("100kFlows", _ArL3Snake100kFlowsBase, "100K-flow snake"))


def _build_variant_test_classes():
    """Create one test class per scenario / lanes / test-case combination.

    Each class is a thin subclass of one test-case base holding only its lane
    config and adjacency scenario, published in this module under a stable name
    of the form ``TestArL3Snake[Ar|Local|Lag][100kFlows]_<lanes>_lanes``.

    Returns:
        list: Generated class names, in creation order.
    """
    created = []
    for prefix, variant, ar_traffic, scenario in _SNAKE_VARIANTS:
        for suffix, base, case in _SNAKE_CASES:
            for lanes, config in _LANE_CONFIGS:
                name = f"{prefix}{suffix}_{lanes}_lanes"
                globals()[name] = type(name, (base,), {
                    "__doc__": f"{scenario} ({variant}, {'RoCE with AR bit' if ar_traffic else 'plain UDP, no BTH'}) "
                    f"{case}, {lanes} lanes per sub-port.",
                    "CONFIG": config,
                    "SNAKE_VARIANT": variant,
                    "AR_TRAFFIC": ar_traffic,
                })
                created.append(name)
    return created


_GENERATED_TEST_CLASSES = _build_variant_test_classes()
