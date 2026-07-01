from fastapi.testclient import TestClient

from src import main as main_api


class FakeRedis:
    def __init__(self):
        self.queue = []
        self.hashes = {}

    def ping(self):
        return True

    def lpush(self, key, value):
        self.queue.insert(0, value)
        return len(self.queue)

    def hset(self, key, mapping):
        self.hashes[key] = {
            k.encode("utf-8"): str(v).encode("utf-8") for k, v in mapping.items()
        }
        return 1

    def expire(self, key, ttl_seconds):
        return True

    def llen(self, key):
        return len(self.queue)

    def hgetall(self, key):
        return self.hashes.get(key, {})


class FakeAsyncResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code


class FakeAsyncClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, params=None):
        return FakeAsyncResponse(status_code=200)


class HealthyWorkflow:
    def create_workflow(self):
        return True


def test_submit_job_queues_payload(monkeypatch):
    fake_redis = FakeRedis()
    monkeypatch.setattr(main_api, "get_redis_client", lambda: fake_redis)
    monkeypatch.setattr(main_api.settings, "NEWSAGENT_API_KEY", "")

    client = TestClient(main_api.api)
    response = client.post(
        "/submit-job",
        json={"source_url": "https://example.com/article", "max_retries": 2},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    assert body["queue_position"] == 1
    assert body["job_id"]
    assert len(fake_redis.queue) == 1


def test_get_job_status_returns_404_when_missing(monkeypatch):
    fake_redis = FakeRedis()
    monkeypatch.setattr(main_api, "get_redis_client", lambda: fake_redis)
    monkeypatch.setattr(main_api.settings, "NEWSAGENT_API_KEY", "")

    client = TestClient(main_api.api)
    response = client.get("/jobs/non-existent")

    assert response.status_code == 404
    assert "Job not found" in response.json()["detail"]


def test_health_check_reports_healthy_with_mocks(monkeypatch):
    fake_redis = FakeRedis()
    monkeypatch.setattr(main_api, "get_redis_client", lambda: fake_redis)
    monkeypatch.setattr(main_api, "MainWorkflow", HealthyWorkflow)
    monkeypatch.setattr(main_api.settings, "NEWSAGENT_API_KEY", "")

    monkeypatch.setattr(main_api.settings, "BROWSERLESS_URL", "http://browserless:3000")
    monkeypatch.setattr(main_api.settings, "BROWSERLESS_TOKEN", "token")

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    client = TestClient(main_api.api)
    response = client.get("/health", params={"check_external": "false"})

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["queue"] == "connected"
    assert data["browserless"] == "connected"
    assert data["scheduler"] == "skipped"