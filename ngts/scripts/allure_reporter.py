import os
import sys
import requests
import json
import base64
import argparse
import logging
import time
import uuid
from requests.packages import urllib3
urllib3.disable_warnings()

path = os.path.abspath(__file__)
sonic_mgmt_path = path.split('/ngts/')[0]
sys.path.append(sonic_mgmt_path)

from ngts.constants.constants import InfraConst  # noqa: E402
from ngts.nvos_constants.constants_nvos import NvosConst, TopologyConsts  # noqa: E402

# Synthetic "MISSING TESTS IN CASES" test is stamped with the earliest possible start
# so it sorts first in the aggregated Allure report (Allure orders tests by start ascending).
MISSING_TESTS_START_MS = 1
ALLURE_PROJECT_ID_SUFFIX = "-session-reports"
ALLURE_DOCKER_SERVICE = 'allure-docker-service'
HTTP_TIMEOUT = 300
SSL_VERIFICATION = False


def get_logger():
    log = logging.getLogger('AllureReporter')
    log.setLevel(logging.DEBUG)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    log.addHandler(handler)
    return log


logger = get_logger()


def parse_args():
    parser = argparse.ArgumentParser(description='Process some integers.')
    parser.add_argument("--action", required=True, dest="action", default="upload",
                        choices=["upload", "generate", "cleanup", "synthesize"], help="Action to do")
    parser.add_argument('--setup_name', dest='setup_name', help='Setup name')
    parser.add_argument('--cli_type', dest='cli_type',
                        required=False, default="", help='cli type of the OS')
    parser.add_argument('--test_name', dest='test_name', required=False, default=None,
                        help='Name of the synthetic test case injected by the "synthesize" action')
    parser.add_argument('--test_status', dest='test_status', required=False, default='skipped',
                        choices=['passed', 'failed', 'broken', 'skipped'],
                        help='Status of the synthetic test case injected by the "synthesize" action')
    parser.add_argument('--test_body', dest='test_body', required=False, default='',
                        help='Free-text body/description to embed in the synthetic test case '
                             '(e.g. the reason no real tests were executed)')
    parser.add_argument('--suite_name', dest='suite_name', required=False, default=None,
                        help='Suite name for the synthetic test case (defaults to --setup_name)')
    parser.add_argument('--project_id', dest='project_id', required=False, default=None,
                        help='Allure project ID; overrides the ID derived from --setup_name. '
                             'Required for "synthesize" when --setup_name is not provided.')

    return parser.parse_args()


def upload_results(results_directory, allure_server, project_id):
    if os.path.exists(results_directory):
        create_project(allure_server, project_id)
        allure_report_items_list = collect_allure_results(results_directory)
        upload_data_to_server(allure_report_items_list, allure_server, project_id)


def create_project(allure_server_url, allure_project):
    data = {'id': allure_project}
    http_headers = {'Content-type': 'application/json'}
    url = allure_server_url + '/projects'

    if requests.get(url + '/' + allure_project, timeout=HTTP_TIMEOUT, verify=SSL_VERIFICATION).status_code != 200:
        logger.info('Creating project {} on allure server'.format(allure_project))
        response = requests.post(url, json=data, headers=http_headers, timeout=HTTP_TIMEOUT, verify=SSL_VERIFICATION)
        if response.raise_for_status():
            logger.error('Failed to create project on allure server, error: {}'.format(response.content))
    else:
        logger.info('Allure project {} already exist on server. No need to create project'.format(allure_project))


def collect_allure_results(allure_report_dir):
    allure_report_file_list = os.listdir(allure_report_dir)

    allure_report_items_list = []
    for allure_report_file_item in allure_report_file_list:
        report_item_dict = {}

        file_path = allure_report_dir + "/" + allure_report_file_item
        logger.info(file_path)

        if os.path.isfile(file_path):
            try:
                with open(file_path, "rb") as f:
                    content = f.read()
                    if content.strip():
                        b64_content = base64.b64encode(content)
                        report_item_dict['file_name'] = allure_report_file_item
                        report_item_dict['content_base64'] = b64_content.decode('UTF-8')
                        allure_report_items_list.append(report_item_dict)
                    else:
                        logger.info('Empty File skipped: ' + file_path)
            finally:
                f.close()
        else:
            logger.info('Directory skipped: ' + file_path)

    return allure_report_items_list


def upload_data_to_server(allure_report_items_list, allure_server, project_id):
    headers = {'Content-type': 'application/json'}
    request_body = {
        "results": allure_report_items_list
    }
    json_request_body = json.dumps(request_body)

    logger.info("------------------SEND-RESULTS------------------")
    start_time = time.time()
    send_result_url = '{}/send-results?project_id={}'.format(allure_server, project_id)
    diff_time = time.time() - start_time
    logger.info(f"uploading data takes {diff_time}")
    response = requests.post(send_result_url, headers=headers, data=json_request_body, verify=SSL_VERIFICATION,
                             timeout=HTTP_TIMEOUT)
    logger.info("STATUS CODE:")
    logger.info(response.status_code)


def store_allure_url(allure_project, report_url):
    try:
        if TopologyConsts.NVOS.lower() in allure_project.lower():
            logger.info("Upload url path to file")
            file_path = os.path.join(NvosConst.allure_url_folder(allure_project), f"{allure_project}.txt")
            with open(file_path, "w") as f:
                f.write(report_url)
            logger.info(f"File {file_path} created")
            with open(file_path, "r") as f:
                content = f.read()
            logger.info(f"Contents of {file_path}:\n{content}")
    except BaseException as ex:
        logger.warning(f"Failed to store allure url for NVOS ({ex})")


def generate_report(allure_server_url, allure_project):
    allure_report_url = predict_allure_report_link(allure_server_url, allure_project)
    store_allure_url(allure_project, allure_report_url)

    start_time = time.time()
    try:
        response = requests.get('{}/generate-report?project_id={}'.format(allure_server_url, allure_project),
                                verify=SSL_VERIFICATION, timeout=HTTP_TIMEOUT)
        logger.info(f"RESPONSE STATUS: {response.ok}")
        if not response.ok:
            logger.info("project doesn't exist, creating project")
            create_project(allure_server_url, allure_project)
            response = requests.get('{}/generate-report?project_id={}'.format(allure_server_url, allure_project),
                                    verify=SSL_VERIFICATION, timeout=HTTP_TIMEOUT)
            logger.info(f"RESPONSE STATUS: {response.ok}")
        response = response.json()
        diff_time = time.time() - start_time
        logger.info(f"generating report takes {diff_time}")
        report_url = response['data']['report_url']
        logger.info('Allure report URL: {}'.format(report_url))
        cleanup_report(allure_server_url, allure_project)
    except requests.exceptions.Timeout:
        diff_time = time.time() - start_time
        logger.warning(
            "Timed out after {:.0f}s waiting for generate-report response; report generation is "
            "likely still running on the server. Predicted Allure report URL: {}".format(
                diff_time, allure_report_url)
        )


def predict_allure_report_link(allure_server_url, allure_project):
    """
    THe function is to construct allure report link base on the current allure report index.
     The new allure report index is equal to the current index + 1
    """
    allure_report_url = None
    try:
        response = requests.get('{}/projects/{}'.format(allure_server_url, allure_project),
                                verify=SSL_VERIFICATION, timeout=HTTP_TIMEOUT).json()
        allure_report_index = int(response['data']['project']["reports_id"][1]) + 1 \
            if len(response['data']['project']["reports_id"]) > 1 else 1

        allure_report_url = "{}/projects/{}/reports/{}/index.html".format(
            allure_server_url, allure_project, allure_report_index)
    except Exception as err:
        logger.error("Fail to construct allure report url. Err:{}".format(err))
    else:
        logger.info("\n\n\n When allure report size it too bigger, generating allure report usually timeouts"
                    "\n To avoid missing allure report link"
                    "\n We construct the allure report link base current report index before generating allure report"
                    "\n Predict Allure report URL:{} \n\n\n".format(allure_report_url))
    return allure_report_url


def cleanup_report(allure_server_url, allure_project):
    try:
        requests.get('{}/clean-results?project_id={}'.format(allure_server_url, allure_project),
                     verify=SSL_VERIFICATION, timeout=HTTP_TIMEOUT)
    except requests.exceptions.Timeout:
        # clean-results is a synchronous, potentially slow server-side delete (e.g. a large
        # staging backlog from a prior session). It keeps running after our client-side read
        # times out, so don't fail the MARS step for this - it isn't a real failure.
        logger.warning(
            "Timed out after {:.0f}s waiting for clean-results response for project {}; "
            "cleanup is likely still running on the server.".format(HTTP_TIMEOUT, allure_project)
        )


def build_synthetic_result_item(test_name, suite_name, status, body):
    now_ms = int(time.time() * 1000)
    test_uuid = str(uuid.uuid4())
    result = {
        "uuid": test_uuid,
        "historyId": test_uuid,
        "name": test_name,
        "fullName": test_name,
        "description": body,
        "status": status,
        "stage": "finished",
        "start": now_ms,
        "stop": now_ms,
        "statusDetails": {
            "message": body,
        },
        "labels": [
            {"name": "suite", "value": suite_name},
            {"name": "framework", "value": "pytest"},
            {"name": "language", "value": "python"},
        ],
    }
    content = json.dumps(result).encode('UTF-8')
    return {
        'file_name': '{}-result.json'.format(test_uuid),
        'content_base64': base64.b64encode(content).decode('UTF-8'),
    }


def synthesize_empty_report(allure_server_url, allure_project, test_name, suite_name, status, body):
    create_project(allure_server_url, allure_project)
    result_item = build_synthetic_result_item(test_name, suite_name, status, body)
    upload_data_to_server([result_item], allure_server_url, allure_project)
    generate_report(allure_server_url, allure_project)


def build_missing_case_step(record):
    step_name = record.get('step_name', 'unknown-step')
    setup_name = record.get('setup_name', '')
    session_id = record.get('session_id', '')
    mars_key_id = record.get('mars_key_id', '')
    db_name = record.get('db_name', '')
    case_name = record.get('case_name', '')
    test_name = record.get('test_name', '')
    pytest_cmd = record.get('pytest_cmd', '')
    reason = record.get('reason', '')
    message = (
        "No tests were collected for this MARS case.\n"
        "Setup: {}\nSession id: {}\nMARS db: {}\nMARS test: {}\nMARS case: {}\nMARS key id: {}\n"
        "Reason: {}\nPytest command: {}"
    ).format(setup_name or 'n/a', session_id or 'n/a', db_name or 'n/a', test_name or 'n/a',
             case_name or 'n/a', mars_key_id or 'n/a', reason, pytest_cmd)
    parameters = [
        {"name": "MARS db", "value": db_name or 'n/a'},
        {"name": "MARS test", "value": test_name or 'n/a'},
        {"name": "MARS case", "value": case_name or 'n/a'},
        {"name": "MARS key id", "value": mars_key_id or 'n/a'},
        {"name": "Session id", "value": session_id or 'n/a'},
        {"name": "Setup", "value": setup_name or 'n/a'},
        {"name": "Reason", "value": reason or 'n/a'},
        {"name": "Pytest command", "value": pytest_cmd or 'n/a'},
    ]
    return {
        "name": "{} (case: {})".format(step_name, case_name) if case_name else step_name,
        "status": "failed",
        "stage": "finished",
        "start": MISSING_TESTS_START_MS,
        "stop": MISSING_TESTS_START_MS,
        "statusDetails": {"message": message},
        "steps": [],
        "attachments": [],
        "parameters": parameters,
    }


def build_missing_cases_result_item(records, suite_name):
    test_uuid = str(uuid.uuid4())
    result = {
        "uuid": test_uuid,
        "historyId": test_uuid,
        "name": "MISSING TESTS IN CASES",
        "fullName": "MISSING TESTS IN CASES",
        "status": "failed",
        "stage": "finished",
        "start": MISSING_TESTS_START_MS,
        "stop": MISSING_TESTS_START_MS,
        "steps": [build_missing_case_step(record) for record in records],
        "labels": [
            {"name": "suite", "value": suite_name},
            {"name": "framework", "value": "pytest"},
            {"name": "language", "value": "python"},
        ],
    }
    content = json.dumps(result).encode('UTF-8')
    return {
        'file_name': '{}-result.json'.format(test_uuid),
        'content_base64': base64.b64encode(content).decode('UTF-8'),
    }


def consume_empty_allure_steps(allure_server_url, allure_project, empty_steps_file):
    if not os.path.exists(empty_steps_file):
        return

    with open(empty_steps_file, 'r') as f:
        lines = [line.strip() for line in f if line.strip()]

    os.remove(empty_steps_file)

    if not lines:
        return

    records = []
    for line in lines:
        try:
            records.append(json.loads(line))
        except ValueError:
            logger.warning('Skipping malformed empty-step record: {}'.format(line))

    if not records:
        return

    create_project(allure_server_url, allure_project)
    result_item = build_missing_cases_result_item(records, "MISSING TESTS IN CASES")
    upload_data_to_server([result_item], allure_server_url, allure_project)
    logger.info('Injecting "MISSING TESTS IN CASES" with {} step(s)'.format(len(records)))


if __name__ == "__main__":
    args = parse_args()

    allure_report_dir = InfraConst.ALLURE_REPORT_DIR
    allure_server_addr = InfraConst.ALLURE_SERVER_URL
    setup_name = args.setup_name
    cli_type = args.cli_type
    cli_type_str = f"-{cli_type}" if cli_type else ""
    if args.project_id:
        allure_project_id = args.project_id
    elif setup_name is not None:
        allure_project_id = (setup_name.replace('_', '-') + cli_type_str).lower() + ALLURE_PROJECT_ID_SUFFIX
    else:
        allure_project_id = None
    allure_server_base_url = '{}/{}'.format(allure_server_addr, ALLURE_DOCKER_SERVICE)

    if args.action == 'upload':
        upload_results(allure_report_dir, allure_server_base_url, allure_project_id)
    if args.action == 'generate':
        empty_steps_file = InfraConst.CASE_INCLUDE_EMPTY_ALLURE_STEPS_TMPL.format(allure_project_id)
        consume_empty_allure_steps(allure_server_base_url, allure_project_id, empty_steps_file)
        generate_report(allure_server_base_url, allure_project_id)
    if args.action == 'cleanup':
        cleanup_report(allure_server_base_url, allure_project_id)
    if args.action == 'synthesize':
        if not args.test_name:
            raise ValueError('--test_name is required for the "synthesize" action')
        if not allure_project_id:
            raise ValueError('--setup_name or --project_id is required for the "synthesize" action')
        synthesize_empty_report(allure_server_base_url, allure_project_id, args.test_name,
                                args.suite_name or setup_name, args.test_status, args.test_body)
