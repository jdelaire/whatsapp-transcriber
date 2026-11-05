set -eu
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)/env.sh"
mkdir -p "$PORTABLE_ROOT/build/pkgconf"
cd "$PORTABLE_ROOT/build/pkgconf"
"$PORTABLE_ROOT/sources/pkgconf-2.5.1/configure" --prefix="$PORTABLE_ROOT/build-support" --disable-shared --enable-static \
 CFLAGS="-O2 -mcpu=apple-m1 -mmacosx-version-min=15.0 -ffile-prefix-map=$PORTABLE_ROOT=/tmp/transcriber-portable-public -fdebug-prefix-map=$PORTABLE_ROOT=/tmp/transcriber-portable-public" > "$PORTABLE_ROOT/logs/pkgconf-configure.log" 2>&1
make -j4 > "$PORTABLE_ROOT/logs/pkgconf-build.log" 2>&1
make install > "$PORTABLE_ROOT/logs/pkgconf-install.log" 2>&1
