import logging
import logging.config
import time
import allure
import os
import json
import time
import pytest
import shutil
from datetime import datetime
from ngts.helpers.general_helper import get_pytest_test_name
from ngts.constants.constants import BugHandlerConst, CliType
from ngts.constants.performance_constants import PerfConsts, MongoDbConsts, ValidationConsts, Cl_Consts, BwFairnessThreshold, OccupancyVarianceConfig
from ngts.helpers.thread_log_filter import redirect_thread_stdout
from ngts.helpers.custom_catch_exception_thread import CatchExceptionThread, parse_threads_exceptions_at_join
from devts.infra.tools.exceptions.test_issue import TestIssue
from ngts.helpers.performance.performance_db_helpers import add_test_mongo_metadata, get_perf_test_name
from ngts.helpers.performance.traffic_helpers import (validate_bw, validate_bw_utilization_fairness, validate_tc,
                                                      validate_counters, validate_no_drops_on_tg_ports,
                                                      validate_required_counters, validate_tc_occupancy_fairness)
from ngts.helpers.performance.performance_counter_helpers import validate_performance_counters
from ngts.helpers.performance.topology_helpers import get_dvs_topology_obj, get_nvue_sonic_topology_obj
from ngts.helpers.performance.power_temp_helpers import validate_temperature, validate_power
from ngts.helpers.performance.port_selection import set_resolved_excluded_dut_ports
from ngts.cli_wrappers.dvs.dvs_cli import DvsCli
from ngts.cli_wrappers.nvue.nvue_cli import NvueCli
from ngts.cli_wrappers.sonic.sonic_cli import SonicCli
from ngts.tools.infra import get_chip_type

from dataclasses import dataclass, field, fields
from typing import Optional, List, Dict, Any, Callable

logger = logging.getLogger()


def get_is_simx(players):
    """True when DUT is SimX. Uses lspci on DUT (OS-agnostic)."""
    try:
        engine = players[PerfConsts.DUT_ALIAS]['engine']
        return 'simx' in engine.run_cmd("lspci -vv").lower()
    except Exception as e:
        logger.exception(f"get_is_simx failed: {e}")
        raise


# Type alias for validation functions: (traffic_json, *, violations_list, **kwargs) -> None
ValidationFunc = Callable[..., None]


@dataclass
class Validation:
    """
    Represents a validation operation with its function and additional arguments.
    """
    func: ValidationFunc
    extra_args: Dict[str, Any] = field(default_factory=dict)


def _validation_spec(name, func, get_extra_args, enabled_if):
    """Builds a validation spec tuple for data-driven get_validations."""
    return (name, func, get_extra_args, enabled_if)


@dataclass
class ValidationConfig:
    """
    Configuration class for managing various validation settings and thresholds.

    Attributes:
        players: Test players configuration
        test_name (str): Name of the test being run
        scenario (str): Test scenario identifier
        chip_type (str): Type of chip being tested
        run_validate_counters (bool): Whether to run counter validations
        samples_params_dict (Dict): Parameters for sampling configuration
        tc_occ_threshold (Dict): Traffic class occupancy threshold
        temperature_threshold (float): Maximum allowed temperature
        bw_threshold (float, optional): Bandwidth threshold
        power_threshold (float, optional): Power consumption threshold
        port_list (List[str], optional): List of ports to validate
        skip_first_counters_iteration (bool): Whether to skip first counter check
        additional_validations (List[Validation], optional): Additional validations to run from test
        players_to_be_validated (List[str], optional): List of player aliases to run validation on.
            Defaults to DUT only. Can be set to TG aliases for traffic generator validation.
        required_counters_port_group (str, optional): Port group to check for required counters.
            If None, all port groups are checked.
        max_tc_occ_variance_per_port_group (Dict[str, OccupancyVarianceConfig]): Derived automatically
            from tc_occ_threshold. None when tc_occ_threshold is None (disables this validation).
            Per-port-group dict when tc_occ_threshold is keyed by port group, otherwise
            {"default": OccupancyVarianceConfig()} applied to all port groups.
    """
    players: Any
    test_name: str
    scenario: str
    chip_type: str
    bw_fairness_threshold_per_port_group: Optional[Dict[str, BwFairnessThreshold]] = field(default_factory=dict)
    run_validate_counters: bool = True
    run_validate_no_drops_on_tg_ports: bool = True
    validate_bw_rx: bool = True
    samples_params_dict: Dict = field(default_factory=lambda: PerfConsts.SAMPLES_PARAMS)
    tc_occ_threshold: Dict = field(default_factory=lambda: PerfConsts.OCC_TH_DICT)
    run_validate_performance_counters: bool = False
    allowed_deviation: float = PerfConsts.PERF_COUNTERS_ALLOWED_DEVIATION
    packet_size: Optional[int] = None
    temperature_threshold: float = PerfConsts.TEMPERATURE_TH
    bw_threshold: Optional[float] = None
    power_threshold: Optional[float] = None
    port_list: Optional[List[str]] = None
    ignore_counter_list: List = field(default_factory=list)
    required_counter_list: List = field(default_factory=list)
    required_counters_port_group: Optional[str] = None
    skip_first_counters_iteration: Optional[bool] = False
    additional_validations: Optional[List[Validation]] = field(default_factory=dict)
    players_to_be_validated: List[str] = field(default_factory=lambda: PerfConsts.PERF_SETUP_DUT_ALIASES)
    max_tc_occ_variance_per_port_group: Optional[Dict[str, OccupancyVarianceConfig]] = field(init=False)

    def __post_init__(self):
        self.max_tc_occ_variance_per_port_group = OccupancyVarianceConfig.from_tc_occ_threshold(self.tc_occ_threshold)
        # Copy so per-test overrides (and auto-enable below) do not mutate PerfConsts.SAMPLES_PARAMS.
        self.samples_params_dict = dict(self.samples_params_dict)
        if self.run_validate_performance_counters:
            self.samples_params_dict[PerfConsts.COLLECT_SDK_DUMP_ENV_VAR] = "True"

    def get_validations(self) -> Dict[str, Validation]:
        """
        Returns a dictionary of validation configurations. Never empty.

        Each validation is included; disabled ones are None (skipped at run time).
        When running on Simx, normal validations are skipped (None) and only 'tx_rx_counters' runs.

        Returns:
            Dict[str, Validation]: Dictionary mapping validation names to their configurations (or None if skipped)
        """
        is_simx = get_is_simx(self.players)

        specs = [
            _validation_spec(
                'counters', validate_counters,
                lambda: {'skip_first_counters_iteration': self.skip_first_counters_iteration,
                         ValidationConsts.IGNORE_COUNTER_LIST: self.ignore_counter_list + self.required_counter_list},
                lambda: not is_simx and self.run_validate_counters,
            ),
            _validation_spec(
                'bandwidth', validate_bw,
                lambda: {'bw_threshold': self.bw_threshold, 'validate_bw_rx': self.validate_bw_rx},
                lambda: not is_simx and self.bw_threshold is not None,
            ),
            _validation_spec(
                'bw_fairness', validate_bw_utilization_fairness,
                lambda: {'bw_fairness_threshold_per_port_group': self.bw_fairness_threshold_per_port_group},
                lambda: not is_simx and bool(self.bw_fairness_threshold_per_port_group),
            ),
            _validation_spec(
                'tc', validate_tc,
                lambda: {'tc_occ_threshold': self.tc_occ_threshold},
                lambda: not is_simx and self.tc_occ_threshold is not None,
            ),
            _validation_spec(
                'tc_occ_fairness', validate_tc_occupancy_fairness,
                lambda: {'max_tc_occ_variance_per_port_group': self.max_tc_occ_variance_per_port_group},
                lambda: not is_simx and bool(self.max_tc_occ_variance_per_port_group),
            ),
            _validation_spec(
                'temperature', validate_temperature,
                lambda: {'temperature_threshold': self.temperature_threshold},
                lambda: not is_simx and self.temperature_threshold is not None,
            ),
            _validation_spec(
                'power', validate_power,
                lambda: {'players': self.players, 'test_name': self.test_name,
                         'chip_type': self.chip_type, 'power_threshold': self.power_threshold},
                lambda: not is_simx and self.power_threshold is not None,
            ),
            _validation_spec(
                'traffic_pattern', validate_no_drops_on_tg_ports,
                lambda: {'players': self.players},
                lambda: self.run_validate_no_drops_on_tg_ports,
            ),
            _validation_spec(
                'performance_counters', validate_performance_counters,
                lambda: {'cli_object': self.players['dut']['cli'],
                         'allowed_deviation': self.allowed_deviation,
                         'packet_size': self.packet_size, 'test_name': self.test_name},
                lambda: not is_simx and self.run_validate_performance_counters and bool(self.packet_size),
            ),
            _validation_spec(
                'tx_rx_counters', _validate_simx_tx_rx_validation,
                lambda: {'players': self.players},
                lambda: is_simx,
            ),
            _validation_spec(
                'required_counters', validate_required_counters,
                lambda: {'required_counter_list': self.required_counter_list,
                         'port_group_name': self.required_counters_port_group},
                lambda: not is_simx and bool(self.required_counter_list),
            ),
        ]
        validations = {
            name: Validation(func, get_extra_args()) if enabled() else None
            for name, func, get_extra_args, enabled in specs
        }
        validations.update(self.additional_validations)
        return validations


def unsplit_all_ports(players, players_aliases=PerfConsts.PERF_SETUP_PLAYERS_ALIASES,
                      step="basic_test_configuration - unsplit_all_ports", parallel_run=True):
    """Bring ports to a known baseline before performance templates render.

    - DVS: SPC5 only (SPC6 DVS keeps its default breakout).
    - NVUE/Cumulus: SPC4/SPC5 run ``initialize_physical_ports`` (breakout 1x);
      SPC6 keeps the platform default 2x and disables WJH.

    Args:
        players (dict): Dictionary containing player information and CLI interfaces
        players_aliases (list): List of player aliases to unsplit. Defaults to PerfConsts.PERF_SETUP_PLAYERS_ALIASES
        step (str): Description of the current setup step
        parallel_run (bool): If True, unsplits in parallel. If False, unsplits sequentially. Defaults to True
    """
    aliases_to_unsplit = []
    try:
        for player_alias in players_aliases:
            switch_attributes = players[player_alias]['attributes'].noga_query_data['attributes']
            chip_type = get_chip_type(switch_attributes)
            cli_obj = players[player_alias]['cli']
            if isinstance(cli_obj, (SonicCli, NvueCli)) and chip_type in ("SPC5", "SPC6"):
                aliases_to_unsplit.append(player_alias)
            elif chip_type == "SPC5":
                aliases_to_unsplit.append(player_alias)
    except (KeyError, AttributeError) as e:
        raise TestIssue(f"Could not determine chip_type from topology: {e}")

    if not aliases_to_unsplit:
        return

    logger.info(f"Unsplitting / initializing ports on players: {aliases_to_unsplit}")

    if parallel_run:
        call_performance_function_with_threads(players, players_aliases=aliases_to_unsplit,
                                               action="unsplit all ports",
                                               performance_clis_function_name="unsplit_all_ports",
                                               performance_clis_function_args=(), step=step)
    else:
        for player_alias in aliases_to_unsplit:
            players[player_alias]['cli'].performance.unsplit_all_ports()


def _prime_dut_port_selection_cascade(dut_performance, tries=12, interval=10):
    """Compute the DUT port-selection cascade on the main thread before the parallel apply.

    Retries so transient empty LLDP (ports still converging) does not leave the cascade
    empty. TG threads only read the resulting cached ``excluded_port_names`` (read-only,
    in-process) to skip oper-down ports in their readiness waits; they must not call the DUT
    engine themselves. Fails open with a warning.

    Args:
        dut_performance: The DUT performance CLI object.
        tries: Max attempts.
        interval: Seconds between attempts.
    """
    for attempt in range(1, tries + 1):
        try:
            ports = dut_performance.get_right_left_ports_dict()
        except Exception as e:
            logging.warning(f"Priming DUT port-selection cascade attempt {attempt}/{tries} failed: {e}")
            ports = None
        if dut_performance.excluded_port_names:
            logging.info(f"DUT port-selection cascade primed: excluded "
                         f"{sorted(dut_performance.excluded_port_names)}")
            return
        if ports is not None and not ports.get("left_ports") and not ports.get("right_ports"):
            logging.info(f"Priming DUT cascade: LLDP left/right still empty "
                         f"(attempt {attempt}/{tries}), retrying in {interval}s")
        time.sleep(interval)
    logging.warning("DUT port-selection cascade is empty after priming; exclusion may not "
                    "apply. Check DUT LLDP and the config-file entry for this setup/scenario.")


def apply_test_configuration(players, scenario, conf_args,
                             players_aliases=PerfConsts.PERF_SETUP_PLAYERS_ALIASES,
                             step="basic_test_configuration - apply_test_configuration", parallel_run=True):
    """
    Applies test configuration to multiple players either sequentially (debug mode) or in parallel.

    Args:
        players (dict): Dictionary containing player information and CLI interfaces
        scenario (str): The test scenario to be configured
        conf_args (dict): Configuration arguments to be applied
        players_aliases (list): List of player aliases to configure. Defaults to PerfConsts.PERF_SETUP_PLAYERS_ALIASES
        step (str): Description of the current setup step. Defaults to "basic_test_configuration - set-up"
        parallel_run (bool): If True, applies configuration parallel using threads. If False, applies in sequentially. Defaults to True

    The function either:
    - In debug mode (parallel_run=False): Sequentially applies configuration to each player
    - In normal mode (parallel_run=True): Uses threading to apply configuration to all players in parallel
    """
    # Resolve port selection (exclude/include) for the running scenario before rendering the
    # config templates, so get_right_left_ports_dict()/get_player_ports() see the selection.
    # No-op unless --perf-exclude-ports / --perf-include-ports was supplied.
    for player_alias in players_aliases:
        players[player_alias]['cli'].performance.resolve_port_selection(scenario)
    # Reset any previously-published exclusion (stale from an earlier test in this session)
    # before recomputing for this apply, then pre-compute the DUT cascade on the MAIN thread
    # (with retry for LLDP readiness). TGs read the published names in-process; they must never
    # call the DUT engine themselves (that corrupts the DUT's shared SSH session). Fail open.
    set_resolved_excluded_dut_ports(set())
    dut_player = players.get(PerfConsts.DUT_ALIAS)
    if dut_player and dut_player['cli'].performance.port_selection.is_active():
        _prime_dut_port_selection_cascade(dut_player['cli'].performance)

    if parallel_run:
        call_performance_function_with_threads(players, players_aliases=players_aliases,
                                               action="apply test configuration",
                                               performance_clis_function_name="apply_configuration_file",
                                               performance_clis_function_args=(scenario, conf_args), step=step)
    else:
        for player_alias in players_aliases:
            players[player_alias]['cli'].performance.apply_configuration_file(scenario, conf_args)


def validate_perf_dut_ingress_buffer_mode(players):
    """Cumulus (NVUE) DUT only: assert IBM (ingress buffer) AR profile is active."""
    cli_obj = players[PerfConsts.DUT_ALIAS]["cli"]
    if not isinstance(cli_obj, NvueCli):
        return
    cli_obj.performance.validate_ingress_buffer_mode_active()


def validate_perf_dut_rebalancer_buffer_mode(players):
    """Cumulus (NVUE) DUT only: assert automatic/rebalancer buffer mode is active."""
    cli_obj = players[PerfConsts.DUT_ALIAS]["cli"]
    if not isinstance(cli_obj, NvueCli):
        return
    cli_obj.performance.validate_rebalancer_buffer_mode_active()


def allure_attach_performance_conf_context(players, conf_args, attach_dut_applied_yaml=True):
    """Attach ``conf_args`` as JSON and, when possible, the DUT-applied NVUE file to Allure.

    The applied YAML is read from ``/home/cumulus/tmp.yaml`` on the DUT after ``nv config replace``
    (same path used by ``NvuePerformanceCli.apply_configuration_file``).

    Args:
        players: Pytest players dict.
        conf_args: Scenario / Jinja parameter dict passed to performance templates.
        attach_dut_applied_yaml: When True, try to attach DUT ``tmp.yaml`` contents.

    Returns:
        None
    """
    try:
        payload = json.dumps(conf_args, indent=2, sort_keys=True, default=str)
    except TypeError:
        payload = str(conf_args)
    allure.attach(payload, name="performance_conf_args.json", attachment_type=allure.attachment_type.JSON)
    if not attach_dut_applied_yaml:
        return
    dut = players.get(PerfConsts.DUT_ALIAS)
    if not dut or "engine" not in dut:
        return
    try:
        yaml_path = f"{Cl_Consts.CL_HOME_DIR}/tmp.yaml"
        out = dut["engine"].run_cmd(f"sudo cat {yaml_path} 2>/dev/null || true")
        if out and str(out).strip():
            allure.attach(str(out), name="dut_applied_tmp.yaml", attachment_type=allure.attachment_type.YAML)
    except Exception as exc:
        logger.info("allure_attach_performance_conf_context: skip DUT tmp.yaml (%s)", exc)


def configure_mloops(players, validate_mloops=True, is_simx=False,
                     step="basic_test_configuration - configure_mloops", parallel_run=True):
    if parallel_run:
        call_performance_function_with_threads(players, players_aliases=PerfConsts.PERF_SETUP_TG_ALIASES,
                                               action="configure mloops",
                                               performance_clis_function_name="configure_mloops",
                                               performance_clis_function_args=(validate_mloops, is_simx), step=step)
    else:
        for player_alias in PerfConsts.PERF_SETUP_TG_ALIASES:
            players[player_alias]['cli'].performance.configure_mloops(validate_mloops, is_simx)


def save_base_configuration(players, step="basic_test_configuration - save_base_configuration"):
    call_performance_function_with_threads(players, players_aliases=PerfConsts.PERF_SETUP_PLAYERS_ALIASES,
                                           action="save base configuration",
                                           performance_clis_function_name="save_basic_configuration",
                                           performance_clis_function_args=(players,), step=step)


def restore_basic_configuration(players, players_aliases=PerfConsts.PERF_SETUP_PLAYERS_ALIASES,
                                step="Tear down - restore_base_configuration"):
    call_performance_function_with_threads(players, players_aliases=players_aliases,
                                           action="restore base configuration",
                                           performance_clis_function_name="restore_basic_configuration",
                                           performance_clis_function_args=(), step=step)


def dynamic_configuration_helper(players, scenario, performance_parameters, step="basic_test_configuration - dynamic_configuration_helper"):
    call_performance_function_with_threads(players, players_aliases=PerfConsts.PERF_SETUP_DUT_ALIASES,
                                           action="dynamic configuration",
                                           performance_clis_function_name="dynamic_configuration_helper",
                                           performance_clis_function_args=(scenario, performance_parameters), step=step)


def run_traffic(players, scenario, traffic_jsons, step="Running Traffic - Test body", attach_traffic_json=True, parallel_run=True):

    if parallel_run:
        call_performance_function_with_threads(players, players_aliases=PerfConsts.PERF_SETUP_TG_ALIASES,
                                               action="run traffic",
                                               performance_clis_function_name="run_traffic",
                                               performance_clis_function_args=(scenario, traffic_jsons),
                                               step=step)
    else:
        for player_alias in PerfConsts.PERF_SETUP_TG_ALIASES:
            players[player_alias]['cli'].performance.run_traffic(scenario, traffic_jsons)
    if attach_traffic_json:
        attach_json_traffic_to_allure(players, tg_players_aliases=PerfConsts.PERF_SETUP_TG_ALIASES,
                                      traffic_jsons=traffic_jsons)


def attach_json_traffic_to_allure(players, tg_players_aliases, traffic_jsons):
    for alias in tg_players_aliases:
        traffic_json_path = traffic_jsons[alias]
        cli_obj = players[alias]['cli']
        hostname = cli_obj.chassis.get_hostname()
        attach_json_to_allure(traffic_json_path, f'Traffic JSON configuration on {alias} - {hostname}')


def stop_traffic(players, step="Stopping Traffic - Tear down"):
    call_performance_function_with_threads(players, players_aliases=PerfConsts.PERF_SETUP_TG_ALIASES,
                                           action="stop traffic",
                                           performance_clis_function_name="stop_traffic",
                                           performance_clis_function_args=(),
                                           step=step)


def validate_traffic_results(players, test_name, scenario, samples_params_dict,
                             players_to_be_validated=PerfConsts.PERF_SETUP_DUT_ALIASES,
                             attach_to_allure=True,
                             add_validator_results_to_mongo_db=True):
    traffic_validation_results = []
    for player_alias in players_to_be_validated:
        cli_object = players[player_alias]['cli']
        hostname = cli_object.chassis.get_hostname()
        time_now = datetime.now()
        hour_str = time_now.strftime("%H:%M:%S")
        full_path = os.path.join(BugHandlerConst.NGTS_PATH, "performance_tests",
                                 "traffic_validation_json_files",
                                 scenario, f"{hour_str}_{player_alias}_{hostname}_{test_name}_TrafficValidator.json")
        os.environ[PerfConsts.TRAFFIC_VALIDATION_JSON_PATH] = full_path
        call_performance_function_with_threads(players, players_aliases=[player_alias],
                                               action="run traffic validator",
                                               performance_clis_function_name="validate_traffic",
                                               performance_clis_function_args=(full_path, samples_params_dict),
                                               step="Traffic Validation - Test Body")
        traffic_json = attach_json_to_allure(full_path,
                                             f'Traffic Validation JSON results on {player_alias} - {hostname}',
                                             attach_to_allure)
        traffic_validation_results.append({
            'player_alias': player_alias,
            'traffic_json': traffic_json
        })

        if add_validator_results_to_mongo_db:
            add_test_mongo_metadata(test_name, {MongoDbConsts.VALIDATOR_RESULTS: traffic_json})
    return traffic_validation_results


def attach_json_to_allure(json_path, attachment_name, attach_to_allure=True):
    if not os.path.exists(json_path):
        raise TestIssue(f"Traffic validation JSON file was not created at expected path: {json_path}. "
                        f"This may indicate that the traffic validator failed to run or the file copy from DUT failed. "
                        f"Check the DUT logs for more details.")
    with open(json_path) as f:
        json_str = f.read()
        if attach_to_allure:
            allure.attach(json_str, attachment_name, allure.attachment_type.JSON)
        json_obj = json.loads(json_str)
    return json_obj


def _validate_simx_tx_rx_validation(traffic_json, players, violations_list):
    """
    Validation entry point for Simx TX/RX cross-check. Uses validator output (tx_rx_per_port_group).
    """
    _validate_simx_tx_rx_cross_check(players, violations_list, traffic_json=traffic_json)


def _validate_simx_tx_rx_cross_check(players, violations_list, traffic_json=None):
    """
    Simx-only: for each port group, RX of that group must equal sum of TX of all other port groups.
    Fails if all port groups have RX of 0. Uses tx_rx_per_port_group from the validator JSON.
    """
    tx_rx_per_port_group = (traffic_json or {}).get(ValidationConsts.TX_RX_PER_PORT_GROUP)
    if not tx_rx_per_port_group:
        violations_list.append(
            "Simx TX/RX validation requires validator to provide tx_rx_per_port_group. "
            "Ensure multi_nos_validator is run and outputs TX/RX per port group.")
        return
    port_group_names = list(tx_rx_per_port_group.keys())
    total_tx = sum(tx_rx_per_port_group[name].get("tx", 0) for name in port_group_names)
    total_rx = sum(tx_rx_per_port_group[name].get("rx", 0) for name in port_group_names)
    per_group_lines = [f"  {name}: TX={tx_rx_per_port_group[name].get('tx', 0)}, RX={tx_rx_per_port_group[name].get('rx', 0)}"
                       for name in port_group_names]
    summary = f"Overall: TX={total_tx}, RX={total_rx}\nPer port group:\n" + "\n".join(per_group_lines)
    allure.attach(summary, "Simx TX/RX per port group", allure.attachment_type.TEXT)
    with allure.step(f"Simx TX/RX validation: overall TX={total_tx}, RX={total_rx}"):
        rx_counts = [tx_rx_per_port_group[name].get("rx", 0) for name in port_group_names]
        if all(count == 0 for count in rx_counts):
            violations_list.append("Simx TX/RX validation failed: all port groups have RX of 0.")
            return
        for port_group_name in port_group_names:
            rx_count_this_group = tx_rx_per_port_group[port_group_name].get("rx", 0)
            tx_count_from_other_groups = sum(
                tx_rx_per_port_group[other].get("tx", 0) for other in port_group_names if other != port_group_name)
            if rx_count_this_group != tx_count_from_other_groups:
                violations_list.append(
                    f"Simx TX/RX mismatch: RX({port_group_name})={rx_count_this_group} != "
                    f"sum(TX of other groups)={tx_count_from_other_groups}")


def get_expected_tx_packets_per_port_group(players, conf_args):
    """
    Returns expected number of packets to be sent per port group from traffic config (e.g. left_num_packets,
    right_num_packets). Call before sending traffic when is_simx to log expected counts per port group.

    Returns:
        Dict[str, int]: port group name -> expected TX packet count
    """
    dut_alias = PerfConsts.DUT_ALIAS
    perf = players[dut_alias]['cli'].performance
    port_groups = getattr(perf, 'port_groups', None) or {}
    result = {}
    for group_name in port_groups:
        key = group_name.replace('_ports', '_num_packets') if group_name.endswith('_ports') else f'{group_name}_num_packets'
        result[group_name] = int(conf_args.get(key, 0) or 0)
    return result


def run_validation(config: ValidationConfig, ignore_violations=False, attach_to_allure=True, add_validator_results_to_mongo_db=True):
    """
    Executes traffic validation based on the provided configuration.

    Args:
        config (ValidationConfig): Configuration object containing validation settings

    Returns:
        tuple: (list of traffic validation results, list of all violations)

    Raises:
        TestIssue: If any validation violations are detected
    """
    with allure.step("Run traffic validation on Json results"):
        traffic_validation_results = validate_traffic_results(players=config.players, test_name=config.test_name,
                                                              scenario=config.scenario,
                                                              samples_params_dict=config.samples_params_dict,
                                                              players_to_be_validated=config.players_to_be_validated,
                                                              attach_to_allure=attach_to_allure,
                                                              add_validator_results_to_mongo_db=add_validator_results_to_mongo_db)

        all_violations = []
        all_validations = config.get_validations()
        validations_to_run = {k: v for k, v in all_validations.items() if v is not None}

        for result in traffic_validation_results:
            player_alias = result['player_alias']
            traffic_json = result['traffic_json']

            if 'tc_pg_collector' in traffic_json:
                allure.dynamic.parameter(f'tc_pg_collector[{player_alias}]',
                                         traffic_json['tc_pg_collector'])

            player_violations = []
            skipped_validations = [n for n, v in all_validations.items() if v is None]
            logging.info(f"[{player_alias}] Skipped validations: {skipped_validations}\n")
            logging.info(f"[{player_alias}] Validations to run: {list(validations_to_run.keys())}\n")

            for name, validation in validations_to_run.items():
                try:
                    logger.info(f"Running validation: {name}")
                    violations_before = len(player_violations)
                    t_start = time.monotonic()
                    validation.func(traffic_json, **(validation.extra_args or {}),
                                    violations_list=player_violations)
                    elapsed = time.monotonic() - t_start
                    logger.info(f"Running validation: {name}: finished in {elapsed:.3f}s, "
                                f"violations found: {len(player_violations) - violations_before}")
                except Exception as e:
                    player_violations.append(f"Validation '{name}' raised an unexpected error: {e}")

            if player_violations:
                player_header = f"Validation failures on {player_alias}:"
                all_violations.append(player_header)
                all_violations.extend([f"  - {violation}" for violation in player_violations])
                all_violations.append("")

        if attach_to_allure:
            with allure.step("Adding SDK dump reference to allure report"):
                sdk_dump_path = os.path.join(BugHandlerConst.NGTS_PATH, "performance_tests",
                                             "sdk_dumps", config.scenario, "sdk_dump")
                create_sdk_dump(config.players, sdk_dump_path)
                shared_dump_path = copy_sdk_dump_to_shared_storage(sdk_dump_path, config.players,
                                                                   config.test_name, config.scenario)
                if shared_dump_path:
                    dump_reference = (f"SDK dump file: {os.path.basename(shared_dump_path)}\n"
                                      f"Full path: {shared_dump_path}")
                    allure.attach(dump_reference, "SDK dump location",
                                  attachment_type=allure.attachment_type.TEXT)
                else:
                    logger.warning("SDK dump was not copied to shared storage; "
                                   "no reference attached to allure report.")

        if all_violations and not ignore_violations:
            raise TestIssue("\n".join(all_violations))

        traffic_validation_jsons_list = [result['traffic_json'] for result in traffic_validation_results]

        return traffic_validation_jsons_list, all_violations


def set_ports_admin_state(players, port_list, port_state="up", step="Test Body - set_ports_admin_state"):
    call_performance_function_with_threads(players, players_aliases=PerfConsts.PERF_SETUP_DUT_ALIASES,
                                           action=f"set ports: {port_list} to {port_state}",
                                           performance_clis_function_name="set_ports",
                                           performance_clis_function_args=(port_list, port_state),
                                           step=step)


def set_shaper_on_traffic_gen(players, speed, shaper_value, shaper_profile="default-global", step="Test Body - set_shaper_on_traffic_gen"):
    call_performance_function_with_threads(players, players_aliases=PerfConsts.PERF_SETUP_TG_ALIASES,
                                           action="set shaper on traffic gen",
                                           performance_clis_function_name="set_shaper",
                                           performance_clis_function_args=(speed, shaper_value, shaper_profile), step=step)


def call_performance_function_with_threads(players, players_aliases, action,
                                           performance_clis_function_name,
                                           performance_clis_function_args, step):

    players_aliases_str = ",".join(players_aliases)
    logging.info(f"Start {action} on {players_aliases_str}")
    threads_list = []
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for player_alias in players_aliases:
        player_cli_obj = players[player_alias]['cli']
        ip_route = player_cli_obj.performance.retrieve_default_route()
        logger.debug(f"{current_time} Action {action} started on {player_alias}. IP Route: {ip_route}")
        with allure.step(f"[{current_time}] Start {action} on player {player_alias}."):
            performance_method = get_obj_method(player_cli_obj.performance, performance_clis_function_name)
            thread = CatchExceptionThread(target=redirect_thread_stdout,
                                          args=(performance_method,
                                                performance_clis_function_args),
                                          name=player_alias)
            threads_list.append(thread)
    for th in threads_list:
        th.start()
    with allure.step(f"Check {action} on {players_aliases_str} was applied correctly"):
        parse_threads_exceptions_at_join(threads_list, players, step)
    completion_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for player_alias in players_aliases:
        player_cli_obj = players[player_alias]['cli']
        ip_route = player_cli_obj.performance.retrieve_default_route()
        logger.debug(f"{completion_time} Action {action} completed on {player_alias}. IP Route: {ip_route}")


def get_obj_method(cli_obj, method_name):
    if hasattr(cli_obj, method_name):
        method = getattr(cli_obj, method_name)
        if callable(method):
            return method
    raise TestIssue(f"Failed to find a callable function with name \"{method_name}\" "
                    f"in {cli_obj.__class__.__name__}")


def skip_test_on_unsupported_os(cli_obj, unsupported_os):
    with allure.step(f"Skip test on unsupported OS: {unsupported_os}"):
        if unsupported_os == CliType.NVUE and isinstance(cli_obj, NvueCli):
            pytest.skip(f"This test is not supported in {CliType.NVUE}")
        elif unsupported_os == CliType.DVS and isinstance(cli_obj, DvsCli):
            pytest.skip(f"This test is not supported in {CliType.DVS}")
        elif unsupported_os == CliType.SONIC and isinstance(cli_obj, SonicCli):
            pytest.skip(f"This test is not supported in {CliType.SONIC}")


def skip_test_on_unsupported_chip_type(current_chip_type, unsupported_chip_type):
    if current_chip_type == unsupported_chip_type:
        pytest.skip(f"This test is not supported in {unsupported_chip_type}")


def skip_performance_test_conditionally(condition, skip_message):
    if condition:
        pytest.skip(skip_message)


def get_topology_obj(players):
    cli_obj = players['dut']['cli']
    if isinstance(cli_obj, DvsCli):
        return get_dvs_topology_obj(players)
    else:
        return get_nvue_sonic_topology_obj(players)


def create_acl_dump(players):
    return players[PerfConsts.DUT_ALIAS]['cli'].performance.create_acl_dump()


def create_occ_watermark_dump(players, sonic_mgmt_path, tar_file_name="occ_headroom_per_port.tar.gz",
                              tar_file_system='/tmp'):
    """
    Copy occupancy and watermark data dump from DUT and attach to Allure report.

    Args:
        players: Test players configuration
        sonic_mgmt_path (str): The local path where the tar file will be copied.
        tar_file_name (str, optional): The name of the tar file on the DUT.
                                       Defaults to "occ_headroom_per_port.tar.gz".
        tar_file_system (str, optional): The remote filesystem path where the tar file is located.
                                         Defaults to '/tmp'.

    Returns:
        Path to the copied tar archive file.
    """
    tar_file = players[PerfConsts.DUT_ALIAS]['cli'].performance.get_occ_watermark_per_port_dump(
        sonic_mgmt_path=sonic_mgmt_path,
        tar_file_name=tar_file_name,
        tar_file_system=tar_file_system
    )

    if os.path.exists(tar_file):
        try:
            allure.attach.file(
                source=tar_file,
                name='Occupancy and Watermark Data',
                extension='.tar.gz'
            )
            logger.info(f"Attached occupancy/watermark tar file to Allure report: {tar_file}")
        except Exception as e:
            logger.warning(f"Failed to attach tar file to Allure report: {e}")
    else:
        logger.warning(f"Tar file not found: {tar_file}")

    return tar_file


def create_sdk_dump(players, full_path):
    """Fetch SDK dump text from the DUT into local file, then decode by content.

    Args:
        players: Test players dict (must include DUT with performance CLI).
        full_path: Intended local path for the dump.

    Returns:
        str: SDK dump file contents.
    """
    return players[PerfConsts.DUT_ALIAS]['cli'].performance.create_sdk_dump(full_path)


def _is_gzip_file(path):
    """Return True if ``path`` starts with the gzip magic header bytes."""
    try:
        with open(path, 'rb') as fh:
            return fh.read(len(PerfConsts.GZIP_MAGIC_BYTES)) == PerfConsts.GZIP_MAGIC_BYTES
    except OSError:
        return False


def copy_sdk_dump_to_shared_storage(local_dump_path, players, test_name, scenario):
    """Copy a locally fetched SDK dump to the shared performance dumps directory.

    The local ``create_sdk_dump`` artifact is overwritten on every test run, so we mirror
    it to ``PerfConsts.SHARED_SDK_DUMPS_DIR`` under a unique name built from timestamp,
    DUT hostname, scenario and test_name. A ``.gz`` suffix is added when the source file
    is still gzipped, so the destination matches the actual byte stream.

    Failures to fetch the hostname or to copy the file are logged and swallowed so a
    storage hiccup never fails the test.

    Args:
        local_dump_path (str): Path to the dump file produced by ``create_sdk_dump``.
        players: Test players dict (DUT entry used to fetch hostname).
        test_name (str): Test identifier, e.g. ``ValidationConfig.test_name``.
        scenario (str): Test scenario identifier, e.g. ``ValidationConfig.scenario``.

    Returns:
        Optional[str]: Destination path on success, ``None`` if the copy was skipped or
        failed.
    """
    if not os.path.exists(local_dump_path):
        logger.warning(f"SDK dump not found at {local_dump_path}; skipping shared copy.")
        return None

    try:
        hostname = players[PerfConsts.DUT_ALIAS]['cli'].chassis.get_hostname()
    except Exception as exc:
        logger.warning(f"Could not fetch DUT hostname for shared SDK dump filename: {exc}")
        hostname = "unknown-host"

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    extension = ".gz" if _is_gzip_file(local_dump_path) else ""
    file_name = f"{timestamp}_{hostname}_{scenario}_{test_name}_sdk_dump{extension}"
    dest_path = os.path.join(PerfConsts.SHARED_SDK_DUMPS_DIR, file_name)

    try:
        os.makedirs(PerfConsts.SHARED_SDK_DUMPS_DIR, exist_ok=True)
        shutil.copy2(local_dump_path, dest_path)
    except OSError as exc:
        logger.warning(f"Failed to copy SDK dump to shared storage {dest_path}: {exc}")
        return None

    logger.info(f"SDK dump copied to shared storage: {dest_path}")
    return dest_path


def configure_incremental_dips_on_tg(players, players_aliases=PerfConsts.PERF_SETUP_TG_ALIASES,
                                     step="basic_test_configuration - configure_incremental_dips_on_tg"):
    call_performance_function_with_threads(players, players_aliases=players_aliases,
                                           action="create incremental dips",
                                           performance_clis_function_name="configure_incremental_dips_on_tg",
                                           performance_clis_function_args=(), step=step)


def modify_pg_buffer_for_connected_ports(players, step="Test Body - modify_pg_buffer_for_connected_ports", parallel_run=True):
    """
    Modify PG buffer configuration for connected ports on traffic generators.

    This function expects pg_buffer_configs to already be in conf_args and loaded into conf.json
    on each TG from apply_test_configuration. It simply runs the sys_sdk test that reads
    the configuration and applies it.

    The test modifies only:
    - pipeline_latency_size (from user config)
    - override_default_max_borrowed_delta (always True)
    - max_borrowed_delta (from user config)

    All other values (size, xon, xoff, is_lossy, etc.) are preserved from current configuration.

    Args:
        players: Test players configuration
        step: Description of the current step for allure reporting
        parallel_run: If True, modifies buffers in parallel. If False, modifies sequentially.

    Returns:
        int: Number of TGs where buffer configuration was successfully modified

    Raises:
        TestIssue: If pg_buffer_config is missing or buffer modification fails on any TG
    """
    if parallel_run:
        call_performance_function_with_threads(players, players_aliases=PerfConsts.PERF_SETUP_TG_ALIASES,
                                               action="modify PG buffer for connected ports",
                                               performance_clis_function_name="modify_pg_buffer_for_connected_ports",
                                               performance_clis_function_args=(),
                                               step=step)
        modified_count = len(PerfConsts.PERF_SETUP_TG_ALIASES)
    else:
        modified_count = 0
        for player_alias in PerfConsts.PERF_SETUP_TG_ALIASES:
            players[player_alias]['cli'].performance.modify_pg_buffer_for_connected_ports()
            modified_count += 1

    return modified_count


def update_port_group_in_df(port_group_df, port_group_name, port_list):
    """
    Update the port group dataframe with the port list
    :param port_group_df: port group dataframe, i.e [{"port": "1", "port_group_name": "left"}, {"port": "2", "port_group_name": "right"}]
    :param port_group_name: port group name, i.e "left" or "right"
    :param port_list: port list, i.e ["1", "2"]
    :return: port group dataframe, i.e [{"port": "1", "port_group_name": "left"}, {"port": "2", "port_group_name": "left"}, {"port": "3", "port_group_name": "right"}]
    """
    for port in port_list:
        port_group_df.append({ValidationConsts.PORT: port, MongoDbConsts.PORT_GROUP_NAME: port_group_name})
    return port_group_df


def _build_default_port_group_df(dut_performance):
    """Build the default DUT port-group dataframe (left_ports / right_ports).

    Prefers cached ``port_groups`` on the performance CLI and falls back to
    ``get_right_left_ports_dict()`` when groups are empty.

    Args:
        dut_performance: DUT performance CLI wrapper.

    Returns:
        list: Port group entries for ``update_port_group_df_on_dut``.
    """
    port_group_df = []
    port_groups = dut_performance.port_groups
    if not port_groups or not any(port_groups.values()):
        port_groups = dut_performance.get_right_left_ports_dict()
    for port_group_name, port_list in port_groups.items():
        if not port_list:
            continue
        sdk_port_list = dut_performance.get_sdk_ports(port_list)
        for port in sdk_port_list:
            port_group_df.append({ValidationConsts.PORT: port,
                                  MongoDbConsts.PORT_GROUP_NAME: port_group_name})
    return port_group_df


def restore_default_port_group_df_on_dut(dut_performance):
    """Push default left/right port groups to DUT ``/tmp/conf.json``.

    SRv6 and other scenarios write custom groups (``ingress_ports``, etc.) that must
    not leak into later SPCX-RA TrafficValidator runs.

    Args:
        dut_performance: DUT performance CLI wrapper.
    """
    port_group_df = _build_default_port_group_df(dut_performance)
    if not port_group_df:
        logging.warning("Skipping restore of default port groups: no SDK ports resolved")
        return
    dut_performance.update_port_group_df_on_dut(port_group_df)
