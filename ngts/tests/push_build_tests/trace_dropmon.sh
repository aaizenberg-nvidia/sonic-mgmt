#!/usr/bin/env bash
#
# trace_dropmon.sh - trace net_dm_hw_trap_packet_probe and log
#                    qlen + limit + trap name/group with timestamps.
set -euo pipefail
LOG=/var/log/bpftrace.log
MODULE=drop_monitor
apt update
apt install bpftrace -y
# offsetof(per_cpu_dm_data, drop_queue) + offsetof(sk_buff_head, qlen).
# 32 holds when lock-debugging is off; verify with:
#   zcat /proc/config.gz | grep -E 'DEBUG_LOCK_ALLOC|DEBUG_SPINLOCK|PREEMPT_RT'
QLEN_OFF=32
# --- dynamically resolve symbols ($3 = name, $4 = [module]) ---
base=$(awk '$3=="dm_hw_cpu_data"   {print $1}' /proc/kallsyms)   # per-cpu offset (type 'a')
limit=$(awk '$3=="net_dm_queue_len"{print $1}' /proc/kallsyms)   # module-data address (type 'd')
if [[ -z "${base}" || -z "${limit}" ]]; then
    echo "error: symbols not found in /proc/kallsyms - is ${MODULE} loaded?" >&2
    exit 1
fi
echo "resolved dm_hw_cpu_data offset=0x${base}  net_dm_queue_len=0x${limit}" >&2
# --- generate the bpftrace program with addresses substituted ---
BT=$(mktemp /tmp/dropmon.XXXXXX.bt)
trap 'rm -f "${BT}"' EXIT
cat > "${BT}" <<EOF
BEGIN { printf("%s tracing net_dm_hw_trap_packet_probe...\n", strftime("%Y-%m-%d %H:%M:%S", nsecs)); }
kprobe:net_dm_hw_trap_packet_probe {
    \$hw   = (uint64)0x${base} + *(uint64 *)(kaddr("__per_cpu_offset") + cpu * 8);
    \$meta = (struct devlink_trap_metadata *)arg3;
    @attempts = count();
    printf("%s cpu %d qlen=%d limit=%d trap=%s group=%s\n",
           strftime("%Y-%m-%d %H:%M:%S", nsecs),
           cpu,
           *(uint32 *)(\$hw + ${QLEN_OFF}),
           *(uint32 *)0x${limit},
           str(\$meta->trap_name),
           str(\$meta->trap_group_name));
}
EOF
# stdbuf -oL => line-buffered so the log updates live under redirection.
exec stdbuf -oL bpftrace "${BT}" > "${LOG}" 2>&1
