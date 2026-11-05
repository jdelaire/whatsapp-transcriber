set -eu
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)/env.sh"
export CFLAGS="-O2 -arch arm64 -mcpu=apple-m1 -mmacosx-version-min=15.0 -ffile-prefix-map=$PORTABLE_ROOT=/tmp/transcriber-portable-public -fdebug-prefix-map=$PORTABLE_ROOT=/tmp/transcriber-portable-public"
export LDFLAGS='-arch arm64 -mmacosx-version-min=15.0'
"${CMAKE:-cmake}" -S "$PORTABLE_ROOT/sources/opus-1.6.1" -B "$PORTABLE_ROOT/build/opus" \
 -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$PORTABLE_ROOT/tools" \
 -DCMAKE_OSX_DEPLOYMENT_TARGET=15.0 -DCMAKE_OSX_ARCHITECTURES=arm64 \
 -DCMAKE_C_FLAGS="$CFLAGS" -DBUILD_SHARED_LIBS=OFF -DOPUS_BUILD_TESTING=OFF \
 -DOPUS_BUILD_PROGRAMS=OFF -DOPUS_INSTALL_PKG_CONFIG_MODULE=ON \
 > "$PORTABLE_ROOT/logs/opus-configure.log" 2>&1
"${CMAKE:-cmake}" --build "$PORTABLE_ROOT/build/opus" -j4 > "$PORTABLE_ROOT/logs/opus-build.log" 2>&1
"${CMAKE:-cmake}" --install "$PORTABLE_ROOT/build/opus" > "$PORTABLE_ROOT/logs/opus-install.log" 2>&1
mkdir -p "$PORTABLE_ROOT/build/lame"
cd "$PORTABLE_ROOT/build/lame"
"$PORTABLE_ROOT/sources/lame-3.100/configure" --prefix="$PORTABLE_ROOT/tools" --host=aarch64-apple-darwin \
 --disable-shared --enable-static --disable-frontend --disable-decoder --disable-gtktest --disable-analyzer-hooks \
 > "$PORTABLE_ROOT/logs/lame-configure.log" 2>&1
make -j4 > "$PORTABLE_ROOT/logs/lame-build.log" 2>&1
make install > "$PORTABLE_ROOT/logs/lame-install.log" 2>&1
