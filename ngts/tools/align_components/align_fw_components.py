from __future__ import annotations

import argparse
import json
import os.path
import re
import signal
import subprocess
import time
import traceback

import nogaq
from Component import BmcComponent, Component, normalize_cpld_versions
from ComponentManager import ComponentManager
from Constants import FW_INVENTORY_IDS, Defaults, NogaConstants, RedfishCollection
from ip_utils import resolve_bmc_ip
from Redfish_rest_api import RedFishRestApi

COMPONENT_PATH_ARGS = {
    Defaults.BMC_NAME: "bmc_path",
    Defaults.BIOS_NAME: "bios_path",
    Defaults.EROT_NAME: "erot_path",
    Defaults.FPGA_NAME: "fpga_path",
    Defaults.FPGA_ENCRYPTED_NAME: "fpga_path",
    Defaults.SMA_NAME: "sma_path",
    Defaults.CPLD_NAME: "cpld_path",
}


def get_switch_info(setup_name: str) -> dict:
    setup_dict = nogaq.get_noga_resource_data(resource_name=setup_name)
    switch_names = [item[NogaConstants.NAME] for item in
                    setup_dict[NogaConstants.RELATIONS][NogaConstants.HAS_A]
                    if item[NogaConstants.TYPE_TITLE] == NogaConstants.SWITCH]
    switches = [nogaq.get_noga_resource_data(resource_name=switch) for switch in switch_names]
    assert switches, f"No switches found in noga for setup {setup_name}"
    return switches[0]


def get_provisioning(switch_info: dict) -> str:
    is_opn = switch_info[NogaConstants.ATTRIBUTES][NogaConstants.SPECIFIC][NogaConstants.OPN].lower()
    return Defaults.PRODUCTION if is_opn == NogaConstants.YES else Defaults.DEVELOPMENT


def start_components_update(_args):
    switch_info = get_switch_info(_args.setup_name)
    bmc_ip = resolve_bmc_ip(switch_info)
    provisioning = get_provisioning(switch_info)
    switch_name = switch_info[NogaConstants.ATTRIBUTES][NogaConstants.COMMON]['Name'].strip()
    assert bmc_ip, "No bmc ip found in noga"
    update_via_parameter = any(getattr(_args, arg) for arg in COMPONENT_PATH_ARGS.values())
    cli_paths = _get_cli_paths(_args)
    rf_api = get_reachable_rf_api(bmc_ip, switch_info, _args)
    json_dict = create_json_dict(_args.fw_versions_json_file)
    devices_with_rel_prod_erot = {
        "juliet-160": {Defaults.EROT_NAME, Defaults.BIOS_NAME, Defaults.BMC_NAME},
        "juliet-195": {Defaults.EROT_NAME, Defaults.BIOS_NAME, Defaults.BMC_NAME},
        # juliet-126 has mixed secure-state: only bmc is prod (bios/asic/nvos are dev),
        # so only erot+bmc take the prod package. See `nv show fae platform secure-state`.
        "juliet-126": {Defaults.EROT_NAME, Defaults.BMC_NAME},
    }

    unused_fpga = Defaults.FPGA_ENCRYPTED_NAME if _has_non_encrypted_fpga(bmc_ip) else Defaults.FPGA_NAME
    components_to_update = list(cli_paths) if update_via_parameter else \
        _get_recipe_components(json_dict.get(provisioning, {}))
    if unused_fpga in components_to_update:
        print(f"{unused_fpga}: this BMC takes the other fpga package, skipping")
        components_to_update.remove(unused_fpga)
    inventory = _member_ids(rf_api.get_query(RedfishCollection.FIRMWARE_INVENTORY))

    components: list[Component] = []
    missing_path_errors = []

    for component_name in components_to_update:
        fw_ids = [fw_id for fw_id in inventory if re.fullmatch(FW_INVENTORY_IDS[component_name], fw_id)]
        if not fw_ids:  # the versions json lists the platform components, so the BMC must have them
            print(f"ERROR: {component_name} is not in the BMC firmware inventory")
            missing_path_errors.append(f"{component_name}: not in the BMC firmware inventory")
            continue

        required_version = None

        if update_via_parameter:
            install_path = cli_paths[component_name]
        else:
            use_prod = component_name in devices_with_rel_prod_erot.get(switch_name, ())
            latest = json_dict[Defaults.PRODUCTION if use_prod else provisioning][component_name]['latest']
            install_path = latest['path']
            required_version = normalize_cpld_versions(latest['version_name']) \
                if component_name == Defaults.CPLD_NAME else latest['version_name']

        install_path = install_path.strip()  # Make sure there are no extra spaces in the path

        if not verify_install_path(install_path):
            error_msg = f"{component_name}: path does not exist - {install_path}"
            print(f"WARNING: {error_msg}")
            missing_path_errors.append(error_msg)
            continue  # Skip this component but continue with others

        components.append(BmcComponent(component_name, install_path, required_version, rf_api, fw_ids))

    if not components:
        if missing_path_errors:
            raise FileNotFoundError("No valid components found to install:\n" + "\n".join(missing_path_errors))
        print("No component to align through the BMC")
        return

    component_manager = ComponentManager(components)

    component_manager.print_installed_versions()
    was_update_performed = component_manager.perform_update()
    update_errors = component_manager.get_errors()

    if was_update_performed:
        component_manager.perform_pc(switch_info)
        try:
            _wait_for_bmc(rf_api, RedfishCollection.FIRMWARE_INVENTORY, "CPU firmware inventory",
                          lambda r: any(re.fullmatch(FW_INVENTORY_IDS[Defaults.BIOS_NAME], fw_id)
                                        for fw_id in _member_ids(r)))
            _wait_for_bmc(rf_api, RedfishCollection.BMC_MANAGER, "BMC manager",
                          lambda r: r["Status"]["State"] == "Enabled")
        except Exception as e:
            print("Timed out waiting for BMC to complete boot.")
            raise e

    component_manager.print_installed_versions()

    # Collect all errors (missing paths + update errors)
    all_errors = missing_path_errors + update_errors

    if all_errors:
        error_summary = "The following errors occurred during component alignment:\n" + "\n".join(all_errors)
        raise Exception(error_summary)


def get_reachable_rf_api(bmc_ip, switch_info, _args) -> RedFishRestApi:
    """
    The whole update flow talks to the BMC with the root user, so make sure its credentials are valid.
    If they are not, reset the root password from the switch, using the nvos user
    whose password is stored in the TPM.
    """
    assert is_bmc_pingable(bmc_ip), (f"BMC {bmc_ip} does not answer to ping, so the bmc itself is down. "
                                     f"Resetting the root password will not help here")

    rf_api = RedFishRestApi(bmc_ip, _args.bmc_user, _args.bmc_pass)
    if is_bmc_reachable(rf_api):
        print(f"BMC {bmc_ip} is reachable with user {_args.bmc_user}")
        return rf_api

    print(f"BMC {bmc_ip} answers to ping, so its redfish rejected the password of user {_args.bmc_user}")
    reset_bmc_root_password(bmc_ip, switch_info, _args)
    rf_api = RedFishRestApi(bmc_ip, Defaults.DEFAULT_BMC_USER, Defaults.DEFAULT_BMC_PASSWORD)
    assert is_bmc_reachable(rf_api), (f"BMC {bmc_ip} still rejects user {Defaults.DEFAULT_BMC_USER} with the "
                                      f"default password after the root password reset was performed")
    print(f"BMC {bmc_ip} is reachable with user {Defaults.DEFAULT_BMC_USER} after the root password reset")
    return rf_api


def is_bmc_reachable(rf_api: RedFishRestApi, retries: int = 2, interval: int = 2) -> bool:
    """
    :param retries: number of additional attempts on top of the first one
    :param interval: seconds to wait between the attempts
    """
    for attempt in range(retries + 1):
        try:
            rf_api.get_query(RedfishCollection.BMC_MANAGER)
            return True
        except Exception as err:
            print(f"Attempt {attempt + 1}/{retries + 1}: redfish query to BMC {rf_api.ip} "
                  f"with user {rf_api.username} failed: {err}")
            if attempt < retries:
                time.sleep(interval)
    return False


def is_bmc_pingable(bmc_ip, count: int = 3, wait: int = 2) -> bool:
    """
    :param count: number of echo requests to send
    :param wait: seconds to wait for a reply of each request
    """
    ping_cmd = ['ping', '-c', str(count), '-W', str(wait), bmc_ip]
    return subprocess.run(ping_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False).returncode == 0


def reset_bmc_root_password(bmc_ip, switch_info, _args):
    switch_hostname = switch_info[NogaConstants.ATTRIBUTES][NogaConstants.COMMON]['Name']
    print(f"Resetting bmc root password via {Defaults.BMC_NVOS_USER} user")
    tpm_password = run_ssh_cmd(switch_hostname, _args.ssh_user, _args.ssh_pass,
                               Defaults.GET_BMC_PASSWORD_FROM_TPM_CMD)
    assert tpm_password, f"Failed to get the bmc password from the tpm of {switch_hostname}"

    nvos_rf_api = RedFishRestApi(bmc_ip, Defaults.BMC_NVOS_USER, tpm_password)
    nvos_rf_api.patch_query(RedfishCollection.ROOT_ACCOUNT, {"Password": Defaults.DEFAULT_BMC_PASSWORD})


def run_ssh_cmd(switch_ip, ssh_user, ssh_pass, command, timeout=Defaults.SSH_CMD_TIMEOUT):
    """
    execute command on the switch via ssh connection and wait for output
    :param timeout: seconds to wait for the command to complete before killing it
    :return: last line of the command output
    """
    ssh_command = [
        'sshpass', '-p', ssh_pass,
        'ssh', '-o', 'UserKnownHostsFile=/dev/null', '-o', 'StrictHostKeyChecking=no',
        '-o', 'TCPKeepAlive=yes', '-o', 'ServerAliveInterval=30', '-o', 'ConnectTimeout=30',
        f'{ssh_user}@{switch_ip}', command
    ]

    process = subprocess.Popen(ssh_command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               start_new_session=True)
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as err:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        output, _ = process.communicate()
        raise Exception(f"Ssh command on {switch_ip} did not complete within {timeout} seconds\n"
                        f"{output.decode('latin-1').strip()}") from err

    output = output.decode('latin-1').strip()
    if process.returncode:
        raise Exception(f"Failed to run ssh command on {switch_ip}\n"
                        f"Exit Code: {process.returncode}\n{output}")
    return output.split('\n')[-1].strip()


def verify_install_path(install_path):
    return os.path.exists(install_path)


def create_json_dict(json_file_path):
    print(f'Read platform components info from json {json_file_path}')
    if not verify_install_path(json_file_path):
        print(f"Json file path does not exist {json_file_path}")
        raise FileNotFoundError(json_file_path)
    with open(json_file_path) as file:
        return json.load(file)


def _get_cli_paths(_args):
    """Packages given as --<component>_path arguments."""
    return {name: path for name, arg in COMPONENT_PATH_ARGS.items()
            if (path := getattr(_args, arg)) and not _is_align_cpld_package(name, path)}


def _is_align_cpld_package(name, path):
    """A .vme cpld package is flashed over ssh by align_cpld.py, only a .fwpkg one goes through the BMC."""
    if name != Defaults.CPLD_NAME or path.endswith(Defaults.BMC_PACKAGE_SUFFIX):
        return False
    print(f"{name}: {os.path.basename(path)} is not a {Defaults.BMC_PACKAGE_SUFFIX}, left to align_cpld.py")
    return True


def _get_recipe_components(recipe):
    """Components with a package in the versions json (no key / empty path = skip)."""
    components = []
    for name in FW_INVENTORY_IDS:
        path = recipe.get(name, {}).get('latest', {}).get('path', '').strip()
        if name not in recipe:
            print(f"{name}: not in the versions json, skipping")
        elif not path:
            print(f"{name}: empty package path in the versions json, skipping")
        elif not _is_align_cpld_package(name, path):
            components.append(name)
    return components


def _member_ids(respond):
    return [member['@odata.id'].rsplit('/', 1)[1] for member in respond['Members']]


def _has_non_encrypted_fpga(bmc_ip):
    non_encrypted_fpga_ips = {'10.7.113.148', '10.193.65.142'}
    return bmc_ip in non_encrypted_fpga_ips

# Try for 3 minutes (24 * 5 seconds) to wait for BMC complete boot.


def _wait_for_bmc(rf_api, query_path, what, ready):
    tries, delay = Defaults.BMC_READY_TRIES, Defaults.BMC_READY_DELAY
    for attempt in range(1, tries + 1):
        try:
            if ready(rf_api.get_query(query_path)):
                print(f"{what} ready.")
                return
        except Exception as err:  # BMC still booting: connection refused / partial JSON
            print(f"{what} query failed: {err}")
        print(f"{what} not ready; attempt {attempt}/{tries}. Retrying in {delay}s...")
        time.sleep(delay)

    raise RuntimeError(f"{what} not ready after {tries * delay}s")


def parse_args():
    """Handle parsing the command line arguments using argparse. Documented in the code."""
    usage_str = """Script to be used as a regression step in order
     to align juliet components firmware versions to specific recipe"""
    epilog_str = f'How to run the script:\n{usage_str}'
    parser = argparse.ArgumentParser(usage=usage_str, epilog=epilog_str,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)

    def parse_mars_default(value):
        if value == 'False':
            return None
        return value

    parser.add_argument('--setup_name',
                        help='Setup name in NOGA', required=True)
    parser.add_argument('--sonic_mgmt_repo_branch',
                        help='Release branch target', required=False, type=parse_mars_default,
                        default=Defaults.DEFAULT_BRANCH_NAME)
    parser.add_argument('--bmc_user',
                        help='Bmc username', required=False, type=parse_mars_default, default=Defaults.DEFAULT_BMC_USER)
    parser.add_argument('--bmc_pass',
                        help='Bmc password', required=False, type=parse_mars_default,
                        default=Defaults.DEFAULT_BMC_PASSWORD)

    parser.add_argument('--fw_versions_json_file',
                        help='Path to file containing required version',
                        default='/auto/sw_system_project/NVOS_INFRA/verification_files/platform_components/juliet_versions.json',
                        type=parse_mars_default)

    parser.add_argument('--bmc_path',
                        help='Path to bmc fwpkg', required=False, type=parse_mars_default, default=None)
    parser.add_argument('--bios_path',
                        help='Path to bios fwpkg', required=False, type=parse_mars_default, default=None)
    parser.add_argument('--fpga_path',
                        help='Path to fpga fwpkg', required=False, type=parse_mars_default, default=None)
    parser.add_argument('--erot_path',
                        help='Path to erot fwpkg', required=False, type=parse_mars_default, default=None)
    parser.add_argument('--cpld_path',
                        help='Path to cpld fwpkg', required=False, type=parse_mars_default, default=None)
    parser.add_argument('--sma_path',
                        help='Path to sma fwpkg', required=False, type=parse_mars_default, default=None)
    parser.add_argument('--pldm_path',
                        help='Path to pldm fwpkg', required=False, type=parse_mars_default, default=None)

    parser.add_argument('--ssh_user',
                        help='SSH username', required=False, type=parse_mars_default,
                        default=Defaults.DEFAULT_SWITCH_USERNAME)
    parser.add_argument('--ssh_pass',
                        help='SSH password', required=False, type=parse_mars_default,
                        default=Defaults.DEFAULT_SWITCH_PASSWORD)

    args = parser.parse_args()
    for arg, value in vars(args).items():
        if not value:
            setattr(args, arg, parser.get_default(arg))
    return args


if __name__ == '__main__':
    try:
        print(f"Start component alignment script ({__file__})")
        _args = parse_args()
        start_components_update(_args)
        print(f"Finished component alignment script ({__file__})")
    except Exception as err:
        print(f"\nException {type(err)} occurred with message: {err}\nTraceback:\n{traceback.format_exc()}")
        traceback.print_exc()
        raise  # raise an exception, keeping the stack trace
