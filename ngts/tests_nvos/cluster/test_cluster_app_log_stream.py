import logging
import os
import random
import time

import pytest

from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.nmx.Cluster import Cluster
from ngts.tests_nvos.cluster.cluster_consts import ClusterConsts
from ngts.nvos_constants.constants_nvos import NvosConst, SystemConsts
from ngts.tests_nvos.cluster.cluster_tools import ClusterTools
from ngts.tests_nvos.constants import MINUTE
from ngts.tools.test_utils import allure_utils as allure
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.tests_nvos.helpers.redmine_helpers import is_bug_active
from ngts.nvos_constants.constants_nvos import ApiType

from retry import retry
import re

logger = logging.getLogger()


def nmxc_log_stream_remote_url(player, stream_protocol, url, port):
    """Build NMX-C log-stream remote-url per protocol.

    rsyslog: hostname:port
    splunk:  hostname:port?api_token=...
    elk:     user:pass@hostname:port
    """
    if stream_protocol == ClusterConsts.PROTOCOL_RSYSLOG:
        full_url = f'{url}:{port}'
        return full_url, full_url
    if stream_protocol == ClusterConsts.PROTOCOL_SPLUNK:
        token = player.password
        full_url = f'{url}:{port}?api_token={token}'
        return full_url, f'{url}:{port}?api_token=********'
    full_url = f'{player.username}:{player.password}@{url}:{port}'
    return full_url, f'{player.username}:********@{url}:{port}'


@pytest.mark.parametrize('stream_protocol', ClusterConsts.NMXC_LOG_STREAM_PROTOCOLS)
@pytest.mark.timeout(7 * MINUTE, func_only=True)
def test_nmxc_log_stream_set(engines, random_api, stream_protocol):
    player = engines.sonic_mgmt
    dut_ip = engines.dut.run_cmd("hostname  -I | awk '{print $1}'")
    ipv4 = player.ip
    ipv6 = player.run_cmd("hostname  -I | awk '{print $3}'")
    hostname = player.run_cmd("hostname")
    url = random.choice([hostname, ipv4, f'[{ipv6}]'])

    if is_bug_active(4918461):
        # IPv6 Bug is active, removing IPv6")
        url = random.choice([hostname, ipv4])

    check_hosts = engines.dut.run_cmd("cat /etc/hosts")
    if f"{ipv4} {hostname}" not in check_hosts:
        echo_cmd = f"echo '{ipv4} {hostname}' >> /etc/hosts"
        add_hostname_cmd = f'sudo sh -c "{echo_cmd}"'
        engines.dut.run_cmd(add_hostname_cmd)

    port = ClusterConsts.NMXC_LOG_STREAM_PORT[stream_protocol]
    full_url, full_url_show = nmxc_log_stream_remote_url(player, stream_protocol, url, port)
    stream = f'{stream_protocol} "{full_url}"'
    msg_uid = int(time.time())  # generates a unique ID using seconds elapsed since epoch
    log_msg = f"Unique Message ID {msg_uid}: Hello from NVOS"
    service = ClusterConsts.CONTROLLER_LOG_STREAM_SERVICE
    cluster_restored = False
    cluster_enabled = False
    try:
        with allure.step("Start Cluster"):
            cluster = Cluster()
            output = OutputParsingTool.parse_show_output_to_dict(cluster.show()).get_returned_value()
            if output[SystemConsts.STATE] == 'disabled':
                cluster.set(op_param_name="state", op_param_value='enabled', apply=True)
            ClusterTools.wait_for_apps_to_be_in_wanted_state(cluster, cluster_expected_state='enabled',
                                                             nmx_c_expected_state='up')
            cluster_enabled = True
            time.sleep(5)

        with allure.step("Set NMX-C log stream configuration"):
            retry_set_unset_cluster_apps_log_stream(cluster, stream)

        with allure.step("Validate NMX-Controller log stream configuration is {}".format(stream)):
            output = cluster.apps.app_name[ClusterConsts.NMX_CONTROLLER].logstream.show(exempted_err_msgs="Error")
            assert validate_nmxc_log_stream_config(output, protocol=stream_protocol, remote_url=full_url_show), \
                "NMX-C log stream config is not set to {}".format(stream)

        with allure.step("Validate logs are received at the remote"):
            validate_log_msg_on_remote(engines, player, log_msg, stream_protocol, dut_ip, service, url)

        with allure.step("Unset NMX-C log stream configuration"):
            retry_set_unset_cluster_apps_log_stream(cluster, stream, set_flag=False)

        with allure.step("Validate NMX-Controller log stream configuration is back to empty"):
            output = cluster.apps.app_name[ClusterConsts.NMX_CONTROLLER].logstream.show(exempted_err_msgs="Error")
            assert validate_nmxc_log_stream_config(output, empty=True), "NMX-C log stream config is not empty"
            cluster_restored = True

    finally:
        if not cluster_restored:
            with allure.step("Unset NMX-C log stream configuration"):
                retry_set_unset_cluster_apps_log_stream(cluster, stream, set_flag=False)

        if cluster_enabled:
            with allure.step("Stop Cluster"):
                logger.info("Set cluster state to disable")
                cluster.set(op_param_name="state", op_param_value='disabled', apply=True)
                ClusterTools.wait_for_apps_to_be_in_wanted_state(cluster, cluster_expected_state='disabled',
                                                                 nmx_c_expected_state='down')

        with allure.step("Remove temporary config file"):
            os.system(f"rm -rf {NvosConst.MARS_RESULTS_FOLDER}{ClusterConsts.CONTROLLER_LOG_STREAM_CONFIG_FILE}")


@pytest.mark.nmx
def test_nmxc_log_stream_show(random_api):
    cluster = Cluster()
    try:
        with allure.step("Validate initial NMX-Controller log stream configuration is empty"):
            output = cluster.apps.app_name[ClusterConsts.NMX_CONTROLLER].logstream.show(exempted_err_msgs="Error")
            assert validate_nmxc_log_stream_config(output, empty=True), f"NMX-C log stream config is not empty: {output}"

    finally:
        clean_up_cluster_nmxc_log_stream(cluster)


@pytest.mark.nmx
@pytest.mark.timeout(2 * MINUTE, func_only=True)
def test_nmxc_log_stream_set_unsupported_app(engines, random_api):
    player = engines.sonic_mgmt
    stream_protocol = ClusterConsts.PROTOCOL_RSYSLOG
    url = player.ip
    app_name = ClusterConsts.NMX_TELEMETRY
    expected_str = f"{app_name} not supported yet"
    helper_nmxc_log_stream_set_incorrect(player, stream_protocol, url, expected_str, app_name)


@pytest.mark.nmx
@pytest.mark.timeout(2 * MINUTE, func_only=True)
def test_nmxc_log_stream_set_incorrect_app(engines, random_api):
    player = engines.sonic_mgmt
    stream_protocol = ClusterConsts.PROTOCOL_RSYSLOG
    url = player.ip
    app_name = "dummy_app_name"
    expected_str = f"{app_name} not supported yet"
    helper_nmxc_log_stream_set_incorrect(player, stream_protocol, url, expected_str, app_name)


@pytest.mark.nmx
@pytest.mark.timeout(2 * MINUTE, func_only=True)
def test_nmxc_log_stream_set_incorrect_protocol(engines, random_api):
    player = engines.sonic_mgmt
    stream_protocol = "dummy_protocol"
    url = player.ip
    expected_str_openapi = f"'{stream_protocol}' is not one of {ClusterConsts.NMXC_LOG_STREAM_PROTOCOLS + [None]}"
    expected_str_nvue = "Invalid Command"
    expected_str = {ApiType.NVUE: expected_str_nvue, ApiType.OPENAPI: expected_str_openapi}
    helper_nmxc_log_stream_set_incorrect(player, stream_protocol, url, expected_str[random_api])


@pytest.mark.nmx
@pytest.mark.timeout(2 * MINUTE, func_only=True)
def test_nmxc_log_stream_set_incorrect_url(engines, random_api):
    player = engines.sonic_mgmt
    stream_protocol = ClusterConsts.PROTOCOL_RSYSLOG
    url = "dummy:123"
    expected_str = f"is not a 'log-remote-url'"
    helper_nmxc_log_stream_set_incorrect(player, stream_protocol, url, expected_str)


def helper_nmxc_log_stream_set_incorrect(player, stream_protocol, url, expected_str="",
                                         app_name=ClusterConsts.NMX_CONTROLLER):

    if stream_protocol in ClusterConsts.NMXC_LOG_STREAM_PORT.keys():
        port = ClusterConsts.NMXC_LOG_STREAM_PORT[stream_protocol]
    else:
        port = "0"
    full_url, _ = nmxc_log_stream_remote_url(player, stream_protocol, url, port)
    stream = f'{stream_protocol} {full_url}'
    try:
        with allure.step("Start Cluster"):
            cluster = Cluster()
            output = OutputParsingTool.parse_show_output_to_dict(cluster.show()).get_returned_value()
            if output[SystemConsts.STATE] == 'disabled':
                cluster.set(op_param_name="state", op_param_value='enabled', apply=True)
            ClusterTools.wait_for_apps_to_be_in_wanted_state(cluster, cluster_expected_state='enabled',
                                                             nmx_c_expected_state='up')

        with allure.step("Set NMX-C log stream configuration with incorrect argument"):
            retry_set_unset_cluster_apps_log_stream(cluster, stream, expected_str=expected_str, app_name=app_name)

        with allure.step("Validate NMX-Controller log stream configuration remains empty"):
            output = cluster.apps.app_name[ClusterConsts.NMX_CONTROLLER].logstream.show(exempted_err_msgs="Error")
            assert validate_nmxc_log_stream_config(output, empty=True), "NMX-C log stream config is not empty"

    finally:
        with allure.step("Unset NMX-C log stream configuration"):
            retry_set_unset_cluster_apps_log_stream(cluster, stream, set_flag=False)

        with allure.step("Stop Cluster"):
            cluster.set(op_param_name="state", op_param_value='disabled', apply=True)
            ClusterTools.wait_for_apps_to_be_in_wanted_state(cluster, cluster_expected_state='disabled',
                                                             nmx_c_expected_state='down')


def clean_up_cluster_nmxc_log_stream(cluster):
    output = cluster.apps.app_name[ClusterConsts.NMX_CONTROLLER].logstream.show(exempted_err_msgs="Error")
    if "The requested item does not exist." not in output:
        cluster.apps.app_name[ClusterConsts.NMX_CONTROLLER].logstream.action_restore_cluster_log_stream()


def validate_nmxc_log_stream_config(output, protocol="", remote_url="", empty=False):
    if "The requested item does not exist." in output:
        # NMX-C config keys itself are not available
        return empty

    output = OutputParsingTool.parse_json_str_to_dictionary(output).get_returned_value()

    if output["protocol"] != protocol:
        "Protocol is {} instead of {}".format(output["protocol"], protocol)
        return False
    if output["remote-url"] != remote_url:
        "Remote-url is {} instead of {}".format(output["remote-url"], remote_url)
        return False

    return True


def enable_rsyslog_server_on_remote(player, service):
    check_process_cmd = f'sudo service {service} status'
    start_process_cmd = f'sudo service {service} start'
    stop_process_cmd = f'sudo service {service} stop'
    output = player.run_cmd(check_process_cmd)
    if "unrecognized service" in output:
        check_process_cmd = f"pgrep -a {service}d"
        stop_process_cmd = f"pkill {service}d 2>/dev/null"
        start_process_cmd = f"{service}d"

    with allure.step("Verify and update protocol config file"):
        ret_value = verify_update_rsyslog_conf_file(player)
        if ret_value == "change":
            with allure.step("Restarting {} server on {}".format(service, player.ip)):
                player.run_cmd(stop_process_cmd)
                time.sleep(5)
                player.run_cmd(start_process_cmd)
                time.sleep(5)
        else:
            output = player.run_cmd(check_process_cmd)
            if f'{service}d' not in output:
                with allure.step("Starting {} server on {}".format(service, player.ip)):
                    player.run_cmd(start_process_cmd)
                    time.sleep(5)

    with allure.step("Checking log server is running on remote"):
        retry_check_log_server_on_remote(player, service, check_process_cmd)


def verify_update_rsyslog_conf_file(player):
    port = ClusterConsts.NMXC_LOG_STREAM_PORT[ClusterConsts.PROTOCOL_RSYSLOG]
    file_name = ClusterConsts.CONTROLLER_LOG_STREAM_CONFIG_FILE
    conf_file = f"{NvosConst.MARS_RESULTS_FOLDER}{file_name}"
    config_line_1 = 'module(load="imudp")\n'
    config_line_2 = f'input(type="imudp" port="{port}")\n'
    config_lines = [config_line_1, config_line_2]
    cmd_copy_from_remote = f"sshpass -p {player.password} scp -o StrictHostKeyChecking=no {player.username}@" \
        f"{player.ip}:/etc/{file_name} {NvosConst.MARS_RESULTS_FOLDER}"
    cmd_copy_to_remote = f"sshpass -p {player.password} scp -o StrictHostKeyChecking=no " \
        f"{NvosConst.MARS_RESULTS_FOLDER}{file_name} {player.username}@{player.ip}:/etc/{file_name}"

    # Extract protocol config file from server
    os.system(cmd_copy_from_remote)

    # Update the protocol config file
    with open(conf_file, 'r') as file:
        lines = file.readlines()

    if "#" + config_line_1 in lines and "#" + config_line_2 in lines:
        # commented config lines present
        logger.info("Uncomment commented config lines in protocol config file")
        with open(conf_file, 'w') as file:
            for line in lines:
                if line[1:] in config_lines:
                    file.write(line[1:])
                else:
                    file.write(line)

    elif config_line_1 in lines and config_line_2 in lines:
        # config lines already present
        logger.info("Protocol config already present")
        return "no change"

    else:
        # config lines not present
        logger.info("Adding config lines in protocol config file")
        with open(conf_file, 'a') as file:
            file.write("\n# provides UDP syslog reception\n")
            for config_line in config_lines:
                file.write(config_line)

    # Copy back the updated protocol config file and remove local copy
    player.run_cmd(cmd_copy_to_remote)
    os.system(f"rm -rf {NvosConst.MARS_RESULTS_FOLDER}{file_name}")
    return "change"


@retry(Exception, tries=24, delay=10)
def retry_check_log_server_on_remote(player, service, check_processes_cmd):
    output = player.run_cmd(check_processes_cmd)
    assert f'{service}d' in output, \
        "Not able to start log server on {}".format(player.ip)


def check_elk_server_on_remote(player):
    shared_apps = player.run_cmd('ls /usr/share')
    if "logstash" not in shared_apps:
        logger.info("Logstash not found on {}, Installing.".format(player.ip))
        player.run_cmd("sudo wget -qO - https://artifacts.elastic.co/GPG-KEY-elasticsearch | "
                       "sudo gpg --batch --y --dearmor -o /usr/share/keyrings/elastic-keyring.gpg")
        player.run_cmd("sudo apt-get install apt-transport-https")
        player.run_cmd('echo "deb [signed-by=/usr/share/keyrings/elastic-keyring.gpg] https://artifacts.elastic.co/'
                       'packages/8.x/apt stable main" | sudo tee -a /etc/apt/sources.list.d/elastic-8.x.list')
        player.run_cmd("sudo apt-get update && sudo apt-get install logstash")
        shared_apps = player.run_cmd('ls /usr/share')
        assert "logstash" in shared_apps, "Unable to install Logstash in {}".format(player.ip)

    logstash_cnf = 'input {\n    http {\n        port => 6001\n        codec => "json"\n        user     => "root"\n' \
                   '        password => "12345"\n    }\n}\noutput {\n    stdout {\n        codec => rubydebug\n    }\n}'

    player.run_cmd("echo '{}' > /tmp/logstash.conf".format(logstash_cnf))


def validate_log_msg_on_remote(engines, player, log_msg, stream_protocol, dut_ip, service, url):
    if "[" in url[0]:
        url = url[1:-1]  # url is ipv6, remove parenthesis

    if stream_protocol == ClusterConsts.PROTOCOL_RSYSLOG:
        port = ClusterConsts.NMXC_LOG_STREAM_PORT[ClusterConsts.PROTOCOL_RSYSLOG]
        with allure.step("Enable {} server on remote {}".format(stream_protocol, player.ip)):
            enable_rsyslog_server_on_remote(player, service)
        with allure.step("Stream logs to remote url for rsyslog protocol"):
            log_cmd = f'logger -n {url} -P {port} "{log_msg}"'
            engines.dut.run_cmd(log_cmd)
        with allure.step("Checking log message is present on remote"):
            retry_validate_log_msg_on_remote_rsyslog(player, log_msg)

    elif stream_protocol == ClusterConsts.PROTOCOL_ELK:
        cluster = Cluster()
        with allure.step("Check and install for Logstash binary on remote {}".format(player.ip)):
            check_elk_server_on_remote(player)
        with allure.step("Checking log message is present on remote"):
            # Set log level to info to generate frequent logs
            cluster.apps.app_name[ClusterConsts.NMX_CONTROLLER].loglevel.action_update_cluster_log_level(level="info")
            validate_log_msg_on_remote_elk(player, dut_ip)
            cluster.apps.app_name[ClusterConsts.NMX_CONTROLLER].loglevel.action_restore_cluster()

    elif stream_protocol == ClusterConsts.PROTOCOL_SPLUNK:
        retry_validate_log_msg_on_remote_splunk(player, log_msg, url)


@retry(Exception, tries=6, delay=10)
def retry_validate_log_msg_on_remote_rsyslog(player, log_msg):
    log_msg_read_cmd = f'cat /var/log/syslog | grep "{log_msg}"'
    output = player.run_cmd(log_msg_read_cmd)
    assert output, "Log msg {} not found on {}".format(log_msg, player.ip)


def validate_log_msg_on_remote_elk(player, dut_ip):
    logstash_cmd = "timeout 10s /usr/share/logstash/bin/logstash -f /tmp/logstash.conf > /tmp/logstash.log 2>&1"
    player.run_cmd(logstash_cmd)
    log_read_cmd = "cat /tmp/logstash.log"
    logs = player.run_cmd(log_read_cmd)
    match = None
    for dut_ip in [dut_ip]:
        # dut_url = dut_url.replace("::", ":")
        reg = r'host.*ip".*"' + re.escape(dut_ip) + '"'
        # reg = r'.*ip".*"' + re.escape(dut_url) + '"'
        match = re.search(reg, logs, re.DOTALL)
        if match is not None:
            break
    assert match is not None, "Logs from switch are not streamed to ELK server:{}".format(reg)


@retry(Exception, tries=6, delay=10)
def retry_validate_log_msg_on_remote_splunk(player, log_msg, ipv4=None):
    logger.info("Checking log message is present on remote")
    # TO DO: Add log validation for Splunk


@retry(AssertionError, tries=10, delay=5)
def retry_set_unset_cluster_apps_log_stream(cluster, stream, set_flag=True, should_succeed=True, expected_str="",
                                            app_name=ClusterConsts.NMX_CONTROLLER):
    if set_flag:
        cluster.apps.app_name[app_name].logstream.action_update_cluster_log_stream(
            stream=stream, expected_str=expected_str).verify_result(should_succeed=should_succeed)
    else:
        cluster.apps.app_name[app_name].logstream.action_restore_cluster_log_stream().\
            verify_result(should_succeed=should_succeed)
