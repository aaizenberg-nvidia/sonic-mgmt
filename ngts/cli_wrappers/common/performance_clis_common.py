import gzip
import logging
import os
import re
import json
import fcntl
import pandas as pd
from retry import retry
from ngts.constants.constants import BugHandlerConst
from ngts.constants.performance_constants import PerfConsts, MongoDbConsts, ValidationConsts, MultiNosSharedData
from ngts.helpers.performance.port_selection import (PortSelection, build_port_selection, SWP,
                                                     get_cli_port_selection_options)
from ngts.nvos_tools.infra.FilesTool import FilesTool
from devts.infra.tools.exceptions.test_issue import TestIssue


def _file_is_gzip(filepath):
    """Return True if the file begins with gzip magic bytes (``GZIP_MAGIC_BYTES``; RFC 1952).

    Uses content sniffing rather than the filename extension so misnamed files are handled
    correctly.

    Args:
        filepath (str): Path to the local file to inspect.

    Returns:
        bool: True if the first two bytes match gzip signature.
    """
    with open(filepath, 'rb') as f:
        return f.read(len(PerfConsts.GZIP_MAGIC_BYTES)) == PerfConsts.GZIP_MAGIC_BYTES


def _read_sdk_dump_support_gzip(path):
    """Read a local file as UTF-8 text, gzip-decompressing if content has gzip magic bytes.

    Call this after copying a dump from the DUT: the remote name may be ``*.gz`` or not;
    decoding follows the actual bytes, not the extension.

    Args:
        path (str): Local filesystem path.

    Returns:
        str: File contents as text.
    """
    if _file_is_gzip(path):
        with gzip.open(path, 'rt', encoding='utf-8', errors='replace') as f:
            return f.read()
    with open(path, encoding='utf-8', errors='replace') as f:
        return f.read()


class PerformanceCommon:
    PORT_STYLE = SWP

    def __init__(self, topology_obj, engine, dut_alias, cli_obj):
        self.topology_obj = topology_obj
        self.engine = engine
        self.dut_alias = dut_alias
        self.cli_obj = cli_obj
        self.port_selection = PortSelection(port_style=self.PORT_STYLE)
        self._port_selection_params = None
        self.excluded_port_names = set()

    def set_port_selection_params(self, params):
        """Store raw port-selection parameters until the scenario is known.

        Args:
            params: Setup name, mode flags, and optional config path.
        """
        self._port_selection_params = params

    def resolve_port_selection(self, scenario):
        """Resolve port selection for a performance scenario.

        Args:
            scenario: Scenario key in the port-selection configuration.

        Returns:
            The resolved selection, inactive when no mode was enabled.
        """
        params = self._port_selection_params or get_cli_port_selection_options()
        self.port_selection = build_port_selection(
            setup_name=params.get("setup_name"), scenario=scenario,
            exclude_enabled=params.get("exclude_enabled", False),
            include_enabled=params.get("include_enabled", False),
            config_path=params.get("config_path"), port_style=self.PORT_STYLE)
        logging.info(f"[{self.dut_alias}] Port selection resolved for scenario '{scenario}': "
                     f"mode={self.port_selection.mode} (active={self.port_selection.is_active()})")
        return self.port_selection

    def get_chip_type(self):
        """Return the DUT chip type from Noga metadata when available."""
        if getattr(self, "chip_type", None):
            return self.chip_type
        try:
            return self.topology_obj.players[self.dut_alias]['attributes'].noga_query_data['attributes']['Specific'][
                'chip_type']
        except (AttributeError, KeyError, TypeError):
            return None

    def get_power_sensor_env_variables(self):
        """Return validator env vars for power sensor collection."""
        if self.get_chip_type() == "SPC6":
            return [f'{PerfConsts.SENSORS_CMD_ENV_VAR}="{PerfConsts.SPC6_SENSORS_CMD}"']
        return []

    def configure_dummy_acls(self, template_path, dut_ports, num_acls):
        """Configure dummy ACLs for pipeline lookup stress. Must be implemented by child class."""
        raise NotImplementedError

    def remove_dummy_acls(self):
        """Remove all dummy ACLs. Must be implemented by child class."""
        raise NotImplementedError

    # Mandatory functions to be implemented by child class
    def get_cmd_for_sdk(self, cmd, env_variables=None):
        """
        This method should be implemented in child class
        Returns:
        This method should return a cmd that is running on the sdk per OS
        """
        raise NotImplementedError

    @staticmethod
    def get_controllers_info_dicts_list(sensors_output):
        """
        returns voltage/current per controller
        Args:
            sensors_output: a string with the output of sensors command

        Returns:
        A list of dicts, each dict contains the values of a controller on the device i.e,
        [{'vout1': 1.20, 'vout2': 1.20, 'iout1': 13.00, 'iout2': 94.00},...]
        """
        raise NotImplementedError

    def apply_configuration_file(self, scenario, conf_args, template_suite=PerfConsts.DEFAULT_PERF_TEMPLATES_DIR):
        """
        This method should be implemented in child class
        """
        raise NotImplementedError

    def get_configuration_file(self, scenario, conf_args, template_suite=PerfConsts.DEFAULT_PERF_TEMPLATES_DIR):
        """
        This method should be implemented in child class
        """
        raise NotImplementedError

    def unsplit_all_ports(self):
        """
        Unsplit all ports before test configuration.
        Default implementation does nothing (for SONiC/Cumulus).
        Overridden in DvsPerformance for DVS-specific behavior.
        """
        pass

    def validate_ingress_buffer_mode_active(self):
        """Assert ingress buffer mode (IBM) is active. NVUE-only; no-op on other CLIs."""
        pass

    def validate_rebalancer_buffer_mode_active(self):
        """Assert automatic/rebalancer buffer mode is active. NVUE-only; no-op on other CLIs."""
        pass

    def save_configuration_file(self, conf_path, conf_json, dst_dut_dir="/tmp"):
        """
        This method should be implemented in child class
        """
        raise NotImplementedError

    def save_basic_configuration(self, players):
        """
        This method should be implemented in child class
        """
        raise NotImplementedError

    def get_player_ports(self, dst_dut_dir="/tmp"):
        """
        This method should be implemented in child class
        """
        raise NotImplementedError

    def restore_basic_configuration(self):
        """
        This method should be implemented in child class
        """
        raise NotImplementedError

    def get_tg_unconnected_ports(self):
        """
        This method should be implemented in child class
        """
        raise NotImplementedError

    def get_dut_ports(self):
        """
        This method should be implemented in child class
        """
        raise NotImplementedError

    def get_sdk_ports(self, ports_list):
        """
        This method should be implemented in child class
        """
        raise NotImplementedError

    def get_traffic_parameters(self, scenario, conf_args):
        """

        Args:
            scenario: name of the scenario, i.e, spcx_ra
            conf_args: a dict with the configuration arguments per test

        Returns:
        player traffic params based on the scenario configuration file, i.e,
        {
                "MAC" : {"src" : "aa:bb:cc:dd:ee:ff", "dst" : "aa:bb:cc:dd:ee:ff"},
                "IP   : {"src" : "11.11.11.11", "dst" : "22:22:22:22"},
                -------------    OR   -------------
                "IP"  :  {"src" : "1::1", "dst" : "2::2"}
                "UDP" : {"src": int, "dst": int},
                "AR"  : 0/1
            }
        """
        raise NotImplementedError

    def get_os_ports_name_mapping(self):
        """
        This method should be implemented in child class
        Returns:
        a list of dicts with os port name for each port
        i.e,
        [{'osPortName': 'Ethernet0', 'port': '0x100f1'},...]
        """
        raise NotImplementedError

    # Optional Functions to be implemented by child class for topology object support
    def get_player_unconnected_connected_ports_aliases(self):
        """
        This method should be implemented in child class
        """
        pass

    def get_player_left_right_ports_aliases(self):
        """
        This method should be implemented in child class
        """
        pass

    # Optional Functions

    def validate_no_drops_on_tg_ports(self):
        """
        This Method is checking if any packets were dropped on the traffic genrators
        Mloop ports, indicating that the test full traffic pattern did not reach the dut
        TODO: Should be implemented in DVS and Cumulus
        """
        return []

    def wait_for_nexthop_resolution(self, conf_args=None, number_of_nexthops=None, timeout=120):
        """
        Wait for the number of nexthops to be resolved on the dut
        Implemented for Cumulus only
        """
        pass

    def logrotate(self, daemon=''):
        '''
        This method is optional for dvs and sonic but mandatory for cumulus
        Returns:
        This method should return a cmd that rotates the log before running sdk tests.
        '''
        pass

    def check_mloops_up(self):
        """
        This method is used to check if the mloops are up on the traffic generator
        and if not, it will wait for them to be up
        """
        pass

    def execute_cmd(self, cmd, print_output=False):
        """
        All functions on the inheritance classes (DVS, SONiC, Cumulus) will be executed
        by a thread running on 'left-tg', 'dut' and 'right-tg' switch.
        to have each thread write it's log into individualise file we are setting the logging
        class prior and post the thread run using redirect_thread_stdout.
        since run_cmd use logger and not logging we use this wrapper to get the output
        :param cmd: Command to be executed on the switch
        :return: None or raise error
        """
        try:
            output = self.engine.run_cmd(cmd, print_output=print_output, validate=True)
            logging.debug(f"command output: {output}")
            return output
        except TestIssue as e:
            error_msg = f"Command: {cmd} failed on {self.dut_alias} with error:\n{e}\n"
            logging.error(error_msg)
            raise TestIssue(msg=error_msg) from e

    @staticmethod
    def _parse_ports_dump(ports_dump, bonus_ports_to_remove=0):
        """Parse sx_api_ports_dump.py output and classify ports as connected/unconnected.

        Extracts (log_port, oper_state, module_state) from each row of the dump table.
        Ports with module_state == "PLUGGED" are connected; all others are unconnected.
        ASIC-specific bonus ports are trimmed from the tail of the list.

        Args:
            ports_dump: Raw string output of sx_api_ports_dump.py.
            bonus_ports_to_remove: Number of trailing rows to discard (management/bonus ports).

        Returns:
            tuple: (sorted_connected, sorted_unconnected) — each a sorted list of
                   SDK logical port numbers as ints.
        """
        _log_port_hex = r"\|\s+(0x[\da-fA-F]+)\|"
        _four_numeric_columns = r"(?:\s+\d+\|){4}"
        _two_skip_columns = r"(?:\s+\S+\|){2}"
        _oper_state = r"\s+(\S+)\|"
        _module_state = r"\s+(\S+)\|"
        port_status_list = re.findall(
            _log_port_hex + _four_numeric_columns + _two_skip_columns + _oper_state + _module_state,
            ports_dump,
        )
        if bonus_ports_to_remove > 0:
            port_status_list = port_status_list[:-bonus_ports_to_remove]
        connected, unconnected = [], []
        for log_port, _, module_s in port_status_list:
            log_port_int = int(log_port, 16)
            if module_s == "PLUGGED":
                connected.append(log_port_int)
            else:
                unconnected.append(log_port_int)
        return sorted(connected), sorted(unconnected)

    def _save_player_ports(self, player_ports, dst_dut_dir="/tmp"):
        """Save player ports dict to local JSON and copy to the switch.

        Args:
            player_ports: dict with 'connected_ports' and 'unconnected_ports' lists.
            dst_dut_dir: Remote directory for tg_ports.json (default /tmp).
        """
        get_ports_output = os.path.join(BugHandlerConst.NGTS_PATH, "performance_tests",
                                        f"{self.dut_alias}_ports.json")
        with open(get_ports_output, "w") as f:
            json.dump(player_ports, f, indent=4)
        self.execute_cmd(f"sudo rm -f {dst_dut_dir}/tg_ports.json")
        self.engine.copy_file(source_file=get_ports_output, file_system=dst_dut_dir, dest_file="tg_ports.json",
                              overwrite_file=True, verify_file=False, direction='put')

    def get_port_mapping_df(self):
        sdk_port_to_local_port_mapping = {}
        sdk_port_speed_mapping = {}
        ports_dump_cmd = "sx_api_ports_dump.py"
        output = self.execute_cmd(self.get_cmd_for_sdk(ports_dump_cmd))
        ports_info = re.findall(r"(0x[\d|\w]{5})\|\s+\d+\|\s+\d+\|\s+(\d+)\|.*\|\s+(\d+)G", output)
        for sdk_port, local_port, speed in ports_info:
            sdk_port_to_local_port_mapping[sdk_port] = str(hex(int(local_port) - 1))
            sdk_port_speed_mapping[sdk_port] = int(speed)
        return sdk_port_to_local_port_mapping, sdk_port_speed_mapping

    @retry(exceptions=TestIssue, tries=2, delay=2)
    def configure_mloops(self, validate_mloops=True, is_simx=False):
        try:
            logging.info(f"Configure Mloop on {self.dut_alias}")
            self.logrotate("rsyslog")
            env_variables = [f"{PerfConsts.SEND_FWS_ENV}={'false' if is_simx else 'true'}"]
            configure_mloops_cmd = f"{PerfConsts.DVS_RUN_TEST_PATH} --names {PerfConsts.DVS_TG_MLOOP_CONFIGURATION}"
            self.execute_cmd(self.get_cmd_for_sdk(configure_mloops_cmd, env_variables=env_variables))
            if validate_mloops:
                self.check_mloops_up()
        except Exception as e:
            logging.warning(f"Failed to configure Mloop on {self.dut_alias}: {e}")
            raise TestIssue(msg=f"Failed to configure Mloop on {self.dut_alias}: {type(e).__name__}: {e}") from e

    @retry(exceptions=TestIssue, tries=2, delay=10)
    def run_traffic(self, scenario, traffic_jsons):
        self.cli_obj.interface.clear_counters()
        json_path = traffic_jsons[self.dut_alias]
        traffic_json_path = self.copy_traffic_json_to_player(scenario, json_path)
        self.set_tg_json_env_var(traffic_json_path)
        logging.info("Running traffic onto the device")
        run_traffic_cmd = f"{PerfConsts.DVS_RUN_TEST_PATH} --names {PerfConsts.DVS_TG_NAME}"
        self.logrotate("rsyslog")
        try:
            self.execute_cmd(self.get_cmd_for_sdk(run_traffic_cmd, env_variables=[f'TG_JSON={traffic_json_path}']))
        except Exception as e:
            logging.error(f"Error running traffic: {e}")
            raise TestIssue(msg=f"Error running traffic: {type(e).__name__}: {e}") from e

    def _create_sdk_dump_dirs(self):
        """Create dump directories required by the SDK TrafficValidator.

        The SDK daemon (sx_sdk) may run with systemd PrivateTmp, giving it an
        isolated /tmp.  The daemon writes dump files into its private /tmp,
        but the SDK test process (TrafficValidator) runs outside that namespace
        and needs to read those files.

        The solution is by finding the daemon's actual private tmp path and
        creating symlinks from /tmp/dump_* to the physical location.  This
        lets both the daemon and the test process access the same files.
        If PrivateTmp is not detected, we use nsenter into the daemon's mount
        namespace as a safe default (handles PrivateTmp regardless of whether
        we can discover the path).
        """
        dump_dir_names = ["dump_with_perf_counters", "dump_without_perf_counters"]
        try:
            priv_tmp = self._find_private_tmp()
            if priv_tmp:
                for d in dump_dir_names:
                    self.execute_cmd(f"sudo mkdir -p {priv_tmp}/{d}")
                    self.execute_cmd(f"sudo ln -sfnT {priv_tmp}/{d} /tmp/{d}")
                return
        except Exception:
            logging.warning("Symlink approach failed, falling back to nsenter + plain mkdir")

        self._create_sdk_dump_dirs_via_nsenter(dump_dir_names)

    def _find_private_tmp(self):
        """Find the systemd PrivateTmp path for the SDK service.

        The systemd-private-* directories under /tmp are owned by root with
        mode 0700, so both the glob expansion and the listing must run as
        root.  We use ``sudo bash -c '...'`` so the shell that expands the
        glob is already privileged.
        """
        for service in ("sx_sdk", "switchd"):
            try:
                path = self.execute_cmd(
                    f"sudo bash -c 'ls -d /tmp/systemd-private-*-{service}.service-*/tmp 2>/dev/null | head -1'"
                ).strip()
                if path:
                    return path
            except Exception:
                continue
        return None

    def _create_sdk_dump_dirs_via_nsenter(self, dump_dir_names):
        """Enter the SDK daemon mount namespace and create dump directories."""
        dump_dirs = " ".join(f"/tmp/{d}" for d in dump_dir_names)
        try:
            self.execute_cmd(
                f"sudo bash -c 'PID=$(pgrep -ox sx_sdk || pgrep -ox switchd) && "
                f"nsenter -m -t $PID -- mkdir -p {dump_dirs} || "
                f"mkdir -p {dump_dirs}'"
            )
        except Exception as e:
            raise TestIssue(msg=f"Failed to create SDK dump directories: {e}") from e

    @retry(exceptions=TestIssue, tries=2, delay=2)
    def validate_traffic(self, json_path, samples_params_dict, dst_dut_dir="/tmp"):
        logging.info("Running traffic validator on the dut")
        self._create_sdk_dump_dirs()
        env_variables = self.get_power_sensor_env_variables()
        for env_var_name, param_val in samples_params_dict.items():
            set_interval_cmd = f"export {env_var_name}={param_val}"
            env_variables.append(f"{env_var_name}={param_val}")
            self.execute_cmd(set_interval_cmd)
        run_validator_cmd = f"{PerfConsts.DVS_RUN_TEST_PATH} --names {PerfConsts.DVS_TG_VALIDATOR_NAME}"
        self.logrotate("rsyslog")
        try:
            self.execute_cmd(self.get_cmd_for_sdk(run_validator_cmd, env_variables=env_variables))
        except Exception as e:
            logging.error(f"Error running traffic validator: {e}")
            raise TestIssue(msg=f"Error running traffic validator: {type(e).__name__}: {e}") from e

        source_path = os.path.join(dst_dut_dir, "TrafficValidator.json")
        try:
            self.engine.copy_file(source_file="TrafficValidator.json", file_system=dst_dut_dir, dest_file=json_path,
                                  overwrite_file=True, verify_file=False, direction='get')
        except Exception as e:
            logging.error(f"Error copying validation file from {source_path} on DUT to {json_path}: {e}")
            raise TestIssue(msg=f"Error copying validation file from DUT: {type(e).__name__}: {e}") from e

        if not os.path.exists(json_path):
            raise TestIssue(f"Traffic validation file was not created at {json_path} after copy from DUT. "
                            f"The file may not exist on the DUT at {source_path}.")

    @retry(exceptions=TestIssue, tries=2, delay=2)
    def stop_traffic(self):
        logging.info(f"Remove Mloop configuration from {self.dut_alias}")
        remove_mloops_cmd = f"{PerfConsts.DVS_RUN_TEST_PATH} --names {PerfConsts.DVS_TG_REMOVE_MLOOP_CONFIGURATION}"
        self.logrotate("rsyslog")
        try:
            self.execute_cmd(self.get_cmd_for_sdk(remove_mloops_cmd))
        except Exception as e:
            logging.error(f"Error stopping traffic: {e}")
            raise TestIssue(msg=f"Error stopping traffic: {type(e).__name__}: {e}") from e

    def copy_traffic_json_to_player(self, scenario, json_path, dst_dut_dir="/tmp"):
        file_name = f"{scenario.replace('/', '_')}_traffic.json"
        traffic_json_path = os.path.join(dst_dut_dir, file_name)
        logging.info(f"Copy Traffic JSON to : {traffic_json_path} on {self.dut_alias}")
        self.engine.copy_file(source_file=json_path, file_system=dst_dut_dir, dest_file=file_name,
                              overwrite_file=True, verify_file=False)
        return traffic_json_path

    def set_tg_json_env_var(self, traffic_json_path):
        logging.info(f"set TG_JSON ={traffic_json_path} on {self.dut_alias}")
        set_traffic_json_cmd = f"export TG_JSON=\"{traffic_json_path}\""
        self.execute_cmd(set_traffic_json_cmd)

    def get_test_specific_values(self, testname):
        """

        Args:
            testname: returns all the test values stored during the test run,
            and adds any OS specific info

        Returns:
            a json obj with the test info, i.e,
            {
                "testName": "test_ar_perf_max_bandwidth[4096-IPv6]",
                "timeStamp": "23-02-2025 14:09:25",
                "configurationName": "x2_400G", ...
            }
        """
        with open(os.path.join(PerfConsts.REQUIRMENTS_DIR, f"{testname}_info_dump.json")) as f:
            test_specific_values = json.load(f)
        return test_specific_values

    def get_sensors_data(self):
        sensors_cmd = (PerfConsts.SPC6_SENSORS_CMD if self.get_chip_type() == "SPC6"
                       else PerfConsts.DEFAULT_SENSORS_CMD)
        output = self.execute_cmd(sensors_cmd)
        return output

    def convert_port_group_df_to_dict(self, port_group_df):
        """
        Convert list of dicts format [{"port":"0x101f1", "portGroupName": "uplink"},..]
        to dict format {"uplink": [all ports], ...} using pandas
        """
        df = pd.DataFrame(port_group_df)
        port_group_df_dict = df.groupby(MongoDbConsts.PORT_GROUP_NAME)[ValidationConsts.PORT].apply(list).to_dict()
        for port_group_name, ports in port_group_df_dict.items():
            port_group_df_dict[port_group_name] = [int(port, PerfConsts.HEX_BASE) for port in ports]
        return {ValidationConsts.PORT_GROUPS: port_group_df_dict}

    def update_port_group_df_on_dut(self, port_group_df):
        """
        Convert list of dicts format [{"port":"0x101f1", "portGroupName": "uplink"},..]
        to dict format {"uplink": [65777, 65778, ...], ...}
        """
        port_group_df_dict = self.convert_port_group_df_to_dict(port_group_df)
        port_group_df_file = "conf.json"
        full_path = os.path.join(PerfConsts.CONFIG_FILES_DIR, port_group_df_file)
        with open(full_path, 'w') as f:
            json.dump(port_group_df_dict, f)
        self.engine.copy_file(source_file=full_path,
                              dest_file=port_group_df_file,
                              file_system='/tmp',
                              direction='put'
                              )

    def retrieve_default_route(self):
        """
        Retrieve the default route on the the setup
        """
        try:
            retrieve_default_route_cmd = 'ip route | grep default'
            output = self.execute_cmd(retrieve_default_route_cmd)
            return output
        except Exception as e:
            logging.warning(f"Error retrieving default route: {e}")
            return "No route found"

    def restart_daemon(self, daemon):
        """
        This method is optional for dvs and sonic but mandatory for cumulus
        it restarts the daemon on the dut
        """
        pass

    def run_customer_examples_on_sdk(self, example_name):
        """
        Execute a customer example script on the SDK.

        Args:
            example_name (str): The name of the example script to run (e.g., "sx_api_flex_acl_dump.py").

        Returns:
            The result of executing the command via execute_cmd().
        """
        return self.execute_cmd(self.get_cmd_for_sdk(example_name))

    def create_acl_dump(self):
        """
        Generate an ACL dump using the SDK API examples.

        This method runs the sx_api_flex_acl_dump.py script to create a dump of
        all the ACL configurations.

        Returns:
            The output from running the ACL dump command.
        """
        create_acl_dump_cmd = "sx_api_flex_acl_dump.py"
        return self.run_customer_examples_on_sdk(create_acl_dump_cmd)

    def modify_pg_buffer_for_connected_ports(self):
        """
        Modify PG buffer configuration for connected ports only.

        This method runs a sys_sdk test that modifies the buffer configuration for
        specified Priority Groups on all connected ports. It preserves all existing buffer
        parameters and only modifies the pipeline_latency_size and max_borrowed_delta values.

        The modification sets:
        - override_default_max_borrowed_delta: True
        - pipeline_latency_size: User-defined value
        - max_borrowed_delta: User-defined value

        Note: This method assumes pg_buffer_config is already in /tmp/conf.json from apply_test_configuration.
        The configuration is read from the existing conf.json file by the sys_sdk test.

        Returns:
            str: The output from running the buffer modification test.

        Raises:
            TestIssue: If pg_buffer_config is missing, required fields are missing,
                      or the buffer modification fails.
        """
        logging.info(f"[{self.dut_alias}] Running PG buffer modification from existing conf.json")

        modify_pg_buffer_cmd = (f"{PerfConsts.DVS_RUN_TEST_PATH} --names "
                                f"{PerfConsts.DVS_MODIFY_PG_BUFFER_CONNECTED_PORTS}")

        try:
            result = self.execute_cmd(self.get_cmd_for_sdk(modify_pg_buffer_cmd))
        except TestIssue as e:
            raise TestIssue(msg=f"[{self.dut_alias}] Failed to modify PG buffer for connected ports: {e}") from e

        return result

    def get_occ_watermark_per_port_dump(self, sonic_mgmt_path, tar_file_name="occ_headroom_per_port.tar.gz",
                                        tar_file_system='/tmp'):
        """
        Copy occupancy and watermark data tar archive from DUT to local machine.

        This method copies the tar archive file that was created during validation
        from the DUT to the local management system.

        Args:
            sonic_mgmt_path (str): The local path where the tar file will be copied.
            tar_file_name (str, optional): The name of the tar file on the DUT.
                                           Defaults to "occ_headroom_per_port.tar.gz".
            tar_file_system (str, optional): The remote filesystem path where the tar file is located.
                                             Defaults to '/tmp'.

        Returns:
            str: The local path to the copied tar file.
        """
        logging.info(f"Copying occupancy and watermark dump from DUT: {tar_file_system}/{tar_file_name} "
                     f"to {sonic_mgmt_path}")

        self.engine.copy_file(source_file=tar_file_name, file_system=tar_file_system,
                              dest_file=sonic_mgmt_path, overwrite_file=True, verify_file=False, direction='get')

        return sonic_mgmt_path

    def create_sdk_dump(self, sonic_mgmt_path, sdk_dump_file_system=PerfConsts.SDK_DUMP_FILE_SYSTEM):
        """
        Generate an SDK debug dump and retrieve its contents.

        Runs "sx_api_dbg_generate_dump.py" with an explicit base path. The SDK may write the dump with or without .gz

        Args:
            sonic_mgmt_path (str): Local path for the copied dump
            sdk_dump_file_system (str, optional): Remote directory for the dump.
                Defaults to ``PerfConsts.SDK_DUMP_FILE_SYSTEM``.

        Returns:
            str: The contents of the SDK dump file as a string.

        Raises:
            TestIssue: If neither ``{basename}.gz`` nor ``{basename}`` exists after generation.
        """
        basename = PerfConsts.SDK_DUMP_REMOTE_BASENAME
        remote_dump_path = os.path.join(sdk_dump_file_system, basename)
        create_sdk_dump_cmd = f"sx_api_dbg_generate_dump.py {remote_dump_path}"
        self.run_customer_examples_on_sdk(create_sdk_dump_cmd)

        remote_gz = os.path.join(sdk_dump_file_system, f'{basename}.gz')
        remote_plain = os.path.join(sdk_dump_file_system, basename)
        if FilesTool.file_exists(self.engine, remote_gz):
            remote_name = f'{basename}.gz'
        elif FilesTool.file_exists(self.engine, remote_plain):
            remote_name = basename
        else:
            raise TestIssue(
                f'No SDK dump file on DUT under {sdk_dump_file_system!r} '
                f'(expected {remote_gz!r} or {remote_plain!r}).'
            )

        os.environ[PerfConsts.SDK_DUMP_FILE_SYSTEM] = sonic_mgmt_path
        os.makedirs(os.path.dirname(sonic_mgmt_path), exist_ok=True)
        self.engine.copy_file(source_file=remote_name, file_system=sdk_dump_file_system,
                              dest_file=sonic_mgmt_path,
                              overwrite_file=True, verify_file=False, direction='get')

        return _read_sdk_dump_support_gzip(sonic_mgmt_path)

    def get_pg_buffer_attributes_from_sdk_dump(self, sdk_dump_str, port, pg=0):
        """
        Parse SDK dump text to extract buffer attributes for a specific port and PG.

        The SDK dump contains pipe-delimited tables per port like:
            Port Data PGs 0x10001
            ---...---
            PG|Pool|Size|HR Size|...|PL Size|...|MBD|
            ---...---
             0|   2|   0|   106|...|    106|...|1600|

        Extracts HR Size (headroom size) and MBD (max borrowing depth)
        for the specified PG from the port's table.

        Args:
            sdk_dump_str: The SDK dump contents as a string (from create_sdk_dump)
            port: Port name (e.g. 'Ethernet0') — will be converted to SDK hex port
            pg: Priority group number (default 0)

        Returns:
            dict: {'hr_size': int or None, 'mbd': int or None} in cells
        """
        sdk_port = self.get_sdk_port(port)
        port_section_pattern = re.compile(rf'Port Data PGs\s+{re.escape(sdk_port)}\b', re.IGNORECASE)

        result = {'hr_size': None, 'mbd': None, 'max_hr_util': None}
        required_columns = {'PG', 'HR Size', 'MBD', 'Max HR Util'}
        lines = sdk_dump_str.splitlines()
        in_port_section = False
        header_indices = {}

        for line in lines:
            if port_section_pattern.search(line):
                in_port_section = True
                header_indices = {}
                continue

            if not in_port_section:
                continue

            stripped = line.strip()

            # Skip separator lines
            if stripped.startswith('---'):
                continue

            columns = [col.strip() for col in stripped.split('|')]

            # Parse header row to find column indices by splitting on '|' first
            if not header_indices:
                if required_columns.issubset(set(columns)):
                    for i, col in enumerate(columns):
                        header_indices[col] = i
                    continue

            # Parse data rows once we have the header
            if header_indices:
                if not columns or not columns[0]:
                    continue
                try:
                    row_pg = int(columns[header_indices['PG']])
                except (ValueError, KeyError, IndexError):
                    # End of table or non-data row — stop searching this port section
                    in_port_section = False
                    continue
                if row_pg == pg:
                    try:
                        result['hr_size'] = int(columns[header_indices['HR Size']])
                        result['mbd'] = int(columns[header_indices['MBD']])
                        result['max_hr_util'] = int(columns[header_indices['Max HR Util']])
                    except (ValueError, KeyError, IndexError) as e:
                        logging.warning(f"Failed to parse PG{pg} buffer attributes for {port}: {e}")
                    break

        logging.info(f"SDK dump PG buffer attributes for {port} ({sdk_port}) PG{pg}: "
                     f"hr_size={result['hr_size']}, mbd={result['mbd']}, max_hr_util={result['max_hr_util']}")
        return result

    def write_shared_json(self, key=None, json_path='/tmp', data=None, raise_on_existing=True):
        """
        Write a value for a specific key to the shared JSON file.
        Args:
            key (str): Key to write/update
            json_path (str): Path to the shared JSON file
            data (any): Data to store under the key
            raise_on_existing (bool): If True, raise KeyError if key already exists.
                                      If False, override the existing value. Default is True.
        Raises:
            KeyError: If the key already exists and raise_on_existing is True
        """
        file_name = MultiNosSharedData.DEFAULT_SHARED_JSON
        full_path = os.path.join(json_path, file_name)

        mode = 'r' if os.path.exists(full_path) else 'w'
        with open(full_path, mode, encoding='utf-8') as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                if os.path.exists(full_path) and os.stat(full_path).st_size > 0:
                    with open(full_path, 'r', encoding='utf-8') as f:
                        content = json.load(f)
                else:
                    content = {}

                if key in content and raise_on_existing:
                    raise KeyError(f"Key '{key}' already exists in the shared JSON file.")
                content[key] = data

                with open(full_path, 'w', encoding='utf-8') as f:
                    json.dump(content, f, indent=2)
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)

        self.engine.copy_file(source_file=full_path, dest_file=file_name, file_system=json_path,
                              overwrite_file=True, verify_file=True, direction='put')

    def read_shared_json(self, key, json_path='/tmp'):
        """
        Read the value for a specific key from the shared JSON file.
        Args:
            key (str): Key to read
            json_path (str): Path to the shared JSON file
        Returns:
            The value stored under the key, or None if not found
        Raises:
            FileNotFoundError: If the JSON file does not exist
        """

        file_name = MultiNosSharedData.DEFAULT_SHARED_JSON
        full_path = os.path.join(json_path, file_name)
        self.engine.copy_file(source_file=file_name, dest_file=json_path, file_system=json_path,
                              overwrite_file=True, verify_file=True, direction='get')

        with open(full_path, 'r') as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            try:
                content = json.load(f)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
        return content.get(key, None)

    def cleanup_shared_json_file(self, json_path='/tmp'):
        """
        Create an empty (zero-byte) shared JSON file at the given path.
        If no path is provided, use the default from PerfConsts.
        Args:
            json_path (str): Path to the shared JSON file. If None, use default.
        """
        file_name = MultiNosSharedData.DEFAULT_SHARED_JSON
        full_path = os.path.join(json_path, file_name)

        open(full_path, 'w').close()

        self.engine.copy_file(source_file=full_path, dest_file=file_name, file_system=json_path, direction='put')

    def dynamic_configuration_helper(self, scenario, performance_parameters):
        """
        This method is used to apply the dynamic configuration on the dut
        """
        pass

    def update_dst_mac_address(self, src_port, dut_mac_addresses, traffic_parameters):
        """
        This method is used to update the dst mac address on the traffic parameters
        Implemented for Nvue only
        """
        pass

    def configure_interfaces_mac_neighbor(self, vlan_interface_configuration_dict):
        """
        This method is used to configure the mac neighbor on the dut
        Implemented for SONiC only
        """
        pass

    def set_shaper(self, speed, shaper_value, shaper_profile="default-global"):
        """
        This method is used to set the shaper on the traffic gen
        """
        pass

    def validate_ets(self, interface_list, queues_list, violations_list):
        """
        This method is used to validate the ETS on the dut
        """
        pass

    def configure_incremental_dips_on_tg(self):
        """
        This method is used to create the incremental dips on the traffic generator
        """
        raise NotImplementedError
