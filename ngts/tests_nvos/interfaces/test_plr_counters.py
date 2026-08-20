from __future__ import annotations

from allure_commons.types import AttachmentType
from decimal import Decimal, InvalidOperation
from typing import Any
import logging
import pytest
import json
import re

from ngts.nvos_constants.constants_nvos import ApiType, OutputFormat
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.tests_nvos.system.gnmi import helpers as gnmi_helpers
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.tests_nvos.system.gnmi.constants import GnmiMode
from ngts.tools.test_utils import allure_utils as allure
from ngts.constants.constants import GnmiConsts
from ngts.ngts_types import DevicesT, EnginesT
from ngts.tests_nvos.constants import MINUTE

from . import helpers

logger = logging.getLogger(__name__)

NEW_PLR_FIELDS: frozenset[str] = frozenset({
    "plr-rcv-block-uncorrectable-by-prev",
    "plr-rcv-block-flushed-due-to-csn",
    "plr-rcv-new-nack-received",
    "plr-xmit-new-nack-sent",
})

LEGACY_CLI_PLR_FIELDS: frozenset[str] = frozenset({
    "plr-xmit-retry-events-within-t-sec-max",
    "plr-rcv-uncorrectable-code",
    "plr-xmit-retry-events",
    "plr-xmit-retry-codes",
    "plr-bw-loss-percent",
    "plr-rcv-codes-err",
    "plr-sync-events",
    "plr-xmit-codes",
    "plr-codes-loss",
    "plr-rcv-codes",
})

CLI_PLR_FIELDS: frozenset[str] = LEGACY_CLI_PLR_FIELDS | NEW_PLR_FIELDS
PLR_FIELD_PATTERN = re.compile(r"\bplr-[a-z0-9-]+\b")  # SpellCheck-ignoreWord:bplr
PLR_GNMI_STATE_PATH = "phy/infiniband/plr/state"


def _assert_expected_fields(actual_fields: frozenset[str], expected_fields: frozenset[str], context: str) -> None:
    """Assert that the actual PLR field set exactly matches the expected set.

    Args:
        actual_fields: PLR fields reported by the tested interface.
        expected_fields: Complete field set required by the tested output.
        context: Interface and output context included in assertion errors.
    """
    missing = sorted(expected_fields - actual_fields)
    unexpected = sorted(actual_fields - expected_fields)
    assert actual_fields == expected_fields, \
        f"{context}: expected exactly {len(expected_fields)} PLR fields; missing={missing}, unexpected={unexpected}"


def _validate_cli_output(output: str, context: str) -> None:
    """Extract PLR field names from CLI text and verify the complete 14-field set."""
    actual_fields = frozenset(PLR_FIELD_PATTERN.findall(output))
    _assert_expected_fields(actual_fields, CLI_PLR_FIELDS, context)


def _validate_numeric_values(plr_values: dict[str, Any], context: str) -> None:
    """Verify PLR values are finite and non-negative, with integer counter values."""
    for field, value in plr_values.items():
        try:
            numeric_value = Decimal(str(value))
        except (InvalidOperation, ValueError):
            pytest.fail(f"{context}: {field} has a non-numeric value: {value!r}")

        assert numeric_value.is_finite() and numeric_value >= 0, f"{context}: {field} must be a finite, non-negative value; got {value!r}"
        if field != "plr-bw-loss-percent":
            assert numeric_value == numeric_value.to_integral_value(), f"{context}: counter {field} must be an integer; got {value!r}"


def _validate_json_output(output: str, context: str) -> dict[str, Any]:
    """Parse PHY-detail JSON and validate its PLR field set and values.

    Args:
        output: JSON output returned by the PHY-detail show command.
        context: Interface context included in assertion errors.

    Returns:
        Mapping of the 14 validated PLR field names to their reported values.
    """
    output_dict = OutputParsingTool.parse_json_str_to_dictionary(output).verify_result()
    plr_values = {
        field: value
        for field, value in output_dict.items()
        if isinstance(field, str) and field.startswith("plr-")
    }
    _assert_expected_fields(frozenset(plr_values), CLI_PLR_FIELDS, context)
    _validate_numeric_values(plr_values, context)
    return plr_values


def _attachment_name(context: str, output_format: str) -> str:
    """Build a filesystem-safe Allure attachment name for one PLR result."""
    normalized_context = re.sub(r"[^a-zA-Z0-9_.-]+", "-", context).strip("-")
    return f"{normalized_context}-plr-counters.{output_format}"


@pytest.mark.timeout(5 * MINUTE, func_only=True)
def test_plr_counters_cli_and_json(engines: EnginesT) -> None:
    """Verify the complete PLR field contract in CLI and JSON output.

    Test flow:
        1. Select five random active ACPs.
        2. Read each port's PHY-detail output in the default NVUE CLI format.
        3. Read the same PHY-detail data in JSON format.
        4. Validate and attach both outputs independently for every selected port.

    Expected outcome:
        Every CLI and JSON result contains exactly the ten legacy and four new PLR fields.
        Every JSON value is finite and non-negative; all fields except
        ``plr-bw-loss-percent`` contain integer values.
    """
    _, ports = helpers.get_random_ports(engines.dut, num_of_ports_to_select=5)

    with TestToolkit.force_api(ApiType.NVUE):
        with allure.step("Validate PLR CLI and JSON output on five random active ACPs"):
            for port in ports:
                with allure.independent_step(f"Validate {port.name} PLR fields in CLI and JSON output"):
                    context = f"interface {port.name}"
                    phy_detail = port.interface.link.phy.detail
                    with allure.independent_step(f"Validate {context} PLR fields in CLI output"):
                        cli_output = phy_detail.show(output_format=OutputFormat.auto)
                        allure.attach(_attachment_name(context, "txt"), cli_output, attachment_type=AttachmentType.TEXT)
                        _validate_cli_output(cli_output, context)

                    with allure.independent_step(f"Validate {context} PLR fields in JSON output"):
                        json_output = phy_detail.parse_show()
                        attachment_name = _attachment_name(context, "json")
                        allure.attach(attachment_name, json.dumps(json_output, indent=2), attachment_type=AttachmentType.JSON)
                        plr_values = _validate_json_output(json_output, context)
                    logger.info("%s PLR values: %s", context, plr_values)


def _validate_gnmi_plr_tree(engines: EnginesT, devices: DevicesT, prefix: str, context: str) -> None:
    """Subscribe once to the four new PLR leaves and validate their notification.

    Args:
        engines: Test engines used to run the gNMI client and reach the DUT.
        devices: Device metadata providing the DUT gNMI credentials.
        prefix: Interface PLR state-tree prefix to subscribe to.
        context: Interface context used for the attachment and assertion errors.
    """
    notifications = gnmi_helpers.run_gnmi_client_and_parse_multi_path_json_output(
        engines=engines,
        devices=devices,
        target_ip=engines.dut.ip,
        target_port=GnmiConsts.GNMI_DEFAULT_PORT,
        prefix=prefix,
        paths=sorted(NEW_PLR_FIELDS),
        mode=GnmiMode.ONCE,
    )
    allure.attach(
        _attachment_name(context, "json"),
        json.dumps(notifications, indent=2, sort_keys=True),
        attachment_type=AttachmentType.JSON,
    )
    assert notifications, f"{context}: gNMI returned no PLR notifications for prefix {prefix!r}"
    gnmi_helpers.validate_notification_has_prefix_and_leaves(
        notifications=notifications,
        expected_prefix=prefix,
        expected_leaves=NEW_PLR_FIELDS,
    )


@pytest.mark.gnmi
@pytest.mark.timeout(5 * MINUTE, func_only=True)
def test_plr_counters_gnmi(engines: EnginesT, devices: DevicesT) -> None:
    """Verify availability of the four new PLR leaves over gNMI.

    Test flow:
        1. Select five random active ACPs.
        2. Build the interface PLR state-tree prefix for each selected port.
        3. Perform a gNMI ONCE subscription requesting the four new PLR leaves.
        4. Attach the raw notification and validate its prefix and leaf names.

    Expected outcome:
        Each selected ACP returns one notification under its requested PLR prefix
        containing all four new PLR leaves.
    """
    _, ports = helpers.get_random_ports(engines.dut, num_of_ports_to_select=5)

    with allure.step("Validate new PLR gNMI leaves on five random active ACPs"):
        for port in ports:
            context = f"interface {port.name}"
            prefix = f"interfaces/interface[name={port.name}]/{PLR_GNMI_STATE_PATH}"
            with allure.independent_step(f"Validate {context} PLR gNMI leaves"):
                _validate_gnmi_plr_tree(engines, devices, prefix, context)
