import re
from typing import Optional

from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from devts.infra.tools.connection_tools.linux_ssh_engine import LinuxSshEngine
from ngts.nvos_tools.infra.BmcTool import BmcTool
from ngts.nvos_tools.infra.Fae import Fae
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.system.System import System
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.nvos_constants.constants_nvos import ActionConsts, IssuConsts, NtpConsts, SystemConsts, OutputFormat
from ngts.nvos_tools.cli_coverage.operation_time import OperationTime
from ngts.tests_nvos.cluster.cluster_consts import ClusterConsts
from ngts.tests_nvos.cluster.cluster_tools import ClusterTools, disabled_access_ports
from ngts.nvos_tools.nmx.Cluster import Cluster
from ngts.tests_nvos.constants import MINUTE
from ngts.tools.test_utils import allure_utils as allure
from ngts.tools.test_utils.nvos_general_utils import loganalyzer_ignore
from ngts.tests_nvos.helpers.redmine_helpers import is_bug_active
from ngts.tests_nvos.ib_router.constants import IbRouterConsts
import logging
import pytest

logger = logging.getLogger()

SSD_DUMP_OUTPUT_PATTERN = (
    r'SSD dump saved to '
    r'(?P<path>/var/log/ssd_dump_\d{4}_\d{2}_\d{2}_'
    r'\d{2}_\d{2}_\d{2}_UTC\.tar\.gz)'
)


@pytest.mark.general
@pytest.mark.tech_support
@pytest.mark.disable_loganalyzer
def test_techsupport_with_dockers_down(engines, dockers_list=['gnmi-server']):
    """
    Test flow:
        1. run sudo systemctl stop ib-utils
        2. run nv action generate system tech-support
        3. validate it's working as expected
    """
    try:
        with allure.step('Run nv action generate system tech-support while at least one docker is down'):
            system = System(None)
            for docker in dockers_list:
                engines.dut.run_cmd('sudo systemctl stop {docker}'.format(docker=docker))
            tech_support_folder, duration = system.techsupport.action_generate()
        with allure.step('validate commands works as expected'):
            assert 'nvos_dump' in tech_support_folder, "{err}".format(err=tech_support_folder)

        cleanup_techsupport(engines.dut, [], [tech_support_folder])

    finally:
        for docker in dockers_list:
            engines.dut.run_cmd('sudo systemctl start {docker}'.format(docker=docker))


@pytest.mark.general
@pytest.mark.tech_support
@pytest.mark.skynet
@disabled_access_ports
@pytest.mark.timeout(20 * MINUTE, func_only=True)
def test_techsupport_expected_files(engines, devices, test_name, skynet, ib_router, has_loopbox, standalone_system, setup_name):
    """
    Run nv show system tech-support files command and verify the required fields are exist
    and measure how long it takes
    command: nv show system tech-support files

    Test flow:
        1. get_expected dummy files per device
        2. get new files after configurations
        3. run nv action generate system tech-support
        4. Verify obscurity of config files
        5. verify files' names of techsupport using expected_files_dict
        6. verify files' sizes of techsupport using expected_files_dict

    note - some steps will not happen (cleanup of cluster and tacacs config) on skynet type of runs
    as such setups already has those configs applied
    """

    LDAP_SECRET: str = 'ldap_pass'
    RADIUS_SECRET: str = 'radius_pass'
    TACACS_SECRET: str = 'tacacs_pass'
    password_pattern = r'password[^:]*: [\"\']*([^\"\'\s]+)'
    login_cmd_pattern = r'COMMAND=/usr/sbin/usermod --password\s([^\s]+)'

    config_file_secret_dict = {
        'ldap': {
            'secret': LDAP_SECRET,
            'pattern': r"ldap.+\s+[\"\']*secret[^:]*:\s[\"\']*([^\"\'\s]+)",
            'set_nv_cmd': lambda: system.aaa.ldap.set(op_param_name='secret', op_param_value=LDAP_SECRET).verify_result()
        },
        'radius': {
            'secret': RADIUS_SECRET,
            'pattern': r"radius.+\s+[\"\']*secret[^:]*:\s[\"\']*([^\"\'\s]+)",
            'set_nv_cmd': lambda: system.aaa.radius.set(op_param_name='secret', op_param_value=RADIUS_SECRET).verify_result()
        },
        'tacacs': {
            'secret': TACACS_SECRET,
            'pattern': r"tacacs.+\s+[\"\']*secret[^:]*:\s[\"\']*([^\"\'\s]+)",
            'set_nv_cmd': lambda: system.aaa.tacacs.set(op_param_name='secret',
                                                        op_param_value=TACACS_SECRET).verify_result()
        },
        'ntp_key': {
            'secret': NtpConsts.KEY_VALUE,
            'pattern': r'key[^:]*:\s+[\"\']*([^\"\'\s]+)',
            'set_nv_cmd': lambda: (
                system.ntp.keys.set_resource(NtpConsts.KEY_1).verify_result(),
                system.ntp.keys.resources_dict[NtpConsts.KEY_1].set(op_param_name=NtpConsts.VALUE, op_param_value=NtpConsts.KEY_VALUE).verify_result()
            )
        },
        'snmp_community': {
            'secret': SystemConsts.SNMP_READONLY_COMMUNITY,
            'pattern': r'readonly\-community[^:]*:\s+[\"\']*([^\"\'\s]+)',
            'set_nv_cmd': lambda: system.snmp_server.set(SystemConsts.SNMP_READONLY_COMMUNITY, IssuConsts.SNMP_READ_ONLY_COMMUNITY).verify_result()
        }
    }
    if not skynet:
        config_file_secret_dict.update(
            {'tacacs': {
                'secret': TACACS_SECRET,
                'pattern': r"tacacs.+\s+[\"\']*secret[^:]*:\s[\"\']*([^\"\'\s]+)",
                'set_nv_cmd': lambda: system.aaa.tacacs.set(op_param_name='secret', op_param_value=TACACS_SECRET).verify_result()
            }
            })

    """
    Verify that sensitive information in config files is properly obscured.
    Args: content (str): The content of the config file to verify
    """
    def verify_obscurity_in_config_file(content: str, file_name: str) -> None:
        for secret_name, secret_info in config_file_secret_dict.items():
            verify_secret_obscurity(content=content, pattern=secret_info['pattern'], file_name=file_name,
                                    secret_name=secret_name, secret=secret_info['secret'])
        verify_secret_obscurity(content=content, pattern=password_pattern, file_name=file_name, secret_name='password')

    system = System()
    if ib_router:
        devices.dut.constants.dump_files.append(IbRouterConsts.IBR_DUMP_FILE)
    expected_files_dict = {'dump': devices.dut.constants.dump_files,
                           'log': devices.dut.constants.log_dump_files,
                           'log/audit': devices.dut.constants.audit_files,
                           'log/nginx': devices.dut.constants.log_nginx_files,
                           'stats': devices.dut.constants.stats_dump_files,
                           'hw-mgmt': devices.dut.constants.hw_mgmt_files,
                           'etc': devices.dut.constants.etc_files}

    # Add SDK dump validation for each ASIC (multi-ASIC systems have multiple directories)
    # For multi-ASIC: sai_sdk_dump0 uses dev1, sai_sdk_dump1 uses dev2, etc.
    if hasattr(devices.dut, 'asic_amount'):
        from ngts.nvos_tools.Devices.BaseDevice import BaseSwitch
        for asic_num in range(devices.dut.asic_amount):
            expected_files_dict[f'sai_sdk_dump{asic_num}'] = BaseSwitch.get_sdk_dump_files_for_asic(
                asic_num, devices.dut.constants.sdk_dump_files_template)

    if is_bug_active(4303918) and "hdparm" in expected_files_dict["dump"]:
        expected_files_dict["dump"].remove("hdparm")

    # Dynamically add tech-support directories for cluster apps (if device has any)
    if hasattr(devices.dut, 'expected_cluster_apps') and devices.dut.expected_cluster_apps:
        techsupport_dirs = getattr(devices.dut, 'cluster_techsupport_dirs_by_app', {})
        log_files_by_app = getattr(devices.dut, 'cluster_log_files_by_app', {})

        if techsupport_dirs and isinstance(techsupport_dirs, dict):
            for app in devices.dut.expected_cluster_apps:
                if app in techsupport_dirs:
                    log_dir = techsupport_dirs[app]
                    if log_dir:
                        app_log_files = log_files_by_app.get(app, None)
                        if app_log_files:
                            if standalone_system:
                                excluded = getattr(devices.dut, 'cluster_standalone_excluded_files', [])
                                app_log_files = [f for f in app_log_files if f not in excluded]
                            if app_log_files:
                                expected_files_dict[log_dir] = app_log_files

        # Add cluster config/state files directory if defined
        cluster_files = getattr(devices.dut.constants, 'cluster_files', None)
        if cluster_files:
            expected_files_dict['cluster'] = cluster_files

    if devices.dut.has_bmc:
        bmc_dump_files = getattr(devices.dut.constants, 'bmc_dump_files', None)
        expected_files_dict['bmc'] = bmc_dump_files

    for secret_info in config_file_secret_dict.values():
        secret_info['set_nv_cmd']()
    NvueGeneralCli.apply_config(engine=engines.dut)
    NvueGeneralCli.save_config(engine=engines.dut)

    generated_ssd_dump_paths = set()
    try:
        if devices.dut.has_nmx:
            cluster = Cluster()
            with allure.step("Start cluster"):
                output = OutputParsingTool.parse_show_output_to_dict(
                    cluster.show(output_format=OutputFormat.json),
                    output_format=OutputFormat.json).get_returned_value()

                if output[SystemConsts.STATE] == 'disabled':
                    cluster.set(op_param_name="state", op_param_value='enabled', apply=True)
                    ClusterTools.wait_for_apps_to_be_in_wanted_state(cluster, cluster_expected_state='enabled', nmx_c_expected_state='up')

        with allure.step('Generate standalone SSD diagnostic dump'):
            validate_nvme_dump_generation(engines, generated_ssd_dump_paths)

        with allure.step('Run nv action generate system tech-support and validate dump files'):
            tech_support_file, duration = system.techsupport.action_generate(test_name=test_name)
            tech_support_dir = tech_support_file.replace('.tar.gz', '')
            with allure.step("Tech-support generation takes: {} seconds".format(duration)):
                logger.info("Tech-support generation takes: {} seconds".format(duration))
                OperationTime.verify_operation_time(
                    duration, devices.dut.generate_tech_support, devices
                ).verify_result()
            system.techsupport.extract_techsupport_files(engines.dut)

            with allure.step('Extract hw-mgmt-dump.tar.gz under hw-mgmt folder'):
                system.techsupport.extract_techsupport_subfile(engines.dut, 'hw-mgmt', 'hw-mgmt-dump.tar.gz', tech_support_dir)

            with allure.step(
                'Validate SSD dump status is included in tech-support'
            ):
                hw_mgmt_dir = f'{tech_support_dir}/hw-mgmt'
                ssd_dump_dir = f'{hw_mgmt_dir}/ssd-dump'
                assert engines.dut.run_cmd(
                    f'test -d {ssd_dump_dir} && echo exists'
                ).strip() == 'exists', \
                    f'{ssd_dump_dir} directory is missing'
                ssd_dump_status_path = f'{ssd_dump_dir}/ssd-dump-status.log'
                assert engines.dut.run_cmd(
                    f'test -f {ssd_dump_status_path} && echo exists'
                ).strip() == 'exists', \
                    f'{ssd_dump_dir}/ssd-dump-status.log is missing'

            techsupport_files_dict = system.techsupport.get_techsupport_files_names(engines.dut, expected_files_dict)

        with allure.step('validate obscurity in config files'):
            nvue_dir_path = extract_nvue_show_tech(engines.dut, tech_support_dir)
            assert not verify_file_in_folder(engines.dut, 'startup.yaml', nvue_dir_path), 'startup.yaml exist in {nvue_dir_path}'
            nvued_dir_path = nvue_dir_path + '/nvue.d'

            with allure.step('validate obscurity in startup.yaml'):
                assert verify_file_in_folder(engines.dut, 'startup.yaml', nvued_dir_path), 'startup.yaml exist in {nvued_dir_path}'
                startup_content = engines.dut.run_cmd(f'cat {nvued_dir_path}/startup.yaml')
                verify_obscurity_in_config_file(content=startup_content, file_name='startup.yaml')

            with allure.step('validate obscurity in applied_configuration'):
                assert verify_file_in_folder(engines.dut, 'applied_configuration', nvue_dir_path), 'applied_configuration exist in {nvue_dir_path}'
                applied_config = engines.dut.run_cmd(f'cat {nvue_dir_path}/applied_configuration')
                verify_obscurity_in_config_file(content=applied_config, file_name='applied_configuration')

        with allure.step("validate obscurity in auth.log"):
            auth_log_path = extract_auth_log(engines.dut, tech_support_dir)
            auth_log_content = engines.dut.run_cmd(f'cat {auth_log_path}')
            verify_secret_obscurity(content=auth_log_content, pattern=login_cmd_pattern, file_name=auth_log_path,
                                    secret_name='login cmd', expected_obscurity='***', must_exist=False)

        with allure.step("validate etc/sonic/nvue.d obscurity"):
            assert not verify_file_in_folder(engines.dut, 'startup.yaml', f'{tech_support_dir}/etc/sonic/'), 'startup.yaml exist in /etc/sonic/'
            assert not verify_file_in_folder(engines.dut, 'applied_configuration', f'{tech_support_dir}/etc/sonic/'), 'applied_configuration exist in /etc/sonic/'
            assert not verify_file_in_folder(engines.dut, 'startup.yaml', f'{tech_support_dir}/etc/sonic/nvue.d'), 'startup.yaml exist in /etc/sonic/nvue.d'
            assert not verify_file_in_folder(engines.dut, 'applied_configuration', f'{tech_support_dir}/etc/sonic/nvue.d'), 'applied_configuration exist in /etc/sonic/nvue.d'

        with allure.step("validate each expected file name and size"):
            with allure.independent_step('validate dump/bkv matches nv show fae platform bkv'):
                validate_techsupport_bkv_output(engines.dut, tech_support_dir, expected_files_dict['dump'])

            with allure.independent_step('validate files names'):
                # Clean timestamps from SDK dump file names for all ASICs
                for folder_key in techsupport_files_dict.keys():
                    if folder_key.startswith('sai_sdk_dump'):
                        techsupport_files_dict[folder_key] = system.techsupport.clean_timestamp_techsupport_sdk_files_names(techsupport_files_dict[folder_key])

                for folder, files in techsupport_files_dict.items():
                    with allure.independent_step(f'validate files names for {folder}'):
                        verify_techsupport_files_names(files, expected_files_dict[folder])

            with allure.independent_step('validate files sizes'):
                for folder in expected_files_dict.keys():
                    if expected_files_dict[folder]:  # skip empty folders if files are not expected for a specific system
                        files_list = system.techsupport.get_techsupport_empty_files(engines.dut, tech_folder=folder)
                        with allure.independent_step(f'validate files sizes for {folder}'):
                            verify_techsupport_files_sizes(files_list, folder, devices, skynet)

            # sx_core module validation only applies to switches that have the sx_core kernel module.
            # IB (XDR) switches (Crocodile, Mamba, Taipan) have sx_core; NVLink switches (Juliet, Rosalind) do not.
            if devices.dut.has_sx_core_modules:
                with allure.independent_step('validate sx_core modules in dump'):
                    validate_sx_core_modules_in_dump_nvos(engines.dut, tech_support_dir)

                with allure.independent_step('validate present transceivers in dump'):
                    result = validate_present_transceivers_in_dump_nvos(engines.dut, tech_support_dir)
                    if result == "skip":
                        logger.info('validate_present_transceivers_in_dump_nvos: skipped (no present transceivers)')
            else:
                logger.info('Skipping sx_core module validation: not an IB switch (switch_type={})'.format(
                    devices.dut.switch_type))

            # NVBridge (Rosalind) tech-support must automatically bundle the
            # standalone /var/log/nv-bridge/ directory and the nv-bridge status
            # snapshot /var/run/nvbridge/nvbs_info.json without a manual folder
            # spec. Gated to devices that set requires_nv_bridge_config (Rosalind
            # family); a no-op on every other platform.
            if getattr(devices.dut, 'requires_nv_bridge_config', False):
                with allure.independent_step('validate nv-bridge logs and status are bundled in tech-support'):
                    validate_nv_bridge_logs_in_dump(engines.dut, tech_support_dir)

    finally:
        if devices.dut.has_nmx and not skynet:
            Cluster().unset(apply=True)
            ClusterTools.wait_for_apps_to_be_in_wanted_state(cluster, cluster_expected_state='disabled', nmx_c_expected_state='down')
        for ssd_dump_path in generated_ssd_dump_paths:
            engines.dut.run_cmd(f'sudo rm -f {ssd_dump_path}')
        if system.techsupport.file_name:
            system.techsupport.cleanup(engines.dut)
            system.techsupport.files.file_name[system.techsupport.file_name].action_delete()


@pytest.mark.general
@pytest.mark.tech_support
def test_techsupport_bmc_badflow(engines, test_name):
    """
    This test verifies the behavior of the system when generating tech-support data and handling BMC failures.

    Steps:
    1. Initialize the system object.
    2. Retrieve the password for BMC from TPM.
    3. Gracefully restart the BMC via Redfish.
    4. Generate system tech-support data.
    5. Extract tech-support files.
    6. Verify that the BMC folder in the tech-support files is empty.
    7. Check for the specific error message in the system logs.
    8. Perform cleanup operations.
    """

    # LA ignores some BMC errors during this test, pending on FR 4210208
    system = System()
    try:
        dut_engine: LinuxSshEngine = TestToolkit.engines.dut
        with loganalyzer_ignore():  # supposed to be able to ignore LA here because #4223438
            with allure.step('gracefully restart bmc via redfish'):
                BmcTool.reset(dut_engine)

            with allure.step('Run nv action generate system tech-support'):
                tech_support_folder, duration = system.techsupport.action_generate(test_name=test_name)
                with allure.step("Tech-support generation takes: {} seconds".format(duration)):
                    logger.info("Tech-support generation takes: {} seconds".format(duration))

            system.techsupport.extract_techsupport_files(engines.dut)

            with allure.step('verify bmc folder is empty'):
                files_list = system.techsupport.get_techsupport_files_list(engines.dut, 'bmc')
                files_list = " ".join(files_list)
                assert "No such file or directory" in files_list, f'bmc folder exist and got: {files_list}'

            with allure.step('verify error msg in logs'):
                output = engines.dut.run_cmd("cat /var/log/syslog | grep -a 'bmc_techsupport.py'")
                assert re.search(r'Failed to trigger BMC debug log dump',
                                 output), f"Expected to find 'Failed trigger BMC debug log dump' in output. Got: {output}"
    finally:
        engines.dut.run_cmd("sudo ifup usb0")
        system.techsupport.cleanup(engines.dut)
        if system.techsupport.file_name:
            system.techsupport.files.file_name[system.techsupport.file_name].action_delete()


def cleanup_techsupport(engine, before, after):
    new_folders = [file for file in after if file not in before]
    for dump in new_folders:
        engine.run_cmd('sudo rm -rf ' + dump)


def validate_techsupport_bkv_output(dut_engine, tech_support_dir: str, expected_dump_files: list) -> None:
    """
    When the device expects a BKV file in dump/ (per expected_dump_files, from the device class and
    test-specific adjustments), verify dump/bkv matches nv show fae platform bkv.
    """
    if 'bkv' not in expected_dump_files:
        return
    fae = Fae()
    bkv_show = OutputParsingTool.parse_json_str_to_dictionary(
        fae.platform.bkv.show(output_format=OutputFormat.json, dut_engine=dut_engine)).get_returned_value()
    version = bkv_show.get('version')
    assert version is not None and str(version).strip(), (
        f"Unexpected nv show fae platform bkv output: {bkv_show!r}")
    bkv_path = f'{tech_support_dir}/dump/bkv'
    content = dut_engine.run_cmd(f'sudo cat {bkv_path}')
    assert str(version) in content, (
        f"BKV operational version {version!r} not found in tech-support file {bkv_path!r}: {content!r}")


def validate_nv_bridge_logs_in_dump(engine, tech_support_dir):
    """Verify tech-support bundles the nv-bridge logs and status snapshot.

    Checks that the dump automatically collects the standalone
    /var/log/nv-bridge/ directory and the nv-bridge status snapshot
    (/var/run/nvbridge/nvbs_info.json), with no manual folder specification.

    :param engine: NVOS SSH engine (engines.dut)
    :param tech_support_dir: path to the extracted tech-support dump folder
    """
    with allure.step('locate nv-bridge log directory in the tech-support dump'):
        # Prefer the expected log/nv-bridge location, but fall back to a broad
        # search so a layout change does not silently pass an empty result.
        nvb_dir = engine.run_cmd(
            'sudo find {} -type d -path "*/log/nv-bridge" -print -quit '
            '2>/dev/null || true'.format(tech_support_dir)).strip()
        if not nvb_dir:
            nvb_dir = engine.run_cmd(
                'sudo find {} -type d -name nv-bridge -print -quit '
                '2>/dev/null || true'.format(tech_support_dir)).strip()
        assert nvb_dir and 'No such file' not in nvb_dir, \
            ('/var/log/nv-bridge/ was not bundled into the tech-support dump under '
             '{} (expected it to be collected automatically)'.format(tech_support_dir))

    with allure.step('verify the bundled nv-bridge log directory is non-empty'):
        non_empty = engine.run_cmd(
            'sudo find {} -type f -not -empty -print -quit '
            '2>/dev/null || true'.format(nvb_dir)).strip()
        assert non_empty, \
            ('nv-bridge log directory {} was bundled but contains no non-empty log '
             'files'.format(nvb_dir))

    with allure.step('verify the nv-bridge status snapshot (nvbs_info.json) is bundled'):
        nvbs_info = engine.run_cmd(
            'sudo find {} -type f -name nvbs_info.json -not -empty -print -quit '
            '2>/dev/null || true'.format(tech_support_dir)).strip()
        assert nvbs_info and 'No such file' not in nvbs_info, \
            ('nv-bridge status snapshot /var/run/nvbridge/nvbs_info.json was not '
             'bundled (non-empty) into the tech-support dump under {} '
             '(expected it to be collected automatically)'.format(tech_support_dir))


def verify_techsupport_files_names(files_list, expected_files):
    """
    Verify that all expected files are present in the files list.

    :param files_list: list of actual files found
    :param expected_files: list of expected files
    :return: None
    """
    actual_files_set = set(files_list)
    expected_files_set = set(expected_files)

    ValidationTool.validate_subset_in_superset(expected_files_set, actual_files_set).verify_result()


def verify_techsupport_files_sizes(files_list, folder, devices, skynet=False):
    if folder == 'dump':
        files_list = [file for file in files_list if file not in devices.dut.techsupport_dump_empty_files_to_ignore]
    elif folder == 'etc':
        files_list = [file for file in files_list if file not in devices.dut.techsupport_etc_empty_files_to_ignore]
    elif folder == 'cluster':
        files_list = [file for file in files_list if file not in devices.dut.techsupport_cluster_empty_files_to_ignore]
    elif folder == 'hw-mgmt':
        files_list = [file for file in files_list if file not in devices.dut.techsupport_hw_mgmt_empty_files_to_ignore]
        if skynet:
            files_list += [file for file in files_list if file not in devices.dut.techsupport_skynet_hw_mgmt_empty_files_to_ignore]

    assert len(files_list) == 0, f"the following files are empty {files_list}"


def validate_techsupport_folder_name(system, tech_support_folder):
    """
    Test flow:
        1. run nv show system
        2. get the hostname value
        3. validate the tar.gz name is nvos_dump_<hostname>_<time_now>.tar.gz
    """
    with allure.step('Check that tech-support name is as expected :nvos_dump_<hostname>_<time_now>.tar.gz'):
        system_output = OutputParsingTool.parse_json_str_to_dictionary(system.show()).get_returned_value()
        hostname = system_output[SystemConsts.HOSTNAME]
        assert 'nvos_dump_' + hostname in tech_support_folder, 'the tech-support should be under host dump and includes hostname'


def extract_nvue_show_tech(engine, dump_path: str) -> str:
    engine.run_cmd(f'sudo tar -xf {dump_path}/nvue/nvue_show_tech.tar -C {dump_path}/nvue/')
    engine.run_cmd(f'sudo chmod -R 775 {dump_path}/nvue')
    return f'{dump_path}/nvue/'


def extract_auth_log(engine, dump_path: str) -> str:
    engine.run_cmd(f'sudo chmod g=rx {dump_path}/log/auth.log.gz')
    engine.run_cmd(f'sudo gunzip -k {dump_path}/log/auth.log.gz')
    return f'{dump_path}/log/auth.log'


def verify_file_in_folder(engine, file_name: str, folder_path: str) -> bool:
    return file_name in engine.run_cmd(f'ls {folder_path}')


def _get_ssd_dump_paths(engine) -> set:
    output = engine.run_cmd(
        "find /var/log -maxdepth 1 -type f "
        "-name 'ssd_dump_*_UTC.tar.gz' -print"
    )
    path_pattern = re.compile(
        r'^/var/log/ssd_dump_\d{4}_\d{2}_\d{2}_'
        r'\d{2}_\d{2}_\d{2}_UTC\.tar\.gz$'
    )
    return {
        line.strip() for line in output.splitlines()
        if path_pattern.match(line.strip())
    }


def validate_nvme_dump_generation(engines, generated_dump_paths: set) -> str:
    """
    Validate standalone SSD dump generation before collecting tech-support.

    Every NVOS switch must generate a timestamped archive under /var/log.
    """
    dumps_before = _get_ssd_dump_paths(engines.dut)
    try:
        result = Fae().system.nvme.dump.action(
            ActionConsts.GENERATE,
            timeout=10,
        )
    finally:
        generated_dump_paths.update(
            _get_ssd_dump_paths(engines.dut) - dumps_before
        )

    output = result.verify_result()
    dump_path_match = re.search(SSD_DUMP_OUTPUT_PATTERN, output)
    assert dump_path_match, \
        f'SSD dump success message or path missing in output: {output}'

    dump_path = dump_path_match.group('path')
    assert engines.dut.run_cmd(
        f'test -f {dump_path} && echo exists'
    ).strip() == 'exists', f'SSD dump archive {dump_path} does not exist'
    return dump_path


def verify_secret_obscurity(content: str, pattern: str, file_name: str, secret_name: str, expected_obscurity: str = 'XXX',
                            secret: Optional[str] = None, must_exist: bool = True) -> None:
    match = re.search(pattern, content)
    if must_exist:
        assert match, f'{secret_name} secret not found in {file_name}'
    if match:
        if secret:
            assert match.group(1) != secret, f'{secret_name} secret is visible'
        assert match.group(1) == expected_obscurity, f'{secret_name} secret is not obscured correctly'


def _parse_sx_core_module_names(raw_output):
    """
    Extract only valid sx_core module directory names (e.g. 'module0', 'module72')
    from raw command output.

    Stray lines -- such as bash "command substitution: ignored null byte in input"
    warnings emitted when reading binary sysfs 'present' files, error messages, or
    blank lines -- are filtered out so they cannot pollute the module set or counts.

    :param raw_output: raw stdout/stderr text returned by engine.run_cmd()
    :return: set of valid module directory names
    """
    _SX_CORE_MODULE_NAME_RE = re.compile(r'^module[0-9]+$')
    return {line.strip() for line in raw_output.splitlines()
            if _SX_CORE_MODULE_NAME_RE.match(line.strip())}


def validate_sx_core_modules_in_dump_nvos(engine, dump_folder_path):
    """
    Validate that all sx_core SysFS transceiver module directories from the DUT
    are properly collected in the techsupport dump, and that collected files are not empty.
    Answers the question: "Were all module slots collected into the dump?"
    NVOS version -- uses engine.run_cmd().

    Test flow:
        1. Verify sx_core SysFS path is accessible on the DUT (assert if not)
        2. Collect module directory names from the DUT (ground truth)
        3. Collect module directory names from the techsupport dump
        4. log module counts between DUT and dump for diagnostics
        5. Assert every dump module exists in the dut ( dump_module_set ⊆ DUT_module_set )
        6. Assert no zero-byte files under sx_core/asic0/module* in the dump
    :param engine: NVOS SSH engine (engines.dut)
    :param dump_folder_path: path to folder which has extracted dump file content
    """
    # sx_core sysfs is global under asic0 on Mellanox multi-ASIC systems;
    # all transceiver modules are mapped to asic0 regardless of ASIC count.
    # If sx_core ever exposes per-asic directories (asic1/2/...), this
    # validator must be revisited to include asicX in the comparison key.
    SX_CORE_SYSFS_PATH = "/sys/module/sx_core/asic0"

    with allure.step('Verify sx_core SysFS modules are accessible on the DUT'):
        # List all module directories under sx_core sysfs and extract just the directory names
        # Example output: module0\nmodule1\n...\nmodule72
        dut_modules_output = engine.run_cmd(
            'ls -d {}/module* | xargs -I{{}} basename {{}}'.format(SX_CORE_SYSFS_PATH))
        output_lower = dut_modules_output.strip().lower()
        assert dut_modules_output.strip() and 'module' in output_lower \
            and "error" not in output_lower and "failed" not in output_lower \
            and "no such file" not in output_lower and "cannot access" not in output_lower, \
            'sx_core module path {} not accessible on this switch. ' \
            'The sx_core kernel module may not be loaded. Output: {}'.format(
                SX_CORE_SYSFS_PATH, dut_modules_output.strip())

    with allure.step('Collect module names from DUT'):
        dut_modules = _parse_sx_core_module_names(dut_modules_output)
        logger.info('DUT has {} sx_core modules: {}'.format(len(dut_modules), sorted(dut_modules)))

    with allure.step('Collect module names from techsupport dump'):
        # Search the extracted dump for sx_core/asic0 module directories and extract their names.
        # Matches the exact path generate_dump uses: $TAR_DIR/sdk_sysfs/sx_core/asic0/module*
        # Use a strict regex that anchors on `module<digits>` only -- otherwise `-path` with `*`
        # would also match nested directories like `module0/eeprom/pages/0`, polluting the set
        # with names like `eeprom`, `pages`, `0` and breaking the count comparison below.
        dump_modules_output = engine.run_cmd(
            'sudo find {} -regextype posix-extended '
            '-regex ".*/sx_core/asic0/module[0-9]+" -type d '
            '-printf "%f\\n" 2>/dev/null || true'.format(dump_folder_path))
        dump_modules = _parse_sx_core_module_names(dump_modules_output)
        logger.info('Dump has {} sx_core modules: {}'.format(len(dump_modules), sorted(dump_modules)))

    with allure.step('more info for logger before assert'):
        # related to 'Validate no empty files in collected module data' step
        empty_files_output = engine.run_cmd(
            'sudo find {} -path "*/sx_core/asic0/module*" -type f -empty 2>/dev/null || true'.format(dump_folder_path))
        logger.info('empty_files_output: ' + str(empty_files_output.strip()))

    with allure.step('Validate all dump modules exist in dut'):
        missing_modules = []  # missing modules from dump collector
        for module_name in dump_modules:
            if module_name not in dut_modules:
                missing_modules.append(module_name)
        assert missing_modules == [], \
            f"sx_core modules in techsupport dump but not on DUT: {missing_modules} not found in dump modules  {dump_modules}"

    with allure.step('Validate no empty files in collected module data'):
        # Search for zero-byte files anywhere inside the module subtree, including nested
        # paths like `module*/eeprom/pages/N/data`. Unlike the strict regex used for module
        # directory enumeration above, here the broad `-path module*` pattern is intentional --
        # any empty file in module data indicates failed collection.
        assert not empty_files_output.strip(), \
            ('Found empty files in sx_core module data in techsupport dump:\n{}\n'
             'The module data may not have been read correctly.').format(empty_files_output.strip())


def validate_present_transceivers_in_dump_nvos(engine, dump_folder_path):
    """
    Cross-validate techsupport dump against transceiver presence data on the DUT.
    Compares the number of present transceivers (modules with present=1 in sysfs)
    against modules with present=1 in the dump, and verifies those modules have non-empty files.
    Answers the question: "Were the plugged-in modules collected with valid data?"
    NVOS version -- uses engine.run_cmd() and reads sysfs directly.

    Test flow:
        1. Count modules with present=1 on the DUT (via sysfs)
        2. Count modules with present=1 in the dump
        3. Sanity check: if DUT has present modules, dump must have at least one
        4. Verify all DUT-present modules exist in the dump present set
        5. Verify present modules in the dump have non-empty data files

    :param engine: NVOS SSH engine (engines.dut)
    :param dump_folder_path: path to folder which has extracted dump file content
    """
    SX_CORE_SYSFS_PATH = "/sys/module/sx_core/asic0"

    with allure.step('Verify sx_core SysFS path exists on the DUT'):
        # On Mellanox switches sx_core must be loaded; missing = SDK/kernel bug (FAIL, not SKIP)
        sx_core_check = engine.run_cmd(
            'ls -d {}/module* 2>/dev/null || true'.format(SX_CORE_SYSFS_PATH))
        assert sx_core_check.strip(), \
            'sx_core module path {} not accessible. Cannot validate present transceivers.'.format(
                SX_CORE_SYSFS_PATH)

    with allure.step('Count modules with present=1 on the DUT'):
        # For each module's 'present' file in sysfs, read its value;
        # if "1" (transceiver plugged in), print the module directory name.
        # The output is captured into dut_present_cmd for later parsing.
        dut_present_cmd = (
            'for f in {}/module*/present; do '
            'val=$(cat "$f" 2>/dev/null); '
            '[ "$val" = "1" ] && basename $(dirname "$f"); '
            'done'.format(SX_CORE_SYSFS_PATH))
        dut_present_output = engine.run_cmd(dut_present_cmd)
        dut_present_modules = _parse_sx_core_module_names(dut_present_output)
        dut_present_count = len(dut_present_modules)
        if dut_present_count == 0:
            # sx_core exists (verified above) but no transceivers plugged in -- legitimate skip
            logger.info('No present transceivers on DUT, skipping cross-validation')
            return "skip"
        logger.info('DUT has {} modules with present=1: {}'.format(
            dut_present_count, sorted(dut_present_modules)))

    with allure.step('Count modules with present=1 in dump'):
        # Same logic but searches the extracted dump for 'present' files under sx_core/asic0.
        # Use a strict regex anchored on `module<digits>/present` to avoid matching nested
        # paths like `module0/eeprom/pages/0/present` should the kernel ever expose them.
        dump_present_cmd = (
            'for f in $(sudo find {} -regextype posix-extended '
            '-regex ".*/sx_core/asic0/module[0-9]+/present" -type f 2>/dev/null); do '
            'val=$(sudo cat "$f" 2>/dev/null); '
            '[ "$val" = "1" ] && basename $(dirname "$f"); '
            'done'.format(dump_folder_path))
        dump_present_output = engine.run_cmd(dump_present_cmd)
        dump_present_modules = _parse_sx_core_module_names(dump_present_output)
        dump_present_count = len(dump_present_modules)
        logger.info('Dump has {} modules with present=1: {}'.format(
            dump_present_count, sorted(dump_present_modules)))

    with allure.step('Sanity-check DUT vs dump present counts'):
        logger.info('DUT present count = {}, dump present count = {}'.format(
            dut_present_count, dump_present_count))
        assert dump_present_count > 0, \
            'DUT has {} modules with present=1 but dump has none'.format(dut_present_count)

    with allure.step('Verify all DUT-present modules exist in dump'):
        # Note: this could fail legitimately if a transceiver was removed between
        # dump generation and this check, but in regression environments this
        # should not happen.
        missing_present = dut_present_modules - dump_present_modules
        assert not missing_present, \
            ('Modules present on DUT but missing from dump: {}. '
             'DUT present: {}, Dump present: {}'.format(
                 sorted(missing_present), sorted(dut_present_modules),
                 sorted(dump_present_modules)))

    with allure.step('Validate present modules in dump have non-empty data files'):
        if dump_present_modules:
            for module_name in sorted(dump_present_modules):
                # Find the exact module directory under asic0 and check for zero-byte files
                empty_check = engine.run_cmd(
                    'sudo find {} -path "*/sx_core/asic0/{}" -type d -exec '
                    'find {{}} -type f -empty \\; 2>/dev/null || true'.format(
                        dump_folder_path, module_name))
                assert not empty_check.strip(), \
                    ('Module {} has present=1 but contains empty files in dump:\n{}\n'
                     'The transceiver data may not have been read correctly.').format(
                         module_name, empty_check.strip())
        else:
            assert False, 'No present modules found in dump to validate'

    return "pass"
