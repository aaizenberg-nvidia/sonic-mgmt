"""Interface gNMI xpath builder — reproduces the SSIM3 expected xpath set for
``interface_top_level_subscription``.

Source parity: cl-cafe ``cafe/components/telemetry.py`` (``InterfaceXPaths*`` hierarchy)
and cl-tests ``test_files/gnmi/lib/utils.py`` (enumeration + histogram helper).

Only the xpath strings, ``top_level`` and (histogram-derived) ``additional_xpaths`` are
consumed by the top-level subscription test, so this port keeps just those fields. SSIM3's
value-comparison metadata (show command / key mapping) is intentionally dropped.
"""

from __future__ import annotations


# ========================================================================
# InterfaceXPaths class hierarchy (verbatim SSIM3 parity)
# ========================================================================

class InterfaceXPaths:
    def __init__(self):
        pass

    def populate_gnmi_xpaths(self, interface="swp1", lane=5, **kwargs):
        self.interface = interface
        self.lane = lane
        self.top_level = f"/interfaces/interface[name={interface}]"
        self.in_broadcast_pkts = {
            "xpath": f"/interfaces/interface[name={interface}]/state/counters/in-broadcast-pkts",
        }
        self.in_multicast_pkts = {
            "xpath": f"/interfaces/interface[name={interface}]/state/counters/in-multicast-pkts",
        }
        self.in_octets = {"xpath": f"/interfaces/interface[name={interface}]/state/counters/in-octets"}
        self.admin_status = {
            "xpath": f"/interfaces/interface[name={interface}]/state/admin-status",
        }


class InterfaceXPaths190(InterfaceXPaths):
    def __init__(self):
        super().__init__()

    def populate_gnmi_xpaths(self, interface="swp1", lane=5, **kwargs):
        super().populate_gnmi_xpaths(interface, lane, **kwargs)
        self.interface = interface
        self.lane = lane
        self.top_level = f"/interfaces/interface[name={interface}]"
        self.in_pkts = {
            "xpath": f"/interfaces/interface[name={interface}]/state/counters/in-pkts",
        }
        self.out_pkts = {
            "xpath": f"/interfaces/interface[name={interface}]/state/counters/out-pkts",
        }
        self.negotiated_duplex_mode = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/state/negotiated-duplex-mode",
        }
        self.port_speed = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/state/port-speed",
        }
        self.ifindex = {"xpath": f"/interfaces/interface[name={interface}]/state/ifindex"}
        self.oper_status = {
            "xpath": f"/interfaces/interface[name={interface}]/state/oper-status",
        }
        self.in_errors = {
            "xpath": f"/interfaces/interface[name={interface}]/state/counters/in-errors",
        }
        self.out_errors = {
            "xpath": f"/interfaces/interface[name={interface}]/state/counters/out-errors",
        }
        self.pause_frames = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/state/counters/in-mac-pause-frames",
        }
        self.in_oversize_frames = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/state/counters/in-oversize-frames",
        }
        self.in_fcs_errors = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/state/counters/in-fcs-errors",
        }
        self.in_frames_64_octets = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/state/counters/in-distribution/in-frames-64-octets",
        }
        self.in_frames_65_127_octets = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/state/counters/in-distribution/in-frames-65-127-octets",
        }
        self.in_frames_128_255_octets = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/state/counters/in-distribution/in-frames-128-255-octets",
        }
        self.in_frames_256_511_octets = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/state/counters/in-distribution/in-frames-256-511-octets",
        }
        self.in_frames_512_1023_octets = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/state/counters/in-distribution/in-frames-512-1023-octets",
        }
        self.in_frames_1024_1518_octets = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/state/counters/in-distribution/in-frames-1024-1518-octets",
        }
        self.out_bits_rate = {
            "xpath": f"/interfaces/interface[name={interface}]/rates/state/out-bits-rate",
        }
        self.in_bits_rate = {
            "xpath": f"/interfaces/interface[name={interface}]/rates/state/in-bits-rate",
        }
        self.in_pkts_rate = {
            "xpath": f"/interfaces/interface[name={interface}]/rates/state/in-pkts-rate",
        }
        self.mtu = {"xpath": f"/interfaces/interface[name={interface}]/state/mtu"}
        self.in_discards = {"xpath": f"/interfaces/interface[name={interface}]/state/counters/in-discards"}
        self.out_octets = {
            "xpath": f"/interfaces/interface[name={interface}]/state/counters/out-octets",
        }
        self.out_pkts_rate = {
            "xpath": f"/interfaces/interface[name={interface}]/rates/state/out-pkts-rate",
        }
        self.out_unicast_pkts = {
            "xpath": f"/interfaces/interface[name={interface}]/state/counters/out-unicast-pkts",
        }
        self.out_broadcast_pkts = {
            "xpath": f"/interfaces/interface[name={interface}]/state/counters/out-broadcast-pkts",
        }
        self.out_discards = {"xpath": f"/interfaces/interface[name={interface}]/state/counters/out-discards"}
        self.out_multicast_pkts = {
            "xpath": f"/interfaces/interface[name={interface}]/state/counters/out-multicast-pkts",
        }
        self.in_octets = {
            "xpath": f"/interfaces/interface[name={interface}]/state/counters/in-octets",
        }
        self.in_unicast_pkts = {
            "xpath": f"/interfaces/interface[name={interface}]/state/counters/in-unicast-pkts",
        }
        self.carrier_transitions = {
            "xpath": f"/interfaces/interface[name={interface}]/state/counters/carrier-transitions",
        }
        self.rs_fec_uncorrectable_blocks = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/phy/state/rs-fec-uncorrectable-blocks",
        }
        self.rs_fec_single_error_blocks = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/phy/state/rs-fec-single-error-blocks",
        }
        self.rs_fec_no_error_blocks = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/phy/state/rs-fec-no-error-blocks",
        }
        self.fc_fec_corrected_blocks = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/phy/state/lane[lane={lane}]/fc-fec-corrected-blocks",
            "additional_xpaths": [f"/interfaces/interface[name={interface}]/ethernet/phy/state/lane[lane={x}]/fc-fec-corrected-blocks" for x in range(4)],
        }
        self.fc_fec_uncorrected_blocks = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/phy/state/lane[lane={lane}]/fc-fec-uncorrected-blocks",
            "additional_xpaths": [f"/interfaces/interface[name={interface}]/ethernet/phy/state/lane[lane={x}]/fc-fec-uncorrected-blocks" for x in range(4)],
        }
        self.rs_fec_corrected_symbols = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/phy/state/lane[lane={lane}]/rs-fec-corrected-symbols",
            "additional_xpaths": [f"/interfaces/interface[name={interface}]/ethernet/phy/state/lane[lane={x}]/rs-fec-corrected-symbols" for x in range(8)],
        }
        self.corrected_bits = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/phy/state/corrected-bits",
        }
        self.effective_errors = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/phy/state/effective-errors",
        }
        self.effective_ber = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/phy/state/effective-ber",
        }
        self.received_bits = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/phy/state/received-bits",
        }
        self.symbol_errors = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/phy/state/symbol-errors",
        }
        self.symbol_ber = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/phy/state/symbol-ber",
        }
        self.raw_ber = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/phy/state/lane[lane={lane}]/raw-ber",
            "additional_xpaths": [f"/interfaces/interface[name={interface}]/ethernet/phy/state/lane[lane={x}]/raw-ber" for x in range(8)],
        }
        self.raw_error = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/phy/state/lane[lane={lane}]/raw-errors",
            "additional_xpaths": [f"/interfaces/interface[name={interface}]/ethernet/phy/state/lane[lane={x}]/raw-errors" for x in range(8)],
        }
        self.fec_time_since_last_clear = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/phy/state/fec-time-since-last-clear",
        }
        self.ber_time_since_last_clear = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/phy/state/ber-time-since-last-clear",
        }
        self.in_crc_errors = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/state/counters/in-crc-errors",
        }
        self.in_acl_drops = {
            "xpath": f"/interfaces/interface[name={interface}]/state/counters/in-acl-drops",
        }


class InterfaceXPaths110(InterfaceXPaths190):
    def __init__(self):
        super(InterfaceXPaths110, self).__init__()

    def populate_gnmi_xpaths(self, interface="swp1", lane=5, auto_neg=True, **kwargs):
        super(InterfaceXPaths110, self).populate_gnmi_xpaths(interface, lane, **kwargs)

        self.interface = interface
        self.lane = lane
        self.top_level = f"/interfaces/interface[name={interface}]"
        self.in_jabber_frames = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/state/counters/in-jabber-frames",
        }
        self.last_change = {"xpath": f"/interfaces/interface[name={interface}]/state/last-change"}
        self.type = {"xpath": f"/interfaces/interface[name={interface}]/state/type"}
        self.name = {"xpath": f"/interfaces/interface[name={interface}]/state/name"}
        self.hw_mac_address = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/state/hw-mac-address",
        }
        self.mac_address = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/state/mac-address",
        }
        self.protodown = {
            "xpath": f"/interfaces/interface[name={interface}]/state/protodown",
        }
        self.auto_negotiate = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/state/auto-negotiate",
        }
        if auto_neg:
            self.negotiated_port_speed = {
                "xpath": f"/interfaces/interface[name={interface}]/ethernet/state/negotiated-port-speed",
            }
            self.port_speed = {
                "xpath": f"/interfaces/interface[name={interface}]/ethernet/state/port-speed",
            }
        else:
            self.port_speed = {
                "xpath": f"/interfaces/interface[name={interface}]/ethernet/state/port-speed",
            }


class InterfaceXPaths111(InterfaceXPaths110):
    def __init__(self):
        super(InterfaceXPaths111, self).__init__()

    def populate_gnmi_xpaths(self, interface="swp1", lane=5, auto_neg=True, histogram_boundary=None, **kwargs):
        if histogram_boundary is None:
            histogram_boundary = []

        self.interface = interface
        self.lane = lane
        self.top_level = f"/interfaces/interface[name={interface}]"
        self.admin_status = {
            "xpath": f"/interfaces/interface[name={interface}]/state/admin-status",
        }
        self.out_discards = {"xpath": f"/interfaces/interface[name={interface}]/state/counters/out-discards"}
        self.last_change = {"xpath": f"/interfaces/interface[name={interface}]/state/last-change"}
        self.type = {"xpath": f"/interfaces/interface[name={interface}]/state/type"}
        self.name = {"xpath": f"/interfaces/interface[name={interface}]/state/name"}
        self.hw_mac_address = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/state/hw-mac-address",
        }
        self.mac_address = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/state/mac-address",
        }
        self.protodown = {
            "xpath": f"/interfaces/interface[name={interface}]/state/protodown",
        }
        self.auto_negotiate = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/state/auto-negotiate",
        }
        if auto_neg:
            self.negotiated_port_speed = {
                "xpath": f"/interfaces/interface[name={interface}]/ethernet/state/negotiated-port-speed",
            }
            self.port_speed = {
                "xpath": f"/interfaces/interface[name={interface}]/ethernet/state/port-speed",
            }
        else:
            self.port_speed = {
                "xpath": f"/interfaces/interface[name={interface}]/ethernet/state/port-speed",
            }
        self.negotiated_duplex_mode = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/state/negotiated-duplex-mode",
        }
        self.ifindex = {"xpath": f"/interfaces/interface[name={interface}]/state/ifindex"}
        self.oper_status = {
            "xpath": f"/interfaces/interface[name={interface}]/state/oper-status",
        }
        self.out_bits_rate = {
            "xpath": f"/interfaces/interface[name={interface}]/rates/state/out-bits-rate",
        }
        self.in_bits_rate = {
            "xpath": f"/interfaces/interface[name={interface}]/rates/state/in-bits-rate",
        }
        self.in_pkts_rate = {
            "xpath": f"/interfaces/interface[name={interface}]/rates/state/in-pkts-rate",
        }
        self.mtu = {"xpath": f"/interfaces/interface[name={interface}]/state/mtu"}
        self.in_discards = {"xpath": f"/interfaces/interface[name={interface}]/state/counters/in-discards"}
        self.rs_fec_uncorrectable_blocks = {
            "xpath": f"/interfaces/interface[name={interface}]/phy/fec/state/rs-fec-uncorrectable-blocks",
        }
        self.rs_fec_single_error_blocks = {
            "xpath": f"/interfaces/interface[name={interface}]/phy/fec/state/rs-fec-single-error-blocks",
        }
        self.rs_fec_no_error_blocks = {
            "xpath": f"/interfaces/interface[name={interface}]/phy/fec/state/rs-fec-no-error-blocks",
        }
        self.fc_fec_corrected_blocks = {
            "xpath": f"/interfaces/interface[name={interface}]/phy/channels/channel[id={lane}]/state/fc-fec-corrected-blocks",
            "additional_xpaths": [f"/interfaces/interface[name={interface}]/phy/channels/channel[id={x}]/state/fc-fec-corrected-blocks" for x in range(4)],
        }
        self.fc_fec_uncorrected_blocks = {
            "xpath": f"/interfaces/interface[name={interface}]/phy/channels/channel[id={lane}]/state/fc-fec-uncorrected-blocks",
            "additional_xpaths": [f"/interfaces/interface[name={interface}]/phy/channels/channel[id={x}]/state/fc-fec-uncorrected-blocks" for x in range(4)],
        }
        self.rs_fec_corrected_symbols = {
            "xpath": f"/interfaces/interface[name={interface}]/phy/channels/channel[id={lane}]/state/rs-fec-corrected-symbols",
            "additional_xpaths": [f"/interfaces/interface[name={interface}]/phy/channels/channel[id={x}]/state/rs-fec-corrected-symbols" for x in range(8)],
        }
        self.corrected_bits = {
            "xpath": f"/interfaces/interface[name={interface}]/phy/fec/state/corrected-bits",
        }
        self.effective_errors = {
            "xpath": f"/interfaces/interface[name={interface}]/phy/state/effective-errors",
        }
        self.effective_ber = {
            "xpath": f"/interfaces/interface[name={interface}]/phy/ber/state/effective-ber",
        }
        self.received_bits = {
            "xpath": f"/interfaces/interface[name={interface}]/phy/state/received-bits",
        }
        self.symbol_errors = {
            "xpath": f"/interfaces/interface[name={interface}]/phy/state/symbol-errors",
        }
        self.symbol_ber = {
            "xpath": f"/interfaces/interface[name={interface}]/phy/ber/state/symbol-ber",
        }
        self.raw_ber = {
            "xpath": f"/interfaces/interface[name={interface}]/phy/channels/channel[id={lane}]/state/raw-ber",
            "additional_xpaths": [f"/interfaces/interface[name={interface}]/phy/channels/channel[id={x}]/state/raw-ber" for x in range(8)],
        }
        self.raw_error = {
            "xpath": f"/interfaces/interface[name={interface}]/phy/channels/channel[id={lane}]/state/raw-errors",
            "additional_xpaths": [f"/interfaces/interface[name={interface}]/phy/channels/channel[id={x}]/state/raw-errors" for x in range(8)],
        }
        self.fec_time_since_last_clear = {
            "xpath": f"/interfaces/interface[name={interface}]/phy/fec/state/fec-time-since-last-clear",
        }
        self.ber_time_since_last_clear = {
            "xpath": f"/interfaces/interface[name={interface}]/phy/ber/state/ber-time-since-last-clear",
        }
        self.out_pkts_rate = {
            "xpath": f"/interfaces/interface[name={interface}]/rates/state/out-pkts-rate",
        }
        self.in_jabber_frames = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/state/counters/in-jabber-frames",
        }
        self.in_broadcast_pkts = {
            "xpath": f"/interfaces/interface[name={interface}]/state/counters/in-broadcast-pkts",
        }
        self.in_multicast_pkts = {
            "xpath": f"/interfaces/interface[name={interface}]/state/counters/in-multicast-pkts",
        }
        self.in_octets = {
            "xpath": f"/interfaces/interface[name={interface}]/state/counters/in-octets",
        }
        self.in_pkts = {
            "xpath": f"/interfaces/interface[name={interface}]/state/counters/in-pkts",
        }
        self.out_pkts = {
            "xpath": f"/interfaces/interface[name={interface}]/state/counters/out-pkts",
        }
        self.in_oversize_frames = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/state/counters/in-oversize-frames",
        }
        self.in_fcs_errors = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/state/counters/in-fcs-errors",
        }
        self.in_frames_64_octets = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/state/counters/in-distribution/in-frames-64-octets",
        }
        self.in_frames_65_127_octets = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/state/counters/in-distribution/in-frames-65-127-octets",
        }
        self.in_frames_128_255_octets = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/state/counters/in-distribution/in-frames-128-255-octets",
        }
        self.in_frames_256_511_octets = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/state/counters/in-distribution/in-frames-256-511-octets",
        }
        self.in_frames_512_1023_octets = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/state/counters/in-distribution/in-frames-512-1023-octets",
        }
        self.in_frames_1024_1518_octets = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/state/counters/in-distribution/in-frames-1024-1518-octets",
        }
        self.out_octets = {
            "xpath": f"/interfaces/interface[name={interface}]/state/counters/out-octets",
        }
        self.out_unicast_pkts = {
            "xpath": f"/interfaces/interface[name={interface}]/state/counters/out-unicast-pkts",
        }
        self.out_broadcast_pkts = {
            "xpath": f"/interfaces/interface[name={interface}]/state/counters/out-broadcast-pkts",
        }
        self.out_multicast_pkts = {
            "xpath": f"/interfaces/interface[name={interface}]/state/counters/out-multicast-pkts",
        }
        self.in_unicast_pkts = {
            "xpath": f"/interfaces/interface[name={interface}]/state/counters/in-unicast-pkts",
        }
        self.carrier_transitions = {
            "xpath": f"/interfaces/interface[name={interface}]/state/counters/carrier-transitions",
        }
        self.in_crc_errors = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/state/counters/in-crc-errors",
        }
        self.in_acl_drops = {
            "xpath": f"/interfaces/interface[name={interface}]/state/counters/in-acl-drops",
        }
        self.in_errors = {
            "xpath": f"/interfaces/interface[name={interface}]/state/counters/in-errors",
        }
        self.out_errors = {
            "xpath": f"/interfaces/interface[name={interface}]/state/counters/out-errors",
        }
        self.pause_frames = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/state/counters/in-mac-pause-frames",
        }
        self.out_mac_pause_frames = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/state/counters/out-mac-pause-frames",
        }
        self.in_maxsize_exceeded = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/state/counters/in-maxsize-exceeded",
        }
        self.in_symbol_error = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/state/counters/in-symbol-error",
        }
        self.in_fragment_frames = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/state/counters/in-fragment-frames",
        }
        self.in_undersize_frames = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/state/counters/in-undersize-frames",
        }
        self.carrier_down_transitions = {
            "xpath": f"/interfaces/interface[name={interface}]/state/counters/carrier-down-transitions",
        }
        self.carrier_up_transitions = {
            "xpath": f"/interfaces/interface[name={interface}]/state/counters/carrier-up-transitions",
        }
        self.out_hoq_drops = {
            "xpath": f"/interfaces/interface[name={interface}]/state/counters/out-hoq-drops",
        }
        self.out_hoq_stall_drops = {
            "xpath": f"/interfaces/interface[name={interface}]/state/counters/out-hoq-stall-drops",
        }
        self.out_sll_drops = {
            "xpath": f"/interfaces/interface[name={interface}]/state/counters/out-sll-drops",
        }
        self.out_acl_drops = {
            "xpath": f"/interfaces/interface[name={interface}]/state/counters/out-acl-drops",
        }
        self.out_stp_filter_drops = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/state/counters/out-stp-filter-drops",
        }
        self.out_vlan_membership_drops = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/state/counters/out-vlan-membership-drops",
        }
        self.in_vlan_tag_allowance_drops = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/state/counters/in-vlan-tag-allowance-drops",
        }
        self.in_link_down_drops = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/state/counters/in-link-down-drops",
        }
        self.in_vlan_membership_drops = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/state/counters/in-vlan-membership-drops",
        }
        self.in_loopback_drops = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/state/counters/in-loopback-drops",
        }
        self.pkt_drop_events_probe_resource_lack = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/state/counters/pkt-drop-events-probe-resource-lack",
        }
        self.in_distribution_in_frames_1519_2047_octets = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/state/counters/in-distribution/in-frames-1519-2047-octets",
        }
        self.in_distribution_in_frames_2048_4095_octets = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/state/counters/in-distribution/in-frames-2048-4095-octets",
        }
        self.in_distribution_in_frames_4096_8191_octets = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/state/counters/in-distribution/in-frames-4096-8191-octets",
        }
        self.in_distribution_in_frames_8192_9216_octets = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/state/counters/in-distribution/in-frames-8192-9216-octets",
        }
        self.no_buffer_multicast_dropped_pkts = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/state/counters/no-buffer-multicast-dropped-pkts",
        }
        self.in_buffer_almost_full = {
            "xpath": f"/interfaces/interface[name={interface}]/state/counters/in-buffer-almost-full",
        }
        self.in_buffer_full = {
            "xpath": f"/interfaces/interface[name={interface}]/state/counters/in-buffer-full",
        }
        self.in_ebp_pkts = {
            "xpath": f"/interfaces/interface[name={interface}]/state/counters/in-ebp-pkts",
        }
        self.out_ebp_pkts = {
            "xpath": f"/interfaces/interface[name={interface}]/state/counters/out-ebp-pkts",
        }
        self.pkts_payload_internal_checksum_errors = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/state/counters/pkts-payload-internal-checksum-errors",
        }
        self.out_distribution_out_frames_1024_1518_octets = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/state/counters/out-distribution/out-frames-1024-1518-octets",
        }
        self.out_distribution_out_frames_128_255_octets = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/state/counters/out-distribution/out-frames-128-255-octets",
        }
        self.out_distribution_out_frames_1519_2047_octets = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/state/counters/out-distribution/out-frames-1519-2047-octets",
        }
        self.out_distribution_out_frames_2048_4095_octets = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/state/counters/out-distribution/out-frames-2048-4095-octets",
        }
        self.out_distribution_out_frames_256_511_octets = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/state/counters/out-distribution/out-frames-256-511-octets",
        }
        self.out_distribution_out_frames_4096_8191_octets = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/state/counters/out-distribution/out-frames-4096-8191-octets",
        }
        self.out_distribution_out_frames_512_1023_octets = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/state/counters/out-distribution/out-frames-512-1023-octets",
        }
        self.out_distribution_out_frames_64_octets = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/state/counters/out-distribution/out-frames-64-octets",
        }
        self.out_distribution_out_frames_65_127_octets = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/state/counters/out-distribution/out-frames-65-127-octets",
        }
        self.out_distribution_out_frames_8192_9216_octets = {
            "xpath": f"/interfaces/interface[name={interface}]/ethernet/state/counters/out-distribution/out-frames-8192-9216-octets",
        }
        self.in_unknown_protos = {
            "xpath": f"/interfaces/interface[name={interface}]/state/counters/in-unknown-protos",
        }
        self.ecn_marked_pkts = {
            "xpath": f"/interfaces/interface[name={interface}]/state/counters/ecn-marked-pkts",
        }
        self.tx_wait = {
            "xpath": f"/interfaces/interface[name={interface}]/state/counters/tx-wait",
        }
        self.ece_marked_pkts = {
            "xpath": f"/interfaces/interface[name={interface}]/state/counters/ece-marked-pkts",
        }
        self.rs_fec_histogram_count = {
            "xpath": f"/interfaces/interface[name={interface}]/phy/histograms/state/rs-num-corr-err[upper-boundary=0]/count",
            "additional_xpaths": [
                f"/interfaces/interface[name={interface}]/phy/histograms/state/rs-num-corr-err[upper-boundary={x}]/count"
                for x in histogram_boundary
            ] if histogram_boundary else [],
        }
        self.rs_fec_histogram_upper_boundary = {
            "xpath": f"/interfaces/interface[name={interface}]/phy/histograms/state/rs-num-corr-err[upper-boundary=0]/upper-boundary",
            "additional_xpaths": [
                f"/interfaces/interface[name={interface}]/phy/histograms/state/"
                f"rs-num-corr-err[upper-boundary={x}]/upper-boundary"
                for x in histogram_boundary
            ] if histogram_boundary else [],
        }


class InterfaceXPaths112(InterfaceXPaths111):
    def __init__(self):
        super(InterfaceXPaths112, self).__init__()

    def populate_gnmi_xpaths(self, interface="swp1", lane=5, auto_neg=True, histogram_boundary=None, **kwargs):
        if histogram_boundary is None:
            histogram_boundary = []
        super(InterfaceXPaths112, self).populate_gnmi_xpaths(
            interface=interface,
            lane=lane,
            auto_neg=auto_neg,
            histogram_boundary=histogram_boundary,
            **kwargs
        )

        self.description = {
            "xpath": f"/interfaces/interface[name={interface}]/state/description",
        }
        self.transceiver = {
            "xpath": f"/interfaces/interface[name={interface}]/state/transceiver",
        }


class InterfaceXPaths113(InterfaceXPaths112):
    def __init__(self):
        super(InterfaceXPaths113, self).__init__()

    def populate_gnmi_xpaths(self, interface="swp1", lane=5, auto_neg=True, histogram_boundary=None, link_debounce=False, **kwargs):
        if histogram_boundary is None:
            histogram_boundary = []
        super(InterfaceXPaths113, self).populate_gnmi_xpaths(
            interface=interface,
            lane=lane,
            auto_neg=auto_neg,
            histogram_boundary=histogram_boundary,
            **kwargs
        )
        # Link-debounce (hold-time) counters are only streamed by gNMI when the interface
        # link-debounce feature knob is enabled on the device. The feature is disabled by
        # default, so these xpaths are not advertised; including them in the expected set
        # would cause perpetual missing-xpath failures. Callers running with link debounce
        # enabled must pass link_debounce=True to add them.
        if not link_debounce:
            return
        self.ignored_up_events = {
            "xpath": f"/interfaces/interface[name={interface}]/hold-time/state/counters/ignored-up-events",
        }
        self.received_up_events = {
            "xpath": f"/interfaces/interface[name={interface}]/hold-time/state/counters/received-up-events",
        }
        self.ignored_down_events = {
            "xpath": f"/interfaces/interface[name={interface}]/hold-time/state/counters/ignored-down-events",
        }
        self.received_down_events = {
            "xpath": f"/interfaces/interface[name={interface}]/hold-time/state/counters/received-down-events",
        }
        self.timer_cancellations = {
            "xpath": f"/interfaces/interface[name={interface}]/hold-time/state/counters/timer-cancellations",
        }
        self.timer_expirations = {
            "xpath": f"/interfaces/interface[name={interface}]/hold-time/state/counters/timer-expirations",
        }


# ========================================================================
# XPath enumeration + builder API (SSIM3 parity)
# ========================================================================

# Non-xpath attributes set on the object that must be excluded from xpath enumeration.
_SKIP_ATTR_NAMES = {'top_level', 'interface', 'lane'}

# Map ``python3-nvue`` major.minor -> InterfaceXPaths class used on device (Telemetry515+).
INTERFACE_XPATH_CLASS_BY_NVUE = {
    '1.11': InterfaceXPaths111,
    '1.12': InterfaceXPaths112,
    '1.13': InterfaceXPaths113,
    '1.14': InterfaceXPaths113,
}


def get_all_variables(cls, skip_variables=None):
    if skip_variables is None:
        skip_variables = []
    xs = []
    for name, value in vars(cls).items():
        if name.startswith('__') or isinstance(value, classmethod):
            continue
        if name in skip_variables or name in _SKIP_ATTR_NAMES:
            continue
        xs.append(name)
    return xs


def get_xpath_for_given_variable(xpath_var):
    return xpath_var['xpath']


def get_all_supported_xpaths(xpath_var):
    ans = []
    if 'additional_xpaths' in xpath_var.keys():
        ans.extend(xpath_var['additional_xpaths'])
    else:
        ans.append(get_xpath_for_given_variable(xpath_var))
    return ans


def resolve_interface_xpath_class(nvue_pkg_version=None):
    """Return the ``InterfaceXPaths*`` class for a ``python3-nvue`` version (default 113)."""
    if nvue_pkg_version:
        parts = str(nvue_pkg_version).split('.')
        if len(parts) >= 2:
            key = f'{parts[0]}.{parts[1]}'
            if key in INTERFACE_XPATH_CLASS_BY_NVUE:
                return INTERFACE_XPATH_CLASS_BY_NVUE[key]
    return InterfaceXPaths113


def build_interface_top_level_xpaths(
    interface,
    *,
    xpath_class=None,
    nvue_pkg_version=None,
    lane=5,
    auto_neg=True,
    histogram_boundary=None,
    link_debounce=False,
):
    """Reproduce SSIM3 ``interface_top_level_subscription`` expected-xpath discovery.

    Returns ``(all_xpaths, top_level_path)``.
    """
    cls = xpath_class or resolve_interface_xpath_class(nvue_pkg_version)
    obj = cls()
    obj.populate_gnmi_xpaths(
        interface=interface,
        lane=lane,
        auto_neg=auto_neg,
        histogram_boundary=histogram_boundary or [],
        link_debounce=link_debounce,
    )
    all_xpaths = []
    for counter in get_all_variables(obj):
        all_xpaths.extend(get_all_supported_xpaths(getattr(obj, counter)))
    return all_xpaths, obj.top_level
