"""Unit tests for the reference comparison helpers used by the SRv6 performance tests."""

import pytest

from ngts.constants.performance_constants import MRCConsts, ValidationConsts
from ngts.helpers.performance.traffic_helpers import compare_tc_occ_to_reference

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
