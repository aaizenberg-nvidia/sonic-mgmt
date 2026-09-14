"""OTEL telemetry — NVUE CLI / ``nv show`` tests (ported from Cumulus SSIM ``otel_show``).

These validate applied NVUE state after ``nv set`` (config → ``--applied`` show parity).
They do **not** collect OTLP artifacts or assert exported metric values. Functional
OTLP tests live in ``test_otel_mgmt_vrf_*.py`` and ``test_otel_metric_coverage.py``.

Add new CLI/show coverage here rather than the functional OTEL test files.
"""

import pytest

from ngts.nvos_constants.constants_nvos import ApiType
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.tests_nvos.system.telemetry.otel.cumulus.cli_unset_coverage import (
    require_cumulus_at_least,
    require_cumulus_release_family,
)
from ngts.tests_nvos.system.telemetry.otel.dual_platform import (
    run_adaptive_routing_stats_show_validation,
    run_lldp_show_validation,
    run_software_stats_validation,
    run_transceiver_info_validation,
)

pytestmark = [
    pytest.mark.system,
    pytest.mark.otel,
]


@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test_nv_show_system_telemetry_platform_stats_class_transceiver_info_validation(
    engines, devices, otel_suite_mgmt, test_api,
):
    """Validate the transceiver-info platform-stats class (SSIM otel_show).

    Checks root telemetry and ``stats-group sg_01``: ``state`` and ``sample-interval``.
    """
    TestToolkit.tested_api = test_api
    run_transceiver_info_validation(engines, devices, otel_suite_mgmt)


@pytest.mark.parametrize("test_api", [ApiType.NVUE])
# NVOS has no software-stats/systemd telemetry subtree.
@pytest.mark.cumulus_only
def test_nv_show_system_telemetry_software_stats_validation(
    engines, devices, otel_suite_mgmt, test_api,
):
    """Validate the software-stats systemd show subtree (SSIM software_stats).

    Applies the ``custom`` systemd unit profile at the telemetry root and under
    ``stats-group sg_01``, then checks ``nv show system telemetry software-stats``
    (and its ``systemd`` / ``export`` / ``unit-profile`` subtrees) with ``--applied``.
    """
    TestToolkit.tested_api = test_api
    require_cumulus_at_least(engines.dut, "5.13.0")
    run_software_stats_validation(engines, devices, otel_suite_mgmt)


@pytest.mark.parametrize("test_api", [ApiType.NVUE])
# NVOS has no adaptive-routing-stats telemetry subtree.
@pytest.mark.cumulus_only
def test_nv_show_system_telemetry_adaptive_routing_stats_validation(
    engines, devices, otel_suite_mgmt, test_api,
):
    """Validate the adaptive-routing-stats show subtree (SSIM test16a).

    Checks ``nv show system telemetry adaptive-routing-stats`` (and its ``export``
    leaf) at the telemetry root and under ``stats-group sg_01`` with ``--applied``.
    Cumulus-only: adaptive-routing-stats telemetry has no NVOS equivalent.
    """
    TestToolkit.tested_api = test_api
    require_cumulus_release_family(engines.dut, "5.13")
    run_adaptive_routing_stats_show_validation(engines, devices, otel_suite_mgmt)


@pytest.mark.parametrize("test_api", [ApiType.NVUE])
# NVOS has no LLDP telemetry subtree.
@pytest.mark.cumulus_only
def test_nv_show_system_telemetry_lldp_validation(
    engines, devices, otel_suite_mgmt, test_api,
):
    """Validate the lldp show subtree (SSIM test19a).

    Checks ``nv show system telemetry lldp`` (and its ``export`` leaf) at the telemetry
    root and under ``stats-group sg_01`` with ``--applied``.
    """
    TestToolkit.tested_api = test_api
    require_cumulus_release_family(engines.dut, "5.13")
    run_lldp_show_validation(engines, devices, otel_suite_mgmt)
