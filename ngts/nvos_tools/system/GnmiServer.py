import inspect
import logging
import time
from typing import Dict

from retry import retry

from ngts.constants.constants import GnmiConsts
from ngts.nvos_constants.constants_nvos import ActionConsts
from ngts.nvos_tools.infra.BaseComponent import BaseComponent
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.system.MTLSableServerResource import MTLSableServerResource
from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger()


def wait_for_gnmi_config_to_settle():
    with allure.step(f'wait {GnmiConsts.CONFIG_SETTLE_TIME_SEC}s for gnmi config to settle'):
        logger.info(f'wait {GnmiConsts.CONFIG_SETTLE_TIME_SEC}s for gnmi config to settle after set/unset')
        time.sleep(GnmiConsts.CONFIG_SETTLE_TIME_SEC)


def _called_with_apply_true(fn, args, kwargs) -> bool:
    try:
        bound = inspect.signature(fn).bind_partial(*args, **kwargs)
        bound.apply_defaults()
        return bool(bound.arguments.get('apply', False))
    except (TypeError, ValueError):
        return bool(kwargs.get('apply', False))


class _GnmiConfigSettlingMixin:
    """Wait for gNMI server config to settle after applied set/unset calls.

    Non-applied set/unset calls only stage configuration, so there is nothing
    on the server to settle. This mixin is applied only to gnmi-server itself;
    gnmi-server/mtls keeps the plain BaseComponent behavior.
    """

    def set(self, *args, **kwargs):
        result = super().set(*args, **kwargs)
        if _called_with_apply_true(super().set, args, kwargs):
            wait_for_gnmi_config_to_settle()
        return result

    def unset(self, *args, **kwargs):
        result = super().unset(*args, **kwargs)
        if _called_with_apply_true(super().unset, args, kwargs):
            wait_for_gnmi_config_to_settle()
        return result


class Status(BaseComponent):
    """Sub-component for 'nv show system gnmi-server status' and 'nv action clear system gnmi-server status'."""

    def __init__(self, parent_obj=None):
        super().__init__(parent=parent_obj, path='/status')

    def action_clear(self):
        return self.action(ActionConsts.CLEAR)


class GnmiServer(_GnmiConfigSettlingMixin, MTLSableServerResource):
    def __init__(self, parent_obj=None):
        super().__init__(parent=parent_obj, path='/gnmi-server')
        self.status = Status(self)

    def enable_gnmi_server(self, apply=True):
        return self.set(GnmiConsts.GNMI_STATE_FIELD, GnmiConsts.GNMI_STATE_ENABLED, apply=apply)

    def disable_gnmi_server(self, apply=True):
        return self.set(GnmiConsts.GNMI_STATE_FIELD, GnmiConsts.GNMI_STATE_DISABLED, apply=apply)

    def unset_gnmi_server(self, apply=True):
        return self.unset(apply=apply)

    def parsed_show_gnmi(self):
        return OutputParsingTool.parse_json_str_to_dictionary(self.show()).get_returned_value()

    @retry(Exception, tries=4, delay=3)
    def compare_show_gnmi_output(self, expected: Dict):
        show_output = self.parsed_show_gnmi()
        msg = ''
        for key, value in expected.items():
            if show_output[key] != expected[key]:
                msg += f"{key} field is different than expected: \n" \
                    f"Expected: {expected[key]}, but got: {value}\n"
        assert not msg, f"The output of show gnmi-server is different than expected:\n{msg}"
