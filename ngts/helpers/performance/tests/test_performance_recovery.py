"""Unit-only coverage for performance recovery; no lab devices are accessed."""

from unittest.mock import Mock

import pytest

from ngts.helpers.performance import performance_recovery


FATAL_RETURN_CODE = 42


class NvueCli:
    """Minimal class-name-compatible NVUE CLI test double."""

    pass


@pytest.fixture(autouse=True)
def reset_recovery_state(monkeypatch):
    """Reset process-local state and prevent real Allure attachment writes."""
    performance_recovery.reset_performance_session_state()
    monkeypatch.setattr(performance_recovery.allure, "attach", Mock())


@pytest.mark.parametrize(
    "message",
    ["COMM_ERROR", "sx_api_open failed", "SDK GOT Signal 15"])
def test_sdk_fatal_signatures_are_detected(message):
    """Detect every approved SDK fatal signature."""
    assert performance_recovery.is_sdk_fatal_exception(RuntimeError(message))


def test_ordinary_traffic_failure_is_not_sdk_fatal():
    """Do not reclassify ordinary traffic assertions as environment failures."""
    assert not performance_recovery.is_sdk_fatal_exception(
        AssertionError("TX utilization 0.90 is below threshold 0.95"))


@pytest.mark.parametrize(
    "primary_error, call_failed, expected",
    [
        (RuntimeError("setup failed"), False, True),
        (None, True, True),
        (None, False, False),
    ])
def test_primary_failure_detection_covers_setup_and_test_body(
        primary_error, call_failed, expected):
    """Preserve setup exceptions and failed pytest call reports during teardown."""
    node = Mock(rep_call=Mock(failed=call_failed))

    assert performance_recovery.has_primary_test_failure(
        primary_error, node) is expected


def test_class_scoped_primary_failure_detects_failed_child_item():
    """Preserve a child test failure when class-scoped teardown also fails."""
    performance_recovery.record_performance_test_failure(
        "suite/test_example.py::TestExample::test_failed")
    class_node = Mock(
        nodeid="suite/test_example.py::TestExample",
        spec=["nodeid"])

    assert performance_recovery.has_primary_test_failure(
        None, class_node)


def test_class_scoped_primary_failure_ignores_other_class():
    """Do not suppress teardown errors because an unrelated class failed."""
    performance_recovery.record_performance_test_failure(
        "suite/test_example.py::TestOther::test_failed")
    class_node = Mock(
        nodeid="suite/test_example.py::TestExample",
        spec=["nodeid"])

    assert not performance_recovery.has_primary_test_failure(
        None, class_node)


def test_save_session_baseline_only_once():
    """Keep the first baseline and never invoke the save callback twice."""
    save_callback = Mock()
    capture = Mock(return_value={"dut": {"connected_ports": [], "expected_neighbors": []}})

    assert performance_recovery.save_session_baseline(
        {}, save_callback, topology_capture=capture)
    assert not performance_recovery.save_session_baseline(
        {}, save_callback, topology_capture=capture)

    save_callback.assert_called_once_with()
    capture.assert_called_once_with({})


def test_failed_baseline_save_is_fatal_and_cannot_be_retried():
    """Never let a later class replace a partially saved baseline."""
    original = RuntimeError("baseline save failed")
    save_callback = Mock(side_effect=original)

    with pytest.raises(RuntimeError) as error:
        performance_recovery.save_session_baseline(
            {}, save_callback, topology_capture=lambda players: {})

    assert error.value is original
    state = performance_recovery.get_performance_session_state()
    assert state.baseline_save_failed
    assert state.environment_fatal
    with pytest.raises(performance_recovery.TestIssue, match="Refusing to overwrite"):
        performance_recovery.save_session_baseline(
            {}, save_callback, topology_capture=lambda players: {})
    save_callback.assert_called_once_with()


def test_session_baseline_rejects_xdist_worker(monkeypatch):
    """Reject process-local recovery only when an affected fixture runs under xdist."""
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")

    with pytest.raises(performance_recovery.TestIssue, match="xdist is unsupported"):
        performance_recovery.save_session_baseline(
            {}, Mock(), topology_capture=lambda players: {})


def test_save_callback_blocked_after_restore_and_fatal():
    """Do not overwrite the known-good baseline after restore or fatal state."""
    save_callback = Mock()
    performance_recovery.save_session_baseline(
        {}, save_callback, topology_capture=lambda players: {})
    performance_recovery.restore_session_baseline(
        {}, Mock(), health_checker=lambda players, topology: None)
    performance_recovery.mark_environment_fatal(
        {}, "COMM_ERROR", diagnostics_collector=Mock())

    assert not performance_recovery.save_session_baseline(
        {}, save_callback, topology_capture=lambda players: {})
    save_callback.assert_called_once_with()


@pytest.mark.parametrize(
    "blocked_field",
    ["baseline_save_failed", "restore_attempted", "restore_failed", "environment_fatal"])
def test_initial_save_is_rejected_after_unsafe_state(blocked_field):
    """Reject a first save if restore or fatal state has already been recorded."""
    setattr(performance_recovery.get_performance_session_state(), blocked_field, True)
    save_callback = Mock()

    with pytest.raises(performance_recovery.TestIssue, match="Refusing to overwrite"):
        performance_recovery.save_session_baseline(
            {}, save_callback, topology_capture=lambda players: {})

    save_callback.assert_not_called()


def test_restore_failure_marks_fatal_and_preserves_exception():
    """Mark restore failures fatal without replacing the original exception."""
    original = RuntimeError("restore apply failed")
    collector = Mock()
    performance_recovery.save_session_baseline(
        {}, Mock(), topology_capture=lambda players: {})

    with pytest.raises(RuntimeError) as error:
        performance_recovery.restore_session_baseline(
            {}, Mock(side_effect=original), nodeid="node",
            diagnostics_collector=collector)

    assert error.value is original
    state = performance_recovery.get_performance_session_state()
    assert state.restore_failed
    assert state.environment_fatal
    assert state.fatal_nodeid == "node"
    collector.assert_called_once()


def test_health_failure_marks_fatal_and_preserves_exception():
    """Mark health-gate failures fatal without replacing the health exception."""
    original = RuntimeError("expected LLDP neighbor missing")
    performance_recovery.save_session_baseline(
        {}, Mock(), topology_capture=lambda players: {})

    with pytest.raises(RuntimeError) as error:
        performance_recovery.restore_session_baseline(
            {}, Mock(), health_checker=Mock(side_effect=original),
            diagnostics_collector=Mock())

    assert error.value is original
    assert performance_recovery.get_performance_session_state().restore_failed


def test_nvue_health_gate_checks_only_expected_topology_ports():
    """Ignore unrelated physical ports while checking connected baseline ports."""
    cli_obj = NvueCli()
    cli_obj.engine = Mock()
    cli_obj.performance = Mock()
    cli_obj.interface = Mock()
    cli_obj.performance.execute_cmd.return_value = (
        "swp1 up up\n"
        "swp99 up down\n")
    cli_obj.interface.filter_lldp_neighbors.return_value = {"left-tg": ["swp1"]}
    players = {"dut": {"cli": cli_obj, "engine": cli_obj.engine}}
    topology = {
        "dut": {
            "connected_ports": ["swp1"],
            "expected_neighbors": ["left-tg"],
            "neighbor_ports": {"left-tg": ["swp1"]},
        },
    }

    performance_recovery._default_nvue_health_gate(
        players, topology, timeout=0, interval=0)

    cli_obj.engine.run_cmd.assert_called_once()


def test_nvue_health_gate_rejects_expected_port_down():
    """Fail health validation when a topology-connected port is not oper-up."""
    cli_obj = NvueCli()
    cli_obj.engine = Mock()
    cli_obj.performance = Mock()
    cli_obj.interface = Mock()
    cli_obj.performance.execute_cmd.return_value = "swp1 up down\n"
    players = {"dut": {"cli": cli_obj, "engine": cli_obj.engine}}
    topology = {
        "dut": {
            "connected_ports": ["swp1"],
            "expected_neighbors": ["left-tg"],
            "neighbor_ports": {"left-tg": ["swp1"]},
        },
    }

    with pytest.raises(performance_recovery.TestIssue, match="not oper-up"):
        performance_recovery._default_nvue_health_gate(
            players, topology, timeout=0, interval=0)


def test_nvue_health_gate_waits_for_exact_lldp_attachment_to_converge(monkeypatch):
    """Retry transient LLDP movement and require the original attachment point."""
    cli_obj = NvueCli()
    cli_obj.engine = Mock()
    cli_obj.performance = Mock()
    cli_obj.interface = Mock()
    cli_obj.performance.execute_cmd.return_value = "swp1 up up\n"
    cli_obj.interface.filter_lldp_neighbors.side_effect = [
        {"left-tg": ["swp2"]},
        {"left-tg": ["swp1"]},
    ]
    players = {"dut": {"cli": cli_obj, "engine": cli_obj.engine}}
    topology = {
        "dut": {
            "connected_ports": ["swp1"],
            "expected_neighbors": ["left-tg"],
            "neighbor_ports": {"left-tg": ["swp1"]},
        },
    }
    monotonic_values = iter([0, 0, 1])
    monkeypatch.setattr(
        performance_recovery.time, "monotonic",
        lambda: next(monotonic_values))
    monkeypatch.setattr(performance_recovery.time, "sleep", Mock())

    performance_recovery._default_nvue_health_gate(
        players, topology, timeout=2, interval=1)

    assert cli_obj.interface.filter_lldp_neighbors.call_count == 2
    performance_recovery.time.sleep.assert_called_once_with(1)


def test_nvue_health_gate_retries_transient_sdk_probe_failure(monkeypatch):
    """Retry a transient SDK command failure within the convergence window."""
    cli_obj = NvueCli()
    cli_obj.engine = Mock()
    cli_obj.engine.run_cmd.side_effect = [RuntimeError("SDK not ready"), None]
    cli_obj.performance = Mock()
    cli_obj.interface = Mock()
    cli_obj.performance.execute_cmd.return_value = "swp1 up up\n"
    cli_obj.interface.filter_lldp_neighbors.return_value = {"left-tg": ["swp1"]}
    players = {"dut": {"cli": cli_obj, "engine": cli_obj.engine}}
    topology = {
        "dut": {
            "connected_ports": ["swp1"],
            "expected_neighbors": ["left-tg"],
            "neighbor_ports": {"left-tg": ["swp1"]},
        },
    }
    monotonic_values = iter([0, 0])
    monkeypatch.setattr(
        performance_recovery.time, "monotonic",
        lambda: next(monotonic_values))
    monkeypatch.setattr(performance_recovery.time, "sleep", Mock())

    performance_recovery._default_nvue_health_gate(
        players, topology, timeout=2, interval=1)

    assert cli_obj.engine.run_cmd.call_count == 2
    performance_recovery.time.sleep.assert_called_once_with(1)


def test_nvue_health_gate_rejects_additional_lldp_attachment():
    """Require exact baseline LLDP attachment points, not a subset."""
    cli_obj = NvueCli()
    cli_obj.engine = Mock()
    cli_obj.performance = Mock()
    cli_obj.interface = Mock()
    cli_obj.performance.execute_cmd.return_value = "swp1 up up\n"
    cli_obj.interface.filter_lldp_neighbors.return_value = {
        "left-tg": ["swp1", "swp2"],
    }
    players = {"dut": {"cli": cli_obj, "engine": cli_obj.engine}}
    topology = {
        "dut": {
            "connected_ports": ["swp1"],
            "expected_neighbors": ["left-tg"],
            "neighbor_ports": {"left-tg": ["swp1"]},
        },
    }

    with pytest.raises(performance_recovery.TestIssue, match="attachment points changed"):
        performance_recovery._default_nvue_health_gate(
            players, topology, timeout=0, interval=0)


def test_guarded_operation_marks_only_sdk_failure_and_preserves_exception():
    """Classify validator SDK failure while re-raising the same exception."""
    original = RuntimeError("validator returned COMM_ERROR")
    performance_recovery.save_session_baseline(
        {}, Mock(), topology_capture=lambda players: {})

    with pytest.raises(RuntimeError) as error:
        performance_recovery.run_guarded_operation(
            {}, Mock(side_effect=original), nodeid="validator",
            diagnostics_collector=Mock())

    assert error.value is original
    assert performance_recovery.get_performance_session_state().environment_fatal


def test_fatal_gate_exits_with_return_code_42():
    """Exit before the next test with the existing fatal-state return code."""
    performance_recovery.get_performance_session_state().environment_fatal = True
    performance_recovery.get_performance_session_state().fatal_reason = "SDK GOT Signal"
    exit_call = {}

    def fake_exit(message, returncode):
        exit_call.update(message=message, returncode=returncode)
        raise RuntimeError("pytest exit")

    with pytest.raises(RuntimeError, match="pytest exit"):
        performance_recovery.exit_if_performance_environment_fatal(
            fake_exit, FATAL_RETURN_CODE)

    assert exit_call["returncode"] == FATAL_RETURN_CODE


def test_session_finish_preserves_fatal_return_code_without_next_test():
    """Set return code 42 even when no later item reaches pytest_runtest_setup."""
    session = Mock(exitstatus=1)
    performance_recovery.get_performance_session_state().environment_fatal = True

    performance_recovery.enforce_fatal_session_exit_status(
        session, FATAL_RETURN_CODE)

    assert session.exitstatus == FATAL_RETURN_CODE


@pytest.mark.parametrize("exitstatus", [2, 3, 4, 5])
def test_session_finish_preserves_higher_priority_exit_status(exitstatus):
    """Do not mask interrupted, internal, usage, or no-tests outcomes."""
    session = Mock(exitstatus=exitstatus)
    performance_recovery.get_performance_session_state().environment_fatal = True

    performance_recovery.enforce_fatal_session_exit_status(
        session, FATAL_RETURN_CODE)

    assert session.exitstatus == exitstatus


def test_diagnostics_swallow_command_failures_and_attach_manifest():
    """Record command errors in the manifest without raising."""
    cli_obj = NvueCli()
    engine = Mock()
    engine.run_cmd.side_effect = RuntimeError("command unavailable")
    players = {"dut": {"cli": cli_obj, "engine": engine}}

    manifest = performance_recovery.collect_fatal_diagnostics(
        players, "restore failed", "node")

    assert manifest["players"]["dut"]["nvue_supported"]
    assert all("error" in artifact
               for artifact in manifest["players"]["dut"]["artifacts"].values())
    assert performance_recovery.get_performance_session_state().diagnostics_collected
    assert any(call.kwargs.get("name") == "fatal_diagnostics_manifest.json"
               for call in performance_recovery.allure.attach.call_args_list)


def test_diagnostics_bound_output_and_record_hash():
    """Limit each collected artifact and record its size and digest."""
    cli_obj = NvueCli()
    engine = Mock()
    engine.run_cmd.return_value = "x" * (performance_recovery.DIAGNOSTIC_OUTPUT_LIMIT + 100)
    players = {"dut": {"cli": cli_obj, "engine": engine}}

    manifest = performance_recovery.collect_fatal_diagnostics(
        players, "SDK GOT Signal", "node")

    artifact = manifest["players"]["dut"]["artifacts"]["journals"]
    assert artifact["bytes"] == performance_recovery.DIAGNOSTIC_OUTPUT_LIMIT
    assert artifact["truncated"]
    assert len(artifact["sha256"]) == 64


def test_diagnostics_exact_limit_is_not_marked_truncated():
    """Do not mark naturally exact-limit output as truncated without the marker."""
    engine = Mock()
    engine.run_cmd.return_value = "x" * performance_recovery.DIAGNOSTIC_OUTPUT_LIMIT

    output, truncated = performance_recovery._run_bounded_command(
        engine, "nv show interface")

    assert len(output) == performance_recovery.DIAGNOSTIC_OUTPUT_LIMIT
    assert not truncated


def test_non_nvue_diagnostics_degrade_safely():
    """Record unsupported CLI types without running NVUE commands."""
    engine = Mock()

    manifest = performance_recovery.collect_fatal_diagnostics(
        {"dut": {"cli": object(), "engine": engine}}, "fatal", "node")

    assert not manifest["players"]["dut"]["nvue_supported"]
    engine.run_cmd.assert_not_called()


def test_diagnostic_collector_exception_is_swallowed():
    """Never let a collector failure replace the primary fatal failure."""
    performance_recovery.mark_environment_fatal(
        {}, "COMM_ERROR", diagnostics_collector=Mock(side_effect=RuntimeError("collector failed")))

    assert performance_recovery.get_performance_session_state().environment_fatal
