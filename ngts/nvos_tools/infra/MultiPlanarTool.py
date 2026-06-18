import logging
import random
from functools import lru_cache
from typing import Tuple, List

from retry import retry

from ngts.nvos_constants.constants_nvos import MultiPlanarConsts, PlatformConsts, SystemConsts
from ngts.nvos_tools.Devices.IbDevice import CrocodileSwitch
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port, PortRequirements
from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import IbInterfaceConsts, NvosConsts
from ngts.nvos_tools.infra.Fae import Fae
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.RandomizationTool import RandomizationTool
from ngts.nvos_tools.platform.Platform import Platform
from ngts.nvos_tools.infra.RegressionConfigurations import RegressionLinks
from ngts.nvos_tools.infra.Tools import Tools
from ngts.nvos_tools.system.System import System
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger()


class MultiPlanarTool:

    @staticmethod
    def override_platform_file(system, engines, devices, new_platform):
        """
        override platform file on switch.
        """
        if devices.dut.asic_amount == 1:
            engine = engines.dut
            device = devices.dut
            player = engines['sonic_mgmt']

            # in case of installing xdr simulation, save the origin file in order to restore at the end of the test
            if new_platform != MultiPlanarConsts.ORIGIN_FILE:
                with allure.step("Save the origin platform.json file in tmp folder"):
                    engine.run_cmd("sudo cp {} {}{}".format(device.platform_file_path, MultiPlanarConsts.INTERNAL_PATH,
                                                            MultiPlanarConsts.ORIGIN_FILE))

                with allure.step("Override platform.json file"):
                    file_path = MultiPlanarConsts.SIMULATION_PATH + new_platform
                    player.upload_file_using_scp(dest_username=device.default_username,
                                                 dest_password=device.default_password,
                                                 dest_folder=MultiPlanarConsts.INTERNAL_PATH,
                                                 dest_ip=engine.ip,
                                                 local_file_path=file_path)

                    engine.run_cmd("sudo mv {}{} {}".format(MultiPlanarConsts.INTERNAL_PATH, new_platform,
                                                            device.platform_file_path))
            else:
                with allure.step("Restore the origin platform.json file"):
                    engine.run_cmd("sudo mv {}{} {}".format(MultiPlanarConsts.INTERNAL_PATH,
                                                            MultiPlanarConsts.ORIGIN_FILE,
                                                            device.platform_file_path))

            with allure.step("Remove config_db.json and port_mapping.json files"):
                engine.run_cmd("sudo rm -f /etc/sonic/config_db.json")
                engine.run_cmd("sudo rm -f /etc/sonic/port_mapping.json")

            with allure.step("Perform system reboot"):
                system.reboot.action_reboot(params='force').verify_result()

    @staticmethod
    def select_random_aggregated_port(device, setup_name):
        with allure.step("Select a random aggregated port"):
            if isinstance(device, CrocodileSwitch):
                with allure.step("Get random aggregated port for Crocodile systems"):
                    with allure.step("Get all aggregated ports"):
                        port_requirements = PortRequirements()
                        port_requirements.set_port_ib_speed(IbInterfaceConsts.XDR)
                        selected_up_ports = Tools.RandomizationTool.select_random_ports(
                            requested_ports_state=NvosConsts.LINK_STATE_UP,
                            requested_ports_type=device.switch_type.lower(),
                            port_requirements_object=port_requirements,
                            num_of_ports_to_select=0)
                        with allure.step('Assert XDR ports found'):
                            assert selected_up_ports, 'No XDR ports are currently UP or available for selection'
                            selected_up_ports = selected_up_ports.get_returned_value()
                        ports_connected = RegressionLinks.get_filtered_ports_list(setup_name=setup_name, is_loopback=True)
                        assert ports_connected, 'Aggregated_port ports not found'
                    with allure.step('Pick random port from ports_connected that are UP'):
                        up_port_names = {p.name for p in selected_up_ports}
                        ports_connected_and_up = {k: v for k, v in ports_connected.items()
                                                  if k in up_port_names and v in up_port_names}
                        assert ports_connected_and_up, 'No UP ports connected in loopback found'
                    port_name = Tools.RandomizationTool.select_random_value(
                        ports_connected_and_up).get_returned_value()
            else:
                port_name = RandomizationTool.select_random_port(
                    requested_ports_logical_state=IbInterfaceConsts.LINK_LOGICAL_PORT_STATE_ACTIVE
                ).get_returned_value().name
        allure.attach(f"Selected port: {port_name}")
        return Fae(port_name=port_name)

    @staticmethod
    def select_random_fnm_port_and_plane(device):
        with allure.step("Select a random fnm port"):
            fnm_port_name = RandomizationTool.select_random_value(device.fnm_port_list).get_returned_value()
            selected_fae_fnm_port = Fae(port_name=fnm_port_name)
        with allure.step("Select a random internal fnm port"):
            internal_fnm_port_name = RandomizationTool.select_random_value(
                device.interface_active_internal_fnm_ports).get_returned_value()
            selected_fae_internal_fnm_port = Fae(port_name=internal_fnm_port_name)
        allure.attach(f"Selected port and plane: {fnm_port_name}, {internal_fnm_port_name}")
        return selected_fae_fnm_port, selected_fae_internal_fnm_port

    @staticmethod
    def select_random_plane_port(fae_aggregated_port, num_of_planes_on_port=None, device=None) -> Fae:
        return MultiPlanarTool.select_random_plane_ports(
            fae_aggregated_port, num_of_planes_to_return=1, num_of_planes_on_port=num_of_planes_on_port, device=device
        )[0]

    @staticmethod
    def select_random_plane_ports(fae_aggregated_port, num_of_planes_to_return, num_of_planes_on_port=None, device=None
                                  ) -> List[Fae]:
        if num_of_planes_on_port is None:
            num_of_planes_on_port = (device or TestToolkit.get_device()).num_of_plane_ports
        with allure.step(f"Choose {num_of_planes_to_return} random plane ports (of the aggregated port)"):
            plane_num = random.sample(range(1, num_of_planes_on_port + 1), num_of_planes_to_return)
            result = [Fae(port_name=f"{fae_aggregated_port.port.name}pl{p}") for p in plane_num]
            allure.attach(f"Selected plane-ports", result)
        return result

    @staticmethod
    def enumerate_plane_ports(aport_name: str, num_planes: int) -> List[Port]:
        """Return the plane-port ``Port`` objects for ``aport_name`` (planes 1..num_planes)."""
        if num_planes < 1:
            raise ValueError(f"num_planes must be >= 1; got {num_planes}")
        aport = Port(aport_name)
        return [aport.get_plane_port(plane_index) for plane_index in range(1, num_planes + 1)]

    @staticmethod
    def select_random_port_and_plane(device, setup_name) -> Tuple[Port, Port, Port]:
        with allure.step("Select a random aggregated port (connected in loop back to another port)"):
            selected_fae_aggregated_port = MultiPlanarTool.select_random_aggregated_port(device, setup_name)
            selected_aggregated_port = Port(selected_fae_aggregated_port.port.name)
        with allure.step("Select a random plane port"):
            selected_fae_plane_port = MultiPlanarTool.select_random_plane_port(selected_fae_aggregated_port,
                                                                               device.num_of_plane_ports)
        return selected_aggregated_port, selected_fae_aggregated_port.port, selected_fae_plane_port.port

    @staticmethod
    @retry(Exception, tries=4, delay=2)
    def _get_split_ports(port="swA10p1"):
        all_ports = Port.get_list_of_ports()
        split_ports = []
        split_port_names = [port]
        for port_iterator in all_ports:
            if port_iterator.name in split_port_names:
                split_ports.append(port_iterator)
        if not split_ports:
            raise Exception("Didn't find split ports on the system")
        return split_ports

    @staticmethod
    def _get_split_child_ports(parent_port):
        list_of_all_ports = Port.get_list_of_ports()
        child_ports = []
        for port in list_of_all_ports:
            if parent_port.name in port.name and port.name[-2] == 's':
                child_ports.append(port)
        return child_ports

    @staticmethod
    @lru_cache
    def get_asic_conf_dict(engine):
        """
        Parses asic.conf file to dict
            NUM_ASIC = 4
            DEV_ID_ASIC_0 = 05:00.0
            DEV_ID_ASIC_1 = 04:00.0
            DEV_ID_ASIC_2 = 03:00.0
            DEV_ID_ASIC_3 = 09:00.0
        """
        asic_conf = dict()

        platform = Platform()
        platform_info = OutputParsingTool.parse_json_str_to_dictionary(
            platform.show()).get_returned_value()
        asic_conf_path = PlatformConsts.ASIC_CONF_FILE_PATH.format(platform_info[PlatformConsts.SYSTEM_TYPE].lower())
        with allure.step(f"Generate asic conf dictionary from {asic_conf_path}"):
            asic_conf_values = engine.run_cmd(f"cat {asic_conf_path}")
            for line in asic_conf_values.split('\n'):
                line = line.strip()

                if not line or '=' not in line:
                    continue

                asic_dev_id, value = line.split('=')

                asic_conf[asic_dev_id] = value

            logger.info(f"{asic_conf=}")
            return asic_conf

    @staticmethod
    def asic_letter_to_number(letter):
        return ord(letter) - ord('A')

    @staticmethod
    @lru_cache
    def get_primary_asic(fae):  # todo: use port_name instead and use nv_command inside the function
        """
        Returns the primary ASIC as reported by nv show fae interface <port>
        Note the nv command's different behavior for different ports and devices:

        Aggregated port (Mamba or Crocodile):
        admin@mamba-248-mgmt2:~$ nv sh fae int sw1p1 | grep asic
        asic                                 ['0', '1', '2', '3']
        primary-asic                         0                        <-- return this

        Crocodile plane port:
        admin@croc-61-mgmt2:~$ nv sh fae int swA2p1pl1 | grep asic
        asic                                 0
        primary-asic                         0                        <-- return this

        Mamba plane-port for the plane matching the primary asic:
        admin@mamba-248-mgmt2:~$ nv sh fae int sw1p1pl1 | grep asic
        asic                                 0
        primary-asic                         0                        <-- return this

        Mamba plane-port for another plane:
        admin@mamba-248-mgmt2:~$ nv sh fae int sw1p1pl2 | grep asic
        asic                                 1                        <-- return this (primary-asic field doesn't exist)
        """
        output_fae_port = OutputParsingTool.parse_show_interface_output_to_dictionary(
            fae.interface.show()).get_returned_value()
        return (output_fae_port.get(IbInterfaceConsts.PRIMARY_ASIC) or
                output_fae_port.get(IbInterfaceConsts.ASIC) or "0")
