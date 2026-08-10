"""Offline dry-run command assertions for Gen2 CPO.

Verifies that every CPO component/action produces the exact command defined in
CPO_HLD.md - for NVUE the full `nv ...` string (captured via a fake engine, so
the real command-building code runs end-to-end), and for OpenAPI the REST
path + payload (captured by stubbing OpenApiCommandHelper). No DUT involved.

Run offline (no setup) with:
    python -m pytest ngts/tests_nvos/unit_tests/cpo -c ngts/pytest.ini \
        -o filterwarnings=ignore --noconftest

NOTE: "no DUT" does not mean "no network": importing BaseComponent transitively
imports devts redmine_api, which calls redmine.mellanox.com AT IMPORT TIME
(devts issue - module-level get_issue_status() with @retry). On a box without
lab network/VPN, collection hangs on that retry loop.
"""

import pytest

from ngts.nvos_constants.constants_nvos import ApiType, Cpov2Consts, OpenApiReqType
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit


class FakeEngine:
    """Stands in for LinuxSshEngine and its inner netmiko engine.

    NVUE `show`/`set`/`unset` go through run_cmd; NVUE actions go through
    send_command_timing (plus an `echo $?` return-code probe, which is answered
    but not recorded). OpenAPI code only reads the connection attributes.
    """

    ip = "192.0.2.1"
    ssh_port = 22
    open_api_port = 443
    username = "admin"
    password = "admin"

    def __init__(self):
        self.commands: list[str] = []
        self.engine = self  # .engine.username / .engine.send_command_timing(...)

    def run_cmd(self, cmd: str, **kwargs) -> str:
        self.commands.append(cmd)
        return "{}"

    def send_command_timing(self, cmd: str, **kwargs) -> str:
        if cmd.strip() == "echo $?":
            return "0"
        self.commands.append(cmd)
        return "Action succeeded"

    def find_prompt(self) -> str:
        return "$"


@pytest.fixture
def engine() -> FakeEngine:
    return FakeEngine()


@pytest.fixture
def no_reboot_check(monkeypatch):
    """BaseComponent.action probes ssh liveness after every action - stub it out."""
    monkeypatch.setattr(
        "ngts.nvos_tools.infra.BaseComponent.check_port_status_till_alive",
        lambda *args, **kwargs: None,
    )


@pytest.fixture
def nvue(monkeypatch, no_reboot_check):
    monkeypatch.setattr(TestToolkit, "tested_api", ApiType.NVUE)


@pytest.fixture
def openapi(monkeypatch, no_reboot_check):
    monkeypatch.setattr(TestToolkit, "tested_api", ApiType.OPENAPI)


@pytest.fixture
def rest_calls(monkeypatch) -> list[dict]:
    """Capture OpenAPI requests instead of sending HTTP."""
    from ngts.cli_wrappers.openapi.openapi_command_builder import OpenApiCommandHelper

    calls: list[dict] = []

    def fake_execute_script(username, password, req_type, ip, port, resource_path, *rest, **kwargs):
        calls.append({"method": req_type, "path": resource_path, "params": list(rest)})
        return "{}"

    def fake_execute_action(action_key, username, password, ip, port, url, data, *rest, **kwargs):
        calls.append({"action": action_key, "path": url, "data": data})
        return "Action succeeded"

    monkeypatch.setattr(OpenApiCommandHelper, "execute_script", staticmethod(fake_execute_script))
    monkeypatch.setattr(OpenApiCommandHelper, "execute_action", staticmethod(fake_execute_action))
    return calls


def _platform():
    from ngts.nvos_tools.platform.Platform import Platform

    return Platform()


def _fae():
    from ngts.nvos_tools.infra.Fae import Fae

    return Fae()


def _interface(port_name: str):
    from ngts.nvos_tools.ib.InterfaceConfiguration.Interface import Interface

    return Interface(None, port_name)


class TestNvueShowCommands:
    SUFFIX = " --output json --color off"

    def test_show_platform_cpo(self, nvue, engine):
        platform = _platform()
        platform.cpo.show(dut_engine=engine)
        assert engine.commands[-1] == "nv show platform cpo" + self.SUFFIX

        platform.cpo.cpo_id["cpo1"].show(dut_engine=engine)
        assert engine.commands[-1] == "nv show platform cpo cpo1" + self.SUFFIX

    def test_show_platform_cpo_drill_down(self, nvue, engine):
        """Drill-down paths include the YANG list-container segment (oe/channel)."""
        cpo1 = _platform().cpo.cpo_id["cpo1"]

        cpo1.oe.show(dut_engine=engine)
        assert engine.commands[-1] == "nv show platform cpo cpo1 optical-engines" + self.SUFFIX

        cpo1.oe.oe_id["oe1"].show(dut_engine=engine)
        assert engine.commands[-1] == "nv show platform cpo cpo1 optical-engines oe1" + self.SUFFIX

        cpo1.channel.channel_id["channel-3"].show(dut_engine=engine)
        assert engine.commands[-1] == "nv show platform cpo cpo1 channels channel-3" + self.SUFFIX

    def test_show_platform_laser_source(self, nvue, engine):
        platform = _platform()
        platform.laser_source.show(dut_engine=engine)
        assert engine.commands[-1] == "nv show platform laser-source" + self.SUFFIX

        platform.laser_source.els_id["els1"].show(dut_engine=engine)
        assert engine.commands[-1] == "nv show platform laser-source els1" + self.SUFFIX

        platform.laser_source.els_id["els1"].laser.laser_id["laser-2"].show(dut_engine=engine)
        assert engine.commands[-1] == "nv show platform laser-source els1 laser laser-2" + self.SUFFIX

    def test_show_interface_cpo(self, nvue, engine):
        interface_cpo = _interface("sw1p1s1").cpo
        interface_cpo.show(dut_engine=engine)
        assert engine.commands[-1] == "nv show interface sw1p1s1 cpo" + self.SUFFIX

        interface_cpo.oe.oe_id["oe2"].show(dut_engine=engine)
        assert engine.commands[-1] == "nv show interface sw1p1s1 cpo optical-engines oe2" + self.SUFFIX

    def test_show_fae_system_cpo(self, nvue, engine):
        fae = _fae()
        fae.system.cpo.show(dut_engine=engine)
        assert engine.commands[-1] == "nv show fae system cpo" + self.SUFFIX

        fae.system.cpo.show(Cpov2Consts.ELS_INITIALIZATION, dut_engine=engine)
        assert engine.commands[-1] == "nv show fae system cpo els-initialization" + self.SUFFIX

        fae.system.cpo.show(Cpov2Consts.ELS_INITIALIZATION_PER_LASER, dut_engine=engine)
        assert engine.commands[-1] == "nv show fae system cpo els-initialization-per-laser" + self.SUFFIX


class TestNvueActionCommands:
    def test_reset_cpo(self, nvue, engine):
        result = _platform().cpo.cpo_id["cpo1"].action_reset(engine=engine)
        assert result.result, result.info
        assert engine.commands[-1] == "nv action reset platform cpo cpo1"

    def test_reset_laser_source(self, nvue, engine):
        platform = _platform()
        platform.laser_source.els_id["els1"].action_reset(engine=engine)
        assert engine.commands[-1] == "nv action reset platform laser-source els1"

    def test_reset_laser_source_single_laser(self, nvue, engine):
        # per HLD sample the laser is positional: '... els1 laser-2' (no keyword)
        platform = _platform()
        platform.laser_source.els_id["els1"].action_reset("laser-2", engine=engine)
        assert engine.commands[-1] == "nv action reset platform laser-source els1 laser-2"

    def test_fae_activate_laser_source(self, nvue, engine):
        fae = _fae()
        els1 = fae.platform.laser_source.els_id["els1"]

        els1.action_activate(engine=engine)
        assert engine.commands[-1] == "nv action activate fae platform laser-source els1"

        els1.action_activate(laser_id="laser-4", engine=engine)
        assert engine.commands[-1] == "nv action activate fae platform laser-source els1 laser laser-4"

        els1.action_activate(laser_id="laser-4", step=Cpov2Consts.STEP_LASER_UP, engine=engine)
        assert engine.commands[-1] == ("nv action activate fae platform laser-source els1 laser laser-4 step laser-up")

        els1.action_activate(step=Cpov2Consts.STEP_LASER_FINE_TUNE, engine=engine)
        assert engine.commands[-1] == ("nv action activate fae platform laser-source els1 step laser-fine-tune")


class TestNvueSetUnsetCommands:
    def test_set_cpo_dump_state(self, nvue, engine):
        fae = _fae()
        fae.system.cpo.set(Cpov2Consts.CPO_DUMP_STATE, "disabled", dut_engine=engine)
        assert engine.commands[-1] == "nv set fae system cpo cpo-dump-state disabled"

    def test_unset_cpo_dump_state(self, nvue, engine):
        fae = _fae()
        fae.system.cpo.unset(Cpov2Consts.CPO_DUMP_STATE, dut_engine=engine)
        assert engine.commands[-1] == "nv unset fae system cpo cpo-dump-state"

    def test_platform_cpo_set_unset_not_supported(self, nvue):
        platform = _platform()
        with pytest.raises(Exception, match="not implemented"):
            platform.cpo.set("param", "value")
        with pytest.raises(Exception, match="not implemented"):
            platform.laser_source.unset("param")

    def test_activate_rejected_on_non_fae_mount(self, nvue, engine):
        """The same component class is mounted under /platform and
        /fae/platform, but `nv action activate ... laser-source` only exists
        on the fae mount - the non-FAE instance must refuse to build it."""
        platform = _platform()
        with pytest.raises(Exception, match="FAE-only"):
            platform.laser_source.els_id["els1"].action_activate(engine=engine)


class TestOpenApiCommands:
    def test_show_paths(self, openapi, engine, rest_calls):
        platform = _platform()
        platform.cpo.show(dut_engine=engine)
        assert rest_calls[-1]["method"] == OpenApiReqType.GET
        assert rest_calls[-1]["path"] == "/platform/cpo"

        platform.cpo.cpo_id["cpo1"].show(dut_engine=engine)
        assert rest_calls[-1]["path"] == "/platform/cpo/cpo1"

        platform.laser_source.els_id["els1"].show(dut_engine=engine)
        assert rest_calls[-1]["path"] == "/platform/laser-source/els1"

        _interface("sw1p1s1").cpo.show(dut_engine=engine)
        assert rest_calls[-1]["path"] == "/interface/sw1p1s1/cpo"

    def test_show_drill_down_paths(self, openapi, engine, rest_calls):
        platform = _platform()
        platform.cpo.cpo_id["cpo1"].oe.oe_id["oe1"].show(dut_engine=engine)
        assert rest_calls[-1]["path"] == "/platform/cpo/cpo1/optical-engines/oe1"

        platform.cpo.cpo_id["cpo1"].channel.channel_id["channel-3"].show(dut_engine=engine)
        assert rest_calls[-1]["path"] == "/platform/cpo/cpo1/channels/channel-3"

        platform.laser_source.els_id["els1"].laser.laser_id["laser-2"].show(dut_engine=engine)
        assert rest_calls[-1]["path"] == "/platform/laser-source/els1/laser/laser-2"

    def test_reset_actions(self, openapi, engine, rest_calls):
        platform = _platform()
        platform.cpo.cpo_id["cpo1"].action_reset(engine=engine)
        assert rest_calls[-1] == {
            "action": "@reset",
            "path": "/platform/cpo/cpo1",
            "data": {"state": "start", "parameters": {}},
        }

        platform.laser_source.els_id["els1"].action_reset("laser-2", engine=engine)
        assert rest_calls[-1] == {
            "action": "@reset",
            "path": "/platform/laser-source/els1",
            "data": {"state": "start", "parameters": {"laser": "laser-2"}},
        }

    def test_fae_actions(self, openapi, engine, rest_calls):
        fae = _fae()
        fae.platform.laser_source.els_id["els1"].action_activate(
            laser_id="laser-4", step=Cpov2Consts.STEP_LASER_UP, engine=engine
        )
        assert rest_calls[-1] == {
            "action": "@activate",
            "path": "/fae/platform/laser-source/els1",
            "data": {
                "state": "start",
                "parameters": {"laser": "laser-4", "step": "laser-up"},
            },
        }

    def test_set_unset_cpo_dump_state(self, openapi, engine, rest_calls):
        fae = _fae()
        fae.system.cpo.set(Cpov2Consts.CPO_DUMP_STATE, "disabled", dut_engine=engine)
        assert rest_calls[-1]["method"] == OpenApiReqType.PATCH
        assert rest_calls[-1]["path"] == "/fae/system/cpo"
        assert {Cpov2Consts.CPO_DUMP_STATE: "disabled"} in rest_calls[-1]["params"]

        fae.system.cpo.unset(Cpov2Consts.CPO_DUMP_STATE, dut_engine=engine)
        assert rest_calls[-1]["method"] == OpenApiReqType.DELETE
        assert rest_calls[-1]["path"] == "/fae/system/cpo"
        assert Cpov2Consts.CPO_DUMP_STATE in rest_calls[-1]["params"]
