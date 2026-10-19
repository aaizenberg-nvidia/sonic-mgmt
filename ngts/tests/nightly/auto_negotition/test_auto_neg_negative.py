import random
import logging
import os
import pytest
from retry.api import retry_call

from ngts.cli_util.verify_cli_show_cmd import verify_show_cmd
from ngts.tests.nightly.auto_negotition.conftest import convert_speeds_to_mb_format, get_matched_types, \
    get_interface_cable_width, NO_LOOPBACKS_SKIP_REASON
from ngts.tests.nightly.auto_negotition.auto_neg_common import TestAutoNegBase
from tests.common.plugins.loganalyzer.loganalyzer import LogAnalyzer
from ngts.tests.nightly.conftest import cleanup
from ngts.helpers.interface_helpers import get_lb_mutual_speed, is_uneven_split
from ngts.constants.constants import AutonegCommandConstants, SonicConst
from tests.common.plugins.allure_wrapper import allure_step_wrapper as allure
from ngts.tests.nightly.fec.test_fec import is_port_pam4
from tests.common.plugins.loganalyzer.bug_handler_helper import get_bughandler_instance

logger = logging.getLogger()

ALL_CABLE_TYPES = {'CR', 'CR2', 'CR4', 'SR', 'SR2', 'SR4', 'LR',
                   'LR4', 'KR', 'KR2', 'KR4', 'CAUI', 'GMII',
                   'SFI', 'XLAUI', 'CAUI4', 'XAUI', 'XFI'}

INVALID_SPEED = '30G'
INVALID_INTERFACE_NAME = "EthernetX"
INVALID_AUTO_NEG_MODE = "enable"
INVALID_PORT_ERR_REGEX = r"Invalid\s+port"
INVALID_SPEED_ERR_REGEX = r"Invalid\s+speed\s+specified"
INVALID_AUTO_NEG_MODE_ERR_REGEX = r"Error: Invalid value.*enable.*enabled.*disabled"

EVEN_SPLIT_MODES = (SonicConst.PORT_SPLIT_NUM_1, SonicConst.PORT_SPLIT_NUM_2,
                    SonicConst.PORT_SPLIT_NUM_4, SonicConst.PORT_SPLIT_NUM_8)
NO_SPLIT_LOOPBACK_SKIP_REASON = ("Test is skipped because the test could only run on loopback that is split,"
                                 " the dut does not have such loopback")


@pytest.fixture(autouse=False)
def local_loganalyzer_mismatch_speed_type(duthosts, request):
    """
    This fixture is specific for the mismatch speed and type test.
    It creates a local loganalyzer to catch the SAI errors in the log.
    It also disables the main loganalyzer to avoid the SAI errors in the log.

    :param duthosts: duthosts fixture
    :param request: pytest request object fixture
    :return: None
    """
    assert duthosts, "Need to have loganalyzer enabled to run this test"

    # Create a manual loganalyzer to catch the SAI errors in the log
    local_loganalyzer = LogAnalyzer(ansible_host=duthosts[0], marker_prefix='autoneg_mismatch_local',
                                    request=request, bughandler=get_bughandler_instance({"type": "default"}))
    local_loganalyzer.load_common_config()
    local_loganalyzer.expect_regex.clear()

    # add test specific regexps
    match_regex_list = \
        local_loganalyzer.parse_regexp_file(src=str(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                                 "expected_negative_auto_neg_logs.txt")))
    ignore_regex_list = \
        local_loganalyzer.parse_regexp_file(src=str(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                                 "ignore_negative_auto_neg_logs.txt")))
    local_loganalyzer.expect_regex.extend(match_regex_list)
    local_loganalyzer.ignore_regex.extend(ignore_regex_list)

    yield local_loganalyzer


class TestAutoNegNegative(TestAutoNegBase):

    @pytest.fixture()
    def tested_lb_dict(self, available_lb_dict):
        """
        :param available_lb_dict: available_lb_dict fixture
        :return: the dut loopbacks without the session skip, so the CLI checks run without loopbacks
        """
        return available_lb_dict

    @pytest.fixture()
    def tested_dut_host_lb_dict(self):
        """
        :return: an empty dict, the tests do not use the dut to host loopbacks
        """
        return {}

    def test_negative_config_interface_autoneg(self, negative_cli_port):
        """
        Test command "config interface autoneg <interface_name> <mode>".
        Verify the command return error if given invalid interface

        :return: raise assertion error in case of failure
        """
        with allure.step("Verify the command return error if given invalid auto neg mode"):
            logger.info("Verify the command return error if given invalid auto neg mode.")
            output = \
                self.cli_objects.dut.interface.config_auto_negotiation_mode(negative_cli_port,
                                                                            INVALID_AUTO_NEG_MODE)
            verify_show_cmd(output, [(INVALID_AUTO_NEG_MODE_ERR_REGEX, True)])

        with allure.step("Verify the command return error if given invalid interface_name"):
            logger.info("Verify the command return error if given invalid interface_name")
            output = \
                self.cli_objects.dut.interface.config_auto_negotiation_mode(INVALID_INTERFACE_NAME,
                                                                            "enabled")

            verify_show_cmd(output, [(INVALID_PORT_ERR_REGEX, True)])

    def skip_test_for_pam4(self, conf):
        with allure.step("Skip test for PAM4 ports"):
            for port in conf.keys():
                if is_port_pam4(conf[port]['Width'], conf[port]['Speed']):
                    logger.info("port {} is PAM4".format(port))
                    pytest.skip("Disable autoneg is not supported on PAM4 ports")

    def test_negative_config_advertised_speeds(self, cleanup_list, negative_cli_port):
        """
        Test command config interface advertised-speeds <interface_name> <speed_list>.
        Verify the command return error if given invalid interface name or speed list.
        Verify auto-negotiation fails in case of mismatch advertised speeds list,
        meaning the port should not change speed because ports advertised different speeds.
        port should remain in up state even if the auto negotiation failed.

        :param cleanup_list:  a list of cleanup functions that should be called in the end of the test
        :return: raise assertion error in case of failure
        """
        self.verify_invalid_advertised_speeds_cli(negative_cli_port)
        self.try_even_advertised_speeds_mismatch(cleanup_list)
        self.try_uneven_advertised_speeds_mismatch(cleanup_list)

    def get_mismatch_speed_conf(self, split_mode, lb, lb_mutual_speeds):
        rand_idx = random.choice(range(1, len(lb_mutual_speeds)))
        port_1_adv_speed, port_2_adv_speed = [lb_mutual_speeds[0:rand_idx], lb_mutual_speeds[rand_idx:]]
        tested_lb_dict = {split_mode: [lb]}
        conf = self.generate_default_conf(tested_lb_dict)
        conf[lb[0]][AutonegCommandConstants.ADV_SPEED] = convert_speeds_to_mb_format(port_1_adv_speed)
        conf[lb[1]][AutonegCommandConstants.ADV_SPEED] = convert_speeds_to_mb_format(port_2_adv_speed)
        return conf

    def verify_auto_neg_failure_scenario(self, lb, conf, cleanup_list):
        base_interfaces_speeds = self.cli_objects.dut.interface.get_interfaces_speed(interfaces_list=conf.keys())
        with allure.step("Set auto negotiation mode to disabled on ports"):
            logger.info("Set auto negotiation mode to disabled on ports")
            self.configure_port_auto_neg(self.cli_objects.dut, lb, conf,
                                         cleanup_list, mode='disabled')
        with allure.step("Configure mismatch auto neg values"):
            logger.info("Configure mismatch auto neg values")
            self.configure_ports(self.engines.dut, self.cli_objects.dut, conf, base_interfaces_speeds, cleanup_list)
        with allure.step("Check ports are up while auto neg is disabled"):
            logger.info("Check ports are up while auto neg is disabled")
            retry_call(self.cli_objects.dut.interface.check_ports_status,
                       fargs=[lb], tries=10, delay=10,
                       logger=logger)
        with allure.step("Enable auto neg on ports: {}".format(lb)):
            logger.info("Enable auto neg on ports: {}".format(lb))
            self.configure_port_auto_neg(self.cli_objects.dut, ports_list=lb, conf=conf,
                                         cleanup_list=cleanup_list, mode='enabled')
        with allure.step("verify ports are down due to mismatch"):
            logger.info("verify ports are down due to mismatch")
            retry_call(self.cli_objects.dut.interface.check_ports_status, fargs=[lb, 'down'],
                       tries=10, delay=10, logger=logger)
        with allure.step("Cleanup mismatch configuration"):
            logger.info("Cleanup mismatch configuration")
            cleanup(cleanup_list)
        with allure.step("Enable auto neg on ports: {}".format(lb)):
            logger.info("Enable auto neg on ports: {}".format(lb))
            self.configure_port_auto_neg(self.cli_objects.dut, ports_list=lb, conf=conf,
                                         cleanup_list=cleanup_list, mode='enabled')
        with allure.step("validate ports are up"):
            logger.info("validate ports are up")
            retry_call(self.cli_objects.dut.interface.check_ports_status, fargs=[lb],
                       tries=10, delay=10, logger=logger)

    def test_negative_config_interface_type(self):
        """
        Test command "config interface type <interface_name> <interface_type>".
        Verify the command return error if given invalid interface name.

        the port cable number and split mode including host port
        :return: raise assertion error in case of failure
        """
        logger.info("Verify the command return error if given invalid interface name")
        types_supported_on_dut = []
        interfaces_types_dict = random.choice(list(self.interfaces_types_port_dict.values()))
        for supported_types_dict in interfaces_types_dict.values():
            types_supported_on_dut += supported_types_dict.keys()
        output = self.cli_objects.dut.interface.config_interface_type(INVALID_INTERFACE_NAME,
                                                                      random.choice(types_supported_on_dut))
        verify_show_cmd(output, [(INVALID_PORT_ERR_REGEX, True)])

    def test_negative_config_advertised_types(self, cleanup_list):
        """
        Test command config interface advertised-types <interface_name> <interface_type_list>.
        Verify the command return error if given invalid interface name.
        verify auto-negotiation fails in case of mismatch advertised list.

        :param cleanup_list:  a list of cleanup functions that should be called in the end of the test
        :return: raise assertion error in case of failure
        """
        with allure.step("Verify the command return error if given invalid interface name"):
            logger.info("Verify the command return error if given invalid interface name")
            output = self.cli_objects.dut.interface.config_advertised_interface_types(INVALID_INTERFACE_NAME,
                                                                                      "all")
            verify_show_cmd(output, [(INVALID_PORT_ERR_REGEX, True)])
        self.try_even_advertised_types_mismatch(cleanup_list)
        self.try_uneven_advertised_types_mismatch(cleanup_list)

    def get_mismatch_type_conf(self, split_mode, lb, lb_mutual_types):
        if len(lb_mutual_types) <= 1:
            logger.warning(f"Skipping lb {lb} because it doesn't support more than 1 interface type, "
                           f"supported interfaces type on lb are: {lb_mutual_types}")
            return None
        rand_idx = random.choice(range(1, len(lb_mutual_types)))
        port_1_adv_type, port_2_adv_type = [lb_mutual_types[0:rand_idx], lb_mutual_types[rand_idx:]]
        tested_lb_dict = {split_mode: [lb]}
        conf = self.generate_default_conf(tested_lb_dict)
        conf[lb[0]][AutonegCommandConstants.ADV_TYPES] = ",".join(port_1_adv_type)
        conf[lb[1]][AutonegCommandConstants.ADV_TYPES] = ",".join(port_2_adv_type)
        return conf

    @pytest.mark.disable_loganalyzer
    def test_negative_advertised_speed_type_mismatch(self,
                                                     local_loganalyzer_mismatch_speed_type,
                                                     cleanup_list):
        """
        Verify error in log when configuring mismatch type and speed, like 'CR4' and '10G',
        Verify port state is up when speed and type doesn't match,
        and configuration is not applied because of SAI recognize it as invalid configuration.

        :param cleanup_list:  a list of cleanup functions that should be called in the end of the test
        :return: raise assertion error in case of failure
        """
        marker = local_loganalyzer_mismatch_speed_type.init()
        if self.cli_objects.dut.im.is_im_enabled():
            pytest.skip("Test must run while SW control feature is disabled")
        split_mode = 1
        if not self.tested_lb_dict.get(split_mode):
            pytest.skip(NO_LOOPBACKS_SKIP_REASON)
        first_lb = 0
        lb = self.tested_lb_dict[split_mode][first_lb]
        tested_lb_dict = {1: [lb]}
        conf = self.get_mismatch_speed_type_conf(lb, split_mode, tested_lb_dict)
        self.skip_test_for_pam4(conf)
        for port in lb:
            self.cli_objects.dut.interface.config_advertised_speeds(port, "all")
            self.cli_objects.dut.interface.config_advertised_interface_types(port, "all")
            conf[port]['expected_mlxlink_autoneg'] = "Force"
        logger.info("verify auto-negotiation fails in case of mismatch advertised types and speeds")
        self.configure_port_auto_neg(self.cli_objects.dut, conf.keys(),
                                     conf, cleanup_list, mode='disabled',
                                     set_expected_mlxlink_autoneg=False)
        try:
            self.auto_neg_checker(tested_lb_dict, conf, cleanup_list)
        except AssertionError:
            # ignore any test failures
            # only expecting loganalyzer to catch SAI errors
            pass

        # Disable the autoneg to prevent further error messages in this test
        self.configure_port_auto_neg(self.cli_objects.dut, conf.keys(),
                                     conf, cleanup_list, mode='disabled',
                                     set_expected_mlxlink_autoneg=False)

        # analyze the log and check if the expected SAI errors are in the log
        result = local_loganalyzer_mismatch_speed_type.analyze(marker)
        # Expecting to see all expected error messages
        assert result["total"]["expected_match"] >= len(local_loganalyzer_mismatch_speed_type.expect_regex), \
            "Expecting to match all SAI errors in the log"
        assert len(result['unused_expected_regexp']) == 0, \
            ("Expecting to match and REGEXPs in the log, remaining regexps: {}"
                .format(result['unused_expected_regexp']))

    def check_if_interface_support_max_cr_type(self, port, conf_min_speed, max_type):
        """
        This method is used to check whether interface support specific cr type
        :param conf_min_speed: speed to be tested
        :param max_type: max advertise type, such 'CR4'
        """
        for _, values_dict in self.interfaces_types_port_dict[port].items():
            if values_dict.get(max_type) and conf_min_speed in values_dict.get(max_type):
                pytest.skip("This test is not supported")

    def get_mismatch_speed_type_conf(self, lb, split_mode, tested_lb_dict):
        """
        return configuration with mismatch type and speed, like 'CR4' and '10G',
        and configuration is not applied because of SAI recognize it as invalid configuration.
        so the expected speed, type and width should be the default values configured
        :param lb: a tuple of ports, i.e ('Ethernet4', 'Ethernet8')
        :param split_mode: the port split mode, i.e, 1/2/4
        :param tested_lb_dict: the tested lb dict, i.e, {1: [lb]}
        :return: a dictionary with auto neg configuration for the ports
        """
        conf = self.generate_default_conf(tested_lb_dict, use_min_speed=True)
        conf_min_speed = conf[lb[0]][AutonegCommandConstants.SPEED]
        min_speed_matched_type = get_matched_types(self.ports_lanes_dict[lb[0]], [conf_min_speed],
                                                   types_dict=self.interfaces_types_port_dict[lb[0]]).pop()
        lb_mutual_speeds = get_lb_mutual_speed(lb, split_mode, self.split_mode_supported_speeds)
        lb_mutual_types = get_matched_types(self.ports_lanes_dict[lb[0]], lb_mutual_speeds,
                                            types_dict=self.interfaces_types_port_dict[lb[0]])
        max_type = max(lb_mutual_types, key=get_interface_cable_width)
        if min_speed_matched_type == max_type:
            pytest.skip("This test is not supported")
        self.check_if_interface_support_max_cr_type(lb[0], conf_min_speed, max_type)
        conf[lb[0]][AutonegCommandConstants.ADV_SPEED] = \
            convert_speeds_to_mb_format([conf[lb[0]][AutonegCommandConstants.SPEED]])
        conf[lb[0]][AutonegCommandConstants.ADV_TYPES] = max_type
        for port, port_conf_dir in conf.items():
            conf[port]['expected_speed'] = conf[port][AutonegCommandConstants.SPEED]
            conf[port]['expected_type'] = conf[port][AutonegCommandConstants.TYPE]
            conf[port]['expected_width'] = conf[port][AutonegCommandConstants.WIDTH]
        conf[lb[0]]['expected_autoneg_when_both_enabled'] = 'Force'
        conf[lb[1]]['expected_autoneg_when_both_enabled'] = 'enabled'
        return conf

    def conf_is_pam4(self, conf):
        return any(is_port_pam4(port_conf['Width'], port_conf['Speed']) for port_conf in conf.values())

    def verify_invalid_advertised_speeds_cli(self, port):
        with allure.step("Verify the command return error if given invalid speed list"):
            logger.info("Verify the command return error if given invalid speed list")
            output = self.cli_objects.dut.interface.config_advertised_speeds(port, INVALID_SPEED)
            verify_show_cmd(output, [(INVALID_SPEED_ERR_REGEX, True)])
        with allure.step("Verify the command return error if given invalid interface name"):
            logger.info("Verify the command return error if given invalid interface name")
            output = self.cli_objects.dut.interface.config_advertised_speeds(INVALID_INTERFACE_NAME, "all")
            verify_show_cmd(output, [(INVALID_PORT_ERR_REGEX, True)])

    @property
    def has_uneven_split_lb(self):
        return bool(self.tested_lb_dict.get(SonicConst.PORT_SPLIT_NUM_3))

    def get_first_tested_lb(self, split_mode):
        """
        :param split_mode: the split mode to take the loopback from
        :return: a (split_mode, lb) tuple of the first tested loopback, or None when there is none
        """
        lb_list = self.tested_lb_dict.get(split_mode)
        return (split_mode, lb_list[0]) if lb_list else None

    def choose_even_mismatch_lb(self, min_mutual_speeds):
        """
        :param min_mutual_speeds: the least number of mutual speeds the loopback has to support
        :return: a random (split_mode, lb) tuple of the even-split loopbacks that can be used,
        or None when the dut has no such loopback
        """
        candidates = [(split_mode, lb) for split_mode in EVEN_SPLIT_MODES
                      for lb in self.tested_lb_dict.get(split_mode) or []
                      if len(get_lb_mutual_speed(lb, split_mode,
                                                 self.split_mode_supported_speeds)) >= min_mutual_speeds]
        return random.choice(candidates) if candidates else None

    def get_even_speeds_mismatch_lb(self):
        """
        The advertised speeds mismatch needs a loopback with at least 2 mutual speeds, so that the two
        ports can advertise disjoint speed subsets. Setups without an uneven-split loopback keep using
        the first split-2 loopback, so that their flow is unaffected by the uneven-split support.
        :return: a (split_mode, lb) tuple to run on, or None when the dut has no usable loopback
        """
        if not self.has_uneven_split_lb:
            selected = self.get_first_tested_lb(SonicConst.PORT_SPLIT_NUM_2)
            if not selected:
                pytest.skip(NO_SPLIT_LOOPBACK_SKIP_REASON)
            return selected
        return self.choose_even_mismatch_lb(min_mutual_speeds=2)

    def get_even_types_mismatch_lb(self):
        """
        Setups without an uneven-split loopback keep choosing between the first split-1 and split-2
        loopbacks, so that their flow is unaffected by the uneven-split support.
        :return: a (split_mode, lb) tuple to run on, or None when the dut has no usable loopback
        """
        if not self.has_uneven_split_lb:
            possible_split_modes = [SonicConst.PORT_SPLIT_NUM_1]
            if self.tested_lb_dict.get(SonicConst.PORT_SPLIT_NUM_2):
                possible_split_modes.append(SonicConst.PORT_SPLIT_NUM_2)
            return self.get_first_tested_lb(random.choice(possible_split_modes))
        return self.choose_even_mismatch_lb(min_mutual_speeds=1)

    def try_advertised_speeds_mismatch(self, split_mode, lb, cleanup_list):
        """
        Verify auto-negotiation fails when the two loopback ports advertise disjoint speed subsets.
        PAM4 ports cannot have auto-neg disabled, so such a loopback is not usable for this check. It
        only skips the whole test when there is no uneven-split loopback left to check instead.
        :param split_mode: the split mode of the loopback
        :param lb: the loopback to run on
        :param cleanup_list: a list of cleanup functions that should be called in the end of the test
        :return: True if the mismatch scenario ran, False otherwise
        """
        lb_mutual_speeds = get_lb_mutual_speed(lb, split_mode, self.split_mode_supported_speeds)
        conf = self.get_mismatch_speed_conf(split_mode, lb, lb_mutual_speeds)
        if not is_uneven_split(split_mode) and not self.tested_lb_dict.get(SonicConst.PORT_SPLIT_NUM_3):
            self.skip_test_for_pam4(conf)
        if self.conf_is_pam4(conf):
            logger.info("Skip advertised-speeds mismatch on lb %s: disable autoneg is not supported on PAM4", lb)
            return False
        with allure.step("Verify auto-negotiation fails in case of mismatch advertised speeds"):
            logger.info("Verify auto-negotiation fails in case of mismatch advertised speeds")
            self.verify_auto_neg_failure_scenario(lb, conf, cleanup_list)
        return True

    def try_even_advertised_speeds_mismatch(self, cleanup_list):
        selected = self.get_even_speeds_mismatch_lb()
        if not selected:
            return False
        split_mode, lb = selected
        return self.try_advertised_speeds_mismatch(split_mode, lb, cleanup_list)

    def try_uneven_advertised_speeds_mismatch(self, cleanup_list):
        split_mode = SonicConst.PORT_SPLIT_NUM_3
        if not self.tested_lb_dict.get(split_mode):
            return False
        return self.try_advertised_speeds_mismatch(split_mode, self.tested_lb_dict[split_mode][0], cleanup_list)

    def try_even_advertised_types_mismatch(self, cleanup_list):
        selected = self.get_even_types_mismatch_lb()
        if not selected:
            return False
        split_mode, lb = selected
        lb_mutual_speeds = get_lb_mutual_speed(lb, split_mode, self.split_mode_supported_speeds)
        lb_mutual_types = get_matched_types(self.ports_lanes_dict[lb[0]], lb_mutual_speeds,
                                            types_dict=self.interfaces_types_port_dict[lb[0]])
        conf = self.get_mismatch_type_conf(split_mode, lb, list(lb_mutual_types))
        if conf and not self.conf_is_pam4(conf):
            with allure.step("verify auto-negotiation fails in case of mismatch advertised types"):
                logger.info("verify auto-negotiation fails in case of mismatch advertised types")
                self.verify_auto_neg_failure_scenario(lb, conf, cleanup_list)
            return True
        if not self.tested_lb_dict.get(SonicConst.PORT_SPLIT_NUM_3):
            if conf is None:
                pytest.skip(f"This test is not supported because lb {lb} doesn't support more than 1 "
                            f"interface type, supported interfaces type on lb are: {lb_mutual_types}")
            self.skip_test_for_pam4(conf)
        return False

    def try_uneven_advertised_types_mismatch(self, cleanup_list):
        split_mode = SonicConst.PORT_SPLIT_NUM_3
        if not self.tested_lb_dict.get(split_mode):
            return False
        lb = self.tested_lb_dict[split_mode][0]
        lb_mutual_speeds = get_lb_mutual_speed(lb, split_mode, self.split_mode_supported_speeds)
        lb_mutual_types = list(
            get_matched_types(self.ports_lanes_dict[lb[0]], lb_mutual_speeds,
                              types_dict=self.interfaces_types_port_dict[lb[0]]) &
            get_matched_types(self.ports_lanes_dict[lb[1]], lb_mutual_speeds,
                              types_dict=self.interfaces_types_port_dict[lb[1]]))
        conf = self.get_mismatch_type_conf(split_mode, lb, lb_mutual_types)
        if conf is None or self.conf_is_pam4(conf):
            logger.info("Skip advertised-types mismatch on split-3 lb %s", lb)
            return False
        with allure.step("verify auto-negotiation fails in case of mismatch advertised types"):
            logger.info("verify auto-negotiation fails in case of mismatch advertised types")
            self.verify_auto_neg_failure_scenario(lb, conf, cleanup_list)
        return True
