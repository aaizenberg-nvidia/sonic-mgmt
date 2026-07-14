import pytest
import logging
import time
from datetime import datetime, timezone
from typing import Dict

from ngts.nvos_constants.constants_nvos import EventConsts
from ngts.nvos_tools.Devices import IbDevice
from ngts.nvos_tools.infra.McraTool import McraTool
from ngts.nvos_tools.infra.NvCommand import NvCommand
from ngts.nvos_tools.infra.RandomizationTool import RandomizationTool
from ngts.nvos_tools.infra.SecureBootTool import SecureBootTool
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.tests_nvos.helpers.redmine_helpers import is_bug_active
from ngts.tests_nvos.system.clock.ClockTools import ClockTools
from ngts.tools.test_utils import allure_utils as allure


@pytest.mark.system
def test_physical_monitor(devices, engines):
    """
    Tests the Physical Monitor feature, which raises a system event in case of a PSC failure. Flow:
    0.  Choose 2 ASICs randomly and make sure their counter is currently at 0 (otherwise the test cannot proceed).
    1.  "Open backdoor" for both ASICs (enables error injection).
    2.  Inject PSC error into one ASIC and make sure a system event is created, specifying this ASIC.
    3.  Inject PSC error to the other ASIC and make sure a system event is created for this ASIC.
    4.  Inject 4 more errors into the first ASIC and make sure another event was created.
    5.  Cleanup: close backdoor.

    Note: event is raised only for the first error and every fifth error (per ASIC).
    Step #3 is crucial because steps 2 and 4 cause identical events to be created, and the system-events mechanism
    ignores a repeated event.
    """

    if SecureBootTool.is_prod_system(engines.dut):
        pytest.skip("Tests are supported only on development systems. The current system is a production system.")

    device: IbDevice = devices.dut
    nv = NvCommand()

    with allure.step("Choose 2 ASICs randomly"):
        asics = RandomizationTool.select_random_asics(device, how_many=2).get_returned_value()
        asic_devices = [device.mst_dev_name[i - 1] for i in asics]

    with allure.step("Make sure PSC event counter is 0 before the test begins"):
        for asic_dev in asic_devices:
            event_counter = get_physical_counter(asic_dev)
            if event_counter > 0:
                raise Exception(f"Cannot test feature because the physical-monitor event counter must be 0 before the test "
                                f"starts, but it's current value for {asic_dev} is {event_counter}")

    with McraTool.enable_backdoor(asic_devices):
        time_before_first = datetime.now(timezone.utc)
        prepare_and_inject_error(asic_devices[0])
        event = nv.system.events.get_last()
        assert_event(event, asics[0], time_before_first)

        time_before_second = datetime.now(timezone.utc)
        prepare_and_inject_error(asic_devices[1])
        event = nv.system.events.get_last()
        assert_event(event, asics[1], time_before_second)

        time_before_third = datetime.now(timezone.utc)
        for _ in range(4):
            inject_error(asic_devices[0])
        event = nv.system.events.get_last()
        assert_event(event, asics[0], time_before_third)


def prepare_and_inject_error(mst_device: str) -> None:
    """
    From FW team - the following commands are used for injecting a PSC event:
    mcra /dev/mst/mt54004_pciconf0 0x3ffffffc 0x80000000    # open backdoor
    # prepare register values:
    mcra /dev/mst/mt54004_pciconf0 0x201b50.0 0x4e444247
    mcra /dev/mst/mt54004_pciconf0 0x201b54.0 0xc
    mcra /dev/mst/mt54004_pciconf0 0x201b58.0 0x10000000
    mcra /dev/mst/mt54004_pciconf0 0x201b5c.0 0x000f4c1c
    mcra /dev/mst/mt54004_pciconf0 0x201b60.0 0x200
    # inject:
    mcra /dev/mst/mt54004_pciconf0 0x201f4c 0xa0300001
    """
    with allure.step(f"Preparing error-injection to {mst_device}"):
        McraTool.run_commands(mst_device, ("0x201b50.0 0x4e444247", "0x201b54.0 0xc", "0x201b58.0 0x10000000",
                                           "0x201b5c.0 0x000f4c1c", "0x201b60.0 0x200"))
    inject_error(mst_device)


def inject_error(mst_device: str) -> None:
    with allure.step(f"Performing error injection into {mst_device}"):
        McraTool.run_commands(mst_device, "0x201f4c 0xa0300001")


def get_physical_counter(mst_device: str) -> int:
    """
    Reads the PSC event counter directly from the ASIC. The counter is initially 0 and increases by 1 after each event
    happens (or is simulated) up to a maximum value of 15, then it stops increasing.
    """
    return int(McraTool.run_commands(mst_device, "0x318ffc.4"), base=16)


def assert_event(event: Dict[str, str], asic: int, time_before_injection: datetime):
    """
    Asserts that the last system event has the proper message, severity and component (ASIC).
    Also validates that the event was created after the specified time (ensuring it's a fresh event).
    """
    with allure.step(f"Asserting system-event was raised for asic {asic} and validating its content"):
        ValidationTool.validate_fields_values_in_output(*zip(
            (EventConsts.SEVERITY, EventConsts.MAJOR),
            (EventConsts.RESOURCE, f'asic {asic}'),
            (EventConsts.TEXT, "PSC detected failure")
        ), event).verify_result()

        timestamp_str = event[EventConsts.TIME_CREATED]
        parts = timestamp_str.rsplit(' ', 1)
        time_str = parts[0]
        tz_str = parts[1] if len(parts) > 1 else None

        # Parse the datetime
        event_time = ClockTools.parse_datetime(time_str)

        # If timezone is provided and indicates UTC, make it timezone-aware
        if tz_str and ('UTC' in tz_str or 'utc' in tz_str.lower()):
            event_time = event_time.replace(tzinfo=timezone.utc)
            current_time = datetime.now(timezone.utc)
        else:
            # If no timezone or non-UTC, assume local time
            current_time = datetime.now()
            # Make time_before_injection timezone-naive for comparison
            time_before_injection = time_before_injection.replace(tzinfo=None)

        # Ensure the event was created AFTER the injection (i.e., it's a fresh event)
        time_since_injection = (event_time - time_before_injection).total_seconds()
        assert time_since_injection >= 0, (
            f"Event timestamp {event_time.isoformat()} is OLDER than injection time "
            f"{time_before_injection.isoformat()}. This suggests the event is from a previous test run. "
            f"Event details: {event}"
        )

        time_since_event = (current_time - event_time).total_seconds()
        assert time_since_event < 5, (
            f"Event is too old. Current time: {current_time.isoformat()}, "
            f"event timestamp: {event_time.isoformat()} (from '{timestamp_str}'), "
            f"time since event: {time_since_event:.2f} seconds"
        )
