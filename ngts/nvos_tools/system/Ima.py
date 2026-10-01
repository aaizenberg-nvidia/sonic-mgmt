from ngts.nvos_tools.infra.BaseComponent import BaseComponent
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.ResultObj import ResultObj
from ngts.nvos_tools.infra.SendCommandTool import SendCommandTool
from ngts.tools.test_utils import allure_utils as allure


class ImaActionConsts:
    """IMA action sub-resource names used by NVUE upload commands."""

    MEASUREMENTS = 'measurements'


class Ima(BaseComponent):
    """
    NVUE model for the IMA (Integrity Measurement Architecture) feature.

    Resource path: ``/system/security/ima``.

    The ``state`` knob (``enabled`` / ``disabled``) is handled by the inherited
    ``show`` / ``set`` / ``unset`` methods of :class:`BaseComponent`, so callers can use:

        ``System().security.ima.show()``                       -> ``{"state": ...}``
        ``System().security.ima.set('state', 'enabled', apply=True)``
        ``System().security.ima.unset('state', apply=True)``

    The only IMA-specific action is uploading the live kernel measurement log,
    which is exposed by :meth:`action_upload_measurements`.
    """

    def __init__(self, parent_obj=None):
        super().__init__(parent=parent_obj, path='/ima')

    def action_upload_measurements(self, remote_url: str, expected_str: str = '', dut_engine=None) -> ResultObj:
        """
        Upload the live IMA measurement log to a remote destination.

        Runs ``nv action upload system security ima measurements <remote_url>`` (NVUE)
        or the equivalent OpenAPI upload action.

        :param remote_url: SCP destination, e.g. ``scp://user:pass@host/tmp/ima_measurements``.
        :param expected_str: Optional sub-string expected in the command output.
        :param dut_engine: Engine to run on. Defaults to the current test engine.
        :return: ResultObj describing the command result.
        """
        with allure.step(f'Execute action upload measurements for {self.get_resource_path()}'):
            engine = dut_engine if dut_engine else TestToolkit.get_engine()
            return SendCommandTool.execute_command_expected_str(
                self._cli_wrapper.action_upload, expected_str, engine,
                self.get_resource_path(), ImaActionConsts.MEASUREMENTS, remote_url)
