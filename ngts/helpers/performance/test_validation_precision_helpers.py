import numbers

import numpy as np
import pandas as pd
import pytest

from ngts.helpers.performance.validation_precision_helpers import (
    round_pair_for_comparison,
    round_to_threshold_precision,
    threshold_decimal_places,
    value_above_threshold,
    value_below_threshold,
)


@pytest.mark.parametrize("threshold,expected_places", [
    (0.975, 3),
    (0.97, 2),
    (1, 0),
    (100, 0),
    (0.05, 2),
])
def test_threshold_decimal_places(threshold, expected_places):
    assert threshold_decimal_places(threshold) == expected_places


def test_round_to_threshold_precision_bw_example():
    threshold = 0.975
    value = 0.974999999005
    assert round_to_threshold_precision(value, threshold) == 0.975
    assert not value_below_threshold(value, threshold)


def test_round_to_threshold_precision_still_fails_when_below_threshold():
    threshold = 0.975
    value = 0.974
    assert round_to_threshold_precision(value, threshold) == 0.974
    assert value_below_threshold(value, threshold)


def test_round_pair_for_comparison():
    assert round_pair_for_comparison(0.974999999005, 0.975) == (0.975, 0.975)


@pytest.mark.parametrize("value", [
    np.float64(0.974999999005),
    np.float32(0.974999999005),
    pd.Series([0.974999999005]).iloc[0],
    pd.DataFrame({"tx": [0.974999999005]})["tx"].values[0],
])
def test_round_pair_for_comparison_numpy_pandas_scalars(value):
    # traffic_helpers feeds DataFrame .values[0] / Series scalars into these helpers.
    assert isinstance(value, numbers.Real)
    assert round_pair_for_comparison(value, 0.975) == (0.975, 0.975)
    assert not value_below_threshold(value, 0.975)


@pytest.mark.parametrize("threshold", [np.int64(100), np.int32(0)])
def test_numpy_integral_threshold_uses_zero_decimal_places(threshold):
    assert threshold_decimal_places(threshold) == 0
    assert isinstance(threshold, numbers.Integral)


@pytest.mark.parametrize("value,threshold", [
    (True, 0.975),
    (False, 1),
    ("0.97", 0.975),
    (None, 0.975),
    (0.974999999005, True),
    (0.974999999005, "0.975"),
])
def test_round_pair_for_comparison_non_numeric_is_noop(value, threshold):
    assert round_pair_for_comparison(value, threshold) == (value, threshold)


def test_round_to_threshold_precision_bool_value_is_noop():
    assert round_to_threshold_precision(True, 0.975) is True


def test_value_above_threshold():
    assert value_above_threshold(100.4, 100)
    assert not value_above_threshold(99.9, 100)
    assert value_above_threshold(100.45, 100.4)
    assert not value_above_threshold(100.35, 100.4)


def test_zero_decimal_threshold_compares_raw_values():
    # Rounding to whole numbers would hide violations, so 0-place thresholds compare raw.
    assert round_pair_for_comparison(100.4, 100) == (100.4, 100.0)
    assert value_below_threshold(99.6, 100)


def test_zero_threshold_detects_traffic_on_down_port():
    assert value_above_threshold(0.4, 0)
    assert not value_above_threshold(0.0, 0)


def test_zero_decimal_precision_keeps_fractional_values():
    # Whole-number thresholds must not crush 0.4 / 0.8 into 0 / 1.
    assert round_to_threshold_precision(0.4, 1) == 0.4
    assert round_to_threshold_precision(0.8, 1) == 0.8
    assert round_pair_for_comparison(0.4, 1) == (0.4, 1.0)
    assert round_pair_for_comparison(0.8, 1) == (0.8, 1.0)


@pytest.mark.parametrize("threshold", [0.4, 0.8])
def test_fractional_threshold_precision_is_preserved(threshold):
    places = threshold_decimal_places(threshold)
    assert places == 1
    noisy = threshold - 5e-12
    assert round_to_threshold_precision(noisy, threshold) == threshold
    assert round_pair_for_comparison(noisy, threshold) == (threshold, threshold)
