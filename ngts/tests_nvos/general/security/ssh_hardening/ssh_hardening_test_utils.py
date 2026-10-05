import logging
import subprocess
import re

from ngts.tools.test_utils import allure_utils as allure
from devts.infra.tools.connection_tools.proxy_ssh_engine import ProxySshEngine
from ngts.tests_nvos.general.security.ssh_hardening.constants import SshHardeningConsts


def get_ssh_verbose_output(server_engine: ProxySshEngine, timeout: int = SshHardeningConsts.TIMEOUT,
                           ssh_options='') -> str:
    """
    @summary: Run SSH with verbose flag and return its output
    @param server_engine: engine of the ssh server
    @param timeout: timeout to give the ssh command (to generate output)
    @param ssh_options: other options to add to the ssh command (optional)
    @return: Value of the verbose ssh command
    """
    with allure.step('Run ssh -vvvv to get info'):
        cmd = f'timeout {timeout} ssh -vvvv{" " + ssh_options if ssh_options else ""} ' \
            f'{server_engine.username}@{server_engine.ip}'
        try:
            logging.info(f"Run: '{cmd}'")
            res = subprocess.run(
                cmd.split(' '),
                # shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                # check=True
            )
            std_output = str(res.stdout)
            err_output = str(res.stderr)
        except subprocess.CalledProcessError as e:
            logging.info(f'Error: {e}')
            raise e
    logging.debug(f'Errors: {err_output}')
    logging.debug(f'Output: {std_output}')
    output = err_output + '\n' + std_output
    return output


def get_ssh_server_proposal(server_engine: ProxySshEngine) -> str:
    """
    @summary: Get SSH server proposal from handshake phase
    @param server_engine: engine of the ssh server
    @return: Servers proposal from the handshake phase (string)
    """
    output = get_ssh_verbose_output(server_engine)

    with allure.step('Get server proposal lines'):
        pattern = r"peer server KEXINIT proposal(.*?)reserved"
        matches = re.findall(pattern, output, re.DOTALL)
        logging.debug(f'Matches:\n{matches}')
        server_proposal = matches[0]
        logging.debug(f'Server proposal:\n{server_proposal}')

    return server_proposal


def get_value_from_ssh_output(get_ssh_output_function, server_engine: ProxySshEngine, substr_to_find: str) -> str:
    """
    @summary: Get value of a specified property the SSH command output
    @param get_ssh_output_function: function from which to get ssh server info
    @param server_engine: engine of the ssh server
    @param substr_to_find: substring that the target value should follow in the output
    @return: the target value from the ssh output (str)
    """
    ssh_output = get_ssh_output_function(server_engine)

    with allure.step('Extract relevant line'):
        lines = ssh_output.splitlines()
        relevant_line = next(line for line in lines if substr_to_find in line)
        logging.info(f'Relevant line: {relevant_line}')

    with allure.step('Extract value from the relevant line'):
        pattern_to_find = fr"{substr_to_find} ([^\s\n]+)"
        matches = re.findall(pattern_to_find, relevant_line)
        assert matches, f'Pattern "{pattern_to_find}" not found in "{relevant_line}"'
        logging.info(f'Matches: {matches}')
        return matches[0]


def get_ssh_server_protocol(server_engine: ProxySshEngine):
    """
    @summary: Get the SSH protocol version of the given SSH server
    @param server_engine: engine of the ssh server
    @return: protocol version (str)
    """
    server_protocol_value = get_value_from_ssh_output(
        get_ssh_output_function=get_ssh_verbose_output,
        server_engine=server_engine,
        substr_to_find='Remote protocol version'
    )
    server_protocol_value = server_protocol_value.replace(',', '')
    logging.info(f'Server protocol: {server_protocol_value}')
    return server_protocol_value


def get_ssh_server_compression_state(server_engine: ProxySshEngine):
    """
    @summary: Get the SSH compression state of the given SSH server
    @param server_engine: engine of the ssh server
    @return: compression state (str)
    """
    server_compression_state = get_value_from_ssh_output(
        get_ssh_output_function=get_ssh_server_proposal,
        server_engine=server_engine,
        substr_to_find='compression ctos:'
    )
    logging.info(f'Server compression state: {server_compression_state}')
    return server_compression_state


def get_ssh_server_ciphers(server_engine: ProxySshEngine):
    """
    @summary: Get the SSH ciphers of the given SSH server
    @param server_engine: engine of the ssh server
    @return: server ciphers (list of str)
    """
    server_ciphers = get_value_from_ssh_output(
        get_ssh_output_function=get_ssh_server_proposal,
        server_engine=server_engine,
        substr_to_find='ciphers ctos:'
    )
    logging.info(f'Server ciphers: {server_ciphers}')
    return server_ciphers.split(',')


def get_device_ciphers_list(devices):
    """
    @summary: Get device ciphers list
    @param devices: devices
    @return: ciphers list
    """
    return SshHardeningConsts.VALUES[SshHardeningConsts.CIPHERS]


def get_ssh_server_macs(server_engine: ProxySshEngine):
    """
    @summary: Get the SSH MACs of the given SSH server
    @param server_engine: engine of the ssh server
    @return: server MACs (list of str)
    """
    server_ciphers = get_value_from_ssh_output(
        get_ssh_output_function=get_ssh_server_proposal,
        server_engine=server_engine,
        substr_to_find='MACs ctos:'
    )
    logging.info(f'Server MACs: {server_ciphers}')
    return server_ciphers.split(',')


def get_device_macs_list(devices):
    """
    @summary: Get device macs list
    @param devices: devices
    @return: macs list
    """
    return SshHardeningConsts.VALUES[SshHardeningConsts.MACS]


def drop_kex_signaling_markers(kex_algorithms: list) -> list:
    """
    @summary: Drop the OpenSSH signaling markers from a list of KEX-algorithms
    @param kex_algorithms: list of KEX-algorithms
    @return: the given KEX-algorithms, without the signaling markers (list of str)
    """
    return [algo for algo in kex_algorithms if algo not in SshHardeningConsts.KEX_SIGNALING_MARKERS]


def get_ssh_server_kex_algorithms(server_engine: ProxySshEngine):
    """
    @summary: Get the SSH KEX-algorithms of the given SSH server
    @param server_engine: engine of the ssh server
    @return: server KEX-algorithms (list of str)
    """
    server_ciphers = get_value_from_ssh_output(
        get_ssh_output_function=get_ssh_server_proposal,
        server_engine=server_engine,
        substr_to_find='KEX algorithms:'
    )
    logging.info(f'Server KEX-algorithms: {server_ciphers}')
    kex_algorithms = drop_kex_signaling_markers(server_ciphers.split(','))
    logging.info(f'Server KEX-algorithms without signaling markers: {kex_algorithms}')
    return kex_algorithms


def get_device_kex_algotithms_list(devices):
    """
    @summary: Get device SSH KEX-algorithms list
    @param devices: devices
    @return: SSH KEX-algorithms list
    """
    return drop_kex_signaling_markers(devices.dut.kex_algorithms)


def verify_switch_ssh_property(engines, property_name, expected_value, value_extraction_function):
    """
    @summary: Generic test helper function to verify ssh configuration in the switch
    """
    with allure.step(f'Verify new {property_name}'):
        with allure.step(f'Get switch ssh {property_name}'):
            value = value_extraction_function(engines.dut)
            logging.info(f'{property_name}: {value}')

        with allure.step(f'Verify that {property_name} is set to correctly'):
            if isinstance(value, list):
                assert isinstance(expected_value, list)
                value = set(value)
                expected_value = set(expected_value)
            assert value == expected_value, f'{property_name} not as expected\n' \
                f'Expected: {expected_value}\n' \
                f'Actual: {value}'


def verify_ssh_with_option(engines, devices, good_flow: bool, option_to_check: str, get_option_list_function):
    assert option_to_check in SshHardeningConsts.OPTIONS_FOR_FUNCTIONAL_TEST, \
        f'Received option to check: {option_to_check}\nExpected: {SshHardeningConsts.OPTIONS_FOR_FUNCTIONAL_TEST}'
    with allure.step(f'{"Good" if good_flow else "Bad"} flow: ssh with {"" if good_flow else "in"}valid '
                     f'{option_to_check}'):
        optional_values = get_option_list_function(devices) if good_flow else \
            list(
                set(SshHardeningConsts.DEFAULTS[option_to_check]) - set(get_option_list_function(devices)))
        logging.debug(f'Optional values: {optional_values}')
        for value in optional_values:
            logging.debug(f'Chosen value: {value}')
            ssh_output = get_ssh_verbose_output(
                server_engine=engines.dut,
                ssh_options=f'{SshHardeningConsts.SSH_CMD_FLAGS[option_to_check]}{value}'
            )

            logging.debug(f'Verify ssh result. Expect {option_to_check} error: {not good_flow}')
            err_pattern = SshHardeningConsts.ERROR_PATTERNS[option_to_check]
            got_error = True if re.search(err_pattern, ssh_output) else False
            assert got_error == (not good_flow), \
                f'Could not find expected error pattern "{err_pattern}" in "{ssh_output}"'
