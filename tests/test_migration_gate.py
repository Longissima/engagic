import pytest

from database import migrate as migration_module


class FakeConnection:
    def __init__(self):
        self.fetch_calls = []

    async def fetch(self, query, *args):
        self.fetch_calls.append((query, args))
        return []

    async def close(self):
        pass


class AppliedConnection:
    def __init__(self, versions):
        self.versions = versions

    async def fetch(self, query, *args):
        del query, args
        return [{"version": version} for version in self.versions]


@pytest.mark.asyncio
async def test_migrate_raises_when_a_migration_fails(monkeypatch, tmp_path):
    connection = FakeConnection()
    migration = tmp_path / "999_breaks.sql"
    migration.write_text("SELECT 1")

    async def fake_connection():
        return connection

    async def fake_ensure(_connection):
        pass

    async def fake_applied(_connection):
        return set()

    async def fake_apply(_connection, _version, _name, _path):
        return False

    monkeypatch.setattr(migration_module, "get_connection", fake_connection)
    monkeypatch.setattr(migration_module, "ensure_migrations_table", fake_ensure)
    monkeypatch.setattr(migration_module, "get_applied_migrations", fake_applied)
    monkeypatch.setattr(
        migration_module,
        "get_pending_migrations",
        lambda _applied: [("999", "breaks", migration)],
    )
    monkeypatch.setattr(migration_module, "apply_migration", fake_apply)

    with pytest.raises(migration_module.MigrationFailedError):
        await migration_module.migrate()


@pytest.mark.asyncio
async def test_runtime_gate_reports_every_pending_migration(monkeypatch, tmp_path):
    (tmp_path / "001_first.sql").write_text("SELECT 1;")
    (tmp_path / "002_second.sql").write_text("SELECT 2;")
    monkeypatch.setattr(migration_module, "MIGRATIONS_DIR", tmp_path)

    with pytest.raises(migration_module.PendingMigrationsError) as error:
        await migration_module.assert_schema_current(AppliedConnection({"001"}))

    assert "002_second" in str(error.value)


@pytest.mark.asyncio
async def test_runtime_gate_accepts_current_schema(monkeypatch, tmp_path):
    (tmp_path / "001_first.sql").write_text("SELECT 1;")
    monkeypatch.setattr(migration_module, "MIGRATIONS_DIR", tmp_path)

    await migration_module.assert_schema_current(AppliedConnection({"001"}))


def test_migration_help_exits_before_any_database_action(monkeypatch, capsys):
    called = False

    async def forbidden_migrate():
        nonlocal called
        called = True

    monkeypatch.setattr(migration_module, "migrate", forbidden_migrate)
    monkeypatch.setattr("sys.argv", ["database.migrate", "--help"])

    with pytest.raises(SystemExit) as exit_info:
        migration_module.main()

    assert exit_info.value.code == 0
    assert "--rollback" in capsys.readouterr().out
    assert called is False


@pytest.mark.asyncio
async def test_migration_status_is_read_only(monkeypatch, tmp_path, capsys):
    connection = FakeConnection()
    ensure_called = False

    async def fake_connection():
        return connection

    async def forbidden_ensure(_connection):
        nonlocal ensure_called
        ensure_called = True

    monkeypatch.setattr(migration_module, "get_connection", fake_connection)
    monkeypatch.setattr(migration_module, "ensure_migrations_table", forbidden_ensure)
    monkeypatch.setattr(migration_module, "MIGRATIONS_DIR", tmp_path)

    await migration_module.status()

    assert ensure_called is False
    assert all("CREATE" not in query.upper() for query, _ in connection.fetch_calls)
    assert "Pending Migrations" in capsys.readouterr().out
