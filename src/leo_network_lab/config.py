"""Central, stable phase-one project configuration."""

PROJECT_NAME = "leo-network-lab"
UE_IMAGE = f"{PROJECT_NAME}-ue:latest"
ROUTER_IMAGE = f"{PROJECT_NAME}-router:latest"
SERVER_IMAGE = f"{PROJECT_NAME}-server:latest"

ROLE_IMAGE_MAP = {
    "satellite": ROUTER_IMAGE,
    "ground_station": UE_IMAGE,
    "gateway": ROUTER_IMAGE,
    "host": SERVER_IMAGE,
}

ROLE_VISUAL_MAP = {
    "satellite": "satellite",
    "ground_station": "ue",
    "gateway": "gateway",
    "host": "server",
}

TEST_CONTAINER_PREFIX = "leo-test-"
TEST_CONTAINER_NAMES = (
    f"{TEST_CONTAINER_PREFIX}ue1",
    f"{TEST_CONTAINER_PREFIX}sat1",
    f"{TEST_CONTAINER_PREFIX}gw1",
    f"{TEST_CONTAINER_PREFIX}srv1",
)
