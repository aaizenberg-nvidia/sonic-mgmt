import logging
import socket
from typing import Any

from devts.infra.tools.connection_tools.proxy_ssh_engine import ProxySshEngine

from ngts.nvos_tools.infra.EngineAdapterTool import EngineAdapterTool

logger = logging.getLogger(__name__)


class DatabaseTool:

    @staticmethod
    def _run_cmd(engine: ProxySshEngine, cmd):
        """Run command on engine, handling both SSH and serial engines."""
        return EngineAdapterTool.run_cmd(engine, cmd)

    @staticmethod
    def _output_to_text(output: Any) -> str:
        if isinstance(output, tuple):
            output = output[0] if output else ""
        if isinstance(output, (bytes, bytearray)):
            return output.decode()
        return str(output)

    @staticmethod
    def redis_cli_hset(engine: ProxySshEngine, db_num, db_config, param, value):
        cmd = f'redis-cli -n {db_num} hset "{db_config}" "{param}" "{value}"'
        logger.info(f'Running {cmd}')
        return engine.run_cmd(cmd)

    @staticmethod
    def redis_cli_hget(engine: ProxySshEngine, db_num, db_config, param):
        cmd = f'redis-cli -n {db_num} hget "{db_config}" "{param}"'
        logger.info(f'Running {cmd}')
        return engine.run_cmd(cmd)

    @staticmethod
    def redis_cli_eval(engine, db_num, script, keys):
        keys_str = " ".join(f'"{key}"' for key in keys)
        cmd = f'redis-cli -n {db_num} EVAL "{script}" {len(keys)} {keys_str}'
        logger.info(f'Running {cmd}')
        return engine.run_cmd(cmd)

    @staticmethod
    def sonic_db_cli_exists(engine: ProxySshEngine, asic: str, db_name: str, db_config: str) -> bool:
        """ Check if a table exists in the database """
        asic = f"-n {asic} " if asic else ""
        cmd = f'sonic-db-cli {asic}{db_name} exists "{db_config}"'
        logger.info(f"Running {cmd}")
        if not (output := DatabaseTool._output_to_text(DatabaseTool._run_cmd(engine, cmd)).strip()):
            return False
        return output.splitlines()[-1].strip() == "1"

    @staticmethod
    def sonic_db_cli_hset(
        engine: ProxySshEngine,
        asic: str,
        db_name: str,
        db_config: str,
        param: str,
        value: str,
        *,  # force add arguments to be keyword-only
        allow_create: bool = False,
    ) -> str:
        """ Set a value in a table in the database """
        if not allow_create and not DatabaseTool.sonic_db_cli_exists(engine, asic, db_name, db_config):
            raise ValueError(f'Refusing to hset on {db_name}: table "{db_config}" does not exist')

        asic = f"-n {asic} " if asic else ""
        cmd = f'sonic-db-cli {asic}{db_name} hset "{db_config}" "{param}" "{value}"'
        logger.info("Running sonic-db-cli hset on %s (table, field, and value redacted)", db_name)
        return engine.run_cmd(cmd)

    @staticmethod
    def sonic_db_cli_hget(engine: ProxySshEngine, asic, db_name, db_config, param):
        asic = f"-n {asic} " if asic else ""
        cmd = f'sonic-db-cli {asic}{db_name} hget "{db_config}" "{param}"'
        logger.info(f'Running {cmd}')
        return engine.run_cmd(cmd)

    @staticmethod
    def sonic_db_cli_hdel(engine: ProxySshEngine, asic, db_name, db_config, param):
        asic = f"-n {asic} " if asic else ""
        cmd = f'sonic-db-cli {asic}{db_name} hdel "{db_config}" "{param}"'
        logger.info(f'Running {cmd}')
        return engine.run_cmd(cmd)

    @staticmethod
    def sonic_db_cli_get_keys(engine: ProxySshEngine, asic, db_name, grep_str=None):
        asic = f"-n {asic} " if asic else ""
        cmd = f"sonic-db-cli {asic}{db_name} keys \\*"
        if grep_str:
            cmd += f" | grep {grep_str}"
        logger.info(f'Running {cmd}')
        return engine.run_cmd(cmd)

    @staticmethod
    def sonic_db_run_get_keys_in_docker(docker_name, engine: ProxySshEngine, asic, db_name, grep_str=None):
        asic = f"-n {asic} " if asic else ""
        cmd = f'docker exec -it {docker_name} sonic-db-cli {asic}{db_name} keys \\*'
        if grep_str:
            cmd += f" | grep {grep_str}"
        logger.info(f'Running {cmd}')
        return engine.run_cmd(cmd)

    @staticmethod
    def sonic_db_run_hget_in_docker(docker_name, engine: ProxySshEngine, asic, db_name, db_config, param):
        asic = f"-n {asic} " if asic else ""
        cmd = f'docker exec -it {docker_name} sonic-db-cli {asic}{db_name} hget {db_config} {param}'
        logger.info(f'Running {cmd}')
        return engine.run_cmd(cmd)

    @staticmethod
    def sonic_db_cli_hgetall(engine: ProxySshEngine, asic, db_name, table_name):
        try:
            cmd = DatabaseTool._get_hgetall_cmd(asic, db_name, table_name)
            return DatabaseTool._run_cmd(engine, cmd)
        except socket.error as e:
            logger.info('Got "OSError: Socket is closed" - Current engine was also disconnected')
            engine.disconnect()
            return "Action succeeded"

    @staticmethod
    def sonic_db_cli_hgetall_serial(engine: Any, asic, db_name, table_name):
        engine.sendline(DatabaseTool._get_hgetall_cmd(asic, db_name, table_name))

    @staticmethod
    def _get_hgetall_cmd(asic, db_name, table_name):
        asic = f"-n {asic} " if asic else ""
        cmd = f'sonic-db-cli {asic}{db_name} HGETALL {table_name}'
        logger.info(f'Running {cmd}')
        return cmd
