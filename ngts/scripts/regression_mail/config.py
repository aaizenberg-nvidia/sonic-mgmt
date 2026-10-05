"""Environment-backed configuration for regression mail."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Tuple


@dataclass(frozen=True)
class Settings:
    """Runtime settings that do not expand the public CLI surface."""

    smtp_host: str
    smtp_port: int
    sender: str
    api_base_url: str
    github_token: str
    rc_status_repository: str
    rc_status_ref: str
    rc_status_tag: str
    redmine_api_token: str
    redmine_author_id: int
    http_timeout_seconds: int
    github_authors: Tuple[str, ...]
    gerrit_username: str
    gerrit_api_key: str
    inference_api_key: str
    inference_base_url: str
    inference_model: str
    inference_batch_size: int
    inference_timeout_seconds: int
    repo_root: Path
    log_path: Optional[Path]

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "Settings":
        values = os.environ if env is None else env
        default_repo_root = Path(__file__).resolve().parents[3]
        log_value = values.get("REGRESSION_MAIL_LOG_PATH", "").strip()
        return cls(
            smtp_host=values.get("REGRESSION_MAIL_SMTP_HOST", "mailgw.nvidia.com").strip(),
            smtp_port=_positive_int(values, "REGRESSION_MAIL_SMTP_PORT", 25),
            sender=values.get(
                "REGRESSION_MAIL_SENDER",
                "nbu-system-sw-sonic-ver@exchange.nvidia.com",
            ).strip(),
            api_base_url=values.get(
                "REGRESSION_MAIL_API_BASE_URL",
                "https://regression-report.mec01-asgard.nvidia.com",
            ).rstrip("/"),
            github_token=_secret(
                values,
                "GITHUB_API_TOKEN",
            ),
            rc_status_repository=values.get(
                "REGRESSION_MAIL_RC_STATUS_REPOSITORY",
                "nvidia-sonic/sonic-buildimage",
            ).strip(),
            rc_status_ref=values.get(
                "REGRESSION_MAIL_RC_STATUS_REF",
                "",
            ).strip(),
            rc_status_tag=values.get(
                "REGRESSION_MAIL_RC_STATUS_TAG",
                "",
            ).strip(),
            redmine_api_token=_secret(values, "REDMINE_API_TOKEN"),
            redmine_author_id=_positive_int(
                values,
                "REGRESSION_MAIL_REDMINE_AUTHOR_ID",
                7680,
            ),
            http_timeout_seconds=_positive_int(values, "REGRESSION_MAIL_HTTP_TIMEOUT", 60),
            github_authors=_csv_values(
                values.get("REGRESSION_MAIL_GITHUB_AUTHORS", "")
            ),
            gerrit_username=_secret(values, "SVC_SONIC_VER_BOT"),
            gerrit_api_key=_secret(values, "GERRIT_API_KEY"),
            inference_api_key=_secret(values, "INFERENCE_API_KEY"),
            inference_base_url=values.get(
                "REGRESSION_MAIL_INFERENCE_BASE_URL",
                "https://inference-api.nvidia.com/v1",
            ).rstrip("/"),
            inference_model=values.get(
                "REGRESSION_MAIL_INFERENCE_MODEL",
                "openai/openai/gpt-5.6-sol",
            ).strip(),
            inference_batch_size=_positive_int(
                values,
                "REGRESSION_MAIL_INFERENCE_BATCH_SIZE",
                8,
            ),
            inference_timeout_seconds=_positive_int(
                values,
                "REGRESSION_MAIL_INFERENCE_TIMEOUT",
                120,
            ),
            repo_root=Path(values.get("REGRESSION_MAIL_REPO_ROOT", str(default_repo_root))).resolve(),
            log_path=Path(log_value).expanduser() if log_value else None,
        )


def _positive_int(values: Mapping[str, str], name: str, default: int) -> int:
    raw = values.get(name)
    if raw is None or not str(raw).strip():
        return default
    value = int(raw)
    if value <= 0:
        raise ValueError("{} must be a positive integer".format(name))
    return value


def _secret(values: Mapping[str, str], *names: str) -> str:
    for name in names:
        value = str(values.get(name) or "").strip()
        if value and value.lower() not in {"none", "null"}:
            return value
    return ""


def _csv_values(value: str) -> Tuple[str, ...]:
    result = []
    for item in value.replace("\n", ",").split(","):
        normalized = item.strip()
        if normalized and normalized not in result:
            result.append(normalized)
    return tuple(result)
