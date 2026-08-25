"""
gNMI subscription heartbeat / suppress-redundant coverage.

Every test here asserts continuity *per leaf* rather than in aggregate. That distinction is the
whole point, and it is the gap Redmine 5230671 item 1 describes: the existing continuity checks
are aggregate-over-window, so a leaf that goes silent after its initial sync passes today. The
tolerance (1.5x the heartbeat) comes from that item.

Related customer report: NVBug 6635565.

Covered forms of a heartbeat (gNMI spec sections 3.5.1.5.1 and 3.5.1.5.2):
  - SAMPLE + suppress-redundant + heartbeat, swept over HEARTBEAT_INTERVALS: unchanged leaf must
    be resent once per heartbeat, at every interval the sweep covers.
  - SAMPLE + suppress-redundant, no heartbeat: unchanged leaf must stay silent.
  - SAMPLE + heartbeat, no suppress-redundant: heartbeat is a no-op, every sample is delivered.
  - ON_CHANGE + heartbeat: resend once per heartbeat regardless of change.
  - ON_CHANGE, no heartbeat: silent while static, update on change.
  - TARGET_DEFINED + suppress-redundant + heartbeat: server picks the mode, heartbeat still holds.
  - updates-only + heartbeat: no initial sync, leaves arrive on the heartbeat.
  - Scale: wildcard over every interface, the whole phy-diag/state subtree on one interface and
    then on every interface at once, and that subtree running alongside nine distinct light
    subscriptions (Redmine 5230671 items 3, 4 and 7).
  - Perturbation: a link flap mid-window must not silence any leaf.
"""

import pytest

from ngts.constants.constants import GnmiConsts
from ngts.nvos_constants.constants_nvos import NvosConst
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import NvosConsts
from ngts.nvos_tools.infra.Tools import Tools
from ngts.tests_nvos.constants import MINUTE
from ngts.tests_nvos.system.gnmi.GnmiClient import GnmiClient
from ngts.tests_nvos.system.gnmi.constants import (
    HEARTBEAT_CLIENT_CMD_TIME_SEC, HEARTBEAT_CUSTOMER_SCALE_LEAF_COUNT, HEARTBEAT_INTERVAL,
    HEARTBEAT_INTERVAL_SHORT, HEARTBEAT_INTERVALS, HEARTBEAT_LARGE_LEAF_COUNT_MIN,
    HEARTBEAT_LIGHT_INTERFACE_LEAF, HEARTBEAT_MIXED_LIGHT_SUBSCRIBERS,
    HEARTBEAT_PACED_SHARE_MIN, HEARTBEAT_PERTURBATION_AT_SEC, HEARTBEAT_SAMPLE_INTERVAL,
    HEARTBEAT_STATIC_INTERFACE_LEAVES, HEARTBEAT_TOLERANCE_FACTOR, MAX_GNMI_SUBSCRIBERS,
    NANOSEC_PER_SEC, RAW_STREAM_EXCERPT_CHARS, GnmiPaths, GnmiStreamMode)
from ngts.tests_nvos.system.gnmi.helpers import (
    StreamSubscription, SubscriberResult, analyze_leaf_continuity,
    attach_plain_summary, build_leaf_timeline, get_gnmic_engine, gnmi_once_notifications,
    gnmi_path_is_exposed, gnmi_service_refused_reason, heartbeat_window_sec, is_gnmi_unavailable,
    is_path_not_modelled, max_heartbeat_deliveries, parse_duration_sec, run_stream_subscribers,
    timeline_end_ns, verify_heartbeat_drove_delivery, verify_leaf_heartbeat_continuity,
    verify_leaves_not_resent, verify_subscribers_streamed)
from ngts.tools.test_utils import allure_utils as allure


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope='module')
def heartbeat_client(engines, devices) -> GnmiClient:
    """
    gNMI client shared by this module, skipping the whole module if the server is unreachable.

    gnmic runs on the sonic-mgmt engine when the topology defines one, falling back to the local
    test player otherwise. Built with ``verify_tools_installed=False`` (gnmi guideline 1); gnmic is
    expected to be present on whichever of the two runs it.
    """
    client = GnmiClient(engines.dut.ip, GnmiConsts.GNMI_DEFAULT_PORT,
                        devices.dut.default_username, devices.dut.default_password,
                        cmd_time=HEARTBEAT_CLIENT_CMD_TIME_SEC, verify_tools_installed=False,
                        engine=get_gnmic_engine(engines))
    _, err = client.gnmic_capabilities(skip_cert_verify=True)
    if is_gnmi_unavailable(err):
        pytest.skip(f"gNMI server is unavailable on {engines.dut.ip}: {err.strip()}")
    return client


@pytest.fixture(scope='module')
def subscribed_port(heartbeat_client):
    """A link-up Port to subscribe on (heartbeat_client gates gNMI reachability first)."""
    return _select_link_up_port('subscribe on')


@pytest.fixture(scope='module')
def subscribed_port_name(subscribed_port) -> str:
    """Name of the port every single-port test in this module subscribes to."""
    return subscribed_port.name


@pytest.fixture
def flappable_port(heartbeat_client):
    """A link-up Port that a test may admin-down mid-window; restored to up on teardown."""
    port = _select_link_up_port('flap')
    try:
        yield port
    finally:
        # Deliberately unguarded: the port is admin-down by the time this runs, so a swallowed
        # restore failure would hand every later test in the session a down port.
        with allure.step(f"Restore link state up on {port.name}"):
            _set_port_link_state(port, NvosConsts.LINK_STATE_UP)


# ---------------------------------------------------------------------------
# Module-local glue
# ---------------------------------------------------------------------------


def _select_link_up_port(purpose: str):
    """A random link-up Port, failing the test when the DUT has none."""
    port_result = Tools.RandomizationTool.select_random_port(
        requested_ports_state=NvosConsts.LINK_STATE_UP)
    if not port_result.result:
        port_result.ignore_result()
        # Fails rather than skips: every case in this module needs a port, so a skip here would
        # green the whole suite on a switch with no live links and hide a heartbeat regression.
        pytest.fail(f"WRONG LAB SETUP: no link-up port available to {purpose} on this DUT, so "
                    f"none of the heartbeat coverage in this module can run: {port_result.info}")
    return port_result.get_returned_value()


def _set_port_link_state(port, state) -> None:
    """``nv set interface <port> link state <state>`` + apply."""
    port.interface.link.state.set(op_param_name=state, apply=True,
                                  ask_for_confirmation=True).verify_result()


def _static_leaf_paths(port_name: str) -> list:
    """Absolute paths of the leaves that stay constant on an idle link, for one port."""
    return [GnmiPaths.INTERFACE_LEAF.format(name=port_name, leaf=leaf)
            for leaf in HEARTBEAT_STATIC_INTERFACE_LEAVES]


def _static_wildcard_leaf_paths() -> list:
    """Same static leaves, subscribed across every interface via ``[name=*]``."""
    return [GnmiPaths.INTERFACE_WILDCARD_LEAF.format(leaf=leaf)
            for leaf in HEARTBEAT_STATIC_INTERFACE_LEAVES]


def _phy_diag_subtree_path(port_name: str) -> str:
    """
    The whole ``phy-diag/state`` subtree of one port, as a single subscription path.

    Subscribed as a subtree rather than leaf by leaf because the server enumerates what it
    exposes; hard-coding the leaf names here would silently under-subscribe on any DUT whose
    phy-diag model differs.
    """
    return GnmiPaths.INTERFACE_LEAF.format(name=port_name, leaf=GnmiPaths.PHY_DIAG_STATE)


def _phy_diag_subtree_or_skip(client, port_name: str) -> str:
    """
    The phy-diag subtree path, skipping the test when this DUT does not expose it.

    phy-diag is not modelled everywhere, and a platform that has no such subtree would otherwise
    fail the continuity assertion for lack of any notification at all - reporting a heartbeat bug
    where there is only an absent feature.
    """
    subtree_path = _phy_diag_subtree_path(port_name)
    with allure.step(f"Verify {port_name} exposes {GnmiPaths.PHY_DIAG_STATE} over gNMI"):
        if not gnmi_path_is_exposed(client, subtree_path):
            pytest.skip(f"{subtree_path} returned no data for a ONCE subscribe, so this DUT does "
                        f"not expose the phy-diag subtree")
    return subtree_path


def _phy_diag_wildcard_path() -> str:
    """The ``phy-diag/state`` subtree of *every* interface, as a single subscription path."""
    return GnmiPaths.INTERFACE_WILDCARD_LEAF.format(leaf=GnmiPaths.PHY_DIAG_STATE)


def _enumerated_leaves(client, path: str, purpose: str) -> set:
    """
    The leaf set a ONCE subscribe on `path` enumerates, failing when it enumerates none.

    Lets a subscription be sized against what this DUT actually exposes instead of against a
    hardcoded per-platform leaf count, and gives the streamed result something to be compared
    with: leaves the server listed once but then stopped resending are exactly the symptom.

    Enumerating nothing fails rather than skips. Every path this is called with belongs to a
    family the DUT has already been shown to expose, so an empty enumeration is the server losing
    paths it serves - which is the defect being looked for, not a platform that lacks the feature.
    """
    notifications, out, err = gnmi_once_notifications(client, path)
    leaves = set(build_leaf_timeline(notifications))
    if leaves:
        return leaves
    refusal = next((stream.strip() for stream in (err, out) if is_path_not_modelled(stream)), '')
    reason = (f"the server rejected it as absent from its YANG model, though the same family is "
              f"exposed per interface: {refusal}" if refusal
              else "the server accepted the subscription and then returned no data")
    raise AssertionError(f"a ONCE subscribe on {path} enumerated no leaves, so {purpose} cannot "
                         f"be exercised: {reason}")


def _interfaces_from_wildcard(client) -> list:
    """Interface keys (``interfaces/interface[name=<port>]``) the wildcard expands to."""
    wildcard_light_path = GnmiPaths.INTERFACE_WILDCARD_LEAF.format(
        leaf=HEARTBEAT_LIGHT_INTERFACE_LEAF)
    leaves = _enumerated_leaves(client, wildcard_light_path,
                                'a subscription spread across interfaces')
    return sorted({f"{leaf.partition(']/')[0]}]" for leaf in leaves if ']/' in leaf})


def _distinct_light_paths(client, count: int) -> list:
    """
    `count` distinct cheap leaf paths, drawn from the interfaces the wildcard expands to.

    Spread leaf-major so consecutive paths differ by interface first, which keeps them genuinely
    distinct subscriptions rather than one path opened repeatedly - per Redmine 5230671 item 7 the
    customer's ten light subscriptions passed, so identical cheap ones cannot reproduce the mix
    that failed. Only leaves this module already streams are used: a subscription naming even one
    path outside the YANG model is rejected wholesale, taking the valid paths down with it.
    """
    interfaces = _interfaces_from_wildcard(client)
    paths = [f"{interface}/{leaf}"
             for leaf in HEARTBEAT_STATIC_INTERFACE_LEAVES
             for interface in interfaces]
    assert len(paths) >= count, (
        f"only {len(paths)} distinct light leaf paths are available across {len(interfaces)} "
        f"interface(s), fewer than the {count} a mixed-cost subscription of "
        f"{MAX_GNMI_SUBSCRIBERS} needs; a switch exposing this few interfaces over gNMI is itself "
        f"the problem to report")
    return paths[:count]


def _attach_scale_summary(path: str, expected_leaves: set, timeline: dict, notifications: list,
                          window_sec: int) -> None:
    """
    Report observed against expected volume, which Redmine 5230671 item 4 asks for explicitly.

    Per-leaf delivery counts are summarized rather than listed: at the customer's scale the list
    runs to thousands of entries, while what matters is the extremes - a leaf delivered once and
    never again is the silent leaf the whole item is about.
    """
    deliveries = sorted(len(stamps) for stamps in timeline.values())
    attach_plain_summary(
        f"path: {path}\n"
        f"leaves enumerated by the ONCE probe: {len(expected_leaves)}\n"
        f"leaves observed on the stream: {len(timeline)}\n"
        f"customer scale quoted by item 4, for reference: {HEARTBEAT_CUSTOMER_SCALE_LEAF_COUNT}\n"
        f"notifications received: {len(notifications)}\n"
        f"deliveries per leaf: min {deliveries[0]}, "
        f"median {deliveries[len(deliveries) // 2]}, max {deliveries[-1]}\n"
        f"deliveries a {HEARTBEAT_INTERVAL} heartbeat allows over {window_sec}s: "
        f"{max_heartbeat_deliveries(window_sec, HEARTBEAT_INTERVAL)}",
        "wildcard phy-diag scale",
    )


def _relative_leaves(leaf_paths: list) -> list:
    """Prefix-relative form of absolute leaf paths, for the timeline suffix matching."""
    return [path.split(']/', 1)[-1] if ']/' in path else path for path in leaf_paths]


def _collect(heartbeat_client, subscriptions, window_sec, **kwargs) -> list:
    """
    Stream `subscriptions` for `window_sec`, attach what each requested, and return their results.

    Two conditions are named explicitly here, because both leave a subscription with no
    notifications and would otherwise be misreported as a heartbeat bug. A refused sample interval
    fails: nv-gnmi accepts the short interval these tests use, so a refusal is a regression rather
    than agent policy. The server having stopped serving this client part-way through skips, since
    nothing can be concluded about the heartbeat once the client is no longer being served.
    """
    results = run_stream_subscribers(
        target_ip=heartbeat_client.server_host,
        subscriptions=subscriptions,
        window_sec=window_sec,
        username=heartbeat_client.username,
        password=heartbeat_client.password,
        target_port=heartbeat_client.server_port,
        engine=heartbeat_client.engine,
        **kwargs,
    )
    for subscription, result in zip(subscriptions, results):
        _attach_subscription_summary(subscription, window_sec, result)
    verify_subscribers_streamed(results)
    _skip_if_server_stopped_serving(heartbeat_client, results)
    return results


def _skip_if_server_stopped_serving(heartbeat_client, results: list) -> None:
    """
    Skip when nothing arrived because the server refused this client, not because it went quiet.

    Only probed when a subscription delivered nothing, so a healthy run costs no extra request.
    """
    silent = [result.label for result in results if not result.notifications]
    if not silent:
        return
    reason = gnmi_service_refused_reason(heartbeat_client)
    if reason:
        pytest.skip(f"the gNMI server stopped serving this client during the test, so "
                    f"{len(silent)} subscription(s) {silent} received nothing and heartbeat "
                    f"continuity cannot be judged: {reason}")


def _collect_one(heartbeat_client, subscription, window_sec, **kwargs) -> list:
    """Notifications from a single subscription streamed for `window_sec`."""
    return _collect(heartbeat_client, [subscription], window_sec, **kwargs)[0].notifications


def _subscription(paths, **kwargs) -> StreamSubscription:
    """A STREAM subscription on `paths`, defaulting to the shared heartbeat sample interval."""
    kwargs.setdefault('sample_interval', HEARTBEAT_SAMPLE_INTERVAL)
    return StreamSubscription(paths=paths, **kwargs)


def _attach_subscription_summary(subscription: StreamSubscription, window_sec: int,
                                 result: SubscriberResult) -> None:
    """
    Record what was actually requested and received, so a failure is debuggable.

    A subscription that returns nothing is ambiguous on its own: the server may have gone silent,
    or gnmic may have been unable to subscribe in a way none of the GnmicErr markers recognize.
    The raw streams are therefore reported whenever nothing arrived, since that is exactly the case
    where the notification count alone explains nothing.
    """
    lines = [
        f"stream-mode: {subscription.stream_mode or 'sample (implied by sample interval)'}",
        f"sample interval: {subscription.sample_interval}",
        f"heartbeat interval: {subscription.heartbeat_interval}",
        f"suppress-redundant: {subscription.suppress_redundant}",
        f"updates-only: {subscription.updates_only}",
        f"paths subscribed: {len(subscription.paths)}",
        f"window: {window_sec}s",
        f"notifications received: {len(result.notifications)}",
    ]
    if not result.notifications:
        lines += [
            "",
            f"gnmic stderr: {result.err.strip() or '<empty>'}",
            f"gnmic stdout: {result.out.strip()[:RAW_STREAM_EXCERPT_CHARS] or '<empty>'}",
        ]
    attach_plain_summary("\n".join(lines), f"subscription request: {subscription.label or 'subscriber'}")


# ---------------------------------------------------------------------------
# Heartbeat semantics
# ---------------------------------------------------------------------------


@pytest.mark.system
@pytest.mark.gnmi
@pytest.mark.timeout(6 * MINUTE, func_only=True)
@pytest.mark.parametrize('heartbeat_interval', HEARTBEAT_INTERVALS)
def test_heartbeat_resends_unchanged_leaves_under_suppress_redundant(
        heartbeat_client, subscribed_port_name, heartbeat_interval):
    """
    SAMPLE + suppress-redundant + heartbeat: every unchanged leaf is resent once per heartbeat.

    Redmine 5230671 item 1, swept across heartbeat intervals because a server can honor one timer
    value and not another. Leaves are chosen from the static families so suppression genuinely
    engages - a live counter changes every sample and is never suppressed, which would leave the
    heartbeat untested.
    """
    leaf_paths = _static_leaf_paths(subscribed_port_name)
    window_sec = heartbeat_window_sec(heartbeat_interval)
    context = f"{heartbeat_interval} suppress-redundant heartbeat on {subscribed_port_name}"
    subscription = _subscription(
        leaf_paths, stream_mode=GnmiStreamMode.SAMPLE, suppress_redundant=True,
        heartbeat_interval=heartbeat_interval,
        label=f"suppress+{heartbeat_interval} heartbeat on {subscribed_port_name}")

    with allure.step(f"Stream {len(leaf_paths)} static leaves with suppress-redundant and a "
                     f"{heartbeat_interval} heartbeat for {window_sec}s"):
        notifications = _collect_one(heartbeat_client, subscription, window_sec)

    with allure.step("Verify every unchanged leaf kept heartbeating"):
        timeline = verify_leaf_heartbeat_continuity(
            notifications, heartbeat_interval,
            expected_leaf_paths=_relative_leaves(leaf_paths), window_sec=window_sec,
            context=context)

    # Continuity is a lower bound on delivery, so it is equally satisfied by a server that ignored
    # suppress-redundant and sent every sample. Every subscribed leaf here is from the static
    # families, so all of them are expected inside the ceiling.
    with allure.step(f"Verify delivery was driven by the {heartbeat_interval} heartbeat, not the "
                     f"{HEARTBEAT_SAMPLE_INTERVAL} sample interval"):
        verify_heartbeat_drove_delivery(
            timeline, window_sec, heartbeat_interval, context=context)


@pytest.mark.system
@pytest.mark.gnmi
@pytest.mark.timeout(5 * MINUTE, func_only=True)
def test_suppress_redundant_without_heartbeat_stays_silent(
        heartbeat_client, subscribed_port_name):
    """
    SAMPLE + suppress-redundant with no heartbeat: an unchanged leaf is sent once and no more.

    The negative control. Without it, a server that ignores suppress-redundant entirely would
    pass the heartbeat test above for the wrong reason - every sample would arrive and the gap
    check would be trivially satisfied.
    """
    leaf_paths = _static_leaf_paths(subscribed_port_name)
    window_sec = heartbeat_window_sec(HEARTBEAT_INTERVAL_SHORT)
    subscription = _subscription(
        leaf_paths, stream_mode=GnmiStreamMode.SAMPLE, suppress_redundant=True,
        label=f"suppress without heartbeat on {subscribed_port_name}")

    with allure.step(f"Stream {len(leaf_paths)} static leaves with suppress-redundant and no "
                     f"heartbeat for {window_sec}s"):
        notifications = _collect_one(heartbeat_client, subscription, window_sec)

    with allure.step("Verify no unchanged leaf was resent"):
        verify_leaves_not_resent(
            notifications, expected_leaf_paths=_relative_leaves(leaf_paths),
            context=f"suppress-redundant without heartbeat on {subscribed_port_name}")


@pytest.mark.system
@pytest.mark.gnmi
@pytest.mark.timeout(5 * MINUTE, func_only=True)
def test_sample_without_suppress_redundant_delivers_every_interval(
        heartbeat_client, subscribed_port_name):
    """
    SAMPLE + heartbeat without suppress-redundant: the heartbeat must not throttle delivery.

    Per gNMI spec section 3.5.1.5.2 the heartbeat only governs suppressed updates, so an
    unsuppressed subscription keeps delivering every sample. Asserting continuity at the *sample*
    interval catches a server that let a long heartbeat slow an unsuppressed stream down.
    """
    leaf_paths = _static_leaf_paths(subscribed_port_name)
    window_sec = heartbeat_window_sec(HEARTBEAT_INTERVAL_SHORT)
    subscription = _subscription(
        leaf_paths, stream_mode=GnmiStreamMode.SAMPLE,
        heartbeat_interval=HEARTBEAT_INTERVAL_SHORT,
        label=f"heartbeat without suppress on {subscribed_port_name}")

    with allure.step(f"Stream {len(leaf_paths)} static leaves with a "
                     f"{HEARTBEAT_INTERVAL_SHORT} heartbeat and no suppression for {window_sec}s"):
        notifications = _collect_one(heartbeat_client, subscription, window_sec)

    with allure.step(f"Verify every leaf was delivered every {HEARTBEAT_SAMPLE_INTERVAL}"):
        timeline = verify_leaf_heartbeat_continuity(
            notifications, HEARTBEAT_SAMPLE_INTERVAL,
            expected_leaf_paths=_relative_leaves(leaf_paths), window_sec=window_sec,
            context=f"unsuppressed sample stream on {subscribed_port_name}")

    # A heartbeat-paced stream would yield about window/heartbeat deliveries per leaf; a
    # sample-paced one yields far more. Half the theoretical count absorbs registration delay.
    expected_deliveries = window_sec / parse_duration_sec(HEARTBEAT_SAMPLE_INTERVAL) / 2
    with allure.step(f"Verify delivery is sample-paced (> {expected_deliveries:.0f} per leaf), "
                     f"not heartbeat-paced"):
        for leaf, timestamps in sorted(timeline.items()):
            with allure.independent_step(f"{leaf}: {len(timestamps)} deliveries"):
                assert len(timestamps) > expected_deliveries, (
                    f"{leaf} was delivered only {len(timestamps)} times over {window_sec}s at a "
                    f"{HEARTBEAT_SAMPLE_INTERVAL} sample interval - the "
                    f"{HEARTBEAT_INTERVAL_SHORT} heartbeat appears to be pacing an "
                    f"unsuppressed subscription")


@pytest.mark.system
@pytest.mark.gnmi
@pytest.mark.timeout(5 * MINUTE, func_only=True)
def test_on_change_heartbeat_resends_static_leaves(heartbeat_client, subscribed_port_name):
    """
    ON_CHANGE + heartbeat: a leaf that never changes is still resent once per heartbeat.

    gNMI spec section 3.5.1.5.1. No sample interval is sent, so any delivery after the initial
    sync can only have come from the heartbeat timer.
    """
    leaf_paths = _static_leaf_paths(subscribed_port_name)
    window_sec = heartbeat_window_sec(HEARTBEAT_INTERVAL_SHORT)
    subscription = StreamSubscription(
        paths=leaf_paths, stream_mode=GnmiStreamMode.ON_CHANGE,
        heartbeat_interval=HEARTBEAT_INTERVAL_SHORT,
        label=f"on-change+heartbeat on {subscribed_port_name}")

    with allure.step(f"Stream {len(leaf_paths)} static leaves ON_CHANGE with a "
                     f"{HEARTBEAT_INTERVAL_SHORT} heartbeat for {window_sec}s"):
        notifications = _collect_one(heartbeat_client, subscription, window_sec)

    with allure.step("Verify each static leaf was resent on the heartbeat"):
        verify_leaf_heartbeat_continuity(
            notifications, HEARTBEAT_INTERVAL_SHORT,
            expected_leaf_paths=_relative_leaves(leaf_paths), window_sec=window_sec,
            context=f"ON_CHANGE heartbeat on {subscribed_port_name}")


@pytest.mark.system
@pytest.mark.gnmi
@pytest.mark.timeout(6 * MINUTE, func_only=True)
def test_on_change_without_heartbeat_is_silent_until_a_change(
        heartbeat_client, subscribed_port):
    """
    ON_CHANGE with no heartbeat: silent while static, and delivers exactly the change.

    The control for the ON_CHANGE heartbeat test. The description is changed mid-window, so the
    subscription has to prove both halves: it stayed quiet beforehand and still reported the
    change afterwards.
    """
    changing_leaf = GnmiPaths.INTERFACE_LEAF.format(name=subscribed_port.name,
                                                    leaf=HEARTBEAT_LIGHT_INTERFACE_LEAF)
    window_sec = heartbeat_window_sec(HEARTBEAT_INTERVAL_SHORT)
    new_description = Tools.RandomizationTool.get_random_string(7)
    subscription = StreamSubscription(
        paths=[changing_leaf], stream_mode=GnmiStreamMode.ON_CHANGE,
        label=f"on-change without heartbeat on {subscribed_port.name}")

    def _change_description():
        subscribed_port.interface.set(NvosConst.DESCRIPTION, new_description,
                                      apply=True).verify_result()

    with allure.step(f"Stream {changing_leaf} ON_CHANGE for {window_sec}s, changing the "
                     f"description after {HEARTBEAT_PERTURBATION_AT_SEC}s"):
        try:
            notifications = _collect_one(heartbeat_client, subscription, window_sec,
                                         mid_window_action=_change_description,
                                         action_at_sec=HEARTBEAT_PERTURBATION_AT_SEC)
        finally:
            subscribed_port.interface.unset(NvosConst.DESCRIPTION, apply=True).verify_result()

    timeline = build_leaf_timeline(notifications)
    with allure.step("Verify the change was reported and nothing else was resent"):
        assert timeline, "ON_CHANGE subscription delivered nothing, not even the initial sync"
        for leaf, timestamps in sorted(timeline.items()):
            with allure.independent_step(f"{leaf}: {len(timestamps)} deliveries"):
                # Initial sync plus the one change. More than that means the server resent an
                # unchanged value without a heartbeat being requested.
                assert len(timestamps) <= 2, (
                    f"{leaf} was delivered {len(timestamps)} times over {window_sec}s on an "
                    f"ON_CHANGE subscription with no heartbeat and a single value change")
        with allure.independent_step(f"the new description {new_description!r} was streamed"):
            assert new_description in str(notifications), (
                f"the description change to {new_description!r} was never streamed, so the "
                f"ON_CHANGE subscription missed a real change")


@pytest.mark.system
@pytest.mark.gnmi
@pytest.mark.timeout(5 * MINUTE, func_only=True)
def test_target_defined_stream_mode_honors_heartbeat(heartbeat_client, subscribed_port_name):
    """
    TARGET_DEFINED + suppress-redundant + heartbeat: the heartbeat holds whichever mode the
    server picks.

    This is gnmic's default stream-mode, so it is what every subscription in the suite that does
    not name a mode actually requests - worth asserting explicitly rather than by accident.
    """
    leaf_paths = _static_leaf_paths(subscribed_port_name)
    window_sec = heartbeat_window_sec(HEARTBEAT_INTERVAL_SHORT)
    subscription = _subscription(
        leaf_paths, stream_mode=GnmiStreamMode.TARGET_DEFINED, suppress_redundant=True,
        heartbeat_interval=HEARTBEAT_INTERVAL_SHORT,
        label=f"target-defined+heartbeat on {subscribed_port_name}")

    with allure.step(f"Stream {len(leaf_paths)} static leaves TARGET_DEFINED with a "
                     f"{HEARTBEAT_INTERVAL_SHORT} heartbeat for {window_sec}s"):
        notifications = _collect_one(heartbeat_client, subscription, window_sec)

    with allure.step("Verify the heartbeat held under the server-chosen stream mode"):
        verify_leaf_heartbeat_continuity(
            notifications, HEARTBEAT_INTERVAL_SHORT,
            expected_leaf_paths=_relative_leaves(leaf_paths), window_sec=window_sec,
            context=f"TARGET_DEFINED heartbeat on {subscribed_port_name}")


@pytest.mark.system
@pytest.mark.gnmi
@pytest.mark.timeout(5 * MINUTE, func_only=True)
def test_updates_only_subscription_still_heartbeats(heartbeat_client, subscribed_port_name):
    """
    updates-only + suppress-redundant + heartbeat: leaves arrive on the heartbeat, not on sync.

    With updates-only the server skips the initial sync, so on a static leaf the heartbeat is the
    only thing that can ever deliver a value. If the heartbeat is dropped the leaf is never seen
    at all, which is the most severe form of the failure.
    """
    leaf_paths = _static_leaf_paths(subscribed_port_name)
    window_sec = heartbeat_window_sec(HEARTBEAT_INTERVAL_SHORT)
    subscription = _subscription(
        leaf_paths, stream_mode=GnmiStreamMode.SAMPLE, suppress_redundant=True,
        heartbeat_interval=HEARTBEAT_INTERVAL_SHORT, updates_only=True,
        label=f"updates-only+heartbeat on {subscribed_port_name}")

    with allure.step(f"Stream {len(leaf_paths)} static leaves updates-only with a "
                     f"{HEARTBEAT_INTERVAL_SHORT} heartbeat for {window_sec}s"):
        notifications = _collect_one(heartbeat_client, subscription, window_sec)

    with allure.step("Verify the heartbeat delivered every leaf without an initial sync"):
        verify_leaf_heartbeat_continuity(
            notifications, HEARTBEAT_INTERVAL_SHORT,
            expected_leaf_paths=_relative_leaves(leaf_paths), window_sec=window_sec,
            no_initial_sync=True,
            context=f"updates-only heartbeat on {subscribed_port_name}")


# ---------------------------------------------------------------------------
# Scale: wildcard, large leaf count, mixed-cost concurrency
# ---------------------------------------------------------------------------


@pytest.mark.system
@pytest.mark.gnmi
@pytest.mark.timeout(8 * MINUTE, func_only=True)
def test_heartbeat_holds_on_wildcard_across_all_interfaces(heartbeat_client):
    """
    A wildcard subscription must heartbeat every interface it expanded to, per leaf.

    Redmine 5230671 item 3: every subscribed path in the suite targets one concrete
    ``[name=<port>]``, so wildcard expansion is untested. Continuity has to be judged per
    interface because an aggregate check over a wildcard subscription is satisfied by a single
    healthy interface while the rest go silent.
    """
    leaf_paths = _static_wildcard_leaf_paths()
    window_sec = heartbeat_window_sec(HEARTBEAT_INTERVAL)
    subscription = _subscription(
        leaf_paths, stream_mode=GnmiStreamMode.SAMPLE, suppress_redundant=True,
        heartbeat_interval=HEARTBEAT_INTERVAL, label='wildcard suppress+heartbeat')

    with allure.step(f"Stream {len(leaf_paths)} wildcard leaf paths with a {HEARTBEAT_INTERVAL} "
                     f"heartbeat for {window_sec}s"):
        notifications = _collect_one(heartbeat_client, subscription, window_sec)

    with allure.step("Verify every leaf of every expanded interface kept heartbeating"):
        timeline = verify_leaf_heartbeat_continuity(
            notifications, HEARTBEAT_INTERVAL,
            expected_leaf_paths=_relative_leaves(leaf_paths), window_sec=window_sec,
            context='wildcard heartbeat')

    # Majority rather than every leaf: the wildcard spans interfaces this test does not own, and a
    # link genuinely changing state mid-window resends its own oper-status legitimately.
    with allure.step(f"Verify most leaves arrived on the {HEARTBEAT_INTERVAL} heartbeat rather "
                     f"than every {HEARTBEAT_SAMPLE_INTERVAL}"):
        verify_heartbeat_drove_delivery(
            timeline, window_sec, HEARTBEAT_INTERVAL, context='wildcard heartbeat',
            min_paced_share=HEARTBEAT_PACED_SHARE_MIN)

    # Group the absolute leaf keys back into {interface: {leaf, ...}} so the expansion can be
    # compared interface against interface.
    leaves_by_interface = {}
    for leaf in timeline:
        interface, _, relative = leaf.partition(']/')
        if relative:
            leaves_by_interface.setdefault(f"{interface}]", set()).add(relative)
    attach_plain_summary(
        f"interfaces expanded: {len(leaves_by_interface)}\nleaves observed: {len(timeline)}\n" +
        "\n".join(f"  {interface}: {len(leaves)} leaves"
                  for interface, leaves in sorted(leaves_by_interface.items())),
        "wildcard expansion",
    )

    with allure.step("Verify the wildcard expanded to more than one interface"):
        assert len(leaves_by_interface) > 1, (
            f"the wildcard subscription only ever delivered "
            f"interface(s) {sorted(leaves_by_interface)}, so it did not expand across the DUT's "
            f"interfaces and the per-interface continuity result is not meaningful")

    # Comparing interfaces against the widest observed leaf set, rather than against the five
    # subscribed paths, keeps the check meaningful on interfaces whose model genuinely omits a
    # leaf while still catching the symptom that matters: the same leaf flowing on one interface
    # and silent on another.
    with allure.step("Verify every interface reported the same leaf set"):
        widest = max(leaves_by_interface.values(), key=len)
        for interface, leaves in sorted(leaves_by_interface.items()):
            with allure.independent_step(f"{interface}: {len(leaves)} leaves"):
                assert leaves == widest, (
                    f"{interface} is missing {sorted(widest - leaves)}, which other interfaces of "
                    f"the same wildcard subscription delivered - the wildcard lost a subset of "
                    f"leaves on this interface")


@pytest.mark.system
@pytest.mark.gnmi
@pytest.mark.timeout(8 * MINUTE, func_only=True)
def test_heartbeat_holds_on_phy_diag_subtree(heartbeat_client, subscribed_port_name):
    """
    Every leaf of the phy-diag/state subtree must keep heartbeating.

    Redmine 5230671 item 4: the suite's biggest subscription is six counter leaves against the
    customer's ~2,826, and phy-diag/state is only ever read ONCE today. It is the interesting
    family here because it is firmware-register-backed rather than cached, so it is the expensive
    one to sample. The item asks for 18+ leaves, which is asserted on the observed expansion so
    the test cannot pass on a subtree that yielded a handful.

    Note the item also says a fully populated Q3400-RA is needed to represent the customer's
    scale; on a smaller or sparsely populated switch this asserts the mechanism, not their volume.
    """
    subtree_path = _phy_diag_subtree_or_skip(heartbeat_client, subscribed_port_name)
    window_sec = heartbeat_window_sec(HEARTBEAT_INTERVAL)
    subscription = _subscription(
        [subtree_path], stream_mode=GnmiStreamMode.SAMPLE, suppress_redundant=True,
        heartbeat_interval=HEARTBEAT_INTERVAL,
        label=f"phy-diag subtree on {subscribed_port_name}")

    with allure.step(f"Stream {subtree_path} with a {HEARTBEAT_INTERVAL} heartbeat for "
                     f"{window_sec}s"):
        notifications = _collect_one(heartbeat_client, subscription, window_sec)

    with allure.step("Verify every phy-diag leaf kept heartbeating"):
        timeline = verify_leaf_heartbeat_continuity(
            notifications, HEARTBEAT_INTERVAL, window_sec=window_sec,
            context=f"phy-diag subtree on {subscribed_port_name}")

    with allure.step(f"Verify most leaves arrived on the {HEARTBEAT_INTERVAL} heartbeat rather "
                     f"than every {HEARTBEAT_SAMPLE_INTERVAL}"):
        verify_heartbeat_drove_delivery(
            timeline, window_sec, HEARTBEAT_INTERVAL,
            context=f"phy-diag subtree on {subscribed_port_name}",
            min_paced_share=HEARTBEAT_PACED_SHARE_MIN)

    with allure.step(f"Verify the subtree expanded to at least "
                     f"{HEARTBEAT_LARGE_LEAF_COUNT_MIN} leaves"):
        assert len(timeline) >= HEARTBEAT_LARGE_LEAF_COUNT_MIN, (
            f"{subtree_path} expanded to only {len(timeline)} leaves, fewer than the "
            f"{HEARTBEAT_LARGE_LEAF_COUNT_MIN} this case calls for, so this DUT does not exercise "
            f"the large-subscription path: {sorted(timeline)}")


@pytest.mark.system
@pytest.mark.gnmi
@pytest.mark.timeout(15 * MINUTE, func_only=True)
def test_heartbeat_holds_on_wildcard_phy_diag_across_all_interfaces(
        heartbeat_client, subscribed_port_name):
    """
    phy-diag/state on every interface at once: every leaf must keep heartbeating.

    Redmine 5230671 item 4: the suite's biggest subscription is six counter leaves against the
    customer's ~2,826, and nothing crosses a wildcard with the expensive firmware-register-backed
    subtree - which is the shape the customer was running. One phy-diag subtree is worth on the
    order of a hundred leaves per interface, so ``[name=*]`` over it is the only subscription here
    that reaches the customer's order of magnitude on a populated switch.

    The expected leaf set comes from a ONCE probe of the same path rather than a hardcoded count,
    so the assertions hold at any port count while the observed-versus-expected figures the item
    asks for are reported against it. How close a given DUT gets to the customer's volume is a
    property of how many of its ports are populated, so that is reported, not asserted.
    """
    # Prove phy-diag is modelled on a single port first, so a platform that lacks the family
    # entirely is told apart from one that has it but declines to expand a wildcard over it.
    _phy_diag_subtree_or_skip(heartbeat_client, subscribed_port_name)
    wildcard_path = _phy_diag_wildcard_path()
    expected_leaves = _enumerated_leaves(heartbeat_client, wildcard_path,
                                         'a wildcard phy-diag subscription')
    window_sec = heartbeat_window_sec(HEARTBEAT_INTERVAL)
    subscription = _subscription(
        [wildcard_path], stream_mode=GnmiStreamMode.SAMPLE, suppress_redundant=True,
        heartbeat_interval=HEARTBEAT_INTERVAL, label='wildcard phy-diag subtree')

    with allure.step(f"Stream {wildcard_path} ({len(expected_leaves)} leaves enumerated) with a "
                     f"{HEARTBEAT_INTERVAL} heartbeat for {window_sec}s"):
        notifications = _collect_one(heartbeat_client, subscription, window_sec)

    with allure.step("Verify every phy-diag leaf of every interface kept heartbeating"):
        timeline = verify_leaf_heartbeat_continuity(
            notifications, HEARTBEAT_INTERVAL, window_sec=window_sec,
            context='wildcard phy-diag heartbeat')

    _attach_scale_summary(wildcard_path, expected_leaves, timeline, notifications, window_sec)

    with allure.step(f"Verify most leaves arrived on the {HEARTBEAT_INTERVAL} heartbeat rather "
                     f"than every {HEARTBEAT_SAMPLE_INTERVAL}"):
        verify_heartbeat_drove_delivery(
            timeline, window_sec, HEARTBEAT_INTERVAL, context='wildcard phy-diag heartbeat',
            min_paced_share=HEARTBEAT_PACED_SHARE_MIN)

    with allure.step("Verify the stream covered every leaf the ONCE probe enumerated"):
        missing = sorted(expected_leaves - set(timeline))
        assert not missing, (
            f"{len(missing)}/{len(expected_leaves)} leaves that a ONCE subscribe on "
            f"{wildcard_path} enumerated never arrived on the equivalent STREAM subscription, so "
            f"the wildcard dropped them once the server had to keep resending: {missing[:20]}")

    with allure.step(f"Verify the subscription reached at least "
                     f"{HEARTBEAT_LARGE_LEAF_COUNT_MIN} leaves"):
        assert len(timeline) >= HEARTBEAT_LARGE_LEAF_COUNT_MIN, (
            f"{wildcard_path} only ever delivered {len(timeline)} leaves, fewer than the "
            f"{HEARTBEAT_LARGE_LEAF_COUNT_MIN} item 4 calls for, so this run does not exercise "
            f"the large-subscription path at all")


@pytest.mark.system
@pytest.mark.gnmi
@pytest.mark.timeout(10 * MINUTE, func_only=True)
def test_heartbeat_holds_with_mixed_cost_concurrent_subscriptions(
        heartbeat_client, subscribed_port_name):
    """
    A heavy subscription alongside light ones: every subscription must keep heartbeating.

    Redmine 5230671 item 7: the customer's ten light subscriptions passed and only the mix
    containing phy-diag failed, so opening ten *identical* cheap subscriptions - what the suite
    does today - cannot reproduce it. The light paths are therefore all different from each other
    as well as from the heavy one, spread across the interfaces the DUT exposes. The phy-diag
    subscription goes last so it is the one competing with nine already-established RPCs.
    """
    heavy_path = _phy_diag_subtree_or_skip(heartbeat_client, subscribed_port_name)
    light_paths = _distinct_light_paths(heartbeat_client, HEARTBEAT_MIXED_LIGHT_SUBSCRIBERS)
    common = {
        'stream_mode': GnmiStreamMode.SAMPLE,
        'sample_interval': HEARTBEAT_SAMPLE_INTERVAL,
        'suppress_redundant': True,
        'heartbeat_interval': HEARTBEAT_INTERVAL,
    }
    subscriptions = [
        StreamSubscription(paths=[path], label=f"light subscriber {idx} ({path})", **common)
        for idx, path in enumerate(light_paths)
    ]
    subscriptions.append(StreamSubscription(
        paths=[heavy_path], label='heavy phy-diag subscriber', **common))

    window_sec = heartbeat_window_sec(HEARTBEAT_INTERVAL)
    with allure.step(f"Open {HEARTBEAT_MIXED_LIGHT_SUBSCRIBERS} distinct single-leaf subscriptions "
                     f"plus one phy-diag subtree subscription for {window_sec}s"):
        results = _collect(heartbeat_client, subscriptions, window_sec)

    with allure.step("Verify every subscription in the mix kept heartbeating per leaf"):
        for result in results:
            with allure.independent_step(f"{result.label}: "
                                         f"{len(result.notifications)} notifications"):
                verify_leaf_heartbeat_continuity(
                    result.notifications, HEARTBEAT_INTERVAL, window_sec=window_sec,
                    context=result.label)


# ---------------------------------------------------------------------------
# Perturbation: a mid-window change must not silence a leaf
# ---------------------------------------------------------------------------


@pytest.mark.system
@pytest.mark.gnmi
@pytest.mark.timeout(8 * MINUTE, func_only=True)
def test_heartbeat_survives_link_flap_mid_subscription(heartbeat_client, flappable_port):
    """
    A link going down mid-window must not stop the heartbeat on any leaf.

    Injecting a real state change is a way to try to knock leaves out of a live subscription
    without waiting out a soak. Continuity is asserted on the interfaces that were *not* flapped,
    and the flapped interface is required to keep reporting after the event - so a server that
    drops leaves when a port changes state is caught either way.
    """
    leaf_paths = _static_wildcard_leaf_paths()
    window_sec = heartbeat_window_sec(HEARTBEAT_INTERVAL_SHORT, epochs=5)
    subscription = _subscription(
        leaf_paths, stream_mode=GnmiStreamMode.SAMPLE, suppress_redundant=True,
        heartbeat_interval=HEARTBEAT_INTERVAL_SHORT, label='wildcard heartbeat across a link flap')

    def _admin_down_the_port():
        _set_port_link_state(flappable_port, NvosConsts.LINK_STATE_DOWN)

    with allure.step(f"Stream wildcard static leaves for {window_sec}s, admin-downing "
                     f"{flappable_port.name} after {HEARTBEAT_PERTURBATION_AT_SEC}s"):
        notifications = _collect_one(heartbeat_client, subscription, window_sec,
                                     mid_window_action=_admin_down_the_port,
                                     action_at_sec=HEARTBEAT_PERTURBATION_AT_SEC)

    timeline = build_leaf_timeline(notifications)
    assert timeline, (
        f"the wildcard subscription streamed nothing across the flap of {flappable_port.name}, "
        f"so the flap's effect on the heartbeat cannot be judged")
    # The flapped interface's own leaves legitimately change (oper-status goes down), so they are
    # split out: continuity is asserted on the interfaces the flap should not have touched, and
    # the flapped one only has to keep reporting.
    flapped_key = GnmiPaths.INTERFACE_BY_NAME.format(name=flappable_port.name)
    unaffected = {leaf: stamps for leaf, stamps in timeline.items() if flapped_key not in leaf}
    flapped = {leaf: stamps for leaf, stamps in timeline.items() if flapped_key in leaf}
    window_end_ns = timeline_end_ns(timeline)
    max_gap_sec = parse_duration_sec(HEARTBEAT_INTERVAL_SHORT) * HEARTBEAT_TOLERANCE_FACTOR

    with allure.step("Verify the flap did not silence the leaves of any other interface"):
        assert unaffected, (
            f"no leaves from interfaces other than {flappable_port.name} were streamed, so the "
            f"flap's effect on unrelated interfaces cannot be judged")
        violations = analyze_leaf_continuity(unaffected, max_gap_sec, window_end_ns=window_end_ns)
        attach_plain_summary(
            f"unaffected leaves: {len(unaffected)}\nflapped-interface leaves: {len(flapped)}\n"
            f"allowed gap: {max_gap_sec}s\nleaves that missed an epoch: {len(violations)}\n" +
            "\n".join(f"  {leaf}: {detail}" for leaf, detail in list(violations.items())[:20]),
            "heartbeat continuity across the link flap",
        )
        assert not violations, (
            f"{len(violations)}/{len(unaffected)} leaves on interfaces other than the flapped "
            f"{flappable_port.name} missed a heartbeat epoch (allowed gap {max_gap_sec}s), so "
            f"admin-downing one port disturbed the heartbeat of the rest: "
            f"{dict(list(violations.items())[:20])}")

    with allure.step(f"Verify {flappable_port.name} kept reporting after going down"):
        assert flapped, (
            f"{flappable_port.name} disappeared from the subscription entirely after being "
            f"admin-downed, instead of continuing to report its state")
        # Anchored on the end of the stream rather than on the action: the action fires on the
        # runner's clock, which cannot be compared with server timestamps without importing
        # DUT/player skew, and inferring it from the first notification would carry the
        # registration delay instead. Being this close to the end also says more than landing
        # somewhere after the flap - the interface was still reporting when the window closed.
        resumed = sorted(leaf for leaf, stamps in flapped.items()
                         if max(stamps) > window_end_ns - int(max_gap_sec * NANOSEC_PER_SEC))
        assert resumed, (
            f"none of {flappable_port.name}'s {len(flapped)} leaves were delivered in the last "
            f"{max_gap_sec}s of the window after the admin-down, so the flap silenced the "
            f"interface for the rest of it")
