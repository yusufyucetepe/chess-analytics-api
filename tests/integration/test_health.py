from httpx import AsyncClient


async def test_healthz_is_dependency_free(client: AsyncClient) -> None:
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_readyz_reports_backing_services(client: AsyncClient) -> None:
    resp = await client.get("/readyz")
    body = resp.json()
    assert resp.status_code == 200, body
    assert body["status"] == "ready"
    assert body["checks"] == {"postgres": "ok", "redis": "ok"}
