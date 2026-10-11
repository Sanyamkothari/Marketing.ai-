"""Turn the compliance gate on or off in a test's configuration root (DEC-1405 (a)).

The gate's one switch is `compliance.gate.enabled` in `configs/agentic/agentic.yaml` (DEC-1400 (d));
`configs/compliance/compliance.yaml` holds its settings only and refuses an `enabled` of its own.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from engine.agentic.config import AGENTIC_CONFIG_FILE

__all__ = ["set_gate_switch"]


def set_gate_switch(root: Path, *, on: bool) -> None:
    """Set `compliance.gate.enabled` in `root`'s `agentic/agentic.yaml`, keeping the file's other switches."""
    path = root / AGENTIC_CONFIG_FILE
    document: dict[str, Any] = {}
    if path.is_file():
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    document.setdefault("compliance", {}).setdefault("gate", {})["enabled"] = on
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
