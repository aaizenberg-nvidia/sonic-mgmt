import logging
import os
import re
import time
import retry
from datetime import datetime, timezone
from dateutil import parser

from ngts.constants.constants import LinuxConsts
from ngts.nvos_constants.constants_nvos import HealthConsts, NvosConst, ApiType, SystemConsts, RemarkableLogsConsts
from ngts.nvos_tools.ib.opensm.OpenSmTool import OpenSmTool
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.Tools import Tools
from ngts.nvos_tools.infra.IbRouterTool import IbRouterTool
from ngts.nvos_tools.platform.Platform import Platform
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.system.clock.ClockTools import ClockTools
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger()

running_dockers = {}
KEEP_ALL_CONFIG = "keep all-config"
KEEP_ONLY_FILES = "keep only-files"
KEEP_BASIC = "keep basic"
DO_NOT_CHECK_DOCKERS = ['snmp']


def update_timezone(system):
    with allure.step('Configure timezone'):
        logger.info(
            "Configuring same time zone for dut and local engine to {}".format(LinuxConsts.JERUSALEM_TIMEZONE))
        ClockTools.set_timezone(LinuxConsts.JERUSALEM_TIMEZONE, system, apply=True).verify_result()
        with allure.step('Set timezone using timedatectl command'):
            os.popen('sudo timedatectl set-timezone {}'.format(LinuxConsts.JERUSALEM_TIMEZONE))


def validate_health_status_report(system, pre_health_status):
    if pre_health_status == HealthConsts.OK:
        start_time = time.time()
        system.health.wait_until_health_status_change_after_reboot(HealthConsts.OK)
        end_time = time.time()
        duration = end_time - start_time

        logger.info("Took {} seconds until health status changed to OK after reset factory".format(duration))


def validate_port_description(engine, port, expected_description):
    output_dictionary = Tools.OutputParsingTool.parse_show_interface_output_to_dictionary(
        port.show_interface(engine, port.name)).get_returned_value()
    if expected_description:
        Tools.ValidationTool.verify_field_value_in_output(output_dictionary=output_dictionary,
                                                          field_name=NvosConst.DESCRIPTION,
                                                          expected_value=expected_description).verify_result()


def split_port(engines):
    with allure.step("Start openSM"):
        OpenSmTool.start_open_sm(engines).verify_result()
        time.sleep(7)

    with allure.step("Select an active random port"):
        logging.info("Select an active random port")
        random_port = Tools.RandomizationTool.get_random_active_port().get_returned_value()[0]

        with allure.step("Split selected port ({})".format(random_port.name)):
            logging.info("Split selected port ({})".format(random_port.name))
            logging.warning("Currently not supported")

    return random_port


def create_date_time_obj(str_info):
    tmp_str = str_info.split()
    str_date_time = "{date} {time}".format(date=tmp_str[1], time=tmp_str[2].split('.')[0])
    datetime_object = datetime.strptime(str_date_time, '%Y-%m-%d %H:%M:%S')
    return datetime_object


def add_verification_data(engine, system):
    if TestToolkit.devices.dut.is_ib():
        return add_verification_data_for_ib(engine, system)
    if TestToolkit.devices.dut.is_eth():
        return add_verification_data_for_cumulus(engine, TestToolkit.devices.dut)


def add_verification_data_for_ib(engine, system):
    with allure.step("Clear running dockers dict"):
        running_dockers.clear()
    with allure.step(f"Add file to {NvosConst.PATH_TO_IMAGES}"):
        output = engine.run_cmd("ls /host/mlnx")
        if "No such file or directory" in output:
            output = engine.run_cmd("sudo mkdir /host/mlnx")
        output = engine.run_cmd(f"ls {NvosConst.PATH_TO_IMAGES}")
        if "No such file or directory" in output:
            output = engine.run_cmd(f"sudo mkdir {NvosConst.PATH_TO_IMAGES}")
        output = engine.run_cmd(f"sudo touch {NvosConst.PATH_TO_IMAGES}/verification_test")

    with allure.step("Add file to /var/dump/"):
        output = engine.run_cmd("ls /var/dump/")
        if "No such file or directory" in output:
            output = engine.run_cmd("sudo mkdir /var/dump/")
        output = engine.run_cmd("sudo touch /var/dump/verification_test")

    with allure.step("Add file to /var/stats/"):
        output = engine.run_cmd("ls /var/stats/")
        if "No such file or directory" in output:
            output = engine.run_cmd("sudo mkdir /var/stats/")
        output = engine.run_cmd("sudo touch /var/stats/verification_test")

    with allure.step("Add file to /host/stats/"):
        output = engine.run_cmd("ls /host/stats/")
        if "No such file or directory" in output:
            output = engine.run_cmd("sudo mkdir /host/stats/")
        output = engine.run_cmd("sudo touch /host/stats/verification_test")

    with allure.step("Add history files to /home"):
        output = engine.run_cmd("ls /home")
        if ".bash_history" not in output:
            output = engine.run_cmd("sudo touch /home/.bash_history")
        if ".python_history" not in output:
            output = engine.run_cmd("sudo touch /home/.python_history")
        if ".viminfo" not in output:
            output = engine.run_cmd("sudo touch /home/.viminfo")
        output = engine.run_cmd("sudo touch /home/verification_test")

    with allure.step("Add file to /etc/sonic"):
        output = engine.run_cmd("ls /etc/sonic")
        if "No such file or directory" in output:
            output = engine.run_cmd("sudo mkdir /etc/sonic")
        output = engine.run_cmd("sudo touch /etc/sonic/verification_test")

    with allure.step("Add file to /host/warmboot"):
        output = engine.run_cmd("ls /host/warmboot")
        if "No such file or directory" in output:
            output = engine.run_cmd("sudo mkdir /host/warmboot")
        output = engine.run_cmd("sudo touch /host/warmboot/verification_test")

    with allure.step("Check running dockers"):
        check_running_dockers(engine)

    with allure.step("Create new user"):
        username, password = System(force_api=ApiType.NVUE).aaa.user.set_new_user(apply=True)
        return username


def verify_cleanup_done(engine, time_before_rf, system, username, param='', ib_router=False, dns_server_id=None):
    logging.info("Verify cleanup done as expected")
    errors = ""
    device = TestToolkit.devices.dut
    if dns_server_id and param == KEEP_BASIC:
        with allure.step('Validate saved DNS config is retained after factory reset'):
            dns_output = Tools.OutputParsingTool.parse_json_str_to_dictionary(
                system.dns.show(SystemConsts.DNS_SERVER)).get_returned_value()
            if dns_server_id not in dns_output:
                errors += "\nThe configured DNS server {} is not retained after factory reset keep basic".format(
                    dns_server_id)
    assert not errors, "DNS verification failed after factory reset:{}".format(errors)
    if TestToolkit.devices.dut.is_ib():
        verify_cleanup_done_for_ib(engine, time_before_rf, device, username, param, ib_router=ib_router)
    if TestToolkit.devices.dut.is_eth():
        verify_cleanup_done_for_cumulus(engine, time_before_rf, param=param, username=username)


def verify_services_status(engines, services):
    """Verify each service is active (systemctl is-active)."""
    for svc in services:
        out = engines.dut.run_cmd(f"systemctl is-active {svc}", validate=False)
        raw = (out or "").strip()
        state = raw.splitlines()[0].strip() if raw else ""
        assert state == "active", f"Service {svc} not active (state={state!r}): {out}"


def verify_cleanup_done_for_ib(engine, time_before_rf, system, username, param='', ib_router=False):
    logging.info("Verify cleanup done as expected")
    errors = ""
    device = TestToolkit.devices.dut
    time_before_rf = time_before_rf.astimezone(timezone.utc).replace(tzinfo=None)
    with allure.step("Verify NVUE reset done"):
        if param != KEEP_ONLY_FILES:
            output = engine.run_cmd("stat /etc/sonic/nvue.d/platform/immutables.yaml | grep Birth")
            if output and "No such file or directory" not in output:
                file_date_time = create_date_time_obj(output)
                if time_before_rf >= file_date_time:
                    errors += "\n/etc/sonic/nvue.d/platform/immutables.yaml was not deleted"

            if param != KEEP_ALL_CONFIG:
                output = engine.run_cmd("stat /etc/sonic/nvue.d/startup.yaml | grep Birth")
                if output and "No such file or directory" not in output:
                    file_date_time = create_date_time_obj(output)
                    if time_before_rf >= file_date_time:
                        errors += "\n/etc/sonic/nvue.d/startup.yaml was not deleted"

    with allure.step("Verify sonic.target stopped"):
        if param != KEEP_ONLY_FILES:
            output = engine.run_cmd("systemctl show sonic.target | grep StateChangeTimestamp=")
            if output:
                output = output.split('=')[1].split()
                file_date_time = create_date_time_obj("date {} {}".format(output[1], output[2]))
                if time_before_rf >= file_date_time:
                    errors += "\nsonic.target probably was not stopped"

    with allure.step("Verify new DB was created"):
        if param not in [KEEP_ALL_CONFIG, KEEP_ONLY_FILES]:
            output = engine.run_cmd("stat /etc/sonic/config_db.json | grep Birth")
            if output and "No such file or directory" not in output:
                file_date_time = create_date_time_obj(output)
                if time_before_rf >= file_date_time:
                    errors += "\nnew /etc/sonic/config_db.json was not created"

    with allure.step("Verify NVOS HOOKs were deleted"):
        if param != KEEP_ONLY_FILES:
            output = engine.run_cmd(f"ls {NvosConst.PATH_TO_IMAGES}")
            if output:
                errors += f"\n{NvosConst.PATH_TO_IMAGES} is not empty as expected"

    with allure.step("Verify tech-support files were deleted"):
        if param != KEEP_ONLY_FILES:
            output = engine.run_cmd(f"sudo ls {SystemConsts.TECHSUPPORT_FILES_PATH} 2>/dev/null || true")
            if output and "No such file or directory" not in output:
                errors += f"\ntech-support files were not deleted from {SystemConsts.TECHSUPPORT_FILES_PATH}: {output}"

    with allure.step("Verify /var/dump was cleaned up"):
        if param != KEEP_ONLY_FILES:
            output = engine.run_cmd("ls /var/dump/ 2>/dev/null || true")
            if output and "No such file or directory" not in output:
                errors += f"\n/var/dump was not cleaned up after factory reset: {output}"

    with allure.step("Verify old stats internal files were deleted"):
        if param != KEEP_ONLY_FILES:
            output = engine.run_cmd("ls /var/stats")
            if output and "No such file or directory" not in output:
                stats_files = list(output.split())
                for stat_file in stats_files:
                    output = engine.run_cmd(f"stat /var/stats/{stat_file} | grep Birth")
                    file_date_time = create_date_time_obj(output)
                    if time_before_rf >= file_date_time:
                        errors += "\nold stats internal file {} was not deleted".format(stat_file)

    with allure.step("Verify DHCP files were deleted, bug #4218163"):
        if param != KEEP_ONLY_FILES:
            for port_number in range(len(device.mgmt_ports)):
                output = engine.run_cmd(f"stat /var/lib/dhcp/dhclient.eth{port_number}.leases | grep Birth")
                if output and "No such file or directory" not in output:
                    file_date_time = create_date_time_obj(output)
                    if time_before_rf >= file_date_time:
                        errors += f"\n/var/lib/dhcp/dhclient.eth{port_number}.leases was not deleted"

    with allure.step("Verify stats external files were deleted"):
        if param != KEEP_ONLY_FILES:
            output = engine.run_cmd("ls /host/stats")
            if output and "No such file or directory" not in output:
                errors += "\nstats external files were not deleted: {}".format(output)

    with allure.step("Verify /etc/sonic content was cleared"):
        if param != KEEP_ONLY_FILES:
            output = engine.run_cmd("ls /etc/sonic/verification_test")
            if output and "No such file or directory" not in output:
                errors += "\n/etc/sonic was not cleared: {}".format(output)

    with allure.step("Verify /etc/sonic content was cleared"):
        if param != KEEP_ONLY_FILES:
            output = engine.run_cmd("ls /host/tpm/oIAK.cert")
            if output and "No such file or directory" not in output:
                errors += "\n/host/tpm/oIAK.cert was not cleared"

    with allure.step("Verify /host/warmboot content was deleted"):
        if param != KEEP_ONLY_FILES:
            output = engine.run_cmd("ls /host/warmboot")
            if output and "No such file or directory" not in output:
                output = engine.run_cmd("find /host/warmboot")
                if not all(re.fullmatch(r"/host/warmboot(/namespace\d)?", item) for item in output.splitlines()):
                    errors += "\n/host/warmboot was not cleared: {}".format(output)

    with allure.step("Verify history was deleted"):
        if param not in [KEEP_BASIC, KEEP_ONLY_FILES]:
            output = engine.run_cmd("ls /home/.bash_history")
            if "No such file or directory" not in output:
                errors += "\n*.bash_history files were not deleted"
            output = engine.run_cmd("ls /home/.python_history")
            if "No such file or directory" not in output:
                errors += "\n*.python_history files were not deleted"
            output = engine.run_cmd("ls /home/.viminfo")
            if "No such file or directory" not in output:
                errors += "\n*.viminfo files were not deleted"
            output = engine.run_cmd("find /home/ -maxdepth 1 -type f ")
            if output:
                errors += "\nHome files were not deleted: {}".format(output)

    with allure.step("Verify btmp files were cleared"):
        if param != KEEP_ONLY_FILES:
            output = engine.run_cmd("stat /var/log/btmp | grep Modify")
            if output and "No such file or directory" not in output:
                file_date_time = create_date_time_obj(output)
                if time_before_rf >= file_date_time:
                    errors += "\n/var/log/btmp was not cleared: {}".format(output)

            output = engine.run_cmd("stat /var/log/lastlog | grep Modify")
            if output and "No such file or directory" not in output:
                file_date_time = create_date_time_obj(output)
                if time_before_rf >= file_date_time:
                    errors += "\n/var/log/lastlog was not created: {}".format(output)

            output = engine.run_cmd("stat /var/log/wtmp | grep Modify")
            if output and "No such file or directory" not in output:
                file_date_time = create_date_time_obj(output)
                if time_before_rf >= file_date_time:
                    errors += "\n/var/log/wtmp was not created: {}".format(output)

    with allure.step("Create new user"):
        if param != KEEP_BASIC:
            output = Tools.OutputParsingTool.parse_json_str_to_dictionary(
                engine.run_cmd("nv show system aaa user -o json")).get_returned_value()
            if username and username in output.keys():
                errors += "\nCreated user was not deleted"

    with allure.step("Check running dockers"):
        if param != KEEP_ONLY_FILES:
            logging.info("Check running dockers")
            for docker_name, orig_create_time in running_dockers.items():
                if docker_name.startswith('nmx'):
                    continue
                output = engine.run_cmd(r"docker inspect -f \{\{'.Created'\}\} " + docker_name)
                if "Error" in output:
                    create_time = ""
                else:
                    create_time = datetime.strptime(output.split(".")[0], f'%Y-%m-%dT%H:%M:%S')

                if "database" in docker_name:
                    if create_time != orig_create_time:
                        errors += "reset factory should not restart database docker"
                else:
                    if not create_time:
                        errors += "\n'{}' is not running after reset factory".format(docker_name)
                    elif orig_create_time == create_time:
                        errors += "\n'{}' was not stopped during reset factory".format(docker_name)

    if ib_router:
        with allure.step("Check ib router profile status"):
            expect_disabled = param != KEEP_ALL_CONFIG
            expected_swid_number = 4 if not expect_disabled else 1
            expected_profile = SystemConsts.PROFILE_STATE_DISABLED if expect_disabled else SystemConsts.PROFILE_STATE_ENABLED
            try:
                IbRouterTool.verify_profile_status(expected_profile_status=expected_profile, expected_swid_number=expected_swid_number)
                IbRouterTool.verify_leaf_port_mapping(expect_disabled=expect_disabled)
            except AssertionError as err:
                errors += f"ib router profile/config check failed with error: {str(err)}"

    assert not errors, errors


def verify_profile_and_split(selected_port):
    logging.info("Verify the breakup mode is disabled and selected port is not split any more")
    with allure.step("Check profile"):
        logging.info("Check current profile")
        logging.warning("Currently not supported")

    with allure.step("Verify port {} in not in split mode".format(selected_port.name)):
        logging.info("Verify port {} in not in split mode".format(selected_port.name))
        logging.warning("Currently not supported")


def verify_the_setup_is_functional(system, engines, had_sm_before_test=True, dut=None):
    logging.info("Verify the setup is functional")

    """if had_sm_before_test:
        with allure.step("Start OpenSM"):
            with allure.step('Check is Juliet Device'):
                if not isinstance(dut, JulietSwitch):
                    OpenSmTool.start_open_sm(engines).verify_result()"""

    with allure.step("Run show commands"):
        system.message.show()
        system.version.show()
        platform = Platform()
        platform.show()

    with allure.step("Run set and apply"):
        system.message.set(op_param_name=SystemConsts.POST_LOGIN_MESSAGE, op_param_value='"new_post_login_msg"',
                           apply=True, dut_engine=engines.dut).verify_result()

    with allure.step("Run unset and apply"):
        system.message.unset(op_param=SystemConsts.POST_LOGIN_MESSAGE,
                             apply=True, dut_engine=engines.dut).verify_result()


def get_current_time(engines):
    date_time_str = engines.dut.run_cmd("date").split(" ", 1)[1]
    current_time = parser.parse(date_time_str)
    return current_time


def verify_cleanup_done_for_cumulus(engine, time_before_rf, param='', username=None):
    # Verify if files are deleted or kept, depending on the option of factory reset
    errors = ""
    if param == KEEP_BASIC:
        output = engine.run_cmd("ls /etc/cumulus/")
        if "verification_test.txt" in output:
            errors += "\n/etc/cumulus/verification_test.txt file was not deleted "
        output = engine.run_cmd("ls /home/cumulus/")
        if "verification_test.txt" not in output:
            errors += "\n/home/cumulus/verification_test.txt file was deleted "
        output = engine.run_cmd("ls /var/log/")
        if "verification_test.txt" in output:
            errors += "\n/var/log/verification_test.txt file was not deleted "
        if errors:
            logger.info(errors)
        verify_factory_reset_log(engine)
    elif param == KEEP_ALL_CONFIG:
        output = engine.run_cmd("ls /etc/cumulus/")
        if "verification_test.txt" not in output:
            errors += "\n/etc/cumulus/verification_test.txt file was deleted "
        output = engine.run_cmd("ls /home/cumulus/")
        if "verification_test.txt" not in output:
            errors += "\n/home/cumulus/verification_test.txt file was deleted "
        output = engine.run_cmd("ls /var/log/")
        if "verification_test.txt" in output:
            errors += "\n/var/log/verification_test.txt file was not deleted "
        if errors:
            logger.info(errors)
        verify_factory_reset_log(engine)
    elif param == KEEP_ONLY_FILES:
        output = engine.run_cmd("ls /etc/cumulus/")
        if "verification_test.txt" in output:
            errors += "\n/etc/cumulus/verification_test.txt file was not deleted "
        output = engine.run_cmd("ls /home/cumulus/")
        if "verification_test.txt" in output:
            errors += "\n/home/cumulus/verification_test.txt file was not deleted "
        output = engine.run_cmd("ls /var/log/")
        if "verification_test.txt" not in output:
            errors += "\n/var/log/verification_test.txt file was deleted "
        if errors:
            logger.info(errors)
    else:
        output = engine.run_cmd("ls /etc/cumulus/")
        if "verification_test.txt" in output:
            errors += "\n/etc/cumulus/verification_test.txt file was not deleted "
        output = engine.run_cmd("ls /home/cumulus/")
        if "verification_test.txt" in output:
            errors += "\n/home/cumulus/verification_test.txt file was not deleted "
        output = engine.run_cmd("ls /var/log/")
        if "verification_test.txt" in output:
            errors += "\n/var/log/verification_test.txt file was not deleted "
        if errors:
            logger.info(errors)
        verify_factory_reset_log(engine)

    with allure.step("Verify created user was deleted"):
        if param not in [KEEP_ALL_CONFIG, KEEP_BASIC]:
            output = engine.run_cmd("nv show system aaa user -o json")
            if username and username in output:
                errors += "\nCreated user was not deleted"

    assert not errors, errors


def verify_factory_reset_log(engine):
    """
    This function checks /var/log/factory-reset.log for 'Factory reset complete'

    """
    logger.info("This function checks /var/log/factory-reset.log for Factory reset complete")
    output = engine.run_cmd("sudo cat /var/log/factory-reset.log | grep -i 'Factory reset complete'")
    logger.info(output)
    output = engine.run_cmd("sudo cat /var/log/factory-reset.log")
    logger.info(output)
    if 'Factory reset complete' not in output:
        logger.error("Failed: 'Factory reset complete. Rebooting now !' log not found in /var/log/factory-reset.log")
        assert False, "Failed: 'Factory reset complete. Rebooting now !' log not found in /var/log/factory-reset.log"
    else:
        logger.info("'Factory reset complete. Rebooting now !' log found in /var/log/factory-reset.log")


def add_verification_data_for_cumulus(engine, device):
    with allure.step("Create test files for Cumulus system"):
        with allure.step("Add file to /etc/cumulus/"):
            output = engine.run_cmd("ls /etc/cumulus/")
            if "verification_test.txt" not in output:
                output = engine.run_cmd("sudo touch /etc/cumulus/verification_test.txt")
        with allure.step("Add file to /home/cumulus/"):
            output = engine.run_cmd("ls /home/cumulus/")
            if "verification_test.txt" not in output:
                output = engine.run_cmd("sudo touch /home/cumulus/verification_test.txt")
        with allure.step("Add file to /var/log/"):
            output = engine.run_cmd("ls /var/log/")
            if "verification_test.txt" not in output:
                output = engine.run_cmd("sudo touch /var/log/verification_test.txt")
    with allure.step("Create new user for Cumulus system"):
        username, password = System(force_api=ApiType.NVUE).aaa.user.set_new_user(apply=True)
        return username


@retry.retry(Exception, delay=5, tries=5)
def check_running_dockers(engine):
    output = engine.run_cmd("docker container list").split('\n')[1:]
    for line in output:
        line = line.split()
        docker_name = line[len(line) - 1]
        if docker_name not in DO_NOT_CHECK_DOCKERS:
            start_time = get_docker_start_time(engine, docker_name)
            running_dockers[docker_name] = start_time


def get_docker_start_time(engine, docker_name):
    result = engine.run_cmd(r"docker inspect -f {{'.Created'}} " + docker_name)
    if "Error" in result:
        raise Exception(f"Error: No such object: {docker_name}")
    return datetime.strptime(result.split(".")[0], '%Y-%m-%dT%H:%M:%S')
