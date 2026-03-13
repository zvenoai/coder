"""Tests for config module."""

import json

import pytest

from orchestrator.config import load_config, parse_repos_config


class TestParseReposConfig:
    def test_parses_repos(self) -> None:
        raw = json.dumps(
            [
                {
                    "url": "https://github.com/test/backend.git",
                    "path": "/workspace/backend",
                    "description": "Go backend",
                },
                {
                    "url": "https://github.com/test/frontend.git",
                    "path": "/workspace/frontend",
                    "description": "React frontend",
                },
            ]
        )

        config = parse_repos_config(raw)

        assert len(config.all_repos) == 2
        assert config.all_repos[0].url == "https://github.com/test/backend.git"
        assert config.all_repos[1].description == "React frontend"

    def test_empty_array(self) -> None:
        config = parse_repos_config("[]")
        assert config.all_repos == []

    def test_not_array_raises(self) -> None:
        with pytest.raises(ValueError, match="JSON array"):
            parse_repos_config('{"url": "x"}')

    def test_invalid_json_raises(self) -> None:
        with pytest.raises(json.JSONDecodeError):
            parse_repos_config("not json")


class TestLoadConfig:
    def test_loads_from_env(self, monkeypatch) -> None:
        monkeypatch.setenv("YANDEX_TRACKER_TOKEN", "test-token")
        monkeypatch.setenv("YANDEX_TRACKER_ORG_ID", "test-org")
        monkeypatch.setenv("TRACKER_QUEUE", "TEST")
        monkeypatch.setenv("POLL_INTERVAL_SECONDS", "30")

        config = load_config()

        assert config.tracker_token == "test-token"
        assert config.tracker_org_id == "test-org"
        assert config.tracker_queue == "TEST"
        assert config.poll_interval_seconds == 30

    def test_sdk_fields_from_env(self, monkeypatch) -> None:
        monkeypatch.setenv("YANDEX_TRACKER_TOKEN", "t")
        monkeypatch.setenv("YANDEX_TRACKER_ORG_ID", "o")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp-test")
        monkeypatch.setenv("AGENT_MODEL", "claude-opus-4-6")
        monkeypatch.setenv("MAX_CONCURRENT_AGENTS", "4")
        monkeypatch.setenv("WORKTREE_BASE_DIR", "/tmp/wt")

        config = load_config()

        assert config.anthropic_api_key == "sk-test"
        assert config.github_token == "ghp-test"
        assert config.agent_model == "claude-opus-4-6"
        assert config.max_concurrent_agents == 4
        assert config.worktree_base_dir == "/tmp/wt"

    def test_defaults(self, monkeypatch) -> None:
        monkeypatch.setenv("YANDEX_TRACKER_TOKEN", "t")
        monkeypatch.setenv("YANDEX_TRACKER_ORG_ID", "o")

        config = load_config()

        assert config.anthropic_api_key == ""
        assert config.agent_model == "claude-opus-4-6"
        assert config.max_concurrent_agents == 2
        assert config.agent_permission_mode == "acceptEdits"
        assert config.agent_max_budget_usd is None

    def test_missing_required_env_raises(self, monkeypatch) -> None:
        monkeypatch.delenv("YANDEX_TRACKER_TOKEN", raising=False)
        monkeypatch.delenv("YANDEX_TRACKER_ORG_ID", raising=False)

        with pytest.raises(KeyError):
            load_config()

    def test_repos_config_from_env(self, monkeypatch) -> None:
        monkeypatch.setenv("YANDEX_TRACKER_TOKEN", "t")
        monkeypatch.setenv("YANDEX_TRACKER_ORG_ID", "o")
        monkeypatch.setenv(
            "REPOS_CONFIG",
            json.dumps(
                [
                    {
                        "url": "https://github.com/test/api.git",
                        "path": "/workspace/api",
                        "description": "API",
                    }
                ]
            ),
        )

        config = load_config()

        assert len(config.repos_config.all_repos) == 1
        assert config.repos_config.all_repos[0].url == ("https://github.com/test/api.git")


class TestMergeConflictConfig:
    def test_default(self, monkeypatch) -> None:
        monkeypatch.setenv("YANDEX_TRACKER_TOKEN", "t")
        monkeypatch.setenv("YANDEX_TRACKER_ORG_ID", "o")

        config = load_config()

        assert config.merge_conflict_max_retries == 2

    def test_from_env(self, monkeypatch) -> None:
        monkeypatch.setenv("YANDEX_TRACKER_TOKEN", "t")
        monkeypatch.setenv("YANDEX_TRACKER_ORG_ID", "o")
        monkeypatch.setenv("MERGE_CONFLICT_MAX_RETRIES", "5")

        config = load_config()

        assert config.merge_conflict_max_retries == 5


class TestCompactionConfig:
    def test_defaults(self, monkeypatch) -> None:
        monkeypatch.setenv("YANDEX_TRACKER_TOKEN", "t")
        monkeypatch.setenv("YANDEX_TRACKER_ORG_ID", "o")

        config = load_config()

        assert config.compaction_enabled is True
        assert config.compaction_buffer_tokens == 20000
        assert config.compaction_model == "claude-haiku-4-5-20251001"

    def test_from_env(self, monkeypatch) -> None:
        monkeypatch.setenv("YANDEX_TRACKER_TOKEN", "t")
        monkeypatch.setenv("YANDEX_TRACKER_ORG_ID", "o")
        monkeypatch.setenv("COMPACTION_ENABLED", "false")
        monkeypatch.setenv("COMPACTION_BUFFER_TOKENS", "15000")
        monkeypatch.setenv("COMPACTION_MODEL", "claude-haiku-custom")

        config = load_config()

        assert config.compaction_enabled is False
        assert config.compaction_buffer_tokens == 15000
        assert config.compaction_model == "claude-haiku-custom"
