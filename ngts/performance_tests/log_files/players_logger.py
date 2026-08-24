"""Process-wide logger recording every command executed on the performance players.

The performance conftest points it at a per-test file; ``PerformanceCommon.execute_cmd``
writes each command it runs to whichever file is current.
"""
import logging
import logging.handlers
import os
import threading


PLAYERS_CMD_LOG_DIR_ENV_VAR = "PLAYERS_CMD_LOG_DIR"
_DEFAULT_PLAYERS_CMD_LOG_DIR = "/tmp/players_commands"
_PLAYERS_CMD_LOG_MAX_BYTES = 10 * 1024 * 1024
_PLAYERS_CMD_LOG_BACKUP_COUNT = 2
PLAYERS_CMD_LOGGER_NAME = "players_commands"
_players_cmd_logger = None
_players_cmd_logger_lock = threading.Lock()


def init_players_cmd_logger(timestamp, log_file_name_prefix):
    """Point the process-wide players-command logger at a new log file.

    Every command later executed on the players (dut / left-tg / right-tg) through
    PerformanceCommon.execute_cmd is appended to
    ``<log_dir>/<log_file_name_prefix>_<timestamp>.log``, until the next call redirects it
    elsewhere. All three player threads write to the file that is current when they run.
    Until this is called the first time, execute_cmd logging is a no-op (nothing is written
    and no file is created).

    ``<log_dir>`` is ``/tmp/players_commands`` unless the ``PLAYERS_CMD_LOG_DIR`` environment
    variable names another directory, so a run whose artifacts are collected from elsewhere
    (or a host with a restricted /tmp) can redirect the logs without a code change.

    A run that never leaves a test cannot fill the disk: the file rotates at
    ``_PLAYERS_CMD_LOG_MAX_BYTES`` and keeps ``_PLAYERS_CMD_LOG_BACKUP_COUNT`` rotated files
    (``<name>.log.1`` ...), capping a single test at the product of the two.

    Each call switches files: the previous handler is closed and replaced, so a caller that
    initializes per test gets one file per test. The timestamp is supplied by the caller so
    the file name lines up with the run's other artifacts. The logger is re-enabled on each
    call; see ``logger.disabled`` below.

    Args:
        timestamp: Identifier placed in the log file name (e.g. a run-start timestamp).
        log_file_name_prefix (str): Log file name prefix, supplied by the caller. Must be safe
            to embed in a file name.

    Returns:
        logging.Logger: The configured logger (its own file handler, propagate disabled so
            these lines do not leak into the per-player stdout redirection).
    """
    global _players_cmd_logger
    with _players_cmd_logger_lock:
        log_dir = os.environ.get(PLAYERS_CMD_LOG_DIR_ENV_VAR, _DEFAULT_PLAYERS_CMD_LOG_DIR)
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, f"{log_file_name_prefix}_{timestamp}.log")
        logger = logging.getLogger(PLAYERS_CMD_LOGGER_NAME)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        # logging.config.dictConfig defaults to disable_existing_loggers=True, so any root-logger
        # reconfiguration after this logger exists switches it off for good. Clear that on every
        # call so a later config_root_logger() cannot silently kill command logging.
        logger.disabled = False
        for previous_handler in logger.handlers[:]:
            logger.removeHandler(previous_handler)
            try:
                previous_handler.close()
            except Exception as close_error:
                # A handler that fails to close must not abort the swap: every remaining
                # handler still has to be detached and closed, or it keeps holding its file
                # descriptor and writing the next test's commands into the previous file.
                logging.warning(f"Failed to close a players-command log handler: {close_error}")
        handler = logging.handlers.RotatingFileHandler(log_path, maxBytes=_PLAYERS_CMD_LOG_MAX_BYTES,
                                                       backupCount=_PLAYERS_CMD_LOG_BACKUP_COUNT)
        handler.setFormatter(logging.Formatter("%(asctime)s [thread_name_%(threadName)s] %(message)s"))
        logger.addHandler(handler)
        _players_cmd_logger = logger
    return _players_cmd_logger


def get_players_cmd_logger():
    """Return the players-command logger, or None if init_players_cmd_logger was not called."""
    return _players_cmd_logger
