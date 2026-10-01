"""Unit tests for the traffic validation helpers used by the performance tests."""

import pytest

from ngts.constants.performance_constants import MRCConsts, ValidationConsts
from ngts.helpers.performance import traffic_helpers
from ngts.helpers.performance.traffic_helpers import (compare_pg_to_reference,
                                                      compare_pg_total_occ_to_reference,
                                                      compare_tc_occ_to_reference,
                                                      validate_counters)

SAMPLE_ID = "sample #1"
DOWNSTREAM_GROUP = MRCConsts.BISECTION_DOWNSTREAM_PORT_GROUP_NAME
UPSTREAM_GROUP = MRCConsts.BISECTION_UPSTREAM_PORT_GROUP_NAME

# (port group, tc, IPv6 reference occAvg, SRv6 measured occAvg) from a failing SPC4 regression run.
# The SRv6 path costs a stable ~2.4 cells more than IPv6 on SPC4, which the plain
# reference-centered +-1 cell band rejects on every single run.
REPORTED_SPC4_SAMPLES = [
    (DOWNSTREAM_GROUP, 1, 0.8271015625, 3.3402109375),
    (DOWNSTREAM_GROUP, 2, 0.808234375, 3.207601562),
    (UPSTREAM_GROUP, 1, 0.847046875, 3.3623125),
    (UPSTREAM_GROUP, 2, 0.8314140625, 3.2241328125),
]

SPC4_EXPECTED_OVERHEAD = MRCConsts.BISECTION_SRV6_OCC_OVERHEAD_BY_CHIP_TYPE["SPC4"]


def build_tc_traffic_json(occupancy_by_group_and_tc):
    """
    Build a minimal traffic validation json holding TC occupancy samples.

    Args:
        occupancy_by_group_and_tc (dict): {(port_group, tc): occAvg}.

    Returns:
        dict: Traffic json shaped like the SDK TrafficValidator output.
    """
    port_groups = {}
    for (group_name, tc_name), occ_avg in occupancy_by_group_and_tc.items():
        tc_dataframe = port_groups.setdefault(group_name, {ValidationConsts.TC_DATAFRAME: []})
        tc_dataframe[ValidationConsts.TC_DATAFRAME].append({
            ValidationConsts.TC_NAME: tc_name,
            ValidationConsts.OCC_AVG: occ_avg,
        })
    return {ValidationConsts.TC_PG_SAMPLES: {SAMPLE_ID: port_groups}}


def run_comparison(measured_by_group_and_tc, reference_by_group_and_tc, allowed_deviation,
                   reference_offset=0):
    """
    Run compare_tc_occ_to_reference over freshly built jsons and return the violations.

    Args:
        measured_by_group_and_tc (dict): {(port_group, tc): occAvg} for the run under test.
        reference_by_group_and_tc (dict): {(port_group, tc): occAvg} for the reference run.
        allowed_deviation: Absolute cells or fractional percentage, as the helper interprets it.
        reference_offset: Expected offset of the measured value from the reference, in cells.

    Returns:
        list: Violation strings produced by the comparison.
    """
    violations_list = []
    compare_tc_occ_to_reference(
        traffic_json=build_tc_traffic_json(measured_by_group_and_tc),
        reference_json=build_tc_traffic_json(reference_by_group_and_tc),
        tc_keys=[ValidationConsts.OCC_AVG],
        tc_to_validate=MRCConsts.TRIMMING_DATA_QUEUE_NUM,
        allowed_deviation=allowed_deviation,
        violations_list=violations_list,
        reference_offset=reference_offset,
    )
    return violations_list


def split_reported_samples():
    """Return the reported SPC4 run as ({key: reference}, {key: measured}) dicts."""
    reference = {(group, tc): reference_occ for group, tc, reference_occ, _ in REPORTED_SPC4_SAMPLES}
    measured = {(group, tc): measured_occ for group, tc, _, measured_occ in REPORTED_SPC4_SAMPLES}
    return reference, measured


@pytest.mark.parametrize("measured_occ,is_violation_expected", [
    (5.0, False),   # exactly on the reference
    (5.9, False),   # inside the +1 cell edge
    (4.1, False),   # inside the -1 cell edge
    (6.1, True),    # above the +1 cell edge
    (3.9, True),    # below the -1 cell edge
])
def test_zero_offset_keeps_reference_centered_band(measured_occ, is_violation_expected):
    """With no offset the band stays centered on the reference, as before the change."""
    violations = run_comparison({(DOWNSTREAM_GROUP, 1): measured_occ},
                                {(DOWNSTREAM_GROUP, 1): 5.0},
                                allowed_deviation=MRCConsts.TC_OCC_ALLOWED_DEVIATION_BISECTION)
    assert bool(violations) is is_violation_expected, violations


def test_reported_spc4_values_fail_without_offset():
    """Without an offset every queue of the reported SPC4 run is rejected."""
    reference, measured = split_reported_samples()
    violations = run_comparison(measured, reference,
                                allowed_deviation=MRCConsts.TC_OCC_ALLOWED_DEVIATION_BISECTION)
    assert len(violations) == len(REPORTED_SPC4_SAMPLES), violations


def test_reported_spc4_values_pass_with_expected_overhead():
    """The reported SPC4 run is accepted once the band allows the expected overhead."""
    reference, measured = split_reported_samples()
    violations = run_comparison(measured, reference,
                                allowed_deviation=MRCConsts.TC_OCC_ALLOWED_DEVIATION_BISECTION,
                                reference_offset=SPC4_EXPECTED_OVERHEAD)
    assert violations == []


def test_offset_rejects_missing_srv6_overhead():
    """An SRv6 result equal to the IPv6 reference is a violation once an offset is expected.

    That is the signature of SRv6 traffic silently falling back to the plain IPv6 path, which
    the reference-centered band could never detect because its lower limit clamps to zero.
    """
    reference, _ = split_reported_samples()
    violations = run_comparison(reference, reference,
                                allowed_deviation=MRCConsts.TC_OCC_ALLOWED_DEVIATION_BISECTION,
                                reference_offset=SPC4_EXPECTED_OVERHEAD)
    assert len(violations) == len(REPORTED_SPC4_SAMPLES), violations


def test_offset_shifts_both_band_limits():
    """The offset moves the whole band, so a value below the shifted lower limit is rejected."""
    reference_occ = 0.8271015625
    shifted_lower_limit = reference_occ + SPC4_EXPECTED_OVERHEAD - 1
    below_limit = run_comparison({(DOWNSTREAM_GROUP, 1): shifted_lower_limit - 0.01},
                                 {(DOWNSTREAM_GROUP, 1): reference_occ},
                                 allowed_deviation=MRCConsts.TC_OCC_ALLOWED_DEVIATION_BISECTION,
                                 reference_offset=SPC4_EXPECTED_OVERHEAD)
    above_limit = run_comparison({(DOWNSTREAM_GROUP, 1): shifted_lower_limit + 0.01},
                                 {(DOWNSTREAM_GROUP, 1): reference_occ},
                                 allowed_deviation=MRCConsts.TC_OCC_ALLOWED_DEVIATION_BISECTION,
                                 reference_offset=SPC4_EXPECTED_OVERHEAD)
    assert len(below_limit) == 1, below_limit
    assert above_limit == []


def test_violation_message_names_the_expected_offset():
    """A violation raised under an offset explains where the shifted band came from."""
    violations = run_comparison({(DOWNSTREAM_GROUP, 1): 99.0},
                                {(DOWNSTREAM_GROUP, 1): 0.8271015625},
                                allowed_deviation=MRCConsts.TC_OCC_ALLOWED_DEVIATION_BISECTION,
                                reference_offset=SPC4_EXPECTED_OVERHEAD)
    assert len(violations) == 1
    assert "expected offset 2.4" in violations[0]
    assert "reference 0.8271015625" in violations[0]


def test_violation_message_unchanged_without_offset():
    """Without an offset the message keeps its original wording."""
    violations = run_comparison({(DOWNSTREAM_GROUP, 1): 99.0},
                                {(DOWNSTREAM_GROUP, 1): 0.8271015625},
                                allowed_deviation=MRCConsts.TC_OCC_ALLOWED_DEVIATION_BISECTION)
    assert len(violations) == 1
    assert "expected offset" not in violations[0]


@pytest.mark.parametrize("chip_type", ["SPC5", "SPC6"])
def test_other_chips_keep_todays_band(chip_type):
    """SPC5 and SPC6 carry a zero offset, so their band is identical to before the change."""
    offset = MRCConsts.BISECTION_SRV6_OCC_OVERHEAD_BY_CHIP_TYPE[chip_type]
    assert offset == 0
    measured = {(DOWNSTREAM_GROUP, 1): 6.8057}
    reference = {(DOWNSTREAM_GROUP, 1): 6.3768}
    with_offset = run_comparison(measured, reference,
                                 allowed_deviation=MRCConsts.TC_OCC_ALLOWED_DEVIATION_BISECTION,
                                 reference_offset=offset)
    without_offset = run_comparison(measured, reference,
                                    allowed_deviation=MRCConsts.TC_OCC_ALLOWED_DEVIATION_BISECTION)
    assert with_offset == without_offset == []


def test_percentage_deviation_still_supported_with_offset():
    """A fractional deviation stays a percentage, applied to the shifted expectation."""
    reference_occ = 10.0
    offset = 2.0
    # 15% of the shifted expectation of 12.0 is 1.8, so 13.7 is inside and 13.9 is outside.
    inside = run_comparison({(DOWNSTREAM_GROUP, 1): 13.7}, {(DOWNSTREAM_GROUP, 1): reference_occ},
                            allowed_deviation=MRCConsts.TC_OCC_ALLOWED_DEVIATION,
                            reference_offset=offset)
    outside = run_comparison({(DOWNSTREAM_GROUP, 1): 13.9}, {(DOWNSTREAM_GROUP, 1): reference_occ},
                             allowed_deviation=MRCConsts.TC_OCC_ALLOWED_DEVIATION,
                             reference_offset=offset)
    assert inside == []
    assert len(outside) == 1, outside


def test_untracked_queues_are_ignored():
    """Only the queues named in tc_to_validate are compared."""
    measured = {(DOWNSTREAM_GROUP, 1): 3.3402109375, (DOWNSTREAM_GROUP, 3): 999.0}
    reference = {(DOWNSTREAM_GROUP, 1): 0.8271015625, (DOWNSTREAM_GROUP, 3): 0.5}
    violations = run_comparison(measured, reference,
                                allowed_deviation=MRCConsts.TC_OCC_ALLOWED_DEVIATION_BISECTION,
                                reference_offset=SPC4_EXPECTED_OVERHEAD)
    assert violations == []


# --- total ingress PG occupancy, summed over the headroom and the shared pool ---

# (port group, headroom occAvg, shared pool occAvg) measured on a real bisection session.
# SPC4 keeps most of the load in the shared pool, SPC6 absorbs it in the headroom, which is
# why neither frame on its own is a usable comparison target across both chips.
SPC4_PG_REFERENCE = [(DOWNSTREAM_GROUP, 15.1235, 51.3895), (UPSTREAM_GROUP, 15.1604, 51.3220)]
SPC4_PG_MEASURED = [(DOWNSTREAM_GROUP, 15.2913, 57.0215), (UPSTREAM_GROUP, 15.2968, 57.1509)]
SPC6_PG_REFERENCE = [(DOWNSTREAM_GROUP, 17.3760, 0.3315), (UPSTREAM_GROUP, 17.4381, 0.3709)]
SPC6_PG_MEASURED = [(DOWNSTREAM_GROUP, 17.7356, 0.3373), (UPSTREAM_GROUP, 17.6941, 0.3647)]


def build_pg_traffic_json(rows, include_headroom=True, include_buffer=True):
    """
    Build a minimal traffic validation json holding ingress PG occupancy samples.

    Args:
        rows (list): [(port_group, headroom occAvg, shared pool occAvg)].
        include_headroom (bool): Emit the headroom frame.
        include_buffer (bool): Emit the shared pool frame.

    Returns:
        dict: Traffic json shaped like the SDK TrafficValidator output.
    """
    port_groups = {}
    for group_name, headroom_occ, buffer_occ in rows:
        group_data = {}
        if include_headroom:
            group_data[ValidationConsts.PG_HEADROOM_DATAFRAME] = [
                {ValidationConsts.PG_HEADROOM: 0, ValidationConsts.OCC_AVG: headroom_occ}]
        if include_buffer:
            group_data[ValidationConsts.PG_BUFFER_DATAFRAME] = [
                {ValidationConsts.PG_BUFFER: 0, ValidationConsts.OCC_AVG: buffer_occ}]
        port_groups[group_name] = group_data
    return {ValidationConsts.TC_PG_SAMPLES: {SAMPLE_ID: port_groups}}


def run_pg_total_comparison(measured_rows, reference_rows,
                            allowed_deviation=MRCConsts.PG_TOTAL_OCC_ALLOWED_DEVIATION,
                            include_headroom=True):
    """
    Run compare_pg_total_occ_to_reference over freshly built jsons and return the violations.

    Args:
        measured_rows (list): [(port_group, headroom occAvg, shared pool occAvg)] under test.
        reference_rows (list): the same shape for the reference run.
        allowed_deviation: percentage or absolute deviation, as the helper interprets it.
        include_headroom (bool): Emit the headroom frame on both sides.

    Returns:
        list: Violation strings produced by the comparison.
    """
    violations_list = []
    compare_pg_total_occ_to_reference(
        traffic_json=build_pg_traffic_json(measured_rows, include_headroom=include_headroom),
        reference_json=build_pg_traffic_json(reference_rows, include_headroom=include_headroom),
        pg_keys=[ValidationConsts.OCC_AVG],
        pg_to_validate=MRCConsts.PG_LIST,
        allowed_deviation=allowed_deviation,
        violations_list=violations_list,
        pg_buffer_types=MRCConsts.PG_TOTAL_OCC_FRAMES,
    )
    return violations_list


@pytest.mark.parametrize("reference_rows,measured_rows", [
    (SPC4_PG_REFERENCE, SPC4_PG_MEASURED),
    (SPC6_PG_REFERENCE, SPC6_PG_MEASURED),
])
def test_measured_pg_totals_pass(reference_rows, measured_rows):
    """Real SPC4 and SPC6 sessions sit inside the total occupancy band."""
    violations = run_pg_total_comparison(measured_rows, reference_rows)
    assert violations == []


def test_pg_total_sums_both_frames():
    """The compared value is the sum of the headroom and the shared pool, not either alone."""
    reference = [(DOWNSTREAM_GROUP, 10.0, 10.0)]
    # Total stays at 20.0 while each frame moves far beyond its own +-15% band.
    measured = [(DOWNSTREAM_GROUP, 19.0, 1.0)]
    assert run_pg_total_comparison(measured, reference) == []


def test_pg_total_catches_headroom_only_regression():
    """A regression confined to the headroom frame is still detected.

    The shared pool dominates the total on SPC4, so this guards against the headroom half
    being carried along without ever influencing the verdict.
    """
    reference = [(DOWNSTREAM_GROUP, 15.1235, 51.3895)]
    # Headroom trebles, shared pool untouched, so the total moves well past 15%.
    measured = [(DOWNSTREAM_GROUP, 45.0, 51.3895)]
    violations = run_pg_total_comparison(measured, reference)
    assert len(violations) == 1, violations
    assert "total (pg_headroom + pg_buffer)" in violations[0]


def test_pg_total_catches_shared_pool_regression():
    """A regression confined to the shared pool frame is still detected."""
    reference = [(DOWNSTREAM_GROUP, 17.3760, 0.3315)]
    measured = [(DOWNSTREAM_GROUP, 17.3760, 25.0)]
    assert len(run_pg_total_comparison(measured, reference)) == 1


def test_pg_total_reports_absent_frame_instead_of_raising():
    """A validator json missing the headroom frame yields a violation, not a KeyError."""
    violations = run_pg_total_comparison(SPC6_PG_MEASURED, SPC6_PG_REFERENCE,
                                         include_headroom=False)
    assert len(violations) == len(SPC6_PG_REFERENCE), violations
    assert "frames absent from the validator json" in violations[0]
    assert ValidationConsts.PG_HEADROOM_DATAFRAME in violations[0]


def test_pg_total_absolute_deviation_branch():
    """An integer deviation is applied as absolute cells rather than a percentage."""
    reference = [(DOWNSTREAM_GROUP, 10.0, 0.5)]
    inside = run_pg_total_comparison([(DOWNSTREAM_GROUP, 11.0, 0.5)], reference,
                                     allowed_deviation=2)
    outside = run_pg_total_comparison([(DOWNSTREAM_GROUP, 13.0, 0.5)], reference,
                                      allowed_deviation=2)
    assert inside == []
    assert len(outside) == 1, outside


def test_pg_total_band_matches_the_configured_deviation():
    """The band edges follow PG_TOTAL_OCC_ALLOWED_DEVIATION exactly."""
    reference_total = 20.0
    reference = [(DOWNSTREAM_GROUP, 10.0, 10.0)]
    deviation = MRCConsts.PG_TOTAL_OCC_ALLOWED_DEVIATION
    just_inside = reference_total * (1 + deviation) - 0.001
    just_outside = reference_total * (1 + deviation) + 0.001
    assert run_pg_total_comparison([(DOWNSTREAM_GROUP, just_inside, 0.0)], reference) == []
    assert len(run_pg_total_comparison([(DOWNSTREAM_GROUP, just_outside, 0.0)], reference)) == 1


# --- shared pool comparison with a band floor ---

# (port group, IPv6 reference occAvg, SRv6 measured occAvg) for the PG0 shared pool.
# SPC6 keeps this pool nearly empty, so its deviation swings in both directions between
# runs while the absolute delta stays under half a cell.
SPC6_SHARED_POOL_SAMPLES = [
    (DOWNSTREAM_GROUP, 0.211528, 0.331261),
    (UPSTREAM_GROUP, 0.203402, 0.367000),
    (DOWNSTREAM_GROUP, 0.331522, 0.337311),
    (UPSTREAM_GROUP, 0.370878, 0.364700),
    (DOWNSTREAM_GROUP, 0.558033, 0.215844),
    (UPSTREAM_GROUP, 0.331639, 0.226739),
]
# SPC4 keeps the shared pool loaded, so the percentage band stays the binding rule there.
SPC4_SHARED_POOL_SAMPLES = [
    (DOWNSTREAM_GROUP, 51.389547, 57.021492),
    (UPSTREAM_GROUP, 51.321984, 57.150867),
]

PG_BAND_FLOOR = MRCConsts.MIN_PG_OCC_BAND_CELLS
PG_BAND_PERCENTAGE = MRCConsts.HEADROOM_ALLOWED_DEVIATION


def build_shared_pool_json(occupancy_by_group):
    """
    Build a minimal traffic validation json holding PG0 shared pool occupancy.

    Args:
        occupancy_by_group (dict): {port_group: occAvg}.

    Returns:
        dict: Traffic json shaped like the SDK TrafficValidator output.
    """
    port_groups = {
        group_name: {ValidationConsts.PG_BUFFER_DATAFRAME: [
            {ValidationConsts.PG_BUFFER: 0, ValidationConsts.OCC_AVG: occ_avg}]}
        for group_name, occ_avg in occupancy_by_group.items()
    }
    return {ValidationConsts.TC_PG_SAMPLES: {SAMPLE_ID: port_groups}}


def run_shared_pool_comparison(measured_by_group, reference_by_group, min_band_cells=0):
    """
    Run compare_pg_to_reference on the shared pool frame and return the violations.

    Args:
        measured_by_group (dict): {port_group: occAvg} for the run under test.
        reference_by_group (dict): {port_group: occAvg} for the reference run.
        min_band_cells: floor for the band half width, in buffer cells.

    Returns:
        list: Violation strings produced by the comparison.
    """
    violations_list = []
    compare_pg_to_reference(
        traffic_json=build_shared_pool_json(measured_by_group),
        reference_json=build_shared_pool_json(reference_by_group),
        pg_keys=[ValidationConsts.OCC_AVG],
        pg_to_validate=MRCConsts.PG_LIST,
        allowed_deviation=PG_BAND_PERCENTAGE,
        violations_list=violations_list,
        pg_buffer_type=ValidationConsts.PG_BUFFER_DATAFRAME,
        min_band_cells=min_band_cells,
    )
    return violations_list


@pytest.mark.parametrize("group_name,reference_occ,measured_occ", SPC6_SHARED_POOL_SAMPLES)
def test_shared_pool_floor_accepts_near_empty_buffer(group_name, reference_occ, measured_occ):
    """Every measured SPC6 shared pool pair sits inside the floored band."""
    violations = run_shared_pool_comparison({group_name: measured_occ},
                                            {group_name: reference_occ},
                                            min_band_cells=PG_BAND_FLOOR)
    assert violations == [], violations


def test_shared_pool_without_floor_rejects_near_empty_buffer():
    """Without the floor the same SPC6 pairs are rejected, which is today's behaviour."""
    rejected = 0
    for group_name, reference_occ, measured_occ in SPC6_SHARED_POOL_SAMPLES:
        if run_shared_pool_comparison({group_name: measured_occ}, {group_name: reference_occ}):
            rejected += 1
    assert rejected == 4, f"expected 4 of 6 rejected without the floor, got {rejected}"


@pytest.mark.parametrize("group_name,reference_occ,measured_occ", SPC4_SHARED_POOL_SAMPLES)
def test_shared_pool_floor_is_inert_on_a_loaded_buffer(group_name, reference_occ, measured_occ):
    """On SPC4 the percentage is the binding rule, so the floor changes nothing."""
    with_floor = run_shared_pool_comparison({group_name: measured_occ},
                                            {group_name: reference_occ},
                                            min_band_cells=PG_BAND_FLOOR)
    without_floor = run_shared_pool_comparison({group_name: measured_occ},
                                               {group_name: reference_occ})
    assert with_floor == without_floor == []


def test_shared_pool_floor_never_narrows_the_band():
    """The floor can only widen a band, so a value that passed still passes.

    Sweeps references either side of the crossover, where the percentage band equals the
    floor, and asserts the floor never turns a pass into a violation.
    """
    crossover = PG_BAND_FLOOR / PG_BAND_PERCENTAGE
    for reference_occ in (0.2, 1.0, crossover / 2, crossover, crossover * 2, 20.0, 51.4):
        for offset in (0.0, reference_occ * PG_BAND_PERCENTAGE):
            measured_occ = reference_occ + offset
            without_floor = run_shared_pool_comparison({DOWNSTREAM_GROUP: measured_occ},
                                                       {DOWNSTREAM_GROUP: reference_occ})
            with_floor = run_shared_pool_comparison({DOWNSTREAM_GROUP: measured_occ},
                                                    {DOWNSTREAM_GROUP: reference_occ},
                                                    min_band_cells=PG_BAND_FLOOR)
            assert not (without_floor == [] and with_floor != []), (
                f"floor narrowed the band at reference={reference_occ}, measured={measured_occ}")


def test_shared_pool_percentage_binds_above_the_crossover():
    """Above the crossover the percentage is wider, so the floor stops being the limit."""
    reference_occ = 40.0
    # 15% of 40 is 6 cells, far beyond the 1 cell floor, so a 5 cell rise must still pass.
    assert run_shared_pool_comparison({DOWNSTREAM_GROUP: reference_occ + 5.0},
                                      {DOWNSTREAM_GROUP: reference_occ},
                                      min_band_cells=PG_BAND_FLOOR) == []
    # A 7 cell rise exceeds the percentage band and must be reported.
    assert len(run_shared_pool_comparison({DOWNSTREAM_GROUP: reference_occ + 7.0},
                                          {DOWNSTREAM_GROUP: reference_occ},
                                          min_band_cells=PG_BAND_FLOOR)) == 1


def test_shared_pool_floor_still_catches_a_loaded_shared_pool():
    """A near-empty reference with a sustained rise past the floor is still reported."""
    violations = run_shared_pool_comparison({DOWNSTREAM_GROUP: 5.0},
                                            {DOWNSTREAM_GROUP: 0.2158},
                                            min_band_cells=PG_BAND_FLOOR)
    assert len(violations) == 1, violations
    assert f"band widened to the {PG_BAND_FLOOR} cell floor" in violations[0]


# --- counters validation: Bug #5258617 ramp-up ports ---

LEFT_PORTS_GROUP = "left_ports"
FIRST_WAIVED_PORT, SECOND_WAIVED_PORT = traffic_helpers.PAUSE_DISCARDS_PORTS
# The neighbouring port of the same cage in a 4x breakout, where 0x10102 does not exist.
OTHER_PORT = "0x10105"
MAC_CONTROL_COUNTER = "a_mac_control_frames_transmitted"
PAUSE_COUNTER = "a_pause_mac_ctrl_frames_transmitted"
DISCARDS_COUNTER = "if_in_discards"
DROP_EVENTS_COUNTER = "ether_stats_drop_events"
NO_BUFFER_COUNTER = "port_rx_no_buffer"
UNWAIVED_COUNTER = "if_out_discards"

# Counters raised by the ramp-up, as reported by the SDK for one port of a failing regression run:
# the pause pair is one event and the three drop counters are another, hence the near-equal values.
RAMP_UP_COUNTERS = {MAC_CONTROL_COUNTER: 11108890,
                    PAUSE_COUNTER: 11108890,
                    DISCARDS_COUNTER: 15815493,
                    DROP_EVENTS_COUNTER: 15815502,
                    NO_BUFFER_COUNTER: 15815584}


def build_counters_sample(counters_by_port, port_group_name=LEFT_PORTS_GROUP):
    """
    Build a counters sample shaped like the TrafficValidator output.

    Every row carries every counter mentioned by any port, defaulting to 0, matching the validator
    which reports the same counter set for all ports of a group.

    Args:
        counters_by_port (dict): {port: {counter: value}}.
        port_group_name (str): Port group the ports belong to.

    Returns:
        dict: A single sample's port groups, keyed by port group name.
    """
    all_counters = sorted({counter for counters in counters_by_port.values() for counter in counters})
    counters_dataframe = [{ValidationConsts.PORT: port,
                           **{counter: counters.get(counter, 0) for counter in all_counters}}
                          for port, counters in counters_by_port.items()]
    return {port_group_name: {ValidationConsts.COUNTERS_DATAFRAME: counters_dataframe}}


def build_counters_traffic_json(counters_by_port_per_sample):
    """
    Build a traffic validation json holding one counters sample per entry.

    Args:
        counters_by_port_per_sample (dict): {sample ordinal: {port: {counter: value}}}.

    Returns:
        dict: Traffic json shaped like the SDK TrafficValidator output.
    """
    samples = {f"sample #{sample_number} 2026-09-06 07:51:{sample_number:02d}":
               build_counters_sample(counters_by_port)
               for sample_number, counters_by_port in counters_by_port_per_sample.items()}
    return {ValidationConsts.COUNTERS_SAMPLES: samples}


def run_counters_validation(counters_by_port_per_sample, skip_first_counters_iteration=False):
    """
    Run validate_counters over freshly built samples and return the violations.

    Args:
        counters_by_port_per_sample (dict): {sample ordinal: {port: {counter: value}}}.
        skip_first_counters_iteration (bool): Forwarded to validate_counters.

    Returns:
        list: Violation strings produced by the validation.
    """
    violations_list = []
    validate_counters(traffic_json=build_counters_traffic_json(counters_by_port_per_sample),
                      skip_first_counters_iteration=skip_first_counters_iteration,
                      ignore_counter_list=[],
                      violations_list=violations_list)
    return violations_list


@pytest.fixture
def force_ramp_up_issue_open(monkeypatch):
    """Hold Bug #5258617 open so the waiver is exercised whatever its live Redmine state is."""
    monkeypatch.setattr(traffic_helpers, "PAUSE_DISCARDS_LAST_TOLERATED_SAMPLE", 2)


@pytest.fixture
def force_ramp_up_issue_closed(monkeypatch):
    """Close Bug #5258617 so the strict zero-counters rule is exercised from the first sample."""
    monkeypatch.setattr(traffic_helpers, "PAUSE_DISCARDS_LAST_TOLERATED_SAMPLE", 0)


@pytest.mark.parametrize("sample_number", [1, 2])
def test_ramp_up_counters_are_waived_on_the_affected_ports(force_ramp_up_issue_open, sample_number):
    """All five counters pass on both affected ports, in either of the two early samples."""
    violations = run_counters_validation({sample_number: {FIRST_WAIVED_PORT: RAMP_UP_COUNTERS,
                                                          SECOND_WAIVED_PORT: RAMP_UP_COUNTERS,
                                                          OTHER_PORT: {}}})
    assert violations == [], violations


def test_ramp_up_counters_on_other_ports_are_still_reported(force_ramp_up_issue_open):
    """The rest of the switch keeps being verified, and the waived ports drop out of the report."""
    violations = run_counters_validation({1: {FIRST_WAIVED_PORT: RAMP_UP_COUNTERS,
                                              OTHER_PORT: RAMP_UP_COUNTERS}})
    assert len(violations) == 1, violations
    assert OTHER_PORT in violations[0]
    assert FIRST_WAIVED_PORT not in violations[0]
    assert PAUSE_COUNTER in violations[0] and NO_BUFFER_COUNTER in violations[0]


def test_other_counters_on_affected_ports_are_still_reported(force_ramp_up_issue_open):
    """The waiver covers five named counters, not everything the affected ports report."""
    violations = run_counters_validation({1: {FIRST_WAIVED_PORT: {**RAMP_UP_COUNTERS,
                                                                  UNWAIVED_COUNTER: 4},
                                              OTHER_PORT: {}}})
    assert len(violations) == 1, violations
    assert UNWAIVED_COUNTER in violations[0] and FIRST_WAIVED_PORT in violations[0]
    assert PAUSE_COUNTER not in violations[0]


def test_waiver_applies_when_the_breakout_hides_one_port(force_ramp_up_issue_open):
    """In the wider breakouts 0x10102 is absent, which must not stop 0x10101 being waived."""
    violations = run_counters_validation({1: {FIRST_WAIVED_PORT: RAMP_UP_COUNTERS, OTHER_PORT: {}}})
    assert violations == [], violations


def test_ramp_up_counters_after_the_second_sample_are_reported(force_ramp_up_issue_open):
    """The bug is a ramp-up effect, so the same counters must fail once the traffic settled."""
    violations = run_counters_validation({1: {FIRST_WAIVED_PORT: RAMP_UP_COUNTERS},
                                          3: {FIRST_WAIVED_PORT: RAMP_UP_COUNTERS}})
    assert len(violations) == 1, violations
    assert "sample #3" in violations[0] and FIRST_WAIVED_PORT in violations[0]


def test_ramp_up_counters_reported_when_the_issue_is_closed(force_ramp_up_issue_closed):
    """Closing the bug restores strict checking from the first sample, with no code change."""
    violations = run_counters_validation({1: {FIRST_WAIVED_PORT: RAMP_UP_COUNTERS}})
    assert len(violations) == 1, violations
    assert "sample #1" in violations[0] and FIRST_WAIVED_PORT in violations[0]
