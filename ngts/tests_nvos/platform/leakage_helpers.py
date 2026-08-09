"""
Shared helpers for SPC-6 / Cumulus Linux leakage detection tests.

Injection paths
---------------
**BMC-side (CPLD I2C)** — primary path for Core #1–#3. BMC Linux shell over OOB SSH::

    i2ctransfer -f -y 5 w3@0x31 0x20 0xff 0xff   # clear all
    i2ctransfer -f -y 5 w3@0x31 0x20 0xff 0xfe   # leak sensor 1
    i2ctransfer -f -y 5 w3@0x31 0x20 0xff 0xfd   # leak sensor 2
    i2ctransfer -f -y 5 w3@0x31 0x20 0xff 0xfc   # both
    i2ctransfer -f -y 5 w3@0x31 0x20 0xfe 0xfe   # leak aggregate
    i2ctransfer -f -y 5 w3@0x31 0x20 0xfe 0xff   # clear aggregate

**NOS-side (sysfs)** — Core #4 fault injection only (missing/invalid/corrupt files).
Polarity when used: ``1`` = OK, ``0`` = leak.

Chameleon6M has more detectors; Salamandra (2 sensors) uses only the sensor-1/2 masks.
"""
import gzip
import io
import ipaddress
import json
import logging
import os
import re
import select
import shlex
import socket
import subprocess
import tarfile
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Sequence, Tuple, Union

import pytest
from devts.infra.tools.connection_tools.linux_ssh_engine import LinuxSshEngine
from ngts.constants.constants import GnmiConsts
from ngts.nvos_constants.constants_nvos import HealthConsts, PlatformConsts
from ngts.nvos_tools.infra.BmcCliTool import BmcCliTool
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.tests_nvos.general.security.bmc.bmc_creds.constants import (
    OPENBMC,
    ROOT,
    get_cl_bmc_root_password,
)
from ngts.tests_nvos.system.gnmi.GnmiClient import GnmiClient, GnmicCmdBuilder
from ngts.tests_nvos.system.gnmi.constants import GnmiMode
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger()

LEAKAGE_NVUE_STATE_NORMAL = "normal"
LEAKAGE_NVUE_STATE_ALERT = "alert"

# Value of the "issue" field in `nv show system health` issues dict for a leaking sensor.
# Confirmed empirically on CL 5.17 / Salamandra: {"leakage1": {"issue": "alert"}}.
HEALTH_ISSUE_LEAKAGE_ALERT = "alert"

SMONCTL_STATUS_NORMAL = 1
SMONCTL_STATUS_LEAK = 0

# How many sensors this platform *declares* (hw-mgmt writes it at init).
LEAKAGE_COUNTER_PATH = "/var/run/hw-management/config/leakage_counter"
# Absolute upper bound on that count: hw-mgmt pre-creates one sysfs leaf per leakage
# input the platform can wire, whether or not a sensor is populated. On mlx-6600-04
# (HW 2026-09-06) leakage1…leakage5 all exist and read '1' while leakage_counter is 2.
# A counter above the number of leaves is impossible — there would be nothing to read —
# so the DUT's own leaf count is the cap and no maximum is hardcoded here.
LEAKAGE_SYSFS_LEAF_PATTERN = "leakage[0-9]+"
TELEMETRY_LEAKAGE_METRIC = "nvswitch_platform_environment_leak_sensor_status"
TELEMETRY_URL = "http://localhost:9100/metrics"
# PNE scrape bounds, in curl itself and in the SSH layer behind it. Both PNE poll loops
# (wait_for_pne_metric_available, wait_for_pne_leakage_statuses) run a 60s budget, so the
# same invariant as the BMC read applies — curl bound < SSH backstop < loop budget — and
# these figures leave the loop at least four attempts instead of the two that reusing the
# BMC 10/30 pair would allow. 5s is far above a real localhost HTTP GET; the point is only
# that an endpoint which accepts the connection and never answers must not inherit
# run_cmd's 10000s default and deadlock the loop meant to retry around it.
PNE_METRICS_CURL_TIMEOUT = 5
PNE_METRICS_READ_TIMEOUT = 15
# PNE metric polarity (same as NOS/BMC sysfs): "1" = OK, "0" = LEAK_DETECTED.
PNE_TELEMETRY_STATUS_OK = "1"
PNE_TELEMETRY_STATUS_LEAK = "0"
NVUE_REST_PORT = 8765
NVUE_REST_LEAKAGE_URL = f"https://localhost:{NVUE_REST_PORT}/nvue_v1/platform/environment/leakage"
# REST read bound. The endpoint is local to the DUT and answers in seconds; this exists
# only so a wedged session fails instead of inheriting ProxySshEngine.run_cmd's 10000s
# default (~2.8h, longer than the suite's own pytest.mark.timeout).
REST_LEAKAGE_READ_TIMEOUT = 120
# Where the short-lived ``curl -K`` config holding the REST credentials is staged on the
# DUT. /var/tmp rather than /tmp: both are 1777, but /tmp is tmpfs-cleaned on some images.
# The mode read-back in _stage_rest_curl_config is what makes a shared parent safe.
REST_CFG_REMOTE_DIR = "/var/tmp"
REST_CFG_REMOTE_TEMPLATE = f"{REST_CFG_REMOTE_DIR}/ngts-rest-leakage-XXXXXX"
REST_CFG_MODE_MARKER = "__NGTS_REST_CFG_MODE"
_REST_CFG_FILE_RE = re.compile(
    rf"({re.escape(REST_CFG_REMOTE_DIR)}/ngts-rest-leakage-[A-Za-z0-9]{{6}})"
)
# cl-support collects NOS + BMC dumps and runs for minutes on this platform (the BMC
# complete dump dominates) — the longest single command in the suite.
CL_SUPPORT_TIMEOUT = 600
# Upper bound for gunzipping a cl-support ``.gz`` member (BMC complete dump).
# Measured baseline mlx-6600 2026-08-31: 1.15 MB from a 29 MB bundle — this cap is
# a sanity guard, not a working limit. Whole-buffer matching is required, see
# _bmc_dump_has_live_leak.
CL_SUPPORT_GZ_MEMBER_MAX_BYTES = 64 * 1024 * 1024

# BMC CPLD I2C injection — Salamandra / SN6600 (Mlx6600Switch) only for now.
# bus 5 / 0x31 / reg 0x20 are platform-specific; -f forces past the kernel claim.
# Wrong-SKU runs must not write: see assert_cpld_i2c_platform_supported().
BMC_CPLD_I2C_BUS = 5
BMC_CPLD_I2C_ADDR = "0x31"
BMC_CPLD_LEAK_REG = "0x20"
BMC_CPLD_LEAK_SENSOR_REG = "0xff"
BMC_CPLD_LEAK_AGGR_REG = "0xfe"

BMC_CPLD_CLEAR_ALL = "0xff"
BMC_CPLD_LEAK_SENSOR1 = "0xfe"
BMC_CPLD_LEAK_SENSOR2 = "0xfd"
BMC_CPLD_LEAK_SENSOR1_AND_2 = "0xfc"
BMC_CPLD_LEAK_AGGR_CLEAR = "0xff"

# DUT classes whose CPLD leak-sim map matches the constants above.
# Expand when another SPC-6 SKU documents bus/addr/masks (and lab sim).
LEAKAGE_CPLD_I2C_SUPPORTED_DUT_TYPES = frozenset({"Mlx6600Switch"})

BMC_LEAK_DETECTION_URI = "/redfish/v1/Chassis/MGX_BMC_0/ThermalSubsystem/LeakDetection"
# Bound for the ``bmc-cli sensor-dump`` read, for the same reason as
# REST_LEAKAGE_READ_TIMEOUT: bmc-cli's own ``--timeout`` only reaches the remote tool, so
# a session that wedges before bmc-cli answers would otherwise inherit the engine default.
# Sized like the REST read (a Redfish query) rather than the sysfs cat.
BMC_CLI_SENSOR_DUMP_TIMEOUT = 120

# BMC hw-management sysfs interface (on the BMC OS, not on CL).
# The BMC hw-management daemon reads the CPLD via I2C and writes these files.
# They are the BMC's internal source of truth and upstream of Redfish LeakDetection.
# Values: "1" = OK, "0" = leak — confirmed empirically on Salamandra (2026-07-23).
# leakage_aggr is NEVER asserted in any test: CPLD simulation does not reliably drive
# the aggregate bit, so asserting it would cause spurious failures. It is logged
# informally at baseline (Core #1) for observability only.
BMC_HWMGMT_LEAKAGE_PATH = "/var/run/hw-management/system"
BMC_HWMGMT_LEAKAGE_OK = "1"
BMC_HWMGMT_LEAKAGE_LEAK = "0"
BMC_HWMGMT_LEAKAGE_AGGR = "leakage_aggr"

# Per-sensor CPLD leak bit masks for BMC i2ctransfer injection.
# Extension point for >2 sensors: add index→mask entries once architecture documents
# the bit layout (and sim supports N sensors). Core #2 parametrizes over these keys.
# Today Salamandra / mlx-6600 + lab sim: 2 sensors only (masks 1 and 2 above).
CPLD_MASK_BY_SENSOR_INDEX = {
    1: BMC_CPLD_LEAK_SENSOR1,
    2: BMC_CPLD_LEAK_SENSOR2,
}

# CPLD write+read-back cycle: the i2c bus is shared with the BMC's own periodic reads,
# so a collision surfaces as a transient i2ctransfer error. Retry the whole cycle and
# space the attempts so a retry does not land in the same bus-busy window. Every attempt
# verifies the register read-back, so a retry can never mask a failed write.
CPLD_WRITE_RETRIES = 3
CPLD_WRITE_RETRY_DELAY_SEC = 5

# BMC OOB SSH: password fallback (lab password, then factory OPENBMC) × attempts.
# Retry delay covers a BMC that is up but not yet accepting SSH; the long post-reboot
# wait belongs to wait_for_bmc_oob_ssh, which calls this with tries=1.
BMC_OOB_SSH_TRIES = 3
BMC_OOB_SSH_RETRY_DELAY_SEC = 10
# Auth/liveness probe (one trivial command). Construction is lazy, so this is where a
# wrong password surfaces — keep it short so fallback and the poll loops stay responsive.
BMC_OOB_SSH_PROBE_TIMEOUT = 20


# BMC version: read via 'nv show platform firmware BMC -o json' on the DUT (actual-firmware field).
# Minimum for SPC-6 leakage on Salamandra / mlx-6600 lab baseline (mlx-6600-04).
# Assert: tuple(BMC) >= tuple(min) — equal or newer passes.
LEAKAGE_MIN_BMC_VERSION: Optional[str] = "88.0060.2112"

# CPLD bundle: read from /var/run/hw-management/system/cpld on the NOS.
# Assert: per-component REV >= baseline for each CPLD#### id in the minimum
# (same IDs compared; extra IDs in actual OK). Baseline = mlx-6600-04 (2026-07-29).
LEAKAGE_MIN_CPLD_BUNDLE: Optional[str] = (
    "CPLD000454_REV0400_CPLD000439_REV0206_CPLD000440_REV0103_CPLD000441_REV0103"
)

# CL image build-id from 'nv show system version -o json' → image.build-id (e.g. 5.18.0.0031).
# Assert: tuple(build-id) >= tuple(min) — equal or newer passes (build 31+).
LEAKAGE_MIN_CL_BUILD_ID: Optional[str] = "5.18.0.0031"

# Set to False to downgrade version mismatches from hard failures to warnings.
# Useful while a setup is being upgraded to the required firmware.
LEAKAGE_ENFORCE_VERSION_CHECK: bool = True

# RM #5171996 — per-sensor Status.Health absent on BMC 2101/2110/2112/2216 (won't fix
# for CL 5.18). health_summary then shows Unknown. DetectorState / membership / count
# are always asserted; Status.Health is asserted only when the field is present on the
# member (auto-reenables when a future BMC restores Health). Historical: 88.0060.0501
# once returned Health correctly but is below LEAKAGE_MIN_BMC_VERSION.
# https://redmine.mellanox.com/issues/5171996
LEAKAGE_BMC_STATUS_HEALTH_BUG_RM = "5171996"

CPLD_BUNDLE_SYSFS_PATH = "/var/run/hw-management/system/cpld"

# ─── cross-surface propagation budgets ──────────────────────────────────────
# Every leakage surface is polled, not pushed, so each wait is a multiple of the slowest
# poll cadence in the chain CPLD → hw-mgmt sysfs → smonctl → NVUE / REST / gNMI / PNE.
# PNE is the slowest of those (scrape cadence ~10–30s in lab, HW 2026-08-04). OTEL is
# excluded — its cadence is the NVUE-enforced 60s sample boundary and it has its own
# budget (OTEL_LEAKAGE_POLL_TIMEOUT_SEC). Expressing the budgets as multiples keeps them
# reviewable: no value below is a free-floating guess.
#   HOLD   1× — expected value should already hold (e.g. a sibling that must stay OK);
#               the wait only tolerates a lagging write.
#   SETTLE 2× — state just changed and this surface must catch up; one entirely missed
#               scrape still passes, so a healthy DUT cannot fail a gate.
#   REBOOT 4× — hw-mgmt / smonctl / nv-umf restart from scratch, so the first poll after
#               boot can be a full cycle late (SN7 [bmc] HW 2026-08-26).
LEAKAGE_SURFACE_CADENCE_SEC = 30
# Errors a polling loop must not retry: these are bugs in the read path, and retrying
# them until the deadline turns a one-line traceback into a misleading timeout. Phrased as
# a deny-list because devts surfaces transport failures as bare ``Exception``
# (proxy_ssh_engine re-raises paramiko errors and raises plain Exception for "Waiting for
# NVUE to become functional"), so an allow-list of transient types would miss exactly the
# failures that are worth another poll.
LEAKAGE_FATAL_READ_ERRORS = (NameError, AttributeError, TypeError, ImportError)

LEAKAGE_SURFACE_HOLD_TIMEOUT = LEAKAGE_SURFACE_CADENCE_SEC
LEAKAGE_SURFACE_SETTLE_TIMEOUT = 2 * LEAKAGE_SURFACE_CADENCE_SEC
LEAKAGE_SURFACE_REBOOT_TIMEOUT = 4 * LEAKAGE_SURFACE_CADENCE_SEC
# One CLI / SSH read per poll: 2s gives the shortest budget ~15 samples without
# hammering the DUT.
LEAKAGE_SURFACE_POLL_INTERVAL = 2

# Marked one-line reads (``_read_marked_terminal_value``): asynchronous terminal output
# — wall broadcasts, ZTP messages — can replace or interleave with stdout. Attempts ×
# delay must outlast one such burst; a marker mismatch is retried, never accepted.
MARKED_READ_ATTEMPTS = 3
MARKED_READ_RETRY_DELAY_SEC = 5
# Staging file for those reads: the command writes its output here and a second, short
# command reads it back, so no command length can wrap into the parsed reply. Named for
# this pytest process, not the remote ``$$``: the name has to be identical across the
# two commands, and only the local pid is guaranteed stable whether the engine keeps one
# shell or opens a channel per command. It also keeps two suites on one DUT (seen
# 2026-09-08 on mlx-6600-08) from reading each other's staged value.
MARKED_READ_STAGE_FILE = f"/tmp/.ngts_marked_read.{os.getpid()}"
# Wider than the longest command this module sends, so the remote shell never wraps an
# echoed command into the reply the engine hands back (see widen_terminal_echo).
TERMINAL_ECHO_COLUMNS = 512

# ─── gNMI constants ────────────────────────────────────────────────────────
# Confirmed on HW (Salamandra / mlx-6600, 2026-07-28):
#   component name = "leakage1" / "leakage2" (matches NVUE sensor names exactly)
#   alarm-status values: "true" = leak detected, "false" = no leak
GNMI_PORT = 9339
# Prefix template — fill with sensor name, e.g. "leakage1"
GNMI_LEAKAGE_COMPONENT_PREFIX = "components/component[name={}]"
# Exact leaf — alarm-status (confirmed HW 2026-07-28: "false"=OK, "true"=LEAK)
GNMI_LEAKAGE_ALARM_STATUS_SUBPATH = "state/leakage/alarm-status"
GNMI_ALARM_STATUS_OK = "false"
GNMI_ALARM_STATUS_LEAK = "true"
# Component-level subtree (broader xpath, contains alarm-status among other leaves)
GNMI_LEAKAGE_COMPONENT_STATE_SUBPATH = "state"
# Oper-status
GNMI_LEAKAGE_OPER_STATUS_SUBPATH = "state/oper-status"
GNMI_OPER_STATUS_ACTIVE = "ACTIVE"
GNMI_OPER_STATUS_INACTIVE = "INACTIVE"
# Collected when gnmic is missing on every candidate host — setup issue, not DUT/gNMI-server failure.
GNMI_SETUP_ERR_GNMIC_MISSING = (
    "gNMI SETUP (test player): gnmic binary not found on the pytest host or on the "
    "sonic-mgmt player — install gnmic on one of them; this is not a DUT / switch "
    "gNMI failure"
)
# ON_CHANGE cannot use a remote player even when one carries gnmic — see
# open_gnmi_on_change_subscription for why the stream has to be local.
GNMI_ON_CHANGE_ERR_LOCAL_GNMIC_REQUIRED = (
    "gNMI ON_CHANGE SETUP: gnmic must be installed on the pytest host specifically — the "
    "subscription stream is read mid-flight, which a remote player cannot provide; a "
    "sonic-mgmt-only gnmic covers the other gNMI surfaces but not ON_CHANGE. This is not "
    "a DUT / switch gNMI failure"
)
# Short probe: 'which gnmic' either answers at once or the host is not usable as a player.
GNMI_PLAYER_PROBE_TIMEOUT = 10
# gnmic resolves every flag from a GNMIC_-prefixed environment variable, so --password can
# be supplied without reaching argv. Used by open_gnmi_on_change_subscription: that process
# stays alive for a whole injection window, and /proc/<pid>/cmdline is readable by any
# local account while /proc/<pid>/environ is not.
GNMIC_PASSWORD_ENV = "GNMIC_PASSWORD"
# Listener readiness on the normal (no-reboot) path: the nv-gnmi container is already
# up and only the listening-address changes, so this covers an NVUE apply + reload.
# Cold-boot paths need far longer — see PDB_GNMI_PORT_READY_TIMEOUT.
GNMI_PORT_READY_TIMEOUT = 90
GNMI_PORT_POLL_INTERVAL = 3
# Wait for ON_CHANGE initial sync leaf (current value) before inject — not a fixed sleep.
GNMI_ON_CHANGE_INITIAL_SYNC_TIMEOUT = 60
# Non-blocking slice for draining gnmic stdout: short enough that the initial-sync loop
# reacts within one notification, long enough not to busy-spin on an idle stream.
GNMI_ON_CHANGE_DRAIN_POLL = 0.2
# After SIGTERM, gnmic flushes and exits promptly; this only bounds a wedged client
# before we escalate to SIGKILL, so teardown cannot hang.
GNMI_ON_CHANGE_CLOSE_TIMEOUT = 10

# ─── OTEL constants ─────────────────────────────────────────────────────────
# Confirmed on HW 2026-08-02 (BSP_SPC6_mlx_6600_04, CL 5.18):
#   metric: nvswitch_platform_environment_leak_sensor_status
#   labels: description="leakage", name="leakage1"/"leakage2"
#   polarity: 1.0 = OK, 0.0 = LEAK_DETECTED  (same as PNE / sysfs)
#   cadence: platform-stats sample interval (60s minimum, NVUE-enforced)
#   path: PNE → nv-umf (Docker) → Unix socket → nv-otel-collector-mgmt (Docker) → OTLP
#   VRF: export must use mgmt VRF ("nv set system telemetry export vrf mgmt")
OTEL_LEAKAGE_STATUS_OK = 1.0
OTEL_LEAKAGE_STATUS_LEAK = 0.0
OTEL_COLLECTOR_PORT = 4317
OTEL_COLLECTOR_BINARY = "otelcol-contrib"
OTEL_COLLECTOR_VERSION = "0.130.0"
# The installed-vs-pinned test in setup_otel_leakage_collector is deliberately equality,
# not ordering: any build that is not the pinned one has to trigger the re-install,
# including a *newer* one, because a config that parses on one collector version can fail
# on another. That only holds while both sides speak the same shape, so this pattern is
# the single definition used by read_otel_collector_version and asserted against the
# constant below — a bump to "0.130" or "v0.130.0" would otherwise never compare equal to
# what the DUT reports and would silently re-install on every run.
OTEL_COLLECTOR_VERSION_RE = r"[0-9]+\.[0-9]+\.[0-9]+"
assert re.fullmatch(OTEL_COLLECTOR_VERSION_RE, OTEL_COLLECTOR_VERSION), (
    f"OTEL_COLLECTOR_VERSION must be N.N.N to match what the collector reports; "
    f"got {OTEL_COLLECTOR_VERSION!r}"
)
OTEL_COLLECTOR_RELEASES_BASE = (
    "https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download"
)
# SHA256 of otelcol-contrib_0.130.0_linux_amd64.tar.gz (GitHub release asset).
# Recompute when bumping OTEL_COLLECTOR_VERSION. Binary stays installed on shared
# players after teardown (deleting it could yank it from a concurrent session, and
# image pre-provision is preferred anyway) — so setup verifies the *version* on PATH
# every run instead of just its presence. A foreign build triggers the pinned
# re-install; ``install`` writes a new inode, so a collector already running from the
# old binary is unaffected.
OTEL_COLLECTOR_LINUX_AMD64_SHA256 = (
    "292ac0d698e80282ec3d848238ffd6976c3cb97c3c169dec955754efc2293e71"
)
# Bound for the collector-side install steps after the download (extract, install, temp
# cleanup): local disk work on an already-fetched 85.6 MB archive, seconds in practice.
# Also LocalOtelCollectorHost.run_cmd's default, so any collector command that asks for
# no timeout still cannot hang the pytest host.
OTEL_COLLECTOR_CMD_TIMEOUT = 120
# Bound for the one collector step that crosses the network: fetching the release
# tarball. Passed explicitly at the call site so it holds on both the sonic_mgmt-player
# (SSH, 10000s default) and the local-runner path, which otherwise disagreed on how long
# the same download may take. Sized from the asset, not guessed: 85.6 MB
# (89,810,736 B, measured 2026-09-08), pulled in ~2s at ~41.7 MB/s from a VDI. 300s still
# passes at ~300 KB/s — a 140x degradation — and fails well inside the module fixture.
OTEL_COLLECTOR_DOWNLOAD_TIMEOUT = 300
# Per-run artifacts live under a mktemp dir (not fixed /etc or /tmp) — see setup.
# The prefix is named rather than inlined because stop_otel_leakage_collector keys its
# no-paths fallback off it: that is what makes "kill leftovers from an aborted run of
# *this* suite" expressible without killing every otelcol-contrib on a shared host.
OTEL_LEAKAGE_RUN_DIR_PREFIX = "/var/tmp/ngts-leakage-otel."
OTEL_LEAKAGE_RUN_DIR_TEMPLATE = f"{OTEL_LEAKAGE_RUN_DIR_PREFIX}XXXXXX"
OTEL_LEAKAGE_OUTPUT_BASENAME = "leakage-test.json"
OTEL_LEAKAGE_CONFIG_BASENAME = "leakage-config.yaml"
OTEL_LEAKAGE_LOG_BASENAME = "otelcol-leakage.log"
# Active paths for the module-scoped collector (set in setup, cleared in teardown).
_otel_leakage_run_paths: Optional["OtelLeakageRunPaths"] = None
# The collector runs on a sonic-mgmt player when the topology has one and on the pytest
# host otherwise (CL BSP setups attach only DUT + BMC — the case for every run of this
# suite so far). resolve_otel_collector_engine picks between them at runtime, so the whole
# collector chain takes this union: annotating those functions LinuxSshEngine described a
# path they are never given on a CL BSP topology. Both sides are used only through
# ``run_cmd`` and ``ip``, which LocalOtelCollectorHost implements to match.
# Forward reference because the class is defined further down, beside the collector code.
OtelCollectorEngine = Union[LinuxSshEngine, "LocalOtelCollectorHost"]
# Platform-stats sample interval minimum (NVUE-enforced lower bound).
# OTEL reflects state at the next sample boundary — not in real time.
OTEL_PLATFORM_STATS_SAMPLE_INTERVAL_SEC = 60
# Poll timeout for value changes: 2× sample interval + 30s buffer.
OTEL_LEAKAGE_POLL_TIMEOUT_SEC = 150
OTEL_LEAKAGE_POLL_INTERVAL_SEC = 5
# Collector startup: otelcol-contrib binds its OTLP port immediately once the config
# parses, so this only has to cover process start on a loaded player — a longer budget
# would just delay reporting a bad config.
OTEL_COLLECTOR_BIND_TIMEOUT_SEC = 30
OTEL_COLLECTOR_BIND_POLL_INTERVAL_SEC = 2
# Grace between SIGTERM and SIGKILL when stopping the collector: enough to flush the
# current batch and exit, short enough that teardown never stalls.
OTEL_COLLECTOR_TERM_GRACE_SEC = 1
# How much of the collector output to pull for "latest" polls (not full-file cat).
# OTLP batches are line-delimited JSON; partial first line is skipped by the parser.
OTEL_LEAKAGE_LATEST_TAIL_BYTES = 2 * 1024 * 1024
# If target sensors' observed OTEL values stay wrong and unchanged for this long,
# force one DUT OTEL re-apply (leakage export stalled). Uses leakage-value freshness,
# not whole-file mtime — other platform-stats batches can keep mtime advancing.
OTEL_FILE_STALL_KICK_SEC = OTEL_PLATFORM_STATS_SAMPLE_INTERVAL_SEC + 15
# A gnmic ONCE right after subscribe/apply can return empty or EOF while the server
# finishes wiring the subscription; retry a few times before treating it as absent.
GNMI_OPER_STATUS_RETRIES = 3
GNMI_OPER_STATUS_RETRY_DELAY_SEC = 2
# SN 5: wait ≥ one PNE scrape under smonctl stub before fail-closed metric check.
# Without a settle, :9100 can still show a pre-stub stale OK (1). One full worst-case
# scrape — same cadence the surface budgets are derived from (HW 2026-08-04); an earlier
# 25s value sat just under the observed upper bound.
SMONCTL_FAULT_PNE_SETTLE_SEC = LEAKAGE_SURFACE_CADENCE_SEC
# SN 5: wait ≥ one platform-stats OTEL sample boundary under stub before fail-closed check.
# Cadence is NVUE-enforced 60s; without a settle the assert can race the next export.
SMONCTL_FAULT_OTEL_SETTLE_SEC = OTEL_PLATFORM_STATS_SAMPLE_INTERVAL_SEC
SMONCTL_BACKUP_SUFFIX = ".ngts_bak"
# Restore must succeed or every later smonctl-dependent test runs against a stub, so
# retry a transient write/verify failure before giving up. Failure is not silent: the
# poison flag below aborts the rest of the module. Same shape for the sysfs restore.
SMONCTL_RESTORE_ATTEMPTS = 3
SMONCTL_RESTORE_RETRY_DELAY_SEC = 2
# Set when stub restore exhausts retries — suite autouse must abort remaining tests.
_SMONCTL_RESTORE_FAILED = False

# Core #4: restore NOS leakage symlinks after corruption; poison suite if restore fails.
SYSFS_RESTORE_ATTEMPTS = 3
SYSFS_RESTORE_RETRY_DELAY_SEC = 2
_SYSFS_RESTORE_FAILED = False

# Set when a CPLD clear/verify in teardown or recovery fails — suite must abort.
_CPLD_CLEAR_FAILED = False

# ─── Core #4 sysfs-fault timing ─────────────────────────────────────────────
# Allow one smonctl / PNE scrape to see the new sysfs content before collecting surfaces.
SYSFS_FAULT_SETTLE_SEC = 2

# ─── reboot / health-history settle timing ──────────────────────────────────
# After health validates OK post-clear, wait for the BAD→OK history entry to land:
# the history writer lags the status flip, so an immediate read can miss the entry.
HEALTH_HISTORY_SETTLE_SEC = 10
# Slack subtracted from the DUT-clock history boundary (sub-second stamp rounding).
HEALTH_HISTORY_SINCE_SLACK_SEC = 5
# How long we may wait to *observe* BMC OOB SSH go down after ``reboot``.
# Fixed sleeps alone are not proof; wait_for_bmc_oob_ssh_down must see unreachability.
BMC_REBOOT_SSH_DOWN_TIMEOUT = 120
# How long we may wait to *observe* DUT SSH go down after a NOS reboot trigger.
# Fixed sleeps alone are not proof; wait_for_dut_ssh_down must see unreachability.
SOFT_REBOOT_SSH_DOWN_TIMEOUT = 120
PDB_REBOOT_SSH_DOWN_TIMEOUT = 180
# Dormant: serves SN 7 [aux_pwr_cycle], skipped at collection (see
# ``_SN7_AUX_PWR_CYCLE_SKIP_REASON``) because CPLD i2ctransfer sim does not survive an
# AUX cycle. Kept measured rather than deleted so re-enabling that case — once a leak
# simulation that survives AUX exists — does not have to re-derive the budget.
AUX_REBOOT_SSH_DOWN_TIMEOUT = 300
# Slack when comparing post-reboot /proc/uptime to wall-clock wait (poll lag / skew).
REBOOT_UPTIME_SLACK_SEC = 60

# ─── service-restart / reboot constants ────────────────────────────────────
PNE_SERVICE_NAME = "prometheus-node-exporter"
HWMGMT_SERVICE_NAME = "hw-management"
# Confirm on DUT with: systemctl list-units --type=service | grep -i umf
UMF_SERVICE_NAME = "nv-umf"
# systemctl restart → active: pne / hw-mgmt come back in seconds; nv-umf is
# docker-backed and is the slow one, so the budget is sized for it.
SERVICE_UP_POLL_INTERVAL = 3
SERVICE_UP_TIMEOUT = 60
# BMC cold boot until OOB SSH accepts again takes minutes; poll coarsely (each poll is
# a TCP probe + SSH auth attempt, so a short interval buys nothing).
BMC_REBOOT_SSH_TIMEOUT = 300
BMC_REBOOT_SSH_POLL_INTERVAL = 10
# Per-read bound for BMC hw-mgmt leakage sysfs, in two layers:
#   remote ``timeout`` kills a blocked ``cat`` on the DUT side,
#   SSH read timeout is the backstop when the session itself is half-open.
# Invariant: remote < SSH backstop < settle interval budget, so the settle loop below
# always keeps control. Default ProxySshEngine.run_cmd timeout is 10000s — a hung
# post-reboot ``cat`` (SN7 [bmc] 2026-08-26) burned the whole pytest.mark.timeout(45min)
# before failing. Keep both short; the settle loop is what retries.
BMC_HWMGMT_REMOTE_READ_TIMEOUT = 10
BMC_HWMGMT_READ_TIMEOUT = 30
# Wall-clock settle after BMC/NOS reboot while hw-mgmt re-creates leakage leaves. Sized
# well above the read layers above so a hung read is retried (with reconnect) several
# times inside the budget rather than consuming it.
BMC_HWMGMT_SETTLE_TIMEOUT = 180
BMC_HWMGMT_SETTLE_INTERVAL = 5.0
# NOS soft reboot until SSH is back; cold-boot paths use the longer AUX / PDB budgets
# below. Poll interval matches the BMC one — a TCP probe per poll.
REBOOT_SSH_POLL_INTERVAL = 10
REBOOT_SSH_TIMEOUT = 300
# Chassis power cycle: NOS cold boot is slower than soft reboot; 300s was too short
# (SN7 [aux_pwr_cycle] 2026-08-02 — SSH wait expired while DUT still coming up).
# Dormant with AUX_REBOOT_SSH_DOWN_TIMEOUT above — the 900s figure is the measured one
# from that run, so it is worth keeping for whenever the case is re-enabled.
AUX_PWR_CYCLE_SSH_TIMEOUT = 900
# Host/PDB power cycle (pwr_cycle): CPU cold boot; BMC stays up — usually faster than AUX,
# but still longer than soft reboot.
PDB_POWER_CYCLE_SSH_TIMEOUT = 600
# Post-pwr_cycle gNMI remote listen: cold boot + docker/nv-gnmi settle; 90s timed out
# on HW 2026-08-03 (SN8 [pwr_cycle] — :9339 not remotely reachable). Soft keeps 90s.
PDB_GNMI_PORT_READY_TIMEOUT = 300
BMC_HWMGMT_PWR_CYCLE_PATH = f"{BMC_HWMGMT_LEAKAGE_PATH}/pwr_cycle"
# Dormant with the AUX budgets above: the trigger SN 7 [aux_pwr_cycle] would write.
BMC_HWMGMT_AUX_PWR_CYCLE_PATH = f"{BMC_HWMGMT_LEAKAGE_PATH}/aux_pwr_cycle"


def _version_tuple(v: str):
    """Convert a dotted version string to a tuple for comparison."""
    return tuple(int(x) for x in re.split(r"[.\-]", v) if x.isdigit())


def _parse_cpld_bundle(bundle: str) -> Dict[str, int]:
    """Parse ``CPLD######_REV####_...`` into ``{CPLD000454: 400, ...}``."""
    if not isinstance(bundle, str) or not re.fullmatch(
        r"CPLD\d+_REV\d+(?:_CPLD\d+_REV\d+)*", bundle
    ):
        raise AssertionError(f"Invalid CPLD bundle format: {bundle!r}")
    pairs = re.findall(r"CPLD(\d+)_REV(\d+)", bundle)
    parsed = {f"CPLD{cpld_id}": int(rev) for cpld_id, rev in pairs}
    if len(parsed) != len(pairs):
        raise AssertionError(f"Duplicate CPLD id in bundle: {bundle!r}")
    return parsed


def cpld_bundle_meets_minimum(actual: str, minimum: str) -> None:
    """Assert each CPLD id in ``minimum`` is present in ``actual`` with REV >= min.

    Extra CPLD ids in ``actual`` are allowed. Raises ``AssertionError`` on missing
    id or older revision.
    """
    actual_revs = _parse_cpld_bundle(actual)
    minimum_revs = _parse_cpld_bundle(minimum)
    if not minimum_revs:
        raise AssertionError(f"CPLD minimum bundle has no CPLD#_REV# pairs: {minimum!r}")
    for cpld_id, min_rev in minimum_revs.items():
        if cpld_id not in actual_revs:
            raise AssertionError(
                f"CPLD bundle missing {cpld_id}: actual={actual!r}, required minimum={minimum!r}"
            )
        actual_rev = actual_revs[cpld_id]
        if actual_rev < min_rev:
            raise AssertionError(
                f"CPLD {cpld_id} REV{actual_rev:04d} is below minimum REV{min_rev:04d} "
                f"(actual={actual!r}, required minimum={minimum!r})"
            )


def read_bmc_firmware_version(dut_engine: LinuxSshEngine) -> Optional[str]:
    """Read BMC firmware version via 'nv show platform firmware BMC -o json' on the DUT.

    Returns the actual-firmware field value e.g. '88.0060.2216', or None if unavailable.
    Uses the DUT engine — consistent with ablyachman's get_bmc_firmware() in bmc_helpers.py.
    """
    raw = dut_engine.run_cmd("nv show platform firmware BMC -o json", validate=False)
    fw = OutputParsingTool.parse_json_str_to_dictionary(raw).get_returned_value()
    version = fw.get(PlatformConsts.FW_ACTUAL) if isinstance(fw, dict) else None
    if not version:
        logger.warning("Could not read BMC firmware version from 'nv show platform firmware BMC'")
    return version


def read_cpld_bundle_version(dut_engine: LinuxSshEngine) -> Optional[str]:
    """Read CPLD bundle string from NOS sysfs e.g. 'CPLD000468_REV0003_...'."""
    return _read_marked_terminal_value(
        dut_engine,
        f"cat {CPLD_BUNDLE_SYSFS_PATH}",
        "__NGTS_CPLD_BUNDLE",
        # Not ``.+``: a whitespace-free pattern is what keeps an echo remnant from
        # being accepted as the bundle string.
        r"\S+",
    )


def assert_cpld_i2c_platform_supported(devices) -> None:
    """Hard-fail unless DUT is a known-safe platform for CPLD i2ctransfer injection.

    Bus/addr/masks below are Salamandra SN6600 (``Mlx6600Switch``) only. Writing
    ``i2ctransfer -f`` on another SKU can corrupt whatever sits at that address.
    Called from the module version gate so the entire suite fails closed.
    """
    dut = getattr(devices, "dut", None)
    type_name = type(dut).__name__ if dut is not None else "<no-dut>"
    if type_name not in LEAKAGE_CPLD_I2C_SUPPORTED_DUT_TYPES:
        raise AssertionError(
            f"SPC-6 leakage CPLD i2ctransfer injection is only supported on "
            f"{sorted(LEAKAGE_CPLD_I2C_SUPPORTED_DUT_TYPES)} for now "
            f"(Salamandra / SN6600 bus {BMC_CPLD_I2C_BUS} addr {BMC_CPLD_I2C_ADDR}). "
            f"DUT type is {type_name!r} — refusing to run the suite."
        )
    logger.info(
        "CPLD i2c platform gate OK: %s (bus=%s addr=%s)",
        type_name, BMC_CPLD_I2C_BUS, BMC_CPLD_I2C_ADDR,
    )


def verify_platform_versions(engines, devices) -> None:
    """Assert CL / BMC / CPLD versions meet suite gates (module fixture).

    - DUT class must be in ``LEAKAGE_CPLD_I2C_SUPPORTED_DUT_TYPES`` (CPLD I2C map).
    - DUT must have a BMC (``devices.dut.has_bmc``): leak simulation and BMC
      surfaces require BMC OOB; missing BMC hard-fails before version checks.
    - CL ``image.build-id``: ordered minimum (``>= LEAKAGE_MIN_CL_BUILD_ID``).
    - BMC firmware: ordered minimum (``>= LEAKAGE_MIN_BMC_VERSION``).
    - CPLD bundle: per-component REV >= ``LEAKAGE_MIN_CPLD_BUNDLE`` (same CPLD IDs).

    Set a constant to None to skip that check. With
    ``LEAKAGE_ENFORCE_VERSION_CHECK`` True, mismatches raise ``AssertionError``.
    """
    assert_cpld_i2c_platform_supported(devices)

    if not getattr(devices.dut, "has_bmc", False):
        raise AssertionError(
            "SPC-6 leakage suite requires a BMC: CPLD leak simulation and BMC "
            "surfaces (OOB i2ctransfer, BMC sysfs, bmc-cli) are BMC-dependent. "
            "devices.dut.has_bmc is False — refusing to run."
        )

    def _version_fail(msg: str) -> None:
        if LEAKAGE_ENFORCE_VERSION_CHECK:
            raise AssertionError(msg)
        logger.warning("Version check (non-enforced): %s", msg)

    raw_ver = engines.dut.run_cmd("nv show system version -o json", validate=False)
    ver_data = OutputParsingTool.parse_json_str_to_dictionary(raw_ver).get_returned_value()
    cl_build_id = ver_data.get("image", {}).get("build-id") if isinstance(ver_data, dict) else None
    logger.info("CL build ID: %s (minimum required: %s)", cl_build_id or "unavailable", LEAKAGE_MIN_CL_BUILD_ID)
    if LEAKAGE_MIN_CL_BUILD_ID is not None:
        if not cl_build_id:
            _version_fail("Could not read CL build-id from 'nv show system version'")
        elif _version_tuple(cl_build_id) < _version_tuple(LEAKAGE_MIN_CL_BUILD_ID):
            _version_fail(
                f"CL build-id {cl_build_id!r} is below minimum required {LEAKAGE_MIN_CL_BUILD_ID!r}"
            )

    bmc_ver = read_bmc_firmware_version(engines.dut)
    logger.info("BMC version: %s (minimum required: %s)", bmc_ver, LEAKAGE_MIN_BMC_VERSION)
    if LEAKAGE_MIN_BMC_VERSION is not None:
        if bmc_ver is None:
            _version_fail("Could not read BMC version from 'nv show platform firmware BMC'")
        elif _version_tuple(bmc_ver) < _version_tuple(LEAKAGE_MIN_BMC_VERSION):
            _version_fail(f"BMC version {bmc_ver!r} is below minimum required {LEAKAGE_MIN_BMC_VERSION!r}")

    cpld_bundle = read_cpld_bundle_version(engines.dut)
    logger.info(
        "CPLD bundle: %s (required per-component REV >=: %s)",
        cpld_bundle,
        LEAKAGE_MIN_CPLD_BUNDLE or "not set",
    )
    if LEAKAGE_MIN_CPLD_BUNDLE is not None:
        if cpld_bundle is None:
            _version_fail(f"Could not read CPLD bundle from {CPLD_BUNDLE_SYSFS_PATH}")
        else:
            try:
                cpld_bundle_meets_minimum(cpld_bundle, LEAKAGE_MIN_CPLD_BUNDLE)
            except AssertionError as err:
                _version_fail(str(err))


def leakage_sensor_names(count: int) -> List[str]:
    return [f"leakage{i}" for i in range(1, count + 1)]


def parse_terminal_json(raw: str):
    """Decode the first JSON object or array amid asynchronous SSH terminal text."""
    text = str(raw)
    decoder = json.JSONDecoder()
    for match in re.finditer(r"[\[{]", text):
        try:
            value, _end = decoder.raw_decode(text, match.start())
            return value
        except json.JSONDecodeError:
            continue
    raise json.JSONDecodeError("No JSON object or array in terminal output", text, 0)


def widen_terminal_echo(engine: LinuxSshEngine) -> None:
    """Keep the remote shell from wrapping an echoed command into the reply.

    A marked read is longer than the default 90-column pty, so the shell echoes it back
    wrapped; the SSH engine strips the echo by character count, and what reaches the
    caller is the wrapped remainder instead of the output (mlx-6600-04 2026-09-06: three
    consecutive reads each returned only their own echo). Widening removes the wrap.
    Best effort, and not the primary defence: ``stty`` silently fails to take on some
    sessions (mlx-6600-06 2026-09-07, and mlx-6600-08 2026-09-08 after a power cycle
    re-established the session). Correctness comes from staging the value in a file and
    reading it back with a line that cannot wrap; this only removes the noise.
    """
    engine.run_cmd(f"stty cols {TERMINAL_ECHO_COLUMNS} 2>/dev/null; true", validate=False)


def _read_marked_terminal_value(
    engine: LinuxSshEngine,
    command: str,
    marker: str,
    value_pattern: str,
    attempts: int = MARKED_READ_ATTEMPTS,
) -> Optional[str]:
    """Read a one-line command value despite asynchronous SSH terminal output.

    The engine strips the echoed command from the reply by character count. When the
    composed line is wider than the terminal, the shell echoes it back wrapped, the
    count no longer lines up, and the misaligned strip eats the real output - the reply
    then carries only echo remnants. Shortening commands only buys margin, and that
    margin has run out three times (mlx-6600-06 2026-09-07: ``read_cpld_bundle_version``
    got back its own truncated ``__ngts_value=$(cat ...); __ngts_rc=$``; mlx-6600-08
    2026-09-08: an 86-char ``read_leakage_sysfs_capacity`` survived 11 tests, then broke
    once a power cycle re-established the session and ``stty`` widening no longer
    applied). The prompt itself occupies part of the width, so no length is safe.

    Execution is never the problem - only our parsing of the echo is - so the value is
    staged in a file by the long line, whose reply is deliberately not parsed, and read
    back by a line short enough that it cannot wrap. ``$$`` keeps the staging file
    per-shell, so two suites against one DUT cannot read each other's value.

    A value line is only accepted if it matches ``value_pattern`` and does not contain
    the marker, which keeps an echo remnant from passing as output. Do not widen
    ``value_pattern`` to ``.+`` for that reason. If the pattern has a capture group the
    group is returned, so a caller can read one field out of a line.
    """
    last_raw = None
    for attempt in range(1, attempts + 1):
        widen_terminal_echo(engine)
        # Reply intentionally ignored: this is the line that may come back mangled. The
        # return code travels in the file, on the marker line, rather than in the reply.
        # Subshell, not a bare redirect: callers pass pipelines and ``|| true``, and
        # ``a||b >f`` would redirect only ``b``, staging an empty file while the real
        # output went to the terminal. ``$?`` is then the subshell's status, i.e. the
        # status of the command's last stage - the same value a bare run would report.
        engine.run_cmd(
            f"( {command} ) >{MARKED_READ_STAGE_FILE} 2>/dev/null; "
            f"echo {marker}:$? >>{MARKED_READ_STAGE_FILE}",
            validate=False,
        )
        raw = engine.run_cmd(f"cat {MARKED_READ_STAGE_FILE}", validate=False)
        last_raw = raw
        lines = [line.strip() for line in str(raw).splitlines()]
        rc_lines = [
            line for line in lines if re.fullmatch(rf"{re.escape(marker)}:\d+", line)
        ]
        if len(rc_lines) == 1 and rc_lines[0].endswith(":0"):
            matches = [
                re.fullmatch(value_pattern, line)
                for line in lines[:lines.index(rc_lines[0])]
                if line and marker not in line
            ]
            values = [
                match.group(1) if match.lastindex else match.group(0)
                for match in matches if match
            ]
            if len(values) == 1:
                return values[0]
        if attempt < attempts:
            logger.warning("Invalid %s response on attempt %d/%d; retrying", marker, attempt, attempts)
            time.sleep(MARKED_READ_RETRY_DELAY_SEC)
    raise AssertionError(f"Unexpected {marker} output after {attempts} attempts: {last_raw!r}")


def probe_leakage_sensor_count(engine: LinuxSshEngine) -> Optional[int]:
    """Return ``leakage_counter`` if the DUT reports one > 0; else ``None`` (suite N/A).

    Two failure modes must not be conflated, because the module gate skips on None:

    * The marker line *arrives* and reports a read error, a non-digit or 0 — the DUT
      genuinely has no leakage sensors, so ``None`` and a skip are correct.
    * No marker line arrives at all. That says nothing about the platform: the engine
      can hand back the echoed command instead of the reply, and async terminal output
      can replace it (seen on mlx-6600-04 2026-09-06, first read after a reboot — the
      whole suite skipped and the run still exited 0). Retried, then raised, so a
      transport hiccup can never masquerade as "no sensors" and report green.

    Staged through a file like ``_read_marked_terminal_value``, for the same reason: the
    composed one-liner ran to 84 characters, and a reply that arrives as the wrapped echo
    is exactly the "no marker line" case above - the one that costs a rerun. ``2>&1``
    rather than ``2>/dev/null`` here because the read error text is what tells the two
    failure modes apart, and only ``isdigit()`` lines are ever taken as the count.
    """
    marker_re = re.compile(r"__NGTS_LEAKAGE_COUNT:(\d+)")
    for attempt in range(1, MARKED_READ_ATTEMPTS + 1):
        widen_terminal_echo(engine)
        engine.run_cmd(
            f"( cat {LEAKAGE_COUNTER_PATH} ) >{MARKED_READ_STAGE_FILE} 2>&1; "
            f"echo __NGTS_LEAKAGE_COUNT:$? >>{MARKED_READ_STAGE_FILE}",
            validate=False,
        )
        raw = engine.run_cmd(f"cat {MARKED_READ_STAGE_FILE}", validate=False)
        lines = [line.strip() for line in str(raw).splitlines()]
        marked = [index for index, line in enumerate(lines) if marker_re.fullmatch(line)]
        if len(marked) == 1:
            read_rc = marker_re.fullmatch(lines[marked[0]]).group(1)
            if read_rc != "0":
                logger.info(
                    "leakage_counter read failed (rc=%s, output=%r) — DUT has no leakage sensors",
                    read_rc, lines[:marked[0]],
                )
                return None
            values = [line for line in lines[:marked[0]] if line.isdigit()]
            if len(values) == 1:
                count = int(values[0])
                return count if count > 0 else None
            logger.warning(
                "leakage_counter read succeeded but returned no single numeric line "
                "(attempt %s/%s); lines=%r",
                attempt, MARKED_READ_ATTEMPTS, lines[:marked[0]],
            )
        else:
            logger.warning(
                "leakage_counter probe returned no marker line (attempt %s/%s); raw=%r",
                attempt, MARKED_READ_ATTEMPTS, str(raw)[:200],
            )
        if attempt < MARKED_READ_ATTEMPTS:
            time.sleep(MARKED_READ_RETRY_DELAY_SEC)
    raise AssertionError(
        f"leakage_counter probe never returned its marker after {MARKED_READ_ATTEMPTS} "
        f"attempts — cannot determine whether this DUT has leakage sensors, so the "
        f"suite fails instead of skipping green"
    )


def skip_suite_unless_leakage_sensors(engine: LinuxSshEngine) -> int:
    """Module gate: skip the whole suite when the DUT has no leakage sensors."""
    count = probe_leakage_sensor_count(engine)
    if count is None:
        pytest.skip(
            f"SPC-6 leakage suite requires leakage sensors "
            f"({LEAKAGE_COUNTER_PATH} missing, unreadable, or 0) — skipping module"
        )
    logger.info("Leakage sensor gate OK: leakage_counter=%s", count)
    return count


def assert_cpld_masks_cover_all_sensors(count: int) -> None:
    """Module gate: the CPLD mask table must line up with the DUT's sensor inventory.

    Core #2 parametrizes over ``CPLD_MASK_BY_SENSOR_INDEX``, and injection is the only
    way this suite creates a leak — so a drift between the table and ``leakage_counter``
    silently changes what "green" means, in either direction:

    * sensor with no mask → never injected; the suite still passes while covering only
      part of the platform (a 4-sensor SKU would report green on 2-sensor coverage),
    * mask with no sensor → Core #2 injects a bit for a sensor the DUT does not have,
      which fails later as a confusing surface mismatch instead of a setup error.

    Extending to more sensors is a deliberate act (mask per bit, sim support): add the
    entries documented at ``CPLD_MASK_BY_SENSOR_INDEX`` rather than relaxing this gate.

    ``1..count`` is not a guess at the index set — it is the only inventory the platform
    exposes. ``leakage_counter`` is a count, and hw-mgmt pre-creates a leaf per *wireable*
    input rather than per populated sensor (mlx-6600-04: leakage1…leakage5 all present and
    reading OK while the counter is 2), so no per-index "populated" flag exists to read a
    real set from. The whole suite models the inventory this way — ``leakage_sensor_names``
    builds leakage1…leakageN, and NVUE/smonctl/PNE/gNMI/BMC checks key off those names — so
    a platform with gapped indices must stop here deliberately, with the offending indices
    named, instead of letting Core #2 inject a bit for a sensor the DUT does not have.
    """
    expected_indices = set(range(1, count + 1))
    mask_indices = set(CPLD_MASK_BY_SENSOR_INDEX)
    uncovered = sorted(expected_indices - mask_indices)
    assert not uncovered, (
        f"DUT reports {count} leakage sensors but CPLD_MASK_BY_SENSOR_INDEX has no mask "
        f"for index/indices {uncovered} — Core #2 would silently skip injecting on "
        f"{[f'leakage{i}' for i in uncovered]} and the suite would pass on partial "
        f"coverage. Add the masks (see CPLD_MASK_BY_SENSOR_INDEX) before running here."
    )
    stale = sorted(mask_indices - expected_indices)
    assert not stale, (
        f"CPLD_MASK_BY_SENSOR_INDEX defines masks for index/indices {stale} but the DUT "
        f"reports only {count} leakage sensor(s) — Core #2 would inject on a sensor this "
        f"platform does not have. Align the mask table with this SKU."
    )
    logger.info(
        "CPLD injection coverage gate OK: masks %s cover all %d leakage sensor(s)",
        sorted(mask_indices), count,
    )


def read_leakage_sensor_count(engine: LinuxSshEngine) -> int:
    raw = _read_marked_terminal_value(
        engine,
        f"cat {LEAKAGE_COUNTER_PATH}",
        "__NGTS_LEAKAGE_COUNT",
        r"\d+",
    )
    assert raw is not None
    count = int(raw)
    assert count > 0, f"Unexpected leakage_counter: {raw!r}"
    return count


def read_leakage_sysfs_capacity(engine: LinuxSshEngine) -> int:
    """Count hw-mgmt leakage sysfs leaves — the platform's maximum sensor slots.

    See LEAKAGE_SYSFS_LEAF_PATTERN: leaves exist per wireable input, so this is the
    hard ceiling for ``leakage_counter``. ``|| true`` keeps a zero count readable
    (``grep -c`` exits 1 on no match) instead of failing the marked read.

    This composed read reached 86 characters and broke on mlx-6600-08 (2026-09-08) once
    ``stty`` widening stopped taking, which is what moved _read_marked_terminal_value to
    a staged read; length no longer decides whether it works. A missing folder surfaces
    as a failed read instead of a silent zero, which is the better outcome for a gate.
    """
    raw = _read_marked_terminal_value(
        engine,
        f"ls {PlatformConsts.LEAKAGE_FILES_FOLDER}"
        f"|grep -cE '^{LEAKAGE_SYSFS_LEAF_PATTERN}$'||true",
        "__NGTS_CAP",
        r"\d+",
    )
    assert raw is not None
    return int(raw)


def get_leakage_sensors(engine: LinuxSshEngine, devices) -> List[str]:
    count = read_leakage_sensor_count(engine)
    capacity = read_leakage_sysfs_capacity(engine)
    assert count <= capacity, (
        f"leakage_counter={count} exceeds the {capacity} leakage leaves under "
        f"{PlatformConsts.LEAKAGE_FILES_FOLDER} — the counter is corrupt or hw-mgmt is "
        f"inconsistent; refusing to build a sensor list the DUT cannot report on"
    )
    logger.info("Leakage inventory: leakage_counter=%d, sysfs capacity=%d", count, capacity)
    device_names = getattr(devices.dut, "list_of_leakages", None) or []
    if device_names and len(device_names) != count:
        # Do not let the mismatch pass silently: below it changes which names we use.
        logger.warning(
            "Device model declares %d leakage sensors %s but DUT leakage_counter=%d — "
            "following the DUT",
            len(device_names), list(device_names), count,
        )
    if len(device_names) >= count:
        return list(device_names[:count])
    return leakage_sensor_names(count)


def leakage_sysfs_filename(sensor_name: str) -> str:
    """Normalize a NOS leakage sysfs leaf (``leakage1``, …). Not for BMC / ``leakage_aggr``."""
    normalized = sensor_name.lower().replace("-", "")
    if not re.fullmatch(r"leakage[0-9_]+", normalized):
        raise ValueError(
            f"Not a NOS leakage sysfs name (expected leakage[0-9_]+): {sensor_name!r}"
        )
    return normalized


def _bmc_hwmgmt_leakage_leaf(sensor_name: str) -> str:
    """Validate BMC hw-mgmt leakage leaf (``leakageN`` or ``leakage_aggr``)."""
    leaf = sensor_name.strip()
    if not re.fullmatch(r"leakage(?:[0-9_]+|_aggr)", leaf):
        raise ValueError(
            f"Not a BMC hw-mgmt leakage name (expected leakage[0-9_]+|leakage_aggr): "
            f"{sensor_name!r}"
        )
    return leaf


def get_hwmon_leakage_folder(engine: LinuxSshEngine) -> str:
    ls_output = engine.run_cmd(f"ls -la {PlatformConsts.LEAKAGE_FILES_FOLDER}")
    match = re.search(r"hwmon/([^/]+)/leakage", ls_output)
    assert match, f"Could not resolve hwmon folder under {PlatformConsts.LEAKAGE_FILES_FOLDER}"
    return match.group(1)


def _nos_leakage_sysfs_path(sensor_name: str) -> str:
    return f"{PlatformConsts.LEAKAGE_FILES_FOLDER}{leakage_sysfs_filename(sensor_name)}"


def remove_sysfs_leakage_file(engine: LinuxSshEngine, sensor_name: str) -> None:
    """Remove the NOS hw-mgmt leakage path (symlink, file, or dir). Core #4 missing-file case."""
    path = _nos_leakage_sysfs_path(sensor_name)
    engine.run_cmd(f"sudo rm -rf {shlex.quote(path)}")
    logger.info("Removed NOS sysfs leakage path %s", path)


def sysfs_leakage_path_is_absent(engine: LinuxSshEngine, sensor_name: str) -> bool:
    """True if the NOS hw-mgmt leakage path does not exist (no file/symlink/dir)."""
    path = _nos_leakage_sysfs_path(sensor_name)
    qpath = shlex.quote(path)
    out = engine.run_cmd(
        f"if [ -e {qpath} ] || [ -L {qpath} ]; then echo PRESENT; else echo ABSENT; fi",
        validate=False,
    ).strip()
    return out.endswith("ABSENT") or out == "ABSENT"


# Core #4 missing-file settle: hw-mgmt may recreate the symlink from CPLD; fixed sleep is flaky.
SYSFS_MISSING_FILE_SETTLE_TIMEOUT = 30
SYSFS_MISSING_FILE_POLL_INTERVAL = 2


def wait_for_sysfs_leakage_missing_settled(
    engine: LinuxSshEngine,
    sensor_name: str,
    timeout: int = SYSFS_MISSING_FILE_SETTLE_TIMEOUT,
    interval: int = SYSFS_MISSING_FILE_POLL_INTERVAL,
) -> None:
    """After remove: keep the path absent and wait until smonctl no longer reports OK.

    hw-mgmt sometimes recreates ``/var/run/hw-management/system/leakageN`` from CPLD.
    When that happens we re-delete and keep polling. Raises if smonctl still shows
    status 1 / OK when the deadline expires, or if it never returned parseable output.
    """
    path = _nos_leakage_sysfs_path(sensor_name)
    deadline = time.time() + timeout
    last_smon = None
    recreate_count = 0
    parse_failures = 0
    last_parse_error = None
    parseable_seen = False

    while time.time() < deadline:
        if not sysfs_leakage_path_is_absent(engine, sensor_name):
            recreate_count += 1
            logger.warning(
                "NOS sysfs %s reappeared after remove (likely hw-mgmt recreate #%d); removing again",
                path, recreate_count,
            )
            engine.run_cmd(f"sudo rm -rf {shlex.quote(path)}")

        try:
            last_smon = read_smonctl_leakage(engine)
        except AssertionError as err:
            # Not fatal here. This loop runs in the window just after the sysfs path was pulled
            # out from under smond, and the read uses validate=False, so an error line on stdout
            # is the answer this window invites. One unparseable read means "not settled yet";
            # a persistently unparseable smonctl is caught by the parseable_seen check below.
            parse_failures += 1
            last_parse_error = err
            logger.info(
                "missing-file settle: smonctl output not parseable (#%d); retrying (%.0fs left): %s",
                parse_failures, max(0, deadline - time.time()), err,
            )
            time.sleep(interval)
            continue
        parseable_seen = True

        try:
            assert_sysfs_fault_smonctl_not_ok(last_smon, sensor_name)
            logger.info(
                "missing-file settle OK for %s (recreates=%d, smonctl=%s)",
                sensor_name, recreate_count, last_smon,
            )
            # Final path check — surfaces must see a still-missing path when possible
            if not sysfs_leakage_path_is_absent(engine, sensor_name):
                logger.warning(
                    "smonctl already not-OK for %s but path %s exists again; removing before surface checks",
                    sensor_name, path,
                )
                engine.run_cmd(f"sudo rm -rf {shlex.quote(path)}")
            return
        except AssertionError:
            logger.info(
                "missing-file settle: %s still OK in smonctl; retrying (%.0fs left)",
                sensor_name, max(0, deadline - time.time()),
            )
            time.sleep(interval)

    if not parseable_seen:
        raise AssertionError(
            f"After removing {path}, smonctl never returned parseable JSON within {timeout}s "
            f"({parse_failures} unparseable reads, recreates={recreate_count}) — cannot prove "
            f"{sensor_name!r} stopped reporting OK. Last parse error: {last_parse_error}"
        )

    raise AssertionError(
        f"After removing {path}, smonctl still reports {sensor_name!r} as OK within {timeout}s "
        f"(recreates={recreate_count}, unparseable reads={parse_failures}). Last smonctl: {last_smon}"
    )


def inject_leak_via_sysfs(engine: LinuxSshEngine, sensor_name: str, sysfs_value: str) -> None:
    """Replace the hw-mgmt leakage symlink/file with a plain file containing ``sysfs_value``.

    Sequence (safer than a single ``unlink && echo``):
      1. ``rm -rf`` the existing path (symlink into mlxreg hwmon, prior plain file, or dir)
      2. ``printf '%s\\n' <value> | sudo tee <path>`` into a new regular file

    ``printf`` + ``tee`` (not ``sh -c`` with nested quotes) writes special characters
    (``$``, ``!``, ``*``, ``&``, …) and empty strings literally without shell
    interpretation. Nested ``shlex.quote`` inside ``sh -c '...'`` previously broke the
    ``special_chars`` fault case into a silent missing-file path.

    This is Core #4 only — never used for Core #1–#3 leak injection.
    """
    path = _nos_leakage_sysfs_path(sensor_name)
    qpath = shlex.quote(path)
    engine.run_cmd(f"sudo rm -rf {qpath}")
    engine.run_cmd(
        f"printf '%s\\n' {shlex.quote(sysfs_value)} | sudo tee {qpath} >/dev/null"
    )
    logger.info("Injected NOS sysfs fault content at %s: %r", path, sysfs_value)


def corrupt_sysfs_leakage_unreadable(engine: LinuxSshEngine, sensor_name: str) -> None:
    """Core #4: leave a leakage path present but unreadable (chmod 000)."""
    path = _nos_leakage_sysfs_path(sensor_name)
    qpath = shlex.quote(path)
    # Start from a plain file with a valid-looking value, then revoke all permissions.
    inject_leak_via_sysfs(engine, sensor_name, PlatformConsts.LEAK_STATUS_OK)
    engine.run_cmd(f"sudo chmod 000 {qpath}")
    ls_out = engine.run_cmd(f"ls -la {qpath}", validate=False)
    logger.info("Made NOS sysfs leakage path unreadable: %s (%s)", path, ls_out.strip())


def corrupt_sysfs_leakage_as_directory(engine: LinuxSshEngine, sensor_name: str) -> None:
    """Core #4: replace the leakage file/symlink with a directory (wrong type)."""
    path = _nos_leakage_sysfs_path(sensor_name)
    qpath = shlex.quote(path)
    engine.run_cmd(f"sudo rm -rf {qpath}")
    engine.run_cmd(f"sudo mkdir {qpath}")
    ls_out = engine.run_cmd(f"ls -ld {qpath}", validate=False)
    logger.info("Replaced NOS sysfs leakage path with directory: %s (%s)", path, ls_out.strip())


def corrupt_sysfs_leakage_binary_garbage(engine: LinuxSshEngine, sensor_name: str) -> None:
    """Core #4: replace the leakage path with a file of non-text binary bytes."""
    path = _nos_leakage_sysfs_path(sensor_name)
    qpath = shlex.quote(path)
    engine.run_cmd(f"sudo rm -rf {qpath}")
    # NUL / 0xff / 0xfe — not valid UTF-8 text and not a polarity digit.
    # printf|tee avoids nested sh -c quoting pitfalls (same pattern as inject_leak_via_sysfs).
    engine.run_cmd(f"printf '\\000\\377\\376' | sudo tee {qpath} >/dev/null")
    xxd_out = engine.run_cmd(
        f"xxd -p {qpath} 2>/dev/null || od -An -tx1 {qpath}", validate=False
    )
    logger.info("Wrote binary garbage to NOS sysfs leakage path %s: %s", path, xxd_out.strip())


def restore_sysfs_leakage_links(
    engine: LinuxSshEngine,
    sensor_names: Sequence[str],
    hwmon_folder: str,
) -> None:
    """Restore NOS hw-mgmt leakage paths as symlinks into the mlxreg hwmon tree.

    Verifies each restore with ``[ -L path ]`` + ``readlink`` matching the expected
    target. Retries per sensor; on exhaustion sets ``sysfs_restore_failed()`` so the
    suite autouse can abort remaining tests (DUT left without usable leakage sysfs).
    """
    global _SYSFS_RESTORE_FAILED
    for name in sensor_names:
        path = _nos_leakage_sysfs_path(name)
        leakage_file = leakage_sysfs_filename(name)
        target = (
            f"{PlatformConsts.LEAKAGE_FILES_SYSFS_FOLDER}"
            f"{hwmon_folder}/{leakage_file}"
        )
        qpath = shlex.quote(path)
        qtarget = shlex.quote(target)
        last_err: Optional[BaseException] = None
        for attempt in range(1, SYSFS_RESTORE_ATTEMPTS + 1):
            try:
                engine.run_cmd(f"sudo rm -rf {qpath}", validate=True)
                engine.run_cmd(f"sudo ln -s {qtarget} {qpath}", validate=True)
                # Single-line marker: LinuxSshEngine.run_cmd often surfaces only the
                # first/last line of multi-line shell output, which dropped readlink
                # when it was printed on a second line (Core4 false restore fail).
                check = engine.run_cmd(
                    f"if [ -L {qpath} ]; then "
                    f"printf 'link_ok:%s\\n' \"$(readlink -n {qpath})\"; "
                    f"else echo link_missing; fi",
                    validate=False,
                ).strip()
                assert check.startswith("link_ok:"), (
                    f"sysfs restore {path!r}: not a symlink after ln -s "
                    f"(attempt {attempt}/{SYSFS_RESTORE_ATTEMPTS}): {check!r}"
                )
                actual_target = check.split(":", 1)[1].strip()
                assert actual_target == target, (
                    f"sysfs restore {path!r}: readlink={actual_target!r}, "
                    f"expected {target!r} (attempt {attempt}/{SYSFS_RESTORE_ATTEMPTS})"
                )
                logger.info(
                    "Restored NOS sysfs leakage symlink %s -> %s (attempt %s/%s)",
                    path, target, attempt, SYSFS_RESTORE_ATTEMPTS,
                )
                last_err = None
                break
            except Exception as err:
                last_err = err
                logger.warning(
                    "sysfs restore attempt %s/%s failed for %s: %s",
                    attempt, SYSFS_RESTORE_ATTEMPTS, path, err,
                )
                if attempt < SYSFS_RESTORE_ATTEMPTS:
                    time.sleep(SYSFS_RESTORE_RETRY_DELAY_SEC)
        if last_err is not None:
            _SYSFS_RESTORE_FAILED = True
            logger.error(
                "CRITICAL: sysfs leakage restore failed after %s attempts for %s: %s — "
                "remaining leakage tests will abort",
                SYSFS_RESTORE_ATTEMPTS, path, last_err,
            )
            raise RuntimeError(
                f"sysfs leakage restore failed after {SYSFS_RESTORE_ATTEMPTS} attempts "
                f"for {path}; DUT may lack a usable leakage symlink. "
                f"manual: sudo ln -sf {target} {path}"
            ) from last_err


def sysfs_restore_failed() -> bool:
    """True if a prior Core #4 sysfs symlink restore exhausted retries."""
    return _SYSFS_RESTORE_FAILED


def cpld_clear_failed() -> bool:
    """True if a prior CPLD clear/verify in cleanup/recovery failed."""
    return _CPLD_CLEAR_FAILED


def mark_cpld_clear_failed() -> None:
    """Poison the module: remaining tests must abort (DUT may still be simulating leak)."""
    global _CPLD_CLEAR_FAILED
    _CPLD_CLEAR_FAILED = True
    logger.error(
        "CPLD clear/verify failed — marking module poison "
        "(DUT may still have i2ctransfer leak sim injected)"
    )


# Core #4 — invalid *content* written into a plain replacement file.
# Valid polarity "0"/"1" excluded.
SYSFS_FAULT_VALUES: Sequence[tuple] = (
    ("invalid_int_99", "99"),
    ("float_1_5", "1.5"),
    ("large_int", "999999999999"),
    ("negative_int", "-1"),
    ("invalid_int_2", "2"),
    ("empty", ""),
    ("alpha_mixed", "abcXYZ"),
    ("special_chars", "?!@$#^^*&$"),
)

# Core #4 — structural corruption (not string content). Each entry is (label, kind).
# kind dispatched by apply_sysfs_fault_corruption().
SYSFS_FAULT_CORRUPTIONS: Sequence[tuple] = (
    ("unreadable_chmod_000", "unreadable"),
    ("wrong_type_directory", "directory"),
    ("binary_garbage", "binary"),
)


_SYSFS_CORRUPTION_DISPATCH = {
    "unreadable": corrupt_sysfs_leakage_unreadable,
    "directory": corrupt_sysfs_leakage_as_directory,
    "binary": corrupt_sysfs_leakage_binary_garbage,
}


def apply_sysfs_fault_corruption(engine: LinuxSshEngine, sensor_name: str, kind: str) -> None:
    """Dispatch Core #4 structural corruption by kind key from SYSFS_FAULT_CORRUPTIONS."""
    handler = _SYSFS_CORRUPTION_DISPATCH.get(kind)
    if handler is None:
        raise ValueError(f"Unknown sysfs fault corruption kind: {kind!r}")
    handler(engine, sensor_name)


def open_bmc_oob_shell(
    oob_ip: str,
    tries: int = BMC_OOB_SSH_TRIES,
    retry_delay: int = BMC_OOB_SSH_RETRY_DELAY_SEC,
) -> LinuxSshEngine:
    """Open BMC OOB SSH; prove the session with a real command (construction is lazy).

    ``LinuxSshEngine`` / ``ProxySshEngine`` only connect when ``run_cmd`` (or ``engine``)
    is first used — ``__init__`` cannot raise on auth failure. Probe with ``true`` so
    password fallback and ``wait_for_bmc_oob_ssh`` polling actually detect a live BMC.
    Empty probe output is OK on this BMC (dropbear / prompt quirks); exceptions are not.
    Failed engines are disconnected before the next password or attempt.
    """
    last_err = None
    passwords = []
    try:
        passwords.append(get_cl_bmc_root_password())
    except RuntimeError as err:
        logger.warning("%s; will try factory OPENBMC only", err)
    passwords.append(OPENBMC)
    for attempt in range(1, tries + 1):
        for pw_index, pw in enumerate(passwords):
            eng = None
            try:
                eng = LinuxSshEngine(ip=oob_ip, username=ROOT, password=pw)
                # Force connect + auth; do not retry the probe itself (outer loops own retries).
                eng.run_cmd(
                    "true",
                    validate=False,
                    timeout=BMC_OOB_SSH_PROBE_TIMEOUT,
                    retry_run=False,
                )
                logger.info(
                    "BMC OOB SSH authenticated to %s (password index %d)",
                    oob_ip, pw_index,
                )
                return eng
            except Exception as err:
                last_err = f"{type(err).__name__}: {err}"
                close_bmc_oob_shell(eng)
        if attempt < tries:
            logger.warning(
                "BMC OOB SSH attempt %d/%d failed (%s); retrying in %ds",
                attempt, tries, last_err, retry_delay,
            )
            time.sleep(retry_delay)
    raise AssertionError(
        f"Could not SSH to BMC OOB {oob_ip} after {tries} attempts: {last_err}. "
        f"Ensure CL_BMC_ROOT_PASSWORD is set in the environment if the BMC is not on "
        f"factory default."
    )


def close_bmc_oob_shell(shell) -> None:
    """Best-effort disconnect of a BMC OOB SSH shell (ignore missing/failed disconnect)."""
    if shell is None:
        return
    try:
        disconnect = getattr(shell, "disconnect", None)
        if callable(disconnect):
            disconnect()
    except Exception as err:
        logger.warning("BMC OOB shell disconnect failed: %s", err)


def _bmc_i2ctransfer_cmd(reg_high: str, value: str) -> str:
    return (
        f"i2ctransfer -f -y {BMC_CPLD_I2C_BUS} w3@{BMC_CPLD_I2C_ADDR} "
        f"{BMC_CPLD_LEAK_REG} {reg_high} {value}"
    )


def _bmc_i2ctransfer_read_cmd(reg_high: str) -> str:
    return (
        f"i2ctransfer -f -y {BMC_CPLD_I2C_BUS} w2@{BMC_CPLD_I2C_ADDR} "
        f"{BMC_CPLD_LEAK_REG} {reg_high} r1"
    )


def read_bmc_cpld_register(bmc_engine: LinuxSshEngine, reg_high: str) -> str:
    """Read one byte from a CPLD leak register. Returns a hex string e.g. '0xfe'."""
    cmd = _bmc_i2ctransfer_read_cmd(reg_high)
    with allure.step(f"BMC CPLD read: {cmd}"):
        return bmc_engine.run_cmd(cmd).strip()


def verify_cpld_clear(bmc_engine: LinuxSshEngine) -> None:
    """Assert both sensor and aggregate CPLD registers are fully cleared (0xff)."""
    sensor_val = read_bmc_cpld_register(bmc_engine, BMC_CPLD_LEAK_SENSOR_REG)
    assert sensor_val == BMC_CPLD_CLEAR_ALL, (
        f"CPLD sensor register not cleared: expected {BMC_CPLD_CLEAR_ALL}, got {sensor_val!r}"
    )
    aggr_val = read_bmc_cpld_register(bmc_engine, BMC_CPLD_LEAK_AGGR_REG)
    assert aggr_val == BMC_CPLD_LEAK_AGGR_CLEAR, (
        f"CPLD aggregate register not cleared: expected {BMC_CPLD_LEAK_AGGR_CLEAR}, got {aggr_val!r}"
    )


def clear_and_verify_cpld(
    bmc_engine: LinuxSshEngine,
    retries: int = CPLD_WRITE_RETRIES,
    delay: int = CPLD_WRITE_RETRY_DELAY_SEC,
) -> None:
    """Clear all CPLD leak bits and verify the registers read back 0xff.

    Retries the full write+read cycle up to ``retries`` times to tolerate
    transient I2C glitches. Raises AssertionError if the registers still do not
    clear after all attempts — the test cannot run without a clean baseline.
    """
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            clear_all_bmc_cpld_leaks(bmc_engine)
            verify_cpld_clear(bmc_engine)
            return
        except Exception as err:
            last_err = err
            if attempt < retries:
                logger.warning(
                    "CPLD clear/verify attempt %d/%d failed: %s; retrying in %ds",
                    attempt, retries, err, delay,
                )
                time.sleep(delay)
    raise AssertionError(
        f"CPLD could not be cleared after {retries} attempts: {last_err}"
    )


def clear_and_verify_cpld_with_revive(
    bmc_engine: LinuxSshEngine,
    oob_ip: Optional[str] = None,
) -> LinuxSshEngine:
    """Clear the CPLD, re-opening the OOB shell once if the handle is dead.

    Cleanup has to survive a stranded session. ``wait_for_bmc_oob_ssh_down`` closes the
    shell before it starts polling, so a test aborting inside that window leaves a closed
    handle behind and the leak still injected in the CPLD. Callers must assign the return
    value back: it may be a replacement engine. Re-raises when there is no ``oob_ip`` to
    revive from, or when the fresh shell cannot clear either.
    """
    try:
        clear_and_verify_cpld(bmc_engine)
        return bmc_engine
    except Exception as first_err:
        if not oob_ip:
            raise
        logger.warning(
            "CPLD clear failed on the current OOB shell (%s: %s) — reopening %s once",
            type(first_err).__name__, first_err, oob_ip,
        )
        close_bmc_oob_shell(bmc_engine)
        revived = open_bmc_oob_shell(oob_ip)
        clear_and_verify_cpld(revived)
        logger.info("CPLD cleared after reviving the BMC OOB shell")
        return revived


def inject_leak_via_bmc_cpld(
    bmc_engine: LinuxSshEngine,
    leak_mask_byte: str,
    *,
    aggregate: bool = False,
    retries: int = CPLD_WRITE_RETRIES,
    delay: int = CPLD_WRITE_RETRY_DELAY_SEC,
) -> None:
    """Write a CPLD leak register byte and verify read-back equals ``leak_mask_byte``.

    Retries the full write+read cycle up to ``retries`` times (same pattern as
    :func:`clear_and_verify_cpld`) to tolerate transient I2C glitches.
    """
    reg = BMC_CPLD_LEAK_AGGR_REG if aggregate else BMC_CPLD_LEAK_SENSOR_REG
    cmd = _bmc_i2ctransfer_cmd(reg, leak_mask_byte)
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            with allure.step(f"BMC CPLD inject: {cmd}"):
                bmc_engine.run_cmd(cmd)
            actual = read_bmc_cpld_register(bmc_engine, reg)
            assert actual == leak_mask_byte, (
                f"CPLD inject verify failed on {reg}: expected {leak_mask_byte}, got {actual!r}"
            )
            return
        except Exception as err:
            last_err = err
            if attempt < retries:
                logger.warning(
                    "CPLD inject/verify attempt %d/%d failed: %s; retrying in %ds",
                    attempt, retries, err, delay,
                )
                time.sleep(delay)
    raise AssertionError(
        f"CPLD inject could not be verified after {retries} attempts: {last_err}"
    )


def clear_all_bmc_cpld_leaks(bmc_engine: LinuxSshEngine) -> None:
    inject_leak_via_bmc_cpld(bmc_engine, BMC_CPLD_CLEAR_ALL, aggregate=False)
    inject_leak_via_bmc_cpld(bmc_engine, BMC_CPLD_LEAK_AGGR_CLEAR, aggregate=True)


def inject_single_sensor_bmc_cpld(bmc_engine: LinuxSshEngine, sensor_index: int) -> None:
    mask = CPLD_MASK_BY_SENSOR_INDEX.get(sensor_index)
    assert mask, f"No CPLD mask defined for sensor index {sensor_index}"
    clear_all_bmc_cpld_leaks(bmc_engine)
    inject_leak_via_bmc_cpld(bmc_engine, mask, aggregate=False)


def resolve_bmc_oob_ip(engines, topology_obj) -> str:
    # Read BMC IPv4 directly from NOGA rather than via BmcTool.get_bmc_ip_addresses,
    # which also attempts IPv6 fetching using sonic_platform.bmc — a SONiC-only module
    # that does not exist on Cumulus Linux.
    oob_ip = (
        topology_obj.players["dut"]["attributes"]
        .noga_query_data["attributes"]["Specific"]
        .get("bmc_ip")
    )
    assert oob_ip, "BMC OOB IPv4 not available (populate bmc_ip in NOGA)"
    return oob_ip


def read_nvue_leakage(nv_command) -> dict:
    return OutputParsingTool.parse_json_str_to_dictionary(
        nv_command.platform.environment.leakage.show()
    ).get_returned_value()


def read_smonctl_leakage(engine: LinuxSshEngine) -> List[dict]:
    """Run smonctl and return the parsed JSON list.

    smonctl -jvt leakage returns a JSON array:
      [{"name": "leakage1", "type": "leakage", "status": 1}, ...]
    where status 1 = normal, 0 = leak.
    """
    raw = engine.run_cmd("smonctl -jvt leakage", validate=False)
    try:
        return parse_terminal_json(raw)
    except (json.JSONDecodeError, ValueError) as err:
        raise AssertionError(
            f"smonctl -jvt leakage returned non-JSON output (command may have failed or "
            f"produced unexpected text); parse error: {err}; raw output: {raw[:300]!r}"
        ) from err


def _redact_cli_secrets(cmd: str) -> str:
    """Redact CLI credential flags for logs/Allure (quoted or bare tokens).

    Covers ``-u`` / ``-p`` (curl, gnmic) and ``user = "name:password"`` curl -K lines.
    """
    redacted = re.sub(
        r"""(-u\s+)(?:'[^']*'|"[^"]*"|\S+)""",
        r"\1***",
        cmd,
    )
    redacted = re.sub(
        r"""(-p\s+)(?:'[^']*'|"[^"]*"|\S+)""",
        r"\1***",
        redacted,
    )
    redacted = re.sub(
        r'(user\s*=\s*")([^":]+):([^"]*)(")',
        r"\1\2:***\4",
        redacted,
    )
    return redacted


def _remove_remote_file(engine: LinuxSshEngine, remote_path: str) -> None:
    """Best-effort remote delete. Never raises: this runs from cleanup paths."""
    try:
        engine.run_cmd(f"rm -f {shlex.quote(remote_path)}", validate=False)
    except Exception:  # noqa: BLE001 - cleanup must not mask the real failure
        logger.warning("could not remove %s from the DUT", remote_path)


def nvue_rest_leakage_config(engine: LinuxSshEngine) -> str:
    """Return the ``curl -K`` config body carrying the NVUE REST credentials."""
    def _curl_cfg_escape(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"')

    return (
        f'user = "{_curl_cfg_escape(engine.username)}:'
        f'{_curl_cfg_escape(engine.password)}"\n'
    )


def _stage_rest_curl_config(engine: LinuxSshEngine) -> str:
    """SCP the curl config to the DUT at mode 0600; return its remote path.

    The password reaches the DUT as file content over SCP and never as part of a
    command string. A command string is held in the remote shell's own
    ``/proc/<pid>/cmdline`` for as long as the read runs, and Linux shows that to any
    local account, so the earlier ``printf ... | curl -K -`` form kept the credential
    off ``curl``'s argv but left it on the shell's. Callers remove the file; see
    :func:`_run_rest_leakage_raw`.
    """
    mktemp_out = str(
        engine.run_cmd(f"umask 077 && mktemp {shlex.quote(REST_CFG_REMOTE_TEMPLATE)}")
    )
    # Pick the reply by the shape mktemp produces rather than by line position: devts
    # echoes commands back and async output can displace a bare reply.
    candidates = _REST_CFG_FILE_RE.findall(mktemp_out)
    if not candidates:
        raise AssertionError(
            f"mktemp for the REST curl config returned no usable path: {mktemp_out!r}"
        )
    remote_path = candidates[-1]
    local_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", delete=False, prefix="ngts-rest-leakage-"
        ) as local_fh:
            local_fh.write(nvue_rest_leakage_config(engine))
            local_path = local_fh.name
        os.chmod(local_path, 0o600)
        engine.copy_file(
            source_file=local_path,
            file_system=REST_CFG_REMOTE_DIR,
            dest_file=os.path.basename(remote_path),
            direction="put",
        )
        # Read the mode back instead of trusting the chmod: SCP takes the destination
        # mode from the transfer, so the 0600 mktemp created is not what survives, and
        # REST_CFG_REMOTE_DIR is world-readable (/var/tmp, 1777). A chmod that quietly
        # failed would leave the switch password in a file any account on the DUT can
        # read, and these switches do carry other people's sessions.
        qpath = shlex.quote(remote_path)
        mode_out = str(
            engine.run_cmd(
                f"chmod 600 {qpath}; stat -c '{REST_CFG_MODE_MARKER}:%a' {qpath}",
                validate=False,
            )
        )
        modes = re.findall(rf"{REST_CFG_MODE_MARKER}:(\d+)", mode_out)
        # Any, not exactly one: devts may replay the command and echo two markers.
        if not modes or any(mode != "600" for mode in modes):
            raise AssertionError(
                f"REST curl config {remote_path} is not mode 600 after chmod "
                f"(read back {modes or 'no mode at all'}); refusing to leave the switch "
                f"password readable by other accounts. output={mode_out!r}"
            )
        return remote_path
    except Exception:
        _remove_remote_file(engine, remote_path)
        raise
    finally:
        if local_path:
            try:
                os.unlink(local_path)
            except OSError:
                logger.debug("could not unlink local REST curl config %s", local_path)


def _run_rest_leakage_raw(engine: LinuxSshEngine) -> str:
    """Run the NVUE REST leakage curl on the DUT, credentials staged in a file."""
    cfg_path = _stage_rest_curl_config(engine)
    try:
        # This string carries only the config's path, so it is safe in logs, in Allure
        # and in the remote shell's process arguments.
        cmd = (
            f"curl -sk -K {shlex.quote(cfg_path)} "
            f"{shlex.quote(NVUE_REST_LEAKAGE_URL)}"
        )
        logger.info("REST leakage: %s", cmd)
        return engine.send_cmd_with_retry(
            cmd, retries=0, timeout=REST_LEAKAGE_READ_TIMEOUT, normalize=True
        )
    finally:
        # Here rather than appended to the command, so a read that raises still cleans up.
        _remove_remote_file(engine, cfg_path)


def read_rest_leakage(engine: LinuxSshEngine) -> dict:
    raw = _run_rest_leakage_raw(engine)
    return OutputParsingTool.parse_json_str_to_dictionary(raw).get_returned_value()


def read_pne_telemetry_leakage_metrics(engine: LinuxSshEngine) -> str:
    # Telemetry here means both gNMI and OTEL — both consume the PNE metric via UMF.
    # We check at the PNE stage (Prometheus endpoint) rather than querying gNMI/OTEL
    # directly: if the metric value is correct here, leakage detection did its job.
    # Verifying UMF routing into gNMI/OTEL belongs in a dedicated telemetry test.
    #
    # -m is curl's own bound rather than a ``timeout`` wrapper (which the BMC ``cat`` read
    # needs only because cat has no equivalent): it ends the transfer from inside curl
    # instead of by signal. The SSH timeout is the backstop for the case -m cannot see, a
    # channel that wedges before curl ever runs. validate=False as before — a failed scrape
    # returns empty, which the poll loops treat as "not yet" and the one-shot call sites
    # already assert on.
    return engine.run_cmd(
        f"curl -s -m {PNE_METRICS_CURL_TIMEOUT} {TELEMETRY_URL}",
        validate=False,
        timeout=PNE_METRICS_READ_TIMEOUT,
    )


def parse_pne_telemetry_leakage_status(metrics_text: str, sensor_name: str) -> Optional[str]:
    pattern = (
        rf'{TELEMETRY_LEAKAGE_METRIC}\{{[^}}]*name="{re.escape(sensor_name)}"[^}}]*\}}\s+(\S+)'
    )
    match = re.search(pattern, metrics_text)
    return match.group(1) if match else None


def count_pne_telemetry_leakage_lines(metrics_text: str) -> int:
    """Return the number of nvswitch_platform_environment_leak_sensor_status metric value lines."""
    return len(re.findall(
        rf'^{re.escape(TELEMETRY_LEAKAGE_METRIC)}\{{[^}}]*\}}\s+\S+',
        metrics_text,
        re.MULTILINE,
    ))


def assert_pne_telemetry_leakage_statuses(
    metrics_text: str,
    sensor_names: Sequence[str],
    expected_status: str,
) -> None:
    """Assert PNE Prometheus metric value for each sensor. expected_status: '1'=OK, '0'=leak.

    Count is verified separately in the test via count_pne_telemetry_leakage_lines().
    """
    for name in sensor_names:
        val = parse_pne_telemetry_leakage_status(metrics_text, name)
        assert val is not None, (
            f"telemetry metric {TELEMETRY_LEAKAGE_METRIC}{{name={name!r}}} "
            f"not found in {TELEMETRY_URL}"
        )
        assert val == expected_status, (
            f"telemetry {name}: expected {expected_status!r}, got {val!r}"
        )


def read_bmc_leak_detection_json(dut_engine: LinuxSshEngine) -> dict:
    """Run bmc-cli sensor-dump and return the parsed JSON block.

    bmc-cli output contains header text, a JSON block, and a plaintext summary
    footer. We locate the first '{' and use raw_decode to extract only the JSON.
    """
    cli = BmcCliTool(dut_engine)
    raw = cli.run(
        "sensor-dump",
        uri=BMC_LEAK_DETECTION_URI,
        ssh_timeout=BMC_CLI_SENSOR_DUMP_TIMEOUT,
    )
    start = raw.find("{")
    assert start != -1, f"No JSON found in bmc-cli sensor-dump output: {raw[:200]}"
    data, _ = json.JSONDecoder().raw_decode(raw, start)
    return data


def assert_bmc_leak_detection_states(
    bmc_data: dict,
    sensor_names: Sequence[str],
    leaking_sensors: Sequence[str],
    bmc_version: Optional[str] = None,
) -> None:
    """Assert BMC LeakDetection sensor member states and count.

    leakage_aggr and other non-sensor members are ignored — only members whose Id
    matches a name in sensor_names are checked and counted.

    leaking_sensors: sensors expected to be in Critical state (empty = baseline, all OK).
    Sensors in sensor_names but not in leaking_sensors are expected to be OK.

    Always asserts membership/count and DetectorState. Status.Health is asserted only
    when the field is present on the member (restored automatically when BMC FW returns
    it). When absent, logs a WARNING citing RM #5171996 (won't fix for CL 5.18) and does
    not fail. summary.health_summary is never asserted (Unknown when Health is missing).

    Note on DetectorState='Critical': indicates the sensor may be in a fault/bad state,
    not merely that it detected moisture. Asserting it here verifies that the BMC Redfish
    layer correctly escalates to Critical when a leak is present — if it doesn't, the sensor
    or the BMC reporting chain is broken.
    """
    members = (
        bmc_data.get("sensor_data", {})
        .get(BMC_LEAK_DETECTION_URI, {})
        .get("_link_LeakDetectors", {})
        .get("_members", [])
    )
    logger.debug("BMC LeakDetection full response: %s", bmc_data)
    assert members, "BMC LeakDetection: no leak detector members found"

    all_ids = [m.get("_data", {}).get("Id", m.get("_uri", "unknown")) for m in members]
    sensor_set = {n.lower() for n in sensor_names}
    sensor_members = [m for m in members if m.get("_data", {}).get("Id", "").lower() in sensor_set]

    # Identity, not arithmetic. A BMC that returned one expected sensor twice and dropped
    # another satisfies any count check, and the loop below then asserts the duplicate twice
    # while never looking at the dropped sensor — a pass on a BMC that lost a sensor. This
    # function already tolerates one Redfish enumeration defect (RM above), so malformed
    # membership is the one thing it must not assume away. Checking the ID set makes the
    # old "more members than expected" case unreachable rather than merely unlikely.
    matched_ids = [m.get("_data", {}).get("Id", "").lower() for m in sensor_members]
    missing = sorted(sensor_set - set(matched_ids))
    duplicated = sorted({i for i in matched_ids if matched_ids.count(i) > 1})
    if missing:
        raise AssertionError(
            f"BMC LeakDetection: expected sensor(s) missing: {missing} — "
            f"expected {list(sensor_names)}, all member IDs found: {all_ids}"
        )
    if duplicated:
        raise AssertionError(
            f"BMC LeakDetection: duplicate detector ID(s) {duplicated} reported — "
            f"expected {list(sensor_names)}, all member IDs found: {all_ids}"
        )

    health_summary = bmc_data.get("summary", {}).get("health_summary")
    if health_summary and set(health_summary.keys()) <= {"Unknown"}:
        logger.warning(
            "BMC %s: summary.health_summary is %s — not asserted (RM #%s; follows missing "
            "per-sensor Status.Health)",
            bmc_version, health_summary, LEAKAGE_BMC_STATUS_HEALTH_BUG_RM,
        )

    leaking_set = {s.lower() for s in leaking_sensors}
    health_asserted = False
    health_skipped = False
    for member in sensor_members:
        data = member.get("_data", {})
        name = data.get("Id", member.get("_uri", "unknown"))
        detector_state = data.get("DetectorState")
        status = data.get("Status") or {}
        # Presence-gated: empty string still counts as present and must match expected.
        health_present = isinstance(status, dict) and "Health" in status
        health = status.get("Health") if health_present else None
        if name.lower() in leaking_set:
            assert detector_state == "Critical", (
                f"BMC LeakDetection {name!r}: DetectorState expected 'Critical', got {detector_state!r}"
            )
            if health_present:
                assert health == "Critical", (
                    f"BMC LeakDetection {name!r}: Status.Health expected 'Critical', got {health!r}"
                )
                health_asserted = True
            else:
                health_skipped = True
        else:
            assert detector_state == "OK", (
                f"BMC LeakDetection {name!r}: DetectorState expected 'OK', got {detector_state!r}"
            )
            if health_present:
                assert health == "OK", (
                    f"BMC LeakDetection {name!r}: Status.Health expected 'OK', got {health!r}"
                )
                health_asserted = True
            else:
                health_skipped = True

    if health_skipped:
        logger.warning(
            "BMC %s: per-sensor Status.Health absent — DetectorState asserted only "
            "(RM #%s, https://redmine.mellanox.com/issues/%s; won't fix for CL 5.18). "
            "Health asserts re-enable automatically when the field returns.",
            bmc_version, LEAKAGE_BMC_STATUS_HEALTH_BUG_RM, LEAKAGE_BMC_STATUS_HEALTH_BUG_RM,
        )
    elif health_asserted:
        logger.info(
            "BMC %s: per-sensor Status.Health present and asserted",
            bmc_version,
        )


def assert_leakage_nvue_rest_states(
    leakage_output: dict,
    sensor_names: Sequence[str],
    expected_state: str,
) -> None:
    """Assert state for each sensor in sensor_names.

    NVUE and REST share the same JSON structure: {sensor_name: {"state": "normal"|"alert"}}.
    Count is verified separately in the test via assert len(output) == len(leakage_sensors).
    """
    for name in sensor_names:
        actual = leakage_output.get(name, {}).get("state")
        assert actual == expected_state, (
            f"{name}: expected {expected_state!r}, got {actual!r}"
        )


def assert_smonctl_leakage_statuses(
    smon_output: List[dict],
    sensor_names: Sequence[str],
    expected_status: int,
) -> None:
    """Assert smonctl leakage statuses. expected_status: 1=normal, 0=leak.

    Count is verified separately in the test via assert len(smon_output) == len(leakage_sensors).
    """
    by_name = {item["name"]: item for item in smon_output}
    for name in sensor_names:
        item = by_name.get(name)
        assert item is not None, f"smonctl: sensor {name!r} not found in output"
        actual = item.get("status")
        assert actual == expected_status, (
            f"smonctl {name}: expected status {expected_status}, got {actual!r}"
        )


def assert_sysfs_fault_nvue_rest_not_ok(
    leakage_output: dict,
    sensor_name: str,
    *,
    sibling_sensors: Optional[Sequence[str]] = None,
) -> None:
    """Core #4 sysfs-fault invariant: faulted sensor must never report NVUE/REST ``normal``.

    Acceptable outcomes (fail-closed), logged explicitly:
      - sensor absent from output
      - state is alert / unknown / leak / anything else non-normal

    When ``sibling_sensors`` is provided, each sibling must be present and ``normal``
    (ABSENT-or-PRESENT paths) — proves the surface answered and only the faulted
    sensor is bad. Omit ``sibling_sensors`` for SN 5 (smonctl stub: empty NVUE/REST
    is expected).
    """
    siblings = [s for s in (sibling_sensors or []) if s.lower() != sensor_name.lower()]
    if sensor_name not in leakage_output:
        if siblings:
            _assert_nvue_rest_siblings_normal(leakage_output, sensor_name, siblings)
        logger.info(
            "NVUE/REST %s: outcome=ABSENT (acceptable fail-closed); keys=%s",
            sensor_name, sorted(leakage_output),
        )
        return
    entry = leakage_output.get(sensor_name, {})
    state = entry.get("state")
    logger.info(
        "NVUE/REST %s: outcome=PRESENT state=%r entry=%s (must not be %r)",
        sensor_name, state, entry, LEAKAGE_NVUE_STATE_NORMAL,
    )
    assert state != LEAKAGE_NVUE_STATE_NORMAL, (
        f"{sensor_name}: expected not {LEAKAGE_NVUE_STATE_NORMAL!r} under sysfs fault, got {state!r}"
    )
    if siblings:
        _assert_nvue_rest_siblings_normal(leakage_output, sensor_name, siblings)


def _assert_nvue_rest_siblings_normal(
    leakage_output: dict, sensor_name: str, siblings: Sequence[str]
) -> None:
    for sib in siblings:
        assert sib in leakage_output, (
            f"NVUE/REST {sensor_name}: sibling {sib!r} missing "
            f"(keys={sorted(leakage_output)}); cannot treat dead/partial surface as pass"
        )
        sib_state = leakage_output.get(sib, {}).get("state")
        assert sib_state == LEAKAGE_NVUE_STATE_NORMAL, (
            f"NVUE/REST {sensor_name}: sibling {sib!r} not "
            f"{LEAKAGE_NVUE_STATE_NORMAL!r} (got {sib_state!r}); "
            f"positive control failed"
        )


def assert_sysfs_fault_smonctl_not_ok(
    smon_output: Optional[List[dict]],
    sensor_name: str,
    *,
    sibling_sensors: Optional[Sequence[str]] = None,
) -> None:
    """Core #4 sysfs-fault invariant: faulted sensor must never report smonctl status 1 (OK).

    Acceptable outcomes (fail-closed), logged explicitly:
      - unparseable / non-JSON (smon_output is None) — only when siblings are not required;
        with siblings, non-JSON fails. Kept as a fail-closed backstop rather than a path any
        caller relies on: the settle helper retries an unparseable read itself instead of
        treating one as settled, since the surfaces checked straight afterwards do pass
        siblings and would reject the same output
      - sensor absent
      - status != 1 (typically 0 = leak)

    When ``sibling_sensors`` is provided, each sibling must report status 1 on a
    parseable surface (ABSENT-or-PRESENT for the faulted sensor).
    """
    siblings = [s for s in (sibling_sensors or []) if s.lower() != sensor_name.lower()]
    if smon_output is None:
        if siblings:
            raise AssertionError(
                f"smonctl {sensor_name}: non-JSON output — cannot prove siblings "
                f"{siblings} still OK; refusing ABSENT-as-pass without positive evidence"
            )
        logger.info("smonctl %s: outcome=NON_JSON (acceptable fail-closed)", sensor_name)
        return
    by_name = {item.get("name"): item for item in smon_output}
    if sensor_name not in by_name:
        if siblings:
            _assert_smonctl_siblings_normal(by_name, sensor_name, siblings)
        logger.info(
            "smonctl %s: outcome=ABSENT (acceptable fail-closed); names=%s",
            sensor_name, sorted(by_name),
        )
        return
    item = by_name[sensor_name]
    actual = item.get("status")
    logger.info(
        "smonctl %s: outcome=PRESENT status=%r item=%s (must not be %r)",
        sensor_name, actual, item, SMONCTL_STATUS_NORMAL,
    )
    assert actual != SMONCTL_STATUS_NORMAL, (
        f"smonctl {sensor_name}: expected not status {SMONCTL_STATUS_NORMAL} under sysfs fault, "
        f"got {actual!r}"
    )
    if siblings:
        _assert_smonctl_siblings_normal(by_name, sensor_name, siblings)


def _assert_smonctl_siblings_normal(
    by_name: dict, sensor_name: str, siblings: Sequence[str]
) -> None:
    for sib in siblings:
        assert sib in by_name, (
            f"smonctl {sensor_name}: sibling {sib!r} missing "
            f"(names={sorted(by_name)}); positive control failed"
        )
        assert by_name[sib].get("status") == SMONCTL_STATUS_NORMAL, (
            f"smonctl {sensor_name}: sibling {sib!r} status="
            f"{by_name[sib].get('status')!r} (expected {SMONCTL_STATUS_NORMAL})"
        )


def assert_sysfs_fault_pne_not_ok(
    metrics_text: str,
    sensor_name: str,
    *,
    sibling_sensors: Optional[Sequence[str]] = None,
) -> None:
    """Core #4 sysfs-fault invariant: faulted sensor must never report PNE OK.

    Acceptable outcomes (fail-closed), logged explicitly:
      - metric line absent
      - value anything other than ``PNE_TELEMETRY_STATUS_OK``

    When ``sibling_sensors`` is provided, each sibling must still report OK
    (ABSENT-or-PRESENT for the faulted sensor) — proves ``/metrics`` is live.
    Omit ``sibling_sensors`` for SN 5 (not-stale-OK only).
    """
    siblings = [s for s in (sibling_sensors or []) if s.lower() != sensor_name.lower()]
    val = parse_pne_telemetry_leakage_status(metrics_text, sensor_name)
    if val is None:
        if siblings:
            if TELEMETRY_LEAKAGE_METRIC not in (metrics_text or ""):
                raise AssertionError(
                    f"telemetry {sensor_name}: ABSENT and scrape has no "
                    f"{TELEMETRY_LEAKAGE_METRIC} — refusing empty/dead /metrics as fail-closed"
                )
            _assert_pne_siblings_ok(metrics_text, sensor_name, siblings)
        logger.info(
            "telemetry %s: outcome=ABSENT (acceptable fail-closed)",
            sensor_name,
        )
        return
    logger.info(
        "telemetry %s: outcome=PRESENT value=%r (must not be %r)",
        sensor_name, val, PNE_TELEMETRY_STATUS_OK,
    )
    assert val != PNE_TELEMETRY_STATUS_OK, (
        f"telemetry {sensor_name}: expected not {PNE_TELEMETRY_STATUS_OK!r} (OK) under sysfs fault, "
        f"got {val!r}"
    )
    if siblings:
        _assert_pne_siblings_ok(metrics_text, sensor_name, siblings)


def _assert_pne_siblings_ok(
    metrics_text: str, sensor_name: str, siblings: Sequence[str]
) -> None:
    for sib in siblings:
        sib_val = parse_pne_telemetry_leakage_status(metrics_text, sib)
        assert sib_val == PNE_TELEMETRY_STATUS_OK, (
            f"telemetry {sensor_name}: sibling {sib!r} value="
            f"{sib_val!r} (expected {PNE_TELEMETRY_STATUS_OK!r}); positive control failed"
        )


# =============================================================================
# SN 5 — smonctl stub (exit 1) while sysfs still tracks CPLD
# =============================================================================

def smonctl_restore_failed() -> bool:
    """True if a prior SN 5 stub restore exhausted retries (DUT may still be stubbed)."""
    return _SMONCTL_RESTORE_FAILED


def resolve_smonctl_path(engine: LinuxSshEngine) -> str:
    """Return absolute path of ``smonctl`` on the DUT (do not hardcode)."""
    path = _read_marked_terminal_value(
        engine, "command -v smonctl", "__NGTS_SMONCTL_PATH", r"/[^\s]+"
    )
    assert path, "smonctl not found on DUT (command -v smonctl empty)"
    return path


def _restore_smonctl_from_backup(engine: LinuxSshEngine, smonctl_path: str) -> str:
    bak = f"{smonctl_path}{SMONCTL_BACKUP_SUFFIX}"
    return engine.run_cmd(
        f"if [ -f {shlex.quote(bak)} ]; then "
        f"sudo mv -f {shlex.quote(bak)} {shlex.quote(smonctl_path)}; "
        f"echo restored; else echo missing_bak; fi",
        validate=False,
    ).strip()


def _verify_smonctl_restored(engine: LinuxSshEngine, smonctl_path: str) -> None:
    """Raise if backup still present or smonctl cannot produce leakage JSON."""
    bak = f"{smonctl_path}{SMONCTL_BACKUP_SUFFIX}"
    bak_ls = engine.run_cmd(
        f"if [ -e {shlex.quote(bak)} ]; then echo bak_present; else echo bak_gone; fi",
        validate=False,
    ).strip()
    assert "bak_gone" in bak_ls, f"smonctl backup still present after restore: {bak}"
    raw = engine.run_cmd("sudo smonctl -jvt leakage 2>&1", validate=False).strip()
    try:
        parsed = parse_terminal_json(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as err:
        raise AssertionError(
            f"smonctl after restore is not usable JSON: {err}; raw={raw[:300]!r}"
        ) from err
    assert isinstance(parsed, list) and parsed, (
        f"smonctl after restore returned empty/non-list: {raw[:300]!r}"
    )


def _restore_smonctl_with_retries(engine: LinuxSshEngine, smonctl_path: str) -> None:
    """Restore smonctl from backup; retry then mark suite poison + raise."""
    global _SMONCTL_RESTORE_FAILED
    bak = f"{smonctl_path}{SMONCTL_BACKUP_SUFFIX}"
    last_err: Optional[BaseException] = None
    for attempt in range(1, SMONCTL_RESTORE_ATTEMPTS + 1):
        try:
            out = _restore_smonctl_from_backup(engine, smonctl_path)
            logger.info(
                "smonctl restore attempt %s/%s: %s",
                attempt, SMONCTL_RESTORE_ATTEMPTS, out,
            )
            _verify_smonctl_restored(engine, smonctl_path)
            logger.info("smonctl restored and verified from %s", bak)
            return
        except Exception as err:
            last_err = err
            logger.warning(
                "smonctl restore attempt %s/%s failed: %s",
                attempt, SMONCTL_RESTORE_ATTEMPTS, err,
            )
            if attempt < SMONCTL_RESTORE_ATTEMPTS:
                time.sleep(SMONCTL_RESTORE_RETRY_DELAY_SEC)
    _SMONCTL_RESTORE_FAILED = True
    logger.error(
        "CRITICAL: smonctl restore failed after %s attempts for %s: %s — "
        "manual: sudo mv -f %s %s  (remaining leakage tests will abort)",
        SMONCTL_RESTORE_ATTEMPTS, smonctl_path, last_err, bak, smonctl_path,
    )
    raise RuntimeError(
        f"smonctl restore failed after {SMONCTL_RESTORE_ATTEMPTS} attempts "
        f"({smonctl_path}); DUT may still be stubbed. "
        f"manual: sudo mv -f {bak} {smonctl_path}"
    ) from last_err


@contextmanager
def smonctl_stubbed(engine: LinuxSshEngine) -> Iterator[str]:
    """Temporarily overwrite ``smonctl`` with ``exit 1`` stub; always restore in finally.

    Owner (Jay): acceptable for a short window; NVUE/health will fail — ignore global
    health, assert leakage fail-closed only. Backup must exist before overwrite.
    Yields the resolved smonctl path. On restore failure after retries, marks
    ``smonctl_restore_failed()`` so the suite can abort remaining tests.

    "Always" covers setup too, not just the body: once the real binary has been replaced the
    restore runs even if the rest of the setup raises before the ``yield``.
    """
    path = resolve_smonctl_path(engine)
    bak = f"{path}{SMONCTL_BACKUP_SUFFIX}"
    # Guards the window between overwriting the real binary and reaching the yield: a failure
    # there used to escape before any restore was armed, leaving an exit-1 smonctl on the DUT
    # with nothing to announce it. Restore-failure sets a suite-wide abort, but that path only
    # runs once a restore has been attempted, so the remaining leakage tests would instead see
    # smonctl failing for no stated reason.
    replaced = False
    try:
        with allure.step(f"Stub smonctl at {path} (backup {bak})"):
            # validate=True: failed cp must not be silent before we overwrite the real binary.
            # Marker check (not `ls` substring): missing bak still prints the path in the error
            # text, which falsely satisfied the old "basename in ls_bak" assert.
            engine.run_cmd(
                f"sudo cp -a {shlex.quote(path)} {shlex.quote(bak)}",
                validate=True,
            )
            bak_check = engine.run_cmd(
                f"if [ -f {shlex.quote(bak)} ]; then echo bak_ok; else echo bak_missing; fi",
                validate=False,
            )
            assert "bak_ok" in bak_check, (
                f"smonctl backup missing after cp -a: {bak_check!r}"
            )
            # Armed after the backup check and before the overwrite, and both halves matter.
            # After: without a backup a restore cannot succeed, so arming earlier would turn
            # a failed cp into a restore failure that aborts the rest of the suite when the
            # real binary was never touched. Before: run_cmd can raise once the command has
            # already run (a reply lost on a dropped connection), so arming afterwards would
            # miss the case this exists for; restoring when the overwrite did not land only
            # moves the backup over an identical binary.
            replaced = True
            engine.run_cmd(
                f"printf '#!/bin/sh\\nexit 1\\n' | sudo tee {shlex.quote(path)} >/dev/null"
            )
            engine.run_cmd(f"sudo chmod +x {shlex.quote(path)}")
            logger.info("smonctl stubbed (exit 1); backup at %s", bak)
        yield path
    finally:
        if replaced:
            with allure.step(f"Restore smonctl from {bak}"):
                _restore_smonctl_with_retries(engine, path)


def assert_smonctl_command_failed(engine: LinuxSshEngine) -> None:
    """Assert ``smonctl -jvt leakage`` does not succeed as a healthy read (SN 5).

    HW 2026-08-04: stub yields exit 1 and no JSON body.
    """
    raw = engine.run_cmd(
        "sudo smonctl -jvt leakage 2>&1; echo __SMONCTL_RC:$?",
        validate=False,
    )
    logger.info("smonctl under stub (raw): %s", raw[:500])
    rc_match = re.search(r"__SMONCTL_RC:(\d+)", raw)
    rc = int(rc_match.group(1)) if rc_match else None
    body = re.sub(r"\n?__SMONCTL_RC:\d+\s*$", "", raw).strip()
    if rc is not None and rc != 0:
        logger.info("smonctl: outcome=EXIT_%s (acceptable fail-closed)", rc)
        return
    # Exit 0 with empty / non-JSON / no status-1 sensors also fail-closed.
    try:
        parsed = json.loads(body) if body else None
    except (TypeError, ValueError, json.JSONDecodeError):
        logger.info("smonctl: outcome=NON_JSON under exit-0 (acceptable fail-closed)")
        return
    if not parsed:
        logger.info("smonctl: outcome=EMPTY (acceptable fail-closed)")
        return
    by_name = {
        item.get("name"): item
        for item in parsed
        if isinstance(item, dict)
    }
    ok_sensors = [
        name for name, item in by_name.items()
        if item.get("status") == SMONCTL_STATUS_NORMAL
    ]
    assert not ok_sensors, (
        f"smonctl reported OK (status={SMONCTL_STATUS_NORMAL}) under stub for {ok_sensors}; "
        f"rc={rc} body={body[:300]!r}"
    )


def assert_smonctl_fault_gnmi_not_ok(
    client: GnmiClient,
    sensor_name: str,
) -> None:
    """SN 5: gNMI must not report stale alarm-status=false while sysfs shows leak.

    Acceptable (HW 2026-08-04): empty / error / None / true. Fail only on ``false``.
    """
    val = read_gnmi_leakage_alarm_status(client, sensor_name)
    logger.info(
        "gNMI %s under smonctl fault: alarm-status=%r (must not be %r)",
        sensor_name, val, GNMI_ALARM_STATUS_OK,
    )
    if val is None:
        logger.info("gNMI %s: outcome=ABSENT/ERROR (acceptable fail-closed)", sensor_name)
        return
    assert val != GNMI_ALARM_STATUS_OK, (
        f"gNMI {sensor_name}: stale alarm-status={GNMI_ALARM_STATUS_OK!r} under smonctl "
        f"fault while sysfs shows leak"
    )


def assert_smonctl_fault_otel_not_ok(
    engine: OtelCollectorEngine,
    sensor_name: str,
    *,
    file_offset_at_fault: int,
) -> None:
    """SN 5: OTEL must not live-report OK (1.0) under smonctl fault.

    Inspect only collector bytes written **after** ``file_offset_at_fault`` (size at
    fault inject). Whole-file mtime is not enough: other platform-stats batches keep
    advancing mtime while the last *leakage-containing* batch can still be pre-fault
    OK — that was a false fail on HW 2026-08-04.

    Acceptable fail-closed:
      - no post-fault leakage sample for the sensor (ABSENT; same class as PNE)
      - post-fault sample present but not OK (e.g. 0.0)
    Fail only when a post-fault batch still reports ``1.0``.
    """
    values = read_otel_leakage_latest_after_offset(engine, file_offset_at_fault)
    val = values.get(sensor_name)
    logger.info(
        "OTEL %s under smonctl fault: post-fault value=%r file_offset_at_fault=%s",
        sensor_name, val, file_offset_at_fault,
    )
    if val is None:
        logger.info("OTEL %s: outcome=ABSENT (acceptable fail-closed)", sensor_name)
        return
    if val != OTEL_LEAKAGE_STATUS_OK:
        logger.info(
            "OTEL %s: outcome=NOT_OK value=%r (acceptable fail-closed)",
            sensor_name, val,
        )
        return
    raise AssertionError(
        f"OTEL {sensor_name}: live post-fault sample still {OTEL_LEAKAGE_STATUS_OK} "
        f"under smonctl fault (file_offset_at_fault={file_offset_at_fault})"
    )


def read_health_history_since(
    engine: LinuxSshEngine,
    slack_sec: int = HEALTH_HISTORY_SINCE_SLACK_SEC,
) -> datetime:
    """Boundary for ``search_line_by_date``, read from the **DUT** clock.

    health_history stamps are written by the DUT, so the boundary must come from the
    same clock — a tester-clock boundary only budgets for skew, and a DUT running ahead
    lets stale entries from earlier runs pass the date filter.

    Cumulus stamps carry no UTC offset (``[2026-08-26T11:03:36.298260]``) and
    ``Health._parse_log_date`` keeps them as written, so we compare against DUT wall
    clock. A non-UTC DUT is logged (both sides still share one domain).
    """
    raw = _read_marked_terminal_value(
        engine,
        "date +%Y-%m-%dT%H:%M:%S%z",
        "__NGTS_DUT_NOW",
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4}",
    )
    assert raw is not None
    dut_now = datetime.strptime(raw[:19], "%Y-%m-%dT%H:%M:%S")
    offset = raw[19:]
    if offset != "+0000":
        logger.warning("DUT clock offset is %s (not UTC); history boundary uses DUT wall clock", offset)
    since = dut_now - timedelta(seconds=slack_sec)
    logger.info("health-history boundary from DUT clock: %s (offset %s, slack %ss)", since, offset, slack_sec)
    return since


def assert_sysfs_fault_health_not_silently_ok(health_output: dict, sensor_name: str) -> None:
    """Core #4 sysfs-fault invariant: faulted sensor must not leave system health silently OK.

    Acceptable outcomes, logged explicitly:
      - sensor present in ``issues`` (any issue value)
      - overall ``status`` is not OK
    Fail only when status is OK and the faulted sensor has no health issue entry.
    """
    status = health_output.get(HealthConsts.STATUS)
    led = health_output.get(HealthConsts.STATUS_LED)
    issues = health_output.get(HealthConsts.ISSUES, {}) or {}
    issue_entry = issues.get(sensor_name)
    logger.info(
        "health %s: status=%r status-led=%r issue=%r issues_keys=%s",
        sensor_name, status, led, issue_entry, list(issues),
    )
    if sensor_name in issues:
        logger.info("health %s: outcome=IN_ISSUES (acceptable)", sensor_name)
        return
    assert status != HealthConsts.OK, (
        f"Health silently {HealthConsts.OK!r} with no issue for faulted sensor {sensor_name!r}; "
        f"issues={list(issues)}"
    )
    logger.info("health %s: outcome=STATUS_NOT_OK (acceptable, sensor not in issues)", sensor_name)


def wait_for_nvue_leakage_states(
    nv_command,
    sensor_names: Sequence[str],
    expected_state: str,
    timeout: int = LEAKAGE_SURFACE_SETTLE_TIMEOUT,
    interval: int = LEAKAGE_SURFACE_POLL_INTERVAL,
) -> dict:
    """Poll NVUE leakage state until all sensors show ``expected_state``.

    Default budget = surface SETTLE (state just changed). Callers pass
    ``LEAKAGE_SURFACE_HOLD_TIMEOUT`` when the value is expected to already hold and
    ``LEAKAGE_SURFACE_REBOOT_TIMEOUT`` after a reboot.
    """
    deadline = time.time() + timeout
    last_output = {}
    last_err = None
    while time.time() < deadline:
        try:
            last_output = read_nvue_leakage(nv_command)
            assert_leakage_nvue_rest_states(last_output, sensor_names, expected_state)
            return last_output
        except LEAKAGE_FATAL_READ_ERRORS:
            raise
        except Exception as err:  # noqa: BLE001 - see LEAKAGE_FATAL_READ_ERRORS
            # Not only AssertionError: the NVUE read itself can fail on the transport in
            # the seconds after an injection or a clear, and that used to end the test
            # instead of spending the propagation window this wait exists to provide.
            last_err = err
            time.sleep(interval)
    raise AssertionError(
        f"NVUE leakage did not reach {expected_state!r} within {timeout}s: " +
        (f"last error {type(last_err).__name__}: {last_err}" if last_err is not None
         else "no read completed")
    )


def read_bmc_hwmgmt_leakage_file(bmc_engine: LinuxSshEngine, sensor_name: str) -> str:
    """Read one BMC hw-management sysfs leakage file. Returns '1' (OK) or '0' (leak).

    ``sensor_name`` may be a numbered leaf (leakage1, …) or ``leakage_aggr``.

    Uses a short SSH + remote ``timeout`` so a blocking post-reboot sysfs read cannot
    stall the suite for the default 10000s ``run_cmd`` timeout.
    """
    leaf = _bmc_hwmgmt_leakage_leaf(sensor_name)
    path = f"{BMC_HWMGMT_LEAKAGE_PATH}/{leaf}"
    # Remote timeout kills a blocked cat; SSH read_timeout is the hard backstop.
    # validate=False: missing/empty leaf during settle is handled by callers.
    return bmc_engine.run_cmd(
        f"timeout {BMC_HWMGMT_REMOTE_READ_TIMEOUT} cat {shlex.quote(path)} 2>/dev/null",
        validate=False,
        timeout=BMC_HWMGMT_READ_TIMEOUT,
        retry_run=False,
    ).strip()


def assert_bmc_hwmgmt_leakage_values(
    bmc_engine: LinuxSshEngine,
    sensor_names: Sequence[str],
    expected_value: str,
) -> None:
    """Assert BMC hw-management sysfs leakage file values. expected_value: '1'=OK, '0'=leak."""
    for name in sensor_names:
        val = read_bmc_hwmgmt_leakage_file(bmc_engine, name)
        assert val == expected_value, (
            f"BMC sysfs {BMC_HWMGMT_LEAKAGE_PATH}/{name}: expected {expected_value!r}, got {val!r}"
        )


def _describe_bmc_hwmgmt_dir(bmc_engine: LinuxSshEngine) -> str:
    """One-line verdict on the leakage sysfs directory, for failure messages only.

    ``read_bmc_hwmgmt_leakage_file`` returns ``''`` both for "this firmware has no such
    path" and for "the leaf is not populated yet", deliberately — an empty leaf during
    settle is normal and is what the loop below retries around. That makes the two
    indistinguishable in a settle timeout, so this names which one was hit rather than
    leaving a path/firmware mismatch looking like a slow settle.

    Probed only after the settle loop has already failed, and never allowed to replace
    that failure: the engine may itself be half-open at this point, so every error is
    turned into text. Marker read for the same reason as elsewhere in this module — the
    echoed command must not be mistaken for the reply.
    """
    probe = f"test -d {shlex.quote(BMC_HWMGMT_LEAKAGE_PATH)}; echo __NGTS_BMC_DIR:$?"
    try:
        raw = bmc_engine.run_cmd(
            probe,
            validate=False,
            timeout=BMC_HWMGMT_READ_TIMEOUT,
            retry_run=False,
        )
    except Exception as err:
        return (
            f"{BMC_HWMGMT_LEAKAGE_PATH} existence unknown — probe failed "
            f"({type(err).__name__}: {err})"
        )
    matches = [
        re.fullmatch(r"__NGTS_BMC_DIR:(\d+)", line.strip())
        for line in str(raw).splitlines()
    ]
    codes = [match.group(1) for match in matches if match]
    if len(codes) != 1:
        return f"{BMC_HWMGMT_LEAKAGE_PATH} existence unknown — probe output {str(raw)!r}"
    if codes[0] != "0":
        return (
            f"{BMC_HWMGMT_LEAKAGE_PATH} does not exist on this BMC — the leaves were "
            f"never there, so this is a path/firmware mismatch, not a slow settle"
        )
    return (
        f"{BMC_HWMGMT_LEAKAGE_PATH} exists — the path is right and the values never "
        f"converged"
    )


def wait_for_bmc_hwmgmt_leakage_values(
    bmc_engine: LinuxSshEngine,
    sensor_names: Sequence[str],
    expected_value: str,
    timeout: int = BMC_HWMGMT_SETTLE_TIMEOUT,
    interval: float = BMC_HWMGMT_SETTLE_INTERVAL,
    oob_ip: Optional[str] = None,
) -> LinuxSshEngine:
    """Retry BMC sysfs leakage reads until values match; return the live BMC engine.

    Needed after NOS reboot (soft/pwr_cycle/aux_pwr_cycle) and after BMC-only reboot: OOB SSH
    can return while BMC hw-mgmt leakage sysfs is still empty (``''``), or with a half-open
    SSH session where ``cat`` hangs. When ``oob_ip`` is set, SSH/read failures trigger a
    fresh ``open_bmc_oob_shell`` so settle can continue on a new session.
    """
    deadline = time.time() + timeout
    last_err: Optional[Exception] = None
    engine = bmc_engine
    while time.time() < deadline:
        try:
            assert_bmc_hwmgmt_leakage_values(engine, sensor_names, expected_value)
            return engine
        except AssertionError as err:
            last_err = err
            logger.warning("BMC sysfs settle retry: %s", err)
        except Exception as err:
            last_err = err
            logger.warning(
                "BMC sysfs read failed (%s: %s); will retry%s",
                type(err).__name__,
                err,
                f" with reconnect to {oob_ip}" if oob_ip else "",
            )
            if oob_ip:
                close_bmc_oob_shell(engine)
                try:
                    engine = open_bmc_oob_shell(oob_ip, tries=1)
                except Exception as reconnect_err:
                    logger.warning(
                        "BMC OOB reconnect failed: %s: %s",
                        type(reconnect_err).__name__,
                        reconnect_err,
                    )
        time.sleep(interval)
    raise AssertionError(
        f"BMC sysfs leakage did not reach {expected_value!r} within {timeout}s: "
        f"{last_err} [{_describe_bmc_hwmgmt_dir(engine)}]"
    )


def run_cl_support(engine: LinuxSshEngine) -> str:
    return engine.run_cmd("sudo cl-support", timeout=CL_SUPPORT_TIMEOUT, validate=False)


def extract_cl_support_archive_path(cl_support_output: str) -> str:
    # Charset instead of \S+: the path reaches shell commands, so nothing that could be
    # reinterpreted there may match. Real names are cl_support_<host>_<date>_<time>.txz
    # (e.g. cl_support_cumulus_20260812_142404.txz), so hyphenated hostnames still pass;
    # anything else fails loudly below rather than being handed to a shell.
    for line in cl_support_output.splitlines():
        match = re.search(r"(/var/support/[A-Za-z0-9._-]+\.txz)", line)
        if match:
            return match.group(1)
    # stdout can be long; keep only the tail in the error to avoid flooding pytest logs
    tail = "\n".join(cl_support_output.splitlines()[-30:])
    raise AssertionError(f"cl-support archive path not found in output (last 30 lines):\n{tail}")


def save_cl_support_archive_aside(
    engine: LinuxSshEngine,
    remote_archive_path: str,
    sensor_name: str,
    dest_dir: str,
) -> str:
    """SCP the DUT ``.txz`` to ``dest_dir`` for local evidence reads; return the path.

    ``dest_dir`` must be an ephemeral run dir (e.g. pytest ``tmp_path``) — do **not**
    park archives under the git checkout. Caller should delete the local copy in
    ``finally``. Filename stamp is second-resolution with year to avoid collisions.
    """
    os.makedirs(dest_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    local_name = f"leakage_cl_support_{stamp}_{sensor_name}.txz"
    local_path = os.path.join(dest_dir, local_name)
    if os.path.exists(local_path):
        # Same-second collision (rare); keep uniqueness without writing into the repo.
        local_path = os.path.join(
            dest_dir, f"leakage_cl_support_{stamp}_{sensor_name}_{os.getpid()}.txz"
        )
    remote_dir, remote_base = os.path.split(remote_archive_path)
    engine.copy_file(
        file_system=remote_dir,
        source_file=remote_base,
        dest_file=local_path,
        direction="get",
    )
    logger.info(
        "Saved cl-support archive aside: remote=%s -> local=%s",
        remote_archive_path, local_path,
    )
    return local_path


def _cl_support_find_member(
    listing: Union[str, Sequence[str]], suffix: str
) -> Optional[str]:
    """Return the first ``tar`` member path ending with ``suffix`` (e.g. ``/Support/smonctl``)."""
    lines = listing.splitlines() if isinstance(listing, str) else listing
    for raw_entry in lines:
        member = raw_entry.strip()
        if member.endswith(suffix):
            return member
    return None


def _cl_support_read_member(engine: LinuxSshEngine, archive_path: str, member: str) -> str:
    return engine.run_cmd(
        f"tar xOf {shlex.quote(archive_path)} {shlex.quote(member)}",
        validate=False,
    )


def _cl_support_read_gz_member(
    engine: LinuxSshEngine, archive_path: str, member: str
) -> str:
    """Extract and gunzip a ``.gz`` archive member on the DUT (stdout text).

    Prefer ``_cl_support_*_local`` for evidence asserts — DUT wall broadcasts (e.g. ZTP)
    can replace SSH stdout and yield a tiny non-JSON blob.
    """
    return engine.run_cmd(
        f"tar xOf {shlex.quote(archive_path)} {shlex.quote(member)} | gzip -dc",
        validate=False,
    )


def _cl_support_list_members_local(archive_path: str) -> List[str]:
    with tarfile.open(archive_path, "r:*") as tf:
        return [m.name for m in tf.getmembers()]


def _cl_support_read_member_local(archive_path: str, member: str) -> str:
    with tarfile.open(archive_path, "r:*") as tf:
        fobj = tf.extractfile(member)
        if fobj is None:
            raise AssertionError(f"cl-support member not readable: {member}")
        return fobj.read().decode("utf-8", errors="replace")


def _cl_support_read_gz_member_local(archive_path: str, member: str) -> str:
    """Extract and gunzip a ``.gz`` member from a local ``.txz`` (no SSH).

    Decompression is bounded: ``_bmc_dump_has_live_leak`` needs the whole dump in one
    buffer (``Id`` / ``DetectorState`` must stay co-located), so instead of streaming we
    cap the size and fail loudly on an implausible bundle. Measured baseline on
    mlx-6600: ~1.2 MB gunzipped out of a 29 MB ``.txz``.
    """
    with tarfile.open(archive_path, "r:*") as tf:
        fobj = tf.extractfile(member)
        if fobj is None:
            raise AssertionError(f"cl-support member not readable: {member}")
        raw = fobj.read()
    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as gz:
        data = gz.read(CL_SUPPORT_GZ_MEMBER_MAX_BYTES + 1)
    assert len(data) <= CL_SUPPORT_GZ_MEMBER_MAX_BYTES, (
        f"cl-support member {member} gunzips beyond "
        f"{CL_SUPPORT_GZ_MEMBER_MAX_BYTES / 1e6:.0f} MB — refusing to load "
        f"(baseline ~1.2 MB); archive looks wrong"
    )
    logger.info("cl-support %s gunzipped: %.2f MB", member, len(data) / 1e6)
    return data.decode("utf-8", errors="replace")


def _bmc_dump_live_leak_from_json(dump_text: str, sensor_name: str) -> Optional[bool]:
    """Is ``sensor_name`` reporting ``DetectorState: Critical`` in its own JSON object?

    Tri-state on purpose: ``None`` means the dump did not parse as JSON, which is a different
    answer from "parsed, and this sensor is not critical" and must not be read as a negative.
    """
    try:
        doc = json.loads(dump_text)
    except ValueError:
        return None
    # Iterative rather than recursive: the complete dump is an aggregate of every Redfish
    # resource on the BMC and nesting depth is not something this code should bet on.
    stack = [doc]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if node.get("Id") == sensor_name and node.get("DetectorState") == "Critical":
                return True
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return False


def _bmc_dump_live_leak_from_text(dump_text: str, sensor_name: str) -> bool:
    """Fallback for a dump that will not parse — e.g. truncated by a collection failure.

    Keeps the two fields in one object by refusing to cross another ``"Id"`` key, since each
    detector object carries exactly one. That is weaker than parsing but strictly better than
    a proximity window, which cannot tell a sibling's state from this sensor's.
    """
    logger.warning(
        "BMC dump did not parse as JSON; falling back to text scan for %s", sensor_name
    )
    sensor = re.escape(sensor_name)
    between = r'(?:(?!"Id"\s*:).){0,4000}?'
    return bool(
        re.search(
            rf'"Id"\s*:\s*"{sensor}"{between}"DetectorState"\s*:\s*"Critical"'
            rf'|"DetectorState"\s*:\s*"Critical"{between}"Id"\s*:\s*"{sensor}"',
            dump_text,
            flags=re.DOTALL,
        )
    )


def _bmc_dump_has_live_leak(dump_text: str, sensor_name: str) -> Optional[str]:
    """Return a hit reason if the BMC complete dump shows an active leak for ``sensor_name``.

    Prefers live Redfish ``DetectorState: Critical`` for the sensor Id. Falls back to the
    liquid_cooling event message ``Leak detected at <sensor>``.

    Note: ``Support/bmc_summary.txt`` is only a thin collection header (often ``Health: OK``)
    and does *not* contain the leak string — that line appears in cl-support stdout next to
    "Created summary…" but is not written into the summary file.

    The live check reads the dump as JSON and requires ``Id`` and ``DetectorState`` to belong
    to the *same* object, because this is the assertion that proves the BMC recorded the leak
    we injected rather than some other sensor's state. Text scanning cannot establish that: a
    ``LeakDetector`` object is only a few hundred characters, so any proximity window spans
    its neighbours, and one detector's ``Critical`` reads as another's.
    """
    live = _bmc_dump_live_leak_from_json(dump_text, sensor_name)
    if live is None:
        live = _bmc_dump_live_leak_from_text(dump_text, sensor_name)
    if live:
        return f"DetectorState=Critical for {sensor_name}"
    if re.search(
        rf"Leak detected at\s+{re.escape(sensor_name)}\b",
        dump_text,
        flags=re.IGNORECASE,
    ):
        return f"Leak detected at {sensor_name}"
    return None


def assert_cl_support_leak_evidence(
    engine: Optional[LinuxSshEngine],
    archive_path: str,
    sensor_name: str,
    *,
    local_archive_path: Optional[str] = None,
) -> List[str]:
    """Assert cl-support documents the active leak in both NOS and BMC snapshots.

    HW-confirmed (mlx-6600-04, 2026-07-29):
      - ``Support/smonctl`` — ``LEAK DETECTED`` + sensor name (live NOS view)
      - ``Support/bmc_complete_dump.json.gz`` — prefer live ``DetectorState: Critical`` for
        the sensor Id (this-run proof). Fallback ``Leak detected at <sensor>`` may match
        historical event text in the dump, not only this run — treat as weaker residual.
        Do *not* use ``bmc_summary.txt`` — it is stats-only and does not carry the leak string.

    Both are required: different sources of truth inside the same bundle.
    Member-name greps for ``leak*`` are intentionally *not* used (false positives:
    yang models, BGP ``route_leak``).

    Prefer ``local_archive_path`` (the aside ``.txz`` already SCP'd off the DUT). Reading
    the multi‑MB BMC dump over SSH stdout is race-prone: wall broadcasts (e.g. ZTP
    "Skipping DHCP refresh…") can replace command output and false-fail SN 8
    (pass 2, 2026-08-05: dump_len=516 == broadcast text). Local ``tarfile``/`gzip`
    avoids that class of pollution entirely.
    """
    use_local = bool(local_archive_path and os.path.isfile(local_archive_path))
    if use_local:
        src = local_archive_path
        listing = _cl_support_list_members_local(src)

        def read_plain(member):
            return _cl_support_read_member_local(src, member)

        def read_gz(member):
            return _cl_support_read_gz_member_local(src, member)

        logger.info("cl-support evidence assert from local archive: %s", src)
    else:
        if engine is None:
            raise AssertionError(
                "assert_cl_support_leak_evidence requires engine when "
                "local_archive_path is missing or not a file"
            )
        src = archive_path
        listing = engine.run_cmd(f"tar tf {shlex.quote(src)}", validate=False)

        def read_plain(member):
            return _cl_support_read_member(engine, src, member)

        def read_gz(member):
            return _cl_support_read_gz_member(engine, src, member)

        logger.warning(
            "cl-support evidence assert via DUT SSH (%s) — prefer local_archive_path "
            "to avoid wall-broadcast stdout pollution",
            src,
        )

    errors: List[str] = []
    hits: List[str] = []

    smonctl_member = _cl_support_find_member(listing, "/Support/smonctl")
    if smonctl_member is None:
        errors.append("archive missing member .../Support/smonctl")
    else:
        smonctl_text = read_plain(smonctl_member)
        smonctl_ok = (
            "LEAK DETECTED" in smonctl_text and
            sensor_name in smonctl_text
        )
        if smonctl_ok:
            hits.append(f"{smonctl_member}: LEAK DETECTED + {sensor_name}")
            logger.info("cl-support smonctl evidence OK: %s", hits[-1])
        else:
            errors.append(
                f"{smonctl_member}: expected 'LEAK DETECTED' and {sensor_name!r}; "
                f"snippet={smonctl_text[:500]!r}"
            )

    bmc_member = _cl_support_find_member(listing, "/Support/bmc_complete_dump.json.gz")
    if bmc_member is None:
        # Uncompressed dump (older / alternate cl-support layouts).
        bmc_member = _cl_support_find_member(listing, "/Support/bmc_complete_dump.json")
    if bmc_member is None:
        errors.append(
            "archive missing member .../Support/bmc_complete_dump.json(.gz)"
        )
    else:
        if bmc_member.endswith(".gz"):
            dump_text = read_gz(bmc_member)
        else:
            dump_text = read_plain(bmc_member)
        reason = _bmc_dump_has_live_leak(dump_text, sensor_name)
        if reason:
            hits.append(f"{bmc_member}: {reason}")
            logger.info(
                "cl-support BMC dump evidence OK: %s (dump_len=%d)",
                hits[-1],
                len(dump_text),
            )
        else:
            errors.append(
                f"{bmc_member}: expected DetectorState=Critical or "
                f"'Leak detected at {sensor_name}' for active BMC leak; "
                f"dump_len={len(dump_text)}; snippet={dump_text[:200]!r}"
            )

    if errors:
        raise AssertionError(
            "cl-support leak evidence incomplete "
            "(need both Support/smonctl and Support/bmc_complete_dump):\n- " +
            "\n- ".join(errors)
        )
    return hits


def cleanup_cl_support_archive(engine: LinuxSshEngine, archive_path: str) -> None:
    """Delete the cl-support ``.txz`` from the DUT (frees switch disk). Local aside copy is kept."""
    engine.run_cmd(f"rm -f {shlex.quote(archive_path)}", validate=False)


# =============================================================================
# gNMI helpers
# =============================================================================

def gnmic_available_on(engine=None) -> bool:
    """True if ``gnmic`` is on PATH on that host. ``engine=None`` means the pytest host.

    Probes through ``GnmiClient.cmd_runner`` rather than a bare subprocess so the check
    lands on the same host the client would later use (``CmdRunner`` locally,
    ``EngineCmdRunner`` remotely).
    """
    probe = GnmiClient("", "", "", "", cmd_time=GNMI_PLAYER_PROBE_TIMEOUT, engine=engine)
    out, err, _ = probe.cmd_runner.run_cmd_in_process("which gnmic")
    return bool(out.strip()) and "not found" not in out and "not found" not in err


def _candidate_gnmi_player_hosts(engines) -> List[Tuple[str, object]]:
    """Hosts that may carry gnmic, as ``(description, engine_or_None)``, local first.

    Local first deliberately. ON_CHANGE can only run on the pytest host (see
    :func:`open_gnmi_on_change_subscription`), so whenever gnmic is local every gNMI
    surface stays on one host; a remote player is a fallback for topologies that put the
    tooling on sonic-mgmt, not a preference.
    """
    candidates: List[Tuple[str, object]] = [("pytest host", None)]
    for attr in ("sonic_mgmt", "server", "oob_mgmt_server"):
        engine = getattr(engines, attr, None)
        if engine is not None:
            candidates.append((f"engines.{attr} ({engine.ip})", engine))
    return candidates


def resolve_gnmi_player_engine(engines) -> Optional[Tuple[object, str]]:
    """Pick the host that will run gnmic: ``(engine_or_None, description)``, or None.

    ``engine_or_None`` is None for the pytest host, which is exactly what ``GnmiClient``
    wants in order to keep its default local runner. None instead of a tuple means no
    candidate host has gnmic at all.

    Mirrors :func:`resolve_otel_collector_engine`: run the tool wherever the testbed
    actually installed it instead of assuming one host. CL BSP topologies attach only
    DUT (+ BMC) and resolve to the pytest host; setups that keep tooling on sonic-mgmt
    resolve there.
    """
    probed = []
    for description, engine in _candidate_gnmi_player_hosts(engines):
        probed.append(description)
        if gnmic_available_on(engine):
            logger.info("gnmic player host: %s", description)
            return engine, description
    logger.warning("gnmic not found on any candidate host: %s", ", ".join(probed))
    return None


def create_leakage_gnmi_client(engines) -> Optional[GnmiClient]:
    """Return a GnmiClient aimed at the DUT gNMI server, or None if gnmic is unavailable.

    gnmic runs on the *test player* — the pytest host, or the sonic-mgmt machine when that
    is where the testbed installed it — never on the DUT. Either way it opens a network
    connection to ``engines.dut.ip:GNMI_PORT``.
    """
    resolved = resolve_gnmi_player_engine(engines)
    if resolved is None:
        logger.warning("%s", GNMI_SETUP_ERR_GNMIC_MISSING)
        return None
    engine, description = resolved
    logger.info("Leakage gNMI client runs gnmic on %s", description)
    return GnmiClient(
        server_host=engines.dut.ip,
        server_port=GNMI_PORT,
        username=engines.dut.username,
        password=engines.dut.password,
        cmd_time=30,
        verify_tools_installed=False,
        engine=engine,
    )


_GNMI_LOCALHOST_BINDS = frozenset({"localhost", "127.0.0.1", "::1"})
# Keep 0.0.0.0: HW (manual + suite) only accepted remote gnmic with this bind on CL.
# Do not switch to dut.ip alone without re-validating on Salamandra.
GNMI_REMOTE_LISTEN_ADDRESS = "0.0.0.0"


@dataclass
class GnmiListenRestoreState:
    """Tracks what ensure_gnmi_server_ready changed so teardown can put all of it back."""

    widened: bool = False
    previous_addrs: frozenset = field(default_factory=frozenset)
    # The DUT ships gnmi-server disabled, so enabling it is a change like any other and has
    # to be reversed here. NGTS per-test config restore happens to unset the whole system
    # subtree as well, but a fixture that depends on that leaves the service running on any
    # switch whose cleanup stops covering it, and no test asserts otherwise.
    was_disabled: bool = False


def _gnmi_listening_addresses(show: dict) -> set:
    addrs = show.get("listening-address") or {}
    if isinstance(addrs, dict):
        return set(addrs.keys())
    if isinstance(addrs, str):
        return {addrs}
    return set()


def _gnmi_has_remote_listen_address(show: dict) -> bool:
    """True if gnmi-server is configured to accept non-loopback clients."""
    addrs = _gnmi_listening_addresses(show)
    return any(a not in _GNMI_LOCALHOST_BINDS for a in addrs)


def _dut_gnmi_port_is_remotely_listening(engine: LinuxSshEngine, port: int = GNMI_PORT) -> bool:
    """True if DUT listens on ``port`` on a non-loopback address (reachable by gnmic).

    Loopback-only ``127.0.0.1:9339`` is *not* enough — gnmic runs on the test player
    and dials ``dut.ip:9339``.
    """
    out = engine.run_cmd(
        f"ss -lnt 2>/dev/null | grep -E ':{port}\\s' || true",
        validate=False,
    )
    if not out or not out.strip():
        return False
    for line in out.splitlines():
        # ss columns: ... Local Address:Port ...
        # Accept *:9339, 0.0.0.0:9339, [::]:9339, or any non-127/::1 bind.
        if f":{port}" not in line:
            continue
        if "127.0.0.1:" in line or "[::1]:" in line:
            continue
        return True
    return False


def wait_for_gnmi_port_listening(
    engine: LinuxSshEngine,
    port: int = GNMI_PORT,
    timeout: int = GNMI_PORT_READY_TIMEOUT,
    interval: int = GNMI_PORT_POLL_INTERVAL,
) -> None:
    """Poll until the DUT listens on the gNMI port for remote clients (or raise)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _dut_gnmi_port_is_remotely_listening(engine, port):
            logger.info("DUT gNMI port %s is listening for remote clients", port)
            return
        logger.info(
            "Waiting for DUT gNMI port %s remote bind (%.0fs left)",
            port, max(0, deadline - time.time()),
        )
        time.sleep(interval)
    raise AssertionError(
        f"DUT gNMI port {port} not remotely reachable within {timeout}s after enable — "
        f"check 'nv show system gnmi-server' listening-address (must not be localhost-only) "
        f"and docker/nv-gnmi on the switch"
    )


def ensure_gnmi_server_ready(
    nv_command, engines, timeout: int = GNMI_PORT_READY_TIMEOUT
) -> GnmiListenRestoreState:
    """Enable DUT gnmi-server for remote gnmic and wait until ``:GNMI_PORT`` accepts connections.

    Idempotent. When the server is localhost-only, adds ``listening-address 0.0.0.0``
    (required for remote gnmic on CL HW — do not replace with dut.ip without re-check).
    Returns restore state for :func:`restore_gnmi_server_state` (fixture teardown).
    """
    restore = GnmiListenRestoreState()
    with allure.step("Ensure gNMI server enabled and listening on DUT"):
        show = nv_command.system.gnmi_server.parsed_show_gnmi()
        state = show.get(GnmiConsts.GNMI_STATE_FIELD)
        logger.info("gNMI server show: %s", show)
        restore.previous_addrs = frozenset(_gnmi_listening_addresses(show))

        # ask_for_confirmation='-y': telemetry/OTLP applies warn that prometheus /
        # nv-telemetry will restart; without -y NVUE prompts and aborts (Core4/SN7
        # ERROR 2026-08-03: "Aborted apply after warnings" / Are you sure? [y/N]).
        if state != GnmiConsts.GNMI_STATE_ENABLED:
            logger.info("gNMI server state=%r — enabling via NVUE", state)
            restore.was_disabled = True
            nv_command.system.gnmi_server.set(
                GnmiConsts.GNMI_STATE_FIELD,
                GnmiConsts.GNMI_STATE_ENABLED,
                apply=True,
                ask_for_confirmation="-y",
            ).verify_result()
            show = nv_command.system.gnmi_server.parsed_show_gnmi()
            restore.previous_addrs = frozenset(_gnmi_listening_addresses(show))
        else:
            logger.info("gNMI server already enabled")

        if not _gnmi_has_remote_listen_address(show):
            logger.info(
                "gNMI listening-address=%s is localhost-only — setting %s for remote gnmic "
                "(will restore previous bind in teardown)",
                sorted(restore.previous_addrs),
                GNMI_REMOTE_LISTEN_ADDRESS,
            )
            nv_command.system.gnmi_server.set(
                "listening-address",
                GNMI_REMOTE_LISTEN_ADDRESS,
                apply=True,
                ask_for_confirmation="-y",
            ).verify_result()
            restore.widened = True

        if _dut_gnmi_port_is_remotely_listening(engines.dut):
            logger.info("DUT gNMI port %s already remotely listening", GNMI_PORT)
            return restore
        wait_for_gnmi_port_listening(engines.dut, timeout=timeout)
    return restore


def restore_gnmi_server_state(
    nv_command, restore: Optional[GnmiListenRestoreState]
) -> None:
    """Undo everything :func:`ensure_gnmi_server_ready` changed: the remote widen and the enable.

    Best-effort: logs and returns on failure so teardown never masks the test result. The two
    concerns are restored under separate handlers so a failed address restore still leaves the
    server disabled rather than running.
    """
    if not restore or not (restore.widened or restore.was_disabled):
        return
    with allure.step("Restore gNMI server state changed by this test"):
        if restore.widened:
            try:
                nv_command.system.gnmi_server.unset(
                    f"listening-address {GNMI_REMOTE_LISTEN_ADDRESS}",
                    apply=True,
                    ask_for_confirmation="-y",
                ).verify_result()
                # If unset dropped every address, put the pre-widen binds back.
                show = nv_command.system.gnmi_server.parsed_show_gnmi()
                current = _gnmi_listening_addresses(show)
                for addr in sorted(restore.previous_addrs):
                    if addr == GNMI_REMOTE_LISTEN_ADDRESS or addr in current:
                        continue
                    logger.info("Re-applying pre-widen gNMI listening-address %s", addr)
                    nv_command.system.gnmi_server.set(
                        "listening-address",
                        addr,
                        apply=True,
                        ask_for_confirmation="-y",
                    ).verify_result()
                logger.info(
                    "gNMI listening-address after restore: %s",
                    sorted(_gnmi_listening_addresses(
                        nv_command.system.gnmi_server.parsed_show_gnmi()
                    )),
                )
            except Exception as err:
                logger.warning(
                    "gNMI listening-address restore failed (non-fatal): %s", err
                )

        if restore.was_disabled:
            try:
                logger.info("Disabling gNMI server — it was disabled before this test")
                nv_command.system.gnmi_server.set(
                    GnmiConsts.GNMI_STATE_FIELD,
                    GnmiConsts.GNMI_STATE_DISABLED,
                    apply=True,
                    ask_for_confirmation="-y",
                ).verify_result()
            except Exception as err:
                logger.warning(
                    "gNMI server disable failed (non-fatal); the DUT may be left with "
                    "gnmi-server enabled: %s", err
                )


def read_gnmi_leakage_alarm_status(client: GnmiClient, sensor_name: str) -> Optional[str]:
    """Read alarm-status for one leakage sensor via gNMI ONCE subscription.

    Returns GNMI_ALARM_STATUS_OK ('false'), GNMI_ALARM_STATUS_LEAK ('true'), or None.
    """
    out, err, _, _ = client.gnmic_subscribe(
        prefix=GNMI_LEAKAGE_COMPONENT_PREFIX.format(sensor_name),
        path=GNMI_LEAKAGE_ALARM_STATUS_SUBPATH,
        mode=GnmiMode.ONCE,
        flat=True,
        skip_cert_verify=True,
    )
    for line in out.splitlines():
        if "alarm-status" in line and ":" in line:
            return line.split(":", 1)[-1].strip().lower()
    logger.warning("gNMI alarm-status not found for %s. out=%s err=%s", sensor_name, out[:300], err[:200])
    return None


def assert_gnmi_leakage_statuses(
    client: GnmiClient,
    sensor_names: Sequence[str],
    expected_status: str,
) -> None:
    """Assert gNMI alarm-status for each sensor in sensor_names.

    expected_status: GNMI_ALARM_STATUS_OK ('false') or GNMI_ALARM_STATUS_LEAK ('true').
    """
    for name in sensor_names:
        deadline = time.time() + SERVICE_UP_TIMEOUT
        while time.time() < deadline:
            val = read_gnmi_leakage_alarm_status(client, name)
            if val is None:
                time.sleep(SERVICE_UP_POLL_INTERVAL)
                continue
            assert val == expected_status, (
                f"gNMI {name}: alarm-status expected {expected_status!r}, got {val!r}"
            )
            break
        else:
            raise AssertionError(
                f"gNMI: alarm-status not found for {name!r} within "
                f"{SERVICE_UP_TIMEOUT}s — confirm component name and xpath on this platform"
            )


def read_gnmi_leakage_oper_status(client: GnmiClient, sensor_name: str) -> Optional[str]:
    """Read oper-status for one leakage sensor via gNMI ONCE subscription.

    Returns GNMI_OPER_STATUS_ACTIVE, GNMI_OPER_STATUS_INACTIVE, or None.
    Retries on empty output / Unavailable / EOF (gNMI blips mid-test, HW 2026-08-03).
    """
    last_err = ""
    for attempt in range(1, GNMI_OPER_STATUS_RETRIES + 1):
        out, err, _, _ = client.gnmic_subscribe(
            prefix=GNMI_LEAKAGE_COMPONENT_PREFIX.format(sensor_name),
            path=GNMI_LEAKAGE_OPER_STATUS_SUBPATH,
            mode=GnmiMode.ONCE,
            flat=True,
            skip_cert_verify=True,
        )
        last_err = err or ""
        for line in out.splitlines():
            if "oper-status" in line and ":" in line:
                return line.split(":", 1)[-1].strip()
        transient = (
            not out.strip() or
            "Unavailable" in last_err or
            "EOF" in last_err
        )
        if transient and attempt < GNMI_OPER_STATUS_RETRIES:
            logger.warning(
                "gNMI oper-status %s attempt %d/%d empty/EOF — retry in %ds (err=%s)",
                sensor_name, attempt, GNMI_OPER_STATUS_RETRIES,
                GNMI_OPER_STATUS_RETRY_DELAY_SEC, last_err[:200],
            )
            time.sleep(GNMI_OPER_STATUS_RETRY_DELAY_SEC)
            continue
        break
    logger.warning(
        "gNMI oper-status not found for %s after %d attempts. err=%s",
        sensor_name, GNMI_OPER_STATUS_RETRIES, last_err[:200],
    )
    return None


def assert_gnmi_oper_statuses(
    client: GnmiClient,
    sensor_names: Sequence[str],
    expected: str,
) -> None:
    """Assert gNMI oper-status for each sensor in sensor_names."""
    for name in sensor_names:
        val = read_gnmi_leakage_oper_status(client, name)
        assert val is not None, (
            f"gNMI oper-status not found for {name!r}"
        )
        assert val == expected, (
            f"gNMI oper-status {name}: expected {expected!r}, got {val!r}"
        )


def read_gnmi_leakage_component_state(client: GnmiClient, sensor_name: str) -> str:
    """Subscribe to the component state/ subtree via gNMI ONCE (component-level xpath).

    Returns the raw flat output. All leaves under state/ are included:
    alarm-status, oper-status, name, type, etc.
    """
    out, err, _, _ = client.gnmic_subscribe(
        prefix=GNMI_LEAKAGE_COMPONENT_PREFIX.format(sensor_name),
        path=GNMI_LEAKAGE_COMPONENT_STATE_SUBPATH,
        mode=GnmiMode.ONCE,
        flat=True,
        skip_cert_verify=True,
    )
    if not out.strip():
        logger.warning("gNMI component state/ empty for %s. err=%s", sensor_name, err[:200])
    return out


def assert_gnmi_component_alarm_statuses(
    client: GnmiClient,
    sensor_names: Sequence[str],
    expected_status: str,
) -> None:
    """Assert alarm-status via component state/ xpath for each sensor (same shape as exact-leaf assert).

    Verifies leakage is visible when subscribing at the broader component path, not only
    at the exact alarm-status leaf. Read + assert are bundled here — mirrors
    ``assert_gnmi_leakage_statuses``.
    """
    for name in sensor_names:
        deadline = time.time() + SERVICE_UP_TIMEOUT
        last_out = ""
        while time.time() < deadline:
            out = read_gnmi_leakage_component_state(client, name)
            last_out = out
            parsed = next(
                (
                    line.split(":", 1)[-1].strip().lower()
                    for line in out.splitlines()
                    if "alarm-status" in line and ":" in line
                ),
                None,
            )
            if parsed is None:
                time.sleep(SERVICE_UP_POLL_INTERVAL)
                continue
            assert parsed == expected_status, (
                f"gNMI component xpath {name}: alarm-status expected "
                f"{expected_status!r}, got {parsed!r}"
            )
            break
        else:
            raise AssertionError(
                f"gNMI component xpath {name}: alarm-status not found in state/ output "
                f"within {SERVICE_UP_TIMEOUT}s. Output: {last_out[:300]}"
            )


# ── ON_CHANGE assertion helpers ────────────────────────────────────────────
# Design decision (2026-07-28): we assert state transitions, not timing.
# The PNE metric surface check is the implicit gate — once PNE shows the correct
# value (which we assert), UMF has already forwarded the update to gNMI ON_CHANGE
# subscribers. All remaining surface checks after PNE provide further margin.

def _parse_gnmi_alarm_status_values(output: str) -> List[str]:
    """Extract alarm-status leaf values from flat gnmic output, in order of appearance."""
    values = []
    for line in output.splitlines():
        if "alarm-status" in line and ":" in line:
            values.append(line.split(":", 1)[-1].strip().lower())
    return values


def assert_gnmi_on_change_no_leak(output: str, sensor_name: str) -> None:
    """Assert ON_CHANGE stream for Core #1 baseline.

    Expected: initial sync present (false = OK state), no leak notification.
    A SAMPLE subscription would produce repeated 'false' values at fixed intervals.
    ON_CHANGE produces exactly one initial sync — its presence confirms:
    (1) subscription worked, (2) baseline state correctly reported, (3) no spurious leak.
    """
    values = _parse_gnmi_alarm_status_values(output)
    assert values, (
        f"gNMI ON_CHANGE {sensor_name}: no alarm-status leaf in stream — "
        f"subscription may not have received initial state. Output: {output[:300]}"
    )
    assert GNMI_ALARM_STATUS_OK in values, (
        f"gNMI ON_CHANGE {sensor_name}: expected initial sync alarm-status="
        f"{GNMI_ALARM_STATUS_OK!r}, got values={values}"
    )
    assert GNMI_ALARM_STATUS_LEAK not in values, (
        f"gNMI ON_CHANGE {sensor_name}: unexpected alarm-status={GNMI_ALARM_STATUS_LEAK!r} "
        f"during baseline — no injection occurred. values={values}"
    )


def assert_gnmi_on_change_leak_transition(output: str, sensor_name: str) -> None:
    """Assert ON_CHANGE stream for Core #2 / Core #3.

    Expected sequence: false (initial sync) → true (inject) → false (recovery).
    A SAMPLE subscription would produce repeated intermediates between each transition.
    ON_CHANGE produces exactly one notification per state change — the sparse output
    is implicit evidence of event-driven delivery.
    """
    values = _parse_gnmi_alarm_status_values(output)
    assert values, (
        f"gNMI ON_CHANGE {sensor_name}: no alarm-status leaf in stream. "
        f"Output: {output[:300]}"
    )
    assert GNMI_ALARM_STATUS_OK in values, (
        f"gNMI ON_CHANGE {sensor_name}: alarm-status={GNMI_ALARM_STATUS_OK!r} not found — "
        f"initial sync missing or recovery notification not received. values={values}"
    )
    assert GNMI_ALARM_STATUS_LEAK in values, (
        f"gNMI ON_CHANGE {sensor_name}: alarm-status={GNMI_ALARM_STATUS_LEAK!r} not found "
        f"after CPLD injection. values={values}"
    )
    first_ok = values.index(GNMI_ALARM_STATUS_OK)
    first_leak = values.index(GNMI_ALARM_STATUS_LEAK)
    assert first_ok < first_leak, (
        f"gNMI ON_CHANGE {sensor_name}: expected initial {GNMI_ALARM_STATUS_OK!r} before "
        f"{GNMI_ALARM_STATUS_LEAK!r} (ok@{first_ok}, leak@{first_leak}). values={values}"
    )
    last_ok = len(values) - 1 - values[::-1].index(GNMI_ALARM_STATUS_OK)
    assert last_ok > first_leak, (
        f"gNMI ON_CHANGE {sensor_name}: expected recovery {GNMI_ALARM_STATUS_OK!r} after "
        f"{GNMI_ALARM_STATUS_LEAK!r} (last_ok@{last_ok}, leak@{first_leak}). values={values}"
    )


def assert_gnmi_on_change_clear_transition(output: str, sensor_name: str) -> None:
    """Assert ON_CHANGE stream for a subscription opened while the sensor is leaking.

    Expected sequence: true (initial sync) → false (CPLD clear). SN 6 uses this after
    an nv-umf restart: ONCE reads can recover while the event path stays dead, so the
    falling edge must arrive on a subscription opened after the restart.
    """
    values = _parse_gnmi_alarm_status_values(output)
    assert values, (
        f"gNMI ON_CHANGE {sensor_name}: no alarm-status leaf in stream. "
        f"Output: {output[:300]}"
    )
    assert values[0] == GNMI_ALARM_STATUS_LEAK, (
        f"gNMI ON_CHANGE {sensor_name}: expected initial sync alarm-status="
        f"{GNMI_ALARM_STATUS_LEAK!r} (subscribed while leaking), got values={values}"
    )
    assert GNMI_ALARM_STATUS_OK in values[1:], (
        f"gNMI ON_CHANGE {sensor_name}: no {GNMI_ALARM_STATUS_OK!r} notification after "
        f"CPLD clear — event path did not deliver the falling edge. values={values}"
    )


@dataclass
class GnmiOnChangeHandle:
    """Live ON_CHANGE gnmic process with stdout retained for later assertions.

    Bytes drained while waiting for initial sync must not be lost on close — they
    are the baseline ``false`` sample required for Core #2/#3 ordering checks.
    """

    proc: subprocess.Popen
    sensor_name: str
    collected: List[str] = field(default_factory=list)


def open_gnmi_on_change_subscription(
    engines, sensor_name: str
) -> GnmiOnChangeHandle:
    """Start a gNMI ON_CHANGE subscription for one leakage sensor.

    Returns a :class:`GnmiOnChangeHandle`. Close with
    :func:`close_gnmi_on_change_subscription`. Prefer
    :func:`wait_for_gnmi_on_change_initial_status` before injecting so the
    protocol-required current-value sync is observed (not a fixed sleep).

    Pytest-host only, unlike the rest of the gNMI surfaces: the stream is read while
    gnmic is still running (:func:`_drain_gnmi_on_change_stdout` selects on the pipe) and
    ``EngineCmdRunner`` surrenders a remote process's output only at wait/kill. Checked
    here rather than left to fail later, because a remote-only gnmic would otherwise
    surface as an unexplained initial-sync timeout.
    """
    if not gnmic_available_on(None):
        raise AssertionError(GNMI_ON_CHANGE_ERR_LOCAL_GNMIC_REQUIRED)
    builder = (
        GnmicCmdBuilder(host=engines.dut.ip, port=GNMI_PORT)
        .skip_verify()
        .subscribe(
            prefix=GNMI_LEAKAGE_COMPONENT_PREFIX.format(sensor_name),
            path=GNMI_LEAKAGE_ALARM_STATUS_SUBPATH,
            target="nvos",
        )
        .format_flat()
    )
    # Username appended here rather than through user_creds(), which renders the password
    # as a ``-p`` flag too; the password travels in the environment instead (see
    # GNMIC_PASSWORD_ENV). Both land in ``options``, which build() emits ahead of the
    # subcommand, so this is positionally identical to what user_creds() produced.
    builder.options += f" -u {shlex.quote(engines.dut.username)}"
    builder.operation += " --stream-mode on-change"
    cmd = builder.build()
    # Retained even though cmd no longer carries a credential: it is what keeps a future
    # user_creds() call here from silently reintroducing one into the log.
    logged_cmd = _redact_cli_secrets(cmd)
    logger.info("Starting gNMI ON_CHANGE subscription: %s", logged_cmd)
    proc = subprocess.Popen(
        shlex.split(cmd),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
        env={**os.environ, GNMIC_PASSWORD_ENV: engines.dut.password},
    )
    return GnmiOnChangeHandle(proc=proc, sensor_name=sensor_name)


def _drain_gnmi_on_change_stdout(handle: GnmiOnChangeHandle, wait_sec: float = 0.0) -> None:
    """Append any currently available gnmic stdout lines into ``handle.collected``."""
    proc = handle.proc
    if proc.stdout is None:
        return
    waited = False
    while True:
        # First select may block up to wait_sec; subsequent reads are non-blocking.
        timeout = 0.0 if waited else max(0.0, wait_sec)
        ready, _, _ = select.select([proc.stdout], [], [], timeout)
        waited = True
        if not ready:
            break
        line = proc.stdout.readline()
        if not line:
            break
        handle.collected.append(line)


def wait_for_gnmi_on_change_initial_status(
    handle: GnmiOnChangeHandle,
    expected_status: str = GNMI_ALARM_STATUS_OK,
    timeout: int = GNMI_ON_CHANGE_INITIAL_SYNC_TIMEOUT,
) -> None:
    """Block until ON_CHANGE stream shows the leaf value (initial sync), then return.

    gNMI ON_CHANGE (without ``updates_only``) must emit current path values before
    ``sync_response`` — not a sync-only message. Flat gnmic prints that as
    ``...alarm-status: false|true``. Gate inject on seeing ``expected_status``.
    """
    deadline = time.time() + timeout
    with allure.step(
        f"Wait for gNMI ON_CHANGE initial sync alarm-status={expected_status!r} "
        f"({handle.sensor_name})"
    ):
        while time.time() < deadline:
            _drain_gnmi_on_change_stdout(handle, wait_sec=GNMI_ON_CHANGE_DRAIN_POLL)
            values = _parse_gnmi_alarm_status_values("".join(handle.collected))
            if expected_status in values:
                logger.info(
                    "gNMI ON_CHANGE %s: initial sync observed values=%s",
                    handle.sensor_name, values,
                )
                return
            if handle.proc.poll() is not None:
                # Process died — drain whatever remains and fail with context.
                _drain_gnmi_on_change_stdout(handle, wait_sec=0.5)
                break
        snippet = "".join(handle.collected)[-500:]
        raise AssertionError(
            f"gNMI ON_CHANGE {handle.sensor_name}: no initial sync alarm-status="
            f"{expected_status!r} within {timeout}s (got values="
            f"{_parse_gnmi_alarm_status_values(''.join(handle.collected))}). "
            f"Output tail: {snippet!r}"
        )


def close_gnmi_on_change_subscription(handle: GnmiOnChangeHandle) -> str:
    """Terminate an ON_CHANGE subscription and return all accumulated stdout."""
    proc = handle.proc
    proc.terminate()
    try:
        stdout, _ = proc.communicate(timeout=GNMI_ON_CHANGE_CLOSE_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, _ = proc.communicate()
    if stdout:
        handle.collected.append(stdout)
    return "".join(handle.collected)


# =============================================================================
# Service restart helpers
# =============================================================================

def _service_restart_identity(engine: LinuxSshEngine, service_name: str) -> tuple:
    """Return the PID and activation timestamp used to prove a service restart."""
    out = engine.run_cmd(
        f"systemctl show {shlex.quote(service_name)} --property=MainPID "
        "--property=ActiveEnterTimestampMonotonic --value",
        validate=False,
    )
    values = [line.strip() for line in out.splitlines() if re.fullmatch(r"\d+", line.strip())]
    if len(values) != 2:
        raise AssertionError(
            f"Cannot read restart identity for {service_name!r}: {out!r}"
        )
    return tuple(values)


def restart_service(engine: LinuxSshEngine, service_name: str) -> None:
    """Restart a service and prove the unit entered a new active generation."""
    before = _service_restart_identity(engine, service_name)
    logger.info("Restarting service %s (before=%s)", service_name, before)
    raw = engine.run_cmd(
        f"sudo systemctl restart {shlex.quote(service_name)}; printf '\\n__NGTS_RC:%s\\n' \"$?\"",
        validate=False,
    )
    rc_match = re.search(r"__NGTS_RC:(\d+)", raw)
    rc = int(rc_match.group(1)) if rc_match else None
    if rc not in (0, None):
        raise AssertionError(
            f"systemctl restart {service_name!r} failed (rc={rc}): {raw[-500:]!r}"
        )
    if rc is None:
        logger.warning(
            "Restart response for %s did not include a return code; "
            "requiring post-restart generation proof",
            service_name,
        )

    deadline = time.time() + SERVICE_UP_TIMEOUT
    last_identity = None
    while time.time() < deadline:
        active = engine.run_cmd(
            f"systemctl is-active {shlex.quote(service_name)}", validate=False
        ).strip()
        if active == "active":
            last_identity = _service_restart_identity(engine, service_name)
            if last_identity != before:
                logger.info("Service %s restart verified (after=%s)", service_name, last_identity)
                return
        time.sleep(SERVICE_UP_POLL_INTERVAL)
    raise AssertionError(
        f"Service {service_name!r} did not prove a restart within {SERVICE_UP_TIMEOUT}s "
        f"(before={before}, after={last_identity})"
    )


def wait_for_service_active(
    engine: LinuxSshEngine,
    service_name: str,
    timeout: int = SERVICE_UP_TIMEOUT,
    interval: int = SERVICE_UP_POLL_INTERVAL,
) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        out = engine.run_cmd(
            f"systemctl is-active {service_name}", validate=False
        ).strip()
        if out == "active":
            logger.info("Service %s is active", service_name)
            return
        time.sleep(interval)
    raise AssertionError(
        f"Service {service_name!r} did not become active within {timeout}s"
    )


def wait_for_pne_metric_available(
    engine: LinuxSshEngine,
    timeout: int = SERVICE_UP_TIMEOUT,
    interval: int = SERVICE_UP_POLL_INTERVAL,
) -> None:
    """Poll :9100/metrics until the leakage metric reappears after pne restart."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        metrics = read_pne_telemetry_leakage_metrics(engine)
        if TELEMETRY_LEAKAGE_METRIC in metrics:
            return
        time.sleep(interval)
    raise AssertionError(
        f"PNE leakage metric did not reappear at {TELEMETRY_URL} within {timeout}s"
    )


def wait_for_pne_leakage_statuses(
    engine: LinuxSshEngine,
    sensor_names: Sequence[str],
    expected_status: str,
    timeout: int = LEAKAGE_SURFACE_SETTLE_TIMEOUT,
    interval: int = LEAKAGE_SURFACE_POLL_INTERVAL,
) -> None:
    """Poll PNE :9100 until every sensor shows expected_status ('0' leak / '1' OK).

    Used as a post-clear recovery gate before OTEL/gNMI ON_CHANGE close: NVUE can
    already show normal while the scrape→UMF path still lags. Differs from
    ``wait_for_pne_metric_available`` (metric line present only) and from a one-shot
    ``assert_pne_telemetry_leakage_statuses`` (no settle poll).
    """
    deadline = time.time() + timeout
    last_metrics = ""
    last_err = None
    while time.time() < deadline:
        try:
            # The scrape belongs inside the try: it is a bounded SSH read, and one that
            # failed used to end the wait outright. During post-clear recovery - exactly
            # when this gate is used - a single failed scrape is the expected case, not a
            # reason to stop scraping.
            last_metrics = read_pne_telemetry_leakage_metrics(engine)
            assert_pne_telemetry_leakage_statuses(
                last_metrics, sensor_names, expected_status=expected_status
            )
            logger.info(
                "PNE leakage %s = %s confirmed", list(sensor_names), expected_status
            )
            return
        except LEAKAGE_FATAL_READ_ERRORS:
            raise
        except Exception as err:  # noqa: BLE001 - see LEAKAGE_FATAL_READ_ERRORS
            last_err = err
            time.sleep(interval)
    raise AssertionError(
        f"PNE leakage did not reach {expected_status!r} for {list(sensor_names)} "
        f"within {timeout}s" +
        (f" (last error {type(last_err).__name__}: {last_err})"
         if last_err is not None else "") +
        f". Tail:\n{last_metrics[-500:]}"
    )


def wait_for_gnmi_leakage_status(
    client: GnmiClient,
    sensor_name: str,
    expected_status: str,
    timeout: int = LEAKAGE_SURFACE_SETTLE_TIMEOUT,
    interval: int = LEAKAGE_SURFACE_POLL_INTERVAL,
) -> None:
    """Poll gNMI ONCE until alarm-status matches expected (e.g. after nv-umf restart)."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = read_gnmi_leakage_alarm_status(client, sensor_name)
        if last == expected_status:
            return
        time.sleep(interval)
    raise AssertionError(
        f"gNMI {sensor_name}: alarm-status did not reach {expected_status!r} "
        f"within {timeout}s (last: {last!r})"
    )


def wait_for_sysfs_leakage_value(
    engine: LinuxSshEngine,
    sensor_name: str,
    expected_value: str,
    timeout: int = LEAKAGE_SURFACE_SETTLE_TIMEOUT,
    interval: int = LEAKAGE_SURFACE_POLL_INTERVAL,
) -> None:
    """Poll NOS sysfs leakage file until it shows expected_value ('0' or '1')."""
    path = _nos_leakage_sysfs_path(sensor_name)
    qpath = shlex.quote(path)
    deadline = time.time() + timeout
    last_val = None
    while time.time() < deadline:
        last_val = engine.run_cmd(f"cat {qpath}", validate=False).strip()
        if last_val == expected_value:
            return
        time.sleep(interval)
    raise AssertionError(
        f"sysfs {path} did not reach {expected_value!r} within {timeout}s (last: {last_val!r})"
    )


# =============================================================================
# Reboot helpers
# =============================================================================

# LinuxSshEngine.run_cmd default timeout is 10000s. Reboot / power-cycle commands
# drop the SSH session and never return cleanly — without a short timeout the
# caller blocks for hours (observed: SN8 [bmc] hang; [aux_pwr_cycle] ~2.7h wait).
REBOOT_CMD_TIMEOUT = 30
# TCP probe budgets for SSH port polling. The down-probe stays short so a blackholed
# port is classified as down within a single poll instead of stretching the loop; the
# up-probe allows the slower handshake of a host that just finished booting.
SSH_PORT_DOWN_PROBE_TIMEOUT = 3
SSH_PORT_UP_PROBE_TIMEOUT = 5
# Budget for reading the server's SSH identification string. A bare TCP connect only
# proves something is listening; during a BMC reboot the port can accept while sshd is
# not yet serving, and devts turns that into a 400s netmiko connect retried
# engine_connect_retries times, followed by a password-recovery path that opens its own
# 400s session — one such poll outlasts BMC_REBOOT_SSH_TIMEOUT on its own.
SSH_BANNER_PROBE_TIMEOUT = 5
# One trivial command on a possibly half-open post-reboot session (liveness / boot
# identity). Short for the same reason as BMC_HWMGMT_READ_TIMEOUT: the polling loop owns
# the long wait, so a wedged channel must fail fast enough to be retried.
DUT_LIVENESS_CMD_TIMEOUT = 15
# Reading boot_id + uptime right after the BMC returns can hit a half-open session
# (SN7 [bmc] 2026-08-26); retry within this budget, reconnecting between attempts.
# Real proof failures (unchanged boot_id, absurd uptime) fail immediately regardless.
BMC_BOOT_IDENTITY_READ_TIMEOUT = 60
BMC_BOOT_IDENTITY_RETRY_DELAY_SEC = 3.0


def _run_reboot_cmd(engine: LinuxSshEngine, cmd: str, label: str) -> None:
    """Fire a reboot-like command; treat SSH drop / short timeout as success."""
    try:
        engine.run_cmd(cmd, validate=False, timeout=REBOOT_CMD_TIMEOUT, retry_run=False)
        logger.info("%s command returned within %ss", label, REBOOT_CMD_TIMEOUT)
    except Exception as e:
        logger.info(
            "%s SSH dropped / timed out as expected (%s: %s)",
            label, type(e).__name__, e,
        )


def reboot_dut_soft(engine: LinuxSshEngine) -> None:
    """Initiate a soft reboot. SSH session will drop; caller must wait for recovery."""
    logger.info("Initiating soft reboot")
    _run_reboot_cmd(engine, "sudo reboot", "NOS soft reboot")


def reboot_dut_aux_pwr_cycle(bmc_engine: LinuxSshEngine) -> None:
    """Board/AUX power cycle via BMC OOB ``ipmitool chassis power cycle``.

    On Salamandra/sonicBMC this maps to ``powerctrl.sh reset_board()`` →
    ``aux_pwr_cycle`` (full board including BMC), **not** host-only ``pwr_cycle``.
    CPLD leak-injection simulation registers do **not** survive this cycle
    (owner-expected / Guy Poel — use host ``pwr_cycle`` for persist checks).
    """
    logger.info("Initiating BMC board/AUX power cycle (ipmitool chassis power cycle)")
    _run_reboot_cmd(bmc_engine, "ipmitool chassis power cycle", "BMC AUX power cycle")


def reboot_dut_pdb_power_cycle(bmc_engine: LinuxSshEngine) -> None:
    """Host/PDB power cycle via BMC OOB write to ``pwr_cycle`` sysfs.

    Drops internal 12V / power-cycles the CPU only; BMC and CPLD stay powered
    (Guy Poel “PDB power cycle”). CPLD leak-injection simulation is expected to
    **persist**. Contrast: ``reboot_dut_aux_pwr_cycle`` → ``aux_pwr_cycle`` (clears sim).
    """
    cmd = f"echo 1 > {BMC_HWMGMT_PWR_CYCLE_PATH}"
    logger.info("Initiating host/PDB power cycle (%s)", BMC_HWMGMT_PWR_CYCLE_PATH)
    # BMC stays up; the write returns quickly. SSH drop is still possible around
    # host rail bounce — treat timeout/drop as success like other reboot helpers.
    _run_reboot_cmd(bmc_engine, cmd, "BMC host/PDB pwr_cycle")


def reboot_bmc_soft(bmc_engine: LinuxSshEngine) -> None:
    """Reboot BMC only via OOB SSH ``reboot``.

    SPC5/6 architecture: CPU/NOS and ASIC stay powered (reset-tolerance / main power
    on). OOB management is temporarily unavailable until BMC comes back.
    Prefer plain ``reboot`` on the BMC shell (SN 8); Redfish Manager.Reset /
    ``bmc-cli reset bmc-reset`` are alternate CL paths.

    Caller must prove the reboot: ``wait_for_bmc_oob_ssh_down`` then
    ``assert_bmc_rebooted`` (same pattern as NOS soft reboot).
    """
    logger.info("Initiating BMC soft reboot (OOB reboot)")
    _run_reboot_cmd(bmc_engine, "reboot", "BMC soft reboot")


def read_bmc_boot_identity(bmc_engine: LinuxSshEngine) -> tuple:
    """Return ``(boot_id, uptime_sec)`` used to prove a BMC reboot occurred."""
    return read_dut_boot_identity(bmc_engine)


def wait_for_bmc_oob_ssh_down(
    oob_ip: str,
    bmc_engine: Optional[LinuxSshEngine] = None,
    timeout: int = BMC_REBOOT_SSH_DOWN_TIMEOUT,
    interval: int = BMC_REBOOT_SSH_POLL_INTERVAL,
    ssh_port: int = 22,
) -> None:
    """Poll until BMC OOB SSH is unreachable (TCP closed / connect fails).

    Required before ``wait_for_bmc_oob_ssh`` so a no-op ``reboot`` cannot false-pass
    when OOB never dropped. Closes ``bmc_engine`` first when provided so a stale
    channel does not mask the outage.
    """
    if bmc_engine is not None:
        close_bmc_oob_shell(bmc_engine)

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((oob_ip, ssh_port), timeout=SSH_PORT_DOWN_PROBE_TIMEOUT):
                pass
        except OSError:
            logger.info(
                "BMC OOB SSH unreachable (%s:%s down) — reboot outage observed",
                oob_ip, ssh_port,
            )
            return
        time.sleep(interval)
    raise AssertionError(
        f"BMC OOB SSH never went down within {timeout}s after reboot trigger "
        f"({oob_ip}:{ssh_port} still accepting TCP) — BMC reboot likely did not start"
    )


def assert_bmc_rebooted(
    bmc_engine: LinuxSshEngine,
    before: tuple,
    *,
    reboot_started_at: float,
    oob_ip: Optional[str] = None,
    read_timeout: int = BMC_BOOT_IDENTITY_READ_TIMEOUT,
    retry_delay: float = BMC_BOOT_IDENTITY_RETRY_DELAY_SEC,
) -> LinuxSshEngine:
    """Assert BMC reboot via boot_id change and uptime consistent with a fresh boot.

    ``before`` is ``read_bmc_boot_identity()`` taken before the reboot trigger.
    ``reboot_started_at`` is ``time.monotonic()`` from just before the trigger.

    Right after BMC OOB returns, the first SSH session can be half-open (empty
    boot-identity reads / wedged channels — SN7 [bmc] 2026-08-26). When ``oob_ip``
    is set, unreadable identity triggers reconnect + retry. Real proof failures
    (unchanged boot_id / absurd uptime) still fail immediately.
    """
    before_boot_id, before_uptime = before
    engine = bmc_engine
    last_err: Optional[BaseException] = None
    deadline = time.time() + read_timeout
    while time.time() < deadline:
        try:
            after_boot_id, after_uptime = read_bmc_boot_identity(engine)
        except Exception as err:
            last_err = err
            logger.warning(
                "BMC boot identity unreadable (%s: %s); retry%s",
                type(err).__name__,
                err,
                f" with reconnect to {oob_ip}" if oob_ip else "",
            )
            if oob_ip:
                close_bmc_oob_shell(engine)
                try:
                    engine = open_bmc_oob_shell(oob_ip, tries=1)
                except Exception as reconnect_err:
                    logger.warning(
                        "BMC OOB reconnect failed: %s: %s",
                        type(reconnect_err).__name__,
                        reconnect_err,
                    )
            time.sleep(retry_delay)
            continue

        elapsed = max(0.0, time.monotonic() - reboot_started_at)
        logger.info(
            "BMC boot identity before=(%s, %ss) after=(%s, %ss) elapsed=%.1fs",
            before_boot_id, before_uptime, after_boot_id, after_uptime, elapsed,
        )
        if after_boot_id == before_boot_id:
            raise AssertionError(
                f"BMC boot_id unchanged after reboot trigger ({after_boot_id}); "
                f"uptime before={before_uptime}s after={after_uptime}s — reboot did not occur"
            )
        if after_uptime > elapsed + REBOOT_UPTIME_SLACK_SEC:
            raise AssertionError(
                f"BMC uptime after reboot ({after_uptime}s) exceeds reboot wait window "
                f"~{elapsed:.0f}s (+{REBOOT_UPTIME_SLACK_SEC}s slack); boot_id changed "
                f"({before_boot_id} → {after_boot_id}) but uptime looks pre-reboot"
            )
        return engine

    raise AssertionError(
        f"Could not read BMC boot identity within {read_timeout}s after OOB return: {last_err}"
    )


def read_dut_boot_identity(engine: LinuxSshEngine) -> tuple:
    """Return ``(boot_id, uptime_sec)`` used to prove a NOS/BMC reboot occurred.

    Uses two simple reads (not nested ``printf "$(…)"``) — OpenBMC/netmiko sessions
    have dropped empty output or wedged on the compound form right after BMC reboot.
    """
    boot_raw = engine.run_cmd(
        "cat /proc/sys/kernel/random/boot_id",
        validate=False,
        timeout=DUT_LIVENESS_CMD_TIMEOUT,
        retry_run=False,
    ).strip()
    uptime_raw = engine.run_cmd(
        "awk '{print int($1)}' /proc/uptime",
        validate=False,
        timeout=DUT_LIVENESS_CMD_TIMEOUT,
        retry_run=False,
    ).strip()
    boot_parts = boot_raw.split()
    boot_id = boot_parts[-1] if boot_parts else ""
    uptime_parts = uptime_raw.split()
    uptime_tok = uptime_parts[-1] if uptime_parts else ""
    if not boot_id or not uptime_tok.isdigit():
        raise AssertionError(
            f"Cannot read DUT boot identity: boot_id={boot_raw!r} uptime={uptime_raw!r}"
        )
    return boot_id, int(uptime_tok)


def wait_for_dut_ssh_down(
    engine: LinuxSshEngine,
    timeout: int = SOFT_REBOOT_SSH_DOWN_TIMEOUT,
    interval: int = REBOOT_SSH_POLL_INTERVAL,
) -> None:
    """Poll until DUT SSH is unreachable (TCP 22 closed / connect fails).

    Required before ``wait_for_dut_ssh`` so a no-op reboot cannot false-pass when
    SSH never dropped. Drops any local stale channel first so ControlMaster does
    not mask the outage.
    """
    try:
        engine.disconnect()
        logger.info("Disconnected DUT SSH before waiting for outage")
    except Exception as e:
        logger.info("DUT disconnect before SSH-down wait (ok): %s: %s", type(e).__name__, e)

    ssh_port = int(getattr(engine, "ssh_port", None) or 22)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((engine.ip, ssh_port), timeout=SSH_PORT_DOWN_PROBE_TIMEOUT):
                pass
        except OSError:
            logger.info("DUT SSH unreachable (port %s down) — reboot outage observed", ssh_port)
            return
        time.sleep(interval)
    raise AssertionError(
        f"DUT SSH never went down within {timeout}s after reboot trigger "
        f"({engine.ip}:{ssh_port} still accepting TCP) — reboot likely did not start"
    )


def assert_dut_rebooted(
    engine: LinuxSshEngine,
    before: tuple,
    *,
    reboot_started_at: float,
) -> None:
    """Assert NOS reboot via boot_id change and uptime consistent with a fresh boot.

    ``before`` is ``read_dut_boot_identity()`` taken before the reboot trigger.
    ``reboot_started_at`` is a ``time.monotonic()`` stamp from just before the
    trigger — wall clock can jump (NTP) mid-reboot and skew the bound. Uptime must
    fit that window (plus slack); a no-op reboot leaves a large continuing uptime
    that fails this check even if boot_id probing somehow raced.
    """
    before_boot_id, before_uptime = before
    after_boot_id, after_uptime = read_dut_boot_identity(engine)
    elapsed = max(0.0, time.monotonic() - reboot_started_at)
    logger.info(
        "DUT boot identity before=(%s, %ss) after=(%s, %ss) elapsed=%.1fs",
        before_boot_id, before_uptime, after_boot_id, after_uptime, elapsed,
    )
    if after_boot_id == before_boot_id:
        raise AssertionError(
            f"DUT boot_id unchanged after reboot trigger ({after_boot_id}); "
            f"uptime before={before_uptime}s after={after_uptime}s — reboot did not occur"
        )
    if after_uptime > elapsed + REBOOT_UPTIME_SLACK_SEC:
        raise AssertionError(
            f"DUT uptime after reboot ({after_uptime}s) exceeds reboot wait window "
            f"~{elapsed:.0f}s (+{REBOOT_UPTIME_SLACK_SEC}s slack); boot_id changed "
            f"({before_boot_id} → {after_boot_id}) but uptime looks pre-reboot"
        )


def wait_for_dut_ssh(
    engine: LinuxSshEngine,
    timeout: int = REBOOT_SSH_TIMEOUT,
    interval: int = REBOOT_SSH_POLL_INTERVAL,
) -> None:
    """Poll until SSH to DUT is responsive after a reboot.

    Drops any stale SSH channel first (post-reboot ControlMaster / dead session),
    then TCP-probes port 22 before each ``echo alive`` so a half-open channel cannot
    burn the full default ``run_cmd`` timeout.
    """
    try:
        engine.disconnect()
        logger.info("Disconnected stale DUT SSH before post-reboot wait")
    except Exception as e:
        logger.info("DUT disconnect before SSH wait (ok): %s: %s", type(e).__name__, e)

    ssh_port = int(getattr(engine, "ssh_port", None) or 22)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((engine.ip, ssh_port), timeout=SSH_PORT_UP_PROBE_TIMEOUT):
                pass
        except OSError:
            time.sleep(interval)
            continue
        try:
            out = engine.run_cmd(
                "echo alive", validate=False, timeout=DUT_LIVENESS_CMD_TIMEOUT, retry_run=False
            ).strip()
            if "alive" in out:
                logger.info("DUT SSH responsive after reboot")
                return
        except Exception as e:
            logger.info("DUT SSH probe not ready yet: %s: %s", type(e).__name__, e)
        time.sleep(interval)
    raise AssertionError(f"DUT SSH did not recover within {timeout}s after reboot")


def ssh_banner_ready(
    host: str,
    ssh_port: int = 22,
    timeout: int = SSH_BANNER_PROBE_TIMEOUT,
) -> bool:
    """True if ``host:ssh_port`` accepts TCP *and* sends an SSH identification banner.

    Readiness gate in front of engine construction, not a login: nothing is sent and no
    auth is attempted, so the only trace left on the peer is sshd's usual "did not
    receive identification string" note. RFC 4253 has the server send ``SSH-2.0-...``
    first, which is why a read alone distinguishes sshd serving from a port merely
    listening. Both the connect and the read are bounded by ``timeout``.
    """
    try:
        with socket.create_connection((host, ssh_port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            banner = sock.recv(256)
    except OSError:
        return False
    return banner.startswith(b"SSH-")


def wait_for_bmc_oob_ssh(
    oob_ip: str,
    timeout: int = BMC_REBOOT_SSH_TIMEOUT,
    interval: int = BMC_REBOOT_SSH_POLL_INTERVAL,
    ssh_port: int = 22,
) -> LinuxSshEngine:
    """Poll until BMC OOB SSH accepts auth after a BMC reboot; return a fresh shell.

    Outer loop owns the long wait (up to ``timeout``, sleeping ``interval`` between
    attempts). Each attempt calls ``open_bmc_oob_shell(..., tries=1)`` — a *single*
    auth pass (both passwords once). Nested retries here would stack delays on top of
    the outer poll and slow failure detection while BMC is still down.

    Gated on :func:`ssh_banner_ready` so an engine is only built once sshd is serving.
    Ungated, a poll against a still-rebooting BMC enters devts' connection path, where
    one attempt can outlast this whole wait (see SSH_BANNER_PROBE_TIMEOUT) — and because
    ``timeout`` is only tested between attempts, overshooting it is not even detected.
    """
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        if not ssh_banner_ready(oob_ip, ssh_port):
            time.sleep(interval)
            continue
        try:
            return open_bmc_oob_shell(oob_ip, tries=1)
        except Exception as err:
            last_err = f"{type(err).__name__}: {err}"
            time.sleep(interval)
    raise AssertionError(
        f"BMC OOB SSH {oob_ip} did not recover within {timeout}s after BMC reboot: " +
        (last_err if last_err is not None else
         f"{oob_ip}:{ssh_port} never sent an SSH banner — BMC did not finish booting")
    )


# ─── OTEL helpers ───────────────────────────────────────────────────────────

# Optional override when topology has no sonic-mgmt player (CL BSP / VDI runs).
# Example: export LEAKAGE_OTEL_COLLECTOR_IP=10.209.103.48
LEAKAGE_OTEL_COLLECTOR_IP_ENV = "LEAKAGE_OTEL_COLLECTOR_IP"


@dataclass
class OtelLeakageRunPaths:
    """Per-module OTEL collector artifact paths (under a private mktemp dir)."""

    run_dir: str
    output_path: str
    config_path: str
    log_path: str


def _set_otel_leakage_run_paths(
    engine: OtelCollectorEngine, paths: Optional[OtelLeakageRunPaths]
) -> None:
    global _otel_leakage_run_paths
    _otel_leakage_run_paths = paths
    setattr(engine, "_leakage_otel_paths", paths)


def _otel_leakage_paths(engine: Optional[OtelCollectorEngine] = None) -> OtelLeakageRunPaths:
    """Resolve active per-run OTEL paths from the collector engine or module state."""
    if engine is not None:
        paths = getattr(engine, "_leakage_otel_paths", None)
        if paths is not None:
            return paths
    if _otel_leakage_run_paths is not None:
        return _otel_leakage_run_paths
    raise AssertionError(
        "OTEL leakage collector paths not initialized — setup_otel_leakage_collector "
        "must run before reading collector output"
    )


class LocalOtelCollectorHost:
    """Run collector commands on the pytest host via local subprocess.

    Used when topology has no ``sonic-mgmt`` player (typical for CL BSP setups run
    from a VDI). ``ip`` is the address advertised to the DUT as the OTLP destination
    (must be reachable from the switch mgmt VRF — not 127.0.0.1).
    """

    def __init__(self, advertise_ip: str):
        self.ip = advertise_ip

    def run_cmd(
        self,
        cmd: str,
        validate: bool = False,
        timeout: Optional[int] = None,
        retry_run: bool = True,
    ) -> str:
        """Match ``LinuxSshEngine.run_cmd`` default (``validate=False``).

        Callers that must succeed (install, config write, launch) pass
        ``validate=True`` explicitly — same contract on VDI and sonic_mgmt player.
        """
        del retry_run  # SSH-engine API compatibility; local runs are single-shot.
        try:
            completed = subprocess.run(
                ["bash", "-lc", cmd],
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout or OTEL_COLLECTOR_CMD_TIMEOUT,
            )
        except subprocess.TimeoutExpired as e:
            raise AssertionError(f"Local OTEL host cmd timed out: {cmd!r}") from e
        out = (completed.stdout or "") + (completed.stderr or "")
        if validate and completed.returncode != 0:
            raise AssertionError(
                f"Local OTEL host cmd failed (rc={completed.returncode}): {cmd!r}\n{out}"
            )
        return out


def _advertise_ip_toward_dut(dut_engine: LinuxSshEngine) -> str:
    """Pick the local source IP used to reach the DUT (DUT-reachable collector addr)."""
    env_ip = os.environ.get(LEAKAGE_OTEL_COLLECTOR_IP_ENV, "").strip()
    if env_ip:
        # Validate at the point the override enters, not where it is used: this value is
        # operator-supplied and ends up inside ``nv set ... destination <ip>`` on the DUT.
        # ip_address() accepts only a bare address, so shell metacharacters are rejected
        # here rather than relying on every downstream command to quote it.
        try:
            ipaddress.ip_address(env_ip)
        except ValueError as exc:
            raise AssertionError(
                f"{LEAKAGE_OTEL_COLLECTOR_IP_ENV}={env_ip!r} is not an IP address: {exc}"
            ) from exc
        return env_ip
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect((dut_engine.ip, 22))
        return sock.getsockname()[0]


def resolve_otel_collector_engine(engines) -> OtelCollectorEngine:
    """Return the host that runs otelcol-contrib for leakage OTEL checks.

    Prefer ``engines.sonic_mgmt`` when present. Fall back to local subprocess on the
    pytest host for CL BSP topologies that only attach DUT (+ BMC).
    """
    for attr in ("sonic_mgmt", "server", "oob_mgmt_server"):
        eng = getattr(engines, attr, None)
        if eng is not None:
            logger.info("OTEL collector host: engines.%s (%s)", attr, eng.ip)
            return eng
    advertise_ip = _advertise_ip_toward_dut(engines.dut)
    logger.info(
        "OTEL collector host: local pytest host (no sonic-mgmt player); advertise_ip=%s",
        advertise_ip,
    )
    return LocalOtelCollectorHost(advertise_ip)


def _otel_leakage_config_yaml(port: int, output_path: str) -> str:
    return (
        f"receivers:\n"
        f"  otlp:\n"
        f"    protocols:\n"
        f"      grpc:\n"
        f"        endpoint: 0.0.0.0:{port}\n"
        f"processors: {{}}\n"
        f"exporters:\n"
        f"  file:\n"
        f"    path: {output_path}\n"
        f"    rotation:\n"
        f"      max_megabytes: 100\n"
        f"      max_backups: 1\n"
        f"service:\n"
        f"  telemetry:\n"
        f"    metrics:\n"
        f"      level: none\n"
        f"  pipelines:\n"
        f"    metrics:\n"
        f"      receivers: [otlp]\n"
        f"      exporters: [file]\n"
    )


def read_otel_collector_version(engine: OtelCollectorEngine) -> Optional[str]:
    """Version of ``otelcol-contrib`` on PATH (e.g. ``'0.130.0'``), or None.

    Tolerant on purpose: absent binary, non-zero exit and unparseable output all mean
    "not the pinned build", which the caller resolves by installing — so this never
    raises. Marked read rather than bare stdout because async terminal output can
    replace an SSH reply. ``engine`` is an SSH engine or the local collector host.
    """
    raw = engine.run_cmd(
        f"__ngts_value=$({OTEL_COLLECTOR_BINARY} --version 2>/dev/null "
        f"| grep -oE '{OTEL_COLLECTOR_VERSION_RE}' | head -1); "
        f"printf '__NGTS_OTEL_VERSION:%s\\n' \"$__ngts_value\"",
        validate=False,
    )
    matches = [
        re.fullmatch(rf"__NGTS_OTEL_VERSION:({OTEL_COLLECTOR_VERSION_RE})", line.strip())
        for line in str(raw).splitlines()
    ]
    values = [match.group(1) for match in matches if match]
    if len(values) != 1:
        return None
    return values[0]


def setup_otel_leakage_collector(engines, nv_command) -> OtelCollectorEngine:
    """Install + start the OTEL collector and configure the switch.

    Call once per module (module-scoped fixture). Returns the collector engine
    (``engines.sonic_mgmt`` when present, else a local pytest-host runner).

    Switch config applied:
        nv set system telemetry export otlp state enabled
        nv set system telemetry export otlp grpc insecure enabled
        nv set system telemetry export otlp grpc destination <collector_ip> port 4317
        nv set system telemetry export vrf mgmt
        nv set system telemetry platform-stats export state enabled

    VRF note: the nv-otel-collector Docker container runs in the global (default)
    network namespace. Without "export vrf mgmt", it cannot route to the management
    network. The VRF knob is at the "export" level, not under "otlp grpc".
    Confirmed on HW 2026-08-02 (BSP_SPC6_mlx_6600_04, CL 5.18).
    """
    collector_engine = resolve_otel_collector_engine(engines)
    collector_ip = collector_engine.ip

    # ── Collector side: install unless the pinned version is already present ───
    # Presence alone is not enough: a concurrent session or an older run can leave a
    # different otelcol-contrib in /usr/local/bin, and a config that parses on one
    # version can fail on another — so compare versions, not existence.
    installed_version = read_otel_collector_version(collector_engine)
    if installed_version != OTEL_COLLECTOR_VERSION:
        logger.info(
            "Installing %s v%s on %s (found: %s; SHA256-pinned; binary left installed "
            "after suite — concurrent sessions / image pre-provision preferred)",
            OTEL_COLLECTOR_BINARY, OTEL_COLLECTOR_VERSION, collector_ip,
            installed_version or "not installed",
        )
        archive = f"{OTEL_COLLECTOR_BINARY}_{OTEL_COLLECTOR_VERSION}_linux_amd64.tar.gz"
        url = f"{OTEL_COLLECTOR_RELEASES_BASE}/v{OTEL_COLLECTOR_VERSION}/{archive}"
        tmp = _read_marked_terminal_value(
            collector_engine,
            "mktemp -d",
            "__NGTS_OTEL_TMP",
            r"/[^\s:]+",
        )
        if not tmp:
            raise AssertionError("mktemp -d for otelcol install returned no path")
        qtmp = shlex.quote(tmp)
        qarchive = shlex.quote(f"{tmp}/{archive}")
        qbin = shlex.quote(f"{tmp}/{OTEL_COLLECTOR_BINARY}")
        collector_engine.run_cmd(
            f"curl -fsSL -o {qarchive} {shlex.quote(url)}",
            validate=True,
            timeout=OTEL_COLLECTOR_DOWNLOAD_TIMEOUT,
        )
        # sha256sum prints "<hash>  <path>"; take the digest from a capture group rather
        # than piping through awk, so the read stays one command and one process.
        actual_sha = _read_marked_terminal_value(
            collector_engine,
            f"sha256sum {qarchive}",
            "__NGTS_SHA",
            r"([0-9a-f]{64})\s+\S+",
        )
        if actual_sha != OTEL_COLLECTOR_LINUX_AMD64_SHA256:
            collector_engine.run_cmd(
                f"rm -rf {qtmp}", validate=False, timeout=OTEL_COLLECTOR_CMD_TIMEOUT
            )
            raise AssertionError(
                f"otelcol-contrib tarball SHA256 mismatch for {archive}: "
                f"got {actual_sha!r}, expected {OTEL_COLLECTOR_LINUX_AMD64_SHA256!r}"
            )
        collector_engine.run_cmd(
            f"tar -xzf {qarchive} -C {qtmp} {shlex.quote(OTEL_COLLECTOR_BINARY)}",
            validate=True,
            timeout=OTEL_COLLECTOR_CMD_TIMEOUT,
        )
        collector_engine.run_cmd(
            f"sudo install -m 0755 {qbin} "
            f"/usr/local/bin/{OTEL_COLLECTOR_BINARY}",
            validate=True,
            timeout=OTEL_COLLECTOR_CMD_TIMEOUT,
        )
        collector_engine.run_cmd(
            f"rm -rf {qtmp}", validate=False, timeout=OTEL_COLLECTOR_CMD_TIMEOUT
        )
        # Prove the replacement is what PATH now resolves to — an earlier PATH entry
        # shadowing /usr/local/bin would otherwise leave the foreign build in play.
        installed_version = read_otel_collector_version(collector_engine)
        assert installed_version == OTEL_COLLECTOR_VERSION, (
            f"{OTEL_COLLECTOR_BINARY} on {collector_ip} reports "
            f"{installed_version or 'no version'} after installing "
            f"v{OTEL_COLLECTOR_VERSION} — another PATH entry likely shadows "
            f"/usr/local/bin/{OTEL_COLLECTOR_BINARY}"
        )
    # Logged on both paths (fresh install or already pinned) so the report always
    # names the collector build the OTEL assertions ran against.
    logger.info(
        "OTEL collector on %s: %s v%s",
        collector_ip, OTEL_COLLECTOR_BINARY, installed_version,
    )

    # ── Collector side: per-run dir + config + empty output, then start ─────
    # Private mktemp under /var/tmp (not fixed /etc or world-writable /tmp).
    # Output is 644 owned by the SSH user so suite reads need no sudo; root
    # collector can still write the file.
    stop_otel_leakage_collector(collector_engine)
    run_dir = _read_marked_terminal_value(
        collector_engine,
        f"mktemp -d {OTEL_LEAKAGE_RUN_DIR_TEMPLATE}",
        "__NGTS_OTEL_RUN",
        r"/[^\s:]+",
    )
    if not run_dir:
        raise AssertionError("mktemp -d for OTEL run dir returned no path")
    paths = OtelLeakageRunPaths(
        run_dir=run_dir,
        output_path=f"{run_dir}/{OTEL_LEAKAGE_OUTPUT_BASENAME}",
        config_path=f"{run_dir}/{OTEL_LEAKAGE_CONFIG_BASENAME}",
        log_path=f"{run_dir}/{OTEL_LEAKAGE_LOG_BASENAME}",
    )
    _set_otel_leakage_run_paths(collector_engine, paths)
    q_out = shlex.quote(paths.output_path)
    q_cfg = shlex.quote(paths.config_path)
    q_log = shlex.quote(paths.log_path)

    config = _otel_leakage_config_yaml(OTEL_COLLECTOR_PORT, paths.output_path)
    collector_engine.run_cmd(
        "bash -lc " + shlex.quote(
            f"tee {paths.config_path} > /dev/null << 'OTELEOF'\n{config}\nOTELEOF"
        ),
        validate=True,
    )
    # Empty output before launch (avoids truncate-after-open sparse-file bug).
    collector_engine.run_cmd(
        f"umask 022 && : > {q_out} && chmod 644 {q_out}",
        validate=True,
    )
    # Launch the collector in the background (nohup + &) so SSH command returns
    # immediately. stdout/stderr go to the per-run log; process survives SSH drop.
    collector_engine.run_cmd(
        f"bash -c 'nohup sudo {OTEL_COLLECTOR_BINARY} "
        f"--config={q_cfg} "
        f"> {q_log} 2>&1 &'",
        validate=True,
    )
    deadline = time.time() + OTEL_COLLECTOR_BIND_TIMEOUT_SEC
    listening = False
    while time.time() < deadline:
        out = collector_engine.run_cmd(
            f"ss -lntp 2>/dev/null | grep ':{OTEL_COLLECTOR_PORT}' || true", validate=False
        ).strip()
        if out:
            logger.info(
                "OTEL leakage collector listening on %s:%d (run_dir=%s)",
                collector_ip, OTEL_COLLECTOR_PORT, paths.run_dir,
            )
            listening = True
            break
        time.sleep(OTEL_COLLECTOR_BIND_POLL_INTERVAL_SEC)
    if not listening:
        raise AssertionError(
            f"OTEL collector did not bind :{OTEL_COLLECTOR_PORT} within "
            f"{OTEL_COLLECTOR_BIND_TIMEOUT_SEC}s on {collector_ip} "
            f"(run_dir={paths.run_dir}, log={paths.log_path})"
        )

    # ── Switch side: configure OTEL export ──────────────────────────────────
    apply_otel_leakage_dut_export(engines.dut, collector_ip)

    # Poll for first leakage batch (same discipline as wait_for_otel_leakage_value).
    wait_for_otel_leakage_samples(collector_engine)
    return collector_engine


def _otel_dut_export_is_configured(dut: LinuxSshEngine, collector_ip: str) -> bool:
    """Return True when DUT already exports platform-stats OTLP to collector_ip:4317."""
    raw = dut.run_cmd(
        "nv show system telemetry --output json --color off 2>/dev/null || true",
        validate=False,
    ).strip()
    if not raw:
        return False
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return False

    # NVUE JSON may nest under applied/operational or be flat — walk both shapes.
    export = (
        data.get("export") or
        data.get("applied", {}).get("export") or
        data.get("operational", {}).get("export") or
        {}
    )
    otlp = export.get("otlp") or {}
    grpc = otlp.get("grpc") or {}
    dest = grpc.get("destination") or {}
    if isinstance(dest, dict):
        port_info = dest.get(collector_ip) or {}
        port_ok = str(port_info.get("port", "")).strip() == str(OTEL_COLLECTOR_PORT)
    else:
        port_ok = False

    otlp_state = str(otlp.get("state", "")).lower()
    insecure = str(grpc.get("insecure", "")).lower()
    vrf = str(export.get("vrf", "")).lower()

    platform = (
        data.get("platform-stats") or
        data.get("applied", {}).get("platform-stats") or
        data.get("operational", {}).get("platform-stats") or
        {}
    )
    ps_export = platform.get("export") or {}
    ps_state = str(ps_export.get("state", "")).lower()

    ok = (
        otlp_state in ("enabled", "enable") and
        insecure in ("enabled", "enable") and
        port_ok and
        vrf == "mgmt" and
        ps_state in ("enabled", "enable")
    )
    logger.info(
        "OTEL export check dest=%s port_ok=%s otlp=%s insecure=%s vrf=%s platform-stats=%s => %s",
        collector_ip, port_ok, otlp_state, insecure, vrf, ps_state, ok,
    )
    return ok


def apply_otel_leakage_dut_export(
    dut: LinuxSshEngine, collector_ip: str, force: bool = False
) -> None:
    """(Re)apply DUT OTLP + platform-stats export toward the leakage collector.

    Required after NGTS per-test config cleanup, which runs
    ``nv unset system telemetry`` and wipes the module-scoped OTEL setup
    (seen on HW 2026-08-03: Core1 PASS then Core2 stuck at stale 1.0).
    Also needed after soft/PDB reboot when saved applied config no longer
    has telemetry.

    Idempotent: skips ``nv set`` + apply when already configured (unless
    ``force=True``), to avoid restarting prometheus/nv-telemetry on every test
    (those restarts froze OTEL mid-Core2 recovery / aborted gNMI apply).
    """
    if not force and _otel_dut_export_is_configured(dut, collector_ip):
        logger.info(
            "DUT OTEL export already configured toward %s:%d — skip apply",
            collector_ip, OTEL_COLLECTOR_PORT,
        )
        return

    logger.info("Applying DUT OTEL export to %s:%d", collector_ip, OTEL_COLLECTOR_PORT)
    dut.run_cmd("nv set system telemetry export otlp state enabled")
    dut.run_cmd("nv set system telemetry export otlp grpc insecure enabled")
    dut.run_cmd(
        f"nv set system telemetry export otlp grpc destination "
        f"{shlex.quote(collector_ip)} port {OTEL_COLLECTOR_PORT}"
    )
    dut.run_cmd("nv set system telemetry export vrf mgmt")
    dut.run_cmd("nv set system telemetry platform-stats export state enabled")
    # --assume-yes: same service-restart warnings as gNMI path; -y alone can still prompt.
    dut.run_cmd("nv config apply --assume-yes")


def teardown_otel_leakage_collector(engines, nv_command) -> None:
    """Stop the OTEL collector, remove per-run dir, and unset switch OTEL export."""
    collector_engine = resolve_otel_collector_engine(engines)
    collector_ip = collector_engine.ip
    paths = getattr(collector_engine, "_leakage_otel_paths", None) or _otel_leakage_run_paths
    stop_otel_leakage_collector(collector_engine)
    if paths is not None:
        collector_engine.run_cmd(
            f"rm -rf {shlex.quote(paths.run_dir)}", validate=False
        )
        logger.info("Removed OTEL leakage run dir %s", paths.run_dir)
    _set_otel_leakage_run_paths(collector_engine, None)
    dut = engines.dut
    dut.run_cmd(
        f"nv unset system telemetry export otlp grpc destination "
        f"{shlex.quote(collector_ip)}",
        validate=False,
    )
    dut.run_cmd("nv unset system telemetry export otlp grpc insecure", validate=False)
    dut.run_cmd("nv unset system telemetry export otlp state", validate=False)
    dut.run_cmd("nv unset system telemetry export vrf", validate=False)
    dut.run_cmd("nv unset system telemetry platform-stats export state", validate=False)
    dut.run_cmd("nv config apply -y", validate=False)


def _pkill_pattern(literal: str) -> str:
    """Turn a literal command fragment into a ``pkill -f`` pattern that matches only it.

    ``pkill -f`` takes an *extended regex*, not a literal, and matches it against every
    process's full command line — including the ``sudo``/``sh`` wrapper that carries this
    very pattern in its own argv. Two things therefore have to be neutralised:

    * **Self-match.** Left alone, ``sudo pkill -f <pattern>`` signals its own parent, and
      sudo forwards that signal down to pkill, so the sweep can die before it reaches the
      collector — the stop silently does nothing. Bracketing the first character is the
      standard fix: the target's plain ``otelcol...`` still matches ``[o]telcol...``, while
      the wrapper's literal ``[o]telcol...`` text does not.
    * **Dots.** Our paths carry them (``ngts-leakage-otel.XXXXXX``, ``.yaml``) and ``.``
      matches any character. Only one position each, so this was never the flake risk, but
      an unescaped pattern is not the thing we want to reason about at teardown.

    Dots become ``[.]`` rather than ``re.escape``-style backslashes on purpose: ``re.escape``
    also backslashes ``-``, which POSIX leaves undefined for ordinary characters in an ERE,
    and every path here is our own constant plus mktemp's ``[A-Za-z0-9]`` suffix, so ``.`` is
    the only metacharacter that can occur. Bracket expressions are unambiguous in ERE.

    The result still needs ``shlex.quote`` at the call site: the brackets are glob
    characters to the shell.
    """
    assert literal, "pkill pattern must not be empty — it would match every process"
    return f"[{literal[0]}]{literal[1:].replace('.', '[.]')}"


def stop_otel_leakage_collector(engine: OtelCollectorEngine) -> None:
    """Stop this suite's otelcol-contrib (prefer --config path match over run-dir prefix)."""
    paths = getattr(engine, "_leakage_otel_paths", None) or _otel_leakage_run_paths
    if paths is not None:
        match = shlex.quote(_pkill_pattern(paths.config_path))
    else:
        # No paths yet: this is the pre-launch sweep in setup, clearing collectors stranded
        # by an aborted run. Matching the bare binary name would also kill collectors this
        # suite never started — someone else's run on a shared player — so scope it to our
        # own run-dir prefix, which every stray of ours carries in its --config argument.
        match = shlex.quote(
            _pkill_pattern(f"{OTEL_COLLECTOR_BINARY} --config={OTEL_LEAKAGE_RUN_DIR_PREFIX}")
        )
    engine.run_cmd(
        f"sudo pkill -TERM -f {match} 2>/dev/null || true",
        validate=False,
    )
    time.sleep(OTEL_COLLECTOR_TERM_GRACE_SEC)
    engine.run_cmd(
        f"sudo pkill -KILL -f {match} 2>/dev/null || true",
        validate=False,
    )


def _parse_otel_leakage_latest_from_text(raw: str) -> Dict[str, float]:
    """Parse OTLP JSON lines; return latest batch that contains leakage metrics."""
    if not raw or not raw.strip():
        return {}
    latest: Dict[str, float] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            doc = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        batch: Dict[str, float] = {}
        for rm in doc.get("resourceMetrics", []):
            for sm in rm.get("scopeMetrics", []):
                for m in sm.get("metrics", []):
                    if m.get("name") != TELEMETRY_LEAKAGE_METRIC:
                        continue
                    for dp in m.get("gauge", {}).get("dataPoints", []):
                        sensor_name = next(
                            (
                                a["value"]["stringValue"]
                                for a in dp.get("attributes", [])
                                if a.get("key") == "name"
                            ),
                            None,
                        )
                        if sensor_name is not None:
                            batch[sensor_name] = float(dp.get("asDouble", -1))
        if batch:
            latest = batch
    return latest


def read_otel_leakage_latest(engine: OtelCollectorEngine) -> Dict[str, float]:
    """Read latest leakage values from the OTEL output file on engine.

    Pulls only the last ``OTEL_LEAKAGE_LATEST_TAIL_BYTES`` (not a full-file ``cat``)
    so polls stay cheap as the rotated collector file grows. Parses OTLP JSON
    batches (one object per line) and returns values from the most recent batch
    that contains leakage metrics. Keyed by the ``name`` label (leakage1, …).
    Returns {} when the file is absent or no leakage data has arrived yet.
    """
    path = shlex.quote(_otel_leakage_paths(engine).output_path)
    raw = engine.run_cmd(
        f"tail -c {OTEL_LEAKAGE_LATEST_TAIL_BYTES} {path} 2>/dev/null || true",
        validate=False,
    ).strip()
    return _parse_otel_leakage_latest_from_text(raw)


def read_otel_leakage_latest_after_offset(
    engine: OtelCollectorEngine,
    file_offset: int,
) -> Dict[str, float]:
    """Latest leakage batch from collector bytes written after ``file_offset``.

    Used by SN 5 so pre-fault leakage OK samples cannot be mistaken for a live
    post-fault export when other platform-stats keep appending to the file.
    A partial first line (cut mid-JSON) is skipped by the JSON parser.
    """
    start = max(0, int(file_offset)) + 1  # tail -c +N is 1-based
    raw = engine.run_cmd(
        f"tail -c +{start} {shlex.quote(_otel_leakage_paths(engine).output_path)} "
        f"2>/dev/null || true",
        validate=False,
    ).strip()
    return _parse_otel_leakage_latest_from_text(raw)


def assert_otel_leakage_statuses(
    engine: OtelCollectorEngine,
    sensors: Sequence[str],
    expected_value: float,
) -> None:
    """Assert all sensors show expected_value in the latest OTEL batch.

    Does NOT poll — reads current state of the output file once.
    Use wait_for_otel_leakage_value when the state may not yet have reached
    the next 60s sample boundary after an event.
    """
    values = read_otel_leakage_latest(engine)
    errors = []
    for sensor in sensors:
        actual = values.get(sensor)
        if actual is None:
            errors.append(f"{sensor}: absent from OTEL output (no sample yet?)")
        elif actual != expected_value:
            errors.append(f"{sensor}: expected {expected_value}, got {actual}")
    assert not errors, "OTEL leakage status mismatch:\n" + "\n".join(errors)


def assert_otel_sensors_present(
    engine: OtelCollectorEngine,
    sensors: Sequence[str],
) -> None:
    """Assert all sensors appear in the latest OTEL batch (oper-status ACTIVE equivalent).

    In gNMI, oper-status=ACTIVE means the sensor is present and healthy. The OTEL
    equivalent is that the metric for that sensor appears in the exported batch at all —
    both 0.0 and 1.0 values count as present (same logic as leakageStatus → ACTIVE).
    """
    values = read_otel_leakage_latest(engine)
    missing = [s for s in sensors if s not in values]
    assert not missing, (
        f"Sensors absent from OTEL output (oper-status ACTIVE equivalent failed): {missing}. "
        f"Got sensors in latest batch: {sorted(values)}"
    )


def otel_collector_file_mtime(engine: OtelCollectorEngine) -> int:
    """Return epoch mtime of the OTEL leakage output file, or 0 if absent."""
    raw = engine.run_cmd(
        f"stat -c '%Y' {shlex.quote(_otel_leakage_paths(engine).output_path)} "
        f"2>/dev/null || echo 0",
        validate=False,
    ).strip()
    try:
        return int(raw.split()[0])
    except (TypeError, ValueError, IndexError):
        return 0


def otel_collector_file_size(engine: OtelCollectorEngine) -> int:
    """Return byte size of the OTEL leakage output file, or 0 if absent."""
    raw = engine.run_cmd(
        f"stat -c '%s' {shlex.quote(_otel_leakage_paths(engine).output_path)} "
        f"2>/dev/null || echo 0",
        validate=False,
    ).strip()
    try:
        return int(raw.split()[0])
    except (TypeError, ValueError, IndexError):
        return 0


def wait_for_otel_leakage_samples(
    engine: OtelCollectorEngine,
    timeout: int = OTEL_LEAKAGE_POLL_TIMEOUT_SEC,
    interval: int = OTEL_LEAKAGE_POLL_INTERVAL_SEC,
) -> Dict[str, float]:
    """Poll until the collector has at least one leakage metric batch.

    Used by module setup after enabling DUT export — replaces a fixed sleep so we
    return as soon as a sample arrives (or fail if none within ``timeout``).
    Same poll interval/timeout constants as :func:`wait_for_otel_leakage_value`.
    """
    deadline = time.time() + timeout
    last: Dict[str, float] = {}
    logger.info(
        "Waiting up to %ds for first OTEL leakage sample (poll every %ds)",
        timeout, interval,
    )
    while time.time() < deadline:
        last = read_otel_leakage_latest(engine)
        if last:
            logger.info("First OTEL leakage sample batch: %s", last)
            return last
        time.sleep(interval)
    raise AssertionError(
        f"No OTEL leakage metrics within {timeout}s after enabling DUT export "
        f"(last batch={last!r}; output={_otel_leakage_paths(engine).output_path})"
    )


def wait_for_otel_leakage_value(
    engine: OtelCollectorEngine,
    sensors: Sequence[str],
    expected_value: float,
    timeout: int = OTEL_LEAKAGE_POLL_TIMEOUT_SEC,
    dut: Optional[LinuxSshEngine] = None,
    collector_ip: Optional[str] = None,
) -> None:
    """Poll the OTEL output file until all sensors show expected_value or timeout.

    Required after CPLD injection or CPLD clear because OTEL updates only at the
    next 60s platform-stats sample boundary, not in real time. By the time this is
    called (after PNE + gNMI + BMC surface checks), significant time has already
    elapsed since the event — the remaining poll typically completes within one cycle.

    When ``dut`` and ``collector_ip`` are provided: if the target sensors' observed
    leakage values stay wrong and unchanged for ``OTEL_FILE_STALL_KICK_SEC``, force
    one ``apply_otel_leakage_dut_export(..., force=True)`` to restart the DUT export
    path (seen HW 2026-08-03: export frozen after leak sample). Freshness is per
    leakage values, not collector-file mtime, so non-leakage batches cannot mask a stall.
    """
    deadline = time.time() + timeout
    last_observed: Optional[tuple] = None
    values_unchanged_since: Optional[float] = None
    kicked = False
    while time.time() < deadline:
        values = read_otel_leakage_latest(engine)
        logger.info(
            "OTEL poll want=%s sensors=%s latest=%s",
            expected_value, list(sensors), values,
        )
        if all(values.get(s) == expected_value for s in sensors):
            logger.info(
                "OTEL leakage %s = %s confirmed", [s for s in sensors], expected_value
            )
            return

        # Stall = wanted sensors still wrong and their observed tuple has not moved.
        observed = tuple(values.get(s) for s in sensors)
        now = time.time()
        if observed == last_observed:
            if values_unchanged_since is None:
                values_unchanged_since = now
        else:
            last_observed = observed
            values_unchanged_since = now

        if (
            not kicked and
            dut is not None and
            collector_ip and
            values_unchanged_since is not None and
            (now - values_unchanged_since) >= OTEL_FILE_STALL_KICK_SEC
        ):
            logger.warning(
                "OTEL leakage values stalled %ss (observed=%s want=%s) — "
                "force re-apply DUT export to %s",
                OTEL_FILE_STALL_KICK_SEC, observed, expected_value, collector_ip,
            )
            apply_otel_leakage_dut_export(dut, collector_ip, force=True)
            kicked = True
            # Reset stall clock so we do not immediately re-kick; allow a new sample.
            values_unchanged_since = time.time()

        time.sleep(OTEL_LEAKAGE_POLL_INTERVAL_SEC)
    values = read_otel_leakage_latest(engine)
    errors = [
        f"{s}: expected {expected_value}, got {values.get(s, 'ABSENT')}"
        for s in sensors
        if values.get(s) != expected_value
    ]
    raise AssertionError(
        f"OTEL leakage did not reach {expected_value} within {timeout}s"
        f"{' (stall kick already attempted)' if kicked else ''}:\n" +
        "\n".join(errors)
    )
