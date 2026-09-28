import pytest
import os

from ngts.config_templates.dhcp_relay_config_template import DhcpRelayConfigTemplate
from ngts.helpers.general_helper import is_smartswitch_platform

DHCPD_CONF_NAME = 'dhcpd.conf'
DHCPD6_CONF_NAME = 'dhcpd6.conf'
DHCPD_CONF_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), DHCPD_CONF_NAME)
DHCPD6_CONF_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), DHCPD6_CONF_NAME)


@pytest.fixture(scope='package', autouse=True)
def dhcp_server_configuration(topology_obj, engines):
    """
    Pytest fixture which are doing configuration fot dhcp server
    :param topology_obj: topology object fixture
    :param engines: fixture with engines
    """

    # DHCP Relay config which will be used in test
    dhcp_servers = ['6900::2'] if is_smartswitch_platform(topology_obj) else ['69.0.0.2', '6900::2']
    dhcp_relay_config_dict = {
        'dut': [{'vlan_id': 690, 'dhcp_servers': dhcp_servers}]
    }

    DhcpRelayConfigTemplate.configuration(topology_obj, dhcp_relay_config_dict)

    engines.hb.copy_file(source_file=DHCPD_CONF_PATH, dest_file=DHCPD_CONF_NAME, file_system='/etc/dhcp/',
                         overwrite_file=True, verify_file=False)
    engines.hb.copy_file(source_file=DHCPD6_CONF_PATH, dest_file=DHCPD6_CONF_NAME, file_system='/etc/dhcp/',
                         overwrite_file=True, verify_file=False)

    # Create dhclient.conf with timeout of 10 sec
    ha_engine = topology_obj.players['ha']['engine']
    ha_engine.run_cmd('echo "timeout 10;" > dhclient.conf')

    dhcp_ifaces = 'bond0.69'
    engines.hb.run_cmd(
        'sed -e "s/INTERFACESv4=\\"\\"/INTERFACESv4=\\"{}\\"/g" -i /etc/default/isc-dhcp-server'.format(dhcp_ifaces))
    engines.hb.run_cmd(
        'sed -e "s/INTERFACESv6=\\"\\"/INTERFACESv6=\\"{}\\"/g" -i /etc/default/isc-dhcp-server'.format(dhcp_ifaces))
    engines.hb.run_cmd('/etc/init.d/isc-dhcp-server restart')

    yield

    DhcpRelayConfigTemplate.cleanup(topology_obj, dhcp_relay_config_dict)

    engines.hb.run_cmd('rm -f dhclient.conf')
    engines.hb.run_cmd(
        'sed -e "s/INTERFACESv4=\\"{}\\"/INTERFACESv4=\\"\\"/g" -i /etc/default/isc-dhcp-server'.format(dhcp_ifaces))
    engines.hb.run_cmd(
        'sed -e "s/INTERFACESv6=\\"{}\\"/INTERFACESv6=\\"\\"/g" -i /etc/default/isc-dhcp-server'.format(dhcp_ifaces))


@pytest.fixture()
def configure_additional_dhcp_server(topology_obj, cli_objects, engines):
    dut_cli_object = topology_obj.players['dut']['cli']
    skip_v4 = is_smartswitch_platform(topology_obj)

    engines.ha.copy_file(source_file=DHCPD_CONF_PATH, dest_file=DHCPD_CONF_NAME, file_system='/etc/dhcp/',
                         overwrite_file=True, verify_file=False)
    engines.ha.copy_file(source_file=DHCPD6_CONF_PATH, dest_file=DHCPD6_CONF_NAME, file_system='/etc/dhcp/',
                         overwrite_file=True, verify_file=False)

    engines.ha.run_cmd('sed -e "s/INTERFACESv4=\\"\\"/INTERFACESv4=\\"bond0\\"/g" -i /etc/default/isc-dhcp-server')
    engines.ha.run_cmd('sed -e "s/INTERFACESv6=\\"\\"/INTERFACESv6=\\"bond0\\"/g" -i /etc/default/isc-dhcp-server')
    engines.ha.run_cmd('/etc/init.d/isc-dhcp-server restart')
    if not skip_v4:
        dut_cli_object.dhcp_relay.add_dhcp_relay(690, '30.0.0.2', topology_obj=topology_obj)
    dut_cli_object.dhcp_relay.add_dhcp_relay(690, '3000::2', topology_obj=topology_obj)
    if not skip_v4:
        cli_objects.ha.route.add_route('69.0.1.0', '30.0.0.1', '24')
    cli_objects.ha.route.add_route('6900:1::', '3000::1', '64')

    yield

    engines.ha.run_cmd('sed -e "s/INTERFACESv4=\\"bond0\\"/INTERFACESv4=\\"\\"/g" -i /etc/default/isc-dhcp-server')
    engines.ha.run_cmd('sed -e "s/INTERFACESv6=\\"bond0\\"/INTERFACESv6=\\"\\"/g" -i /etc/default/isc-dhcp-server')
    if not skip_v4:
        dut_cli_object.dhcp_relay.del_dhcp_relay(690, '30.0.0.2', topology_obj=topology_obj)
    dut_cli_object.dhcp_relay.del_dhcp_relay(690, '3000::2', topology_obj=topology_obj)
    if not skip_v4:
        cli_objects.ha.route.del_route('69.0.1.0', '30.0.0.1', '24')
    cli_objects.ha.route.del_route('6900:1::', '3000::1', '64')
