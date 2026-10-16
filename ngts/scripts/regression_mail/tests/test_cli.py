from __future__ import annotations

import io
import json
import re
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from ngts.scripts.regression_mail.cli import main, trigger_from_jenkins
from ngts.scripts.regression_mail.config import Settings
from ngts.scripts.upload_regression_results_to_msft.parse_script_params import (
    parse_modify_params,
)


VERSION = "202605_RC.70-2e2faf00ae_Internal"


class CliTest(unittest.TestCase):
    def test_report_env_uses_vault_secrets_not_phase_parameters(self) -> None:
        scripts_root = Path(__file__).parents[2]
        build_root = scripts_root / "upload_regression_results_to_msft" / "build"
        groovy = (build_root / "run_upload_script.groovy").read_text(
            encoding="utf-8"
        )
        self.assertIn('report_requested = command == "collect" ||', groovy)
        self.assertIn(
            'echo "${command} regression mail: recipients=${regression_mail_to}"',
            groovy,
        )
        self.assertNotIn(
            'if (report_requested && command == "modify") {', groovy
        )
        assigned_env = set(
            re.findall(r"report_values\.([A-Z][A-Z0-9_]*)\s*=", groovy)
        )
        self.assertEqual(
            {
                "GITHUB_API_TOKEN",
                "GERRIT_API_KEY",
                "INFERENCE_API_KEY",
                "REDMINE_API_TOKEN",
                "SVC_SONIC_VER_BOT",
                "REGRESSION_MAIL_API_BASE_URL",
                "REGRESSION_MAIL_CC",
                "REGRESSION_MAIL_ENABLED",
                "REGRESSION_MAIL_HTTP_TIMEOUT",
                "REGRESSION_MAIL_INFERENCE_BASE_URL",
                "REGRESSION_MAIL_INFERENCE_BATCH_SIZE",
                "REGRESSION_MAIL_INFERENCE_MODEL",
                "REGRESSION_MAIL_INFERENCE_TIMEOUT",
                "REGRESSION_MAIL_GITHUB_AUTHORS",
                "REGRESSION_MAIL_RC_STATUS_REF",
                "REGRESSION_MAIL_RC_STATUS_REPOSITORY",
                "REGRESSION_MAIL_RC_STATUS_TAG",
                "REGRESSION_MAIL_REDMINE_AUTHOR_ID",
                "REGRESSION_MAIL_SENDER",
                "REGRESSION_MAIL_SMTP_HOST",
                "REGRESSION_MAIL_SMTP_PORT",
                "REGRESSION_MAIL_TO",
            },
            assigned_env,
        )

        config = json.loads(
            (build_root / "build_config.json").read_text(encoding="utf-8")
        )
        properties = "\n".join(config["properties"])
        for forbidden_property in (
            "REGRESSION_MAIL_JENKINS_USER",
            "REGRESSION_MAIL_JENKINS_API_TOKEN",
            "REDMINE_API_TOKEN",
        ):
            self.assertNotRegex(
                properties,
                r"(?m)^{}=".format(re.escape(forbidden_property)),
            )
        self.assertRegex(properties, r"(?m)^INFERENCE_API_KEY=\S+$")
        phase_parameters = config["phases"][0]["phase_1"][-1]["parameters"]
        expected_parameters = {
            "regression_mail_to",
            "regression_mail_github_authors",
            "regression_mail_rc_status_repository",
            "regression_mail_rc_status_ref",
            "regression_mail_rc_status_tag",
        }
        self.assertEqual(
            set(),
            expected_parameters - set(phase_parameters),
        )
        self.assertIn(
            'env."${name}_regression_mail_rc_status_repository"'
            " ?: env.REGRESSION_MAIL_RC_STATUS_REPOSITORY",
            groovy,
        )
        self.assertIn(
            "regression_mail_github_authors = env.regression_mail_github_authors?.trim() ?:"
            '\n              env."${name}_regression_mail_github_authors" ?:'
            "\n              env.REGRESSION_MAIL_GITHUB_AUTHORS",
            groovy,
        )
        for setting in (
            "github_api_token: env.GITHUB_API_TOKEN",
            "redmine_api_token: env.REDMINE_API_TOKEN",
            "gerrit_username: env.SVC_SONIC_VER_BOT",
            "gerrit_api_key: env.GERRIT_API_KEY",
            "inference_api_key: env.INFERENCE_API_KEY",
        ):
            self.assertIn(setting, groovy)
        for forbidden in (
            "regression_mail_jenkins_user",
            "regression_mail_jenkins_api_token",
            "github_api_token",
            "redmine_api_token",
            "inference_api_key",
        ):
            self.assertNotIn(forbidden, phase_parameters)
        for phase_name, global_name in (
            (
                "regression_mail_rc_status_ref",
                "REGRESSION_MAIL_RC_STATUS_REF",
            ),
            (
                "regression_mail_rc_status_tag",
                "REGRESSION_MAIL_RC_STATUS_TAG",
            ),
        ):
            self.assertIn(
                'env."${{name}}_{}" != null ?'.format(phase_name),
                groovy,
            )
            self.assertIn(
                ': env.{}'.format(global_name),
                groovy,
            )
        self.assertIn(
            "values.findAll { key, value -> value != null }",
            groovy,
        )
        self.assertIn("chmod 600", groovy)
        self.assertIn("rm -f '${report_env_file}'", groovy)
        self.assertIn(
            "archiveArtifacts artifacts: artifact_name",
            groovy,
        )
        self.assertIn("readFile(file: file_path)", groovy)
        self.assertIn("table_content + artifact_banner", groovy)
        self.assertNotIn("Tables Details:", groovy)

        secret_template = (
            build_root / "sonic_secret_template.yaml"
        ).read_text(encoding="utf-8")
        for path in (
            "nvidia/nbu/mars/sonic/kv/GitHub",
            "nvidia/nbu/mars/sonic/kv/Redmine",
        ):
            self.assertIn(path, secret_template)
        self.assertNotIn(
            "nvidia/nbu/mars/sonic/kv/inference_api",
            secret_template,
        )

    def test_inference_settings_are_read_from_environment(self) -> None:
        settings = Settings.from_env(
            {
                "INFERENCE_API_KEY": "secret",
                "REGRESSION_MAIL_INFERENCE_BATCH_SIZE": "3",
                "REGRESSION_MAIL_INFERENCE_TIMEOUT": "45",
            }
        )

        self.assertEqual("secret", settings.inference_api_key)
        self.assertEqual(
            "https://inference-api.nvidia.com/v1",
            settings.inference_base_url,
        )
        self.assertEqual("openai/openai/gpt-5.6-sol", settings.inference_model)
        self.assertEqual(3, settings.inference_batch_size)
        self.assertEqual(45, settings.inference_timeout_seconds)

    def test_github_authors_are_read_from_environment(self) -> None:
        self.assertEqual((), Settings.from_env({}).github_authors)
        self.assertEqual(
            ("zypgithub", "nhe-NV"),
            Settings.from_env(
                {
                    "REGRESSION_MAIL_GITHUB_AUTHORS": (
                        "zypgithub,nhe-NV,zypgithub"
                    )
                }
            ).github_authors,
        )
        self.assertEqual(
            "",
            Settings.from_env({"GITHUB_TOKEN": "personal-token"}).github_token,
        )
        self.assertEqual(
            "vault-token",
            Settings.from_env(
                {
                    "GITHUB_API_TOKEN": "vault-token",
                    "GITHUB_TOKEN": "personal-token",
                }
            ).github_token,
        )

    def test_redmine_author_is_configurable(self) -> None:
        settings = Settings.from_env(
            {"REGRESSION_MAIL_REDMINE_AUTHOR_ID": "1234"}
        )

        self.assertEqual(1234, settings.redmine_author_id)

    def test_accepts_only_documented_arguments_and_is_silent(self) -> None:
        captured = {}

        def runner(request, settings):
            captured["request"] = request
            return 0

        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = main(
                [
                    "--excel",
                    "/tmp/results.xlsx",
                    "--version",
                    "202608_RC.17-deadbeef_Internal",
                    "--to",
                    "owner@nvidia.com",
                    "--cc",
                    "reviewer@nvidia.com",
                ],
                runner=runner,
            )

        self.assertEqual(0, result)
        self.assertEqual("", stdout.getvalue())
        self.assertEqual("", stderr.getvalue())
        self.assertEqual(("owner@nvidia.com",), captured["request"].to)
        self.assertEqual(("reviewer@nvidia.com",), captured["request"].cc)

    def test_rejects_header_injection_before_runner(self) -> None:
        called = []
        stderr = io.StringIO()
        with self.assertRaises(SystemExit) as context, redirect_stderr(stderr):
            main(
                [
                    "--excel",
                    "/tmp/results.xlsx",
                    "--version",
                    "202608_RC.17-deadbeef_Internal",
                    "--to",
                    "owner@nvidia.com\nBcc: attacker@example.com",
                ],
                runner=lambda request, settings: called.append(request),
            )
        self.assertEqual(2, context.exception.code)
        self.assertFalse(called)

    def test_rejects_unknown_public_option(self) -> None:
        stderr = io.StringIO()
        with self.assertRaises(SystemExit) as context, redirect_stderr(stderr):
            main(
                [
                    "--excel",
                    "/tmp/results.xlsx",
                    "--version",
                    "202608_RC.17-deadbeef_Internal",
                    "--to",
                    "owner@nvidia.com",
                    "--dry-run",
                ],
            )
        self.assertEqual(2, context.exception.code)


class JenkinsEntrypointTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.excel = Path(self.temp_dir.name) / "results.xlsx"
        self.excel.write_text("test\n", encoding="utf-8")

    def test_runs_in_current_python_process_with_explicit_parameters(self) -> None:
        calls = []

        def runner(request, settings):
            calls.append(request)
            return 0

        result = trigger_from_jenkins(
            version=VERSION,
            recipients=[
                "yanpengz@nvidia.com",
                "yanpengz@nvidia.com",
            ],
            excel_path=self.excel,
            cc=["owner@nvidia.com"],
            runner=runner,
        )

        self.assertEqual(0, result)
        self.assertEqual(1, len(calls))
        request = calls[0]
        self.assertEqual(self.excel, request.excel_path)
        self.assertEqual(VERSION, request.version)
        self.assertEqual(("yanpengz@nvidia.com",), request.to)
        self.assertEqual(("owner@nvidia.com",), request.cc)

    def test_accepts_comma_separated_recipients(self) -> None:
        calls = []
        trigger_from_jenkins(
            VERSION,
            "yanpengz@nvidia.com,owner@nvidia.com",
            self.excel,
            runner=lambda request, settings: calls.append(request) or 0,
        )
        self.assertEqual(
            ("yanpengz@nvidia.com", "owner@nvidia.com"),
            calls[0].to,
        )

    def test_rejects_invalid_recipient(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid recipients"):
            trigger_from_jenkins(
                VERSION,
                ["not-an-email"],
                self.excel,
            )

    def test_rejects_unreadable_excel(self) -> None:
        with self.assertRaisesRegex(ValueError, "not readable"):
            trigger_from_jenkins(
                VERSION,
                "yanpengz@nvidia.com",
                Path(self.temp_dir.name) / "missing.xlsx",
            )

    def test_modify_mail_uses_explicit_excel_and_generation_flag(self) -> None:
        self.assertEqual(
            [
                "--user_excel_table_path",
                "/auto/results.xlsx",
                "--generate_upload_mail",
            ],
            parse_modify_params("true,/auto/results.xlsx,true,false,,"),
        )
        self.assertEqual(
            [
                "--user_excel_table_path",
                "/auto/results.xlsx",
                "--redmine_issues_to_update_path",
                "/auto/skips.json",
            ],
            parse_modify_params(
                "true,/auto/results.xlsx,false,true,/auto/skips.json,"
            ),
        )


if __name__ == "__main__":
    unittest.main()
