#!/usr/bin/env python3
"""
Local PyInstaller build script for saka-cli.

This builds the CLI executable locally, same as CI/CD.

Usage:
    uv run python scripts/build_local.py [--clean]

The built executable will be in: dist/saka-cli (or dist/saka-cli.exe on Windows)
"""

import subprocess
import sys
import shutil
import platform
from pathlib import Path


def main():
    project_root = Path(__file__).parent.parent
    dist_dir = project_root / "dist"
    build_dir = project_root / "build"

    # Parse args
    clean = "--clean" in sys.argv

    if clean:
        print("🧹 Cleaning previous build...")
        if dist_dir.exists():
            shutil.rmtree(dist_dir)
        if build_dir.exists():
            shutil.rmtree(build_dir)

    print("📦 Building saka-cli with PyInstaller...")
    print(f"   Platform: {platform.system()}")
    print(f"   Python: {sys.version}")

    # Run PyInstaller with same config as CI
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "build.spec",
        "--noconfirm",
    ]
    if clean:
        cmd.append("--clean")

    result = subprocess.run(cmd, cwd=project_root)

    if result.returncode != 0:
        print("❌ Build failed!")
        sys.exit(1)

    # Determine output name
    if platform.system() == "Windows":
        exe_name = "saka-cli-windows.exe"
        original_name = "saka-cli.exe"
    else:
        exe_name = "saka-cli-linux"
        original_name = "saka-cli"

    # Rename/Move to standard name
    original_path = dist_dir / original_name
    exe_path = dist_dir / exe_name

    if original_path.exists():
        # Rename if distinct
        if original_path != exe_path:
            shutil.move(str(original_path), str(exe_path))

        print("✅ Build successful!")
        print(f"   Output: {exe_path}")
        print(f"   Size: {exe_path.stat().st_size / 1024 / 1024:.1f} MB")
        print()
        print("Next steps:")
        print(f"   1. Test: {exe_path} --help")
        print(f"   2. Run:  {exe_path} -s hinatazaka46")
    else:
        print(f"❌ Expected output not found: {original_path}")
        sys.exit(1)


if __name__ == "__main__":
    main()
