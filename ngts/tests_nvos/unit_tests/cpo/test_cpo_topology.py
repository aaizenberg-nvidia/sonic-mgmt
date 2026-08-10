"""Offline unit tests for CpoTopology (Gen2 CPO expected-topology model).

Pure Python - no DUT, no engines.

Run offline (no setup) with:
    python -m pytest ngts/tests_nvos/unit_tests/cpo -c ngts/pytest.ini \
        -o filterwarnings=ignore --noconftest
(--noconftest skips the tests_nvos conftest, which otherwise requires a Noga
setup_name; these tests use no conftest fixtures.)
"""

import dataclasses

import pytest

from ngts.nvos_tools.Devices.cpo.CpoTopology import CpoTopology

PORTIA_SIMX_CPO_COUNT = 4
PORTIA_LANES_PER_LASER = 4  # 64 channels / 16 lasers per (single) ELS


@pytest.fixture
def topology() -> CpoTopology:
    """Default Portia simx topology: 4 CPOs, 4 OEs + 1 ELS each."""
    return CpoTopology(cpo_count=PORTIA_SIMX_CPO_COUNT)


class TestCounts:
    def test_default_portia_counts(self, topology):
        assert topology.cpo_count == 4
        assert topology.oe_per_cpo == 4
        assert topology.els_per_cpo == 1
        assert topology.lasers_per_els == 16
        assert topology.lanes_per_oe == 16
        assert topology.channels_per_cpo == 64

    def test_derived_counts(self, topology):
        assert topology.oe_count == 16
        assert topology.els_count == 4
        assert topology.lanes_per_laser == PORTIA_LANES_PER_LASER

    def test_single_asic_counts(self):
        single = CpoTopology(cpo_count=1)
        assert single.oe_count == 4
        assert single.els_count == 1

    def test_invalid_cpo_count_rejected(self):
        with pytest.raises(ValueError):
            CpoTopology(cpo_count=0)
        with pytest.raises(ValueError):
            CpoTopology(cpo_count=-1)

    def test_all_cardinalities_must_be_positive(self):
        """PortiaCpoCapability exposes every count as a public override - a
        zero/negative topology must not construct."""
        with pytest.raises(ValueError, match="oe_per_cpo"):
            CpoTopology(cpo_count=4, oe_per_cpo=0)
        with pytest.raises(ValueError, match="els_per_cpo"):
            CpoTopology(cpo_count=4, els_per_cpo=0)
        with pytest.raises(ValueError, match="lasers_per_els"):
            CpoTopology(cpo_count=4, lasers_per_els=-16)
        with pytest.raises(ValueError, match="lanes_per_oe"):
            CpoTopology(cpo_count=4, lanes_per_oe=0)
        with pytest.raises(ValueError, match="channels_per_cpo"):
            CpoTopology(cpo_count=4, channels_per_cpo=0)

    def test_channels_must_cover_every_laser(self):
        """lanes_per_laser is a floor division, so too few channels for the
        laser count would silently yield 0 instead of failing here."""
        with pytest.raises(ValueError, match="cannot feed"):
            CpoTopology(cpo_count=2, oe_per_cpo=2, lanes_per_oe=2, channels_per_cpo=4)

    def test_out_of_range_identifiers_rejected(self, topology):
        """Out-of-range or wrong-prefix identifiers must raise instead of
        silently generating phantom names (e.g. oes_for_cpo(0) -> 'oe-3')."""
        for bad_call in (
            lambda: topology.oes_for_cpo(0),
            lambda: topology.oes_for_cpo("cpo99"),
            lambda: topology.oes_for_cpo("els1"),
            lambda: topology.els_for_cpo(5),
            lambda: topology.cpo_for_els("els0"),
            lambda: topology.channels_for_cpo("cpo9"),
            lambda: topology.lasers_for_els("els9"),
        ):
            with pytest.raises(ValueError):
                bad_call()

    def test_frozen(self, topology):
        with pytest.raises(dataclasses.FrozenInstanceError):
            topology.cpo_count = 8


class TestNames:
    def test_cpo_names(self, topology):
        assert topology.cpo_names() == ["cpo1", "cpo2", "cpo3", "cpo4"]

    def test_els_names(self, topology):
        assert topology.els_names() == ["els1", "els2", "els3", "els4"]

    def test_oe_names_global(self, topology):
        names = topology.oe_names()
        assert names[0] == "oe1"
        assert names[-1] == "oe16"
        assert len(names) == 16

    def test_laser_names_per_els(self, topology):
        names = topology.laser_names()
        assert names[0] == "laser-1"
        assert names[-1] == "laser-16"
        assert len(names) == 16

    def test_channel_names_per_cpo(self, topology):
        names = topology.channel_names()
        assert names[0] == "channel-1"
        assert names[-1] == "channel-64"
        assert len(names) == 64


class TestRelationships:
    def test_oes_for_cpo_global(self, topology):
        assert topology.oes_for_cpo("cpo1") == ["oe1", "oe2", "oe3", "oe4"]
        assert topology.oes_for_cpo("cpo3") == ["oe9", "oe10", "oe11", "oe12"]
        assert topology.oes_for_cpo(4) == ["oe13", "oe14", "oe15", "oe16"]

    def test_els_for_cpo(self, topology):
        assert topology.els_for_cpo("cpo1") == ["els1"]
        assert topology.els_for_cpo("cpo4") == ["els4"]

    def test_cpo_for_els(self, topology):
        assert topology.cpo_for_els("els1") == "cpo1"
        assert topology.cpo_for_els("els4") == "cpo4"

    def test_subcomponents_for_cpo(self, topology):
        """Expected gNMI subcomponent references: the CPO's ELS(s) + OEs."""
        assert topology.subcomponents_for_cpo("cpo1") == [
            "els1",
            "oe1",
            "oe2",
            "oe3",
            "oe4",
        ]
        assert topology.subcomponents_for_cpo("cpo3") == [
            "els3",
            "oe9",
            "oe10",
            "oe11",
            "oe12",
        ]

    def test_relationships_are_mutually_consistent(self, topology):
        assert topology.oe_names() == [
            oe for cpo in topology.cpo_names() for oe in topology.oes_for_cpo(cpo)
        ]
        for cpo in topology.cpo_names():
            for els in topology.els_for_cpo(cpo):
                assert topology.cpo_for_els(els) == cpo


class TestAssertConsistent:
    @staticmethod
    def _good_maps(topology: CpoTopology) -> dict:
        return {
            "cpo_to_oes": {c: topology.oes_for_cpo(c) for c in topology.cpo_names()},
            "cpo_to_els": {c: topology.els_for_cpo(c) for c in topology.cpo_names()},
            "cpo_to_channels": {c: topology.channels_for_cpo(c) for c in topology.cpo_names()},
        }

    def test_good_maps_pass(self, topology):
        topology.assert_consistent(**self._good_maps(topology)).verify_result()

    def test_ports_cross_reference_pass(self, topology):
        cpo_to_ports = {"cpo1": ["sw1p1s1", "sw1p1s2"], "cpo2": ["sw8p1s1"]}
        port_to_cpo = {
            "sw1p1s1": "cpo1",
            "sw1p1s2": "cpo1",
            "sw8p1s1": "cpo2",
        }
        topology.assert_consistent(cpo_to_ports=cpo_to_ports, port_to_cpo=port_to_cpo).verify_result()

    def test_missing_cpo_key_fails(self, topology):
        maps = self._good_maps(topology)
        del maps["cpo_to_oes"]["cpo4"]
        info = topology.assert_consistent(cpo_to_oes=maps["cpo_to_oes"]).verify_result(should_succeed=False)
        assert "cpo_to_oes" in info

    def test_wrong_oe_count_fails(self, topology):
        maps = self._good_maps(topology)
        maps["cpo_to_oes"]["cpo1"] = maps["cpo_to_oes"]["cpo1"][:-1]
        topology.assert_consistent(cpo_to_oes=maps["cpo_to_oes"]).verify_result(should_succeed=False)

    def test_duplicated_oe_across_cpos_fails(self, topology):
        maps = self._good_maps(topology)
        maps["cpo_to_oes"]["cpo2"] = maps["cpo_to_oes"]["cpo1"]
        info = topology.assert_consistent(cpo_to_oes=maps["cpo_to_oes"]).verify_result(should_succeed=False)
        assert "cpo2" in info

    def test_oe_ownership_swap_fails(self, topology):
        """Swapping two CPOs' OE sets keeps counts and global uniqueness intact;
        only the per-CPO membership check catches it."""
        maps = self._good_maps(topology)
        oes = maps["cpo_to_oes"]
        oes["cpo1"], oes["cpo2"] = oes["cpo2"], oes["cpo1"]
        info = topology.assert_consistent(cpo_to_oes=oes).verify_result(should_succeed=False)
        assert "cpo1" in info and "cpo2" in info

    def test_duplicate_channel_names_fail(self, topology):
        """A duplicated channel keeps the count right - membership catches it."""
        maps = self._good_maps(topology)
        channels = list(maps["cpo_to_channels"]["cpo1"])
        channels[1] = channels[0]
        maps["cpo_to_channels"]["cpo1"] = channels
        topology.assert_consistent(cpo_to_channels=maps["cpo_to_channels"]).verify_result(should_succeed=False)

    def test_phantom_channel_name_fails(self, topology):
        maps = self._good_maps(topology)
        maps["cpo_to_channels"]["cpo1"][-1] = "channel-999"
        topology.assert_consistent(cpo_to_channels=maps["cpo_to_channels"]).verify_result(should_succeed=False)

    def test_port_mismatch_fails(self, topology):
        cpo_to_ports = {"cpo1": ["sw1p1s1"], "cpo2": ["sw8p1s1"]}
        port_to_cpo = {"sw1p1s1": "cpo2", "sw8p1s1": "cpo2"}
        topology.assert_consistent(
            cpo_to_ports=cpo_to_ports, port_to_cpo=port_to_cpo
        ).verify_result(should_succeed=False)

    def test_one_sided_port_maps_rejected(self, topology):
        """The port check is a two-way cross-reference; supplying only one side
        would silently check nothing, so it must be rejected."""
        with pytest.raises(ValueError):
            topology.assert_consistent(cpo_to_ports={"cpo1": ["sw1p1s1"]})
        with pytest.raises(ValueError):
            topology.assert_consistent(port_to_cpo={"sw1p1s1": "cpo1"})

    def test_unknown_cpo_in_port_maps_fails(self, topology):
        """A port attributed to a nonexistent CPO must fail even when the two
        port maps agree with each other."""
        info = topology.assert_consistent(
            cpo_to_ports={"cpo9": ["sw1p1s1"]},
            port_to_cpo={"sw1p1s1": "cpo9"},
        ).verify_result(should_succeed=False)
        assert "unknown CPO" in info
