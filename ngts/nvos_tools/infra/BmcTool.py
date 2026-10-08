import base64
import json
import logging
import re
import shlex
import time
from typing import Dict, Optional

from retry import retry

from devts.infra.tools.connection_tools.linux_ssh_engine import LinuxSshEngine
from ngts.nvos_constants.constants_nvos import DatabaseConst
from ngts.nvos_tools.Devices.EthDevice import EthSwitch
from ngts.nvos_tools.infra.BmcCliTool import (
    BmcCliTool,
    BMC_IP,
    BMC_USB_IP,
    BMC_USB_IP_BARE,
)
from ngts.nvos_tools.platform.Platform import Platform
from ngts.nvos_tools.cli_coverage.operation_time import OperationTime
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.SecureBootTool import SecureBootTool
from ngts.nvos_tools.infra.CurlTool import CurlTool
from ngts.nvos_tools.infra.Tools import Tools
from ngts.nvos_tools.infra.IpTool import IpTool
from ngts.nvos_tools.infra.TpmTool import TpmTool
from ngts.tests_nvos.constants import PRODUCTION, DEVELOPMENT
from ngts.tests_nvos.general.security.bmc.bmc_creds.constants import ADMIN
from ngts.tools.test_utils import allure_utils as allure
from ngts.nvos_tools.infra.ResultObj import ResultObj
from ngts.tests_nvos.general.security.bmc.bmc_creds.constants import (
    BMC_USER_BACKUP_PASSWORD, ROOT, get_cl_bmc_root_password,
)
from ngts.nvos_constants.constants_nvos import OpenApiReqType
from ngts.nvos_constants.constants_nvos import FansConsts, PlatformConsts, SystemConsts
from ngts.nvos_tools.infra.NvCommand import NvCommand
from ngts.nvos_tools.infra.PacketCaptureTool import NetworkInterfaceUtils
from ngts.nvos_tools.infra.usb0_addressing import log_active_profile, mismatch_hint

logger = logging.getLogger()

# Re-export usb0 address pair from BmcCliTool (single source of truth; see
# BMC_IP / BMC_USB_IP* there). Callers may keep importing these names from BmcTool.


# USB NICs often report operstate "unknown" even when ifup'd and forwarding;
# accept that alongside "up" when address + BMC ping succeed.
USB0_OPERSTATE_OK = frozenset({'up', 'unknown'})


def dut_ping(engine, ip, interface=None, count=1, wait=2):
    """Ping ``ip`` from the DUT. Returns ``(got_reply, raw_output)``.

    Reads the received count rather than testing for "0% packet loss": that
    string is a substring of "100% packet loss", so the membership test reads a
    total failure as success and silently turns a reachability gate into a
    no-op. The raw output comes back too, since a failed ping is only
    diagnosable from its text.

    ``interface`` pins the egress interface (``ping -I``).
    """
    iface = f'-I {interface} ' if interface else ''
    output = engine.run_cmd(f'ping {iface}-c {count} -W {wait} {ip}', validate=False)
    received = re.search(r'(\d+) received', output)
    return bool(received) and int(received.group(1)) >= 1, output


def collect_usb0_link_problems(engine, expect_prefix=False):
    """Collect usb0 <-> BMC link problems; an empty list means the link is healthy.

    Assumes usb0 exists: callers check that separately because the reaction
    differs (non-CL platforms have no usb0 and must no-op).

    ``expect_prefix`` selects which form of the CPU-side address has to appear in
    ``ip -4 addr show usb0``. With it, ``BMC_USB_IP`` (address and prefix) is
    required, which also proves the mask; without it, only ``BMC_USB_IP_BARE``
    (address alone) is required. #5124733 changes the prefix as well as the
    address, so product-path asserts ask for the prefix while the broad pre-test
    gate stays on the looser check.
    """
    problems = []
    usb0_state = engine.run_cmd('cat /sys/class/net/usb0/operstate', validate=False).strip()
    logger.info("usb0 operstate=%s", usb0_state)
    if usb0_state not in USB0_OPERSTATE_OK:
        problems.append(
            "operstate={!r} (expected one of {})".format(
                usb0_state, sorted(USB0_OPERSTATE_OK))
        )

    expected_addr = BMC_USB_IP if expect_prefix else BMC_USB_IP_BARE
    usb0_ip = engine.run_cmd('ip -4 addr show usb0', validate=False)
    if expected_addr not in usb0_ip:
        problems.append(
            f"missing product address {expected_addr} on usb0 "
            "(expected from /etc/network/interfaces.d/bmc-usb.intf); "
            f"ip -4 addr show usb0 returned: {usb0_ip.strip()}")

    # DUT-local ping to BMC over usb0 — not DutUtilsTool.ping_device (local host).
    # usb0 is pinned because the BMC address sits in 169.254.0.0/16, and a stray
    # link-local route on another interface would otherwise let the ping answer
    # off usb0 — so a usb0 that lost its address could still pass here.
    reachable, ping_result = dut_ping(engine, BMC_IP, interface='usb0')
    if not reachable:
        problems.append(f"BMC at {BMC_IP} not reachable over usb0: {ping_result.strip()}")

    return problems


def ensure_usb0_up(engine, strict=True):
    """Check that usb0 has the product BMC USB address and reaches the BMC.

    Does not configure usb0. CL product path (verified on SPC6 / CL >= 5.18.31):
    ``/etc/network/interfaces.d/bmc-usb.intf`` assigns ``BMC_USB_IP``;
    udev ``70-hw-management-bmc.rules`` -> ``hw-management-ifupdown.sh`` ->
    ``ifup usb0``. Tests must not ``nv set`` / ``nv config save`` usb0 (Y10).

    Existence uses ``NetworkInterfaceUtils.interface_exists``. Operstate and
    address have no suitable shared wrappers; DUT->BMC ping stays on
    ``engine.run_cmd`` (``DutUtilsTool.ping_device`` pings from the local host).

    Operstate may be ``up`` or ``unknown`` (common for USB net); ``down`` and
    other values fail. Address presence and BMC reachability remain required.

    :param strict: If True (default), raise ``AssertionError`` when usb0 is
        missing on CL, or when it exists but is down / missing the product
        address / BMC unreachable. If False, log a warning and return (for
        reachability poll / recovery loops). Non-CL platforms without usb0
        still no-op (IB uses a different BMC path).
    """
    if not NetworkInterfaceUtils.interface_exists(engine, 'usb0'):
        msg = (
            "usb0 interface does not exist on this device "
            "(CL product path expects bmc-usb.intf + hw-management ifup)"
        )
        if strict and BmcTool._is_cl_platform():
            raise AssertionError(msg)
        logger.info("%s -- skipping BMC USB check", msg)
        return

    problems = collect_usb0_link_problems(engine)

    if not problems:
        logger.info("usb0 ok with product IP and BMC reachable")
        return

    # The addresses this gate checks come from the active profile too, so it can fail
    # for a profile/DUT mismatch just as easily as for a dead link -- and being the
    # earlier check, it is the message a user actually sees. Carry the same hint.
    msg = (
        "usb0 BMC link check failed (product path should configure usb0 via "
        f"bmc-usb.intf + hw-management ifup — tests do not repair): {'; '.join(problems)}. "
        f"{mismatch_hint()}"
    )
    if strict:
        raise AssertionError(msg)
    logger.warning(msg)


def assert_usb0_bmc_link_addrs(engine, expect_prefix=True):
    """Fail closed unless usb0 carries the profile's addresses and reaches the BMC.

    This is the product-path assertion for #5124733. It is used in the
    steady-state connectivity case and, more importantly, after the events that
    re-enumerate usb0: BMC reset / recover, BMC firmware install, CPU reboot.
    It only reads state and never configures usb0, so a pass means the product
    brought the link up on the expected addresses, not the test.

    The address check includes the prefix by default, unlike the ``ensure_usb0_up``
    pre-test gate, because the FR moves the mask as well as the address. A failure
    names both addressing profiles so a profile/DUT mismatch cannot be mistaken
    for an unreachable BMC.

    Non-CL platforms no-op, same as ``ensure_usb0_up``: the host<->BMC usb0 gadget
    is a CL/SPC construct, and IB reaches its BMC another way. Some of the call
    sites (SN12) are shared with NVOS, and the alternative was repeating a
    platform guard at every one of them. Where a usb0 is expected, the assert is
    still fail-closed.
    """
    if not BmcTool._is_cl_platform():
        logger.info("Not a CL platform -- skipping the usb0 addressing assert")
        return

    profile = log_active_profile()
    with allure.step(f"Assert usb0 carries {profile.host_cidr} "
                     f"and reaches BMC {profile.bmc_ip}"):
        assert NetworkInterfaceUtils.interface_exists(engine, 'usb0'), (
            "usb0 interface does not exist — the product path (bmc-usb.intf + "
            f"hw-management ifup) did not create it. {mismatch_hint(profile)}"
        )
        problems = collect_usb0_link_problems(engine, expect_prefix=expect_prefix)
        assert not problems, (
            "usb0 BMC link does not match the expected addressing: "
            f"{'; '.join(problems)}. {mismatch_hint(profile)}"
        )


def read_usb0_ifindex(engine):
    """Return usb0's current kernel ifindex as a string, or None if it is absent.

    Used to prove usb0 actually re-enumerated across a BMC event: the gadget
    comes back as a *new* netdev, so the ifindex changes (observed on SPC6 /
    CL 5.19: ifindex 143 deleted, re-created as 144).
    """
    ifindex = engine.run_cmd('cat /sys/class/net/usb0/ifindex', validate=False).strip()
    return ifindex if ifindex.isdigit() else None


def wait_for_usb0_bmc_link(engine, timeout_sec=180, poll_sec=5, expect_prefix=False,
                           previous_ifindex=None):
    """Wait until usb0 is back with the product address and the BMC answers again.

    This is the CL equivalent of waiting for the NVOS ``BMC status changed ... to
    Ok`` log line, which Cumulus never emits (verified: zero occurrences across
    all rotated syslogs on two SPC6 / CL 5.19 DUTs). On CL the observable signal
    after a BMC reset is usb0 re-enumerating and udev
    ``70-hw-management-bmc.rules`` -> ``hw-management-ifupdown.sh`` -> ``ifup
    usb0`` restoring the address, after which the BMC answers over the link.
    Measured on SPC6 / CL 5.19: unregister -> ``ifup usb0 succeeded`` -> oper up
    in ~50s, so the default timeout leaves a wide margin.

    Reads state only; it never configures usb0, so a pass means the product
    recovered the link, not the test.

    :param previous_ifindex: usb0's ifindex from before the BMC event, as returned
        by ``read_usb0_ifindex``. Required to make this a real wait: the USB
        disconnect lands seconds *after* ``BmcTool.reset`` returns (~11s on SPC6),
        so without it the first poll sees the still-healthy pre-reset link and
        returns immediately. With it, the link only counts as recovered once usb0
        has re-enumerated under a new ifindex.
    :raises AssertionError: if the link is not healthy within ``timeout_sec``.
    """
    started = time.monotonic()
    deadline = started + timeout_sec
    problems = ['usb0 interface did not re-appear']
    while time.monotonic() < deadline:
        if NetworkInterfaceUtils.interface_exists(engine, 'usb0'):
            ifindex = read_usb0_ifindex(engine)
            if previous_ifindex is not None and ifindex == previous_ifindex:
                problems = [
                    f"usb0 still on the pre-event ifindex {previous_ifindex} "
                    "(has not re-enumerated yet)"
                ]
            else:
                problems = collect_usb0_link_problems(engine, expect_prefix=expect_prefix)
                if not problems:
                    logger.info("usb0/BMC link back after %.0fs (ifindex %s -> %s)",
                                time.monotonic() - started, previous_ifindex, ifindex)
                    return
        time.sleep(poll_sec)

    raise AssertionError(
        f"usb0/BMC link did not recover within {timeout_sec}s after the BMC event "
        f"(product path is bmc-usb.intf + hw-management ifup — tests do not repair): "
        f"{'; '.join(problems)}"
    )


class BmcTool:
    BMC_LOCAL_IP = "10.0.1.1"
    BASE_REDFISH_URL = "/redfish/v1/"
    BASE_URL = "https://" + BMC_LOCAL_IP + BASE_REDFISH_URL
    # IB Redfish username only (matches TPM admin path). Prefer get_bmc_credentials()
    # for new code; CL uses ROOT via that helper — do not treat USER_NAME as CL auth.
    USER_NAME = ADMIN
    PLATFORM_COMPONENTS_DICT = dict()
    FW_VERSIONS_JSON_FILE = None

    @classmethod
    def set_fw_versions_json_file(cls, path):
        cls.FW_VERSIONS_JSON_FILE = path

    @staticmethod
    def _is_cl_platform():
        """Check if the current device is a CL Ethernet switch (not IB)."""
        device = TestToolkit.get_device()
        return isinstance(device, EthSwitch)

    @staticmethod
    def get_bmc_credentials(engine: LinuxSshEngine):
        """Return (username, password) for BMC Redfish access.

        On CL platforms, uses root credentials directly.
        On IB/NVOS, uses TPM-derived admin credentials.
        """
        if BmcTool._is_cl_platform():
            return ROOT, get_cl_bmc_root_password()
        return ADMIN, TpmTool(engine).get_bmc_admin_password_from_tpm()

    @staticmethod
    def _get_bmc_password(engine: LinuxSshEngine):
        _, password = BmcTool.get_bmc_credentials(engine)
        return password

    @staticmethod
    def reset(engine: LinuxSshEngine):
        if BmcTool._is_cl_platform():
            ensure_usb0_up(engine)
            response = BmcCliTool(engine).reset_bmc()
            low = (response or "").lower()
            if ("login failed" in low or "failed with error code" in low or
                    "error code:" in low):
                raise Exception(
                    "CL BMC reset failed with the following response:\n" +
                    (response or "")
                )
        else:
            username, password = BmcTool.get_bmc_credentials(engine)
            cmd = (
                "curl -k --max-time 30 -u {} -H "
                '"Content-Type: application/json" -X POST '
                "-d '{{\"ResetType\": \"GracefulRestart\"}}' {}"
                "Managers/BMC_0/Actions/Manager.Reset"
                .format("{}:{}".format(username, password), BmcTool.BASE_URL)
            )
            response = engine.run_cmd(cmd)
            if "The request completed successfully" not in response:
                raise Exception(
                    "Shutdown command failed with the following response:\n" + response
                )

    @staticmethod
    def fetch_and_install_platform_component(
            platform_component, path, name, filename, topology_obj, test_name,
            skip_version_check=False) -> ResultObj:
        with allure.step(f'Fetch {name} image from: {path}'):
            platform_component.action_fetch(path).verify_result()

        with allure.step(f'installing image {name}'):
            return BmcTool.install_fw_image(
                platform_component, test_name, filename, topology_obj, name,
                skip_version_check)

    @staticmethod
    def fetch_and_install_platform_component_without_reboot(
            platform_component, path, name, filename, test_name,
            skip_version_check=False) -> ResultObj:
        with allure.step(f'Fetch {name} image from: {path}'):
            platform_component.action_fetch(path).verify_result()

        with allure.step(f'installing image {name}'):
            return BmcTool.install_fw_image_without_reboot(
                platform_component, test_name, filename, skip_version_check)

    @staticmethod
    def verify_platform_component_version(platform_component, expected_version: str):
        with allure.step(f'Making sure component is now on version {expected_version}'):
            platform_component_output = OutputParsingTool.parse_json_str_to_dictionary(
                platform_component.show()).verify_result()
            output_version = platform_component_output[PlatformConsts.FW_ACTUAL]
            logger.info(f"Found version: {output_version}")

            assert output_version == expected_version, \
                f"firmware is {output_version}, expected {expected_version} after the install"

    @staticmethod
    def verify_cpld_versions(image_details: Dict[str, Dict[str, str]]):
        firmware_shown = OutputParsingTool.parse_json_str_to_dictionary(Platform().firmware.show()).get_returned_value()
        with allure.step('validate cpld firmware versions'):
            for cpld_number, expected_version in image_details.items():
                with allure.independent_step(f"Checking {cpld_number}"):
                    actual_firmware = firmware_shown[cpld_number][PlatformConsts.FW_ACTUAL]
                    assert actual_firmware == expected_version, (
                        "{} version mismatch: Expected {!r}, Got {!r}".format(
                            cpld_number, expected_version, actual_firmware)
                    )

    @staticmethod
    def compare_bmc_version_issu_module(engines, expected_version: str):
        with allure.step(f'Making sure component is now on version {expected_version}'):
            bmc_output = Tools.DatabaseTool.sonic_db_cli_hgetall(engine=engines.dut, asic="",
                                                                 db_name=DatabaseConst.STATE_DB_NAME,
                                                                 table_name='\"SYSTEM_COMPONENTS|BMC\"')

            assert bmc_output == expected_version, \
                f"firmware is {bmc_output}, expected {expected_version} after the install"

    @staticmethod
    def _get_fw_component_version_info(component_name, version):
        fw_path = BmcTool.FW_VERSIONS_JSON_FILE
        if not fw_path:
            logger.info("FW_VERSIONS_JSON_FILE is not set")
            return None, None, None
        with allure.step(f'Read platform components info from json {fw_path}'):
            if not BmcTool.PLATFORM_COMPONENTS_DICT:
                with open(fw_path, 'r') as file:
                    BmcTool.PLATFORM_COMPONENTS_DICT = json.load(file)
            platform_components_dict = BmcTool.PLATFORM_COMPONENTS_DICT
            provisioning = DEVELOPMENT if SecureBootTool.is_dev_system(TestToolkit.get_engine()) else PRODUCTION
            if component_name not in platform_components_dict[provisioning].keys():
                logger.info("Component {} missing from json".format(component_name))
                return None, None, None
            component_image_info = platform_components_dict[provisioning][component_name][version]
            path = component_image_info['path']
            # Derive filename from path if not explicitly provided (e.g., for CPLD components)
            filename = component_image_info.get('filename') or path.rsplit('/', 1)[-1]
            return path, filename, component_image_info['version_name']

    @staticmethod
    def get_fw_component_version_dict(component_name, version):
        device = TestToolkit.get_device()
        fw_path = BmcTool.FW_VERSIONS_JSON_FILE or device.fw_versions_json_file_path
        with allure.step(f'Read platform components info from json {fw_path}'):
            if not BmcTool.PLATFORM_COMPONENTS_DICT:
                with open(fw_path, 'r') as file:
                    BmcTool.PLATFORM_COMPONENTS_DICT = json.load(file)
            platform_components_dict = BmcTool.PLATFORM_COMPONENTS_DICT
            provisioning = DEVELOPMENT if SecureBootTool.is_dev_system(TestToolkit.get_engine()) else PRODUCTION
            component_image_info = platform_components_dict[provisioning][component_name][version]
            return component_image_info

    @staticmethod
    def is_automatic_background_copy_enabled(engine, erot_name: str) -> bool:
        """
        Check if AutomaticBackgroundCopyEnabled is true for the given EROT component.

        Uses Redfish API to query the EROT chassis and check the Oem.Nvidia.AutomaticBackgroundCopyEnabled field.

        :param engine: Engine object to execute commands
        :param erot_name: EROT name (e.g., 'EROT-BMC', 'EROT-CPU')
        :return: True if automatic background copy is enabled, False otherwise
        """
        # Convert EROT name to Redfish chassis name (e.g., 'EROT-BMC' -> 'MGX_ERoT_BMC_0')
        # The format is: EROT-XXX -> MGX_ERoT_XXX_0
        erot_suffix = erot_name.replace('EROT-', '')
        redfish_chassis_name = f"MGX_ERoT_{erot_suffix}_0"
        chassis_path = f"/Chassis/{redfish_chassis_name}"

        with allure.step(f"Checking if AutomaticBackgroundCopyEnabled is true for {erot_name}"):
            username, password = BmcTool.get_bmc_credentials(engine)
            curl_tool = CurlTool(server_host=BmcTool.BMC_LOCAL_IP, username=username, password=password)
            response = curl_tool.run_redfish_command(rest_op='GET', path=chassis_path, dut_engine=engine)

            try:
                response_dict = json.loads(response)
                auto_bg_copy_enabled = (
                    response_dict.get("Oem", {})
                    .get("Nvidia", {})
                    .get("AutomaticBackgroundCopyEnabled", True)
                )
                logger.info(
                    "AutomaticBackgroundCopyEnabled for {}: {}".format(
                        erot_name, auto_bg_copy_enabled)
                )
                return auto_bg_copy_enabled
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning(
                    "Failed to parse Redfish response for {}: {}. "
                    "Assuming background copy is enabled.".format(erot_name, e)
                )
                return True

    @staticmethod
    @retry(AssertionError, tries=14, delay=30)
    def verify_background_copy_completed(platform, erot_name):
        with allure.step(f"Verifying {erot_name} completed background copy"):
            firmware_shown: Dict[str, str] = OutputParsingTool.parse_json_str_to_dictionary(
                platform.firmware.erot_id[erot_name].show()).get_returned_value()
            background_copy_status = firmware_shown[PlatformConsts.FW_BACKGROUND_COPY_STATUS]
            assert background_copy_status.lower() == "Completed".lower(), "Background copy status is not completed"

    @staticmethod
    def verify_background_copy_completed_if_enabled(engine, platform, erot_name):
        """
        Verify background copy is completed only if AutomaticBackgroundCopyEnabled is true.

        If automatic background copy is disabled, this method skips the verification and logs a message.

        :param engine: Engine object to execute commands for Redfish API calls
        :param platform: Platform object for NVUE show commands
        :param erot_name: EROT name (e.g., 'EROT-BMC', 'EROT-CPU')
        """
        if not BmcTool.is_automatic_background_copy_enabled(engine, erot_name):
            with allure.step(
                "Skipping background copy verification - "
                "AutomaticBackgroundCopyEnabled is false for {}".format(erot_name)
            ):
                logger.info(
                    "AutomaticBackgroundCopyEnabled is false for {}, "
                    "skipping background copy verification".format(erot_name)
                )
                return

        BmcTool.verify_background_copy_completed(platform, erot_name)

    @staticmethod
    @retry(AssertionError, tries=3, delay=15)
    def wait_for_cpu_to_detect_bmc(dut_engine: LinuxSshEngine, nv_command: NvCommand):
        bmc_inventory: dict = OutputParsingTool.parse_json_str_to_dictionary(
            nv_command.platform.inventory.show("BMC")).get_returned_value()
        assert bmc_inventory.get(SystemConsts.STATE) == FansConsts.STATE_OK, "BMC is not detected by CPU"

    @staticmethod
    def get_fw_component_version_latest(component_name):
        return BmcTool._get_fw_component_version_info(component_name, "latest")

    @staticmethod
    def get_fw_component_version_previous(component_name):
        return BmcTool._get_fw_component_version_info(component_name, "previous")

    @staticmethod
    def _is_curl_redfish_transport_failure(raw: Optional[str]) -> bool:
        """True when the HTTP fetch could not get a response body (OOB down, no route, timeout, etc.).
        """
        if raw is None or not str(raw).strip():
            return True
        s = str(raw)
        return bool(
            s.strip().startswith("curl:") or
            "Failed to connect" in s or
            "Connection refused" in s or
            "Could not resolve host" in s or
            "Name or service not known" in s or
            "No route to host" in s or
            "Network is unreachable" in s or
            "curl: (28)" in s or
            "Connection timed out" in s or
            "timed out" in s or
            "Couldn't connect" in s
        )

    @staticmethod
    def _redfish_bmc_eth0_fetch_slaac_line(engine, bmc_host: str) -> str:
        """
        On the DUT, one ``sudo python3 -c`` invocation (no bash script). The
        DUT-side script obtains the BMC admin password locally from the TPM via
        ``sonic_platform.bmc.BMC`` (the same lookup ``TpmTool`` uses), then
        issues the Redfish GET via ``urllib.request`` and writes the body to
        ``/tmp/ngts_bmc_eth0.json`` for parsing in the same process. A single
        ``BMC_IP_FETCH:...`` line is printed for the runner to consume. Immune
        to SSH splitting multiline shell; avoids truncated stdout for long
        bodies.

        Credentials never traverse the SSH command line -- not in plain text
        and not in base64. The password is materialized only as an in-memory
        Python string on the DUT and as the ``Authorization`` header on a
        single TLS connection; no curl subprocess argv exposure, no on-disk
        credential file.

        BMC_IP_FETCH markers (kept stable for the caller):
          slaac:<addr>     success; body parsed; SLAAC address extracted
          redfish:<msg>    Redfish app-level error (HTTPError or error body)
          json:<msg>       could not parse JSON body
          empty            zero-length body written to disk
          curl:<text>      transport failure (legacy prefix kept for
                           ``_is_curl_redfish_transport_failure``); urllib
                           transport errors share this prefix with curl errors
          cred:<msg>       could not obtain BMC credentials on the DUT
          parse:<repr>     no marker line found in stdout
        """
        eth0_url = "https://{}/redfish/v1/Managers/BMC_0/EthernetInterfaces/eth0".format(bmc_host)
        # Non-f-string on purpose: an f-string here makes flake8 scan the embedded
        # DUT script and emit false-positive E231/E272 on dict/URL syntax inside it.
        script = """import json, ssl, base64, pathlib
import urllib.request, urllib.error
URL = __URL__
USER = __USER__
OUT = "/tmp/ngts_bmc_eth0.json"
try:
    from sonic_platform.bmc import BMC
    pwd = BMC("10.0.1.1").get_login_password()
except Exception as e:
    print("BMC_IP_FETCH:cred:" + type(e).__name__ + ":" + str(e)[:300].replace(chr(10), " "))
    raise SystemExit(0)
auth = "Basic " + base64.b64encode((USER + ":" + (pwd or "")).encode()).decode()
req = urllib.request.Request(URL, headers={"Authorization": auth})
ctx = ssl._create_unverified_context()
try:
    with urllib.request.urlopen(req, timeout=90, context=ctx) as r:
        body = r.read()
    pathlib.Path(OUT).write_bytes(body)
except urllib.error.HTTPError as e:
    try:
        b = e.read()
    except Exception:
        b = b""
    msg = (b.decode(errors="replace") or e.reason or "")[:1200].replace(chr(10), " ")
    print("BMC_IP_FETCH:redfish:" + msg)
    raise SystemExit(0)
except urllib.error.URLError as e:
    print("BMC_IP_FETCH:curl:" + str(e.reason)[:500].replace(chr(10), " "))
    raise SystemExit(0)
except Exception as e:
    print("BMC_IP_FETCH:curl:" + type(e).__name__ + ":" + str(e)[:500].replace(chr(10), " "))
    raise SystemExit(0)
try:
    if pathlib.Path(OUT).stat().st_size == 0:
        print("BMC_IP_FETCH:empty")
        raise SystemExit(0)
except OSError as e:
    print("BMC_IP_FETCH:curl:" + str(e))
    raise SystemExit(0)
try:
    with open(OUT) as f:
        d = json.load(f)
except json.JSONDecodeError as e:
    print("BMC_IP_FETCH:json:" + str(e))
    raise SystemExit(0)
if isinstance(d, dict) and "error" in d:
    err = d.get("error", d)
    msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
    print("BMC_IP_FETCH:redfish:" + (msg or "")[:1200])
else:
    slaac = next(
        (a["Address"] for a in d.get("IPv6Addresses", []) if a.get("AddressOrigin") in {"SLAAC", "DHCPv6"}),
        None,
    )
    print("BMC_IP_FETCH:ipv6:" + (slaac or ""))
""".replace("__URL__", json.dumps(eth0_url)).replace("__USER__", json.dumps(ADMIN))
        b64 = base64.b64encode(script.encode()).decode("ascii")
        on_dut = "import base64; exec(base64.b64decode(" + repr(b64) + ").decode())"
        out = engine.run_cmd("sudo python3 -c " + shlex.quote(on_dut))
        for line in (out or "").splitlines():
            s = line.strip()
            if s.startswith("BMC_IP_FETCH:"):
                return s
        return "BMC_IP_FETCH:parse:" + repr((out or "")[:400])

    @staticmethod
    def get_bmc_ip_addresses(engines, topology_obj) -> Dict[str, str]:
        """Return BMC OOB addresses: IPv4 from NOGA; IPv6 via Redfish on IB only.

        :return: ``{"IPv4": ..., "IPv6": ...}`` (IPv6 may be ``None``)

        IB/NVOS: IPv6 is fetched on the DUT by ``_redfish_bmc_eth0_fetch_slaac_line``
        (TPM password stays on the DUT — never on the SSH/curl command line). Auth,
        transport, and empty-body failures raise typed ``AssertionError``s.

        CL Ethernet: only IPv4 is resolved here. IPv6 is returned as ``None`` without
        an authenticated Redfish call so root/env credentials are never placed on
        argv (CL callers in this suite consume IPv4 only).
        """
        ip_addresses = {}
        with allure.step("Get bmc ipv4 from noga"):
            bmc_ipv4_address = topology_obj.players['dut']['attributes'].noga_query_data['attributes']['Specific'][
                'bmc_ip']
            assert bmc_ipv4_address, "Could you please check the BMC IP box in NOGA? It appears to be empty"
            logger.info(f"the bmc IPv4 is {bmc_ipv4_address}")
            ip_addresses["IPv4"] = bmc_ipv4_address

        # CL: no password-on-argv Redfish fetch; CL helpers only need OOB IPv4.
        if BmcTool._is_cl_platform():
            with allure.step("CL: skip authenticated IPv6 Redfish fetch"):
                logger.info(
                    "get_bmc_ip_addresses: CL platform — IPv6=None without Redfish auth "
                    "(avoids putting credentials on the command line)"
                )
                ip_addresses["IPv6"] = None
                return ip_addresses

        with allure.step("Sending a Redfish request to get the IPv6 address from the BMC"):
            line = BmcTool._redfish_bmc_eth0_fetch_slaac_line(engines.dut, bmc_ipv4_address)
            oob_line = line
            if bmc_ipv4_address != BmcTool.BMC_LOCAL_IP and line.startswith("BMC_IP_FETCH:curl:") and (
                BmcTool._is_curl_redfish_transport_failure(line[len("BMC_IP_FETCH:curl:"):])
            ):
                logger.warning(
                    "get_bmc_ip_addresses: OOB Redfish to %s: %r; retrying in-band %s",
                    bmc_ipv4_address,
                    line,
                    BmcTool.BMC_LOCAL_IP,
                )
                line = BmcTool._redfish_bmc_eth0_fetch_slaac_line(engines.dut, BmcTool.BMC_LOCAL_IP)
            if line.startswith("BMC_IP_FETCH:ipv6:") or line.startswith("BMC_IP_FETCH:slaac:"):
                rest = line.split(":", 2)[2].strip()
                ip_addresses["IPv6"] = rest or None
                return ip_addresses
            if line.startswith("BMC_IP_FETCH:redfish:"):
                msg = line[len("BMC_IP_FETCH:redfish:"):]
                raise AssertionError(
                    f"get_bmc_ip_addresses: Redfish error (eth0) NOGA bmc_ip={bmc_ipv4_address!r} "
                    f"(OOB first line: {oob_line!r}): {msg}"
                )
            if line.startswith("BMC_IP_FETCH:cred:"):
                msg = line[len("BMC_IP_FETCH:cred:"):]
                raise AssertionError(
                    f"get_bmc_ip_addresses: could not obtain BMC credentials on DUT "
                    f"bmc_ip={bmc_ipv4_address!r} (OOB: {oob_line!r}): {msg}"
                )
            if line.startswith("BMC_IP_FETCH:json:"):
                msg = line[len("BMC_IP_FETCH:json:"):]
                raise AssertionError(
                    f"get_bmc_ip_addresses: could not parse saved eth0 JSON on DUT "
                    f"bmc_ip={bmc_ipv4_address!r}: {msg}"
                )
            if line.startswith("BMC_IP_FETCH:empty"):
                raise AssertionError(
                    f"get_bmc_ip_addresses: empty HTTP body to /tmp/ngts_bmc_eth0.json from "
                    f"NOGA bmc_ip={bmc_ipv4_address!r} (OOB: {oob_line!r})"
                )
            if line.startswith("BMC_IP_FETCH:curl:"):
                msg = line[len("BMC_IP_FETCH:curl:"):]
                raise AssertionError(
                    f"get_bmc_ip_addresses: curl error for NOGA bmc_ip={bmc_ipv4_address!r} "
                    f"(after in-band {BmcTool.BMC_LOCAL_IP!r} if retried): {msg!r} "
                    f"(OOB: {oob_line!r})"
                )
            raise AssertionError(
                f"get_bmc_ip_addresses: unexpected result line: {line!r} "
                f"(OOB attempt: {oob_line!r})"
            )

    @staticmethod
    def install_fw_image(
            platform_component, test_name, filename, topology_obj, name,
            skip_version_check=False) -> ResultObj:
        component_name = platform_component.get_resource_basename().lower()
        res_obj, duration = OperationTime.save_duration(
            f'{component_name} install with reboot', '',
            test_name,
            platform_component.files.file_name[
                filename].action_file_install_with_reboot,
            topology_obj=topology_obj, skip_version_check=skip_version_check)
        return res_obj

    @staticmethod
    def install_fw_image_without_reboot(
            platform_component, test_name, filename,
            skip_version_check=False) -> ResultObj:
        component_name = platform_component.get_resource_basename().lower()
        res_obj, duration = OperationTime.save_duration(
            f'{component_name} install without reboot', '',
            test_name,
            platform_component.files.file_name[filename].action_file_install,
            force=False, skip_version_check=skip_version_check)
        return res_obj

    @staticmethod
    def _build_curl_base(method, bmc_ip_address, component_path):
        """Build base curl command with authentication and URL."""
        if IpTool.is_address_ipv6(bmc_ip_address):
            bmc_ip_address = f'[{bmc_ip_address}]'
        # Quote credentials so shell does not interpret '#' or other special chars (e.g. over SSH)
        creds = "{}:{}".format(ROOT, BMC_USER_BACKUP_PASSWORD)
        if "'" in creds:
            creds_quoted = "'" + creds.replace("'", "'\"'\"'") + "'"
        else:
            creds_quoted = "'" + creds + "'"
        return (
            "curl -s -k -u {} https://{}{}{} -X {}".format(
                creds_quoted,
                bmc_ip_address,
                BmcTool.BASE_REDFISH_URL,
                component_path,
                method,
            )
        )

    @staticmethod
    def send_get_request(engine, bmc_ip_address, component_path) -> ResultObj:
        """
        Send a GET request to BMC Redfish API.

        :param engine: Engine object to execute commands
        :param bmc_ip_address: BMC IP address
        :param component_path: Redfish API endpoint path
        :return: ResultObj with response or error
        """

        with allure.step(f"Send GET request to {bmc_ip_address}"):
            curl_get_cmd = BmcTool._build_curl_base(OpenApiReqType.GET, bmc_ip_address, component_path)
            curl_get_output = engine.run_cmd(curl_get_cmd + ' --fail && echo')

            if not curl_get_output:
                return ResultObj(False, "Received empty response from BMC")

            return ResultObj(True, "", curl_get_output)

    @staticmethod
    def send_patch_request(engine, bmc_ip_address, component_path, new_values, expected_value="") -> ResultObj:
        """
        Send a PATCH request to BMC Redfish API.

        :param engine: Engine object to execute commands
        :param bmc_ip_address: BMC IP address
        :param component_path: Redfish API endpoint path
        :param new_values: Dictionary of values to update
        :param expected_value: Expected value to verify in response
        :return: ResultObj with response or error
        """
        with allure.step(f"Send PATCH request to {bmc_ip_address}"):

            json_data = json.dumps(new_values)
            # Build as two shell commands joined by ';' via list join so flake8
            # does not treat the shell separator as Python E702.
            curl_path_cmd = " ".join([
                BmcTool._build_curl_base(OpenApiReqType.PATCH, bmc_ip_address, component_path),
                "-H 'Content-Type: application/json'",
                "-d '{}'".format(json_data),
                "--fail 2>&1",
            ]) + '; echo "CURL_EXIT=$?"'
            curl_patch_output = engine.run_cmd(curl_path_cmd, validate=False)

            if "CURL_EXIT=0" not in curl_patch_output:
                return ResultObj(
                    False,
                    "PATCH failed (exit code or curl error). Output: {}".format(
                        curl_patch_output)
                )

            if expected_value and expected_value not in curl_patch_output:
                return ResultObj(
                    False,
                    "Expected value {!r} missing from response: {}".format(
                        expected_value, curl_patch_output)
                )

            response_output = curl_patch_output.split("CURL_EXIT=")[0].rstrip()
            return ResultObj(True, "", response_output)
