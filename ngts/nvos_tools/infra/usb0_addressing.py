"""Host <-> BMC usb0 addressing profiles for CL/SPC6 (Redmine #5124733).

The FR moves the usb0 pair from ``169.254.0.2 / 169.254.0.1`` to
``169.254.100.2 / 169.254.100.1`` as a hard cut: no dual address on the host and
no runtime resolve of the BMC address (Architecture decision, 2026-08-30). A DUT
therefore runs exactly one of the two schemes, so the suite picks a profile for
the whole run rather than parametrizing individual tests.

The profile is selected by environment variable and resolved when this module is
imported. That matters: callers do ``from ...BmcCliTool import BMC_IP``, which
binds the value at import time, so a switch applied later (from
``pytest_configure``, say) would silently never reach them.

    CL_BMC_USB0_PROFILE=legacy   # default -- 169.254.0.x, product today
    CL_BMC_USB0_PROFILE=new      # 169.254.100.x, product after #5124733

An unrecognized value raises rather than falling back, so a typo cannot quietly
run the suite against the wrong addresses.
"""
import logging
import os
from dataclasses import dataclass

logger = logging.getLogger()

PROFILE_ENV_VAR = 'CL_BMC_USB0_PROFILE'
LEGACY_PROFILE = 'legacy'
NEW_PROFILE = 'new'


@dataclass(frozen=True)
class Usb0Addressing:
    """One usb0 addressing scheme: the CPU-side address and the BMC peer."""

    name: str
    host_cidr: str
    bmc_ip: str

    @property
    def host_bare(self) -> str:
        return self.host_cidr.split('/')[0]

    @property
    def prefix_len(self) -> int:
        return int(self.host_cidr.split('/')[1])

    def describe(self) -> str:
        return (f"usb0 profile {self.name!r}: host {self.host_cidr}, "
                f"BMC peer {self.bmc_ip}")


# The 'new' prefix length follows the FS (169.254.100.0/30: .0 network, .1 BMC,
# .2 CPU, .3 broadcast). Re-confirm it against the FS revision before relying on
# a run made with this profile.
PROFILES = {
    LEGACY_PROFILE: Usb0Addressing(LEGACY_PROFILE, '169.254.0.2/24', '169.254.0.1'),
    NEW_PROFILE: Usb0Addressing(NEW_PROFILE, '169.254.100.2/30', '169.254.100.1'),
}


def active_profile_name() -> str:
    """Profile requested through the environment, defaulting to ``legacy``."""
    return os.environ.get(PROFILE_ENV_VAR, '').strip().lower() or LEGACY_PROFILE


def get_profile(name: str | None = None) -> Usb0Addressing:
    name = (name or active_profile_name()).strip().lower()
    if name not in PROFILES:
        raise ValueError(
            f"Unknown {PROFILE_ENV_VAR}={name!r}. "
            f"Valid profiles: {', '.join(sorted(PROFILES))}"
        )
    return PROFILES[name]


ACTIVE_PROFILE = get_profile()


def other_profile(active: Usb0Addressing | None = None) -> Usb0Addressing:
    """The profile we are *not* running, for mismatch diagnostics."""
    active = active or ACTIVE_PROFILE
    name = NEW_PROFILE if active.name == LEGACY_PROFILE else LEGACY_PROFILE
    return PROFILES[name]


def mismatch_hint(active: Usb0Addressing | None = None) -> str:
    """Text that tells a profile/DUT mismatch apart from a genuinely dead BMC."""
    active = active or ACTIVE_PROFILE
    alternative = other_profile(active)
    return (
        f"Expected {active.describe()} (selected by {PROFILE_ENV_VAR}="
        f"{active.name}). If the DUT runs the other scheme "
        f"({alternative.host_cidr} / {alternative.bmc_ip}), this is an addressing "
        f"profile mismatch, not an unreachable BMC -- re-run with "
        f"{PROFILE_ENV_VAR}={alternative.name}."
    )


def log_active_profile() -> Usb0Addressing:
    logger.info(ACTIVE_PROFILE.describe())
    return ACTIVE_PROFILE
