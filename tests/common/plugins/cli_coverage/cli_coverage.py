import json
import logging
import threading

from ngts.tools.infra import get_infra_type, CANONICAL_INFRA_TYPE

logger = logging.getLogger()
table_data_lock = threading.Lock()

class CliCoverage:

    def __init__(self, request):
        self.request = request
        self.dut_engine = None
        self.is_canonical_setup = get_infra_type(self.request) == CANONICAL_INFRA_TYPE
        self.get_dut_engine()
        self.config_db = {}


    def get_dut_engine(self):
        """
        Determines CLI command execution based on if setup is community or canonical
        """
        if self.is_canonical_setup:
            self.dut_engine = self.request.getfixturevalue('engines').dut.run_cmd
        else:
            self.dut_engine = self.request.getfixturevalue('duthost').shell


    def get_config_data(self):
        with table_data_lock:
            config_db = self.dut_engine("sudo show runningconfiguration all")
            if self.is_canonical_setup:
                self.config_db = json.loads(config_db)
            else:
                self.config_db = json.loads(str(config_db.get('stdout')))
            return self.config_db


    def parse_table_data(self, table, commands_set, commands_list):
        """
        Parse dict-like object of running configuration to executed commands list
        :param commands_list: list of executed commands
        :param commands_set: set of all commands previously executed in session
        :param table: table of running configuration
        :param json_format: dict of json format
        """
        for section, value in table.items():
            if not isinstance(value, dict):
                continue
            for subsection, data in value.items():
                if not isinstance(data, dict):
                    continue
                for attribute, content in data.items():
                    key_list = subsection.split('|')
                    if len(key_list) > 1:
                        keys = ['<key' + str(x) + '>' for x in range(1, (len(key_list) + 1))]
                    else:
                        keys = ['<key>']
                    command = f'{section} {":".join(keys)} {attribute} <attribute>'
                    section_json = {
                        "command executed": f'{section} {subsection} {attribute} {content}',
                        "command": f'{section} {":".join(keys)} {attribute} <attribute>'}
                    if command not in commands_set:
                        commands_set.add(command)
                        commands_list.append(section_json)

    def save_hit_list(self, table_parse, commands_set, commands_list):
        """
        Run parse_table_data
        :param commands_list: list of executed commands
        :param commands_set: set of all commands previously executed in session
        :param test_status: status of test function
        :param table_parse: parsed table of commands
        :param start_time: start test time in Unix format
        """
        self.parse_table_data(table_parse, commands_set, commands_list)

