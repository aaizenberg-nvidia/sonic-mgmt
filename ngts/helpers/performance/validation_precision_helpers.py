"""Round measured metrics to threshold decimal precision before validation comparisons."""

import numbers


def _is_comparable_number(value):
    """
    Return True for real numbers, including numpy/pandas scalars.

    Built-in ``bool`` is a ``numbers.Real`` (via ``int``) and is excluded.
    ``numbers.Real`` also covers numpy scalars such as ``float32`` / ``int64``
    that are not subclasses of built-in ``float`` / ``int``.
    """
    return isinstance(value, numbers.Real) and not isinstance(value, bool)


def threshold_decimal_places(threshold):
    """
    Return the number of decimal places implied by a numeric threshold.

    Args:
        threshold: Numeric threshold value.

    Returns:
        Number of decimal places for ints, or None when threshold is not numeric.
    """
    if not _is_comparable_number(threshold):
        return None
    if isinstance(threshold, numbers.Integral):
        return 0
    threshold_str = f"{threshold:.12f}".rstrip('0').rstrip('.')
    if '.' not in threshold_str:
        return 0
    return len(threshold_str.split('.')[1])


def round_to_threshold_precision(value, threshold):
    """
    Round a measured value to the decimal precision of its threshold.

    Floating-point accumulation (for example ``0.974999999005`` against threshold
    ``0.975``) can fail strict comparisons even when the metric is within spec.
    Rounding the measured value to the threshold's precision avoids those false
    failures.

    Thresholds that imply 0 decimal places (integers such as ``0`` or ``1``) leave
    the measured value as a raw float. Rounding to whole numbers would turn real
    fractions like ``0.4`` / ``0.8`` into ``0`` / ``1``.

    Args:
        value: Measured metric value.
        threshold: Threshold whose decimal precision defines rounding.

    Returns:
        Value rounded to the threshold's non-zero decimal precision; raw
        ``float(value)`` when the threshold implies 0 places; otherwise the
        original value for non-numeric inputs.
    """
    if not _is_comparable_number(value):
        return value
    decimal_places = threshold_decimal_places(threshold)
    if decimal_places is None:
        return value
    if decimal_places == 0:
        return float(value)
    return round(float(value), decimal_places)


def round_pair_for_comparison(value, threshold):
    """
    Round a measured value and its threshold to a shared comparison precision.

    Non-numeric inputs (including ``bool``, which is a subclass of ``int``) are
    returned unchanged so callers get the same no-op policy as
    :func:`round_to_threshold_precision` instead of a surprising ``float()``
    coercion.

    Thresholds that imply 0 decimal places (integers such as ``0`` or ``100``) are
    compared as raw floats. Rounding to whole numbers would hide real violations:
    with ``bw_threshold=0``, a down port measuring ``0.4`` would round to ``0.0``
    and compare equal to its threshold.

    Args:
        value: Measured metric value.
        threshold: Threshold to compare against.

    Returns:
        Tuple of ``(value, threshold)`` as floats, rounded to the threshold's
        decimal precision when that precision is non-zero. Non-numeric inputs
        (``bool`` included) are returned unchanged.
    """
    if not _is_comparable_number(value) or not _is_comparable_number(threshold):
        return value, threshold
    decimal_places = threshold_decimal_places(threshold)
    if not decimal_places:
        return float(value), float(threshold)
    return round(float(value), decimal_places), round(float(threshold), decimal_places)


def value_below_threshold(value, threshold):
    """Return True when the rounded value is strictly below the rounded threshold."""
    rounded_value, rounded_threshold = round_pair_for_comparison(value, threshold)
    return rounded_value < rounded_threshold


def value_above_threshold(value, threshold):
    """Return True when the rounded value is strictly above the rounded threshold."""
    rounded_value, rounded_threshold = round_pair_for_comparison(value, threshold)
    return rounded_value > rounded_threshold


def round_for_reference_comparison(value, reference_value):
    """
    Round a measured value using the decimal precision of a reference baseline.

    Args:
        value: Measured metric value.
        reference_value: Reference baseline used to derive comparison precision.

    Returns:
        Value rounded to reference precision when both inputs are numeric.
    """
    return round_to_threshold_precision(value, reference_value)
