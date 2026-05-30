#!/usr/bin/env bash
# asmrun.sh — assemble (elf64), link (-no-pie) and run a NASM file on x86_64,
# inside the pclp2-x86 Docker image. Works on an arm64 host.
#
#   ./asmrun.sh main.asm            # build + run
#   ./asmrun.sh main.asm --gdb      # build + drop into gdb
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE="pclp2-x86"
ASM="${1:?usage: asmrun.sh file.asm [--gdb]}"
MODE="${2:-run}"

[ -f "$ASM" ] || { echo "no such file: $ASM"; exit 1; }

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "[asmrun] building $IMAGE (one-time)..."
    docker build --platform=linux/amd64 -t "$IMAGE" "$HERE"
fi

# build dir must live under a Docker-Desktop-shared path (macOS shares /Users,
# not /tmp or /var/folders), so put it next to the .asm file.
ASMDIR="$(cd "$(dirname "$ASM")" && pwd)"
WORK="$(mktemp -d "$ASMDIR/.asmrun_XXXX")"
trap 'rm -rf "$WORK"' EXIT
cp "$ASM" "$WORK/main.asm"
# bring printf64.asm along if the source %includes it
if grep -q 'printf64.asm' "$ASM" && [ -f "$HERE/templates/printf64.asm" ]; then
    cp "$HERE/templates/printf64.asm" "$WORK/"
fi

CMD="nasm -f elf64 -g main.asm -o main.o && gcc -no-pie -Wl,--no-warn-execstack -g main.o -o main && echo '----- output -----' && ./main"
if [ "$MODE" = "--gdb" ]; then
    CMD="nasm -f elf64 -g main.asm -o main.o && gcc -no-pie -Wl,--no-warn-execstack -g main.o -o main && gdb ./main"
    exec docker run --rm -it --platform=linux/amd64 -v "$WORK:/work" -w /work "$IMAGE" bash -lc "$CMD"
fi

docker run --rm --platform=linux/amd64 -v "$WORK:/work" -w /work "$IMAGE" bash -lc "$CMD"
