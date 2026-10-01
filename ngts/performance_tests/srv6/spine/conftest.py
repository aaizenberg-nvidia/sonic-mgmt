import logging

import allure
import pytest

from ngts.constants.performance_constants import MRCConsts, Cl_Consts
from ngts.constants.constants import SonicConst
from ngts.helpers.performance.performance_recovery import has_primary_test_failure
from ngts.helpers.performance.performance_setup_helpers import (
    apply_test_configuration_guarded, restore_session_basic_configuration,
    save_session_base_configuration)
from ngts.cli_wrappers.nvue.nvue_performance_clis import get_srv6_dut_left_ports_num


logger = logging.getLogger()


@pytest.fixture(scope='class', autouse=True)
def conf_args(chip_type, players):
    sku = MRCConsts.HWSKU_BY_CHIP_TYPE[chip_type]["spine"]
    is_cumulus = players["dut"].get("is_cumulus", False)
    conf_args = {
        "is_ipv6": True,
        "packet_size": 4096,
        "scenario": "srv6",
        "hwsku": sku,
        "split_left": Cl_Consts.SRV6_SPLIT_LEFT_BY_CHIP_TYPE[chip_type],
        "split_right": Cl_Consts.SRV6_SPLIT_RIGHT_BY_CHIP_TYPE[chip_type],
        "dut_mac": players['dut']['cli'].performance.mac,
        "dut": "spine",
        "chip_type": chip_type,
        "split_left": Cl_Consts.SRV6_SPLIT_LEFT_BY_CHIP_TYPE[chip_type],
        "split_right": Cl_Consts.SRV6_SPLIT_RIGHT_BY_CHIP_TYPE[chip_type],
        "speed": Cl_Consts.SRV6_SPEED_BY_CHIP_TYPE[chip_type] if is_cumulus else SonicConst.HWSKU_DOWNSTREAM_PORTS_SPEED[sku]
    }
    if (is_cumulus and chip_type == "SPC6" and
            conf_args["split_left"] == 8 and conf_args["split_right"] == 8):
        conf_args["link_phy_speed"] = "200G"
    if is_cumulus:
        conf_args["dut_left_ports_num"] = get_srv6_dut_left_ports_num(
            players['dut']['cli'].performance, conf_args["split_left"])
    return conf_args


@pytest.fixture(scope='class', autouse=True)
def basic_setup_configuration(request, players, conf_args):
    baseline_available = False
    primary_error = None
    try:
        with allure.step('Save Players initial Configuration'):
            save_session_base_configuration(players)
            baseline_available = True
        with allure.step("Apply Test configuration on all Players"):
            apply_test_configuration_guarded(
                players, scenario=conf_args["scenario"], conf_args=conf_args,
                nodeid=request.node.nodeid)
        yield
    except Exception as error:
        primary_error = error
        raise
    finally:
        if baseline_available:
            try:
                with allure.step('Restore Base Configuration on all Players'):
                    restore_session_basic_configuration(
                        players, nodeid=request.node.nodeid)
            except Exception:
                if not has_primary_test_failure(primary_error, request.node):
                    raise
                logger.exception(
                    "Baseline restore failed while preserving the primary setup/test failure")
