#!/usr/bin/env python3
"""Bulk migration script to replace value_fabric.layer* imports with canonical paths."""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent

# Import mappings
IMPORT_MAPPINGS = {
    "value_fabric.layer1": "layer1_ingestion",
    "value_fabric.layer2": "layer2_extraction", 
    "value_fabric.layer3": "layer3_knowledge",
    "value_fabric.layer4": "layer4_agents",
    "value_fabric.layer6": "layer6_benchmarks",
}

def migrate_file(file_path: Path) -> int:
    """Migrate imports in a single file. Returns number of changes made."""
    content = file_path.read_text(encoding="utf-8")
    original_content = content
    changes = 0
    
    for old, new in IMPORT_MAPPINGS.items():
        # Replace "from value_fabric.layerX" imports
        pattern = rf"from {old}\."
        replacement = f"from {new}."
        if re.search(pattern, content):
            content = re.sub(pattern, replacement, content)
            changes += 1
            
        # Replace "import value_fabric.layerX" imports
        pattern = rf"import {old}\."
        replacement = f"import {new}."
        if re.search(pattern, content):
            content = re.sub(pattern, replacement, content)
            changes += 1
    
    if changes > 0:
        file_path.write_text(content, encoding="utf-8")
        print(f"Migrated {file_path.relative_to(REPO_ROOT)} ({changes} changes)")
    
    return changes

def main():
    """Migrate all Python files in tests/ directory."""
    tests_dir = REPO_ROOT / "tests"
    total_changes = 0
    
    for py_file in tests_dir.rglob("*.py"):
        changes = migrate_file(py_file)
        total_changes += changes
    
    print(f"\nTotal changes: {total_changes}")

if __name__ == "__main__":
    main()
