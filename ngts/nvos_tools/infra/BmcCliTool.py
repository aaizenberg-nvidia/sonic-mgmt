import json
import logging
import os
import re
import shlex
import tempfile
import uuid

from devts.infra.tools.connection_tools.linux_ssh_engine import LinuxSshEngine
from ngts.nvos_tools.infra.usb0_addressing import ACTIVE_PROFILE
from ngts.tests_nvos.general.security.bmc.bmc_creds.constants import ROOT, get_cl_bmc_root_password
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger()

BMC_CLI = "/usr/cumulus/bin/bmc-cli"

# Authoritative CL/SPC6 usb0 address pair (product ``bmc-usb.intf``), taken from
# the addressing profile selected by CL_BMC_USB0_PROFILE (default ``legacy``) —
# Redmine #5124733. Keep these names here so every caller (BmcTool, bmc-cli host,
# product-path assert, negative tests) follows the profile in one place.
#
# Two forms of the CPU-side address: BMC_USB_IP keeps the prefix
# (169.254.0.2/24), which is the form an address is configured with and the only
# form that also proves the mask; BMC_USB_IP_BARE drops it (169.254.0.2) for
# substring matches against command output such as ``ip -4 addr show usb0``.
# #5124733 changes the prefix (/24 -> /30) as well as the address, so a check
# that matches only the bare form would pass on a wrong mask.
BMC_USB_IP = ACTIVE_PROFILE.host_cidr       # NOS/CPU side of usb0, with prefix
BMC_USB_IP_BARE = ACTIVE_PROFILE.host_bare  # same address, prefix stripped
BMC_IP = ACTIVE_PROFILE.bmc_ip              # BMC side of usb0 (bmc-cli --host default)
BMC_CLI_HOST = BMC_IP

# bmc-cli reset failures that used to be swallowed because _run_reset
# ran with validate=False and callers ignored the output.
_RESET_FAIL_MARKERS = (
    "login failed",
    "failed with error code",
    "error code:",
    "tpm password unavailable",
)

# Redact --password / --new-password values in Allure titles and logs.
_SECRET_FLAG_RE = re.compile(
    r"(--(?:password|new-password)\s+)('(?:\\'|[^'])*'|\"(?:\\\"|[^\"])*\"|\S+)",
    re.IGNORECASE,
)


def _redact_secrets(cmd: str) -> str:
    """Return a copy of ``cmd`` with password flag values replaced by ******."""
    return _SECRET_FLAG_RE.sub(r"\1'******'", cmd)


class BmcCliTool:
    """Wrapper around the CL-only ``bmc-cli`` binary.

    bmc-cli authenticates to the BMC explicitly via ``--host/--username/--password``
    and is run under ``sudo`` (passwordless on deployed CL setups). The previous
    ``--use-tpm`` mode is not usable here: the TPM-derived password lookup fails on
    these surrogate setups ("TPM password unavailable: Failed to communicate with
    TPM" -> "Login failed -6"), and bmc-cli also requires root for ``/etc/cumulus/bmc.conf``.
    """

    def __init__(self, engine: LinuxSshEngine, host: str = BMC_CLI_HOST,
                 username: str = ROOT, password: str | None = None):
        # Password is optional at construct time so credential-free paths
        # (``reset`` via ``sudo su -c``) work with no BMC password in the environment.
        # Authed commands resolve it lazily on first use via ``_ensure_auth``.
        self.engine = engine
        self._host = host
        self._username = username
        self._password = password  # may stay None until an authed command runs
        self._auth = None
        self._auth_redacted = None

    def _ensure_auth(self):
        """Materialize ``--host/--username/--password`` flags on first authed use."""
        if self._auth is not None:
            return
        if self._password is None:
            self._password = get_cl_bmc_root_password()
        # shlex.quote every value so passwords containing quotes/spaces/shell
        # metacharacters can never break tokenization or the command syntax.
        self._auth = (
            f"--host {shlex.quote(self._host)} "
            f"--username {shlex.quote(self._username)} "
            f"--password {shlex.quote(self._password)}"
        )
        self._auth_redacted = (
            f"--host {shlex.quote(self._host)} "
            f"--username {shlex.quote(self._username)} "
            f"--password '******'"
        )

    def _step_title(self, prefix: str, cmd: str) -> str:
        # Replace the exact auth substring first: this reliably scrubs the root
        # password even when shlex.quote wraps it in a way the regex cannot fully
        # match (e.g. passwords containing single quotes). ``_redact_secrets`` is
        # then a best-effort second pass for any other ``--password`` occurrence.
        redacted = cmd
        if self._auth is not None:
            redacted = redacted.replace(self._auth, self._auth_redacted)
        return f"{prefix}: {_redact_secrets(redacted)}"

    @staticmethod
    def _format_flags(kwargs) -> str:
        """Render kwargs as bmc-cli flags.

        ``True`` renders as a valueless switch (``--force``); ``None``/``False``
        are dropped; anything else renders as ``--key <quoted-value>``.
        """
        parts = []
        for key, value in kwargs.items():
            if value is None or value is False:
                continue
            if value is True:
                parts.append(f"--{key}")
            else:
                parts.append(f"--{key} {shlex.quote(str(value))}")
        return " ".join(parts)

    def run(self, subcmd, validate=False, *, ssh_timeout: int | None = None, **kwargs):
        """Run a bmc-cli subcommand and return its output.

        ``validate`` is False by default and passed explicitly rather than left
        to the engine: the negative tests drive bmc-cli into its error paths and
        assert on the returned text, which only works while a non-zero exit
        returns instead of raising. Pass True to make failures raise.

        ``ssh_timeout`` bounds the devts SSH read, which is a different thing from
        any ``--timeout`` flag bmc-cli itself takes: that flag only ever reaches the
        remote tool, so a transport that wedges before bmc-cli can answer leaves the
        engine waiting on its own default (10000s, and ``retry_run=True`` means up to
        three of those). Keyword-only so it cannot be mistaken for a bmc-cli flag, and
        opt-in rather than defaulted because ``collect-all``, ``sensor-dump`` and the
        attestation subcommands are legitimately slow -- a blanket cap would make
        working calls flaky.
        """
        self._ensure_auth()
        flags = self._format_flags(kwargs)
        cmd = f"sudo {BMC_CLI} {self._auth} {subcmd} {flags}".strip()
        run_kwargs = {} if ssh_timeout is None else {"timeout": ssh_timeout}
        with allure.step(self._step_title("bmc-cli", cmd)):
            return self.engine.run_cmd(cmd, validate=validate, **run_kwargs)

    def build_cmd(self, subcmd) -> str:
        """Return the full ``bmc-cli ... <subcmd>`` string without executing it.

        Use for fire-and-forget commands that drop the SSH session (e.g. a host
        ``reset cpu-reset`` / ``reset power-cycle`` issued from the CPU), where the
        caller must run it via a disconnect-tolerant runner such as
        ``DutUtilsTool.run_cmd_with_disconnect``.

        ``reset`` actions are run under a real root shell (``sudo su -c``): see
        :meth:`_reset_cmd` -- running bmc-cli as the ``cumulus`` user (even with
        ``sudo`` on the single command) or with explicit ``--host/--username/
        --password`` prints "Performing ..." but does NOT actually reset the host;
        a root shell uses the local TPM auth path that performs the reset.
        """
        if subcmd.strip().startswith("reset "):
            return self._reset_cmd(subcmd.strip().split(None, 1)[1])
        self._ensure_auth()
        return f"sudo {BMC_CLI} {self._auth} {subcmd}".strip()

    @staticmethod
    def _reset_cmd(reset_type) -> str:
        """Build a bmc-cli reset command that runs under a real root shell.

        Reset actions must run as root via ``sudo su`` -- running bmc-cli as the
        ``cumulus`` user fails TPM auth ("Login failed -6"), and running it with
        ``sudo`` + explicit ``--host/--username/--password`` prints "Performing
        <type>..." but does NOT actually reset the host. A root shell (``sudo su``)
        uses the local TPM path that performs the reset.
        """
        # Quote reset_type so values with spaces/metacharacters stay one argv token
        # inside the root shell.
        return f"sudo su -c {shlex.quote(f'{BMC_CLI} reset {reset_type}')}"

    def _run_reset(self, reset_type) -> str:
        """Run a bmc-cli reset and fail if the CLI did not accept it.

        Host SSH stays up for ``bmc-reset`` (the BMC reboots, not the CPU).
        ``$?`` must not appear on the Netmiko command string — the parser
        treats it as a regex and drops the ``echo __RC=`` line (suite
        20260831: ``Performing bmc-reset...`` with rc=None). Write rc to a
        file from a small script, then cat it. Host resets (cpu-reset /
        power-cycle) must not use this path — they drop SSH and go through
        ``build_cmd``.
        """
        cmd = self._reset_cmd(reset_type)
        # One token for all three artifacts of a reset: two resets overlapping on
        # a DUT would otherwise share the rc/out files and report each other's
        # result, which reads a failed reset as rc=0.
        token = uuid.uuid4().hex
        rc_file = f"/tmp/ngts_bmc_reset_{token}.rc"
        out_file = f"/tmp/ngts_bmc_reset_{token}.out"
        name = f"ngts_bmc_reset_{token}.sh"
        remote = f"/tmp/{name}"
        body = (
            "#!/bin/bash\n"
            f"{cmd} >{shlex.quote(out_file)} 2>&1\n"
            f"echo $? >{shlex.quote(rc_file)}\n"
        )
        local = None
        try:
            with tempfile.NamedTemporaryFile(
                    "w", delete=False, encoding="utf-8") as tmp:
                tmp.write(body)
                local = tmp.name
            os.chmod(local, 0o700)
            self.engine.copy_file(
                source_file=local, file_system="/tmp",
                dest_file=name, direction="put")
            with allure.step(self._step_title("bmc-cli reset (root shell)", cmd)):
                self.engine.run_cmd(
                    f"sudo bash {shlex.quote(remote)}", validate=False)
                raw = self.engine.run_cmd(
                    f"cat {shlex.quote(out_file)}", validate=False) or ""
                rc_raw = self.engine.run_cmd(
                    f"cat {shlex.quote(rc_file)}", validate=False) or ""
        finally:
            if local:
                try:
                    os.unlink(local)
                except OSError:
                    pass
            self.engine.run_cmd(
                f"sudo rm -f {shlex.quote(remote)} "
                f"{shlex.quote(out_file)} {shlex.quote(rc_file)}",
                validate=False)
        match = re.search(r"(\d+)", rc_raw)
        rc = int(match.group(1)) if match else None
        fail_hits = [m for m in _RESET_FAIL_MARKERS if m in raw.lower()]
        if rc != 0 or fail_hits:
            raise RuntimeError(
                f"bmc-cli reset {reset_type} failed "
                f"(rc={rc}, markers={fail_hits}): {raw[:400]!r}"
            )
        return raw

    def run_bare(self, args) -> tuple:
        """Run bmc-cli with NO host/credential arguments; return (output, rc).

        ``run()`` materialises ``--host/--username/--password`` via
        ``_ensure_auth()``, so it cannot be used to prove that a subcommand
        refuses *before* reaching the BMC: the credentials would hand the CLI a
        way to connect and the assertion would no longer mean anything. This
        deliberately skips ``_ensure_auth()``.

        rc comes back through a file for the same reason as ``_run_reset``:
        ``$?`` on the Netmiko command string is treated as a regex, which drops
        the ``echo`` line from the output and yields rc=None.
        """
        token = uuid.uuid4().hex
        rc_file = f"/tmp/ngts_bmc_bare_{token}.rc"
        out_file = f"/tmp/ngts_bmc_bare_{token}.out"
        name = f"ngts_bmc_bare_{token}.sh"
        remote = f"/tmp/{name}"
        body = (
            "#!/bin/bash\n"
            f"{BMC_CLI} {args} >{shlex.quote(out_file)} 2>&1\n"
            f"echo $? >{shlex.quote(rc_file)}\n"
        )
        local = None
        try:
            with tempfile.NamedTemporaryFile(
                    "w", delete=False, encoding="utf-8") as tmp:
                tmp.write(body)
                local = tmp.name
            os.chmod(local, 0o700)
            self.engine.copy_file(
                source_file=local, file_system="/tmp",
                dest_file=name, direction="put")
            with allure.step(self._step_title(
                    "bmc-cli (no credentials)", f"{BMC_CLI} {args}")):
                self.engine.run_cmd(
                    f"sudo bash {shlex.quote(remote)}", validate=False)
                raw = self.engine.run_cmd(
                    f"cat {shlex.quote(out_file)}", validate=False) or ""
                rc_raw = self.engine.run_cmd(
                    f"cat {shlex.quote(rc_file)}", validate=False) or ""
        finally:
            if local:
                try:
                    os.unlink(local)
                except OSError:
                    pass
            self.engine.run_cmd(
                f"sudo rm -f {shlex.quote(remote)} "
                f"{shlex.quote(out_file)} {shlex.quote(rc_file)}",
                validate=False)
        match = re.search(r"(\d+)", rc_raw)
        return str(raw), (int(match.group(1)) if match else None)

    def _run_json(self, subcmd, **kwargs):
        self._ensure_auth()
        cmd = f"sudo {BMC_CLI} {self._auth} -j {subcmd}"
        flags = self._format_flags(kwargs)
        if flags:
            cmd += f" {flags}"
        with allure.step(self._step_title("bmc-cli (json)", cmd)):
            raw = self.engine.run_cmd(cmd)
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError) as err:
            snippet = (raw or '')[:300]
            raise RuntimeError(
                f"bmc-cli '{subcmd}' returned non-JSON ({type(err).__name__}: {err}). "
                f"raw[:300]={snippet!r}"
            ) from err

    def system_info(self) -> dict:
        return self._run_json("info")

    def thermal_status(self) -> dict:
        return self._run_json("thermal")

    def power_status(self) -> dict:
        return self._run_json("power")

    def firmware_list(self, component: str = "MGX_FW_BMC_0") -> dict:
        # 'firmware-detail' requires a positional component id (e.g. MGX_FW_BMC_0,
        # CPLD_0). Without it bmc-cli errors with "the following arguments are required".
        return self._run_json(f"firmware-detail {component}")

    def firmware(self, file, force=False) -> str:
        """Update BMC firmware from a local image file on the switch.

        ``bmc-cli firmware --file <path> [--force]`` stages a firmware image;
        follow with :meth:`reset_bmc` to activate it. ``force`` allows installing
        an older or equal version (mirrors the NVUE 'force' flag).
        """
        return self.run("firmware", file=file, force=force)

    def firmware_update(self, file, targets="MGX_FW_BMC_0", force=False) -> str:
        """Targeted firmware update: ``firmware-update-multipart --file <f> --targets <ids>``.

        ``--targets`` is mandatory (comma-separated Redfish component IDs, e.g.
        ``MGX_FW_BMC_0,MGX_FW_ERoT_BMC_0``); omitting it makes bmc-cli error out.
        """
        return self.run("firmware-update-multipart", file=file, targets=targets, force=force)

    def reset_bmc(self) -> str:
        return self._run_reset("bmc-reset")

    def password_policy(self) -> str:
        return self.run("password-policy")

    def set_password_policy_min_length(self, length: int) -> str:
        return self.run("password-policy", **{"set-min-length": length})

    def debug_token_generate(self, output) -> str:
        """Generate a debug-token (CRDT) request blob: ``debug-token generate --output <file>``."""
        return self.run("debug-token generate", output=output)

    def scp_to_bmc(self, local, remote) -> str:
        """``bmc-cli scp --to-bmc <local> <remote>``, retired.

        Returns the not-supported error without contacting the BMC. Kept so the
        retirement itself can be asserted.
        """
        return self.run(
            f"scp --to-bmc {shlex.quote(str(local))} {shlex.quote(str(remote))}"
        )

    def scp_from_bmc(self, remote, local) -> str:
        """``bmc-cli scp --from-bmc <remote> <local>``, retired.

        Returns the not-supported error without contacting the BMC. Kept so the
        retirement itself can be asserted.
        """
        return self.run(
            f"scp --from-bmc {shlex.quote(str(remote))} {shlex.quote(str(local))}"
        )

    def collect_all(self, output) -> str:
        return self.run("collect-all", output=output)

    def sensor_dump(self, output=None) -> str:
        return self.run("sensor-dump", output=output)

    def eeprom(self, component: str = "") -> str:
        """List EEPROM components, or show one: ``bmc-cli eeprom [<component>]``."""
        return self.run(f"eeprom {component}".strip())

    def boot_policy(self) -> str:
        return self.run("boot-policy")

    def erot_status(self, component: str = "MGX_ERoT_BMC_0") -> str:
        return self.run(f"erot status {component}")

    def attestation_list(self) -> str:
        return self.run("attestation list")

    def debug_token_status(self) -> str:
        return self.run("debug-token status")

    def ping(self, timeout: int = 30) -> str:
        return self.run("ping", timeout=timeout)

    def password_reset(self, user: str = ROOT, new_password: str | None = None) -> str:
        # Real bmc-cli signature: 'password-reset --user <u> --new-password <pw>'.
        # '--new-password' is mandatory (the hyphenated flag can't be passed via **kwargs).
        self._ensure_auth()
        if new_password is None:
            new_password = get_cl_bmc_root_password()
        cmd = (f"sudo {BMC_CLI} {self._auth} password-reset "
               f"--user {shlex.quote(user)} --new-password {shlex.quote(new_password)}")
        # Build the Allure/log title from an explicitly redacted command so the
        # new password is never emitted, regardless of its characters.
        redacted_cmd = (f"sudo {BMC_CLI} {self._auth} password-reset "
                        f"--user {shlex.quote(user)} --new-password '******'")
        with allure.step(self._step_title("bmc-cli", redacted_cmd)):
            return self.engine.run_cmd(cmd)

    def factory_reset_password(self) -> str:
        # Resets the BMC root password to the factory default.
        return self.run("factory-reset-password")

    def console_get(self) -> str:
        return self.run("console get")

    def console_set(self, target) -> str:
        return self.run("console set", target=target)

    def inventory(self):
        return self._run_json("inventory")
