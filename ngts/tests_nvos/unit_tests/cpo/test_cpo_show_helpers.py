import copy

import pytest

from ngts.nvos_constants.constants_nvos import Cpov2Consts, HealthConsts
from ngts.nvos_tools.platform.Cpo import Cpo
from ngts.tests_nvos.platform.cpo.helpers import (
    sample_names,
    validate_cpo_detail,
    validate_cpo_summary,
    validate_healthy_instances,
    validate_interface_cpo,
    validate_laser_source_detail,
    validate_laser_source_summary,
)
from ngts.tests_nvos.unit_tests.cpo import sample_outputs as samples

TOPOLOGY = samples.TOPOLOGY


def test_show_fixtures_satisfy_field_contracts():
    validate_cpo_summary(samples.SHOW_PLATFORM_CPO, TOPOLOGY)
    validate_laser_source_summary(samples.SHOW_PLATFORM_LASER_SOURCE, TOPOLOGY)
    for cpo, detail in samples.SHOW_PLATFORM_CPO_DETAIL.items():
        validate_cpo_detail(cpo, detail, TOPOLOGY)
    for els, detail in samples.SHOW_PLATFORM_LASER_SOURCE_DETAIL.items():
        validate_laser_source_detail(els, detail, TOPOLOGY)


def test_interface_fixture_is_platform_subset():
    interface_detail = samples.SHOW_INTERFACE_CPO_SW9P1S1
    parent = interface_detail[Cpov2Consts.PARENT]
    assert validate_interface_cpo("sw9p1s1", interface_detail, samples.SHOW_PLATFORM_CPO_DETAIL[parent]) == parent


def test_interface_header_must_be_inherited_from_parent():
    interface_detail = copy.deepcopy(samples.SHOW_INTERFACE_CPO_SW9P1S1)
    parent = interface_detail[Cpov2Consts.PARENT]
    interface_detail[Cpov2Consts.PORTS] = "sw9p1s1"
    with pytest.raises(AssertionError, match="differs from its parent CPO header"):
        validate_interface_cpo("sw9p1s1", interface_detail, samples.SHOW_PLATFORM_CPO_DETAIL[parent])


def test_interface_must_show_only_its_own_channel_slice():
    interface_detail = copy.deepcopy(samples.SHOW_INTERFACE_CPO_SW9P1S1)
    parent = interface_detail[Cpov2Consts.PARENT]
    interface_detail[Cpov2Consts.CHANNEL] = copy.deepcopy(samples.SHOW_PLATFORM_CPO_DETAIL[parent][Cpov2Consts.CHANNEL])
    with pytest.raises(AssertionError, match="channel slice"):
        validate_interface_cpo("sw9p1s1", interface_detail, samples.SHOW_PLATFORM_CPO_DETAIL[parent])


def test_detailed_topology_maps_include_channels():
    maps = Cpo.build_topology_maps(samples.SHOW_PLATFORM_CPO_DETAIL)
    assert maps["cpo_to_channels"]["cpo1"] == TOPOLOGY.channels_for_cpo("cpo1")
    maps = Cpo.build_topology_maps(samples.SHOW_PLATFORM_CPO)
    assert "cpo_to_channels" not in maps


def test_sample_names_covers_boundaries_and_middle_without_duplicates():
    assert sample_names(["one"]) == ["one"]
    assert sample_names(["one", "two", "three", "four"]) == ["one", "three", "four"]


def test_missing_required_field_is_reported():
    detail = copy.deepcopy(samples.SHOW_PLATFORM_CPO_DETAIL["cpo1"])
    del detail[Cpov2Consts.STATUS]
    with pytest.raises(AssertionError, match=Cpov2Consts.STATUS):
        validate_cpo_detail("cpo1", detail, TOPOLOGY)


def test_health_contract_requires_expected_healthy_instances():
    health = {
        HealthConsts.Component.CPO: {
            HealthConsts.Component.INSTANCE: {
                cpo: {
                    HealthConsts.Component.STATE: HealthConsts.Component.HEALTHY,
                    HealthConsts.Component.UNHEALTHY_COUNT: "0",
                }
                for cpo in TOPOLOGY.cpo_names()
            }
        }
    }
    validate_healthy_instances(HealthConsts.Component.CPO, health, TOPOLOGY.cpo_names())
    health[HealthConsts.Component.CPO][HealthConsts.Component.INSTANCE]["cpo1"][
        HealthConsts.Component.UNHEALTHY_COUNT
    ] = "1"
    with pytest.raises(AssertionError, match=HealthConsts.Component.UNHEALTHY_COUNT):
        validate_healthy_instances(HealthConsts.Component.CPO, health, TOPOLOGY.cpo_names())
