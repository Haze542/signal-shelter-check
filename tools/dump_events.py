#!/usr/bin/env python3
"""Print raw signal-cli SSE events for controlled parser-fixture capture only."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sheltercheck.config import ConfigError, validate_loopback_url  # noqa: E402
from sheltercheck.signal_client import SignalClient  # noqa: E402


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Dump raw JSON from signal-cli /api/v1/events (contains personal data)."
    )
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument(
        "--jsonl",
        type=Path,
        help="also append each complete event as one JSON line to this local file",
    )
    return parser.parse_args()


async def _run(url: str, jsonl_path: Path | None) -> None:
    output = None
    if jsonl_path is not None:
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        output = jsonl_path.open("a", encoding="utf-8")
    try:
        async with SignalClient(url) as client:
            await client.check_health()
            print("Connected. Raw events may contain personal data; press Ctrl-C to stop.")
            async for event in client.events():
                print(json.dumps(event, ensure_ascii=False, indent=2), flush=True)
                if output is not None:
                    output.write(json.dumps(event, ensure_ascii=False) + "\n")
                    output.flush()
    finally:
        if output is not None:
            output.close()


def main() -> int:
    args = _arguments()
    try:
        url = validate_loopback_url(args.url)
        asyncio.run(_run(url, args.jsonl))
    except KeyboardInterrupt:
        return 130
    except (ConfigError, OSError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
