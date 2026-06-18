import logging
import json
import re
import os
import time
import allure
import netmiko
import glob
import subprocess
from ngts.cli_wrappers.interfaces.interface_general_clis import GeneralCliInterface
from ngts.cli_wrappers.sonic.sonic_onie_clis import SonicOnieCli
from ngts.constants.constants import InfraConst, SSHConsts, Sonic_Cache
from ngts.constants.performance_constants import PerfConsts
from ngts.helpers.run_process_on_host import run_process_on_host
from ngts.helpers.secure_boot_helper import SecureBootHelper
from ngts.helpers.system_helpers import copy_files_to_syncd
from devts.infra.tools.topology_tools.nogaq import get_noga_entire_resource_data
from devts.infra.tools.validations.traffic_validations.port_check.port_checker import check_port_status_till_alive
from devts.infra.tools.connection_tools.linux_ssh_engine import LinuxSshEngine
from devts.infra.tools.exceptions.test_issue import TestIssue
from ngts.cli_wrappers.common.sdk_clis_common import SdkCliCommon

logger = logging.getLogger()

DUMMY_COMMAND = 'echo dummy_command'


class GeneralCliCommon(GeneralCliInterface, SdkCliCommon):
    """
    This class hosts methods which are implemented identically for Linux and SONiC
    """

    def __init__(self, engine, cli_obj=None, dut_alias=None):
        SdkCliCommon.__init__(self, engine, cli_obj, dut_alias)

    def start_service(self, service):
        # lldpad is managed by supervisorctl
        if service == 'lldpad':
            output = self.engine.run_cmd('supervisorctl start {}'.format(service), validate=True)
        else:
            output = self.engine.run_cmd('sudo service {} start'.format(service), validate=True)
        return output

    def stop_service(self, service):
        # lldpad is managed by supervisorctl
        if service == 'lldpad':
            output = self.engine.run_cmd('supervisorctl stop {}'.format(service), validate=True)
        else:
            output = self.engine.run_cmd('sudo service {} stop'.format(service), validate=True)
        return output

    def systemctl_start(self, service):
        output = self.engine.run_cmd(f'sudo systemctl start {service}', validate=True)
        return output

    def systemctl_stop(self, service):
        output = self.engine.run_cmd(f'sudo systemctl stop {service}', validate=True)
        return output

    def systemctl_restart(self, service, daemon_reload=False):
        if not isinstance(service, str):
            service = ' '.join(service)
        if daemon_reload:
            self.engine.run_cmd("sudo systemctl daemon-reload", validate=True)
        output = self.engine.run_cmd(f'sudo systemctl restart {service}', validate=True)
        return output

    def systemctl_is_service_active(self, service):
        service_status = False
        output = self.engine.run_cmd(f'sudo systemctl is-active {service}', validate=True)
        if output == 'active':
            service_status = True
        return service_status

    def get_container_status(self, app_name):
        """
        Get specified container's status:
        Example:
            docker ps -a -f name=snmp$ --format "{'ID':'{{ .ID }}', 'Names':'{{ .Names }}', 'Status':'{{ .Status }}'}"
            {'ID':'bb2ef5fcd2b1', 'Names':'snmp', 'Status':'Up 3 hours'}
        :param app_name: ssh engine object
        :Return app container status, None if no container data
        """
        container_data_format = "{'ID':'{{ .ID }}', 'Names':'{{ .Names }}', 'Status':'{{ .Status }}'}"
        app_container_info = self.engine.run_cmd('docker ps -a -f name={}$ --format "{}" '.
                                                 format(app_name, container_data_format), validate=True)
        logger.info("get {} container status:{}".format(app_name, app_container_info))
        app_container_status = None
        if app_container_info:
            app_container_status = eval(app_container_info)["Status"]
        return app_container_status

    def get_running_containers_names(self):
        """
        Returns the names of running Docker containers in the system.
        """
        return self.engine.run_cmd("docker ps --format '{{.Names}}'").splitlines()

    def verify_container_running(self, container_name):
        """
        Verify a single container is running.

        :param container_name: Docker container name
        :raises Exception: If container is not running
        """
        try:
            self.engine.run_cmd(f'docker ps | grep {container_name}', validate=True)
        except BaseException as e:
            logger.error(f"✗ Container {container_name} is NOT running - Error: {e}")
            raise Exception(f"Container {container_name} is not running")

    def get_container_restart_counts(self, container_names):
        """Return {container: RestartCount} for each named container."""
        # RestartCount catches a crash-and-recover bounce that the `docker ps`
        # Status string ("Up <short time>" again) hides.
        counts = {}
        for name in container_names:
            out = self.engine.run_cmd(
                f"docker inspect {name} --format '{{{{.RestartCount}}}}'",
                validate=True,
            )
            text = (out or "").strip()
            if not text.lstrip("-").isdigit():
                raise ValueError(
                    f"docker inspect {name} returned non-numeric RestartCount: {out!r}"
                )
            counts[name] = int(text)
        missing = set(container_names) - set(counts)
        if missing:
            raise ValueError(
                f"docker inspect omitted monitored containers: {sorted(missing)!r}"
            )
        return counts

    def hostname(self, flags=''):
        return self.engine.run_cmd(f'hostname {flags}')

    def echo(self, string, flags=''):
        return self.engine.run_cmd(f'echo {flags} {string}')

    def ls(self, path, flags='', validate=False):
        return self.engine.run_cmd(f'ls {flags} {path}', validate=validate)

    def mv(self, src_path, dst_path, flags=''):
        return self.engine.run_cmd(f'mv {flags} {src_path} {dst_path}')

    def cp(self, src_path, dst_path, flags=''):
        return self.engine.run_cmd(f'cp {flags} {src_path} {dst_path}')

    def rm(self, path, flags=''):
        return self.engine.run_cmd(f'rm {flags} {path}')

    def mkdir(self, path, flags=''):
        return self.engine.run_cmd(f'mkdir {flags} {path}')

    def who(self, flags=''):
        return self.engine.run_cmd(f'who {flags}')

    def which(self, path):
        return self.engine.run_cmd(f'which {path}')

    def sed(self, path, script, flags=''):
        return self.engine.run_cmd(f"sed {flags} '{script}' {path}")

    def chmod_by_mode(self, path, mode, flags=''):
        return self.engine.run_cmd(f'chmod {flags} {mode} {path}')

    def chmod_by_ref_file(self, path, ref_path, flags=''):
        return self.engine.run_cmd(f'chmod {flags} --reference={ref_path} {path}')

    def chown_by_user(self, path, user, group='', flags=''):
        if group:
            group = f':{group}'
        return self.engine.run_cmd(f'chown {flags} {user}{group} {path}')

    def chown_by_ref_file(self, path, ref_path, flags=''):
        return self.engine.run_cmd(f'chown {flags} --reference={ref_path} {path}')

    def apt_update(self, flags=''):
        return self.engine.run_cmd(f'apt {flags} update', validate=True)

    def apt_install(self, package, flags=''):
        return self.engine.run_cmd(f'apt {flags} install {package}', validate=True)

    def coverage_combine(self, flags=''):
        return self.engine.run_cmd(f'coverage combine {flags}', validate=True)

    def coverage_xml(self, out_file, flags=''):
        return self.engine.run_cmd(f'coverage xml -i -o {out_file} {flags}', validate=True)

    def tar(self, flags=''):
        return self.engine.run_cmd(f'tar {flags}')

    def pip3_install(self, package, flags=''):
        return self.engine.run_cmd(f'pip3 {flags} install {package}', validate=True)

    def gcovr(self, paths='', flags='', additional_flags=''):
        return self.engine.run_cmd(f'gcovr {flags} {paths} {additional_flags}', validate=True)

    def lcovr(self, flags=''):
        return self.engine.run_cmd(f'lcov {flags}', validate=True)

    def get_time(self):
        output = self.engine.run_cmd('date +"%T"', validate=True)
        return output

    def get_date(self):
        output = self.engine.run_cmd('date +"%d-%m-%y"', validate=True)
        return output

    def get_utc_time(self):
        output = self.engine.run_cmd('date +%s', validate=True)
        return output

    def set_time(self, time_str):
        output = self.engine.run_cmd(f'sudo date -s "{time_str}"', validate=True)
        return output

    def find(self, folder, name):
        """
        get result of find command
        :return: command output
        """
        return self.engine.run_cmd(f"sudo find {folder} -name {name}")

    def remove_module(self, module):
        """
        remove module
        """
        return self.engine.run_cmd(f"sudo rmmod {module}")

    def check_module_status(self, module):
        """
        check module install status
        :return: command output
        """
        return self.engine.run_cmd(f"sudo lsmod | grep {module}")

    def install_module(self, module):
        """
        install module
        """
        return self.engine.run_cmd(f"sudo insmod {module}")

    def extract_key_from_module(self, module):
        """
        extract key from a module
        """
        return self.engine.run_cmd(f"sudo strip -g {module}")

    def get_version(self, cli_type):
        version = None
        if cli_type == "NVUE":
            nv_version_json_str = self.engine.run_cmd("nv show system version -o json", validate=True)
            json_output = json.loads(nv_version_json_str)
            version = json_output.get("image")
        elif cli_type == "Sonic":
            sonic_version_output = self.engine.run_cmd('sudo sonic-cfggen -y /etc/sonic/sonic_version.yml'
                                                       ' -v build_version', validate=True)
            version = sonic_version_output.strip()
        return version

    def stat(self, file):
        """
        get status of file
        :return: file stat. e.g.  {"exists": False,"islink": False}
        """
        file_stat = {"exists": False,
                     "islink": False}
        reg_no_file = r"stat: cannot statx .* No such file or directory"
        reg_symbolic_file = r"Size:.*Blocks:.*O Block:.*symbolic link"
        file_stat_res = self.engine.run_cmd(f"sudo stat {file}")
        if re.search(reg_no_file, file_stat_res):
            return file_stat
        file_stat["exists"] = True
        if re.search(reg_symbolic_file, file_stat_res):
            file_stat["islink"] = True
        logger.info(f"{file}: {file_stat}")
        return file_stat

    def read_file(self, file_path, is_sudo=False):
        """
        Read file content.
        :param file_path:  file path.
        :return: Content of file.
        """
        file_status = self.stat(file_path)
        if not file_status['exists']:
            raise Exception(f'{file_path} not exist')
        prefix = "sudo " if is_sudo else ""
        return self.engine.run_cmd(f"{prefix}cat {file_path}")

    def get_performance_ports_list(self, topology_obj):
        """
        Method returns ports list of traffic generator from performance setup, which connected to DUT
        :return: TG ports list
        """
        ports_list = []
        switch_name = topology_obj.players[self.cli_obj.dut_alias]['attributes'].noga_query_data['attributes']['Common']['Name']
        noga_entire_data = get_noga_entire_resource_data(resource_name=switch_name)
        for resource in noga_entire_data:
            if 'etp' in resource['name'] and switch_name not in resource['connected with']:
                ports_list.append(resource['if'])
        return ports_list

    def check_dut_is_alive(self):
        ip = self.engine.ip
        port = self.engine.ssh_port
        dut_is_alive = True
        try:
            logger.info('Checking whether device is alive')
            check_port_status_till_alive(should_be_alive=True, destination_host=ip, destination_port=port, tries=2)
            logger.info('Device is alive')
        except Exception:
            logger.info('Device is not alive')
            dut_is_alive = False

        return dut_is_alive

    def prepare_for_installation(self, topology_obj, dut_alias='dut'):
        logger.info(f"prepare_for_installation: starting for dut_alias={dut_alias} ip={self.engine.ip}")
        switch_in_onie = False
        is_alive = self.check_dut_is_alive()
        in_dvs = self.check_if_in_dvs()
        logger.info(f"prepare_for_installation: ip={self.engine.ip} alive={is_alive} in_dvs={in_dvs}")
        if is_alive and not in_dvs:
            logger.info(f"prepare_for_installation: ip={self.engine.ip} attempting confirm_onie_boot_mode_install "
                        f"(assumes DUT is already in ONIE)")
            try:
                SonicOnieCli(self.engine.ip, self.engine.ssh_port).confirm_onie_boot_mode_install()
                switch_in_onie = True
            except Exception as err:
                logger.warning(f'DUT is not in ONIE. \n Got error: {err}')
                # it can cover the following scenarios
                # 1. user/password doesn't match the default one
                # 2. ping and ssh switch are ok, but cannot login into it
                if self.switch_dut_to_onie_by_remote_reboot(topology_obj, dut_alias):
                    switch_in_onie = True
        else:
            logger.info(f"prepare_for_installation: ip={self.engine.ip} taking serial/remote-reboot path "
                        f"(alive={is_alive}, in_dvs={in_dvs})")
            if self.switch_dut_to_onie_by_remote_reboot(topology_obj, dut_alias):
                switch_in_onie = True
            elif self.switch_dut_to_onie_by_serial_on_dut_stuck_on_selecting_os_page(topology_obj, dut_alias):
                switch_in_onie = True
            elif self.switch_dut_from_sonic_to_onie_by_serial_on_dut_is_not_alive(topology_obj, dut_alias):
                switch_in_onie = True
        logger.info(f"prepare_for_installation: ip={self.engine.ip} done, switch_in_onie={switch_in_onie}")
        return switch_in_onie

    def boot_into_onie_by_serial_on_remote_reboot(self, topology_obj, dut_alias='dut'):
        serial_engine = SecureBootHelper.get_serial_engine_instance(topology_obj, dut_alias)
        serial_engine.create_serial_engine(login_to_switch=False)
        arrow_down_key = "\x1b[B"
        arrow_up_key = "\x1b[A"
        enter_key = '\r'

        logger.info("Wait for GNU GRUB  version")
        output, respond = serial_engine.run_cmd(
            '', ['GRUB loading.', 'GNU GRUB  version'], timeout=240, send_without_enter=True)
        logger.info(f"GNU GRUB  version is ready.\n output:{output} \n respond:{respond}")

        logger.info("Select ONIE by pressing arrow down")
        # press the arrow up several times to ensure the item is selected
        for i in range(3):
            logger.info("Sending one arrow down")
            serial_engine.run_cmd(arrow_down_key, expected_value='.*', send_without_enter=True)
            time.sleep(0.5)
        logger.info("Onie option selected")

        logger.info("Pressing Enter to enter ONIE grub menu")
        serial_engine.run_cmd(enter_key, expected_value='.*', timeout=30, send_without_enter=True)

        logger.info("Select 'ONIE: Install OS' by entering arrow up")
        # press the arrow up several times to ensure the item is selected
        for i in range(2):
            logger.info("Sending one arrow up")
            serial_engine.run_cmd(arrow_up_key, expected_value='.*', send_without_enter=True)
            time.sleep(0.5)

        logger.info("Pressing Enter to enter ONIE: Install OS")
        serial_engine.run_cmd('\r', expected_value='.*', timeout=30, send_without_enter=True)

    def switch_dut_to_onie_by_remote_reboot(self, topology_obj, dut_alias='dut'):
        with allure.step('Do remote reboot because dut is not alive'):
            try:
                logger.info("Do remote reboot ...")
                self.remote_reboot(topology_obj, dut_alias=dut_alias, boot_into_onie=True)
            except Exception as err:
                logger.info(f"remote reboot err:{err}")

        with allure.step('Check dut is in onie or not after remote reboot'):
            return self.check_dut_in_onie_install_status()

    def is_dummy_command_succeed(self):
        try:
            self.engine.run_cmd(DUMMY_COMMAND, validate=True)
            logger.info('login with credentials username: {} ,password:{} succeed!'.
                        format(self.engine.username, self.engine.password))
            return True
        except netmiko.ssh_exception.NetmikoAuthenticationException:
            logger.info('login with credentials username: {} ,password:{} did not succeed!'.
                        format(self.engine.username, self.engine.password))
            return False

    def remote_reboot(self, topology_obj, dut_alias='dut', boot_into_onie=False, wait_till_alive=True):
        ip = self.engine.ip
        port = self.engine.ssh_port
        logger.info('Executing remote reboot')
        cmd = topology_obj.players[dut_alias]['attributes'].noga_query_data['attributes']['Specific'][
            'remote_reboot']
        _, _, rc = run_process_on_host(cmd)
        if rc == InfraConst.RC_SUCCESS:
            if boot_into_onie:
                self.boot_into_onie_by_serial_on_remote_reboot(topology_obj, dut_alias)
            if wait_till_alive:
                check_port_status_till_alive(should_be_alive=True, destination_host=ip, destination_port=port)
        else:
            raise Exception('Remote reboot rc is other then 0')

    def switch_dut_to_onie_by_serial_on_dut_stuck_on_selecting_os_page(self, topology_obj, dut_alias='dut'):
        """
        This function is to switch dut to onie by serial,
        when dut is stuck on the page of select os and losing ssh connection
        """
        with allure.step('Create serial engine without login to switch'):
            try:
                serial_engine = SecureBootHelper.get_serial_engine_instance(topology_obj, dut_alias)
                serial_engine.create_serial_engine(login_to_switch=False)
            except Exception as err:
                logger.error(f"Create serial engine error: {err}")
        with allure.step('switch dut to onie by serial'):
            try:
                time_out = 10
                wait_serial_take_effect = 2
                cmd_enter = "\n"
                cmd_press_esc = "\33"

                # before selecting onie, press esc and enter key to make sure the page is in the os selected page
                logger.info("Press esc ")
                serial_engine.run_cmd(cmd_press_esc, expected_value=" ", timeout=time_out)
                time.sleep(wait_serial_take_effect)
                logger.info("Press enter")
                serial_engine.run_cmd(cmd_enter, expected_value=" ", timeout=time_out)
                time.sleep(wait_serial_take_effect)

                logger.info("Select the last item: ONIE")
                cmd_last_one = "\03"
                serial_engine.run_cmd(cmd_last_one, expected_value="ONIE", timeout=time_out)
                time.sleep(wait_serial_take_effect)

                logger.info("Boot into ONIE by pressing enter")
                serial_engine.run_cmd(cmd_enter, expected_value=" ", timeout=time_out)
                time.sleep(wait_serial_take_effect)

                logger.info("Boot into ONIE install by pressing enter")
                serial_engine.run_cmd(cmd_enter, expected_value=" ", timeout=time_out)
                time.sleep(wait_serial_take_effect)
                logger.info("DUT is switched to onie by serial")

            except Exception as err:
                logger.error(f"Switching dut to onie by serial failed. {err}")

        with allure.step('Check dut is in onie or not after switching it from stuck page to onie by serial'):
            return self.check_dut_in_onie_install_status()

    def switch_dut_from_sonic_to_onie_by_serial_on_dut_is_not_alive(self, topology_obj, dut_alias='dut'):
        """
        This function is to switch dut from sonic into onie by serial, when dut is losing ssh connection
        """
        with allure.step('Create serial engine'):
            try:
                serial_engine = SecureBootHelper.get_serial_engine_instance(topology_obj, dut_alias)
                serial_engine.create_serial_engine()
            except Exception as err:
                logger.error(f"Create serial engine with login switch error: {err}")
        with allure.step('Switch dut from sonic to onie by serial'):
            try:
                time_out = 10
                logger.info("Set next_entry=ONIE in grub")
                cmd_set_next_entry = "sudo grub-editenv /host/grub/grubenv set next_entry=ONIE"
                serial_engine.run_cmd(cmd_set_next_entry, timeout=time_out)

                logger.info("Do reboot ")
                cmd_reboot = "sudo reboot"
                serial_engine.run_cmd(cmd_reboot, expected_value=" ", timeout=time_out)
                logger.info("DUT is switched to onie by serial")
            except Exception as err:
                logger.error(f"Switching dut to onie by serial failed. {err}")

        with allure.step('Check dut is in onie or not after switching it from sonic to onie by serial'):
            return self.check_dut_in_onie_install_status(tries=30)

    def check_dut_in_onie_install_status(self, tries=20):
        switch_in_onie = False
        with allure.step('Check dut is in onie or not '):
            try:
                logger.info('Checking whether device is alive')
                check_port_status_till_alive(should_be_alive=True, destination_host=self.engine.ip,
                                             destination_port=self.engine.ssh_port,
                                             tries=tries)
            except Exception as err:
                logger.error(f"Dut is not alive. {err}")
        with allure.step("Check dut is in onie install status"):
            try:
                logger.info('Checking dut is in onie install status')
                SonicOnieCli(self.engine.ip, self.engine.ssh_port).confirm_onie_boot_mode_install()
                switch_in_onie = True
            except Exception as err:
                logger.error(f"Dut is not in onie. {err}")

        logger.info(f"Dut onie status is {switch_in_onie}")
        return switch_in_onie

    @staticmethod
    def install_image_onie(engine, image_path):
        sonic_cli_ssh_connect_timeout = 10
        dut_ip = engine.ip
        dut_ssh_port = engine.ssh_port

        logger.info(f"install_image_onie: ip={dut_ip} starting onie-nos-install with image_path={image_path}")
        with allure.step('Installing image by "onie-nos-install"'):
            SonicOnieCli(dut_ip, dut_ssh_port).install_image(image_path=image_path)
        logger.info(f"install_image_onie: ip={dut_ip} onie-nos-install returned, switch will reboot into new image")

        with allure.step('Waiting for switch shutdown after reload command'):
            logger.info('Waiting for switch shutdown after reload command')
            check_port_status_till_alive(False, dut_ip, dut_ssh_port)

        with allure.step('Waiting for switch bring-up after reload'):
            logger.info('Waiting for switch bring-up after reload')
            check_port_status_till_alive(True, dut_ip, dut_ssh_port)

        with allure.step('Waiting for CLI bring-up after reload'):
            logger.info('Waiting for CLI bring-up after reload')
            time.sleep(sonic_cli_ssh_connect_timeout)
        logger.info(f"install_image_onie: ip={dut_ip} install flow finished")

    @staticmethod
    def is_performance_setup(str_with_setup_name):
        return 'performance' in str_with_setup_name

    def execute_command_in_docker(self, docker, command):
        return self.engine.run_cmd('docker exec -i {} {}'.format(docker, command))

    def copy_to_docker(self, docker, src_path_on_host, dst_path_in_docker):
        return self.engine.run_cmd('docker cp {} {}:{}'.format(src_path_on_host, docker, dst_path_in_docker))

    def copy_from_docker(self, docker, dst_path_on_host, src_path_in_docker):
        return self.engine.run_cmd('sudo docker cp {}:{} {}'.format(docker, src_path_in_docker, dst_path_on_host))

    def remove_from_docker(self, docker, src_path_in_docker):
        return self.engine.run_cmd('sudo docker exec {} rm -rf {}'.format(docker, src_path_in_docker))

    def check_if_in_dvs(self):
        try:
            engine = LinuxSshEngine(self.engine.ip, username=SSHConsts.DVS_CREDS['username'],
                                    password=SSHConsts.DVS_CREDS['password'])
            dvs_marker = engine.run_cmd("command -v dvs_start.sh")
            return bool(dvs_marker.strip()) and "dvs_start.sh" in dvs_marker
        except Exception:
            return False

    def prepare_onie_reboot_script_on_dut(self):
        onie_reboot_script = 'onie_reboot.sh'
        onie_reboot_script_path = f'/tmp/{onie_reboot_script}'
        onie_reboot_script_local_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                     f'../../scripts/sonic_deploy/{onie_reboot_script}')
        self.engine.run_cmd('sudo rm -rf /tmp/*')
        self.engine.copy_file(source_file=onie_reboot_script_local_path, file_system='/tmp',
                              dest_file=onie_reboot_script)
        self.engine.run_cmd(f'chmod 777 {onie_reboot_script_path}', validate=True)
        return onie_reboot_script_path

    def reboot_by_onie_reboot_script(self, onie_reboot_script_path, mode, chip_type="SPC3"):
        logger.info(f"Reboot to ONIE with boot-mode {mode}")
        with allure.step(f"Reboot to ONIE with boot-mode {mode}"):
            if mode == "uninstall":
                timeout = PerfConsts.TIMEOUT_FOR_UNINSTALL_MODE.get(
                    chip_type, PerfConsts.TIMEOUT_FOR_UNINSTALL_MODE_DEFAULT)
            else:
                timeout = PerfConsts.TIMEOUT_FOR_INSTALL_MODE

            self.engine.reload([f'{onie_reboot_script_path} {mode}'], wait_after_ping=timeout, ssh_after_reload=False)

    def uninstall_os_flow(self, current_os, target_cli_type, chip_type):
        logger.info(target_cli_type)
        if current_os == "Cumulus":
            if target_cli_type == "NVUE":
                logger.info("Skipping uninstall mode since cumulus would wipe out the system")
                self.engine.reload("sudo onie-select -i -f && sudo reboot", wait_after_ping=PerfConsts.TIMEOUT_FOR_INSTALL_MODE, ssh_after_reload=False)
            else:
                logger.info("Cumulus/NVOS detected wiping out the entire system")
                uninstall_wait = PerfConsts.TIMEOUT_FOR_UNINSTALL_MODE.get(
                    chip_type, PerfConsts.TIMEOUT_FOR_UNINSTALL_MODE_DEFAULT)
                self.engine.reload("sudo onie-select -k -f && sudo reboot", wait_after_ping=uninstall_wait,
                                   ssh_after_reload=False)
        else:
            onie_reboot_script_path = self.prepare_onie_reboot_script_on_dut()
            if target_cli_type == "NVUE":
                logger.info("Skipping uninstall mode since cumulus would wipe out the system")
                self.reboot_by_onie_reboot_script(onie_reboot_script_path, 'install', chip_type)
            else:
                logger.info(f"Wiping the entire system for {target_cli_type} install")
                self.reboot_by_onie_reboot_script(onie_reboot_script_path, 'uninstall', chip_type)
