"""Shared steps and helpers for the IMA test suite."""
import base64
import binascii
import json
import logging
import re
import time
from typing import Iterable, Tuple

from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from ngts.nvos_constants.constants_nvos import ConfState
from ngts.nvos_tools.infra.DutUtilsTool import RebootParams
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.SendCommandTool import SendCommandTool
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.general.security.ima.constants import ImaConsts, ImaBootTiming
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# SCP url builder                                                             #
# --------------------------------------------------------------------------- #
def get_scp_url(remote_engine, filename: str) -> str:
    """
    Build the SCP url used to upload an IMA artifact to the remote host.

    One generic builder (mirroring the TPM suite's ``get_scp_url``) for every
    uploaded artifact - the measurement log, the quote and the TPM event log -
    which differ only by ``filename``.

    :param remote_engine: Engine bound to the SCP destination host.
    :param filename: Destination file name under :data:`ImaConsts.REMOTE_PATH`,
        e.g. :data:`ImaConsts.MEASUREMENTS_REMOTE_FILENAME`.
    """
    return ImaConsts.REMOTE_SCP_URL.format(user=remote_engine.username, password=remote_engine.password,
                                           ip=remote_engine.ip, filename=filename)


# --------------------------------------------------------------------------- #
# Uploaded-file checks (assert the generated/uploaded artifacts are usable)    #
# --------------------------------------------------------------------------- #
def remote_file_size(remote_engine, remote_filename: str) -> int:
    """
    Return the byte size of an uploaded file on the remote host (0 if missing).

    :param remote_engine: Engine bound to the SCP destination host.
    :param remote_filename: File name under :data:`ImaConsts.REMOTE_PATH`.
    """
    out = remote_engine.run_cmd(f'wc -c < {ImaConsts.REMOTE_PATH}/{remote_filename} 2>/dev/null')
    try:
        return int(out.strip().split()[-1])
    except (ValueError, IndexError):
        return 0


def verify_uploaded_file_not_empty(remote_engine, remote_filename: str):
    """Assert that an uploaded artifact exists on the remote host and is not empty."""
    with allure.step(f'verify uploaded file "{remote_filename}" is present and not empty'):
        size = remote_file_size(remote_engine, remote_filename)
        assert size > 0, \
            f'uploaded file {ImaConsts.REMOTE_PATH}/{remote_filename} is missing or empty (size={size})'


def verify_measurement_upload_rejected_when_disabled(remote_engine, ima=None):
    """Assert that NVUE refuses IMA measurement-log upload while IMA is disabled."""
    with allure.step('upload measurements while IMA is off fails with "IMA is not enabled"'):
        scp_url = get_scp_url(remote_engine, ImaConsts.MEASUREMENTS_REMOTE_FILENAME)
        (ima or System().security.ima).action_upload_measurements(
            scp_url, expected_str=ImaConsts.IMA_NOT_ENABLED_ERROR).verify_result()


def generate_attestation_quote(tpm=None):
    """Generate a quote over the boot PCRs + PCR 9 (initrd) + PCR 10 (IMA)."""
    with allure.step('generate quote over boot PCRs + PCR 9 (initrd) + PCR 10 (IMA)'):
        (tpm or System().security.tpm).action_generate_quote(
            ImaConsts.ATTEST_PCRS, ImaConsts.VALID_NONCE).verify_result()


def upload_attestation_artifacts(remote_engine, ima=None, tpm=None):
    """Upload the IMA measurement log, TPM event log and quote to the remote host."""
    measurements_url = get_scp_url(remote_engine, ImaConsts.MEASUREMENTS_REMOTE_FILENAME)
    quote_url = get_scp_url(remote_engine, ImaConsts.QUOTE_REMOTE_FILENAME)
    event_log_url = get_scp_url(remote_engine, ImaConsts.EVENT_LOG_REMOTE_FILENAME)
    ima = ima or System().security.ima
    tpm = tpm or System().security.tpm

    with allure.step('upload IMA measurement log'):
        ima.action_upload_measurements(measurements_url).verify_result()
    with allure.step('upload TPM event log'):
        tpm.action_upload(ImaConsts.TPM_EVENT_LOG_FILENAME, event_log_url).verify_result()
    with allure.step('upload attestation quote'):
        tpm.action_upload(ImaConsts.QUOTE_REMOTE_FILENAME, quote_url).verify_result()


def quote_contains_pcrs(quote_text: str, pcr_indices: Iterable) -> bool:
    """
    Return True if the quote file references every PCR index in ``pcr_indices``.

    The quote file (``quotes.json``) embeds the PCR selection/values. The exact
    JSON shape is not contracted here, so this is tolerant: it first tries to read
    PCR indices from a parsed JSON ``pcrs`` section, then falls back to matching
    each index as a JSON key (``"9":``) or a ``pcr 9`` / ``PCR9`` token.

    :param quote_text: Raw content of the generated quote file.
    :param pcr_indices: PCR indices that must all be present (e.g. ``('9', '10')``).
    """
    indices = [str(i) for i in pcr_indices]

    # NVOS emits the quote as JSON with base64 blobs ('quote' = TPMS_ATTEST,
    # 'PCR_bank' = marshaled TPML_PCR_SELECTION). The PCR indices are encoded in
    # the selection bitmap, not as plaintext - decode them first.
    selected = _pcr_indices_from_quote_binary(quote_text)
    if selected:
        return all(i in selected for i in indices)

    found = _pcr_indices_from_json(quote_text)
    if found:
        return all(i in found for i in indices)

    return all(_quote_references_pcr(quote_text, i) for i in indices)


def _pcr_indices_from_quote_binary(quote_text: str) -> set:
    """
    Decode the PCR indices selected in an NVOS TPM quote's base64 blobs.

    The quote is emitted as JSON-*like* text, but it is not strictly valid JSON:
    the embedded ``IAK`` PEM certificate contains literal newlines inside the
    string, so ``json.loads`` rejects it. We therefore pull the base64 fields out
    with a regex (its value is pure base64, newline-free) instead of parsing the
    whole document. ``PCR_bank`` is the marshaled ``TPML_PCR_SELECTION``; its
    selection bitmap yields the PCR indices (e.g. {'0','4','7','9','10'}).

    Returns an empty set if the ``PCR_bank`` selection blob is missing or cannot be decoded.
    """
    # Only PCR_bank is a marshaled TPML_PCR_SELECTION, so it is the single field we
    # decode for PCR indices. The 'quote' field is a TPMS_ATTEST structure with a
    # different layout - feeding it to the selection parser would yield arbitrary
    # indices - so it is intentionally NOT parsed here. When PCR_bank is absent or
    # cannot be decoded we return nothing and let the caller fall back to the JSON / text
    # matching helpers. Capture the whole quoted value (rather than a base64-only
    # character class) and strip any whitespace before decoding, so a line-wrapped /
    # newline-padded PCR_bank value is still decoded instead of silently skipped.
    match = re.search(r'"PCR_bank"\s*:\s*"([^"]+)"', quote_text)
    if not match:
        return set()
    b64_value = re.sub(r'\s+', '', match.group(1))
    try:
        raw = base64.b64decode(b64_value, validate=False)
    except (binascii.Error, ValueError):
        return set()
    return _pcrs_from_tpml_selection(raw)


def _pcrs_from_tpml_selection(raw: bytes) -> set:
    """
    Parse a marshaled TPML_PCR_SELECTION and return the selected PCR indices.

    Layout: count (UINT32) then, per bank, alg id (2 bytes), sizeofSelect (1 byte)
    and the selection bitmap (sizeofSelect bytes, LSB-first: bit b of byte n is
    PCR n*8 + b). Bounds are validated so a non-selection blob just yields nothing.
    """
    pcrs = set()
    if len(raw) < 4:
        return pcrs
    count = int.from_bytes(raw[0:4], 'little')
    if not 0 < count <= 16:  # sanity guard against mis-decoding arbitrary bytes
        return pcrs
    offset = 4
    for _ in range(count):
        if offset + 3 > len(raw):
            break
        offset += 2  # skip the hash-alg id (value not needed to read the bitmap)
        size = raw[offset]
        offset += 1
        if not 0 < size <= 8 or offset + size > len(raw):
            break
        for byte_index, byte in enumerate(raw[offset:offset + size]):
            for bit in range(8):
                if byte >> bit & 1:
                    pcrs.add(str(byte_index * 8 + bit))
        offset += size
    return pcrs


def _pcr_indices_from_json(quote_text: str) -> set:
    """Collect PCR index strings from any ``pcr``-named mapping in the JSON, if parseable."""
    try:
        data = json.loads(quote_text)
    except (ValueError, TypeError):
        return set()

    found = set()

    def walk(node, under_pcrs):
        if isinstance(node, dict):
            for key, value in node.items():
                key_is_pcr_section = 'pcr' in str(key).lower()
                if under_pcrs and str(key).isdigit():
                    found.add(str(key))
                walk(value, under_pcrs or key_is_pcr_section)
        elif isinstance(node, list):
            for item in node:
                if under_pcrs and isinstance(item, (str, int)) and str(item).isdigit():
                    found.add(str(item))
                walk(item, under_pcrs)

    walk(data, False)
    return found


def _quote_references_pcr(quote_text: str, index: str) -> bool:
    """Tolerant text match for a single PCR index in the quote file."""
    patterns = [rf'"{re.escape(index)}"\s*:',     # JSON key, e.g. "9":
                r'\b' + rf'pcr[_\s]*{re.escape(index)}\b']  # pcr9 / pcr 9 / pcr_9 / PCR 9
    return any(re.search(pattern, quote_text, re.IGNORECASE) for pattern in patterns)


# --------------------------------------------------------------------------- #
# State helpers (centralize the show/set/unset wiring so tests stay readable)  #
# --------------------------------------------------------------------------- #
def get_ima_state(dut_engine=None) -> str:
    """Return the current IMA ``state`` value as reported by ``nv show``."""
    with allure.step('read IMA state'):
        output = System().security.ima.show(dut_engine=dut_engine)
        parsed = OutputParsingTool.parse_json_str_to_dictionary(output).get_returned_value()
        return parsed.get(ImaConsts.STATE, '')


def verify_ima_state(expected_state: str, dut_engine=None):
    """
    Assert that ``nv show system security ima`` reports the expected state.

    ``nv show`` reflects the *operational* (running) value, so use this only when
    operational and applied are expected to match (e.g. after a reboot). While a
    state change is still pending a reboot, assert the configured value with
    :func:`verify_ima_applied_state`.
    """
    with allure.step(f'verify IMA state is "{expected_state}"'):
        output = System().security.ima.show(dut_engine=dut_engine)
        parsed = OutputParsingTool.parse_json_str_to_dictionary(output).get_returned_value()
        ValidationTool.verify_field_value_in_output(parsed, ImaConsts.STATE, expected_state).verify_result()


def get_ima_applied_state(dut_engine=None) -> str:
    """
    Return the *applied* (configured) IMA state from the applied config revision.

    For the reboot-required IMA setting ``nv show system security ima -o json``
    reports only the *operational* (running) value - it keeps the pre-reboot value
    and never exposes an operational/applied split. The applied value therefore has
    to be read from the applied configuration revision via
    ``nv config show --rev applied``, whose JSON is a list of sections; the
    configured state lives at ``set -> system -> security -> ima -> state`` and is
    absent while IMA is left at its default.
    """
    engine = dut_engine or TestToolkit.get_engine()
    with allure.step('read applied IMA state from the applied config revision'):
        output = NvueGeneralCli.show_config(engine, revision=ConfState.APPLIED)
        config = OutputParsingTool.parse_json_str_to_dictionary(output).get_returned_value()
        return _extract_ima_state_from_config(config)


def _extract_ima_state_from_config(config) -> str:
    """
    Pull ``system/security/ima/state`` out of an ``nv config show`` JSON document.

    ``nv config show`` returns a list of sections (an optional ``header`` plus a
    ``set`` tree). Returns :data:`ImaConsts.DEFAULT_STATE` when IMA is not present
    in the configuration, i.e. it is left at its default.
    """
    sections = config if isinstance(config, list) else [config]
    for section in sections:
        if not isinstance(section, dict):
            continue
        ima = (section.get('set', {}).get('system', {})
               .get('security', {}).get('ima', {}))
        if isinstance(ima, dict) and ImaConsts.STATE in ima:
            return ima[ImaConsts.STATE]
    return ImaConsts.DEFAULT_STATE


def get_ima_states(dut_engine=None) -> Tuple[str, str]:
    """
    Return the IMA state as an ``(operational, applied)`` tuple.

    ``operational`` is the running value reported by ``nv show system security ima``
    (for the reboot-required IMA setting this stays at the pre-reboot value until a
    reboot); ``applied`` is the configured value taken from the applied config
    revision. They are read from different sources on purpose: ``nv show -o json``
    only ever returns the operational value for IMA and never an
    operational/applied split, so the applied value cannot be derived from it.
    """
    with allure.step('read IMA operational/applied state'):
        operational = get_ima_state(dut_engine=dut_engine)
        applied = get_ima_applied_state(dut_engine=dut_engine)
        return operational, applied


def verify_ima_applied_state(expected_applied: str, dut_engine=None):
    """
    Assert the *applied* (configured) IMA state, ignoring the operational value.

    ``set`` / ``unset`` + ``apply`` change the applied state immediately, while the
    operational state only follows after a reboot. No-reboot configuration tests
    should therefore assert the applied value rather than the operational one.
    """
    with allure.step(f'verify IMA applied state is "{expected_applied}"'):
        _, applied = get_ima_states(dut_engine=dut_engine)
        assert applied == expected_applied, \
            f'unexpected applied IMA state: expected "{expected_applied}", got "{applied}"'


def verify_ima_pending_state(expected_operational: str, expected_applied: str, dut_engine=None):
    """
    Assert the pre-reboot operational/applied split.

    An IMA state change only takes effect after a reboot, so right after
    ``set`` + ``apply`` (before reboot) ``nv show`` reports the running value as
    ``operational`` and the new value as ``applied``.
    """
    with allure.step(f'verify IMA pending state operational="{expected_operational}", '
                     f'applied="{expected_applied}"'):
        operational, applied = get_ima_states(dut_engine=dut_engine)
        assert operational == expected_operational and applied == expected_applied, \
            (f'unexpected IMA pending state - expected operational="{expected_operational}", '
             f'applied="{expected_applied}"; got operational="{operational}", applied="{applied}"')


def set_ima_state(state: str, apply: bool = True):
    """
    Set IMA ``state`` to the given value and (by default) apply the config.

    Persisting across a reboot additionally requires :func:`save_config`.

    :param state: ``enabled`` or ``disabled``.
    :param apply: run ``nv config apply`` after the set.
    """
    with allure.step(f'set IMA state to "{state}"'):
        System().security.ima.set(ImaConsts.STATE, state, apply=apply).verify_result()


def save_config(engines):
    """
    Persist the running configuration so it survives a reboot.

    ``nv config save`` is rejected ("Cannot save configuration while a job operation is
    in progress") when a config job - e.g. a just-issued ``apply`` - has not finished
    yet. We retry until the job clears, then assert the save actually succeeded: a
    silently-dropped save would leave the config unsaved, and the (no-flag) reboot would
    then stop on an interactive "configuration modified, but not saved" prompt and abort.
    """
    with allure.step('save configuration'):
        output = ''
        for attempt in range(1, ImaConsts.SAVE_RETRY_TRIES + 1):
            output = NvueGeneralCli.save_config(engines.dut)
            if ImaConsts.SAVE_JOB_IN_PROGRESS_MARKER not in output:
                break
            logger.info('nv config save deferred (job operation in progress); retry %d/%d',
                        attempt, ImaConsts.SAVE_RETRY_TRIES)
            time.sleep(ImaConsts.SAVE_RETRY_DELAY_SEC)
        # Fail loudly if the job never cleared: otherwise the config is left unsaved and
        # the following (no-flag) reboot aborts on the "modified but not saved" prompt.
        # verify_no_error_message only catches generic error keywords, so the
        # job-in-progress message could slip through as a false success without this check.
        assert ImaConsts.SAVE_JOB_IN_PROGRESS_MARKER not in output, \
            (f'nv config save still blocked by an in-progress job after '
             f'{ImaConsts.SAVE_RETRY_TRIES} retries; last output:\n{output}')
        SendCommandTool.verify_no_error_message(output).verify_result()


def unset_ima_state(apply: bool = True):
    """Revert IMA ``state`` to its default (disabled)."""
    with allure.step('unset IMA state (revert to default)'):
        System().security.ima.unset(ImaConsts.STATE, apply=apply).verify_result()


# --------------------------------------------------------------------------- #
# Cold reboot                                                                 #
# --------------------------------------------------------------------------- #
def build_reboot_params(devices, topology_obj=None) -> RebootParams:
    """
    Build the cold-reboot parameters shared by every IMA reboot.

    An IMA-on boot hashes every file at boot and is much slower than a normal
    boot, so the readiness wait is bounded by the device's expected reboot
    duration (``expected_operation_durations[reboot_type]``) instead of the
    framework's fixed default - matching the factory-reset/cluster-reboot tests.
    Centralized here so every IMA cold reboot (measurement, reboot-timing and
    upgrade tests) waits with the same IMA-tolerant budget.

    :param devices: The ``devices`` fixture (provides ``dut`` timing metadata).
    :param topology_obj: Optional topology object forwarded to the reboot flow.
    """
    return RebootParams(
        topology_obj=topology_obj,
        system_is_ready_timeout=devices.dut.expected_operation_durations.get(devices.dut.reboot_type))


# --------------------------------------------------------------------------- #
# Boot timing                                                                 #
# --------------------------------------------------------------------------- #
def expected_boot_time(ima_enabled: bool) -> float:
    """
    Return the expected cold-boot duration (seconds) for the given IMA state.

    Only cold reboot is exercised; the HLD ranges live in :class:`ImaBootTiming`.
    """
    return ImaBootTiming.ON_EXPECTED_SEC if ima_enabled else ImaBootTiming.OFF_EXPECTED_SEC


def check_boot_time(duration_sec: float, expected_sec: float, label: str):
    """
    Log a measured boot duration and assert it is within the expected upper bound.

    Boot time varies per platform, so we only flag a clear regression above
    ``expected_sec * ImaBootTiming.TOLERANCE_RATIO`` (the HLD ranges are the
    reference point, see test-plan section 8).
    """
    upper_bound = expected_sec * ImaBootTiming.TOLERANCE_RATIO
    message = (f'{label} boot took {duration_sec:.1f}s '
               f'(expected ~{expected_sec:.0f}s, upper bound {upper_bound:.0f}s)')
    logger.info(message)
    with allure.step(message):
        assert duration_sec <= upper_bound, \
            f'{label} boot time {duration_sec:.1f}s exceeds upper bound {upper_bound:.0f}s'
