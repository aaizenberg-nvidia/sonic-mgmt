import argparse
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from devts.infra.tools.connection_tools.linux_ssh_engine import LinuxSshEngine
from devts.infra.tools.validations.traffic_validations.port_check.port_checker import check_port_status_till_alive

from ngts.constants.constants import SSHConsts

logger = logging.getLogger()


def setup_console_logger(log_level):
    """
    Attach a console (stdout) handler to the module logger, so script progress
    is always visible regardless of any logging config inherited from the caller.
    :param log_level: logging level to print at
    """
    logger.setLevel(log_level)
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    logger.addHandler(handler)


# SPC6 rel-39_2020_1940 broken-FW recovery constants.
FW_IMAGE_PATH = ("/auto/sx_mlnx_fw/SPC6/rel-39_2020_1940/dev/SPE_binaries/"
                 "fw-Spectrum-6-rel-39_2020_1940-920-9N62F-09LI-ES1_Ax.bin")
FLINT_DEVICE = "/dev/mst/mt628_pciconf0"
I2C_DEVICE = "/dev/mst/dev-i2c-12"
I2C_ADDR = "2"
I2C_DATA_LEN = "1"
I2C_REGISTER = "0x20E0"
I2C_VALUE_BEFORE_FLASH = "0x80"
I2C_VALUE_AFTER_FLASH = "0x81"


def _i2c_reboot_cmd(i2c_value):
    return (f"mst start --with_i2cdev && "
            f"i2c -a {I2C_ADDR} -d {I2C_DATA_LEN} {I2C_DEVICE} w 0x32 {I2C_REGISTER} {i2c_value} && "
            f"reboot")


def _flint_burn_cmd(fw_image_path):
    return f"flint -d {FLINT_DEVICE} -i {fw_image_path} b"


def wait_for_reboot(engine, poll_interval, reboot_timeout):
    """
    Wait for a switch to go down and come back up after a reboot has been triggered.
    :param engine: connected LinuxSshEngine
    :param poll_interval: seconds between port checks
    :param reboot_timeout: max seconds to wait for the switch to come back up
    """
    tries = max(1, reboot_timeout // poll_interval)
    logger.info(f'{engine.ip}: waiting for shutdown')
    check_port_status_till_alive(False, engine.ip, engine.ssh_port)
    logger.info(f'{engine.ip}: port is down, disconnecting engine')
    engine.disconnect()
    logger.info(f'{engine.ip}: waiting for bring-up (poll every {poll_interval}s, up to {reboot_timeout}s)')
    check_port_status_till_alive(True, engine.ip, engine.ssh_port, tries=tries, delay=poll_interval)
    logger.info(f'{engine.ip}: port is back up')


def restore_host(host, username, password, poll_interval, reboot_timeout, fw_image_path):
    """
    Run the broken-FW recovery flow on a single DUT/TG: flip the i2c boot-source
    register, reboot, re-burn known-good FW with flint, flip the register back, reboot.
    :param host: DUT/TG hostname or IP
    :param username: DVS root username
    :param password: DVS root password
    :param poll_interval: seconds between post-reboot port checks
    :param reboot_timeout: max seconds to wait for each reboot to complete
    :param fw_image_path: path to the FW image to burn with flint
    """
    logger.info(f'{host}: starting broken-FW recovery')
    logger.info(f'{host}: connecting via SSH as user {username!r}')
    engine = LinuxSshEngine(ip=host, username=username, password=password)

    logger.info(f'{host}: step 1/3 - switching boot source and rebooting')
    cmd = _i2c_reboot_cmd(I2C_VALUE_BEFORE_FLASH)
    logger.info(f'{host}: running: {cmd}')
    engine.send_config_set([cmd], exit_config_mode=False, cmd_verify=False)
    wait_for_reboot(engine, poll_interval, reboot_timeout)

    logger.info(f'{host}: step 2/3 - burning FW with flint')
    cmd = _flint_burn_cmd(fw_image_path)
    logger.info(f'{host}: running: {cmd}')
    engine.run_cmd(cmd, validate=True)
    logger.info(f'{host}: flint burn completed')

    logger.info(f'{host}: step 3/3 - restoring boot source and rebooting')
    cmd = _i2c_reboot_cmd(I2C_VALUE_AFTER_FLASH)
    logger.info(f'{host}: running: {cmd}')
    engine.send_config_set([cmd], exit_config_mode=False, cmd_verify=False)
    wait_for_reboot(engine, poll_interval, reboot_timeout)

    logger.info(f'{host}: recovery completed successfully')


def init_parser():
    description = ('Restore a DVS switch (DUT or TG) after a broken FW install.\n'
                   'Runs on every host given: switch boot source via i2c + reboot, '
                   're-burn known-good FW with flint, switch boot source back + reboot.')

    parser = argparse.ArgumentParser(description=description, formatter_class=argparse.RawTextHelpFormatter)

    parser.add_argument('hosts', nargs='+', help='DUT/TG hostname(s) or IP(s) to recover.')

    parser.add_argument('-u', '--username', dest='username', default=SSHConsts.DVS_CREDS['username'],
                        help='DVS root username. Defaults to DVS_ROOT_USER env var.')

    parser.add_argument('-p', '--password', dest='password', default=SSHConsts.DVS_CREDS['password'],
                        help='DVS root password. Defaults to DVS_ROOT_PASSWORD env var.')

    parser.add_argument('--poll-interval', dest='poll_interval', type=int, default=10,
                        help='Seconds between post-reboot up/down checks. Default: 10.')

    parser.add_argument('--reboot-timeout', dest='reboot_timeout', type=int, default=900,
                        help='Max seconds to wait for each reboot to complete. Default: 900.')

    parser.add_argument('--fw-image-path', dest='fw_image_path', default=FW_IMAGE_PATH,
                        help=f'Path to the FW image to burn with flint. Default: {FW_IMAGE_PATH}')

    parser.add_argument('-l', '--log-level', dest='log_level', default=logging.INFO, help='log verbosity')

    arguments, unknown = parser.parse_known_args()
    if unknown:
        raise Exception(f'unknown argument(s): {unknown}')
    return arguments


def main():
    args = init_parser()
    setup_console_logger(args.log_level)
    safe_args = {k: v for k, v in vars(args).items() if k != 'password'}
    logger.info(f'Parsed arguments: {safe_args}')

    if not args.username or not args.password:
        raise Exception('DVS credentials not provided: pass --username/--password or set '
                        'DVS_ROOT_USER/DVS_ROOT_PASSWORD.')

    logger.info(f'Recovering {len(args.hosts)} host(s) in parallel: {args.hosts}')
    failed_hosts = []
    with ThreadPoolExecutor(max_workers=len(args.hosts)) as pool:
        futures = {
            pool.submit(restore_host, host, args.username, args.password,
                        args.poll_interval, args.reboot_timeout, args.fw_image_path): host
            for host in args.hosts
        }
        for future in as_completed(futures):
            host = futures[future]
            try:
                future.result()
            except Exception as err:
                logger.error(f'{host}: recovery failed: {err}')
                failed_hosts.append(host)

    if failed_hosts:
        raise Exception(f'Recovery failed on: {", ".join(failed_hosts)}')

    logger.info('Recovery completed successfully on all hosts.')


if __name__ == '__main__':
    main()
