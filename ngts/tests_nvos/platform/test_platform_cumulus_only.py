import logging
import pytest
import time
import re
import json

from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.nvos_tools.platform.Platform import Platform
from ngts.tools.test_utils import allure_utils as allure
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_constants.constants_nvos import ApiType, NvosConst
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port
from ngts.nvos_tools.system.System import System
from ngts.nvos_tools.infra.HostMethods import HostMethods
from ngts.nvos_tools.infra.DutUtilsTool import DutUtilsTool


cumulus_owner = "hiept"


log = logging.getLogger()


@pytest.mark.platform
@pytest.mark.cumulus
@pytest.mark.cumulus_only
def test_watchdog_good_kill(engines, random_api):
    """
    Name: Watchdog Good Kill
    ===============================================

    Description:
    ===============================================
    This test ensures that when the watchdog is killed correctly the system
    does not restart due to a watchdog timeout

    Steps:
    ===============================================
    1. Enable the watchdog if it is not enabled
    2. Kill the watchdog
    3. Ensure the system does not restart
    """

    with allure.step("If watchdog is not running, Skip the test"):
        pid_wd = engines.dut.run_cmd("pidof wd_keepalive")
        if not pid_wd:
            pytest.skip("No pid for wd_keep_alive skipping test.")

    with allure.step("Getting the current timeout of the watchdog."):
        watchdog_timeout = int(get_current_watchdog_time(engines))
        log.info(f"Current watchdog timeout - {watchdog_timeout}")

    # Need to get the boot id to see if it actually shuts down
    with allure.step("Get boot id before shutting down watchdog."):
        cmd = "cat /proc/sys/kernel/random/boot_id"
        start_boot_id = engines.dut.run_cmd(cmd)

    # Kill watchdog
    with allure.step("Killing the watchdog process correctly."):
        engines.dut.run_cmd("sudo kill -TERM %s" % pid_wd)

    # Give enough time for the reset to occur
    with allure.step("Wait to ensure DUT doesn't reboot."):
        log.info(
            "Waiting %d seconds to ensure DUT stays up." %
            (
                (watchdog_timeout * 4) + 5
            )
        )
        time.sleep((watchdog_timeout * 4) + 5)

        try:
            code = check_shutdown_state(engines, start_boot_id)
        except Exception:
            assert False, "DUT never came back after the watchdog fired"

        # Code of 1 means the DUT did not shutdown, good
        if code != 1:
            assert (False), "DUT either shutdown or rebooted when the correct \
shutdown for the watchdog was used."
        else:
            log.info("DUT correctly stayed up with a 'good' kill of watchdog")

    with allure.step("Restart watchdog"):
        engines.dut.run_cmd("sudo systemctl start wd_keepalive")
        new_pid_wd = engines.dut.run_cmd("pidof wd_keepalive")
        if not new_pid_wd or (new_pid_wd == pid_wd):
            assert False, "Watchdog restart failed"

    log.info("Pass")


@pytest.mark.platform
@pytest.mark.cumulus
@pytest.mark.cumulus_only
def test_watchdog_bad_kill(engines, random_api):
    """
    Name: Watchdog Bad Kill
    ===============================================

    Description:
    ===============================================
    This test ensures that when the watchdog is killed incorrectly (kill -9)
    that the system will reset after the timer expires

    Steps:
    ===============================================
    1. Enable the watchdog if it is not enabled
    2. Kill the watchdog incorrectly
    3. Ensure the system restarts
    """

    with allure.step("If watchdog is not running, Skip the test"):
        pid_wd = engines.dut.run_cmd("pidof wd_keepalive")
        if not pid_wd:
            pytest.skip("No pid for wd_keep_alive skipping test.")

    with allure.step("Getting the current timeout of the watchdog."):
        watchdog_timeout = int(get_current_watchdog_time(engines))
        log.info(f"Current watchdog timeout - {watchdog_timeout}")

    # Need to get the boot id to see if it actually shuts down
    with allure.step("Get boot id before shutting down watchdog."):
        cmd = "cat /proc/sys/kernel/random/boot_id"
        start_boot_id = engines.dut.run_cmd(cmd)
        log.info(f"Boot ID before killing watchdog - {start_boot_id}")

    # Kill watchdog
    with allure.step("Killing the watchdog process INCORRECTLY."):
        engines.dut.run_cmd("sudo kill -9 %s" % pid_wd)

    # Give enough time for the reset to occur
    with allure.step("Wait to ensure DUT eventually reboots."):
        log.info(
            "Waiting %d seconds to ensure DUT eventually resets."
            % ((watchdog_timeout * 6) + 5)
        )
        time.sleep((watchdog_timeout * 6) + 5)
        engines.dut.disconnect()

        try:
            code = check_shutdown_state(engines, start_boot_id)
        except Exception:
            assert False, "DUT never came back after the watchdog fired"

        # Code of 2 means the DUT restarted, good
        if code != 2:
            assert False, "DUT did not reset when the watchdog was killed \
incorrectly."
        else:
            log.info(
                "DUT correctly restarted when the watchdog was killed \
incorrectly."
            )

    with allure.step("Restart watchdog"):
        engines.dut.run_cmd("sudo systemctl start wd_keepalive")
        new_pid_wd = engines.dut.run_cmd("pidof wd_keepalive")
        if not new_pid_wd or (new_pid_wd == pid_wd):
            assert False, "Watchdog restart failed"

    log.info("Pass")


def get_current_watchdog_time(engines):
    """Grabs the amount of time the watchdog uses before reset

    This function is used to know the time the watchdog takes before it will
    actually reset the device

    :Returns:
        Returns the time in seconds that the watchdog takes before reset
    """

    timer_location = "/etc/watchdog.conf"

    time_for_reset = engines.dut.run_cmd("sudo grep 'watchdog-timeout' %s"
                                         % timer_location).split("=")[1]
    time_for_reset = time_for_reset.strip()

    return time_for_reset


def check_shutdown_state(engines, start_boot_id):
    """
    Returns 0 for good shutdown
    Returns 1 for a system that did not shutdown
    Returns 2 for a system that shutdown but also restarted
    """
    cmd = "cat /proc/sys/kernel/random/boot_id"
    new_boot_id = engines.dut.run_cmd(cmd)

    if new_boot_id == start_boot_id:
        # This means the DUT never actually shutdown
        return 1
    else:
        # This means that the DUT rebooted
        return 2


@pytest.mark.cumulus_only
@pytest.mark.cumulus
def test_platform_port_amount(engines, devices, random_api):
    """
    Validates the actual number of ports is equal to the expected number of ports
    defined in the HW specification for the platform. This test is specific to
    Cumulus Linux.
    Test flow:
       1. nv show platform interface
       2. Parse output to dict
       3. Count the number of ports excluding breakout ports
       4. Validate the actual number of ports is equal to the expected number of ports
    """

    output_dictionary = OutputParsingTool.parse_show_all_interfaces_output_to_dictionary(
        Port.show_interface()).get_returned_value()

    # dont count breakout ports
    breakout_ports = {'s1', 's2', 's3'}
    port_amount = 0
    for key in output_dictionary:
        if not any(ele in key for ele in breakout_ports) and key.startswith("swp"):
            port_amount += 1
    assert port_amount == devices.dut.get_ib_ports_num(), f'Found {port_amount} ports, expected {devices.dut.get_ib_ports_num()}'
    log.info(f'Found expected amount of ports {port_amount}')


@pytest.mark.cumulus_only
@pytest.mark.cumulus
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
def test_platform_fan_speed_change(engines, devices, test_api, output_format):
    """
    Name: Fan Speed Change
    ===============================================

    Description:
    ===============================================
    This test ensures that when the temperature is increased, the
    system will increase the fan speeds. When the temperature is lowered
    the fans speeds will decrease accordingly.

    Steps:
    ===============================================
    1. Get Original Fan Speeds
    2. Fake (simulate) a Temperature Change
    3. Ensure the fan speeds increase accordingly
    4. Set temperature back to normal.
    5. Endure the fan speeds are reduced.
    """

    TestToolkit.tested_api = test_api

    with allure.step("Create Platform object"):
        platform = Platform()

    with allure.step("Capture Original Fan Speed"):
        fan_db = {}
        raw_output = platform.environment.fan.show(output_format=output_format)
        field_names = OutputParsingTool.parse_show_output_to_field_names(
            raw_output, output_format=output_format, field_name_dict=devices.dut.fan_prop_auto).get_returned_value()
        ValidationTool.validate_set_equal(field_names, devices.dut.platform_environment_fan_values.keys()
                                          ).verify_result()
        fan_dict = OutputParsingTool.parse_show_output_to_dict(
            raw_output, output_format=output_format, field_name_dict=devices.dut.fan_prop_auto).get_returned_value()
        actual_fan_list = fan_dict.keys()
        ValidationTool.validate_set_equal(actual_fan_list, devices.dut.fan_list + devices.dut.psu_fan_list
                                          ).verify_result()
        for fan in devices.dut.fan_list:
            fan_db[fan] = {}
            fan_db[fan]['speed'] = int(fan_dict[fan]['current-speed'])
            fan_db[fan]['variance'] = int(fan_dict[fan]['current-speed']) * .15
            log.info(f'{fan}: Speed: {fan_db[fan]["speed"]} Variance: {fan_db[fan]["variance"]}')

    with allure.step("Fake Temperature Change"):
        fake_temperature = '70000'
        output = engines.dut.run_cmd('ls -ls /var/run/hw-management/thermal/fan_amb')
        fan_amb = str(re.findall('>\\s+([0-9a-z\\-_/\\.]+)', output)[0])
        output = engines.dut.run_cmd('ls -ls /var/run/hw-management/thermal/port_amb')
        port_amb = str(re.findall('>\\s+([0-9a-z\\-_/\\.]+)', output)[0])
        log.info(f'fan_amb register: {fan_amb} port_amb register: {port_amb}')
        engines.dut.run_cmd(f'sudo bash -c "echo {fake_temperature} > /var/run/hw-management/thermal/port_amb_fake_temp"')
        engines.dut.run_cmd(f'sudo bash -c "echo {fake_temperature} > /var/run/hw-management/thermal/fan_amb_fake_temp"')
        engines.dut.run_cmd('sudo unlink /var/run/hw-management/thermal/fan_amb')
        engines.dut.run_cmd('sudo unlink /var/run/hw-management/thermal/port_amb')
        engines.dut.run_cmd('sudo ln -s /var/run/hw-management/thermal/fan_amb_fake_temp /var/run/hw-management/thermal/fan_amb')
        engines.dut.run_cmd('sudo ln -s /var/run/hw-management/thermal/port_amb_fake_temp /var/run/hw-management/thermal/port_amb')

    with allure.step("Check Fan Speed Increased"):
        max_wait_time = 60  # seconds
        sleep_interval = 2  # seconds
        start_time = time.time()
        for fan in devices.dut.fan_list:
            passed = False
            target_speed = int(fan_db[fan]['speed'] + fan_db[fan]['variance'])

            while time.time() - start_time < max_wait_time:
                fan_output = OutputParsingTool.parse_show_output_to_dict(
                    platform.environment.fan.show(fan, output_format=output_format),
                    output_format=output_format
                ).get_returned_value()

                current_speed = int(fan_output['current-speed'])
                if current_speed > target_speed:
                    log.info(f"{fan} fan speed has increased. New speed is {current_speed}, the original speed was {fan_db[fan]['speed']}")
                    passed = True
                    break

                time.sleep(sleep_interval)

            if not passed:
                # Set fake temperature back to real temperature
                engines.dut.run_cmd('sudo unlink /var/run/hw-management/thermal/fan_amb')
                engines.dut.run_cmd('sudo unlink /var/run/hw-management/thermal/port_amb')
                engines.dut.run_cmd(f'sudo ln -s {fan_amb} /var/run/hw-management/thermal/fan_amb')
                engines.dut.run_cmd(f'sudo ln -s {port_amb} /var/run/hw-management/thermal/port_amb')
                log.info(f"{fan} fan new speed is {current_speed}, the original speed was {fan_db[fan]['speed']} target speed was {target_speed}")
                assert False, 'Fan speed did not increase after the temperature changed'

    with allure.step("Check Fan Speed Decreased"):
        # Set fake temperature back to real temperature
        engines.dut.run_cmd('sudo unlink /var/run/hw-management/thermal/fan_amb')
        engines.dut.run_cmd('sudo unlink /var/run/hw-management/thermal/port_amb')
        engines.dut.run_cmd(f'sudo ln -s {fan_amb} /var/run/hw-management/thermal/fan_amb')
        engines.dut.run_cmd(f'sudo ln -s {port_amb} /var/run/hw-management/thermal/port_amb')

        max_wait_time = 60  # seconds
        sleep_interval = 2  # seconds
        start_time = time.time()
        for fan in devices.dut.fan_list:
            passed = False
            target_speed = int(fan_db[fan]['speed'] + fan_db[fan]['variance'])

            while time.time() - start_time < max_wait_time:
                fan_output = OutputParsingTool.parse_show_output_to_dict(
                    platform.environment.fan.show(fan, output_format=output_format),
                    output_format=output_format
                ).get_returned_value()

                current_speed = int(fan_output['current-speed'])
                if current_speed <= target_speed:
                    log.info(f"{fan} fan speed has decreased. New speed is {current_speed}, the original speed was {fan_db[fan]['speed']}")
                    passed = True
                    break

                time.sleep(sleep_interval)

            if not passed:
                log.info(f"{fan} fan new speed is {current_speed}, the original speed was {target_speed} target speed was {target_speed}")
                assert False, 'Fan speed did not decrease after the temperature changed'


@pytest.mark.cumulus_only
@pytest.mark.cumulus
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
def test_platform_fan_speed_restart_switchd(engines, devices, test_api, output_format):
    """
        Name: Fan Speed Restart Switchd
        ===============================================
        Description:

        This test ensures that the fan speed goes back to target speed (original speed plus variance)
        Steps:
        ===============================================
        1. Read current fan speed
        2. Restart switchd.services
        4. Poll fans and verify speed returns to target speed within 800 seconds

    """

    TestToolkit.tested_api = test_api

    with allure.step("Create Platform object"):
        platform = Platform()

    with allure.step("Capture Original Fan Speed"):
        fan_db = {}
        raw_output = platform.environment.fan.show(output_format=output_format)
        field_names = OutputParsingTool.parse_show_output_to_field_names(
            raw_output, output_format=output_format, field_name_dict=devices.dut.fan_prop_auto).get_returned_value()
        ValidationTool.validate_set_equal(field_names, devices.dut.platform_environment_fan_values.keys()
                                          ).verify_result()
        fan_dict = OutputParsingTool.parse_show_output_to_dict(
            raw_output, output_format=output_format, field_name_dict=devices.dut.fan_prop_auto).get_returned_value()
        actual_fan_list = fan_dict.keys()
        ValidationTool.validate_set_equal(actual_fan_list, devices.dut.fan_list + devices.dut.psu_fan_list
                                          ).verify_result()
        for fan in devices.dut.fan_list:
            fan_db[fan] = {}
            fan_db[fan]['speed'] = int(fan_dict[fan]['current-speed'])
            fan_db[fan]['variance'] = int(fan_dict[fan]['current-speed']) * .15
            log.info(f'{fan}: Speed: {fan_db[fan]["speed"]} Variance: {fan_db[fan]["variance"]}')

    with allure.step("Restart watchdog"):
        engines.dut.run_cmd("sudo systemctl reset-failed switchd")
        engines.dut.run_cmd("sudo systemctl restart switchd")

    with allure.step("Check Fan Speeds Return to Normal"):
        max_wait_time = 800  # seconds
        sleep_interval = 5  # seconds
        start_time = time.time()
        for fan in devices.dut.fan_list:
            passed = False
            target_speed = int(fan_db[fan]['speed'] + fan_db[fan]['variance'])
            while time.time() - start_time < max_wait_time:
                fan_output = OutputParsingTool.parse_show_output_to_dict(
                    platform.environment.fan.show(fan, output_format=output_format),
                    output_format=output_format
                ).get_returned_value()

                current_speed = int(fan_output['current-speed'])
                if current_speed <= target_speed:
                    log.info(f'{fan} target speed = {target_speed}, current speed: {current_speed}. The difference is {current_speed - target_speed}')
                    passed = True
                    break

                time.sleep(sleep_interval)

            if not passed:
                log.info(f'Fan speed {current_speed} is not within the original speed + variance: {target_speed}')
                assert False, f'Fan speed did not return to normal for {fan} after switch restart'
