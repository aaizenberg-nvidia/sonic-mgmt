from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path
import logging
import sys
import os

import requests
import retry

FILE_PATH = Path(__file__).resolve()
_PATH_TO_UPLOAD_URL = Path("/auto/sw_system_project/NVOS_INFRA/verification_files/")
sonic_mgmt_path = FILE_PATH.parents[len(FILE_PATH.parts) - FILE_PATH.parts.index('ngts') - 1]
sys.path.append(str(sonic_mgmt_path))

from ngts.constants.constants import CliType, DbConstants, InfraConst  # noqa: E402
from ngts.nvos_constants.constants_nvos import TopologyConsts  # noqa: E402
from ngts.scripts.pbi import la_helpers, parser_helpers, database_helpers  # noqa: E402
from ngts.scripts.pbi.models import Args, LABug, Summary, TestResult  # noqa: E402
from ngts.tools.mars_test_cases_results.Connect_to_MSSQL import ConnectMSSQL  # noqa: E402

logger = logging.getLogger(Path(__file__).stem if __name__ == "__main__" else __name__)


# **********************************************************************************************************************
# Main Helpers
# **********************************************************************************************************************
def _setup_logger(level: int = logging.DEBUG):
    logging.basicConfig(
        level=level,
        format='[%(asctime)s.%(msecs)03d][%(levelname)-7s][%(name)-22s %(lineno)4d] %(message)s'
    )


def _parse_args() -> Args:
    parser = ArgumentParser(description='Process some integers.')
    parser.set_defaults(
        allure_server_addr=InfraConst.ALLURE_SERVER_URL,
    )
    parser.add_argument('--setup_name', dest='setup_name', help='Setup name')
    parser.add_argument('--session_id', default="", dest='session_id', help='Session id')
    parser.add_argument('--target-version', type=parser_helpers.parse_version, default="", dest='target_version', help='Target version')
    parser.add_argument('--tarball', type=parser_helpers.parse_branch_name, default="", dest='branch', help='Tarball')
    parser.add_argument('--dut_hwsku', default="", dest='dut_hwsku', help='Switch Type')

    return parser.parse_args(namespace=Args())
# **********************************************************************************************************************


@retry.retry(Exception, tries=3, delay=3)
def insert_data_to_pbi_db(args: Args, parsed_results: list[TestResult], summary: Summary, loganalyzer_bugs_summary: list[LABug]) -> None:
    if not args.target_version:
        return

    mssql_connection_obj = None
    try:
        connections_params = DbConstants.CREDENTIALS[CliType.NVUE]
        mssql_connection_obj = ConnectMSSQL(**connections_params)
        mssql_connection_obj.connect_db()
        logger.info("Connection to DB was completed successfully")

        logger.info("Insert results to test_analytics DB")
        database_helpers.insert_test_analytics_data_to_pbi_db(args, mssql_connection_obj, parsed_results)

        logger.info("Insert results to regression_matrix DB")
        database_helpers.insert_regression_matrix_data_to_pbi_db(args, mssql_connection_obj, summary)

        logger.info("Insert results to loganalyzer_bug DB")
        database_helpers.insert_loganalyzer_bugs_data_to_pbi_db(args, mssql_connection_obj, loganalyzer_bugs_summary)

    finally:
        if mssql_connection_obj:
            mssql_connection_obj.disconnect_db()


def summarize_results_and_upload(report_url: str, args: Args) -> None:
    if TopologyConsts.NVOS.lower() not in args.allure_project_id.lower():
        logger.info(f"Skipping upload for {args.allure_project_id} because it is not a NVOS project")
        return

    try:
        base_url = os.path.dirname(report_url.rstrip("/"))
        logger.debug(f"Base URL: {base_url}")
        suites_resp = requests.get(f"{base_url}/data/suites.json")
        suites_resp.raise_for_status()
        suites_data = suites_resp.json()

        logger.info("Parse run results:")
        parsed_results = parser_helpers.parse_suites(suites_data, base_url, [], [])

        logger.info("Summarize run's results:")
        summary = Summary.summarize_test_run(parsed_results, report_url)

        logger.info("Parse loganalyzer bugs:")
        loganalyzer_bugs_summary = la_helpers.parse_loganalyzer_bugs(parsed_results, base_url)

        insert_data_to_pbi_db(args, parsed_results, summary, loganalyzer_bugs_summary)
    except Exception as e:
        logger.error(f"Failed with the following issue: {e}")
        logger.exception(e)


def main():
    args = _parse_args()
    _setup_logger()

    file_path = _PATH_TO_UPLOAD_URL / f"{args.allure_project_id}.txt"
    logger.info(f"File Path: {file_path}")
    if not (report_url := file_path.read_text().strip()):
        file_path.unlink()
        raise ValueError(f"Failed to retrieve report URL from {file_path}")

    logger.info(f"Retrieved URL: {report_url}")
    file_path.unlink()

    summarize_results_and_upload(report_url, args)


if __name__ == "__main__":
    main()
