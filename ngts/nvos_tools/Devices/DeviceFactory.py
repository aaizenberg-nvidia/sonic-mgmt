import logging

from dotted_dict import DottedDict

from ngts.helpers.object_filters import filter_objects
from ngts.nvos_tools.Devices.EthDevice import Mlx2410Switch, Mlx4600Switch, Mlx4600cSwitch, Mlx4700Switch, \
    Mlx5600Switch, Mlx6600Switch, \
    Mlx5400Switch, Mlx4410Switch, Mlx3750sxSwitch, Mlx3700csSwitch, Mlx3700cSwitch, Mlx3420Switch, Mlx2700Switch, \
    Mlx2201Switch, Mlx2100Switch, \
    Mlx2010Switch, Mlx3700Switch, Mlx5640Switch
from ngts.nvos_tools.Devices.IbDevice import (GorillaSwitch, GorillaSwitchBF3, CrocodileSwitch, BlackMambaSwitch,
                                              BlackMambaDGXSwitch, CrocodileSimxSwitch, JulietScaleoutSwitch,
                                              JulietScaleoutSunbirdSwitch, JulietTTMSwitch, JulietNonScaleoutSwitch,
                                              JulietAriel, JulietNonScaleoutSwitchNoNCI, JulietArielPS,
                                              JulietNonScaleoutSwitchNoNCI5600, JulietNonScaleoutSwitchGB300,
                                              JulietNonScaleoutSwitchGB300QS, JulietNonScaleoutSwitchGB300PS,
                                              RosalindSimx, RosalindSwitch, RosalindSurrogateSwitch,
                                              RosalindStackedSwitch, RosalindStackedSimx, RosalindChipless,
                                              TaipanSwitch, TaipanSingleAsicSwitch, PortiaSwitch, PortiaSimx,
                                              PortiaCpoSwitch, PortiaCpoSimx, PortiaCpoTASimx,
                                              PortiaNpoSwitch, PortiaNpoSimx, PortiaNpoTASimx,
                                              RosalindSA, RosalindRTF)

logger = logging.getLogger()


class DeviceFactory:
    device_type_dict = \
        {
            'MQM9700 - Gorilla Blackbird': GorillaSwitch,
            'MQM9700 - Gorilla BF3': GorillaSwitchBF3,
            'MQM9700': GorillaSwitch,
            'MSN3700': Mlx3700Switch,
            'MSN3700 - Anaconda': Mlx3700Switch,
            'Q3200-RA-Crocodile Sunbird': CrocodileSwitch,
            'QM3400': CrocodileSwitch,
            'QM3400 - Crocodile': CrocodileSwitch,
            'QM3400_simx - Crocodile': CrocodileSwitch,
            'QM8790 - Black Mamba': BlackMambaSwitch,
            'QM3000 - Black Mamba': BlackMambaSwitch,
            'Q3400-RA Black Mamba': BlackMambaSwitch,
            'Q3400-RA Black_Mamba': BlackMambaSwitch,
            'Q3401-RD Black Mamba DGX': BlackMambaDGXSwitch,
            'Mellanox SN5600': Mlx5600Switch,
            'Mellanox SN5640': Mlx5640Switch,
            'Mellanox SN6600': Mlx6600Switch,
            'Mellanox SN5400': Mlx5400Switch,
            'Mellanox SN4700': Mlx4700Switch,
            'Mellanox SN4600': Mlx4600Switch,
            'Mellanox SN4600c': Mlx4600cSwitch,
            'Mellanox SN4410': Mlx4410Switch,
            'Mellanox SN3750sx': Mlx3750sxSwitch,
            'Mellanox SN3700': Mlx3700Switch,
            'Mellanox SN3700cs': Mlx3700csSwitch,
            'Mellanox SN3700c': Mlx3700cSwitch,
            'Mellanox SN3420': Mlx3420Switch,
            'Mellanox 2700': Mlx2700Switch,
            'Mellanox 2410': Mlx2410Switch,
            'Mellanox 2201': Mlx2201Switch,
            'Mellanox 2100': Mlx2100Switch,
            'Mellanox 2010': Mlx2010Switch,
            'N5110_LD - JulietScaleout': JulietScaleoutSwitch,
            'N5110_LD - JulietScaleoutSunbirdSwitch': JulietScaleoutSunbirdSwitch,
            'N5110_LD - JulietTTM': JulietTTMSwitch,
            'N5100_LD - JulietNonScaleout': JulietNonScaleoutSwitch,
            'N5112_LD - JulietAriel': JulietAriel,
            'N5200_LD - JulietNonScaleoutSwitchNoNCI': JulietNonScaleoutSwitchNoNCI,
            'N5112_LD - JulietArielPS': JulietArielPS,
            'N5600_LD - JulietNonScaleoutSwitchNoNCI': JulietNonScaleoutSwitchNoNCI5600,
            'Q3450_LD - Taipan': TaipanSwitch,
            'Q3451_LD - TaipanSingleAsic': TaipanSingleAsicSwitch,
            'N5500_LD - JulietNonScaleoutSwitchGB300': JulietNonScaleoutSwitchGB300,
            'N5500_LD - JulietNonScaleoutSwitchGB300QS': JulietNonScaleoutSwitchGB300QS,
            'N5500_LD - JulietNonScaleoutSwitchGB300PS': JulietNonScaleoutSwitchGB300PS,
            'N6100_LD - Rosalind': RosalindSwitch,
            'N6100_LD - RTF': RosalindRTF,
            'N6150_LD - RosalindSurrogate': RosalindSurrogateSwitch,
            'N6100_LD - RosalindChipless': RosalindChipless,
            'N6100_LD_simx - Rosalind': RosalindSimx,
            "N6100_LD_simx - Rosalind_SA": RosalindSA,
            "N6300_LD - RosalindStacked": RosalindStackedSwitch,
            "N6300_LD_simx - RosalindStacked": RosalindStackedSimx,
            "N7200_LD - Portia": PortiaSwitch,
            "N7200_LD_simx - Portia": PortiaSimx,
            "N7300_LD - Portia": PortiaCpoSwitch,
            "N7300_LD_simx - Portia": PortiaCpoSimx,
            "N7300_LD_TA_simx - Portia": PortiaCpoTASimx,
            "N7400_LD - Portia": PortiaNpoSwitch,
            "N7400_LD_simx - Portia": PortiaNpoSimx,
            "N7400_LD_TA_simx - Portia": PortiaNpoTASimx,
        }

    @staticmethod
    def create_device(device_name):
        try:
            if device_name not in DeviceFactory.device_type_dict.keys():
                if "6600" in device_name:
                    device_name = 'Mellanox SN6600'
                elif "5600" in device_name:
                    device_name = 'Mellanox SN5600'
                elif "5640" in device_name:
                    device_name = 'Mellanox SN5640'
                elif "4600C" in device_name:
                    device_name = 'Mellanox SN4600c'
                elif "_LD-Juliet" in device_name:
                    device_name = device_name.replace('_LD-Juliet', '_LD - Juliet')
                elif "simx-" in device_name:
                    device_name = device_name.replace('simx-', 'simx - ')
                else:
                    device_name = device_name[0:7]
            instance_type = DeviceFactory.device_type_dict[device_name]
            instance = instance_type()
            logger.info('Received switch type {device_name}, created Device instance {instance_type}'.format(
                device_name=device_name, instance_type=str(instance_type)))
            return instance
        except Exception:
            logger.error("please configure device_name = %s", device_name)
            raise

    @staticmethod
    def create_devices_object(topology_obj):
        """
        Create device objects for all DUTs in the topology.

        :param topology_obj: Topology object containing player information
        :return: DottedDict with device objects keyed by DUT name (dut, dut2, etc.)
        """
        device_objects = DottedDict()
        for player_name, player in filter_objects(topology_obj.players, host_type="dut", engine_type="ssh").items():
            dev_name = player['attributes'].noga_query_data['attributes']['Specific']['switch_type']
            device_objects[player_name] = DeviceFactory.create_device(dev_name)
        return device_objects
