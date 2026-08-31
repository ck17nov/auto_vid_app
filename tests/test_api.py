"""Backend API tests: auth, validation, path safety, rate limiting
(spec sections 28, 31, 36).
"""
from __future__ import annotations

import os

import pytest

# The API module reads config at import time, so the token must be set first.
TEST_TOKEN = "test-token-do-not-use-in-production-0123456789"
os.environ.setdefault("AUTOTUBE_API_TOKEN", TEST_TOKEN)
os.environ.setdefault("DRY_RUN", "true")

from fastapi.testclient import TestClient  # noqa: E402

from backend.api.main import app  # noqa: E402

AUTH = {"X-API-Key": os.environ["AUTOTUBE_API_TOKEN"]}


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


# ==========================================================================
class TestHealth:
    def test_health_is_unauthenticated(self, client):
        """A capability probe must work without holding the key."""
        response = client.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert "ffmpeg" in body
        assert "llm_providers" in body

    def test_health_leaks_no_secrets(self, client):
        text = client.get("/health").text.lower()
        for forbidden in ("api_key", "token", "secret", "gsk_", "aiza"):
            assert forbidden not in text or forbidden == "token" and \
                "auth_required" in text
        # auth_required is a boolean flag, never the value itself
        assert TEST_TOKEN not in client.get("/health").text


class TestAuthentication:
    def test_protected_endpoint_rejects_missing_key(self, client):
        assert client.get("/jobs").status_code == 401

    def test_protected_endpoint_rejects_wrong_key(self, client):
        response = client.get("/jobs", headers={"X-API-Key": "wrong"})
        assert response.status_code == 401

    def test_protected_endpoint_accepts_correct_key(self, client):
        assert client.get("/jobs", headers=AUTH).status_code == 200

    @pytest.mark.parametrize("path", [
        "/config", "/jobs", "/quota", "/youtube/status",
        "/niche/preview?niche=science",
    ])
    def test_every_read_endpoint_is_protected(self, client, path):
        assert client.get(path).status_code == 401, f"{path} is unauthenticated"

    def test_mutating_endpoints_are_protected(self, client):
        assert client.post("/automations", json={"niche": "science"}).status_code == 401
        assert client.post("/youtube/token",
                           json={"refresh_token": "x" * 20}).status_code == 401


class TestValidation:
    def test_niche_too_short_is_rejected(self, client):
        response = client.post("/automations", headers=AUTH, json={"niche": "a"})
        assert response.status_code == 422

    def test_duration_out_of_range_is_rejected(self, client):
        for duration in (0, 5, 99999):
            response = client.post("/automations", headers=AUTH,
                                   json={"niche": "science",
                                         "duration_seconds": duration})
            assert response.status_code == 422, duration

    def test_bad_upload_time_is_rejected(self, client):
        for value in ("25:00", "8pm", "20:99", "2000"):
            response = client.post("/automations", headers=AUTH,
                                   json={"niche": "science", "upload_time": value})
            assert response.status_code == 422, value

    def test_valid_upload_time_passes_validation(self, client):
        response = client.post("/automations", headers=AUTH,
                               json={"niche": "science", "upload_time": "20:00",
                                     "frequency": "daily"})
        # 202 accepted, or 503 if the backend lacks ffmpeg / API key.
        assert response.status_code in (202, 503)

    def test_unknown_timezone_is_rejected(self, client):
        response = client.post("/automations", headers=AUTH,
                               json={"niche": "science",
                                     "timezone": "Mars/Olympus_Mons"})
        assert response.status_code == 422

    def test_invalid_weekday_is_rejected(self, client):
        response = client.post("/automations", headers=AUTH,
                               json={"niche": "science", "days": [0, 9]})
        assert response.status_code == 422

    def test_invalid_enum_is_rejected(self, client):
        for payload in ({"video_format": "VERTICAL"}, {"mode": "TURBO"},
                        {"frequency": "hourly"}):
            response = client.post("/automations", headers=AUTH,
                                   json={"niche": "science", **payload})
            assert response.status_code == 422, payload

    def test_count_is_bounded(self, client):
        response = client.post("/automations", headers=AUTH,
                               json={"niche": "science", "count": 99})
        assert response.status_code == 422


class TestKidsConfirmation:
    def test_kids_niche_without_flag_returns_409(self, client):
        """Spec section 9: classification must be confirmed before publishing."""
        response = client.post("/automations", headers=AUTH,
                               json={"niche": "kids bedtime stories",
                                     "made_for_kids": False})
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert detail["error"] == "kids_confirmation_required"

    def test_kids_niche_with_flag_is_accepted(self, client):
        response = client.post("/automations", headers=AUTH,
                               json={"niche": "kids bedtime stories",
                                     "made_for_kids": True})
        assert response.status_code in (202, 503)

    def test_preview_flags_a_kids_niche(self, client):
        response = client.get("/niche/preview", headers=AUTH,
                              params={"niche": "kids bedtime stories"})
        assert response.status_code == 200
        body = response.json()
        assert body["kids_niche_detected"] is True
        assert body["requires_kids_confirmation"] is True
        assert body["profile"]["made_for_kids"] is True

    def test_preview_does_not_flag_a_general_niche(self, client):
        response = client.get("/niche/preview", headers=AUTH,
                              params={"niche": "science"})
        assert response.json()["kids_niche_detected"] is False


class TestNichePreview:
    def test_preview_returns_a_complete_profile(self, client):
        response = client.get("/niche/preview", headers=AUTH,
                              params={"niche": "space", "duration": 45})
        assert response.status_code == 200
        profile = response.json()["profile"]
        for key in ("tone", "visual_style", "pacing", "hook_style",
                    "scene_seconds", "words_per_second"):
            assert key in profile, key
        assert profile["requires_fact_check"] is True

    def test_preview_rejects_an_empty_niche(self, client):
        assert client.get("/niche/preview", headers=AUTH,
                          params={"niche": "a"}).status_code == 422


class TestJobEndpoints:
    def test_unknown_job_returns_404(self, client):
        assert client.get("/jobs/does-not-exist", headers=AUTH).status_code == 404

    def test_unknown_file_kind_is_rejected(self, client):
        response = client.get("/jobs/any/file/etc-passwd", headers=AUTH)
        # 404 for the unknown job is checked first; either is a refusal.
        assert response.status_code in (400, 404)

    def test_approving_unknown_job_returns_404(self, client):
        assert client.post("/jobs/nope/approve", headers=AUTH).status_code == 404

    def test_job_list_shape(self, client):
        body = client.get("/jobs", headers=AUTH).json()
        assert "jobs" in body and isinstance(body["jobs"], list)
        assert "queue_depth" in body


class TestQuotaEndpoint:
    def test_quota_reports_real_costs(self, client):
        body = client.get("/quota", headers=AUTH).json()
        assert body["costs"]["video_insert"] == 1600
        assert body["costs"]["search_list"] == 100
        assert body["limit"] == 10000
        assert body["max_uploads_per_day"] == 6
        assert "Pacific" in body["resets"]

    def test_reserve_is_subtracted_from_research_budget(self, client):
        body = client.get("/quota", headers=AUTH).json()
        assert body["available_for_research"] <= \
            body["limit"] - body["reserved_for_uploads"]


class TestConfigEndpoint:
    def test_config_exposes_no_secrets(self, client):
        body = client.get("/config", headers=AUTH).json()
        flat = str(body).lower()
        assert "aiza" not in flat
        assert "gsk_" not in flat
        assert TEST_TOKEN not in str(body)
        # But it must expose the operational switches the app needs.
        assert "quality" in body and "automation" in body
        assert body["dry_run"] is True

    def test_quota_costs_are_omitted_from_youtube_config(self, client):
        body = client.get("/config", headers=AUTH).json()
        assert "quota_costs" not in body["youtube"]
