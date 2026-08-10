"""Sample `nv show` outputs for Gen2 CPO, mirroring CPO_HLD.md section 2.3.1.3.

These are the JSON-mode equivalents of the HLD's human-readable samples, used
as offline parser fixtures. Shapes and field names
follow the HLD; values are representative. Generated for a four-ASIC Portia
SIMX test topology (4 CPOs, 4 OEs + 1 ELS each, 64 channels, 16 lasers).
"""

from ngts.nvos_tools.Devices.cpo.CpoTopology import CpoTopology

TOPOLOGY = CpoTopology(cpo_count=4)

# 2 ports per CPO in this fixture; the real port count comes from the DUT and is
# only cross-checked (cpo_to_ports vs port_to_cpo), never asserted as a constant.
_PORTS_PER_CPO = {
    cpo: [f"sw{(idx - 1) * 8 + 1}p1s1", f"sw{(idx - 1) * 8 + 1}p1s2"]
    for idx, cpo in enumerate(TOPOLOGY.cpo_names(), start=1)
}


def _channel_entry() -> dict:
    return {
        "rx-power": {"power": "-1.0 dBm", "alarm-status": "off", "alarm-severity": "none"},
        "tx-power": {"power": "-0.6 dBm", "alarm-status": "off", "alarm-severity": "none"},
        "rx-los": "False",
        "tx-los": "False",
        "tx-fault": "False",
        "laser-source-input-power": {
            "power": "-1.0 dBm",
            "alarm-status": "off",
            "alarm-severity": "none",
        },
        "advanced-fault-opcode": 0,
        "dp-state": "Initialized",
    }


def _oe_entry(serial_suffix: int) -> dict:
    return {
        "identifier": "OE 16x",
        "serial-number": f"MT2219FT0317{serial_suffix:02d}",
        "temperature": "36.8C",
    }


def _cpo_thresholds() -> dict:
    """Keyed by measured value, each with its own four bounds."""
    return {
        "laser-source-input-power": {
            "high-alarm": "5.0 dBm",
            "low-alarm": "-14.0 dBm",
            "high-warning": "4.0 dBm",
            "low-warning": "-12.0 dBm",
        },
        "rx-power": {
            "high-alarm": "3.0 dBm",
            "low-alarm": "-10.0 dBm",
            "high-warning": "2.0 dBm",
            "low-warning": "-8.0 dBm",
        },
        "tx-power": {
            "high-alarm": "3.0 dBm",
            "low-alarm": "-8.0 dBm",
            "high-warning": "2.0 dBm",
            "low-warning": "-6.0 dBm",
        },
    }


def _oe_container(cpo: str) -> dict:
    return {oe: _oe_entry(i) for i, oe in enumerate(TOPOLOGY.oes_for_cpo(cpo), start=1)}


def make_cpo_summary(cpo: str, ports: list[str] | None = None) -> dict:
    """One CPO's entry in `nv show platform cpo` (summary).

    The OE container is what carries OE membership - there is no OE mapping
    field.
    """
    return {
        "fw-version": "42.40.15",
        "ports": ", ".join(ports if ports is not None else _PORTS_PER_CPO[cpo]),
        "laser-sources": ", ".join(TOPOLOGY.els_for_cpo(cpo)),
        "optical-engines": _oe_container(cpo),
    }


def make_cpo_detail(cpo: str, ports: list[str] | None = None) -> dict:
    """`nv show platform cpo <cpo-id>` (HLD 'nv show platform cpo cpoN', Portia).

    CPO status is up/down (the HLD's Inserted is stale); the ELS keeps
    Inserted/Removed.
    """
    return {
        "status": "up",
        "error-status": "N/A",
        "identifier": "CPO Virtual Module",
        "fw-version": "42.40.15",
        "ports": ", ".join(ports if ports is not None else _PORTS_PER_CPO[cpo]),
        "laser-sources": ", ".join(TOPOLOGY.els_for_cpo(cpo)),
        "thresholds": _cpo_thresholds(),
        "optical-engines": _oe_container(cpo),
        "channels": {ch: _channel_entry() for ch in TOPOLOGY.channel_names()},
    }


def _laser_entry() -> dict:
    return {
        "enabled": "true",
        "oper-status": "up",
        "error-status": "N/A",
        "ramping-status": "on",
        "power-restriction": "off",
        "laser-age": "50%",
        "target-output-power": "1.50mW",
        "laser-mpd-current": "0.12mA",
        "laser-bias-current": {
            "current": "12.2 mA",
            "alarm-status": "off",
            "alarm-severity": "none",
            "threshold": {
                "high-alarm": "14.0 mA",
                "low-alarm": "8.0 mA",
                "high-warning": "13.0 mA",
                "low-warning": "9.0 mA",
            },
        },
        "tec-current": "55mA",
        "tec-voltage": "2.9V",
        "laser-temperature": "23.5C",
        "laser-health": "25 mV",
        "tec-health": "15 mV",
        "frequency-error": "2.3 GHz",
        "tx-power": {"power": "32 mW", "alarm-status": "off", "alarm-severity": "none"},
    }


def make_laser_source_detail(els: str) -> dict:
    """`nv show platform laser-source <els-id>` (HLD Portia sample)."""
    return {
        "diagnostics-status": "Diagnostic Data Available",
        "status": "Inserted",
        "error-status": "N/A",
        "vendor-date-code": "220505",
        "identifier": "ELS",
        "vendor-name": "NVIDIA",
        "vendor-rev": "A4",
        "vendor-pn": "CPO-800G-2x400G",
        "vendor-sn": "MT2443FT01035",
        "fw-version": "42.40.15",
        "parent": TOPOLOGY.cpo_for_els(els),
        "temperature": {
            "temperature": "28.00 C",
            "high-alarm-threshold": "80.00 C",
            "high-warning-threshold": "60.00 C",
        },
        "power-consumption": "3.52W",
        "icc-current": "122mA",
        "threshold": {
            "warning": {"tx-power-upper": "0.0 dBm", "tx-power-lower": "-3.0 dBm"},
            "alarm": {"tx-power-upper": "1.0 dBm", "tx-power-lower": "-5.0 dBm"},
        },
        "laser": {laser: _laser_entry() for laser in TOPOLOGY.laser_names()},
    }


def make_interface_cpo(cpo: str, oe: str, channels: list[str]) -> dict:
    """`nv show interface <port> cpo` for a Portia CPO trunk subport.

    The header is inherited AS-IS from the parent CPO (full associated-*
    lists); only the oe/channel blocks are the port's slice.
    """
    parent_detail = make_cpo_detail(cpo)
    sliced = ("optical-engines", "channels")
    return {
        "parent": cpo,
        **{field: parent_detail[field] for field in parent_detail if field not in sliced},
        "optical-engines": {oe: _oe_entry(1)},
        "channels": {ch: _channel_entry() for ch in channels},
    }


# `nv show platform cpo` (summary) - keyed by CPO name
SHOW_PLATFORM_CPO = {cpo: make_cpo_summary(cpo) for cpo in TOPOLOGY.cpo_names()}

# `nv show platform cpo <cpo-id>` for every CPO - keyed by CPO name
SHOW_PLATFORM_CPO_DETAIL = {cpo: make_cpo_detail(cpo) for cpo in TOPOLOGY.cpo_names()}

# `nv show platform laser-source` (summary) - keyed by ELS name
SHOW_PLATFORM_LASER_SOURCE = {
    els: {
        "identifier": "ELS x16",
        "vendor-name": "NVIDIA",
        "vendor-pn": f"MCP7Y60-H01{chr(ord('A') + i)}",
        "vendor-sn": f"MT2233VS0222{i + 1}",
        "vendor-rev": "A4",
        "fw-version": "42.40.15",
    }
    for i, els in enumerate(TOPOLOGY.els_names())
}

SHOW_PLATFORM_LASER_SOURCE_DETAIL = {els: make_laser_source_detail(els) for els in TOPOLOGY.els_names()}

SHOW_INTERFACE_CPO_SW9P1S1 = make_interface_cpo(cpo="cpo2", oe="oe6", channels=["channel-17", "channel-18"])

# port -> parent cpo, as reported by `nv show interface <port> cpo` per port
PORT_TO_CPO = {port: cpo for cpo, ports in _PORTS_PER_CPO.items() for port in ports}

# `nv show fae system cpo els-initialization` (Portia: 5 steps)
SHOW_FAE_ELS_INITIALIZATION = {
    els: {
        "fiber-check": "failed",
        "laser-tuning": "N/A",
        "laser-up": "N/A",
        "laser-fine-tune": "failed",
        "power-setpoint": "failed",
    }
    for els in TOPOLOGY.els_names()
}

# `nv show fae system cpo els-initialization-per-laser` (per-laser breakdown).
# NOTE: per the HLD sample this output keys lasers as laser1..laser16 (no dash),
# UNLIKE the laser-source show / activate action which use laser-1..laser-16.
# Pin the real key format on the first DUT run.
SHOW_FAE_ELS_INITIALIZATION_PER_LASER = {
    els: {
        "els-initialization": {
            f"laser{i}": {
                "fiber-check": "failed",
                "fiber-tuning": "N/A",
                "laser-up": "not-reached",
                "laser-fine-tune": "failed",
                "power-setpoint": "failed",
                "error": "Operation failed - No specified reason",
            }
            for i in range(1, TOPOLOGY.lasers_per_els + 1)
        }
    }
    for els in TOPOLOGY.els_names()
}

# `nv show fae system cpo`
SHOW_FAE_SYSTEM_CPO = {"cpo-dump-state": "enabled"}

# `nv show system events` after one cpo1 reset cascading to els1 (HLD sample).
# Literal wording on purpose: catches drift in the Cpov2Consts event strings.
# Envelope pinned from a live NVL6 (rosalind-mec-2164, 25.03.0442): numeric
# string keys; 'time-created' carries a timezone suffix; 'table-size' is a
# STRING while 'table-occupancy' is an INT.
SHOW_SYSTEM_EVENTS_CPO_RESET = {
    "1": {
        "severity": "INFORMATIONAL",
        "resource": "els1",
        "text": "Laser source was ejected",
        "time-created": "2026-07-19 10:00:01 IDT",
    },
    "2": {
        "severity": "INFORMATIONAL",
        "resource": "cpo1",
        "text": "CPO was ejected",
        "time-created": "2026-07-19 10:00:01 IDT",
    },
    "3": {
        "severity": "INFORMATIONAL",
        "resource": "els1",
        "text": "Laser source was inserted",
        "time-created": "2026-07-19 10:00:24 IDT",
    },
    "4": {
        "severity": "INFORMATIONAL",
        "resource": "cpo1",
        "text": "CPO was inserted",
        "time-created": "2026-07-19 10:00:26 IDT",
    },
    "table-size": "1000",
    "table-occupancy": 4,
}

# `nv show system events` fault and 'Cleared: ' recovery wording (HLD samples)
SHOW_SYSTEM_EVENTS_CPO_FAULTS = {
    "5": {
        "severity": "WARNING",
        "resource": "cpo1",
        "text": "HW Component health is not ok: Bad or unsupported EEPROM",
        "time-created": "2026-07-19 11:00:00",
    },
    "6": {
        "severity": "INFORMATIONAL",
        "resource": "cpo1",
        "text": "Cleared: HW Component health is not ok: Bad or unsupported EEPROM",
        "time-created": "2026-07-19 11:00:30",
    },
    "7": {
        "severity": "WARNING",
        "resource": "els1",
        "text": "ELS Operational State is not ok: Laser 1 Down with Fault",
        "time-created": "2026-07-19 11:01:00",
    },
    "8": {
        "severity": "INFORMATIONAL",
        "resource": "els1",
        "text": "Cleared: ELS Operational State is not ok: Laser 1 Down with Fault",
        "time-created": "2026-07-19 11:01:30",
    },
}

# `nv show system events` per-port link-up publications, wording and format
# captured verbatim from a live NVL6 (rosalind-mec-2164, 25.03.0442); these
# events are the link-up timing source. Event 540 is a synthetic re-link of
# acp158 to cover earliest-wins.
SHOW_SYSTEM_EVENTS_PORT_UP = {
    "530": {
        "resource": "acp158",
        "severity": "INFORMATIONAL",
        "text": "Interface operational state is up",
        "time-created": "2026-07-21 09:03:13 IDT",
    },
    "531": {
        "resource": "acp85",
        "severity": "INFORMATIONAL",
        "text": "Interface operational state is up",
        "time-created": "2026-07-21 09:03:13 IDT",
    },
    "540": {
        "resource": "acp158",
        "severity": "INFORMATIONAL",
        "text": "Interface operational state is up",
        "time-created": "2026-07-21 09:05:02 IDT",
    },
    "table-size": "1000",
    "table-occupancy": 3,
}

# `nv show system health component cpo|laser-source` (HLD Portia sample shape).
# Instance-entry shape confirmed on a live NVL6 (rosalind-mec-2164, 25.03.0442)
# for asic/cpu/software: 'unhealthy-count' is a STRING, 'last-unhealthy' is ""
# when never unhealthy and '%Y-%m-%d %H:%M:%S' otherwise.
SHOW_SYSTEM_HEALTH_COMPONENT_CPO = {
    "cpo": {
        "instance": {
            cpo: {"state": "HEALTHY", "last-unhealthy": "", "unhealthy-count": "0"} for cpo in TOPOLOGY.cpo_names()
        }
    },
    "laser-source": {
        "instance": {
            els: {"state": "HEALTHY", "last-unhealthy": "", "unhealthy-count": "0"} for els in TOPOLOGY.els_names()
        }
    },
}

# `nv show interface <port> link` captured verbatim from a live NVL6 access
# port (rosalind-mec-2164, N6100_LD, 25.03.0442, acp1).
SHOW_INTERFACE_ACP_LINK_NVL6 = {
    "auto-negotiate": "enabled",
    "connection-mode": "ndr",
    "diagnostics": {},
    "fec": "octal-fec",
    "lanes": "2X",
    "logical-state": "Initialize",
    "max-supported-mtu": 256,
    "mtu": 256,
    "op-vls": "VL0-VL7",
    "phy": {},
    "physical-state": "LinkUp",
    "plr": {
        "margin-threshold": 0,
        "mode": "cs-and-crc",
        "reject-mode": "rejection-based-on-crc-and-cs",
    },
    "speed": "328G",
    "state": {"up": {}},
    "supported-lanes": "1X,2X",
    "supported-speed": "200G,328G",
    "vl-capabilities": "VL0-VL7",
}

# `nv show interface <port> link` captured verbatim from a live NVL5 trunk
# subport (juliet-126, N5112_LD, 25.03.0594, sw7p1s1). NVL5 vs the NVL6
# capture above: adds low-power + maintenance-state, drops fec +
# connection-mode; trunk and acp link shapes are identical on NVL5 (only
# speed differs: trunk subports 200G, acp 400G).
SHOW_INTERFACE_SW_LINK_NVL5_UP = {
    "auto-negotiate": "enabled",
    "diagnostics": {},
    "lanes": "2X",
    "logical-state": "Initialize",
    "low-power": {"state": "disabled"},
    "maintenance-state": "Up",
    "max-supported-mtu": 256,
    "mtu": 256,
    "op-vls": "VL0-VL7",
    "phy": {},
    "physical-state": "LinkUp",
    "plr": {
        "margin-threshold": 0,
        "mode": "cs-and-crc",
        "reject-mode": "rejection-based-on-crc-and-cs",
    },
    "speed": "200G",
    "state": {"up": {}},
    "supported-lanes": "2X",
    "supported-speed": "400G",
    "vl-capabilities": "VL0-VL7",
}

# Same command on a DOWN trunk subport (sw1p1s1): plr leaves become JSON
# null and the negotiated fields (lanes/speed/mtu/op-vls/low-power) disappear
# from the payload entirely.
SHOW_INTERFACE_SW_LINK_NVL5_DOWN = {
    "auto-negotiate": "enabled",
    "diagnostics": {},
    "logical-state": "Down",
    "maintenance-state": "Up",
    "max-supported-mtu": 256,
    "phy": {},
    "physical-state": "Disabled",
    "plr": {"margin-threshold": None, "mode": None, "reject-mode": None},
    "state": {"down": {}},
    "supported-lanes": "2X",
    "supported-speed": "400G",
    "vl-capabilities": "VL0-VL7",
}

# `nv show interface <port> counters` captured verbatim from the same port.
# Values are JSON ints, with 'n/a' strings for unsupported directions;
# 'link.carrier-down-count' is the real per-port link-bounce counter.
# The NVL5 trunk-subport counters payload (juliet-126, sw7p1s1) has the
# identical schema - only traffic values differ.
SHOW_INTERFACE_ACP_COUNTERS_NVL6 = {
    "buffer-overrun-errors": 0,
    "in-bytes": 1586016,
    "in-drops": 0,
    "in-errors": 0,
    "in-multicast-pkts": 0,
    "in-pkts": 5507,
    "in-unicast-pkts": 5507,
    "link": {
        "carrier-down-count": 0,
        "error-recovery": 0,
        "local-integrity-errors": 0,
        "port-rcv-constraint-errors": 0,
        "port-rcv-remote-physical-errors": 0,
        "port-rcv-switch-relay-errors": 0,
    },
    "nvl": {
        "drops": {"qp1-drops": {"receive": 0, "transmit": 0}},
        "errors": {
            "icrc-errors": {"receive": 0, "transmit": "n/a"},
            "symbol-errors": {"receive": 0, "transmit": "n/a"},
            "tx-parity-errors": {"receive": 0, "transmit": 0},
        },
    },
    "out-bytes": 1586016,
    "out-drops": 0,
    "out-errors": 0,
    "out-multicast-pkts": 0,
    "out-pkts": 5507,
    "out-unicast-pkts": 5507,
    "out-wait": 0,
}
