"""Disposable ownership and collision regressions for batch-two client cleanup."""

import json
import stat
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace

import pytest
import tomlkit

from dots_brain import clients, removal
from dots_brain.auth import authenticate, issue_client, read_connection
from dots_brain.clients import bridge_entry, configure, file_hash, integration_key, registrations
from dots_brain.errors import InputError
from dots_brain.local import atomic_write, write_json
from dots_brain.removal import disconnect_client
from dots_brain.store import Store


@pytest.fixture
def install(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

    def create(
        provider="cursor",
        *,
        existing=True,
        prior_backup=None,
        local=True,
        legacy=False,
        register=True,
        publish_hook=None,
        entry_extra=None,
        private_url=None,
    ):
        store = Store(tmp_path / "memory")
        issuer = store if local else Store(tmp_path / "issuer")
        issuer.initialize()
        store.directory.mkdir(exist_ok=True)
        config = tmp_path / "profile" / ("config.toml" if provider == "codex" else "config.json")
        directories = [str(parent) for parent in config.parents if not parent.exists()]
        if existing:
            config.parent.mkdir(parents=True, exist_ok=True)
            config.write_text(
                '# original comment\nmodel = "Žluťoučký"\n'
                if provider == "codex"
                else '{"theme":"Žluťoučký"}\n',
                encoding="utf-8",
            )
            config.chmod(0o640)
            directories = []
        original_info = config.stat() if config.exists() else None
        key = integration_key(provider, config)
        private = store.directory / "connections"
        if local:
            private.mkdir()
            directories.append(str(private))
            connection = private / (f"{provider}.json" if legacy else f"{provider}-{key}.json")
        else:
            connection = issuer.directory / "supplied.json"
        issued = issue_client(
            issuer,
            name=provider,
            scopes=["memory:read", "memory:write"] if private_url else ["memory:read"],
            projects=None,
            days=1,
            output=connection,
            url=private_url or "https://synthetic.invalid/mcp",
        )
        token = read_connection(connection)["token"]
        entry = bridge_entry(
            connection, store.directory if local and not private_url else None, provider
        )
        entry.update(entry_extra or {})
        ownership = {"directories": directories}
        if local:
            ownership["credential_hash"] = file_hash(connection)
        record = {
            "provider": provider,
            "config_file": str(config),
            "entry": entry,
            "connection_file": str(connection),
            "client_id": issued["client_id"],
            "local": local,
            "ownership": ownership,
        }
        backup = config.with_name(config.name + ".before-dots-brain")
        if prior_backup is not None:
            backup.write_text(prior_backup, encoding="utf-8")

        def save():
            write_json(
                store.directory / "integrations.json", {"version": 1, "items": {key: record}}
            )

        def publish():
            if register:
                save()
            if publish_hook is not None:
                publish_hook(config)

        configure(config, provider=provider, entry=entry, ownership=ownership, publish=publish)
        return SimpleNamespace(
            store=store,
            issuer=issuer,
            config=config,
            connection=connection,
            backup=backup,
            token=token,
            entry=entry,
            record=record,
            key=key,
            provider=provider,
            save=save,
            original_info=original_info,
        )

    return create


def document(path, provider):
    text = path.read_text(encoding="utf-8")
    return tomlkit.parse(text) if provider == "codex" else json.loads(text)


@pytest.mark.parametrize(
    "provider,comment_style",
    [
        ("cursor", None),
        ("codex", "eof"),
        ("codex", "header"),
        ("codex", "inline"),
        ("codex", "array"),
        ("codex", "hash-string"),
    ],
)
def test_surrounding_edits_preserve_settings_and_backup_but_remove_own_access(
    install, provider, comment_style
):
    extra = (
        {"env": {"LABEL": "value # is not a comment"}} if comment_style == "hash-string" else None
    )
    item = install(provider, entry_extra=extra)
    backup = item.backup.read_bytes()
    marker = "# new unrelated comment: příliš"
    if provider == "codex":
        text = item.config.read_text()
        if comment_style == "header":
            header = "[mcp_servers.dots-brain]"
            text = text.replace(header, f"{header} {marker}")
        elif comment_style == "inline":
            line = next(line for line in text.splitlines() if line.startswith("command ="))
            text = text.replace(line, f"{line} {marker}")
        elif comment_style == "array":
            line = next(line for line in text.splitlines() if line.startswith("args ="))
            elements = [f"{json.dumps(arg)}," for arg in item.entry["args"]]
            elements[0] += f" {marker}"
            text = text.replace(line, "args = [\n" + "\n".join(elements) + "\n]")
        else:
            text += f"\n{marker}\n"
        item.config.write_text(text)
    else:
        current = document(item.config, provider)
        current["theme"] = "Nové nastavení"
        current["mcpServers"]["other"] = {"command": "keep"}
        item.config.write_text(json.dumps(current, ensure_ascii=False))
    result = disconnect_client(item.store, provider=provider, config=item.config)
    assert result["state"] == "disconnected" and result["access"] == "revoked"
    current = document(item.config, provider)
    section = "mcp_servers" if provider == "codex" else "mcpServers"
    assert "dots-brain" not in current.get(section, {})
    if provider == "codex":
        assert marker in item.config.read_text()
        assert "# is not a comment" not in item.config.read_text()
        assert current["model"] == "Žluťoučký"
    else:
        assert current["theme"] == "Nové nastavení"
        assert current[section]["other"] == {"command": "keep"}
    assert item.backup.read_bytes() == backup
    assert result["client"]["backup"]["path"] == str(item.backup)
    assert not item.connection.exists() and authenticate(item.store, item.token) is None
    assert item.key not in registrations(item.store.directory)["items"]


@pytest.mark.parametrize("provider", ["cursor", "codex"])
def test_modified_own_entry_preserves_config_backup_and_token(install, provider):
    item = install(provider)
    current = document(item.config, provider)
    section = "mcp_servers" if provider == "codex" else "mcpServers"
    current[section]["dots-brain"]["env"] = {"USER_SETTING": "keep"}
    item.config.write_text(tomlkit.dumps(current) if provider == "codex" else json.dumps(current))
    before = [path.read_bytes() for path in (item.config, item.backup, item.connection)]
    result = disconnect_client(item.store, provider=provider, config=item.config)
    assert result["state"] == "partial" and result["access"] == "preserved"
    assert result["client"]["state"] == "preserved_modified"
    assert [path.read_bytes() for path in (item.config, item.backup, item.connection)] == before
    assert authenticate(item.store, item.token) is not None
    assert item.key in registrations(item.store.directory)["items"]


@pytest.mark.parametrize("backup_kind", ["owned", "preexisting", "changed"])
def test_backup_cleanup_requires_created_and_unchanged_evidence(install, backup_kind):
    item = install(
        prior_backup="Preexisting recovery copy" if backup_kind == "preexisting" else None
    )
    if backup_kind == "changed":
        item.backup.write_text("Edited recovery copy: příliš")
    before = item.backup.read_bytes()
    result = disconnect_client(item.store, provider=item.provider, config=item.config)
    assert result["access"] == "revoked"
    assert item.config.exists() and document(item.config, item.provider)["theme"] == "Žluťoučký"
    if backup_kind == "owned":
        assert result["client"]["backup"]["state"] == "removed"
        assert not item.backup.exists()
    else:
        assert result["client"]["backup"]["state"] == "preserved_modified_or_unowned"
        assert item.backup.read_bytes() == before


@pytest.mark.parametrize("credential_kind", ["owned", "unowned", "changed"])
def test_credential_cleanup_distinguishes_ownership_and_later_edits(
    install, tmp_path, credential_kind
):
    item = install()
    if credential_kind == "unowned":
        del item.record["ownership"]["credential_hash"]
        item.save()
    elif credential_kind == "changed":
        replacement = tmp_path / "different-client.json"
        issue_client(
            item.store,
            name="different-client",
            scopes=["memory:read"],
            projects=None,
            days=1,
            output=replacement,
            url="https://synthetic.invalid/mcp",
        )
        replacement_token = read_connection(replacement)["token"]
        item.connection.write_bytes(replacement.read_bytes())
        replacement.unlink()
    before = item.connection.read_bytes()
    result = disconnect_client(item.store, provider=item.provider, config=item.config)
    if credential_kind == "owned":
        assert result["state"] == "disconnected" and not item.connection.exists()
    else:
        assert result["state"] == "partial" and item.connection.read_bytes() == before
        assert item.key in registrations(item.store.directory)["items"]
    if credential_kind == "unowned":
        assert result["access"] == "shared_or_unowned_credential_retained"
        assert authenticate(item.store, item.token) is not None
    else:
        assert result["access"] == "revoked" and authenticate(item.store, item.token) is None
    if credential_kind == "changed":
        assert authenticate(item.store, replacement_token) is not None


@pytest.mark.parametrize("registered", [False, True])
def test_unknown_legacy_registration_is_never_adopted_for_cleanup(install, registered):
    item = install(legacy=True, register=registered)
    if registered:
        item.record.pop("ownership")
        item.save()
    before = item.config.read_bytes(), item.connection.read_bytes()
    result = disconnect_client(item.store, provider=item.provider, config=item.config)
    assert result["state"] == "partial"
    assert (item.config.read_bytes(), item.connection.read_bytes()) == before
    assert authenticate(item.store, item.token) is not None
    if registered:
        assert result["client"]["state"] == "preserved_unowned"
    else:
        assert result["issues"][0]["state"] == "unsupported_legacy_client"
        assert "manually remove only" in result["issues"][0]["action"]


@pytest.mark.parametrize("operation", ["configure", "disconnect"])
def test_atomic_publication_collision_preserves_external_writer(install, monkeypatch, operation):
    concurrent = '{"theme":"Concurrent external edit: příliš"}\n'
    if operation == "configure":
        observed = []

        def publish_hook(path):
            path.write_text(concurrent, encoding="utf-8")
            observed.append(path)

        with pytest.raises(InputError, match="concurrently"):
            install(publish_hook=publish_hook)
        assert observed[0].read_text(encoding="utf-8") == concurrent
    else:
        item = install()
        registry_before = (item.store.directory / "integrations.json").read_bytes()
        credential_before = item.connection.read_bytes()

        def racing_write(path, content, **kwargs):
            if path == item.config:
                path.write_text(concurrent, encoding="utf-8")
            return atomic_write(path, content, **kwargs)

        monkeypatch.setattr(removal, "atomic_write", racing_write)
        result = disconnect_client(item.store, provider=item.provider, config=item.config)
        assert result["state"] == "partial" and result["access"] == "preserved"
        assert result["client"]["state"] == "preserved_concurrent_change"
        assert item.config.read_text(encoding="utf-8") == concurrent
        assert item.connection.read_bytes() == credential_before
        assert authenticate(item.store, item.token) is not None
        assert (item.store.directory / "integrations.json").read_bytes() == registry_before


@pytest.mark.parametrize("provider", ["cursor", "codex"])
def test_configure_and_disconnect_preserve_utf8_mode_uid_and_gid(install, provider):
    item = install(provider)
    connected = item.config.stat()
    assert (stat.S_IMODE(connected.st_mode), connected.st_uid, connected.st_gid) == (
        stat.S_IMODE(item.original_info.st_mode),
        item.original_info.st_uid,
        item.original_info.st_gid,
    )
    assert "Žluťoučký" in item.config.read_text(encoding="utf-8")
    result = disconnect_client(item.store, provider=provider, config=item.config)
    assert result["state"] == "disconnected"
    after = item.config.stat()
    assert (stat.S_IMODE(after.st_mode), after.st_uid, after.st_gid) == (
        stat.S_IMODE(connected.st_mode),
        connected.st_uid,
        connected.st_gid,
    )
    assert "Žluťoučký" in item.config.read_text(encoding="utf-8")


@pytest.mark.parametrize("extra", ["none", "directory", "configuration"])
def test_created_config_and_directories_are_removed_only_when_empty(install, extra):
    item = install(existing=False)
    private = item.connection.parent
    keep = private / "unrelated.txt"
    if extra == "directory":
        keep.write_text("Unrelated file")
    elif extra == "configuration":
        current = document(item.config, item.provider)
        current["theme"] = "Keep this newly added setting"
        item.config.write_text(json.dumps(current))
    result = disconnect_client(item.store, provider=item.provider, config=item.config)
    assert result["state"] == "disconnected"
    if extra == "configuration":
        assert result["client"]["file"] == "retained"
        assert document(item.config, item.provider) == {
            "mcpServers": {},
            "theme": "Keep this newly added setting",
        }
    else:
        assert result["client"]["file"] == "removed_empty_owned_file"
        assert not item.config.exists()
    if extra == "directory":
        assert keep.read_text() == "Unrelated file"
        assert any(
            row["state"] == "preserved_nonempty_or_unavailable" for row in result["directories"]
        )
    else:
        assert not private.exists()
    # The persistent configuration lock is an explicit reason to retain its directory.
    assert item.config.parent.exists()


def test_remote_supplied_credential_requires_issuer_revocation_without_local_database(install):
    item = install(local=False, existing=False)
    before = item.connection.read_bytes()
    assert not item.store.path.exists()
    result = disconnect_client(item.store, provider=item.provider, config=item.config)
    assert result["state"] == "partial" and result["access"] == "issuer_revocation_required"
    assert result["client"]["state"] == "removed" and not item.config.exists()
    assert item.connection.read_bytes() == before
    assert authenticate(item.issuer, item.token) is not None
    assert not item.store.path.exists()
    assert item.key in registrations(item.store.directory)["items"]


@pytest.mark.parametrize("foreign_backup", ["different", "equal-original"])
def test_backup_created_by_publisher_survives_collision_and_rollback(tmp_path, foreign_backup):
    config = tmp_path / "config.json"
    original = b'{"theme":"original"}\n'
    config.write_bytes(original)
    backup = config.with_name(config.name + ".before-dots-brain")
    foreign = original if foreign_backup == "equal-original" else b"Foreign recovery data\n"

    def publish():
        backup.write_bytes(foreign)

    with pytest.raises(InputError, match="concurrent|collision"), ExitStack() as rollback:
        configure(
            config,
            provider="cursor",
            entry={"command": "synthetic-bridge"},
            ownership={},
            rollback=rollback,
            publish=publish,
        )
    assert config.read_bytes() == original
    assert backup.read_bytes() == foreign


@pytest.mark.parametrize("preexisting", [False, True])
def test_failed_connect_preserves_replacement_credential_and_its_identity(
    install, tmp_path, monkeypatch, preexisting
):
    if preexisting:
        item = install(private_url="http://127.0.0.1:9988/mcp")
        store, config, connection = item.store, item.config, item.connection
    else:
        store = Store(tmp_path / "memory")
        store.initialize()
        config = tmp_path / "profile.json"
        config.write_text('{"theme":"original"}\n')
        key = integration_key("cursor", config)
        connection = store.directory / "connections" / f"cursor-{key}.json"
    config_before = config.read_bytes()
    registry = store.directory / "integrations.json"
    registry_before = registry.read_bytes() if registry.exists() else None
    observed = {}

    async def failed_probe(entry, **_checks):
        path = Path(entry["args"][4])
        assert path == connection
        observed["own_token"] = read_connection(path)["token"]
        replacement = tmp_path / "foreign-credential.json"
        issue_client(
            store,
            name="foreign-client",
            scopes=["memory:read"],
            projects=None,
            days=1,
            output=replacement,
            url="https://synthetic.invalid/mcp",
        )
        observed["foreign_token"] = read_connection(replacement)["token"]
        observed["foreign_bytes"] = replacement.read_bytes()
        path.write_bytes(observed["foreign_bytes"])
        replacement.unlink()
        return {"state": "verification_failed"}

    monkeypatch.setattr(clients, "_read_probe", failed_probe)
    monkeypatch.setattr(clients, "up", lambda *_a, **_kw: pytest.fail("No service may start"))
    with pytest.raises(InputError, match="failed verification"):
        clients.connect_client(
            store, provider="cursor", config=config, mcp_url="http://127.0.0.1:9999/mcp"
        )
    own_policy = authenticate(store, observed["own_token"])
    assert (own_policy is not None) is preexisting
    assert connection.exists(), "Rollback removed another client's replacement credential"
    assert connection.read_bytes() == observed["foreign_bytes"]
    assert authenticate(store, observed["foreign_token"]) is not None
    assert config.read_bytes() == config_before
    assert (registry.read_bytes() if registry.exists() else None) == registry_before


def test_interrupted_config_publication_preserves_referenced_access_and_reports_incomplete(
    tmp_path, monkeypatch
):
    store = Store(tmp_path / "memory")
    store.initialize()
    config = tmp_path / "config.json"
    config.write_text('{"theme":"original"}\n')
    registry = store.directory / "integrations.json"
    observed = {}

    async def read_probe(entry, **_checks):
        connection = Path(entry["args"][4])
        observed["connection"] = connection
        observed["token"] = read_connection(connection)["token"]
        return {"state": "verified", "read": True, "write": False}

    real_write = clients.atomic_write

    def interrupted_write(path, content, **kwargs):
        real_write(path, content, **kwargs)
        if path == config:
            current = json.loads(path.read_text())
            current["theme"] = "Concurrent setting after publication"
            path.write_text(json.dumps(current))
            raise OSError("Simulated failure after configuration publication")

    monkeypatch.setattr(clients, "_read_probe", read_probe)
    monkeypatch.setattr(clients, "atomic_write", interrupted_write)
    monkeypatch.setattr(clients, "up", lambda *_a, **_kw: pytest.fail("No service may start"))
    failure = None
    try:
        clients.connect_client(
            store, provider="cursor", config=config, mcp_url="http://127.0.0.1:9999/mcp"
        )
    except (InputError, OSError) as error:
        failure = error
    current = json.loads(config.read_text())
    assert current["theme"] == "Concurrent setting after publication"
    assert current["mcpServers"]["dots-brain"]["args"][4] == str(observed["connection"])
    assert {
        "registry_retained": registry.exists(),
        "credential_retained": observed["connection"].exists(),
        "own_token_active": authenticate(store, observed["token"]) is not None,
        "explicit_input_error": isinstance(failure, InputError),
    } == dict.fromkeys(
        ["registry_retained", "credential_retained", "own_token_active", "explicit_input_error"],
        True,
    )
    assert "incomplete" in str(failure).lower()
    registered = registrations(store.directory)["items"][integration_key("cursor", config)]
    assert registered["entry"] == current["mcpServers"]["dots-brain"]
    assert registered["connection_file"] == str(observed["connection"])
