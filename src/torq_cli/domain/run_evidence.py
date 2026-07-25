"""Schema-v2 contracts for governed run, attempt, and action evidence."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from torq_cli.domain.evidence_transitions import transition_authority_finding
from torq_cli.domain.run_plan import (
    PLAN_CONTRACT,
    initial_plan_body,
    plan_hash,
    revision_body,
)


ATTEMPT_TRANSITIONS = frozenset(
    {
        "stage_attempt_created",
        "stage_blocked",
        "stage_rejected",
        "stage_dispatch_started",
        "stage_completed",
        "stage_failed",
        "stage_interrupted",
    }
)
TERMINAL_ATTEMPT_TRANSITIONS = frozenset(
    {
        "stage_blocked",
        "stage_rejected",
        "stage_completed",
        "stage_failed",
        "stage_interrupted",
    }
)
CORE_LANES = ("g1d", "g1r", "builder", "g2a")
CONDITIONAL_LANES = ("refine_bug", "refine_ui")
LANE_ORDER = CORE_LANES + CONDITIONAL_LANES


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 71
        and value.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in value[7:])
    )


# A receipt body is signed evidence, not a place for operator prose. The
# producer already bounds operator input, but the verifier is the portable
# trust boundary: a receipt written by any other path must still be refused
# here, so the shapes below constrain what each field may carry.
#: An opaque identifier: a short token, no spaces, no sentences.
_OPAQUE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,127}")
#: A relative artifact path. Machine-generated, so the concern is not prose but
#: length and traversal; either separator is tolerated because existing stores
#: were written on Windows with backslashes. `..` is refused separately.
_ARTIFACT_PATH = re.compile(r"[A-Za-z0-9][A-Za-z0-9/\\._-]{0,255}")
#: A well-formed MIME type: type/subtype with an optional parameter (e.g.
#: `text/plain; charset=utf-8`), case-insensitive as the producer accepts. The
#: whole thing is bounded so a bare `startswith` cannot smuggle a megabyte of
#: prose behind the `text/` prefix, while a real charset parameter still passes.
_MEDIA_TYPE = re.compile(
    r"[A-Za-z]+/[A-Za-z0-9][A-Za-z0-9.+_-]{0,63}"
    r"(?:\s*;\s*[A-Za-z0-9-]{1,32}=[A-Za-z0-9._-]{1,32})?"
)
#: A redaction is a pattern name, uppercase and bounded — never free text.
_REDACTION_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
#: Operator-facing labels on action and decision receipts (a summary, a
#: resolver's name, a short finding). Bounded so the body cannot become a
#: prose channel; content that needs room belongs in an encrypted artifact.
MAX_ACTION_TEXT_LEN = 512
#: An operator-supplied filename is provenance, not content.
MAX_SOURCE_NAME_LEN = 256
#: The redaction registry has a handful of pattern names; a receipt naming more
#: distinct hits than the registry could produce is malformed. Generous so a
#: real list never trips it, bounded so it cannot accumulate bytes.
MAX_REDACTION_NAMES = 64
#: A decision names at most the lanes that were dispatched — one per lane.
MAX_DISPATCHED_ROLES = len(LANE_ORDER)
#: The largest a `content_bytes` count may claim. Mirrors the 1 MiB ingress cap
#: in application/artifact_extraction.py (MAX_ARTIFACT_BYTES); kept local rather
#: than imported because domain must not depend on application.
MAX_CONTENT_BYTES = 1_048_576


def _matches(pattern: re.Pattern[str], value: object) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


#: A label is one line of printable text. Newlines, tabs, and other control
#: characters are refused so a bounded field cannot carry a formatted document
#: within its length budget.
_CONTROL_CHARS = frozenset(chr(c) for c in range(0x20)) | {"\x7f"}


def _bounded_label(value: object, limit: int = MAX_ACTION_TEXT_LEN) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= limit
        and _CONTROL_CHARS.isdisjoint(value)
    )


def _bounded_content_bytes(value: object) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 <= value <= MAX_CONTENT_BYTES
    )


def _redaction_names(value: object) -> bool:
    # A tuple pre-append, a list after the JSON round-trip; a str is refused so
    # prose cannot ride through one character at a time. The list is also
    # length-capped: the registry has a handful of pattern names, so a long
    # list is not evidence — it is bytes accumulating one valid token at a time.
    if not isinstance(value, (list, tuple)) or len(value) > MAX_REDACTION_NAMES:
        return False
    return all(_matches(_REDACTION_NAME, item) for item in value)


def _extra_keys(payload: Mapping[str, Any], allowed: frozenset[str]) -> bool:
    """True when the payload carries a key outside the allowed set."""
    return bool(set(payload) - allowed)


#: No single string value anywhere in a receipt may exceed this. A receipt
#: records identifiers, hashes, enums, short labels, and bounded operator
#: labels — none longer than this. A value past it is a document, and documents
#: belong in the encrypted artifact. This is the floor under every transition,
#: including those (run_planned, stage_*, repair_routed, run_abandoned) that no
#: operator ingress reaches: it costs nothing and closes the class against a
#: writer that is buggy or compromised, not only against operator input.
MAX_RECEIPT_STRING = 4096
#: Structural fan-out cap so a payload cannot smuggle bytes as a giant list or a
#: deeply nested structure of otherwise-valid short values.
MAX_RECEIPT_ITEMS = 512
MAX_RECEIPT_DEPTH = 8


def _oversized_value(value: object, *, depth: int = 0) -> bool:
    """True when any string is too long, or the structure is too wide/deep."""
    remaining = [MAX_RECEIPT_ITEMS]

    def walk(item: object, item_depth: int) -> bool:
        if item_depth > MAX_RECEIPT_DEPTH:
            return True
        remaining[0] -= 1
        if remaining[0] < 0:
            return True
        if isinstance(item, str):
            return len(item) > MAX_RECEIPT_STRING
        if isinstance(item, Mapping):
            return any(
                not isinstance(key, str)
                or len(key) > MAX_RECEIPT_STRING
                or walk(child, item_depth + 1)
                for key, child in item.items()
            )
        if isinstance(item, (list, tuple)):
            return any(walk(child, item_depth + 1) for child in item)
        return False

    return walk(value, depth)


#: Every key each governed receipt may carry. An undeclared key is refused with
#: the transition's own `*_invalid` finding, so a renamed field cannot smuggle
#: what the value schema keeps out of the declared ones.
#:
#: These sets must stay in step with what the producer emits in
#: application/orchestrator.py (inject_context / inject_artifact) and the
#: supervisor/recovery paths. Drift is not silent: every happy-path producer
#: test appends a real receipt and then verifies, so a forgotten key trips
#: `_extra_keys` and those tests fail — but add the key here when you add it
#: there.
COMMAND_ACCEPTED_KEYS = frozenset(
    {
        "command_id",
        "command_type",
        "context_id",
        "target_role",
        "route",
        "artifact",
        "artifact_hash",
        "media_type",
        "source_name",
        "content_bytes",
        "redactions",
        "extraction",
        "direct_route_confirmed",
        "earliest_eligible_attempt",
        "provider_dispatch",
    }
)
COMMAND_REJECTED_KEYS = frozenset(
    {
        "command_id",
        "command_type",
        "target_role",
        "finding",
        # A rejection records the input it refused, including the operator's
        # `media_type` and `source_name`. Those are bounded below rather than
        # dropped, so the rejection stays truthful without becoming a channel.
        "media_type",
        "source_name",
        "earliest_eligible_attempt",
        "content_bytes",
        "provider_dispatch",
    }
)
CONTEXT_INJECTED_KEYS = frozenset(
    {
        "command_id",
        "command_type",
        "context_id",
        "target_role",
        "route",
        "artifact",
        "artifact_hash",
        "media_type",
        "source_name",
        "content_bytes",
        "redactions",
        # Provenance carried forward from the accepted receipt so the verifier
        # can prove the applied record matches what was acknowledged. These are
        # structured, not prose, and are checked field-by-field downstream.
        "extraction",
        "direct_route_confirmed",
        "accepted_sequence",
        "effective_attempt",
        "provider_dispatch",
    }
)
COMMAND_UNAPPLIED_KEYS = frozenset(
    {
        "command_id",
        "accepted_sequence",
        "target_role",
        "reason",
        "last_covered_sequence",
        "provider_dispatch",
    }
)
RUN_REPLANNED_KEYS = frozenset(
    {
        "command_id",
        "plan_contract",
        "plan_revision",
        "previous_replan_sequence",
        "accepted_sequence",
        "reason",
        "old_plan_hash",
        "new_plan_hash",
        "affected_future_attempts",
        "provider_dispatch",
    }
)
ACTION_OPENED_KEYS = frozenset(
    {
        "action_id", "type", "scope", "target", "summary",
        "allowed_resolutions", "outcome_map", "caused_by_sequence",
        "provider_dispatch",
    }
)
ACTION_RESOLVED_KEYS = frozenset(
    {"action_id", "resolution", "resolver_identity", "opened_sequence", "provider_dispatch"}
)
RUN_PLANNED_KEYS = frozenset(
    {
        "mode",
        "profile_id",
        "strategy_id",
        "planned_roles",
        "lane_catalog",
        "plan_contract",
        "plan_hash",
        "rate_table_version",
        "rate_table_hash",
    }
)
RUN_ABANDONED_KEYS = frozenset(
    {"attempt_ids", "last_covered_sequence", "operator_assertion"}
)
#: Every key any `run_decision` may carry, across all three writers.
RUN_DECISION_KEYS = frozenset(
    {
        "decision",
        "provider_dispatch",
        "reason",
        "outcome",
        "next_action",
        "stage",
        "dispatched_roles",
        "repair_cycles",
        "action_id",
        "action_opened_sequence",
        "action_resolved_sequence",
        "interruption_sequence",
    }
)
#: Free-text fields a decision receipt may carry; each is a bounded label.
_DECISION_TEXT_FIELDS = ("reason", "outcome", "next_action", "stage")


def _command_values_ok(payload: Mapping[str, Any]) -> bool:
    """Whether every present command field carries a well-shaped value.

    The caller reports a transition-level finding, so this returns only whether
    the values pass, not which one failed. Each field is checked only when
    present: an absent optional field is fine, a present malformed one is not.
    """
    artifact = payload.get("artifact")
    artifact_ok = _matches(_ARTIFACT_PATH, artifact) and ".." not in str(artifact)
    source_name = payload.get("source_name")
    # An absent filename is provenance the operator simply did not give; only a
    # present one must be a bounded label.
    source_ok = source_name is None or _bounded_label(source_name, MAX_SOURCE_NAME_LEN)
    checks: tuple[tuple[str, bool], ...] = (
        ("command_id", _matches(_OPAQUE_ID, payload.get("command_id"))),
        ("context_id", _matches(_OPAQUE_ID, payload.get("context_id"))),
        ("artifact", artifact_ok),
        ("media_type", _matches(_MEDIA_TYPE, payload.get("media_type"))),
        ("source_name", source_ok),
        ("redactions", _redaction_names(payload.get("redactions"))),
        ("content_bytes", _bounded_content_bytes(payload.get("content_bytes"))),
    )
    return all(ok for field, ok in checks if field in payload)


def _decision_text_finding(payload: Mapping[str, Any]) -> str | None:
    """Bound the operator-facing strings a decision receipt may carry."""
    if _extra_keys(payload, RUN_DECISION_KEYS):
        return "run_decision_text_invalid"
    for field in _DECISION_TEXT_FIELDS:
        if field in payload and not _bounded_label(payload.get(field)):
            return "run_decision_text_invalid"
    roles = payload.get("dispatched_roles")
    if roles is not None and not (
        isinstance(roles, (list, tuple))
        and len(roles) <= MAX_DISPATCHED_ROLES
        and all(role in LANE_ORDER for role in roles)
        and len(set(roles)) == len(roles)
    ):
        return "run_decision_text_invalid"
    if "provider_dispatch" in payload and not isinstance(
        payload.get("provider_dispatch"), bool
    ):
        return "run_decision_structure_invalid"
    if "repair_cycles" in payload and not _nonnegative_int(
        payload.get("repair_cycles")
    ):
        return "run_decision_structure_invalid"
    if "action_id" in payload and not _matches(
        _OPAQUE_ID, payload.get("action_id")
    ):
        return "run_decision_structure_invalid"
    for field in (
        "action_opened_sequence",
        "action_resolved_sequence",
        "interruption_sequence",
    ):
        if field in payload and not _positive_int(payload.get(field)):
            return "run_decision_structure_invalid"
    return None


def validate_receipt_payload(
    transition: str,
    payload: Mapping[str, Any],
    *,
    writer_role: str,
    legacy: bool = False,
) -> str | None:
    """Validate the local shape that can be checked before append."""
    # The floor under every transition: no receipt, whatever its type, may
    # carry a document-sized string or an accumulating structure. Transitions
    # with their own allowlist bound their fields more tightly than this; the
    # floor catches the rest, including the writer-only lifecycle transitions.
    if not legacy and _oversized_value(dict(payload)):
        return "receipt_value_oversized"
    if transition in ATTEMPT_TRANSITIONS:
        if not isinstance(payload.get("role"), str):
            return "attempt_role_invalid"
        attempt_id = payload.get("attempt_id")
        if not isinstance(attempt_id, str) or not attempt_id:
            return "attempt_id_invalid"
        if not _positive_int(payload.get("attempt_ordinal")):
            return "attempt_ordinal_invalid"
        if not _nonnegative_int(payload.get("repair_cycle")):
            return "attempt_repair_cycle_invalid"
        dispatch = payload.get("provider_dispatch")
        if transition == "stage_attempt_created" and dispatch is not False:
            return "attempt_dispatch_invalid"
        if transition == "stage_blocked" and dispatch is not False:
            return "attempt_dispatch_invalid"
        if transition in {"stage_dispatch_started", "stage_completed", "stage_rejected"}:
            if dispatch is not True:
                return "attempt_dispatch_invalid"
        if transition == "stage_rejected" and (
            payload.get("role") not in {"g1r", "g2a"}
            or payload.get("verdict") != "reject"
            or payload.get("reason")
            != (
                "design_rejected"
                if payload.get("role") == "g1r"
                else "audit_rejected"
            )
        ):
            return "stage_rejected_invalid"
        if transition == "stage_failed" and not isinstance(dispatch, bool):
            return "attempt_dispatch_invalid"
        if transition == "stage_interrupted" and dispatch not in {
            False,
            True,
            "unknown",
        }:
            return "attempt_dispatch_invalid"
        if (
            transition == "stage_interrupted"
            and not legacy
            and payload.get("observation_source") != "worker_exit"
        ):
            return "stage_interrupted_observation_invalid"
    elif transition == "run_planned":
        if not legacy and _extra_keys(payload, RUN_PLANNED_KEYS):
            return "lane_catalog_invalid"
        catalog = payload.get("lane_catalog")
        if not isinstance(catalog, list) or len(catalog) != len(LANE_ORDER):
            return "lane_catalog_invalid"
        roles = [lane.get("role") for lane in catalog if isinstance(lane, Mapping)]
        if tuple(roles) != LANE_ORDER:
            return "lane_catalog_order_invalid"
        if not legacy:
            for lane in catalog:
                if (
                    not isinstance(lane, Mapping)
                    or not isinstance(lane.get("required"), bool)
                    or (
                        payload.get("mode") != "dry_run"
                        and
                        lane.get("role") not in CONDITIONAL_LANES
                        and lane.get("required") is not True
                    )
                ):
                    return "lane_catalog_required_invalid"
        contract = payload.get("plan_contract")
        digest = payload.get("plan_hash")
        if (contract is None) != (digest is None):
            return "run_plan_contract_invalid"
        if contract is not None:
            if contract != PLAN_CONTRACT or not _sha256(digest):
                return "run_plan_contract_invalid"
            body = initial_plan_body(
                profile_id=str(payload.get("profile_id", "")),
                strategy_id=str(payload.get("strategy_id", "")),
                planned_roles=[str(role) for role in payload.get("planned_roles", ())],
                lane_catalog=[dict(row) for row in catalog if isinstance(row, Mapping)],
            )
            if digest != plan_hash(body):
                return "run_plan_hash_invalid"
    elif transition == "repair_routed":
        if payload.get("target_role") not in CONDITIONAL_LANES:
            return "repair_route_invalid"
        if not isinstance(payload.get("attempt_id"), str):
            return "repair_route_attempt_invalid"
        if not _positive_int(payload.get("attempt_ordinal")):
            return "repair_route_attempt_invalid"
        if not _positive_int(payload.get("cycle")):
            return "repair_route_cycle_invalid"
    elif transition == "action_opened":
        # An undeclared key is refused, and the operator-facing strings are
        # bounded labels, not documents: a signed receipt is no place for
        # unbounded prose whichever transition carries it.
        if not legacy and _extra_keys(payload, ACTION_OPENED_KEYS):
            return "action_opened_invalid"
        required: tuple[str, ...] = (
            "action_id",
            "type",
            "scope",
            "target",
            "summary",
        )
        if any(
            not (
                isinstance(payload.get(field), str)
                if legacy
                else _bounded_label(payload.get(field))
            )
            for field in required
        ):
            return "action_opened_invalid"
        if not _positive_int(payload.get("caused_by_sequence")):
            return "action_opened_invalid"
        if not legacy:
            allowed = payload.get("allowed_resolutions")
            outcome_map = payload.get("outcome_map")
            if (
                not isinstance(allowed, list)
                or not allowed
                or len(allowed) > 8
                or any(not _matches(_OPAQUE_ID, item) for item in allowed)
                or len(set(allowed)) != len(allowed)
                or not isinstance(outcome_map, Mapping)
                or set(outcome_map) != set(allowed)
                or any(
                    value not in {"completed", "blocked", "failed"}
                    for value in outcome_map.values()
                )
            ):
                return "action_outcome_map_invalid"
    elif transition == "command_accepted":
        if not legacy and _extra_keys(payload, COMMAND_ACCEPTED_KEYS):
            return "command_accept_invalid"
        required = (
            "command_id",
            "command_type",
            "context_id",
            "target_role",
            "route",
            "artifact",
            "artifact_hash",
            "media_type",
        )
        if any(
            not isinstance(payload.get(field), str) or not payload.get(field)
            for field in required
        ):
            return "command_accept_invalid"
        # Bound the operator-influenced values, not just their presence: an id
        # is an opaque token, a hash is a hash, media_type is a MIME token, and
        # redactions are pattern names — none of them a place for prose.
        if not legacy and not _command_values_ok(payload):
            return "command_accept_invalid"
        if not legacy and not _sha256(payload.get("artifact_hash")):
            return "command_accept_invalid"
        if payload.get("command_type") not in {"context", "artifact"}:
            return "command_accept_invalid"
        extraction = payload.get("extraction")
        if payload.get("command_type") == "artifact" and (
            not isinstance(extraction, Mapping)
            or not legacy
            and set(extraction)
            != {"contract_version", "extractor", "source_bytes", "extracted_bytes"}
            or extraction.get("contract_version") != "1.0.0"
            or not (
                isinstance(extraction.get("extractor"), str)
                if legacy
                else _bounded_label(extraction.get("extractor"))
            )
            or not _positive_int(extraction.get("source_bytes"))
            or not _positive_int(extraction.get("extracted_bytes"))
        ):
            return "command_extraction_invalid"
        if payload.get("command_type") == "context" and extraction is not None:
            return "command_extraction_invalid"
        if payload.get("target_role") not in {"lead", *LANE_ORDER}:
            return "command_target_invalid"
        if payload.get("route") not in {"lead_replan", "direct_lane"}:
            return "command_accept_invalid"
        if payload.get("direct_route_confirmed") is not True:
            return "command_accept_invalid"
        boundary = payload.get("earliest_eligible_attempt")
        if (
            not isinstance(boundary, Mapping)
            or not legacy
            and set(boundary) != {"kind"}
            or boundary.get("kind") != "attempt_created_after_acknowledgement"
        ):
            return "command_boundary_invalid"
        if not _nonnegative_int(payload.get("content_bytes")):
            return "command_accept_invalid"
        if payload.get("provider_dispatch") is not False:
            return "command_accept_invalid"
    elif transition == "command_rejected":
        if not legacy and _extra_keys(payload, COMMAND_REJECTED_KEYS):
            return "command_rejection_invalid"
        if (
            not (
                isinstance(payload.get("command_id"), str)
                and bool(payload.get("command_id"))
                if legacy
                else _matches(_OPAQUE_ID, payload.get("command_id"))
            )
            or payload.get("command_type") not in {"context", "artifact"}
            or not (
                isinstance(payload.get("finding"), str)
                and bool(payload.get("finding"))
                if legacy
                else _bounded_label(payload.get("finding"))
            )
            or payload.get("earliest_eligible_attempt") is not None
            or payload.get("provider_dispatch") is not False
            or not _nonnegative_int(payload.get("content_bytes"))
        ):
            return "command_rejection_invalid"
        # A rejection records the input it refused, and the reason may be that
        # the input was itself malformed or oversized — an unknown target, a bad
        # media type, a too-large byte count. So the echoed fields are held only
        # to a length bound, not to accepted-grade validity: a rejection can
        # record a bad value but not a megabyte of prose.
        echoed = ("media_type", "source_name", "target_role")
        if not legacy and any(
            field in payload
            and payload.get(field) is not None
            and not _bounded_label(payload.get(field))
            for field in echoed
        ):
            return "command_rejection_invalid"
    elif transition == "context_injected" and "command_id" in payload:
        if not legacy and _extra_keys(payload, CONTEXT_INJECTED_KEYS):
            return "command_effective_attempt_invalid"
        required = (
            "command_id",
            "context_id",
            "target_role",
            "artifact",
            "artifact_hash",
            "media_type",
        )
        effective = payload.get("effective_attempt")
        if (
            any(
                not isinstance(payload.get(field), str) or not payload.get(field)
                for field in required
            )
            or not legacy
            and not _command_values_ok(payload)
            or not legacy
            and not _sha256(payload.get("artifact_hash"))
            or not _positive_int(payload.get("accepted_sequence"))
            or not isinstance(effective, Mapping)
            or not legacy
            and set(effective)
            != {"role", "attempt_id", "attempt_ordinal", "attempt_created_sequence"}
            or not isinstance(effective.get("role"), str)
            or not isinstance(effective.get("attempt_id"), str)
            or not _positive_int(effective.get("attempt_ordinal"))
            or not _positive_int(effective.get("attempt_created_sequence"))
            or payload.get("provider_dispatch") is not False
        ):
            return "command_effective_attempt_invalid"
    elif transition == "command_unapplied":
        if not legacy and _extra_keys(payload, COMMAND_UNAPPLIED_KEYS):
            return "command_unapplied_invalid"
        target = payload.get("target_role")
        if (
            not (
                isinstance(payload.get("command_id"), str)
                and bool(payload.get("command_id"))
                if legacy
                else _matches(_OPAQUE_ID, payload.get("command_id"))
            )
            or not _positive_int(payload.get("accepted_sequence"))
            or payload.get("reason") not in {
                "no_eligible_future_attempt",
                "run_terminating",
            }
            or not _positive_int(payload.get("last_covered_sequence"))
            or payload.get("provider_dispatch") is not False
            or not legacy
            and (target is not None and target not in {"lead", *LANE_ORDER})
        ):
            return "command_unapplied_invalid"
    elif transition == "run_replanned":
        if not legacy and _extra_keys(payload, RUN_REPLANNED_KEYS):
            return "run_replanned_invalid"
        affected = payload.get("affected_future_attempts")
        if (
            not (
                isinstance(payload.get("command_id"), str)
                and bool(payload.get("command_id"))
                if legacy
                else _matches(_OPAQUE_ID, payload.get("command_id"))
            )
            or payload.get("plan_contract") != PLAN_CONTRACT
            or not _positive_int(payload.get("plan_revision"))
            or payload.get("previous_replan_sequence") is not None
            and not _positive_int(payload.get("previous_replan_sequence"))
            or not _positive_int(payload.get("accepted_sequence"))
            or payload.get("reason") != "operator_context"
            or not _sha256(payload.get("old_plan_hash"))
            or not _sha256(payload.get("new_plan_hash"))
            or payload.get("old_plan_hash") == payload.get("new_plan_hash")
            or not isinstance(affected, Sequence)
            or isinstance(affected, (str, bytes))
            or len(affected) != 1
            or payload.get("provider_dispatch") is not False
        ):
            return "run_replanned_invalid"
        candidate = affected[0]
        if (
            not isinstance(candidate, Mapping)
            or not legacy
            and set(candidate)
            != {"role", "attempt_id", "attempt_ordinal", "attempt_created_sequence"}
            or candidate.get("role") not in LANE_ORDER
            or not isinstance(candidate.get("attempt_id"), str)
            or not _positive_int(candidate.get("attempt_ordinal"))
            or not _positive_int(candidate.get("attempt_created_sequence"))
        ):
            return "run_replanned_invalid"
        body = revision_body(
            plan_revision=int(payload["plan_revision"]),
            previous_plan_hash=str(payload["old_plan_hash"]),
            command_id=str(payload["command_id"]),
            accepted_sequence=int(payload["accepted_sequence"]),
            affected_future_attempts=[candidate],
        )
        if payload.get("new_plan_hash") != plan_hash(body):
            return "run_replanned_hash_invalid"
    elif transition == "action_resolved":
        if not legacy and _extra_keys(payload, ACTION_RESOLVED_KEYS):
            return "action_resolved_invalid"
        required = ("action_id", "resolution", "resolver_identity")
        if any(
            not (
                isinstance(payload.get(field), str)
                if legacy
                else _bounded_label(payload.get(field))
            )
            for field in required
        ):
            return "action_resolved_invalid"
        if not _positive_int(payload.get("opened_sequence")):
            return "action_resolved_invalid"
    elif transition == "run_decision" and writer_role == "supervisor":
        expected = "workflow_failed" if legacy else "failed"
        field = "status" if legacy else "decision"
        if payload.get(field) != expected:
            return "supervisor_decision_invalid"
        if not _positive_int(payload.get("interruption_sequence")):
            return "supervisor_decision_invalid"
        if not legacy and _decision_text_finding(payload) is not None:
            return "supervisor_decision_invalid"
    elif transition == "run_decision" and writer_role == "operator_gateway":
        field = "status" if legacy else "decision"
        allowed = {"workflow_closed"} if legacy else {"completed", "blocked", "failed"}
        if payload.get(field) not in allowed:
            return "operator_decision_invalid"
        if not isinstance(payload.get("action_id"), str):
            return "operator_decision_invalid"
        if not _positive_int(payload.get("action_resolved_sequence")):
            return "operator_decision_invalid"
        if not legacy and _decision_text_finding(payload) is not None:
            return "operator_decision_invalid"
    elif transition == "run_decision":
        if not legacy and payload.get("decision") not in {
            "awaiting_approval",
            "blocked",
            "completed",
            "failed",
        }:
            return "run_decision_value_invalid"
        finding = None if legacy else _decision_text_finding(payload)
        if finding is not None:
            return finding
    elif transition == "run_abandoned" and writer_role == "recovery":
        if not legacy and _extra_keys(payload, RUN_ABANDONED_KEYS):
            return "run_abandoned_invalid"
        attempt_ids = payload.get("attempt_ids")
        if (
            not isinstance(attempt_ids, list)
            or not attempt_ids
            or any(not isinstance(value, str) or not value for value in attempt_ids)
            or len(set(attempt_ids)) != len(attempt_ids)
            or not _positive_int(payload.get("last_covered_sequence"))
            or payload.get("operator_assertion") != "no_live_worker"
        ):
            return "run_abandoned_invalid"
    elif transition == "terminalization_started":
        if set(payload) != {"reason", "provider_dispatch"} or (
            payload.get("reason") != "pending_commands"
            or payload.get("provider_dispatch") is not False
        ):
            return "terminalization_started_invalid"
    return None


def validate_v2_receipt_contract(
    receipts: Sequence[Mapping[str, Any]],
    *,
    sealed: bool,
    legacy: bool = False,
) -> str | None:
    """Validate cross-receipt lifecycle invariants after crypto verification."""
    attempts: dict[str, dict[str, Any]] = {}
    ordinals: dict[str, int] = {}
    repairs: dict[str, tuple[str, int, int]] = {}
    open_actions: dict[str, int] = {}
    action_outcomes: dict[str, dict[str, str]] = {}
    commands: dict[str, dict[str, Any]] = {}
    run_terminating = False
    resolved_actions: dict[int, tuple[str, str | None]] = {}
    interruptions: set[int] = set()
    terminal_decision = False
    execution_complete_action_open = False
    waiting_on_operator = False
    saw_run_planned = False
    saw_catalog = False
    catalog_roles: set[str] = set()
    required_roles: set[str] = set()
    current_plan_hash: str | None = None
    plan_revision = 0
    last_replan_sequence: int | None = None
    replanned_attempts: dict[str, Mapping[str, Any]] = {}

    for receipt in receipts:
        transition = str(receipt.get("transition", ""))
        sequence = receipt.get("sequence")
        payload = receipt.get("payload")
        writer_role = str(receipt.get("writer_role", ""))
        if not isinstance(payload, Mapping) or not _positive_int(sequence):
            return "receipt_payload_invalid"
        assert isinstance(sequence, int) and not isinstance(sequence, bool)
        sequence_number = sequence
        if terminal_decision:
            return "receipt_after_terminal_decision"
        if run_terminating and transition not in {
            "command_unapplied",
            "run_abandoned",
            "run_decision",
        }:
            return "receipt_after_terminating_decision"
        if "evidence_basis" in receipt:
            authority_finding = transition_authority_finding(
                writer_role,
                transition,
                receipt.get("evidence_basis"),
                payload,
                legacy=legacy,
            )
            if authority_finding is not None:
                return authority_finding
        finding = validate_receipt_payload(
            transition,
            payload,
            writer_role=writer_role,
            legacy=legacy,
        )
        if finding is not None:
            return finding

        if transition == "run_planned":
            saw_run_planned = True
            if saw_catalog:
                return "lane_catalog_duplicate"
            saw_catalog = True
            catalog = payload.get("lane_catalog", [])
            catalog_roles = {
                str(row["role"])
                for row in catalog
                if isinstance(row, Mapping) and isinstance(row.get("role"), str)
            }
            required_roles = {
                str(row["role"])
                for row in catalog
                if isinstance(row, Mapping)
                and (
                    row.get("required") is True
                    or legacy and row.get("role") not in CONDITIONAL_LANES
                )
            }
            sealed_plan_hash = payload.get("plan_hash")
            if sealed_plan_hash is not None:
                if not _sha256(sealed_plan_hash):
                    return "run_plan_hash_invalid"
                current_plan_hash = str(sealed_plan_hash)
        if transition == "terminalization_started":
            if run_terminating or open_actions or not any(
                command["transition"] == "command_accepted"
                and command["finalized"] is None
                for command in commands.values()
            ):
                return "terminalization_started_precondition_invalid"
            run_terminating = True
        if transition == "repair_routed":
            attempt_id = str(payload["attempt_id"])
            if attempt_id in repairs:
                return "repair_route_attempt_duplicate"
            repairs[attempt_id] = (
                str(payload["target_role"]),
                int(payload["attempt_ordinal"]),
                int(payload["cycle"]),
            )
        if transition in {"command_accepted", "command_rejected"}:
            command_id = str(payload["command_id"])
            if command_id in commands:
                return "command_id_duplicate"
            commands[command_id] = {
                "transition": transition,
                "sequence": sequence_number,
                "target_role": payload.get("target_role"),
                "artifact": payload.get("artifact"),
                "artifact_hash": payload.get("artifact_hash"),
                "provenance": {
                    field: payload.get(field)
                    for field in (
                        "command_type",
                        "context_id",
                        "target_role",
                        "artifact",
                        "artifact_hash",
                        "media_type",
                        "source_name",
                        "content_bytes",
                        "redactions",
                        "extraction",
                        "direct_route_confirmed",
                    )
                },
                "finalized": None,
                "replanned": False,
            }
        if transition == "run_replanned":
            command_id = str(payload["command_id"])
            accepted = commands.get(command_id)
            if (
                accepted is None
                or accepted["transition"] != "command_accepted"
                or accepted["target_role"] != "lead"
                or accepted["finalized"] is not None
                or accepted["replanned"]
                or current_plan_hash is None
                or payload["accepted_sequence"] != accepted["sequence"]
                or payload["plan_revision"] != plan_revision + 1
                or payload["previous_replan_sequence"] != last_replan_sequence
                or payload["old_plan_hash"] != current_plan_hash
            ):
                return "run_replanned_precondition_invalid"
            affected = payload["affected_future_attempts"]
            if not isinstance(affected, list) or len(affected) != 1:
                return "run_replanned_precondition_invalid"
            effective = affected[0]
            if not isinstance(effective, Mapping):
                return "run_replanned_precondition_invalid"
            attempt = attempts.get(str(effective.get("attempt_id")))
            if (
                attempt is None
                or attempt["role"] != effective.get("role")
                or attempt["ordinal"] != effective.get("attempt_ordinal")
                or attempt["created_sequence"]
                != effective.get("attempt_created_sequence")
                or attempt["created_sequence"] <= accepted["sequence"]
                or attempt["dispatched"]
                or attempt["terminal"] is not None
            ):
                return "run_replanned_attempt_invalid"
            accepted["replanned"] = True
            replanned_attempts[command_id] = effective
            current_plan_hash = str(payload["new_plan_hash"])
            plan_revision += 1
            last_replan_sequence = sequence_number
        if transition == "context_injected" and "command_id" in payload:
            command_id = str(payload["command_id"])
            accepted = commands.get(command_id)
            if accepted is None or accepted["transition"] != "command_accepted":
                return "command_accept_missing"
            if accepted["finalized"] is not None:
                return "command_already_finalized"
            if payload.get("accepted_sequence") != accepted["sequence"]:
                return "command_accept_link_invalid"
            if (
                payload.get("artifact") != accepted["artifact"]
                or payload.get("artifact_hash") != accepted["artifact_hash"]
                or payload.get("target_role") != accepted["target_role"]
            ):
                return "command_accept_link_invalid"
            if any(
                payload.get(field) != value
                for field, value in accepted["provenance"].items()
            ):
                return "command_extraction_link_invalid"
            if accepted["target_role"] == "lead" and not accepted["replanned"]:
                return "command_replan_missing"
            if accepted["target_role"] == "lead" and replanned_attempts.get(
                command_id
            ) != effective:
                return "command_replan_attempt_mismatch"
            effective = payload["effective_attempt"]
            assert isinstance(effective, Mapping)
            attempt_id = str(effective["attempt_id"])
            attempt = attempts.get(attempt_id)
            if (
                attempt is None
                or attempt["role"] != effective["role"]
                or attempt["ordinal"] != effective["attempt_ordinal"]
                or attempt["created_sequence"]
                != effective["attempt_created_sequence"]
                or attempt["dispatched"]
            ):
                return "command_effective_attempt_invalid"
            if int(attempt["created_sequence"]) <= int(accepted["sequence"]):
                return "command_boundary_violated"
            if (
                accepted["target_role"] != "lead"
                and accepted["target_role"] != attempt["role"]
            ):
                return "command_effective_attempt_invalid"
            accepted["finalized"] = "context_injected"
        if transition == "command_unapplied":
            command_id = str(payload["command_id"])
            accepted = commands.get(command_id)
            if accepted is None or accepted["transition"] != "command_accepted":
                return "command_accept_missing"
            if accepted["finalized"] is not None:
                return "command_already_finalized"
            if payload.get("accepted_sequence") != accepted["sequence"]:
                return "command_accept_link_invalid"
            if payload.get("last_covered_sequence") != sequence_number - 1:
                return "command_unapplied_precondition_invalid"
            if payload.get("reason") == "run_terminating" and not run_terminating:
                return "command_unapplied_precondition_invalid"
            accepted["finalized"] = "command_unapplied"
            accepted["unapplied_reason"] = payload.get("reason")
        if transition in ATTEMPT_TRANSITIONS:
            attempt_id = str(payload["attempt_id"])
            role = str(payload["role"])
            ordinal = int(payload["attempt_ordinal"])
            cycle = int(payload["repair_cycle"])
            if transition == "stage_attempt_created":
                if attempt_id in attempts:
                    return "attempt_id_duplicate"
                if catalog_roles and role not in catalog_roles:
                    return "attempt_lane_not_cataloged"
                if any(
                    attempt["role"] == role and attempt["terminal"] is None
                    for attempt in attempts.values()
                ):
                    return "attempt_lane_already_open"
                expected = ordinals.get(role, 0) + 1
                if ordinal != expected:
                    return "attempt_ordinal_discontinuity"
                ordinals[role] = ordinal
                attempts[attempt_id] = {
                    "role": role,
                    "ordinal": ordinal,
                    "cycle": cycle,
                    "dispatched": False,
                    "terminal": None,
                    "created_sequence": sequence_number,
                }
                for command in commands.values():
                    if (
                        command.get("finalized") == "command_unapplied"
                        and command.get("target_role") in {"lead", role}
                    ):
                        return "command_unapplied_precondition_invalid"
                routed = repairs.get(attempt_id)
                if role in CONDITIONAL_LANES and routed != (role, ordinal, cycle):
                    return "repair_route_attempt_mismatch"
                continue
            attempt = attempts.get(attempt_id)
            if attempt is None:
                return "attempt_created_missing"
            if (
                attempt["role"] != role
                or attempt["ordinal"] != ordinal
                or attempt["cycle"] != cycle
            ):
                return "attempt_identity_mismatch"
            if attempt["terminal"] is not None:
                return "attempt_transition_after_terminal"
            if transition == "stage_dispatch_started":
                if attempt["dispatched"]:
                    return "attempt_dispatch_duplicate"
                attempt["dispatched"] = True
            elif transition in TERMINAL_ATTEMPT_TRANSITIONS:
                if transition == "stage_completed" and not attempt["dispatched"]:
                    return "attempt_completed_without_dispatch"
                if transition == "stage_rejected" and not attempt["dispatched"]:
                    return "attempt_rejected_without_dispatch"
                if payload["provider_dispatch"] is True and not attempt["dispatched"]:
                    return "attempt_dispatch_evidence_missing"
                attempt["terminal"] = transition
                if transition == "stage_interrupted":
                    interruptions.add(sequence_number)
        elif transition == "action_opened":
            action_id = str(payload["action_id"])
            if action_id in open_actions or any(
                resolved[0] == action_id for resolved in resolved_actions.values()
            ):
                return "action_id_duplicate"
            if int(payload["caused_by_sequence"]) >= sequence_number:
                return "action_cause_invalid"
            open_actions[action_id] = sequence_number
            if not legacy:
                action_outcomes[action_id] = {
                    str(key): str(value)
                    for key, value in payload["outcome_map"].items()
                }
        elif transition == "action_resolved":
            action_id = str(payload["action_id"])
            opened_sequence = open_actions.get(action_id)
            if opened_sequence is None:
                return "action_open_missing"
            if payload["opened_sequence"] != opened_sequence:
                return "action_open_link_invalid"
            mapped_decision = (
                None
                if legacy
                else action_outcomes.get(action_id, {}).get(
                    str(payload.get("resolution"))
                )
            )
            if not legacy and mapped_decision is None:
                return "action_resolution_unauthorized"
            del open_actions[action_id]
            resolved_actions[sequence_number] = (action_id, mapped_decision)
        elif transition == "run_decision" and writer_role == "supervisor":
            if payload["interruption_sequence"] not in interruptions:
                return "supervisor_decision_link_invalid"
            if (
                open_actions
                or any(attempt["terminal"] is None for attempt in attempts.values())
                or any(
                    command["transition"] == "command_accepted"
                    and command["finalized"] is None
                    for command in commands.values()
                )
            ):
                return "supervisor_decision_precondition_invalid"
            terminal_decision = True
            run_terminating = False
        elif transition == "run_decision" and writer_role == "operator_gateway":
            resolved_sequence = int(payload["action_resolved_sequence"])
            resolved_action = resolved_actions.get(resolved_sequence)
            if resolved_action is None or resolved_action[0] != payload["action_id"]:
                return "operator_decision_link_invalid"
            if not legacy and resolved_action[1] != payload.get("decision"):
                return "operator_decision_outcome_mismatch"
            if (
                open_actions
                or any(attempt["terminal"] is None for attempt in attempts.values())
                or not execution_complete_action_open
                or any(
                    command["transition"] == "command_accepted"
                    and command["finalized"] is None
                    for command in commands.values()
                )
            ):
                return "operator_decision_actions_open"
            terminal_decision = True
            run_terminating = False
        elif transition == "run_decision":
            status = payload.get("status") if legacy else payload.get("decision")
            open_attempts = [
                attempt
                for attempt in attempts.values()
                if attempt["terminal"] is None
            ]
            terminal_attempts = {
                str(attempt["terminal"])
                for attempt in attempts.values()
                if attempt["terminal"] is not None
            }
            latest_by_role: dict[str, Mapping[str, Any]] = {}
            for attempt in attempts.values():
                existing = latest_by_role.get(str(attempt["role"]))
                if existing is None or int(attempt["ordinal"]) > int(
                    existing["ordinal"]
                ):
                    latest_by_role[str(attempt["role"])] = attempt
            required_latest = {
                role: latest_by_role.get(role) for role in required_roles
            }
            if legacy and status == "terminating":
                if run_terminating or open_actions:
                    return "run_decision_precondition_invalid"
                if not any(
                    command["transition"] == "command_accepted"
                    and command["finalized"] is None
                    for command in commands.values()
                ):
                    return "run_decision_precondition_invalid"
                run_terminating = True
            elif legacy and status == "execution_complete_action_open":
                if not open_actions or open_attempts:
                    return "run_decision_precondition_invalid"
                execution_complete_action_open = True
                waiting_on_operator = True
            elif status == "awaiting_approval":
                if not open_actions or open_attempts:
                    return "run_decision_precondition_invalid"
                execution_complete_action_open = True
                waiting_on_operator = True
            elif status in {"workflow_closed", "completed"}:
                if open_actions or open_attempts:
                    return "run_decision_precondition_invalid"
                if not legacy and any(
                    attempt is None or attempt["terminal"] != "stage_completed"
                    for attempt in required_latest.values()
                ):
                    return "run_decision_required_lanes_incomplete"
            elif status in {"blocked", "failed", "workflow_failed"}:
                if legacy:
                    if not terminal_attempts.intersection(
                            {"stage_blocked", "stage_rejected", "stage_failed", "stage_interrupted"}
                    ):
                        return "run_decision_precondition_invalid"
                elif open_actions or open_attempts:
                    return "run_decision_precondition_invalid"
                elif status == "blocked":
                    latest_states = {
                        attempt["terminal"]
                        for attempt in required_latest.values()
                        if attempt is not None
                    }
                    if (
                        not latest_states.intersection({"stage_blocked", "stage_rejected"})
                        or latest_states.intersection(
                            {"stage_failed", "stage_interrupted"}
                        )
                    ):
                        return "run_decision_precondition_invalid"
                elif not any(
                    attempt is not None
                    and attempt["terminal"] in {"stage_failed", "stage_interrupted"}
                    for attempt in required_latest.values()
                ):
                    return "run_decision_precondition_invalid"
            terminal_decision = status not in {
                "awaiting_approval",
                "execution_complete_action_open",
                "terminating",
            }
            if terminal_decision and any(
                command["transition"] == "command_accepted"
                and command["finalized"] is None
                for command in commands.values()
            ):
                return "command_pending_at_terminal"
            if terminal_decision:
                run_terminating = False
        elif transition == "run_abandoned":
            open_attempt_ids = {
                candidate_id
                for candidate_id, attempt in attempts.items()
                if attempt["terminal"] is None
            }
            if waiting_on_operator or open_actions:
                return "run_abandoned_operator_action_open"
            if terminal_decision or set(payload["attempt_ids"]) != open_attempt_ids:
                return "run_abandoned_attempts_invalid"
            if int(payload["last_covered_sequence"]) != sequence_number - 1:
                return "run_abandoned_coverage_invalid"
            for open_attempt_id in open_attempt_ids:
                attempts[open_attempt_id]["terminal"] = "run_abandoned"
            terminal_decision = True
            run_terminating = False
            if any(
                command["transition"] == "command_accepted"
                and command["finalized"] is None
                for command in commands.values()
            ):
                return "command_pending_at_terminal"

    if saw_run_planned and not saw_catalog:
        return "lane_catalog_missing"
    if sealed and any(attempt["terminal"] is None for attempt in attempts.values()):
        return "attempt_terminal_missing"
    if sealed and run_terminating:
        return "run_terminating_incomplete"
    if sealed and any(
        command["transition"] == "command_accepted"
        and command["finalized"] is None
        for command in commands.values()
    ):
        return "command_pending_at_terminal"
    return None


__all__ = [
    "ATTEMPT_TRANSITIONS",
    "CONDITIONAL_LANES",
    "CORE_LANES",
    "LANE_ORDER",
    "TERMINAL_ATTEMPT_TRANSITIONS",
    "validate_receipt_payload",
    "validate_v2_receipt_contract",
]
