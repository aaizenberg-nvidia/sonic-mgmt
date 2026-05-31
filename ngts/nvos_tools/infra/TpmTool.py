import logging
from typing import Dict, Any, List, Tuple

from devts.infra.tools.connection_tools.linux_ssh_engine import LinuxSshEngine
from ngts.nvos_tools.Devices.BaseDevice import BaseDevice
from ngts.tests_nvos.general.security.tpm_attestation import constants as tpmconst
from ngts.tools.test_utils import allure_utils as allure

PATH_NO_EXIST_ERR = 'No such file or directory'
CMD_NOT_FOUND_ERR = 'command not found'
PRIMARY_SED_TPM_BANK = '0x81010001'
SECONDARY_SED_TPM_BANK = '0x81010002'

# Canonical security filesystem path of the firmware/TCG event log (the binary IML the
# ``event_log.bin`` upload action exports). Parsed with ``tpm2_eventlog``.
TPM_EVENT_LOG_SECURITYFS_PATH = '/sys/kernel/security/tpm0/binary_bios_measurements'


class TpmTool:
    def __init__(self, engine: LinuxSshEngine):
        self.engine = engine
        self.tpm_pass = None

    def _require_tpm_pass(self, device: BaseDevice) -> bool:
        if self.tpm_pass is not None:
            return True
        if getattr(device, 'requires_tpm_pass', False):
            try:
                tpm_pass = self.engine.run_cmd(f'sudo dd if={tpmconst.TPM_PASS_LOCATION} bs=1 skip=4 2>/dev/null | base64 -d | awk 1')
            except Exception as e:
                logging.error(f'Error extracting TPM pass: {e}')
                raise e
            self.tpm_pass = tpm_pass.strip()
            if not self.tpm_pass:
                raise Exception('TPM pass is empty while it should be filled')
            logging.info(f'TPM pass extracted: {self.tpm_pass}')
            return True
        return False

    """ API methods """

    def get_files_in_tpm_dir(self) -> List[str]:
        with allure.step('get list of files in tpm dir'):
            return self.engine.run_cmd(f'ls {tpmconst.TPM_DIR}').split()

    def is_tpm_attestation_ready(self) -> bool:
        """
        check if setup is prepared for tpm attestation
        """
        return self._is_sys_tpm_dir_exists() and self._is_dev_tpm_dir_exists() and \
            self._is_tpm_tools_installed() and self._is_tpm_capabilities_ok()

    def install_tpm_tools(self) -> bool:
        with allure.step('install tpm tools'):
            self.engine.run_cmd('sudo apt update -y && sudo apt install tpm2-tools -y')
            return self._is_tpm_tools_installed()

    def is_check_quote_ok(self, nonce: str) -> bool:
        with allure.step('convert AIK from crt to pem'):
            tmp_aik_path = f'/tmp/{tpmconst.AIK_PEM_FILENAME}'
            self.engine.run_cmd(f'sudo rm -f {tmp_aik_path}')
            out = self.engine.run_cmd(f'sudo openssl x509 -in {tpmconst.AIK_FILE_PATH} -pubkey -noout > {tmp_aik_path}')
            assert not out, f'unexpected output after converting AIK crt to pem:\n{out}'
        with allure.step('run check-quote'):
            out = self.engine.run_cmd(f'sudo tpm2_checkquote -u {tmp_aik_path} '
                                      f'-m {tpmconst.TPM_DIR}/pcr_quote '
                                      f'-s {tpmconst.TPM_DIR}/pcr_quote.sig '
                                      f'-q "{nonce}"')
        with allure.step('remove tmp pem file'):
            self.engine.run_cmd(f'sudo rm -f {tmp_aik_path}')
        return 'ERROR' not in out

    def remove_quote_files_from_tpm_dir(self):
        with allure.step('remove quote files from tpm dir'):
            self.engine.run_cmd(f'sudo rm -f {tpmconst.TPM_DIR}/pcr_quote')
            self.engine.run_cmd(f'sudo rm -f {tpmconst.TPM_DIR}/pcr_quote.sig')
            self.engine.run_cmd(f'sudo rm -f {tpmconst.QUOTE_FILE_PATH}')

    def get_quote_file_content(self):
        return self.engine.run_cmd(f'sudo cat {tpmconst.QUOTE_FILE_PATH}')

    def get_event_log(self, event_log_path: str = TPM_EVENT_LOG_SECURITYFS_PATH) -> str:
        """
        Return the human-readable TPM (TCG) event log, parsed with ``tpm2_eventlog``.

        :param event_log_path: Path to the binary event log on the DUT. Defaults to
            the security filesystem IML exported by the ``event_log.bin`` upload action.
        """
        with allure.step(f'parse the TPM event log at {event_log_path}'):
            return self.engine.run_cmd(f'sudo tpm2_eventlog {event_log_path}')

    def event_log_contains(self, marker: str,
                           event_log_path: str = TPM_EVENT_LOG_SECURITYFS_PATH) -> bool:
        """
        Return True if the parsed TPM event log contains ``marker`` (case-insensitive).

        Used to assert boot measurements are present, e.g. the ``INITRD`` event.
        """
        with allure.step(f'check TPM event log contains "{marker}"'):
            return marker.lower() in self.get_event_log(event_log_path).lower()

    def is_tpm_lockout_counter_cleared(self) -> Tuple[bool, str]:
        expected_counter_hex = '0x0'
        with allure.step(f'check if tpm lockout counter is cleared: {expected_counter_hex}'):
            actual_counter_hex = \
                self.engine.run_cmd('sudo tpm2 getcap properties-variable | grep TPM2_PT_LOCKOUT_COUNTER:').split(':')[
                    1].strip()
            return actual_counter_hex == expected_counter_hex, actual_counter_hex

    def clear_tpm_lockout_counter(self):
        with allure.step('clear tpm lockout counter'):
            self.engine.run_cmd('sudo tpm2_dictionarylockout --setup-parameters --clear-lockout')

    def is_host_tpm_dir_exists(self) -> bool:
        with allure.step('check if system tpm directory exists'):
            out = self.engine.run_cmd('sudo ls /host/tpm')
            return out != '' and PATH_NO_EXIST_ERR not in out

    def get_tpm_cipher(self):
        with allure.step('get tpm cipher'):
            salt = "1300NVOS-BMC-USER-Const"
            file_name = "kdfconst.bin"
            self.engine.run_cmd(f'echo {salt} | xxd -r -p > {file_name}')
            return \
                (self.engine.run_cmd(
                    f'sudo tpm2_createprimary -C o -G aes256cfb -u {file_name} | grep symcipher:')).split(
                    ':')[1].strip()

    def get_bmc_admin_password_from_tpm(self):
        with allure.step('get bmc admin password from tpm'):
            tpm_pass = self.engine.run_cmd('sudo python3 -c "from sonic_platform.bmc import BMC; print(BMC(\'10.0.1.1\').get_login_password())"')
            return tpm_pass.strip()

    def get_sed_password_primary_bank(self, device: BaseDevice):
        return self._get_sed_password(PRIMARY_SED_TPM_BANK, device)

    def get_sed_password_secondary_bank(self, device: BaseDevice):
        return self._get_sed_password(SECONDARY_SED_TPM_BANK, device)

    def set_sed_password_primary_bank(self, new_password: str, device: BaseDevice):
        return self._store_sed_password(PRIMARY_SED_TPM_BANK, new_password, device)

    def set_sed_password_secondary_bank(self, new_password: str, device: BaseDevice):
        return self._store_sed_password(SECONDARY_SED_TPM_BANK, new_password, device)

    """ Helper methods """

    def _get_sed_password(self, tpm_bank_context: str, device: BaseDevice):
        with allure.step('get sed password from tpm'):
            if self._require_tpm_pass(device):
                hex_pass = self.tpm_pass.encode().hex()
                return self.engine.run_cmd(f"sudo tpm2_unseal -c '{tpm_bank_context}' -p 'hex:{hex_pass}'")
            return self.engine.run_cmd(f"sudo tpm2_unseal -c '{tpm_bank_context}'")

    def _store_sed_password(self, tpm_bank_context: str, sed_password: str, device: BaseDevice):
        with allure.step('store sed password in tpm bank'):
            self.engine.run_cmd(f'sudo tpm2_evictcontrol -C o -c "{tpm_bank_context}" > /dev/null 2>&1')
            self.engine.run_cmd('sudo tpm2_createprimary -C o --key-algorithm=rsa --key-context=prim.ctx > /dev/null 2>&1')
            tpm2_create_cmd = "sudo tpm2_create -g sha256 -u seal.pub -r seal.priv -C prim.ctx"
            if self._require_tpm_pass(device):
                self.engine.run_cmd(f"echo '{sed_password}' | {tpm2_create_cmd} -p '{self.tpm_pass}' -i - > /dev/null 2>&1")
            else:
                self.engine.run_cmd(f"echo '{sed_password}' | {tpm2_create_cmd} -i - > /dev/null 2>&1")
            self.engine.run_cmd('sudo tpm2_load -C prim.ctx -u seal.pub -r seal.priv -n seal.name -c seal.ctx')
            self.engine.run_cmd(f'sudo tpm2_evictcontrol -C o -c seal.ctx "{tpm_bank_context}"')
            self.engine.run_cmd('sudo rm -f seal.* prim.ctx')

    def _is_sys_tpm_dir_exists(self) -> bool:
        with allure.step('check if system tpm directory exists'):
            out = self.engine.run_cmd('sudo ls /sys/class/tpm/tpm0')
            return out != '' and PATH_NO_EXIST_ERR not in out

    def _is_dev_tpm_dir_exists(self) -> bool:
        with allure.step('check if tpm is enabled'):
            out = self.engine.run_cmd('ls /dev/ | grep tpm')
            return out != '' and PATH_NO_EXIST_ERR not in out

    def _is_tpm_tools_installed(self) -> bool:
        with allure.step('check if tpm tools package is installed'):
            return self.engine.run_cmd('dpkg -l | grep tpm2-tools') != ''

    def _is_tpm_capabilities_ok(self) -> bool:
        with allure.step('check if tpm capabilities are ok'):
            with allure.step('run tpm getcap'):
                out = self.engine.run_cmd('sudo tpm2_getcap properties-variable')
                logging.info(f'getcap output:\n{out}')
                if CMD_NOT_FOUND_ERR in out:
                    return False
            with allure.step('parse output to dict'):
                parsed_out = parse_tpm_output(out)
            with allure.step('check if "TPM2_PT_STARTUP_CLEAR" in output'):
                if 'TPM2_PT_STARTUP_CLEAR' not in parsed_out:
                    return False
            with allure.step('check target attributes'):
                target_attributes = parsed_out['TPM2_PT_STARTUP_CLEAR']
                return 'phEnable' in target_attributes and target_attributes['phEnable'] == '1' \
                       and 'shEnable' in target_attributes and target_attributes['shEnable'] == '1' \
                       and 'ehEnable' in target_attributes and target_attributes['ehEnable'] == '1'

    def _is_tpm_provisioned(self) -> bool:
        return True  # TODO: complete


def parse_tpm_output(tpm_output: str) -> Dict[str, Any]:
    output_lines = tpm_output.split('\n')

    parsed_dict = {}
    current_section = None

    for raw_line in output_lines:
        line = raw_line.strip()
        if line.strip() == '':
            continue
        if line.endswith(':'):
            current_section = line.strip()[:-1]
            parsed_dict[current_section] = {}
        elif current_section is not None:
            key, value = line.strip().split(':')
            parsed_dict[current_section][key.strip()] = value.strip()

    return parsed_dict
