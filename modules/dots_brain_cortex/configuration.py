"""Preserved connection configuration; not called by the disabled core."""

from pathlib import Path

from dots_brain.errors import CapabilityError, InputError
from dots_brain.local import write_json
from dots_brain.store import Store


def load_cortex(store):
    """Load private connection references; this does not contact CORTEX or read its token."""
    from pathlib import Path

    from dots_brain.local import read_json
    from dots_brain.privacy import guard_content

    from .connector import CortexConnectionConfig, CortexConnector, SqliteCortexLedger

    path = store.directory / "cortex.json"
    if not path.exists():
        return None
    try:
        data = read_json(path)
        mapping = tuple(data["project_mapping"].items())
        config = CortexConnectionConfig(
            endpoint=data["endpoint"], token_file=Path(data["token_file"]), project_mapping=mapping
        )
    except (OSError, KeyError, AttributeError, TypeError, ValueError) as exc:
        raise CapabilityError("CORTEX connection configuration is invalid.") from exc
    return CortexConnector(config, ledger=SqliteCortexLedger(store), content_guard=guard_content)


def configure_cortex(store: Store, *, endpoint: str, token_file: Path, projects: list[str]) -> dict:
    from .connector import CortexConnectionConfig

    mapping = {}
    for item in projects:
        local, separator, remote = item.partition("=")
        if not separator or local in mapping:
            raise InputError("Use one unique LOCAL_PROJECT=CORTEX_PROJECT mapping per option.")
        mapping[local] = remote
    config = CortexConnectionConfig(
        endpoint, token_file.expanduser().absolute(), tuple(mapping.items())
    )
    store.status()
    write_json(
        store.directory / "cortex.json",
        {
            "endpoint": config.endpoint,
            "token_file": str(config.token_file),
            "project_mapping": mapping,
        },
    )
    return {
        "state": "configured_pending_verification",
        "projects": list(mapping),
        "restart_required": True,
    }
