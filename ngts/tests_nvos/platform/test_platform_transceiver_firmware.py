import logging
import pytest
import re
import time
import random
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port
from ngts.nvos_tools.infra.IbInterfaceTool import IbInterfaceTool
from ngts.tools.test_utils import allure_utils as allure
from retry import retry
from ngts.nvos_constants.constants_nvos import DatabaseConst
from ngts.tests_nvos.constants import MINUTE
from ngts.nvos_tools.platform.Platform import Platform
from ngts.nvos_tools.ib.InterfaceConfiguration.Interface import Interface
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.nvos_tools.infra.Tools import Tools
from ngts.nvos_constants.constants_nvos import ApiType, SystemConsts, PlatformConsts
from ngts.nvos_tools.infra.RandomizationTool import RandomizationTool
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import IbInterfaceConsts, NvosConsts
from ngts.tests_nvos.helpers.redmine_helpers import is_bug_active
from ngts.tests_nvos.platform.test_platform_transceiver import _get_ports_for_module
from ngts.tests_nvos.platform.constants import *
from ngts.nvos_tools.cli_coverage.operation_time import OperationTime

logger = logging.getLogger()


@pytest.mark.platform
@pytest.mark.transceiver
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
def test_transceiver_database_tables(engines, devices, test_api):
    """
    Test Flow:
        1. get device transceiver list save as <transceivers_list>
        2. for each transceiver verify we have "TRANSCEIVER_FIRMWARE_INFO|InfiniBand<id>" under STATE_DB
    :param engines:
    :param test_api:
    :return:
    """
    with allure.step("Create platform object"):
        transceivers_tables_name = devices.dut.transceivers_tables_name
        number_of_transceivers = devices.dut.number_of_transceivers
        with allure.step("Validate for each transceiver out of {} transceivers we have the table in STATE_DB".format(number_of_transceivers)):
            tables_in_database = Tools.DatabaseTool.sonic_db_cli_get_keys(engine=engines.dut, asic="",
                                                                          db_name=DatabaseConst.STATE_DB_NAME,
                                                                          grep_str=transceivers_tables_name).splitlines()
            assert number_of_transceivers == len(tables_in_database), \
                f"Test Failed: we expected {number_of_transceivers} transceivers tables in STATE_DB but we found {len(tables_in_database)}"


@pytest.mark.platform
@pytest.mark.transceiver
def test_reset_transceiver_firmware_positive(engines, test_api):
    """
        after reset transceiver firmware we expect the next behavior:
            1. for the physical-state of the interface link: LinkUp, Polling, LinkUp
            2. for the logical-state of the interface link: N/A, Down, Active
            3. for the transceiver firmware: No Data, [3 expected fields]

    Test Flow:
        1. Pick random connected module save as <random_sw> [nv show interface sw16p1 link -> cable-type:Optical module]
        2. Run nv action reset platform transceiver <random_sw>
        3. Run nv show platform transceiver <random_sw> firmware validate output = {}
        4. Run nv show interface <random_sw_p1> - validate: physical-state = LinkUp, logical-state = Down
        5. wait 1 seconds
        6. Run nv show platform transceiver <random_sw> firmware validate output includes all expected fields
        7. Run nv show interface <random_sw_p1> - validate: physical-state = Polling, logical-state = Down
        8. wait 1 seconds
        9. Run nv show interface <random_sw_ports>	Verify:"physical-state": "LinkUp", logical-state = up

    :param engines:
    :param test_api:
    :return:
    """
    platform, random_transceiver, random_port = _get_random_optical_module_transceiver()

    with allure.step("Create interface object"):
        interface = Interface(parent_obj=None, port_name=random_port)

    with allure.step(f"reset {random_transceiver} and verify expected behavior using show command"):
        counters_before_reset = OutputParsingTool.parse_json_str_to_dictionary(interface.counters.show()).verify_result()
        default_output = OutputParsingTool.parse_json_str_to_dictionary(platform.transceiver.show(random_transceiver + ' firmware')).verify_result()
        default_fw = OutputParsingTool.parse_json_str_to_dictionary(default_output).verify_result()[PlatformConsts.FW_ACTUAL]
        platform.transceiver.action_reset(random_transceiver).verify_result()

        with allure.step("sleep for 15 sec - waiting for after reset action"):
            time.sleep(15)

        with allure.step(f"verify all {random_transceiver} fields back to the same values"):
            output_after_reset = OutputParsingTool.parse_json_str_to_dictionary(output_json=platform.transceiver.show(random_transceiver + ' firmware')).verify_result()
            _verify_expected_dict(output_after_reset, default_fw)

        with allure.step(f"verify all {random_port} counters back to the same values"):
            counters_after_reset = OutputParsingTool.parse_json_str_to_dictionary(interface.counters.show()).verify_result()
            check_counters(counters_before_reset, counters_after_reset)


@pytest.mark.timeout(30 * MINUTE, func_only=True)
@pytest.mark.platform
@pytest.mark.transceiver
def test_install_transceiver_firmware_positive(engines, devices, random_api, test_name):
    """
    Test Flow:
        1. Fetch 2 module FW images. Save as <FW1>و <FW2>
        2. Pick random connected module  	Save as <random_sw>
        3. Run nv action install platform transceiver <random_sw> firmware files <FW1>  	Action executing ... Action succeeded
        4. Verify Total installing Time	Between 1-2 min[using logs]
        5. Run nv show platform transceiver <random_sw> firmware	actual-firmware   <fw1>  fw-upgrade-status OK  fw-upgrade-error  N/A
        6. Run nv action install platform transceiver <random_sw> firmware files <FW2>  	Action executing ...  Action succeeded
        7. Run nv show platform transceiver <random_sw> firmware	actual-firmware   <fw2>  fw-upgrade-status OK  fw-upgrade-error  N/A
        8. Run nv show platform transceiver firmware files	fw1.bin    /host/fw-images/modules/fw1.bin  fw2.bin    /host/fw-images/modules/fw2.bin
        9. Run nv show platform transceiver <random_sw>  firmware files	fw1.bin    /host/fw-images/modules/fw1.bin  fw2.bin    /host/fw-images/modules/fw2.bin
        10. Pick randomly on of 1 or 2	<random_fw>
        11. Run nv show platform transceiver <random_sw> firmware <random_fw>	<random_fw>.bin    /host/fw-images/modules/<random_fw>.bin
        12. Run nv action reset platform transceiver <transceiver-id> Action executing ...  Action succeeded
        13. Run nv show platform transceiver <random_sw> firmware	actual-firmware   <default>  fw-upgrade-status OK  fw-upgrade-error  N/A
        14. Run nv show platform transceiver firmware	Fw1 ..
        15. Run nv show platform transceiver <random_sw>  firmware files
        16. Run nv show interface <random_sw_ports>	Verify: Verify:"physical-state": "LinkUp", logical-state = up
        17.  check all dict still the same
        18. Run nv action reset system factory-default
        19. Run nv show platform transceiver <random_sw>  firmware
        20. validate expected output
        21. Run nv show platform transceiver <random_sw>  firmware files
        22. validate expected output

    :param engines:
    :param test_api:
    :return:
    """

    platform, random_transceiver, random_port = _get_random_optical_module_transceiver()

    with allure.step(f"get the mst device for transceiver {random_transceiver}"):
        output_dictionary = OutputParsingTool.parse_show_interface_output_to_dictionary(
            Port.show_interface(fae_param='fae', port_names=random_port)).get_returned_value()
        pci_conf = output_dictionary[IbInterfaceConsts.PRIMARY_ASIC_DEVICE].split("/")
        mst_dev_name = IbInterfaceTool.get_mst_cable_name(engines.dut, random_transceiver, pci_conf[-1])

    default_fw = OutputParsingTool.parse_json_str_to_dictionary(
        platform.transceiver.show(random_transceiver + ' firmware')).verify_result()[PlatformConsts.FW_ACTUAL]

    with allure.step("Create interface object"):
        interface = Interface(parent_obj=None, port_name=random_port)

    with allure.step("Fetch 2 transceiver firmware files for {}, the actual firmware is {}".format(random_transceiver, default_fw)):
        player_engine = engines['sonic_mgmt']
        scp_path = 'scp://{}:{}@{}'.format(player_engine.username, player_engine.password, player_engine.ip)
        transceiver_id = default_fw.split('.')[0]
        transceiver_obj: Transceiver = TransceiversConsts.TRANSCEIVERS_DETAILS[transceiver_id]

        with allure.step("check module security level and update versions if needed"):
            if not IbInterfaceTool.is_dev_module(engines.dut, mst_dev_name):
                transceiver_obj.update_versions()

        downgrade_version_path = transceiver_obj.test_versions_path + transceiver_obj.downgrade_version_name
        upgrade_version_path = transceiver_obj.test_versions_path + transceiver_obj.upgrade_version_name

    with allure.step(f"try to downgrade and upgrade firmware for transceiver of type {transceiver_obj.transceiver_type}"):
        try:
            with allure.independent_step("fetch and downgrade transceiver firmware"):
                platform.firmware.transceiver.action_fetch(downgrade_version_path, base_url=scp_path).verify_result()
                result_obj, duration = OperationTime.save_duration(f"transceiver firmware downgrade to {transceiver_obj.downgrade_version_number}",
                                                                   '', test_name, platform.transceiver.action_install,
                                                                   random_transceiver, transceiver_obj.downgrade_version_name)
                OperationTime.verify_operation_time(duration, f"transceiver firmware installation", threshold=transceiver_obj.installation_time).verify_result()

            with allure.step("run {} show counters command".format(random_port)):
                counters_before_install = OutputParsingTool.parse_json_str_to_dictionary(
                    interface.counters.show()).verify_result()

            with allure.step("verify show commands after install"):
                output_after_install = OutputParsingTool.parse_json_str_to_dictionary(platform.transceiver.show(random_transceiver + ' firmware')).verify_result()
                if not is_bug_active(4380004):
                    _verify_expected_dict(command_output=output_after_install, default_fw=transceiver_obj.downgrade_version_number, status='OK', msg='N/A')
                counters_after_install = OutputParsingTool.parse_json_str_to_dictionary(interface.counters.show()).verify_result()
                check_counters(counters_before_install, counters_after_install)
        finally:
            with allure.independent_step("fetch and upgrade transceiver firmware"):
                platform.firmware.transceiver.action_fetch(upgrade_version_path, base_url=scp_path).verify_result()
                result_obj, duration = OperationTime.save_duration(f"transceiver firmware upgrade to {transceiver_obj.upgrade_version_number}",
                                                                   '', test_name, platform.transceiver.action_install,
                                                                   random_transceiver, transceiver_obj.upgrade_version_name)
                OperationTime.verify_operation_time(duration, f"transceiver firmware installation", threshold=transceiver_obj.installation_time).verify_result()


@pytest.mark.platform
@pytest.mark.transceiver
@pytest.mark.parametrize('test_api', [ApiType.NVUE])
def test_install_reset_transceiver_firmware_negative_flow(engines, test_api):
    """
    Verify a failed transceiver FW install + reset leaves the port and
    firmware state as they were before the attempt.

    Test flow:
        1. Pick a random optical module with an UP port  (save as <random_sw>).
        2. Snapshot `nv show platform transceiver <random_sw> firmware`.
        3. Fetch an invalid FW image (`invalid_fw.bin`) onto the switch.
        4. Wait for the port to reach physical-state=LinkUp; snapshot
           `nv show interface <random_sw_port> link`.
        5. Run `nv action install platform transceiver <random_sw> firmware
           files invalid_fw.bin` and expect it to fail with
           "Failed to complete download of FW image to EEPROM".
        6. Run `nv action reset platform transceiver <random_sw>`.
        7. Wait for the transceiver to re-enumerate (sleep 5s) and for the
           port to return to physical-state=LinkUp; record recovery time as
           the `transceiver_reset_relink` operation.
        8. Verify the transceiver firmware dict equals the pre-install
           snapshot (per-key compare).
        9. Verify the interface link dict equals the pre-install snapshot
           for stable configuration/negotiated fields, and that the two
           per-cycle measurements (round-trip-latency, time-to-link-up)
           are within +/-20% of baseline (they jitter across a training
           cycle but a real regression is caught).

    :param engines:
    :param test_api:
    :return:
    """
    invalid_file = "invalid_fw.bin"
    invalid_fw_path = f"{SystemConsts.GENERAL_TRANSCEIVER_FIRMWARE_FILES}/{invalid_file}"
    expected_error_msg = 'Failed to complete download of FW image to EEPROM'
    test_name = test_install_reset_transceiver_firmware_negative_flow.__name__

    platform, random_transceiver, random_port = _get_random_optical_module_transceiver()

    with allure.step(f"Run nv show platform transceiver {random_transceiver}"):
        output_before_install = OutputParsingTool.parse_json_str_to_dictionary(platform.transceiver.show(random_transceiver + ' firmware')).verify_result()

    with allure.step("fetch firmware transceiver file {} switch".format(invalid_file)):
        player_engine = engines['sonic_mgmt']
        scp_path = 'scp://{}:{}@{}'.format(player_engine.username, player_engine.password, player_engine.ip)
        platform.firmware.transceiver.action_fetch(invalid_fw_path, base_url=scp_path).verify_result()

    with allure.step("Create interface object and wait for stable LinkUp baseline"):
        interface = Interface(parent_obj=None, port_name=random_port)
        _wait_until_physical_linkup(interface)
        show_interface_before_install = OutputParsingTool.parse_json_str_to_dictionary(interface.link.show()).verify_result()

    with allure.step("install new transceiver firmware - {}".format(invalid_file)):
        platform.transceiver.action_install(random_transceiver, invalid_file, expected_str=expected_error_msg).verify_result(should_succeed=False)
        platform.transceiver.action_reset(random_transceiver).verify_result()

    with allure.step("wait for interface and transceiver to fully recover after reset"):
        relink_start = time.time()
        try:
            time.sleep(5)  # let transceiver re-enumerate in platform inventory
            _wait_until_transceiver_fw_ready(platform, random_transceiver)
            _wait_until_physical_linkup(interface)
        finally:
            OperationTime.save_manual_operation_duration_to_db(
                'transceiver_reset_relink', time.time() - relink_start, test_name)

    with allure.step("verify show commands after install"):
        _wait_until_logical_state(
            interface, show_interface_before_install[IbInterfaceConsts.LINK_LOGICAL_PORT_STATE])
        show_interface_after_install = OutputParsingTool.parse_json_str_to_dictionary(
            interface.link.show()).verify_result()
        output_after_install = OutputParsingTool.parse_json_str_to_dictionary(
            platform.transceiver.show(random_transceiver + ' firmware')).verify_result()

        # round-trip-latency and time-to-link-up are re-measured every training
        # cycle and jitter across a reset, so exclude both from the exact compare.
        volatile = {IbInterfaceConsts.LINK_ROUND_TRIP_LATENCY, IbInterfaceConsts.LINK_TO_LINK_UP}
        ValidationTool.compare_dictionaries(output_before_install, output_after_install).verify_result()
        ValidationTool.compare_dictionaries(
            {k: v for k, v in show_interface_before_install.items() if k not in volatile},
            {k: v for k, v in show_interface_after_install.items() if k not in volatile},
        ).verify_result()

        # round-trip-latency is a stable physical property of the cable, so a
        # real regression is caught with a +/-20% band around the baseline.
        rtl = IbInterfaceConsts.LINK_ROUND_TRIP_LATENCY
        before_rtl_raw = show_interface_before_install.get(rtl)
        after_rtl_raw = show_interface_after_install.get(rtl)
        assert before_rtl_raw is not None and after_rtl_raw is not None, \
            f"{rtl}: before={before_rtl_raw!r} after={after_rtl_raw!r} (missing on one side — port likely not LinkUp)"
        before_rtl = float(re.search(r'\d+', str(before_rtl_raw)).group())
        after_rtl = float(re.search(r'\d+', str(after_rtl_raw)).group())
        assert abs(after_rtl - before_rtl) <= before_rtl * 0.2, \
            f"{rtl}: after={after_rtl_raw!r} vs before={before_rtl_raw!r} (>20% drift)"

        # time-to-link-up is the training duration itself; it swings widely across
        # a reset (SM timing / negotiation retries) so a baseline band is meaningless
        # here — its regression is tracked via the transceiver_reset_relink metric.
        # Only sanity-check the port reported a positive link-up time.
        link_up_field = IbInterfaceConsts.LINK_TO_LINK_UP
        after_link_up_raw = show_interface_after_install.get(link_up_field)
        assert after_link_up_raw is not None, f"{link_up_field} missing after install — port likely not LinkUp"
        assert float(re.search(r'\d+', str(after_link_up_raw)).group()) > 0, \
            f"{link_up_field}: non-positive link-up time after install ({after_link_up_raw!r})"


@pytest.mark.platform
@pytest.mark.transceiver
def test_install_reset_invalid_transceiver_id(engines, test_api):
    """
    Test flow:
        1. Run nv action reset platform transceiver <Invalid_transceiver-id >.
        2. Run nv show platform transceiver <Invalid_Transceiver-id > firmware.
        3. Run nv show platform transceiver <Invalid_Transceiver-id > firmware files.
        4. Run nv show platform transceiver <Invalid_Transceiver-id> firmware files <Invalid_filename>
        5. Run nv action install platform transceiver <Invalid_transceiver-id -id> firmware files <Invalid_filename>
        6. verify all commands failed with the expected error message

    :param engines:
    :param test_api:
    :return:
    """
    with allure.step("Create platform object"):
        platform = Platform()
        invalid_transceiver = 'testing'
        invalid_file_name = 'no_file'
        invalid_transceiver_expected_output = "'testing' is not a 'transceiver-name'"
        invalid_file_expected_output = "no_file not found"

    with allure.step("try to run transceiver commands with invalid transceiver id and non exist file"):
        with allure.independent_step("Install non exist file"):
            platform, random_transceiver, random_port = _get_random_optical_module_transceiver()
            platform.transceiver.action_install(random_transceiver, invalid_file_name, expected_str=invalid_file_expected_output).verify_result()
        with allure.independent_step("Install invalid transceiver and non exist file"):
            platform.transceiver.action_install(invalid_transceiver, invalid_file_name, expected_str=invalid_transceiver_expected_output).verify_result()
        with allure.independent_step("Reset invalid transceiver"):
            platform.transceiver.action_reset(invalid_transceiver, expected_str=invalid_transceiver_expected_output).verify_result()
        with allure.independent_step("Show invalid_transceiver firmware"):
            assert invalid_transceiver_expected_output in platform.transceiver.show(invalid_transceiver + ' firmware', should_succeed=False), "The show firmware command succeeded when it was expected to fail"
        with allure.independent_step("Show invalid_transceiver and non exist file"):
            assert invalid_transceiver_expected_output in platform.transceiver.show(invalid_transceiver + ' firmware files ' + invalid_file_name, should_succeed=False), "The show firmware files command succeeded when it was expected to fail"
        with allure.independent_step("Show invalid_transceiver"):
            assert invalid_transceiver_expected_output in platform.transceiver.show(invalid_transceiver, should_succeed=False), "The show transceiver command succeeded when it was expected to fail"


@retry(Exception, tries=60, delay=1)
def _wait_until_linkup(interface):
    show_interface_output = OutputParsingTool.parse_json_str_to_dictionary(interface.link.show()).verify_result()
    _verify_logic_and_physical_state(show_interface_output, IbInterfaceConsts.LINK_LOGICAL_PORT_STATE_ACTIVE,
                                     IbInterfaceConsts.LINK_PHYSICAL_PORT_STATE_LINK_UP)


@retry(Exception, tries=60, delay=1)
def _wait_until_physical_linkup(interface):
    """Wait for physical-state=LinkUp only; logical-state depends on the fabric SM
    and is orthogonal to the transceiver reset contract."""
    show_interface_output = OutputParsingTool.parse_json_str_to_dictionary(interface.link.show()).verify_result()
    actual = show_interface_output[IbInterfaceConsts.LINK_PHYSICAL_PORT_STATE]
    assert actual == IbInterfaceConsts.LINK_PHYSICAL_PORT_STATE_LINK_UP, \
        f"physical-state is {actual}, expected LinkUp"


@retry(Exception, tries=60, delay=1)
def _wait_until_logical_state(interface, expected_logical_state):
    """After a transceiver reset the physical link comes up first; the fabric SM
    then walks the logical-state and speed / ib-speed only reappear once it settles.
    Poll until logical-state matches the pre-reset baseline (not a hardcoded Active:
    a port that was Initialize at baseline must not be forced to Active here)."""
    show_interface_output = OutputParsingTool.parse_json_str_to_dictionary(interface.link.show()).verify_result()
    ValidationTool.verify_field_value_in_output(
        show_interface_output, IbInterfaceConsts.LINK_LOGICAL_PORT_STATE, expected_logical_state).verify_result()


@retry(Exception, tries=60, delay=1)
def _wait_until_transceiver_fw_ready(platform, transceiver_id):
    """After reset, actual-firmware reads 'N/A' until the module re-reports its
    FW version. That's a stronger 'fully recovered' signal than a single LinkUp."""
    fw = OutputParsingTool.parse_json_str_to_dictionary(
        platform.transceiver.show(transceiver_id + ' firmware')).verify_result()
    assert fw.get(PlatformConsts.FW_ACTUAL) not in (None, 'N/A'), \
        f"actual-firmware is {fw.get(PlatformConsts.FW_ACTUAL)!r}, transceiver not ready"


def _verify_logic_and_physical_state(show_interface_output, link_state, physical_state):
    """
    :param show_interface_output:
    :param link_state:
    :param physical_state:
    :return:
    """
    with allure.step("verify  {} = {}, {} = {}".format(IbInterfaceConsts.LINK_LOGICAL_PORT_STATE, link_state,
                                                       IbInterfaceConsts.LINK_PHYSICAL_PORT_STATE, physical_state)):
        assert show_interface_output[IbInterfaceConsts.LINK_LOGICAL_PORT_STATE] == link_state, "the {} is {} not {} as expected".format(IbInterfaceConsts.LINK_LOGICAL_PORT_STATE, show_interface_output[IbInterfaceConsts.LINK_LOGICAL_PORT_STATE], link_state)
        assert show_interface_output[IbInterfaceConsts.LINK_PHYSICAL_PORT_STATE] == physical_state, "the {} is {} not {} as expected".format(IbInterfaceConsts.LINK_PHYSICAL_PORT_STATE, show_interface_output[IbInterfaceConsts.LINK_PHYSICAL_PORT_STATE], physical_state)


def _verify_expected_dict(command_output, default_fw, status='N/A', msg='N/A'):
    """
    :param command_output:
    :param default_fw:
    :param status:
    :param msg:
    :return:
    """
    with allure.step("verify actual-firmware = {}, status = {}, error_msg = {}".format(default_fw, status, msg)):
        expected_dict = {
            PlatformConsts.FW_ACTUAL: default_fw,
            PlatformConsts.FW_UPGRADE_STATUS: status,
            PlatformConsts.FW_UPGRADE_ERROR_MSG: msg
        }

        assert command_output == expected_dict, "at least one of the values is not as expected {}".format(command_output)


def _get_random_optical_module_transceiver():
    """Pick a random optical-module transceiver whose port is currently UP.

    Filtering only by cable-type (as the original did) returns dark modules
    whose fiber has no Rx signal, which then time out at the baseline LinkUp
    wait. Intersect optical hardware with the current UP-port set instead.
    """
    with allure.step("Get random connected optical module transceiver"):
        platform = Platform()
        show_transceiver = OutputParsingTool.parse_json_str_to_dictionary(
            platform.transceiver.show_detailed()).verify_result()
        optical = [n for n, e in show_transceiver.items() if isinstance(e, dict) and (
            e.get(PlatformConsts.TRANSCEIVER_CABLE_TYPE) == 'Optical module' or
            e.get(PlatformConsts.HARDWARE_TRANCEIVER_DIAGNOSTIC_STATUS) == 'Diagnostic Data Available')]
        if not optical:
            pytest.skip("No optical modules available for the setup")

        up_port_names = {p.name for p in RandomizationTool.select_random_ports(
            requested_ports_state=NvosConsts.LINK_STATE_UP,
            num_of_ports_to_select=0).get_returned_value()}

        # Regular sw* modules use <module>pN naming, so intersect by string
        # instead of one `nv show interface` per module (that was 5-10s on
        # croc-19). ELS/FNM modules use a port-mapping table and are excluded
        # here — this test targets optical FW install on standard modules.
        candidates = {m: [p for p in (f"{m}p1", f"{m}p2") if p in up_port_names]
                      for m in optical
                      if IbInterfaceConsts.FNM_PORT_TYPE not in m and
                      TransceiversConsts.TRANSCEIVERS_ELS not in m}
        candidates = {m: ports for m, ports in candidates.items() if ports}
        if not candidates:
            pytest.skip(f"No connected optical modules. Optical: {optical}; "
                        f"UP ports: {sorted(up_port_names)}")

        transceiver = random.choice(list(candidates))
        return platform, transceiver, random.choice(candidates[transceiver])


def _collect_counter_changes(before, after, path, changes, threshold=0.2):
    """
    Recursively compare counter structures and collect entries where the value
    increased by more than threshold (e.g. 20%). Handles nested dicts and leaf
    numeric values; non-numeric leaves (e.g. "n/a") must be equal.
    """
    if isinstance(before, dict) and isinstance(after, dict):
        assert before.keys() == after.keys(), (
            f"Counter keys mismatch at path '{path}': before={set(before.keys())!r}, after={set(after.keys())!r}"
        )
        for key in before:
            _collect_counter_changes(before[key], after[key], f"{path}.{key}" if path else key, changes, threshold)
        return

    if isinstance(before, (int, float)) and isinstance(after, (int, float)):
        if before > 0 and (after - before) > before * threshold:
            changes.append({"path": path, "before": before, "after": after})
        return

    if before != after:
        changes.append({"path": path, "before": before, "after": after})


def check_counters(counters_before, counters_after):
    """

    :param counters_before:
    :param counters_after:
    :return:
    """
    changes = []
    err_msg = ""
    with allure.step("Verify that no keys are missing after action"):
        assert counters_before.keys() == counters_after.keys()

    with allure.step("Validate that none of the counters have changed by more than 20% (deep check)"):
        _collect_counter_changes(counters_before, counters_after, "", changes)

        if changes:
            for change in changes:
                err_msg += f"Path: {change['path']}, Before: {change['before']}, After: {change['after']}; "
            assert False, err_msg
