import pytest

from ngts.nvos_constants.constants_nvos import Cpov2Consts
from ngts.tests_nvos.platform.cpo.action_helpers import (
    assert_mapping_unchanged,
    assert_no_counter_increase,
    carrier_down_count,
    error_drop_counters,
    laser_sibling_ports,
    mapping_snapshot,
    poll_until,
)
from ngts.tests_nvos.unit_tests.cpo import sample_outputs as samples


def test_mapping_snapshot_survives_reset_payload_refresh():
    cpo = samples.SHOW_PLATFORM_CPO_DETAIL["cpo1"]
    els = samples.SHOW_PLATFORM_LASER_SOURCE_DETAIL["els1"]
    before = mapping_snapshot(cpo, els)
    assert_mapping_unchanged(before, dict(cpo), dict(els))


def test_mapping_change_is_reported():
    cpo = samples.SHOW_PLATFORM_CPO_DETAIL["cpo1"]
    els = samples.SHOW_PLATFORM_LASER_SOURCE_DETAIL["els1"]
    before = mapping_snapshot(cpo, els)
    changed = dict(cpo)
    changed[Cpov2Consts.PORTS] = "sw99p1s1"
    with pytest.raises(AssertionError, match="mapping changed"):
        assert_mapping_unchanged(before, changed, els)


def test_carrier_down_count_from_real_nvl6_capture():
    """The link-bounce counter extracts from the real counters payload."""
    assert carrier_down_count(samples.SHOW_INTERFACE_ACP_COUNTERS_NVL6) == 0


def test_carrier_down_count_rejects_missing_sections():
    with pytest.raises(AssertionError, match="no link section"):
        carrier_down_count({"in-errors": 0})
    with pytest.raises(AssertionError, match="no carrier-down-count"):
        carrier_down_count({"link": {"error-recovery": 0}})


@pytest.mark.parametrize(
    ("port", "siblings"),
    [
        ("sw1p1s1", ["sw1p1s2", "sw1p1s3", "sw1p1s4"]),
        ("sw1p1s4", ["sw1p1s1", "sw1p1s2", "sw1p1s3"]),
        ("sw1p1s5", ["sw1p1s6", "sw1p1s7", "sw1p1s8"]),
        ("sw1p1s8", ["sw1p1s5", "sw1p1s6", "sw1p1s7"]),
    ],
)
def test_laser_sibling_ports(port, siblings):
    inventory = [f"sw1p1s{lane}" for lane in range(1, 9)]
    assert laser_sibling_ports(port, inventory) == siblings


def test_laser_sibling_ports_rejects_incomplete_inventory():
    with pytest.raises(AssertionError, match="incomplete"):
        laser_sibling_ports("sw1p1s1", ["sw1p1s1", "sw1p1s2"])


def test_error_drop_counters_flatten_real_nvl6_capture():
    """Real `nv show interface <port> counters` payload (rosalind-mec-2164).

    Nested error/drop paths flatten with dots, 'n/a' directions are skipped,
    and traffic/link-bounce counters (in-pkts, link.carrier-down-count) stay
    out of the error/drop snapshot.
    """
    counters = error_drop_counters(samples.SHOW_INTERFACE_ACP_COUNTERS_NVL6)
    assert counters == {
        "buffer-overrun-errors": 0,
        "in-drops": 0,
        "in-errors": 0,
        "link.error-recovery": 0,
        "link.local-integrity-errors": 0,
        "link.port-rcv-constraint-errors": 0,
        "link.port-rcv-remote-physical-errors": 0,
        "link.port-rcv-switch-relay-errors": 0,
        "nvl.drops.qp1-drops.receive": 0,
        "nvl.drops.qp1-drops.transmit": 0,
        "nvl.errors.icrc-errors.receive": 0,
        "nvl.errors.symbol-errors.receive": 0,
        "nvl.errors.tx-parity-errors.receive": 0,
        "nvl.errors.tx-parity-errors.transmit": 0,
        "out-drops": 0,
        "out-errors": 0,
    }


def test_error_drop_counter_snapshot_and_comparison():
    before = error_drop_counters(
        {
            "in-errors": "0",
            "nested": {"out-drops": "1,000", "packets": 5},
            "errors": {"crc": "2"},
        }
    )
    assert before == {"in-errors": 0, "nested.out-drops": 1000, "errors.crc": 2}
    assert_no_counter_increase(before, dict(before), "siblings")
    with pytest.raises(AssertionError, match="increased"):
        assert_no_counter_increase(before, {**before, "in-errors": 1}, "siblings")


def test_poll_until_returns_first_matching_value_without_sleeping():
    values = iter(["Removed", "Inserted"])
    now = iter([0.0, 0.0, 0.1])
    sleeps = []
    result = poll_until(
        lambda: next(values),
        lambda value: value == "Inserted",
        timeout_seconds=1,
        description="inserted state",
        interval_seconds=0.1,
        clock=lambda: next(now),
        sleep=sleeps.append,
    )
    assert result == "Inserted"
    assert sleeps == [0.1]


def test_poll_until_reports_last_value_on_timeout():
    now = iter([0.0, 0.0, 2.0])
    with pytest.raises(AssertionError, match="last value: 'Removed'"):
        poll_until(
            lambda: "Removed",
            lambda value: value == "Inserted",
            timeout_seconds=1,
            description="inserted state",
            clock=lambda: next(now),
            sleep=lambda _: None,
        )


def test_poll_until_tolerates_transient_read_failures():
    reads = iter([AssertionError("cli blip mid-reset"), "Inserted"])

    def read():
        value = next(reads)
        if isinstance(value, Exception):
            raise value
        return value

    now = iter([0.0, 0.0, 0.1])
    sleeps = []
    result = poll_until(
        read,
        lambda value: value == "Inserted",
        timeout_seconds=1,
        description="inserted state",
        interval_seconds=0.1,
        acceptable_exceptions=(AssertionError,),
        clock=lambda: next(now),
        sleep=sleeps.append,
    )
    assert result == "Inserted"
    assert sleeps == [0.1]


def test_poll_until_reraises_last_read_failure_on_timeout():
    def read():
        raise AssertionError("cli blip mid-reset")

    now = iter([0.0, 0.0, 2.0])
    with pytest.raises(AssertionError, match="last read failed: cli blip mid-reset"):
        poll_until(
            read,
            lambda _: True,
            timeout_seconds=1,
            description="inserted state",
            acceptable_exceptions=(AssertionError,),
            clock=lambda: next(now),
            sleep=lambda _: None,
        )


def test_poll_until_propagates_unexpected_read_exceptions():
    def read():
        raise KeyError("missing field")

    with pytest.raises(KeyError, match="missing field"):
        poll_until(
            read,
            lambda _: True,
            timeout_seconds=1,
            description="inserted state",
            acceptable_exceptions=(AssertionError,),
            clock=iter([0.0]).__next__,
            sleep=lambda _: None,
        )
