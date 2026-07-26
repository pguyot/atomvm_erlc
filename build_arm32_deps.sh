#!/bin/sh
#
# Copyright 2026 Paul Guyot <pguyot@kallisys.net>
# SPDX-License-Identifier: Apache-2.0 OR LGPL-2.1-or-later
#
# Cross-build the static C libraries the Linux arm32 erlc links against, and
# emit the CMake toolchain file the AtomVM build then reuses.
#
# The libraries are built from source rather than installed as :armhf multiarch
# packages: that would need the runner's apt sources to also carry armhf, which
# the images do not guarantee. Building them takes a couple of minutes and
# depends on nothing but the cross compiler.
#
#   zlib      -- BEAM literal chunks are zlib-compressed
#   PCRE2     -- the re module, for parity with the native Linux binaries
#   Mbed-TLS  -- erlang:md5/1, which the OTP compiler calls for every module it
#                emits (beam_asm:module_md5/1). Not optional for this binary.
#
# Usage: build_arm32_deps.sh SYSROOT
#
# Everything is installed under SYSROOT/usr, i.e. a sysroot layout, because
# that is what CMAKE_FIND_ROOT_PATH expects: with SYSROOT as the find root, a
# find_library() looking in /usr/lib is re-rooted to SYSROOT/usr/lib and cannot
# reach the host architecture's copy of the same library.
#
# Env overrides: CROSS_TRIPLE, ZLIB_VERSION, PCRE2_VERSION, MBEDTLS_VERSION

set -e

SYSROOT="${1:?usage: build_arm32_deps.sh SYSROOT}"
CROSS_TRIPLE="${CROSS_TRIPLE:-arm-linux-gnueabihf}"
ZLIB_VERSION="${ZLIB_VERSION:-1.3.1}"
PCRE2_VERSION="${PCRE2_VERSION:-10.45}"
MBEDTLS_VERSION="${MBEDTLS_VERSION:-3.6.4}"

case "$SYSROOT" in
    /*) ;;
    *) SYSROOT="$(pwd)/$SYSROOT" ;;
esac
PREFIX="$SYSROOT/usr"
mkdir -p "$PREFIX"

TOOLCHAIN="$SYSROOT/toolchain.cmake"
# CMAKE_SYSTEM_PROCESSOR=arm is what makes AtomVM select
# AVM_JIT_TARGET_ARCH=arm32. No CMAKE_SYSROOT: the compiler must keep using its
# own libc and headers, only CMake's find_* is confined to this tree.
cat > "$TOOLCHAIN" <<EOF
set(CMAKE_SYSTEM_NAME Linux)
set(CMAKE_SYSTEM_PROCESSOR arm)
set(CMAKE_C_COMPILER ${CROSS_TRIPLE}-gcc)
set(CMAKE_CXX_COMPILER ${CROSS_TRIPLE}-g++)
set(CMAKE_FIND_ROOT_PATH ${SYSROOT})
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)
EOF

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
cd "$WORK"

JOBS="$(nproc 2>/dev/null || echo 2)"

# --- zlib --------------------------------------------------------------------
# zlib's CMake cross story is not worth the trouble; its configure honours CHOST.
echo "==> zlib ${ZLIB_VERSION} (${CROSS_TRIPLE})"
curl -fsSL -o zlib.tar.gz \
    "https://github.com/madler/zlib/releases/download/v${ZLIB_VERSION}/zlib-${ZLIB_VERSION}.tar.gz"
tar xf zlib.tar.gz
cd "zlib-${ZLIB_VERSION}"
CHOST="${CROSS_TRIPLE}" \
CC="${CROSS_TRIPLE}-gcc" \
AR="${CROSS_TRIPLE}-ar" \
RANLIB="${CROSS_TRIPLE}-ranlib" \
    ./configure --prefix="$PREFIX" --static
make -j"$JOBS"
make install
cd "$WORK"

# --- PCRE2 -------------------------------------------------------------------
echo "==> PCRE2 ${PCRE2_VERSION} (${CROSS_TRIPLE})"
curl -fsSL -o pcre2.tar.bz2 \
    "https://github.com/PCRE2Project/pcre2/releases/download/pcre2-${PCRE2_VERSION}/pcre2-${PCRE2_VERSION}.tar.bz2"
tar xf pcre2.tar.bz2
cmake -S "pcre2-${PCRE2_VERSION}" -B pcre2-build -G Ninja \
    -DCMAKE_TOOLCHAIN_FILE="$TOOLCHAIN" \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX="$PREFIX" \
    -DBUILD_SHARED_LIBS=OFF \
    -DPCRE2_BUILD_TESTS=OFF \
    -DPCRE2_BUILD_PCRE2GREP=OFF
cmake --build pcre2-build
cmake --install pcre2-build

# --- Mbed-TLS ----------------------------------------------------------------
echo "==> Mbed-TLS ${MBEDTLS_VERSION} (${CROSS_TRIPLE})"
curl -fsSL -o mbedtls.tar.bz2 \
    "https://github.com/Mbed-TLS/mbedtls/releases/download/mbedtls-${MBEDTLS_VERSION}/mbedtls-${MBEDTLS_VERSION}.tar.bz2"
tar xf mbedtls.tar.bz2
cmake -S "mbedtls-${MBEDTLS_VERSION}" -B mbedtls-build -G Ninja \
    -DCMAKE_TOOLCHAIN_FILE="$TOOLCHAIN" \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX="$PREFIX" \
    -DUSE_STATIC_MBEDTLS_LIBRARY=On \
    -DUSE_SHARED_MBEDTLS_LIBRARY=Off \
    -DENABLE_TESTING=Off \
    -DENABLE_PROGRAMS=Off
cmake --build mbedtls-build
cmake --install mbedtls-build

# --- check -------------------------------------------------------------------
for lib in libz.a libpcre2-8.a libmbedcrypto.a libmbedx509.a libmbedtls.a; do
    if [ ! -f "$PREFIX/lib/$lib" ]; then
        echo "FAIL: $PREFIX/lib/$lib was not built" >&2
        exit 1
    fi
done
echo "Built arm32 dependencies in $PREFIX (toolchain file: $TOOLCHAIN)"
