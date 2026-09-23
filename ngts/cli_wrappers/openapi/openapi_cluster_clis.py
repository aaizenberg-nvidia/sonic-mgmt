import logging

from ngts.cli_wrappers.openapi.openapi_base_clis import OpenApiBaseCli
from ngts.nvos_constants.constants_nvos import ActionType, ImageConsts
from .openapi_command_builder import OpenApiCommandHelper
from ngts.tests_nvos.cluster.cluster_consts import ClusterConsts

logger = logging.getLogger()


class OpenApiClusterCli(OpenApiBaseCli):

    def __init__(self):
        self.cli_name = "Cluster"

    @staticmethod
    def action_start_cluster_app(engine, resource_path):
        return OpenApiClusterCli.action_deprecated(engine, action_type=ActionType.START.replace('@', ''), resource_path=resource_path)

    @staticmethod
    def action_stop_cluster_app(engine, resource_path):
        return OpenApiClusterCli.action_deprecated(engine, action_type=ActionType.STOP.replace('@', ''), resource_path=resource_path)

    @staticmethod
    def action_update_cluster_log_level(engine, resource_path, level):
        param_name = "log_level"
        param_value = level
        return OpenApiClusterCli.action_deprecated(engine, action_type=ActionType.UPDATE.replace('@', ''), resource_path=resource_path, param_name=param_name, param_value=param_value)

    @staticmethod
    def action_update_cluster_log_stream(engine, resource_path, stream):
        param_name = ["protocol", "remote-url"]
        param_value = [value.replace('"', '') for value in stream.split(None, 1)]
        return OpenApiClusterCli.action_deprecated(engine, action_type=ActionType.UPDATE.replace('@', ''), resource_path=resource_path, param_name=param_name, param_value=param_value)

    @staticmethod
    def action_restore_cluster_log_stream(engine, resource_path, param_name='', param_value=''):
        return OpenApiClusterCli.action_deprecated(engine, action_type=ActionType.RESTORE.replace('@', ''), resource_path=resource_path, param_name=param_name, param_value=param_value)

    @staticmethod
    def action_update_cluster_chassis_id(engine, resource_path, mapping_id=''):
        return OpenApiClusterCli.action_deprecated(engine, action_type=ActionType.UPDATE.replace('@', ''), resource_path=resource_path, param_name="chassis-id", param_value=mapping_id)

    @staticmethod
    def action_update(engine, resource_path, param_name='', param_val=''):
        logging.info("Running action: 'generate' on dut using OpenApi")
        parameters = {} if not param_name and not param_val else {param_name: param_val}
        params = \
            {
                "state": "start",
                "parameters": parameters
            }

        return OpenApiCommandHelper.execute_action(ActionType.UPDATE, engine.engine.username, engine.engine.password,
                                                   engine.ip, engine.open_api_port, resource_path, params)

    @staticmethod
    def action_restore_cluster(engine, resource_path, param_name='', param_val=''):
        logging.info("Running action: 'restore' on dut using OpenApi")
        parameters = {} if not param_name and not param_val else {param_name: param_val}
        params = \
            {
                "state": "start",
                "parameters": parameters
            }

        return OpenApiCommandHelper.execute_action(ActionType.RESTORE, engine.engine.username, engine.engine.password,
                                                   engine.ip, engine.open_api_port, resource_path, params)

    @staticmethod
    def action_generate(engine, resource_path):
        return OpenApiClusterCli.action_deprecated(engine, action_type=ActionType.GENERATE.replace('@', ''), resource_path=resource_path)

    @staticmethod
    def action_delete(engine, resource_path):
        return OpenApiClusterCli.action_deprecated(engine, action_type=ActionType.DELETE.replace('@', ''), resource_path=resource_path)

    @staticmethod
    def action_install(engine, resource_path, file):
        param_name = "files"
        return OpenApiClusterCli.action_deprecated(engine, action_type=ActionType.INSTALL.replace('@', ''), resource_path=resource_path, param_name=param_name, param_value=file)

    @staticmethod
    def action_install_fae(engine, resource_path=''):
        """
        """
        return OpenApiClusterCli.action_deprecated(engine, action_type=ActionType.INSTALL.replace('@', ''), resource_path=resource_path)

    @staticmethod
    def action_uninstall_fae(engine, resource_path):
        return OpenApiClusterCli.action_deprecated(engine, action_type=ActionType.UNINSTALL.replace('@', ''), resource_path=resource_path)

    @staticmethod
    def action_create_partition(engine, resource_path, name, resiliency_mode, mcast_limit, uuid='', location=''):
        logging.info("Running action: 'create' on dut using OpenApi")
        params = {
            "state": "start",
            "parameters": {
                "name": name,
                "resiliency-mode": resiliency_mode,
                "mcast-limit": mcast_limit,
            }
        }

        # Add optional parameters if provided
        if uuid:
            params["parameters"]["uuid"] = uuid
        if location:
            params["parameters"]["location"] = location

        return OpenApiCommandHelper.execute_action(ActionType.CREATE, engine.engine.username, engine.engine.password,
                                                   engine.ip, engine.open_api_port, resource_path, params)

    @staticmethod
    def action_restore_partition(engine, resource_path, reroute_param=''):
        logging.info("Running action: 'restore' on dut using OpenApi")
        params = {
            "state": "start",
            "parameters": {
            }
        }

        # Add optional parameters if provided
        if reroute_param:
            params["parameters"][reroute_param] = True

        return OpenApiCommandHelper.execute_action(ActionType.RESTORE, engine.engine.username, engine.engine.password,
                                                   engine.ip, engine.open_api_port, resource_path, params)

    @staticmethod
    def action_update_partition(engine, resource_path, reroute_param=''):
        logging.info("Running action: 'create' on dut using OpenApi")
        params = {
            "state": "start",
            "parameters": {
            }
        }
        reroute_param = reroute_param.replace('-', '_')
        # Add optional parameters if provided
        if reroute_param:
            params["parameters"][reroute_param] = True

        return OpenApiCommandHelper.execute_action(ActionType.UPDATE, engine.engine.username, engine.engine.password,
                                                   engine.ip, engine.open_api_port, resource_path, params)

    @staticmethod
    def action_update_cluster_manager_property(engine, resource_path, param_name='', param_val=''):
        logging.info(f'Run action import on: {resource_path} using OpenApi')
        parameters = {} if not param_name and not param_val else {param_name: param_val}
        params = \
            {
                "state": "start",
                "parameters": parameters
            }

        return OpenApiCommandHelper.execute_action(ActionType.UPDATE, engine.engine.username, engine.engine.password,
                                                   engine.ip, engine.open_api_port, resource_path, params)

    @staticmethod
    def action_restore_cluster_manager_property(engine, resource_path):
        logging.info(f'Run action delete on: {resource_path} using OpenApi')
        params = \
            {
                "state": "start",
                "parameters": {}
            }

        return OpenApiCommandHelper.execute_action(ActionType.RESTORE, engine.engine.username, engine.engine.password,
                                                   engine.ip, engine.open_api_port, resource_path, params)

    @staticmethod
    def action_rotate_cluster_manager_property(engine, resource_path):
        logging.info(f'Run action rotate on: {resource_path} using OpenApi')
        params = \
            {
                "state": "start",
                "parameters": {}
            }
        return OpenApiCommandHelper.execute_action(ActionType.ROTATE, engine.engine.username, engine.engine.password,
                                                   engine.ip, engine.open_api_port, resource_path, params)

    @staticmethod
    def action_reset(engine, resource_path, param=''):
        logging.info("Running action: reset {} on dut using OpenApi".format(resource_path))

        params = \
            {
                "state": "start",
                "parameters": {
                    "force": True,
                }
            }

        return OpenApiCommandHelper.execute_action(ActionType.RESET, engine.engine.username, engine.engine.password,
                                                   engine.ip, engine.open_api_port, resource_path, params)

    @staticmethod
    def action_update_sdn_transceiver_maintenance_state(engine, path, maintenance_state=''):
        param_value = maintenance_state
        return OpenApiClusterCli.action_deprecated(engine, action_type=ActionType.UPDATE.replace('@', ''), resource_path=path, param_name=ClusterConsts.MAINTENANCE_STATE, param_value=param_value)

    @staticmethod
    def action_restore_sdn_transceiver_maintenance_state(engine, path):
        return OpenApiClusterCli.action_deprecated(engine, action_type=ActionType.RESTORE.replace('@', ''), resource_path=path, param_name=ClusterConsts.MAINTENANCE_STATE)

    @staticmethod
    def action_import_rbac_file(engine, resource_path, remote_url):
        logging.info(f"Running action: import rbac file {resource_path} on dut using OpenApi")
        parameters = {
            "url": remote_url
        }
        params = \
            {
                "state": "start",
                "parameters": parameters
            }

        return OpenApiCommandHelper.execute_action(ActionType.IMPORT, engine.engine.username, engine.engine.password,
                                                   engine.ip, engine.open_api_port, resource_path, params)

    @staticmethod
    def action_delete_rbac_file(engine, resource_path):
        logging.info(f"Running action: delete rbac file {resource_path} on dut using OpenApi")
        params = \
            {
                "state": "start",
                "parameters": {}
            }

        return OpenApiCommandHelper.execute_action(ActionType.DELETE, engine.engine.username, engine.engine.password,
                                                   engine.ip, engine.open_api_port, resource_path, params)

    @staticmethod
    def action_update_sdn_trays_maintenance_state(engine, path, tray_id='', maintenance_state=''):
        resource_path = path + '/' + tray_id if tray_id else path
        return OpenApiClusterCli.action(
            action_str=ActionType.UPDATE.replace('@', ''),
            resource_path=resource_path,
            main_param=None,
            flags='',
            additional_params={ClusterConsts.MAINTENANCE_STATE: maintenance_state},
            engine=engine,
            reboot_params=None,
            send_user_confirmation=None,
            expected_output='',
            device=None,
        )

    @staticmethod
    def action_restore_sdn_trays_maintenance_state(engine, path, tray_id=''):
        resource_path = path + '/' + tray_id if tray_id else path
        return OpenApiClusterCli.action(
            action_str=ActionType.RESTORE.replace('@', ''),
            resource_path=resource_path,
            main_param=None,
            flags=ClusterConsts.MAINTENANCE_STATE,
            additional_params={},
            engine=engine,
            reboot_params=None,
            send_user_confirmation=None,
            expected_output='',
            device=None,
        )

    @staticmethod
    def show_file(engine, file='', exit_cmd=''):
        # Not used simply return path to file
        return ""

    @staticmethod
    def action_run_sdn_cmd(engine, resource_path, sdn_cmd_str):
        params = {"state": "start", "parameters": {"cmd": sdn_cmd_str}}
        return OpenApiCommandHelper.execute_action(ActionType.RUN, engine.engine.username, engine.engine.password,
                                                   engine.ip, engine.open_api_port, resource_path, params)
