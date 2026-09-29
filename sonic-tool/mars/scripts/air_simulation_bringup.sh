#!/bin/bash

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# Repo root: sonic-tool/mars/scripts -> ../../.. relative to SCRIPT_DIR
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
NGTS_VERSION=$(PYTHONPATH="${SCRIPT_DIR}/lib/" python3 -c 'import constants; print(constants.DOCKER_NGTS_DEFAULT_TAG)')

if [ -z "${NGTS_VERSION}" ]; then
    echo "Failed to read DOCKER_NGTS_DEFAULT_TAG from ${SCRIPT_DIR}/lib/constants.py" >&2
    exit 1
fi

EXTRA_ARGS=(--air_config_dir "${REPO_ROOT}/ngts/scripts/air_bringup/topologies")

pip3 install --ignore-installed "devts[air-start-sim]@git+https://svc_sonic_ver_bot:${GERRIT_API_KEY}@git-nbu-sw.nvidia.com/r/a/devts@${NGTS_VERSION}"
python3 -m devts.scripts.air_simulation_bringup "$@" "${EXTRA_ARGS[@]}"
