"""System gNMI XPath builder — reproduces the SSIM3 expected XPath set for
``system_top_level_subscription``.

Source parity: cl-cafe ``cafe/components/telemetry.py`` (``SystemXPaths*`` hierarchy)
and cl-tests ``system_top_level_subscription`` (attribute categorization + bulk enumeration).

Only the XPath strings and ``top_level`` are consumed by the top-level subscription test, so
this port keeps just those. SSIM3's value-comparison metadata (show command / show key /
otel key / stress_test flags) is intentionally dropped.

SSIM3 ``system_top_level_subscription`` enumerates the modeled system leaves by categorizing
the ``SystemXPaths`` attribute names by prefix and repopulating the object with the matching
dynamic key:

* ``cpu_*``     -> one populate per CPU core index (``cpu_id`` in ``range(numCores)``)
* ``mount_*``   -> one populate per mount point name (``mount_point``)
* ``process_*`` -> one populate per active-service PID (``pid``)
* everything else (general) -> a single populate with no dynamic key

``build_system_top_level_xpaths`` reproduces exactly this expansion.
"""

from __future__ import annotations


# ========================================================================
# SystemXPaths class hierarchy (SSIM3 parity, xpath-only)
# ========================================================================

class SystemXPaths:
    def __init__(self):
        pass

    def populate_gnmi_xpaths(self, cpu_id=0, mount_point="/", **kwargs):
        self.top_level = "/system"
        self.boot_time = {"xpath": "/system/state/boot-time"}
        self.current_datetime = {"xpath": "/system/state/current-datetime"}
        self.software_version = {"xpath": "/system/state/software-version"}
        self.hostname = {"xpath": "/system/state/hostname"}

        # Memory state paths
        self.memory_free = {"xpath": "/system/memory/state/free"}
        self.memory_physical = {"xpath": "/system/memory/state/physical"}
        self.memory_reserved = {"xpath": "/system/memory/state/reserved"}

        # CPU usage paths
        self.cpu_user_seconds = {"xpath": f"/system/cpus/cpu[index={cpu_id}]/state/user/seconds"}
        self.cpu_kernel_seconds = {"xpath": f"/system/cpus/cpu[index={cpu_id}]/state/kernel/seconds"}
        self.cpu_nice_seconds = {"xpath": f"/system/cpus/cpu[index={cpu_id}]/state/nice/seconds"}
        self.cpu_idle_seconds = {"xpath": f"/system/cpus/cpu[index={cpu_id}]/state/idle/seconds"}
        self.cpu_wait_seconds = {"xpath": f"/system/cpus/cpu[index={cpu_id}]/state/wait/seconds"}
        self.cpu_hardware_interrupt_seconds = {"xpath": f"/system/cpus/cpu[index={cpu_id}]/state/hardware-interrupt/seconds"}
        self.cpu_software_interrupt_seconds = {"xpath": f"/system/cpus/cpu[index={cpu_id}]/state/software-interrupt/seconds"}

        # Mount points paths
        self.mount_storage_component = {"xpath": f"/system/mount-points/mount-point[name={mount_point}]/state/storage-component"}
        self.mount_size = {"xpath": f"/system/mount-points/mount-point[name={mount_point}]/state/size"}
        self.mount_available = {"xpath": f"/system/mount-points/mount-point[name={mount_point}]/state/available"}
        self.mount_type = {"xpath": f"/system/mount-points/mount-point[name={mount_point}]/state/type"}
        self.mount_name = {"xpath": f"/system/mount-points/mount-point[name={mount_point}]/state/name"}

        # Control plane traffic paths
        self.cp_in_ipv4_addr_errors = {"xpath": "/system/control-plane-traffic/ingress/ipv4/counters/in-ipv4-addr-errors"}
        self.cp_in_ipv4_pkts = {"xpath": "/system/control-plane-traffic/ingress/ipv4/counters/in-ipv4-pkts"}
        self.cp_in_ipv6_addr_errors = {"xpath": "/system/control-plane-traffic/ingress/ipv6/counters/in-ipv6-addr-errors"}
        self.cp_in_ipv6_pkts = {"xpath": "/system/control-plane-traffic/ingress/ipv6/counters/in-ipv6-pkts"}
        self.cp_out_ipv4_pkts = {"xpath": "/system/control-plane-traffic/egress/ipv4/counters/out-ipv4-pkts"}
        self.cp_out_ipv6_pkts = {"xpath": "/system/control-plane-traffic/egress/ipv6/counters/out-ipv6-pkts"}


class SystemXPaths110(SystemXPaths):
    def __init__(self):
        super(SystemXPaths110, self).__init__()

    def populate_gnmi_xpaths(self, cpu_id=0, mount_point="/", pid=None, **kwargs):
        super(SystemXPaths110, self).populate_gnmi_xpaths(cpu_id, mount_point, **kwargs)

        self.top_level = "/system"

        # Memory state paths
        self.memory_used = {"xpath": "/system/memory/state/used"}
        self.memory_free = {"xpath": "/system/memory/state/free"}
        self.memory_physical = {"xpath": "/system/memory/state/physical"}

        # Adaptive routing paths
        self.adaptive_routing_congestion_changes = {"xpath": "/system/adaptive-routing/state/counters/congestion-changes"}

        # Mount points paths
        self.mount_utilized = {"xpath": f"/system/mount-points/mount-point[name={mount_point}]/state/utilized"}

        # CPU hardware interrupt stats
        self.cpu_hardware_interrupt_avg = {"xpath": f"/system/cpus/cpu[index={cpu_id}]/state/hardware-interrupt/avg"}
        self.cpu_hardware_interrupt_instant = {"xpath": f"/system/cpus/cpu[index={cpu_id}]/state/hardware-interrupt/instant"}
        self.cpu_hardware_interrupt_max = {"xpath": f"/system/cpus/cpu[index={cpu_id}]/state/hardware-interrupt/max"}
        self.cpu_hardware_interrupt_max_time = {"xpath": f"/system/cpus/cpu[index={cpu_id}]/state/hardware-interrupt/max-time"}
        self.cpu_hardware_interrupt_min = {"xpath": f"/system/cpus/cpu[index={cpu_id}]/state/hardware-interrupt/min"}
        self.cpu_hardware_interrupt_min_time = {"xpath": f"/system/cpus/cpu[index={cpu_id}]/state/hardware-interrupt/min-time"}
        self.cpu_hardware_interrupt_interval = {"xpath": f"/system/cpus/cpu[index={cpu_id}]/state/hardware-interrupt/interval"}

        # CPU idle stats
        self.cpu_idle_avg = {"xpath": f"/system/cpus/cpu[index={cpu_id}]/state/idle/avg"}
        self.cpu_idle_instant = {"xpath": f"/system/cpus/cpu[index={cpu_id}]/state/idle/instant"}
        self.cpu_idle_max = {"xpath": f"/system/cpus/cpu[index={cpu_id}]/state/idle/max"}
        self.cpu_idle_max_time = {"xpath": f"/system/cpus/cpu[index={cpu_id}]/state/idle/max-time"}
        self.cpu_idle_min = {"xpath": f"/system/cpus/cpu[index={cpu_id}]/state/idle/min"}
        self.cpu_idle_min_time = {"xpath": f"/system/cpus/cpu[index={cpu_id}]/state/idle/min-time"}
        self.cpu_idle_interval = {"xpath": f"/system/cpus/cpu[index={cpu_id}]/state/idle/interval"}

        # CPU kernel stats
        self.cpu_kernel_avg = {"xpath": f"/system/cpus/cpu[index={cpu_id}]/state/kernel/avg"}
        self.cpu_kernel_instant = {"xpath": f"/system/cpus/cpu[index={cpu_id}]/state/kernel/instant"}
        self.cpu_kernel_max = {"xpath": f"/system/cpus/cpu[index={cpu_id}]/state/kernel/max"}
        self.cpu_kernel_max_time = {"xpath": f"/system/cpus/cpu[index={cpu_id}]/state/kernel/max-time"}
        self.cpu_kernel_min = {"xpath": f"/system/cpus/cpu[index={cpu_id}]/state/kernel/min"}
        self.cpu_kernel_min_time = {"xpath": f"/system/cpus/cpu[index={cpu_id}]/state/kernel/min-time"}
        self.cpu_kernel_interval = {"xpath": f"/system/cpus/cpu[index={cpu_id}]/state/kernel/interval"}

        # CPU nice stats
        self.cpu_nice_avg = {"xpath": f"/system/cpus/cpu[index={cpu_id}]/state/nice/avg"}
        self.cpu_nice_instant = {"xpath": f"/system/cpus/cpu[index={cpu_id}]/state/nice/instant"}
        self.cpu_nice_max = {"xpath": f"/system/cpus/cpu[index={cpu_id}]/state/nice/max"}
        self.cpu_nice_max_time = {"xpath": f"/system/cpus/cpu[index={cpu_id}]/state/nice/max-time"}
        self.cpu_nice_min = {"xpath": f"/system/cpus/cpu[index={cpu_id}]/state/nice/min"}
        self.cpu_nice_min_time = {"xpath": f"/system/cpus/cpu[index={cpu_id}]/state/nice/min-time"}
        self.cpu_nice_interval = {"xpath": f"/system/cpus/cpu[index={cpu_id}]/state/nice/interval"}

        # CPU software interrupt stats
        self.cpu_software_interrupt_avg = {"xpath": f"/system/cpus/cpu[index={cpu_id}]/state/software-interrupt/avg"}
        self.cpu_software_interrupt_instant = {"xpath": f"/system/cpus/cpu[index={cpu_id}]/state/software-interrupt/instant"}
        self.cpu_software_interrupt_max = {"xpath": f"/system/cpus/cpu[index={cpu_id}]/state/software-interrupt/max"}
        self.cpu_software_interrupt_max_time = {"xpath": f"/system/cpus/cpu[index={cpu_id}]/state/software-interrupt/max-time"}
        self.cpu_software_interrupt_min = {"xpath": f"/system/cpus/cpu[index={cpu_id}]/state/software-interrupt/min"}
        self.cpu_software_interrupt_min_time = {"xpath": f"/system/cpus/cpu[index={cpu_id}]/state/software-interrupt/min-time"}
        self.cpu_software_interrupt_interval = {"xpath": f"/system/cpus/cpu[index={cpu_id}]/state/software-interrupt/interval"}

        # CPU total stats
        self.cpu_total_avg = {"xpath": f"/system/cpus/cpu[index={cpu_id}]/state/total/avg"}
        self.cpu_total_instant = {"xpath": f"/system/cpus/cpu[index={cpu_id}]/state/total/instant"}
        self.cpu_total_max = {"xpath": f"/system/cpus/cpu[index={cpu_id}]/state/total/max"}
        self.cpu_total_max_time = {"xpath": f"/system/cpus/cpu[index={cpu_id}]/state/total/max-time"}
        self.cpu_total_min = {"xpath": f"/system/cpus/cpu[index={cpu_id}]/state/total/min"}
        self.cpu_total_min_time = {"xpath": f"/system/cpus/cpu[index={cpu_id}]/state/total/min-time"}
        self.cpu_total_interval = {"xpath": f"/system/cpus/cpu[index={cpu_id}]/state/total/interval"}

        # CPU user stats
        self.cpu_user_avg = {"xpath": f"/system/cpus/cpu[index={cpu_id}]/state/user/avg"}
        self.cpu_user_instant = {"xpath": f"/system/cpus/cpu[index={cpu_id}]/state/user/instant"}
        self.cpu_user_max = {"xpath": f"/system/cpus/cpu[index={cpu_id}]/state/user/max"}
        self.cpu_user_max_time = {"xpath": f"/system/cpus/cpu[index={cpu_id}]/state/user/max-time"}
        self.cpu_user_min = {"xpath": f"/system/cpus/cpu[index={cpu_id}]/state/user/min"}
        self.cpu_user_min_time = {"xpath": f"/system/cpus/cpu[index={cpu_id}]/state/user/min-time"}
        self.cpu_user_interval = {"xpath": f"/system/cpus/cpu[index={cpu_id}]/state/user/interval"}

        # CPU wait stats
        self.cpu_wait_avg = {"xpath": f"/system/cpus/cpu[index={cpu_id}]/state/wait/avg"}
        self.cpu_wait_instant = {"xpath": f"/system/cpus/cpu[index={cpu_id}]/state/wait/instant"}
        self.cpu_wait_max = {"xpath": f"/system/cpus/cpu[index={cpu_id}]/state/wait/max"}
        self.cpu_wait_max_time = {"xpath": f"/system/cpus/cpu[index={cpu_id}]/state/wait/max-time"}
        self.cpu_wait_min = {"xpath": f"/system/cpus/cpu[index={cpu_id}]/state/wait/min"}
        self.cpu_wait_min_time = {"xpath": f"/system/cpus/cpu[index={cpu_id}]/state/wait/min-time"}
        self.cpu_wait_interval = {"xpath": f"/system/cpus/cpu[index={cpu_id}]/state/wait/interval"}

        # System uptime
        self.system_uptime = {"xpath": "/system/state/up-time"}

        # Process stats (per-PID)
        self.process_cpu_usage_system = {"xpath": f"/system/processes/process[pid={pid}]/state/cpu-usage-system"}
        self.process_cpu_usage_user = {"xpath": f"/system/processes/process[pid={pid}]/state/cpu-usage-user"}
        self.process_memory_usage = {"xpath": f"/system/processes/process[pid={pid}]/state/memory-usage"}
        self.process_memory_utilization = {"xpath": f"/system/processes/process[pid={pid}]/state/memory-utilization"}
        self.process_cpu_utilization = {"xpath": f"/system/processes/process[pid={pid}]/state/cpu-utilization"}
        self.process_start_time = {"xpath": f"/system/processes/process[pid={pid}]/state/start-time"}
        self.process_name = {"xpath": f"/system/processes/process[pid={pid}]/state/name"}
        self.process_pid = {"xpath": f"/system/processes/process[pid={pid}]/state/pid"}


class SystemXPaths111(SystemXPaths110):
    def __init__(self):
        super(SystemXPaths111, self).__init__()

    def populate_gnmi_xpaths(self, cpu_id=0, mount_point="/", pid=None, **kwargs):
        super(SystemXPaths111, self).populate_gnmi_xpaths(cpu_id, mount_point, pid, **kwargs)

        self.top_level = "/system"
        self.mount_files_total = {"xpath": f"/system/mount-points/mount-point[name={mount_point}]/state/files-total"}
        self.mount_files_available = {"xpath": f"/system/mount-points/mount-point[name={mount_point}]/state/files-available"}
        self.mount_read_only = {"xpath": f"/system/mount-points/mount-point[name={mount_point}]/state/read-only"}
        self.mount_device_error = {"xpath": f"/system/mount-points/mount-point[name={mount_point}]/state/device-error"}


class SystemXPaths113(SystemXPaths111):
    """System telemetry extended in 113+ images.

    Adds systemd unit state under /system/services, extended process state under
    /system/processes and control-plane-traffic counters under
    /system/control-plane-traffic/{ingress,egress} (which override the base cp_* leaves).
    """

    def __init__(self):
        super(SystemXPaths113, self).__init__()

    def populate_gnmi_xpaths(self, cpu_id=0, mount_point="/", pid=None, unit_name=None, context_switch_type=None, **kwargs):
        super(SystemXPaths113, self).populate_gnmi_xpaths(cpu_id, mount_point, pid, **kwargs)

        # Service-level state xpaths (only when a unit is requested)
        if unit_name is not None:
            self.top_level = "/system/services"
            service_base = f"/system/services/service[name={unit_name}]/state"
            self.service_main_pid = {"xpath": f"{service_base}/main-pid"}
            self.service_unit_state = {"xpath": f"{service_base}/unit-state"}
            self.service_running = {"xpath": f"{service_base}/running"}
            self.service_exe_path = {"xpath": f"{service_base}/exe-path"}
            self.service_restart_count = {"xpath": f"{service_base}/restart-count"}
            self.service_cpu_usage = {"xpath": f"{service_base}/cpu-usage"}
            self.service_memory_usage_bytes = {"xpath": f"{service_base}/memory-usage-bytes"}
            self.service_start_time = {"xpath": f"{service_base}/start-time"}
            self.service_uptime = {"xpath": f"{service_base}/uptime"}
            self.service_thread_count = {"xpath": f"{service_base}/thread-count"}
            self.service_process_count = {"xpath": f"{service_base}/process-count"}

        # Extended process-level state xpaths (only when a PID is requested)
        if pid is not None:
            self.top_level = "/system/processes"
            process_base = f"/system/processes/process[pid={pid}]/state"
            self.parent_pid = {"xpath": f"{process_base}/parent-pid"}
            self.state = {"xpath": f"{process_base}/state"}
            self.thread_count = {"xpath": f"{process_base}/thread-count"}
            self.subprocess_count = {"xpath": f"{process_base}/subprocess-count"}
            type_segment = f"[type={context_switch_type}]" if context_switch_type else "[type=*]"
            base_ctx = f"{process_base}/context-switches/context-switch"
            self.context_switch_type = {"xpath": f"{base_ctx}{type_segment}/type"}
            self.context_switch_count = {"xpath": f"{base_ctx}{type_segment}/count"}
            self.virtual_memory_usage_bytes = {"xpath": f"{process_base}/virtual-memory-usage-bytes"}
            self.shared_memory_usage_bytes = {"xpath": f"{process_base}/shared-memory-usage-bytes"}

        _in = "/system/control-plane-traffic/ingress"
        _out = "/system/control-plane-traffic/egress"

        # IPv4 IP / IpExt — ingress
        self.cp_in_ipv4_pkts = {"xpath": f"{_in}/ipv4/state/in-receives"}
        self.cp_in_ipv4_addr_errors = {"xpath": f"{_in}/ipv4/state/in-addr-errors"}
        self.cp_out_ipv4_pkts = {"xpath": f"{_in}/ipv4/state/in-delivers"}
        self.cp_ip_in_discards = {"xpath": f"{_in}/ipv4/state/in-discarded-pkts"}
        self.cp_ip_in_hdr_errors = {"xpath": f"{_in}/ipv4/state/in-hdr-errors"}
        self.cp_ip_in_unknown_protos = {"xpath": f"{_in}/ipv4/state/in-unknown-protos"}
        self.cp_ip_forw_datagrams = {"xpath": f"{_in}/ipv4/state/in-forwarded-pkts"}
        self.cp_ip_reasm_oks = {"xpath": f"{_in}/ipv4/state/in-reassembly-ok"}
        self.cp_ip_reasm_fails = {"xpath": f"{_in}/ipv4/state/in-reassembly-fails"}
        self.cp_ip_reasm_reqds = {"xpath": f"{_in}/ipv4/state/in-reassembly-reqd"}
        self.cp_ip_ext_in_no_routes = {"xpath": f"{_in}/ipv4/state/in-no-routes"}
        self.cp_ip_ext_in_octets = {"xpath": f"{_in}/ipv4/state/in-octets"}
        self.cp_ip_ext_in_mcast_pkts = {"xpath": f"{_in}/ipv4/state/in-multicast-pkts"}
        self.cp_ip_ext_in_mcast_octets = {"xpath": f"{_in}/ipv4/state/in-multicast-octets"}
        self.cp_ip_ext_in_bcast_pkts = {"xpath": f"{_in}/ipv4/state/in-broadcast-pkts"}
        self.cp_ip_ext_in_truncated_pkts = {"xpath": f"{_in}/ipv4/state/in-truncated-pkts"}
        self.cp_ip_ext_in_ce_pkts = {"xpath": f"{_in}/ipv4/state/in-ce-pkts"}
        self.cp_ip_ext_in_ect0_pkts = {"xpath": f"{_in}/ipv4/state/in-ect0-pkts"}
        self.cp_ip_ext_in_ect1_pkts = {"xpath": f"{_in}/ipv4/state/in-ect1-pkts"}
        self.cp_ip_ext_in_no_ect_pkts = {"xpath": f"{_in}/ipv4/state/in-no-ect-pkts"}

        # IPv4 IP / IpExt — egress
        self.cp_ip_out_requests = {"xpath": f"{_out}/ipv4/state/out-requests"}
        self.cp_ip_out_discards = {"xpath": f"{_out}/ipv4/state/out-discarded-pkts"}
        self.cp_ip_out_no_routes = {"xpath": f"{_out}/ipv4/state/out-no-routes"}
        self.cp_ip_frag_oks = {"xpath": f"{_out}/ipv4/state/out-frag-ok"}
        self.cp_ip_frag_fails = {"xpath": f"{_out}/ipv4/state/out-frag-fails"}
        self.cp_ip_frag_creates = {"xpath": f"{_out}/ipv4/state/out-frag-creates"}
        self.cp_ip_ext_out_octets = {"xpath": f"{_out}/ipv4/state/out-octets"}
        self.cp_ip_ext_out_mcast_pkts = {"xpath": f"{_out}/ipv4/state/out-multicast-pkts"}
        self.cp_ip_ext_out_mcast_octets = {"xpath": f"{_out}/ipv4/state/out-multicast-octets"}
        self.cp_ip_ext_out_bcast_pkts = {"xpath": f"{_out}/ipv4/state/out-broadcast-pkts"}

        # IPv4 ICMP
        self.cp_icmp_in_msgs = {"xpath": f"{_in}/ipv4/icmp/state/in-msgs"}
        self.cp_icmp_in_errors = {"xpath": f"{_in}/ipv4/icmp/state/in-errors"}
        self.cp_icmp_in_dest_unreachs = {"xpath": f"{_in}/ipv4/icmp/state/in-dest-unreachs"}
        self.cp_icmp_in_time_excds = {"xpath": f"{_in}/ipv4/icmp/state/in-time-excds"}
        self.cp_icmp_in_echos = {"xpath": f"{_in}/ipv4/icmp/state/in-echos"}
        self.cp_icmp_in_echo_reps = {"xpath": f"{_in}/ipv4/icmp/state/in-echo-replies"}
        self.cp_icmp_in_redirects = {"xpath": f"{_in}/ipv4/icmp/state/in-redirects"}
        self.cp_icmp_out_msgs = {"xpath": f"{_out}/ipv4/icmp/state/out-msgs"}
        self.cp_icmp_out_errors = {"xpath": f"{_out}/ipv4/icmp/state/out-errors"}
        self.cp_icmp_out_dest_unreachs = {"xpath": f"{_out}/ipv4/icmp/state/out-dest-unreachs"}
        self.cp_icmp_out_time_excds = {"xpath": f"{_out}/ipv4/icmp/state/out-time-excds"}
        self.cp_icmp_out_echos = {"xpath": f"{_out}/ipv4/icmp/state/out-echos"}
        self.cp_icmp_out_echo_reps = {"xpath": f"{_out}/ipv4/icmp/state/out-echo-replies"}
        self.cp_icmp_out_redirects = {"xpath": f"{_out}/ipv4/icmp/state/out-redirects"}

        # IPv4 TCP
        self.cp_tcp_in_segs = {"xpath": f"{_in}/ipv4/tcp/state/in-segs"}
        self.cp_tcp_in_errs = {"xpath": f"{_in}/ipv4/tcp/state/in-errs"}
        self.cp_tcp_out_segs = {"xpath": f"{_out}/ipv4/tcp/state/out-segs"}
        self.cp_tcp_retrans_segs = {"xpath": f"{_out}/ipv4/tcp/state/retrans-segs"}
        self.cp_tcp_active_opens = {"xpath": f"{_out}/ipv4/tcp/state/active-opens"}
        self.cp_tcp_passive_opens = {"xpath": f"{_in}/ipv4/tcp/state/passive-opens"}
        self.cp_tcp_attempt_fails = {"xpath": f"{_out}/ipv4/tcp/state/attempt-fails"}
        self.cp_tcp_estab_resets = {"xpath": f"{_out}/ipv4/tcp/state/estab-resets"}
        self.cp_tcp_curr_estab = {"xpath": f"{_in}/ipv4/tcp/state/curr-estab"}
        self.cp_tcp_out_rsts = {"xpath": f"{_out}/ipv4/tcp/state/out-rsts"}
        self.cp_tcp_ext_listen_drops = {"xpath": f"{_in}/ipv4/tcp/state/listen-drops"}
        self.cp_tcp_ext_listen_overflows = {"xpath": f"{_in}/ipv4/tcp/state/listen-overflows"}
        self.cp_tcp_ext_tcp_timeouts = {"xpath": f"{_out}/ipv4/tcp/state/tcp-timeouts"}
        self.cp_tcp_ext_tcp_syn_retrans = {"xpath": f"{_out}/ipv4/tcp/state/tcp-syn-retrans"}

        # IPv4 UDP
        self.cp_udp_in_datagrams = {"xpath": f"{_in}/ipv4/udp/state/in-datagrams"}
        self.cp_udp_in_errors = {"xpath": f"{_in}/ipv4/udp/state/in-errors"}
        self.cp_udp_no_ports = {"xpath": f"{_in}/ipv4/udp/state/no-ports"}
        self.cp_udp_rcvbuf_errors = {"xpath": f"{_in}/ipv4/udp/state/rcvbuf-errors"}
        self.cp_udp_out_datagrams = {"xpath": f"{_out}/ipv4/udp/state/out-datagrams"}
        self.cp_udp_sndbuf_errors = {"xpath": f"{_out}/ipv4/udp/state/sndbuf-errors"}

        # IPv6 IP — ingress
        self.cp_in_ipv6_pkts = {"xpath": f"{_in}/ipv6/state/in-receives"}
        self.cp_in_ipv6_addr_errors = {"xpath": f"{_in}/ipv6/state/in-addr-errors"}
        self.cp_out_ipv6_pkts = {"xpath": f"{_in}/ipv6/state/in-delivers"}
        self.cp_ip6_in_discards = {"xpath": f"{_in}/ipv6/state/in-discarded-pkts"}
        self.cp_ip6_in_hdr_errors = {"xpath": f"{_in}/ipv6/state/in-hdr-errors"}
        self.cp_ip6_in_unknown_protos = {"xpath": f"{_in}/ipv6/state/in-unknown-protos"}
        self.cp_ip6_in_no_routes = {"xpath": f"{_in}/ipv6/state/in-no-routes"}
        self.cp_ip6_in_octets = {"xpath": f"{_in}/ipv6/state/in-octets"}
        self.cp_ip6_in_mcast_pkts = {"xpath": f"{_in}/ipv6/state/in-multicast-pkts"}
        self.cp_ip6_in_mcast_octets = {"xpath": f"{_in}/ipv6/state/in-multicast-octets"}
        self.cp_ip6_in_truncated_pkts = {"xpath": f"{_in}/ipv6/state/in-truncated-pkts"}

        # IPv6 IP — egress
        self.cp_ip6_out_requests = {"xpath": f"{_out}/ipv6/state/out-requests"}
        self.cp_ip6_out_discards = {"xpath": f"{_out}/ipv6/state/out-discarded-pkts"}
        self.cp_ip6_out_no_routes = {"xpath": f"{_out}/ipv6/state/out-no-routes"}
        self.cp_ip6_out_forw_datagrams = {"xpath": f"{_out}/ipv6/state/out-forwarded-pkts"}
        self.cp_ip6_frag_oks = {"xpath": f"{_out}/ipv6/state/out-frag-ok"}
        self.cp_ip6_frag_fails = {"xpath": f"{_out}/ipv6/state/out-frag-fails"}
        self.cp_ip6_frag_creates = {"xpath": f"{_out}/ipv6/state/out-frag-creates"}
        self.cp_ip6_out_octets = {"xpath": f"{_out}/ipv6/state/out-octets"}
        self.cp_ip6_out_mcast_pkts = {"xpath": f"{_out}/ipv6/state/out-multicast-pkts"}
        self.cp_ip6_out_mcast_octets = {"xpath": f"{_out}/ipv6/state/out-multicast-octets"}

        # IPv6 reassembly — ingress
        self.cp_ip6_reasm_oks = {"xpath": f"{_in}/ipv6/state/in-reassembly-ok"}
        self.cp_ip6_reasm_fails = {"xpath": f"{_in}/ipv6/state/in-reassembly-fails"}
        self.cp_ip6_reasm_reqds = {"xpath": f"{_in}/ipv6/state/in-reassembly-reqd"}
        self.cp_ip6_in_ce_pkts = {"xpath": f"{_in}/ipv6/state/in-ce-pkts"}
        self.cp_ip6_in_ect0_pkts = {"xpath": f"{_in}/ipv6/state/in-ect0-pkts"}
        self.cp_ip6_in_ect1_pkts = {"xpath": f"{_in}/ipv6/state/in-ect1-pkts"}
        self.cp_ip6_in_no_ect_pkts = {"xpath": f"{_in}/ipv6/state/in-no-ect-pkts"}

        # IPv6 ICMP
        self.cp_icmp6_in_msgs = {"xpath": f"{_in}/ipv6/icmp/state/in-msgs"}
        self.cp_icmp6_in_errors = {"xpath": f"{_in}/ipv6/icmp/state/in-errors"}
        self.cp_icmp6_in_dest_unreachs = {"xpath": f"{_in}/ipv6/icmp/state/in-dest-unreachs"}
        self.cp_icmp6_in_time_excds = {"xpath": f"{_in}/ipv6/icmp/state/in-time-excds"}
        self.cp_icmp6_in_echos = {"xpath": f"{_in}/ipv6/icmp/state/in-echos"}
        self.cp_icmp6_in_echo_replies = {"xpath": f"{_in}/ipv6/icmp/state/in-echo-replies"}
        self.cp_icmp6_in_neighbor_advertisements = {"xpath": f"{_in}/ipv6/icmp/state/in-neighbor-advertisements"}
        self.cp_icmp6_in_neighbor_solicits = {"xpath": f"{_in}/ipv6/icmp/state/in-neighbor-solicits"}
        self.cp_icmp6_in_redirects = {"xpath": f"{_in}/ipv6/icmp/state/in-redirects"}
        self.cp_icmp6_in_router_advertisements = {"xpath": f"{_in}/ipv6/icmp/state/in-router-advertisements"}
        self.cp_icmp6_in_router_solicits = {"xpath": f"{_in}/ipv6/icmp/state/in-router-solicits"}
        self.cp_icmp6_out_msgs = {"xpath": f"{_out}/ipv6/icmp/state/out-msgs"}
        self.cp_icmp6_out_errors = {"xpath": f"{_out}/ipv6/icmp/state/out-errors"}
        self.cp_icmp6_out_dest_unreachs = {"xpath": f"{_out}/ipv6/icmp/state/out-dest-unreachs"}
        self.cp_icmp6_out_time_excds = {"xpath": f"{_out}/ipv6/icmp/state/out-time-excds"}
        self.cp_icmp6_out_echos = {"xpath": f"{_out}/ipv6/icmp/state/out-echos"}
        self.cp_icmp6_out_echo_replies = {"xpath": f"{_out}/ipv6/icmp/state/out-echo-replies"}
        self.cp_icmp6_out_neighbor_advertisements = {"xpath": f"{_out}/ipv6/icmp/state/out-neighbor-advertisements"}
        self.cp_icmp6_out_neighbor_solicits = {"xpath": f"{_out}/ipv6/icmp/state/out-neighbor-solicits"}
        self.cp_icmp6_out_redirects = {"xpath": f"{_out}/ipv6/icmp/state/out-redirects"}
        self.cp_icmp6_out_router_advertisements = {"xpath": f"{_out}/ipv6/icmp/state/out-router-advertisements"}
        self.cp_icmp6_out_router_solicits = {"xpath": f"{_out}/ipv6/icmp/state/out-router-solicits"}

        # IPv6 UDP
        self.cp_udp6_in_datagrams = {"xpath": f"{_in}/ipv6/udp/state/in-datagrams"}
        self.cp_udp6_in_errors = {"xpath": f"{_in}/ipv6/udp/state/in-errors"}
        self.cp_udp6_no_ports = {"xpath": f"{_in}/ipv6/udp/state/no-ports"}
        self.cp_udp6_rcvbuf_errors = {"xpath": f"{_in}/ipv6/udp/state/rcvbuf-errors"}
        self.cp_udp6_out_datagrams = {"xpath": f"{_out}/ipv6/udp/state/out-datagrams"}
        self.cp_udp6_sndbuf_errors = {"xpath": f"{_out}/ipv6/udp/state/sndbuf-errors"}


class SystemXPaths114(SystemXPaths113):
    """System telemetry extended in NVUE 1.14 (CL 5.18.0).

    Adds /system/image/partitions/* OpenConfig paths for dual-partition image management.
    """

    def __init__(self):
        super(SystemXPaths114, self).__init__()

    def populate_gnmi_xpaths(self, cpu_id=0, mount_point="/", pid=None, unit_name=None, context_switch_type=None, **kwargs):
        super(SystemXPaths114, self).populate_gnmi_xpaths(
            cpu_id, mount_point, pid=pid, unit_name=unit_name, context_switch_type=context_switch_type, **kwargs
        )

        self.image_partitions_current = {"xpath": "/system/image/partitions/state/current"}
        self.image_partitions_next = {"xpath": "/system/image/partitions/state/next"}
        self.image_partition1_build_id = {"xpath": "/system/image/partitions/partition[id=1]/state/build-id"}
        self.image_partition1_description = {"xpath": "/system/image/partitions/partition[id=1]/state/description"}
        self.image_partition1_disk = {"xpath": "/system/image/partitions/partition[id=1]/state/disk"}
        self.image_partition1_release = {"xpath": "/system/image/partitions/partition[id=1]/state/release"}
        self.image_partition2_build_id = {"xpath": "/system/image/partitions/partition[id=2]/state/build-id"}
        self.image_partition2_description = {"xpath": "/system/image/partitions/partition[id=2]/state/description"}
        self.image_partition2_disk = {"xpath": "/system/image/partitions/partition[id=2]/state/disk"}
        self.image_partition2_release = {"xpath": "/system/image/partitions/partition[id=2]/state/release"}


# ========================================================================
# XPath enumeration + builder API (SSIM3 parity)
# ========================================================================

# Map ``python3-nvue`` major.minor -> SystemXPaths class used on device (Telemetry515+).
SYSTEM_XPATH_CLASS_BY_NVUE = {
    '1.11': SystemXPaths111,
    '1.12': SystemXPaths111,
    '1.13': SystemXPaths113,
    '1.14': SystemXPaths114,
}


def get_all_variables(obj):
    """Return names of attributes that hold an xpath descriptor dict (SSIM ``get_all_variables``)."""
    names = []
    for name, value in vars(obj).items():
        if name.startswith('__'):
            continue
        if isinstance(value, dict) and 'xpath' in value:
            names.append(name)
    return names


def get_all_supported_xpaths(xpath_var):
    """Return the xpath(s) for one descriptor — ``additional_xpaths`` when present (SSIM parity)."""
    if 'additional_xpaths' in xpath_var:
        return list(xpath_var['additional_xpaths'])
    return [xpath_var['xpath']]


def resolve_system_xpath_class(nvue_pkg_version=None):
    """Return the ``SystemXPaths*`` class for a ``python3-nvue`` version (default 113)."""
    if nvue_pkg_version:
        parts = str(nvue_pkg_version).split('.')
        if len(parts) >= 2:
            key = f'{parts[0]}.{parts[1]}'
            if key in SYSTEM_XPATH_CLASS_BY_NVUE:
                return SYSTEM_XPATH_CLASS_BY_NVUE[key]
    return SystemXPaths113


def build_system_top_level_xpaths(
    num_cores,
    mount_points,
    pids,
    *,
    xpath_class=None,
    nvue_pkg_version=None,
):
    """Reproduce SSIM3 ``system_top_level_subscription`` expected-xpath discovery.

    Categorizes the modeled system attributes by name prefix and expands each with its
    dynamic key set (CPU cores / mount points / process PIDs); general attributes are
    populated once. Returns ``(sorted_all_xpaths, top_level_path)``.
    """
    cls = xpath_class or resolve_system_xpath_class(nvue_pkg_version)

    base = cls()
    base.populate_gnmi_xpaths()
    system_attributes = get_all_variables(base)

    cpu_attrs = [a for a in system_attributes if a.startswith('cpu_')]
    mount_attrs = [a for a in system_attributes if a.startswith('mount_')]
    process_attrs = [a for a in system_attributes if a.startswith('process_')]
    general_attrs = [
        a for a in system_attributes
        if not (a.startswith('cpu_') or a.startswith('mount_') or a.startswith('process_'))
    ]

    all_xpaths = set()

    def _collect(param_name, values, attrs):
        if not attrs:
            return
        for value in values:
            kwargs = {param_name: value} if param_name else {}
            obj = cls()
            obj.populate_gnmi_xpaths(**kwargs)
            for attr_name in attrs:
                descriptor = getattr(obj, attr_name, None)
                if isinstance(descriptor, dict) and 'xpath' in descriptor:
                    all_xpaths.update(get_all_supported_xpaths(descriptor))

    _collect('cpu_id', range(int(num_cores) if num_cores else 0), cpu_attrs)
    _collect('mount_point', list(mount_points or []), mount_attrs)
    _collect('pid', list(pids or []), process_attrs)
    _collect(None, [None], general_attrs)

    return sorted(all_xpaths), base.top_level
