"""NVOS OTEL test-plan CLI configuration and functional tests.

Implements the NVOS ``OTEL_TP`` plan items owned by the NVOS verification suite,
kept separate from the ported Cumulus modules in this folder:

* Section 4.1 :func:`test_otel_cli_base_export_config` - base OTLP export subtree.
* Section 4.2 :func:`test_otel_cli_destinations` - destination lifecycle + overrides.
* Section 4.3 :func:`test_otel_cli_labels_and_metric_classes` - labels, classes, intervals.
* Section 4.4 :func:`test_otel_cli_stats_groups` - stats-group lifecycle.
* Section 5.2 :func:`test_otel_metric_class_disable_during_capture` - class and
  sub-class disable mid-capture.
* Section 5.3 :func:`test_otel_plus_gnmi_parallel` - OTEL and gNMI coexistence.

Explicit coverage row that skips with a precise blocker:

* Section 4.2 client-certificate leg :func:`test_otel_cli_destination_client_certificate`
  - needs a DUT client certificate the OTEL harness does not stage today.

The two groups differ in what they need from the setup. Every Section 4 item is
CLI-only: the DUT is configured and read back through the ``applied`` datastore,
and no OTEL collector reachability is required. The Section 5 items exercise the
data plane, so they take the suite fixture and assert on what actually reaches the
collector.
"""

import time

import pytest

import ngts.tools.test_utils.allure_utils as allure
from ngts.constants.constants import GnmiConsts
from ngts.nvos_constants.constants_nvos import ApiType, TelemetryConsts
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.RandomizationTool import RandomizationTool
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.system.gnmi.constants import GnmiMode, GnmicErr
from ngts.tests_nvos.system.gnmi.GnmiClient import GnmiClient
from ngts.tests_nvos.system.gnmi.helpers import (
    gnmi_response_failed,
    verify_msg_not_in_out_or_err,
)
from ngts.tests_nvos.system.gnmi.mapping.helpers import parse_gnmic_flat_output
from ngts.tests_nvos.system.telemetry.otel.constants import (
    OtelCliTestConst,
    OtelCollectorConst,
    OtelFunctionalTestConst,
)
from ngts.tests_nvos.system.telemetry.otel.helpers import (
    assert_metric_values_sane,
    assert_no_otlp_connection_to_collector,
    assert_nvos_export_resumed,
    assert_nvos_export_stopped,
    assert_nvos_metric_names_for_enablement,
    assert_otlp_payload_valid,
    collect_otlp_metrics_window,
    collection_window_for_samples,
    collector_receivers_for,
    enable_nvos_telemetry_families,
    otel_series_for_interface,
    percentage_deviation,
    root_only_enablement,
)
from ngts.tests_nvos.system.telemetry.otel.cumulus.cli_unset_coverage import (
    get_interface_telemetry_labels_applied,
    swp_interfaces_for_unset,
)
from ngts.tests_nvos.system.telemetry.otel.cumulus.tls import (
    generate_tls_certs_locally,
    install_ca_on_dut,
)
from ngts.tests_nvos.system.telemetry.otel.nvos_cli_helpers import (
    create_resource,
    enable_family_export,
    enable_family_phy_class,
    family_node,
    interface_label_node,
    sample_interval_node,
    set_leaf,
    show_applied,
    stage_otlp_destination,
    stats_group_node,
    transport_node,
    unset_path,
    verify_family_defaults,
    verify_key_absent,
    verify_key_present,
    verify_leaf,
)

pytestmark = [
    pytest.mark.system,
    pytest.mark.otel,
]

_ENABLED = TelemetryConsts.State.ENABLED.value
_DISABLED = TelemetryConsts.State.DISABLED.value


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture(autouse=True)
def otel_cli_nvue_default():
    """Default the API to NVUE for every test here, restoring it on the way out.

    Autouse function-scoped fixtures are set up before the requested ones, so the
    preconditions below stage their configuration over NVUE, while ``random_api``
    (set up after this) still selects the API for the test body itself. Going
    through ``force_api`` instead of assigning ``TestToolkit.tested_api`` keeps the
    override from leaking into later modules in the same session.
    """
    with TestToolkit.force_api(ApiType.NVUE):
        yield


@pytest.fixture
def otel_cli_clean_telemetry(engines):
    """Section 4.1 precondition: clean OTEL state on the DUT.

    Unsets the whole ``system telemetry`` subtree so step 1 can assert factory
    defaults regardless of what ran before this test.
    """
    with allure.step("Precondition - unset the whole system telemetry subtree"):
        System().telemetry.unset(
            apply=True, ask_for_confirmation=True, dut_engine=engines.dut
        ).verify_result()


@pytest.fixture
def otel_cli_export_enabled(engines):
    """Sections 4.2-4.4 precondition: OTLP export top-level state enabled and applied.

    Function-scoped because the shared per-test ``clear_config`` teardown resets the
    DUT to factory defaults after every test.
    """
    with allure.step("Precondition - enable and apply OTLP export top-level state"):
        System().telemetry.export.otlp.set(
            TelemetryConsts.STATE,
            _ENABLED,
            apply=True,
            ask_for_confirmation=True,
            dut_engine=engines.dut,
        ).verify_result()


@pytest.fixture(scope="module")
def otel_cli_ca_certificate(engines) -> str:
    """Section 4.2 precondition: a CA certificate imported on the DUT.

    Returns the NVUE certificate id usable as a destination ``certificate`` override.
    Importing a CA is an NVUE *action*, not configuration, so it survives the per-test
    config clear and can stay module-scoped. Module scope also means this runs before
    the function-scoped ``otel_cli_nvue_default``, hence the local pin.
    """
    with TestToolkit.force_api(ApiType.NVUE):
        material = generate_tls_certs_locally(san_ip=engines.dut.ip)
        install_ca_on_dut(engines.dut, material)
    return OtelCollectorConst.OTEL_TLS_CA_NAME


# =============================================================================
# Section 4.1 - base OTLP export configuration
# =============================================================================


@pytest.mark.parametrize("transport", OtelCliTestConst.ALL_TRANSPORTS)
def test_otel_cli_base_export_config(
    engines, otel_cli_clean_telemetry, transport, random_api
):
    """Section 4.1 - default visibility, base enablement, and leaf/subtree/root unset.

    Runs once per OTLP transport (``grpc``, ``http``); the ``encoding`` steps exist
    only on ``http``.
    """
    dut = engines.dut
    system = System()
    otlp = system.telemetry.export.otlp
    node = transport_node(system, transport)
    default_insecure = OtelCliTestConst.DEFAULT_INSECURE_BY_TRANSPORT[transport]

    with allure.step("Step 1 - OTLP subtree is present and shows the default state"):
        parsed = show_applied(otlp, dut)
        verify_key_present(parsed, TelemetryConsts.STATE)
        verify_leaf(parsed, TelemetryConsts.STATE, TelemetryConsts.Defaults.OTLP_STATE)
        verify_leaf(show_applied(node, dut), TelemetryConsts.INSECURE, default_insecure)

    with allure.step(
        f"Step 2 - stage OTLP state enabled and {transport} insecure enabled, then apply"
    ):
        set_leaf(otlp, dut, TelemetryConsts.STATE, _ENABLED)
        set_leaf(node, dut, TelemetryConsts.INSECURE, _ENABLED, apply=True)

    with allure.step(f"Step 3 - OTLP state and {transport} insecure both read enabled"):
        verify_leaf(show_applied(otlp, dut), TelemetryConsts.STATE, _ENABLED)
        verify_leaf(show_applied(node, dut), TelemetryConsts.INSECURE, _ENABLED)

    with allure.step(f"Step 4 - unset the {transport} insecure leaf, then apply"):
        unset_path(node, dut, TelemetryConsts.INSECURE, apply=True)

    with allure.step(
        f"Step 5 - {transport} insecure is back to default and the rest of OTLP is intact"
    ):
        verify_leaf(show_applied(node, dut), TelemetryConsts.INSECURE, default_insecure)
        verify_leaf(show_applied(otlp, dut), TelemetryConsts.STATE, _ENABLED)

    with allure.step(f"Step 6 - re-set {transport} insecure enabled, then apply"):
        set_leaf(node, dut, TelemetryConsts.INSECURE, _ENABLED, apply=True)
        verify_leaf(show_applied(node, dut), TelemetryConsts.INSECURE, _ENABLED)

    with allure.step(f"Step 7 - unset the whole {transport} subtree, then apply"):
        unset_path(node, dut, apply=True)

    with allure.step(
        f"Step 8 - {transport} subtree is back to default and top-level OTLP state remains"
    ):
        verify_leaf(show_applied(node, dut), TelemetryConsts.INSECURE, default_insecure)
        parsed = show_applied(otlp, dut)
        verify_key_present(parsed, TelemetryConsts.STATE)
        verify_leaf(parsed, TelemetryConsts.STATE, _ENABLED)

    with allure.step("Step 9 - restore the base OTLP configuration from step 2"):
        set_leaf(otlp, dut, TelemetryConsts.STATE, _ENABLED)
        set_leaf(node, dut, TelemetryConsts.INSECURE, _ENABLED, apply=True)
        verify_leaf(show_applied(node, dut), TelemetryConsts.INSECURE, _ENABLED)

    if transport == OtelCliTestConst.TRANSPORT_WITH_ENCODING:
        json_encoding = TelemetryConsts.Encoding.JSON.value
        with allure.step(
            f"Steps 10-11 - set http encoding {json_encoding}, apply, and verify"
        ):
            set_leaf(node, dut, TelemetryConsts.ENCODING, json_encoding, apply=True)
            verify_leaf(show_applied(node, dut), TelemetryConsts.ENCODING, json_encoding)

        with allure.step(
            "Steps 12-13 - unset http encoding, apply, and verify it returns to default"
        ):
            unset_path(node, dut, TelemetryConsts.ENCODING, apply=True)
            verify_leaf(
                show_applied(node, dut),
                TelemetryConsts.ENCODING,
                TelemetryConsts.Defaults.HTTP_ENCODING,
            )

    with allure.step("Step 14 - unset the whole OTLP subtree, then apply"):
        unset_path(otlp, dut, apply=True)

    with allure.step("Step 15 - the OTLP subtree returns to the default state"):
        verify_leaf(
            show_applied(otlp, dut),
            TelemetryConsts.STATE,
            TelemetryConsts.Defaults.OTLP_STATE,
        )
        verify_leaf(show_applied(node, dut), TelemetryConsts.INSECURE, default_insecure)


# =============================================================================
# Section 4.2 - destinations
# =============================================================================


@pytest.mark.parametrize("transport", OtelCliTestConst.ALL_TRANSPORTS)
def test_otel_cli_destinations(
    engines, otel_cli_export_enabled, otel_cli_ca_certificate, transport, random_api
):
    """Section 4.2 - destination create/show/remove plus per-destination overrides.

    Runs once per OTLP transport (``grpc``, ``http``); the destination address
    family is selected at random, as the plan specifies.
    """
    dut = engines.dut
    system = System()
    certificate = otel_cli_ca_certificate
    stats_group = OtelCliTestConst.STATS_GROUP_1
    port = OtelCliTestConst.NON_DEFAULT_DESTINATION_PORT
    destinations = transport_node(system, transport).destination

    with allure.step("Select the destination address family"):
        dest_type = RandomizationTool.select_random_value(
            list(OtelCliTestConst.ALL_DEST_TYPES)
        ).get_returned_value()
        dest_id = OtelCliTestConst.DEST_ID_BY_TYPE[dest_type]

    with allure.step("Step 1 - the destination subtree is present and shows no entries"):
        verify_key_absent(show_applied(destinations, dut), dest_id)

    with allure.step(f"Step 2 - create stats group {stats_group}, then apply and verify"):
        group = stats_group_node(system, stats_group)
        interface_stats = family_node(group, TelemetryConsts.INTERFACE_STATS)
        enable_family_export(interface_stats, dut)
        set_leaf(
            interface_stats,
            dut,
            TelemetryConsts.SAMPLE_INTERVAL,
            OtelCliTestConst.STAGED_SAMPLE_INTERVAL_SEC,
            apply=True,
        )
        verify_leaf(
            show_applied(interface_stats.export, dut), TelemetryConsts.STATE, _ENABLED
        )
        verify_leaf(
            show_applied(interface_stats, dut),
            TelemetryConsts.SAMPLE_INTERVAL,
            OtelCliTestConst.STAGED_SAMPLE_INTERVAL_SEC,
        )

    with allure.step(
        "Step 3 - stage the destination with port, certificate and stats-group overrides"
    ):
        node = stage_otlp_destination(
            system,
            dut,
            transport,
            dest_id,
            port=port,
            certificate=certificate,
            stats_group=stats_group,
        )

    with allure.step("Step 4 - the list and single-destination views show the overrides"):
        verify_key_present(show_applied(destinations, dut), dest_id)
        parsed = show_applied(node, dut)
        verify_leaf(parsed, TelemetryConsts.PORT, port)
        verify_leaf(parsed, TelemetryConsts.CERTIFICATE, certificate)
        verify_leaf(parsed, TelemetryConsts.STATS_GROUP, stats_group)

    with allure.step("Step 5 - unset the destination port, then apply"):
        unset_path(node, dut, TelemetryConsts.PORT, apply=True)

    with allure.step(
        "Step 6 - the port override is gone and the destination keeps its other overrides"
    ):
        parsed = show_applied(node, dut)
        # A destination `port` is an override with no default of its own, so unsetting
        # it drops the key and the destination falls back to the transport-level port.
        verify_key_absent(parsed, TelemetryConsts.PORT)
        verify_leaf(parsed, TelemetryConsts.CERTIFICATE, certificate)
        verify_leaf(parsed, TelemetryConsts.STATS_GROUP, stats_group)
        verify_key_present(show_applied(destinations, dut), dest_id)

    with allure.step("Step 7 - unset the destination stats-group binding, then apply"):
        unset_path(node, dut, TelemetryConsts.STATS_GROUP, apply=True)

    with allure.step(
        "Step 8 - the destination remains and the stats-group binding is gone"
    ):
        verify_key_present(show_applied(destinations, dut), dest_id)
        verify_key_absent(show_applied(node, dut), TelemetryConsts.STATS_GROUP)

    with allure.step("Step 9 - unset the destination object, then apply"):
        unset_path(node, dut, apply=True)

    with allure.step("Step 10 - the removed destination is no longer listed"):
        verify_key_absent(show_applied(destinations, dut), dest_id)

    with allure.step("Step 11 - re-stage the destination configuration, apply and verify"):
        node = stage_otlp_destination(
            system,
            dut,
            transport,
            dest_id,
            port=port,
            certificate=certificate,
            stats_group=stats_group,
        )
        verify_leaf(show_applied(node, dut), TelemetryConsts.PORT, port)
        verify_key_present(show_applied(destinations, dut), dest_id)

    with allure.step("Step 12 - unset the whole destination subtree, then apply"):
        unset_path(destinations, dut, apply=True)

    with allure.step("Step 13 - the destination subtree no longer lists the entry"):
        verify_key_absent(show_applied(destinations, dut), dest_id)


def test_otel_cli_destination_client_certificate():
    """Section 4.2 (client-certificate leg) - per-destination mutual-TLS override.

    Intended flow (plan Section 4.2 step 3): stage a destination with
    ``client-certificate <id>``, apply, and verify the override in the
    single-destination view.

    Blocked: needs a client certificate imported on the DUT. The generation script
    (``cumulus/certs_gen_scripts/entity_client_cert_generate.sh``) and the NVUE import
    flow both exist, but the import helper is private to
    ``cumulus/gnmi_dialout_mtls.py``; promoting it to a shared helper is a follow-up
    (OTEL suite owner).
    """
    pytest.skip(
        "Section 4.2 client-certificate leg needs a DUT client certificate; the OTEL "
        "harness stages a CA and a server certificate only, and the entity-cert import "
        "helper is private to the gnmi dial-out module (OTEL suite owner)."
    )


# =============================================================================
# Section 4.3 - labels, metric classes and sample intervals
# =============================================================================


@pytest.mark.parametrize("metric_class", OtelCliTestConst.ALL_METRIC_CLASSES)
def test_otel_cli_labels_and_metric_classes(
    engines, otel_cli_export_enabled, metric_class, random_api
):
    """Section 4.3 - labels, metric-class enablement, sample intervals and their unsets.

    Runs once per metric family. The ``class phy`` steps apply only to the families
    that expose one; the two closing steps cover the family-level ``export`` and
    whole-subtree unsets.
    """
    dut = engines.dut
    system = System()
    labels = system.telemetry.label
    label_id = OtelCliTestConst.SYSTEM_LABEL_ID
    iface_label_id = OtelCliTestConst.INTERFACE_LABEL_ID
    node = family_node(system.telemetry, metric_class)
    interval_node = sample_interval_node(metric_class, node)
    has_phy_class = metric_class in OtelCliTestConst.METRIC_CLASSES_WITH_PHY
    other_families = tuple(
        family for family in OtelCliTestConst.ALL_METRIC_CLASSES if family != metric_class
    )

    if metric_class == TelemetryConsts.PLATFORM_STATS:
        staged_interval = OtelCliTestConst.STAGED_PLATFORM_SAMPLE_INTERVAL_SEC
        default_interval = TelemetryConsts.Defaults.PLATFORM_EXPORT_SAMPLE_INTERVAL
    else:
        staged_interval = OtelCliTestConst.STAGED_SAMPLE_INTERVAL_SEC
        default_interval = TelemetryConsts.Defaults.STATS_SAMPLE_INTERVAL

    interface = swp_interfaces_for_unset(dut, is_nvos=True)[0]

    with allure.step(f"Step 1 - the label and {metric_class} subtrees show their defaults"):
        verify_key_absent(show_applied(labels, dut), label_id)
        verify_family_defaults(node, dut, metric_class)

    with allure.step(f"Step 2 - set a system label and an {interface} interface label"):
        create_resource(labels, label_id)
        set_leaf(
            labels.resources_dict[label_id],
            dut,
            TelemetryConsts.DESCRIPTION,
            OtelCliTestConst.SYSTEM_LABEL_DESCRIPTION,
        )
        set_leaf(
            interface_label_node(interface, iface_label_id),
            dut,
            TelemetryConsts.DESCRIPTION,
            OtelCliTestConst.INTERFACE_LABEL_DESCRIPTION,
            apply=True,
        )

    with allure.step(
        f"Step 3 - enable {metric_class} export, set sample-interval {staged_interval}"
        f"{' and enable class phy' if has_phy_class else ''}, then apply"
    ):
        enable_family_export(node, dut)
        if has_phy_class:
            enable_family_phy_class(node, dut)
        set_leaf(
            interval_node,
            dut,
            TelemetryConsts.SAMPLE_INTERVAL,
            staged_interval,
            apply=True,
        )

    with allure.step(
        f"Step 4 - labels, {metric_class} classes and intervals read back as configured "
        "and no other metric family changed"
    ):
        verify_leaf(
            show_applied(labels.resources_dict[label_id], dut),
            TelemetryConsts.DESCRIPTION,
            OtelCliTestConst.SYSTEM_LABEL_DESCRIPTION,
        )
        verify_leaf(
            show_applied(interface_label_node(interface, iface_label_id), dut),
            TelemetryConsts.DESCRIPTION,
            OtelCliTestConst.INTERFACE_LABEL_DESCRIPTION,
        )
        verify_leaf(show_applied(node.export, dut), TelemetryConsts.STATE, _ENABLED)
        verify_leaf(
            show_applied(interval_node, dut),
            TelemetryConsts.SAMPLE_INTERVAL,
            staged_interval,
        )
        if has_phy_class:
            verify_leaf(
                show_applied(node.cls.phy, dut), TelemetryConsts.STATE, _ENABLED
            )
        for family in other_families:
            with allure.independent_step(f"{family} is untouched at its defaults"):
                verify_family_defaults(
                    family_node(system.telemetry, family), dut, family
                )

    with allure.step(f"Step 5 - unset the {metric_class} sample-interval, then apply"):
        unset_path(interval_node, dut, TelemetryConsts.SAMPLE_INTERVAL, apply=True)

    with allure.step(
        "Step 6 - the sample-interval is back to default and the rest of the subtree remains"
    ):
        verify_leaf(
            show_applied(interval_node, dut),
            TelemetryConsts.SAMPLE_INTERVAL,
            default_interval,
        )
        verify_leaf(show_applied(node.export, dut), TelemetryConsts.STATE, _ENABLED)

    if has_phy_class:
        with allure.step(f"Step 7 - unset the {metric_class} class phy subtree, then apply"):
            unset_path(node.cls.phy, dut, apply=True)

        with allure.step(
            "Step 8 - class phy is back to default while the family export state is unchanged"
        ):
            verify_leaf(
                show_applied(node.cls.phy, dut),
                TelemetryConsts.STATE,
                TelemetryConsts.Defaults.STATS_STATE,
            )
            verify_leaf(show_applied(node.export, dut), TelemetryConsts.STATE, _ENABLED)

    with allure.step("Step 9 - unset the system and interface labels, then apply"):
        unset_path(labels.resources_dict[label_id], dut)
        unset_path(interface_label_node(interface, iface_label_id), dut, apply=True)

    with allure.step("Step 10 - both label objects are gone"):
        verify_key_absent(show_applied(labels, dut), label_id)
        verify_key_absent(
            get_interface_telemetry_labels_applied(dut, interface), iface_label_id
        )

    with allure.step(
        f"Step 11 - unset the {metric_class} export node, then apply "
        "(family-level export unset)"
    ):
        unset_path(node.export, dut, apply=True)
        verify_leaf(
            show_applied(node.export, dut),
            TelemetryConsts.STATE,
            TelemetryConsts.Defaults.STATS_STATE,
        )

    with allure.step(
        f"Step 12 - re-enable {metric_class} export, then unset the whole family subtree"
    ):
        enable_family_export(node, dut, apply=True)
        unset_path(node, dut, apply=True)
        verify_family_defaults(node, dut, metric_class)


# =============================================================================
# Section 4.4 - stats groups
# =============================================================================


def test_otel_cli_stats_groups(engines, otel_cli_export_enabled, random_api):
    """Section 4.4 - stats-group creation, show, update and unset across two groups."""
    dut = engines.dut
    system = System()
    groups = system.telemetry.stats_group
    group_1_id = OtelCliTestConst.STATS_GROUP_1
    group_2_id = OtelCliTestConst.STATS_GROUP_2

    group_1 = stats_group_node(system, group_1_id)
    group_2 = stats_group_node(system, group_2_id)
    group_1_interface = family_node(group_1, TelemetryConsts.INTERFACE_STATS)
    group_1_platform = family_node(group_1, TelemetryConsts.PLATFORM_STATS)
    group_2_ib_router = family_node(group_2, TelemetryConsts.IB_ROUTER_STATS)
    group_2_peer_port = family_node(group_2, TelemetryConsts.PEER_PORT_STATS)

    with allure.step("Step 1 - the stats-group subtree lists neither group"):
        parsed = show_applied(groups, dut)
        verify_key_absent(parsed, group_1_id)
        verify_key_absent(parsed, group_2_id)

    with allure.step(f"Step 2 - create {group_1_id} with interface and platform stats"):
        enable_family_export(group_1_interface, dut)
        set_leaf(
            group_1_interface,
            dut,
            TelemetryConsts.SAMPLE_INTERVAL,
            OtelCliTestConst.STAGED_SAMPLE_INTERVAL_SEC,
        )
        enable_family_phy_class(group_1_interface, dut)
        enable_family_export(group_1_platform, dut, apply=True)

    with allure.step(f"Step 3 - create {group_2_id} with ib-router and peer-port stats"):
        enable_family_export(group_2_ib_router, dut)
        enable_family_export(group_2_peer_port, dut, apply=True)

    with allure.step("Step 4 - both stats-groups show their configured fields"):
        parsed = show_applied(groups, dut)
        verify_key_present(parsed, group_1_id)
        verify_key_present(parsed, group_2_id)
        verify_leaf(
            show_applied(group_1_interface.export, dut), TelemetryConsts.STATE, _ENABLED
        )
        verify_leaf(
            show_applied(group_1_interface, dut),
            TelemetryConsts.SAMPLE_INTERVAL,
            OtelCliTestConst.STAGED_SAMPLE_INTERVAL_SEC,
        )
        verify_leaf(
            show_applied(group_1_interface.cls.phy, dut),
            TelemetryConsts.STATE,
            _ENABLED,
        )
        verify_leaf(
            show_applied(group_1_platform.export, dut), TelemetryConsts.STATE, _ENABLED
        )
        verify_leaf(
            show_applied(group_2_ib_router.export, dut), TelemetryConsts.STATE, _ENABLED
        )
        verify_leaf(
            show_applied(group_2_peer_port.export, dut), TelemetryConsts.STATE, _ENABLED
        )

    with allure.step(f"Step 5 - unset the {group_1_id} interface-stats sample-interval"):
        unset_path(group_1_interface, dut, TelemetryConsts.SAMPLE_INTERVAL, apply=True)

    with allure.step(
        f"Step 6 - the sample-interval is back to default and the rest of {group_1_id} remains"
    ):
        verify_leaf(
            show_applied(group_1_interface, dut),
            TelemetryConsts.SAMPLE_INTERVAL,
            TelemetryConsts.Defaults.STATS_SAMPLE_INTERVAL,
        )
        verify_leaf(
            show_applied(group_1_interface.export, dut), TelemetryConsts.STATE, _ENABLED
        )
        verify_leaf(
            show_applied(group_1_platform.export, dut), TelemetryConsts.STATE, _ENABLED
        )

    with allure.step(f"Step 7 - unset the {group_1_id} interface-stats class phy subtree"):
        unset_path(group_1_interface.cls.phy, dut, apply=True)

    with allure.step(f"Step 8 - class phy is back to default and {group_1_id} remains"):
        verify_leaf(
            show_applied(group_1_interface.cls.phy, dut),
            TelemetryConsts.STATE,
            TelemetryConsts.Defaults.STATS_STATE,
        )
        verify_key_present(show_applied(groups, dut), group_1_id)

    with allure.step(f"Step 9 - unset the {group_1_id} interface-stats export state"):
        unset_path(group_1_interface.export, dut, TelemetryConsts.STATE, apply=True)

    with allure.step("Step 10 - the export state returns to its default"):
        verify_leaf(
            show_applied(group_1_interface.export, dut),
            TelemetryConsts.STATE,
            TelemetryConsts.Defaults.STATS_STATE,
        )

    with allure.step(f"Step 11 - unset {group_1_id}, then apply"):
        unset_path(group_1, dut, apply=True)

    with allure.step(f"Step 12 - {group_1_id} is no longer present"):
        parsed = show_applied(groups, dut)
        verify_key_absent(parsed, group_1_id)
        verify_key_present(parsed, group_2_id)

    with allure.step("Step 13 - unset the whole stats-group subtree, then apply"):
        unset_path(groups, dut, apply=True)

    with allure.step(f"Step 14 - {group_2_id} is no longer present"):
        verify_key_absent(show_applied(groups, dut), group_2_id)


# =============================================================================
# Section 5.2 - metric-class and sub-class disable during capture
# =============================================================================


@pytest.fixture
def data_path_transport(otel_suite_single):
    """Randomized OTLP transport, with the collector set up to receive on it.

    The draw lives in a fixture so the collector is put back on teardown even if
    the test fails mid-flight; see :func:`collector_receivers_for` for why the
    http receiver is not simply left running on every collector.
    """
    transport = RandomizationTool.select_random_value(
        list(OtelFunctionalTestConst.DATA_PATH_TRANSPORTS)
    ).get_returned_value()
    with collector_receivers_for(otel_suite_single.primary, transport=transport):
        yield transport


def test_otel_metric_class_disable_during_capture(
    engines, devices, otel_suite_single, is_ib_router, tmp_path, random_api,
    data_path_transport
):
    """Section 5.2 steps 4-5 - disable a class, then a sub-class of another class.

    Steps 1-3 (OTEL values against the matching CLI fields) stay with the ported
    suite, which on NVOS means ``test05_otel_platform_stats_validation`` for the
    platform sensors and ``test_interface_qos_05_interface_in_broadcast_pkts`` for
    the interface counters - neither is ``cumulus_only``, so both run on NVOS. The
    remaining ported per-counter validators are ungated too but skip themselves
    here: they resolve Cumulus ``ether_stats``/``tc_tx`` metric names, of which NVOS
    exports none (``coexistence.INTERFACE_GNMI_TO_OTEL_METRIC_NVOS`` holds
    ``if_in_broadcast_pkts`` alone). This test therefore adds the two disable flows
    plus the baseline they need.

    After each disable the *whole* expected metric-name set is re-asserted rather
    than just checking the disabled names disappeared. That is what proves the
    disable was clean: the families left enabled must still export exactly their own
    metrics, and their values must stay sane.

    The export goes to the single destination the item's precondition calls for,
    over a transport picked at random per run by ``data_path_transport``, which
    also points the collector at that transport.
    """
    dut = engines.dut
    system = System()
    local_dir = str(tmp_path)
    destination_ip = otel_suite_single.primary_ip
    transport = data_path_transport
    base_class = OtelCollectorConst.TELEMETRY_BASE_CLASS
    kept_family = OtelFunctionalTestConst.DISABLED_SUB_CLASS_FAMILY
    disabled_family = OtelFunctionalTestConst.DISABLED_FAMILY
    sub_class = OtelFunctionalTestConst.DISABLED_SUB_CLASS
    kept_family_node = family_node(system.telemetry, kept_family)
    disabled_family_node = family_node(system.telemetry, disabled_family)
    kept_metric_prefix = OtelFunctionalTestConst.KEPT_FAMILY_METRIC_PREFIX
    platform_classes = frozenset(TelemetryConsts.PLATFORM_CLASSES)

    # Sized on the fast family so the window spans the plan's three sample
    # intervals; the 70s floor still lets the 60s platform family emit a sample.
    window_sec = collection_window_for_samples(
        OtelCollectorConst.INTERFACE_STATS_SAMPLE_INTERVAL_SEC,
        OtelFunctionalTestConst.MIN_SAMPLE_INTERVALS,
    )

    def collect(label: str):
        return collect_otlp_metrics_window(
            otel_suite_single.primary, local_dir, label=label, wait_sec=window_sec
        )

    def assert_exported(families_to_classes, actual_names, *, collector_label: str):
        assert_nvos_metric_names_for_enablement(
            engines,
            devices,
            local_dir,
            actual_metric_names=actual_names,
            enablement=root_only_enablement(
                families_to_classes, destination_ips=(destination_ip,)
            ),
            destination_ip=destination_ip,
            is_ib_router=is_ib_router,
            collector_label=collector_label,
        )

    with allure.step(
        f"Precondition - enable {kept_family} (with class {sub_class}) and "
        f"{disabled_family} over {transport}"
    ):
        enable_nvos_telemetry_families(
            dut,
            destination_ip,
            (kept_family, disabled_family),
            collector_ips=(destination_ip,),
            use_stats_group=False,
            transport=transport,
            clear_other_transports=True,
        )

    with allure.step("Baseline - both families export their full metric set"):
        names, docs, _ = collect("5-2-baseline")
        assert_otlp_payload_valid(docs, collector_label="baseline")
        assert_exported(
            {
                kept_family: {base_class, sub_class},
                disabled_family: platform_classes,
            },
            names,
            collector_label="baseline (both families)",
        )
        assert_metric_values_sane(
            docs, metric_name_prefix=kept_metric_prefix, collector_label="baseline"
        )

    with allure.step(f"Step 4 - disable the {disabled_family} class, then apply"):
        set_leaf(
            disabled_family_node.export, dut, TelemetryConsts.STATE, _DISABLED, apply=True
        )
        verify_leaf(
            show_applied(disabled_family_node.export, dut), TelemetryConsts.STATE, _DISABLED
        )

    with allure.step(
        f"Step 4 - {disabled_family} metrics stop while {kept_family} keeps exporting cleanly"
    ):
        names, docs, _ = collect("5-2-family-disabled")
        assert_exported(
            {kept_family: {base_class, sub_class}},
            names,
            collector_label=f"{disabled_family} disabled",
        )
        assert_metric_values_sane(
            docs,
            metric_name_prefix=kept_metric_prefix,
            collector_label=f"{disabled_family} disabled",
        )

    with allure.step(f"Step 5 - disable the {kept_family} class {sub_class}, then apply"):
        set_leaf(
            kept_family_node.cls.phy, dut, TelemetryConsts.STATE, _DISABLED, apply=True
        )
        verify_leaf(
            show_applied(kept_family_node.cls.phy, dut), TelemetryConsts.STATE, _DISABLED
        )

    with allure.step(
        f"Step 5 - the {sub_class} sub-class metrics stop while the rest of "
        f"{kept_family} keeps exporting cleanly"
    ):
        names, docs, _ = collect("5-2-sub-class-disabled")
        assert_exported(
            {kept_family: {base_class}},
            names,
            collector_label=f"{kept_family} class {sub_class} disabled",
        )
        assert_metric_values_sane(
            docs,
            metric_name_prefix=kept_metric_prefix,
            collector_label=f"{kept_family} class {sub_class} disabled",
        )


# =============================================================================
# Section 5.3 - OTEL and gNMI in parallel
# =============================================================================


@pytest.mark.gnmi
def test_otel_plus_gnmi_parallel(
    engines, devices, otel_suite_single, tmp_path, random_api
):
    """Section 5.3 - OTEL export and a gNMI subscription coexist without interfering.

    On NVOS both paths are served by the same telemetry core (UMF), so the risk this
    covers is one path disturbing the other. The test keeps a gNMI subscription live
    across an OTLP collection window, compares the one counter both paths expose,
    and then toggles the OTEL export off and on underneath the live subscription.

    Transport is gRPC: unlike 5.1 and 5.2 the item's precondition names no
    transport, so the suite default is used and the plan wants clarifying.
    """
    dut = engines.dut
    system = System()
    otlp = system.telemetry.export.otlp
    local_dir = str(tmp_path)
    interface = swp_interfaces_for_unset(dut, is_nvos=True)[0]
    otel_metric = OtelFunctionalTestConst.SHARED_METRIC_OTEL_NAME
    gnmi_prefix = OtelFunctionalTestConst.SHARED_METRIC_GNMI_PREFIX_FMT.format(
        interface=interface
    )
    gnmi_leaf = OtelFunctionalTestConst.SHARED_METRIC_GNMI_LEAF
    window_sec = collection_window_for_samples(
        OtelCollectorConst.INTERFACE_STATS_SAMPLE_INTERVAL_SEC,
        OtelFunctionalTestConst.MIN_SAMPLES_IN_WINDOW,
    )

    with allure.step("Step 1 - enable interface-stats export and verify it is active"):
        enable_nvos_telemetry_families(
            dut,
            otel_suite_single.primary_ip,
            (TelemetryConsts.INTERFACE_STATS,),
            collector_ips=(otel_suite_single.primary_ip,),
            use_stats_group=False,
        )
        verify_leaf(show_applied(otlp, dut), TelemetryConsts.STATE, _ENABLED)

    gnmi_client = GnmiClient(
        dut.ip,
        GnmiConsts.GNMI_DEFAULT_PORT,
        devices.dut.default_username,
        devices.dut.default_password,
        cmd_time=OtelFunctionalTestConst.GNMI_CLIENT_CMD_TIME_SEC,
    )

    with allure.step("Step 2 - enable the gNMI server and confirm it answers"):
        system.gnmi_server.set(
            GnmiConsts.GNMI_STATE_FIELD,
            GnmiConsts.GNMI_STATE_ENABLED,
            dut_engine=dut,
            apply=True,
        ).verify_result()
        capabilities_out, capabilities_err = gnmi_client.gnmic_capabilities(
            skip_cert_verify=True
        )
        assert not gnmi_response_failed(capabilities_out, capabilities_err), (
            f"The gNMI server does not answer on {dut.ip} after being enabled, so "
            f"OTEL/gNMI coexistence cannot be exercised:\n"
            f"  stderr: {capabilities_err}\n  stdout: {capabilities_out}"
        )

    def subscribe_shared_metric():
        """Start a live gNMI stream on the shared counter."""
        _, _, _, process = gnmi_client.gnmic_subscribe(
            prefix=gnmi_prefix,
            path=gnmi_leaf,
            mode=GnmiMode.STREAM,
            flat=True,
            skip_cert_verify=True,
            keep_session_alive=True,
        )
        time.sleep(OtelFunctionalTestConst.GNMI_STREAM_SETTLE_SEC)
        return process

    def close_and_assert_gnmi_streamed(process, *, context: str) -> str:
        """Stop the stream and assert it delivered the counter without error."""
        out, err = gnmi_client.close_session_and_get_out_and_err(process)
        with allure.step(f"Validate the gNMI subscription streamed data ({context})"):
            verify_msg_not_in_out_or_err(GnmicErr.AUTH_FAIL, out, err)
            # Both streams are inspected: gnmic surfaces some failures on stdout, and
            # the non-empty check below would otherwise count an error line as data.
            assert not gnmi_response_failed(out, err), (
                f"gNMI subscription failed while {context}:\n"
                f"  stderr: {err}\n  stdout: {out}"
            )
            assert (out or "").strip(), (
                f"gNMI subscription produced no data while {context}; "
                f"subscribed to {gnmi_prefix}/{gnmi_leaf}"
            )
        return out

    with allure.step("Steps 2-3 - stream gNMI while OTLP batches are collected"):
        gnmi_process = subscribe_shared_metric()
        names, docs, _ = collect_otlp_metrics_window(
            otel_suite_single.primary, local_dir, label="5-3-parallel", wait_sec=window_sec
        )
        gnmi_out = close_and_assert_gnmi_streamed(
            gnmi_process, context="OTLP was being collected in parallel"
        )
        assert_otlp_payload_valid(docs, collector_label="parallel with gNMI")
        assert otel_metric in names, (
            f"{otel_metric} was not exported over OTLP while gNMI was subscribed, so "
            f"the two paths cannot be compared. Collected {len(names)} metric name(s)."
        )

    with allure.step(
        f"Step 4 - {otel_metric} agrees across the OTEL and gNMI paths for {interface}"
    ):
        gnmi_raw = parse_gnmic_flat_output(gnmi_out)
        if gnmi_raw is None:
            pytest.fail(
                f"Could not parse a {gnmi_leaf} value out of the gNMI stream for "
                f"{interface}:\n{gnmi_out}"
            )
        try:
            gnmi_value = float(gnmi_raw)
        except ValueError:
            pytest.fail(
                f"gNMI returned a non-numeric {gnmi_leaf} for {interface}: {gnmi_raw!r}"
            )
        otel_values = otel_series_for_interface(docs, otel_metric, interface)
        assert otel_values, (
            f"{otel_metric} has no OTLP datapoint carrying "
            f"{OtelFunctionalTestConst.INTERFACE_ATTRIBUTE_KEY}={interface}."
        )
        # The gNMI value is the last streamed update, so it is compared against the
        # newest OTLP sample rather than the window mean: on a rising counter the
        # mean of a 70s window sits well below a reading taken at its end, which
        # would show up as a deviation even when both paths agree. The counter is
        # monotonic within a window, so the newest sample is the largest.
        otel_value = max(otel_values)
        deviation = percentage_deviation(gnmi_value, otel_value)
        tolerance = OtelFunctionalTestConst.SHARED_METRIC_TOLERANCE_PERCENT
        assert deviation <= tolerance, (
            f"{otel_metric} disagrees across the two paths for {interface}: "
            f"gnmi={gnmi_value}, otel={otel_value} (newest of {len(otel_values)} "
            f"sample(s)), deviation={deviation:.1f}% > {tolerance}%"
        )

    with allure.step("Step 5 - disable the OTEL export while the gNMI stream is live"):
        gnmi_process = subscribe_shared_metric()
        set_leaf(otlp, dut, TelemetryConsts.STATE, _DISABLED, apply=True)
        verify_leaf(show_applied(otlp, dut), TelemetryConsts.STATE, _DISABLED)
        assert_nvos_export_stopped(
            otel_suite_single.primary,
            local_dir,
            label="5-3-otel-disabled",
            wait_sec=window_sec,
        )
        assert_no_otlp_connection_to_collector(
            dut,
            collector_ip=otel_suite_single.primary_ip,
            transport=TelemetryConsts.GRPC,
        )
        close_and_assert_gnmi_streamed(
            gnmi_process, context="the OTEL export was disabled underneath it"
        )

    with allure.step("Step 6 - re-enable the OTEL export while the gNMI stream is live"):
        gnmi_process = subscribe_shared_metric()
        set_leaf(otlp, dut, TelemetryConsts.STATE, _ENABLED, apply=True)
        verify_leaf(show_applied(otlp, dut), TelemetryConsts.STATE, _ENABLED)
        assert_nvos_export_resumed(
            otel_suite_single.primary,
            local_dir,
            label="5-3-otel-resumed",
            wait_sec=window_sec,
        )
        close_and_assert_gnmi_streamed(
            gnmi_process, context="the OTEL export was re-enabled underneath it"
        )
