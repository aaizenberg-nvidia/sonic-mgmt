import argparse
import logging
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ngts.nvos_tools.infra.JenkinsTool import JenkinsBuildResult, JenkinsTool  # noqa: E402

logger = logging.getLogger(__name__)

TPM_PROJECT_JOB_PATH = "/nbu-sws-nos/job"
TPM_JOB_NAME = "TPM_Build"
SUCCESS_RESULT = "SUCCESS"
DEFAULT_POLL_INTERVAL = 30
DEFAULT_TIMEOUT = 600
DEFAULT_MAIL_RECIPIENT = "ncaro@nvidia.com,nadeemn@nvidia.com,hmechlovich@nvidia.com,aromashin@nvidia.com,nvilder@nvidia.com"
NOGA_DUT_DESCRIPTION = "dut"
NOGA_SWITCH_TYPE = "Switch"


def _load_noga_helpers() -> tuple[Callable[..., Any], Any]:
    try:
        from infra.tools.general_constants.constants import NogaConstants
        from infra.tools.topology_tools.nogaq import get_noga_resource
    except ImportError as err:
        raise RuntimeError("Unable to import NOGA helpers. Set PYTHONPATH to include devts.") from err

    return get_noga_resource, NogaConstants


def _is_dut_switch(noga_resource: dict[str, Any]) -> bool:
    description = str(noga_resource.get("DESCRIPTION", "")).strip().lower()
    return description == NOGA_DUT_DESCRIPTION


def _select_switch(switches: list[dict[str, Any]]) -> dict[str, Any]:
    for switch in switches:
        if _is_dut_switch(switch):
            return switch
    return switches[0]


def get_switch_address_from_setup(
    setup_name: str,
    resource_client: Callable[..., Any] | None = None,
    noga_constants: Any | None = None,
) -> str:
    """
    Resolve the DUT switch management address from a NOGA setup.

    Args:
        setup_name: NOGA setup name.
        resource_client: Optional NOGA get_resources client, mainly for tests.
        noga_constants: Optional NOGA constants module/class, mainly for tests.

    Returns:
        str: Switch management IP address.

    Raises:
        RuntimeError: If the setup, switch, or switch address cannot be resolved from NOGA.
    """
    setup_name = setup_name.strip()
    if not setup_name:
        raise ValueError("setup_name must not be empty")

    if resource_client is None or noga_constants is None:
        default_resource_client, default_noga_constants = _load_noga_helpers()
        resource_client = resource_client or default_resource_client
        noga_constants = noga_constants or default_noga_constants

    resources = resource_client(resource_name=setup_name)
    if not resources:
        raise RuntimeError(f"No devices found in NOGA for setup '{setup_name}'")

    switches = [resource for resource in resources if resource.get(noga_constants.TYPE_TITLE) == NOGA_SWITCH_TYPE]
    if not switches:
        raise RuntimeError(f"No switches found in NOGA for setup '{setup_name}'")

    switch = _select_switch(switches)
    switch_address = str(switch.get(noga_constants.IP, "")).strip()
    if switch_address:
        return switch_address

    raise RuntimeError(f"No switch IP found in NOGA for setup '{setup_name}'")


def generate_tpm_attestation(
    setup_name: str,
    mail_recipient: str = DEFAULT_MAIL_RECIPIENT,
    poll_interval: int = DEFAULT_POLL_INTERVAL,
    timeout: int = DEFAULT_TIMEOUT,
    client: JenkinsTool | None = None,
) -> JenkinsBuildResult:
    """
    Trigger TPM attestation generation and wait for the Jenkins pipeline result.

    Args:
        setup_name: NOGA setup name used to resolve the DUT switch IP address.
        mail_recipient: Comma-separated mail recipient list.
        poll_interval: Seconds between Jenkins status polls.
        timeout: Max seconds to wait for queue start and build completion.
        client: Optional Jenkins client, mainly for tests.

    Returns:
        JenkinsBuildResult: Final Jenkins build result.

    Raises:
        RuntimeError: If Jenkins rejects the trigger or the build does not finish successfully.
        TimeoutError: If the queue item or build does not complete within timeout.
    """
    setup_name = setup_name.strip()
    if not setup_name:
        raise ValueError("setup_name must not be empty")
    if not mail_recipient:
        mail_recipient = DEFAULT_MAIL_RECIPIENT

    def get_job_type_from_setup(setup_name: str) -> str:
        """
        Returns the job type based on the setup name.

        Args:
            setup_name: Name of the setup.

        Returns:
            str: "JULIET" if 'juliet' is in the setup name (case-insensitive), else "ROSALIND".
        """
        if "juliet" in setup_name.lower():
            return "JULIET"
        return "ROSALIND"

    job_type = get_job_type_from_setup(setup_name)

    switch_address = get_switch_address_from_setup(setup_name)
    jenkins_client = client or JenkinsTool(project_job_path=TPM_PROJECT_JOB_PATH)
    params = {
        "SWITCH_ADDRESS": switch_address,
        "MAIL_RECIPIENT": mail_recipient,
        "JOB_TYPE": job_type,

    }

    logger.info("Triggering Jenkins job '%s' for setup '%s' switch '%s'", TPM_JOB_NAME, setup_name, switch_address)
    result = jenkins_client.trigger_and_wait(TPM_JOB_NAME, params, poll_interval=poll_interval, timeout=timeout)
    if result.result != SUCCESS_RESULT:
        raise RuntimeError(f"Jenkins job '{TPM_JOB_NAME}' finished with result '{result.result}'. Build URL: {result.build_url}")

    logger.info("Jenkins job '%s' finished successfully. Build URL: %s", TPM_JOB_NAME, result.build_url)
    return result


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Trigger TPM attestation generation through Jenkins")
    parser.add_argument("-s", "--setup-name", "--setup_name", dest="setup_name", required=True, help="NOGA setup name")
    parser.add_argument(
        "--mail-recipient",
        default=DEFAULT_MAIL_RECIPIENT,
        help="Comma-separated mail recipient list",
    )
    parser.add_argument("--poll-interval", type=int, default=DEFAULT_POLL_INTERVAL, help="Seconds between Jenkins status polls")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="Max seconds to wait for Jenkins queue/build completion")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = _parse_args(argv)
    try:
        result = generate_tpm_attestation(
            setup_name=args.setup_name,
            mail_recipient=args.mail_recipient,
            poll_interval=args.poll_interval,
            timeout=args.timeout,
        )
    except Exception as err:
        logger.error("Failed to generate TPM attestation: %s", err)
        return 1

    logger.info("TPM attestation job passed: build #%s %s", result.build_number, result.build_url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
