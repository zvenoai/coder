"""Test migration version collision detection."""

from __future__ import annotations


class TestMigrationVersionCollision:
    async def test_no_duplicate_migration_versions(self) -> None:
        """All migration files must have unique version numbers."""
        from orchestrator.sqlite_storage import SQLiteStorage

        migrations = SQLiteStorage._discover_migrations()

        # Group migrations by version number
        versions: dict[int, list[str]] = {}
        for version, path in migrations:
            if version not in versions:
                versions[version] = []
            versions[version].append(path.name)

        # Check for duplicates
        duplicates = {v: files for v, files in versions.items() if len(files) > 1}

        assert not duplicates, (
            f"Found duplicate migration versions: {duplicates}. "
            "Each migration must have a unique version number. "
            "Rename one of the conflicting migrations to the next available version."
        )

    async def test_user_version_matches_latest_migration(self, tmp_path) -> None:
        """PRAGMA user_version should equal the highest migration number after all migrations run."""
        from orchestrator.sqlite_storage import SQLiteStorage

        db = SQLiteStorage(str(tmp_path / "version_check.db"))
        await db.open()

        # Get highest migration version
        migrations = SQLiteStorage._discover_migrations()
        if migrations:
            max_version = max(v for v, _ in migrations)

            # Check PRAGMA user_version
            conn = db._require_db()
            cursor = await conn.execute("PRAGMA user_version")
            row = await cursor.fetchone()
            assert row is not None
            actual_version = row["user_version"]

            assert actual_version == max_version, (
                f"PRAGMA user_version is {actual_version}, but highest migration is {max_version}. "
                "This indicates that not all migrations ran successfully."
            )

        await db.close()

    async def test_all_expected_columns_exist(self, tmp_path) -> None:
        """All columns referenced in code must exist after migrations run."""
        from orchestrator.sqlite_storage import SQLiteStorage

        db = SQLiteStorage(str(tmp_path / "columns_check.db"))
        await db.open()

        conn = db._require_db()

        # Check recovery_states table has all expected columns
        cursor = await conn.execute("PRAGMA table_info(recovery_states)")
        rows = await cursor.fetchall()
        columns = {row["name"] for row in rows}

        expected_columns = {
            "issue_key",
            "attempt_count",
            "no_pr_count",
            "provider_rate_limited",
            "already_implemented",  # from 0007_recovery_already_implemented.py
            "last_output",
            "updated_at",
            "no_pr_cost",  # from 0007_recovery_cost.py
        }

        missing = expected_columns - columns
        assert not missing, (
            f"Missing columns in recovery_states table: {missing}. "
            "This indicates that some migrations did not run successfully."
        )

        await db.close()
