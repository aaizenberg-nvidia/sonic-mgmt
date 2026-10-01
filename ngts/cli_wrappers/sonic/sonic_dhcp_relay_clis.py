import logging
import re
import ipaddress
import json

from ngts.helpers.config_db_utils import save_config_db_json, save_config_into_json
from tests.common.utilities import wait_until

logger = logging.getLogger()


class SonicDhcpRelayCli:

    def __new__(cls, **kwargs):
        branch = kwargs['branch']
        engine = kwargs['engine']
        cli_obj = kwargs['cli_obj']

        supported_cli_classes = {'default': SonicDhcpRelayCliMaster(engine, cli_obj),
                                 'master': SonicDhcpRelayCliMaster(engine, cli_obj),
                                 '202205': SonicDhcpRelayCli202205(engine, cli_obj),
                                 '202012': SonicDhcpRelayCli202012(engine, cli_obj),
                                 '202111': SonicDhcpRelayCli202111(engine, cli_obj),
                                 '202211': SonicDhcpRelayCli202211(engine, cli_obj),
                                 '202305': SonicDhcpRelayCli202305(engine, cli_obj),
                                 '202311': SonicDhcpRelayCliMaster(engine, cli_obj),
                                 '202405': SonicDhcpRelayCliMaster(engine, cli_obj)}

        cli_class = supported_cli_classes.get(branch, supported_cli_classes['default'])
        cli_class_name = cli_class.__class__.__name__
        logger.info(f'Going to use DHCP relay CLI class: {cli_class_name}')

        return cli_class


class SonicDhcpRelayCliDefault:

    def __init__(self, engine, cli_obj):
        self.engine = engine
        self.cli_obj = cli_obj

    def add_dhcp_relay(self, vlan, dhcp_server, **kwargs):
        """
        This method adding DHCP relay entry for VLAN interface
        :param vlan: vlan interface ID for which DHCP relay should be added
        :param dhcp_server: DHCP server IP address
        :return: command output
        """
        return self.engine.run_cmd("sudo config vlan dhcp_relay add {} {}".format(vlan, dhcp_server))

    def del_dhcp_relay(self, vlan, dhcp_server, **kwargs):
        """
        This method delete DHCP relay entry from VLAN interface
        :param vlan: vlan interface ID from which DHCP relay should be deleted
        :param dhcp_server: DHCP server IP address
        :return: command output
        """
        return self.engine.run_cmd("sudo config vlan dhcp_relay del {} {}".format(vlan, dhcp_server))

    def add_ipv4_dhcp_relay(self, vlan, dhcp_server):
        return self.engine.run_cmd("sudo config vlan dhcp_relay add {} {}".format(vlan, dhcp_server))

    def del_ipv4_dhcp_relay(self, vlan, dhcp_server):
        return self.engine.run_cmd("sudo config vlan dhcp_relay del {} {}".format(vlan, dhcp_server))

    def add_ipv6_dhcp_relay(self, vlan, dhcp_server):
        return self.engine.run_cmd("sudo config vlan dhcp_relay add {} {}".format(vlan, dhcp_server))

    def del_ipv6_dhcp_relay(self, vlan, dhcp_server):
        return self.engine.run_cmd("sudo config vlan dhcp_relay del {} {}".format(vlan, dhcp_server))

    def get_ipv4_dhcp_relay_cli_config_dict(self, cli_obj):
        return cli_obj.vlan.get_show_vlan_brief_parsed_output()

    def get_ipv6_dhcp_relay_cli_config_dict(self, cli_obj):
        return cli_obj.vlan.get_show_vlan_brief_parsed_output()

    @staticmethod
    def validate_dhcp_relay_cli_config_ipv4(vlan_brief_parsed_output, vlan, expected_dhcp_servers_list):
        """
        This method doing DHCP relay IPv4 CLI validation in "show vlan brief" output
        :param vlan_brief_parsed_output: dict, parsed output of cmd "show vlan brief"
        :param vlan: vlan in which we will do validation
        :param expected_dhcp_servers_list: list, expected DHCP relay servers
        :return: AssertionError in case of failure
        """
        for dhcp_server in expected_dhcp_servers_list:
            logger.info(f'Checking that DHCP relay: {dhcp_server} available in CLI config for VLAN: {vlan}')
            assert dhcp_server in vlan_brief_parsed_output[vlan]['dhcp_servers'], \
                f'Unable to find DHCP relay {dhcp_server} in "show vlan brief" output for VLAN {vlan}'

    @staticmethod
    def validate_dhcp_relay_cli_config_ipv6(vlan_brief_parsed_output, vlan, expected_dhcp_servers_list):
        """
        This method doing DHCP relay IPv6 CLI validation in "show vlan brief" output
        :param vlan_brief_parsed_output: dict, parsed output of cmd "show vlan brief"
        :param vlan: vlan in which we will do validation
        :param expected_dhcp_servers_list: list, expected DHCP relay servers
        :return: AssertionError in case of failure
        """
        for dhcp_server in expected_dhcp_servers_list:
            logger.info(f'Checking that DHCP relay: {dhcp_server} available in CLI config for VLAN: {vlan}')
            assert dhcp_server in vlan_brief_parsed_output[vlan]['dhcp_servers'], \
                f'Unable to find DHCP relay {dhcp_server} in "show vlan brief" output for VLAN {vlan}'


class SonicDhcpRelayCliMaster(SonicDhcpRelayCliDefault):

    def __init__(self, engine, cli_obj):
        self.engine = engine
        self.cli_obj = cli_obj

    def is_dhcp_relay_ready(self):
        """
        Check that the dhcp_relay container is fully ready by verifying that at
        least one actual relay forwarding process (isc-dhcpv4-relay-<Vlan> or
        dhcp6relay) is registered in supervisord and all such entries are RUNNING.
        """
        try:
            status_output = self.engine.run_cmd(
                'docker exec dhcp_relay supervisorctl status',
                validate=False
            )
        except Exception:
            logger.info('supervisorctl status check failed')
            return False

        relay_lines = [
            line for line in status_output.splitlines()
            if re.search(r'(?:^|:)(isc-dhcpv4-relay-\S*|dhcp6relay)\b', line.strip())
        ]
        if not relay_lines:
            logger.info('No relay forwarding processes found in supervisord - dhcprelayd not yet ready')
            return False
        not_running = [line for line in relay_lines if 'RUNNING' not in line]
        if not_running:
            logger.info(f'Some relay forwarding processes are not RUNNING: {not_running}')
            return False
        return True

    def add_dhcp_relay(self, vlan, dhcp_server, **kwargs):
        """
        This method adding DHCP relay entry for VLAN interface
        :param vlan: vlan interface ID for which DHCP relay should be added
        :param dhcp_server: DHCP server IP address
        """
        ip_addr_version = ipaddress.ip_address(dhcp_server).version

        if ip_addr_version == 6:
            topology_obj = kwargs.get('topology_obj')
            self.add_ipv6_dhcp_relay(vlan, dhcp_server, topology_obj)
        else:
            self.add_ipv4_dhcp_relay(vlan, dhcp_server)
        if not wait_until(80, 5, 0, self.is_dhcp_relay_ready):
            raise AssertionError(
                "DHCP relay supervisor processes not ready on time, after restart by configuration change"
            )

    def del_dhcp_relay(self, vlan, dhcp_server, **kwargs):
        """
        This method delete DHCP relay entry from VLAN interface
        :param vlan: vlan interface ID from which DHCP relay should be deleted
        :param dhcp_server: DHCP server IP address
        """
        ip_addr_version = ipaddress.ip_address(dhcp_server).version

        if ip_addr_version == 6:
            topology_obj = kwargs.get('topology_obj')
            self.del_ipv6_dhcp_relay(vlan, dhcp_server, topology_obj)
        else:
            self.del_ipv4_dhcp_relay(vlan, dhcp_server)

    def add_ipv6_dhcp_relay(self, vlan, dhcp_server, topology_obj):
        """
        This method adding DHCPv6 relay entry for VLAN interface using DHCPv6 json config
        :param vlan: vlan interface ID for which DHCP relay should be added
        :param dhcp_server: DHCP server IP address
        :param topology_obj: topology_obj
        """
        vlan_iface = 'Vlan{}'.format(vlan)

        available_dhcp_relays = self.engine.run_cmd(f'sudo sonic-db-cli CONFIG_DB HGETALL "DHCP_RELAY|{vlan_iface}"')
        available_dhcp_relays_dict = json.loads(available_dhcp_relays.replace('\'', '"'))

        dhcpv6_servers = dhcp_server
        if available_dhcp_relays_dict.get('dhcpv6_servers@'):
            available_dhcp_servers = available_dhcp_relays_dict['dhcpv6_servers@']
            dhcpv6_servers = f'{available_dhcp_servers},{dhcp_server}'

        logger.info(f'Adding DHCP relay: {dhcp_server} for VLAN: {vlan}')
        self.engine.run_cmd(f'sudo sonic-db-cli CONFIG_DB HSET "DHCP_RELAY|{vlan_iface}" '
                            f'"dhcpv6_servers@" "{dhcpv6_servers}"')

        self.engine.run_cmd('sudo config save -y')
        self.engine.run_cmd('sudo systemctl reset-failed dhcp_relay')
        self.engine.run_cmd('sudo systemctl restart dhcp_relay')

    def del_ipv6_dhcp_relay(self, vlan, dhcp_server, topology_obj):
        """
        This method delete DHCPv6 relay entry from VLAN interface
        :param vlan: vlan interface ID from which DHCP relay should be deleted
        :param dhcp_server: DHCP server IP address
        :param topology_obj: topology_obj
        """
        vlan_iface = 'Vlan{}'.format(vlan)

        available_dhcp_relays = self.engine.run_cmd(f'sudo sonic-db-cli CONFIG_DB HGETALL "DHCP_RELAY|{vlan_iface}"')
        available_dhcp_relays_dict = json.loads(available_dhcp_relays.replace('\'', '"'))
        available_dhcp_servers_list = available_dhcp_relays_dict['dhcpv6_servers@'].split(',')
        available_dhcp_servers_list.remove(dhcp_server)
        dhcpv6_servers = ','.join(available_dhcp_servers_list)

        logger.info(f'Removing DHCP relay: {dhcp_server} from VLAN: {vlan}')
        if dhcpv6_servers:
            self.engine.run_cmd(f'sudo sonic-db-cli CONFIG_DB HSET "DHCP_RELAY|{vlan_iface}" '
                                f'"dhcpv6_servers@" "{dhcpv6_servers}"')
        else:
            self.engine.run_cmd(f'sudo sonic-db-cli CONFIG_DB DEL "DHCP_RELAY|{vlan_iface}"')

        self.engine.run_cmd('sudo config save -y')
        self.engine.run_cmd('sudo service dhcp_relay restart')

    def parse_show_dhcprelay_helper_ipv6_as_dict(self):
        """
        Parse output of command "show dhcprelay_helper ipv6" and return result as dict
        :return: dict, example: {'Vlan690': ['6900::2', '6900::3'], 'Vlan691': ['6900::2']}
        """
        result = {}
        output = self.engine.run_cmd('show dhcprelay_helper ipv6')
        """
        Example of output:
        -------  -------
        Vlan690  6900::2
                 6900::3
        -------  -------

        -------  -------
        Vlan691  6900::2
        -------  -------
        """
        vlan = None
        for line in output.splitlines():
            if '----' not in line and line:
                # Example of splited_line : ['Vlan690', '6900::2'] or ['6900::3']
                splited_line = line.split()
                if len(splited_line) == 2:
                    vlan = splited_line[0]
                    relay_ip = splited_line[1]
                    result[vlan] = [relay_ip]
                else:
                    relay_ip = splited_line[0]
                    result[vlan].append(relay_ip)

        return result

    def get_ipv4_dhcp_relay_cli_config_dict(self, cli_obj):
        return cli_obj.vlan.get_show_vlan_brief_parsed_output()

    def get_ipv6_dhcp_relay_cli_config_dict(self, cli_obj):
        return cli_obj.dhcp_relay.parse_show_dhcprelay_helper_ipv6_as_dict()

    @staticmethod
    def validate_dhcp_relay_cli_config_ipv6(dhcprelay_helper_ipv6_output_dict, vlan, expected_dhcp_servers_list):
        """
        This method doing DHCP relay CLI IPv6 validation in "show dhcprelay_helper ipv6" output
        :param dhcprelay_helper_ipv6_output_dict: output of cmd "show dhcprelay_helper ipv6" parsed as dict
        :param vlan: vlan in which we will do validation
        :param expected_dhcp_servers_list: list, expected DHCP relay servers
        :return: AssertionError in case of failure
        """
        for dhcp_server in expected_dhcp_servers_list:
            vlan_iface = f'Vlan{vlan}'
            logger.info(f'Checking that DHCP relay: {dhcp_server} available in CLI config for VLAN: {vlan}')
            assert dhcp_server in dhcprelay_helper_ipv6_output_dict[vlan_iface], \
                f'Unable to find DHCP relay {dhcp_server} in "show dhcprelay_helper ipv6" output for VLAN {vlan}'


class SonicDhcpRelayCli202012(SonicDhcpRelayCliMaster):

    def __init__(self, engine, cli_obj):
        self.engine = engine
        self.cli_obj = cli_obj


class SonicDhcpRelayCli202111(SonicDhcpRelayCliMaster):

    def __init__(self, engine, cli_obj):
        self.engine = engine
        self.cli_obj = cli_obj


class SonicDhcpRelayCli202205(SonicDhcpRelayCliMaster):

    def __init__(self, engine, cli_obj):
        self.engine = engine
        self.cli_obj = cli_obj


class SonicDhcpRelayCli202211(SonicDhcpRelayCliMaster):

    def __init__(self, engine, cli_obj):
        self.engine = engine
        self.cli_obj = cli_obj


class SonicDhcpRelayCli202305(SonicDhcpRelayCliMaster):

    def __init__(self, engine, cli_obj):
        self.engine = engine
        self.cli_obj = cli_obj
