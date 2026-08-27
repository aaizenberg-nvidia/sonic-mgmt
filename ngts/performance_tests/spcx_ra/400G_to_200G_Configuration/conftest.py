"""

conftest.py

Defines the methods and fixtures which will be used by pytest for only performance setups.

"""
import pytest
import logging
import allure
import copy
import os
from ngts.helpers.performance.performance_setup_helpers import (
    apply_test_configuration_guarded, restore_session_basic_configuration,
    save_session_base_configuration)
from ngts.helpers.performance.performance_recovery import has_primary_test_failure
from ngts.constants.performance_constants import PerfConsts, SPCXRAConsts, MongoDbConsts
from ngts.helpers.performance.performance_db_helpers import get_perf_test_name, add_test_mongo_metadata
from ngts.constants.constants import CliType
from ngts.helpers.performance.performance_setup_helpers import skip_test_on_unsupported_os

logger = logging.getLogger()
TESTS_SCENARIO = "spcx_ra"


@pytest.fixture(scope='module', autouse=True)
def skip_test_conditionally(players):
    skip_test_on_unsupported_os(players['dut']['cli'], CliType.NVUE)
    yield


@pytest.fixture(scope='class', autouse=True)
def basic_setup_configuration(request, players, conf_args):
    baseline_available = False
    primary_error = None
    try:
        with allure.step('Save Players initial Configuration'):
            # Relevant mostly for CL/Sonic- Save config for later cleanup
            save_session_base_configuration(players)
            baseline_available = True
        with allure.step("Apply Test configuration on all Players"):
            # Apply the configuration from all of the jinja files and run the test.
            apply_test_configuration_guarded(
                players, scenario=TESTS_SCENARIO, conf_args=conf_args,
                nodeid=request.node.nodeid)
        yield
    except Exception as error:
        primary_error = error
        raise
    finally:
        if baseline_available:
            try:
                with allure.step('Restore Base Configuration on all Players'):
                    # In DVS- dvs_stop & start. CL/Sonic- Reload base config file
                    restore_session_basic_configuration(
                        players, nodeid=request.node.nodeid)
            except Exception:
                if not has_primary_test_failure(primary_error, request.node):
                    raise
                logger.exception(
                    "Baseline restore failed while preserving the primary setup/test failure")


@pytest.fixture(scope='class', autouse=True)
def conf_args(chip_type):
    """
    This function alters all of the jinja template files.
    """
    conf_args = {"auto_buffer_mode": "True" if chip_type == "SPC6" else "False",
                 "congestion_thresh_lo": PerfConsts.LOW_AR_THRESHOLD_SPC6 if chip_type == "SPC6" else PerfConsts.LOW_AR_THRESHOLD,
                 "congestion_thresh_med": (PerfConsts.MED_AR_THRESHOLD_SPC6 if chip_type == "SPC6"
                                           else PerfConsts.MED_AR_THRESHOLD),
                 "congestion_thresh_hi": (PerfConsts.HIGH_AR_THRESHOLD_SPC6 if chip_type == "SPC6"
                                          else PerfConsts.HIGH_AR_THRESHOLD),
                 "is_ipv6": False,
                 "is_ipv4": True,
                 "split_right": 4,
                 "split_left": 2,
                 "host": PerfConsts.RIGHT_TG_ALIAS,
                 "spine": PerfConsts.LEFT_TG_ALIAS,
                 "shaper_value": 0.975,
                 "packet_size": PerfConsts.PACKET_SIZE_LIST[0],
                 "left_num_packets": 1,
                 "right_num_packets": SPCXRAConsts.PACKET_NUM_400G_x2[chip_type],
                 "two_sided_ar": False,
                 "scenario": TESTS_SCENARIO,
                 "use_incremental_dips": False,
                 "dip_left_to_right_start_ipv4_list": ["10.0.1.0"],
                 "dip_left_to_right_start_ipv6_list": [],
                 }
    return conf_args


@pytest.fixture(scope='function', autouse=True)
def update_test_mongo_metadata(request, port_group_df):
    test_name = get_perf_test_name(request)
    add_test_mongo_metadata(test_name, {MongoDbConsts.CONF_NAME: "400G_to_200G_leaf",
                                        MongoDbConsts.PORT_GROUP_DF: port_group_df})
    yield
