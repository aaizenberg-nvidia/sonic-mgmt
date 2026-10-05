import logging
import time
from dotted_dict import DottedDict

from ngts.tools.test_utils import allure_utils as allure
from ngts.tests_nvos.ib_router.constants import IbRouterConsts
from ngts.nvos_constants.constants_nvos import SystemConsts
from ngts.nvos_tools.system.System import System
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import IbInterfaceConsts
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.ib.opensm.OpenSmTool import OpenSmTool
from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli

logger = logging.getLogger()

PORT_CONFIG_APPLY_TIME = 20
TEMP_FOLDER = "/tmp/"
GID_UPDATE_TIME = 80


class IbRouterTool:
    @staticmethod
    def get_swid_name(swid_idx):
        """
          helper function that convert SWID index 0 - MAX_SWID_NUM to its name -
          0  - infiniband-default
          1 - 8 - infiniband-swid_idx
        """
        return IbRouterConsts.DEFAULT_SWID_NAME if swid_idx == 0 else IbRouterConsts.NON_DEFAULT_SWID_NAME.format(str(swid_idx))

    @staticmethod
    def enable_ib_router_profile(number_of_swids=IbRouterConsts.SWID_NUM):
        """
        helper function that will enable IB router profile in case it is disabled or has the incorrect number of swids
        """
        with allure.step(f"changing profile to ib router with {number_of_swids} SWIDs"):
            system = System(None)
            system_profile_output = OutputParsingTool.parse_json_str_to_dictionary(system.profile.show()).get_returned_value()
            current_system_profile = system_profile_output[SystemConsts.PROFILE_IB_ROUTING]
            current_swid_count = system_profile_output[SystemConsts.PROFILE_NUMBER_OF_SWIDS]
            logger.info(f"current setup router profile - {current_system_profile}\n, {current_swid_count} SWIDs")
            if current_system_profile == SystemConsts.PROFILE_STATE_DISABLED or current_swid_count != number_of_swids:
                params = {SystemConsts.PROFILE_IB_ROUTING: SystemConsts.PROFILE_STATE_ENABLED,
                          SystemConsts.PROFILE_NUMBER_OF_SWIDS: number_of_swids}
                system.profile.action_profile_change(params_dict=params)
                logger.info(f"sleeping a {GID_UPDATE_TIME} seconds as WA for ticket https://nvbugs/6048600 and to let GID update in the show commands")
                time.sleep(GID_UPDATE_TIME)
            else:
                logger.info("Setup already in correct config, will not change anything")

    @staticmethod
    def disable_ib_router_profile():
        """
        helper function that will disable IB router profile in case it is enabled
        """
        with allure.step(f"disabling ib router profile"):
            system = System(None)
            system_profile_output = OutputParsingTool.parse_json_str_to_dictionary(system.profile.show()).get_returned_value()
            current_system_profile = system_profile_output[SystemConsts.PROFILE_IB_ROUTING]
            logger.info(f"current setup router profile - {current_system_profile}")
            if current_system_profile == SystemConsts.PROFILE_STATE_ENABLED:
                params = {SystemConsts.PROFILE_IB_ROUTING: SystemConsts.PROFILE_STATE_DISABLED}
                system.profile.action_profile_change(params_dict=params)
            else:
                logger.info("Setup already has ib router disabled, will not change anything")

    @staticmethod
    def configure_leaf_port_mapping(engines):
        """
        helper function that will configure leaf ports according to the consts file
        """
        with allure.step(f"configuring port mapping"):
            for idx, port_list in IbRouterConsts.SWID_TO_PORTS_DICT.items():
                with allure.step(f"mapping ports {port_list} to SWID{idx}"):
                    swid_name = IbRouterTool.get_swid_name(idx)
                    for port_name in port_list:
                        port_obj = Port(port_name)
                        port_obj.interface.link.set(op_param_name=IbInterfaceConsts.LINK_IB_SUBNET, op_param_value=swid_name, apply=False).verify_result()
                    TestToolkit.GeneralApi[TestToolkit.tested_api].apply_config(TestToolkit.engines.dut, True)
            time.sleep(PORT_CONFIG_APPLY_TIME)
            NvueGeneralCli.save_config(engines.dut)

    @staticmethod
    def verify_leaf_port_mapping(expect_disabled=False):
        """
        helper function that will verify leaf ports are mapped correctly according to the consts file
        """
        with allure.step(f"verifying port mapping"):
            ports_dict = OutputParsingTool.parse_show_all_interfaces_output_to_dictionary(
                Port.show_interface()).verify_result()
            for idx, port_list in IbRouterConsts.SWID_TO_PORTS_DICT.items():
                idx = 0 if expect_disabled else idx
                with allure.step(f"making sure the ports {port_list} now mapped to SWID{idx}"):
                    swid_name = IbRouterTool.get_swid_name(idx)
                    for port_name in port_list:
                        port_swid = ports_dict[port_name][IbInterfaceConsts.LINK_IB_SUBNET]
                        assert port_swid == swid_name, f"port {port_name} does not belong to SWID {swid_name}, it found to be member of unexpected SWID {port_swid}"

    @staticmethod
    def verify_profile_status(expected_profile_status, expected_swid_number):
        """
          helper function that verify the ib router profile state and SWID count on the system
          @param expected_profile_status: the expected ib-router status - SystemConsts.PROFILE_STATE_DISABLED or SystemConsts.PROFILE_STATE_ENABLED
          @param expected_swid_number: the number of expected SWIDs, int or
        """
        system = System(None)
        system_profile_output = OutputParsingTool.parse_json_str_to_dictionary(system.profile.show()) \
            .get_returned_value()
        assert system_profile_output[
            SystemConsts.PROFILE_IB_ROUTING] == expected_profile_status, f"FAILED - after enabling, ib-routing field is {system_profile_output[SystemConsts.PROFILE_IB_ROUTING]}," \
            f" its expected to be {expected_profile_status}"
        assert system_profile_output[
            SystemConsts.PROFILE_NUMBER_OF_SWIDS] == expected_swid_number, f"FAILED - after enabling, num-of-swids field is {system_profile_output[SystemConsts.PROFILE_NUMBER_OF_SWIDS]}" \
            f"it is expected to be {expected_swid_number}"

    @staticmethod
    def disable_croc_fnm_ports(engines):
        """
        log to each crocodile switch in the setup and disable the FNM ports with the commands:
                sonic-db-cli CONFIG_DB hset "IB_PORT|Infiniband288" "admin_status" "down"
                sonic-db-cli CONFIG_DB hset "IB_PORT|Infiniband290" "admin_status" "down"
                sonic-db-cli CONFIG_DB hset "IB_PORT|Infiniband292" "admin_status" "down"
        """
        with allure.step(f"disable FNM ports on croc switches - {IbRouterConsts.CROC_SWITCHES_NICKNAMES}"):
            for croc_nickname in IbRouterConsts.CROC_SWITCHES_NICKNAMES:
                croc_engine = engines[croc_nickname]
                for fnm_command in IbRouterConsts.FNM_SHUTDOWN_COMMANDS:
                    croc_engine.run_cmd(fnm_command)
                croc_engine.run_cmd("nv config save")

    @staticmethod
    def init_extra_host_engines(topology_obj, engines):
        """
        init engine objects to extended amount of host nicknames - ha, hb, hc and so on
        """
        host_engines_data = DottedDict()
        # ha, hb, hc and so on are the traffic dockers in XDR router setup
        for host_nickname in IbRouterConsts.ALL_HOSTS_NICKNAMES:
            if host_nickname in topology_obj.players:
                host_engines_data[host_nickname] = topology_obj.players[host_nickname]['engine']
                host_engines_data[host_nickname + '_attr'] = topology_obj.players[host_nickname]['attributes']
        TestToolkit.engines.update(host_engines_data)

    @staticmethod
    def stop_sm_on_hosts(engines):
        """
        helper function that will stop every openSM process on hosts ALL_HOSTS_NICKNAMES
        """
        with allure.step(f"stopping SM on ib router hosts"):
            result = OpenSmTool.stop_open_sm_on_non_fnm_hosts(engines, IbRouterConsts.ALL_HOSTS_NICKNAMES)
            if not result.result:
                logging.warning("Failed to stop openSM")

    @staticmethod
    def start_sm_on_hosts(engines, skip_copy_files=False):
        """
        move openSM related file to SM_HOSTS_NICKNAMES and start openSM
        """
        with allure.step(f"enabling SMI interface"):
            for host_nickname in IbRouterConsts.ALL_HOSTS_NICKNAMES:
                host_engine = engines[host_nickname]
                smi_interface_up_cmd = '/opt/mellanox/iproute2/sbin/rdma dev add smi0 type SMI parent mlx5_0'
                host_engine.run_cmd(smi_interface_up_cmd)

        if not skip_copy_files:
            with allure.step(f"copying SM config files to SM hosts"):
                for sm_host_nickname in IbRouterConsts.SM_HOSTS_NICKNAMES:
                    sm_conf_file_name = IbRouterConsts.OPENSM_CONF_FILE_NAME.format(sm_host_nickname)
                    sm_conf_file_path = IbRouterConsts.OPENSM_CONF_PATH + sm_conf_file_name
                    sm_root_guid_file_name = IbRouterConsts.OPENSM_ROOT_GUID_FILE_NAME.format(sm_host_nickname)
                    sm_root_guid_file_path = IbRouterConsts.OPENSM_CONF_PATH + sm_root_guid_file_name
                    sm_engine = engines[sm_host_nickname]
                    for file_path in [sm_conf_file_path, sm_root_guid_file_path]:
                        logger.info(f"copying file {file_path} to host {sm_host_nickname} - {sm_engine.ip}")
                        sm_engine.run_cmd(f"sudo cp {file_path} {TEMP_FOLDER}")

        with allure.step(f"activating openSM on each SM host"):
            for sm_host_nickname in IbRouterConsts.SM_HOSTS_NICKNAMES:
                sm_conf_file_path = TEMP_FOLDER + IbRouterConsts.OPENSM_CONF_FILE_NAME.format(sm_host_nickname)
                sm_engine = engines[sm_host_nickname]
                sm_command = f"opensm -F {sm_conf_file_path} -B"
                logger.info(f"running the command {sm_command} on host {sm_host_nickname} - {sm_engine.ip}")
                sm_engine.run_cmd(sm_command)
                time.sleep(10)
                is_running = OpenSmTool.verify_open_sm_is_running_on_server(engines, sm_host_nickname)
                assert is_running, "failed to start SM on"

    @staticmethod
    def reset_router_config_file(engines):
        """
        this function replace the file OPENSM_CONF_PATH/ROUTER_POLICY_FILE_NAME with ROUTER_POLICY_MASTER_FILE_NAME
        one or more tests might touch it
        """
        with allure.step(f"restoring router config file"):
            sm_engine = engines[IbRouterConsts.SM_HOSTS_NICKNAMES[0]]
            sm_engine.run_cmd(f"sudo cp {IbRouterConsts.OPENSM_CONF_PATH}/{IbRouterConsts.ROUTER_POLICY_MASTER_FILE_NAME} {IbRouterConsts.OPENSM_CONF_PATH}/{IbRouterConsts.ROUTER_POLICY_FILE_NAME}")

    @staticmethod
    def set_router_setup(topology_obj, engines, config_sm=True):
        """
          helper function that will configure the ib router setup with the following steps:
          - stop openSM on setup
          - disable fnm ports on croc machine
          - enable ib router profile
          - map and verify leaf ports
          - start openSM if @config_sm is true

        """
        with allure.step(f"setting up ib router profile"):
            IbRouterTool.init_extra_host_engines(topology_obj, engines)
            IbRouterTool.stop_sm_on_hosts(engines)
            IbRouterTool.disable_croc_fnm_ports(engines)
            IbRouterTool.enable_ib_router_profile()
            IbRouterTool.configure_leaf_port_mapping(engines)
            if config_sm:
                IbRouterTool.start_sm_on_hosts(engines)
