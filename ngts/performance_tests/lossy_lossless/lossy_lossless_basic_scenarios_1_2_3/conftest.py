"""

conftest.py

Defines the methods and fixtures which will be used by pytest for only performance setups.

"""

from datetime import datetime
from ngts.helpers.performance.performance_db_helpers import add_test_mongo_metadata, get_perf_test_name
from ngts.performance_tests.conftest import get_all_players_ports
import pytest
import logging
import allure
import re
from ngts.helpers.general_helper import get_pytest_test_name
from ngts.helpers.performance.performance_setup_helpers import (save_base_configuration,
                                                                restore_basic_configuration,
                                                                apply_test_configuration)
from ngts.constants.performance_constants import PerfConsts, MongoDbConsts


logger = logging.getLogger()
TESTS_SCENARIO = "lossy_lossless"


@pytest.fixture(scope='class', autouse=True)
def basic_setup_configuration(players, conf_args):
    try:
        with allure.step('Save Players initial Configuration'):
            save_base_configuration(players)
        with allure.step("Apply Test configuration on all Players"):
            apply_test_configuration(players, scenario=TESTS_SCENARIO, conf_args=conf_args)
        yield
    except Exception as e:
        raise e
    finally:
        with allure.step('Restore Base Configuration on all Players'):
            restore_basic_configuration(players)


@pytest.fixture(scope='session', autouse=True)
def conf_args(skip_test_conditionally, chip_type):
    """
    This function alters all of the jinja template files.
    """
    split_left = 2
    split_right = 2

    conf_args = {"congestion_thresh_lo": PerfConsts.LOW_AR_THRESHOLD_SPC6 if chip_type == "SPC6" else PerfConsts.LOW_AR_THRESHOLD,
                 "fboss_enabled": True,
                 "auto_buffer_mode": "False",
                 "packet_size": PerfConsts.PACKET_SIZE_LIST[0],
                 "is_ipv6": False,
                 "split_right": 2,
                 "split_left": 2,
                 "two_sided_ar": True,
                 "scenario": TESTS_SCENARIO,
                 "left_num_packets": 0,
                 "right_num_packets": 0,
                 "sdk_test_name": "LossyLosslessDut400G"
                 }
    return conf_args


def set_allure_lossy_lossless_title(request, scenario):
    """
    Adds scenario name to allure title.
    """
    scenario_names_dict = {"lossy_lossless_scenario_1": 'All lossless test',
                           "lossy_lossless_scenario_2": 'All lossy test',
                           "lossy_lossless_scenario_3a": '50% lossless traffic, 50% lossy traffic',
                           "lossy_lossless_scenario_3b": '75% lossless traffic, 25% lossy traffic'}

    test_name = get_perf_test_name(request)
    test_name_with_scenario = re.sub(r'\[.*?\]', test_name, f"- Scenario {scenario}: {scenario_names_dict[scenario]}")

    allure.dynamic.title(test_name_with_scenario)
    return test_name
