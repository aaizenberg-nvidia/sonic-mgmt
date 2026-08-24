import base64
import concurrent.futures
import logging
import random
import re
import time
from datetime import datetime

import pytest

from ngts.constants.constants import BugHandlerConst
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import IbInterfaceConsts, NvosConsts
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port
from ngts.nvos_tools.infra.Fae import Fae
from ngts.nvos_tools.infra.MultiPlanarTool import MultiPlanarTool
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.RandomizationTool import RandomizationTool
from ngts.nvos_tools.infra.Tools import Tools
from ngts.nvos_tools.nmx.Cluster import Cluster
from ngts.nvos_constants.constants_nvos import ActionParamConsts, HealthConsts, OutputFormat, SystemConsts
from ngts.tests_nvos.cluster.ansible_playbooks_tool import AnsiblePlaybooksTool
from ngts.tests_nvos.cluster.cluster_consts import AnsiblePlaybooksConsts as Ansible
from ngts.tests_nvos.cluster.cluster_tools import ClusterTools, disabled_access_ports
from ngts.tests_nvos.cluster.test_cluster_traffic import assert_test_results
from ngts.tests_nvos.constants import MINUTE
from ngts.tests_nvos.interfaces.nvl_port.helpers import wait_for_ports_state
from ngts.tests_nvos.system.clock.ClockTools import ClockTools
from ngts.nvos_tools.infra.McraTool import McraTool
from ngts.nvos_tools.system.System import System
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger(__name__)

SNAPSHOT_LOG_DIR = '/var/log/swss-ibv0'
ACTIVE_SNAPSHOT_PATTERN = re.compile(r'^mlid-event-snapshot\.v(?P<format_version>\d+)\.asic(?P<asic_id>\d+)\.log$')
ROTATED_SNAPSHOT_PATTERN = re.compile(r'^mlid-event-snapshot\.v(?P<format_version>\d+)\.asic(?P<asic_id>\d+)\.log\.(?P<rotate>\d+)\.gz$')
EXPECTED_COUNTER_COUNT = 32
SNAPSHOT_METADATA_FIELD_COUNT = 6
SNAPSHOT_EXPECTED_FIELD_COUNT = SNAPSHOT_METADATA_FIELD_COUNT + EXPECTED_COUNTER_COUNT
CURRENT_SNAPSHOT_FORMAT_VERSION = 1
MLID_RATE_LIMIT_EVENTS_PER_ASIC = 100
MLID_LOGROTATE_SIZE_MIB = 16
MLID_LOGROTATE_SIZE_BYTES = MLID_LOGROTATE_SIZE_MIB * 1024 * 1024  # 16_777_216
MLID_LOGROTATE_TIMER_OVERRIDE_PATH = '/etc/systemd/system/logrotate.timer.d/timerOverride.conf'
SYSTEMCTL_DAEMON_RELOAD_CMD = 'sudo systemctl daemon-reload'
MLID_LOGROTATE_TIMER_RESTART_CMD = 'sudo systemctl restart logrotate.timer'
MLID_SSD_STRESS_FILL_PATH = f'{SNAPSHOT_LOG_DIR}/mlid_ssd_stress_fill'
MLID_SNAPSHOT_DROP_SYSLOG_PATTERN = re.compile(r'isMlidEventSnapshotRateLimited: MLID event snapshot: rate limit reached \(100 events/min/ASIC\), dropping further events this window')


@pytest.fixture(scope='module', autouse=True)
def mlid_mcra_backdoor(devices):
    """Open mcra backdoor on all ASICs once for the module; close all on teardown."""
    with McraTool.enable_backdoor(devices.dut.mst_dev_name):
        yield


@pytest.mark.fae
@pytest.mark.timeout(15 * MINUTE, func_only=True)
def test_mlid_counters_isolation(engines, devices, nv_command, random_api, test_name):
    """
    Verify MLID counter snapshots are isolated per ASIC, persist across reboot, and included in tech-support.

    1. Ensure MLID event snapshot is enabled.
    2. Select one ACP port on each ASIC.
    3. Simulate MLID event on each selected port.
    4. Reboot system.
    5. Verify one active snapshot log file per ASIC.
    6. Generate tech-support and verify snapshot files under log/swss-ibv0.
    7. Parse and verify snapshot file content per ASIC.
    """
    fae = Fae()
    system = None
    try:
        with allure.step("Ensure MLID event snapshot is enabled"):
            output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(
                fae.system.mlid_event_snapshot.show()
            ).get_returned_value()
            Tools.ValidationTool.verify_field_value_in_output(
                output_dictionary, SystemConsts.STATE, ActionParamConsts.ENABLED
            )

        with allure.step("Select one port on each ASIC"):
            acp_ports = devices.dut.nvl_access_ports_list
            ports_per_asic = len(acp_ports) // devices.dut.asic_amount
            ports = {
                asic_id: RandomizationTool.select_random_value(
                    acp_ports[asic_id * ports_per_asic:(asic_id + 1) * ports_per_asic]
                ).get_returned_value()
                for asic_id in range(devices.dut.asic_amount)
            }

        lines_before = {
            asic_id: _count_snapshot_events_for_asic(engines.dut, asic_id)
            for asic_id in ports
        }

        local_ports = {}
        for _, port_name in ports.items():
            with allure.step(f"Simulate MLID event on {port_name}"):
                local_ports[port_name] = _simulate_mlid_event(engines.dut, port_name)

        with allure.step("Reboot system"):
            nv_command.system.reboot.action_reboot(params='force').verify_result()

        with allure.step("Re-open mcra backdoor after reboot"):
            mst_dev_names = devices.dut.mst_dev_name
            if isinstance(mst_dev_names, str):
                mst_dev_names = (mst_dev_names, )
            for mst_dev_name in mst_dev_names:
                McraTool.run_commands(mst_dev_name, McraTool.BACKDOOR_OPEN_CMD, engine=engines.dut)

        with allure.step("Verify all snapshot files were created and persisted after reboot"):
            log_files = _list_snapshot_files(engines.dut, active_only=True)
            assert len(log_files) >= len(ports), (
                f"Expected {len(ports)} active snapshot log files, found {log_files}"
            )

        with allure.step("Generate tech-support"):
            system = nv_command.system
            system.techsupport.action_generate(test_name=test_name)
            system.techsupport.extract_techsupport_files(engines.dut)
            techsupport_snapshot_files = system.techsupport.get_techsupport_files_list(
                engines.dut, f'log/swss-ibv0'
            )

        with allure.step("Parse and verify snapshot files"):
            for asic_id, port_name in ports.items():
                snapshot_basename = _snapshot_active_file(asic_id, return_basename=True)
                snapshot_file = next((path for path in log_files
                                      if path.rsplit('/', 1)[-1] == snapshot_basename), None)
                assert snapshot_file, f"No snapshot file for asic {asic_id} (port {port_name})"
                assert f'{snapshot_basename}.gz' in techsupport_snapshot_files, (
                    f"Snapshot file {snapshot_basename} not found in tech-support"
                )
                parsed_events = _read_snapshot_file_events(engines.dut, snapshot_file)
                _verify_snapshot_event(
                    parsed_events[-1], asic_id, port_name, local_ports[port_name], snapshot_file)
                assert len(parsed_events) == lines_before[asic_id] + 1, (
                    f"ASIC {asic_id}: expected {lines_before[asic_id] + 1} data lines, "
                    f"got {len(parsed_events)} in {snapshot_file}"
                )

    finally:
        if system is not None and system.techsupport.file_name:
            with allure.step("Clean up: tech-support bundle"):
                system.techsupport.cleanup(engines.dut)
                system.techsupport.files.file_name[system.techsupport.file_name].action_delete()


@pytest.mark.fae
@pytest.mark.timeout(10 * MINUTE, func_only=True)
@pytest.mark.disable_loganalyzer
def test_mlid_counters_under_stress(engines, devices, nv_command, random_api):
    """
    Verify MLID snapshot rate limit drops events beyond 100 per ASIC per minute.

    1. Select one ASIC.
    2. Trigger 110-120 events on ports under the selected ASIC.
    3. Verify the first 100 events are logged correctly in the snapshot.
    4. Verify rate-limit drop is logged in syslog.
    """
    event_count = MLID_RATE_LIMIT_EVENTS_PER_ASIC + random.randint(10, 20)
    with allure.step("Select one ASIC and build MLID mcra commands"):
        acp_ports = devices.dut.nvl_access_ports_list
        ports_per_asic = len(acp_ports) // devices.dut.asic_amount
        selected_asic = RandomizationTool.select_random_value(
            list(range(devices.dut.asic_amount))
        ).get_returned_value()
        asic_ports = acp_ports[selected_asic * ports_per_asic:(selected_asic + 1) * ports_per_asic]
        event_ports = [
            RandomizationTool.select_random_value(asic_ports).get_returned_value()
            for _ in range(event_count)
        ]
        local_ports = {}
        mcra_writes = []
        for port_name in event_ports:
            fw_local_port, register, value, mst_dev_name = _mlid_mcra_params(engines.dut, port_name)
            local_ports[port_name] = fw_local_port
            mcra_writes.append((mst_dev_name, register, value))

    lines_before = _count_snapshot_events_for_asic(engines.dut, selected_asic)
    logger.info("ASIC %s snapshot event count before triggering MLID events: %d", selected_asic, lines_before)

    system = System()
    start_time = datetime.strptime(
        ClockTools.get_local_time_from_show_system_date_time_output(system.datetime.show()),
        BugHandlerConst.TIMESTAMP_FORMATS[4],
    )

    with allure.step(f"Trigger {len(mcra_writes)} MLID events via mcra"):
        for mst_dev_name, register, value in mcra_writes:
            McraTool.run_commands(mst_dev_name, f'{hex(register)} {hex(value)}', engine=engines.dut)

    active_path = _snapshot_active_file(selected_asic)
    with allure.step("Verify logged MLID events are valid"):
        snapshot_events = _read_snapshot_file_events(engines.dut, active_path)
        new_events = snapshot_events[lines_before:]
        triggered_ports = set(event_ports)
        # Firmware may write events in a slightly different order than the mcra commands
        # were issued, so validate each event against its own port instead of by position.
        for parsed_event in new_events:
            event_port = str(parsed_event['nvos_port_name'])
            assert event_port in triggered_ports, (
                f"Snapshot event port {event_port} was not triggered in {active_path}"
            )
            _verify_snapshot_event(
                parsed_event, selected_asic, event_port,
                local_ports[event_port], active_path)

    with allure.step("Verify rate limit warning is logged in syslog"):
        new_lines = len(snapshot_events) - lines_before

        system.log.verify_expected_logs_by_time(
            [f'swss-ibv0{selected_asic}.*{MLID_SNAPSHOT_DROP_SYSLOG_PATTERN.pattern}'],
            engines.dut,
            start_time=start_time,
        )
        assert new_lines < len(event_ports), (f"Expected fewer snapshot lines ({new_lines}) than triggered MLID events ")


@pytest.mark.fae
@pytest.mark.timeout(15 * MINUTE, func_only=True)
@pytest.mark.disable_loganalyzer
def test_mlid_snapshot_when_log_filesystem_full(engines, devices, nv_command, random_api):
    """
    Verify new MLID snapshot events do not break the system when the snapshot log filesystem is full.

    1. Select one ASIC and one port.
    2. Fill the snapshot log filesystem almost full.
    3. Trigger more MLID events than the remaining free space can hold.
    4. Verify not all triggered events are written to the snapshot.
    5. Verify switch health and clean up the fill file.
    """
    fill_path = MLID_SSD_STRESS_FILL_PATH

    with allure.step("Select one ASIC and one port"):
        acp_ports = devices.dut.nvl_access_ports_list
        ports_per_asic = len(acp_ports) // devices.dut.asic_amount
        selected_asic = RandomizationTool.select_random_value(
            list(range(devices.dut.asic_amount))
        ).get_returned_value()
        asic_ports = acp_ports[selected_asic * ports_per_asic:(selected_asic + 1) * ports_per_asic]

    with allure.step("Measure snapshot event row size from a real event"):
        data_rows = _get_snapshot_data_rows(engines.dut, selected_asic)
        if not data_rows:
            seed_port = RandomizationTool.select_random_value(asic_ports).get_returned_value()
            _simulate_mlid_event(engines.dut, seed_port)
            data_rows = _get_snapshot_data_rows(engines.dut, selected_asic)
        assert data_rows, f"Expected at least one snapshot data row on ASIC {selected_asic}"
        snapshot_event_bytes = len(f'{data_rows[-1]}\n'.encode())

    try:
        with allure.step("Select one ASIC and build MLID mcra commands"):
            append_capacity = _fill_snapshot_fs_and_get_append_capacity(
                engines.dut, fill_path, snapshot_event_bytes, selected_asic,
            )
            events_that_fit = append_capacity // snapshot_event_bytes
            event_count = events_that_fit + 20
            events_before_pressure = len(_get_snapshot_data_rows(engines.dut, selected_asic))

            event_ports = [
                RandomizationTool.select_random_value(asic_ports).get_returned_value()
                for _ in range(event_count)
            ]
            mcra_writes = []
            for port_name in event_ports:
                _, register, value, mst_dev_name = _mlid_mcra_params(engines.dut, port_name)
                mcra_writes.append((mst_dev_name, register, value))

        with allure.step(f"Trigger {event_count} MLID events on ASIC {selected_asic}"):
            for mst_dev_name, register, value in mcra_writes:
                McraTool.run_commands(mst_dev_name, f'{hex(register)} {hex(value)}', engine=engines.dut)

        with allure.step("Verify not all triggered MLID events were written to snapshot"):
            events_after_pressure = len(_get_snapshot_data_rows(engines.dut, selected_asic))
            new_events_written = events_after_pressure - events_before_pressure
            assert new_events_written < event_count, (
                f"Expected not all triggered MLID events to be written to snapshot on ASIC {selected_asic}"
            )

        with allure.step("Verify switch is healthy"):
            nv_command.system.validate_health_status(HealthConsts.OK)

    finally:
        with allure.step("Clean up snapshot log filesystem fill file"):
            engines.dut.run_cmd(f'sudo rm -f {fill_path}')


@pytest.mark.fae
@pytest.mark.timeout(5 * MINUTE, func_only=True)
def test_mlid_counters_disabled(engines, devices, nv_command, random_api):
    """
    Verify no new snapshot lines are written when MLID event snapshot is disabled.

    1. Disable MLID event snapshot.
    2. Select port and record snapshot line count for its ASIC.
    3. Simulate MLID event.
    4. Verify snapshot line count did not increase.
    5. Clean up: unset MLID event snapshot state.
    """
    fae = Fae()
    try:
        with allure.step("Disable MLID event snapshot"):
            fae.system.mlid_event_snapshot.set(
                op_param_name=SystemConsts.STATE, op_param_value=ActionParamConsts.DISABLED, apply=True
            ).verify_result()
            output_dictionary = OutputParsingTool.parse_json_str_to_dictionary(
                fae.system.mlid_event_snapshot.show()
            ).get_returned_value()
            Tools.ValidationTool.verify_field_value_in_output(
                output_dictionary, SystemConsts.STATE, ActionParamConsts.DISABLED
            )

        with allure.step("Select port"):
            port_name = RandomizationTool.select_random_port(
                requested_ports_state=NvosConsts.LINK_STATE_UP,
                requested_ports_type=devices.dut.switch_type.lower(),
                interface_type='acp',
                dut_engine=engines.dut,
            ).get_returned_value().name
            asic_id = int(MultiPlanarTool.get_primary_asic(Fae(port_name=port_name)))
            lines_before = _count_snapshot_events_for_asic(engines.dut, asic_id)
            logger.info("ASIC %s snapshot event count before simulated event: %d", asic_id, lines_before)

        with allure.step(f"Simulate MLID event on {port_name}"):
            _simulate_mlid_event(engines.dut, port_name)

        with allure.step("Verify snapshot line count did not increase"):
            lines_after = _count_snapshot_events_for_asic(engines.dut, asic_id)
            assert lines_after == lines_before, (
                f"Expected no new snapshot lines while disabled "
                f"(before={lines_before}, after={lines_after})"
            )

    finally:
        with allure.step("Unset MLID event snapshot state"):
            fae.system.mlid_event_snapshot.unset(
                op_param=SystemConsts.STATE, apply=True
            ).verify_result()


@pytest.mark.fae
@pytest.mark.timeout(10 * MINUTE, func_only=True)
def test_mlid_counters_version_mismatch(engines, devices, nv_command, random_api):
    """
    Verify new file is opened after version mismatch.

    1. Select port.
    2. Rename existing v1 snapshot to v0 on the ASIC.
    3. Reboot.
    4. Simulate MLID event.
    5. Parse and verify new snapshot file content.
    """
    with allure.step("Select port"):
        port_name = RandomizationTool.select_random_port(
            requested_ports_state=NvosConsts.LINK_STATE_UP,
            requested_ports_type=devices.dut.switch_type.lower(),
            interface_type='acp',
            dut_engine=engines.dut,
        ).get_returned_value().name
        asic_id = int(MultiPlanarTool.get_primary_asic(Fae(port_name=port_name)))

    with allure.step(f"Rename v1 snapshot to v0 on ASIC {asic_id}"):
        v1_path = _snapshot_active_file(asic_id, format_version=CURRENT_SNAPSHOT_FORMAT_VERSION)
        v0_path = _snapshot_active_file(asic_id, format_version=0)
        assert engines.dut.run_cmd(f'sudo test -f {v1_path} && echo yes || echo no').strip() == 'yes', (
            f"Expected existing snapshot {v1_path}"
        )
        engines.dut.run_cmd(f'sudo mv {v1_path} {v0_path}')

    with allure.step("Reboot system"):
        nv_command.system.reboot.action_reboot(params='force').verify_result()

    with allure.step("Re-open mcra backdoor after reboot"):
        mst_dev_names = devices.dut.mst_dev_name
        if isinstance(mst_dev_names, str):
            mst_dev_names = (mst_dev_names, )
        for mst_dev_name in mst_dev_names:
            McraTool.run_commands(mst_dev_name, McraTool.BACKDOOR_OPEN_CMD, engine=engines.dut)

    with allure.step(f"Simulate MLID event on {port_name}"):
        fw_local_port = _simulate_mlid_event(engines.dut, port_name)

    with allure.step("Verify snapshot files after version mismatch event"):
        active_path = _snapshot_active_file(asic_id)
        snapshot_files = _list_snapshot_files(engines.dut)
        assert v0_path in snapshot_files, (
            f"Expected old-format snapshot {v0_path} to still exist, found {snapshot_files}"
        )
        assert active_path in snapshot_files, (
            f"Expected new active snapshot {active_path}, found {snapshot_files}"
        )

    with allure.step("Parse and verify new snapshot file"):
        parsed_events = _read_snapshot_file_events(engines.dut, active_path)
        _verify_snapshot_event(
            parsed_events[-1], asic_id, port_name, fw_local_port, active_path)


@pytest.mark.fae
@pytest.mark.timeout(20 * MINUTE, func_only=True)
def test_mlid_snapshot_rotation(engines, devices, nv_command, random_api):
    """
    Verify snapshot log rotation policy.

    Rotation threshold is 16 MiB (16 * 1024 * 1024 bytes), 5 files are kept per ASIC.

    1. Select one ASIC and build MLID mcra commands.
    2. Run 6 rotation cycles, each refilling active log to just below 16 MiB, triggering 10 MLID events, and verify retention.
    3. Verify snapshot file count stays at most 5 and new active log is under 16 MiB.
    """
    with allure.step("Select one ASIC and build MLID mcra commands"):
        acp_ports = devices.dut.nvl_access_ports_list
        ports_per_asic = len(acp_ports) // devices.dut.asic_amount
        selected_asic = RandomizationTool.select_random_value(
            list(range(devices.dut.asic_amount))
        ).get_returned_value()
        asic_ports = acp_ports[selected_asic * ports_per_asic:(selected_asic + 1) * ports_per_asic]
        event_ports = [
            RandomizationTool.select_random_value(asic_ports).get_returned_value()
            for _ in range(10)
        ]
        mcra_writes = []
        for port_name in event_ports:
            _, register, value, mst_dev_name = _mlid_mcra_params(engines.dut, port_name)
            mcra_writes.append((mst_dev_name, register, value))
        seed_port = event_ports[0]

    original_timer_override = _logrotate_timer_override(engines.dut)

    try:
        with allure.step("Set logrotate.timer to run every minute"):
            _logrotate_timer_override(
                engines.dut, '[Timer]\nOnCalendar=*-*-* *:*:00\n',
            )

        for attempt in range(1, 6 + 1):
            with allure.step(
                f"Rotation attempt {attempt}/6: "
                f"fill active log to 200 bytes below "
                f"{MLID_LOGROTATE_SIZE_MIB} MiB, trigger 10 "
                f"MLID events, wait for rotation"
            ):
                _fill_snapshot_file_with_data_rows(
                    engines.dut,
                    selected_asic,
                    MLID_LOGROTATE_SIZE_BYTES - 200,
                    seed_port=seed_port,
                )
                for mst_dev_name, register, value in mcra_writes:
                    McraTool.run_commands(mst_dev_name, f'{hex(register)} {hex(value)}', engine=engines.dut)
                logger.info("Waiting 1 minute for logrotate.timer to check whether the rotation condition is met")
                time.sleep(1 * MINUTE)
                snapshot_files = _list_snapshot_files(engines.dut)
                asic_snapshot_files = [
                    path for path in snapshot_files if f'.asic{selected_asic}.' in path
                ]
                assert len(asic_snapshot_files) <= 5, (
                    f"Expected at most 5 snapshot files for "
                    f"ASIC {selected_asic}, found {len(asic_snapshot_files)}: {asic_snapshot_files}"
                )
                active_path = _snapshot_active_file(selected_asic)
                assert engines.dut.run_cmd(f'sudo test -f {active_path} && echo yes || echo no').strip() == 'yes', (
                    f"Expected active snapshot {active_path} to exist"
                )
                active_size = int(engines.dut.run_cmd(f'sudo stat -c%s {active_path}').strip())
                assert active_size < MLID_LOGROTATE_SIZE_BYTES, (
                    f"Expected new active log < {MLID_LOGROTATE_SIZE_MIB} MiB "
                    f"({MLID_LOGROTATE_SIZE_BYTES} bytes), got {active_size}"
                )

    finally:
        with allure.step("Restore logrotate.timer"):
            _logrotate_timer_override(engines.dut, original_timer_override, validate=False)


@disabled_access_ports
@pytest.mark.nmx
@pytest.mark.timeout(45 * MINUTE, func_only=True)
def test_mlid_counters_basic_flow_with_traffic(engines, devices, random_api, has_loopbox, standalone_system, setup_name,
                                               ansible_inventory_file):
    """
    Verify MLID snapshot when a port is brought down during NCCL traffic.

    1. Enable cluster and configure NMX.
    2. Install NCCL tests and start all_reduce_perf traffic in the background.
    3. Set a port link state down while traffic is still running.
    4. Wait for traffic to finish and verify snapshot file.
    5. Restore port state.
    """
    output_format = OutputFormat.json
    port_name = None
    port_down_applied = False

    if standalone_system:
        pytest.skip(f"Skipping test - Standalone system, traffic not supported.")
    try:
        with allure.step("Enable Cluster"):
            cluster = Cluster()
            logger.info("Setting cluster state to enabled")
            ClusterTools.start_cluster(cluster, setup_name, output_format, devices=devices)

        with allure.step("Wait for all access ports to be up"):
            # The NCCL playbooks fail if any access port is still down when they run.
            wait_for_ports_state(devices, devices.dut.nvl_access_ports_list, 'acp',
                                 NvosConsts.LINK_STATE_UP)

        with allure.step("Check cluster status"):
            status_check = AnsiblePlaybooksTool.run_playbook_by_key(
                Ansible.STATUS_HEALTH,
                ansible_inventory_file,
                {}
            )

        with allure.step("Configure NMX"):
            configure_nmx_status = AnsiblePlaybooksTool.run_playbook_by_key(
                Ansible.SOFTWARE_CONFIGURE_SWITCH,
                ansible_inventory_file,
                {}
            )

        with allure.step("Install NCCL tests"):
            install_nccl_status = AnsiblePlaybooksTool.run_playbook_by_key(
                Ansible.TESTS_INSTALL_NCCL,
                ansible_inventory_file,
                {}
            )

        with allure.step("Verify setup playbooks passed"):
            assert_test_results(status_check, configure_nmx_status, install_nccl_status)

        with allure.step("Select a link-up port to bring down during traffic"):
            asic_id = 0
            acp_ports = devices.dut.nvl_access_ports_list
            ports_per_asic = len(acp_ports) // devices.dut.asic_amount
            asic_ports = acp_ports[asic_id * ports_per_asic:(asic_id + 1) * ports_per_asic]
            up_port_names = {
                port.name for port in RandomizationTool.select_random_ports(
                    requested_ports_state=NvosConsts.LINK_STATE_UP,
                    requested_ports_type=devices.dut.switch_type.lower(),
                    interface_type='acp',
                    dut_engine=engines.dut,
                    num_of_ports_to_select=0,
                ).verify_result()
            }
            link_up_asic_ports = [port for port in asic_ports if port in up_port_names]
            assert link_up_asic_ports, f"No link-up ports on acp1-{ports_per_asic}"
            port_name = RandomizationTool.select_random_value(link_up_asic_ports).get_returned_value()
            selected_port = Port(port_name)
            expected_local_port, _, _, _ = _mlid_mcra_params(engines.dut, port_name)

        with allure.step("Running NCCL traffic (all_reduce_perf) with port down mid-run"):
            # Workaround for the known NCCL hang: disable util-centric NVLink scheduling.
            nccl_workaround_env = 'NCCL_NVLINK_UTIL_CENTRIC_SCHED_ENABLE=0'
            nccl_cmd = Ansible.get_playbook_command(
                Ansible.TESTS_EXECUTE_NCCL, ansible_inventory_file, {}, setup_name
            )
            nccl_cmd = f"export {nccl_workaround_env}; {nccl_cmd} -e {nccl_workaround_env}"
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                traffic_future = executor.submit(
                    AnsiblePlaybooksTool.run_playbook_command,
                    nccl_cmd,
                )
                time.sleep(120)
                assert not traffic_future.done(), (
                    "NCCL playbook already finished before the port was set down, so nothing was "
                    "brought down mid-traffic. Either the run failed before traffic started or it "
                    "is shorter than the wait above - check the playbook output."
                )
                lines_before = _count_snapshot_events_for_asic(engines.dut, asic_id)
                with allure.step(f"Set {port_name} link state down during traffic"):
                    selected_port.interface.link.state.set(
                        op_param_name=NvosConsts.LINK_STATE_DOWN,
                        apply=True,
                        ask_for_confirmation=True,
                    ).verify_result()
                    port_down_applied = True
                # The port down interrupts the collective, so the playbook is expected to fail and
                # its status is not asserted.
                traffic_future.result()

        with allure.step("Verify MLID snapshot file during traffic"):
            active_path = _snapshot_active_file(asic_id)
            assert active_path in _list_snapshot_files(engines.dut, active_only=True), (
                f"Expected snapshot file {active_path} after port down during traffic"
            )
            parsed_events = _read_snapshot_file_events(engines.dut, active_path)
            new_events = parsed_events[lines_before:]
            assert len(new_events) >= 1, (
                f"Expected at least one new MLID snapshot line on ASIC {asic_id} after port down, "
                f"before={lines_before}, after={len(parsed_events)}"
            )
            port_events = [
                event for event in new_events
                if str(event['nvos_port_name']) == str(port_name)
            ]
            assert port_events, (
                f"Expected MLID snapshot event for {port_name} after port down on ASIC {asic_id}"
            )
            _verify_snapshot_event(
                port_events[-1], asic_id, port_name, expected_local_port, active_path)

    finally:
        if port_down_applied and port_name:
            with allure.step(f"Restore {port_name} link state up"):
                port = Port(port_name)
                port.interface.link.state.set(
                    op_param_name=NvosConsts.LINK_STATE_UP,
                    apply=True,
                    ask_for_confirmation=True,
                ).verify_result()
                port.interface.wait_for_port_state(NvosConsts.LINK_STATE_UP).verify_result()


def _fill_snapshot_fs_and_get_append_capacity(engine, fill_path, snapshot_event_bytes, asic_id):
    """
    Fill the snapshot log filesystem until df reports no free blocks, then return how many
    snapshot rows can still be appended.
    """
    with allure.step("Fill snapshot log filesystem until almost full"):
        engine.run_cmd(f'sudo rm -f {fill_path}')
        avail_bytes = int(
            engine.run_cmd(f'df -B1 --output=avail {SNAPSHOT_LOG_DIR} | tail -1').strip()
        )
        assert avail_bytes > snapshot_event_bytes, (
            f"Need more than one snapshot event ({snapshot_event_bytes}B) free to run "
            f"filesystem-full test, avail={avail_bytes}B"
        )
        engine.run_cmd(
            f'sudo fallocate -l {avail_bytes} {fill_path} 2>/dev/null || '
            f'sudo dd if=/dev/zero of={fill_path} bs=64M 2>/dev/null || true'
        )
        engine.run_cmd(
            f'sudo bash -c "dd if=/dev/zero bs=512 2>/dev/null >> {fill_path}" || true'
        )

    path = _snapshot_active_file(asic_id)
    block_size = int(engine.run_cmd(f'sudo stat -f -c %s {path}').strip())
    file_size = int(engine.run_cmd(f'sudo stat -c%s {path}').strip())
    df_avail = int(
        engine.run_cmd(f'df -B1 --output=avail {SNAPSHOT_LOG_DIR} | tail -1').strip()
    )
    assert df_avail < snapshot_event_bytes, (
        f"Expected snapshot log filesystem to be full after fill, df avail={df_avail}B"
    )
    used_in_last_block = file_size % block_size
    slack_in_last_block = 0 if used_in_last_block == 0 else block_size - used_in_last_block
    append_capacity = df_avail + slack_in_last_block
    return append_capacity


def _count_snapshot_events_for_asic(engine, asic_id):
    active_path = _snapshot_active_file(asic_id)
    assert active_path in _list_snapshot_files(engine, active_only=True), (
        f"Expected active snapshot {active_path} to exist"
    )
    return len(_get_snapshot_data_rows(engine, asic_id))


def _get_fae_port_mlid_fields(engine, port_name):
    """Return FW local-port and MST device from nv show fae interface <port>."""
    output = OutputParsingTool.parse_show_interface_output_to_dictionary(
        Fae(port_name=port_name).interface.show(dut_engine=engine)
    ).get_returned_value()

    primary_asic_device = output.get(IbInterfaceConsts.PRIMARY_ASIC_DEVICE)
    if isinstance(primary_asic_device, dict):
        mst_dev_name = primary_asic_device.get(IbInterfaceConsts.PRIMARY_ASIC_DEVICE, "")
        fw_local_port = primary_asic_device.get(IbInterfaceConsts.LOCAL_PORT)
    else:
        mst_dev_name = primary_asic_device
        fw_local_port = output.get(IbInterfaceConsts.LOCAL_PORT)

    if not mst_dev_name:
        mst_dev_name = output.get(IbInterfaceConsts.PRIMARY_ASIC_DEVICE, "")
    fw_local_port = fw_local_port if fw_local_port is not None else output.get(IbInterfaceConsts.LOCAL_PORT)

    assert mst_dev_name, f"primary-asic-device not found for {port_name}: {output}"
    assert fw_local_port is not None, f"local-port not found for {port_name}: {output}"
    return int(fw_local_port), str(mst_dev_name)


def _mlid_mcra_params(engine, port_name):
    """Return mcra register details for an MLID notification on the given port."""
    fw_local_port, mst_dev_name = _get_fae_port_mlid_fields(engine, port_name)
    register_index, bit_index = divmod(fw_local_port, 32)
    assert register_index < 5, (
        f'local-port {fw_local_port} exceeds 160 supported ports'
    )
    register = 0x228514 + register_index * 4
    value = 1 << bit_index
    return fw_local_port, register, value, mst_dev_name


def _get_snapshot_data_rows(engine, asic_id):
    """Return valid snapshot data rows, skipping the header and malformed lines."""
    path = _snapshot_active_file(asic_id)
    output = engine.run_cmd(f'sudo cat {path} 2>/dev/null') or ''
    data_rows = []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith('notification_timestamp_ns|'):
            continue
        if len(line.split('|')) == SNAPSHOT_EXPECTED_FIELD_COUNT:
            data_rows.append(line)
    return data_rows


def _fill_snapshot_file_with_data_rows(engine, asic_id, target_bytes, seed_port):
    """
    Extend the active snapshot with duplicated real data rows until size >= target_bytes.
    """
    path = _snapshot_active_file(asic_id)
    data_rows = _get_snapshot_data_rows(engine, asic_id)
    if not data_rows:
        _simulate_mlid_event(engine, seed_port)
        for _ in range(12):
            data_rows = _get_snapshot_data_rows(engine, asic_id)
            if data_rows:
                break
            time.sleep(5)
    sample_row = data_rows[-1] if data_rows else None
    assert sample_row, (
        f"Expected at least one data row in {path} after seed MLID event on {seed_port}"
    )

    current_size = int(engine.run_cmd(f'sudo stat -c%s {path}').strip())
    if current_size >= target_bytes:
        return current_size

    line_b64 = base64.b64encode(f'{sample_row}\n'.encode()).decode()
    engine.run_cmd(
        "sudo python3 - <<'PY'\n"
        "import base64\n"
        f"path = {path!r}\n"
        f"target = {target_bytes}\n"
        f"line = base64.b64decode({line_b64!r})\n"
        "chunk = line * 1000\n"
        "with open(path, 'ab') as snapshot_file:\n"
        "    while snapshot_file.tell() < target:\n"
        "        if snapshot_file.tell() + len(chunk) <= target:\n"
        "            snapshot_file.write(chunk)\n"
        "        else:\n"
        "            snapshot_file.write(line)\n"
        "PY\n"
    )
    new_size = int(engine.run_cmd(f'sudo stat -c%s {path}').strip())
    return new_size


def _simulate_mlid_event(engine, port_name):
    """Trigger an MLID notification for the given port via mcra."""
    fw_local_port, register, value, mst_dev_name = _mlid_mcra_params(engine, port_name)
    with allure.step(
        f"Simulate MLID event on {port_name} via mcra "
        f"(fw local-port {fw_local_port}, register {hex(register)}, "
        f"value {hex(value)}, {mst_dev_name})"
    ):
        McraTool.run_commands(mst_dev_name, f'{hex(register)} {hex(value)}', engine=engine)
    return fw_local_port


def _logrotate_timer_override(engine, content=None, validate=True):
    """Read or set logrotate timer override file content on the DUT."""
    if content is None:
        assert engine.run_cmd(
            f'sudo test -f {MLID_LOGROTATE_TIMER_OVERRIDE_PATH} && echo yes || echo no'
        ).strip() == 'yes', f"Expected logrotate.timer override {MLID_LOGROTATE_TIMER_OVERRIDE_PATH}"
        return engine.run_cmd(f'sudo cat {MLID_LOGROTATE_TIMER_OVERRIDE_PATH}')

    if not content.endswith('\n'):
        content = f'{content}\n'
    engine.run_cmd(
        f"printf '%b' {content!r} | sudo tee {MLID_LOGROTATE_TIMER_OVERRIDE_PATH}",
        validate=validate,
    )
    engine.run_cmd(SYSTEMCTL_DAEMON_RELOAD_CMD, validate=validate)
    engine.run_cmd(MLID_LOGROTATE_TIMER_RESTART_CMD, validate=validate)


def _list_snapshot_files(engine, active_only=False):
    """Return full paths to MLID snapshot logs under /var/log/swss-ibv0/.

    When active_only is True, return only non-rotated active logs (no .gz files).
    """
    output = engine.run_cmd(f'ls -1 {SNAPSHOT_LOG_DIR}/mlid-event-snapshot.v*.asic*.log* 2>/dev/null') or ''
    files = []
    for line in output.splitlines():
        path = line.strip()
        if not path or path.startswith('total '):
            continue
        name = path.rsplit('/', 1)[-1]
        if ACTIVE_SNAPSHOT_PATTERN.match(name):
            files.append(path)
        elif not active_only and ROTATED_SNAPSHOT_PATTERN.match(name):
            files.append(path)
    return files


def _read_snapshot_file_events(engine, path):
    """Read and parse all snapshot events from a file."""
    read_cmd = f'sudo zcat {path}' if path.endswith('.gz') else f'sudo cat {path}'
    basename = path.rsplit('/', 1)[-1]
    match = ACTIVE_SNAPSHOT_PATTERN.match(basename) or ROTATED_SNAPSHOT_PATTERN.match(basename)
    if not match:
        raise ValueError(f"Unrecognized MLID snapshot filename '{basename}' (path: {path})")
    format_version = int(match.group('format_version'))
    asic_id = int(match.group('asic_id'))

    lines = [
        line.strip()
        for line in engine.run_cmd(read_cmd).splitlines()
        if line.strip()
    ]
    if len(lines) < 2:
        raise ValueError('No valid MLID snapshot event row found')

    events = []
    for line in lines[1:]:
        fields = line.split('|')
        assert len(fields) == SNAPSHOT_EXPECTED_FIELD_COUNT, (
            f'Expected {SNAPSHOT_EXPECTED_FIELD_COUNT} fields, got {len(fields)}: {line}'
        )
        counters = {
            str(index): fields[SNAPSHOT_METADATA_FIELD_COUNT + index]
            for index in range(EXPECTED_COUNTER_COUNT)
        }
        events.append({
            'format_version': format_version,
            'asic': asic_id,
            'notification_timestamp_ns': int(fields[0]),
            'collection_timestamp_ns': int(fields[1]),
            'nvos_port_name': fields[2],
            'local_port': fields[3],
            'ib_port': fields[4],
            'notification_port': fields[5],
            'counters': counters,
        })

    if not events:
        raise ValueError('Tried to parse snapshot file, no valid MLID snapshot event row found')
    return events


def _verify_snapshot_event(parsed, expected_asic, expected_port, expected_local_port, filename):
    """Validate a single parsed snapshot event against expectations."""
    Tools.ValidationTool.verify_field_value_in_output(
        parsed, 'format_version', CURRENT_SNAPSHOT_FORMAT_VERSION
    ).verify_result()
    Tools.ValidationTool.verify_field_value_in_output(
        parsed, 'asic', int(expected_asic)
    ).verify_result()
    Tools.ValidationTool.verify_field_value_in_output(
        parsed, 'nvos_port_name', expected_port
    ).verify_result()
    Tools.ValidationTool.verify_field_value_in_output(
        parsed, 'local_port', expected_local_port
    ).verify_result()

    collection_ts = parsed['collection_timestamp_ns']
    notification_ts = parsed['notification_timestamp_ns']
    assert collection_ts >= notification_ts, (
        f"collection_timestamp_ns must be >= notification_timestamp_ns in {filename}"
    )
    delta = collection_ts - notification_ts
    assert delta <= 100 * 10**6, (
        f"Timestamp delta {delta}ns exceeds max expected value in {filename}"
    )

    counters = parsed['counters']
    assert len(counters) == EXPECTED_COUNTER_COUNT, (
        f"Expected {EXPECTED_COUNTER_COUNT} counters, got {len(counters)} in {filename}"
    )


def _snapshot_active_file(asic_id, format_version=CURRENT_SNAPSHOT_FORMAT_VERSION, return_basename=False):
    basename = f'mlid-event-snapshot.v{format_version}.asic{asic_id}.log'
    return basename if return_basename else f'{SNAPSHOT_LOG_DIR}/{basename}'
