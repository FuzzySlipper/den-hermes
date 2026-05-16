#!/usr/bin/env python3
"""Validate the design doc for required sections and terms."""
from __future__ import annotations

import sys
from pathlib import Path

DOC_PATH = Path(__file__).parent.parent / "docs" / "den-memory-provider-initial-shape-1455.md"

REQUIRED_TERMS = [
    "opt-in",
    "manual-tools-only",
    "Den Core REST",
    "MemoryProvider",
    "prefetch",
    "queue_prefetch",
    "sync_turn",
    "on_session_end",
    "on_pre_compress",
    "on_memory_write",
    "on_delegation",
    "no-op",
    "deferred",
    "advanced super",
    "#1454",
    "bridge repo",
    "plugin layer",
    "tool surface",
    "config schema",
    "system prompt block",
    "Den unavailable",
    "provenance",
    "spaces",
    "#1453",
    "#6040",
    "#6057",
    "den_memory_read",
    "den_memory_search",
    "den_memory_write",
    "den_memory_delete",
    "den_memory_list_spaces",
    "Den Core API gap",
    "follow-up",
    "#1457",
    "#1459",
    "#1460",
]

REQUIRED_SECTIONS = [
    "## 1. Purpose and constraints",
    "## 2. Provider placement",
    "## 3. MemoryProvider hook enumeration",
    "## 4. Transport target: Den Core REST",
    "## 5. Tool surface",
    "## 6. Config schema",
    "## 7. System prompt block",
    "## 8. Den unavailable behavior",
    "## 9. Provenance",
    "## 10. Spaces config decisions",
    "## 11. Den Core API gaps",
    "## 12. Implementation roadmap",
    "## 13. Validation checklist",
]


def main() -> int:
    if not DOC_PATH.exists():
        print(f"FAIL: {DOC_PATH} does not exist")
        return 1

    text = DOC_PATH.read_text()
    errors = 0

    for term in REQUIRED_TERMS:
        if term.lower() not in text.lower():
            print(f"FAIL: required term not found: {term}")
            errors += 1
        else:
            print(f"OK: term found: {term}")

    for section in REQUIRED_SECTIONS:
        if section not in text:
            print(f"FAIL: required section not found: {section}")
            errors += 1
        else:
            print(f"OK: section found: {section}")

    # Ensure no implementation of read/write tools is present (this is design-only)
    forbidden = ["def den_memory_read", "def den_memory_write", "def den_memory_search", "def den_memory_delete", "def den_memory_list_spaces"]
    for f in forbidden:
        if f in text:
            print(f"FAIL: forbidden implementation stub found in doc: {f}")
            errors += 1

    if errors:
        print(f"\nValidation failed with {errors} error(s).")
        return 1

    print("\nAll validations passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
