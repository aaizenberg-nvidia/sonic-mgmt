import logging
import time
from collections.abc import Callable

from ngts.nvos_tools.ib.InterfaceConfiguration.nvos_consts import InternalNvosConsts

from .ResultObj import ResultObj

logger = logging.getLogger()


class PollingTool:

    @staticmethod
    def wait_for_ports_state(state, ports, timeout=InternalNvosConsts.DEFAULT_TIMEOUT):
        logging.info("Waiting for ports to reach state '{state}' (timeout: {timeout})".format(
                     state=state, timeout=timeout))

        for port in ports:
            port.interface.wait_for_port_state(state, timeout).verify_result()

        return ResultObj(True, "All ports have reached state '{state}'".format(state=state))

    @staticmethod
    def poll_until[T](  # noqa: PLR0913 - clock/sleep injection keeps polling tests deterministic
        read: Callable[[], T],
        predicate: Callable[[T], bool],
        *,
        timeout_seconds: float,
        description: str,
        interval_seconds: float = InternalNvosConsts.DEFAULT_POLL_INTERVAL_SECONDS,
        acceptable_exceptions: tuple[type[Exception], ...] = (),
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> T:
        """Poll a value until a lifecycle condition is met, returning the last read.

        ``acceptable_exceptions`` raised by ``read`` are transient (mid-reset CLI
        or parse blips): polling continues, and the last one is re-raised with
        context only if the deadline expires without a conclusive read.
        """
        assert timeout_seconds >= 0, "timeout_seconds must be non-negative"
        assert interval_seconds > 0, "interval_seconds must be positive"
        deadline = clock() + timeout_seconds
        last_value: T | None = None
        last_error: Exception | None = None
        while True:
            try:
                last_value = read()
                last_error = None
                if predicate(last_value):
                    return last_value
            except acceptable_exceptions as error:
                last_error = error
            if clock() >= deadline:
                break
            sleep(interval_seconds)
        if last_error is not None:
            raise AssertionError(
                f"Timed out after {timeout_seconds}s waiting for {description}; last read failed: {last_error}"
            ) from last_error
        raise AssertionError(
            f"Timed out after {timeout_seconds}s waiting for {description}; last value: {last_value!r}"
        )
