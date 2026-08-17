from __future__ import annotations

import concurrent.futures
from devts.infra.tools.connection_tools.linux_ssh_engine import LinuxSshEngine
from collections import defaultdict
import functools
import inspect
import logging
import random
import retry
import time
import re

from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import IbInterfaceConsts, NvosConsts
from ngts.nvos_constants.constants_nvos import ChassisLocationConsts, IbConsts, OutputFormat, SystemConsts
from ngts.nvos_tools.infra.RegressionConfigurations import Configurations
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.tests_nvos.cluster.cluster_consts import ClusterConsts
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.Devices.IbDevice import JulietSwitch
from ngts.tools.test_utils import allure_utils as allure
from ngts.nvos_tools.platform.Platform import Platform
from ngts.nvos_tools.infra.ResultObj import ResultObj
from ngts.tests_nvos.helpers import redmine_helpers
from ngts.nvos_tools.nmx.Cluster import Cluster
from ngts.nvos_tools.infra.Tools import Tools
from ngts.nvos_tools.nmx.Sdn import Sdn
from ngts.nvos_tools.ib.Ib import Ib

logger = logging.getLogger(__name__)


class ClusterTools:

    # Well-formed 13-char serial for standalone trays whose EEPROM reports chassis-sn 'N/A'.
    # On such 1-rack systems chassis_mapping is not consumed and GFM is non-operational anyway
    # (bug 4207869), so a synthetic serial only needs to satisfy the file-format validation.
    # TODO: confirm with the nmx-c team that this is acceptable on N/A-serial standalone trays.
    STANDALONE_FALLBACK_CHASSIS_SN = '1234567890123'

    @staticmethod
    def get_chassis_sn():
        """
        Real chassis S/N from 'nv show platform chassis-location'. Used to build a valid
        chassis_mapping line ('chassisId1 <sn>') - a fake S/N keeps GFM from going operational.
        Falls back to a well-formed synthetic serial when the tray reports 'N/A' (plain standalone).
        """
        output = OutputParsingTool.parse_json_str_to_dictionary(
            Platform().chassis_location.show()).get_returned_value()
        chassis_sn = output.get(ChassisLocationConsts.CHAS_SN)
        if not chassis_sn or chassis_sn == ChassisLocationConsts.NA:
            return ClusterTools.STANDALONE_FALLBACK_CHASSIS_SN
        return chassis_sn

    @staticmethod
    def config_edit_expected_to_change(file_type, initial_content):
        """
        Whether CONFIG_FILES_CHANGE[file_type] is expected to actually modify the file.
        The rdm/chassis edits are guarded appends that intentionally no-op when the file already
        carries the section/entries (real clusters generate them populated), so the
        'content changed vs initial' assertions must not run for those cases.
        """
        if ClusterConsts.CONFIG_FILES_CHANGE[file_type] == 'true':
            return False
        initial = initial_content or ''
        if file_type == 'chassis_mapping':
            return 'chassisId' not in initial
        if file_type == 'rdm_config':
            return '[rdm]' not in initial
        return True

    @staticmethod
    def stop_start_app(cluster, engine, devices, has_loopbox, setup_name, standalone_system):
        with allure.step("Stop/Start apps"):
            for app in devices.dut.expected_cluster_apps:
                with allure.step(f"Validate app {app} is up"):
                    ClusterTools.verify_app_is_up(engine, app)
                    if app == ClusterConsts.NMX_CONTROLLER and (has_loopbox or not standalone_system):
                        ClusterTools.verify_lid_value(devices, engine=engine)
                        ClusterTools.verify_interface_up(devices, has_loopbox, setup_name, engine=engine)
                with allure.step("Running 'nv show cluster apps running' command and verifying output"):
                    if app == ClusterConsts.NMX_CONTROLLER and redmine_helpers.is_bug_active(4207869) and standalone_system:
                        pass
                    else:
                        ClusterTools.wait_for_app_healthy(cluster, app, engine=engine)
                with allure.step(f"Stop app {app} and validate its down"):
                    cluster.apps.app_name[app].action_stop_cluster_app(engine=engine)
                    nmx_c_expected_state = 'down' if app == ClusterConsts.NMX_CONTROLLER else ''
                    ClusterTools.wait_for_apps_to_be_in_wanted_state(cluster, cluster_expected_state='enabled', nmx_c_expected_state=nmx_c_expected_state, engine=engine)
                    # TBD -- once "running" is working, use it to verify app is not running
                    ClusterTools.verify_app_is_down(engine, app, devices)

                with allure.step(f"Start app again {app} and validate its up"):
                    _ = cluster.apps.app_name[app].action_start_cluster_app(engine=engine)
                    nmx_c_expected_state = 'up' if app == ClusterConsts.NMX_CONTROLLER else ''
                    app_state = '' if app == ClusterConsts.NMX_CONTROLLER else app
                    ClusterTools.wait_for_apps_to_be_in_wanted_state(cluster, cluster_expected_state='enabled', nmx_c_expected_state=nmx_c_expected_state, app=app_state, engine=engine)
                ClusterTools.verify_app_is_up(engine, app)
                if app == ClusterConsts.NMX_CONTROLLER and (has_loopbox or not standalone_system):
                    ClusterTools.verify_lid_value(devices, engine=engine)
                    ClusterTools.verify_interface_up(devices, has_loopbox, setup_name, engine=engine)
                with allure.step("Running 'nv show cluster apps running' command and verifying output"):
                    if app == ClusterConsts.NMX_CONTROLLER and redmine_helpers.is_bug_active(4207869) and standalone_system:
                        pass
                    else:
                        ClusterTools.wait_for_app_healthy(cluster, app, engine=engine)

            return ResultObj(result=True)

    @staticmethod
    def get_filtered_state_files(devices, standalone_system=False):
        """
        Get state files filtered based on device's actual apps.
        In standalone systems, topology files are excluded.

        :param devices: devices fixture containing dut with cluster apps info
        :param standalone_system: whether this is a standalone system
        :return: list of state file types for this device
        """
        state_file_types = []
        for app in devices.dut.expected_cluster_apps:
            app_state_files = devices.dut.cluster_state_files_by_app.get(app, [])
            state_file_types.extend(app_state_files)

        # Filter out topology for standalone systems
        return [f for f in state_file_types if not (standalone_system and f == 'topology')]

    @staticmethod
    def start_cluster(cluster, setup_name, output_format=OutputFormat.json, verify_nmx_c=True, engine=None, devices=None):
        with allure.step("Start cluster"):
            output = OutputParsingTool.parse_show_output_to_dict(
                cluster.show(output_format=output_format, dut_engine=engine),
                output_format=output_format).get_returned_value()

            if output[SystemConsts.STATE] == 'disabled':
                cluster.set(op_param_name="state", op_param_value='enabled', apply=True, dut_engine=engine)

            ClusterTools.wait_for_apps_to_be_in_wanted_state(cluster, cluster_expected_state='enabled', nmx_c_expected_state='up',
                                                             engine=engine)

            # Apply nv-bridge config for devices that need it (e.g., Rosalind)
            # This must happen AFTER cluster is enabled, then restart nmx-controller
            # This is required because cluster.unset() clears the node primary server config
            ClusterTools._apply_nv_bridge_config_if_needed(cluster, devices, engine=engine)

            output = OutputParsingTool.parse_show_output_to_dict(
                cluster.show(output_format=output_format, dut_engine=engine),
                output_format=output_format).get_returned_value()

            with allure.step("Validate state is enabled"):
                assert output[SystemConsts.STATE] == 'enabled', f"Cluster state is , " \
                    f"{output[SystemConsts.STATE]}, Expected to be: " \
                    f"enabled"
                assert ClusterConsts.NMXC_CONN in output, f"{ClusterConsts.NMXC_CONN} was not found in {output}"
                if verify_nmx_c:
                    expected_nmxc_state = ClusterConsts.NMXC_CONN_STATE_PER_CLUSTER_STATE[output[SystemConsts.STATE]]
                    Tools.ValidationTool.verify_field_value_in_output(output_dictionary=output,
                                                                      field_name=ClusterConsts.NMXC_CONN,
                                                                      expected_value=expected_nmxc_state).verify_result()

    @staticmethod
    def _is_nv_bridge_configured(cluster, engine=None):
        """
        Check if nv-bridge node primary server is already configured.
        Returns True if configured, False otherwise.
        """
        try:
            output = cluster.show(op_param='node', output_format=OutputFormat.json, dut_engine=engine)
            parsed = OutputParsingTool.parse_json_str_to_dictionary(output).get_returned_value()
            # Check if primary/server exists in the config
            return bool(parsed.get('primary', {}).get('server', {}))
        except Exception:
            return False

    @staticmethod
    def _apply_nv_bridge_config_if_needed(cluster, devices=None, engine=None):
        """
        Apply nv-bridge configuration for devices that require it.
        This sets the cluster node primary server which is required for nv-bridge to function.

        Note: Uses device.requires_nv_bridge_config flag (set to True in RosalindSurrogateSwitch).

        Full sequence:
        1. Check if already configured (skip if yes)
        2. Set cluster node primary server 127.0.0.1
        3. Stop nmx-controller
        4. Start nmx-controller
        5. Wait for nmx-conn to be up

        This is called automatically by start_cluster() because cluster.unset() clears
        the node primary server config, and tests need it to be re-applied.

        :param cluster: Cluster object
        :param devices: devices fixture (optional, will try to get from TestToolkit if not provided)
        """
        # Try to get devices from TestToolkit if not provided
        if devices is None:
            if hasattr(TestToolkit, 'devices') and TestToolkit.devices:
                devices = TestToolkit.devices
            else:
                logger.debug("Could not get devices from TestToolkit, skipping nv-bridge config")
                return

        # Check if device requires nv-bridge config (set in device class, e.g., RosalindSurrogateSwitch)
        if getattr(devices.dut, 'requires_nv_bridge_config', False):
            # Check if nv-bridge is already configured - skip if it is
            if ClusterTools._is_nv_bridge_configured(cluster, engine=engine):
                logger.info("nv-bridge node primary server already configured, skipping")
                return

            with allure.step(f"Apply nv-bridge config for {devices.dut.__class__.__name__}"):
                logger.info(f"Device {devices.dut.__class__.__name__} requires nv-bridge config - setting node primary server")

                # Step 1: Set node primary server
                cluster.node.primary.set_cluster_node(op_param_name=SystemConsts.NV_BRIDGE_NODE_SERVER,
                                                      op_param_value=devices.dut.cur_mgmt_port_ip, apply=True, dut_engine=engine)

                # Step 2: Stop nmx-controller
                logger.info("Stopping nmx-controller to apply nv-bridge config")
                cluster.apps.app_name[ClusterConsts.NMX_CONTROLLER].action_stop_cluster_app(engine=engine)
                ClusterTools.wait_for_apps_to_be_in_wanted_state(cluster, cluster_expected_state='', nmx_c_expected_state='down', engine=engine)

                # Step 3: Start nmx-controller
                logger.info("Starting nmx-controller after nv-bridge config")
                cluster.apps.app_name[ClusterConsts.NMX_CONTROLLER].action_start_cluster_app(engine=engine)

                # Step 4: Wait for nmx-conn to be up
                ClusterTools.wait_for_apps_to_be_in_wanted_state(cluster, cluster_expected_state='enabled', nmx_c_expected_state='up', engine=engine)

    @staticmethod
    @retry.retry(AssertionError, tries=6, delay=15, logger=None)
    def validate_cluster_enabled(cluster, output_format=OutputFormat.json, engine=None):
        output = OutputParsingTool.parse_show_output_to_dict(
            cluster.show(output_format=output_format, dut_engine=engine),
            output_format=output_format).get_returned_value()

        with allure.step("Validate state is enabled"):
            assert output[SystemConsts.STATE] == 'enabled', f"Cluster state is , " \
                f"{output[SystemConsts.STATE]}, Expected to be: " \
                f"enabled"
            assert ClusterConsts.NMXC_CONN in output, f"{ClusterConsts.NMXC_CONN} was not found in {output}"
            expected_nmxc_state = ClusterConsts.NMXC_CONN_STATE_PER_CLUSTER_STATE[output[SystemConsts.STATE]]
            Tools.ValidationTool.verify_field_value_in_output(output_dictionary=output,
                                                              field_name=ClusterConsts.NMXC_CONN,
                                                              expected_value=expected_nmxc_state).verify_result()
            assert output[ClusterConsts.NMXC_CONN] == expected_nmxc_state, f"{ClusterConsts.NMXC_CONN} state was expected to be {expected_nmxc_state} but instead it was {output[ClusterConsts.NMXC_CONN]}"

    @staticmethod
    def check_cluster_state(cluster, output_format=OutputFormat.json, engine=None):
        with allure.step("Check cluster state"):
            output = OutputParsingTool.parse_show_output_to_dict(
                cluster.show(output_format=output_format, dut_engine=engine),
                output_format=output_format).get_returned_value()
            return output[SystemConsts.STATE]

    @staticmethod
    def reverse_cluster_state(cluster, setup_name, output_format, engine=None):
        if ClusterTools.check_cluster_state(cluster, output_format, engine=engine) == 'enabled':
            ClusterTools.stop_cluster(cluster, output_format, engine=engine)
        else:
            ClusterTools.start_cluster(cluster, setup_name, output_format=output_format, engine=engine)

    @staticmethod
    def stop_cluster(cluster, output_format=OutputFormat.json, engine=None):
        with allure.step("Stop cluster"):
            output = OutputParsingTool.parse_show_output_to_dict(
                cluster.show(output_format=output_format, dut_engine=engine),
                output_format=output_format).get_returned_value()

            if output[SystemConsts.STATE] == 'enabled':
                # Must unset node config before disabling cluster
                # (product validation: "Cannot have cluster node server configuration when cluster state is not enabled")
                if ClusterTools._is_nv_bridge_configured(cluster):
                    cluster.unset(op_param=SystemConsts.NODE, apply=False, dut_engine=engine)
                cluster.set(op_param_name="state", op_param_value='disabled', apply=True, dut_engine=engine)

            ClusterTools.wait_for_apps_to_be_in_wanted_state(cluster, cluster_expected_state='disabled',
                                                             nmx_c_expected_state='down', engine=engine)
            output = OutputParsingTool.parse_show_output_to_dict(
                cluster.show(output_format=output_format, dut_engine=engine),
                output_format=output_format).get_returned_value()

            with allure.step("Validate state is disabled"):
                output = OutputParsingTool.parse_show_output_to_dict(
                    cluster.show(output_format=output_format, dut_engine=engine),
                    output_format=output_format).get_returned_value()
                assert output[SystemConsts.STATE] == 'disabled', f"State is: " \
                    f"{output[SystemConsts.STATE]}, Expected to be: " \
                    f"disabled"
                assert ClusterConsts.NMXC_CONN in output, f"{ClusterConsts.NMXC_CONN} was not found in {output}"
                expected_nmxc_state = ClusterConsts.NMXC_CONN_STATE_PER_CLUSTER_STATE[output[SystemConsts.STATE]]
                assert output[ClusterConsts.NMXC_CONN] == expected_nmxc_state, f"{ClusterConsts.NMXC_CONN} state was expected to be {expected_nmxc_state} but instead it was {output[ClusterConsts.NMXC_CONN]}"

    @staticmethod
    def verify_app_is_up(engine, app):
        with allure.step("Checking if service is up using docker ps | grep -i nmx"):
            output = engine.run_cmd('docker ps | grep -i nmx')
            assert output != '', f"nmx docker is still down, {output}"
            output = output.split('\n')
            expected_services = ClusterConsts.CONTROLLER_SERVICES if app == ClusterConsts.NMX_CONTROLLER else ClusterConsts.TELEMETRY_SERVICES
            all_services_present = all(any(service in line for line in output) for service in expected_services)
            assert all_services_present, f"Missing services - expected services {expected_services}, actual: {output}"

    @staticmethod
    def verify_app_is_down(engine, app, devices=None):
        # Skip nmx-t docker check since the agent keeps it running even when the app is stopped
        if app == ClusterConsts.NMX_TELEMETRY and devices and getattr(devices.dut, 'has_nmx_telemetry_agent', False):
            logger.info("Skipping nmx-t docker check - system has nmx-telemetry-agent")
            return
        with allure.step("Checking if service is down using docker ps | grep -i nmx"):
            output = engine.run_cmd('docker ps | grep -i nmx')
            output = output.split('\n')
            expected_services = ClusterConsts.CONTROLLER_SERVICES if app == ClusterConsts.NMX_CONTROLLER else ClusterConsts.TELEMETRY_SERVICES
            none_services_present = all(not any(service in line for line in output) for service in expected_services)
            assert none_services_present, f"nmx docker is still up, {output}"

    @staticmethod
    def verify_lid_value(devices, engine=None):
        with allure.step("Create an IB object"):
            ib = Ib(None)

        with allure.step('Run nv show ib device command and verify that each field has a value'):
            output = OutputParsingTool.parse_json_str_to_dictionary(ib.device.show(dut_engine=engine)).get_returned_value()

            ValidationTool.verify_all_fields_value_exist_in_output_dictionary(
                output, devices.dut.device_list).verify_result()
            assert len(devices.dut.device_list) == len(output), "Unexpected amount of ib devices.\n" \
                                                                "Expect {} devices:{} \n" \
                                                                "but got {} devices: {}".format(
                len(devices.dut.device_list),
                devices.dut.device_list,
                len(output), output.keys())

            for device in output:
                with allure.step('Run nv show ib device <device-id> command and verify that each field has a value'):
                    dev_output = OutputParsingTool.parse_json_str_to_dictionary(
                        ib.device.show(op_param=device, dut_engine=engine)).get_returned_value()

                if IbConsts.DEVICE_ASIC_PREFIX in device:
                    lid = dev_output.get('lid')
                    if lid is None:
                        # New schema: guid/lid nested under ib-subnet.<subnet-name>
                        subnets = dev_output.get(IbConsts.DEVICE_ASIC_SUBNET_KEY, {})
                        subnet_entry = subnets.get(IbConsts.DEVICE_ASIC_DEFAULT_SUBNET, {})
                        lid = subnet_entry.get('lid')
                    assert lid is not None and lid > 0, "Invalid number of lid"

    @staticmethod
    def verify_interface_up(devices, has_loopbox, setup_name, engine=None):
        # Build list of interface types to check based on what actually exists
        interface_types = []

        if setup_name in Configurations.non_standalone_systems:
            # Non-standalone systems: only check access ports
            interface_types = ['acp']
        elif has_loopbox:
            # Loopbox systems: check FNM and access ports (if they exist)
            # Check if device actually has regular FNM ports (not just internal FNM)
            if hasattr(devices.dut, 'nvl_fnm_ports') and devices.dut.nvl_fnm_ports:
                interface_types.append('fnm')
            interface_types.append('acp')

        for interface_type in interface_types:
            port_type = 'fnm' if interface_type == 'fnm' else ''
            selected_port = Tools.RandomizationTool.select_random_port(
                dut_engine=engine,
                requested_ports_logical_state=NvosConsts.LINK_LOG_STATE_ACTIVE,
                requested_ports_type=port_type,
                interface_type=interface_type).get_returned_value()
            output_dictionary = Tools.OutputParsingTool.parse_show_interface_link_output_to_dictionary(
                selected_port.interface.link.show(dut_engine=engine)).get_returned_value()
            Tools.ValidationTool.verify_field_value_in_output(output_dictionary=output_dictionary,
                                                              field_name=IbInterfaceConsts.LINK_STATE,
                                                              expected_value=NvosConsts.LINK_STATE_UP).verify_result()
            Tools.ValidationTool.verify_field_value_in_output(output_dictionary=output_dictionary,
                                                              field_name=IbInterfaceConsts.LINK_LOGICAL_PORT_STATE,
                                                              expected_value=IbInterfaceConsts.LINK_LOGICAL_PORT_STATE_ACTIVE).verify_result()
            Tools.ValidationTool.verify_field_value_in_output(output_dictionary=output_dictionary,
                                                              field_name=IbInterfaceConsts.LINK_PHYSICAL_PORT_STATE,
                                                              expected_value=IbInterfaceConsts.LINK_PHYSICAL_PORT_STATE_LINK_UP).verify_result()

        if hasattr(devices.dut, 'nvl_trunk_ports_list') and devices.dut.nvl_trunk_ports_list:
            with allure.step("Verify switch ports state - that are connected to transceivers"):
                ClusterTools().verify_external_interfaces_state_up_and_active(devices)

    @staticmethod
    def get_all_interfaces_with_transceivers(devices, engine=None):
        interfaces = []
        platform = Platform()
        present_transceivers = platform.transceiver.get_list_of_connected_transceivers(dut_engine=engine)

        for transceiver in present_transceivers:
            interfaces.extend([interface for interface in devices.dut.nvl_trunk_ports_list if interface.startswith(f"{transceiver}p")])
            # This way, if sw1 is present and sw10 is not, we will not take sw10. because we force sw1p so sw10p falls.
        return interfaces

    @staticmethod
    def verify_external_interfaces_state_up_and_active(devices, engine=None):
        interfaces = ClusterTools().get_all_interfaces_with_transceivers(devices, engine=engine)
        for interface in interfaces:
            selected_port = Port(interface, "", "")
            # Verify fields.
            output_dictionary = Tools.OutputParsingTool.parse_show_interface_link_output_to_dictionary(
                selected_port.interface.link.show(dut_engine=engine)).get_returned_value()
            Tools.ValidationTool.verify_field_value_in_output(output_dictionary=output_dictionary,
                                                              field_name=IbInterfaceConsts.LINK_STATE,
                                                              expected_value=NvosConsts.LINK_STATE_UP).verify_result()
            Tools.ValidationTool.verify_field_value_in_output(output_dictionary=output_dictionary,
                                                              field_name=IbInterfaceConsts.LINK_LOGICAL_PORT_STATE,
                                                              expected_value=IbInterfaceConsts.LINK_LOGICAL_PORT_STATE_ACTIVE).verify_result()
            Tools.ValidationTool.verify_field_value_in_output(output_dictionary=output_dictionary,
                                                              field_name=IbInterfaceConsts.LINK_PHYSICAL_PORT_STATE,
                                                              expected_value=IbInterfaceConsts.LINK_PHYSICAL_PORT_STATE_LINK_UP).verify_result()

    @staticmethod
    def start_stop_cluster(cluster, setup_name, output_format, engine=None):
        ClusterTools.start_cluster(cluster, setup_name, output_format, engine=engine)
        ClusterTools.stop_cluster(cluster, output_format, engine=engine)
        return ResultObj(result=True)

    @staticmethod
    def verify_apps_running(engine, devices, cluster, expected_state, output_format, standalone_system, has_loopbox,
                            retries=0, retry_interval=15, is_simx=False):
        with allure.step("Running 'nv show cluster apps running' command and verifying output"):
            for app in devices.dut.expected_cluster_apps:
                if app == ClusterConsts.NMX_CONTROLLER and redmine_helpers.is_bug_active(4207869) and standalone_system:
                    continue
                app_status = None
                for attempt in range(retries + 1):
                    output = OutputParsingTool.parse_show_output_to_dict(
                        cluster.apps.running.show(output_format=output_format, dut_engine=engine),
                        output_format=output_format).get_returned_value()
                    app_status = output[app]['status']
                    if app_status == expected_state:
                        break
                    if attempt < retries:
                        logger.info(f"App {app} status is '{app_status}', expected '{expected_state}'. "
                                    f"Retry {attempt + 1}/{retries}, sleeping {retry_interval}s")
                        time.sleep(retry_interval)
                assert app_status == expected_state, \
                    f"App {app} status is {app_status} instead of {expected_state}"
                ClusterTools.verify_app_is_up(engine, app)
            if has_loopbox or is_simx or not standalone_system:
                ClusterTools.verify_lid_value(devices, engine=engine)

    @staticmethod
    def verify_app_version(cluster, app, expected_version, engine=None):
        with allure.step("Running 'nv show cluster apps running' command and verifying output"):
            output = OutputParsingTool.parse_show_output_to_dict(cluster.apps.show(dut_engine=engine)).get_returned_value()
            ValidationTool.verify_field_value_exist_in_output_dict(output, app)
            assert expected_version in output[app][ClusterConsts.APP_VERSION], \
                f"Expected {app} version: {expected_version}. Actual version: {output[app][ClusterConsts.APP_VERSION]}"

    @staticmethod
    def start_app(cluster, app, has_loopbox, standalone_system, engine=None):
        with allure.step(f"Start app {app}"):
            cluster.apps.app_name[app].action_start_cluster_app(engine=engine)
            ClusterTools.wait_for_apps_to_be_in_wanted_state(cluster, cluster_expected_state='enabled', nmx_c_expected_state='up', engine=engine)
            if app == ClusterConsts.NMX_CONTROLLER and redmine_helpers.is_bug_active(4207869) and standalone_system:
                pass
            else:
                with allure.step("Running 'nv show cluster apps running' command and verifying output"):
                    for _ in range(10):
                        output = OutputParsingTool.parse_show_output_to_dict(
                            cluster.apps.running.show(output_format=OutputFormat.json, dut_engine=engine),
                            output_format=OutputFormat.json).get_returned_value()
                        app_status = output[app]['status']
                        if app_status == 'ok':
                            break
                        logger.info("Sleeping for 5 seconds until app state is ok.")
                        time.sleep(5)
                    assert app_status == 'ok', f"App {app} status is {app_status} instead of 'ok"

    @staticmethod
    def stop_app(cluster, app, engine=None):
        with allure.step(f"Stop app {app}"):
            cluster.apps.app_name[app].action_stop_cluster_app(engine=engine)
            if app == ClusterConsts.NMX_CONTROLLER:
                ClusterTools.wait_for_apps_to_be_in_wanted_state(cluster, cluster_expected_state='', nmx_c_expected_state='down', engine=engine)
            else:
                logger.info("Stopping nmx-telemetry app, sleeping for 10 seconds till its status is updated")
                time.sleep(10)

    @staticmethod
    def get_current_config_files_paths(sdn, app, files_types, engine=None):
        files_dict = {}
        with allure.step("Fetch & Generate config files"):
            for file_type in files_types:
                output = sdn.config.apps.app_name[app].type.file_type[file_type].action_generate_sdn(dut_engine=engine)
                installed_file = ClusterTools().get_generated_file_name(output.returned_value, 'config')
                output = OutputParsingTool.parse_show_output_to_dict(sdn.config.apps.app_name[app].type.file_type[file_type].files.show(output_format=OutputFormat.json, dut_engine=engine),
                                                                     output_format=OutputFormat.json).get_returned_value()
                current_installed_config_path = output[installed_file]['path']
                files_dict[file_type] = current_installed_config_path
        return files_dict

    @staticmethod
    def get_all_apps_config_files_paths(sdn, devices, engine=None):
        """
        Get config files paths for all cluster apps on this device type.
        Handles dynamic app lists (Juliet/Surrogate: both apps, Rosalind: controller only)

        :param sdn: Sdn object
        :param devices: devices fixture containing dut with expected_cluster_apps
        :return: dict of {file_type: file_path} for all apps combined
        """
        config_files_paths = {}
        for app in devices.dut.expected_cluster_apps:
            app_config_files = devices.dut.cluster_config_files_by_app.get(app, [])
            app_files_paths = ClusterTools.get_current_config_files_paths(sdn, app, app_config_files, engine=engine)
            config_files_paths.update(app_files_paths)
        return config_files_paths

    @staticmethod
    def get_generated_file_name(output, file_type):
        assert output is not None, f"Generate {file_type} file command returned None output (command may have failed)"
        match = re.search(rf'App {file_type} file (\S+) is successfully generated', output)
        file_name = None
        assert match, f"File was not generated successfully. Output: {output}"
        if match:
            file_name = match.group(1)
            logger.info(f"Extracted file name: {file_name}")
        return file_name

    @staticmethod
    def verify_log_level(log_level, app, output_format, cluster, engine=None):
        with allure.step(f"Verifying log level is updated to {log_level}"):
            output = OutputParsingTool.parse_show_output_to_dict(
                cluster.apps.app_name[app].loglevel.show(output_format=output_format, dut_engine=engine),
                output_format=output_format).get_returned_value()
            # Add assert on log level
            assert output['log-level'] == log_level, f"Expected log level: {log_level}, Actual log-level {output['log-level']}"

    @staticmethod
    def create_empty_partition(sdn, partitions_mapping, output_format=OutputFormat.json, engine=None):
        with allure.step("Create empty partition"):
            resiliency_mode = random.choice(ClusterConsts.RESILIENCY_MODES)
            mcast_limit = random.randint(ClusterConsts.MIN_MCAST, ClusterConsts.MAX_MCAST)
            sdn.partition.partition_id[ClusterConsts.EMPTY_PARTITION_ID].action_create_partition_id(name=ClusterConsts.EMPTY_PARTITION_NAME, resiliency_mode=resiliency_mode, mcast_limit=mcast_limit, engine=engine)

        with allure.step("Checking newly created partition"):
            output = OutputParsingTool.parse_show_output_to_dict(sdn.partition.show(output_format=output_format, dut_engine=engine),
                                                                 output_format=output_format).get_returned_value()
            assert ClusterConsts.EMPTY_PARTITION_ID in list(output.keys()), f'Partition {ClusterConsts.EMPTY_PARTITION_ID} was not created'
            output = OutputParsingTool.parse_show_output_to_dict(sdn.partition.partition_id[ClusterConsts.EMPTY_PARTITION_ID].show(output_format=output_format, dut_engine=engine),
                                                                 output_format=output_format).get_returned_value()
            partitions_mapping[int(ClusterConsts.EMPTY_PARTITION_ID)] = []
            if not redmine_helpers.is_bug_active(4209873):
                expected_output = {'health': 'healthy', 'locations': {}, 'mcast-limit': mcast_limit, 'name': ClusterConsts.EMPTY_PARTITION_NAME, 'num-gpus': 0, 'partition-type': '', 'resiliency-mode': resiliency_mode}
                ClusterTools.validate_partition_content(output, expected_output)

    @staticmethod
    def validate_partition_content(output, expected_output):
        for key, val in expected_output.items():
            if key in ['locations'] and (not expected_output[key]):
                continue
            else:
                if (redmine_helpers.is_bug_active(4290901) and (val == 'user_action')) or val == '':
                    pass
                else:
                    assert str(output[key]) == str(val), f'Expected value: {val}, Actual value:{output[key]}'

    @staticmethod
    def create_empty_partition_and_add_gpu(sdn, no_reroute='', output_format=OutputFormat.json, zero_uuid_gpus=None, engine=None):
        mapping, original_partition_type = ClusterTools.get_partition_mapping(sdn, engine=engine)
        valid_ids = [key for key, value in mapping.items() if len(value) > 0]
        ClusterTools.create_empty_partition(sdn, mapping, engine=engine)
        _ = random.choice(ClusterConsts.PARTITION_TYPES)
        partition_to_remove_from = random.choice(valid_ids)
        gpus_in_partition = mapping[partition_to_remove_from]
        (uuid, location) = ClusterTools.choose_valid_gpu(gpus_in_partition, zero_uuid_gpus)
        with allure.step(f"Remove GPU from partition {partition_to_remove_from}"):
            if original_partition_type == 'location_based':
                sdn.partition.partition_id[partition_to_remove_from].location.location_id[location].action_restore_partition(engine=engine, reroute_param=no_reroute).verify_result()
            else:
                sdn.partition.partition_id[partition_to_remove_from].uuid.uuid_value[uuid].action_restore_partition(engine=engine, reroute_param=no_reroute).verify_result()

        if redmine_helpers.is_bug_active(4285786):
            time.sleep(15)

        with allure.step(f"Add GPU {uuid} {location} to empty partition {ClusterConsts.EMPTY_PARTITION_ID}"):
            empty_partition_type = random.choice(['uuid', 'location'])
            if empty_partition_type == 'location':
                sdn.partition.partition_id[ClusterConsts.EMPTY_PARTITION_ID].location.location_id[location].action_update_partition(engine=engine, reroute_param=no_reroute).verify_result()
            else:
                sdn.partition.partition_id[ClusterConsts.EMPTY_PARTITION_ID].uuid.uuid_value[uuid].action_update_partition(engine=engine, reroute_param=no_reroute).verify_result()
        return uuid, location, ClusterConsts.EMPTY_PARTITION_ID, partition_to_remove_from

    @staticmethod
    def uuid_location_in_partition(sdn, partition_id, engine=None):
        output = OutputParsingTool.parse_show_output_to_dict(sdn.partition.partition_id[partition_id].show(dut_engine=engine)).get_returned_value()
        uuids = [info['uuid'] for _, info in output['locations'].items()]
        locations = list((output['locations']).keys())
        return uuids, locations

    @staticmethod
    def get_partition_mapping(sdn, output_format=OutputFormat.json, engine=None):
        mapping = {}
        with allure.step("Show All Partitions - at the beginning its just the default partition"):
            initial_partition_output = OutputParsingTool.parse_show_output_to_dict(sdn.partition.show(output_format=output_format, dut_engine=engine),
                                                                                   output_format=output_format).get_returned_value()
            partition_ids = list(initial_partition_output.keys())
            default_partition_id = partition_ids[-1]
            default_partition_type = initial_partition_output[default_partition_id]['partition-type']
        with allure.step("Show partition per partition id"):
            for partition_id in partition_ids:
                output = OutputParsingTool.parse_show_output_to_dict(sdn.partition.partition_id[partition_id].show(output_format=output_format, dut_engine=engine),
                                                                     output_format=output_format).get_returned_value()
                list_of_tuples = ClusterTools.get_partition_uuid_location_map(output)
                mapping[partition_id] = list_of_tuples
        return mapping, default_partition_type

    @staticmethod
    def get_partition_uuid_location_map(partition_output):
        location_uuid_map = partition_output['locations']
        mapping_list = [(info['uuid'], location) for location, info in location_uuid_map.items()]
        return mapping_list

    @staticmethod
    def choose_valid_gpu(gpus_in_partition, zero_uuid_gpus=None):
        """
        Choose a random GPU from the partition, excluding GPUs with uuid="0".
        If zero_uuid_gpus set is provided, any zero-uuid GPUs found are recorded in it.
        """
        if zero_uuid_gpus is not None:
            for uuid, loc in gpus_in_partition:
                if str(uuid) == "0":
                    zero_uuid_gpus.add((uuid, loc))

        valid_gpus = [(uuid, loc) for uuid, loc in gpus_in_partition if str(uuid) != "0"]
        assert valid_gpus, (
            f"No valid GPUs available - all GPUs in partition have uuid=0. "
            f"Zero-UUID locations: {[loc for uuid, loc in gpus_in_partition if str(uuid) == '0']}"
        )
        return random.choice(valid_gpus)

    @staticmethod
    def collect_zero_uuid_gpus(partitions_mapping, zero_uuid_gpus):
        """Scan all partitions and record any GPUs with uuid=0 into the tracker set."""
        for partition_id, gpu_list in partitions_mapping.items():
            for uuid, location in gpu_list:
                if str(uuid) == "0":
                    zero_uuid_gpus.add((uuid, location))

    @staticmethod
    def assert_no_zero_uuid_gpus(zero_uuid_gpus):
        """Fail the test if any GPUs with uuid=0 were observed during the test."""
        if zero_uuid_gpus:
            locations = sorted(loc for _, loc in zero_uuid_gpus)
            assert False, (
                f"GPUs with no UUID assigned (uuid=0) detected at location(s): {locations}. "
                f"No UUID was assigned to these GPUs - this indicates a hardware or configuration issue."
            )

    @staticmethod
    def delete_empty_partition(sdn, partitions_mapping, output_format=OutputFormat.json, engine=None):
        with allure.step("Delete empty partition"):
            sdn.partition.partition_id[ClusterConsts.EMPTY_PARTITION_ID].action_delete_partition(engine=engine)
            start_time = time.time()
            timeout = 25
            while True:
                output = OutputParsingTool.parse_show_output_to_dict(sdn.partition.show(output_format=output_format, dut_engine=engine),
                                                                     output_format=output_format).get_returned_value()
                logger.info("Checking if partition is deleted,")
                if ClusterConsts.EMPTY_PARTITION_ID not in list(output.keys()):
                    elapsed_time = time.time() - start_time
                    logger.info(f"Condition met: Partition {ClusterConsts.EMPTY_PARTITION_ID} deleted after {elapsed_time:.2f} seconds")
                    break

                if time.time() - start_time > timeout:
                    logger.error(f"Timeout: Partition {ClusterConsts.EMPTY_PARTITION_ID} was not deleted within {timeout} seconds")
                    break
                logger.info("Partition is not deleted. Retrying")

            assert ClusterConsts.EMPTY_PARTITION_ID not in list(output.keys()), f'Partition {ClusterConsts.EMPTY_PARTITION_ID} was not deleted'
            partitions_mapping.pop(int(ClusterConsts.EMPTY_PARTITION_ID))

    @staticmethod
    def verify_log_messages_log_level(log_level, system, test_api, cluster, setup_name, engine=None):
        ClusterTools().stop_cluster(cluster, engine=engine)
        ClusterTools().start_cluster(cluster, setup_name, engine=engine)
        TestToolkit.tested_api = 'NVUE'
        lines_checked = 0
        # Get the index of the current log level
        current_level_index = ClusterConsts.ClusterAppsLogLevelsList.index(log_level)

        # Define the expected log levels based on the current log level
        expected_log_levels = ClusterConsts.ClusterAppsLogLevelsList[current_level_index:]
        unexpected_log_levels = ClusterConsts.ClusterAppsLogLevelsList[0:current_level_index]
        # Convert expected log levels to uppercase
        expected_log_levels_upper = [level.upper() for level in expected_log_levels]
        unexpected_log_levels = [level.upper() for level in unexpected_log_levels]

        show_output = system.log.show_log(param=f"| grep -E \"{'|'.join(ClusterConsts.NMX_LOG_MESSAGES_TAGS)}\"").split('\n')[1:]
        for line in show_output:
            if ":~$" in line:  # Symbolizes start of prompt line, no need to check.
                continue
            lines_checked = lines_checked + 1
            assert any(level in line for level in expected_log_levels_upper), f"Line in logs is {repr(line)}, which does not contain any of the expected log levels {expected_log_levels_upper}"
            assert all(level not in line for level in unexpected_log_levels), f"Line in logs is {repr(line)}, which does contains an unexpected log levels {unexpected_log_levels}"
        assert lines_checked > 0, "No lines were checked. No log message related to nmx "

        TestToolkit.tested_api = test_api

    @staticmethod
    def wait_for_apps_to_be_in_wanted_state(cluster, cluster_expected_state='', nmx_c_expected_state='', app='', engine=None, standalone_system=False):
        max_retries = 24
        last_app_output = None
        for attempt in range(max_retries):
            final_sleep_time = 2
            if (not cluster_expected_state) and (not nmx_c_expected_state):
                final_sleep_time += 8
            cluster_output = OutputParsingTool.parse_show_output_to_dict(
                cluster.show(output_format=OutputFormat.json, dut_engine=engine),
                output_format=OutputFormat.json).get_returned_value()
            actual_cluster_state = cluster_output.get(SystemConsts.STATE, ClusterConsts.STATE_UNKNOWN)
            actual_nmxc_state = cluster_output.get(ClusterConsts.NMXC_CONN, ClusterConsts.STATE_UNKNOWN)
            with allure.step(
                    f"Polling until cluster state is {cluster_expected_state} "
                    f"and nmx-c state is {nmx_c_expected_state}"
            ):
                if (cluster_expected_state and actual_cluster_state != cluster_expected_state) or (nmx_c_expected_state and actual_nmxc_state != nmx_c_expected_state):
                    logger.info(f"Cluster state not as expected yet (attempt {attempt + 1}/{max_retries}). Retrying...")
                    logger.info(f"Expected: cluster {cluster_expected_state}, nmx_c {nmx_c_expected_state}.\n Actual: cluster {actual_cluster_state}, nmx_c {actual_nmxc_state}")
                    logger.info("Sleeping for 4 seconds between iterations")
                    time.sleep(4)
                else:
                    logger.info(f"Cluster is now in the wanted state. Sleeping for {final_sleep_time} seconds.")
                    time.sleep(final_sleep_time)
                    if app:
                        if app == ClusterConsts.NMX_CONTROLLER and redmine_helpers.is_bug_active(4207869) and standalone_system:
                            break
                        logger.info(f"Checking {app} state")
                        last_app_output = OutputParsingTool.parse_show_output_to_dict(
                            cluster.apps.running.show(output_format=OutputFormat.json, dut_engine=engine),
                            output_format=OutputFormat.json).get_returned_value()
                        app_status = last_app_output[app]['status']
                        if app_status == 'ok':
                            break
                        logger.info(f"{app} status is '{app_status}', reason: {last_app_output[app].get('reason', 'N/A')} "
                                    f"(attempt {attempt + 1}/{max_retries})")
                        time.sleep(2)
                    else:
                        break
        else:
            if app and last_app_output and app in last_app_output:
                app_info = last_app_output[app]
                raise AssertionError(
                    f"{app} did not reach 'ok' status after {max_retries} retries. "
                    f"Last status: {app_info.get('status', 'unknown')}, "
                    f"reason: {app_info.get('reason', 'N/A')}, "
                    f"addition-info: {app_info.get('addition-info', 'N/A')}. "
                    f"Cluster state: {actual_cluster_state}, nmxc-conn: {actual_nmxc_state}"
                )
            raise AssertionError(
                f"Cluster did not reach expected state after {max_retries} retries. "
                f"Expected: cluster={cluster_expected_state}, nmxc={nmx_c_expected_state}. "
                f"Actual: cluster={actual_cluster_state}, nmxc={actual_nmxc_state}"
            )

    @staticmethod
    def reset_sdn_factory_default_and_wait_for_restart(sdn, cluster, engine=None):
        """
        Perform SDN factory reset and wait for the process to complete.

        IMPORTANT: The SDN factory reset operation takes over 30 seconds to clean all
        configuration and restart all related daemons in NMX-C. If any operation
        (such as disabling the cluster) is performed immediately after calling
        sdn.factory_default.action_reset(), it may interrupt the factory reset process
        and leave the system in an inconsistent state (e.g., stale chassis-id mappings
        from a previous test).

        This helper ensures proper synchronization by:
        1. Triggering the factory reset
        2. Trying to observe the apps transition to down state (reset in progress).
           Uses a short polling window since the down->up transition can be fast;
           if already back to up, the reset completed before we started polling.
        3. Waiting for apps to transition back to enabled/up state (reset complete)

        Args:
            sdn: The Sdn object to perform factory reset on.
            cluster: The Cluster object to monitor app states.
        """
        with allure.step("Running sdn factory reset and waiting for completion"):
            sdn.factory_default.action_reset(param='force', engine=engine)
            try:
                ClusterTools.wait_for_apps_to_be_in_wanted_state(
                    cluster, cluster_expected_state='enabled', nmx_c_expected_state='down', engine=engine)
            except AssertionError:
                logger.info("Did not observe nmx-c 'down' state within the polling window. "
                            "The down->up transition likely completed before polling started.")
            time.sleep(5)
            ClusterTools.wait_for_apps_to_be_in_wanted_state(
                cluster, cluster_expected_state='enabled', nmx_c_expected_state='up', engine=engine)

    @staticmethod
    def verify_sdn_config_files_deleted(sdn, devices, engine=None):
        with allure.step("Running nv show sdn config app <app> type <type> files and make sure files are deleted"):
            # Only check file types for apps that exist on this device
            for app in devices.dut.expected_cluster_apps:
                app_config_files = devices.dut.cluster_config_files_by_app.get(app, [])
                for file_type in app_config_files:
                    files = OutputParsingTool.parse_show_output_to_dict(sdn.config.apps.app_name[app].type.file_type[file_type].files.show(output_format=OutputFormat.json, dut_engine=engine),
                                                                        output_format=OutputFormat.json).get_returned_value()
                assert not files, f"Expected to get empty output, but instead received {files}"

    @staticmethod
    def verify_sdn_state_files_deleted(sdn, standalone_system=False, devices=None, engine=None):
        with allure.step("Running nv show sdn state app <app> type <type> files and make sure files are deleted"):
            # If devices provided, use dynamic state files; otherwise fall back to empty list
            if devices and hasattr(devices.dut, 'expected_cluster_apps'):
                state_files = ClusterTools.get_filtered_state_files(devices, standalone_system)
            else:
                # Device doesn't have cluster attributes - skip verification
                state_files = []

            for file_type in state_files:
                app = ClusterConsts.MAP_STATE_FILE_TYPE_TO_APP[file_type]
                files = OutputParsingTool.parse_show_output_to_dict(sdn.state.apps.app_name[app].type.file_type[file_type].files.show(output_format=OutputFormat.json, dut_engine=engine),
                                                                    output_format=OutputFormat.json).get_returned_value()
                assert not files, f"Expected to get empty output, but instead received {files}"

    @staticmethod
    def _reset_single_compute_node_gpu(node):
        """Run nvidia-smi -r on one compute node. Each node gets its own SSH connection,
        so this is safe to run concurrently across nodes."""
        ip_address = node['ip_address']
        username = node['username']
        password = node['password']
        new_engine = LinuxSshEngine(ip_address, username, password)
        # sudo password is fed via stdin in the same command: `sudo -S` reads it from
        # stdin and `-p ''` suppresses the prompt, so it does not depend on a
        # 'password:' prompt appearing in time.
        #
        # Do NOT use run_cmd() here. run_cmd relies on Netmiko prompt auto-detection,
        # but these compute nodes have a *dynamic* prompt with a live timestamp
        # ("[HH:MM:SS] nvidia@host ~ $"). nvidia-smi -r takes tens of seconds; by the
        # time it finishes the shell redraws the prompt with a different timestamp, so
        # the captured prompt never matches again and the read blocks until read_timeout
        # (~hours) -> the "stuck" hang. The DUT (static NVOS prompt) is unaffected.
        #
        # Instead, emit a fixed sentinel and wait for that explicitly. The quotes keep
        # the sentinel out of the echoed command line, so we match the real output and
        # not the command echo.
        reset_cmd = (f"printf '%s\\n' '{password}' | sudo -S -p '' nvidia-smi -r ; "
                     f"echo GPU__RESET__\"DONE\"")
        logger.info("GPU reset (nvidia-smi -r) starting on %s", ip_address)
        output = new_engine.send_cmd_with_retry(reset_cmd, timeout=300, retries=2,
                                                auto_find_prompt=False,
                                                expected_string='GPU__RESET__DONE')
        # Log the real nvidia-smi -r output (e.g. "GPU 0000...:81:00.0 was successfully
        # reset.\n... All done."). Strip the sentinel line so the log shows only the
        # command's own output.
        clean_output = "\n".join(
            line for line in (output or "").splitlines()
            if "GPU__RESET__DONE" not in line).strip()
        logger.info("GPU reset (nvidia-smi -r) output on %s:\n%s", ip_address, clean_output)
        logger.info("GPU reset (nvidia-smi -r) finished on %s", ip_address)

    @staticmethod
    def reboot_compute_nodes_gpus(setup_name):
        nodes = Configurations.compute_nodes_per_system.get(setup_name)
        if not nodes:
            return
        # Reset every compute host in PARALLEL. A single nvidia-smi -r takes ~2 min; doing
        # them sequentially across N hosts, multiplied by the 3-4 reset points in the tray
        # test, blew past the pytest function timeout and aborted the test mid-reset into its
        # finally/cleanup block (which then issued an invalid `nv action restore` on a tray
        # still 'down' -> NMX_ST_BADPARAM). Each node uses its own SSH connection, so the
        # resets are independent; running them concurrently makes wall time ~= one host.
        errors = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(nodes)) as executor:
            future_to_ip = {
                executor.submit(ClusterTools._reset_single_compute_node_gpu, node):
                    node['ip_address']
                for node in nodes
            }
            for future in concurrent.futures.as_completed(future_to_ip):
                ip = future_to_ip[future]
                try:
                    future.result()
                except Exception as exc:  # noqa: BLE001 - aggregate per-host failures
                    errors[ip] = exc
                    logger.error("GPU reset failed on %s: %s", ip, exc)
        if errors:
            raise RuntimeError(
                "nvidia-smi -r failed on compute node(s): " +
                ", ".join(f"{ip} ({err})" for ip, err in errors.items()))
        time.sleep(10)

    @staticmethod
    def edit_config_file(path, edit_commands, engine):
        # Run each edit as a separate command so SSH/Netmiko pattern detection works:
        # multi-line commands are echoed line-by-line with prompts in between, so the
        # full-command pattern never appears and read_until_pattern times out.
        full_text = "\n".join(edit_commands).replace("{file}", path)
        for line in full_text.strip().split("\n"):
            line = line.strip()
            if line and not line.startswith("#"):
                engine.run_cmd(line)

        modified_content = engine.run_cmd(f"cat {path}")
        logger.info(f"File content after edits:\n{modified_content}")

        expected_lines = []
        for cmd in edit_commands:
            match = re.search(r"/c\\(.+?)'", cmd)
            if match:
                expected_lines.append(match.group(1))

        missing = [line for line in expected_lines if line not in modified_content]
        if missing:
            raise AssertionError(
                f"Config file edit verification FAILED for {path}!\n"
                f"Expected lines not found after sed: {missing}\n"
                f"Actual file content:\n{modified_content}"
            )

    @staticmethod
    def get_generated_file_info(sdn, config_type, engine=None):
        output_format = OutputFormat.json
        output = sdn.config.apps.app_name[ClusterConsts.NMX_CONTROLLER].type.file_type[
            config_type].action_generate_sdn(dut_engine=engine).get_returned_value()
        file_name = ClusterTools().get_generated_sdn_file(output, 'config')
        output_dict = OutputParsingTool.parse_show_output_to_dict(
            sdn.config.apps.app_name[ClusterConsts.NMX_CONTROLLER].type.file_type[config_type].files.show(
                output_format=output_format, dut_engine=engine),
            output_format=output_format).get_returned_value()
        path = output_dict[file_name]['path']
        return file_name, path

    @staticmethod
    def edit_fm_config(sdn, engine, devices, standalone_system):
        """
        Edit FM config based on device-specific requirements.
        Returns None if device doesn't require FM config edits (e.g., Rosalind).
        """
        # Check if device has FM config edits defined
        attr = 'sdn_fm_config_edits_standalone' if standalone_system else 'sdn_fm_config_edits'
        config = getattr(devices.dut, attr, None)
        if config is None:
            logger.info(f"Device {devices.dut.__class__.__name__} does not require FM config edits. Skipping.")
            return None

        fm_config = ClusterConsts.NMX_CONTROLLER_CONFIG_FILE_TYPES[0]
        fm_generated_file_name, fm_path = ClusterTools.get_generated_file_info(sdn, fm_config, engine=engine)
        fm_original_content = engine.run_cmd(f"cat {fm_path}")
        logger.info(f"Adjusting fm_config file for {devices.dut.__class__.__name__}.")
        ClusterTools().edit_config_file(fm_path, config, engine)
        sdn.config.apps.app_name[ClusterConsts.NMX_CONTROLLER].type.file_type[fm_config].files.file_name[
            fm_generated_file_name].action_file_install(force=False, dut_engine=engine)
        return fm_config, fm_generated_file_name, fm_path, fm_original_content

    @staticmethod
    def edit_sm_config(sdn, engine, devices, standalone_system, has_loopbox, is_simx=False):
        """
        Edit SM config based on device-specific requirements.
        SM config is ONLY edited when: standalone=True AND has_loopbox=True.
        Returns None if conditions are not met or device doesn't have SM config edits defined.
        """
        if not (standalone_system and has_loopbox) and not is_simx:
            logger.info(f"Device {devices.dut.__class__.__name__} - skipping SM config edit "
                        f"(standalone={standalone_system}, has_loopbox={has_loopbox}).")
            return None

        if not hasattr(devices.dut, 'sdn_sm_config_edits') or devices.dut.sdn_sm_config_edits is None:
            logger.info(f"Device {devices.dut.__class__.__name__} does not have SM config edits defined. Skipping.")
            return None

        sm_config = ClusterConsts.NMX_CONTROLLER_CONFIG_FILE_TYPES[1]
        sm_generated_file_name, sm_path = ClusterTools.get_generated_file_info(sdn, sm_config, engine=engine)
        sm_original_content = engine.run_cmd(f"cat {sm_path}")
        logger.info(f"Adjusting sm_config file for {devices.dut.__class__.__name__}.")
        ClusterTools().edit_config_file(sm_path, devices.dut.sdn_sm_config_edits, engine)
        sdn.config.apps.app_name[ClusterConsts.NMX_CONTROLLER].type.file_type[sm_config].files.file_name[
            sm_generated_file_name].action_file_install(force=False, dut_engine=engine)
        return sm_config, sm_generated_file_name, sm_path, sm_original_content

    @staticmethod
    def wa_to_get_active_interface_for_loopbox_systems(cluster, sdn, devices, engine, dut_engines, has_loopbox, setup_name,
                                                       standalone_system, is_simx=False):
        def get_generated_file_info(config_type):
            output = sdn.config.apps.app_name[ClusterConsts.NMX_CONTROLLER].type.file_type[
                config_type].action_generate_sdn().get_returned_value()
            file_name = ClusterTools().get_generated_sdn_file(output, 'config')
            output_dict = OutputParsingTool.parse_show_output_to_dict(
                sdn.config.apps.app_name[ClusterConsts.NMX_CONTROLLER].type.file_type[config_type].files.show(
                    output_format=output_format),
                output_format=output_format).get_returned_value()
            path = output_dict[file_name]['path']
            return file_name, path

        output_format = OutputFormat.json

        # Device-specific: Run pre-cluster setup if needed (e.g., Rosalind)
        if hasattr(devices.dut, 'sdn_needs_pre_cluster_setup') and devices.dut.sdn_needs_pre_cluster_setup:
            with allure.step(f"Device {devices.dut.__class__.__name__} requires pre-cluster setup"):
                # Call device-specific setup method
                devices.dut.setup_cluster_for_sdn_config(cluster, engine, dut_engines)
                logger.info("Waiting for nmx-controller to be up after cluster setup")
                ClusterTools.wait_for_apps_to_be_in_wanted_state(cluster, cluster_expected_state='enabled', nmx_c_expected_state='up', engine=engine)

        # Edit FM config (device-specific, may return None for devices like Rosalind)
        fm_result = ClusterTools.edit_fm_config(sdn, engine, devices, standalone_system)
        if fm_result:
            fm_config, fm_generated_file_name, fm_path, fm_original_content = fm_result
        else:
            fm_config = fm_generated_file_name = fm_path = fm_original_content = None

        # Edit SM config (includes standalone/loopbox guard internally)
        sm_result = ClusterTools.edit_sm_config(sdn, engine, devices, standalone_system, has_loopbox, is_simx)
        if sm_result:
            sm_config, sm_generated_file_name, sm_path, sm_original_content = sm_result

            # Apply device-specific workaround after SM config is installed (e.g., Rosalind Bug 4910763)
            if hasattr(devices.dut, 'wa_restart_nv_bridge_after_sm_config'):
                devices.dut.wa_restart_nv_bridge_after_sm_config(cluster, engine)
        else:
            sm_config = sm_generated_file_name = sm_path = sm_original_content = None

        # WA for Bug SW #5074056: changes in fm_config are not picked up without restarting
        # nmx-controller. Restart the app after loading both fm and sm configs.
        if redmine_helpers.is_bug_active(5074056):
            with allure.step("WA for Bug 5074056: Restart nmx-controller after loading configs"):
                logger.info("Stopping nmx-controller app")
                cluster.apps.app_name[ClusterConsts.NMX_CONTROLLER].action_stop_cluster_app(engine=engine)
                time.sleep(5)
                logger.info("Starting nmx-controller app")
                cluster.apps.app_name[ClusterConsts.NMX_CONTROLLER].action_start_cluster_app(engine=engine)
                ClusterTools.wait_for_apps_to_be_in_wanted_state(cluster, cluster_expected_state='enabled', nmx_c_expected_state='up', engine=engine)

        ClusterTools.validate_cluster_enabled(cluster, engine=engine)

        yield

        if ClusterTools.check_cluster_state(cluster, output_format, engine=engine) == 'disabled':
            ClusterTools.start_cluster(cluster, setup_name, output_format=output_format, devices=devices, engine=engine)

        # Restore FM config if it was edited
        if fm_path and "Exists" in engine.run_cmd(f'test -e {fm_path} && echo "Exists" || echo "Does not exist"'):
            engine.run_cmd(f"echo '{fm_original_content}' | sudo tee {fm_path} > /dev/null")
            sdn.config.apps.app_name[ClusterConsts.NMX_CONTROLLER].type.file_type[fm_config].files.file_name[
                fm_generated_file_name].action_file_install(force=False, dut_engine=engine)
            sdn.config.apps.app_name[ClusterConsts.NMX_CONTROLLER].type.file_type[fm_config].files.file_name[
                fm_generated_file_name].action_delete(engine=engine).verify_result()

        # Restore SM config if it was edited
        if sm_path and "Exists" in engine.run_cmd(
                f'test -e {sm_path} && echo "Exists" || echo "Does not exist"'):
            engine.run_cmd(f"echo '{sm_original_content}' | sudo tee {sm_path} > /dev/null")
            sdn.config.apps.app_name[ClusterConsts.NMX_CONTROLLER].type.file_type[sm_config].files.file_name[
                sm_generated_file_name].action_file_install(force=False, dut_engine=engine)
            sdn.config.apps.app_name[ClusterConsts.NMX_CONTROLLER].type.file_type[sm_config].files.file_name[
                sm_generated_file_name].action_delete(engine=engine).verify_result()

        # Note: No need to cleanup pre-cluster setup (e.g., cluster node primary server)
        # The disabled_access_ports decorator already does cluster.unset(apply=True) which cleans everything

    @staticmethod
    def get_generated_sdn_file(output, file_type):
        # Use a regular expression to capture the filename
        match = re.search(fr"App {file_type} file (\S+)", output)
        if match:
            filename = match.group(1)
            return filename
        else:
            return None

    @retry.retry(Exception, tries=6, delay=5)
    def wait_until_app_expected_status(cluster, app, expected_status, engine=None):
        with allure.step(f"Waiting for {app} to be in {expected_status} status"):
            output = OutputParsingTool.parse_show_output_to_dict(
                cluster.apps.running.show(output_format=OutputFormat.json, dut_engine=engine)).get_returned_value()
            app_status = output[app]['status']
            assert app_status == expected_status, f"App {app} status is {app_status} instead of {expected_status}"

    @staticmethod
    def verify_control_plane_state(cluster, expected_state, engine=None):
        with allure.step(f"Verify nmx-controller control plane state is {expected_state}"):
            output = OutputParsingTool.parse_show_output_to_dict(
                cluster.apps.running.show(output_format=OutputFormat.json, dut_engine=engine)).get_returned_value()
            control_plane_state = output[ClusterConsts.NMX_CONTROLLER]['addition-info']
            return control_plane_state == expected_state

    @staticmethod
    def wait_for_app_healthy(cluster, app, expected_output=None, max_retries=7, delay=10, engine=None):
        """
        Wait for a cluster app to become healthy. NMX-T can take up to ~60s after cluster
        start to report healthy while the NVLink network is being configured.

        If expected_output dict is provided, uses ValidationTool.validate_output_of_show
        against a per-app show query. Otherwise checks status == 'ok' via apps running.
        """
        with allure.step(f"Waiting for {app} to become healthy (up to {max_retries * delay}s)"):
            for attempt in range(max_retries):
                if expected_output is not None:
                    output = OutputParsingTool.parse_show_output_to_dict(
                        cluster.apps.app_name[app].show(output_format=OutputFormat.json, dut_engine=engine),
                        output_format=OutputFormat.json).get_returned_value()
                    validation_result = ValidationTool.validate_output_of_show(output, expected_output)
                    if validation_result.result:
                        return
                    validation_result.ignore_result()
                else:
                    output = OutputParsingTool.parse_show_output_to_dict(
                        cluster.apps.running.show(output_format=OutputFormat.json, dut_engine=engine),
                        output_format=OutputFormat.json).get_returned_value()
                    if output[app]['status'] == 'ok':
                        return

                if attempt < max_retries - 1:
                    logger.info(f"{app} not healthy yet (attempt {attempt + 1}/{max_retries}), "
                                f"retrying in {delay}s...")
                    time.sleep(delay)

            if expected_output is not None:
                validation_result.verify_result()
            else:
                app_status = output[app]['status']
                assert app_status == 'ok', f"App {app} status is {app_status} instead of 'ok'"


def summarize_switch_ports(ports_list):
    # Dictionary to store ranges for each prefix
    segments = defaultdict(set)

    # Regex to match any prefix followed by numbers (e.g., "sw1", "p1", "s1")
    pattern = re.compile(r'([a-zA-Z]+)(\d+)')

    for port in ports_list:
        # Find all (prefix, number) pairs in each port string
        matches = pattern.findall(port)
        for prefix, num in matches:
            segments[prefix].add(int(num))  # Collect numbers for each prefix

    # Build the summary string
    summary_parts = []
    for prefix, numbers in segments.items():
        min_num, max_num = min(numbers), max(numbers)
        summary_parts.append(f"{prefix}{min_num}-{max_num}")

    # Join the parts into the final string
    return ''.join(summary_parts)


def refresh_switch_ports(ports_list, engine):
    TestToolkit.tested_api = 'NVUE'
    port_name = summarize_switch_ports(ports_list)
    selected_port = Port(port_name, "", "")
    port_state = NvosConsts.LINK_STATE_DOWN
    selected_port.interface.link.state.set(op_param_name=port_state, apply=True, ask_for_confirmation=True, dut_engine=engine).verify_result()
    TestToolkit.GeneralApi[TestToolkit.tested_api].save_config(engine)
    time.sleep(30)
    port_state = NvosConsts.LINK_STATE_UP
    selected_port.interface.link.state.set(op_param_name=port_state, apply=True, ask_for_confirmation=True, dut_engine=engine).verify_result()
    TestToolkit.GeneralApi[TestToolkit.tested_api].save_config(engine)
    time.sleep(30)


def disabled_access_ports(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        sig = inspect.signature(func)
        bound_args = sig.bind(*args, **kwargs)
        bound_args.apply_defaults()
        devices = bound_args.arguments.get('devices', None)
        engine = bound_args.arguments.get('engine', None)
        engines = bound_args.arguments.get('engines', None)
        if engine is None and engines is not None:
            engine = getattr(engines, 'dut', None)
        dut_engines = bound_args.arguments.get('dut_engines', None)
        if dut_engines is None and engines is not None:
            dut_engines = {'dut': engine}
        has_loopbox = bound_args.arguments.get('has_loopbox', None)
        standalone_system = bound_args.arguments.get('standalone_system', None)
        setup_name = bound_args.arguments.get('setup_name', None)
        perform_cleanup = bound_args.arguments.get('perform_cleanup', True)
        is_simx = bound_args.arguments.get('is_simx', False)
        original_api = bound_args.arguments.get('test_api',
                                                bound_args.arguments.get('random_api', TestToolkit.tested_api))
        has_access_ports = True
        interface_wa_called = False
        cluster = Cluster()
        sdn = Sdn()
        try:
            if isinstance(devices.dut, JulietSwitch):
                TestToolkit.tested_api = 'NVUE'
                if not hasattr(devices.dut, 'nvl_access_ports_list'):
                    has_access_ports = False
                if has_access_ports and standalone_system and not has_loopbox:
                    port_name = summarize_ports(devices.dut.nvl_access_ports_list)
                    selected_port = Port(port_name, "", "")
                    port_state = NvosConsts.LINK_STATE_DOWN
                    selected_port.interface.link.state.set(op_param_name=port_state, apply=True, ask_for_confirmation=True, dut_engine=engine).verify_result()
                    TestToolkit.GeneralApi[TestToolkit.tested_api].save_config(engine)
                if not standalone_system:
                    if Configurations.ports_to_disable[setup_name] != []:
                        for port in Configurations.ports_to_disable[setup_name]:
                            selected_port = Port(port, "", "")
                            port_state = NvosConsts.LINK_STATE_DOWN
                            selected_port.interface.link.state.set(op_param_name=port_state, apply=True, ask_for_confirmation=True, dut_engine=engine).verify_result()
                        TestToolkit.GeneralApi[TestToolkit.tested_api].save_config(engine)

                ClusterTools().stop_cluster(cluster, engine=engine)
                ClusterTools().start_cluster(cluster, setup_name, devices=devices, engine=engine)
                interfaces_wa = ClusterTools().wa_to_get_active_interface_for_loopbox_systems(cluster, sdn, devices, engine, dut_engines, has_loopbox, setup_name, standalone_system, is_simx)
                next(interfaces_wa)
                interface_wa_called = True
                with allure.step("Unset Cluster before test starts to run, to make sure we are at the correct init state"):
                    cluster.unset(apply=True, dut_engine=engine)
                    ClusterTools.wait_for_apps_to_be_in_wanted_state(cluster, cluster_expected_state='disabled', nmx_c_expected_state='down', engine=engine)
                    logger.info("Sleeping for 15 seconds to make sure nnx-t docker is down")
                    time.sleep(15)
                # Execute the test function
            return func(*args, **kwargs)
        finally:
            if perform_cleanup:
                if isinstance(devices.dut, JulietSwitch):
                    if has_access_ports and standalone_system and not has_loopbox:
                        port_name = summarize_ports(devices.dut.nvl_access_ports_list)
                        selected_port = Port(port_name, "", "")
                        port_state = NvosConsts.LINK_STATE_UP
                        selected_port.interface.link.state.set(op_param_name=port_state, apply=True, ask_for_confirmation=True, dut_engine=engine).verify_result()
                        TestToolkit.GeneralApi[TestToolkit.tested_api].save_config(engine)
                    if not standalone_system:
                        if Configurations.ports_to_disable[setup_name] != []:
                            for port in Configurations.ports_to_disable[setup_name]:
                                selected_port = Port(port, "", "")
                                port_state = NvosConsts.LINK_STATE_UP
                                selected_port.interface.link.state.set(op_param_name=port_state, apply=True, ask_for_confirmation=True, dut_engine=engine).verify_result()
                            TestToolkit.GeneralApi[TestToolkit.tested_api].save_config(engine)
                    if interface_wa_called:
                        try:
                            next(interfaces_wa)
                        except StopIteration:
                            pass  # Or handle it if necessary
                    if hasattr(devices.dut, 'nvl_trunk_ports_list') and devices.dut.nvl_trunk_ports_list:
                        refresh_switch_ports(devices.dut.nvl_trunk_ports_list, engine)
                    with allure.step("Reset cluster state"):
                        if ClusterTools.check_cluster_state(cluster, OutputFormat.json, engine=engine) == 'enabled':
                            ClusterTools.reset_sdn_factory_default_and_wait_for_restart(sdn, cluster, engine=engine)
                            cluster.unset(apply=True, dut_engine=engine)
                            ClusterTools.wait_for_apps_to_be_in_wanted_state(cluster, cluster_expected_state='disabled', nmx_c_expected_state='down', engine=engine)
            TestToolkit.tested_api = original_api
    return wrapper


def summarize_ports(ports_list: list[str]) -> str:
    """
    Summarize a list of ports into a single string.
    e.g. ['acp1', 'acp2', 'acp3'] -> 'acp1-3'
    e.g. ['acp1', 'acp2', 'acp3', 'acp5'] -> 'acp1,acp2,acp3,acp5'

    Args:
        ports_list: The list of ports to summarize.

    Returns:
        A summarized port range string.
        e.g. 'acp1-3'
        e.g. 'acp1,acp2,acp3,acp5'
    """
    if not ports_list:
        return ''
    if len(ports_list) == 1:
        return ports_list[0]

    # Extract the prefix and numbers from the port names
    pattern = re.compile(r'([^\d]+)(\d+)')
    prefixes = set()
    numbers = []

    for port in ports_list:
        match = pattern.match(port)
        if match:
            prefix, num = match.groups()
            prefixes.add(prefix)
            numbers.append(int(num))
        else:
            raise ValueError(f"Port name '{port}' does not match expected pattern.")

    if len(prefixes) > 1:
        raise ValueError(f"Multiple prefixes found: {prefixes}")

    prefix = prefixes.pop()
    min_num = min(numbers)
    max_num = max(numbers)

    # Check if numbers are consecutive
    expected_numbers = set(range(min_num, max_num + 1))
    if set(numbers) != expected_numbers:
        # If not consecutive, return the list as is
        return ','.join(ports_list)

    return f'{prefix}{min_num}-{max_num}'


class ClusterSimulation:
    # simulation for the SDN maintenance state tests
    # simulate the cluster as topology of 2 racks

    def start_sdn_cluster_simulation(engine, setup_name):
        with allure.step("Start of sdn cluster simulation"):
            cluster = Cluster()

            with allure.step("Disable cluster"):
                ClusterTools.stop_cluster(cluster, engine=engine)

            with allure.step("Generate simulator_config.json file"):
                ClusterSimulation.generate_simulator_config_file(engine)

            with allure.step("Apply the patch for /usr/share/cluster_pkgs/nmx-controller/job.json"):
                ClusterSimulation.apply_patch_for_nmc_controller_job(engine)

            with allure.step("Enable cluster"):
                ClusterTools.start_cluster(cluster, setup_name, engine=engine)

            with allure.step("Config fm config"):
                ClusterSimulation.config_fm_config(engine)

            with allure.step("Wait for nmx-controller to be in ok status"):
                ClusterTools.wait_until_app_expected_status(cluster, ClusterConsts.NMX_CONTROLLER, "ok", engine=engine)

    @staticmethod
    def end_of_sdn_cluster_simulation(engine, setup_name):
        with allure.step("End of sdn cluster simulation"):
            cluster = Cluster()

            with allure.step("Disable cluster"):
                ClusterTools.stop_cluster(cluster, engine=engine)

            with allure.step("Restore /usr/share/cluster_pkgs/nmx-controller/job.json"):
                ClusterSimulation.restore_nmc_controller_job(engine)

            with allure.step("Enable cluster"):
                ClusterTools.start_cluster(cluster, setup_name, engine=engine)

            with allure.step("Reset sdn factory default"):
                ClusterTools.reset_sdn_factory_default_and_wait_for_restart(Sdn(), cluster, engine=engine)

    @staticmethod
    def generate_simulator_config_file(engine):
        with allure.step("Generate simulator_config.json file in /etc/cluster_infra/conf"):
            file_cont = '{"alids_start_id": 1024, "topology_id": 129, "vendor_id": 713, "gpu_pcie_id": 10496, "switch_pcie_id": 54004, \
                    "partition_start_id": 32766, "partition_default_name": "Default Partition", "gpu_description": "GB100 Nvidia Technologies", \
                    "switch_description": "MF0;mc-gb-nvl-020-001-switch:N5110_LD/U", "chassis1_serial_number": "27XYZ27000001", \
                    "chassis2_serial_number": "27XYZ27000002", "nmxc_uid_start": 0, "gpu_reset_probability": 0, "gpu_reset_timeout": 10, \
                    "switch_reset_probability": 0, "switch_reset_timeout": 10, "max_gpu_down": 0, "gpu_down_probability": 0, "max_switch_down": 0, \
                    "switch_down_probability": 0}'
            cmd = f"echo '{file_cont}' | sudo tee /etc/cluster_infra/conf/simulator_config.json"
            engine.run_cmd(cmd)

    @staticmethod
    def apply_patch_for_nmc_controller_job(engine):
        with allure.step("Apply the patch for /usr/share/cluster_pkgs/nmx-controller/job.json"):
            job_json_path = "/usr/share/cluster_pkgs/nmx-controller/job.json"
            # Backup original job.json
            engine.run_cmd(f"sudo mv {job_json_path} {job_json_path}.bak")
            # Use python3 to add --sim-mode arg and SIM_MODE env var to the nmxc task.
            # This handles both juliet (args-based config) and rosalind (env-var-based config)
            # job.json formats. The SIM_MODE env var is required for the nmxc entrypoint script
            # to select the correct supervisord config (supervisord_sim.conf).
            cmd_modify = (
                f"cat {job_json_path}.bak | python3 -c \""
                "import json, sys; "
                "data = json.load(sys.stdin); "
                "[task['Config']['args'].append('--sim-mode') or "
                "task.setdefault('Env', {}).update({'SIM_MODE': 'true'}) "
                "for tg in data['Job']['TaskGroups'] "
                "for task in tg['Tasks'] if task['Name'] == 'nmxc']; "
                "json.dump(data, sys.stdout, indent=2)"
                f"\" | sudo tee {job_json_path}"
            )
            engine.run_cmd(cmd_modify)

    @staticmethod
    def restore_nmc_controller_job(engine):
        with allure.step("Restore /usr/share/cluster_pkgs/nmx-controller/job.json"):
            cmd_restore = "sudo mv /usr/share/cluster_pkgs/nmx-controller/job.json.bak /usr/share/cluster_pkgs/nmx-controller/job.json"
            engine.run_cmd(cmd_restore)

    @staticmethod
    def config_fm_config(engine, topology_value='gb200_nvl72r2_c2g4_topology'):
        with allure.step("Config fm config"):
            # Run each command separately to avoid Netmiko ReadTimeout.
            # Sending multi-line scripts via run_cmd fails because the shell echoes each line
            # with a prompt in between, breaking Netmiko's command-echo pattern detection.
            fmcfg_path = '/host/cluster_infra/app_config/nmx-controller/fm_config'
            fmcfg_name = 'fm_cfg'
            topology_key = 'MNNVL_TOPOLOGY'

            engine.run_cmd(f'sudo mkdir -p "{fmcfg_path}"')
            engine.run_cmd('nv action generate sdn config app nmx-controller type fm_config')
            latest_file = engine.run_cmd(f'ls -Art "{fmcfg_path}" | tail -n 1').strip()
            engine.run_cmd(f'sudo cp "{fmcfg_path}/{latest_file}" "{fmcfg_path}/{fmcfg_name}"')
            engine.run_cmd(
                f'sudo sed -i -e "/^{topology_key}=/{{h;s/=.*/={topology_value}/}};\\${{x;/^$/{{s//{topology_key}={topology_value}/;H}};x}}" '
                f'"{fmcfg_path}/{fmcfg_name}"'
            )
            engine.run_cmd(f'cat "{fmcfg_path}/{fmcfg_name}"')
            engine.run_cmd(
                f'nv action install sdn config app nmx-controller type fm_config files "{fmcfg_name}"',
                validate=True
            )
