"""
Tool for installing, running and cleaning up stress-ng on Cumulus/DUT.
Used by system health and performance stress tests.
"""

import logging

from ngts.tools.test_utils import allure_utils as allure

logger = logging.getLogger()


class StressNgTool:
    """Install and cleanup stress-ng package on DUT (e.g. Cumulus)."""

    SOURCES_LIST = "/etc/apt/sources.list"
    # stress-ng is not in the Cumulus/NVIDIA repos; it lives in Debian bookworm main. Cumulus is
    # Debian 12 (bookworm) based, so these repos are added (only if missing) to make it installable.
    DEBIAN_REPO_LINES = [
        "deb http://deb.debian.org/debian bookworm main",
        "deb http://security.debian.org/debian-security bookworm-security main",
        "deb http://deb.debian.org/debian bookworm-updates main",
        "deb http://deb.debian.org/debian bookworm-backports main",
    ]
    # Repo lines actually appended by the last install_stress_ng call, so cleanup removes only what
    # it added and leaves pre-existing entries untouched.
    _added_repo_lines: list = []

    @staticmethod
    def is_stress_ng_available(engines_dut) -> bool:
        """Return True if the stress-ng binary is present on the DUT PATH."""
        return "/stress-ng" in (engines_dut.run_cmd("command -v stress-ng", validate=False) or "")

    @staticmethod
    def _is_repo_line_present(engines_dut, line) -> bool:
        """Return True if the exact repo line already exists in sources.list."""
        output = engines_dut.run_cmd(
            f"grep -qxF '{line}' {StressNgTool.SOURCES_LIST} && echo PRESENT || echo ABSENT",
            validate=False,
        )
        return "PRESENT" in (output or "")

    @staticmethod
    def install_stress_ng(engines_dut, install_bc: bool = False) -> None:
        """
        Install stress-ng (and optionally bc) and enable Debian repositories if needed.

        Existing behavior is retained: the four sed commands still un-comment any pre-existing
        commented deb.debian.org entries (kept for builds that ship them commented out). On top of
        that, each bookworm repo line is appended only if it is not already present, and the lines
        actually added are tracked so cleanup can remove exactly those.

        run_cmd does not check exit codes, so a failed 'apt-get install stress-ng' would otherwise
        be silently swallowed and only surface much later (e.g. as an unrelated assertion in the
        stress test, or as a false pass driven purely by the tmpfs allocation). We therefore verify
        the binary is actually present after installing and fail fast with a clear message if not.

        :param engines_dut: Engine for the DUT (e.g. engines.dut).
        :param install_bc: If True, also install the bc package.
        :raises AssertionError: if stress-ng is still not available after the install attempt.
        """
        with allure.step("Install stress-ng package"):
            engines_dut.run_cmd(
                "sudo sed -i 's/#deb     http:\\/\\/deb.debian.org\\/debian/deb     http:\\/\\/deb.debian.org\\/debian/g' /etc/apt/sources.list",
                timeout=30,
            )
            engines_dut.run_cmd(
                "sudo sed -i 's/#deb-src http:\\/\\/deb.debian.org\\/debian/deb-src http:\\/\\/deb.debian.org\\/debian/g' /etc/apt/sources.list",
                timeout=30,
            )
            engines_dut.run_cmd(
                "sudo sed -i 's/# deb     http:\\/\\/deb.debian.org\\/debian/deb     http:\\/\\/deb.debian.org\\/debian/g' /etc/apt/sources.list",
                timeout=30,
            )
            engines_dut.run_cmd(
                "sudo sed -i 's/# deb-src http:\\/\\/deb.debian.org\\/debian/deb-src http:\\/\\/deb.debian.org\\/debian/g' /etc/apt/sources.list",
                timeout=30,
            )
            # Add the bookworm repos only if the file doesn't already have them, remembering which
            # lines we appended so cleanup can remove exactly those.
            added_lines = []
            for line in StressNgTool.DEBIAN_REPO_LINES:
                if not StressNgTool._is_repo_line_present(engines_dut, line):
                    engines_dut.run_cmd(
                        f"echo '{line}' | sudo tee -a {StressNgTool.SOURCES_LIST} >/dev/null",
                        timeout=30,
                    )
                    added_lines.append(line)
            StressNgTool._added_repo_lines = added_lines

            engines_dut.run_cmd("sudo apt-get update -y --force-yes", timeout=300)
            engines_dut.run_cmd("sudo apt-get install -y --force-yes stress-ng", timeout=300)
            if install_bc:
                engines_dut.run_cmd("sudo apt-get install -y bc", timeout=300)
            assert StressNgTool.is_stress_ng_available(engines_dut), (
                "stress-ng is not available after 'apt-get install stress-ng'; the package could "
                "not be installed from the enabled repos (verify the DUT can reach the Debian "
                "bookworm mirror)"
            )

    @staticmethod
    def cleanup_stress_ng(engines_dut, remove_bc: bool = False) -> None:
        """
        Stop stress-ng, remove installed packages and restore Debian repositories.

        Existing behavior is retained (re-comment the deb.debian.org entries). In addition, any repo
        lines that install_stress_ng appended (and only those) are removed, so lines that were
        already present before the test are left untouched.

        :param engines_dut: Engine for the DUT (e.g. engines.dut).
        :param remove_bc: If True, also remove the bc package.
        """
        with allure.step("Clean up stress-ng"):
            engines_dut.run_cmd("sudo killall stress-ng", timeout=300)
            engines_dut.run_cmd("sudo apt-get remove -y --force-yes stress-ng", timeout=300)
            if remove_bc:
                engines_dut.run_cmd("sudo apt-get remove -y bc", timeout=300)
            engines_dut.run_cmd(
                "sudo sed -i 's/deb     http:\\/\\/deb.debian.org\\/debian/#deb     http:\\/\\/deb.debian.org\\/debian/g' /etc/apt/sources.list",
                timeout=30,
            )
            engines_dut.run_cmd(
                "sudo sed -i 's/deb-src http:\\/\\/deb.debian.org\\/debian/#deb-src http:\\/\\/deb.debian.org\\/debian/g' /etc/apt/sources.list",
                timeout=30,
            )
            # Remove only the repo lines this tool appended during install.
            for line in StressNgTool._added_repo_lines:
                # '|' delimiter avoids escaping the '/' in the URLs; '.' is escaped so it matches
                # literally, and ^...$ anchors the whole line so only our exact entries are removed.
                escaped = line.replace(".", r"\.")
                engines_dut.run_cmd(
                    f"sudo sed -i '\\|^{escaped}$|d' {StressNgTool.SOURCES_LIST}",
                    timeout=30,
                )
            StressNgTool._added_repo_lines = []
