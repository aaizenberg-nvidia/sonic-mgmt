"""OTEL telemetry — insecure mgmt-VRF tests (ported from Cumulus; run on NVOS too).

Test bodies are identical across platforms; all NVOS/Cumulus divergence lives in
:mod:`ngts.tests_nvos.system.telemetry.otel.dual_platform`. Cumulus exports over
the ``mgmt`` VRF; NVOS falls back to the ``default`` VRF (no ``mgmt`` VRF) — see the
``otel_suite_mgmt`` fixture in ``conftest.py``.
"""

import pytest

from ngts.nvos_constants.constants_nvos import ApiType
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.tests_nvos.system.telemetry.otel.dual_platform import (
    run_control_plane_stats_data_validation,
    run_insecure_collection_and_validate,
    run_platform_stats_validation,
)

pytestmark = [
    pytest.mark.system,
    pytest.mark.otel,
]


@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test01_otel_metrics_collection_mgmt_vrf_insecure(
    engines, devices, otel_suite_mgmt, otel_telemetry_cache, is_ib_router, test_api, tmp_path
):
    """OTLP metric collection on the mgmt/default VRF without TLS (SSIM otlp test01)."""
    TestToolkit.tested_api = test_api
    run_insecure_collection_and_validate(
        engines, devices, otel_suite_mgmt, is_ib_router, str(tmp_path)
    )


@pytest.mark.cumulus_only
@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test04_otel_control_plane_stats_validation(
    engines, devices, otel_suite_mgmt, otel_telemetry_cache, test_api, tmp_path
):
    """Validate OTLP control-plane counters vs ``sx_api`` CLI (SSIM test04).

    Cumulus-only: the reference CLI (``sx_api_host_ifc_counters_get.py``) is an
    sx_sdk tool with no NVOS equivalent.
    """
    TestToolkit.tested_api = test_api
    run_control_plane_stats_data_validation(engines, devices, otel_suite_mgmt, str(tmp_path))


@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test05_otel_platform_stats_validation(
    engines, devices, otel_suite_mgmt, otel_telemetry_cache, test_api, tmp_path
):
    """Validate OTLP platform environment metrics vs ``nv show`` CLI (SSIM test05)."""
    TestToolkit.tested_api = test_api
    run_platform_stats_validation(engines, devices, otel_suite_mgmt, str(tmp_path))
