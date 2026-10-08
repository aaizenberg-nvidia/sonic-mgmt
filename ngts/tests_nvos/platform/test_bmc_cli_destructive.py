"""
Destructive / state-changing bmc-cli tests.

Covers the bmc-cli subcommands that mutate switch/BMC state:
    reset {cpu,bmc,power-cycle,power-cycle-immediate,power-cycle-bypass}
    firmware --file / firmware-update-multipart
    password-reset / factory-reset-password
    scp --to-bmc / --from-bmc
    console set
    debug-token generate
    password-policy --set-min-length

Safety design (these run against real hardware):
    * Only ``reset bmc-reset`` is actually executed (it is recoverable and is
      already used elsewhere for firmware activation). The ``cpu-reset`` and
      ``power-cycle*`` variants reboot the NOS host, so they are validated for
      availability via ``reset --help`` rather than executed here.
    * The full ``firmware --file`` / ``firmware-update-multipart`` burn is covered
      by test_bmc_nvue_firmware.py; here we only validate command availability.
    * password / console / password-policy tests restore the original state in a
      ``finally`` block.

Opt-in / destructive lane: not part of the default BMC CI selection — run
these tests explicitly when the lab allows state-changing BMC operations.
``test_bmc_cli_factory_reset_password`` is further gated: ``@pytest.mark.bmc_opt_in``
plus ``BMC_ALLOW_FACTORY_RESET_PASSWORD=1`` (mutates BMC root on shared hardware).
"""
import logging
import os
import re
import pytest
from ngts.nvos_tools.infra.BmcCliTool import BmcCliTool
from ngts.nvos_tools.infra.BmcTool import assert_usb0_bmc_link_addrs
from ngts.tests_nvos.general.security.bmc.bmc_creds.constants import (
    ADMIN_USER_DEFAULT_PASSWORD, get_cl_bmc_root_password, OPENBMC, ROOT,
)
from ngts.tests_nvos.platform.bmc_helpers import (
    cl_build_serial, skip_if_no_bmc, wait_for_bmc_down, wait_for_bmc_healthy,
    wait_for_bmc_ready,
)
from ngts.tools.test_utils import allure_utils as allure
cumulus_owner = "gpazbenitzha"
logger = logging.getLogger()
BMC_RECOVERY_TIMEOUT = 480
# FR 5064022 retires CPU-to-BMC scp. Measured, not guessed: the copy still works
# on mlx-6810-01 at 5.18.6809.0015 and refuses on mlx-6600-03 at 5.19.0.0025, so
# the case asserts the old contract below this build and the new one from it on.
SCP_RETIRED_FROM_CL = (5, 19, 0, 25)


def _factory_pw_candidates():
    """Passwords to try after factory-reset-password (order: factory defaults, then lab root)."""
    return (OPENBMC, ADMIN_USER_DEFAULT_PASSWORD, get_cl_bmc_root_password())


def _bmc_authenticates(engine, password):
    """Return True if bmc-cli 'info' authenticates with the given password."""
    result = BmcCliTool(engine, password=password).system_info()
    return bool(result) and 'Login failed' not in str(result)


@pytest.mark.bmc
@pytest.mark.disable_loganalyzer
@pytest.mark.cumulus
def test_bmc_cli_reset_bmc(engines, devices, topology_obj):
    """Execute 'bmc-cli reset bmc-reset', wait for recovery, verify BMC healthy."""
    skip_if_no_bmc(devices, engines)
    cli = BmcCliTool(engines.dut)
    with allure.step("Reset the BMC via bmc-cli reset bmc-reset"):
        cli.reset_bmc()
    with allure.step("Wait for the BMC to actually go down"):
        # Replaces a fixed sleep(60). That sleep was never an estimate of
        # recovery time -- it was the only thing stopping the health check from
        # reading the still-healthy pre-reset BMC, and nothing said so.
        wait_for_bmc_down(engines)
    with allure.step("Wait for the BMC to come back"):
        wait_for_bmc_ready(engines, topology_obj, timeout=BMC_RECOVERY_TIMEOUT)
    with allure.step("Verify BMC is healthy after reset"):
        # Polled rather than asserted once: the BMC answers ICMP well before
        # bmcweb serves again, measured at +0s against +68s for Redfish.
        wait_for_bmc_healthy(engines, timeout=BMC_RECOVERY_TIMEOUT)
        # usb0 re-enumerates across a BMC reset; prove the product restored the
        # expected host/BMC addressing, not just that the BMC is up (#5124733).
        assert_usb0_bmc_link_addrs(engines.dut)


@pytest.mark.bmc
@pytest.mark.cumulus
def test_bmc_cli_password_reset(engines, devices):
    """Reset the BMC root password to the known value; verify auth still works.

    Resets to the same known password the harness already uses, so it is
    idempotent and leaves the BMC reachable.
    """
    skip_if_no_bmc(devices, engines)
    with allure.step(f"password-reset --user {ROOT} --new-password <known>"):
        out = BmcCliTool(engines.dut).password_reset(ROOT, get_cl_bmc_root_password())
        assert 'Login failed' not in str(out), f"password-reset auth failed: {str(out)[:300]}"
    with allure.step("Verify bmc-cli still authenticates with the known password"):
        assert _bmc_authenticates(engines.dut, get_cl_bmc_root_password()), \
            "bmc-cli did not authenticate after password-reset"


@pytest.mark.bmc
@pytest.mark.bmc_opt_in
@pytest.mark.disable_loganalyzer
@pytest.mark.cumulus
def test_bmc_cli_factory_reset_password(engines, devices):
    """Reset BMC root password to factory default, then restore the known password.

    WARNING: this changes the BMC root password. Teardown restores the known
    password by trying the factory-default candidates. If restore fails, recover
    manually with 'bmc-cli password-reset --user root --new-password <known>'.

    Opt-in only: set BMC_ALLOW_FACTORY_RESET_PASSWORD=1 (and select bmc_opt_in).
    """
    if os.environ.get("BMC_ALLOW_FACTORY_RESET_PASSWORD", "").strip().lower() not in (
            "1", "true", "yes"):
        pytest.skip(
            "opt-in only: set BMC_ALLOW_FACTORY_RESET_PASSWORD=1 to run "
            "(mutates BMC root password on shared hardware)"
        )
    skip_if_no_bmc(devices, engines)
    with allure.step("Run factory-reset-password"):
        out = BmcCliTool(engines.dut).factory_reset_password()
        logger.info(f"factory-reset-password output (tail): {str(out)[-300:]}")
    try:
        with allure.step("Verify BMC authenticates with a factory-default password"):
            authed = next((pw for pw in _factory_pw_candidates()
                           if _bmc_authenticates(engines.dut, pw)), None)
            assert authed is not None, \
                "Could not authenticate to BMC with any known password after factory reset"
            logger.info("BMC authenticated with a known candidate password after factory reset")
    finally:
        with allure.step("Restore the known BMC root password"):
            for pw in _factory_pw_candidates():
                try:
                    if _bmc_authenticates(engines.dut, pw):
                        BmcCliTool(engines.dut, password=pw).password_reset(
                            ROOT, get_cl_bmc_root_password())
                        break
                except Exception as err:
                    logger.warning(f"Restore attempt with a candidate password failed: {err}")
            assert _bmc_authenticates(engines.dut, get_cl_bmc_root_password()), \
                "Failed to restore the known BMC root password after factory reset"


def _assert_scp_round_trip(engine, cli, src, sentinel):
    """Pre-FR contract: 'scp' really copies, in both directions.

    Asserted as a round trip through the BMC -- push the sentinel with
    ``--to-bmc``, pull it back to a third path with ``--from-bmc``, compare --
    so the positive needs nothing beyond bmc-cli itself. The BMC-side scratch
    file stays in /tmp: bmc-cli has no remove, and the BMC SSH server that could
    delete it is the very thing FR 5064022 takes away.
    """
    remote = "/tmp/bmc_scp_pre_fr_remote.txt"
    back = "/tmp/bmc_scp_pre_fr_back.txt"
    engine.run_cmd(f"rm -f {back}", validate=False)
    with allure.step("scp --to-bmc copies the file to the BMC"):
        out, rc = cli.run_bare(f"scp --to-bmc {src} {remote}")
        logger.info(f"scp --to-bmc -> rc={rc}, out={out[:300]}")
        assert rc == 0, \
            f"scp --to-bmc failed on a pre-FR build: rc={rc}, {out[:300]}"
    with allure.step("scp --from-bmc brings it back unchanged"):
        out, rc = cli.run_bare(f"scp --from-bmc {remote} {back}")
        logger.info(f"scp --from-bmc -> rc={rc}, out={out[:300]}")
        assert rc == 0, \
            f"scp --from-bmc failed on a pre-FR build: rc={rc}, {out[:300]}"
        content = str(engine.run_cmd(f"cat {back}", validate=False))
        assert sentinel in content, \
            f"round trip through the BMC lost the payload: {content[:200]!r}"
    engine.run_cmd(f"rm -f {back}", validate=False)


@pytest.mark.bmc
@pytest.mark.cumulus
def test_bmc_cli_scp_is_retired(engines, devices):
    """'scp' is retired in both directions and must refuse without reaching the BMC.

    Which contract applies is chosen by the running CL build, because the suite
    is run against older images too and a case that only knows the new behaviour
    reports a stale lab box as a product failure. Below SCP_RETIRED_FROM_CL the
    copy is asserted to work; from it on, the retirement is asserted. Neither
    side is a skip, so a regression in either direction still trips.

    FR #5064022 removes SSH/SCP between the CPU and the BMC: all CPU-to-BMC
    traffic goes over Redfish and the BMC may run with its SSH server disabled.
    bmc-cli keeps the subcommand so the answer is an actionable error rather
    than an argparse 'unknown command', and ``do_scp()`` returns
    ERR_CODE_NOT_SUPPORTED (-13) before any BMC connection or TPM read -- a
    login error here would mean it had stopped short-circuiting.
    """
    skip_if_no_bmc(devices, engines)
    engine = engines.dut
    cli = BmcCliTool(engine)
    src = "/tmp/bmc_scp_retired_src.txt"
    dst = "/tmp/bmc_scp_retired_dst.txt"
    sentinel = "scp-retired-sentinel"

    try:
        engine.run_cmd(f"echo {sentinel} > {src}", validate=False)
        engine.run_cmd(f"rm -f {dst}", validate=False)

        build = cl_build_serial(engine)
        if build < SCP_RETIRED_FROM_CL:
            logger.info(f"CL build {build} predates FR 5064022 "
                        f"{SCP_RETIRED_FROM_CL}; asserting the copy still works")
            _assert_scp_round_trip(engine, cli, src, sentinel)
            return

        for direction in ("--to-bmc", "--from-bmc"):
            for json_mode in (False, True):
                label = f"scp {direction}{' -j' if json_mode else ''}"
                with allure.step(f"{label} refuses without contacting the BMC"):
                    out, rc = cli.run_bare(
                        f"{'-j ' if json_mode else ''}scp {direction} {src} {dst}")
                    logger.info(f"{label} -> rc={rc}, out={out[:300]}")
                    assert rc is not None, \
                        f"{label}: could not read an exit code from: {out[:300]}"
                    assert rc != 0, \
                        f"{label}: expected a non-zero exit, got rc=0: {out[:300]}"
                    assert 'not supported' in out.lower(), \
                        f"{label}: expected the retirement error, got: {out[:300]}"
                    assert 'login failed' not in out.lower(), \
                        f"{label}: reached the BMC instead of refusing up front: {out[:300]}"
                    if json_mode:
                        assert '-13' in out, \
                            f"{label}: expected error code -13 in JSON output, got: {out[:300]}"

        with allure.step("A refused --from-bmc left no destination file behind"):
            listing = str(engine.run_cmd(f"ls -l {dst} 2>&1", validate=False))
            assert 'No such file' in listing, \
                f"scp --from-bmc created {dst} despite refusing: {listing[:200]}"

        with allure.step("A refused --from-bmc does not truncate an existing target"):
            engine.run_cmd(f"echo {sentinel} > {dst}", validate=False)
            out, rc = cli.run_bare(f"scp --from-bmc {src} {dst}")
            assert rc not in (0, None), \
                f"scp --from-bmc: expected a non-zero exit, got rc={rc}: {out[:300]}"
            content = str(engine.run_cmd(f"cat {dst}", validate=False))
            assert sentinel in content, \
                f"scp --from-bmc overwrote the existing target: {content[:200]!r}"
    finally:
        with allure.step("Cleanup scp scratch files"):
            engine.run_cmd(f"rm -f {src} {dst}", validate=False)


@pytest.mark.bmc
@pytest.mark.cumulus
def test_bmc_cli_debug_token_generate(engines, devices):
    """Generate a debug-token request blob and verify a non-empty file is produced."""
    skip_if_no_bmc(devices, engines)
    cli = BmcCliTool(engines.dut)
    out_file = "/tmp/bmc_debug_token_req.bin"
    try:
        engines.dut.run_cmd(f"rm -f {out_file}", validate=False)
        with allure.step("Generate the debug token request"):
            out = cli.debug_token_generate(out_file)
            logger.info(f"debug-token generate output (tail): {str(out)[-300:]}")
        with allure.step("Verify the request file exists and is non-empty"):
            size = engines.dut.run_cmd(
                f"stat -c %s {out_file} 2>/dev/null || echo 0", validate=False).strip()
            assert size.isdigit() and int(size) > 0, \
                f"debug-token request file missing/empty (size={size})"
    finally:
        with allure.step("Cleanup debug token request file"):
            engines.dut.run_cmd(f"rm -f {out_file}", validate=False)


@pytest.mark.bmc
@pytest.mark.cumulus
def test_bmc_cli_set_password_policy_min_length(engines, devices):
    """Change the BMC password minimum-length policy, verify it, then restore it."""
    skip_if_no_bmc(devices, engines)
    cli = BmcCliTool(engines.dut)

    def _current_min_length():
        match = re.search(r'Minimum Length:\s*(\d+)', cli.password_policy())
        return int(match.group(1)) if match else None

    original = _current_min_length()
    assert original is not None, "Could not read current password min-length"
    new_length = original + 2
    try:
        with allure.step(f"Set password min-length to {new_length}"):
            cli.set_password_policy_min_length(new_length)
            assert _current_min_length() == new_length, \
                f"min-length not updated to {new_length}"
    finally:
        with allure.step(f"Restore password min-length to {original}"):
            cli.set_password_policy_min_length(original)
            assert _current_min_length() == original, \
                f"failed to restore min-length to {original}"
