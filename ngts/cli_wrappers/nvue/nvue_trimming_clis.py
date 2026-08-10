from pprint import pprint
import tempfile
import yaml
from ngts.helpers.performance.traffic_helpers import convert_to_percentage
from ngts.cli_wrappers.common.trimming_clis_common import TrimmingCommon
import allure
import json
import logging
from devts.infra.tools.exceptions.test_issue import TestIssue
from ngts.constants.performance_constants import Cl_Consts, MRCConsts, ValidationConsts
import time
import os
from jinja2 import Environment, FileSystemLoader
from ngts.constants.constants import BugHandlerConst
from ngts.constants.performance_constants import PerfConsts
import pandas as pd
from devts.infra.tools.redmine.redmine_api import is_redmine_issue_active


class NvueTrimmingCli(TrimmingCommon):

    def __init__(self, topology_obj, engine, dut_alias, cli_obj):
        super().__init__(topology_obj, engine, dut_alias, cli_obj)
        self.topology_obj = topology_obj
        self.engine = engine
        self.dut_alias = dut_alias
        self.cli_obj = cli_obj

    def configure_trimming_size(self, trimming_size):
        self.cli_obj.performance.execute_cmd(f"nv set system forwarding packet-trim size {trimming_size}")
        self.cli_obj.performance.execute_cmd(f"nv config apply -y")

    def build_pre_traffic_packet_trim_state_json(self, scenario_tag):
        """Collect pre-traffic packet-trim and WJH service state for NVUE diagnostics.

        Runs NVUE and systemctl commands on the DUT, builds a structured payload, logs a
        warning summary, and returns JSON text suitable for Allure attachment by the caller.

        Args:
            scenario_tag (str): Label for the traffic phase (e.g. ``many_to_one``).

        Returns:
            str: Pretty-printed JSON string of the diagnostic payload.
        """
        wjh_service_name = "what-just-happened.service"
        wjh_status_cmd = f"systemctl status {wjh_service_name} --no-pager --full || true"
        wjh_active_cmd = f"systemctl is-active {wjh_service_name} || true"
        packet_trim_show_cmd = "nv show system forwarding packet-trim || true"
        packet_trim_json_cmd = "nv sh system forwarding packet-trim -o json || true"

        wjh_service_status = self.engine.run_cmd(wjh_status_cmd, print_output=False)
        wjh_active_state = self.engine.run_cmd(wjh_active_cmd, print_output=False).strip()
        packet_trim_show = self.engine.run_cmd(packet_trim_show_cmd, print_output=False)
        packet_trim_json_raw = self.engine.run_cmd(packet_trim_json_cmd, print_output=False)

        try:
            packet_trim_json = json.loads(packet_trim_json_raw)
        except json.JSONDecodeError:
            packet_trim_json = {"raw_parse_error": "Failed to parse packet-trim JSON output"}

        packet_trim_state = packet_trim_json.get("state")
        session_info = packet_trim_json.get("session-info", {})
        service_port = packet_trim_json.get("service-port")
        session_down_reason = session_info.get("session-down-reason")

        indicators = {
            "wjh_service_name": wjh_service_name,
            "wjh_active_state": wjh_active_state,
            "wjh_is_active": wjh_active_state == "active",
            "packet_trim_state": packet_trim_state,
            "packet_trim_service_port": service_port,
            "packet_trim_session_down_reason": session_down_reason,
            "packet_trim_state_enabled": packet_trim_state == "enabled",
            "recirc_session_reports_problem": bool(session_down_reason and "none" not in str(session_down_reason).lower())
        }

        payload = {
            "scenario_tag": scenario_tag,
            "indicators": indicators,
            "raw": {
                "systemctl_status_what_just_happened_service": wjh_service_status,
                "nv_show_system_forwarding_packet_trim": packet_trim_show,
                "nv_sh_system_forwarding_packet_trim_json": packet_trim_json
            }
        }
        logging.warning(f"Pre-traffic NVUE diagnostics ({scenario_tag}): "
                        f"wjh_active_state={wjh_active_state}, "
                        f"packet_trim_state={packet_trim_state}, "
                        f"service_port={service_port}, "
                        f"session_down_reason={session_down_reason}")
        return json.dumps(payload, indent=2)

    def get_trimming_counters(self):
        output_json = self.engine.run_cmd("nv sh system forwarding packet-trim -o json", print_output=False)
        output_dict = json.loads(output_json)
        trimming_counters = output_dict["session-info"]["trimmed-packet-counters"]
        return trimming_counters

    def clear_trimming_counters(self):
        self.engine.run_cmd("nv action clear system forwarding packet-trim counters")
        time.sleep(1)

    def validate_trimmed_untrimmed_dropped_percentages(self, interface_list, trimming_queue, drop_queues, violations_list, return_dict=False, duration=None, pairing_df=None):
        queue_packet_percentages = []
        with allure.step(f"Validate all packets sent to queue {drop_queues} are dropped and trimmed on queue {trimming_queue} for all egress interfaces"):
            queue_counters = self.cli_obj.interface.get_interface_queue_counters(interface_list, counters_type="egress-queue-stats", sub_type=["tx-frames"])
            drop_counters = self.cli_obj.interface.get_interface_counters(interface_list, counters_type="tx-drop")

            for interface in interface_list:
                with allure.step(f"Validate all packets sent to queue {drop_queues} are dropped and trimmed on queue {trimming_queue} for {interface}"):
                    total_drop_packets, trimmed_packets, trimming_percentage, dropped_without_trimming_percentage, untrimmed_percentage, trimming_packets_per_second = self.get_trimming_counters_for_interface(interface, queue_counters, drop_counters, drop_queues, trimming_queue, duration)

                    queue_packet_percentages_dict = {
                        ValidationConsts.PORT: self.cli_obj.performance.get_sdk_port(interface),
                        ValidationConsts.OS_PORT_NAME: interface,
                        ValidationConsts.UNTRIMMED_PERCENTAGE: untrimmed_percentage,
                        ValidationConsts.TRIMMING_PERCENTAGE: trimming_percentage,
                        ValidationConsts.DROPPED_WITHOUT_TRIMMING_PERCENTAGE: dropped_without_trimming_percentage,
                        ValidationConsts.DROPPED_WITHOUT_TRIMMING: bool(total_drop_packets - trimmed_packets),
                        ValidationConsts.TRIMMING_PPS: trimming_packets_per_second
                    }
                    queue_packet_percentages.append(queue_packet_percentages_dict)

                    self.validate_trimming_counters_values(interface, total_drop_packets, trimmed_packets, violations_list)
                    if return_dict:
                        return queue_packet_percentages_dict

        queue_packet_percentages_df = self.attach_queue_packet_percentages_df(queue_packet_percentages, pairing_df)
        return queue_packet_percentages_df.to_dict(orient='records') if not queue_packet_percentages_df.empty else []

    def get_trimming_counters_for_interface(self, interface, queue_counters, drop_counters, drop_queues, trimming_queue, duration):
        total_untrimmed_packets = sum([queue_counters[interface][drop_queue]["tx-frames"] for drop_queue in drop_queues])
        trimmed_packets = queue_counters[interface][trimming_queue]["tx-frames"]
        total_drop_packets = drop_counters.get(interface, 0)
        total_egress_port_packets = total_untrimmed_packets + total_drop_packets

        if total_drop_packets == 0:
            trimming_percentage = 0
            dropped_without_trimming_percentage = 0
            untrimmed_percentage = 0
        else:
            trimming_percentage = convert_to_percentage(trimmed_packets / total_egress_port_packets)
            dropped_without_trimming = total_drop_packets - trimmed_packets
            dropped_without_trimming_percentage = convert_to_percentage(dropped_without_trimming / total_drop_packets)
            untrimmed_percentage = convert_to_percentage(total_untrimmed_packets / total_egress_port_packets)

        if duration and total_drop_packets > 0:
            trimming_packets_per_second = trimmed_packets / duration
        else:
            trimming_packets_per_second = 0

        return total_drop_packets, trimmed_packets, trimming_percentage, dropped_without_trimming_percentage, untrimmed_percentage, trimming_packets_per_second

    def validate_trimming_counters_values(self, interface, total_drop_packets, trimmed_packets, violations_list):
        if trimmed_packets != total_drop_packets and total_drop_packets > 0:
            if not is_redmine_issue_active([4762193])[0]:
                delta = total_drop_packets - trimmed_packets
                violations_list.append(
                    f"Some packets are not dropped and trimmed on {interface} "
                    f"(tx-drop={total_drop_packets}, trim-queue-tx-frames={trimmed_packets}, delta={delta})"
                )

    def attach_queue_packet_percentages_df(self, queue_packet_percentages, pairing_df=None):
        queue_packet_percentages_df = pd.DataFrame(queue_packet_percentages)
        if not queue_packet_percentages_df.empty:
            average_row = queue_packet_percentages_df.mean(numeric_only=True)
            average_row[ValidationConsts.PORT] = "Average"
            average_row[ValidationConsts.OS_PORT_NAME] = "Average"
            average_row[ValidationConsts.DROPPED_WITHOUT_TRIMMING] = queue_packet_percentages_df[ValidationConsts.DROPPED_WITHOUT_TRIMMING].all()
            queue_packet_percentages_df = pd.concat([queue_packet_percentages_df, average_row.to_frame().T], ignore_index=True)

            if pairing_df is not None:
                queue_packet_percentages_df = pd.merge(queue_packet_percentages_df, pairing_df, on=ValidationConsts.OS_PORT_NAME, how='left')

            with allure.step(f"Attach queue_packet_percentages_df"):
                allure.attach(queue_packet_percentages_df.to_html(), "Queue packet percentages dataframe", attachment_type=allure.attachment_type.HTML)
        return queue_packet_percentages_df

    @staticmethod
    def _validate_custom_qos_profile(profile_name, profile, shared_alpha_by_tc, reserved_cells_by_tc):
        if profile is None:
            supported_profiles = ", ".join(sorted(MRCConsts.CUMULUS_DWRR_PROFILES))
            raise ValueError(f"Unsupported Cumulus DWRR profile '{profile_name}'. "
                             f"Supported profiles: {supported_profiles}")

        dwrr_weights = profile.get("weights", {})
        strict_tcs = profile.get("strict_tcs", [])
        invalid_tcs = [tc for tc in list(dwrr_weights) + strict_tcs
                       if not isinstance(tc, int) or isinstance(tc, bool) or tc not in range(8)]
        if invalid_tcs:
            raise ValueError(f"Traffic classes must be integers from 0 through 7: {invalid_tcs}")

        overlapping_tcs = sorted(set(dwrr_weights).intersection(strict_tcs))
        if overlapping_tcs:
            raise ValueError(f"Traffic classes cannot be both DWRR and strict: {overlapping_tcs}")

        invalid_weights = {tc: weight for tc, weight in dwrr_weights.items()
                           if not isinstance(weight, int) or isinstance(weight, bool) or not 1 <= weight <= 100}
        if invalid_weights:
            raise ValueError(f"DWRR weights must be integers from 1 through 100: {invalid_weights}")
        if sum(dwrr_weights.values()) > 100:
            raise ValueError(f"Total configured DWRR weight cannot exceed 100: {sum(dwrr_weights.values())}")

        if shared_alpha_by_tc:
            invalid_alpha_tcs = [tc for tc in shared_alpha_by_tc
                                 if not isinstance(tc, int) or isinstance(tc, bool) or tc not in range(8)]
            if invalid_alpha_tcs:
                raise ValueError(f"Shared-alpha traffic classes must be integers from 0 through 7: "
                                 f"{invalid_alpha_tcs}")
            invalid_alpha_values = {tc: alpha for tc, alpha in shared_alpha_by_tc.items()
                                    if not isinstance(alpha, str) or not alpha.strip()}
            if invalid_alpha_values:
                raise ValueError(f"Shared-alpha values must be non-empty NVUE alpha strings: "
                                 f"{invalid_alpha_values}")

        if reserved_cells_by_tc:
            invalid_reserved_tcs = [tc for tc in reserved_cells_by_tc
                                    if not isinstance(tc, int) or isinstance(tc, bool) or tc not in range(8)]
            if invalid_reserved_tcs:
                raise ValueError(f"Reserved-buffer traffic classes must be integers from 0 through 7: "
                                 f"{invalid_reserved_tcs}")
            invalid_reserved_cells = {tc: cells for tc, cells in reserved_cells_by_tc.items()
                                      if not isinstance(cells, int) or isinstance(cells, bool) or cells < 0}
            if invalid_reserved_cells:
                raise ValueError(f"Reserved-buffer cell counts must be non-negative integers: "
                                 f"{invalid_reserved_cells}")

        return dict(sorted(dwrr_weights.items())), sorted(strict_tcs)

    def configure_custom_dwrr_weights(self):
        """Apply SRv6 Cumulus scheduler and optional egress-buffer overrides."""
        profile_name = MRCConsts.CUMULUS_DWRR_PROFILE
        profile = MRCConsts.CUMULUS_DWRR_PROFILES.get(profile_name)
        shared_alpha_by_tc = MRCConsts.get_cumulus_egress_lossy_shared_alpha_by_tc()
        chip_type = self.cli_obj.performance.get_chip_type()
        reserved_cells_by_tc = MRCConsts.get_cumulus_egress_lossy_reserved_cells_by_tc(chip_type)
        dwrr_weights, strict_tcs = self._validate_custom_qos_profile(profile_name, profile, shared_alpha_by_tc,
                                                                     reserved_cells_by_tc)
        reserved_bytes_by_tc = MRCConsts.get_cumulus_egress_lossy_reserved_bytes_by_tc(chip_type)
        buffer_tcs = sorted(set(shared_alpha_by_tc).union(reserved_bytes_by_tc))
        egress_lossy_buffer_by_tc = {
            tc: {
                "shared_alpha": shared_alpha_by_tc.get(tc),
                "reserved": reserved_bytes_by_tc.get(tc)
            }
            for tc in buffer_tcs
        }

        templates_path = os.path.join(BugHandlerConst.NGTS_PATH, "performance_tests",
                                      PerfConsts.DEFAULT_PERF_TEMPLATES_DIR, "srv6", "cumulus_jinja")
        template_env = Environment(loader=FileSystemLoader(searchpath=templates_path))
        jinja_template = template_env.get_template("custom_qos_overrides.jinja")
        parameter_dict = {
            "dwrr_weights": dwrr_weights,
            "strict_tcs": strict_tcs,
            "egress_lossy_buffer_by_tc": egress_lossy_buffer_by_tc
        }
        output_text = jinja_template.render(parameter_dict=parameter_dict)
        try:
            yaml.safe_load(output_text)
        except yaml.YAMLError:
            logging.error(f"{self.dut_alias}'s custom QoS Jinja template produced invalid YAML:\n"
                          f"{output_text}")
            raise

        with tempfile.NamedTemporaryFile(mode="w", delete=False) as temp_file:
            temp_file.write(output_text)
            temp_path = temp_file.name

        destination_file = "custom_qos_overrides.yaml"
        try:
            self.engine.copy_file(source_file=temp_path, file_system=Cl_Consts.CL_HOME_DIR,
                                  dest_file=destination_file, overwrite_file=True, verify_file=False)
        finally:
            os.remove(temp_path)

        full_path = os.path.join(Cl_Consts.CL_HOME_DIR, destination_file)
        self.cli_obj.general.detach_config(self.engine)
        self.cli_obj.general.patch_config(self.engine, full_path)
        self.cli_obj.general.apply_config(self.engine, option="-y", verify_execution=True)

        scheduler_output = self.engine.run_cmd(
            "nv show qos egress-scheduler default-global", print_output=False)
        buffer_outputs = {}
        for tc in buffer_tcs:
            buffer_outputs[tc] = self.engine.run_cmd(
                f"nv show qos advance-buffer-config default-global egress-lossy-buffer traffic-class {tc}",
                print_output=False)
        with allure.step(f"Attach applied Cumulus QoS profile '{profile_name}'"):
            allure.attach(output_text, "Cumulus custom QoS configuration",
                          attachment_type=allure.attachment_type.TEXT)
            allure.attach(scheduler_output, "Cumulus egress scheduler",
                          attachment_type=allure.attachment_type.TEXT)
            for tc, buffer_output in buffer_outputs.items():
                allure.attach(buffer_output, f"Cumulus TC{tc} egress buffer configuration",
                              attachment_type=allure.attachment_type.TEXT)

    def configure_trim_of_all_packets(self, ports, queues, scenario, template_suite=PerfConsts.DEFAULT_PERF_TEMPLATES_DIR):
        self.cli_obj.general.detach_config(self.engine)
        templates_path = os.path.join(BugHandlerConst.NGTS_PATH, "performance_tests",
                                      template_suite, scenario, "cumulus_jinja")
        templateLoader = FileSystemLoader(searchpath=templates_path)
        templateEnv = Environment(loader=templateLoader)
        TEMPLATE_FILE = "trim_scheduler.jinja"
        jinja_template = templateEnv.get_template(TEMPLATE_FILE)
        parameter_dict = {
            "port_list": ports
        }
        outputText = jinja_template.render(parameter_dict=parameter_dict)
        try:
            yaml.safe_load(outputText)  # just for checking the YAML sanity
        except yaml.YAMLError as yex:
            logging.error(yex)
            logging.error(f"{self.dut_alias}'s Jinja file has resulted in incorrect YAML configuration :- \r\n{pprint.pformat(outputText, depth=12, width=128)}\r\n")
            raise
        fd, path = tempfile.mkstemp()
        with open(path, 'w') as f:
            f.write(outputText)
        self.engine.copy_file(source_file=path, file_system=Cl_Consts.CL_HOME_DIR,
                              dest_file="trim_scheduler.yaml", overwrite_file=True, verify_file=False)
        full_path = os.path.join(Cl_Consts.CL_HOME_DIR, "trim_scheduler.yaml")
        self.cli_obj.general.patch_config(self.engine, full_path)
        self.cli_obj.general.apply_config(self.engine, option="-y", verify_execution=True)
