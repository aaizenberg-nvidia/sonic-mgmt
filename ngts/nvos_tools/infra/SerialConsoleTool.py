import time

from retry import retry

from devts.infra.tools.connection_tools.pexpect_serial_engine import PexpectSerialEngine
from devts.infra.tools.connection_tools.serial_engine_factory import SerialEngineFactory
from devts.infra.tools.connection_tools.utils import extend_rcon_with_ssh_options, generate_strong_password
from devts.infra.tools.general_constants.constants import DefaultConnectionValues
from ngts.tools.test_utils import allure_utils as allure


class SerialConsoleTool:
    TIME_FOR_LOGIN_PROMPT = 2

    # expect patterns
    SHELL_PROMPT_PATTERNS = DefaultConnectionValues.DEFAULT_PROMPTS
    LOGIN_PROMPT_PATTERN = 'login:'
    ENTER_PASSWORD_PATTERN = '[Pp]assword:'
    NEW_PASSWORD_PATTERN = '[Nn]ew password:'
    RETYPE_NEW_PASSWORD_PATTERN = '[Rr]etype new password:'
    APPLIED_PATTERN = 'applied'
    SAVED_PATTERN = 'saved'

    # nv command consts
    CONFIG_DETACH_CMD = 'nv config detach'
    CONFIG_APPLY_CMD = 'nv config apply -y'
    CONFIG_SAVE_CMD = 'nv config save'
    CHANGE_PW_CMD = 'nv set system aaa user {usr} password {pw}'
    DISABLE_PW_HARDENING_CMD = 'nv set system security password-hardening state disabled'
    ENABLE_PW_HARDENING_CMD = 'nv set system security password-hardening state enabled'

    @classmethod
    def get_serial_console_connection_command(cls, topology_obj, dut_alias='dut') -> str:
        serial_alias = dut_alias + "_serial"
        att = topology_obj.players[serial_alias]['attributes'].noga_query_data['attributes']
        if att['Specific'].get('is_air_resource'):
            raise ValueError(
                f"AIR resources have no rcon command — they reach the console over wss://. "
                f"Use SerialEngineFactory.create() instead. "
                f"(player={serial_alias}, url={att['Specific'].get('serial_console_url')})"
            )
        return extend_rcon_with_ssh_options(att['Specific']['serial_conn_cmd'])

    @classmethod
    def get_serial_console_session(cls, topology_obj, dut_alias='dut'):
        serial_alias = dut_alias + "_serial"
        att = topology_obj.players[serial_alias]['attributes'].noga_query_data['attributes']
        serial_engine = SerialEngineFactory.create(att, timeout=120)
        # we don't want to login to switch because we are doing remote reboot
        serial_engine.create_serial_engine(login_to_switch=False)
        return serial_engine

    @classmethod
    def exit_existing_login(cls, serial_engine: PexpectSerialEngine, num_logouts=3):
        with allure.step('hit logout multiple times to exit existing login'):
            for _ in range(num_logouts):
                serial_engine.serial_engine.sendcontrol('d')
                time.sleep(cls.TIME_FOR_LOGIN_PROMPT)
            time.sleep(cls.TIME_FOR_LOGIN_PROMPT)

    @classmethod
    def login_nos(cls, serial_engine: PexpectSerialEngine, username='', password='',
                  handle_change_password_prompt=True, start_login_tries=2):
        username = username or serial_engine.username
        password = password or serial_engine.password

        @retry(Exception, tries=start_login_tries, delay=5)
        def _start_login():
            with allure.step('hit enter to get login prompt'):
                serial_engine.run_cmd('', cls.LOGIN_PROMPT_PATTERN, 10)
            with allure.step(f'enter username: {username}'):
                serial_engine.run_cmd(username, cls.ENTER_PASSWORD_PATTERN, 10)

        _start_login()

        with allure.step(f'enter password: {password}'):
            out, idx = serial_engine.run_cmd(password, cls.SHELL_PROMPT_PATTERNS + [cls.NEW_PASSWORD_PATTERN], 10)
        # any shell-prompt match means login completed (PAM prints 'New password:'
        # before the MOTD, whose '#' in the kernel line matches SHELL_PROMPT_PATTERNS[1])
        if idx == len(cls.SHELL_PROMPT_PATTERNS) and handle_change_password_prompt:
            cls.handle_change_password_prompt(serial_engine)
        # we're logged in now; make auto_reconnect_on_eof re-login instead of
        # landing on a bare login prompt (engine was created with login_to_switch=False)
        serial_engine._create_engine_kwargs['login_to_switch'] = True

    @classmethod
    def handle_change_password_prompt(cls, serial_engine: PexpectSerialEngine, new_password='', restore_password=True):
        new_password = new_password or generate_strong_password()
        with allure.step(f'enter new password: {new_password}'):
            serial_engine.run_cmd(new_password, cls.RETYPE_NEW_PASSWORD_PATTERN, 10)
        with allure.step(f'retype new password: {new_password}'):
            serial_engine.run_cmd(new_password, cls.APPLIED_PATTERN)
        with allure.step('wait for shell prompt after password change'):
            # login flushes TTY input between the password change and spawning the
            # shell - anything sent before the prompt appears is silently discarded
            serial_engine.run_cmd('', cls.SHELL_PROMPT_PATTERNS)
        if restore_password:
            cls.restore_password_to_default(serial_engine)
            return serial_engine.password
        else:
            return new_password

    @classmethod
    def restore_password_to_default(cls, serial_engine: PexpectSerialEngine, save_config=True):
        with allure.step('detach config'):
            serial_engine.run_cmd(cls.CONFIG_DETACH_CMD, cls.SHELL_PROMPT_PATTERNS, 10)
        with allure.step('disable password hardening'):
            serial_engine.run_cmd(cls.DISABLE_PW_HARDENING_CMD, cls.SHELL_PROMPT_PATTERNS, 10)
            serial_engine.run_cmd(cls.CONFIG_APPLY_CMD, cls.APPLIED_PATTERN)
        with allure.step('restore password to default'):
            serial_engine.run_cmd(cls.CHANGE_PW_CMD.format(usr=serial_engine.username, pw=serial_engine.password),
                                  cls.SHELL_PROMPT_PATTERNS, 10)
            serial_engine.run_cmd(cls.CONFIG_APPLY_CMD, cls.APPLIED_PATTERN)
        with allure.step('enable password hardening'):
            serial_engine.run_cmd(cls.ENABLE_PW_HARDENING_CMD, cls.SHELL_PROMPT_PATTERNS, 10)
            serial_engine.run_cmd(cls.CONFIG_APPLY_CMD, cls.APPLIED_PATTERN)
        if save_config:
            with allure.step('save config'):
                serial_engine.run_cmd(cls.CONFIG_SAVE_CMD, cls.SAVED_PATTERN, 10)
