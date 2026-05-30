#!/usr/bin/env python3
"""
asm2c — turn a PCLP2 NASM template (file.asm / main.asm) into a clean,
simple main.c scaffold so you can solve the subject in C.

It converts:
  * section .data / .rodata / .bss declarations (db/dw/dd/dq/resb...) into
    equivalent C globals (the "variables - sections" part);
  * function signatures mentioned in the TODO comments (e.g.
    `void lowercase(char *src)`) into C stubs;
  * every `; TODO x: ...` comment into a C comment placeholder inside the
    right function (or main).

The output is intentionally plain C — NOT decompiler/Ghidra spaghetti.
After you finish main.c, run  c2asm  to turn it back into 64-bit NASM.

Usage:
    ./asm2c.py file.asm [-o main.c]
"""
import argparse
import re
import sys

SIZE_TO_CTYPE = {
    "db": ("uint8_t", 1),
    "dw": ("uint16_t", 2),
    "dd": ("uint32_t", 4),
    "dq": ("uint64_t", 8),
}
RES_TO_CTYPE = {
    "resb": ("uint8_t", 1),
    "resw": ("uint16_t", 2),
    "resd": ("uint32_t", 4),
    "resq": ("uint64_t", 8),
}


def join_continuations(lines):
    """Join NASM line continuations: a line ending in '\\' continues the next
    one (used for multi-row matrices like `matrix db 1,2,3, \\`)."""
    out = []
    buf = ""
    for line in lines:
        if line.rstrip().endswith("\\"):
            buf += line.rstrip()[:-1] + " "
        else:
            out.append(buf + line)
            buf = ""
    if buf:
        out.append(buf)
    return out


def strip_comment(line):
    """Split a NASM line into (code, comment) respecting quotes."""
    out = []
    i = 0
    quote = None
    while i < len(line):
        c = line[i]
        if quote:
            out.append(c)
            if c == quote:
                quote = None
        else:
            if c in "\"'":
                quote = c
                out.append(c)
            elif c == ";":
                return "".join(out), line[i + 1:].strip()
            else:
                out.append(c)
        i += 1
    return "".join(out), ""


def split_values(s):
    """Split a NASM operand list on commas, respecting quoted strings."""
    parts = []
    cur = ""
    quote = None
    for c in s:
        if quote:
            cur += c
            if c == quote:
                quote = None
        elif c in "\"'":
            quote = c
            cur += c
        elif c == ",":
            parts.append(cur.strip())
            cur = ""
        else:
            cur += c
    if cur.strip():
        parts.append(cur.strip())
    return parts


def parse_num(tok):
    """Parse a NASM numeric token -> (python_int, original_text_for_C)."""
    t = tok.strip()
    try:
        if re.match(r"^0[xX][0-9a-fA-F]+$", t):
            return int(t, 16), t
        if re.match(r"^[0-9a-fA-F]+[hH]$", t):
            return int(t[:-1], 16), "0x" + t[:-1]
        if re.match(r"^0[bB][01]+$", t):
            return int(t, 2), t
        if re.match(r"^-?\d+$", t):
            return int(t), t
    except ValueError:
        pass
    return None, t


C_ESCAPES = {0: "\\0", 9: "\\t", 10: "\\n", 13: "\\r", 34: '\\"', 92: "\\\\"}


def bytes_to_cstring(tokens):
    """tokens: list of ('str', text) | ('num', int). Render a C string literal."""
    # drop a single trailing NUL (C adds it implicitly)
    if tokens and tokens[-1] == ("num", 0):
        tokens = tokens[:-1]
    out = []
    for kind, val in tokens:
        if kind == "str":
            for ch in val:
                o = ord(ch)
                out.append(C_ESCAPES.get(o, ch if 32 <= o < 127 else "\\x%02x" % o))
        else:
            out.append(C_ESCAPES.get(val, chr(val) if 32 <= val < 127 else "\\x%02x" % val))
    return '"' + "".join(out) + '"'


def emit_data_decl(label, directive, values):
    """Return a C declaration string for one NASM data line."""
    directive = directive.lower()
    if directive in RES_TO_CTYPE:
        ctype, _ = RES_TO_CTYPE[directive]
        n, _ = parse_num(values[0]) if values else (1, "1")
        return f"{ctype} {label}[{n or 1}];"

    ctype, _ = SIZE_TO_CTYPE[directive]

    if directive == "db":
        # Could be a string, a char array, or a numeric byte array.
        toks = []
        has_str = False
        for v in values:
            v = v.strip()
            if len(v) >= 2 and v[0] in "\"'" and v[-1] == v[0]:
                inner = v[1:-1]
                if v[0] == "'" and len(inner) == 1:
                    toks.append(("num", ord(inner)))  # single char literal
                else:
                    toks.append(("str", inner))
                    has_str = True
            else:
                n, _ = parse_num(v)
                toks.append(("num", n if n is not None else 0))
        if has_str:
            return f"char {label}[] = {bytes_to_cstring(toks)};"
        nums = [str(t[1]) for t in toks]
        if len(nums) == 1:
            return f"uint8_t {label} = {nums[0]};"
        return f"uint8_t {label}[] = {{{', '.join(nums)}}};"

    # dw / dd / dq -> numeric scalar or array
    rendered = []
    for v in values:
        _, ctext = parse_num(v)
        rendered.append(ctext)
    if len(rendered) == 1:
        return f"{ctype} {label} = {rendered[0]};"
    return f"{ctype} {label}[] = {{{', '.join(rendered)}}};"


RES_CTYPE = {"resb": "uint8_t", "resw": "uint16_t", "resd": "uint32_t",
             "resq": "uint64_t"}


def parse_nasm_structs(raw):
    """Find `struc NAME ... endstruc` blocks -> list of (name, fields, todos).
    fields: list of (field_name, ctype, count). Returns (defs, consumed_idx)."""
    defs = []
    skip = set()
    i = 0
    while i < len(raw):
        code, _ = strip_comment(raw[i])
        s = code.strip()
        m = re.match(r"struc\s+(\w+)", s, re.I)
        if m:
            name = m.group(1)
            fields, todos = [], []
            skip.add(i)
            j = i + 1
            while j < len(raw):
                cj, comj = strip_comment(raw[j])
                sj = cj.strip()
                skip.add(j)
                if re.match(r"endstruc\b", sj, re.I):
                    break
                fm = re.match(r"(\w+):?\s+(res[bwdq])\s+(\d+)", sj, re.I)
                if fm:
                    fields.append((fm.group(1), RES_CTYPE[fm.group(2).lower()],
                                   int(fm.group(3))))
                elif comj.strip():
                    todos.append(comj.strip())
                j += 1
            defs.append((name, fields, todos))
            i = j + 1
            continue
        i += 1
    return defs, skip


def render_struct_def(name, fields, todos):
    out = ["typedef struct {"]
    for c in todos:
        out.append(f"    /* {c} */")
    for fname, ctype, count in fields:
        if count > 1:
            out.append(f"    {ctype} {fname}[{count}];")
        else:
            out.append(f"    {ctype} {fname};")
    if not fields and not todos:
        out.append("    /* (empty) */")
    out.append(f"}} {name};")
    return "\n".join(out)


def collect_todos(lines):
    """Return list of dicts {letter, text(list of lines), lineno}."""
    todos = []
    i = 0
    while i < len(lines):
        _, comment = strip_comment(lines[i])
        m = re.match(r"TODO\s*([a-zA-Z0-9])?\s*[:.)]?\s*(.*)", comment)
        if comment and m:
            letter = m.group(1) or ""
            body = [m.group(2).strip()] if m.group(2).strip() else []
            start = i
            j = i + 1
            while j < len(lines):
                code_j, comment_j = strip_comment(lines[j])
                if code_j.strip():  # real code -> stop
                    break
                if not comment_j.strip():  # blank-ish -> stop
                    break
                if re.match(r"TODO\b", comment_j):  # next TODO
                    break
                body.append(comment_j.strip())
                j += 1
            todos.append({"letter": letter, "text": body, "lineno": start})
            i = j
        else:
            i += 1
    return todos


SIG_RE = re.compile(r"`\s*((?:const\s+)?[A-Za-z_][\w\s\*]*?\s+\*?\s*[A-Za-z_]\w*)\s*\(([^)]*)\)\s*`")


def find_signatures(todos):
    """A TODO that quotes a full C signature in backticks -> function stub."""
    sigs = {}
    for t in todos:
        full = " ".join(t["text"])
        m = SIG_RE.search(full)
        if not m:
            continue
        ret_and_name, params = m.group(1).strip(), m.group(2).strip()
        # require a real return type: at least two tokens (type + name) or a '*'
        head = ret_and_name.replace("*", " * ").split()
        if len(head) < 2:
            continue
        name = head[-1]
        ret = " ".join(head[:-1]).replace(" * ", " *").strip()
        sigs[id(t)] = {"ret": ret, "name": name, "params": params, "todo": t}
    return sigs


def render_todo_comment(t, indent="    "):
    head = f"{indent}/* TODO {t['letter']}:".rstrip()
    body = t["text"]
    if not body:
        return head + " */"
    if len(body) == 1:
        return f"{indent}/* TODO {t['letter']}: {body[0]} */"
    out = [f"{indent}/* TODO {t['letter']}: {body[0]}"]
    for extra in body[1:]:
        out.append(f"{indent}   {extra}")
    out.append(f"{indent} */")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description="NASM template -> simple main.c scaffold")
    ap.add_argument("asm", help="input .asm (file.asm / main.asm)")
    ap.add_argument("-o", "--output", default="main.c")
    args = ap.parse_args()

    with open(args.asm) as f:
        raw = join_continuations(f.read().splitlines())

    # ---- pass 1: NASM struct definitions (struc NAME ... endstruc) ----
    struct_defs, skip = parse_nasm_structs(raw)

    # ---- pass 2: sections, data, and struct instances (istruc ... iend) ----
    section = None
    data_decls = []  # (section, c_decl, original)
    last_label = None
    i = 0
    while i < len(raw):
        if i in skip:
            i += 1
            continue
        code, _ = strip_comment(raw[i])
        s = code.strip()
        if not s:
            i += 1
            continue
        m = re.match(r"section\s+\.(\w+)", s, re.I)
        if m:
            section = m.group(1).lower()
            i += 1
            continue
        # a label on its own line (e.g. `binary_funs:` before an istruc)
        lm = re.match(r"([A-Za-z_.$][\w.$]*):$", s)
        if lm:
            last_label = lm.group(1)
            i += 1
            continue
        # struct instance: LABEL: \n istruc TYPE ... iend
        im = re.match(r"istruc\s+(\w+)", s, re.I)
        if im:
            typ = im.group(1)
            vals = []
            j = i + 1
            while j < len(raw):
                cj, _ = strip_comment(raw[j])
                sj = cj.strip()
                if re.match(r"iend\b", sj, re.I):
                    break
                am = re.match(r"at\s+\w+\s*,\s*(d[bwdq])\s+(.*)", sj, re.I)
                if am:
                    vals.extend(split_values(am.group(2)))
                j += 1
            var = last_label or "instance"
            data_decls.append((section, f"{typ} {var} = {{{', '.join(vals)}}};", s))
            last_label = None
            i = j + 1
            continue
        if section in ("data", "rodata", "bss"):
            m = re.match(r"([A-Za-z_.$][\w.$]*):?\s+(d[bwdq]|res[bwdq])\s+(.*)", s, re.I)
            if m:
                label, directive, rest = m.group(1), m.group(2), m.group(3)
                vals = split_values(rest)
                try:
                    decl = emit_data_decl(label, directive, vals)
                    data_decls.append((section, decl, s))
                except Exception as e:  # noqa
                    data_decls.append((section, f"/* could not convert: {s}  ({e}) */", s))
        i += 1

    todos = collect_todos(raw)
    sigs = find_signatures(todos)
    func_todo_ids = set(sigs.keys())

    # ---- build output ----
    out = []
    out.append("#include <stdio.h>")
    out.append("#include <stdlib.h>")
    out.append("#include <string.h>")
    out.append("#include <stdint.h>")
    out.append("")
    if struct_defs:
        out.append("/* ============ structures (from struc ... endstruc) ============ */")
        for sname, sfields, stodos in struct_defs:
            out.append(render_struct_def(sname, sfields, stodos))
            out.append("")
    out.append("/* ============ data (from section .data / .rodata / .bss) ============ */")
    last_sec = None
    for sec, decl, _ in data_decls:
        if sec != last_sec:
            out.append(f"/* --- .{sec} --- */")
            last_sec = sec
        out.append(decl)
    if not data_decls:
        out.append("/* (no data section found) */")
    out.append("")

    # function stubs
    for sid, sig in sigs.items():
        out.append("/* ---- stub for an asm subject function ---- */")
        out.append(f"{sig['ret']} {sig['name']}({sig['params']}) {{")
        out.append(render_todo_comment(sig["todo"], "    "))
        rt = sig["ret"].replace("const", "").strip()
        if rt not in ("void",):
            out.append("    return 0;")
        out.append("}")
        out.append("")

    # main
    out.append("int main(void) {")
    main_todos = [t for t in todos if id(t) not in func_todo_ids]
    if not main_todos:
        out.append("    /* TODO: solve the subject here */")
    for t in main_todos:
        out.append(render_todo_comment(t, "    "))
        out.append("")
    out.append("    return 0;")
    out.append("}")
    out.append("")

    text = "\n".join(out)
    with open(args.output, "w") as f:
        f.write(text)
    print(f"[asm2c] wrote {args.output}  "
          f"({len(data_decls)} data decl, {len(sigs)} func stub, {len(todos)} TODO)")


if __name__ == "__main__":
    main()
