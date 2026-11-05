set -eu
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
bash "$script_dir/build-whisper.sh"
bash "$script_dir/build-ffmpeg.sh"
