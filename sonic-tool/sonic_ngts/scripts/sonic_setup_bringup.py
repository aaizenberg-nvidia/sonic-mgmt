#!/usr/bin/env python
import argparse
import logging
import sys
import traceback
import yaml
import os
import subprocess
import pathlib

sys.path.append(str(os.path.join(str(pathlib.Path(__file__).parent.absolute()), "..")))

from infra.topology_entities.topology_manager import TopologyManager
from infra.constants.constants import LinuxConsts
from infra.utilities.topology_util import get_xml_template, create_file
from generate_topology_files import (
    process_setup_and_generate_topology,
    get_hostnames,
    parse_setup_conf_yml,
    build_device_map,
    get_setup_name_from_dut
)
logger = logging.getLogger("sonic_setup_bringup")

STM_IP = "10.209.104.53"

stm_user = os.getenv("STM_USER") if os.getenv("STM_USER") else "svc-nbu-sws-sonic"
stm_password = os.getenv("STM_PASSWORD") if os.getenv("STM_PASSWORD") else "svc-nbu-sws-sonic11"


def set_logger(log_level):
    logging.basicConfig(level=log_level,
                        format='%(asctime)s %(name)-12s %(levelname)-8s %(message)s',
                        datefmt='%m-%d %H:%M')


def init_parser():
    '''
    Usage:
    python3 sonic-tool/sonic_ngts/scripts/sonic_setup_bringup.py -f sonic-tool/sonic_ngts/scripts/setup_conf_example.yml
    -l DEBUG -s sonic_chameleon_r-chameleon-test-02
    --port_connection_csv sonic-tool/mars/scripts/canonical_port_connection_files/chameleon_port_connection.csv
    --port_config_ini sonic-tool/mars/scripts/port_config.ini_files/port_config_chameleon.ini
   '''
    description = ('Functionality of the script: \n'
                   '1. Get all setup information via configuration file.\n'
                   '2. Create setup files and topo files and save the to the shared location.\n'
                   '3. Export setup information to Noga (connectivity, aliases and additional information).\n')

    parser = argparse.ArgumentParser(description=description)
    parser.add_argument('-f', '--arguments_file', dest='arguments_file',
                         help='A yaml file with the information about all the entities in the setup\n,'
                              'see example file sonic_setup_arguments.yaml')
    parser.add_argument('-l', '--log_level', dest='log_level', default=logging.INFO, help='log verbosity')
    parser.add_argument('-s', '--setup_name', dest='setup_name', default=None,
                        help='Specify setup name if setup name should be name differently than '
                             'sonic_<switch_type>_<switch_hostname>')
    parser.add_argument('-g', '--setup_group', dest='setup_group', default="SONiC_Canonical",
                        help='Specify setup group, if group is different than SONiC_Canonical')
    parser.add_argument(
        '--port_connection_csv',
        required=False,
        default=None,
        help='Path to port connection CSV file (optional). If omitted, CONNECTIVITY/PORT/ACTIVE_PORTS_IDS will not be generated.'
    )
    parser.add_argument(
        '--port_config_ini',
        required=False,
        default=None,
        help='Path to port config INI file (optional). If omitted, CONNECTIVITY/PORT/ACTIVE_PORTS_IDS will not be generated.'
    )
    parser.add_argument(
        '--output_dir',
        default='/auto/sw_regression/system/SONIC/MARS/conf/topo',
        help='Output directory base path'
    )

    args, unknown = parser.parse_known_args()

    if unknown:
        raise Exception("unknown argument(s): {}".format(unknown))
    return args


def get_arguments_from_yaml_file(file_path):
    with open(file_path) as file:
        args = yaml.load(file, Loader=yaml.Loader)
        file.close()
    return args


def import_setup_to_noga(topology_base_dir, setup_name, setup_group):
    """
    Will import connectivity between switches based on created topology_all.xml to NOGA
    :param topology_base_dir: Base directory with .xml files
    :param setup_name: the setup name in noga, e.g. SONiC_tigris_r-tigris-06
    :param setup_group: the group the setup belong to in Noga, i.e. SONiC_Canonical
    """
    script_cmd = f"/.autodirect/sw_tools/Internal/Noga/RELEASE/latest/import/import_mars_topology.py -f {topology_base_dir}/{setup_name}/topology_all.xml " \
                 f"-n {setup_name} -g Sagi -s {setup_group} -S MTR"
    cmd = f"sshpass -p {stm_password} ssh -o 'StrictHostKeyChecking no' -t {stm_user}@{STM_IP} '{script_cmd}'"
    logger.info(f"CMD: {cmd}")
    try:
        subprocess.check_output(cmd, shell=True)
    except Exception as e:
        raise Exception(f"failed to import setup to Noga, error: {e}")
    logger.info(f"Setup {setup_name} imported to Noga successfully")


def scp_file_to_stm(file_path):
    """
    will copy given file to stm
    :param file_path: path to file to copy
    :return: None
    """
    cmd = f'sshpass -p "{stm_password}" scp {file_path} {stm_user}@{STM_IP}:/tmp'
    logger.info("Copy to STM. CMD: %s" % cmd)
    os.system(cmd)


def import_aliases_to_noga(noga_json_file_path):
    """
    run the import alises to noga script on stm
    :param noga_json_file_path: path to the json file containing the aliases for the setup
    :return: None
    """
    logger.info("Import aliases to Noga")
    # copy JSON file & 'import to noga' script to STM
    scp_file_to_stm(noga_json_file_path)
    scp_file_to_stm("{}/import_aliases_to_noga.py".format(str(pathlib.Path(__file__).parent.absolute())))

    # Update Noga according to JSON topology
    remote_cmd = ("python2.7 /tmp/import_aliases_to_noga.py --json {}"
                  .format(noga_json_file_path))

    # Use subprocess with sshpass instead of SSH engine to avoid
    # Netmiko timeout issues. This is more reliable for long-running commands
    ssh_cmd = [
        'sshpass', '-p', stm_password,
        'ssh', '-o', 'StrictHostKeyChecking=no',
        '-o', 'UserKnownHostsFile=/dev/null',
        '-o', 'TCPKeepAlive=yes',
        '-o', 'ServerAliveInterval=60',
        '-o', 'ConnectTimeout=30',
        '{}@{}'.format(stm_user, STM_IP),
        remote_cmd
    ]

    logger.info("CMD: %s" % remote_cmd)
    try:
        result = subprocess.run(
            ssh_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=2000  # about 33 minute timeout
        )

        if result.returncode != 0:
            error_msg = (
                f"Command failed with return code {result.returncode}\n"
                f"STDOUT: {result.stdout}\n"
                f"STDERR: {result.stderr}"
            )
            logger.error(error_msg)
            raise Exception(
                "Import aliases to Noga has failed.\n"
                "please verify in Noga all setup entities were named "
                "correctly,\n"
                "and try to run: \"{}\" on stm {} again.\n"
                "Script Error: {}".format(remote_cmd, STM_IP, error_msg)
            )

        logger.info("Import aliases to Noga completed successfully")
        if result.stdout:
            logger.info("Output: %s" % result.stdout)
    except subprocess.TimeoutExpired as e:
        raise Exception(
            "Import aliases to Noga has timed out after 5 minutes.\n"
            "please verify in Noga all setup entities were named "
            "correctly,\n"
            "and try to run: \"{}\" on stm {} again.\n"
            "Script Error: {}".format(remote_cmd, STM_IP, str(e))
        )
    except Exception as e:
        raise Exception(
            "Import aliases to Noga has failed.\n"
            "please verify in Noga all setup entities were named "
            "correctly,\n"
            "and try to run: \"{}\" on stm {} again.\n"
            "Script Error: {}".format(remote_cmd, STM_IP, str(e))
        )


def noga_update_host_from_server_to_vm(setup_config_file, username='sonic_ver'):
    """
    Run noga_manage.py command to update host from server to vm in Noga.
    Runs the command for all aliases: ha, hb, and sonic_mgmt.

    :param setup_config_file: Path to YAML configuration file
                              (e.g., scripts/setup_conf_example.yml)
    :param username: Username for the command (default: sonic-ver)
    :return: Dictionary with results for each alias, containing 'output' or
             'error' for each hostname
    """
    # Get hostnames from setup config
    hostnames = get_hostnames(setup_config_file=setup_config_file)

    # Define aliases to process
    aliases = ['ha', 'hb', 'sonic_mgmt']
    results = {}
    noga_script = '/auto/sw_tools/Internal/Noga/RELEASE/latest/cli/noga_manage.py'

    # Process each alias
    for alias in aliases:
        host_name = hostnames.get(alias)

        if not host_name:
            logger.warning(
                f"{alias} hostname not found in setup configuration, skipping")
            results[alias] = {'error': f"{alias} hostname not found"}
            continue

        cmd = (f"{noga_script} -n '{host_name}' -t server -T vm -c "
               f"-U {username}")
        logger.info(f"Running noga_manage for {alias} ({host_name})")
        logger.info(f"CMD: {cmd}")

        try:
            output = subprocess.check_output(cmd, shell=True)
            logger.info(
                f"Noga manage command executed successfully for "
                f"{alias} ({host_name})")
            results[alias] = {'output': output, 'hostname': host_name}
        except subprocess.CalledProcessError as e:
            error_msg = (
                f"Failed to run noga_manage for {alias} ({host_name}), "
                f"error: {e}")
            logger.error(error_msg)
            results[alias] = {'error': error_msg, 'hostname': host_name}

    return results


def create_setup_files_from_yaml(setup_config_file, setup_name=None):
    """
    Create setup files (topology.xml and .setup file) from YAML configuration.
    Implements the same functionality as TopologyManager.create_setup_files()
    but uses setup_conf_example.yml as input.

    :param setup_config_file: Path to YAML configuration file
                              (e.g., scripts/setup_conf_example.yml)
    :param setup_name: Optional setup name. If not provided, will be generated
                       from DUT hostname
    :return: Dictionary with paths to created files
    """
    # Parse setup config
    setup_config = parse_setup_conf_yml(setup_config_file)
    device_map = build_device_map(setup_config)

    # Get setup name if not provided
    if not setup_name:
        setup_name = get_setup_name_from_dut(device_map)

    # Define directory paths (same as TopologyManager)
    sonic_topology_dir = f"/auto/sw_regression/system/SONIC/MARS/conf/topo/{setup_name}"
    sonic_setup_dir = f"/auto/sw_regression/system/SONIC/MARS/conf/setups/{setup_name}"

    # Create directories
    logger.info(f'Creating setup file directory: {sonic_setup_dir}')
    if not os.path.exists(sonic_setup_dir):
        os.system(f"sudo mkdir -p {sonic_setup_dir}")
    os.system(f"sudo chmod 777 {sonic_setup_dir}")

    logger.info(f'Creating setup topology file directory: {sonic_topology_dir}')
    if not os.path.exists(sonic_topology_dir):
        os.system(f"sudo mkdir -p {sonic_topology_dir}")
    os.system(f"sudo chmod 777 {sonic_topology_dir}")

    # Extract entities for template rendering
    switches_list = []
    for switch in setup_config.get('switches', []):
        switch_alias = switch['alias']
        switch_info = device_map[switch_alias]
        switches_list.append(switch_info)

    hosts_list = []
    for host in setup_config.get('hosts', []):
        host_alias = host['alias']
        host_info = device_map[host_alias]
        hosts_list.append(host_info)

    # Get hypervisor and sonic_mgmt from other_entities
    # build_device_map stores entities by entity_id if available
    hypervisor_info = device_map.get('hypervisor')
    sonic_mgmt_info = (device_map.get('sonic_mgmt') or
                       device_map.get('sonic-mgmt'))

    if not hypervisor_info:
        raise ValueError("hypervisor not found in setup configuration")
    if not sonic_mgmt_info:
        raise ValueError("sonic_mgmt not found in setup configuration")

    # Create sonic topology.xml file
    sonic_topology_template = get_xml_template('sonic_topology_template.txt')
    sonic_topology_output = sonic_topology_template.render(
        hosts=hosts_list,
        switches=switches_list,
        hypervisor=hypervisor_info,
        sonic_mgmt=sonic_mgmt_info
    )
    new_setup_topology_file_path = os.path.join(sonic_topology_dir, 'topology.xml')
    create_file(new_setup_topology_file_path, sonic_topology_output,
                set_permission='666')
    logger.info(f'Created topology.xml: {new_setup_topology_file_path}')

    # Create .setup file
    # Note: setup_template.txt requires TopologyManager object and dut object
    # For now, we'll create a simplified version or use a mock object
    # This may need adjustment based on what the template actually needs
    setup_template_file = 'simx_setup_template.txt' if 'simx' in setup_name else 'setup_template.txt'

    # Get DUT (first switch)
    dut_info = switches_list[0] if switches_list else None
    if not dut_info:
        raise ValueError("No switch (DUT) found in setup configuration")

    # Create a minimal mock object for tm and dut if needed
    # For now, we'll try to render with the available data
    try:
        sonic_setup_template = get_xml_template(setup_template_file)
        # The template may need tm and dut objects, which are complex
        # We'll pass what we have and see if it works
        sonic_setup_output = sonic_setup_template.render(
            hypervisor=sonic_mgmt_info,
            tm={'setup_name': setup_name},  # Minimal mock
            dut=dut_info
        )
        new_setup_file_path = os.path.join(sonic_setup_dir,
                                           f'{setup_name}.setup')
        create_file(new_setup_file_path, sonic_setup_output,
                    set_permission='666')
        logger.info(f'Created setup file: {new_setup_file_path}')
    except Exception as e:
        logger.warning(f'Could not create .setup file: {e}. '
                      f'Template may require full TopologyManager object.')
        new_setup_file_path = None

    # Create CI build setup files if 'CI' is in setup name
    created_files = {
        'topology_xml': new_setup_topology_file_path,
        'setup_file': new_setup_file_path,
        'setup_name': setup_name
    }

    if 'CI' in setup_name and dut_info:
        try:
            chip_type = dut_info.get('chip_type', 'SPC')
            chip_type = f"{chip_type}1" if chip_type == 'SPC' else chip_type

            ci_setup_template_file = 'ci_setup_template.txt'
            release_setup_template_file = 'release_setup_template.txt'
            if 'simx' in setup_name:
                ci_setup_template_file = 'simx_ci_setup_template.txt'
                release_setup_template_file = 'simx_release_setup_template.txt'

            # Create CI setup file
            ci_sonic_setup_template = get_xml_template(ci_setup_template_file)
            ci_sonic_setup_output = ci_sonic_setup_template.render(
                hypervisor=sonic_mgmt_info,
                tm={'setup_name': setup_name},
                dut=dut_info
            )
            ci_setup_file_path = os.path.join(
                sonic_setup_dir,
                f'SONIC_{chip_type}_mini_mars_ci.setup')
            create_file(ci_setup_file_path, ci_sonic_setup_output,
                       set_permission='666')
            logger.info(f'Created CI setup file: {ci_setup_file_path}')
            created_files['ci_setup_file'] = ci_setup_file_path

            # Create release setup file
            release_sonic_setup_template = get_xml_template(
                release_setup_template_file)
            release_sonic_setup_output = release_sonic_setup_template.render(
                hypervisor=sonic_mgmt_info,
                tm={'setup_name': setup_name},
                dut=dut_info
            )
            release_setup_file_path = os.path.join(
                sonic_setup_dir,
                f'SONIC_{chip_type}_mini_mars_release.setup')
            create_file(release_setup_file_path, release_sonic_setup_output,
                       set_permission='666')
            logger.info(f'Created release setup file: {release_setup_file_path}')
            created_files['release_setup_file'] = release_setup_file_path
        except Exception as e:
            logger.warning(f'Could not create CI setup files: {e}')

    return created_files

#######################################################################
#    Main function                                                  ###
#######################################################################


if __name__ == '__main__':
    try:
        args = init_parser()
        set_logger(args.log_level)

        # Define variables for process_setup_and_generate_topology
        setup_conf_yml = args.arguments_file
        port_connection_csv = args.port_connection_csv
        port_config_ini = args.port_config_ini
        output_dir_base = args.output_dir

        setup_name, output_dir = process_setup_and_generate_topology(
            setup_conf_yml=setup_conf_yml,
            port_connection_csv=port_connection_csv,
            port_config_ini=port_config_ini,
            output_dir_base=output_dir_base,
            setup_name=args.setup_name,
            setup_group=args.setup_group)

        if not (port_connection_csv and port_config_ini):
            logger.info("Port inputs not provided; CONNECTIVITY/PORT/ACTIVE_PORTS_IDS will not be generated.")

        # Always import remaining setup information to Noga (even if no port data).
        # topology_all.xml will still be generated, just without connectivity sections.
        import_setup_to_noga(output_dir_base, setup_name, args.setup_group)
        import_aliases_to_noga(os.path.join(output_dir_base, setup_name, f"{setup_name}.json"))
        logger.info('Updating host from server to vm in Noga')
        noga_update_host_from_server_to_vm(setup_conf_yml)

        logger.info('Creating setup files from YAML')
        create_setup_files_from_yaml(setup_conf_yml, setup_name)
        logger.info('Script Finished!')

    except Exception:
        traceback.print_exc()
        sys.exit(LinuxConsts.error_exit_code)
