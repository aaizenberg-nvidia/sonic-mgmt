import logging
from typing import Dict

from ngts.nvos_tools.infra.BaseComponent import BaseComponent
from ngts.nvos_tools.infra.DefaultDict import DefaultDict
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.infra.ResultObj import ResultObj
from ngts.nvos_tools.cli_coverage.operation_time import OperationTime
from ngts.nvos_tools.system.Files import Files
from ngts.tools.test_utils import allure_utils as allure
from ngts.nvos_constants.constants_nvos import ActionConsts, PlatformConsts

logger = logging.getLogger()


class TransceiverComponent(BaseComponent):
    """Component for a specific transceiver (e.g., els1, oe1)"""

    def __init__(self, parent_obj=None, transceiver_id=None):
        super().__init__(parent=parent_obj, path=f"/{transceiver_id}")
        self.transceiver_id = transceiver_id

    def activate(self, test_name: str = "", expected_str="", dut_engine=None) -> ResultObj:
        """
        Activate transceiver with duration measurement.

        Args:
            test_name: Name of the test calling this operation (for duration tracking)
            expected_str: Expected string in output
            dut_engine: Engine to run command on

        Returns:
            ResultObj with duration attribute set
        """
        with allure.step(f"Activate transceiver {self.transceiver_id}"):
            result_obj, _ = OperationTime.save_duration(
                f"activate transceiver {self.transceiver_id}",
                self.transceiver_id,
                test_name,
                self.action,
                ActionConsts.ACTIVATE,
                expected_output=expected_str,
                engine=dut_engine
            )
            return result_obj


class Transceiver(BaseComponent):
    def __init__(self, parent_obj=None):
        super().__init__(parent=parent_obj, path='/transceiver')
        self.files = Files(self)
        self.transceiver_id: Dict[str, TransceiverComponent] = DefaultDict(
            lambda transceiver_name: TransceiverComponent(self, transceiver_id=transceiver_name)
        )

    def show_detailed(self):
        op_param = '--view=detail'
        return self.show(op_param=op_param)

    def show_detailed_dict(self) -> Dict:
        """Parsed output of `nv show platform transceiver --view=detail`.

        Callers needing more than one view of the inventory should fetch this
        once and pass it into the getters below, instead of re-running the
        command per query.
        """
        return OutputParsingTool.parse_json_str_to_dictionary(self.show_detailed()).verify_result()

    def get_fw_versions(self, transceivers_detail: Dict = None) -> Dict[str, str]:
        """{transceiver name: fw-version} for every transceiver reporting a 'fw-version' field.

        :param transceivers_detail: parsed show_detailed output as dict. If not provided, will be fetched from the device.
        """
        if transceivers_detail is None:
            transceivers_detail = self.show_detailed_dict()
        return {k: v[PlatformConsts.FW_VERSION]
                for k, v in transceivers_detail.items()
                if isinstance(v, dict) and PlatformConsts.FW_VERSION in v}

    def action_reset(self, transceiver_name, expected_str="", dut_engine=None):
        """nv action reset platform transceiver <transceiver-name> """
        return self.action_deprecated(ActionConsts.RESET, transceiver_name, expected_output=expected_str, dut_engine=dut_engine)

    def action_install(self, transceiver_name, file_name, expected_str="", dut_engine=None):
        """nv action install platform transceiver firmware files <file-name> """
        return self.action_deprecated(ActionConsts.INSTALL, transceiver_name + ' firmware files ' + file_name, expected_output=expected_str, dut_engine=dut_engine)

    def get_dict_of_transceivers(self, cable_type):
        """
        Returns a dict of transceivers according to cable_type
        :param cable_type: None / Copper / Optical
        :return: dict of transceivers according to cable_type
        """
        with allure.step('Search for transceivers that meet provided requirements'):

            logging.info("get_dict_of_transceivers - Searching for relevant transceivers")
            dict_of_transceivers = OutputParsingTool.parse_show_output_to_dict(self.show_detailed()).get_returned_value()

            if cable_type:
                dict_of_transceivers = {k: v for k, v in dict_of_transceivers.items() if v.get(PlatformConsts.TRANSCEIVER_CABLE_TYPE) == cable_type}
                logging.info(f"get_dict_of_transceivers - {dict_of_transceivers} meets the requirements of {cable_type}")
            else:
                dict_of_transceivers = {k: v for k, v in dict_of_transceivers.items() if PlatformConsts.TRANSCEIVER_CABLE_TYPE not in v}
                logging.info(f"get_dict_of_transceivers - {dict_of_transceivers} no {PlatformConsts.TRANSCEIVER_CABLE_TYPE} field therefore cable is not connected")

            return dict_of_transceivers

    def get_list_of_connected_transceivers(self, dut_engine=None):
        dict_of_transceivers = OutputParsingTool.parse_show_output_to_dict(
            self.show(op_param='--view=brief', dut_engine=dut_engine)).get_returned_value()
        return list(dict_of_transceivers.keys())
