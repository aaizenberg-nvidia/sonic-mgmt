"""
Phy-recovery test helpers: NVL5 (serdes-eq) vs NVL6 (recovery-status / link-down-timeout).

NVL7 CPO - HLD open questions resolved by dev (Ben/Omer, 2026-07-17):
  Q1 new-leaf defaults : literal echo (no FW-resolved display); defaults policy/re-iteration=
                         fw-default, timers=0  -> PhyRecoveryConsts.NVL7_NEW_LEAF_DEFAULTS
  Q2 self-recovery     : port stays DOWN after apply; toggle-up is primary -> nvl7_wait_trunk_up
  Q3 enable gate       : recovery-status enabled is the master enable -> nvl7_enable_recovery
  Q4 go-once trigger   : wired; pending real-HW validation only -> nvl7_trigger_recovery
  Q5 clear command     : only the fae-scoped clear resets debug counters -> nvl7_clear_debug_counters
  Q6 pruned-set error  : tentative fragments (Omer to confirm) -> PhyRecoveryConsts.NVL7_ERR_*
"""
import logging
import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, Tuple, Type

from retry.api import retry_call

from ngts.ngts_types import DevicesT
from ngts.nvos_constants.constants_nvos import NvosConst
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import NvosConsts, PhyRecoveryConsts
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port
from ngts.nvos_tools.infra.Fae import Fae
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.RandomizationTool import RandomizationTool
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.tests_nvos.interfaces.nvl_port.helpers import setup_nvl_speed
from ngts.tools.test_utils import allure_utils as allure
from ngts.tools.test_utils.allure_utils import step as allure_step

logger = logging.getLogger(__name__)

# Trunk link-up wait budget after the primary toggle-up (NVL7 ports stay down after a
# phy-recovery apply - see nvl7_wait_trunk_up). 6 tries x 30s.
NVL7_TRUNK_LINK_UP_TRIES = 6
NVL7_TRUNK_LINK_UP_DELAY = 30

PHY_RECOVERY_DEFAULT_TIMEOUT = 100
PHY_RECOVERY_NVL6_DEFAULT_LINK_DOWN_TIMEOUT = 150
PHY_RECOVERY_TIMEOUT_STEP = 10
PHY_RECOVERY_HIGHER_TIMEOUT_MIN = 110
PHY_RECOVERY_HIGHER_TIMEOUT_MAX = 200
PHY_RECOVERY_LOWER_TIMEOUT_MIN = 10
PHY_RECOVERY_LOWER_TIMEOUT_MAX = 90
PHY_RECOVERY_VERIFY_RECHECK_SECONDS = 30
PHY_RECOVERY_VALIDATE_RETRIES = 6
PHY_RECOVERY_VALIDATE_RETRY_DELAY_SECONDS = 30

# Phy-recovery is not supported on 400G..
PHY_RECOVERY_UNSUPPORTED_LINK_SPEEDS = ('400G',)


@dataclass(frozen=True)
class PhyRecoveryFlow:
    """
    Immutable mode/timeout parameters for one phy-recovery flavor (NVL5 vs NVL6/NVL7).
    Obtain via PhyRecoveryFlow.for_device() or phy_recovery_test_profile(devices).flow.
    """

    label: str
    modes: Tuple[str, ...]
    recovery_field: str
    timeout_field: str
    default_timeout_when_enabled: int
    # The configured mode value that show echoes as 'disabled' (NVL5: fw-default; NVL6/NVL7: auto).
    disabled_alias_mode: str

    @classmethod
    def for_device(cls, devices: DevicesT) -> 'PhyRecoveryFlow':
        return phy_recovery_test_profile(devices).flow


class PhyRecoveryTestProfile(ABC):
    """
    Per-ASIC test behavior for phy-recovery (parallel to CRCSTokenManager vs CRDTTokenManager).

    Subclasses implement default-config validation and supply a PhyRecoveryFlow for mode/timeout tests.
    """

    label: str

    def is_nvl6(self) -> bool:
        return self.label == 'NVL6'

    @classmethod
    @abstractmethod
    def matches(cls, devices: DevicesT) -> bool:
        pass

    @property
    @abstractmethod
    def flow(self) -> PhyRecoveryFlow:
        pass

    @abstractmethod
    def verify_default_phy_recovery_show(self, devices: DevicesT, selected_port: Fae) -> None:
        """Assert `nv show ... phy-recovery -o json` matches defaults for this profile."""


class Nvl5PhyRecoveryTestProfile(PhyRecoveryTestProfile):
    """QTM4 / NVL5: serdes-eq-mode and serdes-eq-timeout."""

    label = 'NVL5'

    @classmethod
    def matches(cls, devices: DevicesT) -> bool:
        return devices.dut.asic_type in (NvosConst.QTM4, NvosConst.NVL5)

    @property
    def flow(self) -> PhyRecoveryFlow:
        return PhyRecoveryFlow(
            label=self.label,
            modes=tuple(PhyRecoveryConsts.NVL5_MODES),
            recovery_field=PhyRecoveryConsts.SerdesEQ.MODE,
            timeout_field=PhyRecoveryConsts.SerdesEQ.TIMEOUT,
            default_timeout_when_enabled=PHY_RECOVERY_DEFAULT_TIMEOUT,
            disabled_alias_mode=PhyRecoveryConsts.FW_DEFAULT,
        )

    def verify_default_phy_recovery_show(self, devices: DevicesT, selected_port: Fae) -> None:
        with allure_step("Check default config"):
            output_fae_port = OutputParsingTool.parse_json_str_to_dictionary(
                selected_port.port.interface.link.phy_recovery.show()
            ).get_returned_value()
            expected = PhyRecoveryConsts.DEFAULT_PHY_RECOVERY_DICT
            filtered_out = {k: v for k, v in output_fae_port.items() if k in expected}
            ValidationTool.compare_dictionaries(filtered_out, expected).verify_result()


class Nvl6PhyRecoveryTestProfile(PhyRecoveryTestProfile):
    """NVL6: recovery-status and link-down-timeout (+ NVL6 default JSON tree)."""

    label = 'NVL6'

    @classmethod
    def matches(cls, devices: DevicesT) -> bool:
        return devices.dut.asic_type == NvosConst.NVL6

    @property
    def flow(self) -> PhyRecoveryFlow:
        return PhyRecoveryFlow(
            label=self.label,
            modes=tuple(PhyRecoveryConsts.NVL6_MODES),
            recovery_field=PhyRecoveryConsts.RECOVERY_STATUS,
            timeout_field=PhyRecoveryConsts.LINK_DOWN_TIMEOUT,
            default_timeout_when_enabled=PHY_RECOVERY_NVL6_DEFAULT_LINK_DOWN_TIMEOUT,
            disabled_alias_mode=PhyRecoveryConsts.AUTO,
        )

    def verify_default_phy_recovery_show(self, devices: DevicesT, selected_port: Fae) -> None:
        with allure_step("Check default config"):
            output_fae_port = OutputParsingTool.parse_json_str_to_dictionary(
                selected_port.port.interface.link.phy_recovery.show()
            ).get_returned_value()
            expected = devices.dut.default_phy_recovery_attributes
            filtered_out = {k: v for k, v in output_fae_port.items() if k in expected}
            ValidationTool.compare_dictionaries(filtered_out, expected).verify_result()


# NOTE: there is deliberately NO Nvl7PhyRecoveryTestProfile. The shared mode/timeout flows never
# run on NVL7 today (test_phy_recovery_attributes skips all NVL7; the access flow's speed setup
# skips on 200G-only Portia; the trunk flow skips all Juliet-derived devices) - NVL7 coverage
# lives in the dedicated test_nvl7_phy_recovery_* tests. Per dev (2026-07-17) NVL7 inherits the
# NVL6 mode/timeout semantics (recovery-status master enable, group-vs-local NVL6 rules), so if a
# shared-flow NVL7 run is ever wanted, add a profile AND toggle-up waits inside the flow (any
# phy-recovery apply admin-DOWNs NVL7 ports and they stay down).
_PROFILE_REGISTRY: Tuple[Type[PhyRecoveryTestProfile], ...] = (
    Nvl6PhyRecoveryTestProfile,
    Nvl5PhyRecoveryTestProfile,
)


def phy_recovery_test_profile(devices: DevicesT) -> PhyRecoveryTestProfile:
    for profile_cls in _PROFILE_REGISTRY:
        if profile_cls.matches(devices):
            return profile_cls()
    raise ValueError(
        f'Unsupported ASIC for phy-recovery test profile: {devices.dut.asic_type}'
    )


def phy_recovery_apply_mode(fae_port: Fae, flow: PhyRecoveryFlow, mode: str) -> None:
    fae_port.port.interface.link.phy_recovery.set(
        flow.recovery_field, mode, apply=True, ask_for_confirmation=True
    ).verify_result()


def phy_recovery_apply_timeout(fae_port: Fae, flow: PhyRecoveryFlow, timeout: int) -> None:
    fae_port.port.interface.link.phy_recovery.set(
        flow.timeout_field, timeout, apply=True, ask_for_confirmation=True
    ).verify_result()


def phy_recovery_apply_neg_type(fae_port: Fae, neg_type: str) -> None:
    fae_port.port.interface.link.phy_recovery.set(
        PhyRecoveryConsts.RECOVERY_NEGATIVE_TYPE, neg_type, apply=True, ask_for_confirmation=True
    ).verify_result()


def validate_mode_set(
    selected_port,
    mode: str,
    flow: PhyRecoveryFlow,
    timeout: Optional[int] = None,
):
    with allure.step(f"Validate mode {mode} is applied"):
        output = OutputParsingTool.parse_json_str_to_dictionary(
            selected_port.port.interface.link.phy_recovery.show()
        ).get_returned_value()
        expected_mode = PhyRecoveryConsts.DISABLED if mode == flow.disabled_alias_mode else mode
        ValidationTool.compare_values(output[flow.recovery_field], expected_mode).verify_result()
        if timeout is not None:
            ValidationTool.compare_values(int(output[flow.timeout_field]), timeout).verify_result()


def validate_default_config(selected_port, devices: DevicesT):
    phy_recovery_test_profile(devices).verify_default_phy_recovery_show(devices, selected_port)


def verify_phy_recovery_config(
    selected_port: Fae,
    flow: PhyRecoveryFlow,
    mode: str,
    timeout: Optional[int] = None,
    retries: int = PHY_RECOVERY_VALIDATE_RETRIES,
    delay: int = PHY_RECOVERY_VALIDATE_RETRY_DELAY_SECONDS,
) -> None:
    retry_call(
        validate_mode_set,
        fargs=[selected_port, mode, flow],
        fkwargs={'timeout': timeout},
        exceptions=AssertionError,
        tries=retries,
        delay=delay,
    )


def verify_phy_recovery_config_after_wait(
    selected_port: Fae,
    flow: PhyRecoveryFlow,
    mode: str,
    expected_timeout: int,
    wait_seconds: int,
) -> None:
    verify_phy_recovery_config(selected_port, flow, mode, expected_timeout)
    with allure.step(f"Wait {wait_seconds}s and re-verify timeout is still {expected_timeout}"):
        time.sleep(wait_seconds)
        verify_phy_recovery_config(selected_port, flow, mode, expected_timeout)


# ==================== NVL7 CPO trunk-port PHY recovery helpers ====================
# HLD: HLD_NVOS_CPO_PHY_RECOVERY_NVL7. The 5 new leaves + "counters debug" node exist
# only on NVL7 CPO trunk ports (sw*). All helpers below are FAE-namespace and NVL7-only.


def is_nvl7(devices: DevicesT) -> bool:
    return devices.dut.asic_type == NvosConst.NVL7


def nvl7_parse_phy_recovery(fae_port: Fae) -> dict:
    """Return ``nv show fae interface <intf> link phy-recovery`` as a dict."""
    return OutputParsingTool.parse_json_str_to_dictionary(
        fae_port.port.interface.link.phy_recovery.show()
    ).get_returned_value()


def nvl7_enable_recovery(
    fae_port: Fae,
    policy: Optional[str] = None,
    apply: bool = True,
) -> None:
    """
    Enable NVL7 recovery via ``recovery-status enabled`` - the master enable (same as NVL6).

    Per dev (2026-07-17) ``recovery-policy-config`` only selects behavior when recovery is on;
    policy alone enables nothing. Pass ``policy`` to stage a policy selection in the SAME apply
    (default: leave policy untouched).
    """
    phy_recovery = fae_port.port.interface.link.phy_recovery
    policy_note = f", {PhyRecoveryConsts.RECOVERY_POLICY_CONFIG}={policy}" if policy is not None else ""
    with allure.step(
        f"Enable NVL7 recovery ({PhyRecoveryConsts.RECOVERY_STATUS}={PhyRecoveryConsts.ENABLED}{policy_note})"
    ):
        if policy is not None:
            phy_recovery.set(
                PhyRecoveryConsts.RECOVERY_POLICY_CONFIG, policy, apply=False
            ).verify_result()
        phy_recovery.set(
            PhyRecoveryConsts.RECOVERY_STATUS, PhyRecoveryConsts.ENABLED,
            apply=apply, ask_for_confirmation=True,
        ).verify_result()


def nvl7_set_new_leaf(fae_port: Fae, leaf: str, value, apply: bool = True, **kwargs) -> None:
    """Set one NVL7 new phy-recovery leaf (``nv set fae interface <intf> link phy-recovery <leaf> <value>``)."""
    with allure.step(f"Set {leaf} to {value} on {fae_port.port.name}"):
        fae_port.port.interface.link.phy_recovery.set(
            leaf, value, apply=apply, ask_for_confirmation=True, **kwargs
        ).verify_result()


def nvl7_verify_new_leaves(
    selected_port: Fae,
    expected: dict,
    retries: int = PHY_RECOVERY_VALIDATE_RETRIES,
    delay: int = PHY_RECOVERY_VALIDATE_RETRY_DELAY_SECONDS,
) -> None:
    """
    Verify (with retries; show returns OPERATIONAL values after apply) that each leaf in
    ``expected`` reads back the expected value (string-compared to tolerate int/str show output).
    """

    def _check():
        output = nvl7_parse_phy_recovery(selected_port)
        mismatched = {
            leaf: output.get(leaf)
            for leaf, value in expected.items()
            if str(output.get(leaf)) != str(value)
        }
        assert not mismatched, (
            f"Leaf mismatch on {selected_port.port.name}: expected {expected}, mismatched {mismatched}"
        )

    with allure.step(f"Verify new leaves {expected} on {selected_port.port.name}"):
        retry_call(_check, exceptions=AssertionError, tries=retries, delay=delay)


def nvl7_stage_and_apply_leaves(fae_port: Fae, pairs) -> None:
    """
    Stage several phy-recovery leaf sets on ``fae_port`` and apply them in a single 'nv config apply'.

    ``pairs`` is an ordered iterable of (leaf, value). Batching several leaves per apply limits
    the number of trunk link bounces. Works for a single port or a bulk port range (Fae).
    """
    pairs = list(pairs)
    phy_recovery = fae_port.port.interface.link.phy_recovery
    for index, (leaf, value) in enumerate(pairs):
        is_last = index == len(pairs) - 1
        phy_recovery.set(leaf, value, apply=is_last, ask_for_confirmation=is_last).verify_result()


def nvl7_trunk_port_type() -> str:
    """The port-type token used to select NVL trunk (sw*) ports (device-configurable, default 'nvl')."""
    return getattr(TestToolkit.get_device(), 'nvl_port_type', 'nvl')


def nvl7_trunk_port_names(
    requested_state: str = NvosConsts.LINK_STATE_UP,
    fallback_all_states: bool = True,
) -> list:
    """
    Return the names of NVL7 CPO trunk (sw*) ports currently present, or [] if none exist.

    Single selection primitive for NVL7 trunk ports. Parallels
    ``select_random_nvl_port_name(devices, 'sw')`` but returns the FULL list (needed to build
    bulk port ranges) and tolerates "no ports" by returning [] instead of raising IndexError.
    The device base defines the CPO trunk naming (PortiaCpoSwitch.nvl_trunk_ports_list); selection
    is kept dynamic here for robustness (works regardless of the static list / live link state).
    """
    port_type = nvl7_trunk_port_type()

    def _query(state):
        result = RandomizationTool.select_random_ports(
            requested_ports_state=state, requested_ports_type=port_type,
            interface_type='sw', num_of_ports_to_select=0,
        )
        # A no-match select returns a FAILED ResultObj; consume it (ignore_result) so the autouse
        # verify_result_objects teardown fixture doesn't error on this graceful "no ports" path.
        if not result.result:
            result.ignore_result()
            return []
        return [port.name for port in result.get_returned_value()]

    names = _query(requested_state)
    if not names and fallback_all_states and requested_state != NvosConsts.LINK_STATE_ALL_TYPES:
        names = _query(NvosConsts.LINK_STATE_ALL_TYPES)
    return names


def select_nvl7_trunk_fae_port(
    requested_state: str = NvosConsts.LINK_STATE_UP,
    fallback_all_states: bool = True,
) -> Optional[Fae]:
    """
    Select a random NVL7 CPO trunk (sw*) port and return it as a Fae object, or None if none exist.

    Callers decide how to handle None (pytest.skip / Skipped / log-and-continue).
    """
    names = nvl7_trunk_port_names(requested_state, fallback_all_states)
    if not names:
        return None
    return Fae(port_name=random.choice(names))


def _tolerant_show_dict(component) -> dict:
    """
    Read a show as a dict WITHOUT raising, for present/absent probes.

    show(should_succeed=False) alone still raises (ResultObj.get_returned_value asserts
    should_succeed == result, and on a pruned port the returned value is error TEXT that fails
    JSON parsing). Taking the raw ResultObj (if_returned_value=False) avoids both: a failed show
    (pruned / absent node) yields {}, a successful one yields the parsed dict.

    ignore_result() is required on every ResultObj we take here: a probe of a pruned port
    legitimately fails, and the autouse verify_result_objects teardown fixture raises on any
    unconsumed failed ResultObj.
    """
    res = component.show(should_succeed=False, if_returned_value=False)
    res.ignore_result()
    if not res.result:
        return {}
    parsed = OutputParsingTool.parse_json_str_to_dictionary(res.returned_value)
    parsed.ignore_result()
    output = parsed.returned_value if parsed.result else {}
    return output if isinstance(output, dict) else {}


def nvl7_new_leaves_present(fae_port: Fae) -> Tuple[bool, list]:
    """
    Return (all_present, missing) for the 5 new NVL7 leaves in the phy-recovery show output.

    Tolerant of ports where the phy-recovery node itself is absent/pruned (e.g. eth0/lo or
    non-CPO NVL7): a failing or non-dict show is treated as "all new leaves missing" (pruned).
    """
    output = _tolerant_show_dict(fae_port.port.interface.link.phy_recovery)
    missing = [leaf for leaf in PhyRecoveryConsts.NVL7_NEW_LEAVES if leaf not in output]
    return (not missing, missing)


def nvl7_read_debug_counters(fae_port: Fae) -> dict:
    """
    Return ``nv show fae interface <intf> counters debug`` as a dict of {counter: int}.

    Strict: fails if any of the 6 step-counters is missing or non-numeric, so a partially
    exposed node cannot masquerade as all-zero defaults.
    """
    output = OutputParsingTool.parse_json_str_to_dictionary(
        fae_port.port.interface.counters.debug.show()
    ).get_returned_value()
    missing = [name for name in PhyRecoveryConsts.NVL7_DEBUG_COUNTERS if name not in output]
    assert not missing, f"debug counters missing from show output: {missing} (output={output})"
    return {name: int(output[name]) for name in PhyRecoveryConsts.NVL7_DEBUG_COUNTERS}


def nvl7_missing_debug_counters(fae_port: Fae) -> list:
    """
    Return the debug step-counters ABSENT from the ``counters debug`` show output.

    [] = node fully present (CPO trunk); all 6 = node pruned. Tolerant of pruned ports
    (a failing or non-dict show counts as all-missing).
    """
    output = _tolerant_show_dict(fae_port.port.interface.counters.debug)
    return [name for name in PhyRecoveryConsts.NVL7_DEBUG_COUNTERS if name not in output]


def nvl7_clear_debug_counters(fae_port: Fae, dut_engine=None, fae: bool = True) -> None:
    """
    Clear interface counters.

    Per dev (2026-07-17): ONLY the FAE-scoped clear
    (``nv action clear fae interface <p> link counters``) resets the 6 debug step-counters; the
    plain (non-fae) clear does NOT touch them. Pass ``fae=False`` for the plain clear.
    """
    scope = "fae " if fae else ""
    with allure.step(f"Clear {scope}counters for {fae_port.port.name}"):
        fae_port.port.interface.counters.clear_counters(
            dut_engine=dut_engine, fae_param="fae" if fae else ""
        ).verify_result()


def nvl7_trigger_recovery(port_name: str):
    """
    Trigger one NVL7 recovery event (go-once): ``nv action start fae interface <port> link phy-recovery``.

    Thin name->Fae adapter over the product tool ``PhyRecovery.action_start_go_once()`` (which
    carries its own allure step).

    go-once is confirmed wired by dev (2026-07-17); callers hard-verify the returned ResultObj
    (a rejection is a real failure, not a skip).
    """
    return Fae(port_name=port_name).port.interface.link.phy_recovery.action_start_go_once()


def _assert_nvl7_port_up(port: Port) -> None:
    state = OutputParsingTool.parse_json_str_to_dictionary(
        port.interface.link.state.show()
    ).get_returned_value()
    assert state.get(NvosConsts.LINK_STATE_UP) is not None, f"{port.name} is not up (state={state})"


def nvl7_wait_trunk_up(
    port_name: str,
    tries: int = NVL7_TRUNK_LINK_UP_TRIES,
    delay: int = NVL7_TRUNK_LINK_UP_DELAY,
) -> None:
    """
    Bring an NVL7 trunk port back up after a phy-recovery apply.

    Per dev (2026-07-17): applying phy-recovery config admin-DOWNs the port and it STAYS down (no
    self-recovery on NVL7). So toggling the link state up is the PRIMARY action, then we retry-wait
    for the port to reach up. Shared by the NVL7 tests and test_save_after_reboot cleanup.
    """
    port = Port(port_name)
    with allure.step(f"Toggle {port_name} link state up (NVL7 stays down after phy-recovery apply)"):
        try:
            # consume the ResultObj (a failed set won't raise, but the autouse
            # verify_result_objects teardown fixture would error on an unconsumed failed ResultObj).
            port.interface.link.state.set(
                op_param_name=NvosConsts.LINK_STATE_UP, apply=True, ask_for_confirmation=True
            ).ignore_result()
        except Exception as e:  # noqa: BLE001 - toggle-up is best-effort
            logger.warning("Toggle-up on %s failed: %s", port_name, e)

    retry_call(_assert_nvl7_port_up, fargs=[port], exceptions=AssertionError, tries=tries, delay=delay)


def setup_nvl_speed_for_phy_recovery(devices, exclude_speeds=None, required=False):
    """
    Same as nvl_port.helpers.setup_nvl_speed but always excludes PHY_RECOVERY_UNSUPPORTED_LINK_SPEEDS
    (phy recovery is not supported on 400G in these tests).
    """
    merged = list(exclude_speeds or [])
    for speed in PHY_RECOVERY_UNSUPPORTED_LINK_SPEEDS:
        if speed not in merged:
            merged.append(speed)
    return setup_nvl_speed(devices, exclude_speeds=merged, required=required)
