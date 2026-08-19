#!/usr/bin/env python

from __future__ import division

# Built-in modules
import json
import os
import sys
import time
import re
import yaml
import random

path = os.path.abspath(__file__)
sonic_mgmt_path = path.split('/sonic-tool/')[0]
sys.path.append(sonic_mgmt_path)
print("The sys path: ", sys.path)

# Third-party libs
from xml.etree import ElementTree
from rpyc.utils.classic import connect, download

# Local modules
from reg2_wrapper.common.error_code import ErrorCode
from reg2_wrapper.utils.parser.cmd_argument import RunningStage
from reg2_wrapper.test_wrapper.standalone_wrapper import StandaloneWrapper

from sig_term_handler.handler_mixin import TermHandlerMixin
from lib.utils import get_allure_project_id
from lib.constants import HTTP_SERVER_NBU_NFS
from ngts.tests.nightly.secure.constants import SecureBootConsts

ErrorCode.NO_COLLECTION = 5

TESTBED_YAML_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../../ansible/testbed.yaml")


class RunPytest(TermHandlerMixin, StandaloneWrapper):

    def configure_parser(self):
        super(RunPytest, self).configure_parser()

        # Client arguments
        self.add_cmd_argument("--sonic-mgmt-dir", required=True, dest="sonic_mgmt_path",
                              help="Specify dir of the sonic-mgmt repo on player (sonic-mgmt container), for example: \
                                    /root/mars/workspace/sonic-mgmt")
        self.add_cmd_argument("--dut-name", required=True, dest="dut_name",
                              help="DUT name, for example: arc-switch1029")
        self.add_cmd_argument("--setup-name", required=False, dest="setup_name",
                              help="Setup name, for example: sonic-dual-tor-tigon")
        self.add_cmd_argument("--sonic-topo", required=True, dest="sonic_topo",
                              help="Topology for SONiC testing, for example: t0, t1, t1-lag, ptf32, etc.")
        self.add_cmd_argument("--test-scripts", required=True, dest="test_scripts",
                              help="The pytest scripts to be executed. Multiple scripts should be separated by \
                                    whitespace. Both absolute or relative path are OK.")
        self.add_cmd_argument("--raw-options", nargs="?", default="", dest="raw_options",
                              help="All the other options that to be passed to py.test")
        self.add_cmd_argument("--json-root-dir", required=True, dest="json_root_dir",
                              help="Root directory for storing json metadata")
        self.add_cmd_argument("--is_python3_test", required=False, default=False, dest="is_python3_test",
                              help="True if test case should run from python3, by default False(use python 2.7)")
        self.add_cmd_argument("--test_type", required=False, default="", dest="test_type",
                              help="Decide the pytest marker we want to use in the CI test")
        self.add_cmd_argument("--run_test_on_dpu_only", required=False, default=False, dest="run_test_on_dpu_only",
                              help="run tests only on smartswitch dpu")
        self.add_cmd_argument("--pytest-tout-factor", required=False, default="*1", dest="pytest_tout_factor",
                              help="Arithmetic operation applied to pytest --timeout and --session-timeout in "
                                   "raw-options, e.g. '*2', '+600', '-300', '/2'. Default '*1' (no change).")

    def _parse_junit_xml(self, content):

        result = {}
        try:
            junit_report = ElementTree.fromstring(content)
        except Exception as e:
            self.Logger.error("The junit xml report is not a valid XML file. Exception: %s" % repr(e))
            return result

        if junit_report.tag == "testsuites":
            testsuite = junit_report.getchildren()[0]
        else:
            testsuite = junit_report

        try:
            result["failed"] = int(testsuite.attrib["failures"])
            result["skipped"] = int(testsuite.attrib["skipped"])
            result["errors"] = int(testsuite.attrib["errors"])
        except ValueError as e:
            self.Logger.warning("Converting string to int failed while parsing testsuite. Err: %s" % repr(e))
        except KeyError as e:
            self.Logger.warning("Parse jUnit testsuite info failed. Err=%s" % repr(e))

        all_cases = []
        tag_result_map = {"failure": "failed", "skipped": "skipped", "error": "error"}
        try:
            for testcase in testsuite.getchildren():
                if testcase.tag != "testcase":
                    continue
                case_info = {}
                case_info["name"] = "%s::%s" % (testcase.attrib["file"], testcase.attrib["name"])

                case_children = testcase.getchildren()

                for case_child in case_children:
                    if case_child.tag.lower() in tag_result_map:
                        case_info["result"] = tag_result_map[case_child.tag.lower()]
                        break

                if "result" not in case_info:
                    case_info["result"] = "passed"

                all_cases.append(case_info)

        except KeyError as e:
            self.Logger.warning("Parse jUnit testcase info failed. Err=%s" % repr(e))

        self.Logger.info("All cases: %s" % str(json.dumps(all_cases, indent=4)))

        unique_all_cases = set([case["name"] for case in all_cases])
        result["total"] = len(unique_all_cases)

        # A test case could be both "failed" and "error". Use below code to avoid duplicated test case.
        unique_error_failed_cases = set([case["name"] for case in all_cases if case["result"] in ("failed", "error")])

        result["passed"] = result["total"] - len(unique_error_failed_cases) - result["skipped"]
        try:
            result["pass_rate"] = "{:.0%}".format(result["passed"] / (result["total"] - result["skipped"]))
        except ZeroDivisionError:
            result["pass_rate"] = "0%"
            self.Logger.warning("No test case executed")
        result["testcases_error_failed_list"] = " ".join(["<p>%s</p>" % case for case in unique_error_failed_cases])

        return result

    def dump_metadata(self, json_obj):
        if not self.session_id:
            self.Logger.warning("Metadata Data will not be stored due to rerun command")
            return

        if not json_obj:
            self.Logger.warning("No metadata to be stored")

        # Make json dir
        json_dir = os.path.join(self.json_root_dir, self.session_id)
        if not os.path.isdir(json_dir):
            self.Logger.info("Creating directory %s" % json_dir)
            os.mkdir(json_dir, 0o755)

        json_metadata = {"id": self.mars_key_id, "json": json_obj}
        dump_filename = os.path.join(json_dir, self.mars_key_id + ".json")

        self.Logger.info("Ready to dump %s:\n%s" % (dump_filename, json.dumps(json_metadata, indent=4)))

        with open(dump_filename, 'w') as outfile:
            json.dump(json_metadata, outfile)

    def run_pre_commands(self):
        """
        @summary: Override the method of base class. Export environment variables required for pytest scripts.
        """
        for player in self.Players:
            # Ansible depends on the $HOME environment variable to determine SSH ControlPath location.
            #     -o 'ControlPath=/root/mars/workspace/sonic-mgmt/ansible/$HOME/.ansible/cp/ansible-ssh-%h-%p-%r'
            # The test wrapper is executed in a context without $HOME environment variable. The workaround is to
            # explicitly define one here:
            player.putenv("HOME", "/root")
            player.putenv("ANSIBLE_CONFIG", os.path.join(self.sonic_mgmt_path, "ansible"))
            # Added to support the new ngts image, which defautls python to /opt/venv/bin/python
            player.putenv("VIRTUAL_ENV", "/opt/venv")
            player.putenv("PATH", "/opt/venv/bin" + os.pathsep + player.getenv("PATH"))
        return ErrorCode.SUCCESS

    def _apply_pytest_tout_factor(self):
        """Apply the pytest_tout_factor arithmetic operation (e.g. '*2', '+600', '-300', '/2')
        to pytest --timeout and --session-timeout values inside raw_options."""
        ops = {'*': lambda v, o: v * o, '/': lambda v, o: v / o,
               '+': lambda v, o: v + o, '-': lambda v, o: v - o}
        factor = (self.pytest_tout_factor or "").strip()
        match_op = re.match(r'^([*/+-])\s*(\d+(?:\.\d+)?)$', factor)
        if not match_op:
            self.Logger.warning("Invalid pytest_tout_factor '%s', skipping timeout scaling" % self.pytest_tout_factor)
            return
        op, operand = match_op.group(1), float(match_op.group(2))
        if op == '*' and operand == 1:
            return

        def _scale(match):
            new_val = int(ops[op](int(match.group(2)), operand))
            return "%s %d" % (match.group(1), new_val)

        self.raw_options = re.sub(r'(--timeout|--session-timeout)\s+(\d+)', _scale, self.raw_options)
        self.Logger.info("Applied pytest_tout_factor='%s' to pytest timeouts" % factor)

    def run_commands(self):
        rc = ErrorCode.SUCCESS

        self._apply_pytest_tout_factor()

        self.report_file = "junit_%s_%s.xml" % (self.session_id, self.mars_key_id)
        old_allure_server = "10.215.11.120"
        if self.dut_name in ["arc-switch1004", "arc-switch1025", "r-panther-23", "r-panther-45"] and \
                "test_advanced_reboot.py" in self.test_scripts:
            self.raw_options += ' --ceos_neighbor_lacp_multiplier=5'
        if old_allure_server in self.raw_options:
            self.raw_options = self.raw_options.replace(old_allure_server, "allure.nvidia.com")
        else:
            self.raw_options = self.raw_options + ' --allure_server_addr="allure.nvidia.com" '
        self.raw_options += ' --allure_server_port="" '

        # Handle secure_boot_image parameter
        if '--secure_boot_image=' in self.raw_options:
            # Use regex to find the value of secure_boot_image
            match = re.search(r'--secure_boot_image=(?:"([^"]+)"|\'([^\']+)\'|([^\s]+))', self.raw_options)
            if match:
                image_type = match.group(1) or match.group(2) or match.group(3)
                self.Logger.info(f"The image type is {image_type}")

                if image_type == "non_signed_image":
                    image_path = SecureBootConsts.NON_SECURE_IMAGE_PATH
                elif image_type == "sig_mismatch_prod_image":
                    image_path = SecureBootConsts.SIG_MISMATCH_PROD_IMAGE_PATH
                elif image_type == "sig_mismatch_dev_image":
                    image_path = SecureBootConsts.SIG_MISMATCH_DEV_IMAGE_PATH
                else:
                    raise ValueError(f"Invalid image type: {image_type}. The supported values are: "
                                     f"'non_signed_image', 'sig_mismatch_prod_image', 'sig_mismatch_dev_image'")

                http_image_path = HTTP_SERVER_NBU_NFS + image_path
                self.Logger.info(f"The target image path is {http_image_path}")

                # Add the target_image_list to the raw options
                self.raw_options += f' --target_image_list={http_image_path}'

                # Remove the secure_boot_image option
                self.raw_options = re.sub(r'--secure_boot_image=(?:"[^"]+"|\'[^\']+\'|[^\s]+)', '', self.raw_options)

                self.Logger.info(f"The raw options is {self.raw_options}")

        if self.test_type:
            if self.test_type != "default":
                self.raw_options = re.sub(r" -m \".+\"", "", self.raw_options)
                self.raw_options = re.sub(r" -m \S+", "", self.raw_options)
            if self.test_type == "yaml":
                self.raw_options += " -m yaml"

        if '--alluredir' not in self.raw_options:
            self.raw_options += ' --alluredir="/tmp/allure-results" '

        if '--allure_server_project_id' in self.raw_options:
            allure_proj_pytest_arg = ''
        else:
            setup_id = self.setup_name or os.environ.get('MARS_SETUP_ID')
            self.Logger.info(f"The setup id is {setup_id}")
            allure_proj = get_allure_project_id(setup_id, self.test_scripts, get_dut_name_only=False)
            allure_proj_pytest_arg = '--allure_server_project_id={}'.format(allure_proj)

        # If the test case contains a topology mark, add --topology parameter to the pytest raw option
        # This is to support topology variations
        sonic_mgmt_path = os.path.abspath(__file__).split('/')[0:-4]
        python3_file_path = '/'.join(sonic_mgmt_path + ["tests/python3_test_files.txt", ])
        test_script_path = self.test_scripts.split('::')[0]
        sonic_mgmt_path.extend(['tests', test_script_path])
        test_script_fullpath = '/'.join(sonic_mgmt_path)
        topology_mark_pattern = r'pytest\.mark\.topology\(.+\)'
        is_smartswitch_test = False
        try:
            with open(test_script_fullpath) as test_script_file:
                for line in test_script_file:
                    match = re.search(topology_mark_pattern, line)
                    if match and "smartswitch" in match.group(0):
                        is_smartswitch_test = True
                    if match:
                        if self.sonic_topo:
                            self.convert_topos(is_smartswitch_test)
                            self.raw_options += " --topology %s" % self.topology
                        break
        except Exception as e:
            self.Logger.info("Failed to add '--topology' option for test case {}, failure reason: {}".format(test_script_fullpath, repr(e)))

        pytest_bin_name = "python3 -m pytest"
        random_seed = int(time.time())

        if self.setup_name.endswith("-ha") or 'dualtor' in (self.sonic_topo):
            # HA and dualtor setups use setup_name and topo to form testbed name
            testbed = f'{self.setup_name}-{self.sonic_topo}'
        else:
            testbed = f'{self.dut_name}-{self.sonic_topo}'
        self.Logger.info(f"testbed: {testbed}")
        # support multiple duts for ha testbed
        self.dut_name_list = [temp_dut_name.strip() for temp_dut_name in self.dut_name.split(',')]
        if 'bobcat' in self.dut_name:
            duts = read_duts_from_testbed_yaml(testbed)
            self.Logger.info(f"duts :{duts}")
            for temp_dut_name in self.dut_name_list:
                duts.remove(temp_dut_name)
            dpu_duts = []
            for temp_dut_name in self.dut_name_list:
                dpu_duts.extend(get_installed_dpu_duts(temp_dut_name, duts, self.Players[0].player_ip, self.Logger))
            self.Logger.info(f" dpu duts: {dpu_duts}")
            self.Logger.info(f" self.run_test_on_dpu_only: {self.run_test_on_dpu_only}, {type(self.run_test_on_dpu_only)}")

        if self.run_test_on_dpu_only == "True":
            # dut_name will be replaced by dpu host name. It is to run the tests on dup for smartswitch
            random.seed(self.session_id)
            self.Logger.info(f"session_id :{self.session_id}")
            self.dut_name = random.choice(dpu_duts)
            self.Logger.info(f"the dpu dut is  :{self.dut_name}")

        if self.sonic_topo == "bmc-dual-mgmt":
            # BMC tests run against the BMC host, not the switch. Mars provides the bare switch
            # dut_name (e.g. slm-chipless-2700a1-146), while the BMC host registered in
            # ansible/inventory (and as the testbed 'dut:' entry) is '<dut>-bmc'. The testbed name
            # is already '<dut>-bmc-dual-mgmt' (computed above from dut_name + sonic_topo), so only
            # --host-pattern (driven by dut_name) needs the -bmc suffix.
            self.Logger.info(f"BMC mode: original dut_name={self.dut_name}, testbed={testbed}")
            self.dut_name = f'{self.dut_name}-bmc'
            self.Logger.info(f"BMC mode: overridden dut_name={self.dut_name}")

        if is_smartswitch_test:
            if '--dpu-pattern' in self.raw_options:
                # For tests that require exact number of DPUs(e.g. HA tests)
                # it should be specified in the cases file
                if len(self.dut_name_list) > 1:
                    self.raw_options = self.raw_options.replace('dut-a-dpu-', self.dut_name_list[0] + '-dpu-')
                    self.raw_options = self.raw_options.replace('dut-b-dpu-', self.dut_name_list[1] + '-dpu-')
                else:
                    self.raw_options = self.raw_options.replace('dut-dpu-', self.dut_name_list[0] + '-dpu-')
            else:
                # For general smartswitch tests, we provide all available DPUs
                self.raw_options += f" --dpu-pattern {','.join(dpu_duts)}"

        # The test script file must come first, see explaination on https://github.com/Azure/sonic-mgmt/pull/2131
        cmd = "{PYTEST_BIN_NAME} {SCRIPTS} --inventory=\"../ansible/inventory,../ansible/veos\" --host-pattern {DUT_NAME} --module-path \
               ../ansible/library/ --testbed {TESTBED} --setup_name={SETUP_NAME} --testbed_file ../ansible/testbed.yaml \
               --allow_recover  --session_id {SESSION_ID} --mars_key_id {MARS_KEY_ID} \
               --junit-xml {REPORT_FILE} --assert plain {OPTIONS} {ALLURE_PROJ} --skip_sanity --dynamic_update_skip_reason --random_seed={RANDOM_SEED} --store_la_logs --ignore_la_failure -o junit_family=legacy --rootdir={SONIC_MGMT_PATH}/tests"
        cmd = cmd.format(PYTEST_BIN_NAME=pytest_bin_name,
                         SCRIPTS=self.test_scripts,
                         DUT_NAME=self.dut_name,
                         SONIC_TOPO=self.sonic_topo,
                         SETUP_NAME=self.setup_name,
                         SESSION_ID=self.session_id,
                         MARS_KEY_ID=self.mars_key_id,
                         REPORT_FILE=self.report_file,
                         OPTIONS=self.raw_options,
                         ALLURE_PROJ=allure_proj_pytest_arg,
                         RANDOM_SEED=random_seed,
                         TESTBED=testbed,
                         SONIC_MGMT_PATH=self.sonic_mgmt_path
                         )
        # Take the first epoint as just one is specified in *.setup file. Currently supported are: SONIC_MGMT or NGTS
        # Take the first player as just one is specified in *.setup file
        epoint = self.EPoints[0]
        player = self.Players[0]

        self.Logger.info("Starting pytest on sonic-mgmt player")
        dic_args = self._get_dic_args_by_running_stage(RunningStage.RUN)
        dic_args["epoint"] = epoint
        for _ in range(self.num_of_processes):
            epoint.Player.putenv("PYTHONPATH", "/devts/")
            epoint.Player.testPath = os.path.join(self.sonic_mgmt_path, "tests")
            epoint.Player.add_remote_test_path(epoint.Player.testPath)
            epoint.Player.run_process(cmd, shell=True, disable_realtime_log=False, delete_files=False)
            # Sleep needed to get logs if tests were not executed or even were not collected and exited immediately.
            time.sleep(2)
        rc = player.wait() or rc
        if rc == ErrorCode.NO_COLLECTION:
            rc = 0  # In case no tests are collected, should not fail mars step
        player.remove_remote_test_path(player.testPath)
        return rc

    def run_post_commands(self):
        self.collect_allure_report_data()

        for player in self.Players:
            try:
                self.Logger.info("Connecting to %s" % player.player_ip)
                conn = connect(player.player_ip)
                self.Logger.info("Connected to %s, socket: %s" % (player.player_ip, str(conn)))

                json_dir = os.path.join(self.json_root_dir, self.session_id)
                if not os.path.isdir(json_dir):
                    self.Logger.info("Creating directory %s" % json_dir)
                    os.mkdir(json_dir, 0o755)
                local_report_file = os.path.join(json_dir, self.mars_key_id + ".xml")

                self.Logger.info("Downloading %s from player to %s" % (self.report_file, local_report_file))
                download(conn, self.report_file, local_report_file)
                self.Logger.info("Downloaded report to %s" % local_report_file)

                self.dump_metadata(self._parse_junit_xml(open(local_report_file).read()))
            except Exception as e:
                self.Logger.error(repr(e))
                self.Logger.warning("Failed to get junit xml test report %s from remote player" % self.report_file)
        return ErrorCode.SUCCESS

    def convert_topos(self, is_smartswitch_test=False):
        # Convert the topology name to topology type(for example, t0-64 to t0)
        # and append type "any" for 'any' type in the topology mark
        if self.sonic_topo == "t1-smartswitch-ha":
            topos.append('t1-smartswitch-ha')
            return
        testbed_type_index = 0
        topos = [self.sonic_topo.split('-')[testbed_type_index]]
        topos.append("any")
        topos.append("util")  # this is only for test_pretest and test_nbr_health
        # Need to add t0 for dualtor topology as some community dualtor tests only mark topology as t0
        if 'dualtor' in self.sonic_topo:
            topos.append('t0')
        if 'bobcat' in self.dut_name and is_smartswitch_test:
            topos.append('smartswitch')
        self.topology = ",".join(topos)

    def collect_allure_report_data(self):
        self.Logger.info('Going to upload allure data to server')

        cmd = 'PYTHONPATH=/devts /ngts_venv/bin/python {}/ngts/scripts/allure_reporter.py --action upload --setup_name {}'.format(self.sonic_mgmt_path, self.setup_name)
        self.Logger.info('Running cmd: {}'.format(cmd))
        self.EPoints[0].Player.run_process(cmd, shell=True, disable_realtime_log=False, delete_files=False)

        self.Players[0].wait()
        self.Logger.info('Finished upload allure data to server')

    def is_python3_script(self, script_name, python3_script_file):
        with open(python3_script_file) as file:
            file_contents = [line.replace("\n", "") for line in file.readlines()]
        return script_name in file_contents


def read_duts_from_testbed_yaml(testbed_name):
    """Read yaml testbed info file."""
    duts = []
    with open(TESTBED_YAML_FILE) as f:
        tb_info = yaml.safe_load(f)
        for tb in tb_info:
            if tb["conf-name"] == testbed_name:
                duts = tb.pop("dut")
                break
    return duts


def get_installed_dpu_duts(dut_name, dpu_duts, player_ip, logger):
    installed_dpus_file_path = f"/root/mars/workspace/sonic-mgmt/installed_dpus_{dut_name}"
    logger.info(f"installed_dpus_file_path is {installed_dpus_file_path}")
    conn = connect(player_ip)
    if conn.modules.os.path.exists(installed_dpus_file_path):
        with conn.builtins.open(installed_dpus_file_path, 'r') as f:
            installed_dpus = f.read().split(',')
        logger.info(f"installed_dpus is {installed_dpus}")
        installed_dpu_duts = []
        for dpu in installed_dpus:
            dpu_rename = f'dpu{dpu.split("dpu")[0]}-{dpu.split("dpu")[1]}'
            for dpu_dut in dpu_duts:
                if dpu_dut.startswith(dut_name) and dpu_dut.endswith(dpu_rename):
                    installed_dpu_duts.append(dpu_dut)
                    break
        logger.info(f"installed_dpu_duts is {installed_dpu_duts}")
        return installed_dpu_duts
    else:
        return dpu_duts


if __name__ == "__main__":
    run_pytest = RunPytest("RunPytest")
    run_pytest.execute(sys.argv[1:])
