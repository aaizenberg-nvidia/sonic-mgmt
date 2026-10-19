import logging
import os
import re

logger = logging.getLogger(__name__)

NFS_SONIC_RELEASE_ROOT = "/auto/sw_system_release/sonic"
NFS_SONIC_CI_BUILD_ROOT = f"{NFS_SONIC_RELEASE_ROOT}/sonic_build_tmp"

# sonic_ci images built against a tag append ".0-<sha>_Internal" to the tag image name.
# Example: master_RC.338-2eb4372559_Internal.0-2eb4372559_Internal
# Classic CI images like master_RC.0-<sha>_Internal must not match this (no parent tag folder).
_BASE_TAG_CI_IMAGE_RE = re.compile(
    r"^(?P<base>.+\.\d+-[0-9a-fA-F]+_Internal)\.0-[0-9a-fA-F]+_Internal$"
)

# Every sonic_ci image name ends with ".0-<sha>_Internal", with or without a base tag.
_CI_IMAGE_RE = re.compile(r"\.0-[0-9a-fA-F]+_Internal$")

# CI jobs export LOG_FOLDER inside the build's NFS folder, for example
# /auto/sw_system_release/sonic/sonic_build_tmp/sonic_ci/5416/LOGS/<setup>_log_folder,
# and keep the README of the image they built in sonic/tmp_<job>_<build>/README.
_CI_BUILD_LOG_FOLDER_RE = re.compile(r"/sonic_build_tmp/(?P<job>[^/]+)/(?P<build>\d+)(?:/|$)")


def _image_directory(sonic_image):
    if sonic_image.startswith("SONiC."):
        return sonic_image[len("SONiC."):]
    return sonic_image


def _readme_path_for_directory(image_directory):
    return os.path.realpath(f"{NFS_SONIC_RELEASE_ROOT}/{image_directory}/dev/README")


def _ci_build_readme_path():
    match = _CI_BUILD_LOG_FOLDER_RE.search(os.environ.get("LOG_FOLDER", ""))
    if not match:
        return None
    job, build = match.group("job"), match.group("build")
    return f"{NFS_SONIC_CI_BUILD_ROOT}/{job}/{build}/sonic/tmp_{job}_{build}/README"


def resolve_sonic_image_readme_path(sonic_image, ci_build_fallback=False):
    """Return the NFS README path for a SONiC image, or None if it is not published.

    Tag-based sonic_ci images do not get their own NFS folder. Fall back to the
    README of the tag image the CI build is based on when that suffix is present.

    With ci_build_fallback, a CI image that still has no README falls back to the
    switch README in the build folder of the running CI job, found through LOG_FOLDER.
    That README has no DPU fields, so DPU callers must not enable it.
    """
    image_dir = _image_directory(sonic_image)
    candidates = [image_dir]
    match = _BASE_TAG_CI_IMAGE_RE.match(image_dir)
    if match:
        candidates.append(match.group("base"))

    for directory in candidates:
        readme_path = _readme_path_for_directory(directory)
        if os.path.exists(readme_path):
            if directory != image_dir:
                logger.info(
                    "README for CI image '%s' is not published; using base image README: %s",
                    image_dir, readme_path,
                )
            return readme_path

    if ci_build_fallback and _CI_IMAGE_RE.search(image_dir):
        readme_path = _ci_build_readme_path()
        if readme_path and os.path.exists(readme_path):
            logger.info(
                "README for CI image '%s' is not published; using CI build README: %s",
                image_dir, readme_path,
            )
            return readme_path
    return None
