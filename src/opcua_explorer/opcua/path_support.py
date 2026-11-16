"""Resolve human-readable browse paths to NodeIds with ``TranslateBrowsePathsToNodeIds``.

A sequence or a control target can name a node by path instead of a NodeId, so it survives a
server that renumbers identifiers. The service is the only way to do that generically, and this
module keeps the request building, batching and the "exactly one target" rule in one place.

Browse names are qualified, because a bare name is namespace 0 and would not match a device's
own namespace:

* ``Temperature``   — namespace 0
* ``2:Temperature`` — namespace index 2 (as reported by the session)

Nothing here discovers nodes by guessing: a path that resolves to no target or to more than one
target is reported as such and must be replaced by an explicit NodeId.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any

from asyncua import ua

from .ua_names import status_name

#: Paths per TranslateBrowsePathsToNodeIds request; servers advertise a much smaller limit than
#: the client can send, so batches stay deliberately small.
DEFAULT_PATH_CHUNK = 10

_FULL_MATCH = 0xFFFFFFFF

_NAME_PATTERN = re.compile(r"(?:(?P<index>\d+):)?(?P<name>.+)")


class PathResolutionError(RuntimeError):
    """The path cannot be turned into exactly one NodeId, or no session is available."""


@dataclass(slots=True)
class PathRequest:
    """A browse path relative to ``start_node_id``."""

    start_node_id: str
    names: list[str] = field(default_factory=list)


@dataclass(slots=True)
class PathResolution:
    """Outcome for one request: a NodeId, or the reason it could not be resolved."""

    start_node_id: str
    names: list[str]
    status: str = ""
    node_id: str = ""
    ambiguous: bool = False
    detail: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.node_id) and not self.ambiguous and self.status in ("", "Good")

    def describe(self) -> str:
        return "/".join(self.names) if self.names else "(empty path)"


def parse_browse_name(text: str) -> ua.QualifiedName:
    """Parse ``Name`` or ``index:Name`` into a ``QualifiedName``; raise ``ValueError`` when empty."""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("a browse name must not be empty")
    match = _NAME_PATTERN.fullmatch(raw)
    if not match:  # pragma: no cover - the pattern accepts everything non-empty
        raise ValueError(f"{text!r} is not a browse name")
    name = match.group("name")
    index = match.group("index")
    return ua.QualifiedName(name, int(index) if index is not None else 0)


def _session_of(target: Any) -> Any | None:
    """Accept a ``UaSession``/``UaClient`` or the high-level ``Client``."""
    if hasattr(target, "translate_browsepaths_to_nodeids"):
        return target
    uaclient = getattr(target, "uaclient", None)
    if hasattr(uaclient, "translate_browsepaths_to_nodeids"):
        return uaclient
    return getattr(uaclient, "session", None)


def _element(name: str) -> ua.RelativePathElement:
    return ua.RelativePathElement(
        ReferenceTypeId=ua.NodeId(ua.ObjectIds.HierarchicalReferences),
        IsInverse=False,
        IncludeSubtypes=True,
        TargetName=parse_browse_name(name),
    )


def _target_node_id(target: Any) -> tuple[str, str]:
    expanded = getattr(target, "TargetId", None)
    if expanded is None:
        return "", "the server returned no target NodeId"
    uri = getattr(expanded, "NamespaceUri", None)
    if uri:
        return "", f"the target uses the namespace URI {uri!r}; use the numeric namespace index form"
    plain = ua.NodeId(expanded.Identifier, expanded.NamespaceIndex, expanded.NodeIdType)
    return plain.to_string(), ""


def _apply(resolution: PathResolution, result: Any) -> None:
    resolution.status = status_name(result.StatusCode)
    targets = [target for target in (result.Targets or []) if target is not None]
    complete = [target for target in targets if int(getattr(target, "RemainingPathIndex", 0)) >= _FULL_MATCH]
    if not complete:
        if targets:
            resolution.detail = (
                f"{resolution.describe()} matched only part of the path "
                f"({len(targets)} partial target(s)); use an explicit NodeId instead."
            )
        else:
            resolution.detail = f"{resolution.describe()} was not resolved ({resolution.status})."
        return
    if len(complete) > 1:
        resolution.ambiguous = True
        resolution.detail = (
            f"{resolution.describe()} matched {len(complete)} nodes; use an explicit NodeId instead."
        )
        return
    node_id, problem = _target_node_id(complete[0])
    if not node_id:
        resolution.detail = problem or f"{resolution.describe()} could not be converted to a NodeId."
        return
    resolution.node_id = node_id


async def resolve_paths(
    target: Any,
    requests: list[PathRequest],
    *,
    chunk_size: int = DEFAULT_PATH_CHUNK,
) -> list[PathResolution]:
    """Resolve several paths in bounded batches; every request yields one resolution."""
    session = _session_of(target)
    if session is None:
        raise PathResolutionError("Browse-path resolution needs a connected session.")
    resolutions = [PathResolution(start_node_id=request.start_node_id, names=list(request.names)) for request in requests]

    pending: list[tuple[int, ua.BrowsePath]] = []
    for index, request in enumerate(requests):
        names = [(name or "").strip() for name in request.names if (name or "").strip()]
        resolutions[index].names = names
        if not names:
            resolutions[index].status = "BadBrowseNameInvalid"
            resolutions[index].detail = "The path names no browse name."
            continue
        try:
            start = ua.NodeId.from_string(request.start_node_id)
        except Exception:
            resolutions[index].status = "BadNodeIdInvalid"
            resolutions[index].detail = f"{request.start_node_id!r} is not a NodeId."
            continue
        try:
            elements = [_element(name) for name in names]
        except ValueError as exc:
            resolutions[index].status = "BadBrowseNameInvalid"
            resolutions[index].detail = str(exc)
            continue
        pending.append(
            (
                index,
                ua.BrowsePath(
                    StartingNode=start,
                    RelativePath=ua.RelativePath(Elements=elements),
                ),
            )
        )

    size = max(1, int(chunk_size))
    for offset in range(0, len(pending), size):
        chunk = pending[offset : offset + size]
        try:
            results = await session.translate_browsepaths_to_nodeids([path for _, path in chunk])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            for index, _path in chunk:
                resolutions[index].status = "BadCommunicationError"
                resolutions[index].detail = (
                    f"TranslateBrowsePathsToNodeIds failed: {type(exc).__name__}: {exc}"
                )
            continue
        for (index, _path), result in zip(chunk, results, strict=False):
            _apply(resolutions[index], result)
        for index, _path in chunk[len(results) :]:
            resolutions[index].status = "BadNoResult"
            resolutions[index].detail = "The server returned no result for this path."
    return resolutions


async def resolve_path(
    target: Any,
    start_node_id: str,
    names: list[str],
    *,
    chunk_size: int = DEFAULT_PATH_CHUNK,
) -> PathResolution:
    """Resolve one path; returns the resolution (``resolution.ok`` decides)."""
    resolutions = await resolve_paths(
        target, [PathRequest(start_node_id=start_node_id, names=names)], chunk_size=chunk_size
    )
    return resolutions[0]


__all__ = [
    "DEFAULT_PATH_CHUNK",
    "PathRequest",
    "PathResolution",
    "PathResolutionError",
    "parse_browse_name",
    "resolve_path",
    "resolve_paths",
]
