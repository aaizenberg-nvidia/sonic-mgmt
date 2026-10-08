import random

import pytest

from ngts.ngts_types import DevicesT, EnginesT
from ngts.nvos_constants.constants_nvos import NvosConst
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port
from ngts.nvos_tools.infra.PollingTool import PollingTool
from ngts.nvos_tools.infra.Tools import Tools
from ngts.tests_nvos.system.gnmi.constants import TELEMETRY_CONVERGENCE_TIMEOUT_SEC
from ngts.tests_nvos.system.gnmi.helpers import get_infiniband_name_from_port_name
from ngts.tests_nvos.system.telemetry.telemetry_watch import (
    Reads,
    TelemetrySource,
    assert_bounced,
    gnmi_server_enabled,
    gnmi_watch,
    read_all,
    telemetry_xpaths,
)
from ngts.tools.test_utils import allure_utils as allure

DESCRIPTION_LENGTH = 20


@pytest.mark.system
@pytest.mark.gnmi
def test_gnmi_telemetry_watch_interface_description(engines: EnginesT, devices: DevicesT, random_api: str):
    """
    @summary: An interface description change reaches NVUE, OpenAPI, gNMI and CONFIG_DB, and streams over gNMI.

    1. Read the baseline description of a random port from all 4 sources
    2. Open a gNMI ON_CHANGE stream on the port's description leaf
    3. Set a random description and wait until all 4 sources report it
    4. Restore the baseline description and wait until all 4 sources report it
    5. Verify the stream carried the description away from the baseline and back
    """
    _verify_description_round_trip(engines, devices,
                                   Tools.RandomizationTool.select_random_port(requested_ports_state=None).returned_value)


@pytest.mark.system
@pytest.mark.gnmi
@pytest.mark.cpov2
def test_gnmi_telemetry_watch_cpo_port_description(engines: EnginesT, devices, random_api: str):
    """
    @summary: A description change on a CPO sw port reaches NVUE, OpenAPI, gNMI and CONFIG_DB, and streams over gNMI.

    1. Read the baseline description of a random CPO sw port from all 4 sources
    2. Open a gNMI ON_CHANGE stream on the port's description leaf
    3. Set a random description and wait until all 4 sources report it
    4. Restore the baseline description and wait until all 4 sources report it
    5. Verify the stream carried the description away from the baseline and back
    """
    _verify_description_round_trip(engines, devices, Port(random.choice(devices.dut.nvl_trunk_ports_list)))


def _verify_description_round_trip(engines: EnginesT, devices: DevicesT, port: Port) -> None:
    ib_port = get_infiniband_name_from_port_name(engines.dut, port.name)
    xpath = telemetry_xpaths(port.interface, [NvosConst.DESCRIPTION])[NvosConst.DESCRIPTION]
    description = Tools.RandomizationTool.get_random_string(DESCRIPTION_LENGTH)

    def wait_for_reads(expected: Reads, stage: str) -> None:
        PollingTool.poll_until(lambda: read_all(engines, devices, port.interface, ib_port=ib_port),
                               lambda reads: reads == expected,
                               timeout_seconds=TELEMETRY_CONVERGENCE_TIMEOUT_SEC,
                               description=f'all sources report the {stage} description of {port.name}')

    with gnmi_server_enabled():
        with allure.step(f'Read the baseline description of {port.name} ({ib_port}) from all sources'):
            baseline = read_all(engines, devices, port.interface, ib_port=ib_port)
        original = baseline[NvosConst.DESCRIPTION][TelemetrySource.NVUE]

        def restore_original() -> None:
            if original:
                port.interface.set(NvosConst.DESCRIPTION, original, apply=True).verify_result()
            else:
                port.interface.unset(NvosConst.DESCRIPTION, apply=True).verify_result()

        description_changed = False
        try:
            with gnmi_watch(engines, devices, [xpath]) as watch:
                with allure.step(f'Set description "{description}" and wait for all sources'):
                    description_changed = True
                    port.interface.set(NvosConst.DESCRIPTION, description, apply=True).verify_result()
                    wait_for_reads({field: dict.fromkeys(sources, description) for field, sources in baseline.items()},
                                   'new')
                with allure.step('Restore the baseline description and wait for all sources'):
                    restore_original()
                    description_changed = False
                    wait_for_reads(baseline, 'baseline')
        finally:
            if description_changed:
                with allure.step(f'Cleanup: restore the baseline description of {port.name}'):
                    restore_original()

        with allure.step('Verify the description leaf streamed away from the baseline and back'):
            assert_bounced(watch, xpath, steady=watch.baseline.get(xpath))
