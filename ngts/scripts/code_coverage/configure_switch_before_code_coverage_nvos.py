import allure
import pytest

from ngts.ngts_types import EnginesT

# Host services that run as systemd daemons on the NVOS host (not inside dockers)
# and whose Python code is in the coverage scope (src/sonic-host-services/**/*).
# Each service reads its environment from /etc/default/<service>, so coverage
# env vars must be written there before the service starts.
HOST_SERVICES_FOR_COVERAGE = [
    'nvued',
    'caclmgrd',
    'configmgrd',
    'countermgrd',
    'featured',
    'health-statsd',
    'hostcfgd',
    'mgmtportsyncd',
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

    except Exception as err:
        raise AssertionError(err)
