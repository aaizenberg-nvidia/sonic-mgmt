import logging
import pytest
from ngts.nvos_tools.system.System import System
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.nvos_constants.constants_nvos import SystemConsts
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_constants.constants_nvos import ApiType
from ngts.tools.test_utils.allure_utils import step as allure_step

logger = logging.getLogger()


def clear_system_web_server_api(system, engines):
    """
    Method to unset the system api for state, port number and listening-address
    :param system:  System object
    :param engines: Engines object
    """

    with allure_step('Run unset system external API state command and apply config'):
        system.web_server_api.unset(apply=True, dut_engine=engines.dut).verify_result()


@pytest.mark.webserverapi
@pytest.mark.system
@pytest.mark.simx
def test_show_system_web_server_api(engines):
    """
    Run show/set/unset system api command and verify the required state, port and listening address
        Test flow:
            1. Check show system external API and verify all fields are available
    """

    system = System()

    try:

        with allure_step('Run show system api command and verify that each field has a value'):
            ext_api_output = OutputParsingTool.parse_json_str_to_dictionary(
                system.web_server_api.show()).get_returned_value()
            ValidationTool.verify_field_exist_in_json_output(ext_api_output,
                                                             [SystemConsts.EXTERNAL_API_STATE,
                                                              SystemConsts.EXTERNAL_API_PORT,
                                                              SystemConsts.EXTERNAL_API_LISTEN]).verify_result()

    finally:
        clear_system_web_server_api(system, engines)


@pytest.mark.webserverapi
@pytest.mark.system
@pytest.mark.simx
def test_show_system_web_server_api_conns(engines):
    """
    Run show system api connections command and verify the required connections
        Test flow:
            1. Check show system external API connections and verify connections are displayed
    """
    system = System()

    try:

        with allure_step('Run show system api connections command and verify each field is non-negative'):
            ext_api_conn_output = OutputParsingTool.parse_json_str_to_dictionary(
                system.web_server_api.connections.show()).get_returned_value()
            # JSON -o json returns ints; older text paths may return strings — accept both.
            accepted = int(ext_api_conn_output[SystemConsts.EXTERNAL_API_CONN_ACCEPTED])
            assert accepted >= 0, 'Number of Accepted connections({val}) is negative'.format(val=accepted)
            active = int(ext_api_conn_output[SystemConsts.EXTERNAL_API_CONN_ACTIVE])
            assert active >= 0, 'Number of Active connections({val}) is negative'.format(val=active)
            handled = int(ext_api_conn_output[SystemConsts.EXTERNAL_API_CONN_HANDLED])
            assert handled >= 0, 'Number of Handled connections({val}) is negative'.format(val=handled)
            reading = int(ext_api_conn_output[SystemConsts.EXTERNAL_API_CONN_READING])
            assert reading >= 0, 'Number of Reading connections({val}) is negative'.format(val=reading)
            request = int(ext_api_conn_output[SystemConsts.EXTERNAL_API_CONN_REQUEST])
            assert request >= 0, 'Number of Request connections({val}) is negative'.format(val=request)
            waiting = int(ext_api_conn_output[SystemConsts.EXTERNAL_API_CONN_WAITING])
            assert waiting >= 0, 'Number of Waiting connections({val}) is negative'.format(val=waiting)
            writing = int(ext_api_conn_output[SystemConsts.EXTERNAL_API_CONN_WRITING])
            assert writing >= 0, 'Number of Writing connections({val}) is negative'.format(val=writing)

    finally:
        clear_system_web_server_api(system, engines)


@pytest.mark.webserverapi
@pytest.mark.system
@pytest.mark.simx
def test_show_system_web_server_api_listen_address(engines):
    """
    Run show system api connections command and verify the required connections
        Test flow:
            1. Check show system external API connections and verify connections are displayed
    """
    system = System()

    try:

        with allure_step('Run show system api connections command and verify the output'):
            ext_api_conn_output = OutputParsingTool.parse_json_str_to_dictionary(
                system.web_server_api.listen_address.show()).get_returned_value()
            assert ext_api_conn_output == {SystemConsts.EXTERNAL_API_LISTEN_DEFAULT: {}}, \
                'Output is {output} instead of any'.format(output=type(ext_api_conn_output))

    finally:
        clear_system_web_server_api(system, engines)


@pytest.mark.webserverapi
@pytest.mark.system
@pytest.mark.simx
def test_set_system_web_server_api_state(engines):
    """
    Run set/unset system api command and verify the required state
        Test flow:
            1. Set external api state to 'disabled'[run cmd + apply conf]
            2. Verify external api state changed to 'disabled' in show system
            3. Verify external api state actual gets disabled wrt functionality
            4. Unset external api state [run cmd + apply conf]
            5. Verify external api state changed to default('enabled') in show system
            6. Verify external api state actual gets enabled wrt functionality
    """

    system = System()

    try:
        with allure_step('Run set system external api state command and apply config'):
            system.web_server_api.set(SystemConsts.EXTERNAL_API_STATE,
                                      SystemConsts.EXTERNAL_API_STATE_DISABLED, apply=True, dut_engine=engines.dut)

        with allure_step('Verify external api state changed to new state in show system'):
            ext_api_output = OutputParsingTool.parse_json_str_to_dictionary(
                system.web_server_api.show()).get_returned_value()
            ValidationTool.verify_field_value_in_output(ext_api_output, SystemConsts.EXTERNAL_API_STATE,
                                                        SystemConsts.EXTERNAL_API_STATE_DISABLED).verify_result()

        with allure_step('Verify external api state changed to disabled wrt functionality'):
            # Set API type to OpenAPI to test functionality
            TestToolkit.tested_api = ApiType.OPENAPI
            try:
                system.web_server_api.show()
                assert False, "The Web Server API was not disabled"
            except Exception as ex:
                assert 'ConnectionError' == type(ex).__name__, \
                    "Failed because of unexpected error : {type}".format(type=type(ex).__name__)
            finally:
                # Set API type back to SSH to resume tests
                TestToolkit.tested_api = ApiType.NVUE

        with allure_step('Run unset system external api state command and apply config'):
            system.web_server_api.unset(SystemConsts.EXTERNAL_API_STATE, apply=True, dut_engine=engines.dut)

        with allure_step('Verify external api state changed to default state in show system'):
            ext_api_output = OutputParsingTool.parse_json_str_to_dictionary(
                system.web_server_api.show()).get_returned_value()
            ValidationTool.verify_field_value_in_output(ext_api_output, SystemConsts.EXTERNAL_API_STATE,
                                                        SystemConsts.EXTERNAL_API_STATE_DEFAULT).verify_result()

        with allure_step('Verify external api state changed to enabled wrt functionality'):
            # Set API type to OpenAPI to test functionality
            TestToolkit.tested_api = ApiType.OPENAPI
            try:
                system.web_server_api.show()
            except Exception as ex:
                assert False, "Failed because of error : {type}".format(type=type(ex).__name__)
            finally:
                # Set API type back to SSH to resume tests
                TestToolkit.tested_api = ApiType.NVUE

    finally:
        clear_system_web_server_api(system, engines)


@pytest.mark.webserverapi
@pytest.mark.system
@pytest.mark.simx
def test_set_system_web_server_api_port(engines):
    """
    Run set/unset system api command and verify the required port number
        Test flow:
            1. Set external api port to '442'[run cmd + apply conf]
            2. Verify external api port changed to '442' in show system
            3. Verify external api port actually gets changed to new port wrt functionality
            4. Unset external api port [run cmd + apply conf]
            5. Verify external api port changed to default ('442') in show system
            6. Verify external api port actual gets changed to default wrt functionality
    """
    system = System()

    try:
        with allure_step('Run set system external api port command and apply config'):
            system.web_server_api.set(SystemConsts.EXTERNAL_API_PORT, SystemConsts.EXTERNAL_API_PORT_NON_DEFAULT,
                                      apply=True, dut_engine=engines.dut).verify_result()

        with allure_step('Verify external api port number changed to new port number in show system'):
            ext_api_output = OutputParsingTool.parse_json_str_to_dictionary(
                system.web_server_api.show()).get_returned_value()
            ValidationTool.verify_field_value_in_output(ext_api_output, SystemConsts.EXTERNAL_API_PORT,
                                                        SystemConsts.EXTERNAL_API_PORT_NON_DEFAULT).verify_result()

        with allure_step('Verify external api port changed wrt functionality'):
            # Set API type to OpenAPI to test functionality
            TestToolkit.tested_api = ApiType.OPENAPI
            try:
                system.web_server_api.show()
                assert False, "The Web Server API port was not changed"
            except Exception as ex:
                assert 'ConnectionError' == type(ex).__name__, \
                    "Failed because of unexpected error : {type}".format(type=type(ex).__name__)
            finally:
                # Set API type back to SSH to resume tests
                TestToolkit.tested_api = ApiType.NVUE

        with allure_step('Run unset system external api port command and apply config'):
            system.web_server_api.unset(SystemConsts.EXTERNAL_API_PORT, apply=True, dut_engine=engines.dut).\
                verify_result()

        with allure_step('Verify external api port changed to default port number in show system'):
            ext_api_output = OutputParsingTool.parse_json_str_to_dictionary(
                system.web_server_api.show()).get_returned_value()
            ValidationTool.verify_field_value_in_output(ext_api_output, SystemConsts.EXTERNAL_API_PORT,
                                                        SystemConsts.EXTERNAL_API_PORT_DEFAULT).verify_result()

        with allure_step('Verify external api port changed back to default wrt functionality'):
            # Set API type to OpenAPI to test functionality
            TestToolkit.tested_api = ApiType.OPENAPI
            try:
                system.web_server_api.show()
            except Exception as ex:
                assert False, "Failed because of error : {type}".format(type=type(ex).__name__)
            finally:
                # Set API type back to SSH to resume tests
                TestToolkit.tested_api = ApiType.NVUE

    finally:
        clear_system_web_server_api(system, engines)


@pytest.mark.webserverapi
@pytest.mark.system
@pytest.mark.simx
def test_set_system_web_server_api_listen(engines):
    """
    Run set/unset system api command and verify the listening-address
        Test flow:
            1. Set external api listening-address to 'localhost'[run cmd + apply conf]
            2. Verify external api listening-address changed to 'localhost' in show system
            3. Verify external api listening-address actually gets changed to new port wrt functionality
            4. Unset external api listening-address [run cmd + apply conf]
            5. Verify external api listening-address changed to default (any) in show system
            6. Verify external api port actual gets changed to default wrt functionality
    """

    system = System()
    random_ipv4_address = '192.0.2.146'
    random_ipv6_address = '2001:db8:3333:4444:5555:6666:7777:8888'

    try:
        with allure_step('Run set system external api listening-address command and apply config'):
            system.web_server_api.set(SystemConsts.EXTERNAL_API_LISTEN, SystemConsts.EXTERNAL_API_LISTEN_LOCALHOST,
                                      apply=True, dut_engine=engines.dut).verify_result()

        with allure_step('Verify external api listening-address changed to new address in show system'):
            ext_api_output = OutputParsingTool.parse_json_str_to_dictionary(
                system.web_server_api.show()).get_returned_value()
            ValidationTool.verify_field_value_in_output(ext_api_output, SystemConsts.EXTERNAL_API_LISTEN,
                                                        {SystemConsts.EXTERNAL_API_LISTEN_LOCALHOST: {}}).\
                verify_result()

        with allure_step('Verify external api listening-address changed wrt functionality'):
            # Set API type to OpenAPI to test functionality
            TestToolkit.tested_api = ApiType.OPENAPI
            try:
                system.web_server_api.show()
                assert False, "The Web Server API listening-address was not changed"
            except Exception as ex:
                assert 'ConnectionError' == type(ex).__name__, \
                    "Failed because of unexpected error : {type}".format(type=type(ex).__name__)
            finally:
                # Set API type back to SSH to resume tests
                TestToolkit.tested_api = ApiType.NVUE

        with allure_step('Run unset system external api listening-address command and apply config'):
            system.web_server_api.unset(SystemConsts.EXTERNAL_API_LISTEN, apply=True, dut_engine=engines.dut).\
                verify_result()

        with allure_step('Verify external api port changed to default(any) in show system'):
            ext_api_output = OutputParsingTool.parse_json_str_to_dictionary(
                system.web_server_api.show()).get_returned_value()
            ValidationTool.verify_field_value_in_output(ext_api_output, SystemConsts.EXTERNAL_API_LISTEN,
                                                        {SystemConsts.EXTERNAL_API_LISTEN_DEFAULT: {}}).verify_result()

        with allure_step('Verify external api listening-address changed to default wrt functionality'):
            # Set API type to OpenAPI to test functionality
            TestToolkit.tested_api = ApiType.OPENAPI
            try:
                system.web_server_api.show()
            except Exception as ex:
                assert False, "Failed because of error : {type}".format(type=type(ex).__name__)
            finally:
                # Set API type back to SSH to resume tests
                TestToolkit.tested_api = ApiType.NVUE

        with allure_step('Run set external api listening-address to some ipv4 address command and apply config'):
            system.web_server_api.set(SystemConsts.EXTERNAL_API_LISTEN, random_ipv4_address,
                                      apply=True, dut_engine=engines.dut).verify_result()

        with allure_step('Verify external api listening-address changed to new ipv4 address in show system'):
            ext_api_output = OutputParsingTool.parse_json_str_to_dictionary(
                system.web_server_api.show()).get_returned_value()
            ValidationTool.verify_field_value_in_output(ext_api_output, SystemConsts.EXTERNAL_API_LISTEN,
                                                        {random_ipv4_address: {}}).\
                verify_result()

        with allure_step('Run unset system external api listening-address command and apply config'):
            system.web_server_api.unset(SystemConsts.EXTERNAL_API_LISTEN, apply=True, dut_engine=engines.dut).\
                verify_result()

        with allure_step('Verify external api port changed to default(any) in show system'):
            ext_api_output = OutputParsingTool.parse_json_str_to_dictionary(
                system.web_server_api.show()).get_returned_value()
            ValidationTool.verify_field_value_in_output(ext_api_output, SystemConsts.EXTERNAL_API_LISTEN,
                                                        {SystemConsts.EXTERNAL_API_LISTEN_DEFAULT: {}}).verify_result()

        with allure_step('Run set external api listening-address to some ipv6 address command and apply config'):
            system.web_server_api.set(SystemConsts.EXTERNAL_API_LISTEN, random_ipv6_address,
                                      apply=True, dut_engine=engines.dut).verify_result()

        with allure_step('Verify external api listening-address changed to new ipv4 address in show system'):
            ext_api_output = OutputParsingTool.parse_json_str_to_dictionary(
                system.web_server_api.show()).get_returned_value()
            ValidationTool.verify_field_value_in_output(ext_api_output, SystemConsts.EXTERNAL_API_LISTEN,
                                                        {random_ipv6_address: {}}).\
                verify_result()

        with allure_step('Run unset system external api listening-address command and apply config'):
            system.web_server_api.unset(SystemConsts.EXTERNAL_API_LISTEN, apply=True, dut_engine=engines.dut).\
                verify_result()

        with allure_step('Verify external api port changed to default(any) in show system'):
            ext_api_output = OutputParsingTool.parse_json_str_to_dictionary(
                system.web_server_api.show()).get_returned_value()
            ValidationTool.verify_field_value_in_output(ext_api_output, SystemConsts.EXTERNAL_API_LISTEN,
                                                        {SystemConsts.EXTERNAL_API_LISTEN_DEFAULT: {}}).verify_result()

    finally:
        clear_system_web_server_api(system, engines)


@pytest.mark.webserverapi
@pytest.mark.system
@pytest.mark.simx
def test_show_system_web_server_api_conns_openapi(engines):
    TestToolkit.tested_api = ApiType.OPENAPI
    test_show_system_web_server_api_conns(engines)


@pytest.mark.webserverapi
@pytest.mark.system
@pytest.mark.simx
def test_show_system_web_server_api_listen_address_openapi(engines):
    TestToolkit.tested_api = ApiType.OPENAPI
    test_show_system_web_server_api_listen_address(engines)
