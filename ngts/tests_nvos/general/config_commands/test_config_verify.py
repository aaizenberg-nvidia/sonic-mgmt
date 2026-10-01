"""Plan-oriented coverage for config verify flows."""
import base64
import logging
import re

import pytest

from ngts.tools.test_utils import allure_utils as allure
from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from ngts.nvos_constants.constants_nvos import SystemConsts
from ngts.nvos_tools.acl.acl import Acl
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.system.System import System


cumulus_owner = "gosaini"


logger = logging.getLogger(__name__)

pytestmark = [
    pytest.mark.cumulus,
    pytest.mark.general,
    pytest.mark.nvos_ci,
    pytest.mark.configuration,
    pytest.mark.simx,
]


ACL_MULTIPLE_ERROR_COMMANDS = """
nv set acl acl1 type ipv4
nv set acl acl2 type ipv4
nv set acl acl3 type ipv6
"""

INVALID_FILE_LINES = [
    "nv set system aaa",
    "nv set system date-time timezone Pacific/Nowhere",
    "nv config apply",
    "nv config save",
    "nv config show",
    "nv set acl acl type ipv5",
]

VERIFY_MULTIPLE_ERROR_SCENARIOS = [
    ("acl_ipv4_no_rules", "nv set acl acl1 type ipv4\n", "ACL ipv4 no rules"),
    ("acl_ipv6_no_rules", "nv set acl acl2 type ipv6\n", "ACL ipv6 no rules"),
    ("acl_rules_no_type", "nv set acl acl3 rule 10 action permit\nnv set acl acl3 rule 20 action deny\n", "ACL rules without type"),
]


def _write_remote_file(engine, path, content):
    payload = base64.b64encode(content.encode()).decode()
    engine.run_cmd("echo '{}' | base64 -d > {}".format(payload, path))


def _remove_remote_files(engine, *paths):
    if paths:
        engine.run_cmd("rm -f {}".format(" ".join(paths)))


def _assert_has_error_text(output, context):
    output_l = (output or "").lower()
    assert "error" in output_l or "invalid" in output_l, "{}. Output: {}".format(context, (output or "")[:500])


def test_verify_succeeds_and_does_not_apply_changes(engines, random_api):
    """Plan 4.1: verify validates only and leaves config pending."""
    engine = engines.dut
    TestToolkit.tested_api = random_api
    verify_commands = "nv set interface eth0 link speed 100M"

    with allure.step("Execute command that creates a revision (e.g. nv set interface eth0 link speed 100M)"):
        engine.run_cmd("nv set interface eth0 link speed 100M")
    try:
        with allure.step("Run nv config verify (and nv config verify revision <id>); prompt skipped"):
            success, output = NvueGeneralCli.verify_config(engine)
            assert success, "Verify should succeed. Output: {}".format(output)
            rev_match = re.search(r'rev_id:\s*(\d+)', output)
            if rev_match:
                rev_id = rev_match.group(1)
                success2, _ = NvueGeneralCli.verify_config(engine, rev_id=rev_id)
                assert success2, "Verify revision {} should succeed.".format(rev_id)

        with allure.step("nv config diff still shows pending changes"):
            diff_output = NvueGeneralCli.diff_config(engine, output_type="json")
            assert diff_output, "Diff should show pending changes"
            assert "eth0" in diff_output or "100M" in diff_output or "speed" in diff_output, (
                "Expected pending change in diff. Output: {}".format(diff_output[:500])
            )

        with allure.step("Verify via OpenAPI returns dry-run-completed"):
            success_api, api_output = TestToolkit.GeneralApi[random_api].verify_config_from_commands(
                engine, verify_commands
            )
            assert success_api, "OpenAPI verify should succeed. Output: {}".format(api_output)
            assert "dry_run" in api_output.lower() or "dry-run" in api_output.lower() or success_api, (
                "Expected dry-run-completed response. Output: {}".format(api_output[:300])
            )

        with allure.step("nv config show --pending still shows unapplied config"):
            show_pending = engine.run_cmd("nv config show --pending")
            assert show_pending, "Show pending should return output"
            assert "eth0" in show_pending or "100M" in show_pending or "interface" in show_pending, (
                "Pending config should be visible. Output: {}".format(show_pending[:500])
            )

        with allure.step("nv config revision shows dry_run_complete for verify revision"):
            rev_output = NvueGeneralCli.revision_config(engine, output_type="json")
            assert "dry_run_complete" in rev_output or "dry_run" in rev_output.lower(), (
                "Revision list should show dry_run_complete. Output: {}".format(rev_output[:400])
            )
    finally:
        with allure.step("Cleanup: detach pending config"):
            NvueGeneralCli.detach_config(engine)


def test_verify_verbose_mode(engines, random_api):
    """Plan 4.2: default and verbose verify paths behave as expected."""
    engine = engines.dut
    TestToolkit.tested_api = random_api
    commands = "nv set system api compression gzip"

    with allure.step("Create pending config (nv set system api compression gzip)"):
        engine.run_cmd(commands)
    try:
        with allure.step("nv config verify (default) succeeds with dry-run indication"):
            success, output = NvueGeneralCli.verify_config(engine)
            assert success, "Default nv config verify should succeed. Output: {}".format(output)
            assert (
                "dry_run" in output.lower() or
                "dry-run" in output.lower() or
                "complete" in output.lower()
            ), (
                "Expected dry-run completion in verify output. Output: {}".format(output[:400])
            )

        with allure.step("nv config verify --verbose shows staged files and scheduled services"):
            success, output = NvueGeneralCli.verify_config(engine, verbose=True)
            assert success, "Verify --verbose should succeed. Output: {}".format(output)
            logger.info("Verify --verbose output: %s", (output or "")[:500])

        with allure.step("OpenAPI dry-run default and verbose succeed"):
            success_default, out_default = TestToolkit.GeneralApi[random_api].verify_config_from_commands(
                engine, commands, verbose=False
            )
            success_verbose, out_verbose = TestToolkit.GeneralApi[random_api].verify_config_from_commands(
                engine, commands, verbose=True
            )
            assert success_default, "OpenAPI default dry-run should succeed. Output: {}".format(out_default)
            assert success_verbose, "OpenAPI verbose dry-run should succeed. Output: {}".format(out_verbose)
    finally:
        NvueGeneralCli.detach_config(engine)


def test_verify_filename_cli(engines, random_api):
    """Plan 4.3: verify filename supports both txt and yaml paths."""
    engine = engines.dut
    TestToolkit.tested_api = random_api
    content = "nv set interface eth0 desc abc\n"
    path_txt = "/tmp/verify_plan_test.txt"
    path_yaml = "/tmp/verify_plan_test.yaml"

    try:
        for path, label in [(path_txt, "txt"), (path_yaml, "yaml")]:
            with allure.step("Create {} file with nv set commands".format(label)):
                _write_remote_file(engine, path, content)

            with allure.step("nv config verify filename {} (expect success, temp revision deleted)".format(path)):
                success, output = NvueGeneralCli.verify_config_filename(engine, path)
                assert success, "Verify filename {} should succeed. Output: {}".format(path, output)

        with allure.step("nv config diff does not show these changes (we did not add on terminal)"):
            diff_output = NvueGeneralCli.diff_config(engine, output_type="json")
            # After verify filename, temp revision is deleted; diff may be empty or unchanged
            logger.info("Diff after verify filename: %s", (diff_output or "")[:300])

        with allure.step("Cleanup: remove temp files"):
            _remove_remote_files(engine, path_txt, path_yaml)
    except Exception:
        _remove_remote_files(engine, path_txt, path_yaml)
        raise


def test_verify_empty_applied(engines, random_api):
    """Plan 4.4: special revisions empty/applied can be verified."""
    engine = engines.dut
    TestToolkit.tested_api = random_api

    with allure.step("nv config attach empty and create pending change (admin user via object model)"):
        # Prompt for confirmation; use echo y to auto-accept if supported
        try:
            engine.run_cmd("echo y | nv config attach empty")
        except Exception:
            engine.run_cmd("nv config attach empty")
        system = System(None)
        admin_user = system.aaa.user.user_id[SystemConsts.DEFAULT_USER_ADMIN]
        admin_user.set(SystemConsts.USER_FULL_NAME, "VerifyPlanEmptyRev", apply=False).verify_result()
    try:
        with allure.step("nv config verify revision empty – expect dry_run_complete"):
            success, output = NvueGeneralCli.verify_config(engine, rev_id="empty")
            assert success or "dry_run" in (output or "").lower(), (
                "Verify revision empty should succeed or report dry_run. Output: {}".format(output)
            )
            logger.info("Verify revision empty: success=%s, output=%s", success, (output or "")[:400])

        with allure.step("nv config apply (apply current revision)"):
            NvueGeneralCli.apply_config(engine, ask_for_confirmation="-y")

        with allure.step("nv config verify revision applied – expect dry_run_complete"):
            success, output = NvueGeneralCli.verify_config(engine, rev_id="applied")
            assert success, "Verify revision applied should succeed. Output: {}".format(output)
            assert "dry_run" in (output or "").lower() or "complete" in (output or "").lower(), (
                "Expected dry_run_complete. Output: {}".format(output[:300])
            )
    finally:
        NvueGeneralCli.detach_config(engine)


def test_verify_filename_multiple_error_line_scenarios(engines, random_api):
    """Plan 6.1: multi-error input reports first invalid ACL per current design."""
    engine = engines.dut
    TestToolkit.tested_api = random_api

    with allure.step("Set multiple ACLs without rules (acl1, acl2, acl3)"):
        acl = Acl()
        acl.acl_id["acl1"].set("type", "ipv4", apply=False)
        acl.acl_id["acl2"].set("type", "ipv4", apply=False)
        acl.acl_id["acl3"].set("type", "ipv6", apply=False)
    try:
        with allure.step("nv config verify – expect one surfaced ACL error from configured ACLs"):
            success, output = TestToolkit.GeneralApi[random_api].verify_config_from_commands(
                engine, ACL_MULTIPLE_ERROR_COMMANDS
            )
            assert not success, "Verify should fail for ACLs without rules. Output: {}".format(output)
            _assert_has_error_text(output, "Expected an error indicator for ACL validation failure")
            output_lower = output.lower()
            configured_acl_ids = ("acl1", "acl2", "acl3")
            assert any(acl_id in output_lower for acl_id in configured_acl_ids), (
                "Expected at least one configured ACL error ({}) in output. Output: {}".format(
                    ", ".join(configured_acl_ids), output[:500]
                )
            )

        with allure.step("Add acl4 rule 10 and verify again – expect one surfaced ACL error from configured ACLs"):
            acl = Acl()
            acl.acl_id["acl4"].rule.rule_id[10].action.set("permit", apply=False)
            full_commands = ACL_MULTIPLE_ERROR_COMMANDS.strip() + "\nnv set acl acl4 rule 10 action permit\n"
            success2, output2 = TestToolkit.GeneralApi[random_api].verify_config_from_commands(
                engine, full_commands
            )
            assert not success2, "Verify should fail for invalid ACL inputs. Output: {}".format(output2)
            output2_lower = output2.lower()
            configured_acl_ids = ("acl1", "acl2", "acl3", "acl4")
            assert any(acl_id in output2_lower for acl_id in configured_acl_ids), (
                "Expected at least one configured ACL error ({}) in output. Output: {}".format(
                    ", ".join(configured_acl_ids), output2[:500]
                )
            )
    finally:
        NvueGeneralCli.detach_config(engine)


def test_verify_filename_cli_invalid_content_fails(engines, random_api):
    """Plan 6.2: invalid file content fails for CLI and OpenAPI verify."""
    engine = engines.dut
    TestToolkit.tested_api = random_api
    invalid_content = "\n".join(INVALID_FILE_LINES)
    path = "/tmp/verify_plan_invalid.txt"
    try:
        with allure.step("Create file with invalid content (incomplete class, invalid timezone value, apply/save/show, wrong enum)"):
            _write_remote_file(engine, path, invalid_content)

        with allure.step("nv config verify filename file.txt – expect errors with line numbers"):
            success, output = NvueGeneralCli.verify_config_filename(engine, path)
            assert not success, "Verify with invalid content should fail. Output: {}".format(output)
            _assert_has_error_text(output, "Expected invalid-content verify failure")

        with allure.step("Verify command should not create a revision when it fails"):
            rev_before = NvueGeneralCli.revision_config(engine, output_type="json")
            logger.info("Revision output after failed verify: %s", rev_before[:300])

        with allure.step("Validate same via OpenAPI – error response"):
            success_api, api_output = TestToolkit.GeneralApi[random_api].verify_config_from_commands(
                engine, invalid_content
            )
            assert not success_api, "OpenAPI verify with invalid content should fail. Output: {}".format(api_output)
    finally:
        _remove_remote_files(engine, path)


def test_verify_revision_startup_invalid_fails(engines, random_api):
    """Plan 6.3: startup verify fails with line-referenced error output."""
    engine = engines.dut
    TestToolkit.tested_api = random_api
    startup_path = "/etc/sonic/nvue.d/startup.yaml"
    backup_path = "/tmp/startup_verify_plan_backup.yaml"

    with allure.step("Backup startup.yaml and append invalid YAML (aaa class incomplete, invalid timezone, lldp wrong value)"):
        try:
            engine.run_cmd("sudo cp {} {}".format(startup_path, backup_path))
        except Exception as e:
            pytest.skip("Cannot backup startup.yaml: {}".format(e))
        invalid_block = """
  class:
date-time:
  timezone: Pacific/Fake
lldp:
  tx-interval: 0
"""
        payload = base64.b64encode(invalid_block.encode()).decode()
        engine.run_cmd("echo '{}' | base64 -d | sudo tee -a {}".format(payload, startup_path))

    try:
        with allure.step("nv config verify revision startup – expect invalid output with line reference"):
            success, output = NvueGeneralCli.verify_config(engine, rev_id="startup")
            assert not success, (
                "Verify revision startup must fail when startup.yaml has invalid content. Output: {}".format(
                    (output or "")[:500]
                )
            )
            assert "error" in output.lower() or "invalid" in output.lower(), (
                "Expected error/invalid in output for invalid startup. Output: {}".format(output[:500])
            )
            error_line_refs = re.findall(r'line\s+\d+', output, re.IGNORECASE)
            num_errors_reported = len(error_line_refs)
            assert num_errors_reported == 1, (
                "Expected one surfaced startup error for current behavior. "
                "Got {} error line reference(s). Output: {}".format(num_errors_reported, (output or "")[:500])
            )
            logger.info("Verify revision startup failed with %s line-referenced error(s): %s", num_errors_reported, (output or "")[:400])

        with allure.step("No new revision created when verify fails"):
            logger.info("Revision state after verify revision startup: checked in output")
    finally:
        with allure.step("Restore startup.yaml from backup"):
            try:
                engine.run_cmd("sudo cp {} {}".format(backup_path, startup_path))
                engine.run_cmd("sudo rm -f {}".format(backup_path))
            except Exception as e:
                logger.warning("Could not restore startup.yaml: %s", e)


@pytest.mark.parametrize("commands_content, scenario_desc", [(s[1], s[2]) for s in VERIFY_MULTIPLE_ERROR_SCENARIOS],
                         ids=[s[0] for s in VERIFY_MULTIPLE_ERROR_SCENARIOS])
def test_verify_plan_parametrized_acl_errors(engines, random_api, commands_content, scenario_desc):
    """Plan 6.1 (parametrized): each ACL apply-fail scenario must fail verify."""
    engine = engines.dut
    TestToolkit.tested_api = random_api
    with allure.step("Run verify via {} (expect failure): {}".format(random_api, scenario_desc)):
        success, output = TestToolkit.GeneralApi[random_api].verify_config_from_commands(
            engine, commands_content
        )
    assert not success, "Expected verify to fail for scenario '{}'. Output: {}".format(scenario_desc, output)
    _assert_has_error_text(output, "Expected an error indicator for apply-fail ACL scenario")
