import logging
import json
import re
from typing import List

from .ResultObj import ResultObj
from ngts.tools.test_utils import allure_utils as allure
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import IbInterfaceConsts
from ...nvos_constants.constants_nvos import OutputFormat, ApiType, ConfState, SystemConsts

logger = logging.getLogger()


class OutputParsingTool:

    @staticmethod
    def parse_show_output_to_field_names(output: str, output_format=OutputFormat.json, field_name_dict=None
                                         ) -> ResultObj:
        """Parses the output of generic `nv show` commands to a list/set of the fields (column headers)"""
        if output_format == OutputFormat.auto:
            return OutputParsingTool.parse_auto_output_to_field_names(output, field_name_dict)
        elif output_format == OutputFormat.json:
            as_dict = OutputParsingTool.parse_json_str_to_dictionary(output).get_returned_value()
            result = tuple(as_dict.values())[0].keys()
            return ResultObj(True, returned_value=result)
        else:
            raise NotImplementedError(f"No parser implemented for {output_format} output format")

    @staticmethod
    def parse_show_output_to_dict(output: str, output_format=OutputFormat.json, field_name_dict=None) -> ResultObj:
        """Parses the output of generic `nv show` commands to a dict"""
        if output_format == OutputFormat.auto:
            return OutputParsingTool.parse_auto_output_to_dict(output, field_name_dict)
        elif output_format == OutputFormat.json:
            return OutputParsingTool.parse_json_str_to_dictionary(output)
        else:
            raise NotImplementedError(f"No parser implemented for {output_format} output format")

    @staticmethod
    def parse_show_interface_output_to_dictionary(output_json):
        """
        Creates a dictionary according to provided JSON output of "show interface <name>"
        :param output_json: json output
        :return: a dictionary

                 Example of the input json:


                 {
                    "description": "...",
                    "link": {
                        "logical-state": "Initialized",
                        "physical-state": "LinkUp",
                        "ib-subnet": "infiniband-default",
                        "lanes": "4X",
                        ...
                        "speed": "100G",
                        "ib-speed: "edr",
                        ...
                        "state": {
                            "up": {}
                        },
                        ...
                        "stats": {
                            "in-bytes": 0,
                            ...
                        }
                    }
                    "pluggable": {
                        "cable-length": "2",
                        ...
                        "vendor-sn": "..."
                    }
                    "type": "swp"
                }


                Example of returned dictionary:
                {
                    "description": "...",
                    "link": {
                        "logical-state": "Initialized",
                        "physical-state": "LinkUp",
                        "ib-subnet": "infiniband-default",
                        "lanes": "4X",
                        ...
                        "speed": "100G",
                        "ib-speed: "edr",
                        ...
                        "state": "up"       <------------
                        },
                        ...
                        "stats": {
                            "in-bytes": 0,
                            ...
                        }
                    }
                    "pluggable": {
                        "cable-length": "2",
                        ...
                        "vendor-sn": "..."
                    }
                    "type": "swp"
                }
        """
        with allure.step('Create a dictionary according to provided JSON output of "show interface <name>" command'):
            output_dictionary = json.loads(output_json)

            if IbInterfaceConsts.LINK not in output_dictionary.keys():
                return ResultObj(False, "link field can't be found in the output")

            if IbInterfaceConsts.LINK_STATE not in output_dictionary[IbInterfaceConsts.LINK].keys():
                return ResultObj(False, "state field can't be found in the output")

            output_dictionary[IbInterfaceConsts.LINK][IbInterfaceConsts.LINK_STATE] =\
                list(output_dictionary[IbInterfaceConsts.LINK][IbInterfaceConsts.LINK_STATE].keys())[0]

            return ResultObj(True, "", output_dictionary)

    @staticmethod
    def parse_show_interface_link_output_to_dictionary(output_json):
        """
        Creates a dictionary according to provided JSON output of "show interface <name> link"
            :param output_json: json output
            :return: a dictionary

                     Example of the input json:

                     {
                        "logical-state": "Initialized",
                        "physical-state": "LinkUp",
                        "ib-subnet": "infiniband-default",
                        "lanes": "4X",
                        ...
                        "speed": "100G",
                        "ib-speed: "edr",
                        ...
                        "state": {
                            "up": {}
                        },
                        ...
                        "stats": {
                            "in-bytes": 0,
                            ...
                    }



                    Output:

                    {
                        "logical-state": "Initialized",
                        "physical-state": "LinkUp",
                        "ib-subnet": "infiniband-default",
                        "lanes": "4X",
                        ...
                        "speed": "100G",
                        "ib-speed: "edr",
                        ...
                        "state": "up",
                        ...
                        "stats": {
                            "in-bytes": 0,
                            ...
                        }

        """
        with allure.step('Create a dictionary according to provided JSON output of "show interface link" command'):

            output_json = output_json.replace('\\r', '\r').replace('\\n', '\n')
            start_pos = output_json.find('{')
            end_pos = output_json.rfind('}') + 1
            if start_pos >= 0 and end_pos > start_pos:
                output_json = output_json[start_pos:end_pos]

            output_dictionary = json.loads(output_json)

            if IbInterfaceConsts.LINK_STATE not in output_dictionary.keys():
                return ResultObj(False, "state field can't be found in the output")

            output_dictionary[IbInterfaceConsts.LINK_STATE] = \
                list(output_dictionary[IbInterfaceConsts.LINK_STATE].keys())[0]

            return ResultObj(True, "", output_dictionary)

    @staticmethod
    def parse_show_interface_pluggable_output_to_dictionary(output_json):
        """
        Creates a dictionary according to provided JSON output of "show interface <name> pluggalbe"
        :param output_json: json output
        :return: a dictionary

                     Example:

                     {
                        "cable-length": "2",
                        ...
                        "vendor-sn": "..."
                    }

        """
        with allure.step('Create a dictionary according to provided JSON output of "show interface pluggable" command'):

            output_json = output_json.replace('\\r', '\r').replace('\\n', '\n')
            start_pos = output_json.find('{')
            end_pos = output_json.rfind('}') + 1
            if start_pos >= 0 and end_pos > start_pos:
                output_json = output_json[start_pos:end_pos]

            output_dictionary = json.loads(output_json)
            return ResultObj(True, "", output_dictionary)

    @staticmethod
    def parse_show_interface_stats_output_to_dictionary(output_json):
        """
        Creates a dictionary according to provided JSON output of "show interface <name> link stats"
        :param output_json: json output
        :return: a dictionary
        """
        with allure.step('Create a dictionary according to provided JSON output of "show interface stats" command'):
            output_dictionary = json.loads(output_json)
            return ResultObj(True, "", output_dictionary)

    @staticmethod
    def parse_show_all_interfaces_output_to_dictionary(output_json, require_non_empty=False):
        """
         Creates a dictionary according to provided JSON output of "show interface"
         :param output_json: json output
         :param require_non_empty: when True, fail on empty stdout or an empty
             parsed interface map (avoids masking a failed `nv show interface`).
         :return: a dictionary

         Example:
         {                                                {
             "port1": {                                     "port1": {
                 "link": {                                      "mtu": 1500
                     "mtu": 1500,                               "speed": "1G"
                     "speed": "1G",    ---------->              "state": "up"
                     "state": {                                 "type": "ib"
                        up": {}                              },
                     }                                       ...
                 },                                        }
                 "type": "ib"
             }
            ...
         }

         """
        with allure.step('Create a dictionary according to provided JSON output of "show interface" command'):
            if require_non_empty and (not output_json or not str(output_json).strip()):
                return ResultObj(
                    False,
                    "NVUE 'show interface' returned empty output - treating as a failure "
                    "rather than 'no interfaces' (would mask a failed query).",
                    {},
                )
            output_dictionary = json.loads(output_json)
            dictionary_to_return = {}

            for port_name in output_dictionary.keys():

                if IbInterfaceConsts.LINK not in output_dictionary[port_name].keys() or \
                        IbInterfaceConsts.TYPE not in output_dictionary[port_name].keys():
                    logger.warning(f"'link'/'type' fields can't be found for port {port_name}")
                    continue

                if IbInterfaceConsts.LINK_STATE not in output_dictionary[port_name]["link"].keys() and \
                        (IbInterfaceConsts.LINK_ADMIN_STATUS not in output_dictionary[port_name]["link"].keys() or
                         IbInterfaceConsts.LINK_OPER_STATUS not in output_dictionary[port_name]["link"].keys()):
                    logger.warning(f"'state' fields can't be found for port {port_name}")
                    continue

                dictionary_to_return[port_name] = {}

                for link_field in output_dictionary[port_name][IbInterfaceConsts.LINK].keys():

                    if link_field == IbInterfaceConsts.LINK_STATE:
                        tmp_val = list(output_dictionary[port_name][IbInterfaceConsts.LINK][IbInterfaceConsts.LINK_STATE].
                                       keys())[0]
                        dictionary_to_return[port_name][IbInterfaceConsts.LINK_STATE] = tmp_val
                    else:
                        dictionary_to_return[port_name][link_field] = \
                            output_dictionary[port_name][IbInterfaceConsts.LINK][link_field]

                if IbInterfaceConsts.LINK_STATE not in output_dictionary[port_name]["link"].keys():
                    if output_dictionary[port_name][IbInterfaceConsts.LINK][IbInterfaceConsts.LINK_ADMIN_STATUS] == "up" and \
                            output_dictionary[port_name][IbInterfaceConsts.LINK][IbInterfaceConsts.LINK_OPER_STATUS] == "up":
                        dictionary_to_return[port_name][IbInterfaceConsts.LINK_STATE] = "up"
                    else:
                        dictionary_to_return[port_name][IbInterfaceConsts.LINK_STATE] = "down"

                dictionary_to_return[port_name][IbInterfaceConsts.TYPE] = \
                    output_dictionary[port_name][IbInterfaceConsts.TYPE]

                if output_dictionary[port_name][IbInterfaceConsts.TYPE] == IbInterfaceConsts.IB_PORT_TYPE and \
                        IbInterfaceConsts.DESCRIPTION in output_dictionary[port_name].keys():
                    dictionary_to_return[port_name][IbInterfaceConsts.DESCRIPTION] = \
                        output_dictionary[port_name][IbInterfaceConsts.DESCRIPTION]

            if require_non_empty and not dictionary_to_return:
                return ResultObj(
                    False,
                    f"NVUE 'show interface' parsed to an empty interface map from: {output_json!r}",
                    dictionary_to_return,
                )

            return ResultObj(True, "", dictionary_to_return)

    @staticmethod
    def parse_json_str_to_dictionary(output_json):
        """
        Creates a dictionary according to provided JSON string
        :param output_json: json output
        :return: a dictionary
        """
        if isinstance(output_json, dict):
            return ResultObj(True, "", output_json)  # if output is already dict -> do nothing

        if '2004l' in output_json:
            output_json = ''.join(output_json.split('\n')[1:])
        if output_json == '{}' or output_json == '':
            return ResultObj(True, "", {})
        output_json = output_json.strip()
        if (output_json[0] != '{' or output_json[-1] != '}') and (output_json[0] != "[" or output_json[-1] != "]"):  # Need to parse output from serial engine
            start = output_json.find('{')
            end = output_json.rfind('}')

            if start != -1 and end != -1 and start < end:
                output_json = output_json[start:end + 1]
                output_json = output_json.replace("\\r", "").replace("\\n", "")
        logger.info('Create a dictionary according to provided JSON string')
        try:
            return ResultObj(True, "", json.loads(output_json))
        except json.JSONDecodeError as err:
            return ResultObj(False, f"Json could not parse to dict: \n{err}")

    @staticmethod
    def parse_show_files_to_dict(output_json) -> ResultObj:
        """
        Parses the output of some `show ... files` commands.
        Sample input string:    {"1":
                                    {"path": "/var/my_file"},
                                "2":
                                    {"path": "/var/second.txt"} }
        Output will be {"1": "/var/my_file",
                        "2": "/var/second.txt"}
        """
        dict_from_json = OutputParsingTool.parse_json_str_to_dictionary(output_json).get_returned_value()
        with allure.step("Parsing show-files output to dict"):
            result = {k: v['path'] for k, v in dict_from_json.items()}
            logger.info(result)
        return ResultObj(True, "", result)

    @staticmethod
    def parse_show_files_to_names(output_json) -> ResultObj:
        """Like parse_show_files_to_dict but returns a list of the keys."""
        as_dict = OutputParsingTool.parse_show_files_to_dict(output_json).get_returned_value()
        result = list(as_dict.keys())
        logger.info(f"File names: {result}")
        return ResultObj(True, "", result)

    @staticmethod
    def parse_config_history(output_json):
        """
        Parse the output from 'nv config history --output json' command.

        :param output_json: the output after running nv config history --output json
        :return: list of dictionaries
        Example:
        input =
            {
                'rev_428_apply_1': {
                    'date': '2025-06-03T07:36:14+03:00',
                    'message': 'Config update by admin',
                    'reason': 'Config update',
                    'type': 'CLI',
                    'user': 'admin'
                },
                '427': {
                    'date': '2025-06-03T07:36:09+03:00',
                    'message': 'Config update by admin',
                    'reason': 'Config update',
                    'type': 'CLI',
                    'user': 'admin'
                }
            }

        output:
        [
            {
                'ref': 'rev_428_apply_1',
                'date': '2025-06-03T07:36:14+03:00',
                'message': 'Config update by admin',
                'reason': 'Config update',
                'type': 'CLI',
                'user': 'admin'
            },
            {
                'ref': '427',
                'date': '2025-06-03T07:36:09+03:00',
                'message': 'Config update by admin',
                'reason': 'Config update',
                'type': 'CLI',
                'user': 'admin'
            }
        ]
        """
        with allure.step('Create a list of dictionaries according to provided JSON output of "config history" command'):
            if output_json == '' or output_json == []:
                return ResultObj(True, "", [])

            if output_json.startswith("Currently pending"):
                output_json = output_json.split('\n', 1)[1]

            output_dict = json.loads(output_json)
            list_to_return = [{'ref': key, **value} for key, value in output_dict.items()]

        return ResultObj(True, "", list_to_return)

    @staticmethod
    def parse_lslogins_cmd(lslogins_output):
        """

            Example:
            Input:
                    Username:                           admin
                    UID:                                1000
                    Gecos field:                        System Administrator
                    Home directory:                     /home/admin
                    Shell:                              /bin/bash
                    No login:                           no
                    Primary group:                      admin
                    GID:                                1000
                    Supplementary groups:               nvapply,adm,sudo,docker,redis,nvset
                    Supplementary group IDs:            996,4,27,999,1001,997
                    Last login:                         15:32
                    Last terminal:                      pts/2
                    Last hostname:                      10.228.130.172
                    Hushed:                             no
                    Running processes:                  9

                    Last logs:
                    15:01 sudo[202963]: pam_unix(sudo:session): session closed for user root
                    15:03 sshd[202910]: Received disconnect from 10.228.130.172 port 50244:11: disconnected by user
                    15:03 sshd[202910]: Disconnected from user admin 10.228.130.172 port 50244
            Output:
                {
                    Username: admin,
                    UID: 1000
                    Gecos field: System Administrator
                    ...
                    Running processes: 9
                    Last logs: [ ]
        :param lslogins_output:
        :return:
        """
        if 'lslogins: cannot found ' in lslogins_output:
            return ResultObj(True, lslogins_output, lslogins_output)
        result = {}
        lines = lslogins_output.splitlines()
        logs = lines[lines.index('') + 1:]
        lines = lines[:lines.index('')]

        for line in lines:
            splitted_line = line.split(':', 1)
            result[splitted_line[0]] = splitted_line[1].strip()

        result['Last logs'] = logs[1:]
        return ResultObj(True, "", result)

    @staticmethod
    def parse_linux_cmd_output_to_dic(output):
        """
        ****** THE FOLLOWING OUTPUT STR (example from running 'timedatectl' command):
                   Local time: Mon 2023-01-16 18:53:18 IST
               Universal time: Mon 2023-01-16 16:53:18 UTC
                     RTC time: Mon 2023-01-16 16:53:18
                    Time zone: Asia/Jerusalem (IST, +0200)
    System clock synchronized: no
                  NTP service: n/a
              RTC in local TZ: no

        ****** WILL BECOME THE FOLLOWING DICT:
        {
            'Local time': 'Mon 2023-01-16 18:53:18 IST',
            'Universal time': 'Mon 2023-01-16 16:53:18 UTC'
            ...
        }
        """
        dic = {}

        # split string by '\n'
        output_rows = output.split('\n')

        # parse each row to key, val
        for row in output_rows:
            colon_idx = row.find(':')
            k = row[0: colon_idx].strip()
            v = row[colon_idx + 1:].strip()
            dic[k] = v

        return ResultObj(True, "", dic)

    @staticmethod
    def _str_multi_split(s: str, spans):
        return [s[span[0]:span[1] + 1].strip() for span in spans]

    @staticmethod
    def _get_field_titles_and_indices(output_lines: List[str], name_dict=None):
        with allure.step("Parsing field names"):
            name_dict = name_dict or {}
            if set(output_lines[1]) != {' ', '-'}:
                return None, None
            indices = [m.span() for m in re.finditer(r"-+", output_lines[1])]
            titles = OutputParsingTool._str_multi_split(output_lines[0], indices)[1:]
            titles = [name_dict.get(name) or name.lower().replace(' ', '-') for name in titles]
            logger.info(f"Inferred field names: {titles}")
            return titles, indices

    @staticmethod
    def parse_auto_output_to_field_names(output: str, name_dict=None) -> ResultObj:
        output_lines = output.splitlines()
        result, _ = OutputParsingTool._get_field_titles_and_indices(output_lines, name_dict)
        if result:
            return ResultObj(True, "", result)
        else:
            return ResultObj(False, f"Parsing error: expected the second line of output to contain only '-' and "
                             f"spaces, but line is {output_lines[1]}")

    @staticmethod
    def parse_auto_output_to_dict(output: str, field_name_dict=None, only_operational=True) -> ResultObj:
        """
        Example - output of `nv show platform inventory --output auto`:
                Hw version  Model            Serial        State  Type
        ------  ----------  ---------------  ------------  -----  ------
        FAN1/1  N/A         N/A              N/A           ok     fan
        FAN1/2  N/A         N/A              N/A           ok     fan
        ...

        Will be transformed to the following dict:  (if we set field_name_dict={'Hw version': 'hardware-version'} )
        {"FAN1/1":
            {"hardware-version": "N/A", "model": "N/A", "serial": "N/A", "state": "ok", "type": "fan"}
         "FAN2/2": {...}
        ...}

        If only_operational==True and the output is something like:
             operational  applied
        ---  -----------  -------
        abc  11           90
        xyz  22

        Then the returned dict is {"abc": 11, "xyz": 22}.

        Note: This function infers the fields' lengths according to the second output line ("----  ------ --- " ...)
        """
        with allure.step("Parsing auto output into dict"):
            field_name_dict = field_name_dict or {}
            output_lines = output.splitlines()
            if set(output_lines[1]) <= {' ', '='}:
                output_lines = output_lines[2:]
                if any([line for line in output_lines if set(line) <= {'=', ' '}]):
                    raise NotImplementedError(
                        "Output contains multiple sections but the function currently supports only a single section")

            field_names, field_indices = OutputParsingTool._get_field_titles_and_indices(output_lines, field_name_dict)
            if not field_names:
                return ResultObj(False, f"Parsing error: expected the second line of output to contain only '-' and "
                                 f"spaces, but line is {output_lines[1]}")

            with allure.step("Parsing content"):
                result = {}
                for line in output_lines[2:]:
                    item, *values = OutputParsingTool._str_multi_split(line, field_indices)
                    result[item] = dict(zip(field_names, values))
                if only_operational and (ConfState.OPERATIONAL in field_names):
                    result = {k: v[ConfState.OPERATIONAL] for k, v in result.items()}
                logger.info(f"Returned dict:\n{result}")
                return ResultObj(True, "", result)

    @staticmethod
    def get_reboot_reason_and_user_from_system_events(system):
        events = OutputParsingTool.parse_json_str_to_dictionary(
            system.events.show_events_last_recent_entries(SystemConsts.SYSTEM_LAST_EVENT, '10000')
        ).get_returned_value()

        # Filter reboot events with IDs as integers
        reboot_events = {
            int(event_id): data for event_id, data in events.items()
            if 'System reboot occurred' in data.get('text', '')
        }

        if not reboot_events:
            return '', ''

        # Get the latest event ID
        latest_event_id = max(reboot_events)
        latest_event_text = reboot_events[latest_event_id].get('text', '')

        try:
            # Extract reboot reason
            reason = latest_event_text.split("reason:", 1)[1].split(",", 1)[0].strip()
        except (IndexError, ValueError) as e:
            logger.error(f"Failed to extract reboot reason from event text: '{latest_event_text}'. Exception: {e}")
            raise ValueError(f"Reboot reason not found in event text: '{latest_event_text}'") from e

        try:
            # Extract user who performed the reboot
            user = latest_event_text.split("performed by user:", 1)[1].split(",", 1)[0].strip()
        except (IndexError, ValueError) as e:
            logger.error(f"Failed to extract reboot user from event text: '{latest_event_text}'. Exception: {e}")
            raise ValueError(f"Reboot user not found in event text: '{latest_event_text}'") from e

        return reason, user

    def run_iostat_and_parse(engine):
        with allure.step("Execute the iostat command and parse the output into a dictionary"):
            iostat_output = engine.run_cmd('iostat -o JSON')
            cleaned_output = json.loads(iostat_output)
            host = cleaned_output['sysstat']['hosts']
            disks = host[0]['statistics'][0]    # list of all devices
            result = {}
            for disk in disks['disk']:
                disk_device = disk['disk_device']
                result[disk_device] = {key: disk[key] for key in disk if key != 'disk_device'}

            return result

    @staticmethod
    def parse_dscp_value_from_acl(engine, acl, acl_id, rule_id):
        with allure.step("Execute acl dscp show commands and parse output into dictionary"):
            acl.set(acl_id).verify_result()
            acl_id_obj = acl.acl_id[acl_id]
            rule_id_1_obj = acl_id_obj.rule.rule_id[rule_id]
            action_show = rule_id_1_obj.action.parse_show()
            return action_show['set']
