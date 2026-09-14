"""Pytest plumbing for the OTLP alarm validation in `system_alarms_otel_helpers`.

Registered as a plugin from `ngts/tests_nvos/conftest.py`, so any test under
`ngts/tests_nvos` - a platform fault test as much as a system one - can validate
the alarms it provokes over OTLP without its own suite bring-up.
"""

import logging

import pytest

from ngts.nvos_constants.constants_nvos import ApiType, TelemetryConsts
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.tests_nvos.helpers.alarms.system_alarms_otel_helpers import (enable_otel_alarms_export,
                                                                       unset_otel_alarms_export)
from ngts.tests_nvos.system.telemetry.otel.helpers import (is_nvos_dut, setup_otel_suite,
                                                           teardown_otel_suite)

logger = logging.getLogger(__name__)


@pytest.fixture(scope="function")
def otel_alarms_collector(engines, devices):
    """A collector receiving the DUT's OTLP export, with the alarms export on.

    Function-scoped: bringing the collector up and configuring the DUT's OTLP
    destination is performed for every test that requests this fixture, so each
    test gets a fresh collector environment.

    The alarms export is a knob of its own on top of the suite's destination, so
    it is enabled here rather than in `setup_otel_suite`, and unset again before
    the suite is torn down.
    """
    with TestToolkit.force_api(ApiType.NVUE):
        suite = setup_otel_suite(engines, is_nvos=is_nvos_dut(devices), use_secondary=False, fast_startup=True)
    try:
        with TestToolkit.force_api(ApiType.NVUE):
            suite.primary.truncate_artifact()
            enable_otel_alarms_export(engines.dut)
        yield suite.primary
    finally:
        with TestToolkit.force_api(ApiType.NVUE):
            try:
                unset_otel_alarms_export(engines.dut)
            except Exception as e:
                logger.error(f"Error while unsetting OTLP alarms export: {e}")
        teardown_otel_suite(engines, suite)
