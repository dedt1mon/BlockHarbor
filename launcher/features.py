"""Release capabilities shared by the local API and optional services."""

# Public build sharing is paused while its delivery is being repaired.
# This does not control ZIP exchange, Modrinth, or the shared skin service.
COMMUNITY_SHARING_ENABLED = False


def capabilities():
    return {"communitySharing": COMMUNITY_SHARING_ENABLED}


def require_community_sharing():
    if not COMMUNITY_SHARING_ENABLED:
        raise ValueError("Публикация сборок временно отключена.")
