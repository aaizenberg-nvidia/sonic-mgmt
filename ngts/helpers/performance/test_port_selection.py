"""Unit tests for :mod:`ngts.helpers.performance.port_selection`.

Pure unit tests — no testbed, no switch. Run with::

    pytest ngts/helpers/performance/test_port_selection.py -v
"""
import pytest

import textwrap

from devts.infra.tools.exceptions.test_issue import TestIssue as _TestIssue
from ngts.helpers.performance.port_selection import (PortSelection,
                                                     build_port_selection,
                                                     clear_resolved_excluded_ports,
                                                     get_resolved_excluded_dut_ports,
                                                     port_selection_was_activated,
                                                     resolve_symmetric_cascade,
                                                     set_resolved_excluded_dut_ports)

LEFT = ["swp1s0", "swp1s1", "swp2s0", "swp2s1"]
RIGHT = ["swp33s0", "swp33s1", "swp34s0", "swp34s1"]


class TestBackwardCompatInactive:
    """When no selection is supplied the object must be a complete no-op."""

    def test_inactive_flags(self):
        sel = PortSelection()
        assert sel.is_active() is False
        assert sel.mode == "inactive"

    def test_inactive_never_selects_out(self):
        sel = PortSelection()
        assert sel.is_selected_out("swp1s0") is False

    def test_inactive_filter_returns_input_unchanged(self):
        sel = PortSelection()
        assert sel.filter_selected_out(LEFT) == LEFT

    def test_inactive_cascade_is_empty(self):
        sel = PortSelection()
        assert resolve_symmetric_cascade(LEFT, RIGHT, sel) == (set(), set())

    def test_empty_strings_are_inactive(self):
        sel = PortSelection(exclude="", include=None)
        assert sel.is_active() is False


class TestNormalizationAndMatching:
    def test_string_input_is_split(self):
        sel = PortSelection(exclude="swp1s0, swp2s0 swp2s1")
        assert sel.is_selected_out("swp1s0") is True
        assert sel.is_selected_out("swp2s1") is True
        assert sel.is_selected_out("swp1s1") is False

    def test_parent_matches_all_children(self):
        sel = PortSelection(exclude=["swp1"])
        assert sel.is_selected_out("swp1s0") is True
        assert sel.is_selected_out("swp1s1") is True
        assert sel.is_selected_out("swp2s0") is False

    def test_mutual_exclusion_raises(self):
        with pytest.raises(_TestIssue):
            PortSelection(include=["swp1s0"], exclude=["swp2s0"])

    def test_unsupported_style_raises(self):
        with pytest.raises(_TestIssue):
            PortSelection(exclude=["swp1"], port_style="bogus")


class TestExcludeCascade:
    def test_single_sided_left_mirrors_to_right_by_index(self):
        sel = PortSelection(exclude=["swp1s0"])
        excl_left, excl_right = resolve_symmetric_cascade(LEFT, RIGHT, sel)
        assert excl_left == {"swp1s0"}
        # index 0 on the sorted right side is swp33s0
        assert excl_right == {"swp33s0"}
        assert len(excl_left) == len(excl_right)

    def test_single_sided_right_mirrors_to_left_by_index(self):
        sel = PortSelection(exclude=["swp34s1"])
        excl_left, excl_right = resolve_symmetric_cascade(LEFT, RIGHT, sel)
        # swp34s1 is index 3 on the right; mirror to index 3 on the left = swp2s1
        assert excl_right == {"swp34s1"}
        assert excl_left == {"swp2s1"}

    def test_parent_exclusion_expands_and_mirrors(self):
        sel = PortSelection(exclude=["swp1"])
        excl_left, excl_right = resolve_symmetric_cascade(LEFT, RIGHT, sel)
        assert excl_left == {"swp1s0", "swp1s1"}
        # indices 0 and 1 on the right = swp33s0, swp33s1
        assert excl_right == {"swp33s0", "swp33s1"}

    def test_two_sided_balanced_ok(self):
        sel = PortSelection(exclude=["swp1s0", "swp33s1"])
        excl_left, excl_right = resolve_symmetric_cascade(LEFT, RIGHT, sel)
        assert excl_left == {"swp1s0"}
        assert excl_right == {"swp33s1"}

    def test_two_sided_unbalanced_fails_fast(self):
        sel = PortSelection(exclude=["swp1s0", "swp2s0", "swp33s0"])
        with pytest.raises(_TestIssue):
            resolve_symmetric_cascade(LEFT, RIGHT, sel)


class TestIncludeMode:
    def test_include_balanced_excludes_complement(self):
        sel = PortSelection(include=["swp1s0", "swp1s1", "swp33s0", "swp33s1"])
        excl_left, excl_right = resolve_symmetric_cascade(LEFT, RIGHT, sel)
        assert excl_left == {"swp2s0", "swp2s1"}
        assert excl_right == {"swp34s0", "swp34s1"}

    def test_include_unbalanced_fails_fast(self):
        # Keeps 1 left but 2 right -> imbalance.
        sel = PortSelection(include=["swp1s0", "swp33s0", "swp33s1"])
        with pytest.raises(_TestIssue):
            resolve_symmetric_cascade(LEFT, RIGHT, sel)

    def test_include_parent_keeps_children(self):
        sel = PortSelection(include=["swp1", "swp33"])
        excl_left, excl_right = resolve_symmetric_cascade(LEFT, RIGHT, sel)
        assert excl_left == {"swp2s0", "swp2s1"}
        assert excl_right == {"swp34s0", "swp34s1"}


class TestStructuralGuards:
    def test_unequal_base_counts_fail_fast(self):
        sel = PortSelection(exclude=["swp1s0"])
        with pytest.raises(_TestIssue):
            resolve_symmetric_cascade(LEFT, RIGHT[:-1], sel)


def _write_config(tmp_path, body):
    path = tmp_path / "port_selection_config.yaml"
    path.write_text(textwrap.dedent(body))
    return str(path)


class TestConfigFileBuilder:
    CONFIG = """
        nv_performance_slm-254:
          spcx_ra:
            exclude_ports: [swp26]
            include_ports: [swp1s0, swp1s1, swp33s0, swp33s1]
    """

    def test_no_flags_is_inactive_and_ignores_file(self):
        # Non-existent path must be fine when neither mode is enabled.
        sel = build_port_selection("any", "spcx_ra", exclude_enabled=False,
                                   include_enabled=False, config_path="/does/not/exist.yaml")
        assert sel.is_active() is False

    def test_both_flags_raise(self):
        with pytest.raises(_TestIssue):
            build_port_selection("nv_performance_slm-254", "spcx_ra", exclude_enabled=True,
                                 include_enabled=True)

    def test_exclude_mode_reads_exclude_list(self, tmp_path):
        cfg = _write_config(tmp_path, self.CONFIG)
        sel = build_port_selection("nv_performance_slm-254", "spcx_ra", exclude_enabled=True,
                                   include_enabled=False, config_path=cfg)
        assert sel.mode == "exclude"
        assert sel.is_selected_out("swp26s0") is True

    def test_include_mode_reads_include_list(self, tmp_path):
        cfg = _write_config(tmp_path, self.CONFIG)
        sel = build_port_selection("nv_performance_slm-254", "spcx_ra", exclude_enabled=False,
                                   include_enabled=True, config_path=cfg)
        assert sel.mode == "include"
        assert sel.is_selected_out("swp1s0") is False
        assert sel.is_selected_out("swp2s0") is True

    def test_missing_setup_fails_fast(self, tmp_path):
        cfg = _write_config(tmp_path, self.CONFIG)
        with pytest.raises(_TestIssue):
            build_port_selection("unknown_setup", "spcx_ra", exclude_enabled=True,
                                 include_enabled=False, config_path=cfg)

    def test_missing_scenario_fails_fast(self, tmp_path):
        cfg = _write_config(tmp_path, self.CONFIG)
        with pytest.raises(_TestIssue):
            build_port_selection("nv_performance_slm-254", "srv6", exclude_enabled=True,
                                 include_enabled=False, config_path=cfg)

    def test_enabled_but_empty_list_fails_fast(self, tmp_path):
        cfg = _write_config(tmp_path, """
            setupX:
              spcx_ra:
                exclude_ports: []
                include_ports: [swp1s0]
        """)
        with pytest.raises(_TestIssue):
            build_port_selection("setupX", "spcx_ra", exclude_enabled=True,
                                 include_enabled=False, config_path=cfg)

    def test_missing_file_when_enabled_fails_fast(self):
        with pytest.raises(_TestIssue):
            build_port_selection("setupX", "spcx_ra", exclude_enabled=True,
                                 include_enabled=False, config_path="/no/such/file.yaml")


class TestInactiveNoOpContract:
    """Lock in the backward-compatibility contract used by the per-NOS wrapper methods.

    The NVUE/DVS/SONiC methods (get_right_left_ports_dict, get_down_swp_ports,
    wait_for_nexthop_resolution) all delegate to these helpers behind an
    ``if not port_selection.is_active()`` guard. These tests assert the helpers themselves are
    no-ops for an inactive selection, so an inactive run behaves exactly as before.
    """

    def test_cascade_no_op(self):
        sel = PortSelection()
        assert resolve_symmetric_cascade(LEFT, RIGHT, sel) == (set(), set())

    def test_filter_selected_out_no_op_same_list(self):
        sel = PortSelection()
        assert sel.filter_selected_out(LEFT) == LEFT

    def test_build_inactive_does_not_read_config(self):
        # No flags -> inactive, and the (nonexistent) config path must not be touched.
        sel = build_port_selection("any-setup", "spcx_ra", exclude_enabled=False,
                                   include_enabled=False, config_path="/nonexistent/path.yaml")
        assert sel.is_active() is False

    def test_inactive_build_does_not_mark_activation(self):
        # Building an inactive selection must not flip the session activation flag.
        before = port_selection_was_activated()
        build_port_selection("any-setup", "spcx_ra", exclude_enabled=False,
                             include_enabled=False)
        assert port_selection_was_activated() == before


class TestResolvedDutPortsStore:
    """File-backed DUT-excluded-names store round-trips and clears (DUT -> TGs)."""

    def teardown_method(self):
        clear_resolved_excluded_ports()

    def test_roundtrip(self):
        clear_resolved_excluded_ports()
        assert get_resolved_excluded_dut_ports() == set()
        set_resolved_excluded_dut_ports({"swp26", "swp58"})
        assert get_resolved_excluded_dut_ports() == {"swp26", "swp58"}

    def test_clear(self):
        set_resolved_excluded_dut_ports({"swp26"})
        assert get_resolved_excluded_dut_ports() == {"swp26"}
        clear_resolved_excluded_ports()
        assert get_resolved_excluded_dut_ports() == set()

    def test_empty_publish_is_noop(self):
        clear_resolved_excluded_ports()
        set_resolved_excluded_dut_ports(set())
        assert get_resolved_excluded_dut_ports() == set()


class TestOtherPortStyles:
    def test_ethernet_style(self):
        left = ["Ethernet0", "Ethernet8"]
        right = ["Ethernet128", "Ethernet136"]
        sel = PortSelection(exclude=["Ethernet0"], port_style="ethernet")
        excl_left, excl_right = resolve_symmetric_cascade(left, right, sel)
        assert excl_left == {"Ethernet0"}
        assert excl_right == {"Ethernet128"}

    def test_sdk_hex_style_sorts_numerically(self):
        left = ["0x10001", "0x10003"]
        right = ["0x10041", "0x10043"]
        sel = PortSelection(exclude=["0x10003"], port_style="sdk_hex")
        excl_left, excl_right = resolve_symmetric_cascade(left, right, sel)
        assert excl_left == {"0x10003"}
        # index 1 on the right = 0x10043
        assert excl_right == {"0x10043"}
