import re
from typing import Callable

from devts.infra.tools.connection_tools.proxy_ssh_engine import ProxySshEngine
from ngts.nvos_tools.infra import Fae
from ngts.tools.test_utils import allure_utils as allure

UphyVersionDictT = dict[str, str]
UphyVersionCmdT = Callable[..., UphyVersionDictT]

UPHY_VERSION_FIELD = "uphy-version"
CHECK_VERSION_TEMPLATE_ONLY = False
UPHY_VERSION_REGEX = r"^\d+\.\d+\.\d+$" if CHECK_VERSION_TEMPLATE_ONLY else r"^[78]\.\d+\.\d+$"
SYSFS_UPHY_VERSION_PATH = "{root}/sx_core/asic{asic_index}/local_parameters/uphy_version"
DUT_SYSFS_ROOT = "/sys/module"
TECHSUPPORT_SYSFS_ROOT = "{tech_support_dir}/sdk_sysfs"


def fae_uphy_cmd(asic_numbers: list[str]) -> UphyVersionDictT:
    fae = Fae.Fae()
    return {asic: fae.platform.firmware.parse_show(op_param=asic)[UPHY_VERSION_FIELD] for asic in asic_numbers}


def sysfs_uphy_cmd(asic_numbers: list[str], engine: ProxySshEngine,
                   root: str = DUT_SYSFS_ROOT) -> UphyVersionDictT:
    paths = {asic: SYSFS_UPHY_VERSION_PATH.format(root=root, asic_index=asic_index)
             for asic_index, asic in enumerate(asic_numbers)}
    output = engine.run_cmd("; ".join(f'echo "{asic}:$(sudo cat {path} 2>/dev/null)"' for asic, path in paths.items()))
    lines = (line.partition(":") for line in output.splitlines())
    versions = {asic: version.strip() for asic, _, version in lines if asic in paths}
    assert all(versions.get(asic) for asic in asic_numbers), f"Missing uphy version in {paths}, output:\n{output}"
    return versions


def techsupport_uphy_cmd(asic_numbers: list[str], engine: ProxySshEngine,
                         tech_support_dir: str) -> UphyVersionDictT:
    return sysfs_uphy_cmd(asic_numbers, engine,
                          root=TECHSUPPORT_SYSFS_ROOT.format(tech_support_dir=tech_support_dir))


def verify_uphy_versions(versions: UphyVersionDictT, source: str = "") -> str:
    assert versions, f"No uphy version from {source}"
    for asic, version in versions.items():
        assert re.match(UPHY_VERSION_REGEX, version), \
            f"{source} {asic} uphy version {version} does not match {UPHY_VERSION_REGEX}"
    distinct = set(versions.values())
    assert len(distinct) == 1, f"{source} asics report different uphy versions: {versions}"
    return distinct.pop()


def verify_uphy_cmd(cmd: UphyVersionCmdT, *args, **kwargs) -> str:
    with allure.step(f"Get and verify uphy version from {cmd.__name__}"):
        return verify_uphy_versions(cmd(*args, **kwargs), cmd.__name__)


def verify_uphy_cmds(*cmds_with_args: tuple) -> str:
    sources = [cmd.__name__ for cmd, *_ in cmds_with_args]
    with allure.step(f"Get and verify uphy version matches between {sources}"):
        versions = {cmd.__name__: verify_uphy_cmd(cmd, *args) for cmd, *args in cmds_with_args}
        distinct = set(versions.values())
        assert len(distinct) == 1, f"uphy version differs between sources: {versions}"
        return distinct.pop()
