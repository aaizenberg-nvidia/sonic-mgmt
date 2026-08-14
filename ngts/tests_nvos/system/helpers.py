import copy
import difflib
import json
import logging
import random
from dataclasses import dataclass
from typing import Any, Optional, Union

from ngts.cli_wrappers.nvue.nvue_general_clis import NvueGeneralCli
from ngts.nvos_constants.constants_nvos import AclConsts, ApiType, ConfState, SystemConsts
from ngts.nvos_tools.acl.acl import Acl
from ngts.nvos_tools.ib.InterfaceConfiguration.Port import Port
from ngts.nvos_tools.infra.NvosTestToolkit import TestToolkit
from ngts.nvos_tools.infra.OutputParsingTool import OutputParsingTool
from ngts.nvos_tools.system.System import System
from ngts.tests_nvos.helpers.redmine_helpers import is_bug_active

logger = logging.getLogger(__name__)


def _apply_staged_nvue_config(dut_engine) -> None:
    """
    Run ``nv -y config apply`` + ``nv config save`` so persistence-test edits survive ISSU/reboot.

    Always uses NVUE CLI. OpenAPI pending revisions are invisible to ``nv config apply``, which
    then no-ops with "Revision 'applied' has already been applied" while still looking like
    success (the word ``applied`` is in the message). Callers must stage under NVUE as well
    (see :func:`add_acl_new_configs_for_persistence_checks`).
    """
    with TestToolkit.force_api(ApiType.NVUE):
        output = NvueGeneralCli.apply_config(engine=dut_engine, option="-y", verify_execution=True)
        output_text = str(output)
        if "already been applied" in output_text.lower():
            raise AssertionError(
                "nv config apply was a no-op (no pending CLI diff). Staged ACL change was not "
                "committed — check that sets were done under NVUE, not OpenAPI. output=%r"
                % (output_text,)
            )
        assert ConfState.APPLIED in output_text, "Failed to apply config: %r" % (output_text,)
        NvueGeneralCli.save_config(dut_engine)


@dataclass
class AclPersistenceMangleState:
    """Tracks the single user-change marker + control-plane tweak (reverted by ``clear_acl_configs``)."""

    mode: str                                       # "old" or "new" — naming present pre-ISSU
    marker_acl_id: str                              # ACL touched pre-ISSU (OLD-format in old, lowercase in new)
    marker_rule_id: str                             # "999" in old mode, "400" in new mode
    marker_rule_remark: Optional[str]               # set in old mode (verified preserved post-ISSU)
    # ISSU is additive: both ACL families coexist on the new image, so the marker stays
    # under marker_acl_id post-ISSU rather than being moved to a renamed counterpart.
    expected_post_issu_acl_id: str
    removed_control_plane_acl_id: Optional[str]     # only set in NEW mode (CP CLI exists)


# Single user-change marker per mode. Only one rule is added — never both.
#   OLD mode: rule 999 on ACL_MGMT_INBOUND_CP_DEFAULT_IPV6 with remark "Whitelist-test-marker".
#             ISSU does not migrate user mutations into the new acl-default-* ACLs; instead the OLD
#             ACL_* family is preserved verbatim post-ISSU alongside the new acl-default-* family.
#             So the marker stays exactly where it was added.
#   NEW mode: rule 400 on acl-default-whitelist-ipv6. Survives via nv config save/restore.
_OLD_MODE_MARKER_ACL_ID = "ACL_MGMT_INBOUND_CP_DEFAULT_IPV6"
_OLD_MODE_MARKER_RULE_ID = "999"
_OLD_MODE_MARKER_REMARK = "Whitelist-test-marker"
_NEW_MODE_MARKER_ACL_ID = "acl-default-whitelist-ipv6"   # = SystemConsts.ACL_DEFAULT_WHITELIST_IPV6
_NEW_MODE_MARKER_RULE_ID = "400"


def _detect_acl_mode(acls_dict) -> str:
    """Return ``"new"`` if ``nv show acl`` exposes any new-default ACL name, else ``"old"``."""
    if not isinstance(acls_dict, dict):
        return "new"
    keys = set(acls_dict.keys())
    if keys & set(AclConsts.NEW_DEFAULT_ACLS):
        return "new"
    if keys & set(AclConsts.DEFAULT_ACLS):
        return "old"
    return "new"


def _show_acl_keys(dut_engine) -> dict:
    """``nv show acl`` → unwrapped dict (possibly empty). Raises only on hard failure."""
    acl = Acl()
    acls = OutputParsingTool.parse_show_output_to_dict(acl.show(dut_engine=dut_engine)).get_returned_value()
    if not acls:
        raise AssertionError("nv show acl: empty or None")
    acls = _unwrap_acls_if_needed(acls)
    if not isinstance(acls, dict) or not acls:
        raise AssertionError("nv show acl: no ACL objects after unwrap")
    return acls


def add_acl_new_configs_for_persistence_checks(*, dut_engine) -> AclPersistenceMangleState:
    """
    Stage a single user-change marker for ACL persistence coverage, plus the NEW-mode control-plane
    unbind. Mode is detected from pre-ISSU ``nv show acl`` keys:

    OLD mode (only ``ACL_*`` CAPS keys present):
        ``nv set acl ACL_MGMT_INBOUND_CP_DEFAULT_IPV6 rule 999 action permit`` + remark
        ``Whitelist-test-marker``, apply, save. Post-ISSU verification expects rule 999 to migrate
        into ``acl-default-whitelist-ipv6`` (split routes it there because of the ``Whitelist-`` remark).

    NEW mode (any ``acl-default-*`` key already present):
        ``nv set acl acl-default-whitelist-ipv6 rule 400 action permit`` + ``type ipv6``, apply, save.
        If ``nv show system control-plane acl`` has bindings, also ``nv unset system control-plane acl
        <random acl>`` (preferring an ACL other than the modified whitelist), apply, save.

    Always stages/applies/saves under NVUE so OpenAPI pending revisions cannot leave the marker
    unapplied. After apply+save, asserts the marker is visible in ``nv show acl`` before returning.

    Returns state for :func:`clear_acl_configs` and :func:`verify_acl_rules_preserved`.
    """
    # OpenAPI PATCH stages into a pending revision that ``nv config apply`` does not see; force
    # NVUE for the whole mangle so set/unset + apply + save share one pending changeset.
    with TestToolkit.force_api(ApiType.NVUE):
        return _add_acl_new_configs_for_persistence_checks_impl(dut_engine=dut_engine)


def _add_acl_new_configs_for_persistence_checks_impl(*, dut_engine) -> AclPersistenceMangleState:
    pre_issu_acls = _show_acl_keys(dut_engine)
    mode = _detect_acl_mode(pre_issu_acls)
    logger.info("add_acl_new_configs_for_persistence_checks: detected ACL mode=%s", mode)

    acl = Acl()
    unbound_control_plane_acl_name: Optional[str] = None

    if mode == "old":
        if _OLD_MODE_MARKER_ACL_ID not in pre_issu_acls:
            raise AssertionError(
                "OLD-mode but %s not present in pre-ISSU show acl — cannot add user-change marker"
                % (_OLD_MODE_MARKER_ACL_ID,)
            )
        old_acl = acl.acl_id[_OLD_MODE_MARKER_ACL_ID]
        old_acl.rule.rule_id[_OLD_MODE_MARKER_RULE_ID].action.set(
            AclConsts.PERMIT, apply=False, dut_engine=dut_engine
        ).verify_result()
        old_acl.rule.rule_id[_OLD_MODE_MARKER_RULE_ID].set(
            AclConsts.REMARK, _OLD_MODE_MARKER_REMARK, apply=False, dut_engine=dut_engine
        ).verify_result()
        _apply_staged_nvue_config(dut_engine)
        marker_acl_id = _OLD_MODE_MARKER_ACL_ID
        marker_rule_id = _OLD_MODE_MARKER_RULE_ID
        marker_rule_remark = _OLD_MODE_MARKER_REMARK
        logger.info(
            "OLD-mode marker added: %s rule %s (remark %r) — expected to remain on the same ACL post-ISSU",
            marker_acl_id, marker_rule_id, marker_rule_remark,
        )
    else:
        whitelist_ipv6_acl = acl.acl_id[_NEW_MODE_MARKER_ACL_ID]
        whitelist_ipv6_acl.rule.rule_id[_NEW_MODE_MARKER_RULE_ID].action.set(
            AclConsts.PERMIT, apply=False, dut_engine=dut_engine
        ).verify_result()
        whitelist_ipv6_acl.set(AclConsts.TYPE, "ipv6", apply=False, dut_engine=dut_engine).verify_result()
        _apply_staged_nvue_config(dut_engine)
        marker_acl_id = _NEW_MODE_MARKER_ACL_ID
        marker_rule_id = _NEW_MODE_MARKER_RULE_ID
        marker_rule_remark = None
        logger.info(
            "NEW-mode marker added: %s rule %s — expected to remain on the same ACL post-ISSU",
            marker_acl_id, marker_rule_id,
        )

        system = System()
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
                if bound_acl_name != marker_acl_id
            ]
            acl_names_eligible_for_unbind = candidate_acl_names if candidate_acl_names else bound_control_plane_acl_names
            unbound_control_plane_acl_name = random.choice(acl_names_eligible_for_unbind)
            system.control_plane.acl.acl_id[unbound_control_plane_acl_name].unset(
                apply=False, dut_engine=dut_engine
            ).verify_result()
            _apply_staged_nvue_config(dut_engine)
            logger.info("Removed control-plane binding for %r", unbound_control_plane_acl_name)
        else:
            logger.warning("No system control-plane ACL keys; skipping unset")

    state = AclPersistenceMangleState(
        mode=mode,
        marker_acl_id=marker_acl_id,
        marker_rule_id=marker_rule_id,
        marker_rule_remark=marker_rule_remark,
        expected_post_issu_acl_id=marker_acl_id,
        removed_control_plane_acl_id=unbound_control_plane_acl_name,
    )
    # Fail fast before ISSU/upgrade if apply+save did not actually land the marker.
    _verify_user_change_marker_preserved(_show_acl_keys(dut_engine), state)
    return state


# Requested spelling alias (same implementation).
add_acl_new_configs_for_persistance_checks = add_acl_new_configs_for_persistence_checks


def clear_acl_configs(state: Optional[AclPersistenceMangleState], *, dut_engine) -> None:
    """
    Undo :func:`add_acl_new_configs_for_persistence_checks`. Called post-ISSU, when both the
    OLD-mode and NEW-mode marker rules end up under ``acl-default-whitelist-ipv6``:

    1. ``nv unset acl <expected_post_issu_acl_id> rule <marker_rule_id>``.
    2. NEW mode: if a control-plane ACL was removed, re-attach with empty ``inbound``.
    3. Single ``nv -y config apply`` (+ save).
    """
    if state is None:
        return
    with TestToolkit.force_api(ApiType.NVUE):
        acl = Acl()
        acl.acl_id[state.expected_post_issu_acl_id].rule.rule_id[state.marker_rule_id].unset(
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
    Capture baseline from ``nv show acl``. Detects OLD vs NEW naming on the DUT and returns full JSON
    bodies for each default ACL of the matching set:

    * NEW pre-ISSU (``acl-default-*`` keys present): captures every entry in ``NEW_DEFAULT_ACLS``
      that exists. Used by :func:`verify_acl_rules_preserved` for byte-identical comparison.
    * OLD pre-ISSU (``ACL_*`` CAPS only): captures every entry in ``DEFAULT_ACLS`` that exists.
      Used by :func:`verify_acl_rules_preserved` to walk ``OLD_TO_NEW_ACL_RENAME_MAP`` post-ISSU.

    Raises ``AssertionError`` if ``nv show acl`` is empty/unusable or no default ACL objects match.
    """
    acls = _show_acl_keys(dut_engine)
    logger.info("extract_acl_rules: acls keys from nv show acl: %s", list(acls.keys()))
    mode = _detect_acl_mode(acls)
    default_names = AclConsts.NEW_DEFAULT_ACLS if mode == "new" else AclConsts.DEFAULT_ACLS
    baseline: dict[str, Any] = {}
    for name in default_names:
        if name in acls:
            baseline[name] = copy.deepcopy(acls[name])
            n_rules = len(acls[name].get(AclConsts.RULE, {})) if isinstance(acls[name], dict) else 0
            logger.info("[%s mode] default ACL baseline: %s -> full JSON snapshot (%s rules)",
                        mode, name, n_rules)
    if not baseline:
        raise AssertionError(
            "nv show acl: no %s names matched; keys on DUT: %s"
            % ("NEW_DEFAULT_ACLS" if mode == "new" else "DEFAULT_ACLS", sorted(acls.keys()))
        )
    return baseline


def verify_acl_rules_preserved(baseline, *, mangle_state: Optional[AclPersistenceMangleState] = None,
                               dut_engine=None) -> None:
    """
    Post-ISSU verification. Mode is inferred from *baseline* keys:

    * NEW (any ``acl-default-*`` key in baseline): each baseline body must equal the post-ISSU body
      (byte-identical JSON — works for new→new ISSU where no migration occurs).
    * OLD (only ``ACL_*`` CAPS keys in baseline): apply ``AclConsts.OLD_TO_NEW_ACL_RENAME_MAP``:
        - Every renamed target must exist post-ISSU with at least
          ``AclConsts.NEW_ACL_EXPECTED_RULE_COUNTS`` rules.
        - If *mangle_state* records an OLD-mode marker rule, that rule id must be present on
          ``mangle_state.expected_post_issu_acl_id`` post-ISSU with ``action: permit``.
    """
    if not baseline:
        raise AssertionError("nv show acl comparison: baseline is empty (expected captured default ACL JSON)")
    post_acls = _show_acl_keys(dut_engine)
    baseline_mode = _detect_acl_mode(baseline)

    if baseline_mode == "new":
        # Redmine #5006229: 8000-train ships TCP 9379 in acl-default-whitelist rule 240
        # (NMX unsupported on this train). Ignore that single additive port while bug is open.
        skip_9379 = is_bug_active(5006229)
        for acl_name, body_pre in baseline.items():
            if acl_name not in post_acls:
                raise AssertionError(
                    "ACL %r from baseline missing after upgrade. Present: %s"
                    % (acl_name, list(post_acls.keys()))
                )
            body_post = post_acls[acl_name]
            if skip_9379 and acl_name == "acl-default-whitelist":
                for body in (body_pre, body_post):
                    body.get("rule", {}).get("240", {}).get(
                        "match", {}).get("ip", {}).get("tcp", {}).get("dest-port", {}).pop("9379", None)
            if body_pre != body_post:
                pre_lines = json.dumps(body_pre, indent=2, sort_keys=True).splitlines(keepends=True)
                post_lines = json.dumps(body_post, indent=2, sort_keys=True).splitlines(keepends=True)
                diff = "".join(difflib.unified_diff(pre_lines, post_lines, fromfile="pre", tofile="post", n=2))
                raise AssertionError(
                    "Default ACL %r JSON changed across upgrade:\n%s" % (acl_name, diff)
                )
            logger.info("Default ACL %s preserved (full JSON match)", acl_name)
        _verify_user_change_marker_preserved(post_acls, mangle_state)
        return

    rename_map = AclConsts.OLD_TO_NEW_ACL_RENAME_MAP
    expected_rule_counts = AclConsts.NEW_ACL_EXPECTED_RULE_COUNTS
    expected_new_targets: set = set()
    for old_name in baseline:
        new_targets = rename_map.get(old_name)
        if not new_targets:
            raise AssertionError(
                "OLD-mode baseline contains %r which has no entry in OLD_TO_NEW_ACL_RENAME_MAP"
                % (old_name,)
            )
        expected_new_targets.update(new_targets)

    for new_target in sorted(expected_new_targets):
        if new_target not in post_acls:
            raise AssertionError(
                "Renamed ACL %r missing after ISSU. Present: %s"
                % (new_target, sorted(post_acls.keys()))
            )
        rules_post = post_acls[new_target].get(AclConsts.RULE, {}) if isinstance(post_acls[new_target], dict) else {}
        actual_count = len(rules_post)
        expected_count = expected_rule_counts.get(new_target, 0)
        if actual_count < expected_count:
            raise AssertionError(
                "Renamed ACL %r has %d rules post-ISSU; expected at least %d (migration may have lost rules)"
                % (new_target, actual_count, expected_count)
            )
        logger.info("Renamed ACL %s present with %d rules (expected ≥ %d)",
                    new_target, actual_count, expected_count)

    _verify_user_change_marker_preserved(post_acls, mangle_state)


def _verify_user_change_marker_preserved(post_acls, mangle_state: Optional[AclPersistenceMangleState]) -> None:
    """Assert the single marker rule (rule 400 in NEW mode, rule 999 in OLD mode) survived ISSU.

    The marker is expected on ``mangle_state.expected_post_issu_acl_id`` — same ACL it was added to,
    since the product preserves OLD ACL_* ACLs verbatim alongside the new acl-default-* family.
    """
    if not mangle_state:
        return
    target = mangle_state.expected_post_issu_acl_id
    if target not in post_acls:
        raise AssertionError(
            "User-change marker target ACL %r missing from post-ISSU show acl. Present: %s"
            % (target, sorted(post_acls.keys()))
        )
    target_body = post_acls[target]
    target_rules = target_body.get(AclConsts.RULE, {}) if isinstance(target_body, dict) else {}
    if mangle_state.marker_rule_id not in target_rules:
        raise AssertionError(
            "User-change marker missing: rule %s should be on %s post-ISSU. Present rules on %s: %s"
            % (mangle_state.marker_rule_id, target, target, sorted(target_rules.keys()))
        )
    rule_body = target_rules[mangle_state.marker_rule_id]
    action = rule_body.get("action") if isinstance(rule_body, dict) else None
    if action != {"permit": {}}:
        raise AssertionError(
            "User-change marker rule %s on %s has action=%r, expected {'permit': {}}"
            % (mangle_state.marker_rule_id, target, action)
        )
    if mangle_state.marker_rule_remark is not None:
        post_remark = rule_body.get("remark") if isinstance(rule_body, dict) else None
        if post_remark != mangle_state.marker_rule_remark:
            raise AssertionError(
                "User-change marker rule %s on %s has remark=%r, expected %r"
                % (mangle_state.marker_rule_id, target, post_remark, mangle_state.marker_rule_remark)
            )
    logger.info("User-change marker preserved: rule %s on %s permit (mode=%s, remark=%r)",
                mangle_state.marker_rule_id, target, mangle_state.mode,
                mangle_state.marker_rule_remark)


def extract_control_plane_acl_bindings(*, dut_engine=None, mangle_state=None):
    """
    Capture ACL names bound to system control-plane (``nv show system control-plane acl``). Returns set.

    Some platforms / images return ``{}`` even when default ACLs exist under ``nv show acl`` (bindings only
    on ``interface lo acl``). In that case this returns an **empty set** (no AssertionError).

    On older ISSU base images (e.g. 25.02.5035 / 25.02.6077) the ``system control-plane`` subtree does not
    exist — NVUE responds with ``'control-plane' is not one of [...]``. This is treated as "no baseline
    available" and an **empty set** is returned, so post-ISSU verification skips the subset check
    while still running the loopback-on-``lo`` check against the new image.

    If *mangle_state* is provided and its ``removed_control_plane_acl_id`` is set, that ACL is excluded
    from the returned baseline so post-upgrade verification does not expect an ACL that was deliberately
    unset before the upgrade.
    """
    system = System()
    try:
        control_plane_acl_show = system.control_plane.acl.parse_show(dut_engine=dut_engine)
    except Exception as exc:
        if "'control-plane' is not one of" in str(exc):
            logger.info(
                "nv show system control-plane acl: subtree absent (OLD base image) — "
                "returning empty baseline; subset check skipped post-ISSU"
            )
        else:
            logger.warning(
                "nv show system control-plane acl unexpected failure: %s — "
                "returning empty baseline; subset check skipped post-ISSU",
                exc,
            )
        return set()
    if control_plane_acl_show is None or not isinstance(control_plane_acl_show, dict):
        raise AssertionError(
            "nv show system control-plane acl: expected dict from parse_show, got %r"
            % type(control_plane_acl_show).__name__
        )
    bound_acl_names = set(control_plane_acl_show.keys())
    if mangle_state and mangle_state.removed_control_plane_acl_id:
        removed = mangle_state.removed_control_plane_acl_id
        if removed in bound_acl_names:
            logger.info("Excluding deliberately-unset ACL %r from baseline", removed)
            bound_acl_names.discard(removed)
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
