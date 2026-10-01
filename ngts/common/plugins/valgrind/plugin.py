from __future__ import annotations

from allure import attachment_type
from pathlib import Path
from typing import Self
import hashlib
import logging
import pytest
import shlex
import json
import os
import re

from ngts.tools.test_utils import allure_utils as allure
from ngts.constants.constants import InfraConst
from ngts.ngts_types import EnginesT

from . import patches, helpers as vg_helpers, analyzer as vg_analyzer

logger = logging.getLogger(__name__)

_DEFAULT_IGNORE_DIR = Path(__file__).parent / "ignores"
_VG_IGNORE_REGISTRY_ATTR = "_vg_ignore_registry"


class ValgrindLogFilesSnapshot:
    def __init__(self, nodeid: str, engines: EnginesT):
        self._log = logger.getChild(self.__class__.__name__)
        self._nodeid = nodeid
        self._engine_dut = engines.dut

        self._before: dict[str, int] | None = None
        self._after: dict[str, int] | None = None

    def __enter__(self) -> Self:
        with allure.step('valgrind mark valgrind output files'):
            self._copy_valgrind_helper_scripts_to_dut()
            self._before = self._scan_valgrind_output(f'/tmp/vg.{self._nodeid}_before.json', assume_empty=True)

        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self._copy_valgrind_helper_scripts_to_dut()
        self._after = self._scan_valgrind_output(f'/tmp/vg.{self._nodeid}_after.json')

    def _copy_valgrind_helper_scripts_to_dut(self) -> None:
        with allure.step("Copy valgrind helper scripts to dut"):
            for file in ('vg_scan.py',):  # case we want to add more scripts in the future
                source_file = Path(__file__).parent / file
                dest_file = f'/tmp/{file}'

                cmd = f"if [ -f {shlex.quote(str(dest_file))} ]; then sha256sum {shlex.quote(str(dest_file))}; else echo no; fi"
                remote_file: str = self._engine_dut.run_cmd(cmd).strip()

                if not (need_copy := remote_file == 'no'):
                    need_copy = hashlib.sha256(source_file.read_bytes()).hexdigest() != remote_file.split()[0]

                if need_copy:
                    self._engine_dut.copy_file(
                        source_file=source_file,
                        dest_file=dest_file,
                        file_system='/',
                        direction='put',
                        overwrite_file=True,
                        verify_file=False,
                    )

                    self._engine_dut.run_cmd(f'chmod +x /tmp/{file}')
                else:
                    logger.info(f"Valgrind helper script {file} already exists at {dest_file}")

    def _scan_valgrind_output(self, output_file: str | None = None, /, *, assume_empty: bool = False) -> dict[str, int]:
        with allure.step("Scan valgrind output"):
            if not output_file:
                result = self._engine_dut.run_cmd('python3 /tmp/vg_scan.py')
                return json.loads(result)

            quoted_out = shlex.quote(output_file)
            result: str = self._engine_dut.run_cmd(f'python3 /tmp/vg_scan.py -o {quoted_out}', validate=True)
            match = re.search(r'^Snapshot saved to:\s*(.+)$', result, re.MULTILINE)
            if not match:
                raise ValueError(f"Could not find 'Snapshot saved to:' line in vg_scan.py output: {result!r}")
            result_file_loc = Path(match.group(1).strip())

            dest_file = Path(output_file)
            if not result_file_loc.is_absolute():
                result_file_loc = Path('/tmp') / result_file_loc
            elif not str(result_file_loc).startswith('/tmp/'):
                logger.warning(f"Unexpected absolute path outside /tmp/: {result_file_loc}")
                result_file_loc = Path('/tmp') / result_file_loc
            dest_file.parent.mkdir(parents=True, exist_ok=True)

            self._engine_dut.copy_file(
                source_file=result_file_loc,
                dest_file=dest_file,
                file_system='/',
                direction='get',
                overwrite_file=True,
                verify_file=False,
            )

            try:
                result = json.loads(text := dest_file.read_text())
            except json.JSONDecodeError:
                logger.error(f"Failed to parse JSON from {dest_file}: {text!r}")
                if assume_empty:
                    return {}
                raise

            allure.attach(output_file, result, attachment_type.JSON, log=False)
            return result

    def _diff_valgrind_output(self, before: dict[str, int], after: dict[str, int], /, *, only_grown: bool = True) -> dict[str, dict[str, int]]:
        changes: dict[str, dict[str, int]] = {}

        with allure.step("Diff valgrind output"):
            all_paths = set(before) | set(after)
            logger.debug(f"all_paths: {all_paths}")
            for path in sorted(all_paths):
                logger.debug(f"path: {path}")
                old = before.get(path, 0)
                new = after.get(path, 0)
                delta = new - old

                if only_grown and delta <= 0:
                    continue

                changes[path] = {
                    "old": old,
                    "new": new,
                    "delta": delta,
                }

            return changes

    @property
    def diff(self) -> dict[str, dict[str, int]]:
        if self._before is None or self._after is None:
            # TODO: check... do I want to raise an error here? or just return empty dict?
            raise ValueError("Before or after is not set")

        diff = self._diff_valgrind_output(self._before, self._after)
        self._log.debug(f"diff: {diff}")
        allure.attach('Valgrind diff', json.dumps(diff, indent=2), attachment_type=attachment_type.JSON, log=False)
        return diff


def pytest_addoption(parser: pytest.Parser):
    valgrind = parser.getgroup("Valgrind")
    valgrind.addoption('--valgrind-analyze', action='store_true', default=False, help='Enable valgrind')
    valgrind.addoption('--valgrind-multiplier', nargs='?', type=float, default=None, const=5,
                       help='Multiplier for valgrind mode (increase SSH timeouts)')
    valgrind.addoption('--valgrind-session-analyze', action='store_true', default=False, help='Enable valgrind session analyze')

    valgrind.addoption('--vg-definitely-threshold', type=int, default=2048,
                       help='Max allowed definitely lost in bytes (default %(default)s)')
    valgrind.addoption('--vg-indirectly-threshold', type=int, default=2048,
                       help='Max allowed indirectly lost in bytes (default %(default)s)')
    valgrind.addoption('--vg-possibly-threshold', type=int, default=8192,
                       help='Max allowed possibly lost in bytes (default %(default)s)')
    valgrind.addoption('--vg-no-fail-on-warnings', dest='vg_fail_on_warnings', action='store_false', default=True, help='Fail on warnings')

    valgrind.addoption('--vg-no-ignores', action='store_true', default=False,
                       help='Disable valgrind ignore-traces even if ignore files exist')
    valgrind.addoption('--vg-ignore-dir', default=str(_DEFAULT_IGNORE_DIR), type=Path,
                       help='Directory containing valgrind ignore files (default: %(default)s)')

    valgrind.addoption(
        '--vg-bug-handler-scope',
        choices=list(vg_analyzer.BugHandlerScope),
        type=vg_analyzer.BugHandlerScope.from_value,
        default=vg_analyzer.BugHandlerScope.SUB_SERVICE,
        help='Scope of bug handler (default %(default)s)',
    )
    valgrind.addoption(
        '--vg-trace-id-strategy',
        choices=list(vg_analyzer.TraceIdStrategy),
        type=vg_analyzer.TraceIdStrategy.from_value,
        default=vg_analyzer.TraceIdStrategy.BY3,
        help='Trace-id strategy for ignore matching (default: %(default)s)',
    )

    valgrind.addoption('--vg-poc-mocks', action='store_true', default=False, help='Enable valgrind POC mocks')
    valgrind.addoption('--vg-poc-mocks-r5-artifacts', action='store_true', default=False, help='Enable valgrind POC mocks for R5 artifacts')


def _scale_timeout_option(config: pytest.Config, name: str, multiplier: float) -> None:
    value = config.getoption(name, default=None)
    if value is not None:
        setattr(config.option, name, float(value) * multiplier)


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config):
    if (valgrind_multiplier := config.getoption('--valgrind-multiplier', None)) is not None:
        _scale_timeout_option(config, "timeout", valgrind_multiplier)
        _scale_timeout_option(config, "session_timeout", valgrind_multiplier)
        patches.patch_ssh_engine(valgrind_multiplier)

    # the valgrind build installation can take more time than other builds.
    # a clear indication that this is a valgrind build, is target version contains 'memverif'.
    # which in this case, we need to patch the deploy wait timeout to be the pytest timeout.
    target: str = config.getoption('--target-version', '') or config.getoption('--target_version', '')
    if target and 'memverif' in target.lower():
        if pytest_timeout := config.getoption("--timeout", None):
            if isinstance(pytest_timeout, str):
                if not re.match(r'\d+\.?\d*$', pytest_timeout):
                    logger.warning("Invalid pytest timeout: %s", pytest_timeout)
                    return
                else:
                    pytest_timeout = float(pytest_timeout)

            if not isinstance(pytest_timeout, (int, float)):
                logger.warning("Invalid pytest timeout: %s", pytest_timeout)
                return

            patches.maybe_patch_deploy_memverif_install_wait_timeout(int(pytest_timeout))


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if (multiplier := config.getoption("--valgrind-multiplier", default=None)) is None:
        return

    for item in items:
        if (marker := item.get_closest_marker("timeout")) is None:
            continue

        args = list(marker.args)
        kwargs = dict(marker.kwargs)

        if args:
            args[0] = float(args[0]) * multiplier
        elif "timeout" in kwargs:
            kwargs["timeout"] = float(kwargs["timeout"]) * multiplier
        else:
            continue

        item.add_marker(pytest.mark.timeout(*args, **kwargs), append=False)


@pytest.fixture
def valgrind_config(request: pytest.FixtureRequest):
    """ Valgrind configuration fixture. """
    return vg_analyzer.DecisionConfig(
        definitely_threshold=request.config.getoption('vg_definitely_threshold'),
        indirectly_threshold=request.config.getoption('vg_indirectly_threshold'),
        possibly_threshold=request.config.getoption('vg_possibly_threshold'),
        fail_on_warnings=request.config.getoption('vg_fail_on_warnings'),
        bug_handler_scope=request.config.getoption('vg_bug_handler_scope'),
    )


@pytest.fixture(scope="session")
def vg_ignore_registry(pytestconfig: pytest.Config) -> vg_analyzer.IgnoreRegistry | None:
    """
    Lazy session-scoped ignore registry.

    NOTE: This fixture is intentionally not injected directly into the autouse `valgrind` fixture.
    We request it via `request.getfixturevalue()` only when valgrind analysis actually runs.
    """
    if not pytestconfig.getoption("--valgrind-analyze") or pytestconfig.getoption("vg_no_ignores"):
        return None

    try:
        ignore_dir = pytestconfig.getoption("vg_ignore_dir")
    except Exception:  # noqa: BLE001
        ignore_dir = _DEFAULT_IGNORE_DIR

    strategy = pytestconfig.getoption("vg_trace_id_strategy")
    trace_id_computer = vg_analyzer.TraceIdComputer(strategy=strategy)
    registry = vg_analyzer.IgnoreRegistry(ignore_dir=ignore_dir, trace_id_computer=trace_id_computer)
    setattr(pytestconfig, _VG_IGNORE_REGISTRY_ATTR, registry)
    return registry


@pytest.fixture
def bug_handler_ctx(request: pytest.FixtureRequest, engines: EnginesT, topology_obj):
    """ Valgrind bug handler context fixture. """
    should_skip_bug_handler_action = request.config.getoption("--skip_bug_handler_action")
    bug_handler_params = (request.config.getoption("--bug_handler_params", default=None) or "").strip().lower()
    if bug_handler_params == "only_check":
        bug_handler_mode = "no-action"
    else:
        # Default to "create" unless explicitly forced to only-check.
        # NOTE: `--skip_bug_handler_action` is still supported as a hard skip (skip bug handler integration entirely).
        bug_handler_mode = "create"

    # Keep the legacy flag behavior intact: it disables valgrind bug handler integration entirely.
    if should_skip_bug_handler_action:
        bug_handler_mode = "no-action"
    setup_name = request.config.getoption('--setup_name') or ""
    session_id = request.config.getoption('--session_id') or os.environ.get(InfraConst.ENV_SESSION_ID)
    cli_type = request.session.config.cache.get('CLI_TYPE', os.environ.get('CLI_TYPE'))

    return vg_helpers.ValgrindBugHandlerContext(
        setup_name=setup_name,
        topology_obj=topology_obj,
        engines=engines,
        cli_type=cli_type,
        mode=bug_handler_mode,
        session_id=session_id,
        skip_actions=should_skip_bug_handler_action,
    )


@pytest.fixture(autouse=True)
def valgrind(
    engines: EnginesT,
    request: pytest.FixtureRequest,
    valgrind_config: vg_analyzer.DecisionConfig,
    bug_handler_ctx: vg_helpers.ValgrindBugHandlerContext,
):
    """
    Valgrind fixture.

    This fixture is automatically used for all tests.
    It collects the valgrind output files, diffs them, and analyzes the results.
    """
    valgrind_enabled = request.config.getoption('--valgrind-analyze')
    valgrind_session_scope = request.config.getoption('--valgrind-session-analyze')
    valgrind_marker_disabled = request.node.get_closest_marker('disable_valgrind')
    if valgrind_marker_disabled or valgrind_session_scope or not valgrind_enabled:
        yield
        return

    nodeid = str(request.node.nodeid).split('::')[-1]
    with ValgrindLogFilesSnapshot(nodeid, engines) as vg_snapshot:
        yield  # make the test start

    diff = vg_snapshot.diff

    if not any(diff.values()):
        logger.info('No valgrind diff found')
        return

    tar_path = vg_helpers.zip_valgrind_diff_files(engines.dut, nodeid, list(diff.keys()))

    ignore_registry = None
    if not request.config.getoption("vg_no_ignores"):
        ignore_registry = request.getfixturevalue("vg_ignore_registry")

    vg_helpers.valgrind_analyze(
        diff,
        tar_path,
        valgrind_config=valgrind_config,
        bug_handler_ctx=bug_handler_ctx,
        ignore_registry=ignore_registry,
    )
