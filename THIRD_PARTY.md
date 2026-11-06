# Third-party materials

Release bundles include Sparkle, wacli, Whisper.cpp, FFmpeg/ffprobe, a Python runtime and their dependencies. Each component retains its upstream license. License texts are included in `Transcriber.app/Contents/Resources/Licenses`. Sparkle 2.10.1 uses the MIT license and its upstream third-party notices, included as `Sparkle-LICENSE`.

`Transcriber-SourceMaterials.zip` accompanies each release with pinned upstream source, dependency inventories, build scripts, source locations and checksums. It also includes the application and engine source matching that release.

`Toolchain.json` records unsigned input hashes for build provenance. Signing changes executable bytes. `SHA256SUMS.txt` in the release records hashes of the final downloadable files.

Notable bundled licenses include MIT for wacli and Whisper.cpp, LGPL 2.1 or later for this FFmpeg build, the Python license for CPython, GPLv3 for wacli's libsignal dependency and MPL 2.0 for whatsmeow. Refer to the included license texts and dependency inventory for full details. No license text or upstream attribution is removed from the distributed materials.

The speech model is downloaded separately from the Whisper.cpp model repository and verified against its pinned size and SHA-256. Model files, personal recordings and linked-device credentials are not distributed with the app.
