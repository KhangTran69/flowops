"""Safe event-payload mapping for trigger deliveries (v1.4).

Maps an inbound event payload to workflow request fields using explicit dot
paths. No eval/exec/compile — just dictionary lookups. Unknown paths fail
closed with a clear error.

Supported mapping value forms:
- Fixed literal: ``"employee_name": "Jane Doe"``
- Event path:    ``"employee_name": "event.user.name"``
- Trigger field: ``"requester_id": "trigger.source_key"``

Example mapping_json::

    {
        "employee_name": "event.user.name",
        "employee_email": "event.user.email",
        "department": "Engineering",
        "requester_id": 42
    }
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Optional

from src.expressions import MISSING


class MappingError(ValueError):
    """Raised when a mapping cannot resolve a field."""


def _resolve_path(source: Mapping[str, Any], path: str) -> Any:
    """Walk a dot-separated path through a dict, returning MISSING if absent."""
    current: Any = source
    for segment in path.split("."):
        if not isinstance(current, Mapping):
            return MISSING
        current = current.get(segment, MISSING)
        if current is MISSING:
            return MISSING
    return current


def apply_mapping(
    mapping_json: str,
    event: Mapping[str, Any],
    trigger: Mapping[str, Any],
) -> dict[str, Any]:
    """Resolve a mapping spec against an event payload and trigger record.

    Raises :class:`MappingError` when a referenced path is missing.
    """
    try:
        spec = json.loads(mapping_json) if mapping_json else {}
    except (TypeError, ValueError) as error:
        raise MappingError(f"mapping_json is not valid JSON: {error}") from error

    if not isinstance(spec, dict):
        raise MappingError("mapping_json must be a JSON object.")

    resolved: dict[str, Any] = {}
    for field, value in spec.items():
        if isinstance(value, str) and value.startswith("event."):
            result = _resolve_path(event, value[len("event."):])
            if result is MISSING:
                raise MappingError(f"Event path {value!r} not found in payload.")
            resolved[field] = result
        elif isinstance(value, str) and value.startswith("trigger."):
            result = _resolve_path(trigger, value[len("trigger."):])
            if result is MISSING:
                raise MappingError(f"Trigger path {value!r} not found.")
            resolved[field] = result
        else:
            resolved[field] = value
    return resolved


def validate_mapping(mapping_json: str, target_type: str) -> None:
    """Check that a mapping spec is well-formed (paths are syntactically valid).

    Does not verify that paths exist at runtime — that happens in
    :func:`apply_mapping`. Raises :class:`MappingError` on structural problems.
    """
    try:
        spec = json.loads(mapping_json) if mapping_json else {}
    except (TypeError, ValueError) as error:
        raise MappingError(f"mapping_json is not valid JSON: {error}") from error

    if not isinstance(spec, dict):
        raise MappingError("mapping_json must be a JSON object.")

    required_fields = {
        "onboarding_request": {"employee_name", "employee_email", "department", "requester_id"},
        "offboarding_request": {"employee_name", "employee_email", "department", "requester_id"},
        "ticket": {"userId", "id", "title", "completed"},
    }.get(target_type, set())

    missing = required_fields - set(spec.keys()) if required_fields else set()
    if missing:
        raise MappingError(
            f"Mapping for target_type={target_type!r} is missing required fields: "
            + ", ".join(sorted(missing))
        )

    for field, value in spec.items():
        if isinstance(value, str) and value.startswith(("event.", "trigger.")):
            path = value.split(".", 1)[1]
            if not path or not all(
                segment.replace("_", "").isalnum()
                for segment in path.split(".")
            ):
                raise MappingError(f"Invalid path in mapping for {field!r}: {value!r}")
