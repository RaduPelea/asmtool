#!/usr/bin/env python3
"""
port64 — mechanically port a 32-bit PCLP2 NASM template (file.asm) to a
64-bit skeleton (main.asm). It does NOT solve the subject; it just gives you
a 64-bit starting point so you don't fight the toolchain.

Transforms:
  * %include "printf32.asm"  -> "printf64.asm",  PRINTF32 -> PRINTF64
  * 32-bit prologue/epilogue (ebp/esp/eax) -> 64-bit (rbp/rsp/rax)
  * leaves section .data and the ; TODO comments untouched (sizes are fine
    in elf64; db/dw/dd/dq mean the same thing).

Usage:  ./port64.py file.asm [-o main.asm]
"""
import argparse
import re

REPL = [
    (r'%include\s+"printf32\.asm"', '%include "printf64.asm"'),
    (r'\bPRINTF32\b', 'PRINTF64'),
    (r'\bpush\s+ebp\b', 'push rbp'),
    (r'\bpop\s+ebp\b', 'pop rbp'),
    (r'\bmov\s+ebp\s*,\s*esp\b', 'mov rbp, rsp'),
    (r'\bmov\s+esp\s*,\s*ebp\b', 'mov rsp, rbp'),
    (r'\bxor\s+eax\s*,\s*eax\b', 'xor rax, rax'),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("asm")
    ap.add_argument("-o", "--output", default="main.asm")
    args = ap.parse_args()

    with open(args.asm) as f:
        text = f.read()

    for pat, rep in REPL:
        text = re.sub(pat, rep, text)

    header = ("; ported 32-bit -> 64-bit by port64.py\n"
              "; build/run with:  ./asmrun.sh main.asm\n")
    with open(args.output, "w") as f:
        f.write(header + text)
    print(f"[port64] wrote {args.output}")


if __name__ == "__main__":
    main()
