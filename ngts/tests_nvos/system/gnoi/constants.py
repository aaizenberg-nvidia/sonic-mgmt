"""
gNOI constants (HLD_NVOS_gNOI rev 0.8.0).

gNOI has no config surface of its own: it is served whenever the gNMI server is enabled, on the
same Envoy endpoint (:9339). Hence the port and the container/state constants are shared with gNMI.

Items marked OPEN-<n> are not specified by the HLD and are tracked in tp/gnoi_TP.md §11.1. They are
gathered here so a single edit updates every test once the product answers.
"""
from ngts.constants.constants import GnmiConsts
from ngts.nvos_constants.constants_nvos import NvosConst

# gNOI rides the gNMI front door, so the endpoint is the gNMI one by design.
GNOI_DEFAULT_PORT = GnmiConsts.GNMI_DEFAULT_PORT


class GnoiRpc:
    """The 8 RPCs NVOS supports. Any other RPC must return Unimplemented."""
    SYSTEM_REBOOT = 'System.Reboot'
    SYSTEM_SET_PACKAGE = 'System.SetPackage'
    OS_INSTALL = 'OS.Install'
    OS_ACTIVATE = 'OS.Activate'
    FILE_GET = 'File.Get'
    FILE_PUT = 'File.Put'
    FILE_STAT = 'File.Stat'
    FILE_REMOVE = 'File.Remove'

    SUPPORTED = [SYSTEM_REBOOT, SYSTEM_SET_PACKAGE, OS_INSTALL, OS_ACTIVATE,
                 FILE_GET, FILE_PUT, FILE_STAT, FILE_REMOVE]

    # Out of scope on NVOS - used to assert the rpc config interceptor returns Unimplemented.
    OUT_OF_SCOPE_EXAMPLE = 'System.Time'


class GnoiRpcCounters:
    """
    Leaves of 'nv show system gnmi-server status gnoi-rpc [<rpc-name>]'.

    The leaf and state-file names below were confirmed on juliet-160 (state file version 0.5.0).
    Only RPCs invoked since the gNMI server started are listed: the collection starts empty after
    every enable and grows an entry on first use, so a missing RPC reads as (0, 0).
    """
    RECEIVED = 'received-rpc-requests'
    FAILED = 'failed-rpc-requests'

    # Keys as written by nv-gnoid into the state file (mapped by NVUE onto the leaves above),
    # nested under 'rpcs' next to the state-file schema version.
    STATE_FILE_RPCS = 'rpcs'
    STATE_FILE_RECEIVED = 'receivedCalls'
    STATE_FILE_FAILED = 'failedCalls'


class GnoiPaths:
    """Host-side artifacts the feature adds or consumes."""
    STATE_FILE = '/run/nv-gnmi/nv-gnoi-state.json'
    # Its presence is how NVUE detects the gNMI server is enabled; removed on service stop.
    GNMI_STATE_FILE = '/run/nv-gnmi/nv-gnmi-state.json'
    STAGING_DIR = '/var/tmp/nv-gnmi/gnoi_staging'
    STAGING_IMAGE = '/host/nv-gnmi/gnoi_staging.img'
    USER_LOOKUP_SOCKET = '/var/run/nv-gnmi/user-lookup.sock'
    DIAGS_DUMP_DIR = '/var/log/nv-gnmi/diags-dump'

    # Server-side logs. Both are root-owned, and the gNOI one grows into megabytes, so read them as
    # slices from a position taken before the action (see helpers.dut_log_since).
    GNOID_LOG = '/var/log/nv-gnmi/nv-gnmi-gnoid.log'
    ENVOY_LOG = '/var/log/nv-gnmi/nv-gnmi-envoy.log'

    GNOID_PROGRAM = 'nv-gnoid'
    USER_LOOKUP_DAEMON = 'nv-user-lookupd'

    # Small, world-readable file used as the target of the non-destructive File.Stat probe
    # (the gnoic example in the HLD).
    STAT_PROBE_PATH = '/etc/os-release'


class GnoiFirmware:
    """
    Facts about System.SetPackage in per-component mode (tp/gnoi_TP.md §5.3), confirmed on
    juliet-128.

    NVOS reads the target component out of the RPC's `version` field and the destination name out of
    `filename`, so the client call is `--version <COMPONENT> --dst <name>`. Without `activate` the
    server performs NVUE's @fetch alone, which only stages a file and is safe to run anywhere. With
    it, @fetch is followed by @install, and an install writes firmware - `skip-reboot` defers when
    the new image loads, not whether it is burned. Hence anything that can reach an install is
    gated behind an environment variable rather than run by default.
    """
    # The firmware store the fetch writes into, one directory per component, lowercased.
    STORE_DIR = '/host/fw-images'

    UNKNOWN_COMPONENT = 'NOSUCHCOMPONENT'
    FETCH_FAILED_DESC = 'failed to fetch package'
    NVUE_MISSING_ITEM_DESC = 'does not exist'
    # One message covers both fields, and it names the offending value.
    TRAVERSAL_DESC = 'must not contain path separators or traversal sequences'
    INVALID_FILENAME_DESC = 'invalid filename'
    INVALID_COMPONENT_DESC = 'invalid component name'

    # NVUE component id -> its key in the platform-components JSON that BmcTool reads. Only these
    # carry a firmware file; SSD and transceiver appear in the CLI but have none to fetch.
    COMPONENT_TO_JSON_KEY = {
        'ASIC': 'asic',
        'BIOS': 'bios',
        'BMC': 'bmc',
        'CPLD1': 'cpld',
        'EROT': 'erot',
        'FPGA': 'fpga',
    }

    # ASIC is the only component whose firmware can be put back by configuration alone - set
    # fw-source to default and reboot - so it is the only one this test will ever install for real.
    # A downgrade of BMC, EROT, BIOS or CPLD can leave the switch needing out-of-band recovery.
    BURNABLE_COMPONENT = 'ASIC'

    # Attempting an install of a version that is already present: NVUE's version check is expected
    # to refuse before writing anything, but that is the product's behaviour to prove rather than
    # assume, so the step is opt-in.
    ALLOW_ACTIVATE_ENV_VAR = 'GNOI_ALLOW_FW_ACTIVATE'
    # A real firmware burn on the burnable component, for a maintenance window.
    ALLOW_BURN_ENV_VAR = 'GNOI_ALLOW_FW_BURN'

    # Rejected by nv-gnoid's own path-segment guard: a literal traversal, its percent-encoded form,
    # and a plain separator. Unlike the file service's validator, this one catches all three.
    TRAVERSAL_PREFIXES = ['../', '%2e%2e%2f', 'sub/']
    TRAVERSAL_BASENAME = 'gnoi_evil'

    # The destination name is not free-form: NVUE requires the extension its component expects
    # ('.fwpkg' for BMC/EROT/BIOS/FPGA, '.mfa' for ASIC, '.vme' for CPLD), and the gNOI API offers
    # no way to discover which. So the test names its file after the source it streams, and uses
    # this to prove the check exists.
    WRONG_SUFFIX = '.pkg'
    UNSUPPORTED_TYPE_DESC = 'Unsupported firmware file type'

    # Firmware packages run to a couple of hundred MB, well inside the transfer bound, but the
    # fetch that follows is NVUE work rather than streaming.
    SETPACKAGE_TIMEOUT_SEC = 600


class GnoiStaging:
    """
    Facts about the staging area and the service lifecycle (tp/gnoi_TP.md §7.1 and §9), as
    confirmed on juliet-128.

    Staging is a fixed-size ext4 image on /host, loop-mounted inside the container's namespace, so
    a transfer costs disk rather than memory and a runaway one hits the image's own bound instead
    of filling /host. Nothing about it persists: there is no fstab entry, and the control script
    recreates the image on every service start.
    """
    SERVICE = 'nv-gnmi'
    # What 'systemctl is-active' reports for a running unit, used to put the service back as the
    # test found it.
    SERVICE_ACTIVE = 'active'
    CONTAINER = 'nv-gnmi'
    # systemd's escaped name for the mount unit covering GnoiPaths.STAGING_DIR.
    MOUNT_UNIT = r'var-tmp-nv\x2dgnmi-gnoi_staging.mount'

    # The size the staging image reports, and so the bound a transfer cannot write past. The file
    # is sparse, so it costs /host only what has been written to it, up to this ceiling.
    IMAGE_SIZE_BYTES = 4 * 1024 ** 3
    FS_TYPE = 'ext4'

    SOCKET_DIR = '/var/run/nv-gnmi'
    # The socket's own mode is what bounds access to the lookup daemon. The HLD also calls for a
    # 0700 directory; the delivered one is 0755, which §7.1 step 1 records as a finding.
    EXPECTED_SOCKET_MODE = '600'
    EXPECTED_SOCKET_DIR_MODE = '700'
    ROOT_OWNER = 'root'

    # Larger than the image, so the write cannot succeed however the server chunks it. Created
    # sparse: the client streams zeros and the file costs nothing locally.
    OVERSIZED_PAYLOAD_BYTES = 5 * 1024 ** 3
    ENOSPC_MARKERS = ['no space left on device', 'No space left on device']
    # What nv-gnoid writes when the staging write fails, even though the client is told only 'EOF'.
    ENOSPC_LOG_MARKER = 'no space left on device'
    # A staging cost this far below the payload proves the transfer is not held in memory. Not a
    # tight bound: the point is orders of magnitude, and cgroup accounting includes page cache.
    MEMORY_GROWTH_FRACTION = 0.25

    INSTALL_IN_PROGRESS = 'INSTALL_IN_PROGRESS'

    # How long to wait for a backgrounded transfer to show up in the staging area before giving up
    # on observing it mid-flight.
    STAGED_FILE_APPEARS_TIMEOUT_SEC = 120
    SERVICE_RESTART_TIMEOUT_SEC = 180
    # An install of the lab image answers in well under a minute, competing RPCs or not, so this is
    # generous. Kept far below the OS.Install ceiling so a stall is reported in minutes.
    CONCURRENT_INSTALL_TIMEOUT_SEC = 600

    # NVUE's reboot modes, from 'nv list-commands'. There is no warm or fast reboot on NVOS, which
    # is why §9 covers 'immediate' instead - see the note in that section.
    REBOOT_MODE_COLD = 'cold'
    REBOOT_MODE_IMMEDIATE = 'immediate'
    FSTAB = '/etc/fstab'


class GnoiFileService:
    """
    Facts about the file service (tp/gnoi_TP.md §5.1), confirmed on juliet-160.

    File RPCs run as the requesting user, so a transfer can only land where that user may write.
    /home/<user> is owned by the user, while /host and /host/dump are root-owned drwxr-xr-x: a
    write-role admin reads from them (tech-support dumps are world-readable) but cannot create or
    delete entries there. The NVUE fetch action refuses a destination under /host outright,
    reporting 'File Operation Error', which is the same restriction seen from the CLI side.
    """
    ROOT_OWNED_DIR = '/host'
    TECHSUPPORT_DUMP_DIR = '/host/dump'

    @staticmethod
    def user_home(username):
        return f'/home/{username}'

    # Modes are the gnoic --permission values. 0 is special: it tells gnoic to apply the local
    # file's own permissions instead of a requested mode (its default is 777).
    PUT_MODE = 644
    PUT_MODE_INHERIT_LOCAL = 0
    # Deliberately not 644, so 'inherited from the local file' is distinguishable from 'defaulted'.
    LOCAL_FILE_MODE = 0o600

    # NVUE renders 'nv action list system file-path <path>' as a JSON array, one object per entry,
    # with these keys. A directory listing includes '.' and '..'.
    LIST_FILENAME_KEY = 'filename'
    LIST_SIZE_KEY = 'size'
    LIST_FLAGS_KEY = 'flags'
    LIST_OWNER_KEY = 'owner'

    # Markers in the NVUE action output.
    FETCH_SUCCESS_MARKER = 'Fetched file to'
    DENIED_PATH_MARKER = 'File Operation Error'
    LIST_MISSING_MARKER = 'No such file or directory'

    # fetch/upload take a URI. file:// keeps the test self-contained (no external file server) and
    # is accepted alongside the remote schemes; the action reports the vrf and mode it applied.
    LOCAL_URI_SCHEME = 'file://'
    DEFAULT_FETCH_PERMISSIONS = '644'
    DEFAULT_FETCH_VRF = 'mgmt'


class GnoiOsService:
    """
    Facts about the OS service (tp/gnoi_TP.md §5.2).

    OS.Install only transfers: it drops the image into the NOS image directory and leaves both
    partitions and boot-next alone. The partition write happens during OS.Activate, which NVUE
    performs as install-then-boot-next.
    """
    IMAGE_DIR = NvosConst.PATH_TO_IMAGES

    # The switch names what it stores from the RPC's version field, so the stored name is
    # predictable regardless of what the file was called on the client.
    STORED_NAME_TEMPLATE = 'image-{version}.bin'

    # The image to transfer is named by --target_version, which MARS supplies, or by this
    # environment variable for a run by hand. Point either at a local-disk copy rather than at
    # /.autodirect: gnoic reads the whole file over the wire, and an NFS round trip per chunk slows
    # the transfer down.
    LOCAL_IMAGE_ENV_VAR = 'GNOI_OS_IMAGE'

    # The ONIE installer is a shell archive whose header declares the version it will install.
    # Reading it keeps the test honest about which build it is actually pushing.
    INSTALLER_VERSION_PATTERN = r'image_version="([^"]+)"'
    INSTALLER_HEADER_BYTES = 20000

    # What the switch will actually boot, as opposed to what it reports. `nv action boot-next`
    # rewrites grub's saved entry at once, but the `next` field of `nv show system image` only
    # catches up when NVOS next refreshes that state, measured at ~60s on juliet-128. Reading grub
    # gives the effective target immediately; the CLI is then polled separately so the reporting
    # lag is covered rather than raced against.
    GRUBENV = '/host/grub/grubenv'
    GRUB_CFG = '/host/grub/grub.cfg'
    # next_entry is grub's one-shot override and wins while it is set; boot consumes it, leaving
    # saved_entry as the standing choice.
    GRUBENV_NEXT_PATTERN = r'next_entry=(\d+)'
    GRUBENV_SAVED_PATTERN = r'saved_entry=(\d+)'
    GRUB_MENUENTRY_PATTERN = r"^menuentry\s+'([^']+)'"
    BOOT_NEXT_VISIBILITY_TIMEOUT_SEC = 180
    # An image install replaces the grub environment file, and a read during that window returns
    # an empty listing.
    GRUBENV_READ_TIMEOUT_SEC = 30

    # InstallResponse field names as gnoic renders them under --print-proto. Its ordinary log lines
    # mention only the transfer ('TransferProgress', 'TransferContent done...') and never name the
    # TransferReady or Validated responses, so the RPC's outcome is only visible in proto text.
    # A target that already holds the version may answer Validated straight away, without a transfer.
    TRANSFER_READY = 'transfer_ready'
    TRANSFER_PROGRESS = 'transfer_progress'
    VALIDATED = 'validated'
    INSTALL_ERROR = 'install_error'

    # 1MB is gnoic's default; naming it makes the progress-notification count meaningful.
    CONTENT_CHUNK_SIZE = 1048576


class GnoiRebootMode:
    """gnoic --method values. COLD/HALT are supported; WARM is NVUE-rejected; the rest are invalid."""
    COLD = 'COLD'
    HALT = 'HALT'
    WARM = 'WARM'
    POWERDOWN = 'POWERDOWN'
    NSF = 'NSF'
    POWERUP = 'POWERUP'

    # Modes the switch must refuse, limited to those gnoic will actually put on the wire. NSF is
    # absent on purpose: gnoic rejects it locally with 'unknown reboot method "NSF"', so the switch
    # never sees it and the case cannot be driven from this client.
    UNSUPPORTED_DRIVABLE = [POWERDOWN, POWERUP]
    CLIENT_REJECTED = [NSF]


class GnoiErrorFlows:
    """
    Facts about the negative paths (tp/gnoi_TP.md §6.1), confirmed on juliet-128.

    Read and write paths are deliberately asymmetric: the forbidden-directory denylist is applied
    by the write handlers to their target, while the read path does not consult it, so a
    world-readable file under /var/log can be stat-ed and fetched but not written or removed.
    """
    # Descriptions carried by the gRPC status, as returned by nv-gnoid.
    OUT_OF_SCOPE_DESC = 'not supported on this platform'
    UNSUPPORTED_MODE_DESC = 'unsupported reboot mode'
    AUTH_FAILED_DESC = 'Authentication failed'

    DENYLISTED_DIR = '/var/log'
    # root:shadow 0640, and admin is not in the shadow group: the bound is Unix permissions, which
    # apply because the RPC runs as the requesting user.
    ROOT_ONLY_FILE = '/etc/shadow'

    WRONG_PASSWORD = 'definitely-not-the-password'

    # HALT powers the switch down, so the step runs only once a remote-reboot command has been
    # resolved from NOGA: without one there is no way back and the switch would be stranded.
    # Set this variable to skip the step even where recovery is available.
    SKIP_HALT_ENV_VAR = 'GNOI_SKIP_HALT'

    # A halt takes a few tens of seconds to drop the management interface; the power cycle plus a
    # full NVOS boot is minutes, and wait_for_nvos_to_become_functional applies its own bound after.
    HALT_DOWN_TIMEOUT_SEC = 300
    HALT_PING_POLL_SEC = 5
    REMOTE_REBOOT_TIMEOUT_SEC = 300

    GNOID_LOG = GnoiPaths.GNOID_LOG
    ENVOY_LOG = GnoiPaths.ENVOY_LOG

    # Log markers, as written by nv-gnoid on juliet-128.
    REBOOT_MODE_LOG = 'Reboot request received, but mode {mode} is not supported'
    RBAC_DENIAL_LOG = 'user does not have permission to perform action'
    TRAVERSAL_LOG = "path must not contain '..'"
    PATH_VALIDATION_LOG = 'failed to validate path'
    # Every served RPC gets an INFO 'sent <rpc> request' line; a refusal adds a WARN/ERROR one.
    # The out-of-scope case is documented as counter-only, i.e. the second line must not appear.
    RPC_SEVERITY_MARKERS = ['WARN', 'ERROR']
    OUT_OF_SCOPE_RPC_PATH = '/gnoi.system.System/Time'


class GnoicErr:
    """Markers matched in gnoic stdout/stderr. gRPC status names are stable across gnoic versions."""
    UNIMPLEMENTED = 'Unimplemented'
    INVALID_ARGUMENT = 'InvalidArgument'
    UNAUTHENTICATED = 'Unauthenticated'
    PERMISSION_DENIED = 'PermissionDenied'
    NOT_FOUND = 'NotFound'
    INTERNAL = 'Internal'
    NOT_INSTALLED = 'gnoic: command not found'

    # gNOI is not served (gNMI server disabled): the connection itself does not complete.
    NOT_SERVED = ['connection refused', 'transport: error while dialing', 'Unavailable',
                  'context deadline exceeded']

    ALL_ERRS = [UNIMPLEMENTED, INVALID_ARGUMENT, UNAUTHENTICATED, PERMISSION_DENIED,
                NOT_INSTALLED, 'rpc error', 'failed'] + NOT_SERVED


# Tech-support artifacts added by this feature (HLD 7.5). Matched as substrings against 'tar -tf'
# because the dump renames and compresses what it collects: the gNOI server log of program
# nv-gnoid arrives as log/nv-gnmi/nv-gnmi-gnoid.log.gz. Both entries below were seen on juliet-160.
TECHSUPPORT_EXPECTED_GNOI_ARTIFACTS = [
    'gnoid.log',
    'diags-dump',
]

# HLD 7.5 requires two further artifacts that the dump on juliet-160 does not carry: the gNOI RPC
# stats file and the nv-user-lookupd log. They are reported as findings rather than asserted, so
# the rest of the flow still runs; move them into the list above once they are collected.
TECHSUPPORT_UNCOLLECTED_GNOI_ARTIFACTS = [
    'nv-gnoi-state.json',
    GnoiPaths.USER_LOOKUP_DAEMON,
]

# Transient staged bytes must NOT be collected (size + privacy).
TECHSUPPORT_FORBIDDEN_GNOI_ARTIFACT = 'gnoi_staging'

# gnoic is a unary operational client: a request either completes or fails quickly. Kept well above
# the NVUE action round-trip so a slow-but-healthy action is not mistaken for a hang.
GNOIC_CMD_TIMEOUT_SEC = 30
# The 'not served' probe must fail fast instead of burning the full timeout on a dead endpoint.
GNOIC_UNREACHABLE_PROBE_TIMEOUT_SEC = 10
# Streaming a tech-support dump is a real transfer, not a metadata round-trip: the dumps on
# juliet-160 run from 65MB to 190MB, which is chunked at 64KB per message through the staging area.
GNOIC_TRANSFER_TIMEOUT_SEC = 600
# A NOS image is ~3GB through the same staging area, and the server hashes it before answering
# Validated. Generous on purpose: a client timeout here aborts the transfer and wastes the run.
GNOIC_OS_INSTALL_TIMEOUT_SEC = 2400
# Activation is NVUE install + boot-next on an image already on disk: no transfer, but the install
# unpacks into a partition, which is far from instant.
GNOIC_OS_ACTIVATE_TIMEOUT_SEC = 1800
