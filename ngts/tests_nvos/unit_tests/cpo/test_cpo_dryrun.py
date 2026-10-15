"""Offline dry-run of the Gen2 CPO show tests.

Runs the actual test functions from tests_nvos/platform/cpo/test_cpo_show.py
end-to-end - test body, tool layer, command building, JSON parsing and
validators - against a FakeDutEngine that serves generated sample outputs
instead of an SSH connection (see fake_dut.py, incl. how to add a test).
`devices.dut` is a four-ASIC PortiaCpoSimx test instance, so topology and port
lists use the production device implementation. OpenAPI reuses the NVUE show;
gNMI and DB come from FakeTelemetry. Action/reset/event tests stay DUT-only.

Run offline (no setup) with:
    python -m pytest ngts/tests_nvos/unit_tests/cpo -c ngts/pytest.ini \
        -o filterwarnings=ignore --noconftest
"""

import inspect
import time
from types import SimpleNamespace

import pytest

from ngts.nvos_constants.constants_nvos import ApiType
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.tests_nvos.platform.cpo import helpers
from ngts.tests_nvos.system.telemetry import telemetry_watch
from ngts.tests_nvos.unit_tests.cpo import sample_outputs as samples
from ngts.tests_nvos.unit_tests.cpo.fake_dut import FakeDutEngine, FakeTelemetry, build_show_tree


def _dry_run_tests() -> tuple:
    from ngts.tests_nvos.platform.cpo import test_cpo_show

    return (
        test_cpo_show.test_cpo_show_platform,
        test_cpo_show.test_cpo_show_laser_source,
        test_cpo_show.test_cpo_show_interface,
        test_cpo_show.test_cpo_show_health,
        test_cpo_show.test_cpo_topology_consistency,
    )


@pytest.fixture
def fake_fixtures(monkeypatch) -> dict:
    """Name -> fake for every fixture the dry-run tests may take."""
    monkeypatch.setenv("NVU_SWITCH_NEW_PASSWORD", "dummy")
    monkeypatch.setenv("NVU_SWITCH_USER", "dummy")
    monkeypatch.setenv("NVU_SWITCH_PASSWORD", "dummy")
    from ngts.nvos_tools.Devices.IbDevice import PortiaCpoSimx

    device = PortiaCpoSimx(asic_amount=4)
    # the sample generators are written against this exact topology
    assert device.cpo == samples.TOPOLOGY

    monkeypatch.setattr(TestToolkit, "tested_api", ApiType.NVUE)
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    show_tree = build_show_tree(device)
    telemetry = FakeTelemetry(show_tree, samples.TOPOLOGY)
    nvue_show = telemetry_watch._show
    monkeypatch.setattr(telemetry_watch, "_show", lambda component, api: nvue_show(component, ApiType.NVUE))
    monkeypatch.setattr(telemetry_watch, "gnmi_snapshot", telemetry.snapshot)
    monkeypatch.setattr(telemetry_watch.Tools.DatabaseTool, "sonic_db_cli_hget", telemetry.hget)
    engines = SimpleNamespace(dut=FakeDutEngine(show_tree))
    monkeypatch.setattr(TestToolkit, "engines", engines)
    return {
        "telemetry": telemetry,
        "engines": engines,
        "devices": SimpleNamespace(dut=device),
        "random_api": ApiType.NVUE,
    }


@pytest.mark.parametrize("show_test", _dry_run_tests(), ids=lambda test: test.__name__)
def test_dry_run(show_test, fake_fixtures):
    wanted = inspect.signature(show_test).parameters.keys()
    missing = wanted - fake_fixtures.keys()
    assert not missing, f"{show_test.__name__} needs fixtures {sorted(missing)} - add fakes to fake_fixtures"
    show_test(**{name: fake_fixtures[name] for name in wanted})


CPO1 = "components/component[name=cpo1]"


@pytest.mark.parametrize("break_reference, error", [
    (lambda gnmi: gnmi.pop("components/component[name=oe2]/state/parent"), "gNMI parent"),
    (lambda gnmi: gnmi.update({f"{CPO1}/cpo/physical-channels/channel[index=2]/state/mapping/interface": "sw8p1s1"}),
     "channel mapping/interface only"),
    (lambda gnmi: gnmi.pop("interfaces/interface[name=sw1p1s1]/state/cpo-module"),
     "interface cpo-module/cpo-channels only"),
], ids=["oe parent", "channel interface", "interface cpo-module"])
def test_unmirrored_gnmi_reference_fails_topology(break_reference, error, fake_fixtures):
    from ngts.tests_nvos.platform.cpo.test_cpo_show import test_cpo_topology_consistency

    break_reference(fake_fixtures["telemetry"].gnmi)
    with pytest.raises(AssertionError, match=error):
        test_cpo_topology_consistency(*(fake_fixtures[name] for name in ("engines", "devices", "random_api")))


def test_source_disagreement_fails_parity(fake_fixtures, monkeypatch):
    from ngts.tests_nvos.platform.cpo.test_cpo_show import test_cpo_show_health

    monkeypatch.setattr(helpers, "TELEMETRY_CONVERGENCE_TIMEOUT_SEC", 0)
    fake_fixtures["telemetry"].db[("STATE_DB", "CPO_STATUS|cpo1", "status")] = "down"
    with pytest.raises(AssertionError, match="DB agree on /platform/cpo/cpo1"):
        test_cpo_show_health(*(fake_fixtures[name] for name in ("engines", "devices", "random_api")))
