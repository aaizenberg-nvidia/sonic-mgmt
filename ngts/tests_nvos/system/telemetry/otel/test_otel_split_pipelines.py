"""OTEL split-pipeline stats-group tests (SSIM ``test_telemetry_split_pipelines``).

Ported from Cumulus and adjusted to also run on NVOS. The split-pipeline intent
(a ``test_01`` stats-group bound to the primary collector overriding root
sample-intervals) is preserved on both platforms. NVOS only exports the families
it supports (interface-stats + platform-stats classes) — histogram / buffer-stats /
control-plane-stats / lldp / software-stats subtrees are Cumulus-only, so the NVOS
branch stages a narrower stats-group and validates a trimmed set of expected gaps.
"""

import pytest

import ngts.tools.test_utils.allure_utils as allure
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_constants.constants_nvos import ApiType
from ngts.tests_nvos.system.telemetry.otel.cumulus.constants import CumulusOtelConst
from ngts.tests_nvos.system.telemetry.otel.cumulus.split_pipeline import (
    apply_split_pipeline_test01_stats_group,
    apply_split_pipeline_test01_stats_group_nvos,
    prepare_split_pipeline_insecure_pre_run,
    prepare_split_pipeline_secured_pre_run,
    run_split_pipeline_test01_verification,
    validate_split_pipeline_stats_group_field_unsets,
)
from ngts.tests_nvos.system.telemetry.otel.cumulus.telemetry_health import (
    assert_otlp_session_established,
)

pytestmark = [
    pytest.mark.cumulus,
    pytest.mark.otel,
    pytest.mark.split_pipelines,
]


@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test01_otel_telemetry_split_mgmt_vrf_TLS(
    engines, otel_suite_mgmt_secured, test_api, tmp_path,
):
    TestToolkit.tested_api = test_api
    suite = otel_suite_mgmt_secured
    vrf = suite.export_vrf
    cur_dir = str(tmp_path)
    collector = suite.primary

    if suite.is_nvos:
        with allure.step("Apply split-pipeline test_01 stats-group (NVOS, TLS)"):
            apply_split_pipeline_test01_stats_group_nvos(
                engines.dut, suite.primary_ip, export_vrf=vrf,
            )
        run_split_pipeline_test01_verification(
            engines.dut,
            collector,
            cur_dir,
            vrf=vrf,
            expected_gaps=CumulusOtelConst.NVOS_SPLIT_EXPECTED_GAPS,
            prepare_session=False,
            telemetry_unit=suite.telemetry_unit,
            family_map=list(CumulusOtelConst.NVOS_SPLIT_FAMILY_MAP),
        )
        return

    prepare_split_pipeline_secured_pre_run(engines.dut, collector, vrf=vrf)
    with allure.step("Apply split-pipeline test_01 stats-group (mgmt VRF TLS)"):
        apply_split_pipeline_test01_stats_group(
            engines.dut,
            suite.primary_ip,
            include_router_lldp=False,
        )

    run_split_pipeline_test01_verification(
        engines.dut,
        collector,
        cur_dir,
        vrf=vrf,
        prepare_session=False,
    )


@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test01_otel_telemetry_split_mgmt_vrf_No_TLS(
    engines, otel_suite_mgmt, test_api, tmp_path,
):
    TestToolkit.tested_api = test_api
    suite = otel_suite_mgmt
    vrf = suite.export_vrf
    cur_dir = str(tmp_path)
    collector = suite.primary

    if suite.is_nvos:
        with allure.step("Verify OTLP session before split-pipeline overlay"):
            assert_otlp_session_established(collector)
        with allure.step("Apply split-pipeline test_01 stats-group (NVOS, insecure)"):
            apply_split_pipeline_test01_stats_group_nvos(
                engines.dut, suite.primary_ip, export_vrf=vrf,
            )
        run_split_pipeline_test01_verification(
            engines.dut,
            collector,
            cur_dir,
            vrf=vrf,
            expected_gaps=CumulusOtelConst.NVOS_SPLIT_EXPECTED_GAPS,
            prepare_session=False,
            telemetry_unit=suite.telemetry_unit,
            family_map=list(CumulusOtelConst.NVOS_SPLIT_FAMILY_MAP),
        )
        return

    prepare_split_pipeline_insecure_pre_run(engines.dut, vrf=vrf)
    with allure.step("Verify OTLP session before split-pipeline overlay"):
        assert_otlp_session_established(collector)

    with allure.step("Apply split-pipeline test_01 stats-group (mgmt VRF insecure)"):
        apply_split_pipeline_test01_stats_group(
            engines.dut,
            suite.primary_ip,
            include_router_lldp=True,
        )

    run_split_pipeline_test01_verification(
        engines.dut,
        collector,
        cur_dir,
        vrf=vrf,
        prepare_session=True,
    )


@pytest.mark.cumulus_only
@pytest.mark.parametrize("test_api", [ApiType.NVUE])
def test02_unset_stats_group_validations(
    engines, otel_suite_mgmt, test_api,
):
    """SSIM ``Test_Otel_Split_Mgmt_Vrf_insecured::test02_unset_stats_group_validations``.

    Note: SSIM ``test02_otel_telemetry_split_unset_cli`` belongs to
    ``Test_Otel_Split_Default_Vrf_secured`` (whole stats-group delete + global metric gaps),
    not the mgmt-insecure class. This test covers per-field unset/show on ``test_01``.
    """
    TestToolkit.tested_api = test_api
    suite = otel_suite_mgmt
    prepare_split_pipeline_insecure_pre_run(engines.dut, vrf=suite.export_vrf)
    with allure.step("Create test_01 stats-group baseline for unset coverage"):
        apply_split_pipeline_test01_stats_group(
            engines.dut,
            suite.primary_ip,
            include_router_lldp=True,
        )
    try:
        with allure.step("Validate test_01 per-field unset/show pairs"):
            validate_split_pipeline_stats_group_field_unsets(engines.dut)
    finally:
        with allure.step("Restore test_01 stats-group after field unsets"):
            apply_split_pipeline_test01_stats_group(
                engines.dut,
                suite.primary_ip,
                include_router_lldp=True,
            )
