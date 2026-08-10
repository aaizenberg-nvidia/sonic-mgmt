import json
import logging
import os
import pprint
import tempfile
import yaml
import re
import random
from retry import retry
from retry.api import retry_call
import copy
import allure
from collections import defaultdict
from devts.infra.tools.exceptions.test_issue import TestIssue
from devts.infra.tools.exceptions.real_issue import RealIssue
from ngts.constants.constants import BugHandlerConst, ResultUploaderConst
from ngts.constants.performance_constants import MongoDbConsts, PerfConsts, Cl_Consts, ValidationConsts
from ngts.cli_wrappers.common.performance_clis_common import PerformanceCommon
from ngts.helpers.performance.nvue_port_count import (get_nvue_data_physical_ports_count,
                                                      get_nvue_expected_nexthops)
from ngts.helpers.performance.sensors_power_parse import build_controllers_info_dicts_list
from jinja2 import Environment, FileSystemLoader
from ngts.helpers.performance.traffic_helpers import generate_ip_address_list, is_ipv6, address_calculator
from ngts.helpers.performance.port_selection import (resolve_symmetric_cascade,
                                                     set_resolved_excluded_dut_ports,
                                                     get_resolved_excluded_dut_ports,
                                                     clear_resolved_excluded_ports,
                                                     record_port_selection_debug)
from time import sleep

_LOGICAL_SWP_SPLIT_RE = re.compile(r"^swp\d+s\d+$")
# ``nv show interface`` table row: ``swp1s0  up  up`` (port, admin, oper).
_NV_SHOW_INTERFACE_LINE_RE = re.compile(r"^(swp\d+(?:s\d+)?)\s+(\S+)\s+(\S+)")
_SPC6_SERVICE_PORT_PARENT_NAMES = {"swp65"}


class _SwpPortsNotReady(Exception):
    """Sentinel raised while polling switch-port admin/oper readiness."""


class _LldpNeighborsNotReady(Exception):
    """Sentinel raised while polling LLDP readiness."""


def cumulus_ports_already_logical_split(ports):
    """Return whether all supplied Cumulus ports are logical breakout children.

    Args:
        ports: Iterable of interface names.

    Returns:
        False for an empty list; otherwise True only for ``swpNsM`` names.
    """
    if not ports:
        return False
    return all(_LOGICAL_SWP_SPLIT_RE.match(str(port)) for port in ports)


def get_cumulus_logical_split_factor(ports):
    """Infer the common breakout factor from logical child port names.

    Examples:
        ``['swp1s0', 'swp1s1', 'swp2s0', 'swp2s1']`` -> ``2``
        ``['swp1s0', 'swp2s0']`` -> ``1`` (SPC4/5 after ``breakout 1x`` init)

    Args:
        ports: Iterable of interface names.

    Returns:
        int or None: Common factor when every port is a child and every parent
        exposes the same number of lanes; otherwise ``None``.
    """
    if not cumulus_ports_already_logical_split(ports):
        return None

    children_by_parent = defaultdict(set)
    for port in ports:
        match = re.match(r"^(swp\d+)s(\d+)$", str(port))
        children_by_parent[match.group(1)].add(int(match.group(2)))

    factors = {max(indices) + 1 for indices in children_by_parent.values()}
    if len(factors) != 1:
        return None
    return factors.pop()


def cumulus_ports_match_requested_split(ports, split):
    """Return True when ``ports`` are already broken out to ``split``.

    Unlike :func:`cumulus_ports_already_logical_split`, this rejects a mismatch
    such as SPC4/5 ``breakout 1x`` ports (``swpNs0`` only) when the test asks
    for ``split=2``. Treating those as "already split" previously skipped the
    parent breakout stanza; ``nv config replace`` then wiped the live breakout
    and NVUE rejected the child ports.

    Args:
        ports: Iterable of interface names.
        split: Requested breakout factor.

    Returns:
        bool: True only when the inferred factor equals ``int(split)``.
    """
    factor = get_cumulus_logical_split_factor(ports)
    return factor is not None and factor == int(split)


def sort_swp_split_port_names(ports):
    """Sort ``swpNsM`` (and plain ``swpN``) by numeric N then M for stable YAML ordering."""

    def _key(port_name):
        name = str(port_name)
        match = re.match(r"^swp(\d+)s(\d+)$", name)
        if match:
            return (int(match.group(1)), int(match.group(2)))
        match = re.match(r"^swp(\d+)$", name)
        if match:
            return (int(match.group(1)), -1)
        return (10**9, 0)

    return sorted(ports, key=_key)


def get_swp_parent_port_names(ports):
    """Return unique parent ``swpN`` names from plain or split Cumulus interface names.

    Split child names such as ``swp1s0`` / ``swp1s1`` collapse to ``swp1`` so readiness
    checks and split math operate on physical parent ports rather than breakout indices.
    Plain ``swpN`` names pass through unchanged.
    """

    parent_ports = []
    seen = set()
    for port in sort_swp_split_port_names(ports):
        name = str(port)
        match = re.match(r"^(swp\d+)s\d+$", name)
        parent_port = match.group(1) if match else name
        if parent_port not in seen:
            parent_ports.append(parent_port)
            seen.add(parent_port)
    return sort_swp_split_port_names(parent_ports)


def get_swp_ports_for_split(ports, split):
    """Return the interface names expected after applying ``split`` to parent ports."""

    split = int(split)
    parent_ports = get_swp_parent_port_names(ports)
    if split == 1:
        return parent_ports
    return [f"{port}s{index}" for port in parent_ports for index in range(split)]


def get_srv6_left_ports_count(lldp_ports, split_left):
    """Return logical left-side port count using the Cumulus template rules."""
    if cumulus_ports_match_requested_split(lldp_ports, split_left):
        return len(sort_swp_split_port_names(lldp_ports))
    return len(get_swp_ports_for_split(lldp_ports, split_left))


def get_srv6_dut_left_ports_num(performance_cli, split_left, timeout=180, interval=10):
    """Wait for DUT LLDP and return the rendered SRv6 left-side port count.

    Args:
        performance_cli: DUT NVUE performance wrapper.
        split_left: Requested left-side breakout.
        timeout: Maximum LLDP wait in seconds.
        interval: Poll interval in seconds.

    Returns:
        Logical left-side port count.

    Raises:
        TestIssue: If left-side LLDP remains empty.
    """
    tries = max(1, timeout // interval)

    def _poll_lldp_left_ports():
        left_lldp_ports = performance_cli.get_right_left_ports_dict()['left_ports']
        if not left_lldp_ports:
            logging.info("Waiting for DUT left-side LLDP neighbors before computing dut_left_ports_num")
            raise _LldpNeighborsNotReady()
        return get_srv6_left_ports_count(left_lldp_ports, split_left)

    try:
        return retry_call(_poll_lldp_left_ports, tries=tries, delay=interval,
                          logger=logging.getLogger())
    except _LldpNeighborsNotReady:
        raise TestIssue(
            f"DUT left-side LLDP neighbors not populated after {timeout}s; "
            "cannot compute dut_left_ports_num for SRv6 addressing")


def validate_no_unsupported_service_port_split(ports, split, context):
    """Raise if an SN6600 service port would be moved away from its supported 2x split."""

    split = int(split)
    if split == 2:
        return True
    service_ports = sorted(set(get_swp_parent_port_names(ports)) & _SPC6_SERVICE_PORT_PARENT_NAMES)
    if service_ports:
        raise ValueError(f"{context}: SN6600 service ports {service_ports} support only 2x breakout")
    return True


def validate_no_overlapping_swp_parent_ports(first_ports, second_ports, context):
    """Raise if two logical port groups collapse to the same parent ``swpN`` ports."""

    first_parents = set(get_swp_parent_port_names(first_ports))
    second_parents = set(get_swp_parent_port_names(second_ports))
    overlapping_ports = sorted(first_parents & second_parents, key=sort_swp_split_port_names)
    if overlapping_ports:
        raise ValueError(f"{context}: overlapping parent ports {overlapping_ports} would render duplicate "
                         f"NVUE interface keys. Check DUT-facing LLDP ports and mloop/down ports.")
    return True


def find_null_nvue_set_values(configuration):
    """Return paths whose values are null within NVUE set operations.

    Args:
        configuration: Parsed NVUE YAML configuration.

    Returns:
        List of dotted paths to null values under top-level ``set`` operations.
    """
    null_paths = []

    def _find_null_values(value, path):
        if value is None:
            null_paths.append(path)
        elif isinstance(value, dict):
            for key, child_value in value.items():
                _find_null_values(child_value, f"{path}.{key}")
        elif isinstance(value, list):
            for index, child_value in enumerate(value):
                _find_null_values(child_value, f"{path}[{index}]")

    operations = enumerate(configuration) if isinstance(configuration, list) else [(None, configuration)]
    for index, operation in operations:
        if not isinstance(operation, dict) or "set" not in operation:
            continue
        path = f"[{index}].set" if index is not None else "set"
        _find_null_values(operation["set"], path)
    return null_paths


class NvuePerformanceCli(PerformanceCommon):

    def __init__(self, topology_obj, engine, dut_alias, cli_obj):
        super().__init__(topology_obj, engine, dut_alias, cli_obj)
        self.port_groups = {}
        self.mac = self.cli_obj.general.get_dut_mac_address()
        self.dut_neighbor_dict = {}
        self.ports = []
        self.connected_ports = []
        self.unconnected_ports = []
        self.ports_mapping = {}
        self.sdk_ports_mapping = {}
        self.mloops = []
        self._perf_conf_args = {}
        self._perf_scenario = None

    def set_class_vars(self):
        self.ports = self.get_player_ports()
        self.connected_ports = self.ports["connected_ports"]
        self.unconnected_ports = self.ports["unconnected_ports"]
        self.port_groups = self.get_right_left_ports_dict()
        self.get_os_ports_name_mapping()

    def wait_for_lldp_neighbors(self, timeout=60, interval=10):
        """Wait until all required performance-topology LLDP neighbors are visible.

        Args:
            timeout: Maximum LLDP convergence wait in seconds.
            interval: Poll interval in seconds.

        Returns:
            Mapping of expected neighbor aliases to local interfaces.

        Raises:
            TestIssue: If any required neighbor remains absent.
        """
        if self.dut_alias == PerfConsts.DUT_ALIAS:
            expected_neighbors = PerfConsts.PERF_SETUP_TG_ALIASES
        elif self.dut_alias in PerfConsts.PERF_SETUP_TG_ALIASES:
            expected_neighbors = [PerfConsts.DUT_ALIAS]
        else:
            expected_neighbors = []
        if not expected_neighbors:
            return {}

        tries = max(1, timeout // interval)
        last_neighbors = {}
        missing_neighbors = list(expected_neighbors)

        def _poll_neighbors():
            nonlocal last_neighbors, missing_neighbors
            last_neighbors = self.cli_obj.interface.filter_lldp_neighbors(expected_neighbors)
            missing_neighbors = [neighbor for neighbor in expected_neighbors if not last_neighbors.get(neighbor)]
            if missing_neighbors:
                logging.info(
                    f"Waiting for LLDP neighbors on {self.dut_alias}: missing={missing_neighbors}, "
                    f"visible={last_neighbors}")
                raise _LldpNeighborsNotReady()
            return last_neighbors

        try:
            return retry_call(_poll_neighbors, tries=tries, delay=interval, logger=logging.getLogger())
        except _LldpNeighborsNotReady as error:
            raise TestIssue(
                f"Required LLDP neighbors not populated on {self.dut_alias} after {timeout}s: "
                f"missing={missing_neighbors}, visible={last_neighbors}") from error

    def unsplit_all_ports(self):
        """Initialize NVUE ports before LLDP-based performance templates are rendered."""
        asic_model = self.cli_obj.general.get_asic_model(self.engine)
        if asic_model == "Spectrum-6":
            logging.info("Keeping Spectrum-6 ports in their existing breakout and bringing them up")
            self.cli_obj.general.detach_config(self.engine)
            ports = self.cli_obj.interface.bring_all_existing_swp_ports_up()
            logging.info(f"Brought up {len(ports)} existing Spectrum-6 swp interfaces")
            self.engine.run_cmd("nv set system wjh state disabled")
            self.cli_obj.general.apply_config(self.engine, option="-y", verify_execution=True)
            logging.info("Disabled WJH on Spectrum-6 session baseline")
            self.wait_for_all_swp_ports_admin_up()
            return
        logging.info("Initializing physical ports")
        self.cli_obj.interface.initialize_physical_ports()
        self.wait_for_all_swp_ports_admin_up()

    def apply_configuration_file(self, scenario, conf_args, template_suite=PerfConsts.DEFAULT_PERF_TEMPLATES_DIR, dst_dir=Cl_Consts.CL_HOME_DIR):
        self._perf_conf_args = conf_args or {}
        self._perf_scenario = scenario
        src_file = self.get_configuration_file(scenario, conf_args, template_suite)
        logging.info(f"Applying configuration file on {self.dut_alias}")
        self.engine.copy_file(source_file=src_file, file_system=dst_dir,
                              dest_file="tmp.yaml", overwrite_file=True, verify_file=False)
        full_path = os.path.join(dst_dir, "tmp.yaml")
        self.cli_obj.general.replace_config(self.engine, full_path, output_type="json", verify_execution=True)
        self.cli_obj.general.apply_config(self.engine, option="-y", verify_execution=True)
        logging.info(f"The configuration file on {self.dut_alias} was applied successfully")
        # TODO: validate admin-up wait timeouts/behavior on SPC6 SRv6 lab runs (post-breakout bring-up)
        self.wait_for_all_swp_ports_admin_up()
        self.ports = self.retry_get_player_ports()
        self.connected_ports = self.ports["connected_ports"]
        self.unconnected_ports = self.ports["unconnected_ports"]
        self.port_groups = self.get_right_left_ports_dict()
        self.get_os_ports_name_mapping()

    def _refresh_port_groups_from_lldp(self, timeout=60, interval=10):
        """Refresh left/right port groups once LLDP neighbors are visible on the DUT."""
        tries = max(1, timeout // interval)

        def _poll_port_groups():
            self.port_groups = self.get_right_left_ports_dict()
            left_ports = self.port_groups.get("left_ports", [])
            right_ports = self.port_groups.get("right_ports", [])
            if not left_ports or not right_ports:
                logging.info(
                    "Waiting for DUT LLDP neighbors before updating conf.json: "
                    f"left_ports={left_ports}, right_ports={right_ports}")
                raise _LldpNeighborsNotReady()
            return self.port_groups

        try:
            return retry_call(_poll_port_groups, tries=tries, delay=interval, logger=logging.getLogger())
        except _LldpNeighborsNotReady:
            raise TestIssue(
                f"DUT LLDP neighbors not populated after {timeout}s; "
                f"left_ports={self.port_groups.get('left_ports', [])}, "
                f"right_ports={self.port_groups.get('right_ports', [])}")

    def _update_dut_port_group_conf(self):
        """Push current DUT port groups to /tmp/conf.json for TrafficValidator."""
        if self.dut_alias != PerfConsts.DUT_ALIAS:
            return
        self._refresh_port_groups_from_lldp()
        port_group_df = []
        for port_group_name, port_list in self.port_groups.items():
            if not port_list:
                continue
            for port in self.get_sdk_ports(port_list):
                port_group_df.append({
                    ValidationConsts.PORT: port,
                    MongoDbConsts.PORT_GROUP_NAME: port_group_name,
                })
        if not port_group_df:
            raise TestIssue("Cannot update /tmp/conf.json: no SDK ports resolved for DUT port groups")
        self.update_port_group_df_on_dut(port_group_df)

    def _uses_custom_validator_port_groups(self):
        """Return True when the active scenario owns custom TrafficValidator port groups.

        SRv6 pushes ``ingress_ports`` / ``egress_ports`` (and related names) to
        ``/tmp/conf.json`` inside each test. Refreshing default left/right groups during
        ``validate_traffic`` would overwrite those entries and break SRv6 thresholds.
        """
        scenario = self._perf_scenario or (self._perf_conf_args or {}).get("scenario")
        return scenario == "srv6"

    def wait_for_all_swp_ports_admin_up(self, timeout=180, interval=10):
        """Wait until all non-bonus switch ports shown by NVUE are admin up.

        Polls ``nv show interface`` every ``interval`` seconds. Raises ``TestIssue`` after
        ``timeout`` with the last set of ports still admin-down (bonus/service ports excluded).
        """
        bonus_ports = set(self.cli_obj.interface.get_bonus_ports(self.engine))
        bonus_parent_ports = set(get_swp_parent_port_names(bonus_ports))
        self._wait_for_swp_ports_ready(bonus_parent_ports, require_oper_up=False,
                                       timeout=timeout, interval=interval, state_label="admin")

    def wait_for_all_swp_ports_oper_up(self, timeout=180, interval=10):
        """Wait until all non-bonus switch ports shown by NVUE are admin and oper up.

        Polls ``nv show interface`` every ``interval`` seconds. Raises ``TestIssue`` after
        ``timeout`` with the last down-port snapshot so logs show which interfaces blocked readiness.
        """
        bonus_ports = set(self.cli_obj.interface.get_bonus_ports(self.engine))
        bonus_parent_ports = set(get_swp_parent_port_names(bonus_ports))
        self._wait_for_swp_ports_ready(bonus_parent_ports, require_oper_up=True,
                                       timeout=timeout, interval=interval, state_label="oper")

    def _wait_for_swp_ports_ready(self, bonus_parent_ports, require_oper_up, timeout, interval,
                                  state_label):
        """Poll NVUE until non-bonus swp ports meet admin/oper requirements."""
        tries = max(1, timeout // interval)
        last_down_ports = []

        def _poll_once():
            nonlocal last_down_ports
            last_down_ports = self.get_down_swp_ports(bonus_parent_ports, require_oper_up=require_oper_up)
            if last_down_ports:
                logging.info(
                    f"Waiting for switch ports to become {state_label} up on {self.dut_alias}: "
                    f"{last_down_ports}")
                raise _SwpPortsNotReady()
            logging.info(f"All switch ports are {state_label} up on {self.dut_alias}")

        try:
            retry_call(_poll_once, tries=tries, delay=interval, logger=logging.getLogger())
        except _SwpPortsNotReady:
            raise TestIssue(
                f"Ports did not become {state_label} up on {self.dut_alias} after {timeout}s: "
                f"{last_down_ports}")

    def get_down_swp_ports(self, bonus_parent_ports, require_oper_up=True):
        """Return non-bonus ``swp`` ports that are not admin/oper up per ``nv show interface``.

        Parses each line of NVUE tabular output with ``_NV_SHOW_INTERFACE_LINE_RE``:
        group 1 is the interface name (``swpN`` or split ``swpNsM``), group 2 is admin
        state, group 3 is oper state. Bonus/service parent ports are excluded.
        """
        output = self.execute_cmd("nv show interface", print_output=False)
        # Ports intentionally selected out are excluded from routing and may be oper-down
        # (their peer is unconfigured); skip them so readiness waits don't block. Returns an
        # empty set when no port selection is in effect (backward compatible).
        excluded_parents = set(get_swp_parent_port_names(self._get_device_excluded_swp_names()))
        down_ports = []
        for line in output.splitlines():
            match = _NV_SHOW_INTERFACE_LINE_RE.match(line.strip())
            if not match:
                continue
            port, admin_state, oper_state = match.groups()
            parent_port = get_swp_parent_port_names([port])[0]
            if parent_port in bonus_parent_ports:
                continue
            # Selected-out ports are intentionally removed from config, so they may be
            # admin/oper down; do not block readiness waits on them.
            if parent_port in excluded_parents:
                continue
            if admin_state != "up" or (require_oper_up and oper_state != "up"):
                down_ports.append(f"{port}(admin={admin_state}, oper={oper_state})")
        return down_ports

    def _get_device_excluded_swp_names(self):
        """Return the set of swp ports that should be treated as intentionally-down on this
        device because of port selection.

        Option A (decoupled) design: the physical/mloop layer is left uniform; exclusion only
        removes ports from **DUT routing** (and validation). Consequently:

        - **DUT**: its own excluded ports (from the symmetric cascade) have no L3/route and are
          down — return the cached ``excluded_port_names``.
        - **TG**: the DUT-facing port opposite an excluded DUT port goes oper-down (its DUT
          peer is unconfigured). Because that port is down it is absent from LLDP, so we match
          it by **name** using the DUT's cached excluded names (read-only, in-process — never a
          TG→DUT engine call). Name symmetry here is parent-level; the readiness-wait skip is a
          safety allowance, not a routing decision, so a superset match is harmless.

        Returns an empty set when the selection is inactive.
        """
        if self.dut_alias == PerfConsts.DUT_ALIAS:
            if not self.port_selection.is_active():
                return set()
            if not self.excluded_port_names:
                self.get_right_left_ports_dict()
            return set(self.excluded_port_names)
        # TG: read the DUT-published excluded names from the module-level store (robust to
        # object identity between players/topology_obj and to this TG's own port_selection
        # state; populated on the main thread by the priming step before the parallel apply).
        # Empty when no exclusion is in effect (backward compatible).
        return get_resolved_excluded_dut_ports()

    def configure_mloops(self, validate_mloops=True, is_simx=False):
        super().configure_mloops(validate_mloops=validate_mloops, is_simx=is_simx)
        if validate_mloops:
            # TODO: validate oper-up wait after mloop config on SPC6 SRv6 (may be too strict for mloop ports)
            self.wait_for_all_swp_ports_oper_up()

    @retry(exceptions=Exception, tries=3, delay=5)
    def retry_get_player_ports(self):
        self.ports = None
        self.ports = self.get_player_ports()
        connected_ports = self.ports["connected_ports"]
        unconnected_ports = self.ports["unconnected_ports"]
        if (not unconnected_ports) and (self.dut_alias == PerfConsts.DUT_ALIAS):
            return self.ports
        elif len(connected_ports) != len(unconnected_ports):
            logging.warning(f"The number of connected ports {len(connected_ports)} is not equal to the number of unconnected ports {len(unconnected_ports)}")
            logging.warning(f"The connected ports are {connected_ports}")
            logging.warning(f"The unconnected ports are {unconnected_ports}")
            logging.warning(f"Retrying to get the player ports")
            raise Exception("The number of connected ports is not equal to the number of unconnected ports, Retrying once again ..")
        return self.ports

    def save_basic_configuration(self, players, dst_dir=Cl_Consts.CL_HOME_DIR):
        logging.info(f"Saving the basic configuration on {self.dut_alias}")
        self.cli_obj.general.save_config(self.engine)
        self.engine.run_cmd(f"sudo rm {dst_dir}/startup.yaml")
        self.engine.run_cmd(f"sudo cat /etc/nvue.d/startup.yaml >> {dst_dir}/startup.yaml")

    def restore_basic_configuration(self, file_name="startup.yaml", config_directory=Cl_Consts.CL_HOME_DIR):
        self.cleanup_shared_json_file()
        # Clear published port-selection exclusion state so it can't leak into a later test.
        clear_resolved_excluded_ports()
        if self.port_selection.is_active():
            # Remove the validator port-selection config we wrote so it can't leak to a later
            # (possibly full-scale) run and wrongly pin its groups/line-rate.
            try:
                self.execute_cmd("sudo rm -f /tmp/conf.json")
            except Exception as e:
                logging.warning(f"[{self.dut_alias}] Could not remove /tmp/conf.json: {e}")
        logging.info("Replacing the basic configuration on the device")
        full_path = config_directory + "/" + file_name
        self.cli_obj.general.replace_config(self.engine, full_path, output_type="json", verify_execution=True)
        self.cli_obj.general.apply_config(self.engine, option="-y", verify_execution=True)

    def get_configuration_file_path(self, scenario, template_suite=PerfConsts.DEFAULT_PERF_TEMPLATES_DIR):
        full_path = os.path.join(BugHandlerConst.NGTS_PATH, "performance_tests",
                                 template_suite, scenario, "cumulus", f"{self.dut_alias}.yaml")
        logging.info("Full Path returned is {}".format(full_path))
        return full_path

    def set_ibm(self, scenario, conf_args, chip_type):
        ibm_mode = True if conf_args["auto_buffer_mode"] == "False" else False
        if conf_args['params']:
            ctl = conf_args.get('params', {}).get("low_ar_thresh", PerfConsts.LOW_AR_THRESHOLD)
            ctm = conf_args.get('params', {}).get("med_ar_thresh", PerfConsts.MED_AR_THRESHOLD)
            cth = conf_args.get('params', {}).get("high_ar_thresh", PerfConsts.HIGH_AR_THRESHOLD)
        else:
            ctl = PerfConsts.LOW_AR_THRESHOLD if chip_type != "SPC6" else PerfConsts.LOW_AR_THRESHOLD_SPC6
            ctm = PerfConsts.MED_AR_THRESHOLD
            cth = PerfConsts.HIGH_AR_THRESHOLD

        # SDK AR buffer modes: SX_AR_BUFFER_MODE_INGRESS_E (ibm) and SX_AR_BUFFER_MODE_AUTO_E (rebalancer).
        # Cumulus switchd maps these to ar.ibm = ingress | auto.
        ibm_value = "ingress" if ibm_mode else "auto"
        logging.info(f"Set IBM mode to {ibm_mode} (ar.ibm = {ibm_value})")
        txt = "\n".join([
            "ar.p.m = 0",
            f"ar.ctl = {ctl}",
            f"ar.ctm = {ctm}",
            f"ar.cth = {cth}",
            "ar.srt = 10",
            "ar.srf = 10",
            "ar.p.bit = 0",
            "ar.p.frt = 4",
            "ar.p.but = 0",
            "ar.p.sfe = FALSE",
            "ar.p.ste = FALSE",
            "ar.p.ef = FALSE",
            "ar.ecs = 4096",
            f"ar.ibm = {ibm_value}"
        ])
        cmd = "echo \"echo -e \'{}\' > /etc/cumulus/switchd.d/ar_profile_custom.conf\" | sudo su".format(txt)
        self.execute_cmd(cmd)
        logging.info(f"Enabling the custom ar profile with ar.ibm = {ibm_value}.")
        logging.info(cmd)
        self.execute_cmd("nv set router adaptive-routing profile profile-custom")
        self.cli_obj.general.apply_config(self.engine, option="-y", verify_execution=True)
        return True

    def validate_ingress_buffer_mode_active(self):
        """Assert ingress buffer mode (IBM) is active: custom AR profile and ``ar.ibm = ingress`` in switchd.

        IBM is applied by :meth:`set_ibm` when ``auto_buffer_mode == \"False\"`` (NVUE ``profile-custom`` and
        ``ar.ibm = ingress`` in ``/etc/cumulus/switchd.d/ar_profile_custom.conf``; SDK ``SX_AR_BUFFER_MODE_INGRESS_E``).

        Raises:
            TestIssue: If NVUE profile is not ``profile-custom`` or the custom profile lacks IBM ingress.
        """
        profile_out = self.execute_cmd("nv show router adaptive-routing", print_output=False)
        profile_lower = profile_out.lower().replace("_", "-")
        if "profile-custom" not in profile_lower:
            raise TestIssue(
                "Ingress buffer mode (IBM) requires NVUE adaptive-routing profile 'profile-custom'. "
                f"Got from 'nv show router adaptive-routing': {profile_out!r}")

        conf_text = self.execute_cmd(
            "sudo test -f /etc/cumulus/switchd.d/ar_profile_custom.conf && "
            "sudo cat /etc/cumulus/switchd.d/ar_profile_custom.conf || echo ''",
            print_output=False)
        if not re.search(r"ar\.ibm\s*=\s*ingress", conf_text, re.IGNORECASE):
            raise TestIssue(
                "Ingress buffer mode (IBM) not set: /etc/cumulus/switchd.d/ar_profile_custom.conf "
                f"missing 'ar.ibm = ingress'. File contents: {conf_text!r}")

    def validate_rebalancer_buffer_mode_active(self):
        """Assert automatic/rebalancer buffer mode is active: custom AR profile with ``ar.ibm = auto``.

        ``set_ibm()`` selects automatic mode via NVUE ``profile-custom`` and
        ``ar.ibm = auto`` in ``/etc/cumulus/switchd.d/ar_profile_custom.conf``
        (SDK ``SX_AR_BUFFER_MODE_AUTO_E``).

        Raises:
            TestIssue: If NVUE profile is not ``profile-custom`` or the custom profile lacks ``ar.ibm = auto``.
        """
        profile_out = self.execute_cmd("nv show router adaptive-routing", print_output=False)
        profile_lower = profile_out.lower().replace("_", "-")
        if "profile-custom" not in profile_lower:
            raise TestIssue(
                "Automatic/rebalancer buffer mode requires NVUE adaptive-routing profile 'profile-custom'. "
                f"Got from 'nv show router adaptive-routing': {profile_out!r}")

        conf_text = self.execute_cmd(
            "sudo test -f /etc/cumulus/switchd.d/ar_profile_custom.conf && "
            "sudo cat /etc/cumulus/switchd.d/ar_profile_custom.conf || echo ''",
            print_output=False)
        if not re.search(r"ar\.ibm\s*=\s*auto", conf_text, re.IGNORECASE):
            raise TestIssue(
                "Automatic/rebalancer buffer mode not set: /etc/cumulus/switchd.d/ar_profile_custom.conf "
                f"missing 'ar.ibm = auto'. File contents: {conf_text!r}")

    def get_player_ports(self, dst_dut_dir="/tmp"):
        """Classify switch ports as connected or unconnected by parsing sx_api_ports_dump.py output.

        Runs sx_api_ports_dump.py on the switch, which produces a table like:

            | log_port | dev | local | phy | ... | oper  | module   |
            | 0x10001  |  1  |   1   |  0  | ... | DOWN  | UNPLUGGED|
            | 0x10041  |  1  |  65   | 64  | ... | UP    | PLUGGED  |

        The regex extracts (log_port, oper_state, module_state) from each row.
        Ports with module_state == "PLUGGED" are classified as connected (cable present),
        all others as unconnected. ASIC-specific bonus ports are excluded from the tail.

        The result is also saved as tg_ports.json on the switch and locally as
        {dut_alias}_ports.json.

        Args:
            dst_dut_dir: remote directory for tg_ports.json (default /tmp)

        Returns:
            dict: {'connected_ports': [int, ...], 'unconnected_ports': [int, ...]}
                  where values are sorted SDK logical port numbers (decimal).
        """
        self.logrotate("rsyslog")
        logging.info("Getting player connected and unconnected ports")
        if self.ports:
            return self.ports
        asic = self.cli_obj.general.get_asic_model(self.engine)
        number_of_bonus_ports = len(Cl_Consts.BONUS_PORTS[asic])
        ports_dump = self.execute_cmd("sudo sx_api_ports_dump.py")
        connected, unconnected = self._parse_ports_dump(ports_dump, number_of_bonus_ports)
        player_ports = {"connected_ports": connected,
                        "unconnected_ports": unconnected}
        self.ports = player_ports
        self.connected_ports = connected
        self.unconnected_ports = unconnected
        self._save_player_ports(player_ports, dst_dut_dir)
        return player_ports

    def get_tg_unconnected_ports(self):
        player_ports = self.get_player_ports()
        return player_ports["unconnected_ports"]

    def run_traffic(self, scenario, traffic_jsons):
        """Re-scope the traffic JSON to the connected subset, then send.

        Belt-and-suspenders with the build-time scoping in ``get_traffic_parameters``: at send
        time the DUT cascade is fully resolved, so this rewrites the local traffic JSON so each
        stream's ``ports`` drops the mloops at the cascade's excluded sorted indices. No-op when
        no exclusion is in effect (backward compatible).
        """
        self._rescope_traffic_json_ports(traffic_jsons)
        super().run_traffic(scenario, traffic_jsons)

    def _rescope_traffic_json_ports(self, traffic_jsons):
        """Rewrite the local traffic JSON so each stream's ``ports`` = the scoped mloops."""
        if not get_resolved_excluded_dut_ports() or self.dut_alias not in traffic_jsons:
            return
        scoped_ports = self.get_tg_traffic_ports()
        path = traffic_jsons[self.dut_alias]
        try:
            with open(path) as f:
                data = json.load(f)
            changed = False
            for port_group in data.get("port_groups", []):
                for stream in port_group.get("stream_list", []):
                    if "ports" in stream and set(stream["ports"]) != set(scoped_ports):
                        stream["ports"] = scoped_ports
                        changed = True
            if changed:
                with open(path, "w") as f:
                    json.dump(data, f, indent=3)
        except (OSError, ValueError) as e:
            logging.warning(f"[{self.dut_alias}] Could not update traffic JSON scope {path}: {e}")

    def get_tg_traffic_ports(self):
        """Return the mloop SDK ports to generate test traffic on.

        Drops the mloops paired with this TG's DUT-facing ports that are cabled to excluded DUT
        ports, so no traffic is aimed at an excluded/not-routed DUT port (which would blackhole
        and, on the lossless class, trigger a fabric-wide PFC pause cascade).

        Correlation uses the **authoritative applied pairing**: on NVUE each DUT-facing port and
        its mloop share a bridge access VLAN (see the TG Jinja: ``dut_ports[k]`` and
        ``mloop_ports[k]`` both get access VLAN ``200+k``). So for each excluded DUT-facing port
        (this TG's connected port whose parent name matches a DUT-excluded parent), the mloop is
        the *other* member of its VLAN. This is exact (no assumed sorted-index) and
        granularity-correct for breakout (a parent's children each drop their own mloop), and
        needs no LLDP at traffic time, so it is robust whether the excluded DUT port is
        link-up-but-not-routed or fully down.

        No-op (returns all mloops) when no exclusion is in effect (backward compatible) or when
        the VLAN pairing cannot be read / nothing matches (fails open with a breadcrumb).
        """
        all_mloops = self.get_tg_unconnected_ports()
        excluded_dut_names = get_resolved_excluded_dut_ports()
        if not excluded_dut_names:
            return all_mloops
        # This TG's connected ports cabled to excluded DUT ports (by name/parent).
        excluded_connected = {name: hex(sdk) for sdk, name
                              in self._connected_ports_by_parent(excluded_dut_names).items()}
        if not excluded_connected:
            return all_mloops
        try:
            vlan_cfg = self.get_tg_interfaces_vlan_configuration()
        except Exception as e:
            logging.warning(f"[{self.dut_alias}] Could not read VLAN pairing ({type(e).__name__}"
                            f": {e}); generating on all mloops.")
            return all_mloops
        by_vlan = defaultdict(list)
        for name, vlan in vlan_cfg.items():
            by_vlan[vlan].append(name)
        mloop_sdk_set = set(all_mloops)
        drop_mloops = set()
        dropped_partners = {}
        # For each excluded DUT-facing port, drop its bridge-VLAN partner mloop.
        for name in excluded_connected:
            vlan = vlan_cfg.get(name)
            for other in by_vlan.get(vlan, []):
                if other == name:
                    continue
                osdk_hex = self.ports_mapping.get(other)
                if osdk_hex is None:
                    continue
                osdk_int = int(osdk_hex, 16)
                if osdk_int in mloop_sdk_set:
                    drop_mloops.add(osdk_int)
                    dropped_partners[other] = {"vlan": vlan, "sdk": osdk_hex}
        scoped = [m for m in all_mloops if m not in drop_mloops]
        dropped = sorted(drop_mloops)
        logging.info(f"[{self.dut_alias}] Traffic mloops {len(all_mloops)}->{len(scoped)}; "
                     f"excluded DUT-facing {sorted(excluded_connected)} (VLAN-paired) -> "
                     f"dropped mloops {dropped}")
        record_port_selection_debug("tg_traffic_scope", {
            "dut_alias": self.dut_alias,
            "excluded_dut_names": sorted(excluded_dut_names),
            "excluded_connected": excluded_connected,
            "dropped_mloop_partners": dropped_partners,
            "mloops_total": len(all_mloops),
            "mloops_scoped": len(scoped),
            "dropped_mloops": dropped,
        })
        return scoped

    def validate_traffic(self, json_path, samples_params_dict, dst_dut_dir="/tmp"):
        """Run TrafficValidator with scenario-appropriate DUT ``/tmp/conf.json`` port groups.

        Non-SRv6 scenarios refresh default left/right groups so SRv6-specific groups cannot
        leak into SPCX-RA validation. SRv6 tests push their own groups before validation and
        must not be overwritten here. When port selection is active, additionally writes
        filtered per-side SDK groups and the true per-port line rate.
        """
        if self.dut_alias == PerfConsts.DUT_ALIAS:
            if not self._uses_custom_validator_port_groups():
                self._update_dut_port_group_conf()
            self._write_validator_port_selection_config()
        super().validate_traffic(json_path, samples_params_dict, dst_dut_dir=dst_dut_dir)

    def _write_validator_port_selection_config(self, path="/tmp/conf.json"):
        """Write the SDK TrafficValidator config (filtered port groups + per-port line rate).

        Gated on an active selection so runs without ``--perf-exclude-ports`` /
        ``--perf-include-ports`` are byte-for-byte unchanged (the file is not written and the
        validator auto-discovers exactly as before). Fails open (logs, does not raise).
        """
        if not self.port_selection.is_active():
            return
        try:
            groups = self.get_right_left_ports_dict()
            left = self._side_child_sdk_ports(groups.get("left_ports", []))
            right = self._side_child_sdk_ports(groups.get("right_ports", []))
            speed_g = self._resolve_validator_line_rate_g()
            if not left or not right or not speed_g:
                logging.warning(f"[{self.dut_alias}] Skipping validator port-selection config "
                                f"(left={len(left)} right={len(right)} speed={speed_g}).")
                return
            conf = {"port_groups": {"left_ports": left, "right_Ports": right}, "speed": speed_g}
            payload = json.dumps(conf)
            self.execute_cmd(f"printf '%s' '{payload}' | sudo tee {path} > /dev/null")
            logging.info(f"[{self.dut_alias}] Wrote validator port-selection config {path}: "
                         f"left={len(left)} right={len(right)} ports, speed={speed_g}G")
            record_port_selection_debug("validator_conf", {
                "dut_alias": self.dut_alias, "speed_g": speed_g,
                "left_count": len(left), "right_count": len(right)})
        except Exception as e:
            logging.warning(f"[{self.dut_alias}] Could not write validator port-selection "
                            f"config ({type(e).__name__}: {e}); validator will auto-discover.")

    def _connected_ports_by_parent(self, parents):
        """Return ``{sdk_int: swp_name}`` for this device's connected (DUT-facing) ports whose
        parent name is in ``parents``.

        The single correlation primitive for port selection on this wrapper: it maps the
        device's connected SDK ports to names and keeps those whose parent matches (so a
        broken-out parent naturally matches all its children). Ensures the name<->SDK mapping
        is populated. Returns ``{}`` for an empty/None parent set.
        """
        wanted = set(get_swp_parent_port_names(list(parents))) if parents else set()
        if not wanted:
            return {}
        if not self.sdk_ports_mapping:
            self.get_os_ports_name_mapping()
        connected = self.connected_ports or self.get_player_ports()["connected_ports"]
        matched = {}
        for sdk_int in connected:
            name = self.sdk_ports_mapping.get(hex(int(sdk_int)))
            if name and get_swp_parent_port_names([name])[0] in wanted:
                matched[int(sdk_int)] = name
        return matched

    def _side_child_sdk_ports(self, side_parents):
        """Return this side's connected child SDK ports (sorted ints) for the (filtered) parent
        names. Excluded parents are absent from ``side_parents`` so their children are dropped."""
        return sorted(self._connected_ports_by_parent(side_parents))

    def _resolve_validator_line_rate_g(self):
        """Per-port line rate in Gbps for the validator's txRate normalization (e.g. 400).

        Derived from the configured port speed (``conf_args['speed']`` is in Kbps, e.g.
        ``"400000000"`` -> 400G). Returns 0 if it cannot be resolved (caller then skips)."""
        raw = self._perf_conf_args.get("speed")
        try:
            return int(int(raw) / 1_000_000)
        except (TypeError, ValueError):
            return 0

    def get_mloops_tuples_list(self):
        if self.mloops:
            return self.mloops
        else:
            self.check_mloops_up()
            return self.mloops

    def get_dut_ports(self, sdk_ports=False):
        mgmt_port = "eth0"
        bonus_ports = self.cli_obj.interface.get_bonus_ports(self.engine)
        if sdk_ports:
            player_ports = self.get_player_ports()
            return player_ports["connected_ports"]
        else:
            output = self.execute_cmd("nv sh interface physical -o json")
            try:
                output = json.loads(output)
            except json.JSONDecodeError as j:
                logging.error("Interface output is not a valid JSON object")
                logging.error(f"Output is : {output}")
                raise j
            list_of_ports = list(output.keys())
            list_of_ports.pop(list_of_ports.index(mgmt_port))
            for ports in bonus_ports:
                list_of_ports.pop(list_of_ports.index(ports))
            return list_of_ports

    def get_os_ports_name_mapping(self):
        """
        This method should be implemented in child class
        Returns:
        a list of dicts with os port name for each port
        i.e,
        [{'osPortName': 'Ethernet0', 'port': '0x100f1'},...]
        """
        os_ports_name_mapping = []
        dut_ports = self.get_dut_ports()
        sdk_ports = self.get_sdk_ports(dut_ports)
        for port, sdk_port in zip(dut_ports, sdk_ports):
            self.ports_mapping[port] = sdk_port
            os_ports_name_mapping.append({ValidationConsts.PORT: sdk_port,
                                          ValidationConsts.OS_PORT_NAME: port})
        self.sdk_ports_mapping = {v: k for k, v in self.ports_mapping.items()}
        return os_ports_name_mapping

    def get_cmd_for_sdk(self, cmd, env_variables=[]):
        variables = "sudo env "
        variables += " ".join(env_variables)
        return variables + ' ' + Cl_Consts.CL_PYTHON_PATH + ' ' + cmd

    def run_customer_examples_on_sdk(self, example_name):
        """
        This function runs a SDK example script on switch.
        This function overrides the base class run_customer_examples_on_sdk function,
        because on Cumulus, SDK example scripts (``sx_api_*.py``) must run with sudo twice  .
        First sudo to resolve the script from PATH, and second sudo to run the script.
        Args:
            example_name (str): The name of the example script to run (e.g., "sx_api_ports_dump.py").

        Returns:
            The result of executing the command via execute_cmd().
        """
        return self.execute_cmd(f"sudo {example_name}")

    def logrotate(self, daemon):
        logging.info(f"Rotating log for {daemon}")
        try:
            self.execute_cmd(f"sudo logrotate --force /etc/logrotate.d/{daemon}")
        except Exception as e:
            logging.warning(f"Failed to rotate log for {daemon}: {e}")

    def get_traffic_parameters(self, scenario, conf_args={}):
        if scenario == "srv6":
            traffic_parameters = {
                "ports": self.get_tg_traffic_ports(),
                "MAC": {"src": self.mac,
                        "dst": conf_args["dut_mac"]},
                "IP": {},
                "IPV6": {},
                PerfConsts.IP_PROTOCOL_UDP: {"src": PerfConsts.UDP_SOURCE_PORT, "dst": PerfConsts.ROCE_PORT},
                PerfConsts.IP_PROTOCOL_TCP: {"sport": PerfConsts.TCP_SOURCE_PORT, "dport": PerfConsts.TCP_DOURCE_PORT},
                "packet_size": conf_args["packet_size"],
                "is_ipv6": conf_args["is_ipv6"],
            }
        else:
            tg_regex = PerfConsts.TG_REGEX
            tg_alias = re.search(tg_regex, self.dut_alias).group(1)
            is_ipv6 = conf_args.get("is_ipv6", False)
            ip_key = "IPV6" if is_ipv6 else "IP"
            ip_dict = {
                "IP": {
                    PerfConsts.LEFT_TG_ALIAS: {"src": "4.4.4.4", "dst": "130.130.130.1"},
                    PerfConsts.RIGHT_TG_ALIAS: {"src": "4.4.4.4", "dst": "110.110.110.1"}
                },
                "IPV6": {
                    PerfConsts.LEFT_TG_ALIAS: {"src": "4::4", "dst": "130::1"},
                    PerfConsts.RIGHT_TG_ALIAS: {"src": "4::4", "dst": "110::1"}
                }
            }
            self.logrotate("rsyslog")
            traffic_parameters = {}
            if conf_args["split_left"] == 1:
                dst = self.topology_obj[0]['dut']['cli'].interface.get_interface_mac_address("swp1", verify_execution=True)
            else:
                dst = self.topology_obj[0]['dut']['cli'].interface.get_interface_mac_address("swp1s0", verify_execution=True)
            traffic_parameters["MAC"] = conf_args.get("MAC", {"src": "00:11:22:33:44:55", "dst": dst})
            traffic_parameters["IP"] = conf_args.get("IP", ip_dict[ip_key][self.dut_alias])
            traffic_parameters["UDP"] = conf_args.get("UDP", {"src": PerfConsts.UDP_SOURCE_PORT, "dst": PerfConsts.ROCE_PORT})
            traffic_parameters["AR"] = conf_args.get("AR", PerfConsts.ADAPTIVE_ROUTING_ENABLED)
            traffic_parameters["ports"] = self.get_tg_traffic_ports()
            traffic_parameters["packet_size"] = conf_args["packet_size"]
            traffic_parameters["num_packets"] = conf_args[f"{tg_alias}_num_packets"]
            traffic_parameters["is_ipv6"] = is_ipv6
        return traffic_parameters

    def set_ports(self, port_list: list, port_state):
        self.cli_obj.interface.set_ports_admin_state(port_list, port_state)

    def get_sdk_ports(self, ports_list: list):
        ports_string = " ".join(ports_list)
        if all(port in self.ports_mapping.keys() for port in ports_list):
            return [self.ports_mapping[port] for port in ports_list]
        else:
            self.engine.copy_file(source_file=f'{Cl_Consts.CL_LOG_PORT_FILE_PATH}/{Cl_Consts.CL_LOG_PORT_FILE}',
                                  dest_file=f'{Cl_Consts.CL_LOG_PORT_FILE}',
                                  file_system=Cl_Consts.CL_HOME_DIR, overwrite_file=True, verify_file=False)
            sdk_ports = self.execute_cmd(f'sudo python {Cl_Consts.CL_HOME_DIR}/{Cl_Consts.CL_LOG_PORT_FILE} --ports {ports_string}  | egrep \"^[0-9]\"')
            sdk_ports = sdk_ports.split()
            sdk_ports = [hex(int(port)) for port in sdk_ports]
            for port, sdk_port in zip(ports_list, sdk_ports):
                self.ports_mapping[port] = sdk_port
                self.sdk_ports_mapping[sdk_port] = port
        return sdk_ports

    def get_hex_int_sdk_ports(self, ports_list: list):
        list_of_sdk_ports = []
        if not self.ports_mapping:
            self.get_os_ports_name_mapping()
        if all(port in self.ports_mapping.keys() for port in ports_list):
            for port in ports_list:
                list_of_sdk_ports.append((int(self.ports_mapping[port], PerfConsts.HEX_BASE)))
        else:
            sdk_ports = self.get_sdk_ports(ports_list)
            for sdk_port in sdk_ports:
                list_of_sdk_ports.append((int(sdk_port, PerfConsts.HEX_BASE)))
        return list_of_sdk_ports

    def get_sdk_port(self, port: str):
        try:
            return self.ports_mapping[port]
        except KeyError:
            self.engine.copy_file(source_file=f'{Cl_Consts.CL_LOG_PORT_FILE_PATH}/{Cl_Consts.CL_LOG_PORT_FILE}',
                                  dest_file=f'{Cl_Consts.CL_LOG_PORT_FILE}',
                                  file_system=Cl_Consts.CL_HOME_DIR, overwrite_file=True, verify_file=False)
            sdk_port = self.execute_cmd(f'sudo python {Cl_Consts.CL_HOME_DIR}/{Cl_Consts.CL_LOG_PORT_FILE} --port {port}  | egrep \"^[0-9]\"')
            self.ports_mapping[port] = hex(int(sdk_port))
            self.sdk_ports_mapping[hex(int(sdk_port))] = port
            return hex(int(sdk_port))

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
        return build_controllers_info_dicts_list(sensors_output)

    def _gather_right_left_ports_dict(self, bring_up_ports=False):
        """Return the raw (unfiltered) DUT left/right ports from LLDP plus the TG-peer map.

        Returns:
            tuple(dict, dict): ``({"left_ports": [...], "right_ports": [...]},
            {dut_port: tg_peer_name})`` before any port-selection filtering.
        """
        right_left_port_dict = {
            "right_ports": [],
            "left_ports": []
        }
        if bring_up_ports:
            for dut in PerfConsts.PERF_SETUP_PLAYERS_ALIASES:
                self.topology_obj[0][dut]['cli'].interface.initialize_physical_ports()
            logging.info("Waiting 10 seconds for LLDP neighbor to get populated.")
            sleep(10)
        lldp_json = self.cli_obj.interface.get_lldp_neighbors(output_type="json")
        dut_port_to_tg_peer = {}
        for port, properties in lldp_json.items():
            neighbor = [*properties['lldp']['neighbor'].keys()][0]
            if neighbor == 'right-tg':
                right_left_port_dict["right_ports"].append(port)
            if neighbor == 'left-tg':
                right_left_port_dict["left_ports"].append(port)
            if neighbor in ('right-tg', 'left-tg'):
                dut_port_to_tg_peer[port] = self._extract_lldp_neighbor_port(neighbor, properties)
        return right_left_port_dict, dut_port_to_tg_peer

    def get_right_left_ports_dict(self, bring_up_ports=False):
        """
        Returns:
        A dict of ports on the DUT facing each TG. Names follow LLDP (parent ``swpN``
        on classic SKUs, or pre-split ``swpNsM`` on platforms such as SPC6 default 2x).
        With port selection active, the cascade-excluded ports are removed from both sides.
        """
        right_left_port_dict, dut_port_to_tg_peer = self._gather_right_left_ports_dict(bring_up_ports)
        return self._apply_port_selection_to_right_left(right_left_port_dict, dut_port_to_tg_peer)

    def get_full_right_left_ports_dict(self, bring_up_ports=False):
        """Return the DUT left/right ports **without** port-selection filtering.

        Used by the DUT config template only when port selection is active, so per-port IP
        addressing can be indexed by each port's position in the *full* topology. This keeps
        the DUT's subnets aligned with the (unfiltered) TG side: excluding a port leaves a gap
        in the address space instead of compacting and shifting every downstream port into the
        wrong subnet (which would break ARP/ND on all ports after the excluded one).
        """
        right_left_port_dict, _ = self._gather_right_left_ports_dict(bring_up_ports)
        return right_left_port_dict

    @staticmethod
    def _extract_lldp_neighbor_port(neighbor, properties):
        """Return the neighbor's cabled port name from an LLDP entry, or '?' if unavailable."""
        try:
            return properties['lldp']['neighbor'][neighbor]['port']['name']
        except (KeyError, TypeError):
            return "?"

    def _apply_port_selection_to_right_left(self, right_left_port_dict, dut_port_to_tg_peer):
        """Apply the port-selection symmetric cascade to a DUT left/right ports dict.

        No-op (returns the dict unchanged) when the selection is inactive, preserving the
        original behavior for runs without ``--perf-exclude-ports`` / ``--perf-include-ports``.
        Otherwise removes the cascade-resolved ports from both sides so the DUT stays
        left/right balanced, logging each excluded DUT port with its cabled TG peer.
        """
        if not self.port_selection.is_active():
            return right_left_port_dict
        left_ports = right_left_port_dict["left_ports"]
        right_ports = right_left_port_dict["right_ports"]
        excluded_left, excluded_right = resolve_symmetric_cascade(left_ports, right_ports,
                                                                  self.port_selection)
        # Cache the excluded DUT names (left + right union). Publish them to the module-level
        # store (DUT only) so TGs can read them reliably by name to skip the oper-down
        # DUT-facing ports opposite excluded DUT ports in their readiness waits.
        self.excluded_port_names = set(excluded_left) | set(excluded_right)
        # Publish the excluded DUT port names so every TG can, granularity-correctly, drop the
        # mloops paired (by bridge VLAN) with its own DUT-facing ports cabled to these DUT ports.
        # Name correlation handles breakout (a parent excludes all its children) and is robust
        # whether the excluded DUT port is link-up-but-not-routed or fully down at traffic time.
        if self.dut_alias == PerfConsts.DUT_ALIAS and self.excluded_port_names:
            sorted_excluded = sort_swp_split_port_names(self.excluded_port_names)
            set_resolved_excluded_dut_ports(self.excluded_port_names)
            record_port_selection_debug("dut_cascade", {
                "dut_alias": self.dut_alias,
                "excluded_dut_ports": sorted_excluded,
                "cabled_tg_peers": {p: dut_port_to_tg_peer.get(p, "?") for p in sorted_excluded},
            })
        for side, excluded in (("left_ports", excluded_left), ("right_ports", excluded_right)):
            for dut_port in sort_swp_split_port_names(excluded):
                logging.info(f"Port selection: excluding DUT {side[:-6]} port {dut_port} "
                             f"(cabled TG peer: {dut_port_to_tg_peer.get(dut_port, '?')})")
        right_left_port_dict["left_ports"] = [p for p in left_ports if p not in excluded_left]
        right_left_port_dict["right_ports"] = [p for p in right_ports if p not in excluded_right]
        return right_left_port_dict

    def get_upstream_downstream_ports_dict(self, upstream_ports_num, downstream_ports_num, sequential=False):
        ports = self.get_right_left_ports_dict()
        left_ports = copy.deepcopy(ports["left_ports"])
        right_ports = copy.deepcopy(ports["right_ports"])
        upstream_start_index = random.randint(0, len(left_ports) - upstream_ports_num)
        upstream_end_index = upstream_start_index + upstream_ports_num
        downstream_start_index = random.randint(0, len(right_ports) - downstream_ports_num)
        downstream_end_index = downstream_start_index + downstream_ports_num
        upstream = left_ports[upstream_start_index:upstream_end_index]
        downstream = right_ports[downstream_start_index:downstream_end_index]
        return upstream, downstream

    def configure_dummy_acls(self, template_path, dut_ports, num_acls):
        """Configure dummy ACLs on Cumulus using 'nv set acl' + 'nv config apply'."""
        for acl_idx in range(num_acls):
            acl_name = f"DUMMY_ACL_{acl_idx}"
            self.engine.run_cmd(f"nv set acl {acl_name} type ipv6")
            src_ipv6 = f"fd00:ffff:ffff:ffff::{acl_idx + 1}/128"
            self.engine.run_cmd(f"nv set acl {acl_name} rule 1 match ip source-ip {src_ipv6}")
            self.engine.run_cmd(f"nv set acl {acl_name} rule 1 action deny")
            for port in dut_ports:
                self.engine.run_cmd(f"nv set interface {port} acl {acl_name} inbound")
        self.engine.run_cmd("nv config apply -y")

    def remove_dummy_acls(self):
        """Remove all ACLs on Cumulus using 'nv unset acl' + 'nv config apply'."""
        self.engine.run_cmd("nv unset acl")
        self.engine.run_cmd("nv config apply -y")

    def _get_data_physical_ports_count(self, asic_model=None):
        """Return usable front-panel physical ports without SPC6 service children."""
        asic_model = asic_model or self.cli_obj.general.get_asic_model(self.engine)
        physical_ports = self.cli_obj.interface.get_physical_ports()
        return get_nvue_data_physical_ports_count(
            asic_model, physical_ports, len(Cl_Consts.BONUS_PORTS[asic_model]))

    def get_configuration_file(self, scenario, conf_args, template_suite=PerfConsts.DEFAULT_PERF_TEMPLATES_DIR):
        self.wait_for_lldp_neighbors()
        func_dict = {"get_right_left_ports_dict": self.get_right_left_ports_dict,
                     "generate_ip_address_list": generate_ip_address_list,
                     "filter_ports": self.cli_obj.interface.filter_lldp_neighbors,
                     "down_ports": self.cli_obj.interface.get_down_ports,
                     "address_calculator": address_calculator,
                     "cumulus_ports_already_logical_split": cumulus_ports_already_logical_split,
                     "cumulus_ports_match_requested_split": cumulus_ports_match_requested_split,
                     "sort_swp_split_port_names": sort_swp_split_port_names,
                     "get_swp_parent_port_names": get_swp_parent_port_names,
                     "get_swp_ports_for_split": get_swp_ports_for_split,
                     "validate_no_unsupported_service_port_split": validate_no_unsupported_service_port_split,
                     "validate_no_overlapping_swp_parent_ports": validate_no_overlapping_swp_parent_ports,
                     # Port-selection-only helpers (used by dut.yaml.jinja to keep per-port IP
                     # subnets aligned with the unfiltered TG when ports are excluded).
                     "port_selection_active": self.port_selection.is_active,
                     "get_full_right_left_ports_dict": self.get_full_right_left_ports_dict,
                     "list_index": (lambda seq, item: list(seq).index(item)),
                     }
        asic = self.cli_obj.general.get_asic_model(self.engine)
        total_dut_ports = self._get_data_physical_ports_count(asic)
        chip_type = conf_args.get("chip_type") or asic.replace("Spectrum-", "SPC")
        templates_path = os.path.join(BugHandlerConst.NGTS_PATH, "performance_tests",
                                      template_suite, scenario, "cumulus_jinja")
        templateLoader = FileSystemLoader(searchpath=templates_path)
        templateEnv = Environment(loader=templateLoader)
        TEMPLATE_FILE = "{}.yaml.jinja".format(self.dut_alias)
        jinja_template = templateEnv.get_template(TEMPLATE_FILE)
        jinja_template.globals.update(func_dict)
        parameter_dict = {
            "split_left": conf_args['split_left'],
            "split_right": conf_args['split_right'],
            "total_ports": total_dut_ports,
            "speed": conf_args.get('speed', "400000000"),
            "two_sided_ar": conf_args.get('two_sided_ar', False),
            "link_auto_negotiate": conf_args.get('link_auto_negotiate', False),
            "link_phy_autoneg": conf_args.get("link_phy_autoneg"),
            "link_phy_speed": conf_args.get("link_phy_speed"),
            "dut_left_ports_num": conf_args.get("dut_left_ports_num"),
            "chip_type": chip_type,
        }
        outputText = jinja_template.render(parameter_dict=parameter_dict)
        try:
            configuration = yaml.safe_load(outputText)
        except yaml.YAMLError as yex:
            logging.error(yex)
            logging.error(f"{self.dut_alias}'s Jinja file has resulted in incorrect YAML configuration :- \r\n{pprint.pformat(outputText, depth=12, width=128)}\r\n")
            raise
        null_set_values = find_null_nvue_set_values(configuration)
        if null_set_values:
            raise TestIssue(
                f"{self.dut_alias}'s rendered NVUE configuration contains null values under set operations: "
                f"{null_set_values}")
        fd, path = tempfile.mkstemp()
        with os.fdopen(fd, 'w') as f:
            f.write(outputText)
        return path

    def get_device_configuration(self, conf_args, template_suite=PerfConsts.DEFAULT_PERF_TEMPLATES_DIR):
        """
        Returns:
        A dict of the device configuration for the given scenario
        """
        self.engine.copy_file(source_file=f"{Cl_Consts.CL_LOG_PORT_FILE_PATH}/{Cl_Consts.CL_LOG_PORT_FILE}",
                              dest_file=f"{Cl_Consts.CL_LOG_PORT_FILE}",
                              file_system=Cl_Consts.CL_HOME_DIR, overwrite_file=True, verify_file=False)
        right_left_port_dict = self.get_right_left_ports_dict()
        ports_string = " ".join(right_left_port_dict["right_ports"])
        right_side_ports_to_ip_dict = self.get_ports_to_ip_dict(ports_string, conf_args["is_ipv6"], Cl_Consts.COMMON_IP_PREFIX_RIGHT)
        ports_string = " ".join(right_left_port_dict["left_ports"])
        left_side_ports_to_ip_dict = self.get_ports_to_ip_dict(ports_string, conf_args["is_ipv6"], Cl_Consts.COMMON_IP_PREFIX_LEFT)
        return {"right_side_ports_to_ip_dict": right_side_ports_to_ip_dict, "left_side_ports_to_ip_dict": left_side_ports_to_ip_dict}

    def get_ports_to_ip_dict(self, ports_string, is_ipv6, ip_prefix):
        output = self.execute_cmd(f"sudo python {Cl_Consts.CL_HOME_DIR}/{Cl_Consts.CL_LOG_PORT_FILE} --ports {ports_string}  | egrep \"^[0-9]\"")
        port_list = output.split()
        ports_to_ip_dict = {}
        for index, port in enumerate(port_list):
            if is_ipv6:
                ports_to_ip_dict[port] = f"{ip_prefix}::{index + 1}"
            else:
                ports_to_ip_dict[port] = f"{ip_prefix}.{ip_prefix}.{ip_prefix}.{index + 1}"
        return ports_to_ip_dict

    def get_dut_system_information(self, session_id, setup_name):
        """
        Args:
            session_id: Mars session id, i.e, 9443960
            setup_name: i.e, nv_performance_mtvr-moose-17

        Returns: a dictionary with the full dut system information, .i.e,
         "dutSystemInformation": {
                "marsSessionId": "9438676",
                "setupName": "nv_performance_mtvr-moose-17",
                "osType": "NVUE",
                "chip": "SPECTRUM4",
                "board": "sn5600",
                "sdkVersion": "4.7.3094-003",
                "hwChassisRev": "AJ",
                "modelNumber": "MSN-9N402-00RI-7N0_Ax",
                "hostDetails": "mtvr-moose-17, IP N/A",
                "serialNumber": "MT2443J011Q7",
                "onieVersion": "2023.11-5.3.0012-115200",
                "psid": "MT_0000000955",
                "osVersion": "Cumulus Linux 5.12.0"
            }
        """
        dut_system_information = {"marsSessionId": session_id,
                                  "setupName": setup_name,
                                  "osType": "NVUE"}

        cmd = f"sudo {Cl_Consts.CL_PYTHON_PATH} {PerfConsts.DVS_RUN_TEST_PATH} -si"
        output = self.execute_cmd(cmd)

        regex_dict = {
            "chip": r"ASIC:\s*(SPECTRUM\d+)",
            "board": r"Platform:\s*([a-zA-Z0-9]+)",
            "sdkVersion": r"SDK Version:\s*([\d|.|-]*)",
            "hwChassisRev": r"HW Revision:\s*([A-Z]+)",
            "modelNumber": r"Model:\s*(.*)",
            "serialNumber": r"Serial Number:\s*([A-Za-z0-9]+)",
            "onieVersion": r"ONIE Version:\s*(.*)",
            "psid": r"PSID:\s*([A-Za-z0-9_]+)",
        }

        for key, regex in regex_dict.items():
            match = re.search(regex, output)
            if match:
                dut_system_information[key] = match.group(1)

        os_regex = r"IMAGE_DESCRIPTION=\"(Cumulus Linux [\d|.]+)\""
        os_output = self.execute_cmd("cat /etc/image-release")
        match = re.search(os_regex, os_output)
        if match:
            dut_system_information["osVersion"] = match.group(1)

        self.modify_board_host_internal_name(output, regex_dict, dut_system_information)
        return dut_system_information

    @staticmethod
    def modify_board_host_internal_name(output, regex_dict, dut_system_information):
        match = re.search(regex_dict["board"], output)
        if match:
            dut_system_information["board"] = ResultUploaderConst.HOST_INTERNAL_NAMES_MAP[match.group(1).lower()]

    def wait_for_nexthop_resolution(self, conf_args, number_of_nexthops=None, timeout=120):
        """
        Wait for the number of nexthops to be resolved on the dut
        Implemented for Cumulus only
        """
        asic_model = self.cli_obj.general.get_asic_model(self.engine)
        if number_of_nexthops is None:
            total_dut_ports = self._get_data_physical_ports_count(asic_model)
            # Subtract selected-out DUT parent ports so we do not wait on next-hops that
            # were intentionally removed from the config.
            if self.port_selection.is_active():
                if not self.excluded_port_names:
                    self.get_right_left_ports_dict()
                excluded_parents = get_swp_parent_port_names(self.excluded_port_names)
                if excluded_parents:
                    total_dut_ports -= len(excluded_parents)
                    logging.info(f"Port selection: nexthop calc reduced by {len(excluded_parents)} "
                                 f"excluded parent port(s): {excluded_parents}")
            number_of_nexthops = get_nvue_expected_nexthops(
                total_dut_ports, conf_args["split_left"], conf_args["split_right"])
            logging.info(f"Number of nexthops to resolve: {number_of_nexthops}")
        nexthop_number = 0
        start_time = timeout
        while nexthop_number < number_of_nexthops:
            nexthop_number = int(self.execute_cmd("ip neighbor show | grep swp | wc -l"))
            logging.info("Number of nexthops resolved on the dut at time {} is {}".format(start_time - timeout, nexthop_number))
            sleep(10)
            timeout -= 10
            if timeout < 0 and nexthop_number < number_of_nexthops:
                raise RealIssue("After {} seconds, the number of nexthops resolved on the dut is {}".format(start_time, nexthop_number))
        return True

    def retrieve_default_route(self):
        """
        Retrieve the default route on the the setup
        """
        retrieve_default_route_cmd = "nv sh vrf mgmt router rib ipv4 | grep connected | awk '{print $1}'"
        try:
            output = self.execute_cmd(retrieve_default_route_cmd)
            return output
        except Exception as e:
            logging.warning(f"Error retrieving default route: {e}")
            return "No route found"

    def restart_daemon(self, daemon):
        self.execute_cmd(f"sudo systemctl restart {daemon}")

    def get_dut_interfaces_ipv6_configuration(self):
        output = self.execute_cmd("nv sh interface -o json")
        interface_output = json.loads(output)
        dut_interfaces_ipv6_configuration_dict = {}
        for interface in interface_output:
            if "swp" not in interface:  # skip non-switch ports
                continue
            else:
                ip_addresses = interface_output[interface]["ipv6"]['address'].keys()
                for ip in ip_addresses:
                    if is_ipv6(ip) and ("fe80" not in ip):
                        ipv6_address = ip.split("/")[0]
                        dut_interfaces_ipv6_configuration_dict[interface] = ipv6_address
        return dut_interfaces_ipv6_configuration_dict

    def get_tg_interfaces_vlan_configuration(self):
        output = self.execute_cmd("nv sh bridge domain br_default port vlan -o json")
        port_vlan_info = json.loads(output)
        vlan_interface_configuration_dict = {}
        for port, vlan_info_dict in port_vlan_info.items():
            vlan_interface_configuration_dict[port] = [* vlan_info_dict["vlan"].keys()][0]
        return vlan_interface_configuration_dict

    def configure_mac_neighbor(self, port, port_ipv6_address, port_neighbor_mac, vlan):
        """
        Configure the mac neighbor on the dut

        cmd_list = []
        fdb_discard_conf = []
        cmd_list.append(f"nv set vrf default router static {port_ipv6_address}/120 via {port}")
        cmd_list.append(f"nv set interface {port} neighbor ipv6 {port_ipv6_address} lladdr {port_neighbor_mac}")
        self.engine.run_cmd_set(cmd_list)
        """
        pass

    def add_ports_connectivity_to_dut(self, conf_args, selected_connected_ports=None):
        ports_file = "ports.json"
        full_path = os.path.join(PerfConsts.CONFIG_FILES_DIR, ports_file)
        connected_ports = selected_connected_ports if selected_connected_ports else self.connected_ports
        ports_connectivity_dict = {
            "unconnected_ports": self.get_hex_int_sdk_ports(self.unconnected_ports),
            "connected_ports": self.get_hex_int_sdk_ports(connected_ports),
            "speed": conf_args["speed"]}
        with open(full_path, 'w') as f:
            json.dump(ports_connectivity_dict, f)
        self.engine.copy_file(source_file=full_path, file_system="/tmp",
                              dest_file=ports_file, overwrite_file=True, verify_file=False)

    @retry(exceptions=TestIssue, tries=6, delay=15)
    def check_mloops_up(self):
        """
        This method is used to check if the mloops are up on the traffic generator
        and if not, it will wait for them to be up.
        """
        if not self.dut_neighbor_dict:
            self.dut_neighbor_dict = self.cli_obj.interface.filter_lldp_neighbors(neighbor_list=[PerfConsts.DUT_ALIAS],
                                                                                  include_neighbor_ports=True)[PerfConsts.DUT_ALIAS]
        mloops_tuples_list = []
        down_ports_list = []
        up_ports_list = []
        if len(self.connected_ports) == len(self.unconnected_ports):
            dut_lldp_name = self.dut_alias.replace("_", "-")
            ports_dict = self.cli_obj.interface.filter_lldp_neighbors(neighbor_list=[dut_lldp_name, PerfConsts.DUT_ALIAS])
            down_ports_list = ports_dict[dut_lldp_name]
            up_ports_list = ports_dict[PerfConsts.DUT_ALIAS]
        if len(down_ports_list) != len(self.unconnected_ports):
            raise TestIssue(f"Not all Mloops are up yet on {self.dut_alias}")
        for up_port, down_port in zip(up_ports_list, down_ports_list):
            mloops_tuples_list.append((up_port, down_port))
        self.mloops = mloops_tuples_list
        logging.info(f"Mloops for {self.dut_alias} are up")

    def update_dst_mac_address(self, src_port, dut_mac_addresses, traffic_parameters):
        dut_port = self.dut_neighbor_dict[src_port]
        traffic_parameters["MAC"]["dst"] = dut_mac_addresses[dut_port]

    def get_leaf_many_to_few_port_group_df(self, M, num_of_ingress_ports):
        port_group_df = []
        ports = self.cli_obj.performance.get_right_left_ports_dict()
        left_ports = copy.deepcopy(ports["left_ports"])
        right_ports = copy.deepcopy(ports["right_ports"])
        egress_ports_num = num_of_ingress_ports // M
        egress_ports = right_ports[:egress_ports_num]
        ingress_ports = left_ports[:num_of_ingress_ports]
        sdk_port_list_egress = self.cli_obj.performance.get_sdk_ports(egress_ports)
        sdk_port_list_ingress = self.cli_obj.performance.get_sdk_ports(ingress_ports)
        for port in sdk_port_list_egress:
            port_group_df.append({"port": port, MongoDbConsts.PORT_GROUP_NAME: "egress_ports"})
        for port in sdk_port_list_ingress:
            port_group_df.append({"port": port, MongoDbConsts.PORT_GROUP_NAME: "ingress_ports"})
        return egress_ports, ingress_ports, port_group_df

    def set_shaper(self, speed, shaper_value, shaper_profile="default-global"):
        """
        This method is used to set the shaper on the traffic gen
        """
        # Convert speed to float if it's a string
        if isinstance(speed, str):
            speed = float(speed)
        shaper_value_kbps = int(speed * shaper_value)
        if shaper_value == 1.0:
            self.engine.run_cmd(f"nv unset qos egress-shaper {shaper_profile}")
        else:
            self.engine.run_cmd(f"nv set qos egress-shaper {shaper_profile} port-max-rate {shaper_value_kbps}")
        self.cli_obj.general.apply_config(self.engine, option="-y", verify_execution=True)
