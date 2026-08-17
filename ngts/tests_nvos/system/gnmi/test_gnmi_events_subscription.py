"""
Standalone coverage for system-events gnmi subscription resilience across a gnmi-server (nv-gnmi) restart.

Origin: the fatal-mode gnmi investigation (Google FR 4685731). During fatal-mode handling nv-gnmi is
restarted, and it was found that a subscription re-established by the client after the restart is accepted
by the server (counted in received-subscription-requests / total-active-subscriptions) but never served -
no initial sync and no updates (NVBug 6504764). This test reproduces that scenario with a plain nv-gnmi
service restart, independent of the fatal flow, per Nadeem's request to have it covered in the gnmi suite.
"""
import itertools
import logging
import time

import pytest
from retry.api import retry_call

import ngts.tools.test_utils.allure_utils as allure
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.system.gnmi.constants import GnmiMode
from ngts.tests_nvos.system.gnmi.gnmi_events_tools import (
    assert_verification_summary,
    format_gnmi_event,
    get_events_table,
    get_streamed_events,
    run_check,
    subscribe_system_events_stream,
)

logger = logging.getLogger()

# event-db flood-protection (isFloodedEvent) silently drops an event identical to the latest table entry,
# so consecutive '-c 1' publishes ("Test event with index 0") never re-enter the table on a quiet system.
# Publishing with a count that grows on every call guarantees each publish ends with a never-seen index.
EVENTD_PUBLISH_CMD = 'docker exec eventd events_publish_test.py -c {count}'
_publish_seq = itertools.count(1)
GNMI_SERVER_RESTART_CMD = 'sudo systemctl restart nv-gnmi'
GNMI_DOCKER_UP_TRIES = 12
GNMI_DOCKER_UP_DELAY_SECONDS = 5
# gnmic retries a broken subscription every ~10s by default; give it time to reconnect + re-subscribe
CLIENT_RECONNECT_GRACE_SECONDS = 30
EVENT_PUBLISH_SETTLE_SECONDS = 5


@pytest.mark.system
@pytest.mark.gnmi
def test_gnmi_events_subscription_across_server_restart(engines, devices):
    """
    Verify system-events subscription behavior across a gnmi-server (nv-gnmi) restart - the same restart
    that fatal-mode handling performs:
    1. A live subscription receives newly published events (steady-state streaming).
    2. Events published AFTER the restart must reach the pre-restart subscriber (whose client
       auto-reconnects and re-subscribes, as any real collector does). Currently broken: NVBug 6504764 -
       the re-subscription is accepted by the server but never served.
    3. A NEW subscription opened after the restart receives all events in its initial sync.
    All verifications run to completion and the test ends with a PASS/FAIL summary.
    """
    checks = []
    gnmi_client, gnmi_process, gnmi_was_enabled, baseline_event_id = subscribe_system_events_stream(engines, devices)
    try:
        with allure.step('Publish a test event BEFORE the gnmi-server restart'):
            pre_restart_ids = _publish_test_event_and_get_new_ids(engines.dut, baseline_event_id)

        with allure.step('Restart the gnmi-server (nv-gnmi) - the same restart fatal-mode handling performs'):
            engines.dut.run_cmd(GNMI_SERVER_RESTART_CMD, validate=True)
            _wait_gnmi_docker_up(engines.dut)
            logger.info(f'Waiting {CLIENT_RECONNECT_GRACE_SECONDS}s for the subscriber client to auto-reconnect '
                        f'and re-subscribe')
            time.sleep(CLIENT_RECONNECT_GRACE_SECONDS)

        with allure.step('Publish a test event AFTER the gnmi-server restart'):
            post_restart_ids = _publish_test_event_and_get_new_ids(engines.dut, max(pre_restart_ids))
            time.sleep(EVENT_PUBLISH_SETTLE_SECONDS)  # let the event propagate to the stream before closing it

        with allure.step('Close the original subscription and collect its stream'):
            stream_alive = gnmi_process.poll() is None
            logger.info(f'gnmic stream process still alive before close: {stream_alive}')
            gnmi_out, gnmi_err = gnmi_client.close_session_and_get_out_and_err(gnmi_process)
            logger.info(f'gnmi subscription closed; captured {len(gnmi_out)} bytes of stream output')
            allure.attach('gnmic raw stream output', gnmi_out, log=False)
            streamed_events = get_streamed_events(gnmi_out)
            logger.info(f'The original subscriber received {len(streamed_events)} events in total')

        run_check(checks, 'live subscription received the event published BEFORE the restart',
                  _assert_ids_streamed, streamed_events, pre_restart_ids, gnmi_err)

        run_check(checks, 'pre-restart subscription received the event published AFTER the restart '
                          '(client auto-reconnect; NVBug 6504764)',
                  _assert_ids_streamed, streamed_events, post_restart_ids, gnmi_err)

        run_check(checks, 'NEW subscription opened after the restart receives all events (initial sync)',
                  _assert_ids_via_new_subscription, gnmi_client, pre_restart_ids | post_restart_ids)
    finally:
        try:
            if gnmi_process.poll() is None:
                gnmi_client.close_session_and_get_out_and_err(gnmi_process)
        except Exception as e:
            logger.warning(f'Failed closing leftover gnmi subscription: {e}')
        if not gnmi_was_enabled:
            with allure.step('Restore gnmi-server to disabled'):
                try:
                    System().gnmi_server.disable_gnmi_server()
                except Exception as e:
                    logger.warning(f'Failed restoring gnmi-server to disabled: {e}')

    assert_verification_summary(checks)


def _publish_test_event_and_get_new_ids(engine, min_event_id: int) -> set:
    """
    Publishes one test system-event via eventd and returns the new event-id(s) it created
    (verified against the events table, event-id > min_event_id).
    """
    def _publish_and_collect() -> set:
        output = engine.run_cmd(EVENTD_PUBLISH_CMD.format(count=next(_publish_seq)))
        # tolerate benign output (e.g. warnings); the events-table check below is the real success criterion
        assert 'error' not in output.lower(), f'Error publishing test event: {output}'
        time.sleep(EVENT_PUBLISH_SETTLE_SECONDS)
        new_ids = {event_id for event_id in get_events_table() if event_id > min_event_id}
        assert new_ids, f'No new event appeared in the events table after publishing (baseline {min_event_id})'
        return new_ids

    new_ids = retry_call(_publish_and_collect, exceptions=AssertionError, tries=3, delay=5)
    logger.info(f'Published test event(s), new event-ids: {sorted(new_ids)}')
    return new_ids


def _wait_gnmi_docker_up(engine):
    def _check_up():
        output = engine.run_cmd('docker ps --format "{{.Names}} {{.Status}}" | grep nv-gnmi')
        assert 'Up' in output, f'nv-gnmi docker is not up yet: {output}'
        logger.info(f'nv-gnmi docker status: {output.strip()}')

    retry_call(_check_up, exceptions=AssertionError, tries=GNMI_DOCKER_UP_TRIES, delay=GNMI_DOCKER_UP_DELAY_SECONDS)


def _assert_ids_streamed(streamed_events: dict, expected_ids: set, gnmi_err: str):
    """Asserts the given event-ids were delivered on the (already collected) subscription stream."""
    missing = expected_ids - set(streamed_events)
    received = {event_id: streamed_events[event_id] for event_id in expected_ids & set(streamed_events)}
    for event_id, values in sorted(received.items()):
        logger.info(f'Event delivered to the subscriber:\n{format_gnmi_event(event_id, values)}')
    # keep 'was not streamed by the gnmi server' phrasing - it is the Error pattern of NVBug 6504764,
    # so Allure auto-attribution links this failure to the bug
    assert not missing, (
        f'Event(s) {sorted(missing)} was not streamed by the gnmi server to the subscriber '
        f'(received {len(streamed_events)} events in total). gnmic stderr: {gnmi_err or "(empty)"}')


def _assert_ids_via_new_subscription(gnmi_client, expected_ids: set):
    """Opens a fresh ONCE subscription and asserts the given event-ids appear in its initial sync."""
    out, err, _ = gnmi_client.gnmic_subscribe_system_events(mode=GnmiMode.ONCE, skip_cert_verify=True,
                                                            keep_session_alive=False, cmd_time=60)
    events = get_streamed_events(out)
    logger.info(f'New subscriber received {len(events)} events in initial sync')
    missing = expected_ids - set(events)
    assert not missing, (
        f'Event(s) {sorted(missing)} was not streamed by the gnmi server to a NEW subscriber in its initial '
        f'sync ({len(events)} events received). gnmic stderr: {err or "(empty)"}')
    for event_id in sorted(expected_ids):
        logger.info(f'Event delivered to the new subscriber:\n{format_gnmi_event(event_id, events[event_id])}')
