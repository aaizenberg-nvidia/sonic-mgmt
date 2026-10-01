import logging
import os
import re

logger = logging.getLogger(__name__)

NFS_SONIC_RELEASE_ROOT = "/auto/sw_system_release/sonic"

# sonic_ci images built against a tag append ".0-<sha>_Internal" to the tag image name.
# Example: master_RC.338-2eb4372559_Internal.0-2eb4372559_Internal
# Classic CI images like master_RC.0-<sha>_Internal must not match this (no parent tag folder).
_BASE_TAG_CI_IMAGE_RE = re.compile(
    r"^(?P<base>.+\.\d+-[0-9a-fA-F]+_Internal)\.0-[0-9a-fA-F]+_Internal$"
)


def _image_directory(sonic_image):
    if sonic_image.startswith("SONiC."):
        return sonic_image[len("SONiC."):]
    return sonic_image


def _readme_path_for_directory(image_directory):
    return os.path.realpath(f"{NFS_SONIC_RELEASE_ROOT}/{image_directory}/dev/README")


def resolve_sonic_image_readme_path(sonic_image):
    """Return the NFS README path for a SONiC image, or None if it is not published.

    Tag-based sonic_ci images do not get their own NFS folder. Fall back to the
    README of the tag image the CI build is based on when that suffix is present.
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
    return None
