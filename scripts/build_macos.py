#!/usr/bin/env python3
"""Build a local, ad-hoc-signed menu bar app using the installed Xcode SDK."""

import argparse
import json
import plistlib
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path


def run(*args):
    subprocess.run([str(arg) for arg in args], check=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--arch", choices=["arm64", "x86_64", "universal"], default="universal")
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("The menu bar app must be built on macOS with Xcode Command Line Tools.")
    root = Path(__file__).resolve().parent.parent
    output = root / "dist" / "Claude Cost Lens.app"
    output.parent.mkdir(exist_ok=True)
    architectures = ["arm64", "x86_64"] if args.arch == "universal" else [args.arch]
    sdk = subprocess.check_output(["xcrun", "--sdk", "macosx", "--show-sdk-path"], text=True).strip()
    version = json.loads((root / "package.json").read_text())["version"]
    with tempfile.TemporaryDirectory(prefix="claude-usage-macos-") as temporary:
        work = Path(temporary)
        bundle = work / output.name
        contents = bundle / "Contents"
        executable = contents / "MacOS" / "ClaudeCostLens"
        resources = contents / "Resources"
        executable.parent.mkdir(parents=True)
        resources.mkdir(parents=True)
        binaries = []
        for arch in architectures:
            binary = work / ("monitor-" + arch)
            print("Building " + arch + "…", flush=True)
            run("xcrun", "swiftc", "-swift-version", "5", "-O", "-sdk", sdk,
                "-target", arch + "-apple-macos13.0", "-framework", "AppKit", "-framework", "SwiftUI",
                root / "macos" / "UsageModels.swift", root / "macos" / "MenuBarApp.swift", "-o", binary)
            binaries.append(binary)
        if len(binaries) == 1:
            shutil.copy2(binaries[0], executable)
        else:
            run("xcrun", "lipo", "-create", *binaries, "-output", executable)
        shutil.copy2(root / "app.py", resources / "app.py")
        iconset = work / "AppIcon.iconset"
        iconset.mkdir()
        icon_generator = work / "make-icon"
        run("xcrun", "swiftc", root / "macos" / "MakeIcon.swift", "-o", icon_generator)
        run(icon_generator, iconset)
        run("iconutil", "-c", "icns", iconset, "-o", resources / "AppIcon.icns")
        info = {
            "CFBundleExecutable": "ClaudeCostLens",
            "CFBundleIdentifier": "local.claude-cost-lens.menubar",
            "CFBundleName": "Claude Cost Lens",
            "CFBundleDisplayName": "Claude Cost Lens",
            "CFBundlePackageType": "APPL",
            "CFBundleShortVersionString": version,
            "CFBundleVersion": "1",
            "CFBundleIconFile": "AppIcon",
            "LSMinimumSystemVersion": "13.0",
            "LSUIElement": True,
            "NSHighResolutionCapable": True,
            "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
            "LSApplicationCategoryType": "public.app-category.utilities",
        }
        (contents / "Info.plist").write_bytes(plistlib.dumps(info))
        run("codesign", "--force", "--sign", "-", bundle)
        run("codesign", "--verify", "--deep", "--strict", bundle)
        if output.exists():
            backup = output.with_name("Claude Cost Lens.previous-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".app")
            output.rename(backup)
        shutil.copytree(bundle, output)
    print("Built: " + str(output), flush=True)
    print("Architectures: " + ", ".join(architectures))
    print("Requires Python 3 at runtime; Node.js is not required by the app.")


if __name__ == "__main__":
    main()
