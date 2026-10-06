# SONiC Regression Report Email Automation

This package generates and sends the internal SONiC regression report from
the existing `upload_regression_results_to_msft` Jenkins collect flow.

The implementation runs entirely in the Jenkins NGTS container. It does not
use OpenCode, MCP, a VDI, or SSH to a personal account. Deterministic Python
collects and desensitizes the report; NVIDIA Inference API summarizes existing
internal comments but does not diagnose failures or invent missing analysis.

## Jenkins execution path

For a `command=collect` build:

1. Jenkins checks out `sonic-mgmt`.
2. The existing job starts the NGTS Docker and injects its runtime environment.
3. `upload_regression_results_to_msft.py` creates the exact Excel workbook.
4. The same Python process calls:

```python
from ngts.scripts.regression_mail import trigger_from_jenkins

exit_code = trigger_from_jenkins(
    version=sonic_version,
    recipients=["owner@nvidia.com"],
    excel_path=excel_table_path,
    cc=[],
)
```

5. The report pipeline reads the workbook and external sources, replaces setup
   names with platform models, batches internal comments through NVIDIA
   Inference API, writes `skips.json`, and sends the mail without an attachment.

The call runs inside `/ngts_venv/bin/python` in the existing NGTS Docker.
No callback service or downstream Jenkins PR-report job is required. Public
pull-request metadata comes directly from GitHub REST using the Vault-managed
bot token.
For every report-producing collect or modify build Jenkins installs
`requirements.txt` into `/ngts_venv`.

The Jenkins job accepts `regression_mail_to` as an explicit runtime parameter.
The Groovy step validates that value and passes it to
`trigger_from_jenkins(..., recipients=...)`; no default recipient is stored in
`build_config.json`. For `modify`, the triggering Jenkins user remains the
recipient when a `UserIdCause` is available.

The optional Jenkins string parameter `regression_mail_github_authors` can
override the comma-separated GitHub login list for a build. When it is absent
or empty, the job uses `REGRESSION_MAIL_GITHUB_AUTHORS` from
`build_config.json`, preserving the existing default behavior.

## Public API

```python
trigger_from_jenkins(
    version,
    recipients,
    excel_path,
    cc=(),
    runner=None,
)
```

Required values:

- `version`: exact SONiC version, for example
  `202605_RC.70-2e2faf00ae_Internal`;
- `recipients`: one email address, a comma-separated string, or a sequence;
- `excel_path`: exact collected-results `.xlsx` path.

The function returns the process exit code. Duplicate recipients are removed.

For the existing Jenkins `modify` command, enable its fourth active parameter
(`generate_upload_mail=true`). The uploader first writes the modified workbook,
then passes that exact output path to `trigger_from_jenkins`; the report no
longer falls back to the collect-flow workbook.

## CLI

The same workflow can be invoked directly in a Jenkins agent or compatible
NGTS environment:

```bash
PYTHONPATH=/root/mars/workspace/sonic-mgmt \
/ngts_venv/bin/python -m ngts.scripts.regression_mail \
  --excel /auto/sw_regression/system/SONIC/release_results/202605_RC.70-2e2faf00ae_Internal/collected_results_202605_RC.70-2e2faf00ae_Internal.xlsx \
  --version 202605_RC.70-2e2faf00ae_Internal \
  --to owner@nvidia.com
```

Normal success is silent. A degraded run sends the best available report,
prints one concise diagnostic to stderr, and returns nonzero.

## Environment

The runtime injects:

- `GITHUB_API_TOKEN`: reads private
  `nvidia-sonic/sonic-buildimage/RC_STATUS.md` and public sonic-mgmt PR data;
- `REDMINE_API_TOKEN`: reads SONIC - Design LogAnalyzer bugs;
- `SVC_SONIC_VER_BOT` and `GERRIT_API_KEY`: optionally read titles for
  explicitly referenced `git-nbu-sw` changes;
- `INFERENCE_API_KEY`: calls
  `https://inference-api.nvidia.com/v1` through the OpenAI Python SDK.

Optional settings:

```text
REGRESSION_MAIL_SMTP_HOST          default: mailgw.nvidia.com
REGRESSION_MAIL_SMTP_PORT          default: 25
REGRESSION_MAIL_SENDER             default: nbu-system-sw-sonic-ver@exchange.nvidia.com
REGRESSION_MAIL_API_BASE_URL       regression-report API base URL
REGRESSION_MAIL_HTTP_TIMEOUT       default: 60 seconds
REGRESSION_MAIL_REPO_ROOT          current sonic-mgmt checkout
REGRESSION_MAIL_LOG_PATH           optional detailed diagnostic log
REGRESSION_MAIL_RC_STATUS_REPOSITORY default: nvidia-sonic/sonic-buildimage
REGRESSION_MAIL_RC_STATUS_REF      optional override; normally leave unset
REGRESSION_MAIL_GITHUB_AUTHORS     required comma-separated GitHub logins
REGRESSION_MAIL_INFERENCE_BASE_URL default: https://inference-api.nvidia.com/v1
REGRESSION_MAIL_INFERENCE_MODEL    default: openai/openai/gpt-5.6-sol
REGRESSION_MAIL_INFERENCE_BATCH_SIZE default: 8 groups
REGRESSION_MAIL_INFERENCE_TIMEOUT  default: 120 seconds
REGRESSION_MAIL_TO                 internal env derived from Jenkins runtime recipient
REGRESSION_MAIL_CC                 optional Jenkins integration CC list
REGRESSION_MAIL_ENABLED            true enables collect-flow integration
```

## Managed credential injection

`build_config.json` contains no personal service account and no plaintext
GitHub or Redmine credential. The existing
`vault-sonic-approle-prod` AppRole injects credentials from:

```text
nvidia/nbu/mars/sonic/kv/GitHub
nvidia/nbu/mars/sonic/kv/Redmine
```

The Jenkins runtime may also provide its existing Gerrit service credentials.
They are copied into the container only when regression mail is requested.

`run_upload_script.groovy` copies only the required runtime values into a
mode-0600 temporary Docker env-file and removes the file in `finally`.
Additional sonic-mgmt PRs are read directly through GitHub REST; no downstream
Jenkins API account or remote CSV SSH access is used.

The optional `nvidia/nbu/mars/sonic/kv/inference_api` path must be added to the
Vault template only after its `api_key` field and AppRole read policy have been
provisioned. Until then AI stages use their deterministic degraded fallback
instead of blocking the job during Vault initialization.

## Deterministic data sources

1. Image branch, public image hash, and image PRs:
   `RC_STATUS.md` from the full requested image tag, or from an explicitly
   configured matching ref. Only `APPLIED` rows whose source is a public
   `sonic-net` pull request are included; internal, skipped, and reverted
   entries are excluded. Cross-release tag overrides are rejected.
2. sonic-mgmt internal/public hashes:
   exact stable patch-ID comparison using the current Jenkins checkout and
   public GitHub history.
3. Coverage and pass rate:
   exact-version Excel rows after record de-duplication. Coverage is
   `(pass + fail) / (pass + fail + issue-skipped)`, where an issue-skipped row
   is a `skipped` row for which the matching source skip/xfail rule evaluates
   true from the Excel facts, the issue reference is required for the true
   condition, and the issue was still open when the test started. A different
   true non-ticket OR branch does not attribute the skip to the issue. Pass rate
   is `pass / (pass + fail)`. Dashboard metric values are not used. Rows marked
   `REMOVE_FAILED_TESTCASE` in `modify_xml` are excluded before all analysis.
4. Engineer analysis:
   Regression Report API.
5. Hardware SKU, topology, failures, skips, and coverage issue URLs:
   selected Excel rows for the exact requested version. The GitHub coverage
   table uses the same condition- and closure-validated issue-skip set as the
   Coverage denominator; merely mentioning an issue in Excel is insufficient.
6. Additional sonic-mgmt PRs:
   public GitHub REST returns the complete open PR inventory for the configured
   NVIDIA author list. Draft PRs and PRs without a label containing the exact
   target release train (for example, `202605`) are excluded. The optional
   `sonic_github_pr_report` job is disabled by default; when explicitly enabled,
   its CSV supplies candidates but the same author, draft, state, and release
   label filters are still enforced from GitHub metadata.
7. Internally detected errors:
   live Redmine REST query followed by batched NVIDIA Inference API log
   sentence-pattern classification and first-source-line retention.
8. Failure comments:
   `Comments` contains a desensitized NVIDIA Inference API summary generated
   in fixed-size batches; `Comments for internal review` preserves the draft
   Dashboard analysis. Verified public references are labeled as `test bug`,
   `test fix`, `production bug`, or `production fix`.

The successful mail follows the agreed template order:

```text
intro and exact hashes/version
Coverage / Pass rate
HardwareSku | Topology | ReportId
Failure Analysis
The following tests were skipped over bugs found internally
Github issues that affects tests coverage
Err msgs detected internally
image PRs
sonic-mgmt PRs
Regards,
```

The leading table headers remain exactly `Test` / `Testbed`,
`Test case name` / `Skipped platform or topo`, `Github Issue` /
`Skip condition` / `Related test case`, and `Err msgs` / `Testbeds` /
`Test case`. Hardware rows are ordered case-insensitively by HW SKU and then
topology.
`Comments for internal review` is appended as the red internal-only column.
When a Testbed/Testbeds cell contains multiple values, each value is rendered
on its own line.
All dynamic values in the introductory hash/version/coverage/pass-rate text
are rendered bold in black before the first table.
Every section/table title uses bold 11-point Aptos text with exactly two blank
lines before it. Table cells use 11-point fonts matching the reference email.
The hardware table uses light blue `#CAEDFB` headers and `#F5FBFE` body rows.
Failure Analysis, skipped tests, GitHub coverage, and internal errors use
`#C0E6F5` headers and `#F2F9FC` body rows. Body rows do not alternate colors.
The `Comments for internal review` header and warning cells remain red.

By default no PR-report Jenkins build is started. GitHub is queried directly
for all open PRs, and the configured NVIDIA author list, non-draft state, and
target-release label are enforced deterministically. If
`REGRESSION_MAIL_USE_JENKINS_PR_REPORT=true` is set, the shared job runs and its
fixed CSV is read immediately after completion; a missing or stale CSV falls
back to the same direct GitHub inventory and filters.

## Dashboard matching

Engineer analysis is joined to Excel failures by:

```text
session_id + mars_key_id + canonical test name without parameters
```

This deterministic join handles setup-specific Excel parameters and the
Dashboard's generalized parameters without fuzzy matching. Dashboard metric
queries use the exact `SONiC.<version>` value required by the API.
Only non-empty Dashboard `analysis` is summarized. A red internal-review
message distinguishes API retrieval failure, no matching Dashboard record,
and a matching record whose `analysis` is empty. These last two states render
as the red literals `not found in dashboard` and `not analyzed`; no diagnosis
is fabricated.

Before AI submission, all known setup names are replaced with their Excel
`platform` value. AI output is accepted only as an exact `group_id -> summary`
JSON mapping, then scanned again for setup names, Redmine URLs, and ticket
numbers. The accepted summary replaces only `Comments`; the draft analysis is
retained in `Comments for internal review`.

## Failure and skip folding

- failure and skip rows normally use the same cause-driven folding algorithm;
- `The following tests were skipped over bugs found internally` renders only
  skip groups carrying a Redmine issue reference; its public comment starts as
  `Internal issue, WIP`, and internal review contains the Redmine project,
  tracker, ticket number, subject, assignee, status, and ticket URL;
- `Github issues that affects tests coverage` renders only skipped Excel rows
  whose message or comment contains a public SONiC GitHub issue URL;
- an explicit Redmine ticket or public GitHub issue is the highest-priority
  failure or skip key. The same ticket is rendered in one row even when its
  tests span multiple Python files; different tickets never share a row;
- failures and skips without a ticket first use exact normalized
  comment/message evidence across all Python files; failure evidence may then
  use an AI-confirmed root-cause key across files;
- Excel `ERR`/`ERROR` evidence is compared by a desensitized structural
  signature: timestamp/host prefixes and IP addresses are removed, while
  numeric/hex runtime values receive context-specific placeholders. Thus otherwise identical
  exceptions from different devices, times, IPs, or returned values share one
  cause group;
- Dashboard analysis is used as grouping evidence only when the Excel row has
  no comment/message, so a generic Dashboard note cannot merge unrelated Excel
  failures;
- Dashboard status never acts as a cause or a grouping boundary. Cases with the
  same root cause may merge across `analyzed`, `no_analysis`, and `not_found`;
  the merged row receives the corresponding partial-analysis warning;
- after conservative structural pre-grouping, AI compares cause evidence across
  Python files. It assigns a byte-identical cause key only when exception/error
  class, failing operation, and terminal reason identify the same underlying
  error. Runtime values, wording differences, and file boundaries are ignored,
  but ambiguous groups remain unique. Parameter/class/file title folding runs
  only after this semantic cause match;
- exact matching Dashboard internal-change/Redmine references are strong cause
  keys.
  Other long Excel errors are script-filtered into signal windows around
  exceptions, failures, stderr/stdout, assertions, and terminal messages;
  oversized windows are split with overlap instead of hard-truncated. A first
  AI pass extracts root-cause facts from every chunk, and a second global AI
  pass clusters those facts. Insufficient evidence remains unclassified;
- when no Dashboard status is available and cause evidence is empty, parameter
  variants of the same canonical failure testcase still merge;
- comments, internal comments, tickets, and platforms are de-duplicated in the
  resulting single row; distinct merged comment blocks use a visible dashed
  separator;
- parameters of the same grouped test display as
  `test_name[param1, param2]`;
- multiple grouped cases in one Python class display as
  `tests/.../test_file.py::ClassName`;
- multiple grouped classes in one file display as the relative
  `tests/.../test_file.py` path; multiple module-level cases also fold to that
  single file path;
- one root-cause row may contain several Python files. Each file is folded
  independently by the preceding rules and occupies its own line; different
  tickets or different root-cause keys never share a row;
- source analysis accepts literal `pytest.mark.skip`, `pytest.mark.skipif`,
  `pytest.mark.xfail`, or `pytest.skip` reasons found in the matching test
  function, plus applicable `tests_mark_conditions*.yaml` rules;
- a ticket found in Python source is attached only when the workbook row carries
  that same ticket. Conditional-mark tickets may also be attached when their
  restricted boolean condition can be evaluated from the row's platform, HW SKU,
  topology, testbed, version, or ASIC facts. Unsupported expressions are not
  executed or guessed;
- when every member of a ticket group was selected by conditional-mark YAML, the
  YAML selector is used as the displayed test scope, so a file-level rule is
  rendered as `path/test_file.py` instead of an observed parameter variant;
- dynamic fixture outcomes or non-literal Python expressions are not guessed.

## Redmine LogAnalyzer search

The REST query is restricted before full ticket bodies are fetched:

```text
project_id: 9712 (SONIC - Design)
tracker_id: 28 (Bug SW)
author_id: 7680 (LogAnalyzer User)
status: Redmine open category OR status 11 (Won't fix)
updated_on: on or after a conservative branch cutoff
```

A candidate is included only when:

- `Detected In Version` or the initial description explicitly contains the
  exact requested RC version such as `202605_RC.70`; or
- one journal explicitly says the issue was reproduced/detected/observed on
  that exact RC version.

A broad branch-only value such as `202605_RC` or a different minor RC such as
`202605_RC.69` is rejected.

Test cases and testbeds for an old ticket reproduced on the current branch
are extracted from the same qualifying journal, not from the old occurrence.
Missing fields remain blank. `Won't fix` remains eligible even if
`closed_on` is populated.

`Err msgs detected internally` never uses the Redmine ticket title as its
public error text. Candidate `ERR`/`ERROR` log lines are extracted
from the initial ticket description, which defines the tracked problem. If the
description has no usable error lines, the exact requested-RC occurrence is
used as a fallback. Other historical journals are excluded. The qualifying
RC journal still supplies testcases, testbeds, and version evidence.
Every distinct source error sentence structure is retained. After deterministic
structural pre-deduplication, one AI request maps every candidate line
to the earliest equivalent source line ID from the same Redmine ticket; AI
never supplies the displayed text. Invalid, forward, cross-ticket, or invented
representative IDs are rejected. Repeated
instances that differ only by timestamp, device/process instance, address,
port, returned value, or another runtime field share a representative, and
that first source line is displayed. Evidence that cannot fit one global
request uses the deterministic fallback and marks the report degraded instead
of silently losing cross-batch matching.
Dependent symptoms and traceback-bearing errors remain separate when their
operation or terminal reason differs.
Fragments that merely contain the words `ERR`/`ERROR` but have no syslog-style
component separator are rejected. Redmine prose labels
such as `Expected behavior` are removed when they trail a captured log line.
Every displayed line begins at its
`ERR`/`ERROR` severity token; preceding timestamps and device/host names plus
standalone traceback lines are omitted. `#012` traceback continuations remain
attached to their originating ERR line. No display placeholder is generated:
the first line's original IP, port, PID, interface, returned value, object ID,
line number, URLs, ticket references, and other runtime values are retained.
Only known setup names are replaced in public error text.
The pipeline uses internal structural signatures only for matching; those
signatures are never rendered. All evidence carrying the same exact Redmine
ticket folds into one row, even when it names different Python files or distinct
ERR symptoms. Groups without a ticket fold only when their structural root-cause
signature matches. Different tickets always remain in different rows. Within a
row, testcases use the same parameter/class/file folding rules as Failure and
Skip; separate Python files and testbeds are listed on separate lines.

When failure groups are folded, internal-review text is split on the visible
separator before de-duplication. This prevents a repeated analysis or ticket
URL from reappearing when one source contains a subset of another source's
comment blocks.

The cutoff is the earlier of the release-month boundary and the internal
release branch's merge-base date with `origin/develop`. This may include extra
candidates, but avoids missing a valid ticket because Git does not store an
exact branch-creation timestamp.

## Generated artifacts

The pipeline writes next to the source Excel:

```text
skips.json
```

It maps every Redmine ticket URL from skipped rows to the same fixed value:

```text
Internal issue, WIP
```

`skips.json` is written atomically with mode `0664`.

The source workbook is never modified or attached to the email. For rendered
`Testbed`/`Testbeds`, only the DUT portion is replaced and the topology suffix
is retained, for example `private-dut-t1-lag` becomes
`x86_64-platform-r0-t1-lag`. Excel and Dashboard setup maps are combined so
Redmine-derived testbeds are covered. If no unambiguous platform mapping is
available, the original testbed/setup name is retained rather than displaying
an unknown placeholder. Rendered mail parts are scanned before delivery; a
failed body scan falls back to a minimal message.

## Error and exit behavior

Every generation stage is isolated. Excel/API/Git/GitHub/Redmine/source/AI
failures are rendered in `Generation Errors`; the best available email is
still sent. If AI is unavailable, deterministically desensitized Dashboard
analysis is used for `Comments`, while the draft internal-review value remains
unchanged; deterministic structural matching keeps the first Redmine source
line for each `Err msgs` pattern. The library returns an external-source failure
code, but the parent upload step treats a successfully delivered degraded
report as a warning.

Only these failures prevent delivery:

- invalid recipients;
- inability to construct a minimal MIME message;
- SMTP failure.

Exit codes:

```text
0  success
2  invalid CLI arguments
3  invalid input or deterministic source-analysis/artifact failure
4  external source failure
6  delivery failure
```

The Jenkins collect step treats exit `6` as a delivery failure. Other nonzero
results mean a degraded email was delivered and are logged as warnings.

## Verification

From the repository root:

```bash
python3 -m unittest discover \
  -s ngts/scripts/regression_mail/tests \
  -p 'test_*.py'
```

The tests cover the CLI, local Jenkins entry point, workbook filtering and
artifacts, setup-to-platform zero-leak validation, class/file folding, AI
batching and strict response validation, Dashboard matching, GitHub/RC_STATUS
REST authentication, Python and conditional-YAML skip extraction, ticket-scoped
skip regrouping, Redmine ticket metadata and branch/status/journal filtering,
degraded delivery, and SMTP failure behavior.
