"""
MARS/regression entry point: submit a secret-scan request for the DUT.

A thin pytest wrapper around ngts.tools.secret_scan so a regression can run
this as a step - reusing the ngts infra (topology / engine fixtures) - while
the real logic lives in the reusable, NOS-agnostic client (client.py). Not a
functional test: no setup-selector markers, so regular sweeps skip it; MARS
runs it by pointing --test_script at this file.

The request fields come in as fixtures: project / release_name / dut_version /
dut_name / notify (defined in conftest.py); setup_name and session_id come
from the shared ngts framework (rootdir conftest + ngts.tools.infra plugin).
"""
import logging

from ngts.tools.secret_scan.client import SecretScanClient
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger()


def test_submit_secret_scan_request(topology_obj, engines, setup_name,
                                    session_id, project, release_name,
                                    dut_version, dut_name, notify) -> None:
    with allure.step(f"Submit tech-support secret-scan (TruffleHog) request "
                     f"for DUT {dut_name} session {session_id}"):
        client = SecretScanClient.create_client_for_topology(
            topology_obj, engines.dut,
            project=project, release=release_name, version=dut_version,
            dut_name=dut_name, notify=notify,
            setup_name=setup_name, session_id=session_id)

        request_dir = client.submit()
        logger.info(f"secret-scan request submitted: {request_dir}")
        assert request_dir, "secret-scan request was not submitted"
