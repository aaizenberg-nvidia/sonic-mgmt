from __future__ import annotations

import os.path
import re
import subprocess
import time
from abc import ABC, abstractmethod

import rf_progress
from Constants import Defaults, RedfishCollection
from ip_utils import bracket_for_url, is_ipv6
from Redfish_rest_api import RedFishRestApi


def _redact_sshpass(cmd_list):
    """Return a copy of the subprocess command list with the sshpass password masked."""
    redacted = list(cmd_list)
    for i, tok in enumerate(redacted[:-1]):
        if tok == '-p':
            redacted[i + 1] = '***'
    return redacted


def normalize_cpld_versions(versions: str | dict[str, str]) -> str:
    """CPLD000505_REV0006-style tokens in CPLD1..N order, from a json version_name, a file name or system/cpld."""
    text = ' '.join(versions.values()) if isinstance(versions, dict) else versions
    return '_'.join(re.findall(r'CPLD\d+_REV\d+', text))


class Component(ABC):
    def __init__(self, name: str, install_path: str, required_version: str | None = None):
        self.name = name
        self.required_version = required_version
        self.install_path = install_path
        self._installed_version = None

    @property
    def installed_version(self):
        return self.get_installed_version()

    @abstractmethod
    def update(self) -> bool:
        ...

    @abstractmethod
    def get_installed_version(self) -> str:
        ...

    @abstractmethod
    def power_cycle(self, switch_info):
        ...


class BmcComponent(Component):
    def __init__(self, name: str, install_path: str, required_version: str | None, rf_api: RedFishRestApi,
                 fw_ids: list[str]):
        super().__init__(name, install_path, required_version)
        self.rf_api = rf_api
        self.fw_ids = fw_ids

    def get_installed_version(self) -> str:
        versions = set()
        for fw_id in self.fw_ids:
            respond = self.rf_api.get_query(f'{RedfishCollection.FIRMWARE_INVENTORY}/{fw_id}')
            if 'Version' not in respond:
                raise RuntimeError(f"'Version' missing for {fw_id}. Response: {respond}")
            versions.add(respond['Version'])
        # instances on different versions never match the required one, so they get updated
        return versions.pop() if len(versions) == 1 else ', '.join(sorted(versions))

    def update(self) -> bool:
        print(
            f"Performing update for {self.name} to {self.required_version or self.install_path}")

        targets_list = [] if self.name in [Defaults.EROT_NAME, Defaults.SMA_NAME] else \
            [f"/{RedfishCollection.FIRMWARE_INVENTORY}/{self.fw_ids[0]}"]
        respond = self.rf_api.post_multipart_update(RedfishCollection.UPDATE_SERVICE_MULTIPART, self.install_path,
                                                    targets_list=targets_list)
        od_task = rf_progress.Task(**respond)
        od_task.monitor(self.rf_api)
        if od_task.success():
            if od_task.await_action():
                print(f"Please proceed with {od_task.get_action()}")
            else:
                print("Success.")
            return True
        else:
            od_task.print_error()
            return False

    def power_cycle(self, switch_info):
        reset_type = "PowerCycle"
        data = {
            "ResetType": f"{reset_type}"
        }
        respond, _ = self.rf_api.post_query(RedfishCollection.RESET, data)
        print("Power cycle request sent. Sleeping for 2.5 minutes...")
        time.sleep(150)


class CpldComponent(Component):
    def __init__(self, name: str, install_path: str, required_version: str,
                 switch_ip: str, ssh_user: str, ssh_pass: str, rf_api: RedFishRestApi):
        super().__init__(name, install_path, required_version)
        self.rf_api = rf_api
        self.switch_ip = switch_ip
        self.ssh_user = ssh_user
        self.ssh_pass = ssh_pass

    def get_installed_version(self) -> str:
        cmd = 'cat /var/run/hw-management/system/cpld'
        return normalize_cpld_versions(self._run_ssh(command=cmd))

    def update(self) -> bool:
        file_name = os.path.basename(self.install_path)
        remote_path = '/tmp/'
        self._run_scp(remote_path)
        update_cmd = f'sudo cpldupdate --gpio --print-progress {remote_path}{file_name}'
        try:
            self._run_ssh(update_cmd)
            return True
        except Exception:
            return False

    NVOS_READY_TIMEOUT = 10 * 60
    NVOS_READY_POLL_INTERVAL = 15

    def power_cycle(self, switch_info):
        reset_type = "PowerCycle"
        data = {
            "ResetType": f"{reset_type}"
        }
        respond, _ = self.rf_api.post_query(RedfishCollection.RESET, data)
        print(f"Power cycle request sent. "
              f"Waiting up to {self.NVOS_READY_TIMEOUT // 60} minutes for NVOS to be ready...")
        self._wait_for_nvos_ready()

    def _wait_for_nvos_ready(self):
        """Wait for the switch to go down and come back with NVOS ready.

        Polls the STATE_DB key NVUE itself uses to answer 'System is initializing',
        so the next MARS step never gets a half-booted switch.
        """
        cmd = 'sonic-db-cli STATE_DB hget "SYSTEM_READY|SYSTEM_STATE" Status'
        deadline = time.monotonic() + self.NVOS_READY_TIMEOUT
        went_down = False
        while time.monotonic() < deadline:
            try:
                is_ready = self._run_ssh(cmd) == 'UP'
            except Exception:  # noqa: BLE001 - _run_ssh raises a bare Exception while the switch is down
                is_ready = False  # switch is off, rebooting, or redis is not up yet
            if is_ready and went_down:
                print("NVOS is ready")
                return
            went_down = went_down or not is_ready
            time.sleep(self.NVOS_READY_POLL_INTERVAL)
        raise TimeoutError(f"NVOS did not become ready within {self.NVOS_READY_TIMEOUT} seconds after power cycle")

    def _run_player_cmd(self, command):
        try:
            output = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True
            )

            res = output.stdout.strip()

            return_code = output.returncode
            if return_code:
                raise Exception(f"{command}\nExit Code: {return_code}\n{output}")
            print(f'{command} successfully executed')
            return res
        except subprocess.CalledProcessError as e:
            print(f"Command failed with return code {e.returncode}")
            print(e.output)
            print(e.stderr)
            print("Power cycle failed. Please do one manually.")
            raise

    def _run_ssh(self, command):
        """
        execute command via ssh connection and wait for output
        :param command: command to execute
        :return: command output
        """
        # Construct the SSH command
        ssh_command = [
            'sshpass', '-p', self.ssh_pass,
            'ssh', '-o', 'UserKnownHostsFile=/dev/null', '-o', 'StrictHostKeyChecking=no',
            '-o', 'TCPKeepAlive=yes', '-o', 'ServerAliveInterval=30', '-o', 'ConnectTimeout=15',
        ]
        if is_ipv6(self.switch_ip):
            ssh_command.append('-6')
        ssh_command.append(f'{self.ssh_user}@{self.switch_ip}')

        if isinstance(command, str):
            ssh_command.append(command)
        elif isinstance(command, list):
            ssh_command.extend(command)
        else:
            raise Exception(f'Unsupported command type {type(command)}')

        print(f"[ssh] dispatching: {' '.join(_redact_sshpass(ssh_command))}")
        try:
            process = subprocess.Popen(
                ssh_command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )

            output, _ = process.communicate()
            output = output.decode('latin-1')
            output = output.strip()

            return_code = process.wait()
            if return_code:
                raise Exception(f"{command}\nExit Code: {return_code}\n{output}")
            print(f'{command} successfully executed')
            output = output.split('\n')[-1]
            return output

        except Exception as e:
            print(f"An error occurred: {e}")
            raise

    def _run_scp(self, remote_path):
        """
        execute file_path via scp connection and wait for output
        :return: command output
        """
        # Construct the SSH command
        ssh_command = [
            'sshpass', '-p', self.ssh_pass,
            'scp', '-o', 'PubkeyAuthentication=no', '-o', 'StrictHostKeyChecking=no',
            '-o', 'UserKnownHostsFile=/dev/null',
        ]
        if is_ipv6(self.switch_ip):
            ssh_command.append('-6')
        ssh_command.extend([
            f'{self.install_path}',
            f'{self.ssh_user}@{bracket_for_url(self.switch_ip)}:{remote_path}'
        ])

        print(f"[scp] dispatching: {' '.join(_redact_sshpass(ssh_command))}")
        try:
            process = subprocess.Popen(
                ssh_command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )

            output, _ = process.communicate()
            output = output.decode('latin-1')
            output = output.strip()

            return_code = process.wait()
            if return_code:
                raise Exception(f"{self.install_path}\nExit Code: {return_code}\n{output}")
            print(f"Copy of {self.install_path} performed.")
            output = output.split('\n')[-1]
            return output

        except Exception as e:
            print(f"An error occurred: {e}")
            raise
