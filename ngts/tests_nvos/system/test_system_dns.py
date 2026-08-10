import logging
from ngts.tools.test_utils import allure_utils as allure
import pytest
from ngts.nvos_tools.system.System import System
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_constants.constants_nvos import SystemConsts
from ngts.nvos_constants.constants_nvos import SystemConsts, CumulusConsts, ImageConsts
from ngts.nvos_constants.constants_nvos import ApiType
from ngts.nvos_tools.cli_coverage.operation_time import OperationTime
from devts.infra.tools.validations.traffic_validations.port_check.port_checker import check_port_status_till_alive
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port
from ngts.nvos_tools.vrf.vrf import Vrf
from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from ngts.nvos_tools.infra.DutUtilsTool import DutUtilsTool, RebootParams
from ngts.nvos_tools.infra.ConnectionTool import ConnectionTool
import re
import ipaddress
import time
import crypt
from devts.infra.tools.connection_tools.utils import generate_strong_password
from retry.api import retry_call


cumulus_owner = "lbenbasat"


logger = logging.getLogger()

BASE_IMAGE_VERSION_TO_INSTALL = "nvos-amd64-{pre_release_name}.bin"
BASE_IMAGE_VERSION_TO_INSTALL_PATH = "/auto/sw_system_release/nos/nvos/{pre_release_name}/amd64/{base_image}"
backup_resolv_conf = None


def _dns_config_backup_and_restore_helper(engines, restore=False):
    """
    Helper function to backup and restore the resolv.conf file of the DUT.
    For eth devices dns configs are removed after doing "nv unset system dns" command by the infra at the end of the test,
    so we need to backup and restore the resolv.conf file to restore the mgmt vrf dns configs after the test.
    """
    if TestToolkit.devices.dut.is_eth():
        global backup_resolv_conf
        password = engines.dut.password if hasattr(engines.dut, 'password') else ''
        if not backup_resolv_conf:
            backup_resolv_conf = engines.dut.run_cmd(
                f"echo '{password}' | sudo -S cat /etc/resolv.conf"
            )

        if restore:
            engines.dut.run_cmd(
                f"echo '{password}' | sudo -S sh -c \"echo '{backup_resolv_conf}' | tee /etc/resolv.conf > /dev/null\""
            )


@pytest.fixture
def dns_config_backup_and_restore(engines):
    """
    Fixture to backup the resolv.conf file before test and restore it after.
    """
    _dns_config_backup_and_restore_helper(engines, restore=False)


def clear_system_dns(system, engines):
    """
    Method to unset the system dns configurations
    :param system:  System object
    :param engines: Engines object
    """
    with allure.step('Run unset system DNS server and apply config'):
        if TestToolkit.devices.dut.is_eth():
            system.dns.unset(SystemConsts.DNS_SERVER, apply=True, dut_engine=engines.dut).verify_result()
            _dns_config_backup_and_restore_helper(engines, restore=True)
        if TestToolkit.devices.dut.is_ib():
            system.dns.unset(SystemConsts.DNS_SERVER, apply=True, dut_engine=engines.dut).verify_result()


@pytest.mark.dns
@pytest.mark.system
@pytest.mark.simx
@pytest.mark.cumulus
@pytest.mark.parametrize('dns_server_type', SystemConsts.DNS_SERVER_IDS.keys())
def test_set_system_dns_functionality(engines, random_api, target_version, dns_server_type, devices, dns_config_backup_and_restore):
    """
    Run set system dns server command and verify in show command
        1. Attempt a file fetch from a build server and verify it is successful
        2. Verify contents of resolv.conf file that it does not have <ip> listed as DNS server
        3. Run 'nv set system dns server <ip>' and verify command is completed successfully
        4. Verify contents of resolv.conf file that it have <ip> listed as DNS server
        5. Attempt a file fetch from a build server and verify it is not successful

    """
    _dns_config_backup_and_restore_helper(engines, restore=True)
    system = System()
    dns_server_id = SystemConsts.DNS_SERVER_IDS[dns_server_type]

    try:
        retry_call(verify_dns_in_resolv_file, [engines, [dns_server_id], False], exceptions=AssertionError, tries=2, delay=2)

        with allure.step("Fetch an image {}".format(target_version)):
            system.image.action_fetch(path=target_version, expected_output=devices.dut.fetch_success_message)

        with allure.step('Run set system dns server <server-id>command and apply config'):
            system.dns.set(SystemConsts.DNS_SERVER, dns_server_id,
                           apply=True, dut_engine=engines.dut, ask_for_confirmation=devices.dut.ask_for_confirmation).verify_result()

        retry_call(verify_dns_in_resolv_file, [engines, [dns_server_id]], exceptions=AssertionError, tries=2, delay=2)

        with allure.step("Attempt fetching the image which should fail"):
            system.image.action_fetch(path=target_version, expected_output=devices.dut.fetch_error_message).verify_result(False)

    finally:
        clear_system_dns(system, engines)


@pytest.mark.dns
@pytest.mark.system
@pytest.mark.simx
@pytest.mark.cumulus
def test_unset_system_dns_server(engines, random_api, devices, dns_config_backup_and_restore):
    """
    Run set system dns server command and verify in show command
        1. Run ‘nv set system dns server <dns_server_id_ipv4> and verify command is completed successfully
        2. Run ‘nv set system dns server <dns_server_id_ipv6> and verify command is completed successfully
        3. Check ‘nv show system dns server’ ond validate DNS server ips ipv4 and ipv6 in output
        4. Verify contents of resolv.conf file that it has <ips> ipv4 and ipv6 listed as DNS servers
        5. Run ‘nv unset system dns server' and verify command is completed successfully
        6. Check ‘nv show system dns server’ ond validate DNS server ipv6 and ipv4 are not in output
        7. Verify contents of resolv.conf file that it does not have ipv6 and ipv4 listed as DNS servers

    """
    dns_server_id_ipv4 = SystemConsts.DNS_SERVER_IDS["ipv4"]
    dns_server_id_ipv6 = SystemConsts.DNS_SERVER_IDS["ipv6"]
    system = System()

    try:
        with allure.step('Run set system dns server <server-id> ipv4 command and apply config'):
            system.dns.set(op_param_name=SystemConsts.DNS_SERVER, op_param_value=f'"{dns_server_id_ipv4}"',
                           apply=True, dut_engine=engines.dut, ask_for_confirmation=devices.dut.ask_for_confirmation).verify_result()

        with allure.step('Run set system dns server <server-id> ipv6 command and apply config'):
            system.dns.set(op_param_name=SystemConsts.DNS_SERVER, op_param_value=f'"{dns_server_id_ipv6}"',
                           apply=True, dut_engine=engines.dut).verify_result()

        with allure.step('Verify DNS server ipv4 and ipv6  in show system dns server output'):
            dns_output = OutputParsingTool.parse_json_str_to_dictionary(system.dns.show(SystemConsts.DNS_SERVER)).\
                get_returned_value()
            assert dns_server_id_ipv4 in dns_output, "The configured DNS server ipv4 is not present in show system dns"
            assert dns_server_id_ipv6 in dns_output, "The configured DNS server ipv6 is not present in show system dns"

        retry_call(verify_dns_in_resolv_file, [engines, [dns_server_id_ipv4, dns_server_id_ipv6]],
                   exceptions=AssertionError, tries=2, delay=2)

        with allure.step('Run unset system dns server command and apply config'):
            system.dns.unset(SystemConsts.DNS_SERVER, apply=True, dut_engine=engines.dut).verify_result()

        with allure.step('Verify DNS server ipv4 and ipv6 are not present in show system dns server output'):
            dns_output = OutputParsingTool.parse_json_str_to_dictionary(system.dns.show(SystemConsts.DNS_SERVER)).\
                get_returned_value()
            assert dns_server_id_ipv4 not in dns_output, "The configured DNS server ipv4 is present in show system dns"
            assert dns_server_id_ipv6 not in dns_output, "The configured DNS server ipv6 is present in show system dns"

        retry_call(verify_dns_in_resolv_file, [engines, [dns_server_id_ipv4, dns_server_id_ipv6], False],
                   exceptions=AssertionError, tries=2, delay=2)

    finally:
        clear_system_dns(system, engines)


@pytest.mark.dns
@pytest.mark.system
@pytest.mark.simx
@pytest.mark.cumulus
def test_unset_system_dns_server_ip(engines, random_api, devices, dns_config_backup_and_restore):
    """
    Run set system dns server command and verify in show command
        1. Run ‘nv set system dns server <dns_server_id_ipv4> and verify command is completed successfully
        2. Run ‘nv set system dns server <dns_server_id_ipv6> and verify command is completed successfully
        3. Check ‘nv show system dns server’ ond validate DNS server ips ipv4 and ipv6 in output
        4. Verify contents of resolv.conf file that it has <ips> ipv4 and ipv6 listed as DNS servers
        5. Run ‘nv unset system dns server <dns_server_id_ipv4> and verify command is completed successfully
        6. Check ‘nv show system dns server’ ond validate DNS server ipv6 in output but no ipv4
        7. Verify contents of resolv.conf file that it has ipv6 but no ipv4 listed as DNS servers

    """
    dns_server_id_ipv4 = SystemConsts.DNS_SERVER_IDS["ipv4"]
    dns_server_id_ipv6 = SystemConsts.DNS_SERVER_IDS["ipv6"]
    system = System()

    try:
        with allure.step('Run set system dns server <server-id> ipv4 command and apply config'):
            system.dns.set(op_param_name=SystemConsts.DNS_SERVER, op_param_value=f'"{dns_server_id_ipv4}"',
                           apply=True, dut_engine=engines.dut, ask_for_confirmation=devices.dut.ask_for_confirmation).verify_result()

        with allure.step('Run set system dns server <server-id> ipv6 command and apply config'):
            system.dns.set(op_param_name=SystemConsts.DNS_SERVER, op_param_value=f'"{dns_server_id_ipv6}"',
                           apply=True, dut_engine=engines.dut).verify_result()

        with allure.step('Verify DNS server ipv4 and ipv6  in show system dns server output'):
            dns_output = OutputParsingTool.parse_json_str_to_dictionary(system.dns.show(SystemConsts.DNS_SERVER)).\
                get_returned_value()
            assert dns_server_id_ipv4 in dns_output, "The configured DNS server ipv4 is not present in show system dns"
            assert dns_server_id_ipv6 in dns_output, "The configured DNS server ipv6 is not present in show system dns"

        retry_call(verify_dns_in_resolv_file, [engines, [dns_server_id_ipv4, dns_server_id_ipv6]],
                   exceptions=AssertionError, tries=2, delay=2)

        with allure.step('Run unset system dns server <server-id> ipv4 command and apply config'):
            system.dns.server.server_id[dns_server_id_ipv4].unset(
                apply=True, dut_engine=engines.dut
            ).verify_result()

        with allure.step('Verify DNS server ipv4 is not present and ipv6 is, in show system dns server output'):
            dns_output = OutputParsingTool.parse_json_str_to_dictionary(system.dns.show(SystemConsts.DNS_SERVER)).\
                get_returned_value()
            assert dns_server_id_ipv4 not in dns_output, "The configured DNS server ipv4 is present in show system dns"
            assert dns_server_id_ipv6 in dns_output, "The configured DNS server ipv6 is not present in show system dns"

        with allure.step('Verify in resolv.conf file that it is updated with ipv6 but not ipv4'):
            retry_call(verify_dns_in_resolv_file, [engines, [dns_server_id_ipv6]], exceptions=AssertionError, tries=2,
                       delay=2)
            retry_call(verify_dns_in_resolv_file, [engines, [dns_server_id_ipv4], False], exceptions=AssertionError,
                       tries=2, delay=2)

    finally:
        clear_system_dns(system, engines)


@pytest.mark.dns
@pytest.mark.system
@pytest.mark.simx
@pytest.mark.cumulus
def test_set_system_invalid_dns_server(engines, random_api, dns_config_backup_and_restore):
    """
    Run set system dns server command and verify in show command
        1. Run ‘nv unset system dns server <ip> and verify command is not completed successfully

    """
    system = System()
    invalid_ip = "420.420.42.42"
    invalid_ip_format = "1.1.1"
    invalid_ip_multicast = "255.255.255.255"
    invalid_ip_localhost = "127.0.0.1"
    try:
        with allure.step('Run set system dns server invalid_ip command and apply config'):
            system.dns.set(op_param_name=SystemConsts.DNS_SERVER, op_param_value=f'"{invalid_ip}"',
                           apply=True, dut_engine=engines.dut).verify_result(should_succeed=False)

        with allure.step('Run set system dns server invalid_ip_format command and apply config'):
            system.dns.set(op_param_name=SystemConsts.DNS_SERVER, op_param_value=f'"{invalid_ip_format}"',
                           apply=True, dut_engine=engines.dut).verify_result(should_succeed=False)

        with allure.step('Run set system dns server invalid_ip_multicast command and apply config'):
            system.dns.set(op_param_name=SystemConsts.DNS_SERVER, op_param_value=f'"{invalid_ip_multicast}"',
                           apply=True, dut_engine=engines.dut).verify_result(should_succeed=False)

        with allure.step('Run set system dns server invalid_ip_multicast command and apply config'):
            system.dns.set(op_param_name=SystemConsts.DNS_SERVER, op_param_value=f'"{invalid_ip_localhost}"',
                           apply=True, dut_engine=engines.dut).verify_result(should_succeed=False)

    finally:
        clear_system_dns(system, engines)


@pytest.mark.dns
@pytest.mark.system
@pytest.mark.simx
@pytest.mark.cumulus
def test_system_dns_server_max(engines, random_api, devices, dns_config_backup_and_restore):
    """
    Run set system dns server command and verify in show command
        1. Run ‘nv set system dns server <dns_server_id_ipv4> and verify command is completed successfully
        2. Run ‘nv set system dns server <dns_server_id_ipv6> and verify command is completed successfully
        3. Run ‘nv set system dns server '1.1.1.1' and verify command is completed successfully
        4. Check ‘nv show system dns server’ ond validate all configured DNS servers in output
        5. Run ‘nv set system dns server '2.2.2.2' and verify it shows error that max DNS server config is reached
        6. Run ‘nv unset system dns server 1.1.1.1' and verify command is completed successfully
        7. Run ‘nv set system dns server '2.2.2.2' and verify command is completed successfully
        8. Check ‘nv show system dns server’ ond validate all configured DNS servers in output

    """
    dns_server_id_ipv4 = SystemConsts.DNS_SERVER_IDS["ipv4"]
    dns_server_id_ipv6 = SystemConsts.DNS_SERVER_IDS["ipv6"]
    system = System()

    try:
        with allure.step('Run set system dns server <server-id> ipv4 command and apply config'):
            system.dns.set(op_param_name=SystemConsts.DNS_SERVER, op_param_value=dns_server_id_ipv4,
                           apply=True, dut_engine=engines.dut, ask_for_confirmation=devices.dut.ask_for_confirmation).verify_result()

        with allure.step('Run set system dns server <server-id> ipv6 command and apply config'):
            system.dns.set(op_param_name=SystemConsts.DNS_SERVER, op_param_value=dns_server_id_ipv6,
                           apply=True, dut_engine=engines.dut).verify_result()

        with allure.step('Run set system dns server 1.1.1.1 command and apply config'):
            system.dns.set(op_param_name=SystemConsts.DNS_SERVER, op_param_value="1.1.1.1",
                           apply=True, dut_engine=engines.dut).verify_result()

        with allure.step('Verify all configured DNS servers in show system dns server output'):
            dns_output = OutputParsingTool.parse_json_str_to_dictionary(system.dns.show(SystemConsts.DNS_SERVER)).\
                get_returned_value()
            assert dns_server_id_ipv4 in dns_output, "The configured DNS server ipv4 is not present in show system dns"
            assert dns_server_id_ipv6 in dns_output, "The configured DNS server ipv6 is not present in show system dns"
            assert "1.1.1.1" in dns_output, "The configured DNS server ipv6 is not present in show system dns"

        if TestToolkit.devices.dut.is_eth():
            with allure.step('Run set system dns server 2.2.2.2 command and verify able to configure multiple dns servers'):
                system.dns.set(op_param_name=SystemConsts.DNS_SERVER, op_param_value="2.2.2.2",
                               apply=True, dut_engine=engines.dut).verify_result()
        else:
            with allure.step('Run set system dns server 2.2.2.2 command and verify it throws max error'):
                system.dns.set(op_param_name=SystemConsts.DNS_SERVER, op_param_value="2.2.2.2",
                               apply=True, dut_engine=engines.dut).verify_result(should_succeed=False)

        with allure.step('Run unset system dns server 1.1.1.1 command and apply config'):
            system.dns.server.server_id["1.1.1.1"].unset(
                apply=True, dut_engine=engines.dut
            ).verify_result()

        if TestToolkit.devices.dut.is_eth():
            with allure.step('Run unset system dns server 2.2.2.2 command and apply config'):
                system.dns.server.server_id["2.2.2.2"].unset(
                    apply=True, dut_engine=engines.dut
                ).verify_result()

        with allure.step('Run set system dns server 2.2.2.2 command and apply config'):
            system.dns.set(op_param_name=SystemConsts.DNS_SERVER, op_param_value="2.2.2.2",
                           apply=True, dut_engine=engines.dut).verify_result()

        with allure.step('Verify all configured DNS servers are present in show system dns server output'):
            dns_output = OutputParsingTool.parse_json_str_to_dictionary(system.dns.show(SystemConsts.DNS_SERVER)).\
                get_returned_value()
            assert dns_server_id_ipv4 in dns_output, "The configured DNS server ipv4 is not present in show system dns"
            assert dns_server_id_ipv6 in dns_output, "The configured DNS server ipv6 is not present in show system dns"
            assert "2.2.2.2" in dns_output, "The configured DNS server ipv6 is not present in show system dns"

    finally:
        clear_system_dns(system, engines)


@pytest.mark.dns
@pytest.mark.system
@pytest.mark.simx
@pytest.mark.cumulus
def test_system_dns_server_max_single_apply(engines, random_api, devices, dns_config_backup_and_restore):
    """
    Run set system dns server command and verify in show command
        1. Run ‘nv set system dns server <dns_server_id_ipv4> and verify command is completed successfully
        2. Run ‘nv set system dns server <dns_server_id_ipv6> and verify command is completed successfully
        3. Check ‘nv show system dns server’ ond validate all configured DNS servers in output
        4. Run ‘nv set system dns server '1.1.1.1'
        5. Run ‘nv set system dns server '2.2.2.2'
        6. Apply config and verify it shows error that max DNS server config is reached
        7. Check ‘nv show system dns server’ ond validate output still shows first two configured IPs

    """
    dns_server_id_ipv4 = SystemConsts.DNS_SERVER_IDS["ipv4"]
    dns_server_id_ipv6 = SystemConsts.DNS_SERVER_IDS["ipv6"]
    system = System()

    try:
        with allure.step('Run set system dns server <server-id> ipv4 command and apply config'):
            system.dns.set(op_param_name=SystemConsts.DNS_SERVER, op_param_value=dns_server_id_ipv4,
                           apply=True, dut_engine=engines.dut, ask_for_confirmation=devices.dut.ask_for_confirmation).verify_result()

        with allure.step('Run set system dns server <server-id> ipv6 command and apply config'):
            system.dns.set(op_param_name=SystemConsts.DNS_SERVER, op_param_value=dns_server_id_ipv6,
                           apply=True, dut_engine=engines.dut, ask_for_confirmation=devices.dut.ask_for_confirmation).verify_result()

        with allure.step('Verify all configured DNS servers in show system dns server output'):
            dns_output = OutputParsingTool.parse_json_str_to_dictionary(system.dns.show(SystemConsts.DNS_SERVER)).\
                get_returned_value()
            assert dns_server_id_ipv4 in dns_output, "The configured DNS server ipv4 is not present in show system dns"
            assert dns_server_id_ipv6 in dns_output, "The configured DNS server ipv6 is not present in show system dns"

        with allure.step('Run set system dns server 1.1.1.1 command'):
            system.dns.set(op_param_name=SystemConsts.DNS_SERVER, op_param_value="1.1.1.1",
                           apply=False, dut_engine=engines.dut).verify_result()

        if TestToolkit.devices.dut.is_ib():
            with allure.step('Run set system dns server 2.2.2.2 command and verify it shows error that max DNS'
                             'server config is reached'):
                system.dns.set(op_param_name=SystemConsts.DNS_SERVER, op_param_value="2.2.2.2",
                               apply=True, dut_engine=engines.dut).verify_result(should_succeed=False)

        with allure.step('Verify only the first two configured DNS servers in show system dns server output'):
            dns_output = OutputParsingTool.parse_json_str_to_dictionary(system.dns.show(SystemConsts.DNS_SERVER)).\
                get_returned_value()
            assert dns_server_id_ipv4 in dns_output, "The configured DNS server ipv4 is not present in show system dns"
            assert dns_server_id_ipv6 in dns_output, "The configured DNS server ipv6 is not present in show system dns"

    finally:
        clear_system_dns(system, engines)


@pytest.mark.dns
@pytest.mark.system
@pytest.mark.simx
@pytest.mark.cumulus
def test_system_dns_server_max_set_unset_single_apply(engines, random_api, devices, dns_config_backup_and_restore):
    """
    Run set system dns server command and verify in show command
        1. Run ‘nv set system dns server <dns_server_id_ipv4> and verify command is completed successfully
        2. Run ‘nv set system dns server <dns_server_id_ipv6> and verify command is completed successfully
        4. Run ‘nv set system dns server '1.1.1.1' (don't apply config)
        5. Run ‘nv set system dns server '2.2.2.2' (don't apply config)
        5. Run 'nv unset system dns server <dns_server_id_ipv4>
        6. Apply config and verify it does not shows any error that max DNS server config is reached
        7. Check ‘nv show system dns server’ ond validate output still shows all three configured IPs

    """
    dns_server_id_ipv4 = SystemConsts.DNS_SERVER_IDS["ipv4"]
    dns_server_id_ipv6 = SystemConsts.DNS_SERVER_IDS["ipv6"]
    system = System()

    try:
        with allure.step('Run set system dns server <server-id> ipv4 command and apply config'):
            system.dns.set(op_param_name=SystemConsts.DNS_SERVER, op_param_value=dns_server_id_ipv4,
                           apply=True, dut_engine=engines.dut, ask_for_confirmation=devices.dut.ask_for_confirmation).verify_result()

        with allure.step('Run set system dns server <server-id> ipv6 command and apply config'):
            system.dns.set(op_param_name=SystemConsts.DNS_SERVER, op_param_value=dns_server_id_ipv6,
                           apply=True, dut_engine=engines.dut).verify_result()

        with allure.step('Verify all configured DNS servers in show system dns server output'):
            dns_output = OutputParsingTool.parse_json_str_to_dictionary(system.dns.show(SystemConsts.DNS_SERVER)).\
                get_returned_value()
            assert dns_server_id_ipv4 in dns_output, "The configured DNS server ipv4 is not present in show system dns"
            assert dns_server_id_ipv6 in dns_output, "The configured DNS server ipv6 is not present in show system dns"

        with allure.step('Run set system dns server 1.1.1.1 command'):
            system.dns.set(op_param_name=SystemConsts.DNS_SERVER, op_param_value="1.1.1.1",
                           apply=False, dut_engine=engines.dut).verify_result()

        with allure.step('Run set system dns server 2.2.2.2 command'):
            system.dns.set(op_param_name=SystemConsts.DNS_SERVER, op_param_value="2.2.2.2",
                           apply=False, dut_engine=engines.dut).verify_result()

        with allure.step('Run unset system dns server dns_server_id_ipv4 command and apply config'):
            system.dns.server.server_id[dns_server_id_ipv4].unset(
                apply=True, dut_engine=engines.dut
            ).verify_result()

        with allure.step('Verify the configured DNS servers are in show system dns server output'):
            dns_output = OutputParsingTool.parse_json_str_to_dictionary(system.dns.show(SystemConsts.DNS_SERVER)).\
                get_returned_value()
            assert dns_server_id_ipv6 in dns_output, "The configured DNS server ipv6 is not present in show system dns"
            assert "1.1.1.1" in dns_output, "The configured DNS server 1.1.1.1 is not present in show system dns"
            assert "2.2.2.2" in dns_output, "The configured DNS server ipv6 is not present in show system dns"

    finally:
        clear_system_dns(system, engines)


@pytest.mark.dns
@pytest.mark.system
@pytest.mark.simx
@pytest.mark.cumulus
def test_factory_reset_for_static_system_dns(engines, random_api, devices, nv_command, dns_config_backup_and_restore):
    """
    Run factory reset system command and verify the system DNS fields are removed from show system dns
        Test flow:
        1. Run ‘nv set system dns server <ip> and verify command is completed successfully
        2. Check ‘nv show system dns server’ ond validate DNS server <ip> in output
        3. Run system factory reset
        4. Run 'nv show system' and verify system dns server fields is removed
    """
    system = System()
    dns_server_id = SystemConsts.DNS_SERVER_IDS["ipv4"]

    try:
        if TestToolkit.devices.dut.is_ib():
            with allure.step('Validate system dns is default (Null)'):
                system_dns_output = OutputParsingTool.parse_json_str_to_dictionary(
                    system.dns.show(SystemConsts.DNS_SERVER)).get_returned_value()
                assert system_dns_output == {}, \
                    "System contact in system show is {} instead of Null".format(system_dns_output)

        with allure.step('Run set system dns server <server-id> command and apply config'):
            system.dns.set(op_param_name=SystemConsts.DNS_SERVER, op_param_value=dns_server_id,
                           apply=True, dut_engine=engines.dut, ask_for_confirmation=devices.dut.ask_for_confirmation).verify_result()

        with allure.step('Verify DNS server in show system dns server output'):
            dns_output = OutputParsingTool.parse_json_str_to_dictionary(system.dns.show(SystemConsts.DNS_SERVER)). \
                get_returned_value()
            assert dns_server_id in dns_output, "The configured DNS server {} is not present in show system dns".\
                format(dns_server_id)

        with allure.step("Run reset factory with keep basic param"):
            res_obj = system.factory_default.action_reset(param="keep basic", operation=devices.dut.reset_factory)
            res_obj.verify_result()

        if TestToolkit.devices.dut.is_ib():
            with allure.step('Validate system dns is back to default (Null)'):
                system_dns_output = OutputParsingTool.parse_json_str_to_dictionary(
                    system.dns.show(SystemConsts.DNS_SERVER)).get_returned_value()
                assert system_dns_output == {}, \
                    "System DNS in system show is {} instead of Null".format(system_dns_output)
        elif TestToolkit.devices.dut.is_eth():
            with allure.step('Verify DNS server in show system dns server output after factory reset'):
                dns_output = OutputParsingTool.parse_json_str_to_dictionary(system.dns.show(SystemConsts.DNS_SERVER)). \
                    get_returned_value()
                assert dns_server_id in dns_output, "The configured DNS server is not present in show system dns after factory reset"

    finally:
        clear_system_dns(system, engines)

        with allure.step("Verify operation time"):
            OperationTime.verify_operation_time(res_obj.duration, devices.dut.reset_factory, devices).verify_result()


@pytest.mark.dns
@pytest.mark.system
def test_set_system_dns_server_max_limit(engines, random_api):
    """
    Run set system dns server command and verify maximum DNS servers limit
        1. Configure MAX_DNS_SERVERS (3) DNS servers without applying
        2. Apply config and verify all servers are configured successfully
        3. Check 'nv show system dns server' and validate all MAX_DNS_SERVERS are present
        4. Verify contents of resolv.conf file that it has all DNS servers listed
        5. Attempt to set one more DNS server beyond the limit
        6. Verify error is returned indicating max DNS server limit is exceeded
        7. Check 'nv show system dns server' and validate only MAX_DNS_SERVERS are configured
        8. Verify resolv.conf file still contains only the original MAX_DNS_SERVERS

    """
    system = System()
    max_dns_servers = SystemConsts.MAX_DNS_SERVERS
    base_ip = "10.64.5"

    # Generate DNS server IPs based on MAX_DNS_SERVERS constant
    dns_servers = [f"{base_ip}.{5 + i}" for i in range(max_dns_servers)]
    dns_server_beyond_limit = f"{base_ip}.{5 + max_dns_servers}"

    try:
        # Configure MAX_DNS_SERVERS DNS servers
        for idx, dns_server in enumerate(dns_servers):
            # Apply config only on the last server
            apply_config = (idx == max_dns_servers - 1)
            step_suffix = " and apply config" if apply_config else ""

            with allure.step(f'Run set system dns server {dns_server} command{step_suffix}'):
                system.dns.set(op_param_name=SystemConsts.DNS_SERVER,
                               op_param_value=dns_server,
                               apply=apply_config,
                               dut_engine=engines.dut).verify_result()

        with allure.step(f'Verify all {max_dns_servers} configured DNS servers in show system dns server output'):
            dns_output = OutputParsingTool.parse_json_str_to_dictionary(
                system.dns.show(SystemConsts.DNS_SERVER)).get_returned_value()

            assert len(dns_output) == max_dns_servers, \
                f"Expected {max_dns_servers} DNS servers, but found {len(dns_output)}"

            for dns_server in dns_servers:
                assert dns_server in dns_output, \
                    f"The configured DNS server {dns_server} is not present in show system dns"

        with allure.step(f'Verify all {max_dns_servers} DNS servers are present in resolv.conf'):
            verify_dns_in_resolv_file(engines, dns_servers)

        with allure.step(f'Run set system dns server {dns_server_beyond_limit} and verify max limit error'):
            system.dns.set(op_param_name=SystemConsts.DNS_SERVER,
                           op_param_value=dns_server_beyond_limit,
                           apply=True,
                           dut_engine=engines.dut).verify_result(should_succeed=False,
                                                                 expected_value=f"The maximum number {max_dns_servers} of DNS servers exceeded")

        with allure.step(f'Verify only {max_dns_servers} DNS servers remain in show system dns server output'):
            dns_output = OutputParsingTool.parse_json_str_to_dictionary(
                system.dns.show(SystemConsts.DNS_SERVER)).get_returned_value()

            assert len(dns_output) == max_dns_servers, \
                f"Expected {max_dns_servers} DNS servers, but found {len(dns_output)}"

            for dns_server in dns_servers:
                assert dns_server in dns_output, \
                    f"The configured DNS server {dns_server} is not present in show system dns"

            assert dns_server_beyond_limit not in dns_output, \
                f"The DNS server {dns_server_beyond_limit} should not be present in show system dns"

        with allure.step(f'Verify resolv.conf still contains only {max_dns_servers} DNS servers'):
            verify_dns_in_resolv_file(engines, dns_servers)
            verify_dns_in_resolv_file(engines, [dns_server_beyond_limit], is_present=False)

    finally:
        clear_system_dns(system, engines)


@pytest.mark.dns
@pytest.mark.cumulus_only
@pytest.mark.parametrize('test_api', ApiType.ALL_TYPES)
def test_dns_server_priority(engines, devices, test_api, dns_config_backup_and_restore):
    """
    Test DNS server and search configuration with priorities
        Test flow:
        1. Configure multiple IPv4 DNS servers with different priorities
        2. Configure DNS search domains with different priorities
        3. Verify that 'nv show system dns server' displays the configured priorities correctly
        4. Verify that 'nv show system dns search' displays the configured search priorities
        5. Unset the dns search and dns server
    """
    TestToolkit.tested_api = test_api
    system = System()
    priorities = CumulusConsts.DNS_PRIORITIES
    ipv4_dns_ips = CumulusConsts.DNS_SERVER_IDS_V4_LIST
    ipv6_dns_ips = CumulusConsts.DNS_SERVER_IDS_V6_LIST
    dns_searches = CumulusConsts.DNS_SEARCHES_LIST

    try:
        with allure.step('Configure IPv4 DNS servers with priorities'):
            for ip, priority in zip(ipv4_dns_ips, priorities):
                with allure.step(f'Set DNS server {ip} with priority {priority}'):
                    system.dns.server.server_id[ip].set(CumulusConsts.DNS_PRIORITY, priority, apply=True,
                                                        ask_for_confirmation=devices.dut.ask_for_confirmation, dut_engine=engines.dut).verify_result()

        with allure.step('Configure IPv6 DNS servers with priorities'):
            for ip, priority in zip(ipv6_dns_ips, priorities):
                with allure.step(f'Set DNS server {ip} with priority {priority}'):
                    system.dns.server.server_id[ip].set(CumulusConsts.DNS_PRIORITY, priority, apply=True,
                                                        dut_engine=engines.dut).verify_result()

        with allure.step('Configure DNS searches with priorities'):
            for search, priority in zip(dns_searches, priorities):
                with allure.step(f'Set DNS search {search} with priority {priority}'):
                    system.dns.search.search_id[search].set(CumulusConsts.DNS_PRIORITY, priority, apply=True,
                                                            dut_engine=engines.dut).verify_result()

        with allure.step('Verify DNS server priorities in show output'):
            dns_output = OutputParsingTool.parse_json_str_to_dictionary(system.dns.show(SystemConsts.DNS_SERVER)).\
                get_returned_value()
            for ip, priority in zip(ipv4_dns_ips, priorities):
                assert ip in dns_output, f"DNS server {ip} is not present in show system dns"
                assert dns_output[ip][CumulusConsts.DNS_PRIORITY] == priority, f"Priority for dns server {ip} is not {priority}"

            for ip, priority in zip(ipv6_dns_ips, priorities):
                assert ip in dns_output, f"DNS server {ip} is not present in show system dns"
                assert dns_output[ip][CumulusConsts.DNS_PRIORITY] == priority, f"Priority for dns server {ip} is not {priority}"

        with allure.step('Verify DNS search priorities in show output'):
            dns_search_output = OutputParsingTool.parse_json_str_to_dictionary(system.dns.show(CumulusConsts.DNS_SEARCH)).\
                get_returned_value()

            for search, priority in zip(dns_searches, priorities):
                assert search in dns_search_output, f"DNS search {search} is not present in show system dns search"
                assert dns_search_output[search][CumulusConsts.DNS_PRIORITY] == priority, f"Priority for dns search {search} is not {priority}"

        with allure.step('Run unset DNS servers with priorities'):
            for ip, priority in zip(ipv4_dns_ips, priorities):
                with allure.step(f'Unset DNS server {ip} with priority {priority}'):
                    system.dns.server.server_id[ip].unset(CumulusConsts.DNS_PRIORITY, apply=True,
                                                          dut_engine=engines.dut).verify_result()
            for ip, priority in zip(ipv6_dns_ips, priorities):
                with allure.step(f'Unset DNS server {ip} with priority {priority}'):
                    system.dns.server.server_id[ip].unset(CumulusConsts.DNS_PRIORITY, apply=True,
                                                          dut_engine=engines.dut).verify_result()

        with allure.step('Verify DNS servers with priorities are not present in show system dns server output'):
            dns_server_output = OutputParsingTool.parse_json_str_to_dictionary(system.dns.show(SystemConsts.DNS_SERVER)).\
                get_returned_value()
            for ip, priority in zip(ipv4_dns_ips, priorities):
                assert CumulusConsts.DNS_PRIORITY not in dns_server_output[ip], f"Priority for {ip} is present in show system dns"
            for ip, priority in zip(ipv6_dns_ips, priorities):
                assert CumulusConsts.DNS_PRIORITY not in dns_server_output[ip], f"Priority for {ip} is present in show system dns"

        with allure.step('Run unset DNS searches with priorities'):
            for search, priority in zip(dns_searches, priorities):
                with allure.step(f'Unset DNS search {search} with priority {priority}'):
                    system.dns.search.search_id[search].unset(CumulusConsts.DNS_PRIORITY, apply=True,
                                                              dut_engine=engines.dut).verify_result()

        with allure.step('Verify DNS searches with priorities are not present in show system dns search output'):
            dns_search_output = OutputParsingTool.parse_json_str_to_dictionary(system.dns.show(CumulusConsts.DNS_SEARCH)).\
                get_returned_value()
            for search, priority in zip(dns_searches, priorities):
                assert CumulusConsts.DNS_PRIORITY not in dns_search_output[search], f"Priority for {search} is present in show system dns"

    finally:
        with allure.step('Run unset dns searches and servers'):
            system.dns.search.unset(apply=True, dut_engine=engines.dut).verify_result()
            clear_system_dns(system, engines)


@pytest.mark.dns
@pytest.mark.cumulus_only
def test_dns_domain_config(engines, devices, dns_config_backup_and_restore):
    """
    Verify that able to configure the dns server for mgmt and search with different priority.
    1. Configure the dns server for mgmt vrf with priority and search with priority
    2. Verify the configured value in show cli output
    3. Perform nslookup and verify minimum priority server is used
    4. Configure DNS searches with priorities
    5. Verify DNS searches in show output
    6. Perform nslookup and verify minimum priority server is used
    7. Unset the dns search and dns server
    8. Verify that invalid domain name config throws error
    9. Verify that the nvued service restart syslog shows correct hostname
    10. Try setting invalid host name and verify that after reboot the device domain and hostname is as expected
    """
    system = System()
    priorities = CumulusConsts.DNS_PRIORITIES
    ipv4_dns_ips = CumulusConsts.DNS_SERVER_IDS_V4_LIST
    dns_searches = CumulusConsts.DNS_SEARCHES_LIST
    dns_domain = CumulusConsts.DNS_DOMAIN_LIST

    try:
        with allure.step('Configure DNS servers with priorities and VRF mgmt'):
            for ip, priority in zip(ipv4_dns_ips, priorities):
                with allure.step(f'Set DNS server {ip} with priority {priority} and VRF mgmt'):
                    system.dns.server.server_id[ip].set(CumulusConsts.DNS_PRIORITY, priority, apply=True,
                                                        dut_engine=engines.dut, ask_for_confirmation=devices.dut.ask_for_confirmation)

                    system.dns.server.server_id[ip].set(CumulusConsts.DNS_VRF, CumulusConsts.DNS_VRF_MGMT, apply=True,
                                                        dut_engine=engines.dut).verify_result()
        with allure.step('Configure DNS domain'):
            system.dns.set(CumulusConsts.DNS_DOMAIN, CumulusConsts.DNS_DOMAIN_LIST[0], apply=True, ask_for_confirmation=devices.dut.ask_for_confirmation,
                           dut_engine=engines.dut)
            # file_source(engines)
            capture_dns_via_ssh_with_pexpect(engines, CumulusConsts.DNS_DOMAIN_LIST[0], "cumulus." + CumulusConsts.DNS_DOMAIN_LIST[0], "cumulus")

        with allure.step('Configure DNS domain'):
            system.dns.set(CumulusConsts.DNS_DOMAIN, CumulusConsts.DNS_DOMAIN_LIST[0], apply=True,
                           dut_engine=engines.dut, ask_for_confirmation=devices.dut.ask_for_confirmation).verify_result()

        with allure.step('Verify DNS domain in show output'):
            dns_domain_output = OutputParsingTool.parse_json_str_to_dictionary(
                system.dns.show()
            ).get_returned_value()
            assert dns_domain_output[CumulusConsts.DNS_DOMAIN] == CumulusConsts.DNS_DOMAIN_LIST[0], f"DNS domain is not {CumulusConsts.DNS_DOMAIN_LIST[0]}, got {dns_domain_output}"

        with allure.step('Perform nslookup and verify minimum priority server is used'):
            perform_nslookup_and_verify(system, dns_domain[0], min(priorities))

        with allure.step('Configure DNS searches with priorities'):
            for search, priority in zip(dns_searches, priorities):
                with allure.step(f'Set DNS search {search} with priority {priority}'):
                    system.dns.search.search_id[search].set(CumulusConsts.DNS_PRIORITY, priority, apply=True,
                                                            dut_engine=engines.dut, ask_for_confirmation=devices.dut.ask_for_confirmation).verify_result()

        with allure.step('Perform search verification and verify minimum priority server is used'):
            perform_search_verification(system, dns_domain[2], min(priorities))
        with allure.step("Try setting domain name to invalid domain name"):
            invalid_domains = ["exam%ple.com", "=invalid.com", "inv@lid.com", "space name.com"]
            for domain in invalid_domains:
                system.dns.set(CumulusConsts.DNS_DOMAIN, domain, apply=True, dut_engine=engines.dut, ask_for_confirmation=devices.dut.ask_for_confirmation).verify_result(should_succeed=False)
            NvueGeneralCli.detach_config(TestToolkit.engines.dut)
            system.dns.set(CumulusConsts.DNS_DOMAIN, CumulusConsts.DNS_DOMAIN_LIST[0], apply=True,
                           dut_engine=engines.dut, ask_for_confirmation=devices.dut.ask_for_confirmation).verify_result()

            system.set(SystemConsts.HOSTNAME, "host.name", apply=True, dut_engine=engines.dut, ask_for_confirmation=devices.dut.ask_for_confirmation).verify_result(should_succeed=False)

            NvueGeneralCli.detach_config(TestToolkit.engines.dut)

        with allure.step("Restart nvued and verify that hostname is still same in syslog message"):
            engines.dut.run_cmd('sudo systemctl restart nvued')
            # Wait for service to restart
            time.sleep(10)

            hostname = get_hostname(engines)
            out = engines.dut.run_cmd("sudo tail -20 /var/log/syslog")
            pattern = rf'({re.escape(hostname)})\s.*Started\s+nvued.service'
            matches = re.findall(pattern, out)
            assert matches, f"Hostname is not updated in syslog message, expected {hostname}, actual {out}"

        with allure.step("Try setting invalid host name and verify that after reboot the domain name is still intact"):
            hosts_file_backup = engines.dut.run_cmd("cat /etc/hosts")

            engines.dut.run_cmd("echo 'cumulus_' > /etc/hostname")

            reload_cmd_set = "nv action reboot system"
            # Reload system and wait until the system is ready
            DutUtilsTool.reload(engine=engines.dut, device=TestToolkit.devices.dut, command=reload_cmd_set, confirm=True, reboot_params=RebootParams(should_wait_till_system_ready=True)).verify_result()

            # Reconnect
            ssh_connection = ConnectionTool.create_ssh_conn(engines.dut.ip, engines.dut.username, engines.dut.password).get_returned_value()
            dns_domain_output = OutputParsingTool.parse_json_str_to_dictionary(system.dns.show()).get_returned_value()
            assert dns_domain_output[CumulusConsts.DNS_DOMAIN] == CumulusConsts.DNS_DOMAIN_LIST[0], f"DNS domain is not {CumulusConsts.DNS_DOMAIN_LIST[0]}, got {dns_domain_output}"
            capture_dns_via_ssh_with_pexpect(engines, CumulusConsts.DNS_DOMAIN_LIST[0], "cumulus." + CumulusConsts.DNS_DOMAIN_LIST[0], "cumulus")

    finally:
        with allure.step('Run unset dns searches and domain'):
            system.dns.search.unset(apply=True, dut_engine=engines.dut).verify_result()
            system.dns.unset(CumulusConsts.DNS_DOMAIN, apply=True, dut_engine=engines.dut, ask_for_confirmation=devices.dut.ask_for_confirmation).verify_result()
        clear_system_dns(system, engines)


@pytest.mark.dns
@pytest.mark.cumulus_only
def test_dns_domain_multi_user(engines, devices, dns_config_backup_and_restore):
    """
    Verify when configuring new user the domain name changes are reflected in new use login session too.
    1. Configure the dns server for mgmt vrf with priority and search with priority
    2. Configure new user other than user cumulus
    3. Configure dns domain name
    4. Ssh to device using new user configured
    5. Verify that in the new session the dns domain name is as configured
    6. Unset the dns search and dns server
    """
    system = System()
    priorities = CumulusConsts.DNS_PRIORITIES
    ipv4_dns_ips = CumulusConsts.DNS_SERVER_IDS_V4_LIST
    dns_searches = CumulusConsts.DNS_SEARCHES_LIST
    dns_domain = CumulusConsts.DNS_DOMAIN_LIST
    try:
        with allure.step('Configure DNS servers with priorities and VRF mgmt'):
            for ip, priority in zip(ipv4_dns_ips, priorities):
                with allure.step(f'Set DNS server {ip} with priority {priority} and VRF mgmt'):
                    system.dns.server.server_id[ip].set(CumulusConsts.DNS_PRIORITY, priority, apply=True, dut_engine=engines.dut, ask_for_confirmation=devices.dut.ask_for_confirmation)
                    system.dns.server.server_id[ip].set(CumulusConsts.DNS_VRF, CumulusConsts.DNS_VRF_MGMT, apply=True, dut_engine=engines.dut).verify_result()

        with allure.step('Create a new user '):
            user1_plain_password = generate_strong_password()
            salt = crypt.mksalt(crypt.METHOD_SHA512)
            user_local_hashpw = f"'{crypt.crypt(user1_plain_password, salt)}'"
            system.aaa.user.set_new_user(username='user1', role='nvue-admin', hashed_password=user_local_hashpw, apply=True)
            system.dns.set(CumulusConsts.DNS_DOMAIN, CumulusConsts.DNS_DOMAIN_LIST[0], apply=True,
                           dut_engine=engines.dut, ask_for_confirmation=devices.dut.ask_for_confirmation).verify_result()
            capture_dns_via_ssh_with_pexpect(engines, CumulusConsts.DNS_DOMAIN_LIST[0], "cumulus." + CumulusConsts.DNS_DOMAIN_LIST[0], "cumulus")

    finally:
        with allure.step('Run unset dns searches and domain'):
            system.dns.search.unset(apply=True, dut_engine=engines.dut).verify_result()
            system.dns.unset(CumulusConsts.DNS_DOMAIN, apply=True, dut_engine=engines.dut, ask_for_confirmation=devices.dut.ask_for_confirmation).verify_result()
            clear_system_dns(system, engines)


@pytest.mark.cumulus_only
@pytest.mark.dns
def test_dns_server_and_search_priority_nslookup(engines, devices, dns_config_backup_and_restore):
    """
    1. Configure the dns server for mgmt vrf with ipv4
    2. Configure the dns search with priority
    3. Perform nslookup for domain redmine
    4. Swap the priority of the DNS servers and searches
    5. Perform nslookup for domain redmine after priority swap
    6. Remove the dns search and dns server with lowest priority
    7. Unset the dns search and dns server
    """
    system = System()

    priorities = CumulusConsts.DNS_PRIORITIES
    ipv4_dns_ips = CumulusConsts.DNS_SERVER_IDS_V4_LIST
    dns_searches = CumulusConsts.DNS_SEARCHES_LIST
    dns_domain = CumulusConsts.DNS_DOMAIN_LIST

    try:
        with allure.step("Configuring DNS servers and searches with valid priorities"):
            for ip, priority in zip(ipv4_dns_ips, priorities):
                with allure.step(f'Set DNS server {ip} with priority {priority} and VRF mgmt'):
                    system.dns.server.server_id[ip].set(CumulusConsts.DNS_PRIORITY, priority, apply=True,
                                                        dut_engine=engines.dut, ask_for_confirmation=devices.dut.ask_for_confirmation).verify_result()
                    system.dns.server.server_id[ip].set(CumulusConsts.DNS_VRF, CumulusConsts.DNS_VRF_MGMT, apply=True,
                                                        dut_engine=engines.dut).verify_result()

            for search, priority in zip(dns_searches, priorities):
                with allure.step(f'Set DNS search {search} with priority {priority}'):
                    system.dns.search.search_id[search].set(CumulusConsts.DNS_PRIORITY, priority, apply=True,
                                                            dut_engine=engines.dut).verify_result()

        with allure.step("Performing initial nslookup and verification"):
            server_address = perform_nslookup_and_verify(system, dns_domain[1], min(priorities))

        with allure.step("Swapping priorities for DNS servers"):
            dns_server_output = OutputParsingTool.parse_json_str_to_dictionary(
                system.dns.server.server_id[ipv4_dns_ips[1]].show()
            ).get_returned_value()
            ip1_priority = dns_server_output[CumulusConsts.DNS_PRIORITY]

            system.dns.server.server_id[server_address].set(CumulusConsts.DNS_PRIORITY, ip1_priority, apply=True,
                                                            dut_engine=engines.dut).verify_result()
            system.dns.server.server_id[ipv4_dns_ips[1]].set(CumulusConsts.DNS_PRIORITY, min(priorities), apply=True,
                                                             dut_engine=engines.dut).verify_result()

        with allure.step("Performing nslookup and verification again"):
            perform_nslookup_and_verify(system, dns_domain[1], min(priorities))

        with allure.step("Performing search verification for 'redmine'"):
            domain_name = perform_search_verification(system, dns_domain[2], min(priorities))

        with allure.step("Swapping priorities for DNS searches"):
            dns_search_output = OutputParsingTool.parse_json_str_to_dictionary(
                system.dns.search.search_id[dns_searches[1]].show()
            ).get_returned_value()
            search1_priority = dns_search_output[CumulusConsts.DNS_PRIORITY]

            system.dns.search.search_id[domain_name].set(CumulusConsts.DNS_PRIORITY, search1_priority, apply=True,
                                                         dut_engine=engines.dut).verify_result()
            system.dns.search.search_id[dns_searches[1]].set(CumulusConsts.DNS_PRIORITY, min(priorities), apply=True,
                                                             dut_engine=engines.dut).verify_result()

        with allure.step("Performing search verification again"):
            perform_search_verification(system, dns_domain[2], min(priorities))

    finally:
        with allure.step('Run unset dns search'):
            system.dns.search.unset(apply=True, dut_engine=engines.dut).verify_result()
        clear_system_dns(system, engines)


@pytest.mark.dns
@pytest.mark.cumulus_only
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
def test_dns_server_and_search_non_mgmt_vrf(engines, serial_engine, devices, test_api, dns_config_backup_and_restore):
    """
    1. Configure the dns server for default, RED vrf with ipv4
    2. Configure the dns search with priority
    3. Perform nslookup for domain redmine
    4. Unset the dns search and dns server
    """
    system = System()
    vrf_obj = Vrf()
    priorities = CumulusConsts.DNS_PRIORITIES
    mgmt_ipv4_dns_ips = re.search(r"nameserver\s+([0-9\.]+)", backup_resolv_conf).group(1)
    dns_searches = CumulusConsts.DNS_SEARCHES_LIST
    vrf_list = CumulusConsts.DNS_VRF_LIST
    mgmt_port = Port(devices.dut.get_mgmt_ports()[0])

    try:
        with allure.step("Configuring DNS servers and searches with valid priorities for each VRF"):
            for search, priority, vrf_name in zip(dns_searches, priorities, vrf_list):
                with allure.step(f'Set DNS server {mgmt_ipv4_dns_ips} with priority {priority} and VRF {vrf_name}'):
                    vrf_obj.vrf_id[vrf_name].set(apply=False, dut_engine=engines.dut).verify_result()
                    system.dns.server.server_id[mgmt_ipv4_dns_ips].set(CumulusConsts.DNS_PRIORITY, priority, apply=True,
                                                                       dut_engine=engines.dut, ask_for_confirmation=devices.dut.ask_for_confirmation).verify_result()
                    system.dns.server.server_id[mgmt_ipv4_dns_ips].set(CumulusConsts.DNS_VRF, vrf_name, apply=True, dut_engine=engines.dut, ask_for_confirmation=devices.dut.ask_for_confirmation).verify_result()

                with allure.step(f'Set DNS search {search} with priority {priority}'):
                    system.dns.search.search_id[search].set(CumulusConsts.DNS_PRIORITY, priority, apply=True, dut_engine=engines.dut).verify_result()
                with allure.step(f'Set vrf {vrf_name} for mgmt port'):
                    mgmt_port.interface.vrf.set(vrf_name, apply=True, dut_engine=serial_engine, ask_for_confirmation=devices.dut.ask_for_confirmation).verify_result()
                    check_port_status_till_alive(True, engines.dut.ip, engines.dut.ssh_port)

                # Logout to get proper show results
                with allure.step(f'Logout serial engine session after VRF {vrf_name} configuration'):
                    # More robust logout - send multiple logout signals and wait
                    for _ in range(2):
                        serial_engine.serial_engine.sendcontrol('d')
                        time.sleep(1)
                    serial_engine.serial_engine.close()

                with allure.step(f'Create fresh serial connection for next VRF configuration'):
                    from ngts.nvos_tools.infra.ConnectionTool import ConnectionTool
                    topology_obj = TestToolkit.topology_obj
                    serial_engine = ConnectionTool.create_serial_connection(topology_obj, devices, force_new_login=True)

                with allure.step(f"Performing nslookup and verification for VRF {vrf_name}"):
                    perform_nslookup_and_verify(system, CumulusConsts.DNS_DOMAIN_LIST[2], min(priorities), engine=serial_engine)
    finally:
        with allure.step("Unset the dns search and dns server"):
            system.dns.search.unset(apply=True, dut_engine=serial_engine, ask_for_confirmation=devices.dut.ask_for_confirmation).verify_result()
            system.dns.unset(SystemConsts.DNS_SERVER, apply=True, dut_engine=serial_engine).verify_result()
            mgmt_port.interface.vrf.set("mgmt", apply=True, dut_engine=serial_engine, ask_for_confirmation=devices.dut.ask_for_confirmation).verify_result()
            check_port_status_till_alive(True, engines.dut.ip, engines.dut.ssh_port)
            vrf_obj.unset(apply=True, dut_engine=serial_engine).verify_result()
        clear_system_dns(system, engines)


@pytest.mark.dns
@pytest.mark.cumulus_only
def test_dns_lookup_for_ipv6_dns_via_ipv4(engines, test_api, devices, dns_config_backup_and_restore):
    """
    1. Configure the dns server for mgmt vrf with ipv4
    2. Configure the dns search with priority
    3. Perform nslookup for AAAA and A record of google.com
    4. Swap the priority of the DNS servers
    5. Perform nslookup for AAAA and A record of google.com after priority swap
    6. Unset the dns search and dns server
    """
    TestToolkit.tested_api = test_api
    system = System()
    priorities = CumulusConsts.DNS_PRIORITIES
    ipv4_dns_ips = CumulusConsts.DNS_SERVER_IDS_V4_LIST
    dns_searches = CumulusConsts.DNS_SEARCHES_LIST

    try:
        with allure.step("Configuring DNS servers and searches with valid priorities"):
            for ip, search, priority in zip(ipv4_dns_ips, dns_searches, priorities):
                with allure.step(f'Set DNS server {ip} with priority {priority} and VRF mgmt'):
                    system.dns.server.server_id[ip].set(CumulusConsts.DNS_PRIORITY, priority, apply=True,
                                                        dut_engine=engines.dut, ask_for_confirmation=devices.dut.ask_for_confirmation).verify_result()
                    system.dns.server.server_id[ip].set(CumulusConsts.DNS_VRF, CumulusConsts.DNS_VRF_MGMT, apply=True,
                                                        dut_engine=engines.dut).verify_result()

                with allure.step(f'Set DNS search {search} with priority {priority}'):
                    system.dns.search.search_id[search].set(CumulusConsts.DNS_PRIORITY, priority, apply=True,
                                                            dut_engine=engines.dut).verify_result()

        with allure.step("Performing nslookup for AAAA and A record of google.com"):
            perform_nslookup_ipv4_ipv6_check(engines)

        with allure.step("Swapping the priority of the DNS servers"):
            reversed_priorities = list(reversed(priorities))
            for ip, priority in zip(ipv4_dns_ips, reversed_priorities):
                with allure.step(f'Reconfigure DNS server {ip} with new priority {priority}'):
                    system.dns.server.server_id[ip].set(CumulusConsts.DNS_PRIORITY, priority, apply=True,
                                                        dut_engine=engines.dut).verify_result()

        with allure.step("Performing nslookup for AAAA and A record of google.com after priority swap"):
            perform_nslookup_ipv4_ipv6_check(engines)

    finally:
        with allure.step('Run unset dns search'):
            system.dns.search.unset(apply=True, dut_engine=engines.dut).verify_result()
        clear_system_dns(system, engines)


def verify_dns_in_resolv_file(engines, dns_server_id_list, is_present=True):
    is_present_str = ""
    if not is_present:
        is_present_str = "not"
    for dns_server_id in dns_server_id_list:
        with allure.step('Verify in resolv.conf file that it does {} have DNS server {}'.format(is_present_str,
                                                                                                dns_server_id)):
            cmd = 'cat /etc/resolv.conf'
            resolve_file_output = engines.dut.run_cmd(cmd)
            if is_present:
                assert dns_server_id in resolve_file_output, 'DNS {} is not present in resolv.conf'
            else:
                assert dns_server_id not in resolve_file_output, 'DNS {} is present in resolv.conf'.\
                    format(dns_server_id)


def perform_nslookup_and_verify(system, domain, expected_priority, engine=None):
    if engine is None:
        engine = TestToolkit.engines.dut

    with allure.step(f"Performing nslookup for domain {domain}"):
        if hasattr(engine, 'serial_engine'):
            nslookup_output = get_complete_nslookup_output(engine, domain)
        else:
            nslookup_output = engine.run_cmd(f"nslookup {domain}")

        server_address = extract_server_address(nslookup_output)
        logger.info(f"Extracted Server Address: {server_address}")
        dns_server_output = OutputParsingTool.parse_json_str_to_dictionary(
            system.dns.server.server_id[server_address].show(dut_engine=engine)
        ).get_returned_value()
        return server_address


def get_complete_nslookup_output(serial_engine, domain):
    serial_engine.serial_engine.sendline(f"nslookup {domain}")
    serial_engine.serial_engine.expect(['cumulus@cumulus:~$', 'cumulus@cumulus:\\~$', r'\$'], timeout=30)
    complete_output = serial_engine.serial_engine.before.decode('utf-8', errors='replace')
    logger.info(f"Complete nslookup output: {complete_output}")
    return complete_output


def perform_search_verification(system, domain, expected_priority, engine=None):
    """Perform search verification for a domain"""
    # Use provided engine or fall back to TestToolkit.engines.dut
    if engine is None:
        engine = TestToolkit.engines.dut

    with allure.step(f"Performing search verification for domain {domain}"):
        nslookup_output = engine.run_cmd(f"nslookup {domain}")
        match = re.search(r"Name:\s*(\S+)", nslookup_output)
        search_name = match.group(1) if match else None
        logger.info(f"Extracted Search Name: {search_name}")

        if search_name:
            domain_name = search_name.split(f'{domain}.')[1]
            logger.info(f"Extracted Domain Name: {domain_name}")

            dns_search_output = OutputParsingTool.parse_json_str_to_dictionary(
                system.dns.search.search_id[domain_name].show()
            ).get_returned_value()
            search_priority = dns_search_output[CumulusConsts.DNS_PRIORITY]

            assert search_priority == expected_priority, (
                f"Search name {search_name} does not have the expected priority. "
                f"Expected: {expected_priority}, Got: {search_priority}"
            )
            logger.info(f"Verified search name {search_name} has the expected priority {expected_priority}.")
            return domain_name


def extract_server_address(nslookup_output):
    """
    Extracts the server address from the nslookup output.
    :param nslookup_output: The output string from the nslookup command.
    :return: The server address as a string.
    """
    lines = nslookup_output.splitlines()
    for line in lines:
        if line.startswith("Server:"):
            return line.split()[1]
    return None


def extract_valid_ipv6_addresses(text):
    """
    Extracts valid IPv6 addresses from a given text.
    :param text: The text containing potential IPv6 addresses.
    :return: A list of valid IPv6 addresses.
    """
    candidates = re.findall(r'[\da-fA-F:%]+', text)
    ipv6_addresses = []

    for candidate in candidates:
        try:
            ip = ipaddress.ip_address(candidate)
            if isinstance(ip, ipaddress.IPv6Address):
                ipv6_addresses.append(candidate)
        except ValueError:
            continue

    return ipv6_addresses


def perform_nslookup_ipv4_ipv6_check(engines):
    """
    Perform nslookup for both IPv6 (AAAA) and IPv4 (A) records of google.com,
    and log the extracted addresses.
    """
    with allure.step("Performing nslookup for AAAA record of google.com"):
        nslookup_output = engines.dut.run_cmd("nslookup -type=AAAA google.com")
        logger.info(f"Nslookup output (IPv6): {nslookup_output}")

        ipv6_matches = extract_valid_ipv6_addresses(nslookup_output)
        assert ipv6_matches, "No valid IPv6 addresses found in nslookup output."

        for ipv6_address in ipv6_matches:
            logger.info(f"Extracted valid IPv6 Address: {ipv6_address}")

    with allure.step("Performing nslookup for A record of google.com to check IPv4 address usage"):
        nslookup_output_ipv4_check = engines.dut.run_cmd("nslookup -type=A google.com")
        logger.info(f"Nslookup output (IPv4): {nslookup_output_ipv4_check}")

        ipv4_matches_check = re.findall(r"Address:\s*([\d.]+)", nslookup_output_ipv4_check)
        assert ipv4_matches_check, "No IPv4 addresses found in nslookup output."

        for ipv4_address in ipv4_matches_check:
            logger.info(f"Extracted IPv4 Address: {ipv4_address}")


def get_hostname(engines):
    with allure.step("Get the dut hostname"):
        hostname = engines.dut.run_cmd("hostname")
        return hostname


def file_source(engines):
    with allure.step("source the bash file to get domain reflected in FQDN"):
        engines.dut.run_cmd("source /etc/bash.bashrc")


def check_dns_commands(engines, domain_name, fqdn, hostname):
    with allure.step("Verify the dns domain name in commands"):

        dns_domain_result = engines.dut.run_cmd("dnsdomainname")
        hostname_f_result = engines.dut.run_cmd("hostname -f")
        dns_output = OutputParsingTool.parse_json_str_to_dictionary(system.dns.show()).get_returned_value()

        assert domain_name in dns_domain_result, f"Domain name {domain_name} not found in dnsdomainname output: {dns_domain_result}"
        assert fqdn in hostname_f_result, f"FQDN {fqdn} not found in hostname -f output: {hostname_f_result}"
        assert hostname in hostname_f_result, f"Hostname {hostname} not found in hostname -f output: {hostname_f_result}"
        assert domain_name in dns_output, f"Domain name {domain_name} not found in dnsdomainname output: {dns_domain_result}"


def capture_dns_via_ssh_with_pexpect(engines, domain_name, fqdn, hostname):
    """
    Helper function to capture logout message using pexpect with SSH
    """
    import pexpect

    ssh_cmd = f"ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null {engines.dut.username}@{engines.dut.ip}"

    try:
        child = pexpect.spawn(ssh_cmd, timeout=60)

        # Login
        child.expect("password:")
        child.sendline(engines.dut.password)
        child.expect(['$', '#'])

        # Send logout and capture everything before connection closes
        child.sendline('hostname -f')
        child.expect(['$', '#'], timeout=30)
        hostname_f_result = child.before.decode('utf-8', errors='ignore')
        assert fqdn in hostname_f_result, f"FQDN {fqdn} not found in hostname -f output: {hostname_f_result}"
        assert hostname in hostname_f_result, f"Hostname {hostname} not found in hostname -f output: {hostname_f_result}"

        child.sendline("dnsdomainname")
        child.expect(['$', '#'], timeout=30)
        dns_domain_result = child.before.decode('utf-8', errors='ignore')
        assert domain_name in dns_domain_result, f"Domain name {domain_name} not found in dnsdomainname output: {dns_domain_result}"

        dns_output = OutputParsingTool.parse_json_str_to_dictionary(system.dns.show()).get_returned_value()

        assert domain_name in dns_output, f"Domain name {domain_name} not found in dnsdomainname output: {dns_domain_result}"

    except Exception as e:
        logger.error(f"Failed to capture logout message: {e}")
        return ""
