"""Use the existing MCP memory from an agent shell, without exposing credentials."""

import asyncio
import json
import sys
from pathlib import Path

from dots_brain.bridge import connect, resume_local_connection
from dots_brain.cli import ArgumentParser
from dots_brain.errors import InputError
from dots_brain.protocol import safe_error


async def request(credential: Path, tool: str | None, arguments: dict) -> tuple[dict, int]:
    async with connect(credential) as session:
        if tool is None:
            result = await session.list_tools()
            return result.model_dump(mode="json", exclude_none=True), 0
        result = await session.call_tool(tool, arguments)
        return result.model_dump(mode="json", exclude_none=True), int(result.isError)


def main() -> int:
    parser = ArgumentParser(description=__doc__)
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
                raise InputError("Provide a JSON object on stdin, or empty input for no arguments.")
            raw = sys.stdin.buffer.read(1048577)
            if len(raw) > 1048576:
                raise InputError("Arguments exceed the MCP request limit.")
            try:
                arguments = json.loads(raw) if raw.strip() else {}
            except (ValueError, UnicodeError):
                raise InputError("Arguments must be a valid JSON object.") from None
            if not isinstance(arguments, dict):
                raise InputError("Arguments must be an object.")
        if args.local_data_dir is not None:
            resume_local_connection(args.credential_file, args.local_data_dir)
        result, status = asyncio.run(
            asyncio.wait_for(request(args.credential_file, args.tool, arguments), timeout=45)
        )
        print(json.dumps(result, ensure_ascii=False))
        return status
    except Exception as exc:
        # SDK/transport exceptions may include secret request headers or URLs.
        print(
            json.dumps(
                {
                    "state": "error",
                    **safe_error(exc),
                }
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
