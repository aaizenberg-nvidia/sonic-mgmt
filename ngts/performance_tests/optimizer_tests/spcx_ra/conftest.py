"""

conftest.py

Defines the methods and fixtures which will be used by pytest for only performance setups.

"""
import pytest
import logging
import allure
import copy
import os
from ngts.helpers.performance.performance_setup_helpers import (save_base_configuration,
                                                                restore_basic_configuration,
                                                                apply_test_configuration)
from ngts.constants.performance_constants import PerfConsts, SPCXRAConsts
from ngts.constants.nv_optimizer_constant import NvOptimizerEnvVariables
from ngts.helpers.performance.performance_setup_helpers import get_perf_test_name, add_test_mongo_metadata
from ngts.constants.performance_constants import MongoDbConsts
from ngts.constants.constants import InfraConst

logger = logging.getLogger()
TESTS_SCENARIO = "spcx_ra"


@pytest.fixture(scope='class', autouse=True)
def get_conf_args(is_ipv6, chip_type, performance_parameters):
    conf_args = {
        "auto_buffer_mode": "False",
        "congestion_thresh_lo": PerfConsts.LOW_AR_THRESHOLD_SPC6 if chip_type == "SPC6" else PerfConsts.LOW_AR_THRESHOLD,
        "two_sided_ar": True,
        "is_ipv6": is_ipv6,
        "split_right": 2,
        "split_left": 2,
        "scenario": TESTS_SCENARIO,
        "packet_size": PerfConsts.PACKET_SIZE_LIST[0],
        "left_num_packets": SPCXRAConsts.PACKET_NUM_400G_x2[chip_type],
        "right_num_packets": SPCXRAConsts.PACKET_NUM_400G_x2[chip_type],
        "speed": "400000000",
                 "params": performance_parameters,
                 "result_file_location": os.path.join(str(NvOptimizerEnvVariables.ngts_dir), str(NvOptimizerEnvVariables.result_parameter_file))
    }
    return conf_args


def apply_basic_setup_configuration(is_ipv6, players, chip_type, performance_parameters, init):
    conf_args = get_conf_args(is_ipv6, chip_type, performance_parameters)
    if init:
        with allure.step('Save Players initial Configuration'):
            save_base_configuration(players)
        with allure.step("Apply Test configuration on all Players"):
            apply_test_configuration(players, scenario=TESTS_SCENARIO, conf_args=conf_args)


@pytest.fixture(scope='class')
def basic_setup_configuration(request, players, chip_type, performance_parameters, init, cleanup):
    is_ipv6 = request.param == InfraConst.IPV6
    try:
        apply_basic_setup_configuration(is_ipv6, players, chip_type, performance_parameters, init)
        yield is_ipv6
    except Exception as e:
        raise e
    finally:
        if cleanup:
            with allure.step('Restore Base Configuration on all Players'):
                restore_basic_configuration(players)
