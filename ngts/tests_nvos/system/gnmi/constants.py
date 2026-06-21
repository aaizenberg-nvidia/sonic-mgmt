class GnmiMode:
    ONCE = 'once'
    POLL = 'poll'
    STREAM = ''
    ALL_MODES = [ONCE, POLL, STREAM]


GNMI_INSTALLED = 'gnmi-server'

DUT_HOSTNAME_FOR_CERT = 'nvos-dut'
DUT_GNMI_CERTS_DIR = '/tmp/gnmi-certs'
DOCKER_CERTS_DIR = '/etc/netq/cert'

DUT_MOUNT_GNMI_CERT_DIR = '/etc/gnmi/cert'

ETC_HOSTS = '/etc/hosts'

SERVICE_KEY = 'service.key'
SERVICE_PEM = 'service.pem'

MAX_GNMI_SUBSCRIBERS = 10
MAX_GNMI_CONNECTIVITY_TIME = 6
MAX_GNMI_BAD_SUBSCRIBERS = 100

# Seconds a STREAM subscription flows before SIGINT.
STREAM_SUBSCRIBE_WINDOW_SEC = 8

# gnmic SAMPLE stream-mode interval (one result-set per interval).
STREAM_SUBSCRIBE_SAMPLE_INTERVAL = '1s'


# PAM / load tests: wrong credentials for background ``gnmic`` clients (must not match a real user)
GNMI_INVALID_AUTH_CLIENT_USERNAME = 'abc'
GNMI_INVALID_AUTH_CLIENT_PASSWORD = 'abc'

# ``test_gnmi_auth_failing_clients_ddos``: poll DUT ``ss`` until TCP count drops from a peak
GNMI_PAM_DUT_TCP_WAIT_TIMEOUT_SEC = 180
GNMI_PAM_DUT_TCP_WAIT_POLL_SEC = 0.25
# Base seconds added to bad-subscriber count when joining the plain bad-client launcher thread.
GNMI_PAM_BAD_PLAIN_LAUNCH_JOIN_SLACK_SEC = 90
# Minimum invalid plain STREAM clients that must start before the foreground probe (capped by ``MAX_GNMI_BAD_SUBSCRIBERS``).
GNMI_PAM_BAD_PLAIN_THREAD_MIN_BEFORE_GOOD = 10
# Timeout waiting for that readiness gate in ``_plain_bad_gnmi_background_start_and_wait_ready``.
GNMI_PAM_BAD_PLAIN_READY_WAIT_SEC = 30.0
# Delay between transient-error retries in ``_gnmic_capabilities_mtls_spiffe_success``.
GNMI_PAM_SPIFFE_CAPABILITIES_TRANSIENT_RETRY_SLEEP_SEC = 1.0

# gNMI server DDoS/rate-limit: requests per minute threshold; exceeding it yields local_rate_limited
GNMI_RATE_LIMIT_REQ_PER_MIN = 60

CERTIFICATE = 'certificate'
DEFAULT_CERTIFICATE = 'self-signed'

SERVER_REFLECTION_SUBSCRIBE_RESPONSE = '.gnmi.SubscribeResponse'


# gNMI server status object model (system/gnmi-server/status)
# +--ro total-active-subscriptions      uint64
# +--ro received-subscription-requests  uint64
# +--ro rejected-subscriptions          uint64
# +--ro received-capabilities-requests  uint64
# +--ro client*
#     +--ro client-address              string
#     +--ro client-port                 string
#     +--ro client-type                 enumeration
#     +--ro client-active-subscriptions uint64
class GnmiServerStatus:
    TOTAL_ACTIVE_SUBSCRIPTIONS = 'total-active-subscriptions'
    RECEIVED_SUBSCRIPTION_REQUESTS = 'received-subscription-requests'
    REJECTED_SUBSCRIPTIONS = 'rejected-subscriptions'
    RECEIVED_CAPABILITIES_REQUESTS = 'received-capabilities-requests'
    CLIENT = 'client'
    CLIENT_ADDRESS = 'client-address'
    CLIENT_PORT = 'client-port'
    CLIENT_TYPE = 'client-type'
    CLIENT_ACTIVE_SUBSCRIPTIONS = 'client-active-subscriptions'


class GnmicErr:
    GNMIC_NOT_INSTALLED = 'gnmic: command not found'
    AUTH_FAIL = 'Authentication failed'
    CERT_VERIFY_FAIL = 'failed to verify certificate'
    HANDSHAKE_FAIL = 'authentication handshake failed'
    AUTH_SERVICE_UNAVAILABLE = 'authentication service is unavailable'
    REQUEST_FAILED = 'request failed'
    NO_SUBSCRIBER_SLOT_AVAILABLE = 'no subscriber slot available'
    LOCAL_RATE_LIMITED = 'local_rate_limited'
    DEADLINE_EXCEEDED = 'context deadline exceeded'
    RCV_ERROR = 'rcv error'
    RPC_ERROR = 'rpc error'

    # Transient back-pressure errors the server returns while overloaded (e.g. Envoy rate limiting or
    # requests timing out under load). Safe to retry since the server recovers once load drops, so
    # reachability/recovery checks tolerate and retry them. Add new overload markers here.
    OVERLOAD_ERRS = [LOCAL_RATE_LIMITED, DEADLINE_EXCEEDED]

    ALL_ERRS = [GNMIC_NOT_INSTALLED, AUTH_FAIL, HANDSHAKE_FAIL, AUTH_SERVICE_UNAVAILABLE,
                REQUEST_FAILED, NO_SUBSCRIBER_SLOT_AVAILABLE, RCV_ERROR, RPC_ERROR,
                CERT_VERIFY_FAIL] + OVERLOAD_ERRS


class GrpcMsg:
    LIST_SERVICES_FAIL = 'Failed to list services'
    MSG_SERVER_REFLECT = 'service ServerReflection'
    MSG_SUBSCRIBE_RESPONSE = 'message SubscribeResponse'
    ALL_MSGS = {SERVER_REFLECTION_SUBSCRIBE_RESPONSE: MSG_SUBSCRIBE_RESPONSE}


# gNMI rate-limit test tuning parameters (used by test_gnmi_rate_limit.py and helpers)
RAMP_DURATION_SEC = 90
PER_REQUEST_TIMEOUT_SEC = 15
# How long the over-threshold floods run. The limiter is a ~60/min token bucket with a burst, so it
# only rate-limits once the burst is drained; a settled server depletes it within this window.
OVERLOAD_FLOOD_SEC = 90
# Parallel clients for every step that must overload the server. Each loop runs gnmic back-to-back
# (~0.5-1s/call), so ~4 loops barely reach the 60/min limit and flake; 10 loops push the aggregate
# well past it so it trips reliably even on slow setups. Capped at 10 (the max used elsewhere);
# Capabilities are unary, so not bounded by MAX_GNMI_SUBSCRIBERS.
NUM_CLIENTS_OVER_THRESHOLD = 10
PER_REQUEST_TIMEOUT_OVER_THRESHOLD_SEC = 10
# Skip over-threshold test if achieved RPM is below this (fraction of limit; avoids flake when load is too low)
MIN_RPM_FRACTION_TO_REQUIRE_RATE_LIMIT = 0.9
SAMPLE_ERROR_MAX_LEN = 2000

# Recovery and restart-under-load timings
# Cool the DUT/engine down from prior floods so the overload reaches the same rate it does in
# isolation (a warm server responds slower and the burst never depletes within the flood window).
PRE_OVERLOAD_SETTLE_SEC = 60
# The per-minute rate-limit window: long enough to both trip the limiter and let it fully recover.
RECOVERY_DRAIN_SEC = 60
# Low-rate measurement window that runs *after* the drain; every request in it must succeed cleanly.
RECOVERY_LOW_RATE_SEC = 50
RESTART_UNDER_LOAD_WARMUP_SEC = 22
GNMI_RESTART_POST_DISABLE_WAIT_SEC = 5
GNMI_RESTART_POST_ENABLE_WAIT_SEC = 15
# After gNMI is back, resume unary flood briefly before asserting rate limit still triggers
RESTART_POST_ENABLE_VERIFY_FLOOD_SEC = 35
# After that flood, pause attackers and drain a full RECOVERY_DRAIN_SEC window so the limiter
# recovers before the clean check.
# How long to keep retrying capabilities while overload errors (rate limit / deadline exceeded)
# clear after an overload or gNMI restart. Set to the per-minute rate-limit window.
RECONNECT_CAPABILITIES_MAX_WAIT_SEC = 60
RECONNECT_CAPABILITIES_RETRY_INTERVAL_SEC = 2
# Pause after unary overload before reachability check (bucket / connection settle)
POST_OVERLOAD_PAUSE_SEC = 2
# Thread join timeouts
ATTACKER_THREAD_JOIN_TIMEOUT_SEC = 30
# Parallel Capabilities flood / under-threshold phase-2: workers exit at their wall-clock
# deadline, and the slowest in-flight gnmic call is bounded by PER_REQUEST_TIMEOUT_*_SEC.
# This budget covers that tail with headroom so we can detect a truly hung worker.
CAPABILITIES_FLOOD_THREAD_JOIN_TIMEOUT_SEC = 30
# Background-process flood model: stagger between starting each background loop, and extra grace
# (beyond a single per-request timeout) to let a loop print its final counts and exit on its own
# before collection forces termination.
FLOOD_PROCESS_INIT_DELAY_SEC = 0.2
FLOOD_COLLECT_GRACE_SEC = 5


class GnmiConstants:
    SPEED_NEGOTIATE = 'speed-negotiate'
    SPEED = "speed"
    IN_OCTETS = 'in-octets'
    IN_PKTS = 'in-pkts'
    IN_DISCARDS = 'in-discards'
    IN_ERRORS = 'in-errors'
    OUT_OCTETS = 'out-octets'
    OUT_PKTS = 'out-pkts'
    OUT_DISCARDS = 'out-discards'
    OUT_ERRORS = 'out-errors'
    SYMBOL_ERROR_COUNTER = 'symbol-error-counter'
    XMIT_WAIT = 'xmit-wait'
    LINK_ERROR_RECOVERY = 'link-error-recovery'
    LINK_DOWNED = 'link-downed'
    RCV_REMOTE_PHY_ERRORS = 'rcv-remote-phy-errors'
    RCV_SWITCH_RELAY_ERRORS = 'rcv-switch-relay-errors'
    RCV_CONSTRAINTS_ERRORS = 'rcv-constraints-errors'
    LOCAL_LINK_INTEGRITY_ERRORS = 'local-link-integrity-errors'
    QP1_DROPPED = 'qp1-dropped'
    PORT_BUFFER_OVERRUN_ERRORS = 'port-buffer-overrun-errors'
    IN_UNICAST_PKTS = 'in-unicast-pkts'
    OUT_UNICAST_PKTS = 'out-unicast-pkts'
    IN_MULTICAST_PKTS = 'in-multicast-pkts'
    OUT_MULTICAST_PKTS = 'out-multicast-pkts'
    MTU = "mtu"
    OPERATIONAL_VL = 'operational-vl'
    WIDTH = 'width'
    IB_SPEED = "ib-speed"
    OPER_STATUS = 'oper-status'
    SUPPORTED_IB_SPEEDS = 'supported-ib-speeds'
    SUPPORTED_SPEED = 'supported-speed'
    MAX_SUPPORTED_MTUS = 'max-supported-mtus'
    PHYSICAL_PORT_STATE = 'physical-port-state'
    LOGICAL_PORT_STATE = 'logical-port-state'
    SUPPORTED_WIDTHS = 'supported-widths'
    VL_CAPABILITIES = "vl-capabilities"
    IB_SUBNET = "ib-subnet"
    PLR_RCV_CODES = 'plr-rcv-codes'
    LINK_PLR_RCV_CODE_ERRORS = 'plr-rcv-code-err'
    PHY_RAW_ERRORS = ["raw-errors-ch-1",
                      "raw-errors-ch-2",
                      "raw-errors-ch-3",
                      "raw-errors-ch-4",
                      "raw-errors-ch-5",
                      "raw-errors-ch-6",
                      "raw-errors-ch-7",
                      "raw-errors-ch-8"]
    PHY_RAW_BER = ["raw-ber-ch-1",
                   "raw-ber-ch-2",
                   "raw-ber-ch-3",
                   "raw-ber-ch-4",
                   "raw-ber-ch-5",
                   "raw-ber-ch-6",
                   "raw-ber-ch-7",
                   "raw-ber-ch-8"]

    # Expected paths under gnmic
    # `components/component[name=<id>]/transceiver/physical-channels/channel[index=1]/`,
    # relative to that prefix, for an Inserted module. Removed modules expose no
    # physical-channel leaves (the subtree is structurally absent).
    EXPECTED_TRANSCEIVER_PHYSICAL_CHANNEL_FIELDS = {
        "channel-diag/state/rx-cdr-lol",
        "channel-diag/state/rx-los",
        "channel-diag/state/rx-power-hi-al",
        "channel-diag/state/rx-power-hi-war",
        "channel-diag/state/rx-power-lo-al",
        "channel-diag/state/rx-power-lo-war",
        "channel-diag/state/tx-ad-eq-fault",
        "channel-diag/state/tx-bias-hi-al",
        "channel-diag/state/tx-bias-hi-war",
        "channel-diag/state/tx-bias-lo-al",
        "channel-diag/state/tx-bias-lo-war",
        "channel-diag/state/tx-cdr-lol",
        "channel-diag/state/tx-fault",
        "channel-diag/state/tx-los",
        "channel-diag/state/tx-power-hi-al",
        "channel-diag/state/tx-power-hi-war",
        "channel-diag/state/tx-power-lo-al",
        "channel-diag/state/tx-power-lo-war",
        "index",
        "state/index",
        "state/input-power/instant",
        "state/laser-bias-current/instant",
        "state/output-power/instant",
        "state/rx-cdr-lol",
        "state/rx-los",
        "state/tx-ad-eq-fault",
        "state/tx-failure",
    }

    # Per-module-type leaf-sets for transceiver physical-channel fields.
    # ELS = 22 leaves; OE = ELS plus 4 fields (26); SW = OE plus 1 field (27).
    # Only Inserted modules are asserted in photonics tests.
    # Path fragments below are used to compose the field sets.
    _CHANNEL_DIAG_STATE_PATH = "channel-diag/state/"
    _CHANNEL_STATE_PATH = "state/"

    EXPECTED_TRANSCEIVER_PHYSICAL_CHANNEL_FIELDS_ELS = {
        f"{_CHANNEL_DIAG_STATE_PATH}rx-cdr-lol",
        f"{_CHANNEL_DIAG_STATE_PATH}rx-los",
        f"{_CHANNEL_DIAG_STATE_PATH}rx-power-hi-al",
        f"{_CHANNEL_DIAG_STATE_PATH}rx-power-hi-war",
        f"{_CHANNEL_DIAG_STATE_PATH}rx-power-lo-al",
        f"{_CHANNEL_DIAG_STATE_PATH}rx-power-lo-war",
        f"{_CHANNEL_DIAG_STATE_PATH}tx-ad-eq-fault",
        f"{_CHANNEL_DIAG_STATE_PATH}tx-bias-hi-al",
        f"{_CHANNEL_DIAG_STATE_PATH}tx-bias-hi-war",
        f"{_CHANNEL_DIAG_STATE_PATH}tx-bias-lo-al",
        f"{_CHANNEL_DIAG_STATE_PATH}tx-bias-lo-war",
        f"{_CHANNEL_DIAG_STATE_PATH}tx-cdr-lol",
        f"{_CHANNEL_DIAG_STATE_PATH}tx-fault",
        f"{_CHANNEL_DIAG_STATE_PATH}tx-los",
        f"{_CHANNEL_DIAG_STATE_PATH}tx-power-hi-al",
        f"{_CHANNEL_DIAG_STATE_PATH}tx-power-hi-war",
        f"{_CHANNEL_DIAG_STATE_PATH}tx-power-lo-al",
        f"{_CHANNEL_DIAG_STATE_PATH}tx-power-lo-war",
        f"{_CHANNEL_STATE_PATH}rx-cdr-lol",
        f"{_CHANNEL_STATE_PATH}rx-los",
        f"{_CHANNEL_STATE_PATH}tx-ad-eq-fault",
        f"{_CHANNEL_STATE_PATH}tx-failure",
    }
    EXPECTED_TRANSCEIVER_PHYSICAL_CHANNEL_FIELDS_OE = (
        EXPECTED_TRANSCEIVER_PHYSICAL_CHANNEL_FIELDS_ELS |
        {
            "index",
            f"{_CHANNEL_STATE_PATH}index",
            f"{_CHANNEL_STATE_PATH}input-power/instant",
            f"{_CHANNEL_STATE_PATH}output-power/instant",
        }
    )
