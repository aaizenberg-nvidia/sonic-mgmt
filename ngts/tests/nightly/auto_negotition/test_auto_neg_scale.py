import logging
import pytest

from ngts.constants.constants import AutonegCommandConstants
from ngts.tests.nightly.auto_negotition.auto_neg_common import TestAutoNegBase
from ngts.tests.conftest import get_dut_loopbacks, get_dut_host_loopbacks
from ngts.tests.nightly.auto_negotition.conftest import update_split_2_if_possible, \
    update_split_3_if_possible, update_split_4_if_possible, update_split_8_if_possible, \
    ports_spec_compliance, is_auto_neg_supported_lb, is_auto_neg_supported_port, \
    NO_LOOPBACKS_SKIP_REASON

logger = logging.getLogger()


@pytest.fixture()
def tested_lb_all_dict(topology_obj, engines, interfaces, split_mode_supported_speeds, ports_spec_compliance):
    """
    This function return a dictionary with all the switch ports for each split mode.
    :param topology_obj: topology fixture object
    :param interfaces: a dictionary with dut <-> hosts interfaces
    :param ports_spec_compliance: ports_spec_compliance fixture
    :return: a dictionary with all the switch ports for each split mode., i.e,

    {1: [('Ethernet4', 'Ethernet8'), ('Ethernet36', 'Ethernet40'), ('Ethernet48', 'Ethernet44'),
         ('Ethernet52', 'Ethernet56'), ('Ethernet32', 'enp4s0f0'), ('Ethernet60', 'enp4s0f1'),
         ('Ethernet0', 'enp6s0f0'), ('Ethernet28', 'enp6s0f1')],
    2: [('Ethernet12', 'Ethernet16'), ('Ethernet14', 'Ethernet18')],
    4: [('Ethernet20', 'Ethernet24'), ('Ethernet21', 'Ethernet25'),
        ('Ethernet22', 'Ethernet26'), ('Ethernet23', 'Ethernet27')]}
    """
    tested_lb_dict = {
        1: []
    }
    if 'simx' not in engines.dut.run_cmd("hostname"):
        update_split_2_if_possible(topology_obj, tested_lb_dict, ports_spec_compliance)
        update_split_3_if_possible(topology_obj, split_mode_supported_speeds, tested_lb_dict, ports_spec_compliance)
        update_split_4_if_possible(topology_obj, split_mode_supported_speeds, tested_lb_dict, ports_spec_compliance)
        update_split_8_if_possible(topology_obj, split_mode_supported_speeds, tested_lb_dict, ports_spec_compliance)
    for lb in get_dut_loopbacks(topology_obj):
        if is_auto_neg_supported_lb(lb, ports_spec_compliance):
            tested_lb_dict[1].append(lb)
    for lb in get_dut_host_loopbacks(interfaces):
        if is_auto_neg_supported_port(lb[0], ports_spec_compliance):
            tested_lb_dict[1].append(lb)
    return tested_lb_dict


class TestAutoNegScale(TestAutoNegBase):

    @pytest.fixture()
    def tested_lb_dict(self, available_lb_dict):
        """
        :param available_lb_dict: available_lb_dict fixture
        :return: the dut loopbacks without the session skip, the test runs on tested_lb_all_dict
        """
        return available_lb_dict

    @pytest.fixture()
    def tested_dut_host_lb_dict(self):
        """
        :return: an empty dict, the test does not use the dut to host loopbacks
        """
        return {}

    def test_scale(self, cleanup_list, tested_lb_all_dict, skip_if_rj45_cable):
        """
        The following test checks:
        1) configure the smallest speed, type on all interfaces
        2) configure all advertised speeds, types on all interfaces
        3) enable auto-negotiation
        4) verify speed, type was modified for all ports

        :param cleanup_list: a list of cleanup functions that should be called in the end of the test
        :return: raise assertion error in case of failure
        """
        conf = self.generate_default_conf(tested_lb_all_dict, use_min_speed=True)
        ports = self.topology_obj.players_all_ports['dut']
        dut_conf = dict()
        for port in ports:
            if port in conf:
                dut_conf[port] = conf[port]
        if not dut_conf:
            pytest.skip(NO_LOOPBACKS_SKIP_REASON)
        ports = dut_conf.keys()
        logger.info("Set auto negotiation mode to disabled on ports before test starts")
        self.configure_port_auto_neg(self.cli_objects.dut, ports,
                                     dut_conf, cleanup_list, mode='disabled')
        logger.info("Get ports default speed settings")
        base_interfaces_speeds = self.cli_objects.dut.interface.get_interfaces_speed(ports)
        logger.info("configure the smallest speed/type and configure all advertised speeds/types on all interfaces")
        self.configure_ports(self.engines.dut, self.cli_objects.dut, dut_conf, base_interfaces_speeds, cleanup_list)
        for port, port_conf_dict in dut_conf.items():
            conf[port]['expected_mlxlink_autoneg'] = "Force"
        logger.info("Check auto negotiation was configured correctly")
        self.verify_auto_neg_configuration(dut_conf, check_adv_parm=False)
        logger.info("Set auto negotiation mode to enabled on all ports")
        self.configure_port_auto_neg(self.cli_objects.dut, ports, dut_conf,
                                     cleanup_list, mode='enabled')
        self.configure_auto_fec(ports=ports)
        self.cli_objects.dut.interface.disable_interfaces(dut_conf.keys())
        self.cli_objects.dut.interface.enable_interfaces(dut_conf.keys())
        for port, port_conf_dict in dut_conf.items():
            port_conf_dict[AutonegCommandConstants.SPEED] = dut_conf[port]['expected_speed']
            port_conf_dict[AutonegCommandConstants.TYPE] = dut_conf[port]['expected_type']
            port_conf_dict[AutonegCommandConstants.WIDTH] = dut_conf[port]['expected_width']
            port_conf_dict[AutonegCommandConstants.OPER] = "up"
            port_conf_dict[AutonegCommandConstants.ADMIN] = "up"
        logger.info("verify speed, type was modified for all ports")
        self.verify_auto_neg_configuration(dut_conf)
        self.auto_fec_checker(dut_conf)
