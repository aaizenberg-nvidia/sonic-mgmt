from ngts.nvos_tools.ib.InterfaceConfiguration import Port
from ngts.nvos_tools.ib.InterfaceConfiguration import nvos_consts as ib_consts
from ngts.tests_nvos.interfaces.nvl_port import helpers as nvl_port_helpers
from ngts.tools.test_utils import allure_utils as allure


def wait_and_verify_link(
    ports: list[Port.Port],
    timeout: int,
) -> None:
    port_names: list[str] = [port.name for port in ports]
    with allure.step(f"Wait for port state to be up on ports {port_names} (timeout={timeout}s)"):
        for port in ports:
            port.interface.wait_for_port_state(
                ib_consts.NvosConsts.LINK_STATE_UP,
                timeout=timeout,
            ).verify_result()
    with allure.step(f"Verify link diagnostics on ports {port_names}"):
        nvl_port_helpers.verify_link_diagnostic(ports)
