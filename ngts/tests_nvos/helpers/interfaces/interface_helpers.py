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


def wait_and_verify_link_on_range(
    ports_range: Port.Port,
    diagnostic_ports: list[Port.Port],
    timeout: int,
    additional_expected_link_diagnostic_status: LinkDiagnosticT | list[LinkDiagnosticT] | None = None,
    logical_state: str = nv_consts.NvosConsts.LINK_LOG_STATE_INITIALIZE,
    exclude_ports: list[str] | None = None,
) -> None:
    """
    Range counterpart of wait_and_verify_link, for a range object such as Port('acp1-288').

    Waits broadly and checks diagnostics narrowly: one `nv show interface` per poll covers every
    port in the range, while diagnostics are verified only on `diagnostic_ports` - normally the
    linked pair under test. Diagnostics have no all-interfaces equivalent, and a per-port
    diagnostics show for a 288-port range is not viable.

    Ports listed in `exclude_ports` are skipped by the wait - pass the ports that were already
    down before the operation, since they have no link partner and would otherwise hold the whole
    budget open.

    `timeout` is the plain device budget: the range wait applies the active-bug doubling itself.
    """
    nvl_port_helpers.wait_for_acp_ports_state(
        ports_range,
        nv_consts.NvosConsts.LINK_STATE_UP,
        timeout=timeout,
        logical_state=logical_state,
        exclude_ports=exclude_ports,
    )
    with allure.step(f"Verify link diagnostics on {[port.name for port in diagnostic_ports]}"):
        nvl_port_helpers.verify_link_diagnostic(
            diagnostic_ports,
            additional_link_diagnostic_status=additional_expected_link_diagnostic_status,
        )
