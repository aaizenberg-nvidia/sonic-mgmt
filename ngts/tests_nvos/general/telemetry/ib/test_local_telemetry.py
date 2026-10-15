"""Local switch half of the extended telemetry HLD: port PHY / BER / routing, and platform.

Every leaf here is read off the switch's own interfaces and components, and must carry the
same name, unit and value as its peer-port twin.
"""

# Kept alongside the peer-port suite so both halves share one set of leaf tables and the same
# `gnmi_client` fixture.

import logging
from typing import List

import pytest

from ngts.nvos_tools.infra.ResultObj import IssueType, ResultObj
from ngts.tests_nvos.constants import MINUTE
from ngts.tools.test_utils import allure_utils as allure

from ngts.tests_nvos.general.telemetry.ib import helpers as ibh
from ngts.tests_nvos.general.telemetry.ib import otel_helpers as ibo
from ngts.tests_nvos.general.telemetry.ib.constants import (
    API_GNMIC,
    API_NVUE_CLI,
    API_OTEL,
    ASIC_FREQUENCY_LEAVES,
    GnmiYangPaths,
    LOCAL_PLATFORM_NODE_IDS,
    LOCAL_PLATFORM_OTEL_GROUPS,
    LOCAL_PORT_FAMILIES,
    LOCAL_PORT_OTEL_GROUPS,
    LOCAL_TRANSCEIVER_IDENTITY_LEAVES,
    LOCAL_TRANSCEIVER_NVUE_TO_GNMI_LEAF,
    LocalPlatformNodeId,
    LocalPortFamilyId,
    SAMPLING_JITTER_TOLERANCE_PCT,
    TRANSCEIVER_CHANNEL_SNR_READINGS,
    TRANSCEIVER_SNR_LEAVES,
)

logger = logging.getLogger(__name__)


def _require_link_up_port(purpose: str):
    """A link-up switch Port, or fail: the local leaves cannot be read off a down port."""
    return ibh.require_link_up_port(purpose, skip=False)


def _verify_ber_is_one_combined_value(engines, port_name, cli_leaves, gnmi_leaves) -> None:
    """The published error rate is the polled coefficient and magnitude combined (R-NVOS-3).

    The switch polls two raw hardware fields per rate. Neither may reach either surface on its
    own, and the COUNTERS_DB row is read to confirm the pair really is what the switch holds -
    the exact field spellings are not published, so they are matched by name.
    """
    with allure.step(f"Read the raw BER fields {port_name} polled from System DB"):
        db_row = ibh.read_local_port_counters_row(engines, port_name)
        raw_components = ibh.ber_raw_component_db_fields(db_row)
        ibh.attach_dict(f"{port_name} raw BER components", raw_components)
        ResultObj(
            bool(raw_components),
            f"No COUNTERS_DB field of {port_name} names a BER coefficient or magnitude, so the "
            f"switch does not hold the polled pair the published rate combines (R-NVOS-3)",
            issue_type=IssueType.PossibleBug,
        ).verify_result()

    with allure.step("Neither raw component is published on either surface"):
        problems = (ibh.ber_component_leaf_problems(cli_leaves, f"{port_name} CLI") +
                    ibh.ber_component_leaf_problems(gnmi_leaves, f"{port_name} gNMI"))
        ResultObj(
            not problems,
            f"{len(problems)} raw BER component(s) are published instead of the combined "
            f"rate:\n  " + "\n  ".join(problems),
            issue_type=IssueType.PossibleBug,
        ).verify_result()


# 5.8 test_local_port_phy_and_routing_counters_parity
@pytest.mark.gnmi
@pytest.mark.otel
@pytest.mark.ib
@pytest.mark.interfaces
@pytest.mark.nvos_build
@pytest.mark.timeout(10 * MINUTE, func_only=True)
@pytest.mark.parametrize("family", LOCAL_PORT_FAMILIES, ids=lambda f: f.family_id)
def test_local_port_phy_and_routing_counters_parity(engines, gnmi_client, family, ib_otel):
    """A switch port's own PHY, error-rate-monitor and routing counters agree on CLI, gNMI and OTEL."""
    port = _require_link_up_port(f"read the {family.family_id} family")

    with allure.step(f"Read the {family.family_id} family of {port.name} on both surfaces"):
        cli_leaves = ibh.nvue_local_family_leaves(engines, port, family)
        gnmi_leaves = ibh.gnmi_family_leaves(gnmi_client, port.name, family)
        ibh.attach_dict(f"{port.name} {family.family_id}",
                        {API_NVUE_CLI: cli_leaves, API_GNMIC: gnmi_leaves})

    with allure.step(f"Every {family.family_id} leaf is present and typed on both surfaces"):
        problems = [
            f"[{label}] {problem}"
            for label, leaves in ((API_NVUE_CLI, cli_leaves), (API_GNMIC, gnmi_leaves))
            for problem in ibh.leaf_problems(leaves, family.leaves,
                                             where=f"{port.name} {family.family_id}",
                                             require_value=True)
        ]
        ResultObj(
            not problems,
            f"The {family.family_id} family of {port.name} has {len(problems)} "
            f"problem(s):\n  " + "\n  ".join(problems),
            issue_type=IssueType.PossibleBug,
        ).verify_result()

    with allure.step(f"CLI and gNMI agree on every {family.family_id} leaf"):
        problems, compared = ibh.leaf_parity_problems(
            family.leaves, cli_leaves, gnmi_leaves, API_NVUE_CLI, API_GNMIC)
        logger.info("Compared %d %s leaf value(s) of %s across both surfaces",
                    compared, family.family_id, port.name)
        ResultObj(
            not problems,
            f"{port.name} reports {len(problems)} {family.family_id} leaf value(s) "
            f"differently on the CLI and over gNMI; only the encoding may differ, and "
            f"counters and sampled values may differ within sampling jitter (SR-1):\n  " +
            "\n  ".join(problems),
            issue_type=IssueType.PossibleBug,
        ).verify_result()

    with allure.step(f"CLI and OTEL agree on every {family.family_id} leaf"):
        _verify_otel_leaves(LOCAL_PORT_OTEL_GROUPS[family.family_id], port.name, family.leaves,
                            cli_leaves, family.family_id)

    if family.family_id == LocalPortFamilyId.BER_MONITOR:
        _verify_ber_is_one_combined_value(engines, port.name, cli_leaves, gnmi_leaves)


def _verify_otel_leaves(group, key: str, expected, shown, what: str, platform: bool = False) -> None:
    """Every leaf of ``group`` is exported for ``key`` and reads the same as the CLI (SR-1)."""
    snap = ibo.active_surface().snapshot("local-platform" if platform else "local-port",
                                         platform=platform)
    problems = ibo.group_problems(snap, group, key)
    parity, compared = ibh.otel_leaf_parity_problems(
        snap, group, key, expected, shown, API_NVUE_CLI, single_series=platform)
    problems += parity
    logger.info("Compared %d %s value(s) of %s across the CLI and OTEL", compared, what, key)
    ResultObj(
        not problems,
        f"{key} exports {len(problems)} {what} value(s) over OTEL that are missing or differ "
        f"from the CLI; only the encoding may differ, and sampled values may differ within "
        f"sampling jitter (SR-1):\n  " + "\n  ".join(problems),
        issue_type=IssueType.PossibleBug,
    ).verify_result()


def _select_local_component(gnmi_client, names: List[str], kind: str) -> str:
    """One component name the CLI and the gNMI components tree agree on."""
    listed = ibh.gnmi_local_component_names(gnmi_client)
    ibh.attach_dict(f"local {kind} names", {"nv show": names, "gnmi components": listed})
    shared = [name for name in names if name in listed]
    ResultObj(
        bool(shared),
        f"No {kind} name is reported by both surfaces (nv show: {names!r}, gNMI components: "
        f"{listed!r}); the two must name the same component the same way (SR-1)",
        issue_type=IssueType.PossibleBug,
    ).verify_result()
    return shared[0]


def _verify_local_transceiver_identity(inventory, gnmi_client, name) -> None:
    """Identity leaves of the switch's own module read the same on the CLI and over gNMI.

    The CLI keeps its existing `vendor-name` for the gNMI `vendor` leaf (HLD rev 1.1, SR-1).
    """
    cli_leaves = ibh.remap_leaf_keys(ibh.local_transceiver_leaves(inventory, name),
                                     LOCAL_TRANSCEIVER_NVUE_TO_GNMI_LEAF)
    gnmi_leaves = ibh.gnmi_get_flat_relative(
        gnmi_client, prefix=GnmiYangPaths.COMPONENT_TRANSCEIVER_STATE.format(name=name))
    ibh.attach_dict(f"{name} identity",
                    {API_NVUE_CLI: cli_leaves, API_GNMIC: gnmi_leaves})

    with allure.step(f"Every identity leaf of {name} is present and typed on both surfaces"):
        problems = [
            f"[{label}] {problem}"
            for label, leaves in ((API_NVUE_CLI, cli_leaves), (API_GNMIC, gnmi_leaves))
            for problem in ibh.leaf_problems(leaves, LOCAL_TRANSCEIVER_IDENTITY_LEAVES,
                                             where=f"{name} identity", require_value=True)
        ]
        ResultObj(
            not problems,
            f"The identity of transceiver {name} has {len(problems)} problem(s):\n  " +
            "\n  ".join(problems),
            issue_type=IssueType.PossibleBug,
        ).verify_result()

    with allure.step(f"CLI and gNMI agree on the identity of {name}"):
        problems, compared = ibh.leaf_parity_problems(
            LOCAL_TRANSCEIVER_IDENTITY_LEAVES, cli_leaves, gnmi_leaves,
            API_NVUE_CLI, API_GNMIC)
        logger.info("Compared %d identity leaf value(s) of %s across both surfaces",
                    compared, name)
        ResultObj(
            not problems,
            f"Transceiver {name} reports {len(problems)} identity leaf value(s) differently "
            f"on the CLI and over gNMI; only the encoding may differ (SR-1):\n  " +
            "\n  ".join(problems),
            issue_type=IssueType.PossibleBug,
        ).verify_result()

    with allure.step(f"CLI and OTEL agree on the identity of {name}"):
        _verify_otel_leaves(LOCAL_PLATFORM_OTEL_GROUPS[LocalPlatformNodeId.TRANSCEIVER_IDENTITY],
                            name, LOCAL_TRANSCEIVER_IDENTITY_LEAVES, cli_leaves, "identity",
                            platform=True)


def _verify_local_channel_snr(inventory, gnmi_client, name) -> None:
    """Per-channel signal quality is served over gNMI and, per the HLD, over gNMI only."""
    with allure.step(f"Read the per-channel signal quality of {name} over gNMI"):
        members = ibh.gnmi_list_members(
            gnmi_client, GnmiYangPaths.COMPONENT_TRANSCEIVER_CHANNELS.format(name=name),
            relative=True)
        ibh.attach_dict(f"{name} channels", members)
        problems = [] if members else [f"gNMI exposes no channel for {name}"]
        problems += [
            f"channel [{member_key}] {problem}"
            for member_key, member in sorted(members.items())
            for problem in ibh.leaf_problems(member, TRANSCEIVER_SNR_LEAVES, where=name,
                                             require_value=True)
        ]
        ResultObj(
            not problems,
            f"The per-channel signal quality of {name} has {len(problems)} problem(s):\n  " +
            "\n  ".join(problems),
            issue_type=IssueType.PossibleBug,
        ).verify_result()

    with allure.step("No nv show renders the signal-quality leaves (gNMI-only per the HLD)"):
        cli_leaves = ibh.local_transceiver_leaves(inventory, name)
        leaked = sorted(leaf for leaf in TRANSCEIVER_CHANNEL_SNR_READINGS if leaf in cli_leaves)
        ResultObj(
            not leaked,
            f"`nv show platform transceiver {name}` renders {leaked!r}, but the HLD serves the "
            f"media and host signal quality over gNMI only. If the CLI has gained them, this "
            f"expectation and the test plan's open issue both need updating.",
            issue_type=IssueType.PossibleBug,
        ).verify_result()

    _verify_otel_channel_snr(members, name)


def _verify_otel_channel_snr(gnmi_members, name) -> None:
    """Every gNMI channel exports its signal quality over OTEL, with the same readings (SR-1)."""
    group = LOCAL_PLATFORM_OTEL_GROUPS[LocalPlatformNodeId.CHANNEL_SNR]
    # gNMI keys a reading by its path (`state/media-snr/instant`); OTEL by the reading name.
    gnmi_key = {leaf: next((key for key in TRANSCEIVER_SNR_LEAVES if f"/{leaf}/" in f"/{key}"), None)
                for leaf in group.leaves}
    expected = {leaf: TRANSCEIVER_SNR_LEAVES[key] for leaf, key in gnmi_key.items() if key}
    with allure.step(f"gNMI and OTEL agree on the per-channel signal quality of {name}"):
        snap = ibo.active_surface().snapshot("local-platform", platform=True)
        otel_members = ibh.otel_member_labels(snap, group, name)
        gnmi_by_index = {ibh.list_member_index(key): member for key, member in gnmi_members.items()}
        problems = [f"{name}: gNMI reading {leaf!r} has no counterpart in TRANSCEIVER_SNR_LEAVES"
                    for leaf, key in sorted(gnmi_key.items()) if not key]
        if set(otel_members) != set(gnmi_by_index):
            problems.append(f"{name}: OTEL exports channels {sorted(otel_members)} but gNMI lists "
                            f"{sorted(gnmi_by_index)}")
        compared = 0
        for index, member in sorted(gnmi_by_index.items()):
            if index not in otel_members:
                continue
            shown = {leaf: member[key] for leaf, key in gnmi_key.items() if key and key in member}
            member_problems, member_compared = ibh.otel_leaf_parity_problems(
                snap, group, name, expected, shown, API_GNMIC, member=otel_members[index])
            problems += member_problems
            compared += member_compared
        ResultObj(
            not problems and compared > 0,
            f"The per-channel signal quality of {name} has {len(problems)} problem(s) over "
            f"{API_OTEL} (compared {compared}):\n  " + "\n  ".join(problems),
            issue_type=IssueType.PossibleBug,
        ).verify_result()


def _verify_local_asic_frequency(engines, gnmi_client, name) -> None:
    """The switch chip's clock speed reads the same on the CLI and over gNMI."""
    cli_leaves = ibh.local_asic_frequency_leaves(engines, name)
    gnmi_leaves = ibh.gnmi_get_flat_relative(
        gnmi_client, prefix=GnmiYangPaths.COMPONENT_IC_FREQUENCY.format(name=name))
    ibh.attach_dict(f"{name} frequency", {API_NVUE_CLI: cli_leaves, API_GNMIC: gnmi_leaves})

    with allure.step(f"All four clock-speed values are present on both surfaces for {name}"):
        problems = [
            f"[{label}] {problem}"
            for label, leaves in ((API_NVUE_CLI, cli_leaves), (API_GNMIC, gnmi_leaves))
            for problem in ibh.leaf_problems(leaves, ASIC_FREQUENCY_LEAVES,
                                             where=f"{name} frequency", require_value=True)
        ]
        ResultObj(
            not problems,
            f"The clock speed of {name} has {len(problems)} problem(s):\n  " +
            "\n  ".join(problems),
            issue_type=IssueType.PossibleBug,
        ).verify_result()

    with allure.step(f"CLI and gNMI agree on the clock speed of {name}"):
        problems, compared = ibh.leaf_parity_problems(
            ASIC_FREQUENCY_LEAVES, cli_leaves, gnmi_leaves, API_NVUE_CLI, API_GNMIC)
        logger.info("Compared %d clock-speed value(s) of %s across both surfaces, each a "
                    "sampled reading allowed to differ by up to %.0f%%",
                    compared, name, SAMPLING_JITTER_TOLERANCE_PCT * 100)
        ResultObj(
            not problems,
            f"Chip {name} reports {len(problems)} clock-speed value(s) differently on the CLI "
            f"and over gNMI; the current reading may differ within sampling jitter only "
            f"(SR-1):\n  " + "\n  ".join(problems),
            issue_type=IssueType.PossibleBug,
        ).verify_result()

    with allure.step(f"CLI and OTEL agree on the clock speed of {name}"):
        _verify_otel_leaves(LOCAL_PLATFORM_OTEL_GROUPS[LocalPlatformNodeId.ASIC_FREQUENCY], name,
                            ASIC_FREQUENCY_LEAVES, cli_leaves, "clock-speed", platform=True)


# 5.9 test_local_platform_transceiver_and_asic_parity
@pytest.mark.gnmi
@pytest.mark.otel
@pytest.mark.ib
@pytest.mark.platform
@pytest.mark.transceiver
@pytest.mark.nvos_build
@pytest.mark.timeout(12 * MINUTE, func_only=True)
@pytest.mark.parametrize("node_id", LOCAL_PLATFORM_NODE_IDS)
def test_local_platform_transceiver_and_asic_parity(engines, gnmi_client, node_id, ib_otel):
    """The switch's own module and chip report identity, signal quality and clock speed, on every surface."""
    _require_link_up_port("read the local platform leaves")

    if node_id == LocalPlatformNodeId.ASIC_FREQUENCY:
        with allure.step("List the switch chips and pick one"):
            asics = ibh.gnmi_local_asic_names(gnmi_client)
            ibh.attach_dict("local asic names", asics)
            ResultObj(
                bool(asics),
                "The gNMI components tree lists no component of type ASIC, so the switch "
                "chip and its clock speed are not modelled",
                issue_type=IssueType.PossibleBug,
            ).verify_result()
            name = asics[0]
        _verify_local_asic_frequency(engines, gnmi_client, name)
    else:
        with allure.step("List the fitted transceivers and pick one"):
            inventory = ibh.local_transceiver_inventory(engines)
            transceivers = ibh.fitted_transceiver_names(inventory)
            logger.info("%d of the %d cage(s) the platform lists hold a module: %r",
                        len(transceivers), len(inventory), transceivers)
            if not transceivers:
                ibh.report_wrong_lab(
                    "WRONG LAB SETUP: no transceiver is fitted on this DUT, so the local "
                    "module identity and per-channel signal quality cannot be read. An "
                    "optical transceiver on an up port is required.",
                    skip=False,
                )
            if node_id == LocalPlatformNodeId.CHANNEL_SNR:
                transceivers = ibh.diagnostic_transceiver_names(inventory, transceivers)
                if not transceivers:
                    ibh.report_wrong_lab(
                        "WRONG LAB SETUP: every fitted module reports no diagnostic data "
                        "(passive copper), so none has a channel to sample signal quality on. "
                        "A DDMI-capable (optical or active) transceiver is required.",
                        skip=False,
                    )
            name = _select_local_component(gnmi_client, transceivers, "transceiver")
        if node_id == LocalPlatformNodeId.TRANSCEIVER_IDENTITY:
            _verify_local_transceiver_identity(inventory, gnmi_client, name)
        else:
            _verify_local_channel_snr(inventory, gnmi_client, name)
