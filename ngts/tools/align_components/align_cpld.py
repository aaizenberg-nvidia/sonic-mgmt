import os
import traceback

from align_fw_components import create_json_dict, get_provisioning, get_switch_info, parse_args, verify_install_path
from Component import CpldComponent, normalize_cpld_versions
from ComponentManager import ComponentManager
from Constants import Defaults
from ip_utils import resolve_bmc_ip, resolve_switch_mgmt_ip
from Redfish_rest_api import RedFishRestApi


def perform_cpld_update(_args):
    switch_info = get_switch_info(_args.setup_name)
    hostname = resolve_switch_mgmt_ip(switch_info)
    bmc_ip = resolve_bmc_ip(switch_info)
    assert bmc_ip, "No bmc ip found in noga"
    if _args.cpld_path:
        install_path = _args.cpld_path
        required_version = normalize_cpld_versions(os.path.basename(install_path))
    else:
        json_dict = create_json_dict(_args.fw_versions_json_file)
        cpld = json_dict[get_provisioning(switch_info)][Defaults.CPLD_NAME]['latest']
        install_path = cpld['path']
        required_version = normalize_cpld_versions(cpld['version_name'])

    if install_path.strip().endswith(Defaults.BMC_PACKAGE_SUFFIX):
        print(f"{install_path} is flashed through the BMC by align_fw_components.py, skipping")
        return

    if not verify_install_path(install_path):
        print(f"Provided cpld path does not exist {install_path}")
        raise FileNotFoundError(install_path)

    rf_api = RedFishRestApi(bmc_ip, _args.bmc_user, _args.bmc_pass)

    component = CpldComponent(Defaults.CPLD_NAME, install_path=install_path, switch_ip=hostname,
                              required_version=required_version, ssh_user=_args.ssh_user, ssh_pass=_args.ssh_pass,
                              rf_api=rf_api)
    component_manager = ComponentManager(components=[component])

    component_manager.print_installed_versions()
    was_update_performed = component_manager.perform_update()

    if was_update_performed:
        component_manager.perform_pc(switch_info)

    component_manager.print_installed_versions()


if __name__ == '__main__':
    try:
        print(f"Start component alignment script ({__file__})")
        _args = parse_args()
        perform_cpld_update(_args)
        print(f"Finished component alignment script ({__file__})")
    except Exception as err:
        print(f"\nException {type(err)} occurred with message: {err}\nTraceback:\n{traceback.format_exc()}")
        traceback.print_exc()
        raise  # raise an exception, keeping the stack trace
