"""
NVOS Git Tool - utilities for working with the NVOS git repository.

Provides methods to query git tags, extract firmware versions from fw.mk,
and find previous firmware versions for upgrade/downgrade testing.

Uses pre-cloned repo at shared location (synced by cron).
Approach tested and verified in regression environment (nvos_ver-25-02-7000).
"""
from __future__ import annotations

import atexit
import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Literal, List

logger = logging.getLogger()

# Type alias for image types
ImageType = Literal['dev', 'prod']


@dataclass
class TagSearchContext:
    """Context for tag-based FW search operations."""
    version: str
    image_type: ImageType
    branch_prefix: str
    tags: list[str]
    target_fw: str
    target_exists_in_tags: bool


class NvosGitTool:
    """Tool for NVOS git repository operations."""

    # Pre-cloned nvos repo, refreshed hourly by /auto/sw_system_project/NVOS_INFRA/nvos-cache/pull.sh
    # from an owner's cron. Fetches --tags --prune from gitlab-master so newly cut branches
    # like nvos-25-03-1000 land automatically. Override with $NVOS_GIT_REPO_PATH if needed.
    DEFAULT_NVOS_REPO_PATH = "/auto/sw_system_project/NVOS_INFRA/nvos-cache/nvos"

    # FW version patterns in fw.mk (pre-compiled for performance)
    FW_PATTERNS = {
        'QTM4': re.compile(r'MLNX_IB_QTM4_FW_VER\s*=\s*(\S+)'),
        'QTM3': re.compile(r'MLNX_IB_QTM3_FW_VER\s*=\s*(\S+)'),
    }

    # Pre-compiled regex patterns for version parsing
    VERSION_TAG_PATTERN = re.compile(r'_(\d+\.\d+\.\d+(?:-\d+)?)')
    VERSION_FROM_FILENAME_PATTERN = re.compile(r'nvos-amd64-(\d+\.\d+\.\d+(?:-\d+)?)')
    VERSION_FROM_PATH_PATTERN = re.compile(r'/(\d+\.\d+\.\d+(?:-\d+)?)/amd64/')

    # Env var override: precedence is explicit repo_path > $NVOS_GIT_REPO_PATH > default.
    # Useful when the shared clone at DEFAULT_NVOS_REPO_PATH is stale or not writable
    # and a user-owned mirror (refreshed by their own cron) is preferred.
    ENV_REPO_PATH = "NVOS_GIT_REPO_PATH"

    def __init__(self, repo_path: str | None = None):
        """
        Initialize NvosGitTool.

        Args:
            repo_path: Path to NVOS git repository. If not provided, falls back to
                $NVOS_GIT_REPO_PATH, then to DEFAULT_NVOS_REPO_PATH.

        Example:
            >>> git_tool = NvosGitTool()
            >>> git_tool = NvosGitTool("/custom/path/to/nvos")
        """
        self.repo_path = Path(
            repo_path or
            os.environ.get(self.ENV_REPO_PATH) or
            self.DEFAULT_NVOS_REPO_PATH
        )

    # Class-level temp directory to avoid race conditions between instances
    _tmp_home = None

    @classmethod
    def _get_tmp_home(cls) -> Path:
        """Get or create temporary HOME directory for git config."""
        if cls._tmp_home is None or not cls._tmp_home.exists():
            cls._tmp_home = Path(tempfile.mkdtemp(prefix="nvos_git_tool_"))
            gitconfig = cls._tmp_home / ".gitconfig"
            gitconfig.write_text("[safe]\n\tdirectory = *\n")
            # Register cleanup on process exit
            atexit.register(lambda: shutil.rmtree(cls._tmp_home, ignore_errors=True))
        return cls._tmp_home

    def _run_git_cmd(self, args: List[str], timeout: int = 60) -> subprocess.CompletedProcess:
        """
        Run git command with safe.directory config to handle shared repo ownership.

        Uses the same approach as ISSU tests (nvos_ver-25-02-7000) which was tested
        and verified to work in regression environment.

        Args:
            args: Git command arguments (without 'git' prefix).
            timeout: Command timeout in seconds (default 60).

        Returns:
            CompletedProcess with stdout/stderr.
        """
        repo_path = str(self.repo_path)
        tmp_home = self._get_tmp_home()
        tmp_gitconfig = tmp_home / ".gitconfig"

        cmd = ["git", "-c", "safe.directory=*"] + args

        # Set environment variables to bypass ownership check
        env = os.environ.copy()
        env['HOME'] = str(tmp_home)
        env['GIT_CONFIG_GLOBAL'] = str(tmp_gitconfig)
        env['GIT_CONFIG_SYSTEM'] = '/dev/null'
        env['GIT_CONFIG_NOSYSTEM'] = '1'

        try:
            result = subprocess.run(cmd, cwd=repo_path, capture_output=True, text=True,
                                    env=env, timeout=timeout)
            if result.returncode != 0:
                logger.debug(f"Git command failed: {' '.join(args[:3])}... - {result.stderr[:200]}")
            return result
        except subprocess.TimeoutExpired:
            logger.warning(f"Git command timed out after {timeout}s: {' '.join(args[:3])}...")
            raise ValueError(f"Git command timed out after {timeout}s")

    def _git_output(self, args: list[str]) -> str:
        """Run git command and return stdout stripped."""
        result = self._run_git_cmd(args)
        return result.stdout.strip() if result.returncode == 0 else ""

    def fetch_tags(self) -> None:
        """
        Fetch latest tags from remote.

        Logs a WARNING if the fetch fails so callers know the local tag set may be
        stale — otherwise downstream lookups (e.g. find_previous_fw_version) silently
        operate on an outdated view and can pick wrong "previous" versions.

        Example:
            >>> git_tool.fetch_tags()
        """
        result = self._run_git_cmd(["fetch", "--tags"])
        if result.returncode != 0:
            logger.warning(
                "git fetch --tags failed for %s (rc=%d): %s. "
                "Local tag set may be stale; FW lookups may be inaccurate.",
                self.repo_path, result.returncode,
                (result.stderr or "").strip()[:200]
            )

    @classmethod
    def _version_tuple(cls, version: str) -> tuple[int, ...]:
        """
        Convert an NVOS version string to a tuple of ints for ordered comparison.

        Splits on '.' and '-' and keeps numeric components only, so:
            '25.02.7932-002'  -> (25, 2, 7932, 2)
            '25.02.7930'      -> (25, 2, 7930)
        """
        return tuple(int(p) for p in re.split(r'[.\-]', version) if p.isdigit())

    def list_tags(self, pattern: str | None = None, sort_by_version: bool = True) -> list[str]:
        """
        List git tags, optionally filtered by pattern.

        Args:
            pattern: Glob pattern to filter tags (e.g., 'nvos-25-03-*').
            sort_by_version: Sort by version in descending order.

        Returns:
            List of tag names.

        Example:
            >>> tags = git_tool.list_tags("nvos-25-03-0300_*")
            >>> print(tags[:3])
            ['nvos-25-03-0300_25.03.0209-004', 'nvos-25-03-0300_25.03.0208-001', ...]
        """
        args = ["tag", "-l"]
        if pattern:
            args.append(pattern)
        if sort_by_version:
            args.extend(["--sort=-version:refname"])

        output = self._git_output(args)
        return [t.strip() for t in output.split('\n') if t.strip()] if output else []

    def get_fw_version_from_tag(self, tag: str, asic_type: str | None = None) -> str | None:
        """
        Get firmware version from fw.mk at a specific tag.

        Args:
            tag: Git tag name.
            asic_type: ASIC type ('QTM4', 'QTM3'). If None, tries all patterns.

        Returns:
            Firmware version string or None if not found.

        Example:
            >>> fw = git_tool.get_fw_version_from_tag("nvos-25-03-0300_25.03.0209-004", asic_type="QTM4")
            >>> print(fw)
            '41.2018.0234'
        """
        result = self._run_git_cmd(["show", f"{tag}:platform/mellanox/fw.mk"])
        if result.returncode != 0:
            return None

        patterns = [self.FW_PATTERNS[asic_type]] if asic_type else self.FW_PATTERNS.values()

        for pattern in patterns:
            if match := pattern.search(result.stdout):
                return match.group(1)
        return None

    def find_branch_prefix_for_version(self, version: str) -> str | None:
        """
        Find the branch prefix (e.g., 'nvos-25-03-0300') for a version.

        Args:
            version: NVOS version string (e.g., '25.03.0209-004').

        Returns:
            Branch prefix string or None if not found.

        Example:
            >>> prefix = git_tool.find_branch_prefix_for_version("25.03.0209-004")
            >>> print(prefix)
            'nvos-25-03-0300'
        """
        # Try to find tag ending with this exact version
        tag_output = self._git_output(["tag", "-l", f"*_{version}"])
        if tag_output:
            return tag_output.split('\n')[0].split('_')[0]

        # Try same base version with any sub-build (e.g., 25.03.0106-*)
        # Handles cases where the exact sub-build isn't tagged yet but earlier ones are
        if '-' in version:
            base_version = version.rsplit('-', 1)[0]
            tag_output = self._git_output(["tag", "-l", f"*_{base_version}-*"])
            if tag_output:
                return tag_output.split('\n')[0].split('_')[0]

        # Fallback: construct from version pattern
        parts = version.split('.')
        if len(parts) >= 3:
            build_num = int(parts[2].split('-')[0])
            branch_num = ((build_num // 100) + 1) * 100
            return f"nvos-{parts[0]}-{parts[1]}-{branch_num:04d}"

        return None

    @classmethod
    def parse_version_from_path(cls, version_path: str) -> tuple[str, str]:
        """
        Parse NVOS version and image type from a version path.

        Handles multiple path formats:
        - Standard: /auto/sw_system_release/nos/nvos/25.02.6931-004/amd64/dev/nvos-amd64-25.02.6931-004.bin
        - Lastrc: /auto/sw_system_release/nos/nvos/lastrc_prod_nvos-25-02-7000/nvos-amd64-25.02.6931-019.bin

        Args:
            version_path: Full path to NVOS image.

        Returns:
            Tuple of (version, image_type) e.g., ('25.02.6931-004', 'dev')

        Raises:
            ValueError: If path cannot be parsed.

        Example:
            >>> version, img_type = NvosGitTool.parse_version_from_path(
            ...     "/auto/sw_system_release/nos/nvos/25.03.0209-004/amd64/dev/nvos-amd64-25.03.0209-004.bin"
            ... )
            >>> print(version, img_type)
            '25.03.0209-004' 'dev'
        """
        # Extract version - try filename first (handles lastrc paths), then directory structure
        if not (version_match := cls.VERSION_FROM_FILENAME_PATTERN.search(version_path)):
            if not (version_match := cls.VERSION_FROM_PATH_PATTERN.search(version_path)):
                raise ValueError(f"Could not parse version from path: {version_path}")
        version = version_match.group(1)

        # Determine image type - check explicit paths first, then lastrc patterns
        if '/prod/' in version_path or 'lastrc_prod' in version_path:
            image_type = 'prod'
        elif '/dev/' in version_path or 'lastrc_' in version_path:
            image_type = 'dev'
        elif '-' not in version:
            # No patch suffix (e.g., 25.03.0104) typically means prod
            image_type = 'prod'
        else:
            image_type = 'dev'

        return version, image_type

    # FW release path for building FW file paths
    FW_RELEASE_PATH = "/auto/mswg/release/sx_mlnx_fw"

    # NVOS release path for building image paths
    NVOS_RELEASE_PATH = "/auto/sw_system_release/nos/nvos"

    @classmethod
    def build_image_path(cls, version: str, image_type: ImageType = 'dev') -> str:
        """
        Build NVOS image path for given version.

        Args:
            version: NVOS version (e.g., '25.02.6931-019')
            image_type: 'dev' or 'prod'

        Returns:
            Full path to NVOS image file.

        Example:
            >>> path = NvosGitTool.build_image_path('25.02.6931-019', 'prod')
            >>> print(path)
            '/auto/sw_system_release/nos/nvos/25.02.6931-019/amd64/prod/nvos-amd64-25.02.6931-019.bin'
        """
        return f"{cls.NVOS_RELEASE_PATH}/{version}/amd64/{image_type}/nvos-amd64-{version}.bin"

    @classmethod
    def build_fw_file_path(cls, chip: str, fw_version: str, image_type: ImageType = 'dev') -> str:
        """
        Build explicit FW file path from chip type and version.

        Args:
            chip: Chip type (e.g., 'QTM3', 'QTM4')
            fw_version: FW version with dots (e.g., '35.2016.3068')
            image_type: 'dev' or 'prod'

        Returns:
            Full path to FW file (e.g., /auto/mswg/release/sx_mlnx_fw/QTM3/rel-35_2016_3068/dev/fw-QTM3-rel-35_2016_3068.mfa)
        """
        fw_version_underscores = fw_version.replace('.', '_')
        return (f"{cls.FW_RELEASE_PATH}/{chip}/rel-{fw_version_underscores}/"
                f"{image_type}/fw-{chip}-rel-{fw_version_underscores}.mfa")

    @classmethod
    def resolve_fw_file_path(cls, chip: str, fw_version: str,
                             image_type: ImageType = 'dev') -> str | None:
        """
        Return path to FW file that actually exists.
        For dev image_type, tries dev path first, then prod if dev is missing
        (older releases often have only prod FW published).

        Args:
            chip: Chip type (e.g., 'QTM3', 'QTM4')
            fw_version: FW version with dots (e.g., '35.2016.3068')
            image_type: Preferred 'dev' or 'prod'

        Returns:
            Full path to existing FW file, or None if neither exists.
        """
        path = cls.build_fw_file_path(chip, fw_version, image_type)
        if Path(path).exists():
            return path
        if image_type == 'dev':
            path = cls.build_fw_file_path(chip, fw_version, 'prod')
            if Path(path).exists():
                return path
        return None

    def _parse_target_version(self, target_version: str) -> tuple[str, ImageType]:
        """Parse version and image type from path or version string."""
        if '/' in target_version:
            return self.parse_version_from_path(target_version)
        return target_version, 'dev'

    def _prepare_tag_search(self, target_version: str, asic_type: str | None,
                            fetch: bool = True) -> TagSearchContext:
        """
        Prepare context for tag-based FW search.

        Args:
            target_version: Target NVOS version path or version string.
            asic_type: ASIC type ('QTM4', 'QTM3'). If None, tries all.
            fetch: Whether to fetch tags from remote (default True).

        Returns:
            TagSearchContext with all necessary info for searching.

        Raises:
            ValueError: If setup fails (no branch prefix, no tags, no target FW).
        """
        version, image_type = self._parse_target_version(target_version)

        # Fetch tags before searching so branch prefix lookup has up-to-date data
        if fetch:
            self.fetch_tags()

        # Find branch prefix
        branch_prefix = self.find_branch_prefix_for_version(version)
        if not branch_prefix:
            raise ValueError(f"Could not determine branch prefix for: {version}")

        logger.info(f"Target: {version} ({image_type}), branch: {branch_prefix}")

        # Get tags for branch, narrowed by version major.minor to avoid unrelated tags
        # (e.g., 'master' prefix has 900+ tags including non-version ones)
        version_major_minor = '.'.join(version.split('.')[:2])
        tags = self.list_tags(f"{branch_prefix}_{version_major_minor}.*", sort_by_version=True)
        if not tags:
            tags = self.list_tags(f"{branch_prefix}_*", sort_by_version=True)
        if not tags:
            raise ValueError(f"No tags found for {branch_prefix}")

        # Get target FW version
        target_tag = f"{branch_prefix}_{version}"
        target_fw = self.get_fw_version_from_tag(target_tag, asic_type)

        # Check if target version exists in tags
        tag_versions = [self.VERSION_TAG_PATTERN.search(t).group(1) for t in tags
                        if self.VERSION_TAG_PATTERN.search(t)]
        target_exists_in_tags = version in tag_versions

        if not target_fw:
            # Target tag missing - fall back to the closest predecessor tag (highest tag
            # whose version <= target). Tags are sorted descending, so the first tag
            # with version <= target is the closest predecessor. This avoids picking a
            # tag NEWER than target when target is mid-sequence (and protects against
            # stale-repo cases where tags[0] was taken on faith before).
            target_tuple = self._version_tuple(version)
            proxy_tag = next(
                (t for t in tags
                 if (m := self.VERSION_TAG_PATTERN.search(t)) and
                 self._version_tuple(m.group(1)) <= target_tuple),
                None,
            )
            if proxy_tag:
                target_fw = self.get_fw_version_from_tag(proxy_tag, asic_type)
                logger.warning(
                    "Target version %s is not tagged; using FW from closest predecessor "
                    "tag %s as proxy. If shared repo is stale, refresh with "
                    "'git -C %s fetch --tags' to improve accuracy.",
                    version, proxy_tag, self.repo_path,
                )
        if not target_fw:
            raise ValueError(f"Could not determine target FW version for {asic_type} at tag {target_tag}")

        logger.info(f"Target FW: {target_fw}")

        if not target_exists_in_tags:
            logger.info(f"Target version {version} not found in tags, assuming it's newer than latest tag")

        return TagSearchContext(
            version=version,
            image_type=image_type,
            branch_prefix=branch_prefix,
            tags=tags,
            target_fw=target_fw,
            target_exists_in_tags=target_exists_in_tags,
        )

    def _iterate_tags_for_different_fw(self, ctx: TagSearchContext,
                                       asic_type: str | None) -> Iterator[tuple[str, str, str]]:
        """
        Iterate through tags yielding versions with different FW than target.

        Args:
            ctx: TagSearchContext from _prepare_tag_search.
            asic_type: ASIC type for FW lookup.

        Yields:
            Tuples of (tag_version, tag_fw, base_version) for each candidate.
        """
        found_target = ctx.target_exists_in_tags is False  # Start immediately if target not in tags
        target_tuple = self._version_tuple(ctx.version) if not ctx.target_exists_in_tags else None

        for tag in ctx.tags:
            if not (tag_match := self.VERSION_TAG_PATTERN.search(tag)):
                continue

            tag_version = tag_match.group(1)
            base_ver = tag_version.split('-')[0] if '-' in tag_version else tag_version

            if ctx.target_exists_in_tags:
                # Skip until we find the target version (tags are sorted descending)
                if tag_version == ctx.version:
                    found_target = True
                    continue
                # Only consider versions after we've passed the target
                if not found_target:
                    continue
            else:
                # Target is untagged - skip tags newer than target so we never return
                # a "previous" version that is actually NEWER than the target.
                if self._version_tuple(tag_version) > target_tuple:
                    continue

            tag_fw = self.get_fw_version_from_tag(tag, asic_type)
            if tag_fw and tag_fw != ctx.target_fw:
                yield tag_version, tag_fw, base_ver

    def find_previous_fw_version(self, target_version: str, asic_type: str | None = None,
                                 validate_fw_exists: bool = False) -> tuple[str, str, str | None]:
        """
        Find previous NVOS version with a different firmware version.
        Useful for upgrade/downgrade testing.

        Args:
            target_version: Target NVOS version path or version string.
            asic_type: ASIC type ('QTM4', 'QTM3'). If None, tries all.
            validate_fw_exists: If True, only return FW versions where the file exists.
                               Default False - validation can cause false failures and adds overhead.

        Returns:
            Tuple of (nvos_version, fw_version, fw_file_path). fw_file_path is the
            resolved path to the .mfa file when validate_fw_exists and asic_type were
            used and the file was found; otherwise None.

        Raises:
            ValueError: If no previous version with different FW is found.

        Example:
            >>> nvos_ver, fw_ver, fw_path = git_tool.find_previous_fw_version(
            ...     "/auto/sw_system_release/nos/nvos/25.03.0209-004/amd64/dev/nvos-amd64-25.03.0209-004.bin"
            ... )
            >>> print(f"Previous NVOS: {nvos_ver}, FW: {fw_ver}, path: {fw_path}")
        """
        ctx = self._prepare_tag_search(target_version, asic_type)

        skipped = 0
        for tag_version, tag_fw, _ in self._iterate_tags_for_different_fw(ctx, asic_type):
            # Validate that the FW file exists if requested (try dev then prod for dev image_type)
            fw_path = None
            if validate_fw_exists and asic_type:
                fw_path = self.resolve_fw_file_path(asic_type, tag_fw, ctx.image_type)
                if not fw_path:
                    skipped += 1
                    logger.debug(
                        "Version %s has different FW (%s) but no FW file exists (tried %s, then prod), skipping",
                        tag_version, tag_fw, ctx.image_type)
                    continue
            logger.info(f"Found previous FW: {tag_fw} (from version {tag_version})")
            return tag_version, tag_fw, fw_path

        if skipped:
            logger.info(
                "Searched %d version(s) with different FW; no FW file found (tried %s, then prod) for any.",
                skipped, ctx.image_type)
        raise ValueError(f"Could not find previous version with different FW for {ctx.version}")

    def find_previous_fw_image_path(self, target_version: str, asic_type: str = 'QTM3') -> str | None:
        """
        Find NVOS image path with previous FW version for ISSU Last_FW testing.
        Returns path to NVOS image with different (older) FW, or None.

        This method validates that the NVOS image file exists before returning.

        Args:
            target_version: Target NVOS version path or version string.
            asic_type: ASIC type ('QTM4', 'QTM3'). Defaults to 'QTM3'.

        Returns:
            Full path to NVOS image with different FW, or None if not found.

        Example:
            >>> git_tool = NvosGitTool()
            >>> path = git_tool.find_previous_fw_image_path(
            ...     "/auto/sw_system_release/nos/nvos/25.02.6931-019/amd64/prod/nvos-amd64-25.02.6931-019.bin"
            ... )
            >>> print(path)
            '/auto/sw_system_release/nos/nvos/25.02.6931-012/amd64/prod/nvos-amd64-25.02.6931-012.bin'
        """
        try:
            ctx = self._prepare_tag_search(target_version, asic_type)
            logger.info(f"Finding previous FW image for: {ctx.version} ({ctx.image_type})")
            logger.info(f"Found {len(ctx.tags)} matching tags")

            last_base_ver = None
            last_base_fw = None

            for tag_version, tag_fw, base_ver in self._iterate_tags_for_different_fw(ctx, asic_type):
                # For prod images, check if we've moved to a new base version with different FW
                if ctx.image_type == 'prod' and last_base_ver and base_ver != last_base_ver:
                    base_path = self.build_image_path(last_base_ver, ctx.image_type)
                    if Path(base_path).exists():
                        logger.info(f"Found {ctx.image_type} {last_base_ver} (base) with FW: {last_base_fw}")
                        return base_path

                # Check if image exists for this version
                path = self.build_image_path(tag_version, ctx.image_type)
                if Path(path).exists():
                    logger.info(f"Found {ctx.image_type} {tag_version} with FW: {tag_fw}")
                    return path

                logger.debug(
                    "Version %s has different FW (%s) but no %s image, searching...",
                    tag_version, tag_fw, ctx.image_type)
                last_base_ver = base_ver
                last_base_fw = tag_fw

            logger.warning(f"Could not find previous version with different FW for {ctx.version}")
            return None

        except Exception as e:
            logger.error(f"Error finding previous FW image: {e}")
            return None

    def find_previous_image_path(self, target_version: str, min_version: str | None = None) -> str | None:
        """
        Find the newest NVOS image on the target's branch that is older than the target.
        Useful when the regression base image is too old to include a feature under test.

        This method validates that the NVOS image file exists before returning.

        Args:
            target_version: Target NVOS version path or version string.
            min_version: Oldest acceptable NVOS version (e.g., the first build that has the feature).

        Returns:
            Full path to the NVOS image, or None if no build on the branch qualifies.

        Example:
            >>> git_tool = NvosGitTool()
            >>> path = git_tool.find_previous_image_path(
            ...     "/auto/sw_system_release/nos/nvos/lastrc_nvos-25-03-2000/nvos-amd64-25.03.1950-005.bin",
            ...     min_version="25.03.1950-000"
            ... )
            >>> print(path)
            '/auto/sw_system_release/nos/nvos/25.03.1950-004/amd64/dev/nvos-amd64-25.03.1950-004.bin'
        """
        try:
            version, image_type = self._parse_target_version(target_version)
            self.fetch_tags()

            branch_prefix = self.find_branch_prefix_for_version(version)
            if not branch_prefix:
                logger.warning(f"Could not determine branch prefix for: {version}")
                return None

            version_major_minor = '.'.join(version.split('.')[:2])
            tags = (self.list_tags(f"{branch_prefix}_{version_major_minor}.*", sort_by_version=False) or
                    self.list_tags(f"{branch_prefix}_*", sort_by_version=False))
            tag_versions = {match.group(1) for tag in tags if (match := self.VERSION_TAG_PATTERN.search(tag))}

            target_tuple = self._version_tuple(version)
            min_tuple = self._version_tuple(min_version) if min_version else ()
            for tag_version in sorted(tag_versions, key=self._version_tuple, reverse=True):
                if not min_tuple <= self._version_tuple(tag_version) < target_tuple:
                    continue
                path = self.build_image_path(tag_version, image_type)
                if Path(path).exists():
                    logger.info(f"Found previous {image_type} image on {branch_prefix}: {path}")
                    return path

            logger.warning(f"No {image_type} image on {branch_prefix} is older than {version} "
                           f"(min version: {min_version})")
            return None

        except Exception as e:
            logger.error(f"Error finding previous image: {e}")
            return None
