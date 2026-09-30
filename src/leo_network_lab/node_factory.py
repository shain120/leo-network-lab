"""Strict node-name detection and future topology-ready metadata."""

from dataclasses import dataclass
import re

from .node_types import NODE_IMAGE_MAP, NODE_ROLE_MAP, NodeType

_NAME_PATTERNS: tuple[tuple[NodeType, re.Pattern[str]], ...] = (
    (NodeType.UE, re.compile(r"^ue[1-9][0-9]*$")),
    (NodeType.SATELLITE, re.compile(r"^sat[1-9][0-9]*$")),
    (NodeType.GATEWAY, re.compile(r"^gw[1-9][0-9]*$")),
    (NodeType.SERVER, re.compile(r"^srv[1-9][0-9]*$")),
    (NodeType.BEAM, re.compile(r"^beam[1-9][0-9]*$")),
)


@dataclass(frozen=True)
class NodeSpec:
    """Name-derived metadata; ``image is None`` means a non-container node."""

    name: str
    node_type: NodeType
    role: str
    image: str | None


def detect_node_type(name: str) -> NodeType:
    """Return the type for a strictly valid phase-one node name."""
    for node_type, pattern in _NAME_PATTERNS:
        if pattern.fullmatch(name):
            return node_type
    raise ValueError(f"Unsupported node name: {name}")


def get_node_image(name: str) -> str | None:
    """Return the reusable role image, or ``None`` for a future beam switch."""
    return NODE_IMAGE_MAP[detect_node_type(name)]


def build_node_spec(name: str) -> NodeSpec:
    """Construct the complete topology-facing specification from a node name."""
    node_type = detect_node_type(name)
    return NodeSpec(name, node_type, NODE_ROLE_MAP[node_type], NODE_IMAGE_MAP[node_type])
