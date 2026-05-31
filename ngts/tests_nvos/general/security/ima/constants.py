"""Constants for the IMA (Integrity Measurement Architecture) test suite."""


class ImaConsts:
    # NVUE field / values
    STATE = 'state'
    ENABLED = 'enabled'
    DISABLED = 'disabled'
    DEFAULT_STATE = DISABLED

    # IMA folds every measurement into TPM PCR 10.
    IMA_PCR = '10'
    # The initrd / initramfs is measured into TPM PCR 9 at boot.
    INITRD_PCR = '9'

    # Boot-time PCRs the TPM event log (IML) replays to. The firmware/TCG event
    # log extends PCRs 0-7; we quote a representative subset (matching the existing
    # TPM tests).
    BOOT_PCRS = '0,4,7'

    # Full attestation quote = boot PCRs + PCR 9 (initrd) + PCR 10 (IMA). One quote
    # covers the whole boot+runtime chain (TCG "Guidance on Integrity Measurements
    # and Event Log Processing" sec 5.4 - see test-plan section 6.2).
    ATTEST_PCRS = '0,4,7,9,10'
    # PCR indices that MUST be present in the generated quote file when IMA is on:
    # PCR 9 (initrd) and PCR 10 (IMA measurements).
    QUOTE_REQUIRED_PCRS = (INITRD_PCR, IMA_PCR)

    # A nonce for the quote (reused from the TPM flow; value is opaque).
    VALID_NONCE = '1234ABCD'

    # ----------------------------------------------------------------------- #
    # IMA policy expectations                                                  #
    # ----------------------------------------------------------------------- #
    # When IMA is disabled, only the kernel's built-in boot policy is active. It
    # measures the kexec kernel image, kernel modules and the policy file - but NOT
    # user-space programs/regular files (no BPRM_CHECK/FILE_CHECK). So a freshly
    # run program is NOT measured even though the log already holds module/kexec
    # entries (e.g. ``.ko`` files). Expected `cat .../ima/policy` output:
    #   measure func=KEXEC_KERNEL_CHECK
    #   measure func=MODULE_CHECK
    #   measure func=POLICY_CHECK
    DISABLED_POLICY_FUNCS = ('KEXEC_KERNEL_CHECK', 'MODULE_CHECK', 'POLICY_CHECK')
    # When IMA is enabled the policy additionally measures user-space code, so at
    # least one of these executable/regular-file hooks must appear (this is what
    # makes a freshly run program get measured).
    ENABLED_POLICY_EXEC_FUNCS = ('BPRM_CHECK', 'MMAP_CHECK', 'FILE_CHECK')

    # A measurement string that must appear in the TPM (TCG) event log: the initrd
    # is measured at boot and shows up as an ``INITRD`` event.
    INITRD_EVENT_MARKER = 'INITRD'

    # Invalid "state" values that must be rejected by `nv set ... ima state`.
    # Mirrors test-plan section 5.1 rows 5-6 (typo / wrong-type / empty).
    INVALID_STATES = ['enable', '1', '0', 'on', 'off', 'true', '""']

    # Remote upload destination (filled by helpers.get_scp_url()).
    REMOTE_PATH = '/tmp'
    MEASUREMENTS_REMOTE_FILENAME = 'ima_measurements'
    QUOTE_REMOTE_FILENAME = 'quotes.json'
    # On-DUT source file name + remote destination name for the TPM event log (IML).
    # Uploaded via `nv action upload system security tpm event_log.bin scp://<dest>`.
    TPM_EVENT_LOG_FILENAME = 'event_log.bin'
    EVENT_LOG_REMOTE_FILENAME = 'event_log.bin'
    REMOTE_SCP_URL = 'scp://{user}:{password}@{ip}' + REMOTE_PATH + '/{filename}'

    # Unreachable SCP destination used for the bad-host error flows.
    # Credentials MUST be embedded: without them scp/ssh blocks on an interactive
    # "password:" prompt and the pexpect run_cmd session hangs forever (it waits for
    # a shell prompt that never returns). The unreachable IP makes the connection time
    # out so NVUE returns a clean "Error: Timed out". This mirrors the proven pattern
    # in unit_tests/test_cmd_execution.py (the bad-scp-url timeout case).
    BAD_SCP_HOST = 'root:12345@1.2.3.4'
    BAD_SCP_URL = f'scp://{BAD_SCP_HOST}/no/such/path/ima_measurements'
    BAD_SCP_URL_EVENT_LOG = f'scp://{BAD_SCP_HOST}/no/such/path/event_log.bin'

    # DUT scratch paths for the binaries we create/tamper during measurement tests.
    PROBE_BINARY_PATH = '/usr/local/bin/ima_probe'
    TAMPER_BINARY_PATH = '/usr/local/bin/ima_tamper_probe'

    # Output substrings used to recognize a refused policy write (read-only policy).
    # 'Device or resource busy' (EBUSY) is the kernel's response when the IMA policy
    # is already loaded and write-once (no CONFIG_IMA_WRITE_POLICY) - i.e. the policy
    # is locked/read-only, which is exactly the property the test asserts.
    POLICY_READ_ONLY_ERRORS = ['Permission denied', 'Operation not permitted',
                               'Read-only file system', 'Invalid argument',
                               'Device or resource busy']

    # NVUE refuses to upload the IMA measurement log while IMA is disabled - the
    # action returns this error instead of producing a (meaningless) empty log:
    #   nv action upload system security ima measurements scp://<dest>
    #   Error: Action failed with the following issue:
    #     IMA is not enabled
    IMA_NOT_ENABLED_ERROR = 'IMA is not enabled'

    # `nv config save` is rejected while a config job (e.g. a just-issued apply) is
    # still running: "Cannot save configuration while a job operation is in progress".
    # The save helper retries until the job clears so the config is actually persisted
    # before a (no-flag) reboot - otherwise the reboot stops on a "configuration
    # modified, but not saved" confirmation prompt and is aborted.
    SAVE_JOB_IN_PROGRESS_MARKER = 'job operation is in progress'
    SAVE_RETRY_TRIES = 24
    SAVE_RETRY_DELAY_SEC = 5


class ImaBootTiming:
    """
    Expected cold-boot durations from the HLD (section 8 of the test plan).

    Values are in seconds. They are intentionally generous ranges because boot
    time varies per platform; the boot-timing test logs the measured value and only
    flags a clear regression beyond the upper bound.
    """
    # ~5 min cold boot with IMA off. Even the disabled path is slower than the
    # original ~2.5 min HLD figure on n6100/rosalind (measured ~275s), so the
    # expected value (and the upper bound below) must sit above that.
    OFF_EXPECTED_SEC = 5.0 * 60
    # ~10 min cold boot with IMA on. Hashing every file at boot is slow and varies a
    # lot per platform: real IMA-on cold boots on n6100/rosalind have been measured
    # at ~600s, so the expected value (and therefore the upper bound below) must sit
    # comfortably above 600s rather than the original optimistic ~5.4 min HLD figure.
    ON_EXPECTED_SEC = 10.0 * 60
    # Allowed slack above the expected value before we flag a timing regression.
    # Upper bounds: IMA-off = 300 * 1.3 = 390s, IMA-on = 600 * 1.3 = 780s.
    TOLERANCE_RATIO = 1.3
