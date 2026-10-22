"""
gNMI rate limiting (Envoy): unary Capabilities under/over limit, recovery, ON_CHANGE + unary stress,
restart under load. Overload yields local_rate_limited on clients.
"""
import threading
import time

import pytest

import ngts.tools.test_utils.allure_utils as allure
from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from ngts.constants.constants import GnmiConsts
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.system.gnmi.GnmiClient import GnmiClient
from ngts.tests_nvos.system.gnmi import constants as gnmi_consts
from ngts.tests_nvos.system.gnmi import helpers as gnmi_helpers


@pytest.mark.system
@pytest.mark.gnmi
def test_gnmi_rate_limit_under_threshold(engines, devices):
    """
    Capabilities stays under the global gNMI rate limit with no errors.

    Test flow:
        1. Validate gNMI is running
        2. Single client: throttled Capabilities (~half the limit)
        3. Assert no rate-limit or RPC failures
        4. Max subscribers: parallel clients, same aggregate rate
        5. Assert no rate-limit or RPC failures
    """
    system = System()
    with allure.step("Validate gNMI enabled and running"):
        gnmi_helpers.validate_gnmi_enabled_and_running(system.gnmi_server, engines)

    dut = engines.dut
    username = devices.dut.default_username
    password = devices.dut.default_password
    client = GnmiClient(
        dut.ip,
        GnmiConsts.GNMI_DEFAULT_PORT,
        username,
        password,
        cmd_time=gnmi_consts.PER_REQUEST_TIMEOUT_SEC,
    )
    # Throttle to ~30 req/min (under 60 limit): sleep interval seconds between requests
    rpm_target = gnmi_consts.GNMI_RATE_LIMIT_REQ_PER_MIN // 2
    interval = 60.0 / rpm_target

    success, fail, rate_limit_failures, sample_error = 0, 0, 0, ""
    end_time = time.time() + gnmi_consts.RAMP_DURATION_SEC
    with allure.step(f"Phase 1: single client ~{rpm_target} rpm, {gnmi_consts.RAMP_DURATION_SEC}s"):
        while time.time() < end_time:
            with allure.independent_step("Capabilities request"):
                out, err = client.gnmic_capabilities(skip_cert_verify=True, cmd_time=gnmi_consts.PER_REQUEST_TIMEOUT_SEC)
                if gnmi_helpers.gnmi_response_failed(out, err):
                    fail += 1
                    if gnmi_helpers.gnmi_response_rate_limited(out, err):
                        rate_limit_failures += 1
                    if not sample_error:
                        sample_error = (err or out or "").strip()[:gnmi_consts.SAMPLE_ERROR_MAX_LEN]
                else:
                    success += 1
            time.sleep(interval)

    gnmi_helpers.attach_rate_limit_result(
        success, fail, sample_error, gnmi_consts.RAMP_DURATION_SEC, "Under threshold: phase 1 single-client ramp",
        rate_limit_failures=rate_limit_failures,
    )

    with allure.step("Assert no rate-limit errors (phase 1)"):
        assert rate_limit_failures == 0, (
            f"Expected no rate-limit errors. "
            f"rate_limit_failures={rate_limit_failures}, other_failures={fail - rate_limit_failures}. "
            f"Sample: {sample_error}"
        )
    with allure.step("Assert no RPC failures (phase 1)"):
        assert fail == 0, f"Expected no failures when under rate limit. success={success} fail={fail}"

    # Aim for ~half the limit aggregated across all clients. Integer division can make the realized
    # aggregate dip slightly below half (e.g. 30//10*10 == 30 here, but brittle if constants change),
    # which only makes the under-threshold check stricter, so it is safe.
    aggregate_rpm_multi = gnmi_consts.GNMI_RATE_LIMIT_REQ_PER_MIN // 2
    rpm_per_client_multi = max(1, aggregate_rpm_multi // gnmi_consts.MAX_GNMI_SUBSCRIBERS)
    interval_multi = 60.0 / rpm_per_client_multi
    results_multi = [None] * gnmi_consts.MAX_GNMI_SUBSCRIBERS
    first_error_multi = {}
    lock_multi = threading.Lock()

    def run_under_threshold_capabilities_multi(thread_id):
        c = GnmiClient(
            dut.ip,
            GnmiConsts.GNMI_DEFAULT_PORT,
            username,
            password,
            cmd_time=gnmi_consts.PER_REQUEST_TIMEOUT_SEC,
        )
        s, f, rl = 0, 0, 0
        until = time.time() + gnmi_consts.RAMP_DURATION_SEC
        while time.time() < until:
            with allure.independent_step(f"Client {thread_id} capabilities request"):
                out, err = c.gnmic_capabilities(skip_cert_verify=True, cmd_time=gnmi_consts.PER_REQUEST_TIMEOUT_SEC)
                if gnmi_helpers.gnmi_response_failed(out, err):
                    f += 1
                    if gnmi_helpers.gnmi_response_rate_limited(out, err):
                        rl += 1
                    with lock_multi:
                        if "err" not in first_error_multi:
                            first_error_multi["err"] = (err or out or "").strip()[:gnmi_consts.SAMPLE_ERROR_MAX_LEN]
                else:
                    s += 1
            time.sleep(interval_multi)
        results_multi[thread_id] = (s, f, rl)

    with allure.step(
        f"Phase 2: {gnmi_consts.MAX_GNMI_SUBSCRIBERS} clients ~{rpm_per_client_multi} rpm, {gnmi_consts.RAMP_DURATION_SEC}s"
    ):
        phase2_start = time.time()
        # daemon=True + bounded join so a hung gnmic subprocess cannot hang the suite;
        # workers are deadline-bounded, so a still-alive thread after the bounded join
        # indicates a real stuck process and must fail the test.
        threads_multi = [
            threading.Thread(
                target=run_under_threshold_capabilities_multi,
                args=(i,),
                daemon=True,
                name=f"under-thr-phase2-{i}",
            )
            for i in range(gnmi_consts.MAX_GNMI_SUBSCRIBERS)
        ]
        for t in threads_multi:
            t.start()
        phase2_alive = gnmi_helpers.shutdown_threads(
            threads_multi, gnmi_consts.CAPABILITIES_FLOOD_THREAD_JOIN_TIMEOUT_SEC
        )
        assert not phase2_alive, (
            f"Phase-2 under-threshold worker(s) still alive after bounded join: "
            f"{[t.name for t in phase2_alive]}"
        )
        phase2_elapsed_sec = time.time() - phase2_start

    ms = sum(r[0] for r in results_multi if r is not None)
    mf = sum(r[1] for r in results_multi if r is not None)
    mrl = sum(r[2] for r in results_multi if r is not None)
    sample_multi = first_error_multi.get("err", "")

    gnmi_helpers.attach_rate_limit_result(
        ms,
        mf,
        sample_multi,
        phase2_elapsed_sec,
        "Under threshold: phase 2 max parallel clients",
        rate_limit_failures=mrl,
    )

    with allure.step("Assert no rate-limit errors (phase 2)"):
        assert mrl == 0, (
            f"Expected no rate-limit errors with {gnmi_consts.MAX_GNMI_SUBSCRIBERS} clients under threshold. "
            f"rate_limit_failures={mrl}, other_failures={mf - mrl}. Sample: {sample_multi}"
        )
    with allure.step("Assert no RPC failures (phase 2)"):
        assert mf == 0, (
            f"Expected no failures with {gnmi_consts.MAX_GNMI_SUBSCRIBERS} clients under threshold. "
            f"success={ms} fail={mf}"
        )


@pytest.mark.system
@pytest.mark.gnmi
def test_gnmi_rate_limit_over_threshold(engines, devices):
    """
    Parallel Capabilities exceeds the limit; expect rate limiting, then reachability.

    Test flow:
        1. Validate gNMI is running
        2. Flood Capabilities from parallel clients
        3. Skip if RPM too low; assert rate-limit and failures
        4. Pause; verify Capabilities still works
    """
    system = System()
    with allure.step("Validate gNMI enabled and running"):
        gnmi_helpers.validate_gnmi_enabled_and_running(system.gnmi_server, engines)

    dut = engines.dut
    username = devices.dut.default_username
    password = devices.dut.default_password
    timeout_sec = gnmi_consts.PER_REQUEST_TIMEOUT_OVER_THRESHOLD_SEC

    with allure.step(f"Flood Capabilities {gnmi_consts.OVERLOAD_FLOOD_SEC}s ({gnmi_consts.NUM_CLIENTS_OVER_THRESHOLD} clients)"):
        results, elapsed_sec, first_error = gnmi_helpers.run_parallel_capabilities_flood(
            dut,
            username,
            password,
            duration_sec=gnmi_consts.OVERLOAD_FLOOD_SEC,
            num_clients=gnmi_consts.NUM_CLIENTS_OVER_THRESHOLD,
            cmd_timeout_sec=timeout_sec,
        )

    total_success = sum(r[0] for r in results if r is not None)
    total_fail = sum(r[1] for r in results if r is not None)
    total_rate_limit_failures = sum(r[2] for r in results if r is not None)
    total_overload = sum(r[3] for r in results if r is not None)
    total_requests = total_success + total_fail
    sample_error = first_error.get("err", "")

    # achieved_rpm is derived from elapsed_sec (the wait window); it is biased slightly
    # high because staggered loops run a touch longer. Fine for the skip gate below.
    achieved_rpm = total_requests * (60.0 / elapsed_sec) if elapsed_sec > 0 else 0.0
    min_rpm_required = gnmi_consts.GNMI_RATE_LIMIT_REQ_PER_MIN * gnmi_consts.MIN_RPM_FRACTION_TO_REQUIRE_RATE_LIMIT

    gnmi_helpers.attach_rate_limit_result(
        total_success, total_fail, sample_error, elapsed_sec, "Ramp over threshold",
        rate_limit_failures=total_rate_limit_failures,
    )

    with allure.step("Skip if load below threshold"):
        if achieved_rpm < min_rpm_required:
            pytest.skip(
                f"Environment could not generate enough load: achieved {achieved_rpm:.1f} req/min "
                f"< {min_rpm_required:.0f} req/min (fraction of {gnmi_consts.GNMI_RATE_LIMIT_REQ_PER_MIN} limit). "
                f"Cannot reliably assert rate-limit behavior. Total requests={total_requests}, elapsed={elapsed_sec:.1f}s."
            )

    with allure.step("Assert server throttled (rate limit or deadline exceeded)"):
        # Overload can surface as local_rate_limited or as DeadlineExceeded under heavy flood; either
        # proves the server protected itself, so assert on the combined overload count.
        assert total_overload > 0, (
            f"Expected the server to throttle (local_rate_limited or deadline exceeded) at {achieved_rpm:.1f} req/min. "
            f"overload={total_overload} (rate_limit={total_rate_limit_failures}), "
            f"other_failures={total_fail - total_overload}. Sample: {sample_error}"
        )
    with allure.step("Assert RPC failures"):
        assert total_fail > 0, (
            f"Expected failures when over threshold at {achieved_rpm:.1f} req/min. "
            f"success={total_success} fail={total_fail}"
        )

    time.sleep(gnmi_consts.POST_OVERLOAD_PAUSE_SEC)

    with allure.step("Capabilities after overload"):
        check_client = GnmiClient(
            dut.ip,
            GnmiConsts.GNMI_DEFAULT_PORT,
            username,
            password,
            cmd_time=gnmi_consts.PER_REQUEST_TIMEOUT_SEC,
        )
        gnmi_helpers.capabilities_until_success_after_restart(
            check_client, "Post unary overload reachability"
        )


@pytest.mark.system
@pytest.mark.gnmi
def test_gnmi_rate_limit_recovers_after_over_threshold(engines, devices):
    """
    After overload and rate limiting, a low-rate client recovers without restarting gNMI.

    Test flow:
        1. Validate gNMI is running
        2. Parallel overload until rate limited
        3. Drain for the rate-limit window so the limiter recovers
        4. Single client low rate, assert all Capabilities are clean
    """
    system = System()
    with allure.step("Validate gNMI enabled and running"):
        gnmi_helpers.validate_gnmi_enabled_and_running(system.gnmi_server, engines)

    dut = engines.dut
    username = devices.dut.default_username
    password = devices.dut.default_password
    timeout_sec = gnmi_consts.PER_REQUEST_TIMEOUT_OVER_THRESHOLD_SEC

    # Let the DUT recover from earlier tests so the closed-loop flood reaches its standalone
    # request rate; on a warm server the rate stays just above the limit and never trips.
    with allure.step(f"Settle {gnmi_consts.PRE_OVERLOAD_SETTLE_SEC}s before overload (recover from prior tests)"):
        time.sleep(gnmi_consts.PRE_OVERLOAD_SETTLE_SEC)

    # Use the same flood parameters as test_gnmi_rate_limit_over_threshold so phase 1 trips the
    # limiter as reliably as that test does: the long OVERLOAD_FLOOD_SEC window guarantees the
    # limiter's burst bucket fully depletes and it starts returning overload errors.
    with allure.step(f"Overload {gnmi_consts.OVERLOAD_FLOOD_SEC}s ({gnmi_consts.NUM_CLIENTS_OVER_THRESHOLD} clients)"):
        results, p1_elapsed, first_error = gnmi_helpers.run_parallel_capabilities_flood(
            dut,
            username,
            password,
            duration_sec=gnmi_consts.OVERLOAD_FLOOD_SEC,
            num_clients=gnmi_consts.NUM_CLIENTS_OVER_THRESHOLD,
            cmd_timeout_sec=timeout_sec,
        )

    total_success = sum(r[0] for r in results if r is not None)
    total_fail = sum(r[1] for r in results if r is not None)
    total_rl = sum(r[2] for r in results if r is not None)
    total_ov = sum(r[3] for r in results if r is not None)
    total_req = total_success + total_fail
    # Derived from p1_elapsed (the wait window); biased slightly high (staggered loops).
    achieved_rpm = total_req * (60.0 / p1_elapsed) if p1_elapsed > 0 else 0.0
    sample_error = first_error.get("err", "")

    gnmi_helpers.attach_rate_limit_result(
        total_success,
        total_fail,
        sample_error,
        p1_elapsed,
        "Recovery phase 1 overload",
        rate_limit_failures=total_rl,
    )

    with allure.step("Require overload before testing recovery"):
        # Recovery can only be exercised if we actually drove the server into overload. On a warm or
        # slow setup the closed-loop flood may not deplete the limiter's burst within the window; in
        # that case skip rather than fail - the over-threshold test owns verifying the limiter trips.
        if total_ov == 0:
            pytest.skip(
                f"Could not drive gNMI into overload within {gnmi_consts.OVERLOAD_FLOOD_SEC}s "
                f"(achieved ~{achieved_rpm:.1f} req/min, rate_limit={total_rl}, overload=0); "
                f"cannot verify recovery on this run."
            )

    client = GnmiClient(
        dut.ip,
        GnmiConsts.GNMI_DEFAULT_PORT,
        username,
        password,
        cmd_time=gnmi_consts.PER_REQUEST_TIMEOUT_SEC,
    )
    rpm_low = gnmi_consts.GNMI_RATE_LIMIT_REQ_PER_MIN // 2
    interval = 60.0 / rpm_low

    # Drain: keep a gentle low-rate trickle for the full rate-limit window so the limiter fully
    # recovers before we assert. Results during the drain are intentionally ignored.
    with allure.step(
        f"Drain {gnmi_consts.RECOVERY_DRAIN_SEC}s at ~{rpm_low} rpm (wait for limiter to recover)"
    ):
        drain_end = time.time() + gnmi_consts.RECOVERY_DRAIN_SEC
        while time.time() < drain_end:
            client.gnmic_capabilities(skip_cert_verify=True, cmd_time=gnmi_consts.PER_REQUEST_TIMEOUT_SEC)
            time.sleep(interval)

    # Low-rate check: the system should be back to normal now, so every request must succeed cleanly.
    success2, fail2, rl2 = 0, 0, 0
    sample2 = ""
    with allure.step(f"Low rate ~{rpm_low} rpm, {gnmi_consts.RECOVERY_LOW_RATE_SEC}s (expect all clean)"):
        p2_start = time.time()
        while time.time() < p2_start + gnmi_consts.RECOVERY_LOW_RATE_SEC:
            out, err = client.gnmic_capabilities(skip_cert_verify=True, cmd_time=gnmi_consts.PER_REQUEST_TIMEOUT_SEC)
            if gnmi_helpers.gnmi_response_failed(out, err):
                fail2 += 1
                if gnmi_helpers.gnmi_response_rate_limited(out, err):
                    rl2 += 1
                if not sample2:
                    sample2 = (err or out or "").strip()[:gnmi_consts.SAMPLE_ERROR_MAX_LEN]
            else:
                success2 += 1
            time.sleep(interval)
        p2_elapsed = time.time() - p2_start

    gnmi_helpers.attach_rate_limit_result(
        success2,
        fail2,
        sample2,
        p2_elapsed,
        "Recovery phase 2 low rate",
        rate_limit_failures=rl2,
    )
    gnmi_helpers.attach_plain_summary(
        f"After {gnmi_consts.RECOVERY_DRAIN_SEC}s drain, low-rate samples: "
        f"success={success2} fail={fail2} rate_limit_fail={rl2}.\n",
        "Recovery summary",
    )

    with allure.step("Assert recovery after drain"):
        assert success2 > 0, (
            f"Expected at least one successful low-rate request after drain; "
            f"success={success2}, sample={sample2}"
        )
        assert rl2 == 0, (
            f"Expected no local_rate_limited after {gnmi_consts.RECOVERY_DRAIN_SEC}s drain; got {rl2}. "
            f"other_fail={fail2 - rl2} sample={sample2}"
        )
        assert fail2 == 0, (
            f"Expected no failures at ~{rpm_low} rpm after recovery. "
            f"success={success2} fail={fail2}"
        )


@pytest.mark.system
@pytest.mark.gnmi
def test_gnmi_rate_limit_restart_gnmi_under_load(engines, devices):
    """
    NVUE restart of gNMI while Capabilities flood runs; service returns and limiting still applies.

    Test flow:
        1. Validate gNMI is running
        2. Start flood threads and warm up
        3. Disable/enable gNMI via NVUE
        4. Validate gNMI; pause flood, drain, Capabilities check
        5. Resume flood; assert limiting still seen
        6. Pause flood, drain; Capabilities must succeed (no errors) once rate drops
        7. Stop threads
    """
    system = System()
    dut = engines.dut
    username = devices.dut.default_username
    password = devices.dut.default_password
    timeout_sec = gnmi_consts.PER_REQUEST_TIMEOUT_OVER_THRESHOLD_SEC
    stop_attackers = threading.Event()
    pause_attackers = threading.Event()
    post_restart = {"rl": 0, "fail": 0, "ok": 0}
    post_restart_lock = threading.Lock()

    def attacker_loop():
        client = GnmiClient(
            dut.ip,
            GnmiConsts.GNMI_DEFAULT_PORT,
            username,
            password,
            cmd_time=timeout_sec,
        )
        while not stop_attackers.is_set():
            while pause_attackers.is_set() and not stop_attackers.is_set():
                time.sleep(0.05)
            if stop_attackers.is_set():
                break
            out, err = client.gnmic_capabilities(skip_cert_verify=True, cmd_time=timeout_sec)
            if stop_attackers.is_set():
                break
            with post_restart_lock:
                if gnmi_helpers.gnmi_response_failed(out, err):
                    post_restart["fail"] += 1
                    if gnmi_helpers.gnmi_response_rate_limited(out, err):
                        post_restart["rl"] += 1
                else:
                    post_restart["ok"] += 1

    # daemon=True is a safety net so a stuck attacker cannot survive the suite; the
    # stop_attackers event + alive-check assert in the finally block is the primary shutdown path.
    threads = [
        threading.Thread(target=attacker_loop, daemon=True)
        for _ in range(gnmi_consts.NUM_CLIENTS_OVER_THRESHOLD)
    ]

    with allure.step("Validate gNMI enabled and running"):
        gnmi_helpers.validate_gnmi_enabled_and_running(system.gnmi_server, engines)

    # One parent step for the whole time attacker threads run so GnmiClient's per-RPC Allure
    # nested steps land here (allure_commons seeds worker-thread context from the main thread).
    with allure.step(
        f"Capabilities flood during restart scenario ({gnmi_consts.NUM_CLIENTS_OVER_THRESHOLD} threads)"
    ):
        with allure.step(f"Start {gnmi_consts.NUM_CLIENTS_OVER_THRESHOLD} attacker threads"):
            for t in threads:
                t.start()
        try:
            with allure.step(f"Warm up {gnmi_consts.RESTART_UNDER_LOAD_WARMUP_SEC}s"):
                time.sleep(gnmi_consts.RESTART_UNDER_LOAD_WARMUP_SEC)

            with allure.step("NVUE restart gNMI"):
                with allure.step("Disable gNMI + apply"):
                    with post_restart_lock:
                        post_restart["rl"] = 0
                        post_restart["fail"] = 0
                        post_restart["ok"] = 0
                    system.gnmi_server.disable_gnmi_server(apply=False)
                    NvueGeneralCli.apply_config(engines.dut, ask_for_confirmation=True)
                with allure.step(f"Wait {gnmi_consts.GNMI_RESTART_POST_DISABLE_WAIT_SEC}s"):
                    time.sleep(gnmi_consts.GNMI_RESTART_POST_DISABLE_WAIT_SEC)
                with allure.step("Enable gNMI + apply"):
                    system.gnmi_server.enable_gnmi_server(apply=False)
                    NvueGeneralCli.apply_config(engines.dut, ask_for_confirmation=True)
                with allure.step(f"Wait {gnmi_consts.GNMI_RESTART_POST_ENABLE_WAIT_SEC}s"):
                    time.sleep(gnmi_consts.GNMI_RESTART_POST_ENABLE_WAIT_SEC)

            with allure.step("Validate gNMI after restart"):
                gnmi_helpers.wait_for_gnmi_ready(engines)
                gnmi_helpers.validate_gnmi_enabled_and_running(system.gnmi_server, engines)

            with allure.step("Pause flood; Capabilities reachability"):
                check_client = GnmiClient(
                    dut.ip,
                    GnmiConsts.GNMI_DEFAULT_PORT,
                    username,
                    password,
                    cmd_time=gnmi_consts.PER_REQUEST_TIMEOUT_SEC,
                )
                pause_attackers.set()
                try:
                    # Drain a full quiet rate-limit window (attackers paused, no requests at all)
                    # so the limiter recovers before we probe; otherwise each probe is a slow
                    # timing-out request that keeps the still-saturated server busy.
                    with allure.step(f"Drain {gnmi_consts.RECOVERY_DRAIN_SEC}s (silent)"):
                        time.sleep(gnmi_consts.RECOVERY_DRAIN_SEC)
                    gnmi_helpers.capabilities_until_success_after_restart(
                        check_client, "Post-restart gNMI reachability"
                    )
                finally:
                    pause_attackers.clear()

            with allure.step("Resume flood; assert limiting"):
                with allure.step(f"Observe flood {gnmi_consts.RESTART_POST_ENABLE_VERIFY_FLOOD_SEC}s"):
                    time.sleep(gnmi_consts.RESTART_POST_ENABLE_VERIFY_FLOOD_SEC)
                with allure.step("Attach outcomes; assert limiting"):
                    summary = (
                        f"Post-restart sample (attackers still running): ok={post_restart['ok']} "
                        f"fail={post_restart['fail']} rate_limit={post_restart['rl']}\n"
                    )
                    gnmi_helpers.attach_plain_summary(summary, "Restart under load: post-enable client outcomes")
                    assert post_restart["rl"] > 0 or post_restart["fail"] > 0, (
                        "Expected continued load after restart to hit rate limit or other RPC failures; "
                        f"got ok={post_restart['ok']} fail={post_restart['fail']} rl={post_restart['rl']}"
                    )

            with allure.step(
                "Pause flood; drain; verify Capabilities clean after rate drops (no errors)"
            ):
                settle_client = GnmiClient(
                    dut.ip,
                    GnmiConsts.GNMI_DEFAULT_PORT,
                    username,
                    password,
                    cmd_time=gnmi_consts.PER_REQUEST_TIMEOUT_SEC,
                )
                pause_attackers.set()
                with allure.step(
                    f"Drain {gnmi_consts.RECOVERY_DRAIN_SEC}s while flood paused"
                ):
                    time.sleep(gnmi_consts.RECOVERY_DRAIN_SEC)
                gnmi_helpers.capabilities_until_success_after_restart(
                    settle_client,
                    "After limiting: Capabilities with no gNMI errors",
                )
                # Leave pause set until outer finally stops threads (avoid a brief window with no pause).

        finally:
            with allure.step("Stop flood threads"):
                stop_attackers.set()
                # Clear pause so any worker blocked in the pause loop observes stop_attackers.
                pause_attackers.clear()
                still_alive = gnmi_helpers.shutdown_threads(
                    threads, gnmi_consts.ATTACKER_THREAD_JOIN_TIMEOUT_SEC
                )
                assert not still_alive, (
                    f"Attacker thread(s) still alive after stop_attackers.set() + join: "
                    f"{[t.name for t in still_alive]}. They may leak into subsequent tests."
                )
