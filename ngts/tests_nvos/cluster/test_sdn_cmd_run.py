import json
import logging
import random
import re
import string
import shlex
import time

import pytest

from ngts.nvos_tools.infra.FilesTool import FilesTool
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.nmx.Cluster import Cluster
from ngts.nvos_tools.nmx.Sdn import Sdn
from ngts.nvos_tools.system.System import System
from ngts.nvos_constants.constants_nvos import ApiType, NvosConst, SdnCmdConsts
from ngts.nvos_tools.infra.RandomizationTool import RandomizationTool, ALL_ASCII
from ngts.tools.test_utils import allure_utils as allure
from ngts.tests_nvos.cluster.cluster_tools import ClusterTools, disabled_access_ports
from ngts.tests_nvos.cluster.cluster_consts import ClusterConsts
from ngts.tests_nvos.constants import MINUTE
from ngts.tests_nvos.system.syslog.test_system_syslog import INCOMPLETE_COMMAND, IS_TOO_SHORT

logger = logging.getLogger()

SDN_CMD_MAX_BYTES = 1024
SDN_CMD_FILE_NAME_RE = re.compile(r"sdn-cmd-([^-]+)-(.+)\.json\Z")
ROTATION_POLICY_MAX_FILES_TO_KEEP = 20
ROTATION_POLICY_MAX_SIZE_BYTES = 20 * 1024 * 1024  # 20 MiB


@pytest.mark.nmx
@pytest.mark.timeout(10 * MINUTE, func_only=True)
def test_sdn_cmd_rejection(engines, devices, has_loopbox, random_api):
    """
    Verify sdn cmd rejection when cluster is disabled or NMX-C is not up.

    1. Ensure cluster is disabled.
    2. Run sdn cmd and verify rejection with cluster-not-enabled error.
    3. Enable cluster without waiting for NMX-C up, run sdn cmd, verify rejection with gRPC-down error.
    4. Clean up: disable cluster and wait for nmxc-conn down.
    """
    cluster = Cluster()
    sdn = Sdn()
    sdn_cmd_str = _get_sdn_cmd_command()
    try:
        with allure.step("Ensure cluster disabled"):
            cluster.unset(apply=True).verify_result()
            ClusterTools.wait_for_apps_to_be_in_wanted_state(
                cluster, cluster_expected_state="disabled", nmx_c_expected_state="down"
            )

        time.sleep(30)

        with allure.step(f"Run sdn cmd {sdn_cmd_str!r} while cluster is disabled"):
            sdn.action_run_cmd(sdn_cmd_str).verify_result(
                should_succeed=False,
                expected_value=ClusterConsts.RESET_FACTORY_CLUSTER_DISABLED[random_api],
            )

        with allure.step(f"Run sdn cmd {sdn_cmd_str!r} after enabling cluster (no wait for NMX-C up)"):
            cluster.set(op_param_name="state", op_param_value=NvosConst.ENABLED, apply=True)
            sdn.action_run_cmd(sdn_cmd_str).verify_result(
                should_succeed=False,
                expected_value=ClusterConsts.RESET_FACTORY_NMX_CONN_DISABLED[random_api],
            )
    finally:
        with allure.step("Clean up: disable cluster and wait for nmxc-conn down"):
            cluster.unset(apply=True).verify_result()
            ClusterTools.wait_for_apps_to_be_in_wanted_state(
                cluster, cluster_expected_state="disabled", nmx_c_expected_state="down"
            )


@disabled_access_ports
@pytest.mark.dut_reboot
@pytest.mark.nmx
@pytest.mark.timeout(20 * MINUTE, func_only=True)
def test_sdn_get_cmd(
    engines, devices, has_loopbox, setup_name, standalone_system, random_api,
):
    """
    Verify generic sdn get cmd without/with file option.

    1. Pick get command randomly.
    2. Enable cluster; wait for nmxc-conn up.
    3. Enable nmx-controller cluster-app manager.
    4. Run sdn cmd without file option.
    5. Run sdn cmd with file option.
    6. Check stdout without file option matches generated file.
    7. Reboot.
    8. Generate tech support.
    9. Check file exists in tech support and on switch.
    10. Clean up: delete generated files.
    11. Clean up: restore nmx-controller cluster-app manager.
    12. Clean up: Disable cluster; wait for nmxc-conn down.

    For ``help`` only check command runs and returns output.
    """
    cluster = _cluster_ready_for_sdn_cmd(setup_name, engines.dut)
    sdn = Sdn()
    sdn_cmd_str = _get_sdn_cmd_command()
    logger.info("chosen get command: %s", sdn_cmd_str)
    system = None
    is_help = sdn_cmd_str == SdnCmdConsts.HELP
    try:
        if is_help:
            with allure.step("Run sdn help"):
                stdout_no_file = sdn.action_run_cmd(sdn_cmd_str).verify_result(should_succeed=True)
            n_lines = len(stdout_no_file.splitlines())
            assert n_lines > 10, "expected more lines for help, got %d" % (n_lines,)
        else:

            with allure.step("Run sdn cmd without file option"):
                stdout_no_file = sdn.action_run_cmd(sdn_cmd_str).verify_result(should_succeed=True)

            with allure.step("Run sdn cmd with file option"):
                cmd_with_file = f"--file {sdn_cmd_str}"
                stdout_with_file = sdn.action_run_cmd(cmd_with_file).verify_result(should_succeed=True)
                json_with_file = _json_response_for_compare(stdout_with_file)
                assert "returnCode" in json_with_file, (
                    "Expected returnCode in output with file option. Got: %r" % (json_with_file,)
                )
                generated_file_basename = json_with_file.get("generatedFile")
                assert generated_file_basename, (
                    "Expected generatedFile in output with file option. Got: %r" % (json_with_file,)
                )
                assert SDN_CMD_FILE_NAME_RE.fullmatch(generated_file_basename), (
                    "generatedFile must look like sdn-cmd-<host>-<timestamp>.json. Got: %r"
                    % (generated_file_basename,)
                )

            with allure.step("Verify file was created"):
                files = sdn.cmd_files.get_files(dut_engine=engines.dut)
                assert len(files) == 1, "SDN cmd files list expected to contain exactly one file."
                file_basename = list(files)[0]
                assert file_basename == generated_file_basename, (
                    "Listed file %r should match generatedFile %r from action JSON"
                    % (file_basename, generated_file_basename)
                )

            with allure.step("Verify file contains <sdn-cmd-str> and response"):
                file_path_on_dut = files[file_basename]["path"]
                file_content = engines.dut.run_cmd(
                    f"sudo cat {shlex.quote(file_path_on_dut)}; echo",
                    validate=True,
                ).strip()
                if TestToolkit.tested_api == ApiType.NVUE:
                    show_file_content = "\n".join(
                        sdn.cmd_files.file_name[file_basename].show_file(exit_cmd="q", dut_engine=engines.dut).strip().splitlines()[2:-2]).strip()
                    assert show_file_content in file_content, (
                        "show sdn cmd file output should be a substring (file is too big and need to scroll down) of the file content."
                        "show_file : %r; file : %r" % (show_file_content[:500], file_content[:500])
                    )
                assert cmd_with_file in file_content, (
                    f"File should contain {cmd_with_file!r} (sdn cmd with --file). Got: {file_content[:200]!r}..."
                )
                from_stdout = _json_response_for_compare(stdout_no_file)
                from_file = _json_response_for_compare(file_content, unwrap_cmd_response=True)
                assert from_stdout == from_file, (
                    "SDN response JSON mismatch (after ignoring transactionId):\n"
                    "%r\nvs\n%r" % (from_stdout, from_file)
                )

            with allure.step("Reboot"):
                System().reboot.action_reboot(params="force", engine=engines.dut).verify_result()

            with allure.step("Generate tech support"):
                system = System()
                tech_support_folder, _ = system.techsupport.action_generate()
                system.techsupport.extract_techsupport_files(engines.dut)

            with allure.step("Check file exists in tech support and on switch"):
                files_on_switch = sdn.cmd_files.get_files(dut_engine=engines.dut)
                techsupport_files = system.techsupport.get_techsupport_files_list(
                    engines.dut, "log/nmxc-sdn-cmd/outputs"
                )
                assert generated_file_basename in files_on_switch, (
                    f"File {generated_file_basename!r} should exist on switch after reboot. "
                    f"Got : {list(files_on_switch)}"
                )
                expected_gz = "{}.gz".format(generated_file_basename)
                assert expected_gz in techsupport_files, (
                    "Expect {expected!r} in tech-support {folder!r}. Got: {got}".format(
                        expected=expected_gz,
                        folder="log/nmxc-sdn-cmd/outputs",
                        got=techsupport_files,
                    )
                )

    finally:
        if system is not None and system.techsupport.file_name:
            with allure.step("Clean up: tech-support bundle"):
                system.techsupport.cleanup(engines.dut)
                system.techsupport.files.file_name[system.techsupport.file_name].action_delete()
        if not is_help:
            with allure.step("Clean up: delete sdn cmd file"):
                sdn.cmd_files.delete_all_existing_files(engine=engines.dut).verify_result()
                sdn.cmd_files.verify_show_files_output(dut_engine=engines.dut)

        with allure.step("Clean up: Disable cluster; wait for nmxc-conn down"):
            cluster.unset(apply=True).verify_result()
            ClusterTools.wait_for_apps_to_be_in_wanted_state(
                cluster, cluster_expected_state="disabled", nmx_c_expected_state="down"
            )


@disabled_access_ports
@pytest.mark.nmx
@pytest.mark.timeout(15 * MINUTE, func_only=True)
def test_sdn_set_cmd(engines, devices, has_loopbox, setup_name, standalone_system, random_api):
    """
    Verify generic sdn set cmd works as expected.

    1. Pick a set command randomly.
    2. Enable cluster; wait for nmxc-conn up.
    3. Run the chosen command with the appropriate arguments.
    4. Revert back to original value/state.
    5. Clean up: disable cluster; wait for nmxc-conn down.
    """
    cluster = _cluster_ready_for_sdn_cmd(setup_name, engines.dut)
    sdn = Sdn()
    chosen = _get_sdn_cmd_command(is_set_cmd=True, standalone_system=standalone_system)
    logger.info("chosen set command: %s", chosen)
    try:
        if chosen == SdnCmdConsts.CREATE_PARTITION:
            with allure.step(f"Run {SdnCmdConsts.GPU_INFO_LIST} and pick a free GPU"):
                gpu_info_stdout = sdn.action_run_cmd(SdnCmdConsts.GPU_INFO_LIST).verify_result(
                    should_succeed=True
                )
                gpu_locations = _free_gpu_list(gpu_info_stdout)
                if not gpu_locations:
                    pytest.skip(f"{SdnCmdConsts.GPU_INFO_LIST} has no free GPU")
                loc_create = random.choice(gpu_locations)

            partition_name = ClusterConsts.CREATED_PARTITION_NAME + RandomizationTool.get_random_string(length=10)

            with allure.step(f"Run {SdnCmdConsts.CREATE_PARTITION} -n with -l {loc_create}"):
                cmd = f"{SdnCmdConsts.CREATE_PARTITION} -n {partition_name} -l {loc_create}"
                sdn.action_run_cmd(cmd).verify_result(should_succeed=True)

            with allure.step(f"Verify {loc_create} in non-default partition via {SdnCmdConsts.GPU_INFO_LIST}"):
                after_create = sdn.action_run_cmd(SdnCmdConsts.GPU_INFO_LIST).verify_result(
                    should_succeed=True
                )
                non_free = _non_free_gpu_list(after_create)
                assert any(loc == loc_create for loc, _ in non_free), (
                    "After create partition GPU %r should be in _non_free_gpu_list; pairs=%r" % (loc_create, non_free))

            with allure.step(f"Run {SdnCmdConsts.DELETE_PARTITION}"):
                cmd = f"{SdnCmdConsts.DELETE_PARTITION} -n {partition_name}"
                sdn.action_run_cmd(cmd).verify_result(should_succeed=True)

            with allure.step(f"Verify {loc_create} in default partition via {SdnCmdConsts.GPU_INFO_LIST}"):
                after_delete = sdn.action_run_cmd(SdnCmdConsts.GPU_INFO_LIST).verify_result(
                    should_succeed=True
                )
                free_locations = _free_gpu_list(after_delete)
                assert loc_create in free_locations, (
                    "After delete partition GPU %r should be in _free_gpu_list; free_locations=%r"
                    % (loc_create, free_locations))

        elif chosen == SdnCmdConsts.REMOVE_GPUS_FROM_PARTITION:
            with allure.step(f"Run {SdnCmdConsts.GPU_INFO_LIST} and pick a GPU in a non-default partition"):
                gpu_info_stdout = sdn.action_run_cmd(SdnCmdConsts.GPU_INFO_LIST).verify_result(
                    should_succeed=True
                )
                loc_partition_pairs = _non_free_gpu_list(gpu_info_stdout)
                if not loc_partition_pairs:
                    pytest.skip(f"{SdnCmdConsts.GPU_INFO_LIST} has no GPU in a non-default partition")
                loc_str, partition_id = random.choice(loc_partition_pairs)

            with allure.step(f"Run {SdnCmdConsts.REMOVE_GPUS_FROM_PARTITION}"):
                cmd = f"{SdnCmdConsts.REMOVE_GPUS_FROM_PARTITION} -i {partition_id} -l {loc_str}"
                sdn.action_run_cmd(cmd).verify_result(should_succeed=True)

            with allure.step(f"Verify {loc_str} in default partition via {SdnCmdConsts.GPU_INFO_LIST}"):
                after_remove = sdn.action_run_cmd(SdnCmdConsts.GPU_INFO_LIST).verify_result(
                    should_succeed=True
                )
                free_locations = _free_gpu_list(after_remove)
                assert loc_str in free_locations, (
                    "After remove GPU %r should be in _free_gpu_list; free_locations=%r"
                    % (loc_str, free_locations))

            with allure.step(f"Run {SdnCmdConsts.ADD_GPUS_TO_PARTITION}"):
                cmd = f"{SdnCmdConsts.ADD_GPUS_TO_PARTITION} -i {partition_id} -l {loc_str}"
                sdn.action_run_cmd(cmd).verify_result(should_succeed=True)

            with allure.step(f"Verify {loc_str} in partition {partition_id} via {SdnCmdConsts.GPU_INFO_LIST}"):
                after_add = sdn.action_run_cmd(SdnCmdConsts.GPU_INFO_LIST).verify_result(
                    should_succeed=True
                )
                non_free = _non_free_gpu_list(after_add)
                assert (loc_str, partition_id) in non_free, (
                    "After add (%r, %s) should be in _non_free_gpu_list; pairs=%r" % (loc_str, partition_id, non_free))

        elif chosen == SdnCmdConsts.FACTORY_RESET:
            with allure.step(f"Run {SdnCmdConsts.FACTORY_RESET} -y"):
                sdn.action_run_cmd(f"{SdnCmdConsts.FACTORY_RESET} -y").verify_result(should_succeed=True)

        else:
            _cfg_key = "fm_config:GFM_WAIT_TIMEOUT_SEC"
            with allure.step(f"Read current value of {_cfg_key}"):
                read_out = sdn.action_run_cmd(f"{SdnCmdConsts.STATIC_CONFIG} -k {_cfg_key}").verify_result(
                    should_succeed=True
                )
                old_val = _parse_static_config_value(read_out)
            other_val = "600" if old_val != "600" else "400"
            with allure.step(f"Change {_cfg_key} value"):
                sdn.action_run_cmd(
                    f"{SdnCmdConsts.SET_STATIC_CONFIG} -k {_cfg_key}:{other_val}"
                ).verify_result(should_succeed=True)
            with allure.step(f"Verify {_cfg_key} reads back as {other_val}"):
                read_after_change = sdn.action_run_cmd(
                    f"{SdnCmdConsts.STATIC_CONFIG} -k {_cfg_key}"
                ).verify_result(should_succeed=True)
                val_after_change = _parse_static_config_value(read_after_change)
                assert val_after_change == other_val, (
                    f"expected value {other_val!r} after set, got {val_after_change!r}"
                )
            with allure.step(f"Change {_cfg_key} back to original value"):
                sdn.action_run_cmd(
                    f"{SdnCmdConsts.SET_STATIC_CONFIG} -k {_cfg_key}:{old_val}"
                ).verify_result(should_succeed=True)
            with allure.step(f"Verify {_cfg_key} reads back as original {old_val!r}"):
                read_after_restore = sdn.action_run_cmd(
                    f"{SdnCmdConsts.STATIC_CONFIG} -k {_cfg_key}"
                ).verify_result(should_succeed=True)
                val_after_restore = _parse_static_config_value(read_after_restore)
                assert val_after_restore == old_val, (
                    f"expected value {old_val!r} after restore, got {val_after_restore!r}"
                )

    finally:
        with allure.step("Clean up: Disable cluster; wait for nmxc-conn down"):
            cluster.unset(apply=True).verify_result()
            ClusterTools.wait_for_apps_to_be_in_wanted_state(
                cluster, cluster_expected_state="disabled", nmx_c_expected_state="down"
            )


@pytest.mark.nmx
@pytest.mark.disable_loganalyzer
@pytest.mark.timeout(10 * MINUTE, func_only=True)
def test_sdn_invalid_cmd(engines, devices, has_loopbox, setup_name, random_api):
    """
    Verify sdn cmd rejects bad inputs: invalid str, over 1K bytes, empty str.

    1. Start cluster and wait for nmxc-conn up.
    2. Run invalid str.
    3. Run string >1024 bytes.
    4. Run empty str.
    5. Clean up: disable cluster and wait for nmxc-conn down.
    """
    cluster = Cluster()
    sdn = Sdn()
    try:
        with allure.step("Enable cluster and wait for nmxc-conn up"):
            ClusterTools.start_cluster(cluster, setup_name)

        time.sleep(30)

        with allure.step("Verify sdn cmd rejects invalid inputs"):
            with allure.independent_step("Run with invalid str"):
                invalid_alphabet = (
                    ALL_ASCII if random_api == ApiType.OPENAPI else string.ascii_letters + string.digits
                )
                invalid_cmd = RandomizationTool.get_random_string(
                    length=random.randint(10, 30),
                    ascii_letters=invalid_alphabet,
                )
                logger.info("invalid_cmd=%r", invalid_cmd)
                sdn.action_run_cmd(invalid_cmd).verify_result(
                    should_succeed=False,
                    expected_value="INVALID_COMMAND",
                )

            with allure.independent_step("Run with string longer than 1024 bytes"):
                over_limit_length = random.randint(SDN_CMD_MAX_BYTES + 1, SDN_CMD_MAX_BYTES + 100)
                cmd_over_limit = RandomizationTool.get_random_string(
                    length=over_limit_length,
                    ascii_letters=invalid_alphabet,
                )
                sdn.action_run_cmd(cmd_over_limit).verify_result(
                    should_succeed=False,
                    expected_value="too long",
                )

            with allure.independent_step("Run with empty string"):
                sdn.action_run_cmd("").verify_result(
                    should_succeed=False,
                    expected_value=[INCOMPLETE_COMMAND, IS_TOO_SHORT],
                )
    finally:
        with allure.step("Clean up: disable cluster and wait for nmxc-conn down"):
            cluster.unset(apply=True).verify_result()
            ClusterTools.wait_for_apps_to_be_in_wanted_state(
                cluster, cluster_expected_state="disabled", nmx_c_expected_state="down"
            )


@disabled_access_ports
@pytest.mark.nmx
@pytest.mark.timeout(20 * MINUTE, func_only=True)
def test_sdn_cmd_rotation_policy(
    engines, devices, has_loopbox, setup_name, standalone_system, random_api,
):
    """
    Verify rotation policy for SDN cmd output files.

    1. Start cluster and wait for nmxc-conn up.
    2. Run sdn cmd with file option (max files to keep + 5) times.
    3. Verify only max files are kept and total size is below the threshold.
    4. Clean up: delete generated files
    5. Clean up: disable cluster and wait for nmxc-conn down.
    """
    cluster = _cluster_ready_for_sdn_cmd(setup_name, engines.dut)
    sdn = Sdn()
    sdn_cmd_str = _get_sdn_cmd_command(exclude_help=True)
    try:
        with allure.step(f"Run sdn cmd with file option {ROTATION_POLICY_MAX_FILES_TO_KEEP + 5} times"):
            for _ in range(ROTATION_POLICY_MAX_FILES_TO_KEEP + 5):
                sdn.action_run_cmd(f"--file {sdn_cmd_str}").verify_result(should_succeed=True)

        with allure.step("Verify number of files equals max files to keep"):
            files = sdn.cmd_files.get_files(dut_engine=engines.dut)
            assert len(files) == ROTATION_POLICY_MAX_FILES_TO_KEEP, (
                f"Expected {ROTATION_POLICY_MAX_FILES_TO_KEEP} files after rotation, "
                f"got {len(files)}: {list(files.keys())}"
            )

        with allure.step("Verify each file size < max size"):
            for name, row in files.items():
                size = FilesTool.get_file_size_in_bytes(engines.dut, row["path"])
                assert size < ROTATION_POLICY_MAX_SIZE_BYTES, (
                    f"File {name}: size {size} bytes >= max {ROTATION_POLICY_MAX_SIZE_BYTES} bytes"
                )
    finally:
        with allure.step("Clean up: delete sdn cmd files"):
            sdn.cmd_files.delete_all_existing_files(engine=engines.dut).verify_result()
            sdn.cmd_files.verify_show_files_output(dut_engine=engines.dut)

        with allure.step("Clean up: Disable cluster; wait for nmxc-conn down"):
            cluster.unset(apply=True).verify_result()
            ClusterTools.wait_for_apps_to_be_in_wanted_state(
                cluster, cluster_expected_state="disabled", nmx_c_expected_state="down"
            )


def _first_top_level_json_dict(blob):
    """Parse the first top-level JSON object from mixed NVUE output."""
    start = blob.find("{")
    if start == -1:
        raise ValueError("no '{' in text (preview %r)" % (blob[:200],))
    data, _end = json.JSONDecoder().raw_decode(blob, start)
    if not isinstance(data, dict):
        raise ValueError("first JSON value is not an object, got %s" % type(data).__name__)
    return data


def _parse_static_config_value(static_config_stdout):
    """Parse StaticConfig SDN JSON; return first ``configKeyVal.value`` (assumes response shape)."""
    data = _first_top_level_json_dict(static_config_stdout)
    row = data["staticConfig"]["configKey" + "Val" + "s"]["configKey" + "Val"]
    if isinstance(row, list):
        row = row[0]
    return str(row["value"])


def _json_response_for_compare(blob, *, unwrap_cmd_response=False):
    """
    Take the first top-level JSON object, parse it, and
    return a dict safe to compare across stdout vs file (omit ``serverHeader.transactionId``).

    When unwrap_cmd_response is True (--file payload), use the inner response object.
    """
    data = _first_top_level_json_dict(blob)
    if unwrap_cmd_response:
        data = data["response"]
    header = data.get("serverHeader")
    if isinstance(header, dict) and "transactionId" in header:
        data = {
            **data,
            "serverHeader": {k: v for k, v in header.items() if k != "transactionId"},
        }
    return data


def _free_gpu_list(gpu_info_list_stdout):
    """
    Parse GpuInfoList JSON and return location strings for GPUs still in the default partition (partitionId 0),
    """
    data = _first_top_level_json_dict(gpu_info_list_stdout)
    gpus = data.get("gpuInfoList", [])
    locations = []
    for g in gpus:
        if g["partitionId"]["partitionId"] == 0:
            loc = g["loc"]["location"]
            locations.append(f"{loc['chassisId']}.{loc['slotId']}.{loc['hostId']}.{g['gpuId']}")
    return locations


def _non_free_gpu_list(gpu_info_list_stdout):
    """
    Parse GpuInfoList JSON and return ``(location, partition_id)`` tuples for non-free GPUs
    """
    data = _first_top_level_json_dict(gpu_info_list_stdout)
    gpus = data.get("gpuInfoList", [])
    pairs = []
    for g in gpus:
        partition_id = g["partitionId"]["partitionId"]
        if partition_id != 0:
            loc = g["loc"]["location"]
            location_str = f"{loc['chassisId']}.{loc['slotId']}.{loc['hostId']}.{g['gpuId']}"
            pairs.append((location_str, partition_id))
    return pairs


def _get_sdn_cmd_command(is_set_cmd=False, *, standalone_system=True, exclude_help=False):
    """Return a randomly chosen get or set command string."""
    if is_set_cmd:
        return random.choice(
            SdnCmdConsts.SET_COMMAND_BASE_LIST
            if standalone_system
            else SdnCmdConsts.SET_COMMAND_NON_STANDALONE_LIST)
    commands = SdnCmdConsts.GET_COMMAND_LIST
    if exclude_help:
        commands = [c for c in commands if c != SdnCmdConsts.HELP]
    return random.choice(commands)


def _cluster_ready_for_sdn_cmd(setup_name, dut_engine):
    """Enable cluster after @disabled_access_ports preamble (not a pytest fixture: fixtures run before decorators)."""
    cluster = Cluster()
    with allure.step("Enable cluster; wait for nmxc-conn up"):
        ClusterTools.start_cluster(cluster, setup_name)
    with allure.step("Wait until nmx-controller status is ok (nv show cluster apps running)"):
        ClusterTools.wait_until_app_expected_status(
            cluster, ClusterConsts.NMX_CONTROLLER, "ok", engine=dut_engine
        )
    return cluster
