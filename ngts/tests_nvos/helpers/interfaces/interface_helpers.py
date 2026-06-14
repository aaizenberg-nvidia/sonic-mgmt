from ngts.ngts_types import LinkDiagnosticT
from ngts.nvos_tools.ib.InterfaceConfiguration import Port
from ngts.nvos_tools.ib.InterfaceConfiguration import nvos_consts as nv_consts
from ngts.tests_nvos.interfaces.nvl_port import helpers as nvl_port_helpers
from ngts.tools.test_utils import allure_utils as allure
from ngts.tests_nvos.helpers import redmine_helpers


ARE_BUGS_ACTIVE: bool = (
    redmine_helpers.is_bug_active(5034554) or
    redmine_helpers.is_bug_active(5041195) or
    redmine_helpers.is_bug_active(5044735)
)


def wait_and_verify_link(
    ports: list[Port.Port],
    timeout: int,
    additional_expected_link_diagnostic_status: LinkDiagnosticT | list[LinkDiagnosticT] | None = None,
    logical_state: str = nv_consts.NvosConsts.LINK_LOG_STATE_INITIALIZE,
) -> None:
    port_names: list[str] = [port.name for port in ports]

    if ARE_BUGS_ACTIVE:
        timeout *= 2

    with allure.step(f"Wait for port state to be up on ports {port_names} (timeout={timeout}s)"):
        for port in ports:
            port.interface.wait_for_port_state(
                nv_consts.NvosConsts.LINK_STATE_UP,
                timeout=timeout,
                logical_state=logical_state,
            ).verify_result()
    with allure.step(f"Verify link diagnostics on ports {port_names}"):
        nvl_port_helpers.verify_link_diagnostic(
            ports,
            additional_link_diagnostic_status=additional_expected_link_diagnostic_status,
        )
