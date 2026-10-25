from ngts.tools.test_utils import allure_utils as allure
import logging

from ngts.tests_nvos.helpers.alarms.system_alarms_gnmi_helpers import get_gnmi_alarms, validate_gnmi_alarms
from ngts.tests_nvos.helpers.alarms.system_alarms_otel_helpers import get_otel_alarms, validate_otel_alarms
from ngts.tests_nvos.helpers.alarms.system_alarm_nvue_helpers import validate_nvue_alarms

logger = logging.getLogger(__name__)


def get_all_alarms_once(system, gnmiclient, otel_alarms_collector, tmp_path):
    """
    Get all alarms from all sources: system, gNMI, OTEL.
    """
    system_output = system.alarms.parse_show()
    gnmi_output = get_gnmi_alarms(gnmiclient)
    otel_output = get_otel_alarms(otel_alarms_collector, tmp_path)
    return system_output, gnmi_output, otel_output


def validate_all_alarms(alarm_origin_name: str, alarm_types: str | list[str], expected_present: bool,
                        system_output=None, gnmi_output=None, otel_output=None,
                        system=None, gnmiclient=None, otel_alarms_collector=None, tmp_path=None):
    """
    Validate the alarms of `alarm_origin_name` in NVUE, gNMI and OTEL.

    Each backend is checked against its `*_output` snapshot when one is passed, and validated once;
    otherwise it is read live through its client and retried until it matches or times out.
    Snapshots from `get_all_alarms_once` let several origins be checked against the same moment.
    """
    with allure.step(f"Validate all alarms for {alarm_origin_name} and {alarm_types}"):
        logger.info(f"Validating NVUE alarms for {alarm_origin_name} and {alarm_types}")
        validate_nvue_alarms(system=system, system_alarms=system_output, alarm_types=alarm_types,
                             alarm_origin_name=alarm_origin_name,
                             expected_present=expected_present)
        logger.info(f"Validating gNMI alarms for {alarm_origin_name} and {alarm_types}")
        validate_gnmi_alarms(gnmi_client=gnmiclient,
                             alarm_origin_name=alarm_origin_name,
                             alarm_types=alarm_types, expected_present=expected_present, gnmi_alarms=gnmi_output)
        logger.info(f"Validating OTEL alarms for {alarm_origin_name} and {alarm_types}")
        validate_otel_alarms(collector=otel_alarms_collector, local_dir=str(tmp_path),
                             alarm_origin_name=alarm_origin_name,
                             alarm_types=alarm_types,
                             expected_present=expected_present, snapshot=otel_output)


def validate_all_alarms_once(system, gnmiclient, otel_alarms_collector, tmp_path, alarm_origin_name: str, alarm_types: str | list[str], expected_present: bool):
    """
    Take one snapshot of NVUE, gNMI and OTEL and validate the alarms of `alarm_origin_name` against it, with no retry.

    Unlike a live `validate_all_alarms`, nothing is retried, so call it only once the fault has already been
    observed to settle (for example after the health state changed); all three backends are then held to the
    same moment.
    """
    with allure.step(f"Validate all alarms once for {alarm_origin_name} and {alarm_types}"):
        logger.info(f"Getting all alarms once for {alarm_origin_name} and {alarm_types}")
        system_output, gnmi_output, otel_output = get_all_alarms_once(system, gnmiclient, otel_alarms_collector, tmp_path)
        validate_all_alarms(alarm_origin_name, alarm_types, expected_present, system_output, gnmi_output, otel_output, gnmiclient=gnmiclient)
