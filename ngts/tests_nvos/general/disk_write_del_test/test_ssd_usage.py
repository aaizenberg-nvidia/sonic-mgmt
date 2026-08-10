import os
import time
import pytest
import allure
import logging
import json
import re
from ngts.nvos_constants.constants_nvos import LogComponentsConsts
from ngts.nvos_tools.system.System import System


cumulus_owner = "hiept"


logger = logging.getLogger()

# =========================
# Constants
# =========================
HOURS_IN_10_YEAR = 87600
TB_IN_MB = 1048576  # 1TB=1024GB=1024*1024 MB
MEASUREMENT_DURATION = 3600  # 1 hour

# NVOS-specific constants (for stateful flow)
SSD_DIR = "/home/admin/ssd_check"
WRITTEN_MB_PATH = SSD_DIR + "/last_written_value.txt"


# =========================
# Shared utility functions
# =========================


def strip_ansi(text: str) -> str:
    """Remove ANSI escape codes from text."""
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text or "")


def get_ssd_device_name(dut):
    """Detect SSD block device name with multiple fallback methods."""
    try:
        out = dut.run_cmd("sudo lsblk -o NAME,TYPE -p | grep ' disk' | awk '{print $1}' | head -n 1", validate=False)
        dev = strip_ansi(out).strip().splitlines()[0] if out else ""
        if dev:
            return dev
        out = dut.run_cmd("lsblk -ndo NAME,TYPE -p | awk '$2==\"disk\" {print $1}' | head -n 1", validate=False)
        dev = strip_ansi(out).strip().splitlines()[0] if out else ""
        if dev:
            return dev
        out = dut.run_cmd("ls /dev/nvme*n1 /dev/sd[a-z] 2>/dev/null | head -n 1", validate=False)
        return strip_ansi(out).strip().splitlines()[0] if out else ""
    except Exception:
        return ""


def get_ssd_device_id(dut, dev_name):
    """Get SSD device ID via smartctl."""
    try:
        dev_filter = "'Model Number'" if "nvme" in dev_name else "'Device Model'"
        out = dut.run_cmd(f"sudo smartctl -a {dev_name} | grep {dev_filter} | awk '{{print $NF}}'", validate=False)
        clean = strip_ansi(out)
        lines = [ln.strip() for ln in clean.splitlines() if ln.strip()]
        if lines:
            return lines[-1]
        # Fallback: smartctl -i
        out = dut.run_cmd(f"sudo smartctl -i {dev_name} | grep {dev_filter} | awk '{{print $NF}}'", validate=False)
        clean = strip_ansi(out)
        lines = [ln.strip() for ln in clean.splitlines() if ln.strip()]
        return lines[-1] if lines else ""
    except Exception:
        return ""


def ensure_sysstat(dut):
    """Ensure sysstat/iostat is installed, install if missing."""
    try:
        rc_str = dut.run_cmd("dpkg -s sysstat >/dev/null 2>&1; echo $?", validate=False)
        rc = int(rc_str.strip().splitlines()[-1]) if rc_str else 1
    except Exception:
        rc = 1
    if rc == 0:
        return True
    dut.run_cmd("sudo apt-get update >/dev/null 2>&1", validate=False)
    dut.run_cmd("sudo apt-get install sysstat -y >/dev/null 2>&1", validate=False)
    is_iostat = (
        "0" == dut.run_cmd("command -v iostat >/dev/null 2>&1; echo $?", validate=False).strip().splitlines()[-1]
    )
    return bool(is_iostat)


def get_current_written_mb(dut, device_name):
    """Get current written MB from iostat."""
    if "nvme" in device_name:
        out = dut.run_cmd("iostat -m | grep nvme | awk '{print $7}' | head -n 1", validate=False)
    else:
        out = dut.run_cmd("iostat -m | grep sda | awk '{print $7}' | head -n 1", validate=False)
    clean = strip_ansi(out)
    num_line = clean.strip().splitlines()[0] if clean else "0"
    return int(float(num_line))


def calculate_mb_per_hour(start_written, end_written, elapsed_sec):
    """Calculate MB written per hour."""
    total_written = end_written - start_written
    return (total_written / elapsed_sec) * 3600


def get_threshold(dut, device_id):
    """
    Fetch the SSD TBW threshold from json file.
    Falls back to permissive default if device not found.
    """
    local_path = os.path.join(os.path.dirname(__file__), "ssd_threshold.json")
    if os.path.exists(local_path):
        with open(local_path, "r") as f:
            th_dict = json.load(f)
    else:
        candidate_paths = [
            "ssd_threshold.json",
            "/home/cumulus/ssd_threshold.json",
            "/home/admin/ssd_threshold.json",
        ]
        path_found = None
        for p in candidate_paths:
            try:
                ls_out = dut.run_cmd(f"test -f {p} && echo found", validate=False)
            except Exception:
                ls_out = ""
            if "found" in (ls_out or ""):
                path_found = p
                break
        if not path_found:
            logger.warning("ssd_threshold.json not found locally or on DUT; using permissive default")
            return float("inf")
        content = dut.run_cmd(f"cat {path_found}", validate=False)
        th_dict = json.loads(content)

    for json_device_id, ssd_th_value in th_dict.items():
        pattern = r".*{}$".format(re.escape(json_device_id))
        if re.match(pattern, device_id):
            return float(ssd_th_value)
    # If not found, fall back to a permissive threshold
    logger.warning("Device %s not found in SSD threshold list; using permissive default", device_id or "<empty>")
    return float("inf")


def test_calculate_ssd_writing(last, current, sec, expected):
    result = calculate_mb_per_hour(last, current, sec)
    assert result == expected, f"Expected {expected}, got {result}"


def check_calculate():
    matrix = [
        [0, 100, 3600, 100],
        [500, 1500, 7200, 500],
        [1000, 2000, 3600, 1000],
    ]
    with allure.step("Calculate SSD writing for each parameter set"):
        for start, end, duration, workload in matrix:
            with allure.step(f"Params → start={start}, end={end}, duration={duration}, workload={workload}"):
                test_calculate_ssd_writing(start, end, duration, workload)


def is_nvos(dut):
    """
    Detect if the system is running NVOS (not Cumulus Linux).
    """
    out = dut.run_cmd("nv show system version", validate=False)
    if not out:
        return False
    if "Error" in out or "not found" in out.lower():
        return False
    out_lower = out.lower()
    if "cumulus" in out_lower:
        return False
    if "nvos" in out_lower or "sonic" in out_lower:
        return True
    return False


def build_test_summary(
    os_type, dev_name, dev_id, duration, start_mb, end_mb, mb_per_hour, estimate_10y, threshold_tb, threshold_mb
):
    """Build JSON summary for allure attachment."""
    threshold_usage = (estimate_10y / threshold_mb) * 100 if threshold_mb and threshold_mb != float("inf") else 0
    is_pass = estimate_10y <= threshold_mb
    return {
        "device": {
            "os_type": os_type,
            "name": dev_name,
            "SSD model identifier": dev_id,
        },
        "measurement": {
            "duration_seconds": duration,
            "Total written at start (MB)": start_mb,
            "Total written at end (MB)": end_mb,
            "Total written during test (MB)": end_mb - start_mb,
        },
        "calculation": {
            "Write rate (MB/h)": round(mb_per_hour, 2),
            "Estimated 10-year writes (MB)": round(estimate_10y, 2),
            "Estimated 10-year writes (TB)": round(estimate_10y / TB_IN_MB, 4),
        },
        "threshold": {
            "TBW threshold (TB)": threshold_tb if threshold_tb != float("inf") else "unlimited",
            "TBW threshold (MB)": threshold_mb if threshold_mb != float("inf") else "unlimited",
        },
        "result": {
            "Threshold usage percentage": round(threshold_usage, 2),
            "Within threshold": is_pass,
            "verdict": "PASS" if is_pass else "FAIL",
        },
    }


# =========================
# NVOS-specific functions (stateful flow)
# =========================


class MyLogger:
    def __init__(self, str_init=""):
        self.str = str_init

    def set_str(self, new_str):
        self.str = new_str


def set_log_level_default(dut_engine):
    """Set log level of DUT to default to avoid extensive log writing."""
    system = System(None)
    for component_name in LogComponentsConsts.COMPONENTS_LIST:
        log_level = "info" if component_name in ("symmetry-manager", "nvue") else "notice"
        system.log.component.component_id[component_name].level.set(log_level, apply=True).verify_result()


def update_current_value(dut_engine, current_val):
    """Update the file on DUT with current MB written value."""
    dut_engine.run_cmd(f"mkdir -p {SSD_DIR}", validate=True)
    dut_engine.run_cmd(f"echo {current_val} > {WRITTEN_MB_PATH}", validate=True)


def get_sec_from_last_modification(dut_engine):
    """Get seconds since last modification of the written MB file."""
    return int(
        dut_engine.run_cmd(
            f'stat -c "%Y" {WRITTEN_MB_PATH} | xargs -I{{}} date +%s --date="now - {{}} seconds"',
            validate=True,
        )
    )


def get_last_written_value(dut_engine):
    """Return the last written MB value from DUT, if exists."""
    logger.info("Checking if a file with the latest value exists on DUT")
    dut_engine.run_cmd(f"ls {WRITTEN_MB_PATH}")
    raw = dut_engine.run_cmd("echo $?", validate=True)
    rc = int(raw.strip().splitlines()[-1])
    if rc:
        raise RuntimeWarning("The current value file does not exist, Writing the current value and exiting")
    written_mb_value = dut_engine.run_cmd(f"cat {WRITTEN_MB_PATH}", validate=True)
    if written_mb_value == "" or not written_mb_value.strip().isdigit():
        raise ValueError("Written_mb_value is empty, from get_last_written_value function")
    logger.info("File exists, the last value written to the file is %s mb", written_mb_value)
    return int(written_mb_value)


def validate_last_written_value(dut_engine, min_gap, sec_from_last_modification):
    """Check if switch was rebooted or gap between tests is too low."""
    min_gap_in_sec = min_gap * 24 * 3600
    sec_from_uptime = int(float(dut_engine.run_cmd("sudo cat /proc/uptime | awk '{print $1}'")))
    if sec_from_uptime < sec_from_last_modification:
        raise RuntimeWarning("System has rebooted since last time ssd sampled, value is not valid")
    if sec_from_last_modification < min_gap_in_sec:
        remaining_hours = (min_gap_in_sec - sec_from_last_modification) / 3600
        raise RuntimeWarning(f"Not enough time has passed. Please try again in {remaining_hours} hours")


def to_logger(ssd_threshold_mb, estimate_10y, mb_per_hour, result_str, mylogger):
    """Format results for NVOS text attachment."""
    output1 = f"ssd_threshold_mb = {ssd_threshold_mb}"
    output2 = f"estimate_mb_write_for_10_years = {round(estimate_10y, 2)}"
    output3 = f"mb_written_per_hour = {round(mb_per_hour, 2)}"
    percent = (estimate_10y / ssd_threshold_mb) * 100 if ssd_threshold_mb else 0
    output4 = f"using rate: {round(mb_per_hour)} for 10 years it use: {round(percent, 2)}% of ssd_threshold_mb"
    for out in [output1, output2, output3, output4]:
        logger.info(out)
    mylogger.set_str(f"{result_str}\n{output1}\n{output2}\n{output3}\n{output4}")


def do_ssd_endurance_test(dut_engine, min_gap, my_logger, release_mode=False):
    """
    NVOS SSD endurance check with stateful tracking.
    """
    try:
        skip_writing = False
        with allure.step("Detect SSD device and fetch device ID"):
            ssd_device_name = get_ssd_device_name(dut_engine)
            assert ssd_device_name, "No SSD block device detected"
            ssd_device_id = get_ssd_device_id(dut_engine, ssd_device_name)
            assert ssd_device_id, "Unable to determine SSD device ID"

        with allure.step("Fetch SSD TBW threshold for device"):
            ssd_threshold_tb = get_threshold(dut_engine, ssd_device_id)
            ssd_threshold_mb = ssd_threshold_tb * TB_IN_MB

        with allure.step("Set DUT log level to default"):
            set_log_level_default(dut_engine)

        with allure.step("Read current and last written MB values from DUT"):
            logger.info("Calculate the current 'written mb' value on the DUT")
            current_written_value = get_current_written_mb(dut_engine, ssd_device_name)
            last_written_value = get_last_written_value(dut_engine)
            logger.debug("last_written_value: %d, current_written_value: %d", last_written_value, current_written_value)

        with allure.step("Check time since last modification and validate test can run"):
            sec_from_last_modification = get_sec_from_last_modification(dut_engine)
            raw = dut_engine.run_cmd("echo $?", validate=True)
            rc = int(raw.strip().splitlines()[-1])
            if rc:
                raise RuntimeWarning("An error occurred in get_sec_from_last_modification()")
            logger.info("Checking if current value is valid: reboot check and %d days gap", min_gap)
            validate_last_written_value(dut_engine, min_gap, sec_from_last_modification)

        with allure.step("Calculate SSD writing tempo and estimate 10 year write"):
            logger.info("Calculating the SSD writing tempo")
            mb_written_per_hour = calculate_mb_per_hour(
                last_written_value, current_written_value, sec_from_last_modification
            )
            estimate_10y = mb_written_per_hour * HOURS_IN_10_YEAR
            logger.debug("est_10y: %d, mb/h: %d, thresh: %d", estimate_10y, mb_written_per_hour, ssd_threshold_mb)

            # Attach JSON summary
            summary = build_test_summary(
                "NVOS",
                ssd_device_name,
                ssd_device_id,
                sec_from_last_modification,
                last_written_value,
                current_written_value,
                mb_written_per_hour,
                estimate_10y,
                ssd_threshold_tb,
                ssd_threshold_mb,
            )
            allure.attach(
                json.dumps(summary, indent=2),
                name="SSD Endurance Test Summary",
                attachment_type=allure.attachment_type.JSON,
            )

        with allure.step("Assert SSD write tempo is within threshold"):
            if estimate_10y > ssd_threshold_mb:
                logger.info("Test failed")
                to_logger(ssd_threshold_mb, estimate_10y, mb_written_per_hour, "Test failed", my_logger)
                raise AssertionError("FAILED: The writing tempo to ssd has exceeded the allowed threshold!")
            logger.info("Test passed!")
            to_logger(ssd_threshold_mb, estimate_10y, mb_written_per_hour, "Test passed", my_logger)

    except ValueError as err:
        skip_writing = True
        raise ValueError from err

    except AssertionError as err:
        raise AssertionError from err

    except RuntimeWarning as skip_phrase:
        if re.match("Not enough time", str(skip_phrase)):
            skip_writing = True
        if not release_mode:
            pytest.skip(str(skip_phrase))
        raise RuntimeWarning from skip_phrase

    finally:
        with allure.step("Update current value on DUT (if needed)"):
            if not skip_writing:
                logger.info("Updating the current value to %s", current_written_value)
                update_current_value(dut_engine, current_written_value)
            else:
                logger.info("Update of current value is not needed")


# =========================
# Main test function
# =========================


@pytest.mark.cumulus
@allure.title("SSD Endurance Test (NVOS + Cumulus)")
def test_ssd_endurance(engines, str_gap_time_between_tests="30sec"):
    dut = engines.dut
    pytest.skip_coredump_check = True

    if is_nvos(dut):
        # =========================
        # NVOS flow (stateful, with retry logic)
        # =========================
        print("Running NVOS flow")
        min_gap_dict = {
            "30sec": 0.5 / (24 * 60),
            "60sec": 1 / (24 * 60),
            "10m": 10 / (24 * 60),
            "15m": 15 / (24 * 60),
            "1h": 1 / 24,
            "2h": 2 / 24,
            "12h": 0.5,
            "24h": 1,
        }
        min_gap = min_gap_dict[str_gap_time_between_tests]
        my_logger_results = MyLogger()
        check_calculate()

        with allure.step(f"Run SSD endurance on NVOS, min_gap={min_gap}"):
            try:
                dut.run_cmd(f"rm -f {WRITTEN_MB_PATH}", validate=True)
                do_ssd_endurance_test(dut, min_gap, my_logger_results, True)
            except RuntimeWarning as err:
                with allure.step("Caught RuntimeWarning, going to sleep"):
                    allure.attach(str(err), name="RuntimeWarning", attachment_type=allure.attachment_type.TEXT)
                    dut.disconnect()
                    sleep_time = MEASUREMENT_DURATION + (300 * min_gap)
                    logger.info(f"Sleeping for {min_gap * 24} hours ({min_gap * 24 * 60} minutes)")
                    time.sleep(min_gap * sleep_time)
                    do_ssd_endurance_test(dut, min_gap, my_logger_results)
            finally:
                with allure.step("Reset log level to default"):
                    set_log_level_default(dut)
                logger.info(my_logger_results.str)
                allure.attach(my_logger_results.str, name="Test result", attachment_type=allure.attachment_type.TEXT)
    else:
        # =========================
        # CL flow (stateless, simple)
        # =========================
        print("Running CL flow")
        check_calculate()

        with allure.step("Ensure sysstat/iostat is installed"):
            assert ensure_sysstat(dut), "sysstat (iostat) missing and install failed"

        with allure.step("Detect SSD block device"):
            dev_name = get_ssd_device_name(dut)
            assert dev_name, "No SSD block device detected on DUT"

        with allure.step("Fetch SSD device ID via smartctl"):
            dev_id = get_ssd_device_id(dut, dev_name)
            assert dev_id, "Unable to determine SSD device ID via smartctl"

        with allure.step("Load SSD TBW threshold"):
            ssd_threshold_tb = get_threshold(dut, dev_id)
            ssd_threshold_mb = ssd_threshold_tb * TB_IN_MB

        with allure.step("Capture current written MB"):
            start_written = get_current_written_mb(dut, dev_name)

        with allure.step(f"Sleep for {MEASUREMENT_DURATION} seconds to measure delta"):
            time.sleep(MEASUREMENT_DURATION)

        with allure.step("Capture written MB after wait"):
            end_written = get_current_written_mb(dut, dev_name)

        with allure.step("Compute write rate and 10-year estimate"):
            mb_per_hour = calculate_mb_per_hour(start_written, end_written, MEASUREMENT_DURATION)
            estimate_10y = mb_per_hour * HOURS_IN_10_YEAR
            is_pass = estimate_10y <= ssd_threshold_mb

            # Attach JSON summary
            summary = build_test_summary(
                "CL",
                dev_name,
                dev_id,
                MEASUREMENT_DURATION,
                start_written,
                end_written,
                mb_per_hour,
                estimate_10y,
                ssd_threshold_tb,
                ssd_threshold_mb,
            )
            allure.attach(
                json.dumps(summary, indent=2),
                name="SSD Endurance Test Summary",
                attachment_type=allure.attachment_type.JSON,
            )

            logger.info(
                "CL SSD: %s id: %s | start: %s MB end: %s MB | rate: %.2f MB/h | 10y: %.2f MB | thresh: %.2f MB",
                dev_name,
                dev_id,
                start_written,
                end_written,
                mb_per_hour,
                estimate_10y,
                ssd_threshold_mb,
            )
            assert is_pass, f"Estimated 10-year writes {estimate_10y:.2f} MB exceeds threshold {ssd_threshold_mb} MB"
