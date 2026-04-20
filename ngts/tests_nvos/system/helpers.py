import copy
import json
import logging
import random
from dataclasses import dataclass
from typing import Any, Optional, Union

from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from ngts.nvos_constants.constants_nvos import AclConsts, ConfState, SystemConsts
from ngts.nvos_tools.acl.acl import Acl
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.system.System import System

logger = logging.getLogger(__name__)


def _apply_staged_nvue_config(dut_engine) -> None:
    """Run ``nv -y config apply`` with execution verification (same spirit as ``clear_conf``)."""
    output = NvueGeneralCli.apply_config(engine=dut_engine, option="-y", verify_execution=True)
    assert ConfState.APPLIED in output, "Failed to apply config"


@dataclass
class AclPersistenceMangleState:
    """Tracks ACL / control-plane tweaks done for persistence tests (reverted by ``clear_acl_configs``)."""

    whitelist_acl_id: str
    new_rule_id: str
    removed_control_plane_acl_id: Optional[str]


def add_acl_new_configs_for_persistence_checks(*, dut_engine) -> AclPersistenceMangleState:
    """
    Stage NVUE changes for ACL persistence coverage (caller then runs ``extract_acl_rules`` and
    ``extract_control_plane_acl_bindings``):

    1. ``nv set acl acl-default-whitelist-ipv6 rule <id> action permit`` and ``type ipv6``, then apply
       (handles default-ACL change warning).
    2. If ``nv show system control-plane acl`` has bindings: ``nv unset system control-plane acl <random acl>``
       (prefers an ACL other than the modified whitelist), apply.

    Returns state for :func:`clear_acl_configs`.
    """
    whitelist_acl_id = SystemConsts.ACL_DEFAULT_WHITELIST_IPV6
    new_rule_id = "400"
    acl = Acl()
    whitelist_ipv6_acl = acl.acl_id[whitelist_acl_id]
    whitelist_ipv6_acl.rule.rule_id[new_rule_id].action.set(
        AclConsts.PERMIT, apply=False, dut_engine=dut_engine
    ).verify_result()
    whitelist_ipv6_acl.set(AclConsts.TYPE, "ipv6", apply=False, dut_engine=dut_engine).verify_result()
    _apply_staged_nvue_config(dut_engine)

    system = System()
    unbound_control_plane_acl_name: Optional[str] = None
    try:
        control_plane_acl_show = system.control_plane.acl.parse_show(dut_engine=dut_engine) or {}
    except Exception as exc:
        logger.warning("Could not parse system control-plane acl before unset: %s", exc)
        control_plane_acl_show = {}
    if isinstance(control_plane_acl_show, dict) and control_plane_acl_show:
        bound_control_plane_acl_names = list(control_plane_acl_show.keys())
        candidate_acl_names = [
            bound_acl_name
            for bound_acl_name in bound_control_plane_acl_names
            if bound_acl_name != whitelist_acl_id
        ]
        acl_names_eligible_for_unbind = candidate_acl_names if candidate_acl_names else bound_control_plane_acl_names
        unbound_control_plane_acl_name = random.choice(acl_names_eligible_for_unbind)
        system.control_plane.acl.acl_id[unbound_control_plane_acl_name].unset(
            apply=False, dut_engine=dut_engine
        ).verify_result()
        _apply_staged_nvue_config(dut_engine)
        logger.info(
            "add_acl_new_configs_for_persistence_checks: removed control-plane binding for %r",
            unbound_control_plane_acl_name,
        )
    else:
        logger.warning(
            "add_acl_new_configs_for_persistence_checks: no system control-plane ACL keys; "
            "skipping unset (baseline CP bindings will be empty)"
        )

    return AclPersistenceMangleState(
        whitelist_acl_id=whitelist_acl_id,
        new_rule_id=new_rule_id,
        removed_control_plane_acl_id=unbound_control_plane_acl_name,
    )


# Requested spelling alias (same implementation).
add_acl_new_configs_for_persistance_checks = add_acl_new_configs_for_persistence_checks


def clear_acl_configs(state: Optional[AclPersistenceMangleState], *, dut_engine) -> None:
    """
    Undo :func:`add_acl_new_configs_for_persistence_checks`:

    1. ``nv unset acl acl-default-whitelist-ipv6 rule <id>`` (staged).
    2. If a control-plane ACL was removed, ``nv set system control-plane acl <id> inbound`` (empty attach).
    3. Single ``nv -y config apply`` with verify-on-execution and ``applied`` assertion.
    """
    if state is None:
        return
    acl = Acl()
    modified_whitelist_acl = acl.acl_id[state.whitelist_acl_id]
    modified_whitelist_acl.rule.rule_id[state.new_rule_id].unset(
        apply=False, dut_engine=dut_engine
    ).verify_result()
    if state.removed_control_plane_acl_id:
        System().control_plane.acl.acl_id[state.removed_control_plane_acl_id].inbound.set(
            apply=False, dut_engine=dut_engine
        ).verify_result()
    _apply_staged_nvue_config(dut_engine)
    logger.info("clear_acl_configs: restored ACL persistence mangle state")


# Default loopback ACLs are interface-bound on ``lo``, not only under system control-plane.
LOOPBACK_ACL_INTERFACE = "lo"


def _default_acl_ids_with_loopback_in_name() -> tuple[str, ...]:
    """``NEW_DEFAULT_ACLS`` entries whose id contains ``loopback`` (case-insensitive)."""
    loopback_default_acl_names: list[str] = []
    for default_acl_name in AclConsts.NEW_DEFAULT_ACLS:
        if "loopback" not in default_acl_name.lower():
            continue
        if default_acl_name not in loopback_default_acl_names:
            loopback_default_acl_names.append(default_acl_name)
    return tuple(loopback_default_acl_names)


def _unwrap_acls_if_needed(acls):
    """If nv show acl returned {"acl": {"acl-name": {...}, ...}}, return inner dict."""
    if not acls or not isinstance(acls, dict) or len(acls) != 1:
        return acls
    wrapper_key = next(iter(acls.keys()))
    inner = acls[wrapper_key]
    acl_objects_are_dicts = all(
        isinstance(acl_object_json, dict) for acl_object_json in inner.values()
    )
    if isinstance(inner, dict) and inner and acl_objects_are_dicts:
        return inner
    return acls


def extract_acl_rules(*, dut_engine=None) -> dict[str, Any]:
    """
    Capture baseline from ``nv show acl`` for **``AclConsts.NEW_DEFAULT_ACLS``** only: each name that
    exists on the DUT gets a **deep copy** of that ACL's full JSON object.

    Raises ``AssertionError`` if ``nv show acl`` is empty/unusable or no new-default ACL objects match.
    """
    acl = Acl()
    acls = OutputParsingTool.parse_show_output_to_dict(acl.show(dut_engine=dut_engine)).get_returned_value()
    if not acls:
        raise AssertionError("nv show acl: empty or None — cannot build baseline")
    acls = _unwrap_acls_if_needed(acls)
    if not isinstance(acls, dict) or not acls:
        raise AssertionError("nv show acl: no ACL objects after unwrap (empty or wrong type)")
    logger.info("extract_acl_rules: acls keys from nv show acl: %s", list(acls.keys()))
    baseline: dict[str, Any] = {}
    for name in AclConsts.NEW_DEFAULT_ACLS:
        if name in acls:
            baseline[name] = copy.deepcopy(acls[name])
            n_rules = len(acls[name].get(AclConsts.RULE, {})) if isinstance(acls[name], dict) else 0
            logger.info("NEW default ACL baseline: %s -> full JSON snapshot (%s rules)", name, n_rules)
    if not baseline:
        raise AssertionError(
            "nv show acl: no NEW_DEFAULT_ACLS names matched; keys on DUT: %s" % (sorted(acls.keys()),)
        )
    return baseline


def verify_acl_rules_preserved(baseline, *, dut_engine=None) -> None:
    """After upgrade, verify each ACL in *baseline* still exists under ``nv show acl`` with identical JSON body."""
    if not baseline:
        raise AssertionError("nv show acl comparison: baseline is empty (expected captured default ACL JSON)")
    acl = Acl()
    acls = OutputParsingTool.parse_show_output_to_dict(acl.show(dut_engine=dut_engine)).get_returned_value()
    if not acls:
        raise AssertionError("nv show acl comparison: empty or None after upgrade")
    acls = _unwrap_acls_if_needed(acls)
    if not isinstance(acls, dict) or not acls:
        raise AssertionError("nv show acl comparison: no ACL objects after unwrap (empty or wrong type)")
    for acl_name, body_pre in baseline.items():
        if acl_name not in acls:
            raise AssertionError(
                "ACL %r from baseline missing after upgrade. Present: %s" % (acl_name, list(acls.keys()))
            )
        body_post = acls[acl_name]
        if body_pre != body_post:
            pre_json = json.dumps(body_pre, indent=2, sort_keys=True) if isinstance(body_pre, dict) else repr(body_pre)
            post_json = json.dumps(body_post, indent=2, sort_keys=True) if isinstance(body_post, dict) else repr(body_post)
            raise AssertionError(
                "Default ACL %r JSON changed across upgrade.\n--- pre ---\n%s\n--- post ---\n%s"
                % (acl_name, pre_json, post_json)
            )
        logger.info("Default ACL %s preserved (full JSON match)", acl_name)


def extract_control_plane_acl_bindings(*, dut_engine=None):
    """
    Capture ACL names bound to system control-plane (``nv show system control-plane acl``). Returns set.

    Some platforms / images return ``{}`` even when default ACLs exist under ``nv show acl`` (bindings only
    on ``interface lo acl``). In that case this returns an **empty set** (no AssertionError).

    Raises ``AssertionError`` only if parse_show fails or the payload is not a dict.
    """
    system = System()
    try:
        control_plane_acl_show = system.control_plane.acl.parse_show(dut_engine=dut_engine)
    except Exception as exc:
        raise AssertionError("nv show system control-plane acl: parse_show failed: %s" % exc) from exc
    if control_plane_acl_show is None or not isinstance(control_plane_acl_show, dict):
        raise AssertionError(
            "nv show system control-plane acl: expected dict from parse_show, got %r"
            % type(control_plane_acl_show).__name__
        )
    bound_acl_names = set(control_plane_acl_show.keys())
    if not bound_acl_names:
        logger.warning(
            "nv show system control-plane acl returned no top-level ACL keys (empty object); "
            "skipping control-plane binding subset checks; loopback-on-lo checks still apply"
        )
    else:
        logger.info("Control-plane ACL bindings: %s", bound_acl_names)
    return bound_acl_names


def verify_loopback_named_default_acls_on_lo(*, dut_engine=None) -> None:
    """
    NEW default ACLs whose names contain ``loopback`` must be bound on ``lo`` (``nv show interface lo acl``).
    """
    default_loopback_acl_names = _default_acl_ids_with_loopback_in_name()
    if not default_loopback_acl_names:
        raise AssertionError(
            "No NEW_DEFAULT_ACLS ids with 'loopback' in name; cannot verify interface lo acl"
        )
    try:
        loopback_acl_bindings_show = Port(LOOPBACK_ACL_INTERFACE).interface.acl.parse_show(
            dut_engine=dut_engine
        ) or {}
    except Exception as exc:
        raise AssertionError(
            "nv show interface %r acl: parse_show failed: %s" % (LOOPBACK_ACL_INTERFACE, exc)
        ) from exc
    if not isinstance(loopback_acl_bindings_show, dict):
        raise AssertionError(
            "nv show interface %r acl: expected dict from parse_show, got %r"
            % (LOOPBACK_ACL_INTERFACE, type(loopback_acl_bindings_show).__name__)
        )
    bound_acl_names_on_loopback = set(loopback_acl_bindings_show.keys())
    if not bound_acl_names_on_loopback:
        raise AssertionError(
            "nv show interface %r acl: no bindings (empty) — expected loopback default ACLs here"
            % (LOOPBACK_ACL_INTERFACE,)
        )
    for loopback_default_acl_name in default_loopback_acl_names:
        assert loopback_default_acl_name in bound_acl_names_on_loopback, (
            "Default ACL %r (loopback family) must be bound on interface %r after upgrade. "
            "Bound on %r: %s"
            % (
                loopback_default_acl_name,
                LOOPBACK_ACL_INTERFACE,
                LOOPBACK_ACL_INTERFACE,
                sorted(bound_acl_names_on_loopback),
            )
        )
        logger.info("Default ACL %s bound on %s", loopback_default_acl_name, LOOPBACK_ACL_INTERFACE)


def verify_control_plane_acl_bindings(baseline_bindings, *, dut_engine=None) -> None:
    """
    After upgrade:

    1. If *baseline_bindings* is non-empty: every name is still a top-level key on system control-plane acl,
       and the current show is non-empty (subset check).
    2. Always: every NEW default ACL id containing ``loopback`` is bound on ``interface lo acl``.

    If baseline was empty (NVUE returned ``{}`` before upgrade), step (1) is skipped; step (2) still runs.
    """
    if baseline_bindings is None:
        raise AssertionError(
            "nv show system control-plane acl comparison: baseline is None (expected captured binding set)"
        )
    system = System()
    try:
        control_plane_acl_show_post_upgrade = system.control_plane.acl.parse_show(dut_engine=dut_engine)
    except Exception as exc:
        raise AssertionError(
            "nv show system control-plane acl after upgrade: parse_show failed: %s" % exc
        ) from exc
    if control_plane_acl_show_post_upgrade is None or not isinstance(control_plane_acl_show_post_upgrade, dict):
        raise AssertionError(
            "nv show system control-plane acl after upgrade: expected dict, got %r"
            % type(control_plane_acl_show_post_upgrade).__name__
        )
    current_bound_acl_names = set(control_plane_acl_show_post_upgrade.keys())

    if baseline_bindings:
        if not current_bound_acl_names:
            raise AssertionError(
                "nv show system control-plane acl after upgrade: no keys but baseline had %s"
                % (sorted(baseline_bindings),)
            )
        for baseline_bound_acl_name in baseline_bindings:
            assert baseline_bound_acl_name in current_bound_acl_names, (
                "Default ACL {} not bound to system control-plane after upgrade. "
                "Bound ACLs: {}".format(baseline_bound_acl_name, sorted(current_bound_acl_names))
            )
            logger.info("Default ACL %s bound to control-plane", baseline_bound_acl_name)
        logger.info("All baseline ACL names still bound to system control-plane after upgrade")
    else:
        logger.info(
            "Skipping control-plane ACL binding subset check (baseline empty — NVUE often returns {} here)"
        )

    verify_loopback_named_default_acls_on_lo(dut_engine=dut_engine)


def verify_api_compression_state(system: System, expected_compression: Union[str, None]) -> None:
    applied_compression: Union[str, None] = OutputParsingTool.parse_json_str_to_dictionary(system.api.show()).get_returned_value()[SystemConsts.ApiConsts.COMPRESSION]
    assert applied_compression == expected_compression, \
        f"Compression {'shown' if applied_compression else 'not shown'}, but show is {'expected' if expected_compression else 'not expected'}"
