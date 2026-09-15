#!/usr/bin/env python

from __future__ import annotations

from functools import partial
from pathlib import Path
import logging
import pytest
import os

from ngts.tools.test_utils import allure_utils as allure
from ngts.helpers import system_helpers
from ngts.ngts_types import EnginesT  # , TopologyT  # TODO: enable when VAL in MARS session level is ready

from . import helpers as vg_helpers

logger = logging.getLogger(__name__)
_IGNORE_PATH = str(Path(__file__).with_name('ignores') / 'ignore.{}.txt')
_STATE_DIR = Path(os.path.expanduser("~/.valgrind_manager"))


@pytest.mark.disable_valgrind
@pytest.mark.disable_loganalyzer
def test_start_valgrind(engines: EnginesT, valgrind_services: list[str], valgrind_dockers: list[str]):
    """ Configures the services in SERVICE_LIST to run through valgrind, and restarts them. """
    sudo_engine = system_helpers.PrefixEngine(engines.dut, 'sudo')
    vg_helpers.eval_system_health(sudo_engine)
    with allure.step("Clear valgrind directory & install valgrind package"):
        vg_helpers.ValgrindPkg(sudo_engine).clear_valgrind_dir(valgrind_dockers).install()

    with allure.step("Enable valgrind"):
        if valgrind_dockers:
            with allure.independent_step("Enable valgrind for dockers"):
                vg_helpers.DockerManager(sudo_engine).enable_all(valgrind_dockers)
                vg_helpers.eval_system_ready(engines.dut, reason='after-docker-enable')

        if valgrind_services:
            with allure.independent_step("Enable valgrind for services"):
                vg_helpers.ServiceManager(sudo_engine).enable_all(valgrind_services)
                vg_helpers.eval_system_ready(engines.dut, reason='after-services-enable')


@pytest.mark.disable_valgrind
@pytest.mark.disable_loganalyzer
def test_stop_valgrind(request: pytest.FixtureRequest, engines: EnginesT, valgrind_services: list[str], valgrind_dockers: list[str]):
    """
    Restores the services in SERVICE_LIST to non-valgrind operation and restarts them.
    Also attaches all valgrind output files to the allure report, under Valgrind Results step.
    """
    if request.config.getoption('--valgrind-session-analyze'):
        request.node.addfinalizer(partial(analyze_valgrind_outputs, request, engines))

    sudo_engine = system_helpers.PrefixEngine(engines.dut, 'sudo')
    with allure.step("Disable valgrind"):
        if valgrind_dockers:
            with allure.independent_step("Disable valgrind for dockers"):
                vg_helpers.DockerManager(sudo_engine).disable_all(valgrind_dockers)
                vg_helpers.eval_system_ready(engines.dut, reason='after-docker-disable')

        if valgrind_services:
            with allure.independent_step("Disable valgrind for services"):
                vg_helpers.ServiceManager(sudo_engine).disable_all(valgrind_services)
                vg_helpers.eval_system_ready(engines.dut, reason='after-services-disable')


def analyze_valgrind_outputs(request: pytest.FixtureRequest, engines: EnginesT):
    # TODO: enable when VAL in MARS session level is ready
    raise NotImplementedError("analyze_valgrind_outputs is not implemented")

    # setup_name: str = request.getfixturevalue('setup_name')
    # session_id: str = request.getfixturevalue('session_id')
    # topology_obj: TopologyT = request.getfixturevalue('topology_obj')
    # valgrind_config: vg_analyzer.DecisionConfig = request.getfixturevalue('valgrind_config')

    # remote_root = '/var/log/valgrind'

    # if session_id:
    #     remote_tar = f"/tmp/vg-{session_id}.tgz"
    # else:
    #     remote_tar = "/tmp/vg.tgz"

    # with allure.step("Create tar file"):
    #     engines.dut.run_cmd(f"tar -C {remote_root} -czf {remote_tar} .", validate=True)

    # with allure.step("Copy valgrind output to dumps folder"):
    #     if session_id:
    #         dumps_folder = get_dumps_folder(setup_name, session_id, topology_obj)
    #         dest_tar = os.path.join(dumps_folder, f"valgrind_{session_id}.tgz")
    #     else:
    #         dest_tar = remote_tar

    #     engines.dut.copy_file(
    #         source_file=remote_tar,
    #         dest_file=dest_tar,
    #         file_system='/',
    #         direction='get',
    #         overwrite_file=True,
    #         verify_file=False,
    #     )
    #     os.chmod(dest_tar, 0o664)
    #     allure.attach("Valgrind tarball path", dest_tar)
    #     local_tar = Path(dest_tar)

    # # Extract to RAM (/dev/shm) when available and large enough, else to disk
    # with allure.step("Analyze valgrind outputs"):
    #     with vg_analyzer.ValgrindAnalyzer(local_tar, valgrind_config) as analyzer:
    #         analysis_result: vg_analyzer.ValgrindAnalysisResult | None = None
    #         try:
    #             analysis_result = analyzer.analyze()
    #         except Exception:
    #             analysis_result = analysis_result or analyzer.last_result or vg_analyzer.ValgrindAnalysisResult(services={})
    #             logger.info(
    #                 "Valgrind analysis failed: %d services reported issues (threshold exceeded)",
    #                 len(analysis_result.services),
    #             )
    #             raise
    #         else:
    #             logger.info(
    #                 "Valgrind analysis completed: %d services reported issues",
    #                 len(analysis_result.services),
    #             )
