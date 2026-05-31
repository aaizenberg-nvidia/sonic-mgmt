import logging
from typing import List

from devts.infra.tools.connection_tools.linux_ssh_engine import LinuxSshEngine
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger(__name__)

# Security filesystem locations exposed by an IMA-enabled kernel.
IMA_SECURITYFS_DIR = '/sys/kernel/security/ima'
IMA_MEASUREMENTS_LOG = f'{IMA_SECURITYFS_DIR}/ascii_runtime_measurements'
# The active IMA policy is exposed under the integrity/ subtree; this is the single
# path used for every policy read/write/diagnostics operation.
IMA_INTEGRITY_POLICY_FILE = '/sys/kernel/security/integrity/ima/policy'

# The very first measurement line of every IMA log is the boot aggregate; its
# path field is the literal label ``boot_aggregate`` (not a filesystem path), so
# it is matched exactly.
BOOT_AGGREGATE = 'boot_aggregate'
# When IMA is enabled the policy file itself is measured (POLICY_CHECK). It is
# infrastructure, not a measured program/library/module file, so it never counts
# as "real" content. It is recognized ONLY by the exact security filesystem policy
# path (:data:`IMA_INTEGRITY_POLICY_FILE`) - never by a "policy" substring or a
# generic basename - so unrelated user-space paths that merely contain "policy"
# (e.g. ``/usr/bin/policy-tool`` or ``/usr/bin/policy``) are never misclassified as
# infrastructure and hidden from the "no user-space measurements" checks.

# The kernel's built-in boot policy (active even when NVOS IMA is "disabled")
# measures the kexec kernel image, kernel modules and the policy file - but NOT
# user-space programs/regular files. Their measurement-log entries therefore
# do NOT indicate that IMA is actively measuring user space, so they are treated
# as infrastructure too. Recognized by the measured file path:
#   * kernel modules end in ``.ko`` (optionally compressed)
#   * the kexec kernel image / initramfs live under /boot (vmlinuz / initramfs / initrd)
_KERNEL_MODULE_SUFFIXES = ('.ko', '.ko.xz', '.ko.gz', '.ko.zst')
_BOOT_IMAGE_MARKERS = ('/boot/', 'vmlinuz', 'initramfs', 'initrd')

PATH_NO_EXIST_ERR = 'No such file or directory'

# A real ELF binary so the kernel measures *this* file on exec (BPRM_CHECK),
# as opposed to a shell script whose interpreter would be measured instead.
_SEED_BINARY = '/usr/bin/true'


class ImaTool:
    """
    Kernel-side helper for IMA tests.

    All checks against the actual measurements are done by reading the kernel
    measurement log at ``/sys/kernel/security/ima/ascii_runtime_measurements``.
    Each log line has the form:

        ``<pcr> <template-hash> <template-name> <file-hash> <file-path>``

    so the file path is the last whitespace-separated field of each line.

    This class only *uses* the security filesystem as a tool to observe/simulate behaviour; it
    never replaces NVUE as the source of truth for the feature state.
    """

    def __init__(self, engine: LinuxSshEngine):
        self.engine = engine

    def _get_engine(self, dut_engine=None):
        """Return the explicitly provided engine, or the engine bound to this helper."""
        return dut_engine or self.engine

    # --------------------------------------------------------------------- #
    # Capability / readiness                                                 #
    # --------------------------------------------------------------------- #
    def is_ima_kernel_supported(self) -> bool:
        """Return True if the running kernel exposes the IMA security filesystem interface."""
        with allure.step('check if IMA security filesystem interface exists'):
            out = self.engine.run_cmd(f'sudo ls {IMA_SECURITYFS_DIR}')
            return bool(out) and PATH_NO_EXIST_ERR not in out

    # --------------------------------------------------------------------- #
    # Measurement log reads                                                  #
    # --------------------------------------------------------------------- #
    def get_measurement_log(self) -> str:
        """Return the raw content of the measurement log (empty string if absent)."""
        with allure.step('read IMA measurement log'):
            out = self.engine.run_cmd(f'sudo cat {IMA_MEASUREMENTS_LOG}')
            if PATH_NO_EXIST_ERR in out:
                return ''
            return out

    def get_measurement_log_lines(self) -> List[str]:
        """Return the non-empty lines of the measurement log."""
        content = self.get_measurement_log()
        return [line for line in content.splitlines() if line.strip()]

    def get_measurement_count(self) -> int:
        """Return the number of measurement entries currently in the log."""
        return len(self.get_measurement_log_lines())

    def get_measured_paths(self) -> List[str]:
        """Return the file paths (last field) of every measurement entry."""
        return [line.split()[-1] for line in self.get_measurement_log_lines() if line.split()]

    def is_path_measured(self, file_path: str) -> bool:
        """Return True if the given file path appears in the measurement log."""
        return file_path in self.get_measured_paths()

    def get_file_hashes_for_path(self, file_path: str) -> List[str]:
        """
        Return the ordered list of file-hash fields recorded for ``file_path``.

        Re-measuring a tampered file appends a new entry with a *different* hash,
        so the size and uniqueness of this list reveal tampering.
        """
        hashes = []
        for line in self.get_measurement_log_lines():
            fields = line.split()
            if fields and fields[-1] == file_path:
                # template is "<pcr> <template-hash> <template-name> <file-hash> <path>"
                hashes.append(fields[3] if len(fields) >= 5 else '')
        return hashes

    @staticmethod
    def _is_builtin_boot_measurement(file_path: str) -> bool:
        """
        Return True if a measured path comes from the kernel's built-in boot policy.

        These entries (``boot_aggregate``, the IMA policy file, kernel modules and
        the kexec kernel image / initramfs) are present even when NVOS IMA is
        "disabled", so they never count as a user-space file measurement.
        """
        if file_path == BOOT_AGGREGATE:
            return True
        # Match the policy file only by its exact security filesystem path - not by a
        # substring or basename - so unrelated user-space paths are never swallowed.
        if file_path == IMA_INTEGRITY_POLICY_FILE:
            return True
        if file_path.endswith(_KERNEL_MODULE_SUFFIXES):
            return True
        return any(marker in file_path for marker in _BOOT_IMAGE_MARKERS)

    def has_no_file_measurements(self) -> bool:
        """
        Return True if the log holds no user-space program/library/regular-file
        measurements.

        When NVOS IMA is off the log either does not exist or contains only the
        kernel's built-in boot measurements (``boot_aggregate``, the IMA policy
        file, kernel modules and the kexec image); all of those mean "IMA is not
        measuring user space". Such entries are therefore ignored; any other
        measured path makes this return False.
        """
        for file_path in self.get_measured_paths():
            if not self._is_builtin_boot_measurement(file_path):
                return False
        return True

    # --------------------------------------------------------------------- #
    # IMA policy                                                             #
    # --------------------------------------------------------------------- #
    def read_policy(self, dut_engine=None) -> str:
        """Return the active IMA policy text (empty string if not exposed)."""
        with allure.step('read active IMA policy'):
            out = self._get_engine(dut_engine).run_cmd(f'sudo cat {IMA_INTEGRITY_POLICY_FILE}')
            if PATH_NO_EXIST_ERR in out:
                return ''
            return out

    def get_policy_funcs(self, dut_engine=None) -> List[str]:
        """
        Return the ``func=`` values of every rule in the active IMA policy.

        For example, the disabled (kernel built-in) policy::

            measure func=KEXEC_KERNEL_CHECK
            measure func=MODULE_CHECK
            measure func=POLICY_CHECK

        yields ``['KEXEC_KERNEL_CHECK', 'MODULE_CHECK', 'POLICY_CHECK']``. Rules
        without a ``func=`` token (if any) are ignored.
        """
        funcs = []
        for line in self.read_policy(dut_engine=dut_engine).splitlines():
            for token in line.split():
                if token.startswith('func='):
                    funcs.append(token.split('=', 1)[1])
        return funcs

    def log_runtime_diagnostics(self, dut_engine=None) -> None:
        """
        Record the IMA measurement-log size and active policy for post-reboot triage.

        Runs the two read-only security filesystem queries used when diagnosing IMA behaviour
        after a reboot and attaches their raw output (to allure and the log):

            * ``wc -l /sys/kernel/security/ima/ascii_runtime_measurements`` - how
              many measurement entries the log currently holds.
            * ``cat /sys/kernel/security/integrity/ima/policy`` - the active policy.

        This is purely informational: it makes no assertions and never fails the
        test, so a missing file is simply captured in the attached output.
        """
        with allure.step('log IMA runtime diagnostics (measurement-log size + policy)'):
            engine = self._get_engine(dut_engine)
            measurements_lines = engine.run_cmd(f'sudo wc -l {IMA_MEASUREMENTS_LOG}')
            allure.attach('IMA measurement-log line count', measurements_lines)
            policy = engine.run_cmd(f'sudo cat {IMA_INTEGRITY_POLICY_FILE}')
            allure.attach(f'IMA policy ({IMA_INTEGRITY_POLICY_FILE})', policy)

    def try_modify_policy(self, rule: str = 'measure func=BPRM_CHECK', dut_engine=None) -> str:
        """
        Attempt to write a rule into the IMA policy and return the command output.

        The policy is read-only in this release, so the expected output contains a
        permission/operation error (the caller asserts on that).
        """
        with allure.step('attempt to modify the read-only IMA policy'):
            return self._get_engine(dut_engine).run_cmd(f"echo '{rule}' | sudo tee -a {IMA_INTEGRITY_POLICY_FILE} 2>&1")

    # --------------------------------------------------------------------- #
    # Generating log activity / tampering                                    #
    # --------------------------------------------------------------------- #
    def create_and_run_measured_binary(self, dut_path: str) -> str:
        """
        Create a fresh executable at ``dut_path`` and run it so IMA measures it.

        A copy of a real ELF binary is used (rather than a script) so the kernel's
        ``BPRM_CHECK`` hook measures this exact file on execution.

        :param dut_path: Absolute path on the DUT where the binary is created.
        :return: ``dut_path`` (for convenience in assertions).
        """
        with allure.step(f'create and execute a fresh measured binary at {dut_path}'):
            self.engine.run_cmd(f'sudo cp {_SEED_BINARY} {dut_path}')
            self.engine.run_cmd(f'sudo chmod 755 {dut_path}')
            self.engine.run_cmd(f'sudo {dut_path}')
        return dut_path

    def tamper_and_run_binary(self, dut_path: str) -> str:
        """
        Modify an already-measured binary on disk and run it again.

        Appending bytes changes the file content, so the next execution makes IMA
        record a new measurement line with a different hash.

        :param dut_path: Absolute path of the previously measured binary.
        :return: ``dut_path``.
        """
        with allure.step(f'tamper measured binary {dut_path} and re-run it'):
            self.engine.run_cmd(f'echo "tamper" | sudo tee -a {dut_path} > /dev/null')
            self.engine.run_cmd(f'sudo chmod 755 {dut_path}')
            self.engine.run_cmd(f'sudo {dut_path}')
        return dut_path

    def remove_file(self, dut_path: str):
        """Best-effort removal of a file created during the test."""
        with allure.step(f'remove {dut_path}'):
            self.engine.run_cmd(f'sudo rm -f {dut_path}')
