#!/usr/bin/env python3
"""Validate the conventions doc for required sections and terms."""
from __future__ import annotations

import sys
from pathlib import Path

DOC_PATH = Path(__file__).parent.parent / "docs" / "den-memory-spaces-conventions-1456.md"

REQUIRED_TERMS = [
    "assistant",
    "knowledge_base",
    "project",
    "default_write_space",
    "read_spaces",
    "shared_write_spaces",
    "enabled",
    "solo-assistant profile",
    "cluster member",
    "explicit",
    "space=",
    "no automatic cross-space promotion",
    "slugs",
    "title",
    "summary",
    "tags",
    "MEMORY.md",
    "Den project docs",
    "Den memory docs",
    "topic-clipping",
    "doc_type=memory",
    "note",
    "reference",
    "planner",
    "runner",
    "router",
    "coder",
    "orchestrator",
    "worker profiles have no memory",
    "no reliance on automatic capture",
    "intentional",
    "provenance",
]

REQUIRED_SECTIONS = [
    "## 1. Purpose",
    "## 2. Space taxonomy",
    "## 3. Spaces are allocated intentionally",
    "## 4. Profile config surface",
    "## 5. How a profile decides where to write",
    "## 6. Concrete cluster examples",
    "## 7. Naming conventions",
    "## 8. What belongs where",
    "## 9. Project-space drift guidance",
    "## 10. Non-worker role examples",
    "## 11. Worker profiles have no memory",
    "## 12. Validation checklist",
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

    # Ensure no implementation of read/write tools is present (this is conventions-only)
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
