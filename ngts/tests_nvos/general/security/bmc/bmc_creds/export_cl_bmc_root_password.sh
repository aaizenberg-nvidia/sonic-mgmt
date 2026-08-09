#!/usr/bin/env bash
# Export CL BMC root password for local/lab pytest runs (SPC-6 / mlx-6600, etc.).
#
# Usage (must be sourced, not executed):
#   source ngts/tests_nvos/general/security/bmc/bmc_creds/export_cl_bmc_root_password.sh
#
# Resolution order:
#   1. CL_BMC_ROOT_PASSWORD or BMC_ROOT_PASSWORD already set in the environment
#   2. First line of $CL_BMC_ROOT_PASSWORD_FILE
#      (default: ~/.config/nvidia/cl_bmc_root_password)
#
# One-time setup:
#   mkdir -p ~/.config/nvidia
#   printf '%s\n' 'YOUR_BMC_ROOT_PASSWORD' > ~/.config/nvidia/cl_bmc_root_password
#   chmod 600 ~/.config/nvidia/cl_bmc_root_password

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  echo "Source this script; do not execute it:" >&2
  echo "  source $0" >&2
  exit 1
fi

if [[ -n "${CL_BMC_ROOT_PASSWORD:-}" || -n "${BMC_ROOT_PASSWORD:-}" ]]; then
  echo "CL BMC root password already set in the environment."
  return 0 2>/dev/null || true
fi

_CL_BMC_PW_FILE="${CL_BMC_ROOT_PASSWORD_FILE:-${HOME}/.config/nvidia/cl_bmc_root_password}"
if [[ ! -f "${_CL_BMC_PW_FILE}" ]]; then
  echo "Missing ${_CL_BMC_PW_FILE}" >&2
  echo "Create it with the provisioned BMC root password (chmod 600), or export CL_BMC_ROOT_PASSWORD." >&2
  unset _CL_BMC_PW_FILE
  return 1 2>/dev/null || exit 1
fi
if [[ ! -r "${_CL_BMC_PW_FILE}" ]]; then
  echo "Password file is not readable: ${_CL_BMC_PW_FILE}" >&2
  unset _CL_BMC_PW_FILE
  return 1 2>/dev/null || exit 1
fi
if [[ ! -s "${_CL_BMC_PW_FILE}" ]]; then
  echo "Password file is empty: ${_CL_BMC_PW_FILE}" >&2
  unset _CL_BMC_PW_FILE
  return 1 2>/dev/null || exit 1
fi

# The setup instructions above ask for mode 600, so check it instead of only documenting it:
# this file holds a BMC root credential in cleartext, and on a shared VDI an 0644 copy is
# readable by every other account. Refusing to export it is what turns that instruction into
# something a developer finds out about now rather than after the credential has leaked.
_CL_BMC_PW_STAT="$(stat -L -c '%a %u' "${_CL_BMC_PW_FILE}" 2>/dev/null)"
if [[ -z "${_CL_BMC_PW_STAT}" ]]; then
  echo "Cannot stat password file: ${_CL_BMC_PW_FILE}" >&2
  unset _CL_BMC_PW_FILE _CL_BMC_PW_STAT
  return 1 2>/dev/null || exit 1
fi
_CL_BMC_PW_MODE="${_CL_BMC_PW_STAT%% *}"
if [[ "${_CL_BMC_PW_STAT##* }" != "$(id -u)" ]]; then
  echo "Password file is not owned by you: ${_CL_BMC_PW_FILE}" >&2
  echo "Owned by uid ${_CL_BMC_PW_STAT##* }; re-create it under your own account." >&2
  unset _CL_BMC_PW_FILE _CL_BMC_PW_STAT _CL_BMC_PW_MODE
  return 1 2>/dev/null || exit 1
fi
# Any group or other bit at all, not just read: write would let someone swap the credential,
# and execute has no business being set on it either.
if (( 8#${_CL_BMC_PW_MODE} & 8#077 )); then
  echo "Password file is accessible to other accounts (mode ${_CL_BMC_PW_MODE}): ${_CL_BMC_PW_FILE}" >&2
  echo "Restrict it:  chmod 600 ${_CL_BMC_PW_FILE}" >&2
  unset _CL_BMC_PW_FILE _CL_BMC_PW_STAT _CL_BMC_PW_MODE
  return 1 2>/dev/null || exit 1
fi
unset _CL_BMC_PW_STAT _CL_BMC_PW_MODE

_CL_BMC_PW_VALUE="$(head -n 1 "${_CL_BMC_PW_FILE}" | tr -d '\r')" || {
  echo "Failed to read password file: ${_CL_BMC_PW_FILE}" >&2
  unset _CL_BMC_PW_FILE _CL_BMC_PW_VALUE
  return 1 2>/dev/null || exit 1
}
if [[ -z "${_CL_BMC_PW_VALUE}" ]]; then
  echo "Password file first line is empty: ${_CL_BMC_PW_FILE}" >&2
  unset _CL_BMC_PW_FILE _CL_BMC_PW_VALUE
  return 1 2>/dev/null || exit 1
fi

CL_BMC_ROOT_PASSWORD="${_CL_BMC_PW_VALUE}"
export CL_BMC_ROOT_PASSWORD
unset _CL_BMC_PW_FILE _CL_BMC_PW_VALUE

echo "Exported CL_BMC_ROOT_PASSWORD from local password file."
