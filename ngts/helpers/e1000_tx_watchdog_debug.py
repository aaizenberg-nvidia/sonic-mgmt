"""
Enhanced debug collection for the intermittent e1000 mgmt-NIC TX-watchdog reset seen on SimX
setups (Redmine #5040675 / #4955275).

The bug ("NETDEV WATCHDOG: transmit queue 0 timed out" -> "e1000 ... Reset adapter") is not
reproducible on demand and the logs collected on failure are not enough to root-cause it. Per
Slava Grinbaum's analysis, the e1000 TX queue gets stuck because a completion interrupt is not
delivered in time somewhere along the path QEMU -> host scheduler -> guest handler.

To tell which of these is to blame we need three data streams collected *in parallel* for the
whole test window (each started before the test body runs and stopped afterwards):

  1. SimX container - QEMU interrupt counters (``virsh qemu-monitor-command <vm> --hmp "info irq"``)
  2. Hypervisor host - scheduler events (``perf sched record -a``)
  3. Guest (SONiC VM) - per-CPU interrupt and softirq counters (``/proc/interrupts`` + ``/proc/softirqs``)

All collection here is strictly best-effort debug tooling: any failure is logged and swallowed so
it can never affect the outcome of the test that hosts it.
"""
import logging
import shlex
import time
from contextlib import contextmanager

import allure

logger = logging.getLogger()

# Unique markers embedded in the sampling loops so they can be located and killed with ``pkill -f``.
GUEST_LOOP_MARKER = 'E1000WD_GUEST_IRQ'
SIMX_LOOP_MARKER = 'E1000WD_SIMX_IRQ'

# Remote (in-VM / in-container) paths for the raw collected data.
GUEST_LOG = '/tmp/guest_irq_softirq.log'
SIMX_LOG = '/tmp/info_irq.log'
HOST_PERF_DATA = '/tmp/sched.data'
HOST_PERF_OUT = '/tmp/perf_sched.out'
HOST_PERF_PID = '/tmp/perf_sched.pid'

# Sampling interval (seconds) for the counter loops, as requested in the ticket.
SAMPLE_INTERVAL = 0.5
# Grace period to let ``perf`` flush ``sched.data`` after receiving SIGINT.
PERF_FLUSH_WAIT = 5

DEFAULT_VM_NAME = 'd-switch-001'


class E1000TxWatchdogDebugCollector:
    """Start/stop the three parallel debug collectors and pull the results into the dumps folder."""

    def __init__(self, topology_obj):
        self.topology_obj = topology_obj
        self.dut_engine = topology_obj.players['dut']['engine']
        self.hyper_engine = topology_obj.players['hypervisor']['engine']
        self.container_name = topology_obj.players['dut']['attributes'].noga_query_data[
            'attributes']['Common']['Name']
        self.vm_name = None
        self.perf_available = False

    # ---------------------------------------------------------------- helpers
    def _get_dut_vm_name(self):
        """Discover the libvirt domain name inside the SimX container, falling back to the default."""
        try:
            container = shlex.quote(self.container_name)
            output = self.hyper_engine.run_cmd(
                f'sudo docker exec {container} virsh list --name --state-running', validate=False)
            for line in output.splitlines():
                name = line.strip()
                if name and name.startswith('d-switch'):
                    return name
        except Exception as err:
            logger.warning(f'Could not discover SimX VM name, falling back to {DEFAULT_VM_NAME}: {err}')
        return DEFAULT_VM_NAME

    def _is_perf_available(self):
        try:
            output = self.hyper_engine.run_cmd('which perf', validate=False)
            return bool(output) and 'no perf' not in output and '/perf' in output
        except Exception as err:
            logger.warning(f'Could not check for perf on hypervisor: {err}')
            return False

    # ------------------------------------------------------------------ start
    def start(self):
        """Start all three collectors. Returns True if start was attempted, False if pre-checks failed."""
        self.vm_name = self._get_dut_vm_name()
        logger.info(f'Starting e1000 TX-watchdog debug collection (container={self.container_name}, '
                    f'vm={self.vm_name})')
        with allure.step('Start e1000 TX-watchdog enhanced debug collection'):
            self._start_guest_irq_softirq()
            self._start_simx_info_irq()
            self._start_host_perf_sched()
        return True

    def _start_guest_irq_softirq(self):
        """Guest: sample /proc/interrupts and /proc/softirqs ~every 0.5s."""
        try:
            loop = (f': {GUEST_LOOP_MARKER}; '
                    f'while true; do date +%s.%N; cat /proc/interrupts /proc/softirqs; '
                    f'sleep {SAMPLE_INTERVAL}; done > {GUEST_LOG} 2>&1')
            self.dut_engine.run_cmd(f'sudo rm -f {GUEST_LOG}', validate=False)
            self.dut_engine.run_cmd(f"nohup bash -c {shlex.quote(loop)} >/dev/null 2>&1 & disown",
                                    validate=False)
            logger.info(f'Started guest irq/softirq sampling -> {GUEST_LOG}')
        except Exception as err:
            logger.error(f'Failed to start guest irq/softirq sampling: {err}')

    def _start_simx_info_irq(self):
        """SimX container: sample QEMU 'info irq' via virsh ~every 0.5s."""
        try:
            container = shlex.quote(self.container_name)
            vm = shlex.quote(self.vm_name)
            inner = (f': {SIMX_LOOP_MARKER}; '
                     f'while true; do date +%s.%N; '
                     f'virsh qemu-monitor-command {vm} --hmp "info irq"; '
                     f'sleep {SAMPLE_INTERVAL}; done > {SIMX_LOG} 2>&1')
            self.hyper_engine.run_cmd(f'sudo docker exec {container} rm -f {SIMX_LOG}', validate=False)
            self.hyper_engine.run_cmd(
                f"sudo nohup docker exec {container} sh -c {shlex.quote(inner)} >/dev/null 2>&1 & disown",
                validate=False)
            logger.info(f'Started SimX QEMU info-irq sampling -> {self.container_name}:{SIMX_LOG}')
        except Exception as err:
            logger.error(f'Failed to start SimX QEMU info-irq sampling: {err}')

    def _start_host_perf_sched(self):
        """Hypervisor host: record scheduler events with perf."""
        self.perf_available = self._is_perf_available()
        if not self.perf_available:
            logger.warning('perf is not available on the hypervisor, skipping perf sched recording')
            return
        try:
            # Run perf under sudo sh so that $! is the perf pid (not sudo's) and can be signalled later.
            start_cmd = (f"sudo sh -c 'rm -f {HOST_PERF_DATA} {HOST_PERF_PID}; "
                         f'nohup perf sched record -a -o {HOST_PERF_DATA} >{HOST_PERF_OUT} 2>&1 & '
                         f"echo $! > {HOST_PERF_PID}'")
            self.hyper_engine.run_cmd(start_cmd, validate=False)
            logger.info(f'Started host perf sched recording -> {HOST_PERF_DATA}')
        except Exception as err:
            logger.error(f'Failed to start host perf sched recording: {err}')
            self.perf_available = False

    # ------------------------------------------------------------ stop/collect
    def stop_and_collect(self, dumps_folder, name_prefix):
        """Stop all collectors and copy their output into the dumps folder."""
        logger.info('Stopping e1000 TX-watchdog debug collection and collecting logs')
        with allure.step('Stop e1000 TX-watchdog debug collection and collect logs'):
            self._stop_collectors()
            collected = []
            collected += self._collect_guest_log(dumps_folder, name_prefix)
            collected += self._collect_simx_log(dumps_folder, name_prefix)
            collected += self._collect_host_perf(dumps_folder, name_prefix)
            if collected:
                allure.attach('\n'.join(collected), 'e1000_tx_watchdog_debug_logs',
                              allure.attachment_type.TEXT)
            logger.info(f'e1000 TX-watchdog debug logs collected: {collected}')
        return collected

    def _stop_collectors(self):
        # Guest loop
        try:
            self.dut_engine.run_cmd(f'pkill -f {GUEST_LOOP_MARKER} || true', validate=False)
        except Exception as err:
            logger.error(f'Failed to stop guest irq/softirq sampling: {err}')
        # SimX loop (inside container first, then any leftover docker exec client on the host)
        try:
            container = shlex.quote(self.container_name)
            self.hyper_engine.run_cmd(
                f'sudo docker exec {container} pkill -f {SIMX_LOOP_MARKER} || true', validate=False)
            self.hyper_engine.run_cmd(f'sudo pkill -f {SIMX_LOOP_MARKER} || true', validate=False)
        except Exception as err:
            logger.error(f'Failed to stop SimX QEMU info-irq sampling: {err}')
        # perf: SIGINT so it finalizes sched.data, then wait for the flush
        if self.perf_available:
            try:
                self.hyper_engine.run_cmd(
                    f"sudo sh -c 'kill -INT $(cat {HOST_PERF_PID}) 2>/dev/null || true'", validate=False)
                time.sleep(PERF_FLUSH_WAIT)
            except Exception as err:
                logger.error(f'Failed to stop host perf sched recording: {err}')

    def _collect_guest_log(self, dumps_folder, name_prefix):
        dest = f'{dumps_folder}/{name_prefix}_guest_irq_softirq.log'
        try:
            self.dut_engine.copy_file(source_file=GUEST_LOG, dest_file=dest, file_system='/',
                                      direction='get', overwrite_file=True, verify_file=False)
            self.dut_engine.run_cmd(f'sudo rm -f {GUEST_LOG}', validate=False)
            return [dest]
        except Exception as err:
            logger.error(f'Failed to collect guest irq/softirq log: {err}')
            return []

    def _collect_simx_log(self, dumps_folder, name_prefix):
        dest = f'{dumps_folder}/{name_prefix}_info_irq.log'
        try:
            container = shlex.quote(self.container_name)
            self.hyper_engine.run_cmd(f'sudo docker cp {container}:{SIMX_LOG} {shlex.quote(dest)}',
                                      validate=False)
            self.hyper_engine.run_cmd(f'sudo docker exec {container} rm -f {SIMX_LOG} || true',
                                      validate=False)
            return [dest]
        except Exception as err:
            logger.error(f'Failed to collect SimX QEMU info-irq log: {err}')
            return []

    def _collect_host_perf(self, dumps_folder, name_prefix):
        if not self.perf_available:
            return []
        # perf sched.data is binary and large, so compress it (together with perf's own stdout/stderr).
        dest = f'{dumps_folder}/{name_prefix}_perf_sched.tar.gz'
        try:
            self.hyper_engine.run_cmd(f'sudo chmod a+r {HOST_PERF_DATA} {HOST_PERF_OUT} 2>/dev/null || true',
                                      validate=False)
            self.hyper_engine.run_cmd(
                f'sudo tar -czf {shlex.quote(dest)} -C /tmp sched.data perf_sched.out', validate=False)
            self.hyper_engine.run_cmd(
                f'sudo rm -f {HOST_PERF_DATA} {HOST_PERF_OUT} {HOST_PERF_PID}', validate=False)
            return [dest]
        except Exception as err:
            logger.error(f'Failed to collect host perf sched data: {err}')
            return []


@contextmanager
def collect_e1000_tx_watchdog_debug(topology_obj, dumps_folder, name_prefix):
    """
    Context-manager helper: start the three parallel debug collectors, yield for the test body,
    then stop and collect the logs. Never raises - all errors are logged.

    Usage::

        with collect_e1000_tx_watchdog_debug(topology_obj, dumps_folder, name_prefix):
            <run the test body>
    """
    collector = E1000TxWatchdogDebugCollector(topology_obj)
    started = False
    try:
        started = collector.start()
    except Exception as err:
        logger.error(f'Failed to start e1000 TX-watchdog debug collection: {err}')
    try:
        yield
    finally:
        if started:
            try:
                collector.stop_and_collect(dumps_folder, name_prefix)
            except Exception as err:
                logger.error(f'Failed to stop/collect e1000 TX-watchdog debug logs: {err}')
