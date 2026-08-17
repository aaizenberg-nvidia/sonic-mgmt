import re

from ngts.constants.constants import SflowConsts


class SonicSflowCli:
    """
    This class hosts SONiC Sflow cli methods
    """

    def __init__(self, engine):
        self.engine = engine

    def enable_sflow_feature(self):
        """
        This method is used to enable sflow feature
        :return: the output of cli command
        """
        return self.engine.run_cmd(f'sudo config feature state {SflowConsts.SFLOW_FEATURE_NAME} enabled')

    def disable_sflow_feature(self):
        """
        This method is used to disable sflow feature
        :return: the output of cli command
        """
        return self.engine.run_cmd(f'sudo config feature state sflow disabled')

    def enable_sflow(self):
        """
        This method is used to enable sflow
        :return: the output of cli command
        """
        return self.engine.run_cmd(f'sudo config sflow enable')

    def disable_sflow(self):
        """
        This method is used to disable sflow
        :return: the output of cli command
        """
        return self.engine.run_cmd(f'sudo config sflow disable')

    def is_sflow_enabled(self):
        """
        Check whether the global sFlow admin state is up.
        :return: True when sFlow is globally enabled, otherwise False
        """
        return bool(re.search(r'sFlow Admin State:\s+up', self.show_sflow()))

    def add_collector(self, collector, ip, port=SflowConsts.DEFAULT_UDP, vrf=SflowConsts.VRF_DEFAULT, validate=True):
        """
        This method is to configure collector
        :param collector: collector name
        :param ip: ip address
        :param port: udp port
        :param vrf: vrf
        :return: the output of cli command
        """
        return self.engine.run_cmd(f'sudo config sflow collector add {collector} {ip} --port {port} --vrf {vrf}', validate=validate)

    def del_collector(self, collector):
        """
        This method is used to delete collector
        :param collector: collector name
        :return: the output of cli command
        """
        return self.engine.run_cmd(f'sudo config sflow collector del {collector}')

    def add_agent_id(self, interface_name):
        """
        This method is used to add agent id
        :param interface_name: interface name
        :return: the output of cli command
        """
        return self.engine.run_cmd(f'sudo config sflow agent-id add {interface_name}')

    def del_agent_id(self):
        """
        This method is used to delete agent id
        :return: the output of cli command
        """
        return self.engine.run_cmd(f'sudo config sflow agent-id del')

    def enable_sflow_interface(self, interface_name):
        """
        This method is used to enable sflow interface
        :param interface_name: interface name
        :return: the output of cli command
        """
        return self.engine.run_cmd(f'sudo config sflow interface enable {interface_name}')

    def enable_all_sflow_interface(self):
        """
        This method is used to enable all sflow interface
        :return: the output of cli command
        """
        return self.engine.run_cmd(f'sudo config sflow interface enable all')

    def disable_sflow_interface(self, interface_name):
        """
        This method is used to disable sflow interface
        :param interface_name: interface name
        :return: the output of cli command
        """
        return self.engine.run_cmd(f'sudo config sflow interface disable {interface_name}')

    def disable_all_sflow_interface(self):
        """
        This method is used to disable all sflow interface
        :return: the output of cli command
        """
        return self.engine.run_cmd(f'sudo config sflow interface disable all')

    def config_sflow_interface_sample_rate(self, interface_name, sample_rate):
        """
        This method is used to configure sflow interface sample rate
        :param interface_name: interface name
        :param sample_rate: sample rate, range from 256 to 8388608
        :return: the output of cli method
        """
        return self.engine.run_cmd(f'sudo config sflow interface sample-rate {interface_name} {sample_rate}')

    def config_sflow_polling_interval(self, polling_interval):
        """
        This method is used to configure sflow counter polling interval for all interfaces
        :param polling_interval: counter polling interval, validate range is 0 or 5 to 300 seconds
        :return: the output of cli command
        """
        return self.engine.run_cmd(f'sudo config sflow polling-interval {polling_interval}')

    def show_sflow(self):
        """
        This method is used to show sflow configuration
        :return: the output of cli command
        """
        return self.engine.run_cmd(f'show sflow')

    def show_sflow_interface(self):
        """
        This method is used to show sflow interface configuration
        :return: the output of cli command
        """
        return self.engine.run_cmd(f'show sflow interface')
