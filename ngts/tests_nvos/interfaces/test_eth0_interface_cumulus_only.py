import pytest

from ngts.nvos_tools.infra.IpTool import IpTool
from ngts.nvos_tools.infra.Tools import Tools
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import *
from ngts.tools.test_utils import allure_utils as allure


cumulus_owner = "bravindranat"


logger = logging.getLogger()


@pytest.mark.cumulus
@pytest.mark.ib
@pytest.mark.skynet
def test_mgmt_interface_mac(engines, serial_engine):
    """
    Run show interface eth0 link command and verify eth0 mac address in OS is expected
    Reboot switch into ONIE
    Run ifconfig eth0 and verify eth0 mac address in ONIE is the same as OS expected
    Reboot switch back into OS
    """

    mgmt_port = Port()
    with allure.step('Run show command on selected port and verify that each field has an appropriate '
                     'value according to the state of the port'):
        output_dictionary = Tools.OutputParsingTool.parse_show_interface_link_output_to_dictionary(
            mgmt_port.interface.link.show()).get_returned_value()

        validate_link_fields(output_dictionary)
        mac = IpTool.get_mac_address(engines.dut, mgmt_port.name)
        verify_mac_address(mac, output_dictionary)

    with allure.step('Reboot into onie'):
        serial_engine.serial_engine.sendline("sudo onie-select -r")
        serial_engine.serial_engine.expect("Are you sure (y/N)?", timeout=10)
        serial_engine.serial_engine.sendline("y")
        serial_engine.serial_engine.expect("Reboot required to take effect.", timeout=10)
        serial_engine.serial_engine.sendline("sudo reboot")
        serial_engine.serial_engine.expect("Please press Enter to activate this console.", timeout=300)
        serial_engine.serial_engine.sendline("")
        serial_engine.serial_engine.expect("ONIE:/ #", timeout=10)

    mac = mac.upper()
    with allure.step('Check eth0 mac in ONIE'):
        try:
            serial_engine.serial_engine.sendline("ifconfig eth0")
            serial_engine.serial_engine.expect(mac, timeout=10)
        except BaseException:
            serial_engine.serial_engine.sendline("reboot")
            serial_engine.serial_engine.expect("login:", timeout=300)
            assert False, f"ONIE mac does not match {mac}"

    with allure.step('Reboot into CL '):
        serial_engine.serial_engine.sendline("reboot")
        serial_engine.serial_engine.expect("login:", timeout=300)


def verify_mac_address(expected_mac: str,
                       output_dictionary: dict):
    with allure.step('Verity that MAC address from nv show interface eth0 link matches expected value'):

        mac_address = output_dictionary[IbInterfaceConsts.LINK_MAC]
        assert mac_address == expected_mac, f"MAC address mismatch. Expected: {expected_mac}, Actual: {mac_address}"


def validate_link_fields(output_dictionary):
    with allure.step('Check that all expected fields under link field exist in the output'):
        logging.info('Check that all expected fields under link field exist in the output')
        field_to_check = [IbInterfaceConsts.LINK_MTU,
                          IbInterfaceConsts.LINK_SPEED,
                          IbInterfaceConsts.LINK_MAC,
                          IbInterfaceConsts.LINK_DUPLEX,
                          IbInterfaceConsts.LINK_STATE]
        Tools.ValidationTool.verify_field_exist_in_json_output(output_dictionary, field_to_check).verify_result()
