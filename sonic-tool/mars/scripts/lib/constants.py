
SONIC_MARS_BASE_PATH = "/.autodirect/sw_regression/system/SONIC/MARS"

SONIC_MGMT_DEVICE_ID = "SONIC_MGMT"
NGTS_PATH_PYTEST = "/ngts_venv/bin/pytest"
NGTS_PATH_PYTHON = "/ngts_venv/bin/python"
TEST_SERVER_DEVICE_ID = "TEST_SERVER"
NGTS_DEVICE_ID = "NGTS"
DUT_DEVICE_ID = "DUT"
FANOUT_DEVICE_ID = "FANOUT"
SONIC_MGMT_DIR = '/root/mars/workspace/sonic-mgmt/'
UPDATED_FW_TAR_PATH = 'tests/platform_tests/fwutil/firmware.json'
HTTP_SERVER_NBU_NFS = 'http://nbu-mtr-nfs.nvidia.com'

DOCKER_SONIC_MGMT_IMAGE_NAME = "docker-sonic-mgmt"
DOCKER_NGTS_IMAGE_NAME = "docker-ngts"
# Single source of truth for the pinned docker-ngts tag.
# Used to pin the image for sonic-mgmt container creation (update_docker.py) and the
# devts package installation in the air simulation bringup (air_simulation_bringup.sh).
DOCKER_NGTS_DEFAULT_TAG = "1.3.100"

SONIC_MGMT_REPO_URL = "http://10.7.77.140:8080/switchx/sonic/sonic-mgmt"

COMMON_MOUNTPOINTS = {
    '/auto/sw_regression/system/SONIC/MARS': '/auto/sw_regression/system/SONIC/MARS',
    '/auto/sw_tools/Internal/BugHandling/RELEASES': '/auto/sw_tools/Internal/BugHandling/RELEASES',
    '/auto/sw_system_release': '/auto/sw_system_release',
    '/auto/sw_system_project': '/auto/sw_system_project',
}
BARE_METAL_MOUNTPOINTS = {
    '/.autodirect/mswg/projects': '/.autodirect/mswg/projects',
    '/.autodirect/sw_system_release/': '/.autodirect/sw_system_release/',
    '/auto/sw_system_project': '/auto/sw_system_project',
    '/.autodirect/sw_regression/system/SONIC/MARS': '/.autodirect/sw_regression/system/SONIC/MARS',
    '/workspace': '/workspace',
    '/.autodirect/LIT/SCRIPTS': '/.autodirect/LIT/SCRIPTS',
    '/etc/localtime': '/etc/localtime',
    "/.autodirect/LIT/LOGS/RR": "/.autodirect/LIT/LOGS/RR",
    '/.autodirect/sw/release/': '/.autodirect/sw/release/',
    '/auto/sw/tools/comet/': '/auto/sw/tools/comet/',
    '/auto/sw/projects/performance/results/mongodb/': '/auto/sw/projects/performance/results/mongodb/',
    '/auto/LIT/SCRIPTS/': '/auto/LIT/SCRIPTS/',
    '/auto/LIT/LOGS/RR/': '/auto/LIT/LOGS/RR/',
    '/auto/mswg/utils/bin/': '/auto/mswg/utils/bin/',
    '/auto/cumulus': '/auto/cumulus',
    '/.autodirect/cumulus': '/.autodirect/cumulus',
    '/auto/mswg_release_mft/': '/auto/mswg_release_mft/',
    '/.autodirect/mswg_release_mft/': '/.autodirect/mswg_release_mft/',
}
AIR_SONIC_MGMT_MOUNTPOINTS = {
    '/auto/sw_regression/system/SONIC/MARS': '/auto/sw_regression/system/SONIC/MARS',
    '/auto/sw_tools/Internal/BugHandling/RELEASES': '/auto/sw_tools/Internal/BugHandling/RELEASES',
    '/auto/sw_system_release': '/auto/sw_system_release',
    '/auto/sw_system_release/cumulus': '/auto/sw_system_release/cumulus',
    '/.autodirect/sw_system_release/cumulus/': '/.autodirect/sw_system_release/cumulus/',
}
NVOS_MOUNTPOINTS = {
    '/auto/sw_regression/system/NVOS/MARS': '/auto/sw_regression/system/NVOS/MARS',
    '/.autodirect/sw_regression/system/NVOS/MARS': '/.autodirect/sw_regression/system/NVOS/MARS',
    '/.autodirect/sysgwork/G/MARS_conf/stm_nvos/': '/.autodirect/sysgwork/G/MARS_conf/stm_nvos/',
}
SONIC_MOUNTPOINTS = {}

# for backwards compatibility.
SONIC_MGMT_MOUNTPOINTS = {}
for mountpoints in (COMMON_MOUNTPOINTS, BARE_METAL_MOUNTPOINTS, NVOS_MOUNTPOINTS, SONIC_MOUNTPOINTS):
    SONIC_MGMT_MOUNTPOINTS.update(mountpoints)

SONIC_MGMT_MOUNTPOINTS_MTBC = {
    '/auto/sw_regression/mtbcsw/system/SONIC/MARS': '/auto/sw_regression/mtbcsw/system/SONIC/MARS',
    '/.autodirect/sw_regression/mtbcsw/system/SONIC/MARS': '/.autodirect/sw_regression/mtbcsw/system/SONIC/MARS'
}
MTBC_SERVER_LIST = ['dev-r730-01', '10.75.206.120', 'dev-r730-02', '10.75.207.40', 'dev-r730-03', '10.75.207.5', 'mtbc-r730-04', '10.75.205.21']
MTL_NVOS_SERVER_LIST = ['10.237.116.60']
MTL_NVOS_MOUNTPOINTS = {'/auto/sw/tools/comet/nvos': '/auto/sw/tools/comet/nvos'}
VER_SDK_PATH = "/opt/ver_sdk"
EXTRA_PACKAGE_PATH_LIST = ["/usr/lib64/python2.7/site-packages"]

TOPO_ARRAY = ("t0", "t1-lag", "ptf32", "t0-64", "t0-64-256", "t0-c256", "t0-isolated-d2u254s1", "t1-lag-c224o8", "t1-32-lag", "t1-48-lag", "t1-64-lag", "t1-isolated-d254u2s1", "t0-56", "t0-56-po2vlan", "t0-56-o8v48", "t1-isolated-d28u1", 't1-isolated-d28u4', "t1-isolated-d224u8", "t1-isolated-d448u15-lag", "t0-isolated-d128u128s1", "t0-isolated-d16u16s1", "t0-isolated-d16u16s2", "t0-isolated-d32u32s2", "t0-isolated-v6-d32u32s2", "t0-isolated-d32u32s2-mix", "t1-isolated-v6-d56u1-lag", "t1-isolated-d56u1-lag")
REBOOT_TYPES = {
    "reboot": "reboot",
    "fast-reboot": "fast-reboot",
    "warm-reboot": "warm-reboot"
}

DOCKER_REGISTRY = "nbu-harbor.gtm.nvidia.com"
AIR_DOCKER_REGISTRY = "harbor-mts.nvidia.com"

DUT_LOG_BACKUP_PATH = "/.autodirect/sw_system_project/sonic/dut_logs"
