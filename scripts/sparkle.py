import base64
import json
import plistlib
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / "NativeApp/.build/artifacts/sparkle/Sparkle"
NAMESPACE = "{http://www.andymatuschak.org/xml-namespaces/sparkle}"


def settings():
    return json.loads((ROOT / "build-support/sparkle.json").read_text())


def validate_version(version, build_number):
    if not re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", version):
        raise ValueError("Version must contain three numeric components")
    if not re.fullmatch(r"[1-9][0-9]*", build_number):
        raise ValueError("Build number must be a positive integer")


def validate_appcast(feed, archive, version, build_number, download_url):
    items = ET.parse(feed).findall("./channel/item")
    if len(items) != 1:
        raise ValueError("Expected one release in the generated appcast")
    item = items[0]
    enclosure = item.find("enclosure")
    if enclosure is None:
        raise ValueError("Missing update enclosure")
    expected = {"version": build_number, "shortVersionString": version, "minimumSystemVersion": "15.0"}
    if any(item.findtext(NAMESPACE + name) != value for name, value in expected.items()):
        raise ValueError("Appcast version or minimum macOS does not match the bundle")
    if enclosure.get("url") != download_url or enclosure.get("length") != str(archive.stat().st_size):
        raise ValueError("Appcast does not match the final update archive")
    signature = enclosure.get(NAMESPACE + "edSignature", "")
    try:
        if len(base64.b64decode(signature, validate=True)) != 64:
            raise ValueError("Invalid Ed25519 signature")
    except ValueError as error:
        raise ValueError("Invalid Ed25519 signature") from error
    return signature


def embed_framework(contents):
    xcframework = ARTIFACT / "Sparkle.xcframework"
    libraries = plistlib.loads((xcframework / "Info.plist").read_bytes())["AvailableLibraries"]
    library = next(item for item in libraries if item["SupportedPlatform"] == "macos" and "arm64" in item["SupportedArchitectures"])
    destination = contents / "Frameworks/Sparkle.framework"
    shutil.copytree(xcframework / library["LibraryIdentifier"] / library["LibraryPath"], destination, symlinks=True)
    shutil.copy2(ARTIFACT / "LICENSE", contents / "Resources/Licenses/Sparkle-LICENSE")


def sign_framework(contents, identity, options, log):
    framework = contents / "Frameworks/Sparkle.framework"
    version = framework / "Versions/B"
    for name in ("XPCServices/Installer.xpc", "XPCServices/Downloader.xpc", "Autoupdate", "Updater.app", "."):
        arguments = ["/usr/bin/codesign", "--force", "--sign", identity, *options]
        if name == "XPCServices/Downloader.xpc":
            arguments.append("--preserve-metadata=entitlements")
        subprocess.run([*arguments, str(version / name)], stdout=log, stderr=subprocess.STDOUT, check=True, timeout=120)
