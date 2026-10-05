"""Use the existing MCP memory from an agent shell, without exposing credentials."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from dots_brain.bridge import connect
from dots_brain.runtime import up
from dots_brain.store import Store


async def request(credential: Path, tool: str | None, arguments: dict) -> tuple[dict, int]:
    async with connect(credential) as session:
        if tool is None:
            result = await session.list_tools()
            return result.model_dump(mode="json", exclude_none=True), 0
        result = await session.call_tool(tool, arguments)
        return result.model_dump(mode="json", exclude_none=True), int(result.isError)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credential-file", type=Path, required=True)
    parser.add_argument(
        "--local-data-dir", type=Path, help="Resume an existing enabled local service if needed."
    )
    parser.add_argument("tool", nargs="?", help="Tool name; omit to discover tools and schemas.")
    args = parser.parse_args()
    try:
        arguments = {}
        if args.tool:
            if sys.stdin.isatty():
                raise ValueError("Provide a JSON object on stdin.")
            raw = sys.stdin.buffer.read(131073)
            if len(raw) > 131072:
                raise ValueError("Arguments exceed the MCP request limit.")
            arguments = json.loads(raw)
            if not isinstance(arguments, dict):
                raise ValueError("Arguments must be an object.")
        if args.local_data_dir is not None:
            store = Store(args.local_data_dir)
            store.status()  # Never initialize another canonical memory implicitly.
            up(store)
        result, status = asyncio.run(
            asyncio.wait_for(request(args.credential_file, args.tool, arguments), timeout=45)
        )
        print(json.dumps(result, ensure_ascii=False))
        return status
    except Exception:
        # SDK/transport exceptions may include secret request headers or URLs.
        print(
            json.dumps(
                {
                    "state": "error",
                    "message": "Check JSON arguments, the existing service, and client access.",
                }
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
