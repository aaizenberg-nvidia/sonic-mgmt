import logging
import time
from typing import Dict
from typing import List

import requests

from devts.infra.tools.validations.traffic_validations.port_check.port_checker import check_port_status_till_alive
from ngts.cli_wrappers.nvue.nvue_base_clis import NvueBaseCli
from ngts.cli_wrappers.nvue.nvue_cluster_clis import NvueClusterCli
from ngts.cli_wrappers.nvue.nvue_platform_clis import NvuePlatformCli
from ngts.cli_wrappers.nvue.nvue_system_clis import NvueSystemCli
from ngts.cli_wrappers.openapi.openapi_base_clis import OpenApiBaseCli
from ngts.cli_wrappers.openapi.openapi_cluster_clis import OpenApiClusterCli
from ngts.cli_wrappers.openapi.openapi_platform_clis import OpenApiPlatformCli
from ngts.cli_wrappers.openapi.openapi_system_clis import OpenApiSystemCli
from ngts.nvos_constants.constants_nvos import ApiType, ActionConsts
from ngts.nvos_tools.fae.Debug import Debug
from ngts.nvos_tools.fae.Asic import Asic
from ngts.nvos_tools.fae.FaePowerCapping import FaePowerCapping
from ngts.nvos_tools.fae.SecureState import SecureState
from ngts.nvos_tools.ib.InterfaceConfiguration.Interface import Interface
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port
from ngts.nvos_tools.infra.BaseComponent import BaseComponent
from ngts.nvos_tools.infra.DefaultDict import DefaultDict
from ngts.nvos_tools.infra.DutUtilsTool import DutUtilsTool
from ngts.nvos_tools.infra.ErotComponent import ErotComponent
from ngts.nvos_tools.infra.SmaComponent import SmaComponent
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.PortFastRecovery import PortFastRecovery
from ngts.nvos_tools.infra.ResultObj import ResultObj
from ngts.nvos_tools.infra.SendCommandTool import SendCommandTool
from ngts.nvos_tools.platform.Cpo import Cpo
from ngts.nvos_tools.platform.LaserSource import LaserSource
from ngts.nvos_tools.system.Files import Files
from ngts.nvos_tools.system.Health import Health
from ngts.nvos_tools.system.Profile import Profile
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger()


class Fae(BaseComponent):
    def __init__(self, parent_obj=None, port_name='eth0'):
        super().__init__(parent=parent_obj,
                         api={ApiType.NVUE: NvueBaseCli, ApiType.OPENAPI: OpenApiBaseCli}, path='/fae')
        self.system = FaeSystem(self)
        self.ipoibmapping = BaseComponent(self, path='/ipoib-mapping')
        self.health = Health(self)
        self.port = Port(port_name, parent_obj=self)
        self.fast_recovery = PortFastRecovery(self)
        self.ib = Ib(self)
        self.sonic_cli = SonicCli(self)
        self.interface = Interface(self, port_name)
        self.interfaces: Dict[str, Interface] = DefaultDict(lambda name: Interface(self, name))
        self.platform = FaePlatform(self)
        self.cluster = FaeCluster(self)
        self.nmx_telemetry_agent = FaeNmxTelemetryAgent(self)


class FaeCluster(BaseComponent):
    """Represents fae/cluster subtree"""

    def __init__(self, parent_obj=None):
        super().__init__(parent=parent_obj,
                         api={ApiType.NVUE: NvueClusterCli, ApiType.OPENAPI: OpenApiClusterCli},
                         path='/cluster')
        self.package = FaePackage(self)
        self.apps = FaeApps(self)


class FaePackage(BaseComponent):
    def __init__(self, parent_obj=None):
        super().__init__(parent=parent_obj, path="/package")
        self.files = Files(self)


class FaeApps(BaseComponent):
    def __init__(self, parent_obj=None):
        super().__init__(parent=parent_obj,
                         api={ApiType.NVUE: NvueClusterCli, ApiType.OPENAPI: OpenApiClusterCli},
                         path='/apps')
        self.app_name: Dict[str, FaeClusterApp] = DefaultDict(
            lambda app_name: FaeClusterApp(parent=self, app_name=app_name))


class FaeClusterApp(BaseComponent):
    def __init__(self, parent, app_name):
        super().__init__(parent=parent, path=f'/{app_name}')

    def action_uninstall(self, expect_reboot=False) -> ResultObj:
        """nv action uninstall fae cluster apps <app_name> [force]"""
        return self.action_deprecated(ActionConsts.UNINSTALL, expect_reboot=expect_reboot)


class FaeNmxTelemetryAgent(BaseComponent):
    def __init__(self, parent):
        super().__init__(parent=parent, path=f'/nmx-telemetry-agent')


class Ib(BaseComponent):
    def __init__(self, parent_obj=None):
        super().__init__(parent=parent_obj, path='/ib')
        self.ufm_mad = BaseComponent(self, path='/ufm-m')  # [L.A] temporary change ('/ufm-mad')


class SonicCli(BaseComponent):
    def __init__(self, parent_obj=None):
        super().__init__(parent=parent_obj,
                         api={ApiType.NVUE: NvueSystemCli, ApiType.OPENAPI: OpenApiSystemCli}, path='/sonic-cli')

    def action_general(self, action_str):
        return SendCommandTool.execute_command(
            self.api_obj[TestToolkit.tested_api].action_general_with_expected_disconnect,
            TestToolkit.get_engine(), action_str, self.get_resource_path())


class FaePlatform(BaseComponent):
    """Represents fae/platform subtree"""

    def __init__(self, parent_obj=None):
        super().__init__(parent_obj, path='/platform',
                         api={ApiType.NVUE: NvuePlatformCli, ApiType.OPENAPI: OpenApiPlatformCli})
        self.firmware = FaeFirmware(self)
        self.eeprom = BaseComponent(self, path="/eeprom")
        self.debug = Debug(self)
        self.asic = Asic(self)
        self.power_capping = FaePowerCapping(self)
        self.write_protection = WriteProtection(self)
        self.secure_state = SecureState(self)
        self.voltage_dpc = VoltageDpc(self)
        self.bkv = BaseComponent(self, path="/bkv")
        # Gen2 CPO (Portia): same component classes as platform.cpo/laser_source;
        # mounted here they compose FAE paths (nv action ... fae platform cpo ...)
        self.cpo = Cpo(self)
        self.laser_source = LaserSource(self)


class WriteProtection(BaseComponent):
    def __init__(self, parent_obj=None):
        super().__init__(parent=parent_obj, path="/write-protection")


class VoltageDpc(BaseComponent):
    """Represents fae/platform/voltage-dpc subtree"""

    def __init__(self, parent_obj=None):
        super().__init__(parent=parent_obj, path='/voltage-dpc')
        self.files = Files(self)


class FaeFirmware(BaseComponent):
    """Represents fae/platform/firmware subtree"""

    def __init__(self, parent_obj=None):
        super().__init__(parent=parent_obj, path='/firmware')
        self.asic1 = FaePlatformComponent(self, 'ASIC1')
        # multi-asic devices also have asic2 but our tests don't need it currently
        self.cpld = FaeCpldComponent(self, 'CPLD')
        self.bios = FaePlatformComponent(self, 'BIOS')
        self.ssd = FaePlatformComponent(self, 'SSD')
        self.bmc = FaePlatformComponent(self, 'BMC')  # TODO: Fix after bug closed https://redmine.mellanox.com/issues/3955495
        self.fpga = FaePlatformComponent(self, 'FPGA')
        self.sma = FaePlatformComponent(self, 'SMA')
        self.erot_id: Dict[str, ErotComponent] = DefaultDict(lambda erot_id: ErotComponent(self, erot_name=erot_id))
        self.sma_id: Dict[str, SmaComponent] = DefaultDict(lambda sma_id: SmaComponent(self, sma_name=sma_id))

    def install_bios_firmware(self, bios_image_path, device, topology_obj=None):
        with allure.step("installing bios firmware from {action_type}".format(action_type=bios_image_path)):
            return SendCommandTool.execute_command(
                self.api_obj[TestToolkit.tested_api].action_install_fae_bios_firmware,
                TestToolkit.get_engine(), bios_image_path, self.get_resource_path(), device, topology_obj)

    def create_erot_components(self, switch):
        """This method queries the switch for available ERoT components."""
        erots_names = switch.constants.erots.copy()

        for erot in erots_names:
            self.erot_id[erot] = ErotComponent(self, erot)


class FaeBiosComponent(BaseComponent):
    def __init__(self, parent_obj=None, component_name=None):
        super().__init__(parent=parent_obj, path=f"/{component_name}")


class FaePlatformComponent(BaseComponent):
    def __init__(self, parent_obj=None, component_name=None):
        super().__init__(parent=parent_obj, path=f"/{component_name}")
        # todo: restructure this class (and update relevant tests) to use the Files class
        # todo: self.files = Files(self)

    def show_files(self):
        """nv show fae platform firmware (bios|cpld|ssd) files"""
        return super().show(op_param='files')

    def show_files_as_list(self) -> List[str]:
        return OutputParsingTool.parse_show_files_to_names(self.show_files()).get_returned_value()

    def action_install(self, filename, device, expect_reboot) -> ResultObj:
        """nv action install fae platform firmware (bios|cpld|ssd) files <file-name> [force]"""
        return self.action_deprecated(ActionConsts.INSTALL, 'files ' + filename, 'force', expect_reboot=expect_reboot)

    def action_delete(self, filename) -> ResultObj:
        """nv action delete fae platform firmware (bios|cpld|ssd) files <file-name> [force]"""
        return self.action_deprecated(ActionConsts.DELETE, 'files ' + filename, expected_output='File delete successfully')


class FaeCpldComponent(FaePlatformComponent):
    def action_install(self, filename, device, expect_reboot) -> ResultObj:
        # This override is necessary because cpld-fw installation is done in two steps (BURN then REFRESH), and
        # when running action-install on the REFRESH image the system immediately reboots and connection is lost.
        try:
            return super().action_install(filename, device, expect_reboot)
        except (requests.exceptions.ConnectionError, requests.exceptions.ReadTimeout):
            if expect_reboot:
                logger.info(f"GET request failed as expected because of switch reboot")
                with allure.step("Waiting for reboot to finish"):
                    logger.info(f"Waiting 30 seconds to make sure reboot has started")
                    time.sleep(30)
                    engine = TestToolkit.get_engine()
                    engine.disconnect()
                    check_port_status_till_alive(True, engine.ip, engine.ssh_port)
                    return DutUtilsTool.wait_for_nvos_to_become_functional(engine)
            else:
                raise


class FaeSystem(BaseComponent):
    def __init__(self, parent_obj=None):
        super().__init__(parent=parent_obj, path='/system')
        self.events = BaseComponent(self, path='/events')
        self.mgmt_unsolicited = BaseComponent(self, path='/mgmt-unsolicited')
        self.fatal = BaseComponent(self, path='/fatal')
        self.fatal.monitor = BaseComponent(self.fatal, path='/monitor')
        self.serial_console = BaseComponent(self, path='/serial-console')
        self.profile = Profile(self)
        self.log = FaeLog(self)
        self.control = BaseComponent(self, path='/control')
        self.dockers = BaseComponent(self, path='/control/dockers')
        self.resource_limit = BaseComponent(self, path='/control/dockers/resource-limit')
        self.asic_debug_config = BaseComponent(self, path='/asic-debug-config')
        self.cpo = BaseComponent(self, path='/cpo')
        self.peer_port = FaePeerPort(self)
        self.sto_event = StoEvent(self)

    def ssd_cleanup(self, expected_str="", dut_engine=None):
        """nv action run fae system ssd-cleanup """
        return self.action_deprecated(ActionConsts.RUN, 'ssd-cleanup', expected_output=expected_str)


class FaeLog(BaseComponent):
    def __init__(self, parent_obj=None):
        BaseComponent.__init__(self, parent=parent_obj, path='/log')
        self.remarkable_logs = BaseComponent(self, path='/remarkable-logs')


class FaePeerPort(BaseComponent):
    def __init__(self, parent_obj=None):
        BaseComponent.__init__(self, parent=parent_obj, path='/peer-port')
        self.state = BaseComponent(self, path='/state')
        self.interval = BaseComponent(self, path='/interval')


class StoEvent(BaseComponent):
    def __init__(self, parent_obj=None):
        BaseComponent.__init__(self, parent=parent_obj, path='/sto-debug')
