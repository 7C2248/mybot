from server.classes.api import Capabilities, ReadyStatus


async def readiness(database, *, configured: bool) -> ReadyStatus:
    try:
        connected = await database.ping()
    except Exception:
        connected = False
    return ReadyStatus(
        status="ready" if connected else "not_ready",
        database="connected" if connected else ("unavailable" if configured else "not_configured"),
        schema_version=database.schema_version if connected else None,
        capabilities=Capabilities(memories=connected),
    )
