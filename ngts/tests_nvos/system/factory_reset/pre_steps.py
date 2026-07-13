from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from ngts.nvos_constants.constants_nvos import OutputFormat, LinkDetectionConsts
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import NvosConsts
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.nmx.Cluster import Cluster
from ngts.tests_nvos.cluster.cluster_tools import ClusterTools, disabled_access_ports
from ngts.tests_nvos.system.factory_reset.helpers import *
from ngts.tools.test_utils import allure_utils as allure
from ngts.nvos_tools.Devices.IbDevice import JulietSwitch
from ngts.nvos_tools.infra.ValidationTool import ValidationTool


@disabled_access_ports
def factory_reset_no_params_pre_steps(engines, platform_params, system, devices, has_loopbox, setup_name, standalone_system):
    port_type = devices.dut.port_type
    init_cluster_status = None

    with allure.step('Create System object'):
        machine_type = platform_params['filtered_platform']

    with allure.step('Get health status'):
        health_status = get_health_status(system)

    with allure.step(f'Set description to {port_type} ports'):
        description = "test_reset_factory_without_params"
        ports = Tools.RandomizationTool.select_random_ports(requested_ports_state=None, requested_ports_type=port_type,
                                                            num_of_ports_to_select=3).get_returned_value()
        apply_and_save_port = ports[0]
        just_apply_port = ports[1]
        not_apply_port = ports[2]

    with allure.step(f'Set and apply description to {port_type} port, save config after it'):
        apply_and_save_port.interface.set(NvosConst.DESCRIPTION, description, apply=True).verify_result()
        TestToolkit.GeneralApi[TestToolkit.tested_api].save_config(engines.dut)
        NvueGeneralCli.save_config(engines.dut)

    with allure.step(f'Set and apply description to {port_type} port'):
        just_apply_port.interface.set(NvosConst.DESCRIPTION, description, apply=True).verify_result()

    with allure.step(f'Set description to {port_type} port'):
        not_apply_port.interface.set(NvosConst.DESCRIPTION, description, apply=False).verify_result()

        if devices.dut.has_nmx:
            with allure.step('Juliet Device Check'):
                with allure.step("Config A reverse cluster state than configured"):
                    cluster = Cluster()
                    # Enable cluster and validate its enabled.
                    init_cluster_status = ClusterTools.check_cluster_state(cluster, output_format=OutputFormat.json)
                    ClusterTools.reverse_cluster_state(cluster, setup_name, output_format=OutputFormat.json)
        else:
            init_cluster_status = None

    with allure.step('Validate ports description'):
        validate_port_description(engines.dut, apply_and_save_port, description)
        validate_port_description(engines.dut, just_apply_port, description)
        validate_port_description(engines.dut, not_apply_port, "")

    with allure.step('Run set system contact command and apply config'):
        system.set(op_param_name=SystemConsts.CONTACT, op_param_value="contact_info", apply=True,
                   dut_engine=engines.dut).verify_result()

    with allure.step('Run set system location command and apply config'):
        system.set(op_param_name=SystemConsts.LOCATION, op_param_value="location_info", apply=True,
                   dut_engine=engines.dut).verify_result()

    with allure.step('Verify system contact is set'):
        system_output = OutputParsingTool.parse_json_str_to_dictionary(system.show()).get_returned_value()
        ValidationTool.verify_field_value_in_output(system_output, SystemConsts.CONTACT, "contact_info").\
            verify_result()

    with allure.step('Verify system location is set'):
        ValidationTool.verify_field_value_in_output(system_output, SystemConsts.LOCATION, "location_info").\
            verify_result()

    if devices.dut.check_fec_capability():
        tested_api = TestToolkit.tested_api
        TestToolkit.tested_api = ApiType.NVUE
        fec_mode = LinkDetectionConsts.FEC_MODE_QUAD

        with allure.step("Set the fec mode to {} for the selected port {}".format(fec_mode, apply_and_save_port.name)):
            apply_and_save_port.interface.link.set(op_param_name=LinkDetectionConsts.FEC_MODE, op_param_value=fec_mode,
                                                   apply=True, ask_for_confirmation=True).verify_result()

        with allure.step("Verify applied fec mode is set to {}".format(fec_mode)):
            link_output = apply_and_save_port.interface.link.show(output_format="auto")
            ValidationTool.verify_fec_config_in_auto_output(link_output, fec_mode)

        TestToolkit.tested_api = tested_api

    with allure.step("Add data before reset factory"):
        username = add_verification_data(engines.dut, system)

    with allure.step("Get current time"):
        update_timezone(system)
        current_time = get_current_time(engines)

    return apply_and_save_port, current_time, just_apply_port, health_status, machine_type, not_apply_port, username, init_cluster_status


def factory_reset_keep_basic_pre_steps(engines, system):
    with allure.step('Get health status'):
        health_status = get_health_status(system)

    with allure.step('Set description to eth0 port'):
        mgmt_port = Port('eth0')
        mgmt_port.interface.set(NvosConst.DESCRIPTION, 'nvosdescription', apply=True, ask_for_confirmation=True).verify_result()
        output_dictionary = Tools.OutputParsingTool.parse_show_interface_output_to_dictionary(
            mgmt_port.interface.show()).get_returned_value()

        Tools.ValidationTool.verify_field_value_in_output(output_dictionary=output_dictionary,
                                                          field_name=NvosConst.DESCRIPTION,
                                                          expected_value='nvosdescription')

    dns_server_id = SystemConsts.DNS_SERVER_IDS["ipv4"]
    with allure.step('Set DNS server and save config'):
        system.dns.set(op_param_name=SystemConsts.DNS_SERVER, op_param_value=dns_server_id,
                       apply=True, dut_engine=engines.dut, ask_for_confirmation=True).verify_result()
        TestToolkit.GeneralApi[TestToolkit.tested_api].save_config(engines.dut)

    with allure.step("Add data before reset factory"):
        username = add_verification_data(engines.dut, system)

    with allure.step("Get current time"):
        update_timezone(system)
        current_time = get_current_time(engines)

    return current_time, username, health_status, mgmt_port, output_dictionary, dns_server_id


def factory_reset_general_pre_steps(engines, devices, system):
    port_type = devices.dut.port_type

    with allure.step('Get health status'):
        health_status = get_health_status(system)

    with allure.step(f'Set description to {port_type} ports'):
        description = "with_keep_all_config_param"
        ports = Tools.RandomizationTool.select_random_ports(requested_ports_state=None, requested_ports_type=port_type,
                                                            num_of_ports_to_select=3).get_returned_value()
        apply_and_save_port = ports[0]
        just_apply_port = ports[1]
        not_apply_port = ports[2]

    with allure.step(f'Bring selected {port_type} interfaces up'):
        for port in (apply_and_save_port, just_apply_port, not_apply_port):
            port.interface.link.state.set(
                op_param_name=NvosConsts.LINK_STATE_UP, apply=True,
                ask_for_confirmation=True).verify_result()

    with allure.step(f'Set and apply description to {port_type} port, save config after it'):
        apply_and_save_port.interface.set(NvosConst.DESCRIPTION, description, apply=True, ask_for_confirmation=True).verify_result()
        TestToolkit.GeneralApi[TestToolkit.tested_api].save_config(engines.dut)
        NvueGeneralCli.save_config(engines.dut)

    with allure.step(f'Set and apply description to {port_type} port'):
        just_apply_port.interface.set(NvosConst.DESCRIPTION, description, apply=True, ask_for_confirmation=True).verify_result()

    with allure.step(f'Set description to {port_type} port'):
        not_apply_port.interface.set(NvosConst.DESCRIPTION, description, apply=False).verify_result()

    with allure.step('Validate ports description'):
        validate_port_description(engines.dut, apply_and_save_port, description)
        validate_port_description(engines.dut, just_apply_port, description)
        validate_port_description(engines.dut, not_apply_port, "")

    with allure.step("Add data before reset factory"):
        username = add_verification_data(engines.dut, system)

    with allure.step("Get current time"):
        update_timezone(system)
        current_time = get_current_time(engines)

    with allure.step("get all tech-support files and sizes"):
        engines.dut.run_cmd(f"sudo find {RemarkableLogsConsts.LOGS_PATH} -type f -exec du -h {{}} + | sort -h")

    return health_status, current_time, apply_and_save_port, description, just_apply_port, not_apply_port, username


def get_health_status(system):
    system_output = OutputParsingTool.parse_json_str_to_dictionary(system.health.show()).get_returned_value()
    return system_output[SystemConsts.STATUS]


def factory_reset_system_message_pre_steps(engines, devices, system):
    new_pre_login_msg = "Testing PRE LOGIN MESSAGE"
    new_post_login_msg = "Testing POST LOGIN MESSAGE"
    new_post_logout_msg = "Testing POST LOGOUT MESSAGE"
    with allure.step('Get health status'):
        health_status = get_health_status(system)

    with allure.step('Run set system message pre-login command and apply config'):
        system.message.set(op_param_name=SystemConsts.PRE_LOGIN_MESSAGE, op_param_value=f'"{new_pre_login_msg}"',
                           apply=True, dut_engine=engines.dut).verify_result()

    with allure.step('Run set system message post-login command and apply config'):
        system.message.set(op_param_name=SystemConsts.POST_LOGIN_MESSAGE, op_param_value=f'"{new_post_login_msg}"',
                           apply=True, dut_engine=engines.dut).verify_result()

    with allure.step('Run set system message post-logout command and apply config'):
        system.message.set(op_param_name=SystemConsts.POST_LOGOUT_MESSAGE, op_param_value=f'"{new_post_logout_msg}"',
                           apply=True, dut_engine=engines.dut).verify_result()

    with allure.step('Verify system messages are changed to new messages in show system'):
        message_output = OutputParsingTool.parse_json_str_to_dictionary(system.message.show()).get_returned_value()
        fields_to_verify = [SystemConsts.PRE_LOGIN_MESSAGE, SystemConsts.POST_LOGIN_MESSAGE, SystemConsts.POST_LOGOUT_MESSAGE]
        values_to_verify = [new_pre_login_msg, new_post_login_msg, new_post_logout_msg]
        ValidationTool.validate_fields_values_in_output(fields_to_verify, values_to_verify, message_output).verify_result()

    with allure.step("Add data before reset factory"):
        username = add_verification_data(engines.dut, system)

    with allure.step("Get current time"):
        update_timezone(system)
        current_time = get_current_time(engines)

    return health_status, current_time, username
