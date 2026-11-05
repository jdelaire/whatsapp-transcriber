import argparse
import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
from pathlib import Path
from release_materials import sanitize_tree, zip_tree, reject_private_paths, load_manifest


ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / ".build/app"
DIST = ROOT / "dist"
PORTABLE = ROOT / ".build/portable"
APP = DIST / "Transcriber.app"
MAGIC = {b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"}


def run(arguments, log, **kwargs):
    subprocess.run([str(item) for item in arguments], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=True, **kwargs)


def macho(path):
    if path.is_symlink() or not path.is_file():
        return False
    with path.open("rb") as handle:
        return handle.read(4) in MAGIC


def audit(bundle):
    records = []
    for path in bundle.rglob("*"):
        if path.is_symlink() and not path.resolve().is_relative_to(bundle.resolve()):
            raise RuntimeError(f"External bundle symlink: {path.relative_to(bundle)}")
        if not macho(path):
            continue
        dependencies = subprocess.check_output(["/usr/bin/otool", "-L", str(path)], text=True).splitlines()[1:]
        libraries = [line.strip().split(" (")[0] for line in dependencies if line.strip()]
        for library in libraries:
            if "/opt/homebrew" in library or "/usr/local/" in library or not library.startswith(("/usr/lib/", "/System/Library/", "@")):
                raise RuntimeError(f"Nonportable dependency in {path.name}: {library}")
        commands = subprocess.check_output(["/usr/bin/otool", "-l", str(path)], text=True).splitlines()
        minimums = []
        command = ""
        for line in commands:
            if line.strip().startswith("cmd "):
                command = line.strip().split()[1]
            if command == "LC_BUILD_VERSION" and line.strip().startswith("minos "):
                minimums.append(line.strip().split()[1])
            if command == "LC_VERSION_MIN_MACOSX" and line.strip().startswith("version "):
                minimums.append(line.strip().split()[1])
            if line.strip().startswith("path ") and ("/opt/homebrew" in line or "/usr/local/" in line):
                raise RuntimeError(f"Nonportable rpath in {path.name}")
        if any(tuple(map(int, value.split("."))) > (15, 0, 0) for value in minimums):
            raise RuntimeError(f"Deployment target above macOS 15: {path.name}: {minimums}")
        records.append({"path": str(path.relative_to(bundle)), "minimums": minimums, "dependencies": libraries})
    (DIST / "dependency-audit.json").write_text(json.dumps(records, indent=2))
    return records


def identity():
    result = subprocess.run(["/usr/bin/security", "find-identity", "-v", "-p", "codesigning"], capture_output=True, text=True, check=True)
    for line in result.stdout.splitlines():
        match = re.search(r'([A-Fa-f0-9]{40})\s+"Developer ID Application:', line)
        if match:
            return match.group(1)
    return "-"


def sources(log):
    staging = BUILD / "SourceMaterials"
    if staging.exists():
        for path in staging.rglob("*"):
            if path.is_dir() and not path.is_symlink():
                path.chmod(path.stat().st_mode | 0o700)
        shutil.rmtree(staging)
    staging.mkdir()
    shutil.copytree(PORTABLE / "sources", staging / "sources", symlinks=True, ignore=shutil.ignore_patterns(".git", "__pycache__", ".DS_Store", "._*"))
    shutil.copytree(PORTABLE / "licenses", staging / "Licenses")
    shutil.copy2(PORTABLE / "manifest.json", staging / "PortableBuildManifest.json")
    shutil.copy2(PORTABLE / "env.sh", staging / "env.sh")
    for path in PORTABLE.glob("build-*.sh"):
        shutil.copy2(path, staging / path.name)
    app_source = staging / "TranscriberAppSource"
    app_source.mkdir()
    for name in ("app_engine.py", "app_state.py", "app_pairing.py", "transcriber.py", "manage.py", "README.md", "THIRD_PARTY.md", ".gitignore"):
        shutil.copy2(ROOT / name, app_source / name)
    shutil.copytree(ROOT / "NativeApp", app_source / "NativeApp", ignore=shutil.ignore_patterns(".build", ".swiftpm"))
    shutil.copytree(ROOT / "scripts", app_source / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(ROOT / "tests", app_source / "tests", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(ROOT / "build-support", app_source / "build-support")
    manifest = json.loads((PORTABLE / "manifest.json").read_text())
    original_portable = Path(manifest["artifact_root"])
    if not original_portable.is_absolute():
        original_portable = PORTABLE
    replacements = {str(original_portable): "${PORTABLE_ROOT}", str(original_portable.parents[1]): "${PROJECT_ROOT}", str(PORTABLE): "${PORTABLE_ROOT}", str(ROOT): "${PROJECT_ROOT}", str(Path.home()): "${HOME}"}
    sanitize_tree(staging, replacements)
    reject_private_paths(staging, [Path.home()])
    (staging / "README.txt").write_text("Pinned source, license and build materials for Transcriber. Personal build paths are replaced with documented placeholders. No account data, personal recordings or credentials are included. The complete native app, engine, tests and packaging source are in TranscriberAppSource.\n\n" + (ROOT / "build-support/portable/README.md").read_text())
    archive = DIST / "Transcriber-SourceMaterials.zip"
    archive.unlink(missing_ok=True)
    zip_tree(staging, archive)


def build(sign=True, package_sources=True):
    BUILD.mkdir(parents=True, exist_ok=True)
    DIST.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest(PORTABLE / "manifest.json", PORTABLE, ROOT)
    with (BUILD / "build.log").open("a") as log:
        print("Freezing local engine", flush=True)
        run([manifest["python"]["venv_python"], "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--name", "WAEngine",
             "--target-arch", "arm64", "--exclude-module", "tkinter", "--exclude-module", "unittest",
             "--distpath", BUILD / "frozen", "--workpath", BUILD / "pyinstaller", "--specpath", BUILD,
             "--paths", ROOT, ROOT / "app_engine.py"], log, env=os.environ | {"MACOSX_DEPLOYMENT_TARGET": "15.0"})
        spec = BUILD / "WAEngine.spec"
        spec.write_text(spec.read_text() + '\napp = BUNDLE(coll, name="WAEngine.app", bundle_identifier="com.jdelaire.transcriber.engine", info_plist={"LSMinimumSystemVersion": "15.0"})\n')
        run([manifest["python"]["venv_python"], "-m", "PyInstaller", "--noconfirm", "--distpath", BUILD / "frozen",
             "--workpath", BUILD / "pyinstaller", spec], log)
        print("Building native menu bar app", flush=True)
        run(["/usr/bin/xcrun", "swift", "build", "--package-path", ROOT / "NativeApp", "--build-system", "native", "-c", "release",
             "--triple", "arm64-apple-macosx15.0", "-j", "4", "-Xswiftc", "-file-prefix-map", "-Xswiftc", f"{ROOT}=/build/transcriber", "-Xswiftc", "-debug-prefix-map", "-Xswiftc", f"{ROOT}=/build/transcriber"], log)
        binary = subprocess.check_output(["/usr/bin/xcrun", "swift", "build", "--package-path", str(ROOT / "NativeApp"), "--build-system", "native",
                                          "-c", "release", "--triple", "arm64-apple-macosx15.0", "--show-bin-path"], text=True).strip().splitlines()[-1]
        if APP.exists():
            shutil.rmtree(APP)
        contents = APP / "Contents"
        for name in ("MacOS", "Resources", "Helpers/Tools"):
            (contents / name).mkdir(parents=True)
        shutil.copy2(Path(binary) / "Transcriber", contents / "MacOS/Transcriber")
        run(["/usr/bin/strip", "-S", contents / "MacOS/Transcriber"], log)
        shutil.copytree(BUILD / "frozen/WAEngine.app", contents / "Helpers/Engine.app", symlinks=True)
        for name, tool in manifest["tools"].items():
            source = Path(tool["path"])
            with source.open("rb") as handle:
                if hashlib.file_digest(handle, "sha256").hexdigest() != tool["sha256"]:
                    raise RuntimeError(f"Portable helper checksum changed: {name}")
            shutil.copy2(source, contents / "Helpers/Tools" / name)
        shutil.copytree(PORTABLE / "licenses", contents / "Resources/Licenses")
        iconset = BUILD / "AppIcon.iconset"
        run(["/usr/bin/xcrun", "swift", ROOT / "scripts/make_icon.swift", iconset], log)
        run(["/usr/bin/iconutil", "-c", "icns", iconset, "-o", contents / "Resources/AppIcon.icns"], log)
        public_manifest = {"schema_version": 1, "minimum_macos": "15.0", "architecture": "arm64",
                           "tools": {name: {("unsigned_sha256" if key == "sha256" else key): value for key, value in tool.items() if key in ("version", "sha256", "deployment_target", "dynamic_dependencies")} for name, tool in manifest["tools"].items()},
                           "python": manifest["python"]["version"], "model_sha256": "1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b"}
        (contents / "Resources/Toolchain.json").write_text(json.dumps(public_manifest, indent=2))
        plist = {"CFBundleIdentifier": "com.jdelaire.transcriber", "CFBundleName": "Transcriber", "CFBundleDisplayName": "Transcriber",
                 "CFBundleExecutable": "Transcriber", "CFBundlePackageType": "APPL", "CFBundleShortVersionString": "1.0.0", "CFBundleVersion": "1",
                 "LSUIElement": True, "LSMinimumSystemVersion": "15.0", "NSHighResolutionCapable": True, "CFBundleIconFile": "AppIcon",
                 "NSHumanReadableCopyright": "Copyright © 2026 John Delaire"}
        (contents / "Info.plist").write_bytes(plistlib.dumps(plist))
        print("Auditing portable dependencies", flush=True)
        records = audit(APP)
        reject_private_paths(APP, [Path.home(), ROOT])
        if sign:
            signing_identity = identity()
            print("Signing application" if signing_identity != "-" else "Ad-hoc signing application", flush=True)
            options = ["--options", "runtime", "--timestamp"] if signing_identity != "-" else []
            for record in sorted(records, key=lambda item: len(item["path"]), reverse=True):
                if record["path"] != "Contents/MacOS/Transcriber":
                    run(["/usr/bin/codesign", "--force", "--sign", signing_identity, *options, APP / record["path"]], log, timeout=120)
            run(["/usr/bin/codesign", "--force", "--sign", signing_identity, *options, contents / "Helpers/Engine.app"], log, timeout=120)
            run(["/usr/bin/codesign", "--force", "--sign", signing_identity, *options, APP], log, timeout=120)
            run(["/usr/bin/codesign", "--verify", "--deep", "--strict", APP], log)
        if package_sources:
            print("Packaging source and license materials", flush=True)
            sources(log)
        print("Building distribution archive", flush=True)
        archive = DIST / "Transcriber-1.0.0-arm64.zip"
        archive.unlink(missing_ok=True)
        zip_tree(APP, archive)
        (DIST / "README.txt").write_text("Transcriber 1.0.0 for Apple Silicon, macOS 15+. Move Transcriber.app to Applications and open it. Setup and model download happen in the menu bar. A local speech model is downloaded once; transcription stays on your Mac. Keep and provide the accompanying Transcriber-SourceMaterials.zip when redistributing these bundled tools. Actual runtime verification was performed on macOS 27.2; macOS 15 deployment targets and dependencies were audited, but older-OS execution still needs verification.\n")
        print(f"Built {APP}", flush=True)
        print(f"Mach-O files audited: {len(records)}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-sign", action="store_true")
    parser.add_argument("--no-sources", action="store_true")
    args = parser.parse_args()
    build(sign=not args.no_sign, package_sources=not args.no_sources)
