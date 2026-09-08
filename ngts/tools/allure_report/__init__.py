import os
import logging

import pytest
from pytest import StashKey

from ngts.tools.infra import update_sys_path_by_community_plugins_path
from ngts.tools.infra import get_topology_from_noga, get_chip_type

update_sys_path_by_community_plugins_path()

from plugins.allure_server import pytest_addoption, pytest_terminal_summary, \
    cache_pytest_session_run_cmd, attach_pytest_specific_test_run_cmd_to_allure_report, \
    export_session_info_to_allure  # noqa: E402
from plugins.allure_server import pytest_sessionfinish as _community_pytest_sessionfinish  # noqa: E402

logger = logging.getLogger(__name__)

_allure_env_key: StashKey[dict] = StashKey()

# Regression/MARS: Allure env keys and optional pytest option (None = from os.environ only).
_REGRESSION_ALLURE_ENV = [
    ("issu_version", "--issu_version"),
    ("dut_ip", None),
    ("chip_type", None),
    ("dut_name", None),
    ("target_version", "--target_version"),
    ("downgrade_version", "--downgrade_version"),
    ("traffic_host_ip", None),
    ("mars_session_id", "--session_id"),
    ("traffic_ports", None),
    ("base_version", "--base_version"),
    ("release_name", "--release_name"),
]


def allure_set_environment(config, key, value):
    """
    Add a key-value pair to the Allure report environment.

    Call from a session- or module-scoped fixture to populate the Allure
    environment tab. Values are written to environment.properties at session end.

    :param config: pytest Config object (e.g. request.config).
    :param key: Environment entry name (e.g. "Branch", "Custom_Label").
    :param value: String value. Non-strings are converted to str.
    """
    env = config.stash.setdefault(_allure_env_key, {})
    env[str(key)] = str(value) if value is not None else ""


def _resolve_from_topology(config, missed_keys):
    """Resolve allure env keys from noga topology and static config.

    Called as a fallback for keys not found via CLI options or env vars.
    Returns dict of {key: (value, source_description)} for successfully resolved keys.
    """
    from ngts.nvos_tools.infra.RegressionConfigurations import Configurations

    resolved = {}
    topology = getattr(config, "topology_obj", None)
    attrs = None
    dut_ip = None

    if topology:
        try:
            attrs = topology.players['dut']['attributes'].noga_query_data['attributes']
        except (KeyError, AttributeError):
            logger.debug("Allure env: could not read DUT noga attributes from topology")

        try:
            dut_ip = topology.players['dut']['engine'].ip
        except (KeyError, AttributeError):
            pass

    resolvers = {
        'dut_name': lambda: (
            attrs and attrs.get('Common', {}).get('Name'),
            "noga topology Common.Name"
        ),
        'dut_ip': lambda: (
            dut_ip,
            "noga topology engine.ip"
        ),
        'chip_type': lambda: (
            attrs and get_chip_type(attrs),
            "noga topology chip_type"
        ),
        'dut_type': lambda: (
            attrs and attrs.get('Specific', {}).get('TYPE'),
            "noga topology Specific.TYPE"
        ),
        'traffic_ports': lambda: (
            ', '.join(Configurations.traffic_ports.get(dut_ip, [])) if dut_ip else None,
            "RegressionConfigurations"
        ),
    }

    for key in list(missed_keys):
        resolver = resolvers.get(key)
        if not resolver:
            continue
        try:
            value, source = resolver()
            if value and str(value).strip():
                resolved[key] = (str(value).strip(), source)
        except Exception:
            logger.debug("Allure env: resolver failed for '%s'", key, exc_info=True)

    return resolved


@pytest.fixture(scope="session", autouse=True)
def _allure_regression_environment(pytestconfig):
    """
    Session-scoped fixture: populate Allure environment from pytest options and env vars.

    Resolution order per key (first non-empty wins):
      1. CLI option (e.g. --setup_name, --target_version)
      2. Environment variable (e.g. DUT_IP, CHIP_TYPE)
      3. Noga topology / static config (fallback for topology-derived keys)
    """
    logger.debug("Allure env: all pytest options: %s", vars(pytestconfig.option))

    collected = []
    missed = []
    for allure_key, option_name in _REGRESSION_ALLURE_ENV:
        try:
            value = None
            source = None
            if option_name is not None:
                try:
                    value = pytestconfig.getoption(option_name, default=None)
                    if value is not None and str(value).strip() != "":
                        source = f"CLI option {option_name}"
                except (ValueError, KeyError):
                    pass
            if value is None or value == "":
                value = os.environ.get(allure_key) or os.environ.get(allure_key.upper())
                if value is not None and str(value).strip() != "":
                    source = f"env var {allure_key}/{allure_key.upper()}"
            if value is not None and str(value).strip() != "":
                allure_set_environment(pytestconfig, allure_key, str(value).strip())
                collected.append(allure_key)
                logger.debug("Allure env: collected '%s' (from %s)", allure_key, source)
            else:
                missed.append(allure_key)
        except Exception:
            missed.append(allure_key)
            logger.exception("Allure env: unexpected error collecting '%s'", allure_key)

    if missed:
        resolved = _resolve_from_topology(pytestconfig, missed)
        for key, (value, source) in resolved.items():
            allure_set_environment(pytestconfig, key, value)
            collected.append(key)
            missed.remove(key)
            logger.debug("Allure env: collected '%s' (from %s)", key, source)

    logger.info("Allure env: collected %d keys: %s", len(collected), collected)
    if missed:
        logger.warning("Allure env: %d keys not resolved: %s", len(missed), missed)
    yield


def ansible_host_pattern_param_already_loaded(session):
    # the ansible_host_pattern param is defined when it has the value
    # which is not "stub_string" or "localhost"
    return hasattr(session.config.option, 'ansible_host_pattern') and \
        session.config.option.ansible_host_pattern and \
        session.config.option.ansible_host_pattern != "stub_string" and \
        session.config.option.ansible_host_pattern != "localhost"


def get_setup_topology(session):
    setup_topology = 'ptf-any'
    if hasattr(session.config.option, 'sonic_topo'):
        if session.config.option.sonic_topo:
            setup_topology = session.config.option.sonic_topo
    return setup_topology


def pytest_sessionstart(session):
    session.config.option.allure_server_addr = "allure.nvidia.com"
    session.config.option.allure_server_port = ''

    if not ansible_host_pattern_param_already_loaded(session):
        topology = get_topology_from_noga(session)
        dut_name = topology.players['dut']['attributes'].noga_query_data['attributes']['Common']['Name']
        session.config.option.ansible_host_pattern = dut_name


def pytest_sessionfinish(session, exitstatus):
    """
    write NGTS environment metadata, then delegate to community plugin.

    Our hook writes environment.properties first with NGTS regression/MARS data.
    Then the community hook runs — its get_setup_session_info() will overwrite this file if it meets the requirements.
    In the case of NVOS, it will not run, leaving the file untouched after this hook implementation.
    The community hook then uploads all files (including our environment.properties) to the allure server.
    """
    if not session.config.getoption("--collectonly"):
        try:
            allure_report_dir = getattr(session.config.option, "allure_report_dir", None)
            allure_env = session.config.stash.get(_allure_env_key, {})
            if allure_report_dir and allure_env:
                export_session_info_to_allure(allure_env, allure_report_dir)
        except Exception:
            logger.exception("Failed to write environment.properties; continuing to community hook")

    _community_pytest_sessionfinish(session, exitstatus)
