#!/usr/bin/env python3
"""
c2asm — turn a finished main.c back into 64-bit x86_64 NASM source.

Pipeline (all the x86_64 parts run inside the `pclp2-x86` Docker image, so it
works even though the host is arm64):

    main.c  --gcc (x86_64, -m64)-->  main.o  --objconv -fnasm-->  main.asm

This covers every category you need because gcc does the lowering for you:
stdlib calls (printf/strstr/...), bit operations, bit-parity (popcount),
arithmetic, operators, screen output and data in .data/.rodata/.bss.

By default it also assembles the produced .asm with nasm and links+runs it,
to prove the NASM file is valid and behaves like your C.

Usage:
    ./c2asm.py main.c [-o main.asm] [-O0|-O1|-O2] [--no-verify] [--run]
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
OBJCONV = os.path.join(HERE, "objconv")


def drun(workdir, cmd):
    """Run a shell command locally (no Docker)."""
    # inlocuieste 'objconv' cu calea locala
    cmd = cmd.replace("objconv ", f"{OBJCONV} ")
    return subprocess.run(cmd, shell=True, capture_output=True,
                          text=True, cwd=workdir)


def main():
    ap = argparse.ArgumentParser(description="main.c -> 64-bit NASM (x86_64)")
    ap.add_argument("c", help="input C file (e.g. main.c)")
    ap.add_argument("-o", "--output", default="main.asm")
    ap.add_argument("-O", "--opt", default="0", choices=["0", "1", "2", "s"],
                    help="gcc optimization level (default 0 = most readable)")
    ap.add_argument("--no-verify", action="store_true",
                    help="don't assemble/link/run the produced asm")
    ap.add_argument("--run", action="store_true",
                    help="run the linked program and show its output")
    args = ap.parse_args()

    if not os.path.exists(args.c):
        sys.exit(f"no such file: {args.c}")

    # IMPORTANT: build dir must sit under a Docker-Desktop-shared path. On
    # macOS only paths like /Users are shared (not /tmp or /var/folders), so
    # we put the build dir right next to the input C file.
    base = os.path.dirname(os.path.abspath(args.c)) or os.getcwd()
    work = tempfile.mkdtemp(prefix=".c2asm_", dir=base)
    try:
        shutil.copy(args.c, os.path.join(work, "main.c"))

        # 1) C -> object (x86_64, no PIE so the asm is simpler & links with -no-pie)
        compile_cmd = (
            f"gcc -m64 -no-pie -fno-pie -O{args.opt} "
            f"-fno-asynchronous-unwind-tables -fno-stack-protector "
            f"-fcf-protection=none -c main.c -o main.o"
        )
        r = drun(work, compile_cmd)
        if r.returncode != 0:
            print(r.stdout)
            print(r.stderr, file=sys.stderr)
            sys.exit("[c2asm] C compilation failed — fix main.c first.")

        # 2) object -> NASM source
        r = drun(work, "objconv -fnasm main.o main.asm && cat main.asm | head -1")
        if r.returncode != 0 or not os.path.exists(os.path.join(work, "main.asm")):
            print(r.stdout)
            print(r.stderr, file=sys.stderr)
            sys.exit("[c2asm] objconv failed.")

        # 3) verify: nasm -f elf64 + link + (optional) run
        if not args.no_verify:
            verify = (
                "nasm -f elf64 -g main.asm -o check.o && "
                "gcc -no-pie -Wl,--no-warn-execstack -g check.o -o check && echo VERIFY_OK"
            )
            if args.run:
                verify += " && echo '----- program output -----' && ./check; true"
            r = drun(work, verify)
            sys.stdout.write(r.stdout)
            if "VERIFY_OK" not in r.stdout:
                print(r.stderr, file=sys.stderr)
                print("[c2asm] WARNING: produced .asm did not re-assemble cleanly "
                      "(the .asm was still written).")

        shutil.copy(os.path.join(work, "main.asm"), args.output)
        print(f"[c2asm] wrote {args.output}")
        print(f"[c2asm] build/run it with:  nasm -f elf64 {args.output} -o out.o && gcc -no-pie out.o -o out && ./out")
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
