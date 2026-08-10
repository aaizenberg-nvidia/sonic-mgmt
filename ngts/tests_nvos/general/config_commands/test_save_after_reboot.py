import logging
import random
import time

import pytest
from ngts.nvos_tools.acl.acl import Acl
from ngts.nvos_tools.Devices.cpo.CpoTopology import is_cpo_capable
from ngts.nvos_constants.constants_nvos import FastRecoveryConsts, LinkDetectionConsts, ActionConsts, HealthConsts, \
    FansConsts
from ngts.nvos_constants.constants_nvos import SystemConsts, NvosConst, ApiType, AclConsts, IpConsts, EventConsts
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import IbInterfaceConsts
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.nvos_tools.infra.Tools import Tools
from ngts.nvos_tools.infra.Simulator import HWSimulator
from ngts.nvos_tools.system.System import System
from ngts.nvos_tools.platform.Platform import Platform
from ngts.tests_nvos.constants import MINUTE
from ngts.tests_nvos.helpers.redmine_helpers import is_bug_active
from ngts.tools.test_utils import allure_utils as allure
from ngts.nvos_tools.nmx.Cluster import Cluster
from ngts.tests_nvos.cluster.cluster_consts import ClusterConsts
from ngts.tests_nvos.cluster.cluster_tools import ClusterTools


cumulus_owner = "gosaini"


logger = logging.getLogger()


@pytest.mark.timeout(20 * MINUTE, func_only=True)
@pytest.mark.general
def test_save_reboot(engines, devices):
    """
        Test flow:
            1.  run nv set system hostname <new_hostname> with apply
            2.  Run 'nv set fae fast-recovery state disabled' and apply config
            3.  Run 'nv system contact "contact_info_1"' and apply config
            4.  Run 'nv system location "location_info_1"' and apply config
            5.  Run 'nv set system message pre-login'
            6.  Run 'nv set system message post-logout'
            7.  run nv config save
            8.  Run 'nv system contact "contact_info_2"' and apply config
            9.  Run 'nv system location "location_info_2"' and apply config
            10. Run 'nv set system message post-login'
            11. Run nv set interface eth0 description <new_description> with apply
            12. Run 'nv set fast-recovery trigger credit-watchdog event warning' and apply config
            13. Run nv action reboot system
            14. Run nv show system after reload
            15. verify hostname is new_hostname
            16. Verify fast-recovery state is Disabled
            17. Run nv show interface eth0
            18. Verify the applied description value is ''
            19. Verify that applied system contact is "contact_info_1"
            20. Verify that applied system location is "location_info_1"
            21. Run 'nv show system message' and verify pre-login message is as set
            22. Run 'nv show system message' and verify post-logout message is as set
            23. Run 'nv show system message' and verify post-login message is default
            24. Verify fast-recovery trigger event for trigger-id is Error
            25. cleanup - run nv unset system hostname & reboot
    """

    nmx_log_stream_test = False
    if hasattr(devices.dut, "cluster_app_nmx_controller"):
        nmx_log_stream_test = True

    with allure.step('Run show system command and verify that each field has a value'):
        system = System()
        sys_info = OutputParsingTool.parse_json_str_to_dictionary(system.show()).get_returned_value()
        old_hostname = sys_info[SystemConsts.HOSTNAME]
        new_hostname_value = 'TestingConfigCmds'

        # TODO: Fix fae recovery
        '''with allure_step('Run set fae fast-recovery state command to set to disable and apply config'):
            fae.fast_recovery.set(FastRecoveryConsts.STATE,
                                  FastRecoveryConsts.STATE_DISABLED, apply=True, dut_engine=engines.dut)'''

        with allure.step('Set system events table-size to 1200 and validate'):
            system.events.set(op_param_name='table-size', op_param_value=1200, apply=True, dut_engine=engines.dut).\
                verify_result()
            output = OutputParsingTool.parse_json_str_to_dictionary(system.events.show()).get_returned_value()
            ValidationTool.verify_field_value_in_output(output, EventConsts.TABLE_SIZE, '1200').verify_result()

        with allure.step('Simulate 10 system events'):
            output = engines.dut.run_cmd('docker exec eventd events_publish_test.py -c 10')
            assert output == '', 'Error in executing simulate command: {}'.format(output)
            time.sleep(10)

        with allure.step('Extract last system event to verify post reboot'):
            output = OutputParsingTool.parse_json_str_to_dictionary(system.events.show_events_last_recent_entries(SystemConsts.SYSTEM_LAST_EVENT, '1')).get_returned_value()
            event_id = str(list(output.keys())[0])
            event_time = output[event_id]["time-created"]

        with allure.step('Run set system contact contact_info_1 command and apply config'):
            system.set(op_param_name=SystemConsts.CONTACT, op_param_value="contact_info_1", apply=True,
                       dut_engine=engines.dut).verify_result()

        with allure.step('Run set system location location_info_1 command and apply config'):
            system.set(op_param_name=SystemConsts.LOCATION, op_param_value="location_info_1", apply=True,
                       dut_engine=engines.dut).verify_result()

        with allure.step('Run set system message pre-login command and apply config'):
            system.message.set(op_param_name=SystemConsts.PRE_LOGIN_MESSAGE, op_param_value=f'"Pre login test msg"',
                               apply=True, dut_engine=engines.dut).verify_result()

        with allure.step('Run set system message post-logout command and apply config'):
            system.message.set(op_param_name=SystemConsts.POST_LOGOUT_MESSAGE, op_param_value=f'"Post logout test msg"',
                               apply=True, dut_engine=engines.dut).verify_result()

        with allure.step('Run set system dns server ipv4 command and apply config'):
            system.dns.set(op_param_name=SystemConsts.DNS_SERVER, op_param_value=SystemConsts.DNS_SERVER_IDS["ipv4"],
                           apply=True, dut_engine=engines.dut).verify_result()
        with allure.step('Run set acl <acl_name> rule <rule_id> action set dscp'):
            acl_id = 'ACL_TO_BE_SAVED'
            rule_id = 1
            acl_type = IpConsts.IPV4
            acl_obj = Acl()
            acl_obj.set(acl_id).verify_result()
            acl_id_obj = acl_obj.acl_id[acl_id]
            acl_id_obj.set(AclConsts.TYPE, acl_type).verify_result()
            acl_id_obj.rule.set(rule_id).verify_result()
            rule_id_obj = acl_id_obj.rule.rule_id[rule_id]
            rule_id_obj.action.dscp.set(1, apply=True)
        with allure.step("Validate dscp configuration with show commands"):
            output = OutputParsingTool.parse_dscp_value_from_acl(engines, acl_obj, acl_id, rule_id)
            ValidationTool.verify_field_value_in_output(output, AclConsts.DSCP, 1).verify_result()

        if nmx_log_stream_test:
            with allure.step("Start Cluster"):
                cluster = Cluster()
                cluster.set(op_param_name="state", op_param_value='enabled', apply=True)
                ClusterTools.wait_for_apps_to_be_in_wanted_state(cluster, cluster_expected_state='enabled',
                                                                 nmx_c_expected_state='up')

            with allure.step("Set NMX-C log stream configuration"):
                player = engines.sonic_mgmt
                url = f'{player.username}:{player.password}@{player.ip}:{ClusterConsts.NMXC_LOG_STREAM_DEFAULT_PORT}'
                url_show = f'{player.username}:********@{player.ip}:{ClusterConsts.NMXC_LOG_STREAM_DEFAULT_PORT}'
                stream = f'{ClusterConsts.PROTOCOL_RSYSLOG} {url}'
                cluster.apps.app_name[ClusterConsts.NMX_CONTROLLER].logstream.action_update_cluster_log_stream(stream=stream)

        with allure.step('set hostname to be {hostname} - with apply'.format(hostname=new_hostname_value)):
            system.set(SystemConsts.HOSTNAME, new_hostname_value, apply=True, ask_for_confirmation=True)

        fec_capable = devices.dut.check_fec_capability()
        if fec_capable:
            selected_port = Tools.RandomizationTool.select_random_port(requested_ports_state=None).get_returned_value()
            fec_mode = LinkDetectionConsts.FEC_MODE_DOUBLE
            with allure.step("Set the fec mode to {} for the selected port {}".format(fec_mode, selected_port.name)):
                selected_port.interface.link.set(op_param_name=LinkDetectionConsts.FEC_MODE, op_param_value=fec_mode).\
                    verify_result()

        with allure.step("Update the health component unhealthy counters"):
            with allure.step("clear system health component"):
                system.health.component.action(ActionConsts.CLEAR)
            with allure.step("simulate fan health error"):
                simulated_fan_name = HWSimulator.create_health_component_error_fan(devices, engines)
            with allure.step("Verify pre-reboot fault was registered "
                             "(distinguishes detection failure from persistence failure)"):
                if simulated_fan_name and FansConsts.HW_MANAGEMENT_TC_SERVICE in devices.dut.available_services:
                    pre_health = system.health.component.parse_show()
                    pre_fan = pre_health[HealthConsts.Component.FAN][HealthConsts.Component.INSTANCE][
                        simulated_fan_name]
                    assert int(pre_fan[HealthConsts.Component.UNHEALTHY_COUNT]) == 1, (
                        f"Pre-reboot: simulator did not produce an UNHEALTHY event for {simulated_fan_name}. "
                        f"This is a DETECTION failure (likely thermalctld set_under_speed grace-period "
                        f"race with hw-management-tc PWM updates), not a sticky-events persistence bug. "
                        f"See docs/bug_health_sticky_unhealthy_counters_not_persistent_after_reboot.md."
                    )
                    assert pre_fan[HealthConsts.Component.LAST_HEALTHY] != "", (
                        f"Pre-reboot: last-unhealthy is empty for {simulated_fan_name} despite "
                        f"unhealthy-count being set."
                    )

        # NVL7 CPO trunk-port PHY recovery: set a policy + a random numeric leaf on a
        # trunk port BEFORE save, then verify both are preserved after reboot.
        # Init OUTSIDE the try so the finally cleanup can always reference these.
        nvl7_phy_recovery_port = None
        nvl7_phy_recovery_expected = None
        nvl7_unset_result = None

        try:
            # NVL7 CPO set is INSIDE the try (before save) so the finally cleanup is guaranteed even
            # if Save config / a later step raises. Feature is supported only on CPO-capable NVL7.
            if is_cpo_capable(devices.dut):
                from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import PhyRecoveryConsts
                from ngts.tests_nvos.interfaces.nvl_port.phy_recovery_helpers import (
                    nvl7_stage_and_apply_leaves,
                    nvl7_verify_new_leaves,
                    nvl7_wait_trunk_up,
                    select_nvl7_trunk_fae_port,
                )
                with allure.step("NVL7 CPO: enable phy-recovery + set a numeric leaf on a trunk port (pre-save)"):
                    nvl7_phy_recovery_port = select_nvl7_trunk_fae_port()
                    if nvl7_phy_recovery_port is not None:
                        # A distinctive non-default value pair (policy enum + non-zero timer) so the
                        # "preserved after reboot" check is meaningful. Show echoes configured values literally.
                        nvl7_phy_recovery_expected = {
                            PhyRecoveryConsts.RECOVERY_POLICY_CONFIG:
                                PhyRecoveryConsts.RecoveryPolicyConfig.EXPLICIT_CONFIG_CONTROLS,
                            PhyRecoveryConsts.RECOVERY_TX_TOGGLE_TIME:
                                random.randint(1, PhyRecoveryConsts.NVL7_TIMER_MAX),
                        }
                        nvl7_stage_and_apply_leaves(nvl7_phy_recovery_port, nvl7_phy_recovery_expected.items())
                        # Bring the trunk back up right away (the apply admin-DOWNs it and it
                        # stays down) so the pre-reboot steps don't run with a degraded sw* link.
                        nvl7_wait_trunk_up(nvl7_phy_recovery_port.port.name)
                    else:
                        logger.info("NVL7 save-reboot: no trunk (sw*) port found - skipping phy-recovery step")

            with allure.step('Save config'):
                TestToolkit.GeneralApi[TestToolkit.tested_api].save_config(engines.dut)

            with allure.step('Run set system dns server ipv6 command and apply config'):
                system.dns.set(op_param_name=SystemConsts.DNS_SERVER, op_param_value=SystemConsts.DNS_SERVER_IDS["ipv6"],
                               apply=True, dut_engine=engines.dut, ask_for_confirmation='y').verify_result()

            eth0_port = Port('eth0')
            new_eth0_description = 'eth0_test_desc'
            trigger_id = FastRecoveryConsts.TRIGGER_CREDIT_WATCHDOG

            with allure.step(
                    'set eth0 description to be {description} - with apply'.format(description=new_eth0_description)):
                eth0_port.interface.set(NvosConst.DESCRIPTION, new_eth0_description, apply=True).verify_result()

            # TODO: Fix fae recovery
            '''with allure_step('Run set fae fast-recovery trigger trigger-id event command and apply config'):
                fae.fast_recovery.trigger.set(trigger_id + ' ' + FastRecoveryConsts.TRIGGER_EVENT,
                                              FastRecoveryConsts.SEVERITY_WARNING, apply=True,
                                              dut_engine=engines.dut).verify_result()'''

            with allure.step('Run set system contact contact_info_2 command and apply config'):
                system.set(op_param_name=SystemConsts.CONTACT, op_param_value="contact_info_2", apply=True,
                           dut_engine=engines.dut).verify_result()

            with allure.step('Run set system location location_info_2 command and apply config'):
                system.set(op_param_name=SystemConsts.LOCATION, op_param_value="location_info_2", apply=True,
                           dut_engine=engines.dut).verify_result()

            with allure.step('Run set system message post-login command and apply config'):
                system.message.set(op_param_name=SystemConsts.POST_LOGIN_MESSAGE,
                                   op_param_value=f'"Post login test msg"',
                                   apply=True, dut_engine=engines.dut).verify_result()

            if fec_capable:
                # Platform is FEC capable
                fec_mode_2 = LinkDetectionConsts.FEC_MODE_QUAD
                with allure.step("Set the fec mode to {} for the selected port {}".format(fec_mode_2, selected_port.name)):
                    selected_port.interface.link.set(op_param_name=LinkDetectionConsts.FEC_MODE,
                                                     op_param_value=fec_mode_2).verify_result()

            with allure.step('Run nv action reboot system'):
                system.action_reboot(send_user_confirmation='y').verify_result()

            with allure.step('verify the hostname is {hostname}'.format(hostname=new_hostname_value)):
                system_output = OutputParsingTool.parse_json_str_to_dictionary(system.show()).get_returned_value()
                ValidationTool.verify_field_value_in_output(system_output, SystemConsts.HOSTNAME,
                                                            new_hostname_value).verify_result()

            with allure.step('Verify that system events table-size config was saved'):
                output = OutputParsingTool.parse_json_str_to_dictionary(system.events.show()).get_returned_value()
                ValidationTool.verify_field_value_in_output(output, EventConsts.TABLE_SIZE, '1200').\
                    verify_result()

            with allure.step('Verify that the system event before the reboot is present post reboot as well'):
                output = OutputParsingTool.parse_json_str_to_dictionary(system.events.show(event_id)).get_returned_value()
                assert event_time == output.get("time-created"), 'Event {} removed from system events table post reboot'.format(event_time)

            # TODO: Fix fae recovery
            '''with allure_step('Verify fae fast-recovery state is Disabled'):
                fast_recovery_output = OutputParsingTool.parse_json_str_to_dictionary(
                    fae.fast_recovery.show()).get_returned_value()
                ValidationTool.verify_field_value_in_output(fast_recovery_output, FastRecoveryConsts.STATE,
                                                            FastRecoveryConsts.STATE_DISABLED).verify_result()'''

            with allure.step('Verify DNS server ipv4, configured before save, is in show system dns server output'):
                dns_output = OutputParsingTool.parse_json_str_to_dictionary(system.dns.show(SystemConsts.DNS_SERVER)). \
                    get_returned_value()
                assert SystemConsts.DNS_SERVER_IDS["ipv4"] in dns_output, \
                    "The configured DNS server {} is not present in show system dns". \
                    format(SystemConsts.DNS_SERVER_IDS["ipv4"])

            with allure.step("Validate dscp configuration with show commands after reboot"):
                output = OutputParsingTool.parse_dscp_value_from_acl(engines, acl_obj, acl_id, rule_id)
                ValidationTool.verify_field_value_in_output(output, AclConsts.DSCP, 1).verify_result()

            with allure.step('Verify DNS server ipv6, configured after save, is not in show system dns server output'):
                dns_output = OutputParsingTool.parse_json_str_to_dictionary(system.dns.show(SystemConsts.DNS_SERVER)). \
                    get_returned_value()
                assert SystemConsts.DNS_SERVER_IDS["ipv6"] not in dns_output, \
                    "The configured DNS server {} is unexpectedly present in show system dns".\
                    format(SystemConsts.DNS_SERVER_IDS["ipv6"])

            with allure.step('verify the eth0 description was not saved after reboot'):
                output_dictionary = OutputParsingTool.parse_show_interface_output_to_dictionary(
                    eth0_port.interface.show()).get_returned_value()
                assert IbInterfaceConsts.DESCRIPTION not in output_dictionary.keys() or \
                    output_dictionary[IbInterfaceConsts.DESCRIPTION] != new_eth0_description, \
                    "Description should not be saved after reboot"

            if not is_bug_active(4362872):
                with allure.step('Verify system contact is set to contact_info_1'):
                    ValidationTool.verify_field_value_in_output(system_output, SystemConsts.CONTACT, "contact_info_1").\
                        verify_result()

                with allure.step('Verify system location is set to location_info_1'):
                    ValidationTool.verify_field_value_in_output(system_output, SystemConsts.LOCATION, "location_info_1").\
                        verify_result()

            with allure.step('Verify system messages are as expected in show system message'):
                message_output = OutputParsingTool.parse_json_str_to_dictionary(
                    system.message.show()).get_returned_value()
                ValidationTool.verify_field_value_in_output(message_output, SystemConsts.PRE_LOGIN_MESSAGE,
                                                            "Pre login test msg").verify_result()
                ValidationTool.verify_field_value_in_output(message_output, SystemConsts.POST_LOGOUT_MESSAGE,
                                                            "Post logout test msg").verify_result()
                ValidationTool.verify_field_value_in_output(message_output, SystemConsts.POST_LOGIN_MESSAGE,
                                                            devices.dut.post_login_message).verify_result()

            with allure.step("verify dscp option is loaded back after reboot"):
                dscp_output = OutputParsingTool.parse_json_str_to_dictionary(rule_id_obj.action.show()).\
                    get_returned_value()
                assert dscp_output['set']['dscp'] == 1, \
                    "The configured dscp is not present after reboot"

            with allure.step("Validate health component unhealthy counters and timestamps are retained post reboot if setup with fans"):
                if 'hw-management-tc.service' in devices.dut.available_services:
                    health = OutputParsingTool.parse_json_str_to_dictionary(
                        system.health.component.show()).get_returned_value()
                    fan_instance = health[HealthConsts.Component.FAN][HealthConsts.Component.INSTANCE][simulated_fan_name]
                    fan_unhealthy_count = int(fan_instance[HealthConsts.Component.UNHEALTHY_COUNT])
                    fan_last_unhealthy = fan_instance[HealthConsts.Component.LAST_HEALTHY]
                    assert fan_unhealthy_count == 1, "Fan unhealthy counter is not retained"
                    assert fan_last_unhealthy != "", "Fan last-unhealthy time is not retained"
                else:
                    logger.info("No fan available, skip")

            if nmx_log_stream_test:
                with allure.step("Verify cluster is enabled"):
                    output = OutputParsingTool.parse_show_output_to_dict(cluster.show()).get_returned_value()
                    assert output[SystemConsts.STATE] == 'enabled', "Cluster was disabled instead of enabled"

                with allure.step("Validate NMX-Controller log stream configuration is {}".format(stream)):
                    output = cluster.apps.app_name[ClusterConsts.NMX_CONTROLLER].logstream.show(exempted_err_msgs="Error")
                    output_dict = OutputParsingTool.parse_json_str_to_dictionary(output).get_returned_value()
                    assert output_dict["protocol"] == ClusterConsts.PROTOCOL_RSYSLOG, \
                        "Protocol is {} instead of {}".format(output_dict["protocol"], ClusterConsts.PROTOCOL_RSYSLOG)
                    assert output_dict["remote-url"] == url_show, \
                        "Remote-url is {} instead of {}".format(output_dict["remote-url"], url_show)

            if fec_capable:
                # Platform is FEC capable
                fec_mode = LinkDetectionConsts.FEC_MODE_DOUBLE
                with allure.step("Verify fec mode is {} which was configured before config save".format(fec_mode)):
                    link_dict = OutputParsingTool.parse_json_str_to_dictionary(selected_port.interface.link.show()). \
                        get_returned_value()
                    ValidationTool.verify_field_value_in_output(link_dict, LinkDetectionConsts.FEC_MODE,
                                                                fec_mode).verify_result()

            if nvl7_phy_recovery_port is not None:
                with allure.step("NVL7 CPO: verify phy-recovery config is preserved after reboot"):
                    nvl7_verify_new_leaves(nvl7_phy_recovery_port, nvl7_phy_recovery_expected)

        finally:
            if nvl7_phy_recovery_port is not None:
                with allure.step('Cleanup - unset NVL7 phy-recovery config and wait for trunk up'):
                    try:
                        # CAPTURE the ResultObj (a failed unset won't raise, so except can't catch
                        # it); it is verified AFTER the remaining cleanup ran (see below) so a
                        # failed unset is loud but does not abort hostname-unset/save/reboot.
                        nvl7_unset_result = nvl7_phy_recovery_port.port.interface.link.phy_recovery.unset(
                            apply=True, ask_for_confirmation=True)
                        if not nvl7_unset_result.result:
                            # retry ONCE before the cleanup save below - otherwise the save would
                            # persist the leftover phy-recovery config into the startup baseline.
                            logger.warning("NVL7 phy-recovery unset failed (%s) - retrying before the "
                                           "cleanup save", nvl7_unset_result.info)
                            nvl7_unset_result.ignore_result()
                            nvl7_unset_result = nvl7_phy_recovery_port.port.interface.link.phy_recovery.unset(
                                apply=True, ask_for_confirmation=True)
                        # don't leave the trunk port down for subsequent tests
                        nvl7_wait_trunk_up(nvl7_phy_recovery_port.port.name)
                    except Exception as nvl7_cleanup_err:
                        logger.warning("NVL7 phy-recovery cleanup failed: %s", nvl7_cleanup_err)

            with allure.step('Cleanup - Clear system health component unhealthy information'):
                system.health.component.action(ActionConsts.CLEAR)

            with allure.step('Cleanup - Run unset system DNS server and apply config'):
                system.dns.unset(SystemConsts.DNS_SERVER, apply=True, ask_for_confirmation=True, dut_engine=engines.dut).verify_result()

            with allure.step('Cleanup - set hostname to be {hostname} - with apply'.format(hostname=old_hostname)):
                system.unset(SystemConsts.HOSTNAME, apply=True, ask_for_confirmation=True)
                TestToolkit.GeneralApi[TestToolkit.tested_api].save_config(engines.dut)
                with allure.step('Run nv action reboot system'):
                    system.reboot.action_reboot()

            if nvl7_unset_result is not None:
                with allure.step('Verify the NVL7 phy-recovery cleanup unset succeeded'):
                    # verified LAST so a failed unset cannot abort the cleanup above. The save
                    # above still runs either way - skipping it would resurrect the WHOLE
                    # pre-cleanup test config (hostname/dns/phy-recovery) from the mid-test save
                    # at reboot, which is strictly worse. After the pre-save retry, only a
                    # double-failed unset can reach here failed - and it fails the test loudly.
                    nvl7_unset_result.verify_result()


@pytest.mark.cumulus
@pytest.mark.simx
@pytest.mark.general
@pytest.mark.parametrize('test_api', [random.choice(ApiType.ALL_TYPES)])
def test_general_auto_save(engines, devices, test_api):
    system = System()
    eth0_port = Port('eth0')
    new_eth0_description = 'TestingAutoSave'

    try:
        with allure.step('verify auto-save state is disabled'):
            output = OutputParsingTool.parse_json_str_to_dictionary(system.config.auto_save.show()).verify_result()
            assert SystemConsts.AUTO_SAVE_STATE_DISABLED == output[
                SystemConsts.AUTO_SAVE_STATE], "state should be disabled"

        with allure.step('set auto-save state to enabled'):
            system.config.auto_save.set(op_param_name=SystemConsts.AUTO_SAVE_STATE,
                                        op_param_value=SystemConsts.AUTO_SAVE_STATE_ENABLED).verify_result()

        with allure.step(
                'set eth0 description to be {description} - with apply'.format(description=new_eth0_description)):
            eth0_port.interface.set(NvosConst.DESCRIPTION, new_eth0_description, apply=True).verify_result()

        with allure.step("Verify description is set"):
            time.sleep(1)
            output = TestToolkit.GeneralApi[test_api].show_config(engine=engines.dut, revision='startup')
            assert new_eth0_description in output, "Expected to have new description field after set command, but we do not have it."

    finally:

        with allure.step("Unset description and verify"):
            eth0_port.interface.unset(op_param='description').verify_result()

        with allure.step('unset auto-save state'):
            system.config.auto_save.unset(op_param=SystemConsts.AUTO_SAVE_STATE, apply=True).verify_result()

        with allure.step('verify auto-save state is disabled'):
            output = OutputParsingTool.parse_json_str_to_dictionary(system.config.auto_save.show()).verify_result()
            assert SystemConsts.AUTO_SAVE_STATE_DISABLED == output[
                SystemConsts.AUTO_SAVE_STATE], "state should be disabled"

            TestToolkit.GeneralApi[test_api].save_config(engine=engines.dut)
