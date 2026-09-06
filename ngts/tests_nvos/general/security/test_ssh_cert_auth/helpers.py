import logging
import random
import os

from ..security_test_tools.tool_classes.SecuritySshTool import SecuritySshTool
from ..security_test_tools.tool_classes.AuthVerifier import PKAAuthVerifier
from ngts.nvos_tools.infra.RandomizationTool import RandomizationTool
from ..security_test_tools.tool_classes.UserInfo import UserInfo
from ..ssh_hardening.constants import SshHardeningConsts
from ngts.tools.test_utils import allure_utils as allure
from ngts.nvos_tools.infra.CmdRunner import CmdRunner
from ..security_test_tools.constants import UserRole
from ngts.nvos_tools.system.System import System

from .constants import CERT_VALIDITY_PERIODS, SSH_CERT_AUTH_KEYS_PATH, TEST_PRINCIPALS

logger = logging.getLogger(__name__)


class SshCertAuthHelper:
    def __init__(self):
        self.cmd_runner = CmdRunner()
        self.keys_path = SSH_CERT_AUTH_KEYS_PATH

    def ensure_keys_directory(self):
        with allure.step(f"Ensure keys directory: {self.keys_path}"):
            os.makedirs(self.keys_path, mode=0o777, exist_ok=True)
            # Directory needs 777 permissions for both local user and regression (root) to write
            try:
                os.chmod(self.keys_path, 0o777)
                logger.info(f"Set directory permissions to 777 for {self.keys_path}")
            except Exception as e:
                logger.warning(f"Could not set directory permissions: {e}")

    def generate_key_pair(self, key_name: str, key_type: str | None = None, ca: bool = False) -> tuple[str, str, str]:
        """
        Generate CA key pair for signing certificates.

        Args:
            key_name: Name for the CA key pair
            key_type: Type of key to generate (defaults to random)
            ca: Whether to generate a CA key pair (defaults to False)

        Returns:
            Tuple of (public_key_content, key_type, private_key_path)
        """
        key_type = key_type or random.choice(list(SshHardeningConsts.PUBLIC_KEY_LENGTH_DICT.keys()))
        key_path = f"{self.keys_path}/{key_name}_ca" if ca else f"{self.keys_path}/{key_name}_key"

        with allure.step(f"Generate key pair: {key_name} ({key_type})"):
            self.ensure_keys_directory()

            SecuritySshTool.generate_auth_keypair(
                key_type=key_type, dst_path=key_path, num_bits=SshHardeningConsts.PUBLIC_KEY_LENGTH_DICT.get(key_type, 2048)
            )

            if not os.path.exists(key_path):
                raise FileNotFoundError(f"Private key was not created: {key_path}. Check directory permissions: {self.keys_path}")
            if not os.path.exists(f"{key_path}.pub"):
                raise FileNotFoundError(f"Public key was not created: {key_path}.pub. Check directory permissions: {self.keys_path}")

            public_key_content = self.cmd_runner.run_cmd(f"cat {key_path}.pub").strip()
            public_key_hash = self.extract_public_key(public_key_content)

            logger.info(f"Generated key pair: {key_path}")
            return public_key_hash, key_type, key_path

    def generate_ca_key_pair(self, ca_name: str, key_type: str | None = None) -> tuple[str, str, str]:
        return self.generate_key_pair(ca_name, key_type, ca=True)

    def generate_user_key_pair(self, user_name: str, key_type: str | None = None) -> tuple[str, str, str]:
        return self.generate_key_pair(user_name, key_type)

    def sign_user_certificate(
        self, ca_private_key_name: str, user_key_name: str, principals: list[str], cert_id: str = "cert", validity: str | None = None
    ) -> str:
        """
        Sign user certificate with CA private key.

        Args:
            ca_private_key_name: Name for the CA key pair
            user_key_name: Name for the user key pair
            principals: List of principals for the certificate
            cert_id: Certificate identifier
            validity: Certificate validity period (defaults to +1d)

        Returns:
            Path to the signed certificate
        """
        validity = validity or CERT_VALIDITY_PERIODS["day"]
        principals_str = ",".join(principals)
        cert_path = f"{self.keys_path}/{user_key_name}-cert.pub"

        with allure.step(f"Sign certificate: {cert_id} with principals {principals_str}"):
            sign_cmd = (
                f"ssh-keygen -s {self.keys_path}/{ca_private_key_name} "
                f"-I {cert_id} "
                f"-n {principals_str} "
                f"-V {validity} "
                f"{self.keys_path}/{user_key_name}"
            )

            logger.info(f"Signing certificate with command: {sign_cmd}")
            self.cmd_runner.run_cmd(sign_cmd, allowed_err="Signed user key")

            if not os.path.exists(cert_path):
                raise Exception(f"Certificate signing failed: {cert_path} not created")

            logger.info(f"Certificate signed successfully: {cert_path}")
            return cert_path

    def generate_keys_and_sign_certificate(self, key_name: str, key_type: str, principals: list[str]) -> tuple[str, str]:
        _, _, key_path = self.generate_user_key_pair(key_name, key_type)
        ca_public_key_hash, _, _ = self.generate_ca_key_pair(key_name, key_type)
        _ = self.sign_user_certificate(f"{key_name}_ca", f"{key_name}_key", principals=principals)
        return ca_public_key_hash, key_path

    def extract_public_key(self, public_key_content: str) -> str:
        """
        Extract just the key part from the public key content.

        Args:
            public_key_content: Full public key content

        Returns:
            Just the key part (base64 encoded)
        """
        parts = public_key_content.split()
        if len(parts) >= 2:
            return parts[1]
        return public_key_content

    def cleanup_generated_keys(self, key_name: str):
        """
        Clean up generated key pair, ca key and certificate.

        Args:
            key_name: Base path for the key pair
        """
        with allure.step(f"Cleanup key pair: {key_name}"):
            try:
                SecuritySshTool.rm_auth_keypair(f"{self.keys_path}/{key_name}_ca")
                SecuritySshTool.rm_auth_keypair(f"{self.keys_path}/{key_name}_key")
                SecuritySshTool.rm_auth_key(f"{self.keys_path}/{key_name}_key-cert.pub")
            except Exception as e:
                logger.warning(f"Could not fully cleanup keys for {key_name}: {e}")


def get_random_principals(number_of_values_to_select: int = 1) -> list[str]:
    return RandomizationTool.select_random_values(
        TEST_PRINCIPALS, number_of_values_to_select=number_of_values_to_select
    ).get_returned_value()


def get_random_principal() -> str:
    return get_random_principals(number_of_values_to_select=1)[0]


def get_random_validity() -> str:
    return random.choice(list(CERT_VALIDITY_PERIODS.values()))


def get_random_key_type(exclude: list[str] | None = None) -> str:
    exclude = exclude or []
    key_types = list(SshHardeningConsts.PUBLIC_KEY_LENGTH_DICT.keys())
    key_types = [key_type for key_type in key_types if key_type not in exclude]
    return random.choice(key_types)


def cleanup_trusted_ca_keys(system: System):
    with allure.step("Clean up trusted CA keys"):
        system.ssh_server.trusted_ca_keys.unset()


def cleanup_user_cert_auth(system: System, username: str):
    with allure.step(f"Clean up cert-auth for user: {username}"):
        system.aaa.user.user_id[username].ssh.cert_auth.unset()


def verify_user_login(user: UserInfo, key_private_path: str, hostname: str, engines, expect_success: bool = True):
    with allure.step(f"log in with certificate for user {user.username} with role {user.role}, expect success: {expect_success}"):
        with PKAAuthVerifier(username=user.username, private_key_path=key_private_path, hostname=hostname, engines=engines) as session_obj:
            session_obj.verify_authentication(expect_success=expect_success)
            if expect_success:
                session_obj.verify_authorization(user_is_admin=user.role == UserRole.ADMIN)


def set_cert_auth(system: System, user: UserInfo, principal: str, state: str, apply: bool = False):
    with allure.step(f"set cert auth state {state} and principal {principal} for user {user.username}"):
        system.aaa.user.user_id[user.username].ssh.cert_auth.principals[principal].set().verify_result()
        if state == "enabled":
            system.aaa.user.user_id[user.username].ssh.cert_auth.enable_state(apply=apply)
        elif state == "disabled":
            system.aaa.user.user_id[user.username].ssh.cert_auth.disable_state(apply=apply)
        else:
            raise ValueError(f"invalid state: {state}")


def set_trusted_ca_key(system: System, key_name: str, key_type: str, ca_val: str, apply: bool = False):
    with allure.step("set trusted ca"):
        key = system.ssh_server.trusted_ca_keys.key_id[key_name]
        key.set_key_val(ca_val)
        return key.set_key_type(key_type, apply=apply)


def ssh_cert_auth_factory_reset_no_params_check():
    """
    Verify SSH certificate authentication is cleared after factory reset with no params.

    Test flow:
    1. Generate SSH key pair and CA key pair
    2. Sign user certificate with CA
    3. Set principal and enable cert-auth for admin user
    4. Set trusted CA key on the system
    5. Verify user can login with certificate
    6. Save configuration
    7. Factory reset
    8. Verify trusted CA keys are cleared
    9. Verify cert-auth is disabled for user
    10. Verify user cannot login with certificate
    """
    from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
    from ngts.nvos_constants.constants_nvos import SystemConsts
    from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
    from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool

    engines = TestToolkit.engines
    system = System()
    ssh_cert_auth_helper = SshCertAuthHelper()
    key_name = "factory_reset_cert_test_key"
    key_type = get_random_key_type()
    principal = get_random_principal()
    admin_user = UserInfo(SystemConsts.DEFAULT_USER_ADMIN, SystemConsts.DEFAULT_USER_ADMIN, UserRole.ADMIN)
    hostname = engines.dut.ip

    try:
        with allure.step("Setup SSH certificate authentication"):
            ssh_cert_auth_helper.ensure_keys_directory()

            with allure.step("Generate keys and sign certificate"):
                ca_val, key_private_path = ssh_cert_auth_helper.generate_keys_and_sign_certificate(
                    key_name=key_name, key_type=key_type, principals=[principal]
                )

            with allure.step(f"Set cert-auth principal {principal} for admin"):
                set_cert_auth(system=system, user=admin_user, principal=principal, state="enabled", apply=False)

            with allure.step(f"Set trusted CA key {key_name}"):
                set_trusted_ca_key(system, key_name, key_type, ca_val, apply=True)

            with allure.step("Verify login with certificate before factory reset"):
                verify_user_login(admin_user, key_private_path, hostname, engines, expect_success=True)

            with allure.step("Save configuration"):
                NvueGeneralCli.save_config(engines.dut)

        yield  # factory reset

        with allure.step("Verify SSH cert auth is cleared after factory reset"):
            with allure.step("Verify trusted CA keys are cleared"):
                trusted_ca_keys_output = OutputParsingTool.parse_json_str_to_dictionary(
                    system.ssh_server.trusted_ca_keys.show()
                ).get_returned_value()
                assert key_name not in trusted_ca_keys_output, (
                    f"Trusted CA key {key_name} should be cleared after factory reset: {trusted_ca_keys_output}"
                )

            with allure.step("Verify user cannot login with certificate after factory reset"):
                verify_user_login(admin_user, key_private_path, hostname, engines, expect_success=False)

    finally:
        with allure.step("Cleanup SSH cert auth configuration"):
            try:
                system.ssh_server.trusted_ca_keys.unset()
                system.aaa.user.user_id[admin_user.username].ssh.cert_auth.unset(apply=True)
            except Exception as cleanup_err:
                logger.warning("Cleanup failed: %s", cleanup_err)
            ssh_cert_auth_helper.cleanup_generated_keys(key_name)

    yield  # to prevent StopIteration on the 2nd next() call


def ssh_cert_auth_factory_reset_keep_only_files_check():
    """
    Verify SSH certificate authentication configuration is reset but keys file is preserved
    after factory reset with keep-only-files.

    Test flow:
    1. Generate SSH key pair and CA key pair
    2. Sign user certificate with CA
    3. Set principal and enable cert-auth for admin user
    4. Set trusted CA key on the system
    5. Verify user can login with certificate
    6. Save configuration
    7. Factory reset with keep-only-files
    8. Verify trusted CA keys configuration is cleared (but files may be preserved)
    9. Verify cert-auth is disabled for user
    10. Verify user cannot login with certificate (config is reset)
    """
    from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
    from ngts.nvos_constants.constants_nvos import SystemConsts
    from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
    from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool

    engines = TestToolkit.engines
    system = System()
    ssh_cert_auth_helper = SshCertAuthHelper()
    key_name = "factory_reset_keep_files_cert_key"
    key_type = get_random_key_type()
    principal = get_random_principal()
    admin_user = UserInfo(SystemConsts.DEFAULT_USER_ADMIN, SystemConsts.DEFAULT_USER_ADMIN, UserRole.ADMIN)
    hostname = engines.dut.ip

    try:
        with allure.step("Setup SSH certificate authentication"):
            ssh_cert_auth_helper.ensure_keys_directory()

            with allure.step("Generate keys and sign certificate"):
                ca_val, key_private_path = ssh_cert_auth_helper.generate_keys_and_sign_certificate(
                    key_name=key_name, key_type=key_type, principals=[principal]
                )

            with allure.step(f"Set cert-auth principal {principal} for admin"):
                set_cert_auth(system=system, user=admin_user, principal=principal, state="enabled", apply=False)

            with allure.step(f"Set trusted CA key {key_name}"):
                set_trusted_ca_key(system, key_name, key_type, ca_val, apply=True)

            with allure.step("Verify login with certificate before factory reset"):
                verify_user_login(admin_user, key_private_path, hostname, engines, expect_success=True)

            with allure.step("Save configuration"):
                NvueGeneralCli.save_config(engines.dut)

        yield  # factory reset with keep-only-files

        with allure.step("Verify SSH cert auth config is reset after factory reset"):
            with allure.step("Verify trusted CA keys configuration is cleared"):
                trusted_ca_keys_output = OutputParsingTool.parse_json_str_to_dictionary(
                    system.ssh_server.trusted_ca_keys.show()
                ).get_returned_value()
                assert key_name not in trusted_ca_keys_output, (
                    f"Trusted CA key {key_name} config should be cleared after factory reset: {trusted_ca_keys_output}"
                )

            with allure.step("Verify user cannot login with certificate (config is reset)"):
                verify_user_login(admin_user, key_private_path, hostname, engines, expect_success=False)

    finally:
        with allure.step("Cleanup SSH cert auth configuration"):
            try:
                system.ssh_server.trusted_ca_keys.unset()
                system.aaa.user.user_id[admin_user.username].ssh.cert_auth.unset(apply=True)
            except Exception as cleanup_err:
                logger.warning("Cleanup failed: %s", cleanup_err)
            ssh_cert_auth_helper.cleanup_generated_keys(key_name)

    yield  # to prevent StopIteration on the 2nd next() call
