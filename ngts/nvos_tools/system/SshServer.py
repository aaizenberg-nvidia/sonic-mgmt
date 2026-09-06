from typing import Dict
from ngts.nvos_tools.infra.DefaultDict import DefaultDict
from ngts.nvos_tools.infra.BaseComponent import BaseComponent


class SshServer(BaseComponent):
    def __init__(self, parent_obj=None):
        super().__init__(parent=parent_obj, path='/ssh-server')
        self.active_sessions = ActiveSessions(self)
        self.trusted_ca_keys = TrustedCaKeys(self)
        self.deny_user = DenyUser(self)
        self.allow_user = AllowUser(self)
        self.port = Port(self)


class AllowUser(BaseComponent):
    def __init__(self, parent_obj=None):
        super().__init__(parent=parent_obj, path='/allow-user')

    def set_allow_user(self, user: str, apply: bool = False):
        self.set(op_param_name='user', op_param_value=user, apply=apply)


class DenyUser(BaseComponent):
    def __init__(self, parent_obj=None):
        super().__init__(parent=parent_obj, path='/deny-user')

    def set_deny_user(self, user: str, apply: bool = False):
        self.set(op_param_name='user', op_param_value=user, apply=apply)


class TrustedCaKeys(BaseComponent):
    def __init__(self, parent_obj=None):
        super().__init__(parent=parent_obj, path='/trusted-ca-keys')
        self.key_id: Dict[str, TrustedCaKey] = DefaultDict(
            lambda key_id: TrustedCaKey(key_id=key_id, parent=self))


class TrustedCaKey(BaseComponent):
    def __init__(self, key_id: str, parent=None):
        super().__init__(parent=parent, path=f'/{key_id}')

    def set_key_val(self, key: str, apply: bool = False):
        """
        The key value is a string of the public key in PEM format.
        """
        self.set(op_param_name='key', op_param_value=key, apply=apply)

    def set_key_type(self, key_type: str, apply: bool = False):
        """
        The key type is a string of the key type.
        The key type is one of the following:
        - rsa
        - ecdsa
        - ed25519
        - dsa
        - ssh-ed25519
        """
        return self.set(op_param_name='type', op_param_value=key_type, apply=apply)


class Port(BaseComponent):
    def __init__(self, parent_obj=None):
        super().__init__(parent=parent_obj, path='/port')

    def set(self, port: int, apply: bool = False):
        # For NVUE CLI: nv set system ssh-server port <port>
        # For OpenAPI: PATCH /system/ssh-server/port with body {"<port>": {}}
        return BaseComponent.set(self, op_param_name=str(port), op_param_value={}, apply=apply)


class ActiveSessions(BaseComponent):
    def __init__(self, parent_obj=None):
        super().__init__(parent=parent_obj, path='/active-sessions')
