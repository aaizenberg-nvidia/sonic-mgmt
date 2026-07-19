"""
IMA (Integrity Measurement Architecture) tests.

Combines the full IMA suite in one module, covering these test-plan sections:
  * 5.1 ``test_ima_configuration``                      - show / set / unset / invalid-value handling
  * 7.1 ``test_ima_error_flows``                        - IMA-specific bad-input flows
  * 6.1 ``test_ima_disabled_state``                     (Phase A - 0 reboots)
  * 6.2 ``test_ima_enabled_measure_attest_and_tamper``  (Phase B - cold reboot)
  * 8.1 ``test_ima_reboot_persistence_and_boot_timing``

The configuration/error-flow tests run while IMA is off (no reboot needed), and are
parametrized over the full API set (NVUE CLI + OpenAPI) per the plan's
``ApiType.ALL_TYPES`` note. The measurement/reboot/upgrade tests turn IMA on and rely
on cold reboots to activate it.

Note on reboots: every reboot is a cold reboot via ``System.action_reboot``
(``nv action reboot system``). Warm/kexec reboot is intentionally not exercised. The
boot duration is timed with :meth:`OperationTime.save_duration` and compared against
the cold-boot range (``helpers.expected_boot_time``) as a soft regression gate. No
external attestation verifier is used.

Test-plan section 9.1 (factory reset) is covered by the centralized factory-reset
test via the IMA checkers in ``ima_factory_reset.py`` (registered in
``general/security/centralized_tests/factory_reset/test_factory_reset.py``).
Test-plan section 9.2 (upgrade) is covered by the centralized upgrade test via the
IMA checker in ``ima_upgrade.py`` (registered in
``general/security/centralized_tests/upgrade/test_upgrade.py``).
"""
import pytest

from ngts.nvos_constants.constants_nvos import ApiType
from ngts.nvos_tools.cli_coverage.operation_time import OperationTime
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.TpmTool import TpmTool
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.constants import MINUTE
from ngts.tests_nvos.general.security.ima.constants import ImaConsts
from ngts.tests_nvos.general.security.ima.helpers import (set_ima_state, unset_ima_state,
                                                          verify_ima_state, verify_ima_applied_state,
                                                          verify_ima_pending_state, save_config,
                                                          get_scp_url, verify_uploaded_file_not_empty,
                                                          quote_contains_pcrs, check_boot_time, expected_boot_time,
                                                          get_ima_states, build_reboot_params,
                                                          verify_measurement_upload_rejected_when_disabled,
                                                          generate_attestation_quote, upload_attestation_artifacts)
from ngts.tools.test_utils import allure_utils as allure


@pytest.mark.security
@pytest.mark.ima
@pytest.mark.timeout(15 * MINUTE, func_only=True)
@pytest.mark.parametrize("test_api", ApiType.ALL_TYPES)
def test_ima_configuration(test_api, engines, restore_ima_default):
    """
    Verify the IMA on/off setting: read it, set enabled/disabled, reset to default,
    and that bad values are refused while the setting stays unchanged.

    All assertions look at the *applied* state: these steps do not reboot, and an IMA
    state change only takes effect (operational) after a reboot, so ``set``/``unset``
    are observable on the applied value only (the operational/applied split itself is
    covered by ``test_ima_reboot_persistence_and_boot_timing``).

    1. show -> applied default is disabled
    2. set enabled + apply -> applied enabled
    3. unset + apply -> applied disabled (back to default)
    4. set disabled + apply -> applied disabled
    5. set invalid values (typo / wrong-type / empty) -> refused, applied unchanged
    6. set with no value -> refused, applied unchanged
    """
    TestToolkit.tested_api = test_api
    ima = System().security.ima

    with allure.step('applied default value is disabled'):
        verify_ima_applied_state(ImaConsts.DISABLED)

    with allure.step('set enabled and verify applied'):
        set_ima_state(
            ImaConsts.ENABLED,
            expected_apply_message=(
                ImaConsts.APPLY_REBOOT_WARNING if test_api == ApiType.NVUE else ''
            ),
        )
        verify_ima_applied_state(ImaConsts.ENABLED)

    with allure.step('unset (revert to default) and verify applied'):
        unset_ima_state()
        verify_ima_applied_state(ImaConsts.DISABLED)

    with allure.step('set disabled and verify applied'):
        set_ima_state(ImaConsts.DISABLED)
        verify_ima_applied_state(ImaConsts.DISABLED)

    with allure.step('invalid values are refused and do not change the applied state'):
        for invalid_value in ImaConsts.INVALID_STATES:
            with allure.step(f'set state "{invalid_value}" - expect failure'):
                ima.set(ImaConsts.STATE, invalid_value, apply=True).verify_result(should_succeed=False)
                verify_ima_applied_state(ImaConsts.DISABLED)

    with allure.step('"set state" with no value is refused and does not change the applied state'):
        # Issues `nv set system security ima state` (the knob with no value),
        # which must fail with a missing-value error (test-plan 5.1 row 7).
        ima.set(ImaConsts.STATE, apply=True).verify_result(should_succeed=False)
        verify_ima_applied_state(ImaConsts.DISABLED)


@pytest.mark.security
@pytest.mark.ima
@pytest.mark.timeout(15 * MINUTE, func_only=True)
@pytest.mark.parametrize("test_api", ApiType.ALL_TYPES)
def test_ima_error_flows(test_api, engines, remote_engine, ima_tool, restore_ima_default):
    """
    Verify IMA-specific bad input gives a clear error and does not change the setting.

    (TPM quote error cases are already covered by the TPM tests and are not repeated.)

    1. set invalid state -> error; applied state unchanged
    2. upload IMA measurements to a bad SCP host -> clear failure; nothing half-changed
    3. upload IMA measurements while IMA is off -> fails with "IMA is not enabled"
    4. upload TPM event log to a bad SCP host -> clear failure
    5. upload TPM event log while IMA is off -> works (boot log, IMA-independent)
    """
    TestToolkit.tested_api = test_api
    ima = System().security.ima
    tpm = System().security.tpm

    with allure.step('bad state value is rejected and the applied state stays the same'):
        ima.set(ImaConsts.STATE, 'not_a_state', apply=True).verify_result(should_succeed=False)
        verify_ima_applied_state(ImaConsts.DISABLED)

    with allure.step('upload measurements to a bad SCP host fails cleanly'):
        ima.action_upload_measurements(ImaConsts.BAD_SCP_URL).verify_result(should_succeed=False)

    with allure.step('upload measurements while IMA is off fails with "IMA is not enabled"'):
        verify_measurement_upload_rejected_when_disabled(remote_engine, ima=ima)

    with allure.step('upload the TPM event log to a bad SCP host fails cleanly'):
        tpm.action_upload(ImaConsts.TPM_EVENT_LOG_FILENAME,
                          ImaConsts.BAD_SCP_URL_EVENT_LOG).verify_result(should_succeed=False)

    with allure.step('upload the TPM event log while IMA is off still works'):
        # The TPM event log is the firmware/boot (IML) log, so it uploads regardless
        # of IMA state (there is no "empty log" case like the IMA measurement log).
        event_log_url = get_scp_url(remote_engine, ImaConsts.EVENT_LOG_REMOTE_FILENAME)
        tpm.action_upload(ImaConsts.TPM_EVENT_LOG_FILENAME, event_log_url).verify_result()


@pytest.mark.security
@pytest.mark.ima
@pytest.mark.timeout(15 * MINUTE, func_only=True)
def test_ima_disabled_state(random_api, engines, devices, ima_tool, remote_engine):
    """
    When IMA is off: user-space files are not measured (running a fresh program
    does not grow the log), and asking for a PCR 10 quote / uploading the log
    still works without crashing.

    Note: while "disabled", the kernel's built-in boot policy is still active and
    measures the kexec kernel, kernel modules (``.ko``) and the policy file. That
    is expected, so we do NOT require an empty log; instead we baseline the current
    log and prove a freshly run program adds nothing (no BPRM_CHECK while off).

    1. show -> state disabled
    2. policy matches the built-in "disabled" rules; baseline the measurement log
    3. run a fresh program -> no new measurement line appears for it
    4. log count is unchanged -> "off" really took effect
    5. PCR 10 quote while off works / gives a clear message (no crash)
    6. upload the TPM event log while off still works (boot log, IMA-independent)
    """
    TestToolkit.tested_api = random_api

    with allure.step('IMA state is disabled'):
        verify_ima_state(ImaConsts.DISABLED)

    with allure.step('disabled policy matches the built-in rules (kexec/module/policy)'):
        disabled_funcs = ima_tool.get_policy_funcs(dut_engine=engines.dut)
        assert set(disabled_funcs) == set(ImaConsts.DISABLED_POLICY_FUNCS), \
            (f'unexpected disabled IMA policy.\nexpected functions: {sorted(ImaConsts.DISABLED_POLICY_FUNCS)}\n'
             f'actual functions: {sorted(disabled_funcs)}\npolicy:\n{ima_tool.read_policy(dut_engine=engines.dut)}')

    with allure.step('baseline the measurement log (built-in boot measurements are expected)'):
        baseline_count = ima_tool.get_measurement_count()

    try:
        with allure.step('run a fresh program and confirm it is NOT measured'):
            probe = ima_tool.create_and_run_measured_binary(ImaConsts.PROBE_BINARY_PATH)
            assert not ima_tool.is_path_measured(probe), \
                f'{probe} was measured while IMA is disabled'

        with allure.step('log line count is unchanged'):
            assert ima_tool.get_measurement_count() == baseline_count, \
                'measurement log grew while IMA is disabled'
    finally:
        ima_tool.remove_file(ImaConsts.PROBE_BINARY_PATH)

    with allure.step('PCR 10 quote while IMA is off succeeds'):
        # A PCR 10 quote must work even with IMA off (PCR 10 simply reflects the
        # boot-time measurements). The quote command itself is covered by the TPM tests.
        System().security.tpm.action_generate_quote(ImaConsts.IMA_PCR, ImaConsts.VALID_NONCE).verify_result()

    with allure.step('upload the TPM event log while IMA is off still works'):
        # The TPM event log is the firmware/boot (IML) log, so it exists and uploads
        # regardless of IMA state (unlike the IMA measurement log).
        event_log_url = get_scp_url(remote_engine, ImaConsts.EVENT_LOG_REMOTE_FILENAME)
        System().security.tpm.action_upload(
            ImaConsts.TPM_EVENT_LOG_FILENAME, event_log_url).verify_result()


@pytest.mark.security
@pytest.mark.ima
@pytest.mark.timeout(45 * MINUTE, func_only=True)
def test_ima_enabled_measure_attest_and_tamper(random_api, engines, devices, ima_tool, remote_engine,
                                               topology_obj):
    """
    With IMA on, check the policy and the generated/uploaded attestation artifacts,
    and that the measurement log is built fresh per boot (nothing carries over from
    before a reboot).

    No external verifier is used here. Instead we check the NVOS-observable facts:
      * the active policy matches the expected "enabled" rules and is read-only;
      * running a program is measured and the IMA log grows;
      * the generated quote covers PCR 9 (initrd) and PCR 10 (IMA);
      * the uploaded IMA log / TPM event log / quote exist on the remote and are not
        empty, and the TPM event log contains an INITRD measurement;
      * tampering a measured file appends a new, different hash.

    Log-freshness proof (no carry-over across the enable/disable reboots):
      disabled (baseline count) -> enable -> run programs (log grows) -> disable ->
      back to the same baseline count.

    Preconditions handled inside the test: enable IMA -> apply -> save -> cold reboot.
    """
    TestToolkit.tested_api = random_api
    system = System()
    reboot_params = build_reboot_params(devices, topology_obj)
    tpm = system.security.tpm
    ima = system.security.ima
    tpm_tool = TpmTool(engines.dut)
    # Baseline of the disabled measurement-log size; captured in the first step and
    # re-checked in cleanup. Kept here so the finally block never hits a NameError.
    disabled_count_before = None

    try:
        with allure.step('precondition: start disabled and baseline the measurement-log size'):
            verify_ima_state(ImaConsts.DISABLED)
            disabled_count_before = ima_tool.get_measurement_count()

        with allure.step('precondition: enable IMA, save, cold reboot to activate'):
            set_ima_state(ImaConsts.ENABLED)
            save_config(engines)
            system.action_reboot(reboot_params=reboot_params).verify_result()

        with allure.step('1. state survived the reboot -> enabled'):
            verify_ima_state(ImaConsts.ENABLED)

        with allure.step('2. policy matches the enabled rules and is read-only'):
            policy = ima_tool.read_policy(dut_engine=engines.dut)
            assert policy.strip(), 'active IMA policy is empty while IMA is enabled'
            funcs = ima_tool.get_policy_funcs(dut_engine=engines.dut)
            assert set(ImaConsts.DISABLED_POLICY_FUNCS).issubset(funcs), \
                (f'enabled policy is missing built-in rules.\nexpected superset of: '
                 f'{sorted(ImaConsts.DISABLED_POLICY_FUNCS)}\nactual functions: {sorted(funcs)}\npolicy:\n{policy}')
            assert any(func in funcs for func in ImaConsts.ENABLED_POLICY_EXEC_FUNCS), \
                (f'enabled policy does not measure programs/files (expected one of '
                 f'{ImaConsts.ENABLED_POLICY_EXEC_FUNCS}).\nactual functions: {sorted(funcs)}\npolicy:\n{policy}')
            modify_output = ima_tool.try_modify_policy(dut_engine=engines.dut)
            assert any(err in modify_output for err in ImaConsts.POLICY_READ_ONLY_ERRORS), \
                f'policy write was not refused as expected, output:\n{modify_output}'

        with allure.step('3. run a fresh program -> it is measured and the IMA log grows'):
            count_before_probe = ima_tool.get_measurement_count()
            probe = ima_tool.create_and_run_measured_binary(ImaConsts.PROBE_BINARY_PATH)
            assert ima_tool.is_path_measured(probe), f'{probe} was not measured while IMA is enabled'
            enabled_count = ima_tool.get_measurement_count()
            assert enabled_count > count_before_probe, \
                f'IMA log did not grow after running a program ({count_before_probe} -> {enabled_count})'

        with allure.step('4. generate the quote over boot PCRs + PCR 9 (initrd) + PCR 10 (IMA)'):
            generate_attestation_quote(tpm=tpm)

        with allure.step(f'5. quote file covers PCR {ImaConsts.INITRD_PCR} and PCR {ImaConsts.IMA_PCR}'):
            quote_content = tpm_tool.get_quote_file_content()
            assert quote_contains_pcrs(quote_content, ImaConsts.QUOTE_REQUIRED_PCRS), \
                (f'quote file does not reference PCRs {ImaConsts.QUOTE_REQUIRED_PCRS}.\n'
                 f'quote content:\n{quote_content}')

        with allure.step('6. upload IMA log + TPM event log + quote; remote files exist and are not empty'):
            upload_attestation_artifacts(remote_engine, ima=ima, tpm=tpm)
            verify_uploaded_file_not_empty(remote_engine, ImaConsts.MEASUREMENTS_REMOTE_FILENAME)
            verify_uploaded_file_not_empty(remote_engine, ImaConsts.EVENT_LOG_REMOTE_FILENAME)
            verify_uploaded_file_not_empty(remote_engine, ImaConsts.QUOTE_REMOTE_FILENAME)

        with allure.step(f'7. TPM event log contains an {ImaConsts.INITRD_EVENT_MARKER} measurement'):
            assert tpm_tool.event_log_contains(ImaConsts.INITRD_EVENT_MARKER), \
                f'no {ImaConsts.INITRD_EVENT_MARKER} event found in the TPM event log'

        with allure.step('8. remove runtime-only probe, cold reboot; state stays enabled'):
            ima_tool.remove_file(probe)
            system.action_reboot(reboot_params=reboot_params).verify_result()
            verify_ima_state(ImaConsts.ENABLED)

        with allure.step('9. cold reboot resets the in-memory log (removed runtime entry is gone)'):
            # Remove the probe before reboot so it cannot be re-measured during boot.
            # If the old path still appears afterward, the runtime log carried over.
            assert not ima_tool.is_path_measured(probe), \
                'cold reboot kept a pre-reboot runtime measurement - log was not reset'

        with allure.step('10. add more content -> more lines appended'):
            count_before = ima_tool.get_measurement_count()
            ima_tool.create_and_run_measured_binary(ImaConsts.TAMPER_BINARY_PATH)
            assert ima_tool.get_measurement_count() > count_before, \
                'log did not grow after running more programs'

        with allure.step('11. tamper a measured file -> new line with a different hash'):
            hashes_before = ima_tool.get_file_hashes_for_path(ImaConsts.TAMPER_BINARY_PATH)
            ima_tool.tamper_and_run_binary(ImaConsts.TAMPER_BINARY_PATH)
            hashes_after = ima_tool.get_file_hashes_for_path(ImaConsts.TAMPER_BINARY_PATH)
            assert len(hashes_after) > len(hashes_before) and len(set(hashes_after)) > 1, \
                'tampering did not append a new, different hash to the log'
    finally:
        with allure.step('cleanup: restore files, disable IMA, save, cold reboot, verify off'):
            ima_tool.remove_file(ImaConsts.PROBE_BINARY_PATH)
            ima_tool.remove_file(ImaConsts.TAMPER_BINARY_PATH)
            # Explicitly set 'disabled' rather than unset: teardown must deterministically
            # leave IMA off. 'unset' only clears the config node to null and was observed to
            # leave the effective state 'enabled' across a reboot (possible product issue,
            # tracked separately) - explicit disable is the reliable way to restore the box.
            set_ima_state(ImaConsts.DISABLED)
            save_config(engines)
            # A cold reboot clears the tampered entry from the in-memory log.
            system.action_reboot(reboot_params=reboot_params).verify_result()
            verify_ima_state(ImaConsts.DISABLED)

        if disabled_count_before is not None:
            with allure.step('log-freshness: disabled measurement-log size is back to the pre-enable baseline'):
                # Proves nothing measured while enabled (or before the reboot) carried over:
                # disabled -> baseline; enable + run programs -> bigger; disable -> baseline again.
                disabled_count_after = ima_tool.get_measurement_count()
                assert disabled_count_after == disabled_count_before, \
                    (f'disabled IMA log size changed across the enable/disable cycle - log may be persisting '
                     f'pre-reboot entries (before={disabled_count_before}, after={disabled_count_after})')


@pytest.mark.security
@pytest.mark.ima
@pytest.mark.timeout(40 * MINUTE, func_only=True)
def test_ima_reboot_persistence_and_boot_timing(engines, devices, ima_tool, test_name, topology_obj):
    """
    With a single reboot per state, check that the IMA setting and its measuring
    survive a reboot (off then on) and that the boot times match the HLD ranges.

    The IMA-off (disabled) boot runs first - the system boots disabled by default,
    so this validates the fast/baseline path before paying for the much slower
    IMA-on boot.

    1. baseline: ensure IMA disabled and saved
    2. cold reboot + time it -> boot time in the IMA-off range
    3. state disabled and the log has no measured entries
    4. enable IMA, apply, save; before reboot applied=enabled while operational lags
    5. cold reboot + time it -> boot time in the IMA-on range
    6. state enabled and the log has measured entries
    7. disable IMA, apply, save; before reboot applied=disabled while operational lags
    """
    system = System()

    def timed_reboot(operation: str) -> float:
        """
        Cold-reboot via the generic ``System.action_reboot`` and return the timed duration.

        The reboot uses the same parameters as the factory-reset/cluster-reboot tests: a serial
        ``System is ready`` wait bounded by the device's expected reboot duration
        (``devices.dut.expected_operation_durations[devices.dut.reboot_type]``), followed
        by the standard NVOS functional check.
        """
        reboot_params = build_reboot_params(devices, topology_obj)
        with allure.step(f'cold-reboot DUT ({operation})'):
            result, duration = OperationTime.save_duration(
                operation, '', test_name, system.action_reboot, reboot_params=reboot_params)
            result.verify_result()
            return duration

    try:
        with allure.step('1. baseline: ensure IMA disabled and saved'):
            # Assert the applied (saved) value, not the operational one: that is what
            # the upcoming reboot boots from. The operational value may still lag if a
            # prior run left IMA enabled-and-pending; the reboot below clears that.
            set_ima_state(ImaConsts.DISABLED)
            save_config(engines)
            verify_ima_applied_state(ImaConsts.DISABLED)

        with allure.step('2. cold reboot and time it (IMA off)'):
            duration = timed_reboot('reboot ima off')
            check_boot_time(duration, expected_boot_time(ima_enabled=False), 'IMA-off cold')
            ima_tool.log_runtime_diagnostics(dut_engine=engines.dut)

        with allure.step('3. disabled survives the reboot and nothing is measured'):
            verify_ima_state(ImaConsts.DISABLED)
            assert ima_tool.has_no_file_measurements(), 'log grew while IMA is disabled'

        with allure.step('4. enable IMA, apply, save; applied=enabled before reboot'):
            operational_before, _ = get_ima_states()
            set_ima_state(ImaConsts.ENABLED)
            save_config(engines)
            # The change only takes effect after the reboot: applied is already
            # enabled while operational still shows the pre-reboot value.
            verify_ima_pending_state(expected_operational=operational_before,
                                     expected_applied=ImaConsts.ENABLED)

        with allure.step('5. cold reboot and time it (IMA on)'):
            duration = timed_reboot('reboot ima on')
            check_boot_time(duration, expected_boot_time(ima_enabled=True), 'IMA-on cold')
            ima_tool.log_runtime_diagnostics(dut_engine=engines.dut)

        with allure.step('6. enabled + measuring survive the reboot'):
            verify_ima_applied_state(ImaConsts.ENABLED)
            assert not ima_tool.has_no_file_measurements(), 'no measurements found while IMA is enabled'

        with allure.step('7. disable IMA, apply, save; applied=disabled before reboot'):
            # No extra reboot here: just covers the disable transition's pre-reboot
            # split (operational still enabled while applied is already disabled).
            operational_before, _ = get_ima_states()
            set_ima_state(ImaConsts.DISABLED)
            save_config(engines)
            verify_ima_pending_state(expected_operational=operational_before,
                                     expected_applied=ImaConsts.DISABLED)
    finally:
        with allure.step('cleanup: ensure IMA is reverted to default and saved'):
            set_ima_state(ImaConsts.DISABLED)
            save_config(engines)
