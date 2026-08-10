from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import DelayedRecovery


STATE_ENABLED = "enabled"
STATE_DISABLED = "disabled"
STATE_FW_DEFAULT = "fw-default"
FORCE_ENABLED = "enabled"
FORCE_DISABLED = "disabled"
ADMIN_DEFAULT_TH = "0"
# The FW enabled-mode default for fec-plr-align-loss-th depends on the module:
# passive copper resolves to 127, optical (and other non-passive media) to 126.
# plr-retry-th is 255 on every module type.
# Verified on croc-94, same switch and firmware, one copper loopback pair and one
# optical loopback pair: 127 vs 126 respectively.
OPER_ENABLED_LOSS_TH_COPPER = "127"
OPER_ENABLED_LOSS_TH_OPTICAL = "126"
# NVUE reports passive DACs under this cable-type; everything else (optical, active
# cables) takes the 126 default.
PASSIVE_COPPER_CABLE_TYPE = "Passive copper or linear active copper cable"
OPER_ENABLED_RETRY_TH = "255"
OPER_DISABLED_LOSS_TH = "126"
OPER_DISABLED_RETRY_TH = "32"
CUSTOM_LOSS_TH_A = "111"
CUSTOM_RETRY_TH_A = "222"
CUSTOM_LOSS_TH_B = "100"
CUSTOM_RETRY_TH_B = "200"


def admin_values(
    state=STATE_FW_DEFAULT,
    loss_th=ADMIN_DEFAULT_TH,
    retry_th=ADMIN_DEFAULT_TH,
    state_force=DelayedRecovery.DELAYED_RECOVERY_DEFAULT_FORCE_STATE,
    loss_th_force=DelayedRecovery.DELAYED_RECOVERY_DEFAULT_FORCE_LOSS_TH,
    retry_th_force=DelayedRecovery.DELAYED_RECOVERY_DEFAULT_FORCE_RETRY_TH,
):
    return {
        DelayedRecovery.DELAYED_RECOVERY_STATE: state,
        DelayedRecovery.DELAYED_RECOVERY_LOSS_TH: loss_th,
        DelayedRecovery.DELAYED_RECOVERY_RETRY_TH: retry_th,
        DelayedRecovery.DELAYED_RECOVERY_STATE_FORCE: state_force,
        DelayedRecovery.DELAYED_RECOVERY_LOSS_TH_FORCE: loss_th_force,
        DelayedRecovery.DELAYED_RECOVERY_RETRY_TH_FORCE: retry_th_force,
    }


def oper_values(state=STATE_DISABLED, loss_th=OPER_DISABLED_LOSS_TH, retry_th=OPER_DISABLED_RETRY_TH):
    return {
        DelayedRecovery.DELAYED_RECOVERY_STATE: state,
        DelayedRecovery.DELAYED_RECOVERY_LOSS_TH: loss_th,
        DelayedRecovery.DELAYED_RECOVERY_RETRY_TH: retry_th,
    }


def force_values(state_force=None, loss_th_force=None, retry_th_force=None):
    values = {}
    if state_force is not None:
        values[DelayedRecovery.DELAYED_RECOVERY_STATE_FORCE] = state_force
    if loss_th_force is not None:
        values[DelayedRecovery.DELAYED_RECOVERY_LOSS_TH_FORCE] = loss_th_force
    if retry_th_force is not None:
        values[DelayedRecovery.DELAYED_RECOVERY_RETRY_TH_FORCE] = retry_th_force
    return values
