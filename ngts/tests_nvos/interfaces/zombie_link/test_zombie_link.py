import pytest
import logging
from retry.api import retry_call

from ngts.nvos_constants.constants_nvos import ConfState
from ngts.nvos_tools.infra.Fae import Fae
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.RandomizationTool import RandomizationTool
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import NvosConsts, IbInterfaceConsts, TxBwLossMonitorConsts
from ngts.tests_nvos.interfaces.nvl_port.helpers import skip_if_no_access_links
from ngts.tests_nvos.interfaces.zombie_link import get_zombie_link_manager
from ngts.tools.test_utils import allure_utils as allure


logger = logging.getLogger()

# Shortcuts
ZLState = TxBwLossMonitorConsts.State
ZLMonitor = TxBwLossMonitorConsts.MonitorStatus

# Retry settings — FlexCounter polls monitor-status every ~10s
VALIDATE_RETRIES = 6
VALIDATE_RETRY_DELAY = 5


@pytest.fixture
def zombie_link_manager(devices):
    """Resolve the zombie-link manager for the DUT (port lists per platform)."""
    return get_zombie_link_manager(devices)


def _get_monitor(fae_obj):
    """Return the tx-bandwidth-loss-monitor BaseComponent for a Fae object."""
    return fae_obj.port.interface.link.tx_bandwidth_loss_monitor


def _validate_state(fae_obj, expected_oper_state, expected_applied_state, expected_monitor_status):
    """
    Validate tx-bandwidth-loss-monitor show output for both operational and applied columns.

    Calls ``nv show fae interface <if> link tx-bandwidth-loss-monitor`` twice:
    once for operational values (state + monitor-status) and once for the applied state.
    """
    monitor = _get_monitor(fae_obj)

    oper = OutputParsingTool.parse_json_str_to_dictionary(monitor.show()).get_returned_value()
    assert oper.get(TxBwLossMonitorConsts.STATE) == expected_oper_state, \
        f"Operational state: expected '{expected_oper_state}', got '{oper.get(TxBwLossMonitorConsts.STATE)}'"
    assert oper.get(TxBwLossMonitorConsts.MONITOR_STATUS) == expected_monitor_status, \
        f"Monitor status: expected '{expected_monitor_status}', got '{oper.get(TxBwLossMonitorConsts.MONITOR_STATUS)}'"

    applied = OutputParsingTool.parse_json_str_to_dictionary(
        monitor.show(rev=ConfState.APPLIED)).get_returned_value()
    # Some platforms return an empty ``{}`` for the applied subtree when no
    # configuration is set — treat that as semantically equal to ``fw-default``
    # (the documented "no config" applied value).
    actual_applied = applied.get(TxBwLossMonitorConsts.STATE)
    if actual_applied is None and expected_applied_state == ZLState.FW_DEFAULT.value:
        actual_applied = ZLState.FW_DEFAULT.value
    assert actual_applied == expected_applied_state, \
        f"Applied state: expected '{expected_applied_state}', got '{applied.get(TxBwLossMonitorConsts.STATE)}'"


def _validate_state_with_retry(fae_obj, expected_oper_state, expected_applied_state, expected_monitor_status):
    """Validate state with retry to allow FlexCounter polling to update."""
    retry_call(
        _validate_state,
        fargs=[fae_obj, expected_oper_state, expected_applied_state, expected_monitor_status],
        exceptions=AssertionError,
        tries=VALIDATE_RETRIES,
        delay=VALIDATE_RETRY_DELAY,
    )


# ---------------------------------------------------------------------------
# Test 1 — Configuration (test plan 6.1)
# ---------------------------------------------------------------------------

@pytest.mark.interfaces
@pytest.mark.multiplanar
def test_tx_bw_loss_monitor_configuration(engines, devices, random_api):
    """
    Verify show, set, unset and invalid input on the tx-bandwidth-loss-monitor
    state knob, on a single randomly-picked port that is link-up.

    Platform-agnostic: the port is selected via ``RandomizationTool.select_random_port``,
    so this runs unchanged on any multi-planar IB switch (Juliet / Rosalind /
    Black Mamba / Crocodile / Taipan).

    Steps:
    - Show default state
    - Set disabled -> apply -> verify
    - Set fw-default -> apply -> verify (back to default)
    - Set enabled -> apply -> verify
    - Set invalid value -> verify rejection
    - Cleanup: unset -> verify state returns to fw-default
    """
    with allure.step("Select a random port in up state"):
        port_result = RandomizationTool.select_random_port(requested_ports_state=NvosConsts.LINK_STATE_UP)
        if not port_result.result:
            pytest.skip(f"Skipping test - {port_result.info}")
        selected_port = Fae(port_name=port_result.get_returned_value().name)

    monitor = _get_monitor(selected_port)

    try:
        with allure.step("Verify default state"):
            _validate_state(
                selected_port,
                TxBwLossMonitorConsts.DEFAULT_OPER_STATE,
                TxBwLossMonitorConsts.DEFAULT_APPLIED_STATE,
                TxBwLossMonitorConsts.DEFAULT_MONITOR_STATUS,
            )

        with allure.step("Set state to disabled and verify"):
            monitor.set(TxBwLossMonitorConsts.STATE, ZLState.DISABLED.value,
                        apply=True, ask_for_confirmation=True).verify_result()
            _validate_state_with_retry(selected_port, ZLState.DISABLED.value,
                                       ZLState.DISABLED.value, ZLMonitor.NA.value)

        with allure.step("Set state to fw-default and verify"):
            monitor.set(TxBwLossMonitorConsts.STATE, ZLState.FW_DEFAULT.value,
                        apply=True, ask_for_confirmation=True).verify_result()
            _validate_state_with_retry(selected_port,
                                       TxBwLossMonitorConsts.DEFAULT_OPER_STATE,
                                       TxBwLossMonitorConsts.DEFAULT_APPLIED_STATE,
                                       TxBwLossMonitorConsts.DEFAULT_MONITOR_STATUS)

        with allure.step("Set state to enabled and verify"):
            monitor.set(TxBwLossMonitorConsts.STATE, ZLState.ENABLED.value,
                        apply=True, ask_for_confirmation=True).verify_result()
            _validate_state_with_retry(selected_port, ZLState.ENABLED.value,
                                       ZLState.ENABLED.value, ZLMonitor.NORMAL.value)

        with allure.step("Set invalid state and verify rejection"):
            monitor.set(TxBwLossMonitorConsts.STATE, 'invalid-state',
                        expected_str=TxBwLossMonitorConsts.ERR_MSG_INVALID_STATE).verify_result()

    finally:
        with allure.step("Cleanup: unset and verify defaults restored"):
            monitor.unset(op_param=TxBwLossMonitorConsts.STATE, apply=True,
                          ask_for_confirmation=True).verify_result()
            _validate_state_with_retry(selected_port,
                                       TxBwLossMonitorConsts.DEFAULT_OPER_STATE,
                                       TxBwLossMonitorConsts.DEFAULT_APPLIED_STATE,
                                       TxBwLossMonitorConsts.DEFAULT_MONITOR_STATUS)


# ---------------------------------------------------------------------------
# Test 2 — Link Events (test plan 7.1)
# ---------------------------------------------------------------------------

@pytest.mark.interfaces
@pytest.mark.multiplanar
def test_tx_bw_loss_monitor_link_state_down(engines, devices, random_api):
    """
    Verify that operational state and monitor-status update on link down / up events.

    Platform-agnostic: picks any link-up port (acp on NVL, sw on non-NVL multi-ASIC).

    Steps:
    - Ensure monitor is enabled and port is link-up -> monitor-status=normal
    - Admin-down port -> verify state operational=enabled, monitor-status=N/A
    - Admin-up port, wait for link-up -> verify monitor-status=normal
    """
    with allure.step("Select a random port in up state"):
        port_result = RandomizationTool.select_random_port(requested_ports_state=NvosConsts.LINK_STATE_UP)
        if not port_result.result:
            pytest.skip(f"Skipping test - {port_result.info}")
        port = port_result.get_returned_value()
        selected_port = Fae(port_name=port.name)

    monitor = _get_monitor(selected_port)

    try:
        with allure.step("Enable monitor and verify precondition"):
            monitor.set(TxBwLossMonitorConsts.STATE, ZLState.ENABLED.value,
                        apply=True, ask_for_confirmation=True).verify_result()
            _validate_state_with_retry(selected_port, ZLState.ENABLED.value,
                                       ZLState.ENABLED.value, ZLMonitor.NORMAL.value)

        with allure.step("Admin-down port and verify monitor-status becomes N/A"):
            port.interface.link.state.set(
                op_param_name=NvosConsts.LINK_STATE_DOWN, apply=True,
                ask_for_confirmation=True).verify_result()
            _validate_state_with_retry(selected_port, ZLState.ENABLED.value,
                                       ZLState.ENABLED.value, ZLMonitor.NA.value)

        with allure.step("Admin-up port and verify monitor-status returns to normal"):
            port.interface.link.state.set(
                op_param_name=NvosConsts.LINK_STATE_UP, apply=True,
                ask_for_confirmation=True).verify_result()
            port.interface.wait_for_port_state(
                NvosConsts.LINK_STATE_UP).verify_result()
            _validate_state_with_retry(selected_port, ZLState.ENABLED.value,
                                       ZLState.ENABLED.value, ZLMonitor.NORMAL.value)

    finally:
        with allure.step("Cleanup: bring port up, unset monitor state, and verify defaults restored"):
            port.interface.link.state.set(
                op_param_name=NvosConsts.LINK_STATE_UP, apply=True,
                ask_for_confirmation=True).verify_result()
            port.interface.wait_for_port_state(
                NvosConsts.LINK_STATE_UP).verify_result()
            monitor.unset(op_param=TxBwLossMonitorConsts.STATE, apply=True,
                          ask_for_confirmation=True).verify_result()
            _validate_state_with_retry(selected_port,
                                       TxBwLossMonitorConsts.DEFAULT_OPER_STATE,
                                       TxBwLossMonitorConsts.DEFAULT_APPLIED_STATE,
                                       TxBwLossMonitorConsts.DEFAULT_MONITOR_STATUS)


# ---------------------------------------------------------------------------
# Test 3 — Range-based configuration (ports under test)
# ---------------------------------------------------------------------------

@pytest.mark.interfaces
@pytest.mark.multiplanar
def test_tx_bw_loss_monitor_range(engines, devices, random_api, standalone_system, has_loopbox, is_simx,
                                  zombie_link_manager):
    """
    Verify tx-bandwidth-loss-monitor state can be set on a range of ports
    (e.g. ``acp1-72`` on NVL5/NVL6, ``sw1-144p1`` on Taipan, ``sw1-72p1-2`` on
    Black Mamba, ``swA1-18p1-2,swB1-18p1-2`` on Crocodile).

    The manager picks the platform-appropriate port list (see
    ``managers.py``): NVL access ports when available, otherwise the
    traffic-port subset derived from ``dut.interface_list``.

    Steps:
    - Build a port range from ``manager.ports_under_test``
    - Set disabled on the range -> verify on a random single port
    - Set enabled on the range -> verify on a random single port
    - Unset on the range -> verify defaults restored on the single port
    """
    ports_under_test = zombie_link_manager.ports_under_test
    if not ports_under_test:
        pytest.skip("Platform exposes no ports_under_test for the range test")

    # ``skip_if_no_access_links`` only applies on NVL platforms (acp loopbox concept).
    # Other multi-ASIC platforms have no access-port wiring — skipping there would be wrong.
    if getattr(devices.dut, 'nvl_access_ports_list', None):
        skip_if_no_access_links(has_loopbox, standalone_system, is_simx)

    with allure.step("Build port range"):
        port_range = zombie_link_manager.build_port_range(ports_under_test)
        all_ports = Fae(port_name=port_range)

    with allure.step("Select a link-up port for single-port verification"):
        # monitor-status only flips to 'normal' on link-up ports. On non-NVL
        # platforms most ports in ports_under_test are not physically wired,
        # so picking randomly from the full list gives a flaky ``N/A`` verify.
        up_port_res = RandomizationTool.select_random_port(requested_ports_state=NvosConsts.LINK_STATE_UP)
        if not up_port_res.result:
            pytest.skip(f"no up port to verify range on: {up_port_res.info}")
        verify_port_name = up_port_res.get_returned_value().name
        if verify_port_name not in set(ports_under_test):
            pytest.skip(f"up port {verify_port_name} is not in ports_under_test")
        verify_port = Fae(port_name=verify_port_name)

    range_monitor = _get_monitor(all_ports)

    try:
        with allure.step(f"Set disabled on range {port_range} and verify on {verify_port_name}"):
            range_monitor.set(TxBwLossMonitorConsts.STATE, ZLState.DISABLED.value,
                              apply=True, ask_for_confirmation=True).verify_result()
            _validate_state_with_retry(verify_port, ZLState.DISABLED.value,
                                       ZLState.DISABLED.value, ZLMonitor.NA.value)

        with allure.step(f"Set enabled on range {port_range} and verify on {verify_port_name}"):
            range_monitor.set(TxBwLossMonitorConsts.STATE, ZLState.ENABLED.value,
                              apply=True, ask_for_confirmation=True).verify_result()
            _validate_state_with_retry(verify_port, ZLState.ENABLED.value,
                                       ZLState.ENABLED.value, ZLMonitor.NORMAL.value)

    finally:
        with allure.step("Cleanup: unset on range to restore defaults"):
            range_monitor.unset(op_param=TxBwLossMonitorConsts.STATE, apply=True,
                                ask_for_confirmation=True).verify_result()
            _validate_state_with_retry(verify_port,
                                       TxBwLossMonitorConsts.DEFAULT_OPER_STATE,
                                       TxBwLossMonitorConsts.DEFAULT_APPLIED_STATE,
                                       TxBwLossMonitorConsts.DEFAULT_MONITOR_STATUS)


# ---------------------------------------------------------------------------
# Test 4 — FNM ports (internal or external, depending on platform)
# ---------------------------------------------------------------------------

@pytest.mark.interfaces
@pytest.mark.multiplanar
def test_tx_bw_loss_monitor_fnm_port(engines, devices, random_api, zombie_link_manager):
    """
    Verify tx-bandwidth-loss-monitor is configurable on FNM ports.

    FNM ports come in two flavors:
    - Internal FNM (``fnmaXpY``) — connect ASICs together inside the switch;
      LinkUp by default.
    - External FNM (``fnmN``) — face the outside.

    Either flavor must expose tx-bandwidth-loss-monitor and behave the same as
    user-facing ports. The manager picks the platform-appropriate FNM list
    (see ``managers.py``): NVL internal FNM when available, otherwise the
    platform's external FNM list (``fnm_external_port_list``).

    Steps (mirrors test_tx_bw_loss_monitor_configuration, on an FNM port):
    - Show default state
    - Set disabled -> apply -> verify
    - Set fw-default -> apply -> verify (back to default)
    - Set enabled -> apply -> verify (monitor-status=normal since port is link-up)
    - Set invalid value -> verify rejection
    - Cleanup: unset -> verify state returns to fw-default
    """
    fnm_ports = zombie_link_manager.fnm_ports_under_test
    if not fnm_ports:
        pytest.skip("No FNM ports defined for this platform")

    with allure.step("Select a random FNM port"):
        fnm_port_name = RandomizationTool.select_random_value(fnm_ports).get_returned_value()
        selected_port = Fae(port_name=fnm_port_name)

    with allure.step(f"Verify FNM port {fnm_port_name} is link-up before exercising the monitor"):
        # The tx-bandwidth-loss monitor reports 'N/A' when the port is not up, so
        # the monitor-status assertions below are meaningless on a down FNM port.
        # Gate on the real precondition (link state) rather than the monitor itself.
        link_state = OutputParsingTool.parse_show_interface_link_output_to_dictionary(
            selected_port.port.interface.link.show()).get_returned_value().get(IbInterfaceConsts.LINK_STATE)
        if link_state != NvosConsts.LINK_STATE_UP:
            pytest.skip(f"FNM port {fnm_port_name} is not up (state={link_state}); "
                        "tx-bw-loss monitor is N/A - skipping")

    monitor = _get_monitor(selected_port)

    try:
        with allure.step(f"Verify default state on {fnm_port_name}"):
            _validate_state(
                selected_port,
                TxBwLossMonitorConsts.DEFAULT_OPER_STATE,
                TxBwLossMonitorConsts.DEFAULT_APPLIED_STATE,
                TxBwLossMonitorConsts.DEFAULT_MONITOR_STATUS,
            )

        with allure.step("Set state to disabled and verify"):
            monitor.set(TxBwLossMonitorConsts.STATE, ZLState.DISABLED.value,
                        apply=True, ask_for_confirmation=True).verify_result()
            _validate_state_with_retry(selected_port, ZLState.DISABLED.value,
                                       ZLState.DISABLED.value, ZLMonitor.NA.value)

        with allure.step("Set state to fw-default and verify"):
            monitor.set(TxBwLossMonitorConsts.STATE, ZLState.FW_DEFAULT.value,
                        apply=True, ask_for_confirmation=True).verify_result()
            _validate_state_with_retry(selected_port,
                                       TxBwLossMonitorConsts.DEFAULT_OPER_STATE,
                                       TxBwLossMonitorConsts.DEFAULT_APPLIED_STATE,
                                       TxBwLossMonitorConsts.DEFAULT_MONITOR_STATUS)

        with allure.step("Set state to enabled and verify"):
            monitor.set(TxBwLossMonitorConsts.STATE, ZLState.ENABLED.value,
                        apply=True, ask_for_confirmation=True).verify_result()
            _validate_state_with_retry(selected_port, ZLState.ENABLED.value,
                                       ZLState.ENABLED.value, ZLMonitor.NORMAL.value)

        with allure.step("Set invalid state and verify rejection"):
            monitor.set(TxBwLossMonitorConsts.STATE, 'invalid-state',
                        expected_str=TxBwLossMonitorConsts.ERR_MSG_INVALID_STATE).verify_result()

    finally:
        with allure.step("Cleanup: unset and verify defaults restored"):
            monitor.unset(op_param=TxBwLossMonitorConsts.STATE, apply=True,
                          ask_for_confirmation=True).verify_result()
            _validate_state_with_retry(selected_port,
                                       TxBwLossMonitorConsts.DEFAULT_OPER_STATE,
                                       TxBwLossMonitorConsts.DEFAULT_APPLIED_STATE,
                                       TxBwLossMonitorConsts.DEFAULT_MONITOR_STATUS)
