"""Label/formula loader for UDS identifier JSON files.

Loads manufacturer-specific identifier tables from JSON and provides
a lookup interface for Hudson's UDS discovery system.

Formula string DSL:
    uint8               → int(raw[0])
    uint16_be           → int.from_bytes(raw[:2], 'big')
    int16_be            → int.from_bytes(raw[:2], 'big', signed=True)
    uint32_be           → int.from_bytes(raw[:4], 'big')
    ascii               → raw.decode('ascii', errors='replace').strip()
    uint8 - 40          → int(raw[0]) - 40
    uint16_be / 100     → int.from_bytes(raw[:2], 'big') / 100
    uint16_be / 10 - 40 → int.from_bytes(raw[:2], 'big') / 10 - 40
    int16_be / 100      → signed 16-bit / 100
    (A*256+B) / 1280    → Toyota-style named byte formula
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_DATA_DIR = Path(__file__).parent


@dataclass(frozen=True, slots=True)
class IdentifierInfo:
    identifier: int
    label: str
    unit: str
    confidence: str
    notes: str
    formula_str: str
    ecu_header: str | None = None

    def decode(self, raw: bytes) -> float | str | None:
        """Apply the formula to raw bytes and return the physical value."""
        if not raw:
            return None
        try:
            return _apply_formula(self.formula_str, raw)
        except Exception:
            return None


def _apply_formula(formula: str, raw: bytes) -> float | str | None:
    formula = formula.strip()

    if formula == "ascii":
        return raw.decode("ascii", errors="replace").strip("\x00").strip()

    # Named byte formulas like (A*256+B)/1280
    if re.search(r'\b[A-Z]\b', formula):
        return _apply_named_byte_formula(formula, raw)

    # Build base value
    if formula.startswith("int16_be"):
        base = int.from_bytes(raw[:2], "big", signed=True)
        rest = formula[len("int16_be"):].strip()
    elif formula.startswith("uint32_be"):
        base = int.from_bytes(raw[:4], "big")
        rest = formula[len("uint32_be"):].strip()
    elif formula.startswith("uint16_be"):
        base = int.from_bytes(raw[:2], "big")
        rest = formula[len("uint16_be"):].strip()
    elif formula.startswith("uint8"):
        base = int(raw[0])
        rest = formula[len("uint8"):].strip()
    else:
        return None

    if not rest:
        return float(base)

    # Apply arithmetic operations: / N, * N, - N, + N
    for op_match in re.finditer(r'([+\-*/])\s*([\d.]+)', rest):
        op, num = op_match.group(1), float(op_match.group(2))
        if op == '/':
            base = base / num
        elif op == '*':
            base = base * num
        elif op == '-':
            base = base - num
        elif op == '+':
            base = base + num

    return float(base)


def _apply_named_byte_formula(formula: str, raw: bytes) -> float | None:
    """Handle Toyota-style named byte formulas: A, B, C... map to raw[0], raw[1]..."""
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    scope: dict[str, float] = {}
    for i, letter in enumerate(letters):
        if i < len(raw):
            scope[letter] = float(raw[i])
        else:
            scope[letter] = 0.0

    # Replace letter references in formula (whole word only)
    py_expr = re.sub(r'\b([A-Z])\b', lambda m: str(scope.get(m.group(1), 0.0)), formula)
    try:
        return float(eval(py_expr, {"__builtins__": {}}, {}))  # noqa: S307
    except Exception:
        return None


class LabelDatabase:
    """In-memory label database loaded from JSON files."""

    def __init__(self) -> None:
        self._db: dict[int, IdentifierInfo] = {}

    def load(self, manufacturer: str) -> None:
        """Load labels for a manufacturer. Call once during init."""
        path = _DATA_DIR / f"{manufacturer}.json"
        if not path.exists():
            return
        data = json.loads(path.read_text())

        # Load all identifier sections
        for section_key in ("standardized_identifiers", "engine_identifiers",
                            "transmission_identifiers", "mode_22_identifiers"):
            section = data.get(section_key, {})
            if not isinstance(section, dict):
                continue  # skip note-only dicts
            for id_str, info in section.items():
                if not id_str.startswith("0x"):
                    continue
                identifier = int(id_str, 16)
                self._db[identifier] = IdentifierInfo(
                    identifier=identifier,
                    label=info.get("label", id_str),
                    unit=info.get("unit", ""),
                    confidence=info.get("confidence", "UNKNOWN"),
                    notes=info.get("notes", ""),
                    formula_str=info.get("formula", "uint16_be"),
                    ecu_header=info.get("ecu_header"),
                )

    def lookup(self, identifier: int) -> IdentifierInfo | None:
        return self._db.get(identifier)

    def all_known(self) -> list[IdentifierInfo]:
        return list(self._db.values())

    def known_identifiers(self) -> set[int]:
        return set(self._db.keys())


_INSTANCES: dict[str, LabelDatabase] = {}


def get_label_db(manufacturer: str) -> LabelDatabase:
    """Get (or lazily load) the label database for a manufacturer."""
    if manufacturer not in _INSTANCES:
        db = LabelDatabase()
        db.load(manufacturer)
        _INSTANCES[manufacturer] = db
    return _INSTANCES[manufacturer]
