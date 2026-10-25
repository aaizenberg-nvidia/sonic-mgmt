"""
Helper functions related to alarm and environment validation in NVOS QA framework.

This module provides utility methods for alarm-related checks,
fan grouping, Docker state validation, and time conversions,
used by NVOS system and telemetry tests.
"""

import re
from retry import retry
from datetime import datetime, timezone


NS_PER_SEC = 10 ** 9


def get_fan_drawers(fan_show_output):
    """
    Group the fan instances of "nv show platform environment fan" by their drawer index.

    hw-management indexes fan{N}_status per drawer, while fan{N}_fault and fan{N}_speed_get are
    indexed per rotor, so a drawer with two rotors has a single status file and two fault files.
    :return: dict of drawer id to fan instance names, example: {1: ["FAN1/1", "FAN1/2"]}
    """
    drawers = {}
    for key in fan_show_output:
        match = re.match(r"^FAN(\d+)(?:/\d+)?$", key)
        if match:
            drawers.setdefault(int(match.group(1)), []).append(key)
    return drawers


@retry(Exception, tries=5, delay=2)
def validate_docker_is_up(engine, docker):
    """Validate that the given Docker container is running."""
    assert docker in engine.run_cmd("docker ps")


def epoch_ns_as_utc(epoch_ns: int) -> datetime:
    """Convert nanoseconds since epoch to UTC datetime."""
    return datetime.fromtimestamp(epoch_ns / NS_PER_SEC, tz=timezone.utc)
