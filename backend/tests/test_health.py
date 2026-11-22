from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_ok():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["x-request-id"]


def test_health_request_id_is_echoed_and_capped():
    assert client.get("/health", headers={"x-request-id": "abc"}).headers["x-request-id"] == "abc"
    long = "x" * 500
    assert len(client.get("/health", headers={"x-request-id": long}).headers["x-request-id"]) == 64
