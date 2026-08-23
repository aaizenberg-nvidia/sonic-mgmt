import random
import string

import pytest
from devts.infra.tools.connection_tools.linux_ssh_engine import LinuxSshEngine

from ngts.ngts_types import DevicesT
from ngts.nvos_constants.constants_nvos import ApiType
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.general.security.constants import SecurityConsts
from ngts.tests_nvos.helpers.general_helpers import run_ssh_cmd_with_rc
from ngts.tools.test_utils import allure_utils as allure


@pytest.mark.system
@pytest.mark.security
def test_change_sed_password(engines, devices: DevicesT, sed_default_password, random_api, tpm_tool):
    """
    @summary:
        Verify that the maximum-length SED password works via NVUE and OpenAPI.

        Steps:
        1. Set a valid 127-character SED password via nv action
        2. Verify the password is working via sed util.
        3. Check that tpm primary and secondary bank have this password.
        4. Reboot the system and verify nvos works.
        5. Check that tpm primary and secondary bank have this password.
    """
    switch: LinuxSshEngine = engines.dut
    system = System()
    new_sed_password = "A" * SecurityConsts.SED_MAX_PASSWORD_LENGTH

    _verify_tpm_banks_password(tpm_tool, sed_default_password, device=devices.dut)

    with allure.step(f"Set new SED password via nv action change system security sed-password {new_sed_password}"):
        system.security.action_change_sed_password(new_sed_password).verify_result()

    with allure.step("Get disk name for current device"):
        disk_name_output = switch.run_cmd("sudo sedutil-cli --scan")
        if (start := disk_name_output.find("/dev/")) != -1 and (end := disk_name_output.find(" ", start)) != -1:
            disk_name = disk_name_output[start:end]
        else:
            raise AssertionError("Can't find correct disk name for this device")

    _verify_sed_password_works(switch, new_sed_password, disk_name)

    _verify_tpm_banks_password(tpm_tool, new_sed_password, device=devices.dut)

    with allure.step("Reboot the system"):
        system.reboot.action_reboot(engine=switch, device=devices.dut)

    _verify_tpm_banks_password(tpm_tool, new_sed_password, device=devices.dut)


@pytest.mark.system
@pytest.mark.security
def test_back_old_pass(engines, devices: DevicesT, sed_default_password, tpm_tool):
    """
    @summary:
        Verify that change SED password works when flow was broken during the action flow
        The tpm primary bank password was changed, but SED password didn't change

        Steps:
        1. Set a new SED password in primary tpm bank
        2. Change primary TPM bank to new wrong password.
        3. Reboot the system and verify nvos works.
        4. Check that tpm primary and secondary bank have old SED password.
    """
    switch: LinuxSshEngine = engines.dut
    system = System()
    new_sed_password = "old_password"

    _verify_tpm_banks_password(tpm_tool, sed_default_password, device=devices.dut)

    with allure.step("Set primary tpm bank to new password"):
        tpm_tool.set_sed_password_primary_bank(new_sed_password, devices.dut)

    with allure.step("Reboot the system"):
        system.reboot.action_reboot(engine=switch, device=devices.dut)

    _verify_tpm_banks_password(tpm_tool, sed_default_password, device=devices.dut)


@pytest.mark.system
@pytest.mark.security
def test_back_new_pass(engines, devices: DevicesT, sed_default_password, tpm_tool):
    """
    @summary:
        Verify that change SED password works with new password when banks don't match

        Steps:
        1. Set a new SED password via nv action
        2. Change secondary TPM bank to wrong password.
        3. Reboot the system and verify nvos works.
        4. Check that tpm primary and secondary bank have new SED password.
    """
    TestToolkit.tested_api = ApiType.OPENAPI

    switch: LinuxSshEngine = engines.dut
    system = System()
    new_sed_password = "Another_pass"

    _verify_tpm_banks_password(tpm_tool, sed_default_password, device=devices.dut)

    with allure.step(f"Set new SED password via nv action change system security sed-password {new_sed_password}"):
        system.security.action_change_sed_password(new_sed_password).verify_result()

    with allure.step("Set secondary tpm bank to wrong password"):
        wrong_password = "i_am_wrong_password"
        tpm_tool.set_sed_password_secondary_bank(wrong_password, devices.dut)

    with allure.step("Reboot the system"):
        system.reboot.action_reboot(engine=switch, device=devices.dut)

    _verify_tpm_banks_password(tpm_tool, new_sed_password, device=devices.dut)


@pytest.mark.system
@pytest.mark.security
def test_password_length_negative(engines, devices: DevicesT, sed_default_password, tpm_tool):
    """
    @summary:
        Verify that invalid SED passwords are rejected.

        Steps:
        1. Try to set a 128-character SED password and expect failure
        2. Try to set a too-short SED password via nv action and expect failure
        3. Try to set a password containing whitespace and expect failure
    """
    system = System()
    long_pass = "A" * (SecurityConsts.SED_MAX_PASSWORD_LENGTH + 1)
    short_pass = generate_random_string_with_length(1, SecurityConsts.SED_MIN_PASSWORD_LENGTH - 1)
    whitespace_pass = "bad pass word"

    _verify_tpm_banks_password(tpm_tool, sed_default_password, device=devices.dut)

    with allure.step(f"Try to set long SED password via nv action {long_pass}"):
        system.security.action_change_sed_password(long_pass).verify_result(should_succeed=False)

    _verify_tpm_banks_password(tpm_tool, sed_default_password, device=devices.dut)

    with allure.step(f"Try to set short SED password via nv action {short_pass}"):
        system.security.action_change_sed_password(short_pass).verify_result(should_succeed=False)

    _verify_tpm_banks_password(tpm_tool, sed_default_password, device=devices.dut)

    with allure.step(f"Try to set SED password containing whitespace via nv action {whitespace_pass}"):
        system.security.action_change_sed_password(whitespace_pass).verify_result(should_succeed=False)

    _verify_tpm_banks_password(tpm_tool, sed_default_password, device=devices.dut)


def _verify_tpm_banks_password(tpm_tool, expected_password, device):
    with allure.step("Verify tpm banks have old password"):
        with allure.independent_step("Verify primary bank has correct password"):
            password_primary = tpm_tool.get_sed_password_primary_bank(device=device)
            assert password_primary == expected_password, f"The password from tpm primary bank should match {expected_password}"
        with allure.independent_step("Verify secondary bank has correct password"):
            password_secondary = tpm_tool.get_sed_password_secondary_bank(device=device)
            assert password_secondary == expected_password, f"The password from tpm secondary bank should match {expected_password}"


def _verify_sed_password_works(switch: LinuxSshEngine, password: str, disk_name: str):
    with allure.step("Verify SED is working with provided password"):
        cmd = f"sudo sedutil-cli --listLockingRanges '{password}' '{disk_name}'"
        _, exit_code = run_ssh_cmd_with_rc(switch, cmd)
        assert exit_code == 0, "The sed list locking ranges should be successful"


def _change_sed_password_manually(engine, old_pass: str, new_pass: str, disk_name: str):
    engine.run_cmd(f"sudo sedutil-cli --setadmin1pwd '{old_pass}' '{new_pass}' '{disk_name}'")


def generate_random_string_with_length(minimal: int, maximum: int):
    with allure.step(f"Generate random string with specified range length {minimal}-{maximum}"):
        length = random.randint(minimal, maximum)
        return "".join(random.choices(string.ascii_letters + string.digits, k=length))
