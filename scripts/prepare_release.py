import argparse
import hashlib
import shutil
import subprocess
import tempfile
from pathlib import Path

from build_app import build
from release_materials import zip_tree
from sparkle import ARTIFACT, settings, validate_version, validate_appcast


def prepare(version, build_number, keychain_profile, output_dir):
    validate_version(version, build_number)
    config = settings()
    subprocess.run(["/usr/bin/xcrun", "swift", "package", "--package-path", str(ARTIFACT.parents[3]), "resolve"], check=True)
    public_key = subprocess.check_output([ARTIFACT / "bin/generate_keys", "--account", config["key_account"], "-p"], text=True).strip()
    if public_key != config["public_key"]:
        raise ValueError("Keychain public key does not match the app's pinned update key")
    destination = Path(output_dir).resolve()
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("Release output directory must be empty")
    build(version=version, build_number=build_number, output_dir=destination, require_developer_id=True)
    app = destination / "Transcriber.app"
    archive = destination / f"Transcriber-{version}-arm64.zip"
    subprocess.run(["/usr/bin/xcrun", "notarytool", "submit", archive, "--keychain-profile", keychain_profile, "--wait"], check=True)
    subprocess.run(["/usr/bin/xcrun", "stapler", "staple", app], check=True)
    subprocess.run(["/usr/bin/xcrun", "stapler", "validate", app], check=True)
    subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", app], check=True)
    zip_tree(app, archive)
    prefix = f"https://github.com/jdelaire/whatsapp-transcriber/releases/download/v{version}/"
    with tempfile.TemporaryDirectory(prefix="transcriber-appcast-") as folder:
        staged = Path(folder) / archive.name
        shutil.copy2(archive, staged)
        subprocess.run([ARTIFACT / "bin/generate_appcast", "--account", config["key_account"], "--download-url-prefix", prefix,
                        "--maximum-deltas", "0", folder], check=True)
        feed = Path(folder) / "appcast.xml"
        signature = validate_appcast(feed, staged, version, build_number, prefix + archive.name)
        subprocess.run([ARTIFACT / "bin/sign_update", "--account", config["key_account"], "--verify", staged, signature], check=True)
        shutil.copy2(feed, destination / "appcast.xml")
    files = [archive, destination / "Transcriber-SourceMaterials.zip", destination / "appcast.xml"]
    hashes = []
    for path in files:
        with path.open("rb") as handle:
            hashes.append(f"{hashlib.file_digest(handle, 'sha256').hexdigest()}  {path.name}\n")
    (destination / "SHA256SUMS.txt").write_text("".join(hashes))
    print(f"Release prepared in {destination}. Publish these four files together on GitHub Release v{version}.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build, notarize and sign local Sparkle release artifacts without publishing")
    parser.add_argument("--version", required=True)
    parser.add_argument("--build-number", required=True)
    parser.add_argument("--keychain-profile", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    prepare(args.version, args.build_number, args.keychain_profile, args.output_dir)
