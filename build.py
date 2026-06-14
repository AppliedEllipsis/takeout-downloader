#!/usr/bin/env python3
"""
Build script to create a single standalone TUI executable.

Usage:
    python build.py              # Build for current platform
    python build.py --help       # Show instructions
"""

import subprocess
import sys
import platform
from pathlib import Path

APP_NAME = "takeout"
APP_VERSION = "6.0.0"

def get_platform():
    """Get current platform."""
    system = platform.system().lower()
    if system == "darwin":
        return "macos"
    return system

def install_pyinstaller():
    """Install PyInstaller if not present."""
    try:
        import PyInstaller  # noqa: F401
        print("✓ PyInstaller is installed")
    except ImportError:
        print("Installing PyInstaller...")
        subprocess.run([sys.executable, "-m", "pip", "install", "pyinstaller"], check=True)
        print("✓ PyInstaller installed")

def build():
    """Build single executable for current platform."""
    current_platform = get_platform()
    sep = ';' if current_platform == 'windows' else ':'

    print(f"\n{'='*60}")
    print(f"Building {APP_NAME} v{APP_VERSION} for {current_platform.upper()}")
    print(f"{'='*60}\n")

    # PyInstaller command
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--name", APP_NAME,
        "--onefile",
        "--clean",
        "--noconfirm",
        # Hidden imports for TUI
        "--hidden-import", "textual",
        "--hidden-import", "textual.widgets",
        "--hidden-import", "rich",
        # Collect submodules
        "--collect-submodules", "textual",
        "--collect-submodules", "rich",
        # Bundle the modules the entry point imports at runtime
        "--add-data", f"google_takeout_tui.py{sep}.",
        "--add-data", f"takeout_payload.py{sep}.",
        "--add-data", f"aria2c_integration.py{sep}.",
        # Main script
        "takeout.py",
    ]

    # Platform-specific icon
    if current_platform == "windows":
        icon_path = Path("icon.ico")
        if icon_path.exists():
            cmd.extend(["--icon", str(icon_path)])
    elif current_platform == "macos":
        icon_path = Path("icon.icns")
        if icon_path.exists():
            cmd.extend(["--icon", str(icon_path)])
        cmd.extend(["--osx-bundle-identifier", "com.takeout.downloader"])
    elif current_platform == "linux":
        icon_path = Path("icon.png")
        if icon_path.exists():
            cmd.extend(["--icon", str(icon_path)])

    print("Running PyInstaller...\n")
    result = subprocess.run(cmd)

    if result.returncode == 0:
        dist_path = Path("dist")
        exe_name = f"{APP_NAME}.exe" if current_platform == "windows" else APP_NAME
        output_file = dist_path / exe_name

        print(f"\n{'='*60}")
        print("✓ BUILD SUCCESSFUL!")
        print(f"{'='*60}")
        print(f"\nExecutable: {output_file}")

        if output_file.exists():
            size_mb = output_file.stat().st_size / (1024 * 1024)
            print(f"Size: {size_mb:.1f} MB")

        print(f"""
Usage:
  ./{exe_name}              # Launch the TUI
  ./{exe_name} --version    # Show version
""")
        return True
    else:
        print("\n✗ Build failed!")
        return False

def main():
    print(f"""
╔══════════════════════════════════════════════════════════════╗
║       Google Takeout Downloader - Build Tool                  ║
║                    Version {APP_VERSION}                            ║
╚══════════════════════════════════════════════════════════════╝
""")

    if "--help" in sys.argv or "-h" in sys.argv:
        print("""
This script builds a single TUI executable.

The executable works on the platform it was built on.
To build for other platforms, run this script on that platform.

Requirements:
  pip install pyinstaller textual rich requests

Build:
  python build.py

Output:
  dist/takeout       (Linux/macOS)
  dist/takeout.exe   (Windows)
""")
        return

    install_pyinstaller()

    if not build():
        sys.exit(1)

if __name__ == "__main__":
    main()
