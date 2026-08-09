"""

conftest.py

Defines the methods and fixtures which will be used by pytest

"""

import argparse
import pytest
import logging
import re
import os

from ngts.constants.constants import MarsConstants

logger = logging.getLogger()

SDK_SOURCE_OPTIONS = {
    "latest": "Resolve the newest published SDK package for the DUT SDK branch "
              "(lastrc DEBS lookup).",
    "image": "Use the SDK version already installed on the DUT/image; skip the "
             "latest-SDK lookup.",
}


def _parse_sdk_source(value):
    """Validate ``--sdk_source`` and return a normalized option name.

    Args:
        value (str): User-provided ``--sdk_source`` value.

    Returns:
        str: ``'latest'`` or ``'image'``.

    Raises:
        argparse.ArgumentTypeError: If ``value`` is not one of the supported options.
    """
    if value in SDK_SOURCE_OPTIONS:
        return value

    options_help = "\n".join(
        f"  {name}: {description}" for name, description in SDK_SOURCE_OPTIONS.items()
    )
    raise argparse.ArgumentTypeError(
        f"Invalid --sdk_source={value!r}. Supported values: {', '.join(SDK_SOURCE_OPTIONS)}.\n"
        f"Options:\n{options_help}\n"
        f"If omitted, the platform default is used (SONiC: latest, Cumulus: image)."
    )


def pytest_addoption(parser):
    """
    Parse pytest options
    :param parser: pytest builtin
    """
    logger.info('Parsing deploy type')
    parser.addoption('--deploy_type', action='store', choices=['onie', 'sonic', 'bfb', 'pxe'], required=False, default='onie',
                     help='Deploy type')
    logger.info('Parsing apply_base_config')
    parser.addoption('--apply_base_config', action='store', required=False, default=None,
                     help='Apply base config or not, for canonical True, for community False')
    logger.info('Parsing reboot after install')
    parser.addoption('--reboot_after_install', action='store', required=False, default=None,
                     help='Reboot after installation or not to overcome swss issue')
    logger.info('Parsing is shutdown bgp ')
    parser.addoption('--is_shutdown_bgp', action='store_true', required=False, default=False,
                     help='For sonic install, need shutdown bgp, or it can not access the external IP')
    logger.info('Parsing fw_pkg')
    parser.addoption('--fw_pkg_path', action='store', required=False, default=None,
                     help='firmware package file path')
    logger.info('Parsing base-version')
    parser.addoption("--base-version", action="store", default="",
                     help="URL or path to the SONiC image. Firstly upgrade switch to this version.")
    logger.info('Parsing base-version-dpu')
    parser.addoption("--base-version-dpu", action="store", default="",
                     help="URL or path to the SONiC image for dpu.")
    logger.info('Parsing target-version')
    parser.addoption("--target-version", action="store",
                     help="URL or path to the SONiC image. If this argument is specified, upgrade switch to this \
                              version after upgraded to the base_version. Default: ''")
    logger.info('Parsing serve_files')
    parser.addoption("--serve_files", action="store",
                     help="Specify whether to run http server on the running machine and serve the installer files"
                          "Note: this option is not supported when running from a docker without ip")
    logger.info('Parsing upgrade-only')
    parser.addoption("--upgrade-only", action="store", default="no", choices=["yes", "no"],
                     help="Specify whether to skip topology change and only do upgrade. Default: 'no'")
    logger.info('Parsing deploy_only_target')
    parser.addoption("--deploy_only_target", action="store", default='no', choices=["yes", "no"],
                     dest="deploy_only_target", help="If yes - then the installation of the base version will be "
                                                     "skipped and the target version will be installed instead of "
                                                     "the base.")
    logger.info('Parsing deploy_fanout')
    parser.addoption("--deploy_fanout", help="Specify whether to do fanout deployment. Default is 'no'",
                     choices=["no", "yes"], action="store", default="no")
    logger.info('Parsing onyx_image_url')
    parser.addoption("--onyx_image_url", help="Specify Onyx image url for the fanout switch deployment"
                                              " Example: http://nbu-mtr-nfs.nvidia.com/mswg/release/sx_mlnx_os/lastrc_3_9_3000/X86_64/image-X86_64-3.9.3004-002.img",
                     action="store", default=None)
    logger.info('Parsing port-number')
    parser.addoption("--port-number", action="store", default="",
                     help="Specify the test setup's number of ports. Default: ''")
    logger.info('Parsing recover_by_reboot')
    parser.addoption("--recover_by_reboot", help="If post validation install validation has failed, "
                                                 "reboot the dut and run post validation again."
                                                 "This flag might be useful when the first boot has failed due to fw upgrade timeout",
                     default=True, action='store_true')
    logger.info('Parsing reboot')
    parser.addoption("--reboot", action="store", default="no",
                     choices=["no", "random"] + list(MarsConstants.REBOOT_TYPES.keys()),
                     help="Specify whether reboot the switch after deploy. Default: 'no'")
    logger.info('Parsing additional-apps')
    parser.addoption("--additional-apps", help="Specify url to WJH debian package or JSON data of app extensions",
                     default="", action="store")
    parser.addoption("--wjh-deb-url", help="Specify url to WJH debian package", default="", action="store")

    logger.info('Parsing workspace-path')
    parser.addoption("--workspace-path", help="Specify workspace path",
                     default="/root/mars/workspace/", action="store")
    logger.info('Parsing post_validation')
    parser.addoption("--post_validation", help="Specify whether do post installation validation",
                     default=False, action="store")

    logger.info('Parsing destination hwsku')
    parser.addoption("--dest_hwsku", help="The destination hwsku", default="", action="store")

    logger.info('Parsing fanout_target_version')
    parser.addoption("--fanout_target_version", help="The target version of fanout. Only for SONiC.", default=None)

    logger.info('Parsing custom_config_db_air_path')
    parser.addoption('--custom_config_db_air_path', required=False, default="", action="store",
                     help='path to custom config_db.json file for AIR simulations')

    logger.info('Parsing deploy_testbed_in_parallel')
    parser.addoption("--deploy_testbed_in_parallel", action="store", default='no', choices=["yes", "no"], required=False,
                     help="Deploy testbed on multi servers setup in parallel")

    logger.info('Parsing deploy_image_only')
    parser.addoption("--deploy_image_only", action="store", default='no', choices=["yes", "no"], required=False,
                     help="Deploy the image only, skip add-topo and remove-topo")
    logger.info('Parsing deploy_chipless')
    parser.addoption("--deploy_chipless", help="Specify whether to do chipless deployment. Default is 'no'",
                     choices=["no", "yes"], action="store", default="no")

    logger.info('Parsing deploy_sequential')
    parser.addoption("--deploy_sequential", action="store", default='no', choices=["yes", "no"], required=False,
                     help="Run deploy steps serially and avoid thread pools")

    logger.info('Parsing base-version-bmc')
    parser.addoption("--base-version-bmc", action="store", default="",
                     help="URL or path to the SONiC BMC image tarball "
                          "(containing sonic_tftp_install.fit and sonic-aspeed-arm64-emmc.img.gz). "
                          "Empty value skips BMC installation.")
    logger.info('Parsing neighbor_mode')
    parser.addoption("--neighbor_mode", action="store", default="",
                     help="Neighbor mode to use for the dual-tor deployment")
    logger.info('Parsing sdk_source')
    parser.addoption(
        "--sdk_source",
        action="store",
        default=None,
        type=_parse_sdk_source,
        metavar="{latest,image}",
        help=(
            "Where to take the SDK package from for traffic generator installation. "
            "Supported values: "
            "'latest' - resolve the newest published SDK for the DUT branch; "
            "'image' - use the SDK version from the installed DUT/image. "
            "If omitted, use the platform default (SONiC: latest, Cumulus: image)."
        ),
    )


@pytest.fixture(scope="module")
def workspace_path(request):
    """
    Method for getting workspace path from pytest arguments
    :param request: pytest builtin
    :return: workspace path
    """
    return request.config.getoption('--workspace-path')


@pytest.fixture(scope="module")
def deploy_type(request):
    """
    Method for getting deploy type from pytest arguments
    :param request: pytest builtin
    :return: deploy type
    """
    return request.config.getoption('--deploy_type')


@pytest.fixture(scope="module")
def base_version(request):
    """
    Method for getting base version from pytest arguments
    :param request: pytest builtin
    :return: base version
    """
    return request.config.getoption('--base-version')


@pytest.fixture(scope="session")
def base_version_dpu(request):
    """
    Method for getting base version for dpu from pytest arguments
    :param request: pytest builtin
    :return: base version
    """
    return request.config.getoption('--base-version-dpu')


@pytest.fixture(scope="module")
def target_version(request):
    """
    Method for getting target version from pytest arguments
    :param request: pytest builtin
    :return: target version
    """
    return request.config.getoption('--target-version')


@pytest.fixture(scope="module")
def serve_files(request):
    """
    Method for getting serve files from pytest arguments
    :param request: pytest builtin
    :return: serve files
    """
    return request.config.getoption('--serve_files')


@pytest.fixture(scope="module")
def upgrade_only(request):
    """
    Method for getting upgrade-only from pytest arguments
    :param request: pytest builtin
    :return: True if upgrade-only == yes, False - otherwise
    """
    upgrade_only_arg = request.config.getoption('--upgrade-only')
    if upgrade_only_arg and re.match(r"^(no|false)$", upgrade_only_arg, re.I):
        return True
    return False


@pytest.fixture(scope="module")
def post_installation_validation(request):
    """
    Method for getting post-validation from pytest arguments
    :param request: pytest builtin
    :return: post_validation (True or False)
    """
    return request.config.getoption('--post_validation')


@pytest.fixture(scope="module")
def destination_hwsku(request):
    """
    Method for getting post-validation from pytest arguments
    :param request: pytest builtin
    :return: destination hwsku
    """
    return request.config.getoption('--dest_hwsku')


@pytest.fixture(scope="module")
def fanout_target_version(request):
    """
    Target version of fanout SONiC image
    :param request: pytest builtin
    :return: Image path of fanout SONiC image
    """
    fanout_target_version = request.config.getoption('--fanout_target_version')
    if fanout_target_version == "":
        return None
    return fanout_target_version


@pytest.fixture(scope="module")
def deploy_only_target(request):
    """
    Method for getting deploy only target from pytest arguments
    :param request: pytest builtin
    :return: True/False
    """
    deploy_only_target_arg = request.config.getoption('--deploy_only_target')
    return deploy_only_target_arg == "yes"


@pytest.fixture(scope="module")
def apply_base_config(request):
    """
    Method for getting base version from pytest arguments
    :param request: pytest builtin
    :return: apply base config flag
    """
    return request.config.getoption('--apply_base_config')


@pytest.fixture(scope="module")
def deploy_fanout(request):
    """
    Method for getting deploy only target from pytest arguments
    :param request: pytest builtin
    :return: True/False
    """
    deploy_fanout_arg = request.config.getoption('--deploy_fanout')
    return deploy_fanout_arg == "yes"


@pytest.fixture(scope="module")
def reboot_after_install(request):
    """
    Method for getting reboot after install flag pytest arguments
    :param request: pytest builtin
    :return: whether to do reboot after installation to overcome swss docker in exited state
    """
    return request.config.getoption('--reboot_after_install')


@pytest.fixture(scope="module")
def is_shutdown_bgp(request):
    """
    Method for getting shutdown bgp flag from pytest arguments
    :param request: pytest builtin
    :return: True or False
    """
    return request.config.getoption('--is_shutdown_bgp')


@pytest.fixture(scope="module")
def onyx_image_url(request):
    """
    Method for getting onyx_image_url from pytest arguments
    :param request: pytest builtin
    :return: onyx_image_url
    """
    return request.config.getoption('--onyx_image_url')


@pytest.fixture(scope="module")
def port_number(request):
    """
    Method for getting port-number from pytest arguments
    :param request: pytest builtin
    :return: port-number
    """
    return request.config.getoption('--port-number')


@pytest.fixture(scope="module")
def recover_by_reboot(request):
    """
    Method for getting recover_by_reboot from pytest arguments
    :param request: pytest builtin
    :return: recover_by_reboot
    """
    return request.config.getoption('--recover_by_reboot')


@pytest.fixture(scope="module")
def fw_pkg_path(request):
    """
    Method for getting firmware package file path from pytest arguments
    :param request: pytest builtin
    :return: path to firmware package
    """
    fw_pkg_path = request.config.getoption('--fw_pkg_path')
    if fw_pkg_path and not os.path.exists(fw_pkg_path):
        # if the fw_pkg_path give does not exist, then use the default one
        sonic_mgmt_path = os.path.dirname(os.path.abspath(__file__)).split('ngts')[0]
        fw_pkg_path = os.path.join(sonic_mgmt_path, MarsConstants.UPDATED_FW_TAR_PATH)
    return fw_pkg_path


@pytest.fixture(scope='session')
def reboot(request):
    """
    Method for getting the reboot from pytest arguments
    :param request: pytest builtin
    :return: reboot
    """
    return request.config.getoption('--reboot')


@pytest.fixture(scope='session')
def wjh_deb_url(request):
    """
    Method for getting the WJH from pytest arguments
    :param request: pytest builtin
    :return: wjh-deb-url
    """
    return request.config.getoption('--wjh-deb-url')


@pytest.fixture(scope='session')
def additional_apps(request):
    """
    Method for getting the additional-apps from pytest arguments
    :param request: pytest builtin
    :return: additional-apps
    """
    return request.config.getoption('--additional-apps')


@pytest.fixture(scope="session")
def deploy_dpu(base_version_dpu, platform_params):
    """
    Return True if it's bobcat setup and base_version_dpu is given.
    :param request: pytest builtin
    :param base_version_dpu: base_version_dpu fixture
    :param platform_params: platform_params fixture
    :return: deploy_dpu
    """
    return base_version_dpu and '4280' in platform_params['platform']


@pytest.fixture(scope="module")
def deploy_testbed_in_parallel(request):
    """
    Method for getting deploy_testbed_in_parallel flag from pytest arguments

    Parameter conversion rules:
    - Command line "--deploy_testbed_in_parallel=yes" -> returns True
    - Command line "--deploy_testbed_in_parallel=no" -> returns False
    - No parameter specified (default) -> returns False

    :param request: pytest builtin
    :return: deploy_testbed_in_parallel flag (True or False)
    """
    parallel_arg = request.config.getoption('--deploy_testbed_in_parallel')
    return parallel_arg == "yes"


@pytest.fixture(scope="module")
def deploy_image_only(request):
    """
    Method for getting deploy_image_only from pytest arguments
    :param request: pytest builtin
    :return: deploy_image_only
    """
    deploy_image_only_arg = request.config.getoption('--deploy_image_only')
    return deploy_image_only_arg == "yes"


@pytest.fixture(scope="module")
def deploy_sequential(request):
    """
    Retrieves the deploy_sequential value from pytest arguments.

    Args:
        request: The pytest builtin request fixture.

    Returns:
        bool: True if sequential deployment is requested, False otherwise.
    """
    deploy_sequential_arg = request.config.getoption('--deploy_sequential')
    return deploy_sequential_arg == "yes"


@pytest.fixture(scope="module")
def custom_config_db_air_path(request):
    """
    Method for getting custom_config_db_air_path from pytest arguments
    :param request: pytest builtin
    :return: path to custom config_db.json file
    """
    custom_config_db_air = request.config.getoption('--custom_config_db_air_path')
    if custom_config_db_air and not os.path.exists(custom_config_db_air):
        raise ValueError(f"Custom config_db.json file does not exist: {custom_config_db_air}")
    if not custom_config_db_air:
        return None
    return custom_config_db_air


@pytest.fixture(scope="module")
def deploy_chipless(request):
    """
    Method for getting deploy_chipless from pytest arguments
    :param request: pytest builtin
    :return: deploy_chipless
    """
    deploy_chipless_arg = request.config.getoption('--deploy_chipless')
    return deploy_chipless_arg == "yes"


@pytest.fixture(scope="module")
def base_version_bmc(request):
    """
    Method for getting BMC base version from pytest arguments
    :param request: pytest builtin
    :return: BMC base version (URL or path); empty string when not provided
    """
    return request.config.getoption('--base-version-bmc')


@pytest.fixture(scope="module")
def neighbor_mode(request):
    """
    Method for getting neighbor mode from pytest arguments for dual-tor deployment
    :param request: pytest builtin
    :return: neighbor mode; empty string when not provided
    """
    return request.config.getoption('--neighbor_mode')


@pytest.fixture(scope="module")
def sdk_source(request):
    """
    Where to take the SDK package from for traffic generator installation.

    Controlled by ``--sdk_source``:

    - ``latest``: resolve the newest published SDK package for the DUT branch.
    - ``image``: use the SDK version already installed on the DUT/image.
    - omitted: keep the platform default (SONiC: latest, Cumulus: image).

    Invalid values raise an argparse error that lists the supported options.

    Returns:
        str | None: ``'latest'`` or ``'image'`` when set; otherwise ``None``.
    """
    return request.config.getoption('--sdk_source')