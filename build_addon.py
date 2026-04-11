#!/usr/bin/env python3
"""
Build script — creates the installable Blender addon zip.

Usage:
    python build_addon.py

Output:
    dist/blenddcmcp_v{version}.zip

Install in Blender:
    Edit > Preferences > Add-ons > Install... > select the zip > Enable addon
"""

import ast
import sys
import zipfile
from pathlib import Path

ADDON_DIR = Path(__file__).parent / "addon" / "blenddc_mcp"
DIST_DIR = Path(__file__).parent / "dist"


def _read_version() -> str:
    init = (ADDON_DIR / "__init__.py").read_text()
    tree = ast.parse(init)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "bl_info":
                    info = ast.literal_eval(node.value)
                    return ".".join(str(v) for v in info["version"])
    return "0.0.0"


def build():
    if not ADDON_DIR.is_dir():
        sys.exit(f"ERROR: Addon directory not found: {ADDON_DIR}")

    version = _read_version()
    zip_name = f"blenddcmcp_v{version}.zip"

    DIST_DIR.mkdir(exist_ok=True)
    zip_path = DIST_DIR / zip_name

    files = [p for p in ADDON_DIR.rglob("*") if p.is_file() and p.suffix == ".py"]
    if not files:
        sys.exit("ERROR: No .py files found in addon directory")

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for filepath in sorted(files):
            arcname = Path("blenddc_mcp") / filepath.relative_to(ADDON_DIR)
            zf.write(filepath, arcname)
            print(f"  + {arcname}")

    print(f"\nBuilt: {zip_path}")
    print("\nInstall in Blender:")
    print("  Edit > Preferences > Add-ons > Install... > select the zip > enable addon")
    print("\nMCP endpoint:")
    print("  http://127.0.0.1:8400/mcp")


if __name__ == "__main__":
    build()
