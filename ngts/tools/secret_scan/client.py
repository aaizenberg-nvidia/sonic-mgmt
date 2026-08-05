"""
Secret-scan request clients - a NOS-agnostic base + per-NOS implementations.

A SecretScanClient turns a DUT + regression context into a protocol-compliant
request in the shared store: generate a tech-support dump on the DUT, copy it
into `<base>/requests/<project>/<id>/`, write `request_info.json`, and drop a
`SUBMITTED` marker last (so the server never sees a partial request). The
common flow lives in the base class (template method `submit()`); each NOS
subclass supplies the parts that differ - `generate_support()`,
`read_dut_version()`, and `copy_support_file()`. Pick the right subclass with
`SecretScanClient.create_client_for_topology(topology_obj, engine, ...)`.

The hierarchy follows the CLI frontend, not the NOS name: NVOS and Cumulus
both speak NVUE, so they share `NvueNosClient` (which implements the three
hooks with `nv` commands) and add nothing of their own; SONiC is separate.

See the request/response protocol in the trufflehog repo (docs/protocol.md).
"""
import json
import logging
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any, Dict, Iterable, List, Optional, Tuple, Type, Union

from infra.tools.connection_tools.proxy_ssh_engine import ProxySshEngine
from ngts.nvos_constants.constants_nvos import TopologyConsts
from ngts.tools.secret_scan.consts import SecretScanConsts
from ngts.tools.sysdumps import copy_dump_file
from ngts.tools.test_utils import allure_utils as allure
from ngts.tools.test_utils.nvos_general_utils import get_switch_type

logger = logging.getLogger()

_ARCHIVE_SUFFIXES: Tuple[str, ...] = ('.tar.gz', '.txz', '.tar')


def _sanitize(name: str) -> str:
    """Return a filesystem-safe request-id / directory name."""
    safe = re.sub(r'[^A-Za-z0-9._-]+', '-', str(name)).strip('-.')
    return safe or 'request'


def _checked_project(project: str) -> str:
    """
    Return project unchanged if it's a plain, safe directory name.

    project is just a name (e.g. 'nvos') that becomes a dir under
    <base>/requests/. Only [A-Za-z0-9._-] is allowed (and not '.'/'..'), so it
    stays a single, control-char-free segment that can't escape the store.
    """
    if not re.fullmatch(r'[A-Za-z0-9._-]+', project) or project in ('.', '..'):
        raise ValueError(
            f"invalid project {project!r}: must be a single path segment of "
            f"[A-Za-z0-9._-] (no '/', '.', '..', control chars or absolute)")
    return project


def _as_email_list(notify: Optional[Union[str, Iterable[str]]]) -> List[str]:
    """Normalize notify (None / str / iterable) to a list of strings."""
    if not notify:
        return []
    if isinstance(notify, str):
        notify = notify.split(',')
    return [str(item).strip() for item in notify if str(item).strip()]


class SecretScanClient:
    """
    NOS-agnostic secret-scan request client (abstract).

    Subclasses implement the NOS-specific hooks - `generate_support()`,
    `read_dut_version()`, and `copy_support_file()`. Everything common
    (request_info, markers, dir layout) lives here. Instantiate via
    `create_client_for_topology(...)`.

    Required fields (project / release / session_id / setup_name) must be
    provided by the caller; dut_name and version are optional and read from
    the DUT when not given.
    """

    #: TopologyConsts value for this NOS (set by each subclass).
    NOS: Optional[str] = None

    def __init__(self, engine: ProxySshEngine, *,
                 project: str, release: str, session_id: str, setup_name: str,
                 dut_name: Optional[str] = None, version: Optional[str] = None,
                 notify: Optional[Union[str, Iterable[str]]] = None,
                 base: Optional[str] = None) -> None:
        self.engine: ProxySshEngine = engine
        self.session_id: str = session_id
        self.setup_name: str = setup_name
        self.release: str = release
        self.notify: List[str] = _as_email_list(notify)
        self.base: str = base or SecretScanConsts.BASE
        self.project: str = _checked_project(project)
        # Optional fields - caller override, else read from the DUT once here
        # (not lazily), so the resolved values are fixed at build time.
        self.dut_name: str = dut_name or self._read_dut_hostname()
        self.version: str = version or self.read_dut_version()

    # --- NOS-specific hooks (subclasses must implement) ------------------- #
    def generate_support(self) -> str:
        """Generate a tech-support dump on the DUT; return its DUT path."""
        raise NotImplementedError

    def read_dut_version(self) -> str:
        """Return the DUT software version string (best-effort)."""
        raise NotImplementedError

    def copy_support_file(self, src_remote_path: str,
                          dst_local_path: str) -> None:
        """Copy the generated support file from the DUT to dst_local_path."""
        raise NotImplementedError

    def _read_dut_hostname(self) -> str:
        """Best-effort DUT hostname (falls back to the engine IP)."""
        try:
            out = self.engine.run_cmd('hostname')
            host = out.strip().splitlines()[-1].strip() if out else ''
            if host:
                return host
        except Exception as exc:
            logger.warning(f"could not read DUT hostname: {exc}")
        return getattr(self.engine, 'ip', 'unknown')

    # --- The common flow (template method) ------------------------------- #
    def submit(self) -> str:
        """Generate + submit a protocol-compliant request. Return the dir."""
        with allure.step('Generate tech-support on the DUT'):
            support_file_remote_path: str = self.generate_support()
            support_filename: str = PurePosixPath(
                support_file_remote_path).name
            assert support_filename.endswith(_ARCHIVE_SUFFIXES), (
                f'tech-support generation did not yield an archive '
                f'(nos={self.NOS!r}, path={support_file_remote_path!r})')
            logger.info(
                f"generated {self.NOS} tech-support: "
                f"{support_file_remote_path}")

        with allure.step('Resolve request metadata'):
            request_id, request_info = self._build_request_info(
                support_filename)
            project: str = request_info['project']
            logger.info(
                f"secret-scan request {project}/{request_id} -> "
                f"{json.dumps(request_info)}")

        request_dir: str = os.path.join(self.base,
                                        SecretScanConsts.REQUESTS_DIRNAME,
                                        project, request_id)
        with allure.step(f'Prepare request dir {project}/{request_id}'):
            # Fresh dir per request. A pre-existing dir shouldn't happen
            # (session-unique id), but for completeness override it: wipe the
            # whole dir (clears any stale SUBMITTED) and recreate, so the
            # "marker last" ordering still holds.
            try:
                os.makedirs(request_dir, exist_ok=False)
            except FileExistsError:
                logger.warning(
                    f"request dir already exists, overriding: {request_dir}")
                shutil.rmtree(request_dir)
                os.makedirs(request_dir, exist_ok=False)
            os.chmod(request_dir, 0o777)

        # Payload first (support file + info), SUBMITTED marker LAST: the
        # marker is the readiness signal, so the server must never see it
        # before the request is complete (the protocol guarantees this).
        with allure.step('Copy support file into the request dir'):
            self.copy_support_file(
                support_file_remote_path,
                os.path.join(request_dir, support_filename))

        with allure.step('Write request_info.json'):
            info_path: str = os.path.join(
                request_dir, SecretScanConsts.REQUEST_INFO_FILENAME)
            with open(info_path, 'w') as info_fh:
                json.dump(request_info, info_fh, indent=2)
            os.chmod(info_path, 0o666)
            allure.attach(
                SecretScanConsts.REQUEST_INFO_FILENAME,
                json.dumps(request_info, indent=2),
                allure.AttachmentType.JSON)

        with allure.step('Mark the request SUBMITTED (commit, last)'):
            marker_path: str = os.path.join(
                request_dir, SecretScanConsts.SUBMITTED_MARKER)
            open(marker_path, 'w').close()
            os.chmod(marker_path, 0o666)

        logger.info(f"secret-scan request submitted: {request_dir}")
        return request_dir

    # --- helpers --------------------------------------------------------- #
    def _build_request_info(
            self, support_filename: str) -> Tuple[str, Dict[str, Any]]:
        now = datetime.now(timezone.utc)
        # session_id is unique per run, so setup+session is a stable id.
        request_id: str = _sanitize(f'{self.setup_name}_{self.session_id}')
        return request_id, {
            'id': request_id,
            'submitted_at': now.strftime('%Y-%m-%dT%H:%M:%SZ'),
            'project': self.project,
            # protocol.md: 'version' is the DUT product build/version.
            'version': self.version,
            'support_file': support_filename,
            'release': self.release,
            'regression_platform': f'MARS/{self.setup_name}',
            'regression_id': self.session_id,
            'dut': self.dut_name,
            'notify': self.notify,
            'extra_info': {
                'nos': self.NOS,
                'schema_version': SecretScanConsts.REQUEST_SCHEMA_VERSION,
            },
        }

    # --- factory --------------------------------------------------------- #
    @classmethod
    def create_client_for_topology(
            cls, topology_obj, engine: ProxySshEngine,
            **kwargs) -> "SecretScanClient":
        """Create the SecretScanClient subclass matching the setup's NOS."""
        switch_type: str = get_switch_type(topology_obj)
        client_cls: Optional[Type["SecretScanClient"]] = \
            _NOS_CLIENTS.get(switch_type)
        if client_cls is None:
            raise NotImplementedError(
                f'unsupported NOS type for secret-scan: {switch_type}')
        return client_cls(engine, **kwargs)


class NvueNosClient(SecretScanClient):
    """
    Shared implementation for the NVUE-based NOSes (NVOS + Cumulus).

    Both expose the same `nv` frontend, so the whole flow - generate the
    tech-support, find the produced file, read the version - is identical and
    lives here once. Subclasses only declare which NOS they are; they override
    a hook if (and only if) their `nv` output actually diverges.

    NOS is intentionally left unset: this class is the shared behaviour, not a
    NOS in its own right, so it is not registered in _NOS_CLIENTS.
    """

    def generate_support(self) -> str:
        self.engine.run_cmd('nv action generate system tech-support',
                            validate=True)
        out = self.engine.run_cmd('nv show system tech-support files -o json',
                                  validate=True)
        return json.loads(out)['latest']['path']

    def read_dut_version(self) -> str:
        """
        Return the DUT build id, e.g. 'nvos-dev-otel-release-009' (NVOS) or
        '5.18.0.0045' (Cumulus).

        NVUE reports `image` as a nested object holding build-id + build-date;
        its build-id is the exact build identifier (this is also how ngts reads
        it: System().version.image.show()[...]['build-id']). Verified against
        both NVOS and CL - they return the same schema. Anything else is not
        guessed at: log what we got and fall back to 'unknown', so a schema
        change is visible in the log instead of silently tagging a scan with
        the wrong version.
        """
        try:
            out = self.engine.run_cmd('nv show system version -o json',
                                      validate=True)
            data = json.loads(out)
            image = data.get('image')
            if isinstance(image, dict) and image.get('build-id'):
                return str(image['build-id'])
            logger.warning(
                f"unexpected 'nv show system version' schema on {self.NOS}: "
                f"no image.build-id (top-level keys: {sorted(data)}); "
                f"reporting version as 'unknown'")
        except Exception as exc:
            logger.warning(f"could not read {self.NOS} version: {exc}")
        return 'unknown'

    def copy_support_file(self, src_remote_path: str,
                          dst_local_path: str) -> None:
        copy_dump_file(self.engine, source_file=src_remote_path,
                       dest_file=dst_local_path)


class NvosClient(NvueNosClient):
    # Plain NVUE flow - verified end-to-end on NVOS (MARS + local).
    NOS = TopologyConsts.NVOS


class CumulusClient(NvueNosClient):
    # Cumulus is NVUE too, so it inherits the same flow unchanged. If its `nv`
    # output turns out to differ (tech-support file layout, version keys),
    # override just that hook here rather than forking the flow.
    NOS = TopologyConsts.CL


class SonicClient(SecretScanClient):
    # Seam for SONiC - not NVUE, so it does not share the flow above and is
    # not wired yet. When implementing the hooks, reuse ngts.tools.sysdumps
    # (generate_dump retry / cleanup / path parsing). Until then the inherited
    # hooks raise NotImplementedError.
    NOS = TopologyConsts.SONIC


_NOS_CLIENTS: Dict[str, Type[SecretScanClient]] = {
    TopologyConsts.NVOS: NvosClient,
    TopologyConsts.SONIC: SonicClient,
    TopologyConsts.CL: CumulusClient,
}
