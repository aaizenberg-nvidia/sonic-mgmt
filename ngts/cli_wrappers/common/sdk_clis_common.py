import subprocess
import logging
import os
import glob
import re
from ngts.constants.constants import InfraConst, Sonic_Cache, CliType
from ngts.constants.performance_constants import PerfConsts
from ngts.helpers.system_helpers import copy_files_to_syncd
from ngts.helpers.gerrit_helper import get_latest_patchset_ref
from devts.infra.tools.exceptions.test_issue import TestIssue

logger = logging.getLogger()


class SdkCliCommon():
    """
    This class hosts methods which are implemented identically for Linux
    """

    def __init__(self, engine, cli_obj=None, dut_alias=None):
        self.engine = engine
        self.cli_obj = cli_obj
        self.dut_alias = dut_alias

    def get_sdk_version(self):
        sdk_version_output = self.engine.run_cmd(InfraConst.CMD_GET_SDK_VERSION, validate=True)
        sdk_version = re.search(r"SX-SDK ETH (\d+\.\d+\.\d+)", sdk_version_output).group(1)
        return sdk_version

    def get_sdk_branch(self, sdk_version):
        command = f"cat /auto/sw_system_release/sx_sdk_eth/sx_sdk_eth-{sdk_version}/SDK_BRANCH.txt"
        sdk_branch = os.popen(command).read().strip()
        if sdk_branch.startswith("sx_sdk_"):
            sdk_branch = re.search(r"(sx_sdk_\d+_\d+_\d{4})", sdk_branch).group(1)
        return sdk_branch

    def get_kernel_version(self):
        kernel_version_output = self.engine.run_cmd('uname -r', validate=True)
        kernel_version = re.search(r"(\d+\.\d+\.\d+)", kernel_version_output).group(1)
        return kernel_version

    def get_latest_sdk_version(self, cur_sdk_version, sdk_branch):
        """Resolve the latest SDK version published under the branch lastrc DEBS folder.

        Looks up ``lastrc_{sdk_branch}/DEBS``, finds a kernel folder matching the DUT
        kernel, and extracts the SDK version from the ``sys-sdk-git`` deb filename.
        Returns ``cur_sdk_version`` when no matching kernel folder is found.

        Args:
            cur_sdk_version (str): SDK version currently installed on the DUT, used as
                the fallback when no matching kernel package is available.
            sdk_branch (str): SDK branch name used to build the lastrc DEBS path
                (e.g. ``sx_sdk_4_10_1000``).

        Returns:
            str: Latest SDK version parsed from the matching deb, or ``cur_sdk_version``
            if no DUT-kernel match exists.

        Raises:
            FileNotFoundError: If the lastrc DEBS directory does not exist.
            IndexError: If no ``sys-sdk-git`` deb is present under the matched kernel folder.
            AttributeError: If the deb filename does not match the expected version pattern.
        """
        dut_kernel_version = self.get_kernel_version()
        deb_file_path = os.path.join(PerfConsts.LATEST_SDK_DEB_DIR_TEMPLATE.format(SDK_BRANCH=sdk_branch))
        logger.info(
            f"Looking up latest SDK version under '{deb_file_path}' "
            f"for DUT kernel '{dut_kernel_version}' (current SDK version: '{cur_sdk_version}')"
        )
        available_kernel_versions = os.listdir(deb_file_path)
        logger.info(f"Available kernel folders in lastrc DEBS: {sorted(available_kernel_versions)}")

        deb_kernel_version = None
        for kernel_version in available_kernel_versions:
            if kernel_version.startswith(dut_kernel_version):
                deb_file_path = os.path.join(deb_file_path, kernel_version)
                deb_kernel_version = kernel_version
                break
        if not deb_kernel_version:
            logger.warning(
                f"No matching kernel version found for '{dut_kernel_version}' "
                f"under lastrc DEBS; keeping current SDK version '{cur_sdk_version}'"
            )
            return cur_sdk_version
        logger.info(f"Matched DUT kernel '{dut_kernel_version}' to DEBS folder '{deb_kernel_version}'")
        files_available_in_deb_dir = glob.glob(os.path.join(deb_file_path, PerfConsts.LATEST_SDK_DEB_FILE_TEMPLATE))
        logger.info(f"sys-sdk-git packages found under '{deb_file_path}': {files_available_in_deb_dir}")
        sdk_version = re.search(r"sys-sdk-git_1.mlnx.(\d+.\d+.\d+.*\d*)_", files_available_in_deb_dir[0]).group(1)

        if sdk_version.count('.') == 3:
            last_dot_index = sdk_version.rfind('.')
            sdk_version = sdk_version[:last_dot_index] + '-' + sdk_version[last_dot_index + 1:]

        logger.info(f"Resolved latest SDK version '{sdk_version}' from '{files_available_in_deb_dir[0]}'")
        return sdk_version

    def get_latest_sdk_version_or_current(self, cur_sdk_version, sdk_branch):
        """Resolve the latest SDK version, keeping the DUT version when resolution fails.

        The lastrc release folder for a branch is not always published (or is pruned),
        so upgrading to the latest deb is best effort and must never fail the flow.
        """
        try:
            logger.info(
                f"About to get latest SDK version for branch '{sdk_branch}' "
                f"and current SDK version '{cur_sdk_version}'"
            )
            latest_sdk_version = self.get_latest_sdk_version(cur_sdk_version=cur_sdk_version,
                                                             sdk_branch=sdk_branch)
            logger.info(f"Latest SDK version resolved successfully with version '{latest_sdk_version}'")
        except Exception as err:
            logger.warning(
                f"Could not resolve latest SDK version for branch '{sdk_branch}' ({err}); "
                f"continuing with the SDK version installed on the DUT: {cur_sdk_version}"
            )
            return cur_sdk_version

        if latest_sdk_version != cur_sdk_version:
            logger.info(f"Using latest SDK version {latest_sdk_version} instead of {cur_sdk_version}")
        return latest_sdk_version

    def resolve_sdk_version_and_branch_for_traffic_generator(self, latest_version=True):
        """Resolve the SDK version and branch used to install the traffic generator.

        Reads the DUT SDK version and branch, optionally upgrading to the latest
        published version for that branch when ``latest_version`` is True.

        Args:
            latest_version (bool): Whether to attempt resolving the latest SDK version
                for the DUT branch.

        Returns:
            tuple[str, str]: Selected ``(sdk_version, sdk_branch)``.
        """
        logger.info(
            f"resolve_sdk_version_and_branch_for_traffic_generator called with "
            f"latest_version={latest_version}"
        )
        sdk_version = self.get_sdk_version()
        sdk_branch = self.get_sdk_branch(sdk_version)
        logger.info(
            f"Preparing traffic generator installation with DUT SDK version "
            f"'{sdk_version}' from branch '{sdk_branch}'"
        )
        if latest_version:
            dut_sdk_version = sdk_version
            logger.info(f"Attempting to resolve the latest SDK version for branch '{sdk_branch}'")
            sdk_version = self.get_latest_sdk_version_or_current(cur_sdk_version=sdk_version,
                                                                 sdk_branch=sdk_branch)
            logger.info(
                f"SDK version selected for traffic generator installation: '{sdk_version}' "
                f"(DUT version: '{dut_sdk_version}')"
            )
        else:
            logger.info(f"Latest SDK lookup disabled; using DUT SDK version '{sdk_version}'")
        return sdk_version, sdk_branch

    @staticmethod
    def _kernel_dir_sort_key(kernel_dir):
        """Sort key for kernel folders by their leading numeric version (e.g. '6.12.41-...' -> (6, 12, 41))."""
        match = re.match(r'(\d+)\.(\d+)\.(\d+)', kernel_dir)
        return tuple(int(part) for part in match.groups()) if match else (0, 0, 0)

    def resolve_sdk_git_deb_path(self, sdk_version, cli_type=None):
        """Resolve the absolute path to the sys-sdk-git .deb, discovering the
        kernel-specific DEBS subdirectory at runtime.

        Release builds lay the deb out as ``DEBS/<kernel>/sys-sdk-git_*.deb`` and
        the set of ``<kernel>`` folders changes between SDK versions (e.g. the
        4.10.0300 build shipped ``6.1.0-29-2-amd64`` while 4.10.0964 dropped it in
        favor of ``6.12.41-deb13-dvs-amd64``). Assuming a fixed kernel folder
        breaks whenever that happens, so we prefer the folder matching the DUT
        kernel and otherwise fall back to the newest folder that actually contains
        the deb.
        """
        deb_sdk_version = (sdk_version.replace('-', '.')
                           if cli_type == CliType.SONIC else sdk_version)
        deb_file_name = PerfConsts.SDK_DEB_FILE_TEMPLATE.format(SDK_VERSION=deb_sdk_version)
        debs_root = os.path.join(PerfConsts.SDK_VERSION_PATH, f"sx_sdk_eth-{sdk_version}", "DEBS")
        if not os.path.isdir(debs_root):
            raise TestIssue(f"SDK DEBS directory not found: {debs_root}")

        candidates = [
            kernel_dir for kernel_dir in os.listdir(debs_root)
            if os.path.isfile(os.path.join(debs_root, kernel_dir, deb_file_name))
        ]
        if not candidates:
            raise TestIssue(
                f"No '{deb_file_name}' found under any kernel folder in {debs_root}. "
                f"Available folders: {sorted(os.listdir(debs_root))}"
            )

        dut_kernel_version = self.get_kernel_version()
        chosen = next((kernel_dir for kernel_dir in candidates
                       if kernel_dir.startswith(dut_kernel_version)), None)
        if chosen is None:
            chosen = sorted(candidates, key=self._kernel_dir_sort_key, reverse=True)[0]
            logger.warning(
                f"No SDK deb built for DUT kernel '{dut_kernel_version}' in {debs_root}; "
                f"falling back to '{chosen}' (available: {sorted(candidates)})"
            )
        deb_path = os.path.join(debs_root, chosen, deb_file_name)
        logger.info(f"Resolved SDK git deb for version {sdk_version} to: {deb_path}")
        return deb_path

    def get_perf_sys_sdk_tar_file_name(self, sdk_branch):
        file_name = PerfConsts.PERF_SYS_SDK_TAR_FILE_TEMPLATE.format(SDK_BRANCH=sdk_branch)
        path = PerfConsts.PERF_SYS_SDK_TAR_FILE_PATH
        if not os.path.exists(os.path.join(path, file_name)):
            logger.warning(f"Performance system SDK tar file {file_name} not found in {path}, use master as default")
            file_name = PerfConsts.PERF_SYS_SDK_TAR_FILE_TEMPLATE.format(SDK_BRANCH='master')
        if not os.path.exists(os.path.join(path, file_name)):
            raise TestIssue(f"Performance system SDK tar file {file_name} for master not found in {path}, please check the sdk branch")
        return path, file_name

    def get_generic_cmd_prefix(self, prefix_cmd, cmd):
        generic_cmd_prefix = f"{prefix_cmd} '{cmd}'" if prefix_cmd else cmd
        return generic_cmd_prefix

    PERF_SYS_SDK_CHANGE_BRANCH_PREFIX = "branch_"

    def resolve_perf_sys_sdk_change(self, perf_sys_sdk_change):
        """
        Resolve a raw ``--perf_sys_sdk_change`` value into an explicit branch or Gerrit
        change number, before it's passed to methods like ``overlay_perf_sys_sdk_to_sys_sdk``.

        Args:
            perf_sys_sdk_change (str | None): Raw value: ``'branch_<name>'``, a numeric
                Gerrit change number, or None.

        Returns:
            tuple[str | None, str | None]: ``(perf_sys_sdk_branch, perf_sys_sdk_gerrit_change)``.
            Exactly one is non-None, or both are None when ``perf_sys_sdk_change`` is None.
        """
        if perf_sys_sdk_change is None:
            return None, None
        if perf_sys_sdk_change.startswith(self.PERF_SYS_SDK_CHANGE_BRANCH_PREFIX):
            return perf_sys_sdk_change[len(self.PERF_SYS_SDK_CHANGE_BRANCH_PREFIX):], None
        return None, perf_sys_sdk_change

    def overlay_perf_sys_sdk_to_sys_sdk(self, sdk_branch, is_in_syncd=False, tar_file_enabled=False,
                                        perf_sys_sdk_branch=None, perf_sys_sdk_gerrit_change=None):
        """
        Args:
            sdk_branch (str): SDK branch derived from the DUT's installed SDK version.
            perf_sys_sdk_branch (str | None): Explicit perf_sys_sdk branch to clone, overriding
                ``sdk_branch``. Resolved from ``--perf_sys_sdk_change`` via
                ``resolve_perf_sys_sdk_change``.
            perf_sys_sdk_gerrit_change (str | None): Gerrit change number whose latest patchset
                should be fetched instead of cloning a branch. Mutually exclusive with
                ``perf_sys_sdk_branch``.
        """
        docker_exec_syncd_cmd = InfraConst.DOCKER_EXEC_BASH_CMD.format(DOCKER=InfraConst.SYNCD_DOCKER)
        command_prefix = f"{docker_exec_syncd_cmd}" if is_in_syncd else ""

        if perf_sys_sdk_gerrit_change is not None:
            if tar_file_enabled:
                raise TestIssue(
                    f"perf_sys_sdk_gerrit_change={perf_sys_sdk_gerrit_change!r} requests a Gerrit "
                    f"change number, which is not supported together with the perf_sys_sdk "
                    f"tar-file flow."
                )
            logger.info(f"Overlaying perf_sys_sdk onto sys_sdk from Gerrit change {perf_sys_sdk_gerrit_change}")
            self.get_perf_sys_sdk_with_clone_from_change(perf_sys_sdk_gerrit_change, command_prefix)
        else:
            branch_to_use = perf_sys_sdk_branch or sdk_branch
            logger.info(
                f"Overlaying perf_sys_sdk onto sys_sdk using branch '{branch_to_use}' "
                f"({'explicit --perf_sys_sdk_change branch override' if perf_sys_sdk_branch else 'DUT-derived SDK branch'})"
            )
            if tar_file_enabled:
                self.get_perf_sys_sdk_with_tar(branch_to_use, command_prefix, is_in_syncd)
            else:
                self.get_perf_sys_sdk_with_clone(branch_to_use, command_prefix)

        self.run_overlay_cmd(command_prefix, is_in_syncd)

    def clean_up_existing_sdk_folders(self, sys_sdk_path, sudo_prefix, command_prefix, is_in_syncd):
        """
        Clean up existing SDK folders from sys_sdk.

        Args:
            command_prefix (str): Command prefix to prepend to commands, for example docker exec or similar,
                                  when running inside a container.
            is_in_syncd (bool): Whether to execute commands inside the syncd docker container.
            sys_sdk_path (str): The path to the sys_sdk directory.
            sudo_prefix (str): The prefix to use for sudo commands.
        """
        dirs_to_remove = [
            'sx_sdk_py_tests/libs/base_classes/multi_nos/',
            'sx_sdk_py_tests/libs/multi_nos_lib/',
            'sx_sdk_py_tests/tests/multi_os_tests/',
            'sx_sdk_py_tests/tools/multi_nos/',
        ]
        for dir_name in dirs_to_remove:
            target_dir = os.path.join(sys_sdk_path, dir_name)
            rm_cmd = f'{sudo_prefix}rm -rf {target_dir}'
            if is_in_syncd:
                rm_cmd = self.get_generic_cmd_prefix(command_prefix, rm_cmd)
            self.engine.run_cmd(rm_cmd, validate=True)
            logger.info(f'Directory {target_dir} deleted successfully')

    def run_overlay_cmd(self, command_prefix, is_in_syncd):
        """
        Run the overlay command to copy perf_sys_sdk files into sys_sdk.

        Args:
            command_prefix (str): Command prefix to prepend to commands, for example docker exec or similar,
                                  when running inside a container.
            is_in_syncd (bool): Whether to execute commands inside the syncd docker container.

        Returns:
            None
        """

        perf_sys_sdk_path = '/root/perf_sys_sdk'
        sys_sdk_path = '/root/sys_sdk'

        sudo_prefix = '' if is_in_syncd else 'sudo -i '
        self.clean_up_existing_sdk_folders(sys_sdk_path, sudo_prefix, command_prefix, is_in_syncd)

        overlay_cmd = f'{sudo_prefix}{perf_sys_sdk_path}/overlay_files.py {sys_sdk_path} {perf_sys_sdk_path} to_sys_sdk --verbose'
        cmd_to_run = overlay_cmd if not is_in_syncd else self.get_generic_cmd_prefix(command_prefix, overlay_cmd)
        self.engine.run_cmd(cmd_to_run, validate=True)

    def get_repo_branches(self, repo_url):
        """
        Get the branches from a repository.

        Args:
            repo_url (str): The URL of the repository.

        Returns:
            list: A list of branches.
        """
        # Mask credentials in repo_url (e.g. https://user:pass@... -> https://***:***@...)
        sanitized_url = re.sub(r'://[^@]+@', '://***:***@', repo_url)
        cmd = ["git", "ls-remote", "--heads", repo_url]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        except subprocess.CalledProcessError as e:
            # Mask credentials in stderr and stdout for logging
            sanitized_stderr = re.sub(r'://[^@]+@', '://***:***@', e.stderr or '')
            sanitized_stdout = re.sub(r'://[^@]+@', '://***:***@', e.stdout or '')
            raise RuntimeError(
                f'git ls-remote failed for {sanitized_url} with exit code {e.returncode}. '
                f'stderr: {sanitized_stderr.strip()}. stdout: {sanitized_stdout.strip()}'
            ) from None
        branches = []
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) == 2:
                ref = parts[1]
                if ref.startswith("refs/heads/"):
                    branches.append(ref.replace("refs/heads/", ""))
        return branches

    def get_perf_sys_sdk_with_clone(self, sdk_branch, command_prefix):
        repo_url = f"https://{Sonic_Cache.gerrit_username}:{Sonic_Cache.gerrit_api_token}@git-nbu-sw.nvidia.com/r/a/switchx/perf_sys_sdk"
        # Mask credentials in repo_url (e.g. https://user:pass@... -> https://***:***@...)
        sanitized_url = re.sub(r'://[^@]+@', '://***:***@', repo_url)
        default_sdk_branch = "master"
        remote_branches = self.get_repo_branches(repo_url)
        sdk_branch_to_use = sdk_branch if sdk_branch in remote_branches else default_sdk_branch
        sudo_prefix = '' if command_prefix else 'sudo -i '
        clone_dest = '/root/perf_sys_sdk'

        # Remove any existing clone to avoid "already exists" errors
        cleanup_cmd = f'{sudo_prefix}rm -rf {clone_dest}'
        cleanup_full_cmd = self.get_generic_cmd_prefix(command_prefix, cleanup_cmd)
        self.engine.run_cmd(cleanup_full_cmd, validate=True)

        clone_cmd = f'{sudo_prefix}git clone --branch {sdk_branch_to_use} \"{repo_url}\" {clone_dest}'
        full_cmd = self.get_generic_cmd_prefix(command_prefix, clone_cmd)
        sanitized_clone_cmd = f'{sudo_prefix}git clone --branch {sdk_branch_to_use} \"{sanitized_url}\" {clone_dest}'
        sanitized_full_cmd = self.get_generic_cmd_prefix(command_prefix, sanitized_clone_cmd)
        logger.info(f'Running CMD: {sanitized_full_cmd}')
        self.engine.run_cmd(full_cmd, validate=True, print_output=False)

    def get_perf_sys_sdk_change_ref(self, repo_url, change_number):
        """
        Resolve the Gerrit ref of the latest patchset for a change number.

        Args:
            repo_url (str): The URL of the repository.
            change_number (str): Gerrit change number.

        Returns:
            str: The ref of the latest patchset, e.g. 'refs/changes/42/347942/1'.

        Raises:
            RuntimeError: If ``git ls-remote`` fails.
            TestIssue: If the change has no patch sets on the remote.
        """
        return get_latest_patchset_ref(repo_url, change_number)

    def get_perf_sys_sdk_with_clone_from_change(self, change_number, command_prefix):
        """
        Clone perf_sys_sdk and check out the latest patchset of an unmerged Gerrit change.

        Args:
            change_number (str): Gerrit change number.
            command_prefix (str): Command prefix to prepend to commands, for example docker exec.
        """
        repo_url = f"https://{Sonic_Cache.gerrit_username}:{Sonic_Cache.gerrit_api_token}@git-nbu-sw.nvidia.com/r/a/switchx/perf_sys_sdk"
        sanitized_url = re.sub(r'://[^@]+@', '://***:***@', repo_url)
        change_ref = self.get_perf_sys_sdk_change_ref(repo_url, change_number)
        sudo_prefix = '' if command_prefix else 'sudo -i '
        clone_dest = '/root/perf_sys_sdk'

        # Remove any existing clone to avoid "already exists" errors
        cleanup_cmd = f'{sudo_prefix}rm -rf {clone_dest}'
        cleanup_full_cmd = self.get_generic_cmd_prefix(command_prefix, cleanup_cmd)
        self.engine.run_cmd(cleanup_full_cmd, validate=True)

        clone_cmd = f'{sudo_prefix}git clone \"{repo_url}\" {clone_dest}'
        full_cmd = self.get_generic_cmd_prefix(command_prefix, clone_cmd)
        sanitized_clone_cmd = f'{sudo_prefix}git clone \"{sanitized_url}\" {clone_dest}'
        sanitized_full_cmd = self.get_generic_cmd_prefix(command_prefix, sanitized_clone_cmd)
        logger.info(f'Running CMD: {sanitized_full_cmd}')
        self.engine.run_cmd(full_cmd, validate=True, print_output=False)

        fetch_checkout_cmd = (
            f'{sudo_prefix}git -C {clone_dest} fetch \"{repo_url}\" {change_ref} '
            f'&& {sudo_prefix}git -C {clone_dest} checkout FETCH_HEAD'
        )
        full_fetch_checkout_cmd = self.get_generic_cmd_prefix(command_prefix, fetch_checkout_cmd)
        sanitized_fetch_checkout_cmd = (
            f'{sudo_prefix}git -C {clone_dest} fetch \"{sanitized_url}\" {change_ref} '
            f'&& {sudo_prefix}git -C {clone_dest} checkout FETCH_HEAD'
        )
        logger.info(f'Running CMD: {self.get_generic_cmd_prefix(command_prefix, sanitized_fetch_checkout_cmd)}')
        self.engine.run_cmd(full_fetch_checkout_cmd, validate=True, print_output=False)

    def get_perf_sys_sdk_with_tar(self, sdk_branch, command_prefix, is_in_syncd):
        path, file_name = self.get_perf_sys_sdk_tar_file_name(sdk_branch)
        sudo_prefix = '' if is_in_syncd else 'sudo '
        tar_cmd = f'{sudo_prefix}tar -xzf /tmp/{file_name} -C /root/'
        if is_in_syncd:
            copy_files_to_syncd(self.engine, [file_name], path, syncd_dir='/tmp/')
            self.engine.run_cmd(self.get_generic_cmd_prefix(command_prefix, tar_cmd))
        else:
            self.engine.copy_file(source_file=os.path.join(path, file_name),
                                  dest_file=file_name,
                                  file_system='/tmp/', overwrite_file=True, verify_file=False)
            self.engine.run_cmd(tar_cmd)
