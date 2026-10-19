import logging
from ngts.cli_wrappers.nvue.nvue_base_clis import NvueBaseCli, check_output
from ngts.nvos_constants.constants_nvos import ActionType
from ngts.nvos_constants.constants_nvos import ImageConsts
from ngts.tests_nvos.cluster.cluster_consts import ClusterConsts

logger = logging.getLogger()
server_ip = "10.237.116.60"


class NvueClusterCli(NvueBaseCli):

    """
    This class is for general cli commands for NVOS only
    """

    def __init__(self):
        self.cli_name = "Cluster"

    @staticmethod
    def action_start_cluster_app(engine, path):
        return NvueClusterCli.action_deprecated(engine, action_type=ActionType.START.replace('@', ''), resource_path=path)

    @staticmethod
    def action_stop_cluster_app(engine, path):
        return NvueClusterCli.action_deprecated(engine, action_type=ActionType.STOP.replace('@', ''), resource_path=path)

    @staticmethod
    def action_update_cluster_log_level(engine, path, level=''):
        return NvueClusterCli.action_deprecated(engine, action_type=ActionType.UPDATE.replace('@', ''), resource_path=path, param_value=level)

    @staticmethod
    def action_update_cluster_log_stream(engine, path, stream=''):
        # readline quoted-insert, so '?' reaches nv untouched over the interactive ssh session
        stream = (stream or '').replace('?', '\x16?')
        return NvueClusterCli.action_deprecated(engine, action_type=ActionType.UPDATE.replace('@', ''), resource_path=path, param_value=stream)

    @staticmethod
    def action_update_cluster_chassis_id(engine, path, mapping_id=''):
        param_value = "chassis-id " + str(mapping_id)
        return NvueClusterCli.action_deprecated(engine, action_type=ActionType.UPDATE.replace('@', ''), resource_path=path, param_value=param_value)

    @staticmethod
    def action_update(engine, path, param_name='', param_value=''):
        return NvueClusterCli.action_deprecated(engine, action_type=ActionType.UPDATE.replace('@', ''), resource_path=path, param_name=param_name, param_value=param_value)

    @staticmethod
    def action_restore_cluster(engine, path, param_name='', param_value=''):
        return NvueClusterCli.action_deprecated(engine, action_type=ActionType.RESTORE.replace('@', ''), resource_path=path, param_name=param_name, param_value=param_value)

    @staticmethod
    def action_restore_cluster_log_stream(engine, path, param_name='', param_value=''):
        return NvueClusterCli.action_deprecated(engine, action_type=ActionType.RESTORE.replace('@', ''), resource_path=path, param_name=param_name, param_value=param_value)

    @staticmethod
    @check_output
    def action_generate(engine, resource_path):
        return NvueClusterCli.action_deprecated(engine, action_type=ActionType.GENERATE.replace('@', ''), resource_path=resource_path)

    @staticmethod
    @check_output
    def action_delete(engine, resource_path):
        return NvueClusterCli.action_deprecated(engine, action_type=ActionType.DELETE.replace('@', ''), resource_path=resource_path)

    @staticmethod
    def action_fetch(engine, resource_path, path):
        remote_url = ImageConsts.SCP_PATH + path
        return NvueClusterCli.action_deprecated(engine, action_type=ActionType.FETCH.replace('@', ''), resource_path=resource_path, param_value=remote_url)

    @staticmethod
    def action_install(engine, resource_path, file):
        return NvueClusterCli.action_deprecated(engine, action_type=ActionType.INSTALL.replace('@', ''), resource_path=resource_path, param_value=file)

    @staticmethod
    def action_install_fae(engine, resource_path):
        return NvueClusterCli.action_deprecated(engine, action_type=ActionType.INSTALL.replace('@', ''), resource_path=resource_path)

    @staticmethod
    def action_uninstall_fae(engine, resource_path):
        return NvueClusterCli.action_deprecated(engine, action_type=ActionType.UNINSTALL.replace('@', ''), resource_path=resource_path)

    @staticmethod
    def action_create_partition(engine, resource_path, name, resiliency_mode, mcast_limit, uuid='', location=''):
        cmd = f"nv action create {resource_path.replace('/', ' ')} name {name} resiliency-mode {resiliency_mode} mcast-limit {mcast_limit}"
        if uuid != '':
            cmd += f' uuid {uuid}'
        if location != '':
            cmd += f" location {location}"
        logging.info("Running action cmd: '{cmd}' on dut using NVUE".format(cmd=cmd))
        return engine.run_cmd(cmd)

    @staticmethod
    def action_restore_partition(engine, resource_path, reroute_param=''):
        cmd = f"nv action restore {resource_path.replace('/', ' ')}"
        if reroute_param != '':
            cmd += f" {reroute_param}"
        logging.info("Running action cmd: '{cmd}' on dut using NVUE".format(cmd=cmd))
        return engine.run_cmd(cmd)

    @staticmethod
    def action_update_partition(engine, resource_path, reroute_param=''):
        cmd = f"nv action update {resource_path.replace('/', ' ')}"
        if reroute_param != '':
            cmd += f" {reroute_param}"
        logging.info("Running action cmd: '{cmd}' on dut using NVUE".format(cmd=cmd))
        return engine.run_cmd(cmd)

    @staticmethod
    @check_output
    def action_update_cluster_manager_property(engine, resource_path, param_name='', param_val=''):
        path = resource_path.replace('/', ' ').strip()
        cmd = f'nv action update {path} {param_val}'.strip()
        logging.info(f"Running action cmd: '{cmd}' on dut using NVUE")
        return engine.run_cmd(cmd)

    @staticmethod
    @check_output
    def action_restore_cluster_manager_property(engine, resource_path):
        path = resource_path.replace('/', ' ').strip()
        cmd = f'nv action restore {path}'
        logging.info(f"Running action cmd: '{cmd}' on dut using NVUE")
        return engine.run_cmd(cmd)

    @staticmethod
    @check_output
    def action_rotate_cluster_manager_property(engine, resource_path):
        path = resource_path.replace('/', ' ').strip()
        cmd = f'nv action rotate {path}'
        logging.info(f"Running action cmd: '{cmd}' on dut using NVUE")
        return engine.run_cmd(cmd)

    @staticmethod
    @check_output
    def action_reset(engine, resource_path, param=''):
        path = resource_path.replace('/', ' ').strip()
        cmd = f"nv action reset {path} {param}"
        logging.info("Running '{cmd}' on dut using NVUE".format(cmd=cmd))
        return engine.run_cmd(cmd)

    @staticmethod
    def action_update_sdn_transceiver_maintenance_state(engine, path, maintenance_state=''):
        param_value = ClusterConsts.MAINTENANCE_STATE + " " + maintenance_state
        return NvueClusterCli.action_deprecated(engine, action_type=ActionType.UPDATE.replace('@', ''), resource_path=path, param_value=param_value)

    @staticmethod
    def action_restore_sdn_transceiver_maintenance_state(engine, path):
        return NvueClusterCli.action_deprecated(engine, action_type=ActionType.RESTORE.replace('@', ''), resource_path=path, param_name=ClusterConsts.MAINTENANCE_STATE)

    @staticmethod
    @check_output
    def action_import_rbac_file(engine, resource_path, remote_url):
        path = resource_path.replace('/', ' ').strip()
        cmd = f"nv action import {path} {remote_url}"
        logging.info(f"Running '{cmd}' on dut using NVUE")
        return engine.run_cmd(cmd)

    @staticmethod
    @check_output
    def action_delete_rbac_file(engine, resource_path):
        path = resource_path.replace('/', ' ').strip()
        cmd = f"nv action delete {path}"
        logging.info(f"Running '{cmd}' on dut using NVUE")
        return engine.run_cmd(cmd)

    @staticmethod
    def action_update_sdn_trays_maintenance_state(engine, path, tray_id='', maintenance_state=''):
        return NvueClusterCli.action(
            action_str=ActionType.UPDATE.replace('@', ''),
            resource_path=path,
            main_param=(ClusterConsts.MAINTENANCE_STATE, tray_id),
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
        return NvueClusterCli.action(
            action_str=ActionType.RESTORE.replace('@', ''),
            resource_path=path,
            main_param=(ClusterConsts.MAINTENANCE_STATE, tray_id),
            flags=ClusterConsts.MAINTENANCE_STATE,
            additional_params={},
            engine=engine,
            reboot_params=None,
            send_user_confirmation=None,
            expected_output='',
            device=None,
        )

    @staticmethod
    @check_output
    def show_file(engine, file='', exit_cmd=''):
        cmd = "nv show sdn cmd files {file}".format(file=file)
        logging.info("Running '{cmd}' on dut using NVUE".format(cmd=cmd))
        return engine.run_cmd_after_cmd([cmd, exit_cmd])

    @staticmethod
    def action_run_sdn_cmd(engine, resource_path, sdn_cmd_str):
        path = resource_path.replace('/', ' ').strip()
        cmd = f'nv action run {path} cmd "{sdn_cmd_str}"'
        logging.info("Running '%s' on dut using NVUE", cmd[:200] + ("..." if len(cmd) > 200 else ""))
        return engine.run_cmd(cmd)
