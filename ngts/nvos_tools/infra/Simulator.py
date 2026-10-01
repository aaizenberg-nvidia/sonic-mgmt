import logging
import allure
import random
import time
import re
from contextlib import contextmanager
from typing import Dict, Iterator, Tuple, Union

from devts.infra.tools.connection_tools.linux_ssh_engine import LinuxSshEngine
from ngts.ngts_types import DevicesT, EnginesT
from ngts.nvos_constants.constants_nvos import HealthConsts, SystemConsts, FansConsts
from ngts.nvos_tools.infra.FilesTool import FilesTool
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.system.System import System

logger = logging.getLogger()


class HWSimulator:

    @staticmethod
    def _read_symlink_target(engine: LinuxSshEngine, file_path: str) -> str:
        """Read and return the symlink target of a hw-management sysfs file."""
        target = engine.run_cmd(f'readlink {file_path}').strip()
        logger.info(f"Saved symlink target: {file_path} -> {target}")
        return target

    @staticmethod
    def _inject_value(engine: LinuxSshEngine, file_path: str, value: Union[int, str]) -> None:
        """Replace a sysfs symlink with a regular file containing the given value."""
        engine.run_cmd(f"sudo sh -c 'rm -f {file_path} && echo {value} > {file_path}'")

    @staticmethod
    def _restore_symlink(engine: LinuxSshEngine, file_path: str, symlink_target: str) -> None:
        """Restore a sysfs file's original symlink."""
        engine.run_cmd(f"sudo sh -c 'rm -f {file_path} && ln -s {symlink_target} {file_path}'")

    @staticmethod
    def fan_fw_file_value_get(engine: LinuxSshEngine, thermal_directory: str, fan_id: int) -> str | None:
        """Read the current value of fan{N}_speed_get for the given fan id."""
        if not fan_id:
            logger.info("No fan specified, skip")
            return None
        with allure.step(f"Read fw file fan{fan_id}_speed_get"):
            file = f"fan{fan_id}_speed_get"
            return FilesTool.fw_file_read(engine, file, thermal_directory)

    @staticmethod
    def simulate_health_issue_change_fw_file(engine: LinuxSshEngine, new_val: Union[int, str],
                                             file: str, thermal_directory: str) -> str:
        """Inject a fake value into a hw-management sysfs file.

        Returns the original symlink target for later restoration.
        """
        file_path = f"{thermal_directory}/{file}"
        symlink_target = HWSimulator._read_symlink_target(engine, file_path)
        HWSimulator._inject_value(engine, file_path, new_val)
        return symlink_target

    @staticmethod
    def restore_fw_file(engine: LinuxSshEngine, file: str, thermal_directory: str, symlink_target: str) -> None:
        """Restore a hw-management sysfs file's original symlink."""
        file_path = f"{thermal_directory}/{file}"
        HWSimulator._restore_symlink(engine, file_path, symlink_target)

    @staticmethod
    def simulate_fan_fault(engine: LinuxSshEngine, thermal_directory: str, fan_id: int) -> str | None:
        """Simulate fan fault. Returns symlink target for restoration, or None if fan_id is falsy."""
        if not fan_id:
            logger.info("No fan specified, skip")
            return None
        with allure.step(f"Simulate fan {fan_id} fault"):
            logger.info(f"Simulate fan {fan_id} fault")
            file = f"fan{fan_id}_fault"
            return HWSimulator.simulate_health_issue_change_fw_file(engine, 1, file, thermal_directory)

    @staticmethod
    def clear_fan_fault(engine: LinuxSshEngine, thermal_directory: str, fan_id: int, symlink_target: str) -> None:
        """Restore fan fault file's original symlink."""
        if not fan_id:
            logger.info("No fan specified, skip")
            return
        with allure.step(f"Clear fan {fan_id} fault"):
            logger.info(f"Clear fan {fan_id} fault")
            file = f"fan{fan_id}_fault"
            HWSimulator.restore_fw_file(engine, file, thermal_directory, symlink_target)

    @staticmethod
    def simulate_fan_speed_fault(engine: LinuxSshEngine, thermal_directory: str, fan_id: int,
                                 new_val: Union[int, str]) -> str | None:
        """Simulate fan speed fault. Returns symlink target for restoration, or None if fan_id is falsy."""
        if not fan_id:
            logger.info("No fan specified, skip")
            return None
        with allure.step(f"Simulate fan {fan_id} speed fault"):
            logger.info(f"Simulate fan {fan_id} speed fault")
            file = f"fan{fan_id}_speed_get"
            return HWSimulator.simulate_health_issue_change_fw_file(engine, new_val, file, thermal_directory)

    @staticmethod
    def simulate_fix_fan_speed_fault(engine: LinuxSshEngine, thermal_directory: str, fan_id: int,
                                     symlink_target: str) -> None:
        """Restore fan speed file's original symlink."""
        if not fan_id:
            logger.info("No fan specified, skip")
            return
        with allure.step(f"Simulate fix fan {fan_id} speed fault"):
            logger.info(f"Simulate fix fan {fan_id} speed fault")
            file = f"fan{fan_id}_speed_get"
            HWSimulator.restore_fw_file(engine, file, thermal_directory, symlink_target)

    @staticmethod
    def simulate_psu_fault(engine: LinuxSshEngine, thermal_directory: str, psu_id: str) -> str | None:
        """Simulate PSU fault. Returns symlink target for restoration, or None if psu_id is falsy."""
        if not psu_id:
            logger.info("No psu specified, skip")
            return None
        with allure.step(f"Simulate psu {psu_id} fault"):
            logger.info(f"Simulate psu {psu_id} fault")
            file = f"psu{psu_id}_status"
            return HWSimulator.simulate_health_issue_change_fw_file(engine, 0, file, thermal_directory)

    @staticmethod
    def simulate_fix_psu_fault(engine: LinuxSshEngine, thermal_directory: str, psu_id: str, symlink_target: str) -> None:
        """Restore PSU status file's original symlink."""
        if not psu_id:
            logger.info("No psu specified, skip")
            return
        with allure.step(f"Simulate fix psu {psu_id} fault"):
            logger.info(f"Simulate fix psu {psu_id} fault")
            file = f"psu{psu_id}_status"
            HWSimulator.restore_fw_file(engine, file, thermal_directory, symlink_target)

    @staticmethod
    def simulate_psu_temp_fault(engine: LinuxSshEngine, thermal_directory: str, psu_id: str) -> str | None:
        """Simulate PSU temperature fault. Returns symlink target for restoration.

        Reads the PSU's actual temp1_max threshold and injects a value above it,
        so the fault is detected regardless of PSU model.
        """
        if not psu_id:
            logger.info("No psu specified, skip")
            return None
        with allure.step(f"Simulate psu {psu_id} temperature fault"):
            logger.info(f"Simulate psu {psu_id} temperature fault")
            max_temp_file = f"psu{psu_id}_temp1_max"
            max_temp_path = f"{thermal_directory}/{max_temp_file}"
            max_temp_str = engine.run_cmd(f'cat {max_temp_path}').strip()
            try:
                max_temp = int(max_temp_str)
            except ValueError:
                logger.warning(f"Could not read max temp from {max_temp_path}, using default 70000")
                max_temp = 70000
            fault_temp = max_temp + 10000
            logger.info(f"PSU {psu_id} max temp: {max_temp}, injecting fault temp: {fault_temp}")
            file = f"psu{psu_id}_temp1"
            return HWSimulator.simulate_health_issue_change_fw_file(engine, fault_temp, file, thermal_directory)

    @staticmethod
    def simulate_fix_psu_temp_fault(engine: LinuxSshEngine, thermal_directory: str, psu_id: str,
                                    symlink_target: str) -> None:
        """Restore PSU temperature file's original symlink."""
        if not psu_id:
            logger.info("No psu specified, skip")
            return
        with allure.step(f"Simulate fix psu {psu_id} temperature fault"):
            logger.info(f"Simulate fix psu {psu_id} temperature fault")
            file = f"psu{psu_id}_temp1"
            HWSimulator.restore_fw_file(engine, file, thermal_directory, symlink_target)

    @staticmethod
    def simulate_and_fix_psu_component_error(devices: DevicesT, engines: EnginesT, show_output: Dict) -> str:
        """Inject and clear a temperature fault on a randomly chosen healthy PSU.

        Returns the canonical PSU instance name (e.g. "PSU1") so the caller can
        validate counters/timestamps on the exact instance that was triggered.
        """
        thermal_directory = devices.dut.fan_direction_dir
        psu_id_list = [
            match.group(1) for key in show_output
            if (match := re.search(r"PSU(\d+).*", key)) and
            show_output[key][SystemConsts.STATE] == FansConsts.STATE_OK
        ]
        psu_id = random.choice(psu_id_list)
        psu_instance_name = f"PSU{psu_id}"

        with allure.step(f"Simulate PSU temperature fault for chosen PSU:{psu_id}"):
            symlink_target = HWSimulator.simulate_psu_temp_fault(engines.dut, thermal_directory, psu_id)
            time.sleep(10)
        with allure.step(f"Simulate PSU temperature fix for chosen PSU:{psu_id}"):
            HWSimulator.simulate_fix_psu_temp_fault(engines.dut, thermal_directory, psu_id, symlink_target)
            time.sleep(10)

        return psu_instance_name

    @staticmethod
    def _pick_random_fan(devices: DevicesT) -> Tuple[int, str]:
        """Pick a random fan and return (fan_id, fan_instance_name).

        fan_list is 0-indexed (Python list); sysfs fan numbering is 1-based (fan{N}_fault).
        """
        fan_index = random.randrange(len(devices.dut.fan_list))
        return fan_index + 1, devices.dut.fan_list[fan_index]

    @staticmethod
    def _inject_and_clear_fan_fault(engines: EnginesT, thermal_directory: str, fan_id: int,
                                    inject_step: str, clear_step: str) -> None:
        """Inject a hard fan fault, wait, then restore it.

        Uses the hard-fault file (fan{N}_fault) instead of the speed file (fan{N}_speed_get).
        The speed path is gated by thermalctld.set_under_speed's 15s grace tied to
        last_target_speed changes, which racily suppresses real faults when hw-management-tc
        cycles pwm1 during the injection window. The fault path is independent of that gate.
        """
        with allure.step(inject_step):
            symlink_target = HWSimulator.simulate_fan_fault(engines.dut, thermal_directory, fan_id)
            time.sleep(10)
        with allure.step(clear_step):
            HWSimulator.clear_fan_fault(engines.dut, thermal_directory, fan_id, symlink_target)
            time.sleep(10)

    @staticmethod
    def simulate_and_fix_fan_component_error(devices: DevicesT, engines: EnginesT) -> str:
        """Inject and clear a hard fault on a randomly chosen fan.

        Returns the fan health instance name (e.g. "FAN1") so the caller can
        validate counters/timestamps on the exact instance that was triggered.
        """
        fan_id, fan_instance_name = HWSimulator._pick_random_fan(devices)
        HWSimulator._inject_and_clear_fan_fault(
            engines, devices.dut.fan_direction_dir, fan_id,
            inject_step=f"Simulate fan error on {fan_instance_name} (fan_id={fan_id})",
            clear_step=f"Fix fan error on {fan_instance_name} (fan_id={fan_id})",
        )
        return fan_instance_name

    @staticmethod
    def create_health_component_error_fan(devices, engines) -> str | None:
        """Inject and clear a hard fault on a randomly chosen fan, or skip if no fan service.

        Returns the fan health instance name, or None if the platform has no fans.
        """
        with allure.step("Check whether the setup has fans"):
            if FansConsts.HW_MANAGEMENT_TC_SERVICE not in devices.dut.available_services:
                logger.info("No fan available, skip")
                return None

        with allure.step("get random fan id from fans folder"):
            fan_id, fan_instance_name = HWSimulator._pick_random_fan(devices)
            logger.info(f"Selected fan_id={fan_id}, health instance name: {fan_instance_name}")

        HWSimulator._inject_and_clear_fan_fault(
            engines, devices.dut.fan_direction_dir, fan_id,
            inject_step="Simulate fan error",
            clear_step="Fix fan error",
        )
        return fan_instance_name

    @staticmethod
    def find_sensor_dir(engine: LinuxSshEngine, base_path: str, sensor_name: str) -> str:
        """Find the filesystem directory for a sensor by its CLI display name.

        The filesystem uses '+' separators and includes extra tokens like '+Vol',
        '+Volt', '+VinDC' that don't appear in the CLI name. We strip those tokens
        and compare only lowercase alphanumeric characters to find the match.
        """
        output = engine.run_cmd(f'find {base_path} -maxdepth 3 -name input')
        dirs = [line.strip().rsplit('/input', 1)[0] for line in output.splitlines() if line.strip()]

        def normalize(name: str) -> str:
            for token in ('+VinDC', '+Volt', '+Vol'):
                name = name.replace(token, '')
            name = re.sub(r'\+Temp\+\d+', '+Temp', name)
            return re.sub(r'[^a-z0-9]', '', name.lower())

        target = normalize(sensor_name)
        for d in dirs:
            dir_name = d.rstrip('/').split('/')[-1]
            if normalize(dir_name) == target:
                return d

        raise FileNotFoundError(
            f"No directory found for sensor '{sensor_name}' under {base_path}. "
            f"Available: {[d.split('/')[-1] for d in dirs]}"
        )

    @staticmethod
    @contextmanager
    def simulate_sensor(engine: LinuxSshEngine, input_path: str, fake_value: Union[int, str],
                        stabilize_delay: float) -> Iterator[None]:
        """Context manager: inject a fake sensor value, yield, then restore the original symlink."""
        original_target = HWSimulator._read_symlink_target(engine, input_path)
        try:
            with allure.step(f"Inject fake value '{fake_value}' into {input_path}"):
                HWSimulator._inject_value(engine, input_path, fake_value)
                time.sleep(stabilize_delay)
            yield
        finally:
            with allure.step(f"Restore original symlink for {input_path}"):
                HWSimulator._restore_symlink(engine, input_path, original_target)
                time.sleep(stabilize_delay)

    @staticmethod
    def reset_health_service(engine: LinuxSshEngine) -> None:
        """Restart the health monitoring service on the DUT to clear accumulated issues history.

        The unit name is OS-specific: Cumulus uses 'cumulus-sdk-health-mon', NVOS uses 'health-statsd'.
        """
        service = (HealthConsts.CUMULUS_HEALTH_SERVICE if TestToolkit.devices.dut.is_eth()
                   else HealthConsts.NVOS_HEALTH_SERVICE)
        with allure.step(f"Restart {service} to clear issues history"):
            logger.info(f"Restarting {service} to clear accumulated issues after test")
            engine.run_cmd(f"sudo systemctl restart {service}.service")
            time.sleep(10)
            System().health.wait_until_health_status_change_after_reboot(HealthConsts.OK)
