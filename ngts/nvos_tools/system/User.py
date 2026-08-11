import random

import allure

from devts.infra.tools.connection_tools.utils import generate_strong_password
from ngts.nvos_constants.constants_nvos import SystemConsts
from ngts.nvos_tools.infra.ConnectionTool import ConnectionTool
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.SendCommandTool import SendCommandTool
from ngts.nvos_tools.system.SpiffeId import SpiffeId
from ngts.nvos_tools.system.Ssh import *

logger = logging.getLogger()


class User(BaseComponent):
    def __init__(self, parent_obj=None):
        super().__init__(parent=parent_obj, path='/user')
        self.user_id: Dict[str, UserId] = DefaultDict(
            lambda user_id: UserId(parent=self, user_id=user_id))

    @staticmethod
    def get_lslogins(engine, username):
        return OutputParsingTool.parse_lslogins_cmd(
            engine.run_cmd('lslogins {username}'.format(username=username))).get_returned_value()

    @staticmethod
    def get_ssh_session_count(engine, username) -> int:
        """
        @summary: Count the login sessions of a user, as the number of distinct terminals it holds.

            Debian 13 dropped utmp, so 'who' is now a systemd-logind client and reports sessions
            utmp never held - terminal-less 'ssh host cmd' connections - and reports the same
            terminal twice for a nested connection. Counting raw lines therefore over-counts, and
            counts the sampling command itself. Counting distinct terminals restores what utmp
            used to measure: one line per real login (NVBug 6580053).
        """
        terminals_of_user = f"who | awk '$1 == \"{username}\"' | grep -oE 'pts/[0-9]+|ttyS[0-9]+'"
        output = engine.run_cmd(f'{terminals_of_user} | sort -u | wc -l')
        return int(output.strip())

    def set_new_user(self, username=None, password=None, role=None, engine=None, hashed_password=None, apply=False):
        username = username if username else User.generate_username()
        password = password if password else generate_strong_password()

        if role:
            self.user_id[username].set('role', role, dut_engine=engine).verify_result()
        if hashed_password:
            self.user_id[username].set('hashed-password', hashed_password, dut_engine=engine, apply=apply,
                                       ask_for_confirmation=True).verify_result()
        else:
            self.user_id[username].set('password', password, dut_engine=engine, apply=apply,
                                       ask_for_confirmation=True).verify_result()

        return username, password

    def create_new_user_connection(self, username=None, password=None, role=None, engine=None):
        username, password = self.set_new_user(username, password, role, engine, True)
        return ConnectionTool.create_ssh_conn(TestToolkit.get_engine().ip, username, password).get_returned_value()

    def action_disconnect(self, engine=None):
        engine = engine if engine else TestToolkit.get_engine()
        with allure.step('Disconnect all users'):
            return SendCommandTool.execute_command_expected_str(self._cli_wrapper.action_disconnect,
                                                                "Action succeeded", engine,
                                                                self.get_resource_path().replace('/', ' '))

    @staticmethod
    def generate_username(is_valid=True, random_length=True, length=SystemConsts.USERNAME_MAX_LEN,
                          not_usernames=[SystemConsts.DEFAULT_USER_ADMIN, SystemConsts.DEFAULT_USER_MONITOR],
                          characters=SystemConsts.USERNAME_VALID_CHARACTERS):
        """
        if valid will generate username starts with _ or A-Z or a-z
        else : the username will start with 0-9 or *
        :param is_valid: if is True, the username need to be valid, False- the username invalid.
        :param random_length: True if the username length need to be randomly
        :param length: the max length of the username if random_length is True, else - this is the length of the username
        :param not_usernames: list of the usernames that we don't want to generate.
        :param characters: random username with those characters
        :return:
        """
        with allure.step('generate random username'):
            if random_length:
                name_len = random.randint(3, length)
                logger.info('the username length will be : {len}'.format(len=name_len))
            else:
                name_len = length

            name = "".join(random.choice(characters) for _ in range(name_len))
            while name in not_usernames:
                name = "".join(random.choice(characters) for _ in range(name_len))

            if not is_valid:
                name = str(random.choice(SystemConsts.USERNAME_INVALID_CHARACTERS)) + name[:-1]
            logger.info('generated username is : {username}'.format(username=name))
            return name


class UserId(BaseComponent):
    def __init__(self, parent, user_id):
        super().__init__(parent=parent, path=f'/{user_id}')
        self.username = user_id
        self.ssh = Ssh(self)
        self.role = BaseComponent(self, path='/role')
        self.spiffe_id = SpiffeId(self)

    def action_disconnect(self, engine=None):
        engine = engine if engine else TestToolkit.get_engine()
        with allure.step('Disconnect all users'):
            return SendCommandTool.execute_command_expected_str(self._cli_wrapper.action_disconnect,
                                                                "Action succeeded", engine,
                                                                self.get_resource_path().replace('/', ' '))

    def verify_user_field(self, field, expected_value, show_cmd_engine=None):
        with allure.step(f'verify that field {field} of user "{self.username}" is "{expected_value}"'):
            output = OutputParsingTool.parse_json_str_to_dictionary(self.show(dut_engine=show_cmd_engine)).verify_result()
            assert output[field] == expected_value, f"Actual {field} of user {self.username} is {output[field]}. " \
                f"Expected: {expected_value}"
