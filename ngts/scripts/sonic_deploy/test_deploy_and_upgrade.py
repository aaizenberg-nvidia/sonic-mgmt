import logging
import sys

import allure
import pytest

from ngts.scripts.sonic_deploy.deploy_helper_methods import (
    DeploymentContext, DeployOrchestrator, DeployConnectionHelper
)

logger = logging.getLogger()

pytestmark = [
    pytest.mark.dependency(depends=["test_deploy_and_upgrade"])
]


@pytest.mark.dependency()
@pytest.mark.disable_loganalyzer
@allure.title('Deploy and upgrade image')
def test_deploy_and_upgrade(topology_obj, is_simx, is_performance, base_version, base_version_dpu, target_version,
                            serve_files, sonic_topo, neighbor_type, deploy_only_target, port_number, setup_name,
                            platform_params, deploy_dpu, deploy_type, apply_base_config, reboot_after_install,
                            is_shutdown_bgp, fw_pkg_path, recover_by_reboot, reboot, additional_apps, workspace_path,
                            wjh_deb_url, verify_secure_boot, chip_type, destination_hwsku, show_setup_versions,
                            serial_log_analyzers, fanout_target_version, request, is_air,
                            deploy_testbed_in_parallel, deploy_image_only, deploy_chipless,
                            deploy_sequential, base_version_bmc, neighbor_mode,
                            sdk_source):
    """
        Deploy SONiC/NVOS testing topology and upgrade switch

        Flow:
            1. Get relevant setup info from topology object
            2. Prepare an image to be installed and get base version url
            3. Pre-installation steps
                If it's SONIC Community setup
                3.1. Get ptf docker tag
                3.2. Recover topology
            4. Deploy sonic/nvos image on the dut
            5. Post-installation steps
                For SONIC NOS only:
                5.1. Community only steps - Deploy fanout
                5.2. Post install check
                5.3. Upgrade switch to the target version
                5.4. Reboot validation
                5.5. Install WJH is requested
                5.6. Install supported app extension
                5.7. Port status validation

        :param topology_obj: topology object fixture.
        :param is_simx: is_simx fixture, True in case when setup is SIMX
        :param is_performance: is_performance fixture, True in case when setup is performance
        :param base_version: base_version fixture
        :param target_version: target_version fixture
        :param serve_files: serve_files fixture
        :param sonic_topo: sonic_topo fixture
        :param neighbor_type: neighbor_type fixture
        :param deploy_only_target: deploy_only_target fixture (True/False)
        :param port_number: port_number fixture
        :param setup_name: setup_name fixture
        :param platform_params: platform_params fixture
        :param deploy_type: deploy_type fixture
        :param apply_base_config: apply_base_config fixture
        :param reboot_after_install: reboot_after_install fixture
        :param is_shutdown_bgp: is_shutdown_bgp fixture
        :param fw_pkg_path: fw_pkg_path fixture
        :param recover_by_reboot: recover_by_reboot fixture
        :param reboot: reboot fixture
        :param additional_apps: additional_apps fixture
        :param workspace_path: workspace_path fixture
        :param wjh_deb_url: WJH deb URL
        :param verify_secure_boot: verify_secure_boot
        :param chip_type: chip_type fixture
        :param deploy_testbed_in_parallel: deploy_testbed_in_parallel fixture
        :param deploy_image_only: deploy_image_only fixture
        :param deploy_sequential: deploy_sequential fixture
        :param neighbor_mode: neighbor_mode fixture
        :param sdk_source: where to take the traffic generator SDK from
            (``--sdk_source``): ``latest`` (newest published SDK for the DUT branch),
            ``image`` (SDK from the installed DUT/image),
            or None (platform default: SONiC=latest, Cumulus=image)
        :raise AssertionError: in case of script failure.
    """
    try:
        # Create deployment context with all parameters and derived values
        context = DeploymentContext.from_function_params(
            topology_obj=topology_obj, is_simx=is_simx, is_performance=is_performance,
            base_version=base_version, base_version_dpu=base_version_dpu, target_version=target_version,
            serve_files=serve_files, sonic_topo=sonic_topo, neighbor_type=neighbor_type,
            deploy_only_target=deploy_only_target, port_number=port_number, setup_name=setup_name,
            platform_params=platform_params, deploy_dpu=deploy_dpu, deploy_type=deploy_type,
            apply_base_config=apply_base_config, reboot_after_install=reboot_after_install,
            is_shutdown_bgp=is_shutdown_bgp, fw_pkg_path=fw_pkg_path, recover_by_reboot=recover_by_reboot,
            reboot=reboot, additional_apps=additional_apps, workspace_path=workspace_path,
            wjh_deb_url=wjh_deb_url, verify_secure_boot=verify_secure_boot, chip_type=chip_type,
            destination_hwsku=destination_hwsku, show_setup_versions=show_setup_versions,
            serial_log_analyzers=serial_log_analyzers, fanout_target_version=fanout_target_version,
            request=request, is_air=is_air, deploy_testbed_in_parallel=deploy_testbed_in_parallel,
            deploy_image_only=deploy_image_only, deploy_chipless=deploy_chipless,
            deploy_sequential=deploy_sequential, base_version_bmc=base_version_bmc,
            neighbor_mode=neighbor_mode,
            sdk_source=sdk_source
        )

        # Execute deployment using orchestrator
        orchestrator = DeployOrchestrator(context)
        orchestrator.execute_full_deployment()

    except Exception as err:
        raise AssertionError(err)

    finally:
        DeployConnectionHelper.handle_serial_log_analyzers(context.serial_log_analyzers)


if 'base-version=/auto/sw_system_release/sonic' in ' '.join(sys.argv) and 'target_cli_type' not in ' '.join(sys.argv):
    from ngts.tests.nightly.sanity_checker.test_sanity_checker import (
        platform_json_data, is_in_deploy_image_flow, skip_for_liquid_cooling_platform,
        clear_file_inlcude_failed_sanity_check_case, test_device_asic_check,
        test_cable_connection_for_canonical_check, test_more_then_2_fan_status_wrong_check,
        test_psu_status_check, test_fan_status_check, test_cpld_version_check,
        test_core_dump_file_in_var_core_check, test_component_version_check
    )
