import json
import logging
import os
import shlex
import subprocess
import time
from typing import Tuple, List
from ngts.nvos_tools.infra import ExceptionTool
import ngts.tools.test_utils.allure_utils as allure
from devts.infra.tools.connection_tools.linux_ssh_engine import LinuxSshEngine
from ngts.nvos_constants.constants_nvos import SystemConsts
from ngts.nvos_tools.infra.IpTool import IpTool
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.tests_nvos.general.security.certificate.CertInfo import CertInfo


class CurlTool:
    def __init__(self, server_host: str, username: str, password: str,
                 server_port: str = SystemConsts.EXTERNAL_API_PORT_DEFAULT, cacert='',
                 verify_tools_installed: bool = False, client_cert: CertInfo = None):

        self.server_host = server_host
        self.server_port = server_port
        self.username = username
        self.password = password
        self.cacert = cacert
        self.client_cert: CertInfo = client_cert

        if verify_tools_installed:
            with allure.step('verify curl installed on player'):
                self._verify_curl_installed()

        self._live_processes: List[subprocess.Popen] = []

    def request(self, username: str = '', password: str = '', skip_cert_verify: bool = True, cacert='', path: str = '', request_type='', client_cert: CertInfo = None, resolve_dn: str = '') -> Tuple[str, str]:
        out, err, _ = self._run_rest_op(request_type, skip_cert_verify, cacert, username,
                                        password, path, client_cert, resolve_dn)
        return out, err

    def _run_rest_op(self, rest_op: str, is_insecure: bool, cacert: str, username: str = '',
                     password: str = '', path: str = '', client_cert: CertInfo = None, resolve_dn: str = '') -> Tuple[
            str, str, subprocess.Popen]:
        curl_cmd = self._compose_curl_cmd(cacert, client_cert, is_insecure, password, path, resolve_dn, rest_op,
                                          username)
        with allure.step('run curl command in process'):
            return self._run_cmd_in_process(curl_cmd)

    def _compose_curl_cmd(self, cacert, client_cert, is_insecure, password, path, resolve_dn, rest_op, username):
        with allure.step('compose the curl command'):
            username = username or self.username
            password = password or self.password
            host = f'[{self.server_host}]' if IpTool.is_address_ipv6(self.server_host) else self.server_host

            if is_insecure:
                cert_flag = '--insecure'
            else:
                cacert_to_use = cacert or self.cacert
                assert cacert_to_use, 'cacert path was not specified'
                cert_flag = f'--cacert {cacert_to_use}'
                client_cert_to_use = client_cert or self.client_cert
                if client_cert_to_use:
                    cert_flag += f' --key {client_cert_to_use.private} --cert {client_cert_to_use.public}'
                if resolve_dn:
                    cert_flag += f' --resolve {resolve_dn}:{self.server_port}:{host}'
                    host = resolve_dn

            curl_cmd = (f"curl {cert_flag} --user {username}:{password} "
                        f"--request {rest_op} 'https://{host}:{self.server_port}{path}'")
        return curl_cmd

    def graceful_restart_bmc(self):
        return self.run_redfish_command(rest_op='POST', data='{"ResetType": "GracefulRestart"}',
                                        path='/Managers/BMC_0/Actions/Manager.Reset')

    def reset_bmc_to_factory(self, username='', password=''):
        return self.run_redfish_command(rest_op='POST', data='{"ResetToDefaultsType": "ResetAll"}',
                                        path='/Managers/BMC_0/Actions/Manager.ResetToDefaults',
                                        username=username, password=password)

    def change_root_password(self, username='', password='', dut_engine=None, new_password='ABYX12#14artb',
                             validate=False):
        return self.run_redfish_command(
            rest_op='PATCH',
            data=f'{{"Password": "{new_password}"}}',
            path='/AccountService/Accounts/root',
            username=username,
            password=password,
            dut_engine=dut_engine,
            validate=validate,
        )

    def run_redfish_command(self, rest_op: str, data: str = '', username: str = '',
                            password: str = '', path: str = '', dut_engine=None,
                            validate=False, max_time: int = 30):
        """Run a Redfish request via curl on the DUT.

        :param validate: passed to the engine so a non-zero curl exit can raise;
            ``change_root_password`` above relies on it.
        :param max_time: curl ``--max-time`` seconds (default 30). Keeps CL from
            hanging on dead paths; IB/long POSTs can pass a larger value.
        """
        dut_engine: LinuxSshEngine = dut_engine or TestToolkit.get_engine()
        with allure.step('compose the curl command'):
            username = username or self.username
            password = password or self.password

        # --noproxy '*': the switch/DUT may have http(s)_proxy set; without this curl
        #   routes the BMC address through the proxy and hangs.
        # --max-time: never block indefinitely (e.g. usb0:443 with no listener).
        # Bracket IPv6 literals like _compose_curl_cmd; quote the full URL so
        # host/path concatenation cannot go wrong after quoting pieces separately.
        host = (
            '[{}]'.format(self.server_host)
            if IpTool.is_address_ipv6(self.server_host)
            else self.server_host
        )
        url = 'https://{}/redfish/v1{}'.format(host, path)
        auth = shlex.quote("{}:{}".format(username, password))
        data_flag = '-d {}'.format(shlex.quote(data)) if data else ''
        curl_cmd = (
            "curl -k --noproxy '*' --max-time {} -w '\\n' "
            "-u {} -H 'Content-Type: application/json' "
            "-X {} {} {}"
            .format(
                int(max_time),
                auth,
                shlex.quote(rest_op),
                data_flag,
                shlex.quote(url),
            )
        )
        return dut_engine.run_cmd(curl_cmd, validate=validate)

    def run_redfish_multipart_update(self, file_path: str, targets: list = None,
                                     force_update: bool = False,
                                     path: str = '/UpdateService/update-multipart',
                                     username: str = '', password: str = '', dut_engine=None,
                                     max_time: int = 600):
        """POST a firmware package to the Redfish ``UpdateService`` as multipart form data.

        The BMC advertises ``MultipartHttpPushUri`` and offers neither
        ``SimpleUpdate`` nor a plain ``HttpPushUri``, so a multipart POST is the
        only Redfish route for a firmware update.

        curl runs on ``dut_engine``, so ``file_path`` must already be on that host.

        :param targets: Redfish FirmwareInventory ids to update, e.g.
            ``['/redfish/v1/UpdateService/FirmwareInventory/MGX_FW_BMC_0']``. An
            empty list lets the package's own device records choose the target.
        :param max_time: curl ``--max-time``. bmcweb answers with a Task as soon
            as the upload lands, so this bounds the upload rather than the burn.
        :return: raw response text (a Redfish Task resource on success).
        """
        dut_engine: LinuxSshEngine = dut_engine or TestToolkit.get_engine()
        username = username or self.username
        password = password or self.password

        host = (
            '[{}]'.format(self.server_host)
            if IpTool.is_address_ipv6(self.server_host)
            else self.server_host
        )
        url = 'https://{}/redfish/v1{}'.format(host, path)
        params = json.dumps({'Targets': list(targets or []), 'ForceUpdate': bool(force_update)})
        auth = shlex.quote('{}:{}'.format(username, password))
        # Both parts carry an explicit content type: bmcweb rejects the form when
        # UpdateParameters arrives as text/plain rather than application/json.
        curl_cmd = (
            "curl -k --noproxy '*' --max-time {} -w '\\n' -u {} -F {} -F {} {}"
            .format(
                int(max_time),
                auth,
                shlex.quote('UpdateParameters={};type=application/json'.format(params)),
                shlex.quote('UpdateFile=@{};type=application/octet-stream'.format(file_path)),
                shlex.quote(url),
            )
        )
        with allure.step('Redfish multipart firmware push to {}'.format(url)):
            return dut_engine.run_cmd(curl_cmd)

    def wait_for_bmc_available(self, username: str = '', password: str = '',
                               timeout: int = 3 * 60, retry_interval: int = 10,
                               dut_engine=None) -> bool:
        """
        Wait until BMC is available and responding to requests.

        Args:
            username: BMC username (uses instance default if empty)
            password: BMC password (uses instance default if empty)
            timeout: Maximum time to wait in seconds (default: 180)
            retry_interval: Time between retry attempts in seconds (default: 10)
            dut_engine: SSH engine to use for commands (uses default if None)

        Returns:
            bool: True if BMC becomes available within timeout, False otherwise
        """
        dut_engine: LinuxSshEngine = dut_engine or TestToolkit.get_engine()
        username = username or self.username
        password = password or self.password

        start_time = time.time()
        attempt = 1

        with allure.step(f'Wait for BMC to be available (timeout: {timeout}s)'):
            while time.time() - start_time < timeout:
                try:
                    self._log(f"Attempt {attempt}: Checking BMC availability")

                    # Removed -s flag from the curl - it means silent fail and breaks automation.
                    curl_cmd = (f"curl -k -w '\\n' -u {username}:{password} "
                                f"https://{self.server_host}/redfish/v1/Managers/BMC_0")

                    output = dut_engine.run_cmd(curl_cmd)
                    auth_success = all(err_msg not in output.lower() for err_msg in ['fail', 'error'])

                    if auth_success:
                        self._log(f"BMC is available after {time.time() - start_time:.1f}s")
                        return True

                    self._log(f"BMC not ready yet. HTTP response: {output}")

                except Exception as e:
                    self._log(f"Exception while checking BMC availability: {ExceptionTool.format_exception(e)}")

                if time.time() - start_time + retry_interval < timeout:
                    self._log(f"Waiting {retry_interval}s before next attempt...")
                    time.sleep(retry_interval)
                    attempt += 1
                else:
                    break

            elapsed_time = time.time() - start_time
            self._log(f"BMC did not become available within {timeout}s (elapsed: {elapsed_time:.1f}s)")
            return False

    def wait_for_bmc_unavailable(self, timeout: int = 60, retry_interval: int = 5,
                                 dut_engine=None) -> bool:
        """
        Wait until BMC is no longer responding to ping (e.g. after factory reset).

        Args:
            timeout: Maximum time to wait in seconds (default: 60)
            retry_interval: Time between retry attempts in seconds (default: 5)
            dut_engine: SSH engine to use for commands (uses default if None)

        Returns:
            bool: True if BMC becomes unavailable within timeout, False otherwise
        """
        dut_engine: LinuxSshEngine = dut_engine or TestToolkit.get_engine()

        start_time = time.time()
        attempt = 1

        with allure.step(f'Wait for BMC to become unavailable (timeout: {timeout}s)'):
            while time.time() - start_time < timeout:
                self._log(f"Attempt {attempt}: Pinging BMC at {self.server_host}")

                output = dut_engine.run_cmd(
                    f"ping -c 1 -W 3 {self.server_host} > /dev/null 2>&1 && echo 'UP' || echo 'DOWN'"
                )

                if 'DOWN' in output:
                    self._log(f"BMC is down after {time.time() - start_time:.1f}s")
                    return True

                self._log(f"BMC still responds to ping")
                time.sleep(retry_interval)
                attempt += 1

            elapsed_time = time.time() - start_time
            self._log(f"BMC did not become unavailable within {timeout}s (elapsed: {elapsed_time:.1f}s)")
            return False

    def _verify_curl_installed(self):
        cmd = 'curl -version'
        output = self._run_cmd_in_process(cmd)
        assert 'bash' not in output, f"curl is not installed on player.\n{cmd}\n{output}"

    def _run_cmd_in_process(self, cmd: str) -> Tuple[
            str, str, subprocess.Popen]:
        self._log(f"run: {cmd}")
        process = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   preexec_fn=os.setsid)
        self._log(f"get output from cmd process")
        out, err = self._get_cmd_process_output(process)
        return out, err, None

    def _get_cmd_process_output(self, process: subprocess.Popen):
        output, err = process.communicate()
        output = output.decode('utf-8')
        err = err.decode('utf-8')
        self._log(f"output: {output}")
        self._log(f"err: {err}")
        return output, err

    def _log(self, msg: str):
        logging.info(f"[CurlTool] {msg}")
