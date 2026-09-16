from fnmatch import fnmatch
from itertools import chain
from ipaddress import IPv4Address
import logging
import os
import pytest
from shlex import quote

import ptf.testutils as testutils

from tests.common.plugins.nasa_debug import NASA_DEBUG_ENTITY, NASA_DEBUG_DUMP_DIR, nasa_entity_debug_set
from tests.common.plugins.nasa_debug import get_nasa_entity_debug_enabled, get_nasa_entity_debug_file
from tests.common.plugins.nasa_debug import nasa_debuggability_enable, nasa_debuggability_disable
from tests.common.plugins.nasa_debug import get_file_size
from tests.common.plugins.nasa_debug import nasa_ct_dump_set, get_nasa_ct_dump_enabled
from tests.common.plugins.nasa_debug import NASA_CT_DUMP_FILES, NASA_MST_DUMP_PATTERNS, get_techsupport_file_list
from tests.common.config_reload import config_reload
from tests.common.dash_utils import apply_dash_configs
from packets import outbound_pl_packets
from tests.dash.conftest import get_interface_ip
import configs.privatelink_config as pl
from constants import LOCAL_PTF_INTF, LOCAL_DUT_INTF, REMOTE_DUT_INTF, REMOTE_PTF_RECV_INTF


logger = logging.getLogger(__name__)
pytestmark = [
    pytest.mark.topology("smartswitch")
]


@pytest.fixture(autouse=True)
def nasa_debug_cleanup(dpuhost):

    yield
    # for the debuggability tests, disable the debuggability on the selected DPU
    # this is the cleanup in case the debuggability test failed
    logger.info("Disable/Clean up NASA debuggability after the debuggability tests")
    nasa_debuggability_disable(dpuhost)
    # CT dump is disabled by default
    nasa_ct_dump_set(dpuhost, False)
    assert not get_nasa_ct_dump_enabled(dpuhost), \
        "Expected NASA CT dump to be disabled after cleanup"


# these tests are parametrized by the config record and packet drop, since the CLI commands are very similar
@pytest.mark.parametrize("entity", NASA_DEBUG_ENTITY)
def test_nasa_debug_enabled(dpuhost, entity):
    assert not get_nasa_entity_debug_enabled(dpuhost, entity), f"Expected {entity.value.title} to be disabled at the beginning"
    assert get_nasa_entity_debug_file(dpuhost, entity) is None, f"Expected {entity.value.title} debug file to be None at the beginning"
    debug_files = []
    # check enable and double enable
    for _ in range(2):
        # repeat 2 times
        nasa_entity_debug_set(dpuhost, entity, True)
        assert get_nasa_entity_debug_enabled(dpuhost, entity), f"Expected {entity.value.title} to be enabled both times"
        debug_file = get_nasa_entity_debug_file(dpuhost, entity)
        assert debug_file is not None, f"Expected {entity.value.title} debug file to be set"
        debug_files.append(debug_file)
    assert debug_files[0] == debug_files[1], f"Expected {entity.value.title} debug file to be same"

    # Disable the debug
    nasa_entity_debug_set(dpuhost, entity, False)
    assert not get_nasa_entity_debug_enabled(dpuhost, entity), f"Expected {entity.value.title} to be disabled after disabling"
    assert get_nasa_entity_debug_file(dpuhost, entity) is None, f"Expected {entity.value.title} debug file to be None after disabling"

    # check enable after disable
    nasa_entity_debug_set(dpuhost, entity, True)
    assert get_nasa_entity_debug_enabled(dpuhost, entity), f"Expected {entity.value.title} to be enabled"
    debug_file = get_nasa_entity_debug_file(dpuhost, entity)
    assert debug_file is not None and debug_file != debug_files[0], f"Expected {entity.value.title} debug file to be set and different from the previous one"

    # finally disable the NASA debug for the entity
    nasa_entity_debug_set(dpuhost, entity, False)
    assert not get_nasa_entity_debug_enabled(dpuhost, entity), f"Expected {entity.value.title} to be disabled after disabling"
    assert get_nasa_entity_debug_file(dpuhost, entity) is None, f"Expected {entity.value.title} debug file to be None after disabling"

    # finally check for the extra files present along the reported debug files
    debug_dir = os.path.dirname(debug_file)
    # Get the last 2 files from the debug directory, catch any extra empty files, that should not be there
    result = dpuhost.shell(f"ls {quote(debug_dir)}/* | tail -2")
    found_files = result['stdout_lines']
    assert len(found_files) == 2, f"Expected at least 2 files in the debug directory, but got {len(found_files)}"
    assert found_files[0] == debug_files[0], f"Expected the first file to be the first debug file"
    assert found_files[1] == debug_file, f"Expected the second file to be the debug file after disabling and re-enabling"


@pytest.mark.parametrize("entity", NASA_DEBUG_ENTITY)
def test_nasa_debug_disabled(dpuhost, entity):
    # expect the feature to be disabled and remain disabled after disabling
    assert not get_nasa_entity_debug_enabled(dpuhost, entity), "Expected config record to be disabled at the beginning"
    assert get_nasa_entity_debug_file(dpuhost, entity) is None, "Expected config record debug file to be None at the beginning"
    nasa_entity_debug_set(dpuhost, entity, False)
    assert not get_nasa_entity_debug_enabled(dpuhost, entity), "Expected config record to be disabled after disabling"
    assert get_nasa_entity_debug_file(dpuhost, entity) is None, "Expected config record debug file to be None after disabling"


def get_host_epoch(dpuhost):
    """This function will return the host epoch"""
    return int(dpuhost.shell("date +%s")['stdout'].strip())


def get_nasa_debug_dump_files(dpuhost):
    """This function will return the list of the files in the NASA debug dump directories"""
    files_list = dict()
    for entity in NASA_DEBUG_ENTITY:
        dump_dir = f"{NASA_DEBUG_DUMP_DIR}/{entity.value.config_key}"
        dpuhost.shell(f"mkdir -p {quote(dump_dir)}")
        files_list[entity] = set(dpuhost.shell(f"ls {quote(dump_dir)}/* 2>/dev/null || true")['stdout_lines'])
    return files_list


@pytest.fixture(scope="function")
def dash_pipeline_teardown(dpuhost):
    """
    This fixture will reload the DPU config after the test.
    """

    yield

    config_reload(dpuhost, safe_reload=True)


@pytest.fixture(scope="module", autouse=True)
def setup_npu_routes(duthost, dash_pl_config, dpu_index, dpuhosts):
    """Set up static routes on NPU to forward traffic to DPU.

    Same as test_dash_eni_counter.setup_npu_routes. These routes are required for
    PTF traffic to reach the DPU:
    - APPLIANCE_VIP -> DPU data port IP (so VxLAN packets reach the DPU)
    - VM1_PA -> VM nexthop (so return traffic works)
    - PE_PA -> PE nexthop (so PE traffic works)
    """

    dpuhost = dpuhosts[dpu_index]
    vm_nexthop_ip = get_interface_ip(duthost, dash_pl_config[LOCAL_DUT_INTF]).ip + 1
    pe_nexthop_ip = get_interface_ip(duthost, dash_pl_config[REMOTE_DUT_INTF]).ip + 1

    cmds = []
    cmds.append(f"ip route replace {pl.APPLIANCE_VIP}/32 via {dpuhost.dpu_data_port_ip}")
    cmds.append(f"ip route replace {pl.VM1_PA}/32 via {vm_nexthop_ip}")
    cmds.append(f"ip route replace {pl.PE_PA}/32 via {pe_nexthop_ip}")
    logger.info(f"Adding static routes on NPU: {cmds}")
    duthost.shell_cmds(cmds=cmds)

    yield

    cmds = []
    cmds.append(f"ip route del {pl.APPLIANCE_VIP}/32 via {dpuhost.dpu_data_port_ip}")
    cmds.append(f"ip route del {pl.VM1_PA}/32 via {vm_nexthop_ip}")
    cmds.append(f"ip route del {pl.PE_PA}/32 via {pe_nexthop_ip}")
    logger.info(f"Removing static routes from NPU: {cmds}")
    duthost.shell_cmds(cmds=cmds)


def apply_dash_pipeline_config(localhost, duthost, ptfhost, dpuhost):
    """Apply full DASH pipeline config via gNMI (same as test_dash_eni_counter common_setup_teardown).

    This must be called after NASA debuggability is enabled so that SAI configuration
    changes are captured by config-record.
    """
    # ``INBOUND_VNI_ROUTE_RULE_CONFIG`` is only programmed on Bluefield DPUs;
    # on other platforms ROUTE_RULE entries are skipped at the source.
    bluefield_route_rule_configs = []
    if 'bluefield' in dpuhost.facts['asic_type']:
        bluefield_route_rule_configs = [pl.INBOUND_VNI_ROUTE_RULE_CONFIG]

    # ``apply_dash_configs`` buckets entries by DASH table name and applies
    # them in dependency order (see ``DashPhase`` in ``tests/common/dash_utils.py``):
    # GROUP_1 (APPLIANCE) -> GROUP_2 (ROUTING_TYPE/METER_POLICY/OUTBOUND_PORT_MAP/VNET) ->
    # GROUP_3 (METER_RULE) -> GROUP_4 (TUNNEL/OUTBOUND_PORT_MAP_RANGE/ENI/ROUTE_GROUP) ->
    # GROUP_5 (ROUTE_RULE/ROUTE/VNET_MAPPING) -> GROUP_6 (ENI_ROUTE).
    apply_dash_configs(
        localhost, duthost, ptfhost, dpuhost.dpu_index,
        pl.APPLIANCE_CONFIG,
        pl.ROUTING_TYPE_PL_CONFIG,
        pl.VNET_CONFIG,
        pl.ROUTE_GROUP1_CONFIG,
        pl.METER_POLICY_V4_CONFIG,
        pl.PE_VNET_MAPPING_CONFIG,
        pl.PE_SUBNET_ROUTE_CONFIG,
        pl.VM_SUBNET_ROUTE_CONFIG,
        *bluefield_route_rule_configs,
        pl.METER_RULE1_V4_CONFIG,
        pl.METER_RULE2_V4_CONFIG,
        pl.ENI_CONFIG,
        pl.ENI_ROUTE_GROUP1_CONFIG,
    )

    logger.info("DASH pipeline config applied")


def test_nasa_debug_action(localhost, duthost, ptfhost, dpuhost, dash_pl_config, ptfadapter, dash_pipeline_teardown):
    """Test the NASA debuggability action, by sending traffic and checking the debug files.
    The test has multiple steps:
    1. Enable NASA debuggability
    2. Apply DASH pipeline config (triggers config-record capture)
    3. Record debug file paths and sizes
    4. Send a packet that triggers CA-PA entry miss drop
    5. Disable NASA debuggability
    6. Check the debug files are present and the size is increased
    7. Run the packet analyzer of the dropped packets
    8. Check the packet analyzer output and create a pcap file
    9. Check the pcap file is present, not empty, and created recently
    """

    # Enable NASA debuggability
    nasa_debuggability_enable(dpuhost)

    debug_files = {}
    for entity in NASA_DEBUG_ENTITY:
        # confirm the debug is enabled
        assert get_nasa_entity_debug_enabled(dpuhost, entity), \
            f"Expected {entity.value.title} to be enabled"
        # get the debug file name
        debug_file = get_nasa_entity_debug_file(dpuhost, entity)
        assert debug_file is not None, \
            f"Expected {entity.value.title} debug file to be set and different from the previous one"
        # save the file along with the size
        debug_files[entity] = [debug_file, get_file_size(dpuhost, debug_file)]

    apply_dash_pipeline_config(localhost, duthost, ptfhost, dpuhost)

    logger.info("Sending packet to trigger CA-PA entry miss drop")
    pkt, _ = outbound_pl_packets(dash_pl_config, "vxlan", inner_packet_type="tcp")
    ip_with_same_outbound_route_prefix = str(IPv4Address(pl.PE_CA) + 1)
    pkt["VXLAN"]["IP"].dst = ip_with_same_outbound_route_prefix
    testutils.send(ptfadapter, dash_pl_config[LOCAL_PTF_INTF], pkt, count=1)

    nasa_debuggability_disable(dpuhost)

    # check the debug files, size must increase from the original
    for entity, (debug_file, original_size) in debug_files.items():
        assert get_file_size(dpuhost, debug_file) > original_size, \
            f"Expected {entity.value.title} debug file size to increase from {original_size}"

    # Run the packet analyzer of the dropped packets
    result = dpuhost.shell(f"docker exec syncd pkt_log_analyzer {quote(debug_files[NASA_DEBUG_ENTITY.PACKET_DROP][0])}")

    # Expecting 1 packet to be dropped due to CA2PA table lookup failed
    dropped_packet_count = 0
    for line in result['stdout_lines']:
        if line.startswith('#Pkt hdr:: reason: '):
            dropped_packet_count += 1
            assert "SDN_DBG_PKT_DROP_REASON_ENCAP_CA2PA_TABLE_LOOKUP_FAILED" in line, f"Expecting the packet to be dropped due to CA2PA table lookup failed, found {line}"
            assert dropped_packet_count == 1, f"Expected 1 packet to be dropped due to CA2PA table lookup failed, found at least{dropped_packet_count}"

    # checking in the pcap file was created
    host_epoch = get_host_epoch(dpuhost)

    # expecting the packet to PCAPNG file
    pcap_filename = f"/tmp/{os.path.basename(debug_files[NASA_DEBUG_ENTITY.PACKET_DROP][0])}"
    result = dpuhost.shell(f"docker exec syncd pkt_log_analyzer {quote(debug_files[NASA_DEBUG_ENTITY.PACKET_DROP][0])} -p {quote(pcap_filename)}")

    result = dpuhost.shell(f"docker exec syncd stat -c %s,%F,%W {quote(pcap_filename)}")

    f_size, f_type, f_ctime = result['stdout'].strip().split(',')
    f_size, f_ctime = int(f_size), int(f_ctime)
    assert f_size > 0, f"Expected generated pcap file size to be greater than 0, got {f_size}"
    assert f_type == "regular file", f"Expected generated pcap regular file, got {f_type}"
    assert f_ctime >= host_epoch, f"Expected generated pcap file creation time to be no earlier than host epoch {host_epoch}, got {f_ctime}"


def test_nasa_debug_tech_support(localhost, duthost, ptfhost, dpuhost, dash_pl_config, ptfadapter, dash_pipeline_teardown):
    """This test will send traffic with NASA debuggability enabled,
       then ensure the debug files are created and present in the tech support dump.

    This test verifies:
    1. NASA debuggability creates new debug dump files when drop traffic is sent
    2. CT dump enable/disable via CLI and sentinel file
    3. show techsupport includes:
       - config-record and packet-drop debug files matching on-disk files
       - CT dump files (ct_table_log.bin.gz, ct_table_log_ipvx.bin.gz) in sai_sdk_dump/
    4. Disabling CT dump removes sentinel file
    """
    # Get the host epoch
    host_epoch = get_host_epoch(dpuhost)

    # Get the list of debug dump files before enabling debug (so enable's new file is captured)
    logger.info("Getting the list of the files in the NASA debug dump directory before the test")
    before_files_list = get_nasa_debug_dump_files(dpuhost)

    # Enable NASA debuggability (config-record + packet-drop)
    nasa_debuggability_enable(dpuhost)

    # Enable CT dump and verify sentinel file
    logger.info("Verifying CT dump is disabled by default")
    assert not get_nasa_ct_dump_enabled(dpuhost), \
        "Expected NASA CT dump to be disabled by default"

    logger.info("Enabling NASA CT dump in techsupport")
    nasa_ct_dump_set(dpuhost, True)
    assert get_nasa_ct_dump_enabled(dpuhost), \
        "Expected NASA CT dump to be enabled after enabling"

    # Apply DASH pipeline config after enabling debug so SAI config changes
    # are captured by config-record
    apply_dash_pipeline_config(localhost, duthost, ptfhost, dpuhost)

    # Send a packet that triggers CA-PA entry miss drop to create debug recording files
    logger.info("Sending packet to trigger CA-PA entry miss drop for debug recording")
    pkt, _ = outbound_pl_packets(dash_pl_config, "vxlan", inner_packet_type="tcp")
    ip_with_same_outbound_route_prefix = str(IPv4Address(pl.PE_CA) + 1)
    pkt["VXLAN"]["IP"].dst = ip_with_same_outbound_route_prefix
    testutils.send(ptfadapter, dash_pl_config[LOCAL_PTF_INTF], pkt, count=1)

    # Send outbound pass traffic to create flows in the Connection Table (for CT dump and flow dump)
    logger.info("Sending outbound traffic to create flows in the Connection Table")
    pkt, exp_pkt = outbound_pl_packets(dash_pl_config, outer_encap="vxlan", inner_packet_type="tcp")
    testutils.send(ptfadapter, dash_pl_config[LOCAL_PTF_INTF], pkt, count=5)
    if exp_pkt:
        testutils.verify_packet_any_port(ptfadapter, exp_pkt, dash_pl_config[REMOTE_PTF_RECV_INTF], timeout=10)

    # Disable NASA debuggability
    nasa_debuggability_disable(dpuhost)

    # Get the list of debug dump files after the test
    logger.info("Getting the list of the files in the NASA debug dump directory after the test")
    after_files_list = get_nasa_debug_dump_files(dpuhost)

    # Verify there is exactly one new NASA debug dump file in each folder
    logger.info(f"Verifying there is exactly one new NASA debug dump file in each folder")
    for entity in NASA_DEBUG_ENTITY:
        temp_before_files = before_files_list[entity]
        temp_after_files = after_files_list[entity]
        temp_new_files = temp_after_files - temp_before_files
        assert len(temp_new_files) == 1, f"Expected exactly one new file in {entity.value.title} folder, but got {len(temp_new_files)}: {temp_new_files}"
        new_filename = temp_new_files.pop()
        file_epoch = int(dpuhost.shell(f"stat -c %W {quote(new_filename)}")['stdout'].strip())
        assert file_epoch > host_epoch, "Expecting the nasa debug file to be created recently"

    # Generate the tech support information with --flow-dump option
    logger.info(f"Generating the tech support information to check the debug files are present")
    result = dpuhost.shell("show techsupport")
    tech_support_file = result['stdout_lines'][-1].strip()
    logger.info(f"Tech support file: {tech_support_file}")

    tech_support_file_basename = os.path.basename(tech_support_file)
    tech_support_file_basename, ext = os.path.splitext(tech_support_file_basename)
    assert ext == ".gz", "Expecting the tech support file to be a .gz file"
    tech_support_file_basename, ext = os.path.splitext(tech_support_file_basename)
    assert ext == ".tar", "Expecting the tech support file to be a .tar.gz file"

    # Check if the debug files are present in the tech support information
    # expecting the tech support file /var/dump/<name>.tar.gz
    # Example: /var/dump/sonic_dump_sonic_20250724_231154.tar.gz
    # containing <name>/log/cfg_record_\*.gz <name>/log/pkt_dump_record_\*.gz
    # Example:  sonic_dump_sonic_20250724_231154/log/cfg_record_\*.gz sonic_dump_sonic_20250724_231154/log/pkt_dump_record_\*.gz
    # The same .gz files as are in the /var/log/bluefield/sdk-dumps/config-record/ and /var/log/bluefield/sdk-dumps/packet-drop/
    logger.info(f"Comparing file lists in the tech support file and in the NASA debug dump directories")
    # Example of listing NASA debug files in the tech support file
    # tar --wildcards --sort=name -tf /var/dump/sonic_dump_sonic_20250724_231154.tar.gz sonic_dump_sonic_20250724_231154/log/cfg_record_\*.gz sonic_dump_sonic_20250724_231154/log/pkt_dump_record_\*.gz
    nasa_debug_files_in_tech_support = dpuhost.shell(f"tar --wildcards -tf {quote(tech_support_file)} "
                                                     f"{quote(tech_support_file_basename)}/log/cfg_record_*.gz "
                                                     f"{quote(tech_support_file_basename)}/log/pkt_dump_record_*.gz")['stdout_lines']

    logger.info(f"NASA debug files in the tech support file: {nasa_debug_files_in_tech_support}")
    nasa_debug_files_in_tech_support_set = {os.path.splitext(os.path.basename(file))[0] for file in nasa_debug_files_in_tech_support}
    assert len(nasa_debug_files_in_tech_support_set) == len(nasa_debug_files_in_tech_support), \
        f"Expected the same number of files in the tech support file and in the NASA debug dump directories, but got {len(nasa_debug_files_in_tech_support_set)} and {len(nasa_debug_files_in_tech_support)}"
    nasa_debug_files_in_folders_set = {os.path.basename(filename) for filename in chain(*after_files_list.values())}
    assert len(nasa_debug_files_in_folders_set) == sum(map(len, after_files_list.values())), "Expecting only unique files in NASA debug dump directories"
    assert nasa_debug_files_in_tech_support_set == nasa_debug_files_in_folders_set, \
        f"Expected the same files in the tech support file and in the NASA debug dump directories, but got {nasa_debug_files_in_tech_support_set} and {nasa_debug_files_in_folders_set}"

    # Verify CT dump files are present in sai_sdk_dump/
    logger.info("Verifying CT dump files are present in techsupport sai_sdk_dump/")
    file_list = get_techsupport_file_list(dpuhost, tech_support_file)
    sai_dump_basenames = {
        os.path.basename(f) for f in file_list
        if f.startswith("sai_sdk_dump/") and os.path.basename(f)
    }
    logger.info(f"sai_sdk_dump files found: {sorted(sai_dump_basenames)}")

    for ct_file in NASA_CT_DUMP_FILES:
        assert ct_file in sai_dump_basenames, \
            f"CT dump file '{ct_file}' expected but not found in techsupport sai_sdk_dump/. " \
            f"Found: {sorted(sai_dump_basenames)}"

    # Verify MST dump files are present in sai_sdk_dump/
    logger.info("Verifying MST dump files are present in techsupport sai_sdk_dump/")
    for mst_pattern in NASA_MST_DUMP_PATTERNS:
        matched = [f for f in sai_dump_basenames if fnmatch(f, mst_pattern)]
        assert len(matched) >= 1, \
            f"MST dump file matching '{mst_pattern}' expected but not found in techsupport sai_sdk_dump/. " \
            f"Found: {sorted(sai_dump_basenames)}"
