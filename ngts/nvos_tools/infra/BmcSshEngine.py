from devts.infra.tools.connection_tools.linux_ssh_engine import LinuxSshEngine
from devts.infra.tools.general_constants.constants import DefaultConnectionValues
from ngts.nvos_tools.infra.PexpectTool import PexpectTool
from ngts.nvos_tools.infra.SshCmdBuilder import SshCmdBuilder, SshPassCmdBuilder
from ngts.tests_nvos.general.security.bmc.bmc_creds.constants import get_bmc_backup_password
from ngts.tools.test_utils import allure_utils as allure
import logging

logger = logging.getLogger()

PEXPECT_SSH_ERR = 'failed to start pexpect ssh session'
BMC_SESSION_ERR = 'failure in ssh session with BMC'

BMC_SELL_PROMPT_PATTERNS = ['.*@.*bmc.*#', '#']
SHELL_PROMPT_PATTERNS = DefaultConnectionValues.DEFAULT_PROMPTS + ['#', r'\$']
ENTER_PASSWORD_PATTERNS = ['[Pp]assword:']
CHANGE_PASSWORD_PATTERNS = ['Enter.*old password:']

SSN_CMD = 'ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null {usr}@{ip}'

# LinuxSshEngine kwargs that BmcSshEngine does not implement (Y24).
_UNSUPPORTED_RUN_CMD_KWARGS = frozenset({'validate', 'print_output', 'max_loops'})


class BmcSshEngine:
    """
    This class provides ssh engine to internal BMC, using pexpect tool
    """

    def __init__(self, dut_engine: LinuxSshEngine, bmc_username, bmc_default_password, bmc_another_password='',
                 bmc_internal_ip='10.0.1.1'):
        """
        :param bmc_internal_ip: CPU-side address of the BMC over the internal link.
            IB/NVOS platforms use 10.0.1.1 (default); CL/SPC6 reaches the BMC over
            usb0 at ``BMC_IP`` (see ``BmcCliTool``) -- pass that explicitly for CL setups.
        """
        self._session: PexpectTool = None
        self.dut_engine: LinuxSshEngine = dut_engine
        self.bmc_username = bmc_username
        self.bmc_default_password = bmc_default_password
        self.bmc_another_password = bmc_another_password
        self.bmc_internal_ip = bmc_internal_ip

    def run_cmd(self, cmd, **kwargs) -> str:
        """
        Run command on BMC via pexpect SSH session.

        Supported kwargs: ``sanitized_cmd`` (Allure/log title without secrets).
        Unsupported LinuxSshEngine kwargs (``validate``, ``print_output``,
        ``max_loops``) raise ``TypeError`` so callers do not silently lose
        validation (full parity deferred).
        """
        bad = sorted(_UNSUPPORTED_RUN_CMD_KWARGS.intersection(kwargs))
        if bad:
            raise TypeError(
                f"BmcSshEngine.run_cmd does not implement kwargs {bad}. "
                f"supported optional kwargs: ['sanitized_cmd']"
            )
        unknown = sorted(set(kwargs) - {'sanitized_cmd'})
        if unknown:
            raise TypeError(
                f"BmcSshEngine.run_cmd got unexpected kwargs {unknown}. "
                f"supported optional kwargs: ['sanitized_cmd']"
            )
        exc: Exception = None
        try:
            if not self._session:
                self.connect()
            sanitized = kwargs.get('sanitized_cmd', None)
            log_cmd = cmd if sanitized is None else sanitized
            with allure.step(self._msg(f'run cmd: {log_cmd}')):
                self._session.sendline(cmd)
                res_index, out = self._session.expect_and_get_output(BMC_SELL_PROMPT_PATTERNS)
                assert res_index < len(BMC_SELL_PROMPT_PATTERNS), f'{BMC_SESSION_ERR}: did not get bmc shell prompt'
                return out
        except Exception as e:
            exc = e
            raise
        finally:
            if exc is not None:
                self.disconnect()

    def connect(self):
        with allure.step(self._msg('connect bmc session')):
            # Known gap: the BMC may reject a new session with "There were too
            # many logins for 'admin'." when prior sessions are still open. That
            # surfaces as a generic connect failure here; automatic
            # detection/backoff/cleanup of stale sessions is a follow-up.
            # Skip empty candidates (e.g. default bmc_another_password='').
            passwords = [
                pw for pw in (
                    get_bmc_backup_password(),
                    self.bmc_default_password,
                    self.bmc_another_password,
                )
                if pw
            ]
            if not passwords:
                raise RuntimeError(
                    f'{BMC_SESSION_ERR}: no non-empty BMC password candidates'
                )
            last_err = None
            for idx, pw in enumerate(passwords, start=1):
                try:
                    # Never put candidate passwords in Allure titles / logs.
                    with allure.step(self._msg(
                            f'ssh bmc using user: {self.bmc_username} '
                            f'(candidate {idx}/{len(passwords)})')):
                        session = self._start_new_bmc_ssh_session(pw)
                        self._session = session
                        return
                except Exception as err:
                    last_err = err
                    logger.warning(
                        "%s: BMC ssh candidate %s/%s failed (%s: %s)",
                        self.bmc_username, idx, len(passwords),
                        type(err).__name__, err,
                    )
                    if idx == len(passwords):
                        self.disconnect()
                        raise
            if last_err:
                raise last_err

    def disconnect(self):
        if self._session is not None:
            with allure.step(self._msg('disconnect bmc session')):
                self._session.close()
                self._session = None

    def _start_new_bmc_ssh_session(self, password) -> PexpectTool:
        with allure.step(self._msg('start pexpect ssh session to dut')):
            nos_session = self._start_ssh_to_nos_pexpect_session()
        with allure.step(self._msg('ssh bmc from within the dut ssh session')):
            bmc_session = self._connect_bmc_from_nos_pexpect_session(nos_session, password)
        return bmc_session

    def _start_ssh_to_nos_pexpect_session(self) -> PexpectTool:
        with allure.step(self._msg('send dummy cmd with devts engine to handle password change if needed')):
            self.dut_engine.run_cmd('echo "hi"')
        with allure.step(self._msg('start ssh pexpect session to nos')):
            ssh_cmd = SshPassCmdBuilder(self.dut_engine.username, self.dut_engine.password, self.dut_engine.ip,
                                        self.dut_engine.ssh_port).set_ssn().set_long_lasting_session().build()
            session = PexpectTool(ssh_cmd)
            self._expect_shell_prompt(session)
        return session

    def _connect_bmc_from_nos_pexpect_session(self, nos_shell: PexpectTool, password) -> PexpectTool:
        with allure.step('start ssh to bmc from nos shell'):
            ssn_cmd = SshCmdBuilder(self.bmc_username, self.bmc_internal_ip).set_ssn().build()
            nos_shell.sendline(ssn_cmd)
            res = nos_shell.expect(ENTER_PASSWORD_PATTERNS)
            assert res < len(ENTER_PASSWORD_PATTERNS), f'{PEXPECT_SSH_ERR}: no password prompt'
        with allure.step('enter password'):
            nos_shell.sendline(password)
            # On forced rotation, new password is get_bmc_backup_password() (lab env).
            self._handle_change_password_prompt(nos_shell, password)
            bmc_shell = nos_shell
        return bmc_shell

    def _handle_change_password_prompt(self, session: PexpectTool, old_password: str) -> str:
        expect = SHELL_PROMPT_PATTERNS + CHANGE_PASSWORD_PATTERNS
        res = session.expect(SHELL_PROMPT_PATTERNS + CHANGE_PASSWORD_PATTERNS)
        assert res < len(expect), f'{PEXPECT_SSH_ERR}: did not get expected prompt. expected: {expect}'
        if res < len(SHELL_PROMPT_PATTERNS):
            return old_password

        session.sendline(old_password)
        expect = ['Enter.*new password:']
        res = session.expect(expect)
        assert res < len(expect), f'{PEXPECT_SSH_ERR}: did not get expected prompt. expected: {expect}'

        new_password = get_bmc_backup_password()
        session.sendline(new_password)
        expect = ['Retype.*new password:']
        res = session.expect(expect)
        assert res < len(expect), f'{PEXPECT_SSH_ERR}: did not get expected prompt. expected: {expect}'

        session.sendline(new_password)
        self._expect_shell_prompt(session)

        return new_password

    def _expect_shell_prompt(self, session: PexpectTool):
        res = session.expect(SHELL_PROMPT_PATTERNS)
        assert res < len(SHELL_PROMPT_PATTERNS), f'{PEXPECT_SSH_ERR}: did not get shell prompt'

    def _msg(self, msg: str) -> str:
        return f'[{self.bmc_username}@bmc] {msg}'
