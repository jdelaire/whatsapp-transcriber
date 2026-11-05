set -eu
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)/env.sh"
test "$PORTABLE_ROOT" = /tmp/transcriber-portable-public
cd "$PORTABLE_ROOT/build"
mkdir -p ffmpeg
cd ffmpeg
"$PORTABLE_ROOT/sources/ffmpeg-9.0.2/configure" \
 --prefix="$PORTABLE_ROOT/tools" --cc=/usr/bin/clang --cxx=/usr/bin/clang++ \
 --arch=arm64 --cpu=apple-m1 --target-os=darwin \
 --disable-autodetect --disable-shared --enable-static --disable-gpl --disable-nonfree \
 --disable-everything --disable-doc --disable-debug --disable-network --disable-avdevice \
 --disable-swscale --disable-videotoolbox --disable-audiotoolbox --disable-coreimage --disable-metal \
 --disable-i8mm --disable-sve --disable-sve2 --disable-sme --disable-sme-i16i64 --disable-sme2 \
 --disable-sdl2 --disable-iconv --disable-lzma --disable-bzlib --disable-zlib \
 --enable-ffmpeg --enable-ffprobe --disable-ffplay --enable-avcodec --enable-avformat --enable-avfilter --enable-swresample \
 --pkg-config="$PORTABLE_ROOT/build-support/bin/pkgconf" --pkg-config-flags=--static \
 --extra-cflags="-ffile-prefix-map=$PORTABLE_ROOT=/tmp/transcriber-portable-public -fdebug-prefix-map=$PORTABLE_ROOT=/tmp/transcriber-portable-public -I$PORTABLE_ROOT/tools/include -arch arm64 -mcpu=apple-m1 -mmacosx-version-min=15.0" \
 --extra-ldflags="-L$PORTABLE_ROOT/tools/lib -arch arm64 -mmacosx-version-min=15.0 -Wl,-dead_strip_dylibs" \
 --enable-libopus --enable-libmp3lame --enable-protocol=file,pipe --enable-decoder=aac,aac_fixed,alac,mp3,mp3float,opus,vorbis,flac,pcm_s16le,pcm_s16be,pcm_s24le,pcm_s24be,pcm_s32le,pcm_s32be,pcm_u8,pcm_f32le,pcm_f32be,pcm_f64le,pcm_f64be \
 --enable-parser=aac,aac_latm,mpegaudio,opus,vorbis,flac \
 --enable-demuxer=ogg,mov,mp3,wav,flac,aiff,aac \
 --enable-encoder=libopus,libmp3lame,aac,alac,flac,pcm_s16le,pcm_s16be,pcm_s24le,pcm_s24be,pcm_s32le,pcm_s32be,pcm_u8,pcm_f32le,pcm_f32be \
 --enable-muxer=wav,aiff,flac,adts,ipod,mov,mp4,ogg,mp3,null \
 --enable-filter=aresample,aformat,anull,atrim,asetpts,volume,concat,abuffer,abuffersink,sine \
 > "$PORTABLE_ROOT/logs/ffmpeg-configure.log" 2>&1
make -j4 ffmpeg ffprobe > "$PORTABLE_ROOT/logs/ffmpeg-build.log" 2>&1
cp ffmpeg ffprobe "$PORTABLE_ROOT/tools/"
