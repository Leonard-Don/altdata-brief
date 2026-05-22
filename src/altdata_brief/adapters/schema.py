"""Versioned schema dispatch for upstream public-summary JSON.

Why this module exists
----------------------

Every adapter reads a "public summary" JSON committed by an upstream
sibling project. Those summaries carry a ``schema_version`` field, but
historically the adapters ignored it: they reached straight into deep
nested paths (e.g. ``providers.macro_hf.metals.<metal>.weekly_change_pct``)
regardless of what version the upstream declared.

The failure mode that creates is *silent*: if an upstream bumps its
schema and renames a field, ``dict.get`` returns ``None``/``{}``, the
normalizer yields zeros, and the brief publishes all-zeros with no loud
error. Nobody notices until a human reads a meaningless brief.

This module makes the version an explicit, routed decision:

* :func:`resolve_schema_version` extracts ``schema_version`` from a raw
  payload and validates it against the per-source set of versions the
  adapter actually knows how to parse.
* An unknown / newer version raises :class:`UnsupportedSchemaVersionError`
  — a *loud, visible* condition. The adapter stops rather than guessing.
* A missing ``schema_version`` is handled per-source: most sources treat
  absence as an error, but ETF 512400's ``liveSnapshot.json`` is a JS-app
  artifact that never carried the field, so its adapter declares an
  ``implicit_version`` and absence maps to that — still an explicit,
  documented decision, not an accidental mis-parse. If a payload explicitly
  carries an unusable ``schema_version`` value, that still fails loud.

Each adapter keeps a ``_SCHEMA`` :class:`SchemaContract` describing which
versions it supports, and dispatches its ``_load_from_public_summary``
parsing on the resolved version. When a new upstream schema lands, the
adapter author adds the version to the contract and writes the matching
parse branch — the type system / tests force that to be a deliberate act.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from altdata_brief.adapters.base import AdapterError

__all__ = [
    "SchemaContract",
    "SchemaVersionError",
    "UnsupportedSchemaVersionError",
    "MissingSchemaVersionError",
    "resolve_schema_version",
]


class SchemaVersionError(AdapterError):
    """Base class for schema-version routing failures.

    Subclasses :class:`AdapterError` (not :class:`AdapterUnavailable`) on
    purpose: an unparseable / unknown schema is NOT a "source is offline,
    degrade gracefully" condition. It is a contract breach that a human
    must look at. The synthesis layer catches ``AdapterUnavailable`` and
    degrades; it does NOT catch plain ``AdapterError``, so a
    ``SchemaVersionError`` propagates and fails loud.
    """


class UnsupportedSchemaVersionError(SchemaVersionError):
    """Raised when the upstream declares a schema version the adapter
    does not know how to parse (typically: upstream bumped its schema
    ahead of this adapter)."""


class MissingSchemaVersionError(SchemaVersionError):
    """Raised when ``schema_version`` is absent and the source does not
    permit an implicit default."""


@dataclass(frozen=True, slots=True)
class SchemaContract:
    """Declares which public-summary schema versions an adapter supports.

    Attributes
    ----------
    source:
        Stable adapter source key (``"super_pricing"`` etc.) — used only
        for human-readable error messages.
    supported:
        The set of ``schema_version`` integer values this adapter has a
        parse branch for. Bump this (and add the branch) when an upstream
        ships a new schema.
    implicit_version:
        Version assumed when the payload carries no ``schema_version``
        key at all. ``None`` (the default) means absence is an error —
        used by every source whose upstream genuinely emits the field.
        ETF 512400 sets this because its ``liveSnapshot.json`` predates
        the convention and will never carry the key.
    """

    source: str
    supported: frozenset[int] = field(default_factory=frozenset)
    implicit_version: int | None = None

    def __post_init__(self) -> None:
        # An implicit version that isn't itself supported is a programming
        # error — fail at construction time, not at fetch time.
        if (
            self.implicit_version is not None
            and self.implicit_version not in self.supported
        ):
            raise ValueError(
                f"SchemaContract({self.source!r}): implicit_version "
                f"{self.implicit_version} not in supported={sorted(self.supported)}"
            )


def _coerce_version(raw: Any) -> int | None:
    """Coerce a raw ``schema_version`` value to ``int``.

    Accepts ints and integral-valued numeric strings/floats (``"1"``,
    ``1.0``). Returns ``None`` for anything that isn't an integer-like
    value so the caller can treat it as "missing/unusable".
    """
    if isinstance(raw, bool):  # bool is a subclass of int — reject it
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw) if raw.is_integer() else None
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            try:
                f = float(text)
            except ValueError:
                return None
            return int(f) if f.is_integer() else None
    return None


def resolve_schema_version(
    payload: dict[str, Any],
    contract: SchemaContract,
) -> int:
    """Extract and validate ``schema_version`` from a raw public summary.

    Parameters
    ----------
    payload:
        The raw, freshly-parsed public-summary dict.
    contract:
        The adapter's :class:`SchemaContract`.

    Returns
    -------
    int
        The resolved, *supported* schema version. The adapter dispatches
        its parsing on this value.

    Raises
    ------
    MissingSchemaVersionError
        ``schema_version`` is absent (or non-integer) and the contract
        declares no ``implicit_version``.
    UnsupportedSchemaVersionError
        The declared version is not in ``contract.supported`` — i.e. the
        upstream schema drifted ahead of (or behind) this adapter. This
        is the LOUD signal: rather than silently mis-parsing a shape the
        adapter was never written for, we stop and name the mismatch.
    """
    has_declared_version = "schema_version" in payload
    raw = payload.get("schema_version")
    version = _coerce_version(raw)

    if version is None:
        if not has_declared_version and contract.implicit_version is not None:
            # Documented absence — e.g. ETF liveSnapshot.json. This is an
            # explicit, intentional fallback, not an accidental mis-parse.
            return contract.implicit_version
        implicit_note = (
            "; implicit defaults apply only when the key is absent"
            if contract.implicit_version is not None
            else " and declares no implicit default"
        )
        raise MissingSchemaVersionError(
            f"{contract.source} public summary is missing or declares an unusable "
            f"'schema_version' (got {raw!r}); adapter supports "
            f"{sorted(contract.supported)}{implicit_note}. "
            "Refusing to guess the shape — upstream must emit a usable schema_version."
        )

    if version not in contract.supported:
        raise UnsupportedSchemaVersionError(
            f"{contract.source} public summary declares schema_version="
            f"{version}, but this adapter only supports "
            f"{sorted(contract.supported)}. The upstream schema has drifted; "
            "the adapter needs a parse branch for this version before the "
            "brief can trust it (failing loud instead of publishing zeros)."
        )

    return version
