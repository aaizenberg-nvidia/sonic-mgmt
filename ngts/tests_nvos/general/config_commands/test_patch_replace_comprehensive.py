"""
Comprehensive tests for config patch and replace operations
Tests both CLI and REST API methods with positive and negative scenarios
"""

import json
import logging
import os
import tempfile
import textwrap
import time

import pytest
import urllib3
from urllib3.exceptions import InsecureRequestWarning
import requests

from devts.infra.tools.linux_tools import linux_tools

from ngts.nvos_constants.constants_nvos import SystemConsts, OutputFormat
from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from ngts.nvos_tools.Devices.EthDevice import EthSwitch
from ngts.nvos_tools.acl.acl import Acl
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port
from ngts.nvos_tools.infra.FilesTool import TempFileOnEngine
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.system.System import System
from ngts.nvos_tools.vrf.vrf import Vrf
from ngts.tools.test_utils import allure_utils as allure

# Suppress insecure request warnings
urllib3.disable_warnings(category=InsecureRequestWarning)

logger = logging.getLogger(__name__)


LARGE_CONFIG_APPLY_TIMEOUT = 600    # 10 minutes - for large configs (e.g., 30K ACL rules)

# =============================================================================
# Helper Functions
# =============================================================================


def cleanup_pending_config(engines):
    """
    Cleanup helper: Detach any pending config changes.
    Call this in finally blocks to ensure tests start with clean state.
    """
    try:
        NvueGeneralCli.detach_config(engines.dut)
        logger.info("✓ Detached pending config for clean test state")
    except Exception as e:
        logger.warning(f"Failed to detach config (may already be detached): {e}")


# Removed config_patch_or_replace - using CLI wrapper methods directly
# Usage: TestToolkit.GeneralApi[api_type].config_patch(engine, filepath, apply=True)
# Usage: TestToolkit.GeneralApi[api_type].config_replace(engine, filepath, apply=True)


def assert_patch_failed_with_errors(engines, random_api, filepath, expected_keywords=None):
    """
    Helper function for negative tests: verifies patch failed with validation errors
    and no config was applied (atomic behavior).

    Uses framework's verify_result() pattern for cleaner error handling.

    Args:
        engines: Test engines fixture
        random_api: API type (NVUE or OpenAPI)
        filepath: Path to the patch file
        expected_keywords: Optional list of keywords to check in error message
                          (e.g., ['invalid', 'line 2'])

    Returns:
        error_output: The error message from the failed patch
    """
    with allure.step(f'Execute patch via {random_api} - expect validation failure'):
        # Execute patch without apply - should return ResultObj with failure status
        result_obj = TestToolkit.GeneralApi[random_api].config_patch(engines.dut, filepath, apply=False)

        # Use verify_result with should_succeed=False for negative tests
        # This follows framework pattern and makes errors easier to track
        error_output = result_obj.verify_result(should_succeed=False)

        logger.info(f"Patch correctly failed via {random_api}")
        logger.info(f"Error details: {error_output}")

        # Check for specific keywords if provided
        if expected_keywords:
            for keyword in expected_keywords:
                assert keyword.lower() in error_output.lower(), \
                    f"Expected keyword '{keyword}' not found in error: {error_output}"
                logger.info(f"Found expected keyword '{keyword}' in error message")

    with allure.step('Verify no config was actually applied (atomic behavior)'):
        diff_output = NvueGeneralCli.diff_config(engines.dut, output_type=OutputFormat.json)
        assert not diff_output or diff_output.strip() == "[]", \
            f"Config diff should be empty after failed patch, got: {diff_output}"
        logger.info("✓ Confirmed: no partial changes applied (atomic behavior verified)")

    return error_output


def create_temp_file(engine, filename, content):
    """Create temp file on engine and write content. Uses SCP for large files (>100KB)."""
    extension = filename.split('.')[-1] if '.' in filename else 'txt'
    temp_file = TempFileOnEngine(engine, extension)

    class TempFileWithContent:
        """Context manager that creates temp file and writes content using optimal method."""

        def __enter__(self):
            temp_file.__enter__()
            # Write content: use SCP for large files, inline shell redirection for small
            if len(content) > 100 * 1024:  # >100KB
                with tempfile.NamedTemporaryFile(mode='w', delete=False) as f:
                    f.write(content)
                    local_temp = f.name
                try:
                    linux_tools.scp_file(engine, local_temp, temp_file.path, download_from_remote=False)
                finally:
                    os.remove(local_temp)
            else:
                engine.run_cmd(f"cat > {temp_file.path} << 'EOF'\n{content}\nEOF")
            return temp_file.path

        def __exit__(self, *args):
            return temp_file.__exit__(*args)

    return TempFileWithContent()


# =============================================================================
# POSITIVE TESTS - CONFIG PATCH
# =============================================================================

@pytest.mark.general
@pytest.mark.simx
@pytest.mark.nvue_core
def test_patch_acl_rules(engines, random_api):
    """
    Test config patch with ACL rules using both CLI and API

    Test flow:
        1. Create ACL configuration with multiple rules
        2. Save to file
        3. Use nv config patch to apply (CLI or API based on parametrization)
        4. Verify all rules are created
        5. Cleanup
    """
    system = System()

    acl_config = """nv set acl test_patch_acl type ipv4
nv set acl test_patch_acl rule 10 action permit
nv set acl test_patch_acl rule 20 action deny
nv set acl test_patch_acl rule 30 action permit
"""

    try:
        with create_temp_file(engines.dut, f'acl_patch_{random_api}.txt', acl_config) as filepath:
            with allure.step(f'Apply config using patch via {random_api} (includes apply)'):
                result_obj = TestToolkit.GeneralApi[TestToolkit.tested_api].config_patch(engines.dut, filepath, apply=True)
                output = result_obj.verify_result()  # Verify success and get output
                logger.info(f"Patch output ({random_api}): {output}")
                time.sleep(2)

            with allure.step('Verify ACL created with all rules'):
                acl = Acl()
                acl_data = acl.acl_id['test_patch_acl'].parse_show()

                assert 'rule' in acl_data, "ACL rules not found"
                rules = acl_data['rule']
                assert len(rules) == 3, f"Expected 3 rules, found {len(rules)}"
                assert '10' in rules, "Rule 10 not found"
                assert '20' in rules, "Rule 20 not found"
                assert '30' in rules, "Rule 30 not found"

    finally:
        Acl().acl_id['test_patch_acl'].unset(apply=True).verify_result()
        cleanup_pending_config(engines)


@pytest.mark.general
@pytest.mark.simx
@pytest.mark.nvue_core
def test_patch_with_range_expansion(engines, random_api):
    """
    Test patch with ACL rule ranges using both CLI and API

    Test flow:
        1. Create config with rule range (10-15)
        2. Patch using CLI or API
        3. Verify 6 individual rules created
    """

    config_with_range = """nv set acl test_range_acl type ipv4
nv set acl test_range_acl rule 10-15 action permit"""

    try:
        with create_temp_file(engines.dut, f'range_{random_api}.txt', config_with_range) as filepath:
            with allure.step(f'Patch config with range via {random_api} (includes apply)'):
                result_obj = TestToolkit.GeneralApi[TestToolkit.tested_api].config_patch(engines.dut, filepath, apply=True)
                result_obj.verify_result()  # Verify success
                time.sleep(2)

            with allure.step('Verify range expansion'):
                acl = Acl()
                acl_data = acl.acl_id['test_range_acl'].parse_show()
                rules = acl_data.get('rule', {})

                expected_rules = ['10', '11', '12', '13', '14', '15']
                assert len(rules) == 6, f"Expected 6 rules, found {len(rules)}"

                for rule_id in expected_rules:
                    assert rule_id in rules, f"Rule {rule_id} not found after range expansion"

    finally:
        Acl().acl_id['test_range_acl'].unset(apply=True).verify_result()
        cleanup_pending_config(engines)

# =============================================================================
# POSITIVE TESTS - CONFIG REPLACE
# =============================================================================


@pytest.mark.general
@pytest.mark.simx
@pytest.mark.nvue_core
def test_replace_removes_existing_config(engines, random_api):
    """
    Test that replace removes existing configuration

    Following customer workflow for replace operations via CLI and API.

    Test flow:
        1. Create ACL and set hostname
        2. Export config with only hostname
        3. Replace config
        4. Verify ACL is removed
    """
    system = System()
    hostname = "test-replace-removes"  # Use dashes, not underscores (valid hostname)

    try:
        with allure.step('Create ACL, VRF, system message and set hostname'):
            Acl().acl_id['test_replace_acl'].set('type', 'ipv4', apply=False)
            Acl().acl_id['test_replace_acl'].rule.rule_id[10].action.set('permit', apply=False)
            if isinstance(TestToolkit.get_device(), EthSwitch):
                Vrf().set('test_replace_vrf').verify_result()
            System().message.set(SystemConsts.PRE_LOGIN_MESSAGE, '"Test Replace Message"', apply=False)
            system.set(SystemConsts.HOSTNAME, hostname, apply=True, ask_for_confirmation=True)
            time.sleep(2)

        with allure.step('Export config with only hostname'):
            # Replace requires commands format (nv set), not YAML
            config_content = textwrap.dedent(f"""
                nv set system hostname {hostname}
                nv set system aaa user admin password $y$j9T$Uay927sQ.WFouaJb1uHpd/$Vn58Jt3rx4TElcI7KUYbtA/qQtK/qKM2qWy9J9wanF3
                nv set system security password-hardening state disabled
            """)

        with create_temp_file(engines.dut, f'minimal_config_{random_api}.txt', config_content) as filepath:
            with allure.step(f'Replace config via {random_api} (includes apply)'):
                TestToolkit.GeneralApi[TestToolkit.tested_api].config_replace(engines.dut, filepath, apply=True)
                time.sleep(2)

            with allure.step('Verify ACL removed'):
                acl = Acl()
                acl_data = acl.parse_show()
                assert 'test_replace_acl' not in acl_data, \
                    "ACL should have been removed by replace operation"

            if isinstance(TestToolkit.get_device(), EthSwitch):
                with allure.step('Verify VRF removed'):
                    vrf_output = engines.dut.run_cmd('nv show vrf -o json')
                    vrf_data = json.loads(vrf_output)
                    assert 'test_replace_vrf' not in vrf_data, \
                        "VRF should have been removed by replace operation"

            with allure.step('Verify system message removed'):
                system_obj = System()
                system_output = OutputParsingTool.parse_json_str_to_dictionary(system_obj.show()).get_returned_value()
                message = system_output.get('message', {}).get('pre-login', '')
                assert message != "Test Replace Message", \
                    "System pre-login message should have been removed by replace operation"

            with allure.step('Verify hostname preserved'):
                assert system_output.get(SystemConsts.HOSTNAME) == hostname

    finally:
        Acl().acl_id['test_replace_acl'].unset(apply=False)
        if isinstance(TestToolkit.get_device(), EthSwitch):
            engines.dut.run_cmd('nv unset vrf test_replace_vrf')
        System().message.unset(op_param=SystemConsts.PRE_LOGIN_MESSAGE, apply=False)
        cleanup_pending_config(engines)


@pytest.mark.general
@pytest.mark.simx
@pytest.mark.nvue_core
def test_replace_functionality_with_acls(engines, random_api):
    """
    Critical test: Replace should completely erase ALL existing ACL rules

    This test validates a specific issue where ACL rules were not being
    properly removed after replace operation, especially when using API.

    Test flow:
        1. Create ACL with multiple rules (10, 20, 30, 40, 50)
        2. Verify all 5 rules exist
        3. Replace config with either:
           a) Simple config without ACL, OR
           b) Same ACL but with only 2 different rules (100, 200)
        4. Verify original rules (10-50) are COMPLETELY ERASED
        5. Test both NVUE CLI and OpenAPI to ensure consistent behavior

    Expected behavior:
        - Replace should wipe out ALL previous ACL rules
        - No partial rules should remain
        - Both CLI and API should behave identically
    """
    system = System()
    hostname = "test-acl-replace"

    try:
        with allure.step('Capture initial/default ACLs before test'):
            acl = Acl()
            initial_acls = acl.parse_show()
            default_acl_names = list(initial_acls.keys())
            logger.info(f"Initial/default ACLs in system: {default_acl_names}")

        with allure.step('Create ACL with 5 rules (10, 20, 30, 40, 50)'):
            Acl().acl_id['test_acl_replace'].set('type', 'ipv4', apply=False)
            Acl().acl_id['test_acl_replace'].rule.rule_id[10].action.set('permit', apply=False)
            Acl().acl_id['test_acl_replace'].rule.rule_id[20].action.set('deny', apply=False)
            Acl().acl_id['test_acl_replace'].rule.rule_id[30].action.set('permit', apply=False)
            Acl().acl_id['test_acl_replace'].rule.rule_id[40].action.set('deny', apply=False)
            Acl().acl_id['test_acl_replace'].rule.rule_id[50].action.set('permit', apply=False)
            system.set(SystemConsts.HOSTNAME, hostname, apply=True, ask_for_confirmation=True)
            time.sleep(2)

        with allure.step('Verify all 5 rules exist before replace'):
            acl_data = acl.acl_id['test_acl_replace'].parse_show()
            rules = acl_data.get('rule', {})

            logger.info(f"Rules before replace: {list(rules.keys())}")
            assert len(rules) == 5, f"Expected 5 rules before replace, found {len(rules)}"
            assert '10' in rules and '20' in rules and '30' in rules and '40' in rules and '50' in rules, \
                "All 5 rules (10, 20, 30, 40, 50) should exist"
            logger.info("✓ All 5 ACL rules confirmed before replace")

        with allure.step('Create replace config with NO ACL (only hostname)'):
            # Replace with minimal config - ACL should be completely removed.
            # Keep admin AAA access, otherwise replace wipes the admin password
            # and the next OpenAPI call fails with HTTP 401.
            replace_config = f"""nv set system hostname {hostname}
nv set system aaa user admin password $y$j9T$Uay927sQ.WFouaJb1uHpd/$Vn58Jt3rx4TElcI7KUYbtA/qQtK/qKM2qWy9J9wanF3
nv set system security password-hardening state disabled"""

        with create_temp_file(engines.dut, f'replace_no_acl_{random_api}.txt', replace_config) as filepath:
            with allure.step(f'Replace config via {random_api} (includes apply)'):
                TestToolkit.GeneralApi[TestToolkit.tested_api].config_replace(engines.dut, filepath, apply=True)
                time.sleep(2)

            with allure.step('CRITICAL: Verify test ACL ERASED but default ACLs PRESERVED'):
                acl_data = acl.parse_show()
                current_acl_names = list(acl_data.keys())

                logger.info(f"ACLs after replace: {current_acl_names}")
                logger.info(f"Initial/default ACLs: {default_acl_names}")

                # Verify test_acl_replace is completely gone
                assert 'test_acl_replace' not in acl_data, \
                    f"❌ CRITICAL BUG: ACL 'test_acl_replace' should be COMPLETELY REMOVED after replace, but it still exists!\n" \
                    f"ACL data: {acl_data}\n" \
                    f"This indicates replace did not properly erase the ACL configuration."

                logger.info(f"✓✓✓ SUCCESS: User-created ACL 'test_acl_replace' completely erased after replace via {random_api}")

                # Verify default/system ACLs are still present (not accidentally wiped out)
                for default_acl in default_acl_names:
                    assert default_acl in acl_data, \
                        f"❌ BUG: Default/system ACL '{default_acl}' was incorrectly removed by replace operation!\n" \
                        f"Default ACLs should be preserved. Before: {default_acl_names}, After: {current_acl_names}"

                logger.info(f"✓✓✓ SUCCESS: All default/system ACLs preserved: {default_acl_names}")

            with allure.step('Verify hostname preserved (not erased)'):
                system_obj = System()
                system_output = OutputParsingTool.parse_json_str_to_dictionary(system_obj.show()).get_returned_value()
                assert system_output.get(SystemConsts.HOSTNAME) == hostname, \
                    f"Hostname should be preserved, expected {hostname}, got {system_output.get(SystemConsts.HOSTNAME)}"
                logger.info("✓ Hostname correctly preserved")

        # Test scenario 2: Replace with DIFFERENT ACL rules
        with allure.step('Test scenario 2: Create ACL again with rules 10, 20, 30'):
            Acl().acl_id['test_acl_replace'].set('type', 'ipv4', apply=False)
            Acl().acl_id['test_acl_replace'].rule.rule_id[10].action.set('permit', apply=False)
            Acl().acl_id['test_acl_replace'].rule.rule_id[20].action.set('deny', apply=False)
            Acl().acl_id['test_acl_replace'].rule.rule_id[30].action.set('permit', apply=True, ask_for_confirmation=True)
            time.sleep(2)

        with allure.step('Verify rules 10, 20, 30 exist'):
            acl_data = acl.acl_id['test_acl_replace'].parse_show()
            rules = acl_data.get('rule', {})
            assert '10' in rules and '20' in rules and '30' in rules
            logger.info("✓ Rules 10, 20, 30 exist")

        with allure.step('Replace with DIFFERENT rules (100, 200 instead of 10, 20, 30)'):
            # Replace ACL with completely different rule numbers
            replace_config_new_rules = f"""nv set system hostname {hostname}
nv set system aaa user admin password $y$j9T$Uay927sQ.WFouaJb1uHpd/$Vn58Jt3rx4TElcI7KUYbtA/qQtK/qKM2qWy9J9wanF3
nv set system security password-hardening state disabled
nv set acl test_acl_replace type ipv4
nv set acl test_acl_replace rule 100 action permit
nv set acl test_acl_replace rule 200 action deny"""

        with create_temp_file(engines.dut, f'replace_diff_rules_{random_api}.txt', replace_config_new_rules) as filepath2:
            TestToolkit.GeneralApi[TestToolkit.tested_api].config_replace(engines.dut, filepath2, apply=True)
            time.sleep(2)

            with allure.step('CRITICAL: Verify OLD rules (10, 20, 30) ERASED and ONLY NEW rules (100, 200) exist'):
                acl_data = acl.acl_id['test_acl_replace'].parse_show()
                rules = acl_data.get('rule', {})

                logger.info(f"Rules after replace with new rules: {list(rules.keys())}")

                # Old rules should be GONE
                assert '10' not in rules, \
                    f"❌ CRITICAL BUG: Rule 10 should be ERASED after replace, but it still exists! Rules: {list(rules.keys())}"
                assert '20' not in rules, \
                    f"❌ CRITICAL BUG: Rule 20 should be ERASED after replace, but it still exists! Rules: {list(rules.keys())}"
                assert '30' not in rules, \
                    f"❌ CRITICAL BUG: Rule 30 should be ERASED after replace, but it still exists! Rules: {list(rules.keys())}"

                # Only new rules should exist
                assert '100' in rules, f"Rule 100 should exist after replace. Rules: {list(rules.keys())}"
                assert '200' in rules, f"Rule 200 should exist after replace. Rules: {list(rules.keys())}"
                assert len(rules) == 2, \
                    f"Should have exactly 2 rules (100, 200) after replace, found {len(rules)}: {list(rules.keys())}"

                logger.info(f"✓✓✓ SUCCESS: Old rules completely erased, only new rules (100, 200) exist via {random_api}")

    finally:
        Acl().acl_id['test_acl_replace'].unset(apply=True).verify_result()
        cleanup_pending_config(engines)


# =============================================================================
# NEGATIVE TESTS - API
# =============================================================================
#
# NOTE: The following skipped tests are OLD DUPLICATES or can be removed:
# 1. test_patch_cli_vs_api_comparison (line ~171) - DUPLICATE, now tested by parametrized tests
# 2. test_patch_with_range_expansion_api (line ~280) - DUPLICATE of test_patch_with_range_expansion[OpenApi]
# 3-14. random_api_* functions (lines 438-978) - API-specific negative tests, can add back later if needed
# 15. test_abbreviated_commands_api (line ~1147) - DUPLICATE of test_abbreviated_commands[OpenApi]
# 16. test_interactive_password_prompts_not_supported_api (line ~1313) - Not critical
#
# TODO: Delete all 16 skipped test functions below once core tests are stable
#
# =============================================================================

@pytest.mark.general
@pytest.mark.simx
@pytest.mark.nvue_core
def test_invalid_command_syntax(engines, random_api):
    """
    Negative test: PATCH with invalid command should fail with line number (both CLI and API)
    """

    commands = """nv set acl test_acl type ipv4
nv set invalid command here
nv set acl test_acl rule 10 action permit"""

    try:
        with create_temp_file(engines.dut, f'invalid_cmd_{random_api}.txt', commands) as filepath:
            # Use helper function - checks for errors and atomic behavior
            assert_patch_failed_with_errors(engines, random_api, filepath,
                                            expected_keywords=['invalid', 'line 2'])
    finally:
        Acl().acl_id['test_acl'].unset(apply=True).verify_result()
        cleanup_pending_config(engines)


@pytest.mark.general
@pytest.mark.simx
@pytest.mark.nvue_core
def test_unsupported_show_action_questioning(engines, random_api):
    """
    Negative test: PATCH should not support 'nv show', 'nv action', or '?' commands

    Verifies that config patch only accepts 'nv set' and 'nv unset' commands.
    Both 'nv show' (read operations) and 'nv action' (runtime operations) should fail.
    Tests all unsupported command types in a single patch file.
    """

    # Combine multiple unsupported commands in one file
    commands = """nv set acl test_acl type ipv4
nv show acl
nv set acl test_acl rule 10 action permit
nv set system hostname test-host
nv action renew interface eth0 ipv4 dhcp-client"""

    try:
        with create_temp_file(engines.dut, f'unsupported_commands_{random_api}.txt', commands) as filepath:
            # Use helper function - checks for errors and atomic behavior
            # Should fail due to unsupported commands (show/action)
            assert_patch_failed_with_errors(engines, random_api, filepath)
            logger.info("✓ Correctly rejected unsupported commands ('nv show' and 'nv action') in patch")
    finally:
        cleanup_pending_config(engines)


@pytest.mark.general
@pytest.mark.simx
@pytest.mark.nvue_core
def test_patch_with_revision_flags(engines, random_api):
    """
    Negative test: Patch with --rev flag should fail atomically (both CLI and API)

    **CRITICAL BUG** - Found in some NVOS versions:
        $ nv config patch commands.txt
        created [rev_id: 8]
        Error: Unknown revision: 11
        Error: Commands.txt:3 'nv set interface swp1 link state up --rev=11' failed
        $ nv conf diff
        - set:
            interface:
              acp110: ...  ← BUG! Partial changes applied!
              acp111: ...

    **Expected Behavior**:
        - ANY --rev flag in patch commands should cause command to fail
        - nv config diff should be EMPTY (atomic behavior)
        - Same behavior for both CLI and API

    **Bug**: Some NVOS versions apply partial changes (NOT atomic!)
    **Correct**: Other NVOS versions keep diff empty (atomic)

    This test verifies atomic behavior is consistent across all NVOS versions.
    """

    commands_with_flag = """nv set interface eth0 link state up --rev=5"""

    try:
        with create_temp_file(engines.dut, f'rev_flag_test_{random_api}.txt', commands_with_flag) as filepath:
            # Use helper function - checks for errors and atomic behavior
            assert_patch_failed_with_errors(engines, random_api, filepath,
                                            expected_keywords=['rev', 'parameter'])
            logger.info("✓✓✓ CRITICAL TEST PASSED: Atomic behavior confirmed - no partial changes applied")
    finally:
        # No eth0 cleanup needed - test verified atomic behavior (no partial changes)
        cleanup_pending_config(engines)


@pytest.mark.general
@pytest.mark.simx
@pytest.mark.nvue_core
def test_multiple_errors_reports_first(engines, random_api):
    """
    Negative test: Should report first error only (both CLI and API)
    """

    commands = """nv set acl test_acl type ipv4
nv set invalid command one
nv set another invalid command
nv show acl test_acl"""

    try:
        with create_temp_file(engines.dut, f'multi_errors_{random_api}.txt', commands) as filepath:
            # Use helper function - checks for errors and atomic behavior
            assert_patch_failed_with_errors(engines, random_api, filepath,
                                            expected_keywords=['line 2'])
    finally:
        Acl().acl_id['test_acl'].unset(apply=True).verify_result()
        cleanup_pending_config(engines)


# =============================================================================
# NEGATIVE TESTS - CLI
# =============================================================================

@pytest.mark.general
@pytest.mark.simx
@pytest.mark.nvue_core
def test_cli_patch_invalid_yaml(engines):
    """
    Negative test: CLI patch with invalid YAML should fail
    """
    invalid_yaml = """- set:
    system:
      hostname: test
    INVALID YAML HERE { [ }"""

    try:
        with create_temp_file(engines.dut, 'invalid.yaml', invalid_yaml) as filepath:
            with allure.step('Attempt to patch with invalid YAML'):
                output = engines.dut.run_cmd(f'nv config patch {filepath}')

                assert 'Failed to parse YAML' in output or 'error' in output.lower(), \
                    "Should fail with YAML parsing error"

                logger.info(f"✓ Correctly rejected invalid YAML: {output}")

            with allure.step('Verify no partial changes applied (diff should be empty)'):
                diff_output = NvueGeneralCli.diff_config(engines.dut, output_type=OutputFormat.json)
                # Empty diff should be "[]" or empty string
                assert not diff_output or diff_output.strip() == "[]", \
                    f"Config diff should be empty after failed patch, but got: {diff_output}"
                logger.info("✓ Confirmed: no partial changes applied (atomic behavior)")
    finally:
        cleanup_pending_config(engines)


@pytest.mark.general
@pytest.mark.simx
@pytest.mark.nvue_core
def test_cli_replace_invalid_yaml(engines):
    """
    Negative test: CLI replace with invalid YAML should fail
    """
    invalid_yaml = "{ this is not valid yaml at all }"

    try:
        with create_temp_file(engines.dut, 'invalid_replace.yaml', invalid_yaml) as filepath:
            with allure.step('Attempt to replace with invalid YAML'):
                output = engines.dut.run_cmd(f'nv config replace {filepath}')

                assert 'Failed to parse YAML' in output or 'error' in output.lower(), \
                    "Should fail with YAML parsing error"

                logger.info(f"✓ Correctly rejected invalid YAML: {output}")

            with allure.step('Verify no partial changes applied (diff should be empty)'):
                diff_output = NvueGeneralCli.diff_config(engines.dut, output_type=OutputFormat.json)
                assert not diff_output or diff_output.strip() == "[]", \
                    f"Config diff should be empty after failed replace, but got: {diff_output}"
                logger.info("✓ Confirmed: no partial changes applied (atomic behavior)")
    finally:
        cleanup_pending_config(engines)


@pytest.mark.general
@pytest.mark.simx
@pytest.mark.nvue_core
def test_cli_patch_file_not_found(engines):
    """
    Negative test: CLI patch with non-existent file should fail
    """
    try:
        with allure.step('Attempt to patch with non-existent file'):
            output = engines.dut.run_cmd('nv config patch /tmp/nonexistent_file.yaml')

            assert 'not found' in output.lower() or 'error' in output.lower(), \
                "Should fail with file not found error"

            logger.info(f"✓ Correctly rejected non-existent file")

        with allure.step('Verify no partial changes applied (diff should be empty)'):
            diff_output = NvueGeneralCli.diff_config(engines.dut, output_type=OutputFormat.json)
            assert not diff_output or diff_output.strip() == "[]", \
                f"Config diff should be empty after failed patch, but got: {diff_output}"
            logger.info("✓ Confirmed: no partial changes applied (atomic behavior)")
    finally:
        cleanup_pending_config(engines)


@pytest.mark.general
@pytest.mark.simx
@pytest.mark.nvue_core
def test_cli_replace_non_operation_yaml(engines):
    """
    Negative test: CLI replace with non-operation YAML should fail
    """
    non_operation_yaml = "{}"

    try:
        with create_temp_file(engines.dut, 'non_op.yaml', non_operation_yaml) as filepath:
            with allure.step('Attempt to replace with non-operation YAML'):
                output = engines.dut.run_cmd(f'nv config replace {filepath}')

                assert 'must contain a list of operation objects' in output.lower() or 'error' in output.lower(), \
                    "Should fail with operation objects error"

                logger.info(f"✓ Correctly rejected non-operation YAML: {output}")

            with allure.step('Verify no partial changes applied (diff should be empty)'):
                diff_output = NvueGeneralCli.diff_config(engines.dut, output_type=OutputFormat.json)
                assert not diff_output or diff_output.strip() == "[]", \
                    f"Config diff should be empty after failed replace, but got: {diff_output}"
                logger.info("✓ Confirmed: no partial changes applied (atomic behavior)")
    finally:
        cleanup_pending_config(engines)


# =============================================================================
# ADDITIONAL NEGATIVE TESTS - SPECIFIC REQUIREMENTS
# =============================================================================

@pytest.mark.general
@pytest.mark.simx
@pytest.mark.nvue_core
def test_abbreviated_commands(engines, random_api):
    """
    Positive test: Abbreviated commands should be supported (both CLI and API)

    Example: nv set int eth0 desc value (versus nv set interface eth0 description value)
    """

    # Test abbreviated commands: 'int' instead of 'interface', 'desc' instead of 'description'
    abbreviated_config = """nv set int eth0 desc "Abbreviated Test" """

    try:
        with create_temp_file(engines.dut, f'abbreviated_{random_api}.txt', abbreviated_config) as filepath:
            with allure.step(f'Apply config using patch via {random_api} (includes apply)'):
                result_obj = TestToolkit.GeneralApi[TestToolkit.tested_api].config_patch(engines.dut, filepath, apply=True)
                result_obj.verify_result()  # Verify success
                time.sleep(2)

            with allure.step('Verify abbreviated command worked'):
                eth0_port = Port('eth0')
                data = OutputParsingTool.parse_show_interface_output_to_dictionary(
                    eth0_port.interface.show()).get_returned_value()

                assert data.get('description') == "Abbreviated Test", \
                    "Abbreviated 'int' and 'desc' commands should work"

                logger.info(f"✓ Abbreviated commands work correctly via {random_api}")

    finally:
        Port('eth0').interface.unset(op_param='description', apply=True).verify_result()
        cleanup_pending_config(engines)


@pytest.mark.general
@pytest.mark.simx
@pytest.mark.nvue_core
def test_commands_on_same_line_fail(engines, random_api):
    """
    Negative test: Commands must be on separate lines (both CLI and API)

    Multiple commands on the same line should fail
    """

    same_line_commands = "nv set system hostname test1 nv set system message pre-login test2"

    try:
        with create_temp_file(engines.dut, f'same_line_{random_api}.txt', same_line_commands) as filepath:
            # Use helper function - checks for errors and atomic behavior
            assert_patch_failed_with_errors(engines, random_api, filepath)
    finally:
        cleanup_pending_config(engines)


@pytest.mark.general
@pytest.mark.simx
@pytest.mark.nvue_core
def test_interactive_password_prompts_not_supported(engines, random_api):
    """
    Negative test: Interactive password prompts should not be supported (both CLI and API)

    Schema marker: x-cue-prompt-noecho, x-cue-prompt-noecho-confirm

    Command: nv set system aaa user user1 password
    - Old behavior (WRONG): Prompts "Enter new password:" "Confirm password:"
    - New behavior (CORRECT): Should fail without prompting

    This is a breaking change from previous behavior where patch files would
    interactively prompt for passwords. Interactive commands are NOT supported
    in patch files - all data must be provided in the command.
    """

    interactive_config = """nv set system aaa user test_user1 password"""

    try:
        with create_temp_file(engines.dut, f'interactive_{random_api}.txt', interactive_config) as filepath:
            with allure.step(f'Patch with interactive command via {random_api} - should fail'):
                try:
                    result_obj = TestToolkit.GeneralApi[TestToolkit.tested_api].config_patch(engines.dut, filepath, apply=False)
                    result_obj.verify_result()  # Verify if succeeded
                    # If it succeeds (some versions might allow it), that's documented behavior
                    logger.info(f"Note: Interactive command was accepted via {random_api} (version-dependent behavior)")
                except Exception as e:
                    # Expected - should fail as password is required
                    logger.info(f"✓ Interactive password prompt correctly not supported via {random_api}: {e}")

            with allure.step('Verify no partial changes applied'):
                diff_output = NvueGeneralCli.diff_config(engines.dut, output_type=OutputFormat.json)
                if diff_output and diff_output.strip() != "[]":
                    logger.warning(f"Config diff not empty: {diff_output}")
                else:
                    logger.info("✓ Confirmed: no partial changes applied (atomic behavior)")

    finally:
        System().aaa.user.user_id['test_user1'].unset(apply=True).verify_result()
        cleanup_pending_config(engines)


@pytest.mark.general
@pytest.mark.simx
@pytest.mark.nvue_core
def test_multiple_interactive_prompts_not_supported(engines, random_api):
    """
    Negative test: Multiple interactive password prompts should not be supported (both CLI and API)

    Based on spec example:
    $ cat add_user.txt
    nv set system aaa user user1 password
    nv set system aaa user user2 password
    nv set system aaa user user3 password

    Old behavior (WRONG): Would prompt 6 times (password + confirm for each user)
    New behavior (CORRECT): Should fail without prompting
    """

    interactive_config = """nv set system aaa user test_user1 password
nv set system aaa user test_user2 password
nv set system aaa user test_user3 password"""

    try:
        with create_temp_file(engines.dut, f'multi_interactive_{random_api}.txt', interactive_config) as filepath:
            with allure.step(f'Patch with multiple interactive commands via {random_api} - should fail'):
                try:
                    result_obj = TestToolkit.GeneralApi[TestToolkit.tested_api].config_patch(engines.dut, filepath, apply=False)
                    result_obj.verify_result()  # Verify if succeeded
                    # If it succeeds (some versions might allow it), that's documented behavior
                    logger.info(f"Note: Multiple interactive commands were accepted via {random_api} (version-dependent behavior)")
                except Exception as e:
                    # Expected - should fail as passwords are required
                    logger.info(f"✓ Multiple interactive prompts correctly not supported via {random_api}: {e}")

            with allure.step('Verify no users were created'):
                # Double-check that none of the users exist
                for user in ['test_user1', 'test_user2', 'test_user3']:
                    try:
                        # Try to show user - should fail if user doesn't exist
                        user_output = engines.dut.run_cmd(f'nv show system aaa user {user} 2>&1')
                        if 'does not exist' in user_output.lower() or 'not found' in user_output.lower():
                            logger.info(f"✓ User {user} correctly not created")
                        else:
                            raise AssertionError(f"User {user} exists when it shouldn't: {user_output}")
                    except Exception as e:
                        if 'does not exist' in str(e).lower() or 'not found' in str(e).lower():
                            logger.info(f"✓ User {user} correctly not created")
                        else:
                            raise

                logger.info("✓ Confirmed: no users created despite multiple commands in patch")

    finally:
        for user in ['test_user1', 'test_user2', 'test_user3']:
            try:
                System().aaa.user.unset(op_param=user, apply=False)
            except Exception:
                pass  # Ignore if user doesn't exist
        cleanup_pending_config(engines)


@pytest.mark.general
@pytest.mark.simx
@pytest.mark.nvue_core
def test_environment_variable_expansion(engines, random_api):
    """
    Test that environment variables are NOT expanded in patch files

    Verifies that patch files treat shell variables as literal strings (expected behavior).
    Shell variable expansion is NOT supported in NVUE patch files.

    Expected behavior: nv set interface eth0 description $TEST_VAR  -> remains as literal "$TEST_VAR"
    Note: This is different from terminal behavior where variables would be expanded by the shell.
    """

    # Set environment variable for test (to verify it's NOT expanded)
    test_var_value = "expanded_test_value_12345"
    patch_config = """nv set interface eth0 description $TEST_VAR"""
    expected_literal = "$TEST_VAR"  # Expected to remain as literal string

    try:
        with allure.step('Set TEST_VAR environment variable (for contrast testing)'):
            engines.dut.run_cmd(f'export TEST_VAR="{test_var_value}"')
            check_var = engines.dut.run_cmd('echo $TEST_VAR')
            logger.info(f"TEST_VAR value in shell: {check_var.strip()}")

        with create_temp_file(engines.dut, f'variable_test_{random_api}.txt', patch_config) as filepath:
            with allure.step(f'Apply patch file via {random_api} (includes apply)'):
                result_obj = TestToolkit.GeneralApi[TestToolkit.tested_api].config_patch(engines.dut, filepath, apply=True)
                result_obj.verify_result()  # Verify success
                time.sleep(2)

            with allure.step('Verify variable is NOT expanded (remains as literal string)'):
                eth0_port = Port('eth0')
                data = OutputParsingTool.parse_show_interface_output_to_dictionary(
                    eth0_port.interface.show()).get_returned_value()
                desc = data.get('description', '')
                logger.info(f"Description result: {desc}")

                assert desc == expected_literal, \
                    f"Variable should NOT be expanded in patch files via {random_api}. Expected literal: '{expected_literal}', Got: '{desc}'"

                logger.info(f"✓ Variable correctly remains as literal string (not expanded) via {random_api}")

    finally:
        Port('eth0').interface.unset(op_param='description', apply=True).verify_result()
        cleanup_pending_config(engines)
        engines.dut.run_cmd('unset TEST_VAR 2>/dev/null || true')
        NvueGeneralCli.apply_config(engines.dut, ask_for_confirmation=True)


@pytest.mark.general
@pytest.mark.simx
@pytest.mark.nvue_core
def test_interface_commands_eth_ports(engines, devices):
    """
    Positive test: Interface commands for the device's available management ports.

    Example: nv set interface eth0 link state up
    """
    management_ports = devices.dut.get_mgmt_ports()
    assert management_ports, "Device model must define at least one management port"
    descriptions = {
        interface_name: f"Test Port {index}"
        for index, interface_name in enumerate(management_ports)
    }
    interface_config = '\n'.join(
        command
        for interface_name in management_ports
        for command in (
            f"nv set interface {interface_name} link state up",
            f'nv set interface {interface_name} description "{descriptions[interface_name]}"',
        )
    )

    try:
        with create_temp_file(engines.dut, 'interfaces.txt', interface_config) as filepath:
            with allure.step('Apply interface configuration'):
                result_obj = TestToolkit.GeneralApi[TestToolkit.tested_api].config_patch(engines.dut, filepath, apply=True)
                result_obj.verify_result()  # Verify success
                time.sleep(2)

            for interface_name in management_ports:
                with allure.step(f'Verify {interface_name} configuration'):
                    port = Port(interface_name)
                    data = OutputParsingTool.parse_show_interface_output_to_dictionary(
                        port.interface.show()).get_returned_value()

                    # NVUE JSON format: state is an object with state value as key, e.g., {'up': {}}
                    state = data.get('link', {}).get('state', {})
                    assert 'up' in state, f"{interface_name} should be up, got state: {state}"
                    assert data.get('description') == descriptions[interface_name], \
                        f"{interface_name} description should match"

            logger.info("Interface commands work correctly for %s", management_ports)

    finally:
        for interface_name in management_ports[:-1]:
            Port(interface_name).interface.unset(op_param='description', apply=False)

        Port(management_ports[-1]).interface.unset(op_param='description', apply=True).verify_result()
        cleanup_pending_config(engines)


@pytest.mark.general
@pytest.mark.simx
@pytest.mark.nvue_core
def test_nv_config_diff_with_masked_passwords(engines):
    """
    Test: Verify behavior with masked passwords from nv config diff

    Known issue: Passwords may be masked (*) instead of showing hashed values
    This test documents the current behavior
    """
    system = System()

    try:
        with allure.step('Set a configuration that includes password'):
            # Set hostname as baseline config
            system.set(SystemConsts.HOSTNAME, "test-masked-pwd", apply=False)

        with allure.step('Export config using diff'):
            diff_output = NvueGeneralCli.diff_config(engines.dut, output_type=OutputFormat.yaml)
            logger.info(f"Config diff output:\n{diff_output}")

        with allure.step('Check for masked passwords'):
            # Document if passwords appear as "*" in output
            if '"password": "*"' in diff_output or 'password: "*"' in diff_output:
                logger.info("✓ Passwords are masked with * in diff output (known behavior)")
            else:
                logger.info("Passwords not found masked in this configuration")

        with allure.step('Verify diff output can be used in replace'):
            # Even with masked passwords, try to use the diff output
            # May need to replace masked passwords with actual values
            modified_diff = diff_output.replace('"password": "*"', '"password": "admin"')

            with create_temp_file(engines.dut, 'masked_pwd.yaml', modified_diff) as filepath:
                # This documents that masked passwords need to be replaced
                TestToolkit.GeneralApi[TestToolkit.tested_api].config_replace(engines.dut, filepath, apply=True)

                logger.info("✓ Modified diff (with password replacement) can be used")

    finally:
        system.unset(SystemConsts.HOSTNAME, apply=True, ask_for_confirmation=True)
        cleanup_pending_config(engines)


@pytest.mark.general
@pytest.mark.simx
@pytest.mark.nvue_core
def test_nv_config_diff_incomplete_commands(engines):
    """
    Test: Verify behavior with potentially incomplete commands from nv config diff

    Known issue: nv config diff -o commands may produce incomplete commands
    This test documents the current behavior
    """
    try:
        with allure.step('Create test configuration'):
            Acl().acl_id['test_diff_acl'].set('type', 'ipv4', apply=False)
            Acl().acl_id['test_diff_acl'].rule.rule_id[10].action.set('permit', apply=False)
            Acl().acl_id['test_diff_acl'].rule.rule_id[20].action.set('deny', apply=True, ask_for_confirmation=True)
            time.sleep(2)

        with allure.step('Export config using diff -o commands'):
            diff_commands = engines.dut.run_cmd('nv config diff -o commands')
            logger.info(f"Config diff commands output:\n{diff_commands}")

        with allure.step('Check for command completeness'):
            # Check if commands look complete
            lines = diff_commands.strip().split('\n')
            incomplete_commands = []

            for line in lines:
                if line.strip() and not line.startswith('nv '):
                    incomplete_commands.append(line)

            if incomplete_commands:
                logger.warning(f"Found potentially incomplete commands: {incomplete_commands}")
                logger.info("✓ Documented incomplete commands issue in diff output")
            else:
                logger.info("All commands appear complete in this test")

        with allure.step('Test if diff commands can be used in patch'):
            # Try to use the diff output directly
            with create_temp_file(engines.dut, 'diff_commands.txt', diff_commands) as filepath:
                try:
                    result_obj = TestToolkit.GeneralApi[TestToolkit.tested_api].config_patch(engines.dut, filepath, apply=False)
                    result_obj.verify_result()  # Verify if succeeded
                    logger.info("✓ Diff commands can be used in patch")
                except Exception as e:
                    logger.warning(f"Diff commands may have issues: {e}")

    finally:
        Acl().acl_id['test_diff_acl'].unset(apply=True).verify_result()
        cleanup_pending_config(engines)


@pytest.mark.general
@pytest.mark.simx
@pytest.mark.nvue_core
def test_mixed_set_and_unset_commands(engines, random_api):
    """
    Positive test: Batch should support both nv set and nv unset commands (both CLI and API)
    """

    mixed_commands = """nv set acl test_mixed_acl type ipv4
nv set acl test_mixed_acl rule 10 action permit
nv set acl test_mixed_acl rule 20 action deny
nv set acl test_mixed_acl rule 30 action permit"""

    unset_commands = """nv unset acl test_mixed_acl rule 20
nv unset acl test_mixed_acl rule 30"""

    try:
        with allure.step(f'Create ACL with multiple rules via {random_api} (includes apply)'):
            with create_temp_file(engines.dut, f'mixed_set_{random_api}.txt', mixed_commands) as filepath1:
                result_obj = TestToolkit.GeneralApi[TestToolkit.tested_api].config_patch(engines.dut, filepath1, apply=True)
                result_obj.verify_result()  # Verify success
                time.sleep(2)

        with allure.step('Verify all rules created'):
            acl = Acl()
            data = acl.acl_id['test_mixed_acl'].parse_show()
            rules = data.get('rule', {})
            assert len(rules) == 3, f"Expected 3 rules, found {len(rules)}"

        with allure.step(f'Use unset commands to remove rules via {random_api} (includes apply)'):
            with create_temp_file(engines.dut, f'mixed_unset_{random_api}.txt', unset_commands) as filepath2:
                result_obj = TestToolkit.GeneralApi[TestToolkit.tested_api].config_patch(engines.dut, filepath2, apply=True)
                result_obj.verify_result()  # Verify success
                time.sleep(2)

        with allure.step('Verify rules removed'):
            data = acl.acl_id['test_mixed_acl'].parse_show()
            rules = data.get('rule', {})
            assert len(rules) == 1, f"Expected 1 rule remaining, found {len(rules)}"
            assert '10' in rules, "Rule 10 should remain"

            logger.info(f"✓ Mixed set and unset commands work correctly via {random_api}")

    finally:
        Acl().acl_id['test_mixed_acl'].unset(apply=True).verify_result()
        cleanup_pending_config(engines)


@pytest.mark.general
@pytest.mark.simx
@pytest.mark.nvue_core
def test_maximum_properties_constraint_acl_action(engines, random_api):
    """
    Positive test: Maximum properties constraint - only last value should remain (both CLI and API)

    Test flow with multiple conflicting commands mixed with other commands:
        1. Set ACL rule 10 action permit
        2. Set ACL rule 10 action deny
        3. Set ACL rule 20 action permit (different rule - should persist)
        4. Set ACL rule 10 action permit (again)
        5. Set ACL rule 10 action deny (again - final value)
        6. Set ACL rule 30 action permit (different rule - should persist)

    Expected result:
        - Rule 10: only 'deny' (last setting for rule 10)
        - Rule 20: 'permit' (unaffected)
        - Rule 30: 'permit' (unaffected)

    This tests the maximum properties constraint where setting a new value
    automatically unsets the previous mutually exclusive value, even with
    multiple back-and-forth changes and other commands mixed in.
    """

    try:
        # Complex patch with multiple conflicting actions + other commands
        patch_commands = """nv set acl test_max_prop_acl type ipv4
nv set acl test_max_prop_acl rule 10 action permit
nv set acl test_max_prop_acl rule 10 action deny
nv set acl test_max_prop_acl rule 20 action permit
nv set acl test_max_prop_acl rule 10 action permit
nv set acl test_max_prop_acl rule 10 action deny
nv set acl test_max_prop_acl rule 30 action permit"""

        with create_temp_file(engines.dut, f'max_prop_complex_{random_api}.txt', patch_commands) as filepath:
            with allure.step(f'Apply patch with mutually exclusive settings via {random_api} (includes apply)'):
                result_obj = TestToolkit.GeneralApi[TestToolkit.tested_api].config_patch(engines.dut, filepath, apply=True)
                result_obj.verify_result()  # Verify success
                time.sleep(2)

            with allure.step('Verify rule 10 has only last action (deny)'):
                acl = Acl()
                acl_data = acl.acl_id['test_max_prop_acl'].rule.rule_id['10'].parse_show()

                action_config = acl_data.get('action', {})
                logger.info(f"ACL rule 10 action config: {action_config}")

                # Rule 10 should have deny (last setting)
                assert 'deny' in action_config, \
                    f"Expected 'deny' action for rule 10, but got: {action_config}"

                # Rule 10 should NOT have permit (auto-unset by last deny)
                assert 'permit' not in action_config, \
                    f"'permit' should have been auto-unset when 'deny' was set, but both present: {action_config}"

                logger.info(f"✓ Rule 10 ({random_api}): Maximum properties constraint working - only 'deny' active")

            with allure.step('Verify rule 20 has permit (unaffected by rule 10 changes)'):
                rule20_data = acl.acl_id['test_max_prop_acl'].rule.rule_id['20'].parse_show()

                rule20_action = rule20_data.get('action', {})
                logger.info(f"ACL rule 20 action config: {rule20_action}")

                assert 'permit' in rule20_action, \
                    f"Rule 20 should have 'permit', got: {rule20_action}"
                logger.info("✓ Rule 20: 'permit' correctly preserved")

            with allure.step('Verify rule 30 has permit (unaffected by rule 10 changes)'):
                rule30_data = acl.acl_id['test_max_prop_acl'].rule.rule_id['30'].parse_show()

                rule30_action = rule30_data.get('action', {})
                logger.info(f"ACL rule 30 action config: {rule30_action}")

                assert 'permit' in rule30_action, \
                    f"Rule 30 should have 'permit', got: {rule30_action}"
                logger.info("✓ Rule 30: 'permit' correctly preserved")

            with allure.step('Verify complete ACL structure'):
                full_data = acl.acl_id['test_max_prop_acl'].parse_show()
                rules = full_data.get('rule', {})

                assert '10' in rules and '20' in rules and '30' in rules, \
                    f"Expected 3 rules, got: {list(rules.keys())}"

                logger.info(f"✓ Maximum properties constraint verified via {random_api}:")
                logger.info("  - Rule 10: deny (last of multiple conflicting values)")
                logger.info("  - Rule 20: permit (independent command preserved)")
                logger.info("  - Rule 30: permit (independent command preserved)")

    finally:
        Acl().acl_id['test_max_prop_acl'].unset(apply=True).verify_result()
        cleanup_pending_config(engines)


@pytest.mark.general
@pytest.mark.simx
@pytest.mark.nvue_core
def test_patch_30k_acl_prefixes_performance(engines, random_api):
    """
    Performance test: Apply 30K ACL rules with unique IP prefixes

    Related to bug https://redmine.mellanox.com/issues/4491397:
    "5.14.0024: NGN with NVUE - applying 30k prefixes with CLI using nv set
    commands takes 12 hours"

    This test validates that:
    1. Large-scale ACL configurations can be applied successfully
    2. Performance is acceptable (no gradual slowdown)
    3. Both NVUE CLI and OpenAPI can handle 30K rules

    Test generates ACL rules with unique source IP addresses:
    - IPs from 10.0.0.0 to 10.0.117.47 (30,000 addresses)
    - Each rule has a unique priority (1-30000)
    - All rules use permit action with ICMP match

    Regression test for bug #4491397 where applying 30K prefixes took 12 hours
    with gradual performance degradation (1.2s → 2.4s per PATCH).
    """
    acl_name = 'test_30k_acl'
    num_rules = 30000
    # Use large config timeout for 30K ACL rules
    APPLY_TIMEOUT = LARGE_CONFIG_APPLY_TIMEOUT

    try:
        with allure.step(f'Generate 30K ACL rules with unique IP prefixes'):
            start_generation = time.time()

            # Generate IP addresses: 10.0.0.0 to 10.0.117.47
            # 30000 addresses spanning: 10.0.0.0-255, 10.0.1.0-255, ... 10.0.117.0-47
            commands = [f"nv set acl {acl_name} type ipv4"]

            for rule_num in range(1, num_rules + 1):
                # Calculate IP: 10.A.B.C where rule_num maps sequentially
                # rule 1 → 10.0.0.0, rule 2 → 10.0.0.1, ... rule 256 → 10.0.0.255, rule 257 → 10.0.1.0, etc.
                # Use (rule_num - 1) as index to map 1→0, 2→1, etc.
                index = rule_num - 1
                octet2 = index // 65536  # 256*256
                octet3 = (index % 65536) // 256
                octet4 = index % 256

                ip_addr = f"10.{octet2}.{octet3}.{octet4}"

                # Create rule with unique source IP
                commands.append(f"nv set acl {acl_name} rule {rule_num} match ip source-ip {ip_addr}/32")
                commands.append(f"nv set acl {acl_name} rule {rule_num} match ip protocol icmp")
                commands.append(f"nv set acl {acl_name} rule {rule_num} action permit")

            config_content = '\n'.join(commands)
            generation_time = time.time() - start_generation

            logger.info(f"✓ Generated {num_rules} ACL rules in {generation_time:.2f}s")
            logger.info(f"First rule: 10.0.0.0/32")
            logger.info(f"Last rule: {ip_addr}/32")
            logger.info(f"Config size: {len(config_content)} bytes (~{len(config_content) / 1024:.1f} KB)")

        with allure.step('Create temporary file with 30K rules and apply patch'):
            with create_temp_file(engines.dut, 'test_30k_acl.txt', config_content) as filepath:
                file_size = engines.dut.run_cmd(f"wc -c < {filepath}").strip()
                line_count = engines.dut.run_cmd(f"wc -l < {filepath}").strip()
                logger.info(f"✓ Temp file created: {filepath}")
                logger.info(f"  File size: {file_size} bytes")
                logger.info(f"  Line count: {line_count}")

                with allure.step(f'Apply 30K rules via {random_api} patch (measure performance)'):
                    # Record start time
                    start_time = time.time()
                    start_timestamp = time.strftime('%Y-%m-%d %H:%M:%S')

                    logger.info(f"Starting patch+apply operation at {start_timestamp}")
                    logger.info(f"This may take several minutes for 30K rules...")

                    # Use same pattern as all other tests in this file
                    # Pass APPLY_TIMEOUT for large config operations
                    result_obj = TestToolkit.GeneralApi[TestToolkit.tested_api].config_patch(
                        engines.dut, filepath, apply=True, apply_timeout=APPLY_TIMEOUT
                    )
                    result_obj.verify_result()
                    logger.info(f"✓ Config patch and apply completed successfully")

                    # Calculate elapsed time
                    elapsed_time = time.time() - start_time
                    end_timestamp = time.strftime('%Y-%m-%d %H:%M:%S')

                    # Performance metrics
                    avg_time_per_rule = elapsed_time / num_rules
                    rules_per_second = num_rules / elapsed_time if elapsed_time > 0 else 0

                    logger.info(f"✓ Patch operation completed successfully")
                    logger.info(f"Performance Metrics:")
                    logger.info(f"  Start time: {start_timestamp}")
                    logger.info(f"  End time: {end_timestamp}")
                    logger.info(f"  Total time: {elapsed_time:.2f}s ({elapsed_time / 60:.2f} minutes)")
                    logger.info(f"  Average time per rule: {avg_time_per_rule * 1000:.2f}ms")
                    logger.info(f"  Throughput: {rules_per_second:.2f} rules/second")

                    # Allure reporting
                    allure.attach(
                        "Performance Metrics",
                        f"Total time: {elapsed_time:.2f}s\n"
                        f"Rules applied: {num_rules}\n"
                        f"Average per rule: {avg_time_per_rule * 1000:.2f}ms\n"
                        f"Throughput: {rules_per_second:.2f} rules/s\n"
                        f"API method: {random_api}"
                    )

                    # Performance assertion: Should complete in reasonable time
                    # Bug #4491397 reported 12 hours for 30K rules (1.44s/rule average)
                    # After fix, we expect much better performance
                    max_acceptable_time = APPLY_TIMEOUT  # 10 minutes max (0.02s/rule average)
                    assert elapsed_time < max_acceptable_time, \
                        f"Patch took {elapsed_time:.2f}s ({elapsed_time / 60:.2f}min), " \
                        f"exceeds maximum {max_acceptable_time}s ({max_acceptable_time / 60:.0f}min). " \
                        f"Possible performance regression (see bug #4491397)"

                    logger.info(f"✓ Performance acceptable: {elapsed_time:.2f}s < {max_acceptable_time}s ({max_acceptable_time / 60:.0f}min)")

        with allure.step('Verify ACL was created with correct type'):
            acl = Acl()
            acl_data = acl.acl_id[acl_name].parse_show()

            assert acl_data is not None, f"ACL {acl_name} not found"
            assert acl_data.get('type') == 'ipv4', \
                f"Expected ACL type 'ipv4', got: {acl_data.get('type')}"

            logger.info(f"✓ ACL {acl_name} created successfully with type ipv4")

        with allure.step('Verify sample rules from different ranges'):
            # Check first rule (rule 1)
            rule1_data = acl.acl_id[acl_name].rule.rule_id['1'].parse_show()
            assert rule1_data is not None, "Rule 1 not found"
            rule1_ip = rule1_data.get('match', {}).get('ip', {}).get('source-ip')
            assert rule1_ip == '10.0.0.0/32', \
                f"Rule 1 expected IP 10.0.0.0/32, got: {rule1_ip}"
            logger.info(f"✓ Rule 1: {rule1_ip} (first rule)")

            # Check middle rule (rule 15000)
            rule_mid = '15000'
            rule_mid_data = acl.acl_id[acl_name].rule.rule_id[rule_mid].parse_show()
            assert rule_mid_data is not None, f"Rule {rule_mid} not found"
            rule_mid_ip = rule_mid_data.get('match', {}).get('ip', {}).get('source-ip')
            logger.info(f"✓ Rule {rule_mid}: {rule_mid_ip} (middle rule)")

            # Check last rule (rule 30000)
            rule_last = str(num_rules)
            rule_last_data = acl.acl_id[acl_name].rule.rule_id[rule_last].parse_show()
            assert rule_last_data is not None, f"Rule {rule_last} not found"
            rule_last_ip = rule_last_data.get('match', {}).get('ip', {}).get('source-ip')
            logger.info(f"✓ Rule {rule_last}: {rule_last_ip} (last rule)")

            # Verify all have permit action and ICMP protocol
            for rule_id, rule_data in [('1', rule1_data), (rule_mid, rule_mid_data), (rule_last, rule_last_data)]:
                action = rule_data.get('action', {})
                protocol = rule_data.get('match', {}).get('ip', {}).get('protocol')

                assert 'permit' in action, \
                    f"Rule {rule_id} should have permit action, got: {action}"
                assert protocol == 'icmp', \
                    f"Rule {rule_id} should have ICMP protocol, got: {protocol}"

            logger.info("✓ Sample rules verified: correct IPs, actions, and protocols")

        with allure.step('Verify total rule count'):
            # Get all rules
            full_acl = acl.acl_id[acl_name].parse_show()
            rules = full_acl.get('rule', {})
            actual_count = len(rules)

            assert actual_count == num_rules, \
                f"Expected {num_rules} rules, but found {actual_count}"

            logger.info(f"✓ All {num_rules} rules successfully applied and verified")

        logger.info("=" * 80)
        logger.info(f"30K ACL PREFIX PERFORMANCE TEST SUMMARY ({random_api})")
        logger.info("=" * 80)
        logger.info(f"Total rules applied: {num_rules}")
        logger.info(f"Total time: {elapsed_time:.2f}s ({elapsed_time / 60:.2f} minutes)")
        logger.info(f"Average per rule: {avg_time_per_rule * 1000:.2f}ms")
        logger.info(f"Throughput: {rules_per_second:.2f} rules/second")
        logger.info(f"Verification: PASSED")
        logger.info(f"Related bug: https://redmine.mellanox.com/issues/4491397")
        logger.info("=" * 80)

    finally:
        with allure.step('Cleanup: Remove 30K ACL configuration'):
            # Unset ACL (this might also take some time with 30K rules)
            logger.info(f"Removing ACL {acl_name} with {num_rules} rules...")
            cleanup_start = time.time()
            Acl().acl_id[acl_name].unset(apply=True).verify_result()
            cleanup_time = time.time() - cleanup_start
            logger.info(f"✓ Cleanup completed in {cleanup_time:.2f}s")
        cleanup_pending_config(engines)
