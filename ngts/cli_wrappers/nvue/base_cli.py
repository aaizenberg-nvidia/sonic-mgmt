import re
from abc import abstractmethod, ABCMeta
from typing import Optional, Tuple, Union, Iterable

from ngts.nvos_tools.infra.DutUtilsTool import RebootParams
from ngts.nvos_tools.infra.ResultObj import ResultObj


class BaseCli(metaclass=ABCMeta):
    """Abstract base-class for both NvueBaseCli and OpenApiBaseCli"""
    sub_strings_to_search = ['ib', 'sm', 'quantum']
    check_output_strings = False
    cli_name = ""

    @staticmethod
    @abstractmethod
    def show(engine, resource_path, op_param, output_format): pass

    @staticmethod
    @abstractmethod
    def set(engine, resource_path, op_param_name, op_param_value): pass

    @staticmethod
    @abstractmethod
    def unset(engine, resource_path, op_param): pass

    @staticmethod
    @abstractmethod
    def action_deprecated(engine, device, action_type, resource_path, suffix, param_name, param_value, output_format,
                          expect_reboot, recovery_engine, topology_obj, should_succeed, system_is_ready_timeout,
                          track_boot_intervals, deny_reboot, press_y, expected_output: str): pass

    @classmethod
    def get_nv_action_string(cls, action_str, resource_path, main_param, flags, params):
        """Returns the full NVUE command, e.g. 'nv action uninstall system image force' """
        if main_param:
            if len(main_param) != 2:
                raise ValueError(f'"main_param" argument should be a 2-tuple (name, value) but it is {repr(main_param)}')
            main_param_value = main_param[1]
        else:
            main_param_value = ''
        if not isinstance(flags, str):
            flags = ' '.join(flags or [])
        param_str = ' '.join([f'{k} {v}' for k, v in params.items()])
        ret = f'nv action {action_str} {resource_path.replace("/", " ")} {main_param_value} {param_str} {flags}'
        ret = re.sub(' +', ' ', ret).strip()  # delete double-spaces and trailing spaces
        return ret

    @classmethod
    @abstractmethod
    def action(cls, action_str: str, resource_path: str, main_param: Tuple[str, Union[str, int]] | None,
               flags: Union[str, Iterable[str]], params: dict, engine, reboot_params: Optional[RebootParams],
               send_user_confirmation: Optional[str], expected_output: str, device,
               timeout: Optional[float] = None) -> ResultObj: pass
