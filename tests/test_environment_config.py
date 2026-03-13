"""Tests for environment config storage and retrieval."""

from __future__ import annotations

import pytest


@pytest.fixture
async def storage(tmp_path):
    """Create a SQLiteStorage with a temporary database."""
    from orchestrator.sqlite_storage import SQLiteStorage

    db = SQLiteStorage(str(tmp_path / "test_env.db"))
    await db.open()
    yield db
    await db.close()


class TestEnvironmentConfigTable:
    async def test_table_created(self, storage) -> None:
        rows = await storage.execute_readonly(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='environment_config'"
        )
        assert len(rows) == 1


class TestSetAndGetEnvironment:
    async def test_set_and_get(self, storage) -> None:
        config = {
            "api_url": "https://dev.api.example.com",
            "frontend_url": "https://dev.example.com",
            "api_key": "test-key-123",
        }
        await storage.set_environment("dev", config, "supervisor")
        result = await storage.get_environment("dev")
        assert result is not None
        assert result["name"] == "dev"
        assert result["config"] == config
        assert result["updated_by"] == "supervisor"
        assert "updated_at" in result

    async def test_get_nonexistent_returns_none(self, storage) -> None:
        result = await storage.get_environment("nonexistent")
        assert result is None

    async def test_set_overwrites_existing(self, storage) -> None:
        config_v1 = {"api_url": "https://v1.api.example.com"}
        config_v2 = {"api_url": "https://v2.api.example.com"}
        await storage.set_environment("staging", config_v1, "human")
        await storage.set_environment("staging", config_v2, "supervisor")
        result = await storage.get_environment("staging")
        assert result is not None
        assert result["config"] == config_v2
        assert result["updated_by"] == "supervisor"


class TestListEnvironments:
    async def test_list_empty(self, storage) -> None:
        result = await storage.list_environments()
        assert result == []

    async def test_list_multiple(self, storage) -> None:
        await storage.set_environment("dev", {"url": "dev"}, "supervisor")
        await storage.set_environment("staging", {"url": "staging"}, "human")
        await storage.set_environment("prod", {"url": "prod"}, "supervisor")
        result = await storage.list_environments()
        assert len(result) == 3
        names = {r["name"] for r in result}
        assert names == {"dev", "staging", "prod"}
        # Each entry should have name, updated_at, updated_by
        for entry in result:
            assert "name" in entry
            assert "updated_at" in entry
            assert "updated_by" in entry


class TestDeleteEnvironment:
    async def test_delete_existing(self, storage) -> None:
        await storage.set_environment("dev", {"url": "dev"}, "supervisor")
        await storage.delete_environment("dev")
        result = await storage.get_environment("dev")
        assert result is None

    async def test_delete_nonexistent_no_error(self, storage) -> None:
        # Should not raise
        await storage.delete_environment("nonexistent")


class TestEnvironmentStoresJsonCorrectly:
    async def test_complex_json(self, storage) -> None:
        config = {
            "api_url": "https://dev.api.example.com",
            "frontend_url": "https://dev.example.com",
            "api_key": "test-key-123",
            "test_users": [
                {
                    "email": "test@test.com",
                    "password": "secret123",
                },
                {
                    "email": "admin@test.com",
                    "password": "admin456",
                },
            ],
            "nested": {"a": {"b": [1, 2, 3]}},
        }
        await storage.set_environment("dev", config, "supervisor")
        result = await storage.get_environment("dev")
        assert result is not None
        assert result["config"] == config
        assert len(result["config"]["test_users"]) == 2
        assert result["config"]["nested"]["a"]["b"] == [1, 2, 3]
