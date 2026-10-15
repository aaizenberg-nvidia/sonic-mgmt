#!/auto/app/Python-3.6.2/bin/python
import sys
import argparse
import datetime
import logging
import os
import re
import json
import subprocess
import time
import pdb
path = os.path.abspath(__file__)
sonic_mgmt_path = path.split('/ngts/')[0]

sys.path.append(sonic_mgmt_path)
logger = logging.getLogger()

DOCKER_REGISTRY = "nbu-harbor.gtm.nvidia.com"
JENKINS_MANAGED_LABEL = "nvidia.jenkins.managed"
JENKINS_JOB_LABEL = "nvidia.jenkins.job"
JENKINS_JOB = "TEST_sonic_upload_regression_results_to_msft"
JENKINS_CONTAINER_PREFIX = "sonic-mgmt-sw-r2d2-bot-jenkins-{}-".format(JENKINS_JOB)
STALE_CONTAINER_TIMEOUT = 3 * 24 * 60 * 60
SONIC_MGMT_MOUNTPOINTS = {
    '/.autodirect/mswg/projects': '/.autodirect/mswg/projects',
    '/auto/sw_system_project': '/auto/sw_system_project',
    '/auto/sw_system_release': '/auto/sw_system_release',
    '/.autodirect/sw_system_release/': '/.autodirect/sw_system_release/',
    '/auto/sw_regression/system/SONIC': '/auto/sw_regression/system/SONIC/',
    '/.autodirect/sw_regression/system/SONIC': '/.autodirect/sw_regression/system/SONIC',
    '/workspace': '/workspace',
    '/.autodirect/LIT/SCRIPTS': '/.autodirect/LIT/SCRIPTS',
    '/.autodirect/LIT/LOGS/RR/': '/.autodirect/LIT/LOGS/RR/',
    '/.autodirect/sw/release/': '/.autodirect/sw/release/',
    '/auto/LIT/SCRIPTS/': '/auto/LIT/SCRIPTS/',
    '/auto/LIT/LOGS/RR/': '/auto/LIT/LOGS/RR/',
    '/auto/mswg/utils/bin/': '/auto/mswg/utils/bin/'
}


def init_parser():
    description = ('Functionality of the script: \n'
                   'Start a sonic-mgmt ngts docker with the version specified in update_docker.py file.\n')
    parser = argparse.ArgumentParser(description=description)

    parser.add_argument("--ngts_version", required=True, help="Ngts version tag")
    parser.add_argument("--container_name", required=True, help="Docker container name")
    parser.add_argument('-l', '--log_level', dest='log_level', default=logging.INFO, help='log verbosity')

    arguments, unknown = parser.parse_known_args()
    if unknown:
        raise Exception("unknown argument(s): {}".format(unknown))
    return arguments


def init_logger(log_level):
    """logger configuration."""
    logging.basicConfig(level=log_level,
                        format='%(asctime)s %(name)-12s %(levelname)-8s %(message)s',
                        datefmt='%m-%d %H:%M')


def create_secrets_vars_script(container_name):
    env_file_script_path = "/tmp/{CONTAINER_NAME}_env_file.sh".format(CONTAINER_NAME=container_name)
    regex = r"variableName: (.*)"
    file_dir_path = os.path.dirname(os.path.realpath(__file__))
    sonic_secrets_template_path = os.path.join(file_dir_path, "build/sonic_secret_template.yaml")
    with open(sonic_secrets_template_path) as f:
        sonic_secrets_template_content = f.read()
    env_vars = re.findall(regex, sonic_secrets_template_content)
    env_file_content = []
    for env_var in env_vars:
        env_var_value = os.getenv(env_var)
        env_file_content.append("{env_var}={env_var_value}\n".format(env_var=env_var,
                                                                     env_var_value=env_var_value))
    file_descriptor = os.open(
        env_file_script_path,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    with os.fdopen(file_descriptor, 'w') as f:
        f.writelines(env_file_content)
    os.chmod(env_file_script_path, 0o600)
    return env_file_script_path


def pull_image(ngts_version):
    image_name = "{DOCKER_REGISTRY}/sonic/docker-ngts".format(DOCKER_REGISTRY=DOCKER_REGISTRY)
    command = "sudo docker pull {image_name}:{ngts_version}".format(image_name=image_name,
                                                                    ngts_version=ngts_version)
    os.popen(command).read()
    stream = os.popen("sudo docker images")
    output = stream.read()
    logger.info("docker images:\n {output}".format(output=output))
    regex = r"harbor\.gtm\.nvidia\.com\/sonic\/docker-ngts\s+{ngts_version}".format(ngts_version=ngts_version)
    assert re.search(regex, output), "Not found {image_name}:{ngts_version} " \
                                     "in docker images after pull".format(image_name=image_name,
                                                                          ngts_version=ngts_version)
    return image_name


def get_container_mountpoints():
    container_mountpoints_dict = SONIC_MGMT_MOUNTPOINTS.items()
    container_mountpoints_list = []
    for key, value in container_mountpoints_dict:
        if key == "/workspace":
            container_mountpoints_list.append("-v {}:{}:rw".format(key, value))
        else:
            container_mountpoints_list.append("-v {}:{}:rslave".format(key, value))

    container_mountpoints = " ".join(container_mountpoints_list)
    return container_mountpoints


def remove_container(container_name):
    with open(os.devnull, "w") as devnull:
        subprocess.call(
            ["sudo", "docker", "rm", "-f", container_name],
            stdout=devnull,
            stderr=devnull,
        )


def cleanup_stale_containers():
    managed = subprocess.check_output([
        "sudo", "docker", "ps", "-aq",
        "--filter", "label={}={}".format(JENKINS_MANAGED_LABEL, "true"),
        "--filter", "label={}={}".format(JENKINS_JOB_LABEL, JENKINS_JOB),
    ]).decode().split()
    legacy = subprocess.check_output([
        "sudo", "docker", "ps", "-aq",
        "--filter", "name={}".format(JENKINS_CONTAINER_PREFIX),
    ]).decode().split()
    now = int(time.time())
    for container_id in set(managed + legacy):
        try:
            created_at = subprocess.check_output([
                "sudo", "docker", "inspect", "--format", "{{.Created}}",
                container_id,
            ]).decode().strip()
            created_at = int((datetime.datetime.strptime(created_at[:19], "%Y-%m-%dT%H:%M:%S") -
                              datetime.datetime(1970, 1, 1)).total_seconds())
        except (subprocess.CalledProcessError, ValueError):
            continue
        if now - created_at > STALE_CONTAINER_TIMEOUT:
            logger.info("Removing stale Jenkins container %s", container_id)
            remove_container(container_id)


def check_container_started(container_name, max_retries=3):
    for i in range(max_retries):
        attempt = i + 1
        logger.info("Try to start container %s, max_retries=%d, attempt=%d"
                    % (container_name, max_retries, attempt))
        try:
            os.popen("sudo docker start {container_name}".format(container_name=container_name))
            logger.info("Started container %s, max_retries=%d, attempt=%d"
                        % (container_name, max_retries, attempt))
            logger.info("Check whether the container is started successfully.")
            inspect_cmd = "sudo docker inspect --format '{{json .State}}'"
            inspect_cmd += " {container_name}".format(container_name=container_name)
            stream = os.popen(inspect_cmd)
            inspect_output = stream.read()
            container_state = json.loads(inspect_output)
            assert container_state["Running"], "The created container is not started"
            return True
        except AssertionError as e:
            logger.error("Starting container %s failed, max_retries=%d, attempt=%d" %
                         (container_name, max_retries, attempt))
        time.sleep(5)

    raise AssertionError("Failed to start container %s after tried %d times." % (container_name, max_retries))


def copy_sonic_mgmt_to_container(container_name):
    logger.info("Create a specific path in the docker container")
    create_path_cmd = "sudo docker exec --tty {container_name} /bin/mkdir " \
                      "-p /root/mars/workspace".format(container_name=container_name)
    logger.info("CMD: %s", create_path_cmd)
    os.popen("echo \"{CMD}\" >> /tmp/file.txt".format(CMD=create_path_cmd))
    stream = os.popen(create_path_cmd)
    output = stream.read()
    logger.info("CMD OUTPUT: %s", output)
    path_prefix, sonic_mgmt = sonic_mgmt_path.split("sonic-mgmt")
    tar_cmd = "cd {path_prefix} && tar -czvf sonic-mgmt.tar.gz sonic-mgmt/".format(path_prefix=path_prefix)
    logger.info("CMD: %s", tar_cmd)
    stream = os.popen(tar_cmd)
    output = stream.read()
    logger.debug("CMD OUTPUT: %s", output)
    tar_path = os.path.join(path_prefix, "sonic-mgmt.tar.gz")
    logger.info("Created tar path: %s", tar_path)
    os.popen("chmod 777 {tar_path}".format(tar_path=tar_path))
    logger.info("Copy the repository tar file inside into the docker container")
    copy_tar_to_docker = "sudo docker cp {tar_path} " \
                         "{container_name}:/root/mars/workspace/".format(tar_path=tar_path,
                                                                         container_name=container_name)

    logger.info("CMD: %s", copy_tar_to_docker)
    os.popen("echo \"{CMD}\" >> /tmp/file.txt".format(CMD=copy_tar_to_docker))
    stream = os.popen(copy_tar_to_docker)
    output = stream.read()
    logger.info("CMD OUTPUT: %s", output)
    logger.info("Open the repository tar file inside into the docker container")
    open_tar_cmd = "sudo docker exec {container_name} bash -c " \
                   "\"tar -xzvf /root/mars/workspace/sonic-mgmt.tar.gz " \
                   "-C /root/mars/workspace/\"".format(container_name=container_name)
    logger.info("CMD: %s", open_tar_cmd)
    os.popen("echo \"{CMD}\" >> /tmp/file.txt".format(CMD=open_tar_cmd))
    stream = os.popen(open_tar_cmd)
    output = stream.read()
    logger.debug("CMD OUTPUT: %s", output)


def create_sonic_mgmt_ngts_docker(ngts_version, container_name):
    cleanup_stale_containers()
    image_name = pull_image(ngts_version)
    container_mountpoints = get_container_mountpoints()
    create_secrets_vars_script(container_name)
    logger.info("Try to remove existing docker container anyway")
    remove_container(container_name)
    cmd = "sudo docker run -d --security-opt seccomp=unconfined " \
          "--label {MANAGED_LABEL}=true --label {JOB_LABEL}={JOB} " \
          "{CONTAINER_MOUNTPOINTS} " \
          "--name {CONTAINER_NAME} {IMAGE_NAME}:{IMAGE_TAG}".format(CONTAINER_MOUNTPOINTS=container_mountpoints,
                                                                    MANAGED_LABEL=JENKINS_MANAGED_LABEL,
                                                                    JOB_LABEL=JENKINS_JOB_LABEL,
                                                                    JOB=JENKINS_JOB,
                                                                    CONTAINER_NAME=container_name,
                                                                    IMAGE_NAME=image_name,
                                                                    IMAGE_TAG=ngts_version)
    try:
        subprocess.check_call(cmd, shell=True)
        logger.info("Created container, wait a few seconds for it to start")
        time.sleep(5)
        check_container_started(container_name)
        copy_sonic_mgmt_to_container(container_name)
    except Exception:
        remove_container(container_name)
        raise


if __name__ == '__main__':
    arg = init_parser()
    init_logger(arg.log_level)
    create_sonic_mgmt_ngts_docker(arg.ngts_version, arg.container_name)
    logger.info('Script Finished!')
