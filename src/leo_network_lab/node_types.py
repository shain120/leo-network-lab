"""Node vocabulary and centralized role/image mappings."""

from enum import Enum

from .config import ROUTER_IMAGE, SERVER_IMAGE, UE_IMAGE


class NodeType(str, Enum):
    """Supported logical topology node types."""

    UE = "ue"
    SATELLITE = "sat"
    GATEWAY = "gw"
    SERVER = "server"
    BEAM = "beam"


NODE_IMAGE_MAP: dict[NodeType, str | None] = {
    NodeType.UE: UE_IMAGE,
    NodeType.SATELLITE: ROUTER_IMAGE,
    NodeType.GATEWAY: ROUTER_IMAGE,
    NodeType.SERVER: SERVER_IMAGE,
    NodeType.BEAM: None,
}

NODE_ROLE_MAP: dict[NodeType, str] = {
    NodeType.UE: "user_terminal",
    NodeType.SATELLITE: "leo_satellite",
    NodeType.GATEWAY: "ground_gateway",
    NodeType.SERVER: "application_server",
    NodeType.BEAM: "satellite_beam",
}
