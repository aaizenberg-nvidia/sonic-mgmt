"""
Phy-recovery test helpers: NVL5 (serdes-eq) vs NVL6 (recovery-status / link-down-timeout).
"""
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, Tuple, Type

from retry.api import retry_call

from ngts.ngts_types import DevicesT
from ngts.nvos_constants.constants_nvos import NvosConst
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import PhyRecoveryConsts
from ngts.nvos_tools.infra.Fae import Fae
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.ValidationTool import ValidationTool
from ngts.tests_nvos.interfaces.nvl_port.helpers import setup_nvl_speed
from ngts.tools.test_utils import allure_utils as allure
from ngts.tools.test_utils.allure_utils import step as allure_step

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

# Phy-recovery is not supported on 200G..
PHY_RECOVERY_UNSUPPORTED_LINK_SPEEDS = ('200G',)


@dataclass(frozen=True)
class PhyRecoveryFlow:
    """
    Immutable mode/timeout parameters for one phy-recovery flavor (NVL5 vs NVL6).
    Obtain via PhyRecoveryFlow.for_device() or phy_recovery_test_profile(devices).flow.
    """

    label: str
    modes: Tuple[str, ...]
    recovery_field: str
    timeout_field: str
    default_timeout_when_enabled: int

    @classmethod
    def for_device(cls, devices: DevicesT) -> 'PhyRecoveryFlow':
        return phy_recovery_test_profile(devices).flow

    def is_nvl6(self) -> bool:
        return self.label == 'NVL6'


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
        )

    def verify_default_phy_recovery_show(self, devices: DevicesT, selected_port: Fae) -> None:
        with allure_step("Check default config"):
            output_fae_port = OutputParsingTool.parse_json_str_to_dictionary(
                selected_port.port.interface.link.phy_recovery.show()
            ).get_returned_value()
            expected = devices.dut.default_phy_recovery_attributes
            filtered_out = {k: v for k, v in output_fae_port.items() if k in expected}
            ValidationTool.compare_dictionaries(filtered_out, expected).verify_result()


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
        if flow.is_nvl6():
            expected_mode = PhyRecoveryConsts.DISABLED if mode == PhyRecoveryConsts.AUTO else mode
        else:
            expected_mode = PhyRecoveryConsts.DISABLED if mode == PhyRecoveryConsts.FW_DEFAULT else mode
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
