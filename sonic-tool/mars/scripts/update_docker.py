#!/usr/bin/env python
"""
Prepare the SONiC testing topology.


This script is executed on the STM node. It establishes SSH connection to the hypervisor (Player) and
run commands on it. Purpose is to prepare the SONiC testing topology using the testbed-cli.sh tool.
"""

# Builtin libs
import argparse
import json
import os
import sys
import time
import traceback
from ipaddress import IPv6Interface

# Third-party libs
from fabric import Config
from fabric import Connection
from invoke.exceptions import UnexpectedExit, ThreadException
from retry import retry
from retry.api import retry_call
# Home-brew libs
from lib import constants
from lib.utils import parse_topology, get_logger
from parse_escape_docker_env_secrets import parse_escape_docker_env_secrets

logger = get_logger("UpdateDocker")

CONTAINER_IFACE = "eth0"
NETWORK_NAME = "containers_network"
GET_NETWORK_CMD = "docker network ls | grep '{}'".format(NETWORK_NAME)
SONIC_MGMT_SSH_PORT = 2222
SONIC_MGMT_RPYC_PORT = 18812
CREATE_CONTAINER_MAX_TRIES = 10
NVOS = "NVOS"
SONIC = "SONIC"
SETUPS_LOCATION = {SONIC: "/.autodirect/sw_regression/system/SONIC/MARS/conf/setups",
                   NVOS: "/.autodirect/sysgwork/G/MARS_conf/stm_nvos/setups"}
# Team-specific NFS area (bind-mounted on the hypervisor via constants.*_MOUNTPOINTS) where
# container diagnostics tarballs are archived, so they don't flood the MARS console log.
DIAGNOSTICS_NFS_DIR = {SONIC: "/auto/sw_regression/system/SONIC/MARS/container_diagnostics",
                       NVOS: "/auto/sw_regression/system/NVOS/MARS/container_diagnostics"}

def _test_server_needs_ipv6(topo):
    """
    Check if the test server should enable IPv6 routing based on topology metadata.
    Returns True if the TEST_SERVER has ENABLE_IPV6=true.
    """
    test_server = topo.get_device_by_topology_id(constants.TEST_SERVER_DEVICE_ID)
    enable_ipv6 = getattr(test_server, 'ENABLE_IPV6', None)
    if isinstance(enable_ipv6, bytes):
        enable_ipv6 = enable_ipv6.decode('utf-8')
    return str(enable_ipv6).lower() == 'true'


def _parse_args():
    # Parse arguments
    parser = argparse.ArgumentParser()
    parser.add_argument("--topo", dest="topo", help="Path to the MARS topology configuration file")
    parser.add_argument("--dut-name", nargs="?", dest="dut_name", help="The DUT name")
    parser.add_argument("--registry-url", nargs="?", dest="registry_url",
                        help="Docker registry URL. Default: use lib/constants.DOCKER_REGISTRY")
    parser.add_argument("--docker-name", nargs="?", default="sonic-mgmt", dest="docker_name",
                        help="Name of the sonic-mgmt docker. Default: 'sonic-mgmt'")
    parser.add_argument("--docker-tag", nargs="?", dest="docker_tag",
                        help="Docker image tag. Optional parameter.")
    parser.add_argument("--delete_container", nargs="?", dest="delete_container", default=False,
                        help="Force container removal and recreation, even if a docker container with the expected "
                             "image is already running.")
    parser.add_argument("--send_takeover_notification", help="If set to True, the script will send a takeover "
                                                             "notification to all the active terminals and wait for "
                                                             "a predefined period before starting the deployment",
                        dest="send_takeover_notification", default='no', choices=["yes", "no"])
    parser.add_argument("--skip_weekend_cases", help="If set to yes, the script will set the env variable SKIP_WEEKEND_CASES to yes",
                        dest="skip_weekend_cases", default='yes', choices=["yes", "no"])
    parser.add_argument("--air_setup", dest="air_setup", action="store_true",
                        help="If passed, the script will run the update_docker script for Air setup")
    parser.add_argument("--setup_name", dest="setup_name", required=False, help="The setup name. Required for Air setup.")

    args = parser.parse_args()

    if args.air_setup and not args.setup_name:
        parser.error("The setup name is required for Air setup")
    return args


@retry(ThreadException, tries=3, delay=10)
def inspect_container(conn, image_name, image_tag, container_name):
    """
    @summary: Inspect the specified docker image and container, gather some basic information.
    @param conn: Fabric connection to the host server.
    @param image_name: Docker image name.
    @param image_tag: Docker image tag.
    @param container_name: Docker container name to be inspected.
    @return: Returns inspection result in a dictionary.
    """
    res = {
        "container_exists": None,
        "container_running": None,
        "container_matches_image": None,
        "image_exists": None
    }

    res["container_exists"] = conn.run("docker ps -a -f name=%s$ --format '{{.Names}}'" % container_name).\
        stdout.strip() == container_name
    image_id = conn.run("docker images --no-trunc %s:%s --format '{{.ID}}'" % (image_name, image_tag)).stdout.strip()

    if len(image_id) > 0:
        res["image_exists"] = True

    if res["container_exists"]:
        container_info = json.loads(conn.run("docker inspect {}".format(container_name)).stdout.strip())
        try:
            res["container_running"] = container_info[0]["State"]["Running"]

            if res["image_exists"]:
                res["container_matches_image"] = image_id == container_info[0]["Image"]
        except (IndexError, KeyError) as e:
            logger.error("Failed to get container info, exception: %s" + repr(e))
    logger.info("Inspection result: %s" % json.dumps(res))
    return res


def start_container(conn, container_name, max_retries=3):
    """
    @summary: Try to start an existing container.
    @param conn: Fabric connection to the host server.
    @param container_name: Docker container name.
    @param max_retries: Max number of retries.
    @return: Returns True if starting container succeeded. Returns False if starting failed.
    """
    for i in range(max_retries):
        attempt = i + 1
        logger.info("Try to start container %s, max_retries=%d, attempt=%d" % (container_name, max_retries, attempt))
        try:
            conn.run("docker start %s" % container_name)
            logger.info("Started container %s, max_retries=%d, attempt=%d" % (container_name, max_retries, attempt))
            return True
        except UnexpectedExit as e:
            logger.error("Starting container exception: %s" % repr(e))
        logger.error("Starting container %s failed, max_retries=%d, attempt=%d" %
                     (container_name, max_retries, attempt))
        time.sleep(2)
    logger.error("Failed to start container %s after tried %d times." % (container_name, max_retries))
    return False


def create_mgmt_network(conn):
    """
    @summary: create management network for containers - macvlan networt
    """
    logger.info('Create macvlan network %s', NETWORK_NAME)
    if not conn.run(GET_NETWORK_CMD, warn=True).stdout.strip():
        ip_route_info = conn.run('ip route | grep default').stdout.strip()
        ip_gw = ip_route_info.split()[2]
        ip_iface = ip_route_info.split()[4]
        ip_info = conn.run("ip addr | grep  -A1 ' '{} | grep 'inet'".format(ip_iface)).stdout.strip()
        our_addr = ip_info.split()[1].split('/')[0]
        net_info_data = conn.run('ip route | grep -m1 "scope link src {}"'.format(our_addr)).stdout.strip()
        net_info = net_info_data.split()[0]
        ipv6_info = conn.run("ip -6 addr show {} | grep 'inet6' | grep -v '/128'".format(ip_iface)).stdout.strip()
        our_addr_v6 = ipv6_info.split()[1]
        ipv6 = IPv6Interface(our_addr_v6)
        ipv6_net_info = str(ipv6.network)
        ipv6_gw = str(ipv6.network.network_address + 1)
        cmd = 'docker network create -d macvlan --ipv6 --gateway={} --subnet={} --gateway={} --subnet={} -o parent={} {}'.format(ip_gw, net_info, ipv6_gw, ipv6_net_info, ip_iface, NETWORK_NAME)
        conn.run(cmd)
        if not conn.run(GET_NETWORK_CMD, warn=True).stdout.strip():
            raise Exception("Docker mgmt network was not created!")
    else:
        logger.info('macvlan network \"%s\" - already exist', NETWORK_NAME)


def create_secrets_vars_script(conn, parsed_mars_docker_env_secrets, container_name, skip_weekend_cases):
    export_env_var_script_path = "/tmp/{CONTAINER_NAME}_export_env_var.sh".format(CONTAINER_NAME=container_name)
    if os.path.exists(export_env_var_script_path):
        conn.run("rm -f {SCRIPT_PATH}".format(SCRIPT_PATH=export_env_var_script_path), warn=True)
    script_content = ["export {0}".format(env_var.replace("$", r"\$")) for env_var in parsed_mars_docker_env_secrets]
    script_content.append("export SKIP_WEEKEND_CASES={}".format(skip_weekend_cases))
    script_content = ["#!/bin/bash"] + script_content
    for line in script_content:
        conn.run("echo \"{LINE}\" >> {SCRIPT_PATH}".format(LINE=line, SCRIPT_PATH=export_env_var_script_path), warn=True)
    return export_env_var_script_path

# we got intermittent threaded socket errors randomly occurs in CI
# there's a similar issue reported https://github.com/paramiko/paramiko/issues/998
@retry(ThreadException, tries=3, delay=10)
def create_and_start_container(conn, image_name, image_tag, container_name, mac_address, skip_weekend_cases, air_setup,
                               enable_ipv6=False, topology_file_path=''):
    """
    @summary: Create and start specified container from specified image
    @param conn: Fabric connection to the host server
    @param image_name: Docker image name
    @param image_tag: Docker image tag
    @param container_name: Docker container name to be started
    @param mac_address: MAC address of the container's management interface
    @param skip_weekend_cases: Skip weekend cases
    @param air_setup: Air setup
    @param enable_ipv6: If True, configure default IPv6 route for dual-stack switch access
    @param topology_file_path: topology file path (used to detect project type via get_setup_type)
    """
    container_iface_mac = mac_address
    setup_type = get_setup_type(topology_file_path)
    create_mgmt_network(conn)
    mountpoints = dict(constants.COMMON_MOUNTPOINTS)
    if not air_setup:
        mountpoints.update(constants.BARE_METAL_MOUNTPOINTS)
    if setup_type == NVOS:
        mountpoints.update(constants.NVOS_MOUNTPOINTS)
    else:
        mountpoints.update(constants.SONIC_MOUNTPOINTS)
    if conn.host in constants.MTBC_SERVER_LIST:
        mountpoints.update(constants.SONIC_MGMT_MOUNTPOINTS_MTBC)
    if conn.host in constants.MTL_NVOS_SERVER_LIST:
        mountpoints.update(constants.MTL_NVOS_MOUNTPOINTS)

    container_mountpoints_list = []
    for key, value in mountpoints.items():
        if key == "/workspace":
            container_mountpoints_list.append("-v {}:{}:rw".format(key, value))
        else:
            container_mountpoints_list.append("-v {}:{}:rslave".format(key, value))
    container_mountpoints = " ".join(container_mountpoints_list)

    parsed_mars_docker_env_secrets = parse_escape_docker_env_secrets(os.getenv("MARS_DOCKER_ENV_SECRETS"))
    secrets_vars_script_path = create_secrets_vars_script(conn, parsed_mars_docker_env_secrets, container_name, skip_weekend_cases)
    cmd_tmplt = "docker run --init -d -t --cap-add=NET_ADMIN {CONTAINER_MOUNTPOINTS} " \
                "--privileged --mac-address={MAC_ADDRESS} " \
                "--env ANSIBLE_CONFIG=/root/mars/workspace/sonic-mgmt/ansible/ansible.cfg --env {MARS_DOCKER_ENV_SECRETS} " \
                "--env SKIP_WEEKEND_CASES={SKIP_WEEKEND_CASES} " \
                "--name {CONTAINER_NAME}"
    cmd = cmd_tmplt.format(
        CONTAINER_MOUNTPOINTS=container_mountpoints,
        MAC_ADDRESS=container_iface_mac,
        CONTAINER_NAME=container_name,
        MARS_DOCKER_ENV_SECRETS=" --env ".join(parsed_mars_docker_env_secrets),
        SKIP_WEEKEND_CASES=skip_weekend_cases
    )
    if air_setup:
        cmd += " -p {}:{} -p {}:{}".format(SONIC_MGMT_SSH_PORT, 22, SONIC_MGMT_RPYC_PORT, 18812)
    else:
        cmd += " --network=containers_network"
    cmd += " {}:{} /bin/bash".format(image_name, image_tag)
    logger.info("Try to remove existing docker container anyway")
    conn.run("docker rm -f {CONTAINER_NAME}".format(CONTAINER_NAME=container_name), warn=True)

    # dict, not int: mutated in place so the closure count survives @retry without `nonlocal` (Py2.7 has none)
    create_attempt = {"count": 0}

    @retry(exceptions=AssertionError, tries=CREATE_CONTAINER_MAX_TRIES, delay=60)
    def _create_container():
        create_attempt["count"] += 1
        logger.info("Container creation attempt {}/{}".format(create_attempt["count"], CREATE_CONTAINER_MAX_TRIES))
        conn.run(cmd, warn=True)
        logger.info("Created container, wait a few seconds for it to start")
        time.sleep(5)
        logger.info("Check whether the container is started successfully.")
        try:
            container_state = json.loads(conn.run("docker inspect --format '{{json .State}}' %s" % container_name)
                                         .stdout.strip())
        except UnexpectedExit as err:
            logger.error(err)
            assert False, "Failed to inspect the docker status."
        if not container_state["Running"]:
            logger.error("The created container is not started, try to restart it")
            if not start_container(conn, container_name, max_retries=1):
                logger.error("Restart container failed (attempt {}/{}). "
                             "Remove the container and delay 60s before recreating."
                             .format(create_attempt["count"], CREATE_CONTAINER_MAX_TRIES))
                if create_attempt["count"] >= CREATE_CONTAINER_MAX_TRIES:
                    dump_container_diagnostics(conn, container_name, setup_type)
                conn.run("docker rm -f {CONTAINER_NAME}".format(CONTAINER_NAME=container_name), warn=True)
                assert False, "Failed to create the container."
    _create_container()
    try:
        validate_docker_is_up(conn, container_name)
    except Exception:
        logger.error("Container {} did not respond right after creation - dumping diagnostics"
                     .format(container_name))
        dump_container_diagnostics(conn, container_name, setup_type)
        raise
    logger.info("Configure container after starting it")
    copy_script_cmd = "docker cp {SCRIPT_PATH} " \
                      "{CONTAINER_NAME}:/etc/profile.d/".format(SCRIPT_PATH=secrets_vars_script_path,
                                                                CONTAINER_NAME=container_name)
    conn.run(copy_script_cmd, warn=True)
    conn.run("rm -f {SCRIPT_PATH}".format(SCRIPT_PATH=secrets_vars_script_path), warn=True)
    if air_setup:
        conn.run('docker exec {CONTAINER_NAME} bash -c "sudo /etc/init.d/ssh restart"'
            .format(CONTAINER_NAME=container_name))
    elif not configure_docker_route(conn, container_name, enable_ipv6):
        logger.error("Configure docker container failed.")
        sys.exit(1)

@retry(Exception, tries=3, delay=10)
def validate_docker_is_up(conn, container_name):
    """
    This function will run a dummy echo command on docker containers,
    In order to validate the container is really up.
    This function purpose is to avoid failures,
    when configuring the docker (during configure_docker_route) and the docker is not up yet.
    :param conn: Fabric connection to the host server
    :param container_name: Docker container name that was started
    :return: raise Exception if docker is not up after all retries failed
    """
    logger.info("Validating docker {CONTAINER_NAME} is up by running dummy echo cmd"
                .format(CONTAINER_NAME=container_name))
    conn.run("docker exec {CONTAINER_NAME} bash -c \"echo \"UP\"\"".format(CONTAINER_NAME=container_name))

def _get_macvlan_ipv6_gateway(conn):
    """
    Derive the IPv6 gateway from the hypervisor's physical interface.
    Uses the same logic as create_mgmt_network: first global-scope IPv6 on the
    default-route interface, gateway = network_address + 1.
    """
    ip_route_info = conn.run('ip route | grep default').stdout.strip()
    ip_iface = ip_route_info.split()[4]
    ipv6_line = conn.run(
        "ip -6 addr show {iface} | grep 'inet6' | grep 'scope global' | head -1".format(iface=ip_iface)
    ).stdout.strip()
    addr_str = ipv6_line.split()[1]
    if isinstance(addr_str, bytes):
        addr_str = addr_str.decode('utf-8')
    ipv6_iface = IPv6Interface(u'{}'.format(addr_str))
    return str(ipv6_iface.network.network_address + 1)


def configure_docker_route(conn, container_name, enable_ipv6=False):
    """
    @summary: Configure docker container.
    For the sonic-mgmt container to get IP address from LAB DHCP server.
    Optionally adds a default IPv6 route for dual-stack access to the switch.
    @param conn: Fabric connection to the host server
    @param container_name: Docker container name to be started
    @param enable_ipv6: If True, add default IPv6 route via the macvlan network gateway
    @return: Returns True if configurations are successful. In case of exception, return False.
    """
    try:
        logger.info("Try to configure route and execute dhclient on container")
        # Remove existing default IP assigned by dockerd to macvlan eth0 interface
        available_ips_info = conn.run('docker exec {CONTAINER_NAME} bash -c "sudo ip -j address"'
                                      .format(CONTAINER_NAME=container_name)).stdout.strip()
        ip_address_dict = json.loads(available_ips_info)
        for interface_data in ip_address_dict:
            if CONTAINER_IFACE in interface_data['ifname']:
                available_ip = interface_data['addr_info']
                for ip_data in available_ip:
                    if ip_data['family'] == "inet":
                        cmd = 'ip addr del {}/{} dev {}'.format(ip_data['local'], ip_data['prefixlen'], CONTAINER_IFACE)
                        conn.run('docker exec {CONTAINER_NAME} bash -c "sudo {CMD}"'
                                 .format(CONTAINER_NAME=container_name, CMD=cmd))
        # Connect default bridge network in container which will be used for access hypervisor IP
        conn.run('docker network connect bridge {CONTAINER_NAME}'.format(CONTAINER_NAME=container_name))
        # Remove default route via "bridge" network(added by default after connect network)
        conn.run('docker exec {CONTAINER_NAME} bash -c "sudo ip route del default"'
                 .format(CONTAINER_NAME=container_name), warn=True)
        # Get hypervisor IP in "bridge" network
        parser_line = '{{ .NetworkSettings.Networks.bridge.Gateway }}'
        hyper_local_ip = conn.run("docker inspect -f '{}' {}".format(parser_line, container_name)).stdout.strip()
        # Add route to hypervisor via "bridge" network
        conn.run('docker exec {CONTAINER_NAME} bash -c "sudo ip route add {HYPER_IP} via {HYPER_DOCKER_IP}"'
                 .format(CONTAINER_NAME=container_name, HYPER_IP=conn.host, HYPER_DOCKER_IP=hyper_local_ip))
        # Run dhclient on macvlan network and get public IP/default route from DHCP server based on MAC address
        conn.run('docker exec {CONTAINER_NAME} bash -c "sudo dhclient {CONTAINER_IFACE} -v"'
                 .format(CONTAINER_NAME=container_name, CONTAINER_IFACE=CONTAINER_IFACE))
        # Add default IPv6 route for dual-stack access to the switch.
        # The macvlan network already gives the container an IPv6 address via SLAAC.
        # We just need a default route via the macvlan gateway (derived from hypervisor's interface).
        if enable_ipv6:
            ipv6_gw = _get_macvlan_ipv6_gateway(conn)
            logger.info("Adding default IPv6 route via macvlan gateway %s on %s", ipv6_gw, CONTAINER_IFACE)
            conn.run('docker exec {CONTAINER_NAME} bash -c "sudo ip -6 route add default via {GW} dev {IFACE}"'
                     .format(CONTAINER_NAME=container_name, GW=ipv6_gw,
                             IFACE=CONTAINER_IFACE), warn=True)
        conn.run('docker exec {CONTAINER_NAME} bash -c "sudo /etc/init.d/ssh restart"'
                 .format(CONTAINER_NAME=container_name))
        logger.info("Successfully configured route and executed dhclient on container")
        return True
    except UnexpectedExit as e:
        logger.error("Exception: %s" % repr(e))
        logger.error("Configure route & dhclient on container failed.")
        return False

def dump_container_diagnostics(conn, container_name, setup_type):
    """
    @summary:
     Capture container- and hypervisor-level diagnostics for a container that failed to
     come up or died before the script finished.
     All commands run with warn=True: the container may already be gone, and a failing
     diagnostic command must not mask the real failure being reported.
    :param conn: Fabric connection to the hypervisor (Player) host
    :param container_name: Docker container name - collected for, and used as the per-container subdir
    :param setup_type: SONIC or NVOS - selects the NFS area the archive is stored in
    """
    logger.error("################### Collecting diagnostics for {} ###################".format(container_name))
    # timestamp keeps successive failures on the same container from overwriting each other
    stamp = time.strftime("%Y%m%d_%H%M%S")
    staging_dir = "/tmp/diag_{}_{}".format(container_name, stamp)
    nfs_dir = "{}/{}".format(DIAGNOSTICS_NFS_DIR.get(setup_type, DIAGNOSTICS_NFS_DIR[SONIC]), container_name)
    archive_path = "{}/{}.tar.gz".format(nfs_dir, stamp)
    diagnostics = {
        "docker_inspect.txt": "docker inspect {}".format(container_name),
        "docker_logs.txt": "docker logs --timestamps --tail 500 {}".format(container_name),
        "docker_events.txt": "docker events --since 30m --until 0m --filter container={}".format(container_name),
        "journalctl_kernel.txt": "journalctl -k --since '30 minutes ago' --no-pager",
        "journalctl_docker.txt": "journalctl -u docker --since '30 minutes ago' --no-pager",
        "free.txt": "free -m",
        "df.txt": "df -h",
    }
    # hide=True: keep the (potentially large) diagnostic output out of the MARS console log;
    # it lives in the archive instead.
    try:
        conn.run("rm -rf {0} && mkdir -p {0}".format(staging_dir), warn=True, hide=True, in_stream=False)
        for filename, cmd in diagnostics.items():
            conn.run("{{ {0}; }} > {1}/{2} 2>&1".format(cmd, staging_dir, filename),
                     warn=True, hide=True, in_stream=False)
        conn.run("mkdir -p {}".format(nfs_dir), warn=True, hide=True, in_stream=False)
        conn.run("tar -czf {0} -C {1} .".format(archive_path, staging_dir), warn=True, hide=True, in_stream=False)
        conn.run("rm -rf {}".format(staging_dir), warn=True, hide=True, in_stream=False)
    except Exception as err:
        logger.error("Diagnostics collection for {} failed: {}".format(container_name, err))
        return
    logger.error("Diagnostics for {} archived to {}".format(container_name, archive_path))
    logger.error("################### End diagnostics for {} ###################".format(container_name))

def cleanup_dangling_docker_images(test_server):
    """
    @summary:
     When running docker system prune -a, it will remove both unused and dangling images.
     Therefore any images being used in a container,
     whether they have been exited or currently running, will NOT be affected.
    """
    try:
        test_server.run("docker system prune --all -f", warn=True, in_stream=False)
    except Exception as err:
        logger.warning("Failed to prune dangling docker images, continuing: {}".format(err))

def get_setup_type(topology_file_path):
    if topology_file_path and 'stm_nvos' in topology_file_path:
        return NVOS
    else:
        return SONIC

def get_test_server_device(args, topo):
    """
    Get test server device
    """
    test_server_device_username = os.getenv("TEST_SERVER_USER")
    test_server_device_password = os.getenv("TEST_SERVER_PASSWORD")

    if args.air_setup:
        setup_type = get_setup_type(args.topo)
        logger.info("Setup type: {}".format(setup_type))
        simulation_details_path = '{}/{}/simulation_details.json'.format(SETUPS_LOCATION[setup_type], args.setup_name)
        with open(simulation_details_path, 'r') as file:
            simulation_details = json.load(file)
        server_ssh_service = next(
            (service for service in simulation_details['services'] if service['name'] == 'hypervisor-22'), None)
        if not server_ssh_service:
            raise Exception("Server SSH service not found for setup {}".format(args.setup_name))
        server_ip = server_ssh_service['external_host']
        server_port = server_ssh_service['external_port']
    else:
        test_server_device = topo.get_device_by_topology_id(constants.TEST_SERVER_DEVICE_ID)
        server_ip = test_server_device.BASE_IP
        server_port = 22
    test_server = Connection(server_ip, port=server_port, user=test_server_device_username,
                            config=Config(overrides={"run": {"echo": True}}),
                            connect_kwargs={"password": test_server_device_password})
    return test_server

def main():
    args = _parse_args()
    docker_registry = constants.AIR_DOCKER_REGISTRY if args.air_setup else constants.DOCKER_REGISTRY
    registry_url = '{}/sonic'.format(docker_registry)
    logger.info("Default registry_url=%s" % registry_url)
    if args.registry_url:
        registry_url = args.registry_url
        logger.info("Override default registry_url value, now registry_url=%s" % registry_url)
    docker_image_name = constants.DOCKER_NGTS_IMAGE_NAME
    if args.docker_tag:
        docker_tag = args.docker_tag
    else:
        docker_tag = get_docker_default_tag(docker_image_name)
    if args.dut_name:
        container_name = '{}_{}'.format(args.dut_name, docker_image_name)
    else:
        container_name = args.docker_name
    topo = parse_topology(args.topo)
    docker_host = topo.get_device_by_topology_id(constants.SONIC_MGMT_DEVICE_ID)
    mac = docker_host.MAC_ADDRESS
    enable_ipv6 = _test_server_needs_ipv6(topo)
    if enable_ipv6:
        logger.info("Test server has ENABLE_IPV6 - will configure dual-stack routing on sonic-mgmt container")
    test_server = get_test_server_device(args, topo)
    test_server.open()
    test_server.transport.set_keepalive(30)
    if args.send_takeover_notification == 'yes':
        send_takeover_notification(topo)
    logger.info("Set hypervisor timezone to IST")
    retry_call(test_server.run, fargs=["sudo timedatectl set-timezone Asia/Jerusalem"], tries=3, delay=5, logger=logger)
    logger.info("Pull docker image to ensure that it is up to date")
    retry_call(test_server.run, fargs=["docker pull {}/{}:{}".format(registry_url, docker_image_name, docker_tag)],
               tries=3, delay=10, logger=logger)
    logger.info("Check current docker container and image status")
    inspect_res = inspect_container(test_server, "{}/{}".format(registry_url, docker_image_name), docker_tag,
                                    container_name)
    if not inspect_res["image_exists"]:
        logger.error("No docker image. Please check using commands:")
        logger.error("    curl -X GET http://{}/v2/_catalog".format(registry_url))
        logger.error("    curl -X GET http://{}/v2/{}/tags/list".format(registry_url, docker_image_name))
        sys.exit(1)
    delete_container_required = args.delete_container
    if not delete_container_required:
        if inspect_res["container_matches_image"]:
            if inspect_res["container_running"]:
                logger.info("The {} docker container with expected image is running.".format(container_name))
                logger.info("################### DONE ###################")
                sys.exit(0)
            else:
                logger.info("The {} docker container using expected image is stopped. Try to start it")
                if start_container(test_server, container_name, max_retries=3):
                    if configure_docker_route(test_server, container_name, enable_ipv6):
                        logger.info("################### DONE ###################")
                        sys.exit(0)
                    else:
                        logger.error("Failed to configure routes and dhclient on container. "
                                     "Try to delete and re-create")
                        delete_container_required = True
                else:
                    logger.error("Starting container %s failed. Will delete it and re-create" % container_name)
                    delete_container_required = True
    logger.info("Need to create and start sonic-mgmt container")
    create_and_start_container(test_server, "{}/{}".format(registry_url, docker_image_name),
                               docker_tag, container_name, mac, args.skip_weekend_cases, args.air_setup,
                               enable_ipv6, args.topo)

    logger.info("Try to delete dangling docker images to save space")
    # docker system prune also removes stopped containers - verify the container is alive
    # and dump its diagnostics BEFORE the prune can erase the evidence; prune runs either way.
    try:
        validate_docker_is_up(test_server, container_name)
    except Exception as err:
        logger.error("Container {} is not up right before docker prune: {}".format(container_name, err))
        dump_container_diagnostics(test_server, container_name, get_setup_type(args.topo))
        raise
    finally:
        cleanup_dangling_docker_images(test_server)
    logger.info("################### DONE ###################")


def get_docker_default_tag(docker_name):
    latest = "latest"
    default_list = {constants.DOCKER_NGTS_IMAGE_NAME: constants.DOCKER_NGTS_DEFAULT_TAG}
    return default_list.get(docker_name, latest)


def send_takeover_notification(topo):
    wait_between_notf_to_regression_start = 3
    players_to_be_notified = [constants.SONIC_MGMT_DEVICE_ID, constants.DUT_DEVICE_ID]
    players_were_notified = False
    for player in players_to_be_notified:
        player_info = topo.get_device_by_topology_id(player)
        player_info_username, player_info_password = topo.get_user_access(player_info.USERS[0])
        try:
            notify_player_users(player_info, wait_between_notf_to_regression_start,
                                player_info_username, player_info_password)
            players_were_notified = True
        except Exception:
            logger.warning("Unable to connect to {}:{}, in order to notify logged users about regression "
                           "start".format(player, player_info.BASE_IP))
    if players_were_notified:
        logger.info("Sleeping for {} minutes".format(wait_between_notf_to_regression_start))
        time.sleep(wait_between_notf_to_regression_start * 60)


def notify_player_users(player_info, wait_between_notf_to_regression_start, player_info_username, player_info_password):
    takeover_message = "Mars regression is taking over in {} minutes. Please save your work and logout".\
        format(wait_between_notf_to_regression_start)
    player_ssh_port = getattr(player_info, "PORT", 22)
    player_engine = Connection(player_info.BASE_IP, port=player_ssh_port, user=player_info_username,
                               config=Config(overrides={"run": {"echo": True}}),
                               connect_kwargs={"password": player_info_password},
                               connect_timeout=5)
    logger.info("Sending takeover notification to:{}".format(player_info.BASE_IP))
    player_engine.run('wall {}'.format(takeover_message))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
