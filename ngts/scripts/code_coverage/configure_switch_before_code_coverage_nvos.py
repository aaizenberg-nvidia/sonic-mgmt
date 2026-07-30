import allure
import pytest

from ngts.ngts_types import EnginesT

# Host services that run as systemd daemons on the NVOS host (not inside dockers)
# and whose Python code is in the coverage scope (src/sonic-host-services/**/*).
# Each service reads its environment from /etc/default/<service>, so coverage
# env vars must be written there before the service starts.
HOST_SERVICES_FOR_COVERAGE = [
    'nvued',
    'acltool',
    'configmgrd',
    'countermgrd',
    'featured',
    'health-statsd',
    'hostcfgd',
    'mgmtportsyncd',
    'portsyncmgrd',
]

# Services whose unit file has no EnvironmentFile= directive, so a systemd
# drop-in must be created to make them load /etc/default/<service>.
SERVICES_NEEDING_ENVIRONMENT_FILE_DROPIN = [
    'acltool',
    'configmgrd',
    'countermgrd',
    'featured',
    'health-statsd',
    'hostcfgd',
    'portsyncmgrd',
]


@pytest.mark.disable_loganalyzer
@allure.title('Configure switch before code coverage')
def test_configure_switch_before_code_coverage(engines: EnginesT):
    try:
        with allure.step('Configure switch before code coverage'):
            engines.dut.run_cmd('sudo chmod 777 /sonic/src/sonic-swss-common/common/.libs/*')

            for service in HOST_SERVICES_FOR_COVERAGE:
                with allure.step(f'Configure coverage for {service}'):
                    engines.dut.run_cmd(f'sudo touch /etc/default/{service}')
                    engines.dut.run_cmd(f'sudo chmod 777 /etc/default/{service}')
                    engines.dut.run_cmd(
                        f'echo "COVERAGE_PROCESS_START=/etc/python3/coverage_config" >> /etc/default/{service}')
                    engines.dut.run_cmd(
                        f'echo "COVERAGE_RCFILE=/etc/python3/coverage_config" >> /etc/default/{service}')
                    engines.dut.run_cmd(
                        f'echo "COVERAGE_FILE=/var/lib/python/coverage/raw" >> /etc/default/{service}')

                    if service in SERVICES_NEEDING_ENVIRONMENT_FILE_DROPIN:
                        engines.dut.run_cmd(
                            f'sudo mkdir -p /etc/systemd/system/{service}.service.d')
                        engines.dut.run_cmd(
                            f'sudo bash -c \'printf "[Service]\\nEnvironmentFile=/etc/default/{service}\\n"'
                            f' > /etc/systemd/system/{service}.service.d/coverage.conf\'')

            engines.dut.run_cmd('sudo systemctl daemon-reload')

            with allure.step('Add non-standard install paths to coverage config'):
                # sonic-host-services installs its package (acltool) and scripts
                # (hostcfgd, configmgrd, etc.) under /usr/local/ instead of the
                # standard /usr/lib/. Without explicit source entries, coverage.py
                # ignores these paths. Only added if not already present (idempotent).
                add_usr_local_paths_to_coverage_config(engines, 'acltool')

    except Exception as err:
        raise AssertionError(err)


def add_usr_local_paths_to_coverage_config(engines: EnginesT, service: str):
    """Add /usr/local source paths to coverage config for sonic-host-services.

    Adds two paths:
    - /usr/local/lib/<pyver>/dist-packages/<service>  : the service's Python package
    - /usr/local/bin                                   : all host service scripts
    """
    engines.dut.run_cmd(
        f"grep -q '{service}' /etc/python3/coverage_config || "
        "sudo python3 -c '"
        "import sys; "
        "ver = f\"python{sys.version_info.major}.{sys.version_info.minor}\"; "
        f"pkg_path = f\"/usr/local/lib/{{ver}}/dist-packages/{service}\"; "
        "cfg = open(\"/etc/python3/coverage_config\").read(); "
        f"src = f\"source =\\n    {{pkg_path}}\\n    /usr/local/bin\\n\\n\"; "
        "open(\"/etc/python3/coverage_config\", \"w\").write(cfg.replace(\"[report]\", src + \"[report]\"))"
        "'"
    )