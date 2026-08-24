import logging
import json

# import via the factory module: in the source layout the direct module path loads a second
# copy of the class, breaking isinstance() against factory-created engines
from devts.infra.tools.connection_tools.serial_engine_factory import (  # ty: ignore[unresolved-import]
    PexpectSerialEngine,
)

from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from .openapi_command_builder import OpenApiCommandHelper, OpenApiRequest, RequestData
from ...tests_nvos.general.security.certificate.CertInfo import CertInfo
from ...nvos_constants.constants_nvos import OpenApiConfigVerifyConsts
from ...nvos_tools.infra.ResultObj import ResultObj

logger = logging.getLogger(__name__)


class OpenApiGeneralCli:

    """
    Open API cli wrapper
    """

    def __init__(self):
        pass

    @staticmethod
    def _build_revision_root_request_data(engine) -> RequestData:
        return RequestData(
            user_name=engine.engine.username,
            password=engine.engine.password,
            endpoint_ip=engine.ip,
            resource_path='/',
            param_name='',
            param_value='',
            open_api_port=engine.open_api_port,
        )

    @staticmethod
    def _log_file_content_preview(content: str) -> None:
        logger.info("File content preview:\n%s...", content[:500])

    @staticmethod
    def _cat_remote_file(engine, filepath: str) -> str:
        content = engine.run_cmd(f'cat {filepath}')
        OpenApiGeneralCli._log_file_content_preview(content)
        return content

    @staticmethod
    def _verify_patch_response_failed(patch_res: str) -> bool:
        return bool(patch_res) and any(
            m in patch_res for m in OpenApiConfigVerifyConsts.PATCH_BODY_ERROR_SUBSTRINGS
        )

    @staticmethod
    def verify_config_from_commands(engine, commands_text, timeout=None, verbose=False):
        """
        Verify config from a string of nv commands via API (create revision, patch text, dry-run verify).
        Same logical behavior as CLI 'nv config verify filename <file>' for the given commands.
        :param engine: ssh engine object
        :param commands_text: multiline string of nv set/unset commands
        :param timeout: unused for API (kept for signature parity with NVUE)
        :param verbose: if True, use state-controls dry-run "verbose" (TC-7); else "brief"
        :return: (success: bool, output: str)
        """
        logging.info("Verify config from commands using OpenApi (dry-run, verbose=%s)", verbose)
        request_data = OpenApiGeneralCli._build_revision_root_request_data(engine)
        res, err = OpenApiRequest.update_nvue_changeset(request_data)
        if not res:
            return False, err or "Failed to create revision"
        patch_res = OpenApiRequest.send_patch_request(request_data, text_content=commands_text)
        if OpenApiGeneralCli._verify_patch_response_failed(patch_res):
            OpenApiRequest.clear_changeset_and_payload()
            return False, patch_res
        verify_res = OpenApiRequest._verify_config_dry_run(request_data, verbose=verbose)
        OpenApiRequest.clear_changeset_and_payload()
        verify_res.ignore_result()
        return verify_res.result, verify_res.info or ""

    @staticmethod
    def verify_config_from_file(engine, filepath, timeout=None, verbose=False):
        """
        Verify config from a file on the DUT via API (cat file, same pipeline as verify_config_from_commands).
        """
        logging.info("Verify config from file using OpenApi (dry-run)")
        file_content = OpenApiGeneralCli._cat_remote_file(engine, filepath)
        return OpenApiGeneralCli.verify_config_from_commands(
            engine, file_content, timeout=timeout, verbose=verbose
        )

    def apply_config(engine, ask_for_confirmation=False, option='', validate_apply_message='', rev_id="",
                     skip_no_config_diff_err=True, verify_execution=False, client_certs_after_apply: CertInfo = None,
                     apply_timeout=None):
        """
        Apply configuration
        :param engine: ssh engine object
        """
        if isinstance(engine, PexpectSerialEngine):
            return NvueGeneralCli.apply_config(
                engine, ask_for_confirmation, option, validate_apply_message, rev_id,
                skip_no_config_diff_err, verify_execution, client_certs_after_apply, apply_timeout)
        logger.info("Execute config apply using OpenApi")

        return OpenApiCommandHelper.execute_script(engine.engine.username, engine.engine.password, 'APPLY', engine.ip,
                                                   engine.open_api_port, 'system/config/apply',
                                                   client_certs_after_apply=client_certs_after_apply)

    @staticmethod
    def save_config(engine):
        """
        Save configuration
        :param engine: ssh engine object
        """
        logger.info("Execute config save using OpenApi")

        resource_path = '/revision/applied'
        return OpenApiCommandHelper.execute_script(engine.engine.username, engine.engine.password, 'PATCH', engine.ip,
                                                   engine.open_api_port, resource_path=resource_path,
                                                   op_param_name="state", op_param_value="save")

    @staticmethod
    def show_config(engine, revision='applied', output_type='json', param=''):
        """
        Save configuration
        :param engine: ssh engine object
        :param revision: applied / pending / startup
        :param output_type: json / str
        :param param: --all/ ''
        """
        logger.info("Execute config show using OpenApi")

        resource_path = '/?rev={revision}&filled=False'.format(revision=revision)
        res = OpenApiCommandHelper.execute_script(engine.engine.username, engine.engine.password, 'GET', engine.ip,
                                                  engine.open_api_port, resource_path=resource_path,
                                                  op_param_name=param)

        if output_type == 'json':
            return json.dumps(res)
        else:
            return res

    @staticmethod
    def detach_config(engine, ask_for_confirmation=False):
        """
        Detach configuration
        :param engine: ssh engine object
        """
        logger.info("Execute config save using OpenApi")
        # TODO: not supported yet
        # return OpenApiCommandHelper.execute_script(engine.engine.username, engine.engine.password, 'DETACH',
        #                                            engine.ip, 'system/config/detach')
        return ""

    @staticmethod
    def diff_config(engine, revision_1='', revision_2='', output_type='json'):
        """
        diff configuration
        :param engine: ssh engine object
        :param revision_1: config revision 1
        :param revision_2: config revision 2
        :param output_type: json / str
        """
        # TODO:
        logger.info("Execute config diff using OpenApi")

        resource_path = '/?rev={revision_2}&filled=False&diff={revision_1}'.format(revision_2=revision_2, revision_1=revision_1)
        res = OpenApiCommandHelper.execute_script(engine.engine.username, engine.engine.password, 'GET', engine.ip,
                                                  engine.open_api_port, resource_path=resource_path)

        if output_type == 'json':
            return json.dumps(res)
        else:
            return res

    @staticmethod
    def config_patch(engine, filepath, apply=True, detach_first=True, apply_timeout=None):
        """
        Patch configuration from file with optional apply.

        Args:
            engine: SSH engine
            filepath: Path to config file
            apply: If True, applies config and validates. If False, only creates revision
            detach_first: If True, detaches any pending config before patching (default: True)
                         Note: OpenAPI detach may not be fully supported yet
            apply_timeout: Timeout in seconds for the apply operation (default: None uses engine's default)

        Returns:
            ResultObj with success/failure status
        """
        # Step 1: Detach pending config if requested (default behavior)
        if detach_first:
            OpenApiGeneralCli.detach_config(engine)

        # Step 2: Read file content
        file_content = engine.run_cmd(f'cat {filepath}')
        logger.info(f"File content preview:\n{file_content[:500]}...")

        request_data = RequestData(
            user_name=engine.engine.username,
            password=engine.engine.password,
            endpoint_ip=engine.ip,
            resource_path='/',
            param_name='',
            param_value='',
            open_api_port=engine.open_api_port
        )

        # Step 3: Send PATCH request (creates revision)
        response = OpenApiRequest.send_patch_request(
            request_data,
            text_content=file_content,
            replace=False
        )

        # Check if operation failed
        if response and ('error' in response.lower() or 'failed' in response.lower()):
            return ResultObj(False, info=response, returned_value=response)

        result = ResultObj(True, info="Config patch successful (revision created)", returned_value=response)

        # Step 4: Apply if requested
        if not apply:
            return result

        try:
            apply_result = OpenApiGeneralCli.apply_config(engine, ask_for_confirmation=True)
            logger.info(f"Apply result: {apply_result}")

            # Check if apply failed
            if apply_result and any(err in str(apply_result).lower() for err in ['error', 'failed', 'fail']):
                if 'no config diff' not in str(apply_result).lower():
                    return ResultObj(False, info=f"Apply failed: {apply_result}", returned_value=apply_result)

            return ResultObj(True, info="Patch and apply successful", returned_value=response)
        except Exception as e:
            return ResultObj(False, info=f"Apply failed: {str(e)}", returned_value=str(e))

    @staticmethod
    def config_replace(engine, filepath, apply=True, apply_timeout=None):
        """
        Replace configuration from file with optional apply.

        Args:
            engine: SSH engine
            filepath: Path to config file
            apply: If True, applies config and validates. If False, only creates revision
            apply_timeout: Timeout in seconds for the apply operation (default: None uses engine's default)

        Returns:
            ResultObj with success/failure status
        """
        # Step 1: Read file content
        file_content = engine.run_cmd(f'cat {filepath}')
        logger.info(f"File content preview:\n{file_content[:500]}...")

        request_data = RequestData(
            user_name=engine.engine.username,
            password=engine.engine.password,
            endpoint_ip=engine.ip,
            resource_path='/',
            param_name='',
            param_value='',
            open_api_port=engine.open_api_port
        )

        # Match NVOS `nv config replace`: clear the revision first, then patch the new content into it.
        clear_response = OpenApiRequest.send_delete_request(request_data)
        if clear_response and ('error' in clear_response.lower() or 'failed' in clear_response.lower()):
            return ResultObj(False, info=f"Clear config failed: {clear_response}", returned_value=clear_response)

        # Step 2: Send PATCH request on the cleared revision
        response = OpenApiRequest.send_patch_request(
            request_data,
            text_content=file_content,
            replace=False
        )

        # Check if operation failed
        if response and ('error' in response.lower() or 'failed' in response.lower()):
            return ResultObj(False, info=response, returned_value=response)

        result = ResultObj(True, info="Config replace successful (revision created)", returned_value=response)

        # Step 3: Apply if requested
        if not apply:
            return result

        try:
            apply_result = OpenApiGeneralCli.apply_config(engine, ask_for_confirmation=True)
            logger.info(f"Apply result: {apply_result}")

            # Check if apply failed
            if apply_result and any(err in str(apply_result).lower() for err in ['error', 'failed', 'fail']):
                if 'no config diff' not in str(apply_result).lower():
                    return ResultObj(False, info=f"Apply failed: {apply_result}", returned_value=apply_result)

            return ResultObj(True, info="Replace and apply successful", returned_value=response)
        except Exception as e:
            return ResultObj(False, info=f"Apply failed: {str(e)}", returned_value=str(e))
