from __future__ import annotations

from types import FrameType
from pathlib import Path
import contextlib
import functools
import logging
import inspect
import allure
import sys

if sys.version_info < (3, 11):
    # ExceptionGroup is not built-in before Python 3.11
    from exceptiongroup import ExceptionGroup

from ngts.nvos_tools.infra import ExceptionTool

logger = logging.getLogger(__name__)

_CALLER_NAME_RECORD_ATTRIBUTE = "_allure_step_caller_name"
_THIS_FILE = Path(__file__).resolve()
_NGTS_ROOT = _THIS_FILE.parents[2]
_THIS_LOGGER_NAME = __name__

orig_allure = allure
dynamic = orig_allure.dynamic
AttachmentType = orig_allure.attachment_type

_allure_step_stack = []  # push/pop items when enter/exit allure step. each item is a dict of failed sub-steps.


class _AllureStepCallerNameFilter(logging.Filter):
    """Apply a caller logger name supplied by the step logging helper."""

    FILTER_NAME = "_allure_step_caller_name_filter"

    def filter(self, record: logging.LogRecord) -> bool:
        caller_name = record.__dict__.pop(_CALLER_NAME_RECORD_ATTRIBUTE, None)
        if caller_name:
            record.name = caller_name
        return True


def _safe_resolve(path: str) -> Path | None:
    try:
        return Path(path).resolve()
    except Exception:
        return None


@functools.lru_cache(maxsize=1024)  # entries, not bytes
def _get_ngts_logger_name(filename: str) -> str | None:
    """Return a stable repo-relative logger name for a source file under ngts."""
    path = _safe_resolve(filename)
    if path is None:
        return None

    try:
        relative_path = path.relative_to(_NGTS_ROOT)
    except ValueError:
        return None

    return ".".join(("ngts", *relative_path.with_suffix("").parts))


def _get_caller_logger_name(frame: FrameType) -> str:
    """Resolve the caller name, preferring a correctly configured module logger."""
    module_name = frame.f_globals.get("__name__", "")
    module_logger = frame.f_globals.get("logger")

    if (
        isinstance(module_logger, logging.Logger) and
        module_logger.name != "root" and
        module_logger.name == module_name and
        module_logger.name.startswith("ngts.")
    ):
        return module_logger.name

    if ngts_logger_name := _get_ngts_logger_name(frame.f_code.co_filename):
        return ngts_logger_name

    if module_name and module_name != "__main__":
        return module_name

    return Path(frame.f_code.co_filename).stem


def _is_internal_frame(frame: FrameType) -> bool:
    module_name: str = frame.f_globals.get("__name__", "")
    return module_name == _THIS_LOGGER_NAME or module_name == "contextlib"


def _get_caller_context() -> tuple[int, str]:
    """
    Calculate the logging stacklevel and logger name for the external step caller.

    The stacklevel points logging at the test line:

        with allure.step("..."):
    """

    if (frame := inspect.currentframe()) is None:
        return 1, _THIS_LOGGER_NAME

    try:
        frame = frame.f_back
        stacklevel = 1

        while frame:
            if not _is_internal_frame(frame):
                return stacklevel, _get_caller_logger_name(frame)

            frame = frame.f_back
            stacklevel += 1

        return 1, _THIS_LOGGER_NAME

    finally:
        del frame


def print_step_log_info(log_info_msg: str) -> None:
    """Print log message using the caller's file/line/logger name."""

    stacklevel, caller_name = _get_caller_context()
    logger.info(
        log_info_msg,
        stacklevel=stacklevel,
        extra={_CALLER_NAME_RECORD_ATTRIBUTE: caller_name},
    )


@contextlib.contextmanager
def step(step_msg):
    """
    @summary:
        Context manager that wraps allure step context and a log with the same message
    @param step_msg: The desired step message
    """
    with _step(step_msg, independent=False) as allure_step_context:
        yield allure_step_context


@contextlib.contextmanager
def independent_step(step_msg):
    """
    @summary:
        Like `step`, but if it fails then following steps will still run, and when the parent step finishes then an
        ExceptionGroup will be raised with all failures.
    @param step_msg: The desired step message
    """
    if not _allure_step_stack:
        raise Exception("Error calling allure.independent_step: an independent step must be placed inside a normal "
                        "allure.step, because we test for independent-step failure only when the parent step finishes")
    with _step(step_msg, independent=True) as allure_step_context:
        yield allure_step_context


@contextlib.contextmanager
def _step(step_msg, independent=False):
    error = None

    try:
        with allure.step(step_msg) as allure_step_context:
            print_step_log_info(f'Step start: {step_msg}')
            _allure_step_stack.append({})
            try:
                yield allure_step_context
            except Exception as e:
                error = e
                raise
            else:
                errors = _allure_step_stack[-1]
                if errors:
                    failure_message = (f"{len(errors)} sub-steps failed:\n" + "\n".join(
                        "  " + msg + ":\n    " + ExceptionTool.format_exception(err) for msg, err in errors.items()))
                    error = ExceptionGroup(failure_message, list(errors.values()))
                    ExceptionTool.log_exception(error)
                    raise error
            finally:
                _allure_step_stack.pop(-1)
                print_step_log_info(f'Step end [{"FAIL" if error else "SUCCESS"}]: {step_msg}')
    except Exception as e:
        if independent:
            _allure_step_stack[-1][step_msg] = e
        else:
            raise


def attach(title: str, msg: str | None = None, attachment_type: AttachmentType = AttachmentType.TEXT, log: bool = True) -> None:
    if msg is None:
        log_msg = title
        msg = title
    else:
        log_msg = f"{title}: {msg}"

    if log:
        print_step_log_info(log_msg)
    orig_allure.attach(str(msg), str(title), attachment_type)


if not any(getattr(log_filter, "FILTER_NAME", None) == _AllureStepCallerNameFilter.FILTER_NAME for log_filter in logger.filters):
    logger.addFilter(_AllureStepCallerNameFilter())
