"""
Wrapper around the phy team's ``phy_diag.py`` register-access tool.

``phy_diag.py`` exposes a friendlier view of Mellanox PRM registers than bare
``mlxreg``: enum names instead of bit-masks, single-integer fields instead of
exp/mantissa pairs, and — most importantly — surfaces firmware-side status
codes rather than silently no-op'ing.

Used by the zombie-link injection test to drive PPBMC / PPBMP_BW_LOSS / PTER
on Quantum3 and similar Mellanox switching ASICs.
"""

import logging
import os
import re

from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import PhyDiagConsts
from ngts.nvos_tools.infra.EngineAdapterTool import EngineAdapterTool
from ngts.nvos_tools.infra.FilesTool import FilesTool

logger = logging.getLogger()


class PhyDiagTool:
    """Wrapper around ``/tmp/phy_package/phy_diag/phy_diag.py``.

    The tool path is configurable via :data:`PhyDiagConsts.PHY_DIAG_BIN`.
    All methods accept the ``-d`` device path the caller resolved — typically
    the CR-space variant (``mt54004_pci_cr<N>``) since several BW-loss
    registers aren't reachable via the ``pciconf`` form on Quantum3.
    """

    @staticmethod
    def _run(engine, cmd, validate=False):
        """Run a phy_diag command on the DUT."""
        return EngineAdapterTool.run_cmd(engine, cmd, validate=validate)

    @staticmethod
    def is_available(engine) -> bool:
        """Return True if phy_diag.py is deployed at the expected path.

        "Not there" is the normal state before the first deploy, not a test
        failure -- so drop the negative ResultObj rather than let the autouse
        ``verify_result_objects`` fixture report it as a failed result.
        """
        res = FilesTool.file_exists_sudo(engine, PhyDiagConsts.PHY_DIAG_BIN)
        if not res.result:
            res.ignore_result()
        return res.result

    @staticmethod
    def ensure_deployed(engine, sonic_mgmt_engine=None) -> bool:
        """Make sure phy_diag.py is present on the DUT, deploying it if not.

        ``/auto/mswg`` isn't mounted on NVOS switches, so the DUT can't fetch
        the package itself. The staged tarball (see
        :data:`PhyDiagConsts.PHY_DIAG_SOURCE_TARBALL` for why it isn't read
        straight from the phy team's release dir) is pushed to the DUT and
        unpacked there -- one ~205 MB transfer instead of ~1.3 GB of small
        files. Two push paths, tried in order:

        1. **Local put** (``engine.copy_file``) when the tarball is readable
           from the pytest process itself -- dev-host runs, or a container
           whose bind-mount is live.
        2. **sonic-mgmt player scp** (``sonic_mgmt_engine.upload_file_using_scp``,
           same flow secure_boot uses for its kernel module) when it isn't.
           Inside the MARS ngts docker the ``/auto/sw_system_project``
           bind-mount can be a dead autofs stub even though the path IS in
           COMMON_MOUNTPOINTS -- accessing the path on the *player* (via the
           sonic-mgmt engine) triggers autofs and works, so reachability must
           be checked there, not with ``os.path`` in the test process.

        Idempotent: no-op if the binary already exists. Returns True only if
        phy_diag.py is present *and* actually runs (see
        :meth:`verify_runnable`) -- callers should skip when it returns False.
        """
        if PhyDiagTool.is_available(engine):
            return PhyDiagTool.verify_runnable(engine)

        tarball = PhyDiagConsts.PHY_DIAG_SOURCE_TARBALL
        if os.path.isfile(tarball):
            logger.info(
                f"Deploying phy_package (local put): {tarball} -> "
                f"{engine.ip}:{PhyDiagConsts.PHY_DIAG_DUT_DIR}")
            engine.copy_file(
                source_file=tarball,
                dest_file=os.path.basename(PhyDiagConsts.PHY_DIAG_DUT_TARBALL),
                file_system=os.path.dirname(PhyDiagConsts.PHY_DIAG_DUT_TARBALL),
                direction='put',
            )
        elif sonic_mgmt_engine is not None and PhyDiagConsts.PHY_DIAG_SOURCE_OK in \
                sonic_mgmt_engine.run_cmd(
                    f"test -f {tarball} && echo {PhyDiagConsts.PHY_DIAG_SOURCE_OK}",
                    validate=False):
            logger.info(
                f"Deploying phy_package (scp from sonic-mgmt player): {tarball} -> "
                f"{engine.ip}:{PhyDiagConsts.PHY_DIAG_DUT_DIR}")
            sonic_mgmt_engine.upload_file_using_scp(
                engine.username, engine.password, engine.ip,
                tarball, os.path.dirname(PhyDiagConsts.PHY_DIAG_DUT_TARBALL),
                timeout=PhyDiagConsts.PHY_DIAG_UNTAR_TIMEOUT,
            )
            # scp lands the file under its source basename; align it with the
            # DUT-side name the unpack step expects (no-op when they match).
            landed = (f"{os.path.dirname(PhyDiagConsts.PHY_DIAG_DUT_TARBALL)}/"
                      f"{os.path.basename(tarball)}")
            if landed != PhyDiagConsts.PHY_DIAG_DUT_TARBALL:
                engine.run_cmd(
                    f"sudo mv -f {landed} {PhyDiagConsts.PHY_DIAG_DUT_TARBALL}",
                    validate=False)
        else:
            logger.warning(
                f"phy_diag source tarball not reachable from the test process "
                f"(os.path) nor via the sonic-mgmt engine: {tarball}")
            return False
        # The tarball unpacks to the package root ("./phy_diag/...", etc), so
        # extract into PHY_DIAG_DUT_DIR rather than its parent.
        engine.run_cmd(
            f"sudo mkdir -p {PhyDiagConsts.PHY_DIAG_DUT_DIR} && "
            f"sudo tar xzf {PhyDiagConsts.PHY_DIAG_DUT_TARBALL} "
            f"-C {PhyDiagConsts.PHY_DIAG_DUT_DIR}",
            validate=False,
            timeout=PhyDiagConsts.PHY_DIAG_UNTAR_TIMEOUT,
        )
        engine.run_cmd(f"sudo rm -f {PhyDiagConsts.PHY_DIAG_DUT_TARBALL}", validate=False)
        engine.run_cmd(f"sudo chmod +x {PhyDiagConsts.PHY_DIAG_BIN}", validate=False)
        if not PhyDiagTool.is_available(engine):
            logger.warning("phy_diag.py still missing after unpacking the tarball")
            return False
        return PhyDiagTool.verify_runnable(engine)

    @staticmethod
    def verify_runnable(engine) -> bool:
        """Return True if phy_diag.py can actually execute on the DUT.

        On first use the tool bootstraps its own Python dependencies with
        ``pip install --target <pkg>/.local_packages/py3X`` -- contained
        inside the deployed directory, so the switch's system Python is left
        alone and our :meth:`cleanup_deployed` removes them along with
        everything else.

        Two reasons this is worth checking rather than assuming:

        * the bootstrap needs PyPI reachability from the switch, so on an
          isolated setup the tool is present but unusable -- better to skip
          than to fail later on a confusing ``ModuleNotFoundError``;
        * if that bootstrap is ever interrupted the tool writes a
          ``.local_packages/py3X/.install_failed`` marker and then refuses to
          retry on every subsequent launch, permanently poisoning the
          deployment. ``PHYTOOLS_FORCE_INSTALL=1`` is the documented override,
          so we retry once with it before giving up.
        """
        for force in (False, True):
            # "sudo env VAR=val" rather than "sudo VAR=val": both work on
            # current switch sudo (1.9.x), but the env form is immune to a
            # sudoers policy without setenv. Same form as nvue_performance_clis.
            prefix = "env PHYTOOLS_FORCE_INSTALL=1 " if force else ""
            out = engine.run_cmd(
                f"sudo {prefix}python3 {PhyDiagConsts.PHY_DIAG_BIN} --help "
                f"> /dev/null 2>&1 && echo {PhyDiagConsts.PHY_DIAG_SMOKE_OK}",
                validate=False,
                timeout=PhyDiagConsts.PHY_DIAG_BOOTSTRAP_TIMEOUT,
            )
            if PhyDiagConsts.PHY_DIAG_SMOKE_OK in out:
                return True
            logger.warning(
                f"phy_diag.py not runnable on {engine.ip} "
                f"(force_install={force}); retrying with forced dependency install"
                if not force else
                f"phy_diag.py not runnable on {engine.ip} even after forcing "
                f"its dependency install -- is PyPI reachable from the switch?")
        return False

    @staticmethod
    def cleanup_deployed(engine) -> None:
        """Remove the deployed phy_package from the DUT.

        The unpacked package is ~960 MB in the DUT's ``/tmp`` (on the root
        overlay, not a tmpfs), so leaving it behind eats into the same space
        sysdumps and tech-support archives need. Callers that deployed it
        should always clean up. Best-effort: never raises, so a cleanup
        failure can't mask the test's real verdict.
        """
        logger.info(f"Removing phy_package from {engine.ip}:{PhyDiagConsts.PHY_DIAG_DUT_DIR}")
        try:
            engine.run_cmd(
                f"sudo rm -rf {PhyDiagConsts.PHY_DIAG_DUT_DIR} "
                f"{PhyDiagConsts.PHY_DIAG_DUT_TARBALL}",
                validate=False,
            )
        except Exception as exc:
            logger.warning(f"phy_package cleanup failed (non-fatal): {exc}")

    @staticmethod
    def cr_mst_dev_for(pciconf_mst_dev: str) -> str:
        """Translate a ``mt5xxxx_pciconf<N>`` path to the CR-space variant.

        Some PRM registers (PPBMC / PPBMP_BW_LOSS / PTER) aren't reachable via
        the ``pciconf`` space on Quantum3 — they only respond on ``pci_cr``.
        """
        return pciconf_mst_dev.replace("pciconf", "pci_cr")

    @staticmethod
    def get(engine, mst_dev: str, reg_name: str, indexes: str):
        """Run ``phy_diag.py r -d <mst> -r <REG> -m get -i <indexes>``."""
        cmd = (
            f"sudo {PhyDiagConsts.PHY_DIAG_BIN} r "
            f"-d {mst_dev} -r {reg_name} -m get -i '{indexes}'"
        )
        return PhyDiagTool._run(engine, cmd)

    @staticmethod
    def set(engine, mst_dev: str, reg_name: str, indexes: str, set_params: str = ""):
        """Run ``phy_diag.py r -d <mst> -r <REG> -m set -i <indexes[,set_params]>``.

        ``set_params`` is appended to ``indexes`` only when non-empty — a
        trailing comma in the ``-i`` payload makes phy_diag reject the call.
        """
        payload = f"{indexes},{set_params}" if set_params else indexes
        cmd = (
            f"sudo {PhyDiagConsts.PHY_DIAG_BIN} r "
            f"-d {mst_dev} -r {reg_name} -m set -i '{payload}'"
        )
        return PhyDiagTool._run(engine, cmd)

    # ------------------------------------------------------------------
    # PPBMC — Port Phy BER Monitor Control
    # ------------------------------------------------------------------

    @staticmethod
    def ppbmc_set_monitor_cntl(engine, mst_dev: str, local_port: int,
                               monitor_cntl: int = None):
        """Set the PPBMC ``monitor_cntl`` bitmask on a port.

        Default value (``0x20``) narrows monitoring to the literal
        ``Tx_BW_loss`` monitor type, which classifies a link-down event with
        the local reason code 45 (``BW_LOSS_THRESHOLD_EXCEEDED``).

        Note the FW default ``0x24`` is bits 5+2 and therefore already
        includes Tx_BW_loss -- phy_diag decodes it as
        ``Symbol_FEC_monitor_Tx_BW_loss``. So this set narrows the monitored
        set rather than enabling something that was off.

        Restore with :meth:`ppbmc_restore_defaults` -- the write persists.
        """
        monitor_cntl = monitor_cntl if monitor_cntl is not None else PhyDiagConsts.PPBMC_MONITOR_CNTL_TX_BW_LOSS
        indexes = (
            f"local_port={local_port},port_type=0,lp_msb=0,pnat=0,"
            f"monitor_e_ctrl_ind=0,monitor_cntl={hex(monitor_cntl)}"
        )
        return PhyDiagTool.set(engine, mst_dev, PhyDiagConsts.REG_PPBMC, indexes)

    @staticmethod
    def ppbmc_restore_defaults(engine, mst_dev: str, local_port: int):
        """Revert PPBMC monitor settings on a port to the FW defaults.

        ``clr`` restores monitor_cntl / e / event_ctrl /
        event_generation_cooldown_admin for the selected monitor. Best-effort:
        never raises, so teardown can't mask a test's real verdict.
        """
        indexes = (
            f"local_port={local_port},port_type=0,lp_msb=0,pnat=0,"
            f"monitor_e_ctrl_ind=0,clr={PhyDiagConsts.PPBMC_CLR_RESTORE_DEFAULTS}"
        )
        try:
            return PhyDiagTool.set(engine, mst_dev, PhyDiagConsts.REG_PPBMC, indexes)
        except Exception as exc:
            logger.warning(f"PPBMC restore-defaults failed (non-fatal): {exc}")
            return ""

    # ------------------------------------------------------------------
    # BW-loss monitor — PPBMP_BW_LOSS_MONITOR_PARAMETERS
    # ------------------------------------------------------------------

    @staticmethod
    def ppbmp_bw_loss_set_threshold(engine, mst_dev: str, local_port: int,
                                    monitor_group: int = None,
                                    bw_loss_threshold: int = None):
        """Set the BW-loss alarm threshold for one monitor group on one port.

        The threshold is a percentage in 0.01 units (500 = 5%; ``1`` means
        0.01%, NOT 1%). The default is the measurement-derived
        :data:`PhyDiagConsts.PPBMP_BW_LOSS_THRESHOLD_INJECTION` -- see the
        rationale there. The write also drags ``bw_loss_warning_threshold``
        to the same value and PERSISTS across link flaps, so every caller
        must pair this with :meth:`ppbmp_bw_loss_restore_defaults`.

        ``time_window`` is intentionally not written (``time_window_w_en``
        stays 0, so the FW ignores the field) -- the FW-default window is
        what every validated run used.
        """
        monitor_group = monitor_group if monitor_group is not None else PhyDiagConsts.PPBMP_MONITOR_GROUP_TX_BW_LOSS
        bw_loss_threshold = (bw_loss_threshold if bw_loss_threshold is not None
                             else PhyDiagConsts.PPBMP_BW_LOSS_THRESHOLD_INJECTION)
        indexes = (
            f"local_port={local_port},monitor_group={monitor_group},"
            f"bw_loss_threshold={bw_loss_threshold}"
        )
        return PhyDiagTool.set(
            engine, mst_dev, PhyDiagConsts.REG_PPBMP_BW_LOSS, indexes, set_params="")

    @staticmethod
    def ppbmp_bw_loss_restore_defaults(engine, mst_dev: str, local_port: int,
                                       monitor_group: int = None):
        """Restore a monitor group's BW-loss thresholds to the FW defaults.

        ``clr=1`` is ``set_fw_default_thresholds`` -- it puts
        ``bw_loss_threshold`` / ``bw_loss_warning_threshold`` / ``time_window``
        back to 9500 / 9500 / 1. Mandatory after any
        :meth:`ppbmp_bw_loss_set_threshold`, because those writes survive link
        flaps and would otherwise leave the port's BW-loss monitor crippled
        for every later test on that port. Best-effort: never raises.
        """
        monitor_group = monitor_group if monitor_group is not None else PhyDiagConsts.PPBMP_MONITOR_GROUP_TX_BW_LOSS
        indexes = (
            f"local_port={local_port},monitor_group={monitor_group},"
            f"clr={PhyDiagConsts.PPBMP_CLR_RESTORE_DEFAULTS}"
        )
        try:
            return PhyDiagTool.set(
                engine, mst_dev, PhyDiagConsts.REG_PPBMP_BW_LOSS, indexes, set_params="")
        except Exception as exc:
            logger.warning(f"PPBMP restore-defaults failed (non-fatal): {exc}")
            return ""

    @staticmethod
    def ppbmp_bw_loss_get(engine, mst_dev: str, local_port: int,
                          monitor_group: int = None):
        monitor_group = monitor_group if monitor_group is not None else PhyDiagConsts.PPBMP_MONITOR_GROUP_TX_BW_LOSS
        indexes = f"local_port={local_port},monitor_group={monitor_group}"
        return PhyDiagTool.get(
            engine, mst_dev, PhyDiagConsts.REG_PPBMP_BW_LOSS, indexes)

    # ------------------------------------------------------------------
    # PTER — Port Transmit Errors injection
    # ------------------------------------------------------------------

    @staticmethod
    def pter_clear(engine, mst_dev: str, local_port: int):
        """Reset PTER injection (admin=0). Required before re-arming on
        Quantum3 — fresh arm without a clear gets ``status=2`` from FW.
        """
        indexes = f"local_port={local_port},error_type_admin=0"
        return PhyDiagTool.set(
            engine, mst_dev, PhyDiagConsts.REG_PTER_PHY, indexes, set_params="")

    @staticmethod
    def pter_arm(engine, mst_dev: str, local_port: int,
                 error_type_admin: int = None,
                 ber_mantissa: int = None, ber_exp: int = None,
                 error_injection_time: int = None):
        """Arm a PTER FEC-error injection. Defaults match the verified-working
        recipe on Quantum3 (``Effective_BER`` with ``mantissa=1, exp=4``)."""
        error_type_admin = error_type_admin if error_type_admin is not None else PhyDiagConsts.PTER_ERROR_TYPE_EFFECTIVE_BER
        ber_mantissa = ber_mantissa if ber_mantissa is not None else PhyDiagConsts.PTER_BER_MANTISSA_DEFAULT
        ber_exp = ber_exp if ber_exp is not None else PhyDiagConsts.PTER_BER_EXP_DEFAULT
        error_injection_time = error_injection_time if error_injection_time is not None else PhyDiagConsts.PTER_INJECTION_TIME_MAX
        indexes = (
            f"local_port={local_port},error_type_admin={error_type_admin},"
            f"error_injection_time={error_injection_time},"
            f"ber_mantissa={ber_mantissa},ber_exp={ber_exp}"
        )
        return PhyDiagTool.set(
            engine, mst_dev, PhyDiagConsts.REG_PTER_PHY, indexes, set_params="")

    @staticmethod
    def pter_get(engine, mst_dev: str, local_port: int):
        indexes = f"local_port={local_port}"
        return PhyDiagTool.get(
            engine, mst_dev, PhyDiagConsts.REG_PTER_PHY, indexes)

    @staticmethod
    def is_pter_armed(pter_output: str) -> bool:
        """True iff the firmware armed the injection (the ``error_type_oper``
        row in the PTER response table reads ``1``). phy_diag's table widths
        depend on field-name lengths so we match the row via regex rather than
        an exact whitespace literal. An un-armed response (``oper=0``) means
        the FW silently rejected — engineering-FW-only path."""
        return re.search(PhyDiagConsts.PTER_ARMED_REGEX, pter_output) is not None

    # ------------------------------------------------------------------
    # PDDR — Port Diagnostics Database (read-only)
    # ------------------------------------------------------------------

    @staticmethod
    def pddr_link_down_info(engine, mst_dev: str, local_port: int):
        """Read PDDR page 6 (link-down info) for ``local_reason_opcode``
        and friends. Independent of NVOS — used as a hardware-side
        cross-check on the injection's effect."""
        indexes = (
            f"local_port={local_port},"
            f"page_select={PhyDiagConsts.PDDR_PAGE_LINK_DOWN_INFO}"
        )
        return PhyDiagTool.get(
            engine, mst_dev, PhyDiagConsts.REG_PDDR_LINK_DOWN_INFO, indexes)
