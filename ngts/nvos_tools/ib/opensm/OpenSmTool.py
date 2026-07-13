import pytest
import time
import logging

from devts.infra.tools.redmine.redmine_api import is_redmine_issue_active
from ngts.nvos_tools.ib.Ib import Ib
from ngts.nvos_constants.constants_nvos import IbConsts
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.ResultObj import ResultObj
from ngts.tools.test_utils import allure_utils as allure
from retry import retry

logger = logging.getLogger()

OPEN_SM_PATH = "/opt/ufm/opensm/sbin/opensm"  # UFM opensm (used for multiplanar)
DOCA_OPEN_SM_PATH = "opensm"  # Doca opensm - executed directly via Doca, not through UFM
SM_MASTER_OPEN_SM_PATH = "/labhome/juliav/workspace/sm_regression/sources/SM_MASTER/usr/sbin/opensm"  # SM Master path
OPEN_SM_CFG_PATH = "/auto/sw_system_project/NVOS_INFRA/verification/issu/opensm.cfg"
MISSING_HFNM_MESSAGE = "HA and HFNM can't be found in topology"
MISSING_HOST = "Host {} is missing from topology"
GET_OPENSM_CMD = "ps aux | grep opensm"


class OpenSmTool:
    # Class-level configuration for opensm path.
    # Set automatically by the configure_opensm_path fixture in conftest.py
    OPENSM_PATH = SM_MASTER_OPEN_SM_PATH

    # Whether the DUT requires a multiplanar-capable opensm (e.g. Black Mamba, Taipan).
    # Set automatically by the configure_opensm_multiplanar fixture from devices.dut.multi_planar.
    # Callers that explicitly pass multiplanar=True/False to start_open_sm still override this.
    MULTI_PLANAR = False

    @classmethod
    def set_opensm_path(cls, opensm_path):
        """
        Set opensm path for the test session.
        Called by the configure_opensm_path fixture in conftest.py.

        Args:
            opensm_path: The opensm path to use.
        """
        cls.OPENSM_PATH = opensm_path
        is_doca = opensm_path == DOCA_OPEN_SM_PATH
        logging.info(f"OpenSmTool configured: OPENSM_PATH={cls.OPENSM_PATH} (doca={is_doca})")

    @classmethod
    def set_multi_planar(cls, multi_planar):
        """
        Set the multiplanar flag for the test session. On a multiplanar DUT (Black Mamba,
        Taipan) the SM must run from OPEN_SM_PATH (UFM build with multi-plane support).
        Called by the configure_opensm_multiplanar fixture in tests_nvos/conftest.py.
        """
        cls.MULTI_PLANAR = bool(multi_planar)
        logging.info(f"OpenSmTool configured: MULTI_PLANAR={cls.MULTI_PLANAR}")

    @staticmethod
    def start_open_sm(engines=None, multiplanar=None):
        """Start OpenSM."""
        return OpenSmTool.start_open_sm_on_server(engines, multiplanar)

    @staticmethod
    def stop_open_sm(engines=None):
        return OpenSmTool.stop_open_sm_on_server(engines)

    @staticmethod
    def _get_opensm_process_lines(output):
        """Filter ps aux output to get only opensm process lines (excludes grep)."""
        return [line for line in output.split('\n') if 'grep' not in line and line.strip()]

    @staticmethod
    def _resolve_multiplanar(multiplanar):
        """If caller didn't specify, fall back to the class-level setting (DUT-derived)."""
        return OpenSmTool.MULTI_PLANAR if multiplanar is None else bool(multiplanar)

    @staticmethod
    def _ensure_smi2_device(engine, port_name):
        """
        Ensure the smi2 SMI/GSI proxy device exists on the SM host. Multiplanar opensm
        binds to smi2, not directly to mlx5_0; the device does not survive reboots and
        must be (re)created via `rdma dev add` before opensm can come up.

        Returns True if smi2 is present (already or after creation), False on failure.
        """
        output = engine.run_cmd("ibdev2netdev")
        if "smi2" in output:
            return True
        logging.info(f"smi2 SMI device missing on {engine.ip} (parent={port_name}), creating it")
        engine.run_cmd(
            f"/opt/mellanox/iproute2/sbin/rdma dev add smi2 type SMI parent {port_name}")
        # verify
        output = engine.run_cmd("ibdev2netdev")
        if "smi2" not in output:
            logging.error(f"Failed to create smi2 device on {engine.ip}")
            return False
        return True

    @staticmethod
    def start_open_sm_on_server(engines, multiplanar=None):
        """
        Start OpenSM if not running, or restart it when the wrong binary is running for
        the current setup. On a multiplanar DUT (Black Mamba, Taipan) the only
        acceptable binary is OPEN_SM_PATH (UFM multi-plane build); any other running
        opensm is forcibly replaced. Also re-creates the smi2 SMI device if it is
        missing (it does not survive reboots).
        """
        if not hasattr(engines, "hfnm"):
            logging.warning(MISSING_HFNM_MESSAGE)
            return ResultObj(False, MISSING_HFNM_MESSAGE)

        multiplanar = OpenSmTool._resolve_multiplanar(multiplanar)
        is_running, port_name = OpenSmTool.is_sm_running_on_server(engines)

        # Determine desired opensm path. DOCA wins over the multi-plane explicit-binary
        # selection because the DOCA opensm distribution already includes multi-plane
        # support, so on doca_traffic_systems (e.g. Taipan, which is also multi_planar)
        # the DOCA binary handles both multi-plane and non-multi-plane modes. The
        # explicit UFM multi-plane build (OPEN_SM_PATH) is only required on non-DOCA
        # multi-plane DUTs such as Black Mamba.
        if OpenSmTool.OPENSM_PATH == DOCA_OPEN_SM_PATH:
            desired_path = DOCA_OPEN_SM_PATH
        elif multiplanar:
            desired_path = OPEN_SM_PATH
        else:
            desired_path = OpenSmTool.OPENSM_PATH

        if is_running:
            running_path = OpenSmTool.get_running_opensm_path(engines)
            smi2_ok = (not multiplanar) or ("smi2" in engines.hfnm.run_cmd("ibdev2netdev"))
            if running_path == desired_path and smi2_ok:
                logging.info(f"OpenSM already running (path: {running_path}, multiplanar={multiplanar})")
                return ResultObj(True, "OpenSM is already running")
            if running_path != desired_path:
                logging.warning(
                    f"Wrong OpenSM binary running ({running_path}), restarting with {desired_path} "
                    f"(multiplanar={multiplanar})")
            else:
                logging.warning(
                    f"OpenSM running but smi2 SMI device is missing on multiplanar setup, restarting")
            OpenSmTool.stop_open_sm_on_server(engines)
            time.sleep(2)
        else:
            OpenSmTool.stop_open_sm_on_server(engines)

        with allure.step("Get GUID to start OpenSM"):
            output = engines.hfnm.run_cmd("ibdev2netdev")
            if "No space left on device" in output:
                logger.info("Attempting to clean up opensm.log files to free space...")
                engines.hfnm.run_cmd("rm -f /var/log/opensm.log*")
                logger.info("Retrying ibdev2netdev...")
                output = engines.hfnm.run_cmd("ibdev2netdev")

                if "No space left on device" in output:
                    return ResultObj(False, "Failed to cleanup opensm logs")

            if multiplanar and not OpenSmTool._ensure_smi2_device(engines.hfnm, port_name):
                return ResultObj(False, "Failed to create smi2 SMI device required for multiplanar")

            logging.info(f"Using opensm path: {desired_path}")

            output = engines.hfnm.run_cmd("ibstat {}".format(port_name))
            guid = ''
            for line in output.splitlines():
                if "System image GUID" in line:
                    guid = line.split(":")[1]
                    logging.info("GUID: " + guid)
                    break
            if not guid:
                return ResultObj(False, "Failed to find GUID to start OpenSM")

        with allure.step("Start OpenSM"):
            engines.hfnm.run_cmd(f"{desired_path} -F {OPEN_SM_CFG_PATH} -g {guid} -B")

        with allure.step("Verify OpenSM is running"):
            deadline = time.time() + 30
            while time.time() < deadline:
                if OpenSmTool.verify_open_sm_is_running_on_server(engines):
                    return ResultObj(True, "OpenSM started")
                time.sleep(3)
            return ResultObj(False, "Failed to start OpenSM (port did not come Up within 30s)")

    @staticmethod
    def stop_open_sm_on_server(engines):
        try:
            if not hasattr(engines, "hfnm"):
                logging.warning(MISSING_HFNM_MESSAGE)
                return ResultObj(False, MISSING_HFNM_MESSAGE)

            OpenSmTool.stop_open_sm_process_on_engine(engines.hfnm)
            return ResultObj(True, "OpenSM stopped successfully")

        except BaseException as ex:
            logging.error(f"Failed to stop opensm: {ex}")
            return ResultObj(False, f"Failed to stop opensm: {ex}")

    @staticmethod
    def stop_open_sm_on_non_fnm_hosts(engines, hosts_nicknames):
        """
        go over each host nickname - aka ha, hb and so on and kill openSM if its active on it
        """
        try:
            for host_nickname in hosts_nicknames:
                if not hasattr(engines, host_nickname):
                    logging.warning(MISSING_HOST.format(host_nickname))
                    return ResultObj(False, MISSING_HOST.format(host_nickname))

                host_engine = getattr(engines, host_nickname)
                OpenSmTool.stop_open_sm_process_on_engine(host_engine)

        except Exception as ex:
            logging.error(f"Failed to stop opensm with error: {ex}")
            return ResultObj(False, f"Failed to stop opensm: {ex}")
        return ResultObj(True)

    @staticmethod
    def stop_open_sm_process_on_engine(engine):
        """Stop openSM process on given host engine if running."""
        with allure.step(f"Stop opensm on {engine.ip}"):
            output = engine.run_cmd(GET_OPENSM_CMD)
            lines = OpenSmTool._get_opensm_process_lines(output)
            if not lines:
                logging.info(f"No opensm processes on {engine.ip}")
                return

            pids = [line.split()[1] for line in lines if len(line.split()) >= 2]
            if pids:
                engine.run_cmd("sudo kill -9 " + " ".join(pids))
                logging.info(f"Killed SM processes {pids} on {engine.ip}")

    @staticmethod
    def is_sm_running_on_server(engines, host_nickname=None):
        """
        check on fnm host if it runs openSM
        @param host_nickname - allows to override fnm host with another, for example 'ha' nickname
        """
        host_nickname = IbConsts.HFNM if not host_nickname else host_nickname
        with allure.step("Check if OpenSM is running on a server"):
            # check if open sm process is currently running
            output = engines[host_nickname].run_cmd(GET_OPENSM_CMD)
            lines = OpenSmTool._get_opensm_process_lines(output)

            # check if port is up
            output = engines[host_nickname].run_cmd("ibdev2netdev")
            is_up = "(Up)" in output
            port_name = output.split()[0]

            # return OpenSM is running only if open sm process is running and port is up
            is_running = bool(lines) and is_up

            return is_running, port_name

    @staticmethod
    def get_running_opensm_path(engines, host_nickname=None):
        """Detect which OpenSM binary is currently running."""
        host_nickname = IbConsts.HFNM if not host_nickname else host_nickname
        if not hasattr(engines, host_nickname):
            return None

        output = engines[host_nickname].run_cmd(GET_OPENSM_CMD)
        lines = OpenSmTool._get_opensm_process_lines(output)
        if not lines:
            return None

        # Check for known paths, otherwise assume DOCA (no path prefix)
        for path in [SM_MASTER_OPEN_SM_PATH, OPEN_SM_PATH]:
            if path in output:
                return path
        return DOCA_OPEN_SM_PATH

    @staticmethod
    def verify_open_sm_is_running_on_server(engines, host_nickname=None):
        """
        @param host_nickname - allows to override fnm host with another, for example 'ha' nickname
        """
        is_running, port_name = OpenSmTool.is_sm_running_on_server(engines, host_nickname)
        return is_running
