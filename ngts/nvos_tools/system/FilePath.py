"""
NVUE wrapper for '/system/file-path', the resource the gNOI feature adds for host file transfers.

The four actions and their argument order, as reported by 'nv list-commands' on juliet-160:

    nv action list   system file-path <path>
    nv action fetch  system file-path <path> <uri> [file-permissions <value>] [vrf <vrf-name>]
    nv action upload system file-path <path> <uri> [vrf <vrf-name>]
    nv action delete system file-path <path>
"""
import logging

from ngts.nvos_constants.constants_nvos import ActionConsts, ApiType
from ngts.nvos_tools.infra.BaseComponent import BaseComponent

logger = logging.getLogger()

# The name half of an action's main_param is only used by the OpenAPI flavour; NVUE renders the
# value alone (see BaseCli.get_nv_action_string). These names therefore only document intent.
PATH_PARAM = 'path'
URI_PARAM = 'uri'
FILE_PERMISSIONS_PARAM = 'file-permissions'
VRF_PARAM = 'vrf'


class FilePath(BaseComponent):
    """
    The path is passed as the action's main parameter rather than appended to the resource path,
    because get_nv_action_string() turns every '/' of the resource path into a space - which would
    scatter '/home/admin/f.txt' across the command line.

    fetch and upload take <path> and <uri> positionally, with no keyword in between, so both travel
    in the main parameter's value. Only NVUE renders a main parameter that way; OpenAPI turns the
    (name, value) pair into a single JSON field, which would send '<path> <uri>' as one 'path'
    value and not match the schema. The component therefore pins itself to NVUE instead of
    inheriting the API its parent System was built with.
    """

    def __init__(self, parent_obj=None):
        BaseComponent.__init__(self, parent=parent_obj, path='/file-path', force_api=ApiType.NVUE)

    def action_list(self, path, **kwargs):
        """List one file's metadata, or the entries of a directory, as a JSON array."""
        return self.action(ActionConsts.LIST, main_param=(PATH_PARAM, path), **kwargs)

    def action_fetch(self, path, uri, file_permissions=None, vrf=None, **kwargs):
        """Pull <uri> onto the switch as <path>. Without file-permissions the switch applies 644."""
        params = {}
        if file_permissions is not None:
            params[FILE_PERMISSIONS_PARAM] = file_permissions
        if vrf is not None:
            params[VRF_PARAM] = vrf
        return self.action(ActionConsts.FETCH, main_param=(PATH_PARAM, f'{path} {uri}'),
                           additional_params=params, **kwargs)

    def action_upload(self, path, uri, vrf=None, **kwargs):
        """Push <path> from the switch out to <uri>."""
        params = {VRF_PARAM: vrf} if vrf is not None else {}
        return self.action(ActionConsts.UPLOAD, main_param=(PATH_PARAM, f'{path} {uri}'),
                           additional_params=params, **kwargs)

    def action_delete(self, path, **kwargs):
        """Delete <path> on the switch."""
        return self.action(ActionConsts.DELETE, main_param=(PATH_PARAM, path), **kwargs)
