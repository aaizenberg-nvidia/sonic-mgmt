from __future__ import annotations

from datetime import datetime, timezone
import logging
import random
import pytest
import retry
import math
import json
import time
import re
import os

from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.tests_nvos.system.clock.ClockTools import ClockTools
from ngts.tests_nvos.cluster.cluster_tools import ClusterTools
from ngts.tests_nvos.system.gnmi.GnmiClient import GnmiClient
from ngts.nvos_constants.constants_nvos import DatabaseConst
from ngts.constants.constants import GnmiConsts, InfraConst
from ngts.nvos_constants.constants_nvos import NvosConst
from ngts.tools.test_utils import allure_utils as allure
from ngts.tests_nvos.helpers import redmine_helpers
from ngts.nvos_tools.system.System import System
from ngts.nvos_tools.nmx.Cluster import Cluster
from ngts.ngts_types import EnginesT, DevicesT
from ngts.nvos_tools.infra.Tools import Tools
from ngts.nvos_tools.infra.Fae import Fae
from ngts.tests_nvos import constants

logger = logging.getLogger(__name__)


CPU_WARNING_THRESHOLD = "cpu-warning-threshold"
CPU_RESTARTS_THRESHOLD = "cpu-restart-threshold"
MEMORY_WARNING_THRESHOLD = "memory-warning-threshold"
MEMORY_RESTART_THRESHOLD = "memory-restart-threshold"
NUMBER_OF_SAMPLES = "number-of-samples"

DEFAULT_VALUES = {CPU_WARNING_THRESHOLD: 50,
                  CPU_RESTARTS_THRESHOLD: 100,
                  MEMORY_WARNING_THRESHOLD: 90,
                  MEMORY_RESTART_THRESHOLD: 95,
                  NUMBER_OF_SAMPLES: 5}

# Mapping between warning and restart thresholds (warning must be < restart)
THRESHOLD_PAIRS = {
    MEMORY_WARNING_THRESHOLD: MEMORY_RESTART_THRESHOLD,
    MEMORY_RESTART_THRESHOLD: MEMORY_WARNING_THRESHOLD,
    CPU_WARNING_THRESHOLD: CPU_RESTARTS_THRESHOLD,
    CPU_RESTARTS_THRESHOLD: CPU_WARNING_THRESHOLD,
}

WARNING_MSG_PATTERN = r"WARNING cpu_memory_container_checker: .*(Memory|CPU) usage ([0-9]+(\.[0-9]+))% is above expected threshold ([0-9]+)%"
HIGH_USAGE_EVENT_MSG_PATTERN = r"NOTICE cpu_memory_container_checker: :- publish: EVENT_PUBLISHED:.*\"resource\":\"{}\".*\"text\":\"(Memory|CPU) usage ([0-9]+(\.[0-9]+))% is above expected threshold ([0-9]+)%\""
GENERATING_TECH_SUPPORT_MSG_PATTERN = r"INFO cpu_memory_container_checker: Generating techsupport"
TECH_SUPPORT_GENERATED_MSG_PATTERN = r"INFO cpu_memory_container_checker: Generated techsupport"
NO_TECH_SUPPORT_GENERATED_MSG_PATTERN = r"INFO cpu_memory_container_checker: Techsupport already generated in the last 1440 minutes"
RESTARTING_DOCKER_MSG_PATTERN = r"INFO cpu_memory_container_checker: Restarting .* '{}'"
DOCKER_RESTARTED_MSG_PATTERN = r"INFO cpu_memory_container_checker: Restarted .* '{}'"
BACK_TO_NORMAL_USAGE_MSG_PATTERN = r"INFO cpu_memory_container_checker: (Memory|CPU) usage is back to normal"
NORMAL_USAGE_EVENT_MSG_PATTERN = r"NOTICE cpu_memory_container_checker: :- publish: EVENT_PUBLISHED:.*\"resource\":\"{}\".*\"text\":\"(Memory|CPU) usage is back to normal\""


@pytest.mark.timeout(5 * constants.MINUTE, func_only=True)
def test_configuration_cli(engines: EnginesT, devices: DevicesT, random_api: str) -> None:
    """
        @summary: check 'CPU/memory monitoring' functionality

        Test flow:
        1. Verify that feature is enabled: "nv show fae system control dockers resource-limit"
        2. Disable feature: "nv set fae system control dockers resource-limit state disabled"
        3. Enable feature: "nv set fae system control dockers resource-limit state enabled"
        4. Randomize memory-threshold from list ["memory-warning-threshold", "memory-restart-threshold"]
        5. Change memory-threshold: "nv set fae system control dockers resource-limit <selected memory threshold> <new-threshold>"
        6. Randomize cpu-threshold from list ["cpu-warning-threshold", "cpu-restart-threshold"]
        7. Change cpu-threshold: "nv set fae system control dockers resource-limit <selected cpu threshold> <new-threshold>"
        8. Change sample-number: "nv set fae system control dockers resource-limit number-of-samples <new-sample-number>"
        9. Randomize a docker form monitored dockers list
        10. Change memory-warning-threshold for the selected docker in config_db and verify output
        11. Change memory-restart-threshold for the selected docker in config_db and verify output
        12. Change cpu-warning-threshold for the selected docker in config_db and verify output
        13. Change cpu-restart-threshold for the selected docker in config_db and verify output
        14. Change sample-number for the selected docker in config_db and verify output
        15. Set thresholds to default values
    """
    fae = Fae()
    cluster = Cluster()
    changed_configurations = []

    try:
        with allure.step("Run 'nv show fae system control dockers resource-limit' command and verify feature is enabled"):
            output_dict = OutputParsingTool.parse_json_str_to_dictionary(fae.system.resource_limit.show()).verify_result()
            assert output_dict["state"] == NvosConst.ENABLED, "CPU/memory monitoring feature is unexpectedly disabled"

        with allure.step("Disable feature and verify"):
            enable_feature_and_verify(False)

        with allure.step("Enable feature and verify"):
            enable_feature_and_verify()

        memory_thresholds = [MEMORY_WARNING_THRESHOLD, MEMORY_RESTART_THRESHOLD]
        with allure.step(f"Randomize memory threshold from list: {memory_thresholds}"):
            tested_threshold = random.choice(memory_thresholds)

        with allure.step(f"Change {tested_threshold} value and verify"):
            new_memory_threshold = randomize_new_value(tested_threshold, output_dict)
            set_value_and_verify(limit=tested_threshold, new_value=new_memory_threshold)
            changed_configurations.append(tested_threshold)

        cpu_thresholds = [CPU_WARNING_THRESHOLD, CPU_RESTARTS_THRESHOLD]
        with allure.step(f"Randomize CPU threshold from list: {cpu_thresholds}"):
            tested_threshold = random.choice(cpu_thresholds)

        with allure.step(f"Change {tested_threshold} value and verify"):
            new_cpu_threshold = randomize_new_value(tested_threshold, output_dict)
            set_value_and_verify(limit=tested_threshold, new_value=new_cpu_threshold)
            changed_configurations.append(tested_threshold)

        with allure.step("Change number of samples"):
            new_sample_number = randomize_new_value(NUMBER_OF_SAMPLES, output_dict)
            set_value_and_verify(limit=NUMBER_OF_SAMPLES, new_value=new_sample_number)
            changed_configurations.append(NUMBER_OF_SAMPLES)

        with allure.step("Check if device has NMX"):
            has_nmx = getattr(devices.dut, "has_nmx", False)
            if not has_nmx:
                logger.info("Device does not have NMX")
            else:
                with allure.step("Device has NMX - Start cluster"):
                    cluster.set(op_param_name="state", op_param_value='enabled', apply=True)
                    ClusterTools.wait_for_apps_to_be_in_wanted_state(cluster, cluster_expected_state='enabled',
                                                                     nmx_c_expected_state='up')

        with allure.step("Verify monitored dockers list includes all potential dockers"):
            init_monitored_dockers_list(devices, has_nmx, output_dict)

        with allure.step("Randomize a docker from monitored dockers list"):
            monitored_dockers_list = [item.strip() for item in output_dict["monitored-list"].split(",")]
            docker = random.choice(monitored_dockers_list)
            logger.info(f"Selected docker: {docker}")

        for limit in DEFAULT_VALUES.keys():
            with allure.step(f"Change {limit} for {docker}"):
                set_value_for_docker_and_verify(engines, limit, docker)

    finally:
        with allure.step("Disable cluster in case device has NMX"):
            if has_nmx:
                Cluster().unset(apply=True)
                ClusterTools.wait_for_apps_to_be_in_wanted_state(cluster, cluster_expected_state='disabled',
                                                                 nmx_c_expected_state='down')
        with allure.step("Set thresholds to default values"):
            restore_thresholds_to_defaults(changed_configurations)


@pytest.mark.timeout(15 * constants.MINUTE, func_only=True)
def test_high_memory_usage_simulation(engines: EnginesT, devices: DevicesT, random_api: str) -> None:
    """
        @summary: simulate high memory usage and test feature functionality

        Test flow:
        1. Verify that feature is enabled: run "nv show fae system control dockers resource-limit"
        2. Start cluster for NVL systems
        3. Randomize a docker form monitored dockers list
        4. Change memory-warning-threshold: "nv set fae system control dockers resource-limit memory-warning-threshold 15"
        5. Simulate high memory usage for selected docker
        6. Verify events in events table
        7. Verify there is a warning message in logs describing high memory usage
        8. Change memory-restart-threshold: "nv set fae system control dockers resource-limit memory-restart-threshold 15"
        9. Verify events in events table
        10. Verify there is a warning message in logs describing high memory usage
        11. Verify docker was restarted
        12. Verify tech-support file was generated
        13. Simulate high memory usage again
        12. Verify no new tech-support file was generated
        13. Set thresholds to default values
    """
    fae = Fae()
    cluster = Cluster()
    changed_configurations = []

    try:
        with allure.step("Run 'nv show fae system control dockers resource-limit' command and verify feature is enabled"):
            output_dict = OutputParsingTool.parse_json_str_to_dictionary(fae.system.resource_limit.show()).verify_result()
            assert output_dict["state"] == NvosConst.ENABLED, "CPU/memory monitoring feature is unexpectedly disabled"

        with allure.step("Check if device has NMX"):
            has_nmx = getattr(devices.dut, "has_nmx", False)
            if not has_nmx:
                logger.info("Device does not have NMX")
            else:
                with allure.step("Device has NMX - Start cluster"):
                    cluster.set(op_param_name="state", op_param_value='enabled', apply=True)
                    ClusterTools.wait_for_apps_to_be_in_wanted_state(cluster, cluster_expected_state='enabled',
                                                                     nmx_c_expected_state='up')

        with allure.step("Verify monitored dockers list includes all potential dockers"):
            init_monitored_dockers_list(devices, has_nmx, output_dict)

        with allure.step("Randomize a docker from monitored dockers list"):
            monitored_dockers_list = [item.strip() for item in output_dict["monitored-list"].split(",")]
            docker = random.choice(monitored_dockers_list)
            logger.info(f"Selected docker: {docker}")

        with allure.step(f"Change {MEMORY_WARNING_THRESHOLD} and start high memory usage simulation"):
            warning_threshold = max(_get_max_docker_mem_usage(engines) + 5, 15)
            set_value_and_verify(limit=MEMORY_WARNING_THRESHOLD, new_value=warning_threshold)
            changed_configurations.append(MEMORY_WARNING_THRESHOLD)
            simulate_and_verify_high_memory_usage(engines, devices, docker, warning_phase=True, threshold=warning_threshold)

        with allure.step(f"Change {MEMORY_RESTART_THRESHOLD} and start another high memory simulation"):
            # Restart threshold must be greater than warning threshold
            restart_threshold = warning_threshold + 1
            set_value_and_verify(limit=MEMORY_RESTART_THRESHOLD, new_value=restart_threshold)
            changed_configurations.append(MEMORY_RESTART_THRESHOLD)
            simulate_and_verify_high_memory_usage(engines, devices, docker, threshold=restart_threshold)

        with allure.step("Simulate high memory usage again and verify no new tech-support file was generated"):
            verify_no_new_tech_support_on_repeated_high_usage(engines, docker, warning_threshold)

    finally:
        with allure.step("Disable cluster in case device has NMX"):
            if has_nmx:
                Cluster().unset(apply=True)
                ClusterTools.wait_for_apps_to_be_in_wanted_state(cluster, cluster_expected_state='disabled',
                                                                 nmx_c_expected_state='down')
        with allure.step("Set thresholds to default values"):
            restore_thresholds_to_defaults(changed_configurations)


@pytest.mark.timeout(10 * constants.MINUTE, func_only=True)
def test_high_cpu_usage_simulation(engines: EnginesT, devices: DevicesT, random_api: str) -> None:
    """
        @summary: simulate high cpu usage and test feature functionality

        Test flow:
        1. Verify that feature is enabled: "nv show fae system control dockers resource-limit"
        2. Start cluster for NVL systems
        3. Randomize a docker form monitored dockers list
        4. Change cpu-threshold: "nv set fae system control dockers resource-limit cpu-warning-threshold 10"
        5. Simulate high cpu usage for selected docker
        6. Verify events in events table
        7. Verify there is a warning message in logs describing high cpu usage
    """
    fae = Fae()
    cluster = Cluster()
    changed_configurations = []

    try:
        with allure.step("Run 'nv show fae system control dockers resource-limit' command and verify feature is enabled"):
            output_dict = OutputParsingTool.parse_json_str_to_dictionary(fae.system.resource_limit.show()).verify_result()
            assert output_dict["state"] == NvosConst.ENABLED, "CPU/memory monitoring feature is unexpectedly disabled"

        with allure.step("Check if device has NMX"):
            has_nmx = getattr(devices.dut, "has_nmx", False)
            if not has_nmx:
                logger.info("Device does not have NMX")
            else:
                with allure.step("Device has NMX - Start cluster"):
                    cluster.set(op_param_name="state", op_param_value='enabled', apply=True)
                    ClusterTools.wait_for_apps_to_be_in_wanted_state(cluster, cluster_expected_state='enabled',
                                                                     nmx_c_expected_state='up')

        with allure.step("Check dockers in monitored dockers list"):
            init_monitored_dockers_list(devices, has_nmx, output_dict)

        with allure.step("Randomize a docker from monitored dockers list"):
            monitored_dockers_list = [item.strip() for item in output_dict["monitored-list"].split(",")]
            docker = random.choice(monitored_dockers_list)
            logger.info(f"Selected docker: {docker}")

        with allure.step(f"Change {CPU_WARNING_THRESHOLD}"):
            new_cpu_threshold = 10
            set_value_and_verify(limit=CPU_WARNING_THRESHOLD, new_value=new_cpu_threshold)
            changed_configurations.append(CPU_WARNING_THRESHOLD)

        with allure.step(f"Simulate high CPU usage on {docker}"):
            start_time = datetime.now()
            start_time_utc = _get_system_time()
            simulate_high_cpu_usage(engines, devices, docker)

        with allure.step("Sleeping for 1.5 min"):
            time.sleep(1.5 * constants.MINUTE)

        with allure.step("Verify behavior after high CPU usage"):
            verify_behavior_after_simulation(
                engines,
                devices,
                resource="cpu",
                docker=docker,
                start_time=start_time,
                start_time_utc=start_time_utc,
                warning_phase=True,
            )

    finally:
        with allure.step("Disable cluster in case device has NMX"):
            if has_nmx:
                Cluster().unset(apply=True)
                ClusterTools.wait_for_apps_to_be_in_wanted_state(cluster, cluster_expected_state='disabled',
                                                                 nmx_c_expected_state='down')
        with allure.step("Set thresholds to default values"):
            restore_thresholds_to_defaults(changed_configurations)


def _get_system_time() -> datetime:
    date_format = "%Y-%m-%d %H:%M:%S"
    sys_datetime = System().datetime.show()

    return datetime.strptime(
        ClockTools.get_local_time_from_show_system_date_time_output(sys_datetime),
        date_format,
    )


def _get_system_time_utc(engines: EnginesT) -> datetime:
    with allure.step("Get DUT UTC time"):
        result: str = engines.dut.run_cmd(r"date -u '+%Y-%m-%dT%H:%M:%SZ'")
        timestamp_utc = result.strip().splitlines()[-1]
        allure.attach("DUT UTC time", timestamp_utc, log=False)

    return _parse_utc_timestamp(timestamp_utc)


def _parse_utc_timestamp(timestamp: str) -> datetime:
    """Parse a UTC timestamp with or without fractional seconds."""
    normalized_timestamp = timestamp.strip()
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?Z", normalized_timestamp)
    assert match, f"Failed to parse UTC timestamp: {timestamp!r}"

    seconds = match.group(1)
    fraction = (match.group(2) or "")[:6].ljust(6, "0")
    parsed_timestamp = datetime.strptime(f"{seconds}.{fraction}", "%Y-%m-%dT%H:%M:%S.%f")
    return parsed_timestamp.replace(tzinfo=timezone.utc)


def _get_a_docker_allocated_memory(engines: EnginesT, docker: str) -> int:
    with allure.step("Get a docker allocated memory"):
        result: str = engines.dut.run_cmd(r'docker stats %s --no-stream --format "{{.MemUsage}}"' % docker)
        allure.attach("Docker memory usage", result, log=False)
        _, allocated_memory = result.strip().split(' / ')
        return _parse_memory_value_to_gib(allocated_memory)


def _get_max_docker_mem_usage(engines: EnginesT) -> int:
    with allure.step("Get docker memory usage"):
        result: str = engines.dut.run_cmd(r'docker stats --no-stream --format "table {{.Name}}\t{{.MemUsage}}\t{{.MemPerc}}"')
        allure.attach("Docker memory usage", result, log=False)

        mem_usage_percentages = [float(i.rsplit(maxsplit=1)[-1].strip('%')) for i in result.splitlines()[1:]]
        assert mem_usage_percentages, "docker stats returned no docker memory usage rows"
        max_mem_usage = max(mem_usage_percentages)
        logger.info(f"Max memory usage: {max_mem_usage}%")

    return math.ceil(max_mem_usage)


def _parse_memory_value_to_gib(memory_str: str) -> float:
    """
        Helper function to parse memory value to GiB.
    """
    mapper = {
        'B': 1,
        'KB': 1000,
        'KiB': 1024,
        'MB': 1000 ** 2,
        'MiB': 1024 ** 2,
        'GB': 1000 ** 3,
        'GiB': 1024 ** 3,
    }

    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([KMGT]i?B|B)", memory_str.strip())
    assert match, f"Failed to parse docker memory value: {memory_str!r}"

    value = float(match.group(1))
    unit = match.group(2)
    return value * mapper[unit] / (1024 ** 3)


def _get_docker_mem_limit(engines: EnginesT, docker: str) -> float:
    with allure.step("Get docker memory limit"):
        result: str = engines.dut.run_cmd(r'docker stats %s --no-stream --format "{{.MemUsage}}"' % docker)
        _, limit = result.strip().split(' / ')
        return _parse_memory_value_to_gib(limit)


def randomize_new_value(limit: str, output_dict: dict) -> int:
    """
        Helper function to randomize new value for resource.
        For warning thresholds: value must be less than restart threshold.
        For restart thresholds: value must be greater than warning threshold.
    """
    curr_value = int(output_dict[limit])
    is_warning = limit in [MEMORY_WARNING_THRESHOLD, CPU_WARNING_THRESHOLD]
    is_restart = limit in [MEMORY_RESTART_THRESHOLD, CPU_RESTARTS_THRESHOLD]

    if limit == NUMBER_OF_SAMPLES:
        return random.choice([n for n in range(1, 21) if n != curr_value])

    # Get the related threshold value if this is a warning/restart threshold
    related = THRESHOLD_PAIRS.get(limit)
    related_val = int(output_dict[related]) if related else None

    if is_warning:
        # Warning must be < restart, so max is (restart - 1)
        min_val, max_val = DEFAULT_VALUES[limit], related_val - 1
    elif is_restart:
        # Restart must be > warning, so min is (warning + 1)
        min_val, max_val = related_val + 1, 100
    else:
        min_val, max_val = DEFAULT_VALUES[limit], 100

    return random.choice([n for n in range(min_val, max_val + 1) if n != curr_value])


def enable_feature_and_verify(status: bool = True) -> None:
    """
        Helper function to enable/disable the CPU/memory monitoring feature.
    """
    state = NvosConst.ENABLED if status else NvosConst.DISABLED

    Fae().system.resource_limit.set(op_param_name="state", op_param_value=state, apply=True).verify_result()
    show_and_verify(attribute="state", expected_value=state)


def set_value_and_verify(limit: str, new_value: int) -> None:
    """
        Helper function to set a resource value and verify it.
    """
    with allure.step(f"Set {limit} to be {new_value} and verify threshold changed as expected"):
        Fae().system.resource_limit.set(op_param_name=limit, op_param_value=new_value, apply=True).verify_result()
        show_and_verify(attribute=limit, expected_value=str(new_value))


def restore_thresholds_to_defaults(changed_configurations: list[str]) -> None:
    """
        Helper function to restore thresholds to default values in the correct order.
        Restart thresholds must be set before warning thresholds to avoid constraint violation
        (warning must be less than restart).
    """
    # Restart thresholds first (priority 0), warning thresholds second (priority 1), others last (priority 2)
    restart_thresholds = {CPU_RESTARTS_THRESHOLD, MEMORY_RESTART_THRESHOLD}
    warning_thresholds = {CPU_WARNING_THRESHOLD, MEMORY_WARNING_THRESHOLD}

    def priority(limit):
        return 0 if limit in restart_thresholds else (1 if limit in warning_thresholds else 2)

    for limit in sorted(changed_configurations, key=priority):
        set_value_and_verify(limit=limit, new_value=DEFAULT_VALUES[limit])


def show_and_verify(attribute: str, expected_value: str) -> None:
    """
        Helper function to show output of "nv show fae system control dockers resource-limit" command, and verify it.
    """
    output_dict = OutputParsingTool.parse_json_str_to_dictionary(Fae().system.resource_limit.show()).verify_result()
    curr_value = output_dict[attribute]
    assert curr_value == expected_value, f"Expected: {attribute}={expected_value}. Got: {attribute}={curr_value}"


def set_value_for_docker_and_verify(engines: EnginesT, limit: str, docker: str) -> None:
    """
        Helper function to set a resource value for a specific docker in config_db and verify it.
    """
    with allure.step(f"Get curr {limit}"):
        output_dict = OutputParsingTool.parse_json_str_to_dictionary(Fae().system.resource_limit.show()).verify_result()

    with allure.step(f"Randomize new {limit}"):
        new_value = str(randomize_new_value(limit, output_dict))

    with allure.step(f"Set {limit} to {new_value} in config_db"):
        db_config = f"CPU_MEMORY_MONITOR|{docker}"
        Tools.DatabaseTool.sonic_db_cli_hset(engine=engines.dut, asic="", db_name=DatabaseConst.CONFIG_DB_NAME,
                                             db_config=db_config, param=limit, value=new_value, allow_create=True)

    with allure.step(f"Verify {limit} changed only for {docker}"):
        verify_resource_value_in_db(engines, output_dict, limit, docker, new_value)

    with allure.step(f"Delete {limit} from config_db"):
        Tools.DatabaseTool.sonic_db_cli_hdel(engine=engines.dut, asic="", db_name=DatabaseConst.CONFIG_DB_NAME,
                                             db_config=db_config, param=limit)


def verify_resource_value_in_db(engines: EnginesT, output_dict: dict, limit: str, tested_docker: str, new_value: str) -> None:
    """
        Helper function to verify a resource value for a specific docker in config_db.
    """
    monitored_dockers_list = [item.strip() for item in output_dict["monitored-list"].split(",")]
    for docker in monitored_dockers_list:
        db_config = f"CPU_MEMORY_MONITOR|{docker}"
        curr_value = Tools.DatabaseTool.sonic_db_cli_hget(engine=engines.dut, asic="",
                                                          db_name=DatabaseConst.CONFIG_DB_NAME,
                                                          db_config=db_config, param=limit)
        if docker == tested_docker:
            assert curr_value == new_value, f"{limit} didn't change for {tested_docker}"
        else:
            assert curr_value != new_value, f"{limit} changed for {docker} unexpectedly"


def simulate_high_cpu_usage(engines: EnginesT, devices: DevicesT, docker: str) -> None:
    """
        Helper function to simulate high CPU usage for a docker.
    """
    cmd = """
    end=$(( $(date +%s) + 120 ))
    num_cores=$(nproc)
    iterations=$((3 * {cores_num}))

    for i in $(seq 1 $iterations); do
      (
        while [ $(date +%s) -lt $end ]; do
          start=$(date +%s%3N)

          while :; do
            now=$(date +%s%3N)
            elapsed=$((now - start))
            if [ $elapsed -ge 500 ]; then break; fi
            : $(( (RANDOM * RANDOM) % 10000 ))
          done

          sleep 0.5
        done
      ) &
    done

    wait
    """

    cores_num = devices.dut.core_count
    engines.dut.run_cmd(f"docker exec -it {docker} bash -c '{cmd.strip().format(cores_num=cores_num)}'")
    execution_res = engines.dut.run_cmd("echo \"$?\"").strip().split('\n')[-1]
    assert execution_res in ['0', '137'], "High CPU usage simulation failed"  # 0=success, 137=killed
    logger.info("High CPU usage simulation was executed successfully")


def simulate_and_verify_high_memory_usage(
    engines: EnginesT,
    devices: DevicesT,
    docker: str,
    /, *,
    warning_phase: bool = False,
    threshold: int = 15,
) -> None:
    """
        Helper function to simulate high memory usage for a docker.
    """
    total_memory = _get_docker_mem_limit(engines, docker)
    memory_to_use = math.ceil(total_memory * (min(100, (threshold + 10)) / 100.0))
    start_time = _get_system_time()
    start_time_utc = _get_system_time_utc(engines)

    phase_desc = MEMORY_WARNING_THRESHOLD if warning_phase else MEMORY_RESTART_THRESHOLD
    with allure.step(f"Simulate high memory usage for {docker} - exceed {threshold}% during {phase_desc} phase"):
        engines.dut.run_cmd(f"docker exec -it {docker} bash -c 'head -c {memory_to_use}G /dev/zero | tail | sleep 120'")
        execution_res = engines.dut.run_cmd("echo $?").strip().split('\n')[-1]
        assert execution_res in ['0', '137'], "High memory usage simulation failed"  # 0=success, 137=killed
    logger.info("High memory usage simulation was executed successfully")

    verify_behavior_after_simulation(
        engines,
        devices,
        resource="memory",
        docker=docker,
        start_time=start_time,
        start_time_utc=start_time_utc,
        warning_phase=warning_phase,
    )


def verify_no_new_tech_support_on_repeated_high_usage(engines: EnginesT, docker: str, threshold: int) -> None:
    """
        Helper function to simulate high memory usage for a second time and verify no new tech-support file was generated.
    """
    total_memory = _get_a_docker_allocated_memory(engines, docker)
    memory_to_use = math.ceil(total_memory * min(100, (threshold + 10)) / 100.0)

    with allure.step(f"Simulate high memory usage for {docker} - exceed {threshold}%"):
        start_time_utc = _get_system_time()
        engines.dut.run_cmd(
            f"docker exec -it {docker} bash -c 'head -c {memory_to_use}G /dev/zero | tail | sleep 120'")

    with allure.step("Sleeping for 1 min"):
        time.sleep(constants.MINUTE)

    with allure.step("Verify no new tech-support file was generated"):
        verify_tech_support_generation_time(engines, start_time_utc)


def _verify_expected_logs_by_time(engines: EnginesT, system: System, start_time: datetime, warning_phase: bool) -> None:
    # generating tech-support needs 3+ mins (bug: 4445420), change to 1.5 after fix
    tries = 6
    if redmine_helpers.is_bug_active(4445420) or not warning_phase:
        logger.info("Generating tech-support needs 3+ mins, increasing retry attempts to 7")
        tries = 9

    @retry.retry(tries=tries, delay=constants.MINUTE)
    def _verify_expected_logs_by_time() -> None:
        system.log.verify_expected_logs_by_time([WARNING_MSG_PATTERN], engines.dut, only_latest_log=False, start_time=start_time)

    return _verify_expected_logs_by_time()


def verify_behavior_after_simulation(
    engines: EnginesT,
    devices: DevicesT,
    /, *,
    resource: str,
    docker: str,
    start_time: datetime,
    start_time_utc: datetime = None,
    warning_phase: bool = False,
) -> None:
    """
        Helper function to verify behavior after high usage simulation.
    """
    system = System()
    with allure.step(f"Verify warning message in log from {start_time}"):
        _verify_expected_logs_by_time(engines, system, start_time, warning_phase)

    with allure.step("Verify events in log"):
        verify_events_in_logs(engines, system, docker, start_time)

    events = get_events(resource)
    with allure.step("Verify events in events table"):
        verify_events_in_events_table(events, docker)
    with allure.step("Verify events were streamed by gnmi client"):
        verify_gnmic_events(engines, devices, events, docker, resource)
    if not warning_phase:
        with allure.step("Verify docker was restarted"):
            verify_docker_restart(engines, resource, docker, start_time, start_time_utc)

    if not warning_phase:
        with allure.step("Verify tech-support file was generated"):
            verify_tech_support_generation_time(engines, start_time, not warning_phase)


def get_events(resource: str) -> tuple[tuple[str, dict], tuple[str, dict]]:
    events_dict = OutputParsingTool.parse_json_str_to_dictionary(System().events.show()).verify_result()
    high_usage_event = normal_usage_event = ""
    for event_id, event in events_dict.items():
        if isinstance(event, dict):
            if re.match(r"{} usage ([0-9]+(\.[0-9]+))% is above expected threshold ([0-9]+)%".format(resource), event['text'].lower()):
                high_usage_event = (event_id, event)
            elif re.match(r"{} usage is back to normal".format(resource), event['text'].lower()):
                normal_usage_event = (event_id, event)
    return high_usage_event, normal_usage_event


@retry.retry(tries=13, delay=10)
def verify_events_in_logs(engines: EnginesT, system: System, docker: str, start_time: datetime) -> None:
    log_message_list = [HIGH_USAGE_EVENT_MSG_PATTERN.format(docker),
                        NORMAL_USAGE_EVENT_MSG_PATTERN.format(docker)]
    system.log.verify_expected_logs_by_time(log_message_list, engines.dut, only_latest_log=False,
                                            start_time=start_time)


def verify_events_in_events_table(events: tuple[tuple[str, dict], tuple[str, dict]], docker: str) -> None:
    """
        Helper function to check that events were added to events table.
    """
    high_usage_event = events[0][1]
    normal_usage_event = events[1][1]
    assert high_usage_event != "" and normal_usage_event != "", "Couldn't find events in events table"

    for event in [high_usage_event, normal_usage_event]:
        with allure.independent_step(f"Check which docker caused the event: {docker}"):
            docker_in_log = event["resource"]
            docker_in_log_err_msg = f"Message in log is unexpectedly describing {docker_in_log} and not {docker}"
            assert docker_in_log == docker, docker_in_log_err_msg


def verify_gnmic_events(engines: EnginesT, devices: DevicesT, events: tuple[tuple[str, dict], tuple[str, dict]], docker: str, resource: str) -> None:
    with allure.step("Get events-id"):
        high_usage_event_id = events[0][0]
        normal_usage_event_id = events[1][0]

    with allure.step("Get gnmi client"):
        client = GnmiClient(engines.dut.ip, GnmiConsts.GNMI_DEFAULT_PORT,
                            devices.dut.default_username, devices.dut.default_password,
                            verify_tools_installed=True)

    with allure.step("Run gnmi session (once) and verify events were streamed"):
        for event_id in [high_usage_event_id, normal_usage_event_id]:
            out, err, _ = client.gnmic_subscribe_system_event(event_id=event_id, skip_cert_verify=True,
                                                              keep_session_alive=False)
            # WA: Extract valid JSON from output (may contain extra text after termination)
            # bug: https://redmine.mellanox.com/issues/4782619
            if redmine_helpers.is_bug_active(4782619):
                json_start = out.find('{')
                json_end = out.rfind('}') + 1
                json_object = json.loads(out[json_start:json_end])
            else:
                json_object = json.loads(out)
            with allure.independent_step("Check which docker caused the event"):
                event_docker = get_gnmic_attribute(json_object, "resource")
                assert event_docker == docker, f"Docker caused event is {event_docker} and not {docker}"
            with allure.independent_step("Check event's message"):
                msg = get_gnmic_attribute(json_object, "text")
                if event_id == high_usage_event_id:
                    pattern = fr"{resource} usage (\d+(\.\d+)?)% is above expected threshold (\d+(\.\d+)?)%"
                    assert re.search(pattern, msg.lower()), f"Event's message is not as expected. Got: '{msg}'"
                else:
                    pattern = f"{resource} usage is back to normal"
                    assert msg.lower() == pattern, f"Event's message is not as expected. Got: '{msg}'"


def get_gnmic_attribute(gnmic_out: dict, attribute: str) -> str:
    res = ""
    for update in gnmic_out["updates"]:
        if f"state/{attribute}" in update["values"].keys():
            res = update["values"][f"state/{attribute}"]
    return res


def verify_docker_restart(engines: EnginesT, resource: str, docker: str, start: datetime, start_time_utc: datetime) -> None:
    """
        Helper function to check:
        1. message in log indicating docker was restarted.
        2. docker's up-time was after the simulation, meaning docker was restarted.
    """
    system = System()
    with allure.step("Look for a message in log describing that docker was restarted"):
        log_message_list = [HIGH_USAGE_EVENT_MSG_PATTERN.format(docker),
                            NORMAL_USAGE_EVENT_MSG_PATTERN.format(docker)]
        system.log.verify_expected_logs_by_time(log_message_list, engines.dut, only_latest_log=False,
                                                start_time=start)
    with allure.step(f"Verify docker started after {start_time_utc}"):
        with allure.step("Get docker up-time"):
            docker_started_at = engines.dut.run_cmd(r"docker inspect -f '{{.State.StartedAt}}' %s" % docker).strip().splitlines()[-1]
            allure.attach("Docker StartedAt", docker_started_at, log=False)
            up_time = _parse_utc_timestamp(docker_started_at)

        assert start_time_utc <= up_time, (f"Unexpectedly, {docker} did not restart after high {resource} usage simulation")


def verify_tech_support_generation_time(engines: EnginesT, start_time: datetime, should_generate_tech_support: bool = False) -> None:
    """
        Helper function to verify that a new tech-support file was generated
    """
    system = System()
    skip_verify_tech_support_birth_time = False

    def _verify_tech_support_logs(logs_to_find: list[str]) -> None:
        system.log.verify_expected_logs_by_time(
            logs_to_find,
            engines.dut,
            only_latest_log=False,
            start_time=start_time,
        )

    with allure.step("Look for message in log describing tech-support file was generated/was not generated"):
        if should_generate_tech_support:
            log_message_list = [GENERATING_TECH_SUPPORT_MSG_PATTERN, TECH_SUPPORT_GENERATED_MSG_PATTERN]
        else:
            log_message_list = [NO_TECH_SUPPORT_GENERATED_MSG_PATTERN]

        try:
            _verify_tech_support_logs(log_message_list)
        except AssertionError as e:
            if os.getenv(InfraConst.ENV_SESSION_ID) or not should_generate_tech_support:
                raise e  # This a CI run, tech-support should be generated
            # This is a local run, tech-support might not be generated because it was already generated earlier.
            logger.info("check that tech-support was already generated.")
            _verify_tech_support_logs([NO_TECH_SUPPORT_GENERATED_MSG_PATTERN])
            # there is no need to verify the tech-support birth time, because it's already generated.
            skip_verify_tech_support_birth_time = True

    with allure.step("Verify tech-support birth time"):
        with allure.step("Run 'nv show system tech-support' and get the last one generated"):
            output_list = list(Tools.OutputParsingTool.parse_show_files_to_dict(
                system.techsupport.files.show()).get_returned_value().values())
            latest = output_list[0]

        if skip_verify_tech_support_birth_time:
            logger.info("Skipping tech-support birth time verification, because it's a local run and tech-support was already generated.")
        else:
            system.techsupport.check_techsupport_file_age(engines.dut, system, latest)


def init_monitored_dockers_list(devices: DevicesT, has_nmx: bool, output_dict: dict) -> None:
    """
        Helper function to make sure all relevant docker are in monitored dockers list: [nmx-c, nmx-t, gnmi-server]
    """
    monitored_dockers = output_dict["monitored-list"]
    if len(monitored_dockers) == 0:
        System().gnmi_server.enable_gnmi_server()
    while (len(monitored_dockers) == 0) or (has_nmx and monitored_dockers == "gnmi-server"):
        with allure.step("Wait 1 min to the next poll from db and check if list is updated"):
            time.sleep(constants.MINUTE)
            output_dict = OutputParsingTool.parse_json_str_to_dictionary(Fae().system.resource_limit.show()).verify_result()
            monitored_dockers = output_dict["monitored-list"]

    assert output_dict["state"] == NvosConst.ENABLED, "CPU/memory monitoring feature is unexpectedly disabled"
    logger.info(f"Dockers to monitor: {monitored_dockers}")
