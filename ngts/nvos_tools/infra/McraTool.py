from contextlib import contextmanager
from typing import Iterable, Union

from ngts.nvos_tools.infra.EngineAdapterTool import EngineAdapterTool
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.tools.test_utils import allure_utils as allure


class McraTool:
    BACKDOOR_OPEN_CMD = '0x3ffffffc 0x80000000'
    BACKDOOR_CLOSE_CMD = '0x3ffffffc 0x90000000'

    @staticmethod
    def _normalize_mst_devices(mst_devices: Union[str, Iterable[str]]):
        if isinstance(mst_devices, str):
            return (mst_devices,)
        return tuple(mst_devices)

    @staticmethod
    def _get_engine(engine=None):
        return engine or TestToolkit.get_engine()

    @staticmethod
    def run_commands(mst_device, commands, engine=None, validate=True):
        """
        Run `sudo mcra <mst_device> <cmd>` for each command.

        `commands` can be a single string or an iterable of command argument strings.
        Returns the output of the last command.
        """
        if isinstance(commands, str):
            commands = (commands,)

        last_output = ''
        dut_engine = McraTool._get_engine(engine)
        for command in commands:
            last_output = EngineAdapterTool.run_cmd(
                dut_engine,
                f'sudo mcra {mst_device} {command}',
                validate=validate,
            )
        return last_output

    @staticmethod
    @contextmanager
    def enable_backdoor(mst_devices, engine=None):
        """Open mcra backdoor on the given MST device(s), then close on exit."""
        normalized_devices = McraTool._normalize_mst_devices(mst_devices)
        for mst_device in normalized_devices:
            with allure.step(f'Enabling error-injection for ASIC {mst_device}'):
                McraTool.run_commands(
                    mst_device, McraTool.BACKDOOR_OPEN_CMD, engine=engine,
                )
        try:
            yield
        finally:
            with allure.step('Cleanup'):
                for mst_device in normalized_devices:
                    with allure.step(f'Disabling error-injection for ASIC {mst_device}'):
                        McraTool.run_commands(
                            mst_device, McraTool.BACKDOOR_CLOSE_CMD,
                            engine=engine, validate=False,
                        )
