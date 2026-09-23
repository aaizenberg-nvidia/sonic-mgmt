import os
import sys
import argparse
import logging
import json

path = os.path.abspath(__file__)
sonic_mgmt_path = path.split('/ngts/')[0]
sys.path.append(sonic_mgmt_path)

from ngts.constants.constants import FILE_INCLUDE_FAILED_SANITY_CHECKER_CASE

SANITY_CHECKER_TEST_ACTION_MAP = {
    "test_cpld_version_check": "raise warning msg in allure report and disable bug handler",
    "test_device_asic_check": "stop",
    "test_cable_connection_between_dut_and_fanout_check": "stop",
    "test_cable_connection_for_canonical_check": "stop",
    "test_bgp_session_status_check": "stop",
    "test_more_then_2_fan_status_wrong_check": "stop",
    "test_fan_status_check": "raise warning msg in allure report and disable bug handler",
    "test_psu_status_check": "raise warning msg in allure report and disable bug handler",
    "test_core_dump_file_in_var_core_check": "raise warning msg in allure report and disable bug handler",
    "test_component_version_check": "stop",
    "test_bios_version_check": "raise warning msg in allure report and disable bug handler"
}
RETURN_CODE = {"stop": 1,
               "continue": 0}
ALLURE_RESULT_FOLDER = "/tmp/allure-results"


def get_logger():
    log = logging.getLogger('analyze_sanity_checker_result')
    log.setLevel(logging.DEBUG)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    log.addHandler(handler)
    return log


logger = get_logger()


def get_sanity_checker_result_from_allure_result():
    # get result file name list
    test_result_file_list = []
    allure_result_files = os.listdir(ALLURE_RESULT_FOLDER)

    for filename in allure_result_files:
        if filename.endswith("result.json"):
            test_result_file_list.append(filename)

    # Save latest result of each case, as rerun can override failed cases
    latest_run_per_case = {}

    for filename in test_result_file_list:
        with open(os.path.join(ALLURE_RESULT_FOLDER, filename)) as f:
            case_res = json.load(f)
            case_name = case_res.get("name", "")
            if case_name in SANITY_CHECKER_TEST_ACTION_MAP:
                start = case_res.get("start", 0)
                if case_name not in latest_run_per_case or start > latest_run_per_case[case_name]["start"]:
                    latest_run_per_case[case_name] = {"start": start, "status": case_res["status"]}

    sanity_checker_case_res_dict = {name: data["status"] for name, data in latest_run_per_case.items()}
    logger.info(f"sanity check case results: {sanity_checker_case_res_dict}")
    return sanity_checker_case_res_dict


def take_action_based_on_sanity_checker_result(skip_stop_regression, setup_name):
    sanity_checker_case_res_dict = get_sanity_checker_result_from_allure_result()
    failed_sanity_checker_cases = []
    skip_setup_list_when_cpld_check_fail = ["sonic_tigon_r-tigon-15"]

    logger.info(f"skip stop regression :{skip_stop_regression}")
    for case_name, case_status in sanity_checker_case_res_dict.items():
        if "pass" not in case_status.lower() and "skip" not in case_status.lower():
            if skip_stop_regression != "yes":
                if SANITY_CHECKER_TEST_ACTION_MAP[case_name] == "stop" or (
                        case_name == "test_cpld_version_check" and setup_name in skip_setup_list_when_cpld_check_fail):
                    logger.info(f"{case_name} fails so that it cause stop regression")
                    return RETURN_CODE["stop"]
            failed_sanity_checker_cases.append(case_name)

    if failed_sanity_checker_cases:
        write_failed_sanity_checker_cases_to_file(failed_sanity_checker_cases)

    logger.info(f"failed sanity checker cases: {failed_sanity_checker_cases}")
    return RETURN_CODE["continue"]


def write_failed_sanity_checker_cases_to_file(failed_sanity_checker_cases):
    with open(FILE_INCLUDE_FAILED_SANITY_CHECKER_CASE, "a") as f:
        logger.info(f"write failed sanity checker cases into {FILE_INCLUDE_FAILED_SANITY_CHECKER_CASE}")
        f.write(",".join(failed_sanity_checker_cases))


def parse_args():
    parser = argparse.ArgumentParser(description='Process some parameters.')
    parser.add_argument("--skip_stop_regression", default="no",
                        help="When True, skip stopping regression, else not stop")
    parser.add_argument('--setup_name', dest='setup_name', help='Setup name')

    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    return_code = take_action_based_on_sanity_checker_result(args.skip_stop_regression, args.setup_name)
    sys.exit(return_code)
