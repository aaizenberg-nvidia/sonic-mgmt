import logging
from ngts.nvos_tools.infra.BaseComponent import BaseComponent
from ngts.nvos_constants.constants_nvos import AclConsts, ApiType

logger = logging.getLogger()


class Action(BaseComponent):

    def __init__(self, parent_obj=None):
        BaseComponent.__init__(self, parent=parent_obj, path='/action')
        self.log = Log(self)
        self.dscp = Dscp(self)
        self.recent = Recent(self)
        self.permit = Permit(self)
        self.deny = Deny(self)


class Log(BaseComponent):

    def __init__(self, parent_obj=None):
        BaseComponent.__init__(self, parent=parent_obj, path='/log')

    def set_log_prefix(self, log_prefix):
        return self.set(AclConsts.LOG_PREFIX, log_prefix)


class Dscp(BaseComponent):
    def __init__(self, parent_obj=None):
        BaseComponent.__init__(self, parent=parent_obj, path='/set/dscp')

    def get_resource_path(self):
        # OpenAPI expects PATCH on .../action/set with body {"dscp": value}, not .../action/set/dscp.
        if self._api_to_use == ApiType.OPENAPI:
            return self.parent_obj.get_resource_path() + '/set'
        return super().get_resource_path()

    def set(self, dscp_value, **kwargs):
        """OpenAPI: pass correct param so PATCH body is {"dscp": value}."""
        if self._api_to_use == ApiType.OPENAPI:
            return super().set(op_param_name='dscp', op_param_value=dscp_value, **kwargs)
        return super().set(dscp_value, **kwargs)


class Recent(BaseComponent):
    def __init__(self, parent_obj=None):
        BaseComponent.__init__(self, parent=parent_obj, path='/recent')


class Permit(BaseComponent):
    def __init__(self, parent_obj=None):
        BaseComponent.__init__(self, parent=parent_obj, path='/permit')


class Deny(BaseComponent):
    def __init__(self, parent_obj=None):
        BaseComponent.__init__(self, parent=parent_obj, path='/deny')
