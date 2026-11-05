# Portable preparation inputs

These files record the pinned versions, upstream source hashes and preparation commands used for the release. The corresponding-source archive contains the upstream source and dependency inventories. Application build outputs and local environments are not committed.

## Prepare the build root

Run from the application checkout. Place the downloaded `Transcriber-SourceMaterials.zip` there. Use the exact generic build root `/tmp/transcriber-portable-public`; the FFmpeg script deliberately rejects other roots because its recorded configure command must not expose personal paths. Start with a clean directory at that path.

```sh
export PROJECT_ROOT="$PWD"
export PORTABLE_ROOT=/tmp/transcriber-portable-public
source_materials_dir="$(mktemp -d)"
ditto -x -k Transcriber-SourceMaterials.zip "$source_materials_dir"
mkdir -p "$PORTABLE_ROOT"
ditto "$source_materials_dir/SourceMaterials" "$PORTABLE_ROOT"
mkdir -p "$PORTABLE_ROOT/logs" "$PORTABLE_ROOT/tmp" "$PORTABLE_ROOT/cache" "$PORTABLE_ROOT/build" "$PORTABLE_ROOT/tools" "$PORTABLE_ROOT/build-support"
```

Restore the pinned portable Python runtime, isolated PyInstaller environment, CMake and Go build tools from the source URLs, versions and hashes in `PortableBuildManifest.json` before running the preparation scripts. The isolated Python executable belongs at `venv/bin/python`. macOS command line developer tools are required to rebuild helpers. None of these build dependencies are required to use the app.

## Build in order

Use Bash. Build pkgconf and the audio libraries before FFmpeg. Whisper uses the included source tree and embeds its Metal kernels.

```sh
bash "$PORTABLE_ROOT/build-pkgconf.sh"
bash "$PORTABLE_ROOT/build-audio-libs.sh"
bash "$PORTABLE_ROOT/build-whisper.sh"
bash "$PORTABLE_ROOT/build-ffmpeg.sh"
```

Rebuild or restore pinned wacli under `tools/wacli` using the recorded Go version, module checksums and baseline settings. Check helper versions, dependencies and unsigned hashes against the manifest. Rebuilding with a different toolchain can produce different hashes.

## Stage the application inputs

The temporary build root and the application staging directory are separate. After preparing all helpers and the Python environment, copy the resulting tree to the checkout:

```sh
mkdir -p "$PROJECT_ROOT/.build/portable"
ditto "$PORTABLE_ROOT" "$PROJECT_ROOT/.build/portable"
cp "$PORTABLE_ROOT/PortableBuildManifest.json" "$PROJECT_ROOT/.build/portable/manifest.json"
cd "$PROJECT_ROOT"
python3 scripts/build_app.py
```

The application builder expands PORTABLE_ROOT and PROJECT_ROOT placeholders in the manifest to the staging paths. Tool hashes identify unsigned inputs. Final release asset hashes are listed separately in `SHA256SUMS.txt`.
