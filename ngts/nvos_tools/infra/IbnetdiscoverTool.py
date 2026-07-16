import logging
import re
from typing import TypedDict

from ngts.ngts_types.engines_T import EnginesT
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit

logger = logging.getLogger(__name__)


class PortInfoT(TypedDict):
    port_num: int
    remote_switch_guid: str
    remote_port_num: int


class SwitchInfoT(TypedDict):
    switch_guid: str
    order: int
    ports: list[PortInfoT]


class IbnetdiscoverTool:
    """Static utility class for running and parsing ibnetdiscover output."""

    @staticmethod
    def _normalize_guid(guid: str) -> str:
        """Return an InfiniBand GUID as a lowercase, zero-padded 64-bit value."""
        return f"{int(guid.lower().removeprefix('s-'), 16):016x}"

    @staticmethod
    def run_ibnetdiscover(engines: EnginesT | None = None) -> list[SwitchInfoT]:
        """
        Run ibnetdiscover command and parse the output.

        Args:
            engines: Device engines to run the command on. If None, uses TestToolkit.engines.dut

        Returns:
            list[SwitchInfoT]: List of dictionaries, one per switch containing:
                - switch_guid (str): Switch GUID
                - order (int): Switch order
                - ports (list[PortInfoT]): List of port info, each port contains:
                    - port_num (int): Port number
                    - remote_switch_guid (str): Remote switch GUID
                    - remote_port_num (int): Remote port number
        """
        if engines is None:
            engines = TestToolkit.engines
        assert engines is not None, "TestToolkit engines must be initialized before running ibnetdiscover"

        logger.info('Running ibnetdiscover to discover IB topology')
        output: str = engines.dut.run_cmd('sudo ibnetdiscover', validate=True)
        return IbnetdiscoverTool.parse_output(output)

    @staticmethod
    def parse_output(output: str) -> list[SwitchInfoT]:
        """Parse ``ibnetdiscover`` output into switches and their connected ports."""
        output_switches_list = re.split(r'vendid=.+\ndevid=.+', output)
        switches_list: list[SwitchInfoT] = []
        for switch_output in output_switches_list[1:]:
            # ibnetdiscover can omit a leading zero from the parenthesized GUID while
            # retaining it in S-<guid> references, so normalize both representations.
            m_guid = re.search(r'switchguid=0x[0-9a-f]+\(([0-9a-f]+)\)', switch_output, re.IGNORECASE)
            if m_guid:
                switch_guid = IbnetdiscoverTool._normalize_guid(m_guid.group(1))
            else:
                m_name = re.search(r'Switch\s+\d+\s*"([^"]+)"', switch_output)
                if m_name is None:
                    logger.warning("Could not parse switch GUID or name, skipping block")
                    continue
                switch_guid = IbnetdiscoverTool._normalize_guid(m_name.group(1))
            # /U<n> is in the descriptive quoted value after the S-<guid> switch name.
            m_order = re.search(r'Switch\s+\d+\s*"[^"]+"[^\n]*/U(\d+)"', switch_output)
            if m_order:
                order = int(m_order.group(1))
            else:
                order = len(switches_list) + 1
            tmp_switch = SwitchInfoT(switch_guid=switch_guid, order=order, ports=[])
            logger.info("Parsing ports for switch %s", switch_guid)
            for port_match in re.finditer(r"\[(\d+)\][^\"]*\"S-([^\"]+)\"\s*\[(\d+)\]", switch_output):
                port_info: PortInfoT = {
                    'port_num': int(port_match.group(1)),
                    'remote_switch_guid': IbnetdiscoverTool._normalize_guid(port_match.group(2)),
                    'remote_port_num': int(port_match.group(3)),
                }
                tmp_switch['ports'].append(port_info)
            switches_list.append(tmp_switch)
        switches_list.sort(key=lambda x: x['order'])
        return switches_list
