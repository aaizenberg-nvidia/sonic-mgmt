"""
Shared tooling for verifying NVOS system-events over gnmi, plus a small PASS/FAIL verification framework.

Origin: the fatal-mode gnmi investigation (Google FR 4685731 / NVBug 6504764). Used by
tests_nvos/multi_asic/test_fatal_mode.py and tests_nvos/system/gnmi/test_gnmi_events_subscription.py.
"""
import json
import logging
import re
import time
from datetime import datetime, timezone

from retry.api import retry_call

from ngts.constants.constants import GnmiConsts
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.system.gnmi.GnmiClient import GnmiClient
from ngts.tests_nvos.system.gnmi.constants import GnmiMode
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger()

GNMI_EVENT_ID_REGEX = r'event-id=(\d+)'
GNMI_EVENT_LEAF_REGEX = r'state/[\w-]+$'


#  GENERIC VERIFICATION FRAMEWORK
################################################################################


def run_check(checks: list, name: str, func, *args, **kwargs):
    """Runs one verification and records PASS/FAIL in `checks` instead of failing the test immediately."""
    try:
        # the try wraps the allure step so a failed verification shows as a FAILED step in the report/log
        with allure.step(f'Verification: {name}'):
            func(*args, **kwargs)
        checks.append((name, True, ''))
        logger.info(f'VERIFICATION PASSED: {name}')
    except Exception as e:
        checks.append((name, False, f'{type(e).__name__}: {e}'))
        logger.exception(f'VERIFICATION FAILED: {name}')


def assert_verification_summary(checks: list):
    """Logs a PASS/FAIL summary of all verifications and fails the test if any of them failed."""
    with allure.step('Verification summary'):
        summary = ('VERIFICATION SUMMARY\n' + '=' * 100 + '\n' +
                   '\n'.join(f'{"PASS" if ok else "FAIL"}  {name}' for name, ok, _ in checks) +
                   '\n' + '=' * 100)
        logger.info('\n' + summary)
        allure.attach('verification summary', summary, log=False)
        failed = {name: details for name, ok, details in checks if not ok}
        assert not failed, (f'{len(failed)}/{len(checks)} verifications failed:\n' +
                            '\n'.join(f'  [{name}]: {details}' for name, details in failed.items()))


#  EVENTS TABLE / GNMI SUBSCRIPTION HELPERS
################################################################################


def get_events_table() -> dict:
    """
    Returns the full events table as {event_id (int): event_dict}. Handles both schemas of 'nv show system
    events': entries at the top level (25.03+), or nested under 'last' (older builds).
    """
    events_dict = OutputParsingTool.parse_json_str_to_dictionary(System().events.show()).get_returned_value()
    if isinstance(events_dict.get('last'), dict):
        events_dict = events_dict['last']
    return {int(event_id): event for event_id, event in events_dict.items()
            if str(event_id).isdigit() and isinstance(event, dict)}


def subscribe_system_events_stream(engines, devices):
    """
    Ensures gnmi-server is enabled, then opens a gnmic STREAM subscription to system-events.
    Returns (client, subscription process, whether gnmi-server was already enabled, latest event-id in the events
    table before the subscription). The baseline event-id lets the caller distinguish events raised during the test
    from old table entries replayed in the subscription's initial sync.
    """
    with allure.step('Make sure gnmi-server is enabled'):
        gnmi_server = System().gnmi_server
        gnmi_show = gnmi_server.parsed_show_gnmi()
        logger.info(f'gnmi-server info: {gnmi_show}')  # gnmi-server version identifies the NVOS image family
        gnmi_was_enabled = gnmi_show[GnmiConsts.GNMI_STATE_FIELD] == GnmiConsts.GNMI_STATE_ENABLED
        if not gnmi_was_enabled:
            gnmi_server.enable_gnmi_server()

    try:
        with allure.step('Capture the latest event-id before subscribing'):
            baseline_event_id = max(get_events_table(), default=0)
            logger.info(f'{baseline_event_id=}')

        with allure.step('Open gnmi stream subscription to system-events'):
            # verify_tools_installed=False: it would also install grpcurl (needs sudo, unavailable on some players).
            # This flow only needs gnmic, so verify only it.
            # print_outputs=False: the stream output includes an initial sync of the whole events table (up to 1000
            # events, >1MB of JSON) which floods the log; callers log a readable summary and allure-attach the raw.
            gnmi_client = GnmiClient(engines.dut.ip, GnmiConsts.GNMI_DEFAULT_PORT, devices.dut.default_username,
                                     devices.dut.default_password, print_outputs=False)
            gnmi_client.cmd_runner.spool_output = True  # the stream is read only once it is closed
            gnmi_client.verify_gnmic_installation()

            def _open_stream():
                _, _, process = gnmi_client.gnmic_subscribe_system_events(mode=GnmiMode.STREAM, skip_cert_verify=True,
                                                                          keep_session_alive=True)
                time.sleep(3)  # give gnmic a moment to fail fast if the server is not serving yet
                if process.poll() is not None:
                    out, err = gnmi_client.close_session_and_get_out_and_err(process)
                    raise AssertionError(f'gnmi subscribe process exited immediately.\nout: {out}\nerr: {err}')
                return process

            gnmi_process = retry_call(_open_stream, exceptions=AssertionError, tries=6, delay=10)
            logger.info(f'gnmi subscriber is connected to {engines.dut.ip}:{GnmiConsts.GNMI_DEFAULT_PORT} and '
                        f'streaming "system-events" (any event with event-id > {baseline_event_id} is new)')
        return gnmi_client, gnmi_process, gnmi_was_enabled, baseline_event_id
    except Exception:
        # the callers' finally blocks never engage when the subscribe itself fails - roll back here
        if not gnmi_was_enabled:
            with allure.step('Subscription setup failed - restore gnmi-server to disabled'):
                try:
                    gnmi_server.disable_gnmi_server()
                except Exception as e:
                    logger.warning(f'Failed restoring gnmi-server to disabled: {e}')
        raise


def get_streamed_events(gnmi_out: str) -> dict:
    """
    Parses gnmic stream output (a sequence of pretty-printed JSON notifications) into a dict of
    event-id -> merged leaf values, e.g. {42: {'state/text': ..., 'state/severity': ...}}.
    Handles both subscription shapes:
    - prefix 'system-events': event-id is in each update's Path
      ("system-event[event-id=42]/state/text") and values are keyed "system-event/state/text"
    - prefix 'system-events/system-event[event-id=42]': event-id is in the notification's prefix
      and values are keyed "state/text"
    Leaf keys are normalized to "state/<leaf>". Leaves of the same event may arrive in separate
    updates/notifications, so merge them by event-id.
    """
    decoder = json.JSONDecoder()
    events = {}
    i = gnmi_out.find('{')
    while i != -1:
        try:
            notification, consumed = decoder.raw_decode(gnmi_out[i:])
        except json.JSONDecodeError:
            i = gnmi_out.find('{', i + 1)
            continue
        if isinstance(notification, dict):
            prefix_match = re.search(GNMI_EVENT_ID_REGEX, notification.get('prefix', ''))
            # normally a notification holds an 'updates' list; but gnmic may print the whole initial sync as one
            # giant notification, and if its head is lost (e.g. truncated capture) we decode the bare update
            # objects ({'Path': ..., 'values': ...}) individually - accept those too
            updates = notification.get('updates', [notification] if 'values' in notification else [])
            for update in updates:
                update_path = update.get('Path', update.get('path', ''))
                event_id_match = re.search(GNMI_EVENT_ID_REGEX, update_path) or prefix_match
                if not event_id_match:
                    continue
                values = events.setdefault(int(event_id_match.group(1)), {})
                for key, value in update.get('values', {}).items():
                    leaf_match = re.search(GNMI_EVENT_LEAF_REGEX, key)
                    values[leaf_match.group(0) if leaf_match else key] = value
        i = gnmi_out.find('{', i + consumed)
    return events


def format_gnmi_event(event_id: int, values: dict) -> str:
    """Renders one streamed system-event exactly as the gnmi subscriber sees it (its 'state/*' leaves)."""
    time_created = values.get('state/time-created')
    if isinstance(time_created, int):  # streamed as epoch nanoseconds
        try:
            time_created = (datetime.fromtimestamp(time_created / 10 ** 9, tz=timezone.utc)
                            .strftime('%Y-%m-%d %H:%M:%S UTC'))
        except (ValueError, OSError, OverflowError):
            pass
    return (f"    event-id     : {event_id}\n"
            f"    severity     : {values.get('state/severity')}\n"
            f"    resource     : {values.get('state/resource')}\n"
            f"    type-id      : {values.get('state/type-id')}\n"
            f"    text         : {values.get('state/text')}\n"
            f"    time-created : {time_created}")
