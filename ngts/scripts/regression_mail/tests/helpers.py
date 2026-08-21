from __future__ import annotations

import io
from types import SimpleNamespace
from typing import Any, Callable, Dict, Iterable, Optional

from ngts.scripts.regression_mail.models import ResultRow, SemanticGroup


class HttpResponse(io.BytesIO):
    def __init__(
        self,
        value: bytes = b"",
        headers: Optional[Dict[str, str]] = None,
    ):
        super().__init__(value)
        self.headers = headers or {}

    def __enter__(self) -> "HttpResponse":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


class ScriptedOpenAIClient:
    def __init__(self, handler: Callable[..., str]):
        self.calls = []

        def create(**kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(output_text=handler(**kwargs))

        self.responses = SimpleNamespace(create=create)


def result_row(
    record_id: str,
    test_name: str,
    result: str = "fail",
    testbed: str = "tb",
    message: str = "",
    *,
    excel_row: Optional[int] = None,
    values: Optional[Dict[str, Any]] = None,
) -> ResultRow:
    return ResultRow(
        record_id=record_id,
        excel_row=(
            excel_row
            if excel_row is not None
            else int(record_id.rsplit("-", 1)[-1]) + 1
        ),
        session_id="session-" + record_id,
        mars_key_id="key-" + record_id,
        testbed=testbed,
        test_name=test_name,
        sanitized_testname=test_name,
        result=result,
        message=message,
        topology="t0",
        hwsku="sku",
        values=values if values is not None else (
            {"comments": message} if message else {}
        ),
    )


def semantic_group(
    group_id: str,
    *,
    member_ids: Optional[Iterable[str]] = None,
    test_display: str = "tests/a.py::test_a",
    testbeds: Optional[Iterable[str]] = None,
    comments: str = "",
    internal_comments: str = "",
    ticket_refs: Optional[Iterable[str]] = None,
    redmine_urls: Optional[Iterable[str]] = None,
) -> SemanticGroup:
    return SemanticGroup(
        group_id=group_id,
        member_ids=list(member_ids or (group_id,)),
        test_display=test_display,
        testbeds=list(testbeds or ("SN5600",)),
        comments=comments,
        internal_comments=internal_comments,
        ticket_refs=list(ticket_refs or ()),
        redmine_urls=list(redmine_urls or ()),
    )
