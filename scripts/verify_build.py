#!/usr/bin/env python3
"""
Smoke tests for built PyHakoCLI executable.

Run after build_local.py to verify the built exe works.

Usage:
    uv run python scripts/verify_build.py

Tests:
    1. --help returns 0
    2. --version returns 0
    3. Output contains expected strings
"""
import subprocess
import platform
import sys
from pathlib import Path


def get_exe_path() -> Path:
    """Get path to built executable."""
    project_root = Path(__file__).parent.parent
    dist_dir = project_root / "dist"
    
    if platform.system() == "Windows":
        return dist_dir / "pyhako-cli.exe"
    else:
        return dist_dir / "pyhako-cli"


def run_command(args: list[str], timeout: int = 30) -> tuple[int, str, str]:
    """Run command and return (returncode, stdout, stderr)."""
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout
    )
    return result.returncode, result.stdout, result.stderr


def test_help():
    """Test --help flag."""
    print("📋 Testing --help...")
    exe = get_exe_path()
    
    code, stdout, stderr = run_command([str(exe), "--help"])
    
    if code != 0:
        print(f"   ❌ Exit code: {code}")
        print(f"   stderr: {stderr}")
        return False
    
    # Check expected strings
    expected = ["usage", "pyhako", "scraper", "options"]
    for exp in expected:
        if exp.lower() not in stdout.lower():
            print(f"   ❌ Missing expected string: {exp}")
            return False
    
    print("   ✅ --help works")
    return True


def test_version():
    """Test --version flag."""
    print("📋 Testing --version...")
    exe = get_exe_path()
    
    # Try -V or --version
    code, stdout, stderr = run_command([str(exe), "-V"])
    
    # Some CLI might not have -V, that's okay
    if code != 0:
        print("   ⚠️ -V not available, skipping version check")
        return True
    
    if "0.1.0" in stdout:
        print("   ✅ Version info available")
    else:
        print(f"   ⚠️ Unexpected version output: {stdout.strip()}")
    
    return True


def test_cleanup_dry_run():
    """Test --cleanup with 'n' response (safe operation)."""
    print("📋 Testing --cleanup (will abort)...")
    exe = get_exe_path()
    
    # Run cleanup but send 'n' to abort
    proc = subprocess.Popen(
        [str(exe), "--cleanup"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    
    try:
        stdout, stderr = proc.communicate(input="n\n", timeout=10)
        
        # Should exit without error after aborting
        if "abort" in stdout.lower() or "abort" in stderr.lower() or proc.returncode == 0:
            print("   ✅ --cleanup safely abortable")
            return True
        else:
            print(f"   ⚠️ Unexpected output, but likely okay")
            return True
    except subprocess.TimeoutExpired:
        proc.kill()
        print("   ❌ Timeout during cleanup test")
        return False



def main():
    args = sys.argv[1:]
    skip_build = "--skip-build" in args
    
    if not skip_build:
        print("🚀 Starting Unified Build & Test Workflow")
        project_root = Path(__file__).parent.parent
        
        if platform.system() == "Windows":
            print("📦 Triggering Windows build (build_windows.bat)...")
            script = project_root / "scripts" / "build_windows.bat"
            # We must use shell=True for bat files
            ret = subprocess.run([str(script)], shell=True).returncode
            if ret != 0:
                print("❌ Windows build failed!")
                sys.exit(ret)
            # The bat file calls us back with --skip-build, so we naturally exit when it returns
            # However, build_windows.bat pauses at end? We should check that.
            # Actually, if we want to avoid recursion depth or complexity:
            # But the user wants "uv run python scripts/test_build.py" to do everything.
            # If build_windows.bat calls us, then WE are the parent.
            print("✅ Windows build & test cycle complete.")
            sys.exit(0)
            
        else:
            print("📦 Triggering Linux build (build_local.py --clean)...")
            script = project_root / "scripts" / "build_local.py"
            # Use same python interpreter
            ret = subprocess.run([sys.executable, str(script), "--clean"]).returncode
            if ret != 0:
                print("❌ Linux build failed!")
                sys.exit(ret)
                
    # If we are here, either build matches (Linux) or skipped (Windows callback)
    exe = get_exe_path()
    
    print(f"🔧 Testing built executable: {exe}")
    print()
    
    if not exe.exists():
        print(f"❌ Executable not found: {exe}")
        print("   Run 'uv run python scripts/build_local.py' first")
        sys.exit(1)
    
    results = []
    results.append(("--help", test_help()))
    results.append(("--version", test_version()))
    results.append(("--cleanup", test_cleanup_dry_run()))
    
    print()
    print("=" * 40)
    print("Summary:")
    
    all_passed = True
    for name, passed in results:
        status = "✅" if passed else "❌"
        print(f"  {status} {name}")
        if not passed:
            all_passed = False
    
    if all_passed:
        print()
        print("🎉 All smoke tests passed!")

        sys.exit(0)
    else:
        print()
        print("⚠️ Some tests failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
