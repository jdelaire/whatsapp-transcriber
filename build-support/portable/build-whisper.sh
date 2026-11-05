set -eu
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)/env.sh"
"${CMAKE:-cmake}" -S "$PORTABLE_ROOT/sources/whisper.cpp-1.9.5" -B "$PORTABLE_ROOT/build/whisper" -G 'Unix Makefiles' \
 -DCMAKE_BUILD_TYPE=Release -DCMAKE_OSX_ARCHITECTURES=arm64 -DCMAKE_OSX_DEPLOYMENT_TARGET=15.0 \
 -DCMAKE_C_FLAGS="-mcpu=apple-m1 -ffile-prefix-map=$PORTABLE_ROOT=/tmp/transcriber-portable-public -fdebug-prefix-map=$PORTABLE_ROOT=/tmp/transcriber-portable-public" -DCMAKE_CXX_FLAGS="-mcpu=apple-m1 -ffile-prefix-map=$PORTABLE_ROOT=/tmp/transcriber-portable-public -fdebug-prefix-map=$PORTABLE_ROOT=/tmp/transcriber-portable-public" \
 -DCMAKE_OBJC_FLAGS="-mcpu=apple-m1 -ffile-prefix-map=$PORTABLE_ROOT=/tmp/transcriber-portable-public -fdebug-prefix-map=$PORTABLE_ROOT=/tmp/transcriber-portable-public" -DCMAKE_OBJCXX_FLAGS="-mcpu=apple-m1 -ffile-prefix-map=$PORTABLE_ROOT=/tmp/transcriber-portable-public -fdebug-prefix-map=$PORTABLE_ROOT=/tmp/transcriber-portable-public" \
 -DCMAKE_EXPORT_COMPILE_COMMANDS=ON -DCMAKE_SKIP_RPATH=ON \
 -DBUILD_SHARED_LIBS=OFF -DGGML_NATIVE=OFF -DGGML_CPU_ARM_ARCH=armv8.4-a+nosve+noi8mm+nosme \
 -DGGML_OPENMP=OFF -DGGML_BACKEND_DL=OFF -DGGML_METAL=ON -DGGML_METAL_EMBED_LIBRARY=ON \
 -DGGML_METAL_USE_BF16=OFF -DGGML_BLAS=ON -DGGML_BLAS_VENDOR=Apple -DGGML_CCACHE=OFF \
 -DWHISPER_USE_SYSTEM_GGML=OFF -DWHISPER_USE_SYSTEM_LLAMA=OFF \
 -DWHISPER_BUILD_IS_DEV=OFF -DWHISPER_BUILD_TESTS=OFF -DWHISPER_BUILD_SERVER=OFF -DWHISPER_BUILD_EXAMPLES=ON \
 -DWHISPER_CURL=OFF -DWHISPER_SDL2=OFF -DWHISPER_COMMON_FFMPEG=OFF \
 > "$PORTABLE_ROOT/logs/whisper-configure.log" 2>&1
"${CMAKE:-cmake}" --build "$PORTABLE_ROOT/build/whisper" --target whisper-cli -j4 > "$PORTABLE_ROOT/logs/whisper-build.log" 2>&1
cp "$PORTABLE_ROOT/build/whisper/bin/whisper-cli" "$PORTABLE_ROOT/tools/whisper-cli"
