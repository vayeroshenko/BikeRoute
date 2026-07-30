#!/usr/bin/env python3
"""Sanitize an existing JSON response before storing it as a test fixture."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from idf_commute.config import Settings, coordinate_secrets, load_config
from idf_commute.redaction import redact


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    args = parser.parse_args()

    config = load_config(args.config)
    secrets = coordinate_secrets(config)
    api_key = Settings().prim_api_key
    if api_key:
        secrets.add(api_key.get_secret_value())

    payload: Any = json.loads(args.source.read_text(encoding="utf-8"))
    sanitized = redact(payload, secrets)
    args.destination.parent.mkdir(parents=True, exist_ok=True)
    args.destination.write_text(
        json.dumps(sanitized, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
