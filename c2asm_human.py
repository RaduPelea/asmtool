#!/usr/bin/env python3
"""
c2asm_human — transpile a *restricted* C subset (the kind PCLP2 exam subjects
use) into idiomatic, hand-written-looking x86_64 NASM.

Unlike `c2asm.py` (gcc + objconv = mechanical compiler output), this walks a
real C AST (pycparser) and emits asm in the style of a student solution:
    * System V calling convention (rdi, rsi, rdx, rcx, r8, r9)
    * named scalar locals kept in callee-saved registers (rbx, r12..r15)
    * readable labels (.for_0, .if_end_0, ...) and inline comments
    * %define-d constants for global array lengths
    * proper section .data / .rodata / .bss, extern printf, leave/ret

It supports the common exam subset and *errors loudly* on anything outside it
(so it never silently emits wrong code). After generating, build/run with
`asmrun.sh` to verify.

Usage:
    ./c2asm_human.py main.c [-o main.asm]
"""
import argparse
import os
import re
import subprocess
import sys

try:
    from pycparser import c_parser, c_ast
except ImportError:
    sys.exit("need pycparser:  python3 -m pip install --user pycparser")

# ----------------------------------------------------------------------------
# preprocessing: pycparser can't read system headers, so we strip #include and
# prepend a small prelude of the types/prototypes the exam code uses.
# ----------------------------------------------------------------------------
PRELUDE = """
typedef unsigned char uint8_t;
typedef unsigned short uint16_t;
typedef unsigned int uint32_t;
typedef unsigned long uint64_t;
typedef signed char int8_t;
typedef short int16_t;
typedef int int32_t;
typedef long int64_t;
typedef unsigned long size_t;
int printf(const char *, ...);
int putchar(int);
int puts(const char *);
char *strstr(const char *, const char *);
unsigned long strlen(const char *);
int strcmp(const char *, const char *);
char *strcpy(char *, const char *);
int abs(int);
int __builtin_popcount(unsigned int);
int __builtin_popcountl(unsigned long);
"""


def strip_comments(src):
    """Remove // and /* */ comments, preserving string/char literals."""
    out = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c in "\"'":
            q = c
            out.append(c); i += 1
            while i < n:
                out.append(src[i])
                if src[i] == "\\" and i + 1 < n:
                    out.append(src[i + 1]); i += 2; continue
                if src[i] == q:
                    i += 1; break
                i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            i += 2
            while i + 1 < n and not (src[i] == "*" and src[i + 1] == "/"):
                i += 1
            i += 2
            out.append(" ")
            continue
        out.append(c); i += 1
    return "".join(out)


def preprocess(src):
    src = strip_comments(src)
    out = []
    for line in src.splitlines():
        s = line.strip()
        if s.startswith("#include"):
            continue
        if s.startswith("#define"):
            # support simple object-like  #define NAME value
            m = re.match(r"#define\s+(\w+)\s+(.+)", s)
            if m:
                out.append((m.group(1), m.group(2)))
            continue
        out.append(line)
    # object-like defines -> textual substitution
    defines = [x for x in out if isinstance(x, tuple)]
    code = "\n".join(x for x in out if not isinstance(x, tuple))
    for name, val in defines:
        code = re.sub(r"\b%s\b" % re.escape(name), "(%s)" % val, code)
    return PRELUDE + "\n" + code


# ----------------------------------------------------------------------------
# type helpers
# ----------------------------------------------------------------------------
TYPE_W = {  # width in bytes
    "char": 1, "signed char": 1, "unsigned char": 1, "_Bool": 1,
    "short": 2, "unsigned short": 2,
    "int": 4, "unsigned int": 4, "unsigned": 4,
    "long": 8, "unsigned long": 8, "long long": 8, "unsigned long long": 8,
    "int8_t": 1, "uint8_t": 1, "int16_t": 2, "uint16_t": 2,
    "int32_t": 4, "uint32_t": 4, "int64_t": 8, "uint64_t": 8, "size_t": 8,
}
UNSIGNED = {"unsigned char", "unsigned short", "unsigned int", "unsigned",
            "unsigned long", "unsigned long long", "_Bool",
            "uint8_t", "uint16_t", "uint32_t", "uint64_t", "size_t"}

REGS = {  # full -> (8,4,2,1) byte names
    "rax": ("rax", "eax", "ax", "al"), "rbx": ("rbx", "ebx", "bx", "bl"),
    "rcx": ("rcx", "ecx", "cx", "cl"), "rdx": ("rdx", "edx", "dx", "dl"),
    "rsi": ("rsi", "esi", "si", "sil"), "rdi": ("rdi", "edi", "di", "dil"),
    "r8": ("r8", "r8d", "r8w", "r8b"), "r9": ("r9", "r9d", "r9w", "r9b"),
    "r10": ("r10", "r10d", "r10w", "r10b"), "r11": ("r11", "r11d", "r11w", "r11b"),
    "r12": ("r12", "r12d", "r12w", "r12b"), "r13": ("r13", "r13d", "r13w", "r13b"),
    "r14": ("r14", "r14d", "r14w", "r14b"), "r15": ("r15", "r15d", "r15w", "r15b"),
}
ARG_REGS = ["rdi", "rsi", "rdx", "rcx", "r8", "r9"]
CALLEE_SAVED = ["rbx", "r12", "r13", "r14", "r15"]
SCRATCH = ["r10", "r11"]  # caller-saved temporaries we control


def rsz(full, width):
    idx = {8: 0, 4: 1, 2: 2, 1: 3}[width]
    return REGS[full][idx]


class Unsupported(Exception):
    pass


def walk(node):
    if node is None:
        return
    yield node
    for _, c in node.children():
        yield from walk(c)


def has_call(node):
    """True if evaluating this expression emits a `call` (which would skew the
    stack and break a one-register push/pop temporary)."""
    return any(isinstance(x, c_ast.FuncCall) for x in walk(node))


_CHAR_ESC = {"n": 10, "t": 9, "r": 13, "0": 0, "\\": 92, "'": 39, '"': 34,
             "a": 7, "b": 8, "f": 12, "v": 11}


def char_const_value(lit):
    """Integer value of a C char literal like 'A', '\\n', '\\x41', '\\0'."""
    s = lit
    if len(s) >= 2 and s[0] == "'" and s[-1] == "'":
        s = s[1:-1]
    if s.startswith("\\") and len(s) >= 2:
        c = s[1]
        if c in _CHAR_ESC:
            return _CHAR_ESC[c]
        if c == "x":
            return int(s[2:], 16)
        if c.isdigit():
            return int(s[1:], 8)
        return ord(c)
    return ord(s) if s else 0


def const_int(node):
    """Integer value of an int or char Constant, else None."""
    if not isinstance(node, c_ast.Constant):
        return None
    if node.type == "int":
        return int(node.value, 0)
    if node.type == "char":
        return char_const_value(node.value)
    return None


# ----------------------------------------------------------------------------
# describe a C type from a pycparser node -> (kind, width, signed, base_width)
#   kind: 'scalar' | 'ptr' | 'array'
# ----------------------------------------------------------------------------
def base_typename(node):
    names = node.names if isinstance(node, c_ast.IdentifierType) else node
    return " ".join(names)


class TypeInfo:
    def __init__(self, kind, width, signed, elem_w=None, elem_signed=True,
                 count=None, struct=None):
        self.kind = kind          # scalar / ptr / array / struct
        self.width = width        # storage width of the variable itself
        self.signed = signed
        self.elem_w = elem_w      # for ptr/array: pointed-to element width
        self.elem_signed = elem_signed
        self.count = count        # array length if known
        self.struct = struct      # layout dict if this is a struct or struct*
        self.dims = None          # dimension sizes for multi-dim arrays


# struct registry: typedef/tag name -> {fields:{name:(ti,off)}, order, size, align}
STRUCTS = {}


def align_up(n, a):
    return (n + a - 1) // a * a


def field_size(ti):
    if ti.kind == "struct":
        return ti.struct["size"]
    if ti.kind == "array":
        return (ti.count or 0) * ti.elem_w
    return ti.width


def field_align(ti):
    if ti.kind == "struct":
        return ti.struct["align"]
    if ti.kind == "array":
        return ti.elem_w
    return ti.width


def build_layout(struct_node):
    off, align = 0, 1
    fields, order = {}, []
    for d in struct_node.decls or []:
        fti = typeinfo(d.type)
        fa = field_align(fti)
        off = align_up(off, fa)
        fields[d.name] = (fti, off)
        order.append(d.name)
        off += field_size(fti)
        align = max(align, fa)
    return {"fields": fields, "order": order, "size": align_up(off, align),
            "align": align}


def layout_for_struct(struct_node):
    """Resolve a c_ast.Struct node to a layout (build it if it has fields)."""
    tag = struct_node.name
    if struct_node.decls:
        layout = build_layout(struct_node)
        if tag:
            STRUCTS[tag] = layout
        return layout
    if tag and tag in STRUCTS:
        return STRUCTS[tag]
    raise Unsupported("unknown struct '%s'" % tag)


def register_structs(ast):
    for ext in ast.ext:
        node, tdname = None, None
        if isinstance(ext, c_ast.Typedef) and isinstance(ext.type, c_ast.TypeDecl) \
                and isinstance(ext.type.type, c_ast.Struct):
            node, tdname = ext.type.type, ext.name
        elif isinstance(ext, c_ast.Decl) and isinstance(ext.type, c_ast.Struct):
            node = ext.type
        if node is not None and node.decls:
            layout = build_layout(node)
            # canonical NASM name for the `struc` (prefer the typedef name)
            layout["name"] = tdname or node.name
            if node.name:
                STRUCTS[node.name] = layout
            if tdname:
                STRUCTS[tdname] = layout


def struct_of_typename(tn):
    return STRUCTS.get(tn)


def typeinfo(decl_type):
    t = decl_type
    if isinstance(t, c_ast.TypeDecl):
        if isinstance(t.type, c_ast.Struct):
            layout = layout_for_struct(t.type)
            return TypeInfo("struct", layout["size"], False, struct=layout)
        tn = base_typename(t.type)
        if tn in STRUCTS:
            layout = STRUCTS[tn]
            return TypeInfo("struct", layout["size"], False, struct=layout)
        w = TYPE_W.get(tn, 4)
        return TypeInfo("scalar", w, tn not in UNSIGNED)
    if isinstance(t, c_ast.PtrDecl):
        inner = t.type
        if isinstance(inner, c_ast.FuncDecl):
            # function pointer, e.g. void (*f)(int *) -> just an 8-byte callable
            return TypeInfo("ptr", 8, False, elem_w=1)
        if isinstance(inner, c_ast.TypeDecl) and isinstance(inner.type, c_ast.Struct):
            layout = layout_for_struct(inner.type)
            return TypeInfo("ptr", 8, False, elem_w=layout["size"], struct=layout)
        tn = base_typename(inner.type) if isinstance(inner, c_ast.TypeDecl) else "char"
        if tn in STRUCTS:
            layout = STRUCTS[tn]
            return TypeInfo("ptr", 8, False, elem_w=layout["size"], struct=layout)
        ew = TYPE_W.get(tn, 1)
        return TypeInfo("ptr", 8, False, elem_w=ew, elem_signed=tn not in UNSIGNED)
    if isinstance(t, c_ast.ArrayDecl):
        # peel nested ArrayDecl for multi-dimensional arrays (int m[2][3])
        dims = []
        node = t
        while isinstance(node, c_ast.ArrayDecl):
            d = int(node.dim.value, 0) if isinstance(node.dim, c_ast.Constant) else None
            dims.append(d)
            node = node.type
        tn = base_typename(node.type) if isinstance(node, c_ast.TypeDecl) else "int"
        ew = TYPE_W.get(tn, 4)
        total = 1
        for d in dims:
            total *= (d or 0)
        ti = TypeInfo("array", 8, False, elem_w=ew, elem_signed=tn not in UNSIGNED,
                      count=(total or None))
        ti.dims = dims
        return ti
    raise Unsupported(f"type {t.__class__.__name__}")


# ----------------------------------------------------------------------------
# code generator
# ----------------------------------------------------------------------------
class Gen:
    def __init__(self):
        self.data = []        # lines for section .data
        self.rodata = []      # lines for section .rodata (string literals)
        self.bss = []
        self.text = []
        self.externs = set()
        self.called = set()       # every function name we emit a `call` to
        self.used_structs = []    # layouts whose `struc` def must be emitted
        self.func_names = set()   # functions defined in this file (callable symbols)
        self.defines = []
        self.globals = {}     # name -> TypeInfo (for global arrays/vars)
        self.str_pool = {}    # literal -> label
        self._sc = 0

    # ---- emit helpers ----
    def e(self, line=""):
        self.text.append(line)

    def label(self, name):
        self.text.append(name + ":")

    def comment(self, txt):
        self.text.append("\t; " + txt)

    def new_str(self, s):
        if s in self.str_pool:
            return self.str_pool[s]
        lbl = "str_%d" % len(self.str_pool)
        self.str_pool[s] = lbl
        self.rodata.append("\t%s: db %s" % (lbl, c_string_to_nasm(s)))
        return lbl

    def uid(self):
        self._sc += 1
        return self._sc - 1

    def use_struct(self, layout):
        """Mark a struct as used so its `struc ... endstruc` def gets emitted."""
        if all(s["name"] != layout["name"] for s in self.used_structs):
            self.used_structs.append(layout)

    # ---- globals -> data ----
    def gen_global(self, decl):
        ti = typeinfo(decl.type)
        self.globals[decl.name] = ti
        init = decl.init
        # NOTE: data labels are emitted with a trailing ':' so a name that
        # happens to be an instruction mnemonic (e.g. `str` = Store Task Reg)
        # is still parsed as a label, not an instruction.
        if ti.kind == "array":
            n = ti.count
            if init is None:
                self.bss.append("\t%s: res%s %d" % (decl.name, sz_letter(ti.elem_w),
                                                    n or 0))
            elif isinstance(init, c_ast.Constant) and init.type == "string":
                body = c_string_to_nasm(init.value)         # already NUL-terminated
                if ti.count is None:
                    ti.count = body.count(",") + 1          # rough byte count
                self.defines.append("%%define %s_LEN %d" % (decl.name.upper(), ti.count))
                self.data.append("\t%s: %s %s" % (decl.name, sz_directive(ti.elem_w), body))
            else:
                vals = init_list_values(init)
                if ti.count is None:
                    ti.count = len(vals)
                self.defines.append("%%define %s_LEN %d" % (decl.name.upper(), ti.count))
                self.data.append("\t%s: %s %s" % (decl.name, sz_directive(ti.elem_w),
                                                  ", ".join(vals)))
        elif ti.kind == "struct":
            layout = ti.struct
            self.use_struct(layout)
            if init is None:
                self.bss.append("\t%s: resb %s_size" % (decl.name, layout["name"]))
            else:
                vals = init.exprs if isinstance(init, c_ast.InitList) else [init]
                sname = layout["name"]
                scalar_only = all(layout["fields"][f][0].kind in ("scalar", "ptr")
                                  for f in layout["order"])
                self.data.append("\t%s:" % decl.name)
                if scalar_only:  # idiomatic istruc ... at ... iend
                    self.data.append("\t\tistruc %s" % sname)
                    for i, fname in enumerate(layout["order"]):
                        fti, _ = layout["fields"][fname]
                        v = const_text(vals[i]) if i < len(vals) else "0"
                        self.data.append("\t\t\tat %s.%s,\t%s %s"
                                         % (sname, fname, sz_directive(field_size(fti)), v))
                    self.data.append("\t\tiend")
                else:  # has array/nested fields -> plain directives
                    for i, fname in enumerate(layout["order"]):
                        fti, _ = layout["fields"][fname]
                        v = const_text(vals[i]) if i < len(vals) else "0"
                        self.data.append("\t\t%s %s\t; .%s"
                                         % (sz_directive(field_size(fti)), v, fname))
        elif ti.kind == "ptr":
            if init is None:
                self.bss.append("\t%s: resq 1" % decl.name)
            elif isinstance(init, c_ast.Constant) and init.type == "string":
                lbl = self.new_str(init.value)  # actually store target string
                self.data.append("\t%s: dq %s" % (decl.name, lbl))
            else:
                self.data.append("\t%s: dq %s" % (decl.name, const_text(init)))
        else:  # scalar
            if init is None:
                self.bss.append("\t%s: res%s 1" % (decl.name, sz_letter(ti.width)))
            else:
                self.data.append("\t%s: %s %s" % (decl.name, sz_directive(ti.width),
                                                  const_text(init)))

    # ---- a function ----
    def gen_func(self, fn):
        name = fn.decl.name
        self.externs  # noqa
        F = FuncCtx(self, fn)
        F.run()


def sz_letter(w):
    return {1: "b", 2: "w", 4: "d", 8: "q"}[w]


def sz_directive(w):
    return {1: "db", 2: "dw", 4: "dd", 8: "dq"}[w]


def const_text(node):
    if isinstance(node, c_ast.Constant):
        if node.type == "string":
            return c_string_to_nasm(node.value)
        return node.value
    if isinstance(node, c_ast.UnaryOp) and node.op == "-":
        return "-" + const_text(node.expr)
    if isinstance(node, c_ast.UnaryOp) and node.op == "&" \
            and isinstance(node.expr, c_ast.ID):
        return node.expr.name           # &global -> label address
    if isinstance(node, c_ast.ID):
        return node.name                # global/label reference
    if isinstance(node, c_ast.Cast):
        return const_text(node.expr)
    raise Unsupported("non-constant global initializer")


def init_list_values(init):
    if isinstance(init, c_ast.InitList):
        out = []
        for e in init.exprs:                       # flatten nested {{..},{..}}
            if isinstance(e, c_ast.InitList):
                out.extend(init_list_values(e))
            else:
                out.append(const_text(e))
        return out
    if isinstance(init, c_ast.Constant) and init.type == "string":
        return [c_string_to_nasm(init.value)]
    raise Unsupported("array initializer")


def c_string_to_nasm(raw):
    """raw is the C literal *contents* with quotes, e.g. '"a\\n"'. Render NASM db."""
    s = raw
    if s.startswith('"') and s.endswith('"'):
        s = s[1:-1]
    # unescape C -> bytes
    out, i = [], 0
    parts = []
    cur = ""
    def flush():
        nonlocal cur
        if cur:
            parts.append('"%s"' % cur)
            cur = ""
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            nxt = s[i + 1]
            mp = {"n": 10, "t": 9, "r": 13, "0": 0, "\\": 92, '"': 34, "'": 39}
            if nxt in mp:
                flush(); parts.append(str(mp[nxt])); i += 2; continue
            if nxt == "x":
                j = i + 2
                hexs = ""
                while j < len(s) and s[j] in "0123456789abcdefABCDEF":
                    hexs += s[j]; j += 1
                flush(); parts.append("0x" + hexs); i = j; continue
            cur += nxt; i += 2; continue
        cur += c; i += 1
    flush()
    parts.append("0")
    return ", ".join(parts)


# ----------------------------------------------------------------------------
# per-function context
# ----------------------------------------------------------------------------
class Local:
    def __init__(self, name, ti, reg=None, stack_off=None, is_array_buf=False):
        self.name = name
        self.ti = ti
        self.reg = reg                 # callee-saved full reg holding value/base
        self.stack_off = stack_off     # rbp-relative slot (array base, or spilled scalar)
        self.is_array_buf = is_array_buf
        self.mem = False               # scalar lives in [rbp-stack_off] (register spill)


class FuncCtx:
    def __init__(self, g: Gen, fn):
        self.g = g
        self.fn = fn
        self.name = fn.decl.name
        self.locals = {}
        self.const_locals = {}     # name -> int (constant-folded locals, no register)
        self.array_bufs = []       # local arrays needing a prologue lea
        self.scopes = []           # stack of [Local,...] for register reuse
        self.used_callee = []
        self.free_regs = list(CALLEE_SAVED)
        self.stack_size = 0
        self.ret_signed = True
        self.ret_w = 4
        self.mutated = set()       # local names that are assigned/incremented
        self.loop_stack = []       # (continue_label, break_label) per enclosing loop

    # -- register / stack allocation --
    def take_reg(self):
        if not self.free_regs:
            return None
        r = self.free_regs.pop(0)
        if r not in self.used_callee:
            self.used_callee.append(r)
        return r

    def enter_scope(self):
        self.scopes.append([])

    def exit_scope(self):
        for loc in self.scopes.pop():
            if loc.reg and loc.reg not in self.free_regs:
                self.free_regs.insert(0, loc.reg)   # reuse, still saved in prologue
            self.locals.pop(loc.name, None)

    def alloc_stack(self, nbytes):
        self.stack_size += nbytes
        self.stack_size = (self.stack_size + 15) & ~15
        return self.stack_size

    def declare(self, name, ti):
        if ti.kind in ("array", "struct"):
            if ti.kind == "struct":
                self.g.use_struct(ti.struct)
            nbytes = field_size(ti)
            off = self.alloc_stack(max(nbytes, 16))
            reg = self.take_reg()  # holds base address (None -> lea on each use)
            loc = Local(name, ti, reg=reg, stack_off=off, is_array_buf=True)
            if reg is not None:
                self.array_bufs.append(loc)
        else:
            reg = self.take_reg()
            if reg is None:                       # spill scalar to stack slot
                off = self.alloc_stack(8)
                loc = Local(name, ti, reg=None, stack_off=off)
                loc.mem = True
            else:
                loc = Local(name, ti, reg=reg)
        self.locals[name] = loc
        if self.scopes:
            self.scopes[-1].append(loc)
        return loc

    def scan_mutated(self, node):
        """Collect names of locals that are ever assigned or ++/--'d."""
        for c in walk(node):
            if isinstance(c, c_ast.Assignment) and isinstance(c.lvalue, c_ast.ID):
                self.mutated.add(c.lvalue.name)
            if isinstance(c, c_ast.UnaryOp) and c.op in ("p++", "p--", "++", "--") \
                    and isinstance(c.expr, c_ast.ID):
                self.mutated.add(c.expr.name)

    def maybe_const(self, node):
        """Fold a scalar initializer to an int constant, or return None."""
        if isinstance(node, c_ast.Constant):
            return const_int(node)
        if isinstance(node, c_ast.UnaryOp) and node.op == "-":
            inner = self.maybe_const(node.expr)
            return None if inner is None else -inner
        if isinstance(node, c_ast.ID) and node.name in self.const_locals:
            return self.const_locals[node.name]
        if isinstance(node, c_ast.BinaryOp):
            f = self.try_fold_len(node)
            if f is not None:
                return f
        return None

    def read_local(self, loc, dst, want_w):
        if loc.mem:
            self.load_mem(dst, "[rbp-%d]" % loc.stack_off, loc.ti.width,
                          loc.ti.signed, want_w)
        else:
            self.mov_reg(dst, loc.reg, loc.ti.width, loc.ti.signed, want_w)

    def write_local(self, loc, expr):
        g = self.g
        if loc.mem:
            self.eval_into(expr, "rax", loc.ti.width)
            g.e("\tmov [rbp-%d], %s\t; %s = ..." %
                (loc.stack_off, rsz("rax", loc.ti.width), loc.name))
        else:
            self.eval_into(expr, loc.reg, loc.ti.width)
            g.text[-1] += "\t; %s = ..." % loc.name

    # -- entry --
    def run(self):
        g = self.g
        params = []
        if self.fn.decl.type.args:
            for p in self.fn.decl.type.args.params:
                if isinstance(p, c_ast.Typename):  # void
                    continue
                params.append(p)
        # return type
        rti = typeinfo(self.fn.decl.type.type)
        self.ret_w, self.ret_signed = rti.width, rti.signed

        # pre-scan body decls so we can allocate registers / prologue
        body = self.fn.body
        self.scan_mutated(body)
        self.enter_scope()  # function scope
        # assign param locals
        param_locs = []
        for i, p in enumerate(params):
            ti = typeinfo(p.type)
            loc = self.declare(p.name, ti)
            param_locs.append((i, p, loc))

        # collect declared local arrays' base setup later; first build the body
        # into a temp buffer so we know which callee regs were used.
        saved_text = g.text
        g.text = []
        g.label(self.name)
        g.e("\tpush rbp")
        g.e("\tmov rbp, rsp")
        PROLOGUE_AT = len(g.text)  # we'll splice sub rsp / pushes here

        # move incoming args -> their callee-saved regs
        for i, p, loc in param_locs:
            src = ARG_REGS[i]
            w = loc.ti.width
            g.e("\tmov %s, %s\t; arg %s" % (rsz(loc.reg, 8 if loc.ti.kind != 'scalar' else w),
                                            rsz(src, 8 if loc.ti.kind != 'scalar' else w),
                                            p.name))

        self.gen_block(body)

        # System V requires RSP % 16 == 0 at every `call`. After `push rbp` RSP
        # is 16-aligned; each callee-saved push flips it by 8. So if we push an
        # ODD number of callee-saved registers we reserve an extra 8 bytes to get
        # back onto a 16-byte boundary for the function body (and thus at calls).
        reserve = self.stack_size
        if len(self.used_callee) % 2 == 1:
            reserve += 8

        # default return value
        g.e("\txor eax, eax")
        g.label(".ret_%s" % self.name)
        # epilogue: undo the local reservation FIRST so the pops read the real
        # saved registers (not the locals), then restore callee-saved + rbp.
        if reserve:
            g.e("\tadd rsp, %d" % reserve)
        for r in reversed(self.used_callee):
            g.e("\tpop %s" % r)
        g.e("\tpop rbp")
        g.e("\tret")

        # now splice prologue: push callee-saved + sub rsp + lea array bases
        pro = []
        for r in self.used_callee:
            pro.append("\tpush %s" % r)
        if reserve:
            pro.append("\tsub rsp, %d\t; locals + keep rsp 16-aligned" % reserve)
        for loc in self.array_bufs:
            pro.append("\tlea %s, [rbp-%d]\t; %s[]" % (loc.reg, loc.stack_off, loc.name))
        g.text[PROLOGUE_AT:PROLOGUE_AT] = pro

        func_lines = g.text
        g.text = saved_text
        g.text.append("")
        g.text.extend(func_lines)

    # -- statements --
    def gen_block(self, node):
        if node is None:
            return
        items = node.block_items or []
        for it in items:
            self.gen_stmt(it)

    def gen_stmt(self, n):
        g = self.g
        if isinstance(n, c_ast.Decl):
            ti = typeinfo(n.type)
            # constant-fold simple scalar locals (e.g. int n = sizeof(v)/sizeof(v[0]))
            if ti.kind == "scalar" and n.init is not None and n.name not in self.mutated:
                cv = self.maybe_const(n.init)
                if cv is not None:
                    self.const_locals[n.name] = cv
                    return
            loc = self.declare(n.name, ti)
            if ti.kind == "struct":
                if n.init is not None:
                    self.init_struct_local(loc, n.init)
            elif n.init is not None and ti.kind != "array":
                self.write_local(loc, n.init)
            elif n.init is not None and ti.kind == "array":
                raise Unsupported("local array initializer (declare then fill)")
        elif isinstance(n, c_ast.Assignment):
            self.gen_assign(n)
        elif isinstance(n, c_ast.UnaryOp) and n.op in ("p++", "p--", "++", "--"):
            self.gen_incdec(n)
        elif isinstance(n, c_ast.FuncCall):
            self.gen_call(n, want_result=False)
        elif isinstance(n, c_ast.If):
            self.gen_if(n)
        elif isinstance(n, c_ast.For):
            self.gen_for(n)
        elif isinstance(n, c_ast.While):
            self.gen_while(n)
        elif isinstance(n, c_ast.DoWhile):
            self.gen_do(n)
        elif isinstance(n, c_ast.Switch):
            self.gen_switch(n)
        elif isinstance(n, c_ast.Break):
            if not self.loop_stack:
                raise Unsupported("break outside loop/switch")
            g.e("\tjmp %s" % self.loop_stack[-1][1])
        elif isinstance(n, c_ast.Continue):
            cont = next((c for c, _ in reversed(self.loop_stack) if c), None)
            if cont is None:
                raise Unsupported("continue outside loop")
            g.e("\tjmp %s" % cont)
        elif isinstance(n, c_ast.Return):
            if n.expr is not None:
                self.eval_into(n.expr, "rax", self.ret_w)
            else:
                g.e("\txor eax, eax")
            g.e("\tjmp .ret_%s" % self.name)
        elif isinstance(n, c_ast.Compound):
            self.gen_block(n)
        elif isinstance(n, c_ast.EmptyStatement):
            pass
        else:
            raise Unsupported("statement %s" % n.__class__.__name__)

    def gen_assign(self, n):
        g = self.g
        lhs = n.lvalue
        op = n.op
        if op != "=":
            # x += y  ->  evaluate (x op y) then store
            base = c_ast.BinaryOp(op[:-1], lhs, n.rvalue)
            return self.gen_assign(c_ast.Assignment("=", lhs, base))
        if isinstance(lhs, c_ast.ID) and lhs.name in self.locals \
                and not self.locals[lhs.name].is_array_buf:
            self.write_local(self.locals[lhs.name], n.rvalue)
        elif isinstance(lhs, c_ast.ArrayRef):
            self.store_arrayref(lhs, n.rvalue)
        elif isinstance(lhs, c_ast.StructRef):
            op, fti = self.struct_field_mem(lhs)
            w = 8 if fti.kind == "ptr" else fti.width
            self.eval_into(n.rvalue, "rcx", w)
            g.e("\tmov %s, %s\t; .%s = ..." % (op, rsz("rcx", w), lhs.field.name))
        elif isinstance(lhs, c_ast.ID) and lhs.name in self.g.globals \
                and self.g.globals[lhs.name].kind == "scalar":
            ti = self.g.globals[lhs.name]
            self.eval_into(n.rvalue, "rax", ti.width)
            g.e("\tmov [rel %s], %s\t; %s = ..." % (lhs.name, rsz("rax", ti.width), lhs.name))
        elif isinstance(lhs, c_ast.UnaryOp) and lhs.op == "*":
            self.store_deref(lhs.expr, n.rvalue)
        else:
            raise Unsupported("assignment target")

    def store_deref(self, ptr_expr, value):
        """Store `value` through a pointer: *ptr = value."""
        g = self.g
        ew, _ = self.ptr_elem_info(ptr_expr)
        w = ew if ew <= 4 else 8
        # fast path: pointer already sits in a register
        if isinstance(ptr_expr, c_ast.ID) and ptr_expr.name in self.locals \
                and not self.locals[ptr_expr.name].mem \
                and not self.locals[ptr_expr.name].is_array_buf:
            preg = self.locals[ptr_expr.name].reg
            self.eval_into(value, "rcx", w)
            g.e("\tmov [%s], %s\t; *%s = ..." % (preg, rsz("rcx", ew), ptr_expr.name))
            return
        # general: value first, then the pointer (preserving the value safely)
        self.eval_into(value, "rax", w)
        if has_call(ptr_expr):
            g.e("\tsub rsp, 16\t; save+align across call")
            g.e("\tmov [rsp], rax")
            self.eval_into(ptr_expr, "r10", 8)
            g.e("\tmov rcx, [rsp]")
            g.e("\tadd rsp, 16")
        else:
            g.e("\tpush rax")
            self.eval_into(ptr_expr, "r10", 8)
            g.e("\tpop rcx")
        g.e("\tmov [r10], %s\t; *ptr = ..." % rsz("rcx", ew))

    def gen_incdec(self, n):
        g = self.g
        tgt = n.expr
        opc = "add" if n.op in ("p++", "++") else "sub"
        if isinstance(tgt, c_ast.ID) and tgt.name in self.locals:
            loc = self.locals[tgt.name]
            # pointer arithmetic: p++ advances by sizeof(*p), not by 1
            delta = str(loc.ti.elem_w) if loc.ti.kind == "ptr" else "1"
            if loc.mem:
                g.e("\t%s %s [rbp-%d], %s\t; %s%s" %
                    (opc, size_word(loc.ti.width), loc.stack_off, delta,
                     tgt.name, n.op.replace("p", "")))
            else:
                g.e("\t%s %s, %s\t; %s%s" % (opc, rsz(loc.reg, loc.ti.width), delta,
                                             tgt.name, n.op.replace("p", "")))
        else:
            raise Unsupported("++ on non-local")

    def flatten_arrayref(self, ref):
        """Turn a nested ArrayRef on a 2-D array, m[i][j], into the equivalent
        flat m[i*cols + j] so the normal 1-D addressing handles it."""
        if not isinstance(ref.name, c_ast.ArrayRef):
            return ref
        inner = ref.name                      # m[i] ; ref.subscript = j
        base = inner.name
        if not isinstance(base, c_ast.ID):
            return ref
        name = base.name
        ti = (self.locals[name].ti if name in self.locals
              else self.g.globals.get(name))
        dims = getattr(ti, "dims", None) if ti else None
        if not dims or len(dims) < 2 or dims[1] is None:
            return ref
        cols = dims[1]
        flat = c_ast.BinaryOp("+",
                              c_ast.BinaryOp("*", inner.subscript,
                                             c_ast.Constant("int", str(cols))),
                              ref.subscript)
        return c_ast.ArrayRef(base, flat)

    def ptr_elem_info(self, expr):
        """(elem_w, elem_signed) of the thing a pointer/array expression points to."""
        if isinstance(expr, c_ast.StructRef):
            fti = self.structref_field_ti(expr)
            return fti.elem_w or 4, fti.elem_signed
        if isinstance(expr, c_ast.ID) and expr.name in self.locals:
            ti = self.locals[expr.name].ti
            return ti.elem_w or 4, ti.elem_signed
        if isinstance(expr, c_ast.ID) and expr.name in self.g.globals:
            ti = self.g.globals[expr.name]
            return ti.elem_w or 4, ti.elem_signed
        if isinstance(expr, c_ast.Cast):
            return self.ptr_elem_info(expr.expr)
        if isinstance(expr, c_ast.BinaryOp) and expr.op in ("+", "-"):
            # pointer +/- int -> element type comes from the pointer operand
            for side in (expr.left, expr.right):
                try:
                    return self.ptr_elem_info(side)
                except Unsupported:
                    pass
        raise Unsupported("element type of this pointer")

    def structref_field_ti(self, ref):
        field = ref.field.name
        base = ref.name
        if ref.type == "->":
            layout = self.locals[base.name].ti.struct
        elif base.name in self.g.globals:
            layout = self.g.globals[base.name].struct
        else:
            layout = self.locals[base.name].ti.struct
        return layout["fields"][field][0]

    # -- array element address into a scratch reg; returns (addr_reg, elem_w, signed) --
    def array_addr(self, ref, addr_reg):
        g = self.g
        name = ref.name.name if isinstance(ref.name, c_ast.ID) else None
        if name is None:
            # base is a pointer-valued expression (e.g. v->arr): load it
            ew, es = self.ptr_elem_info(ref.name)
            self.eval_into(ref.name, addr_reg, 8)
            sub = ref.subscript
            idx_reg = "rax" if addr_reg != "rax" else "rcx"
            self.eval_into(sub, idx_reg, 8, sign_extend=True)
            if ew == 1:
                g.e("\tadd %s, %s" % (addr_reg, idx_reg))
            else:
                g.e("\tlea %s, [%s + %s*%d]" % (addr_reg, addr_reg, idx_reg, ew))
            return ew, es
        # base
        if name in self.locals:
            loc = self.locals[name]
            ti = loc.ti
            if loc.is_array_buf:
                if loc.reg:
                    g.e("\tmov %s, %s\t; &%s[0]" % (addr_reg, loc.reg, name))
                else:
                    g.e("\tlea %s, [rbp-%d]\t; &%s[0]" % (addr_reg, loc.stack_off, name))
            elif loc.mem:  # pointer spilled to stack
                g.e("\tmov %s, [rbp-%d]" % (addr_reg, loc.stack_off))
            else:  # pointer in reg
                g.e("\tmov %s, %s" % (addr_reg, loc.reg))
            ew, es = ti.elem_w, ti.elem_signed
        elif name in self.g.globals:
            ti = self.g.globals[name]
            g.e("\tlea %s, [rel %s]\t; &%s[0]" % (addr_reg, name, name))
            ew, es = ti.elem_w, ti.elem_signed
        else:
            raise Unsupported("unknown array %s" % name)
        # + index*elem_w
        sub = ref.subscript
        # fast path: a plain register-backed counter is already zero-extended
        # in its 64-bit register (32-bit writes clear the high half), so we can
        # index with it directly — no movsxd, no scratch.
        if isinstance(sub, c_ast.ID) and sub.name in self.const_locals:
            k = self.const_locals[sub.name]
            if k == 0:
                pass
            elif ew == 1:
                g.e("\tadd %s, %d" % (addr_reg, k))
            else:
                g.e("\tlea %s, [%s + %d]" % (addr_reg, addr_reg, k * ew))
            return ew, es
        if isinstance(sub, c_ast.ID) and sub.name in self.locals \
                and not self.locals[sub.name].mem \
                and not self.locals[sub.name].is_array_buf:
            idx64 = self.locals[sub.name].reg
            if ew == 1:
                g.e("\tadd %s, %s" % (addr_reg, idx64))
            else:
                g.e("\tlea %s, [%s + %s*%d]" % (addr_reg, addr_reg, idx64, ew))
            return ew, es
        idx_reg = "rax" if addr_reg != "rax" else "rcx"
        self.eval_into(sub, idx_reg, 8, sign_extend=True)
        if ew == 1:
            g.e("\tadd %s, %s" % (addr_reg, idx_reg))
        else:
            g.e("\tlea %s, [%s + %s*%d]" % (addr_reg, addr_reg, idx_reg, ew))
        return ew, es

    def elem_info(self, ref):
        if not isinstance(ref.name, c_ast.ID):
            return self.ptr_elem_info(ref.name)
        name = ref.name.name
        if name in self.locals:
            ti = self.locals[name].ti
            return ti.elem_w or 4, ti.elem_signed
        if name in self.g.globals:
            ti = self.g.globals[name]
            return ti.elem_w or 4, ti.elem_signed
        raise Unsupported("unknown array %s" % name)

    def mem_operand(self, ref):
        """Build a single NASM effective address for arr[idx] when both the
        base (global symbol / local buffer reg / pointer reg) and the index
        (register counter or constant) are directly addressable. Returns
        (operand, elem_w, elem_signed) or None to fall back to address-in-reg.
        Valid under -no-pie: `[symbol + reg*scale]` assembles as absolute."""
        name = ref.name.name if isinstance(ref.name, c_ast.ID) else None
        if name is None:
            return None
        sym = base = None
        if name in self.locals:
            loc = self.locals[name]
            if loc.is_array_buf and loc.reg:
                base, ew, es = loc.reg, loc.ti.elem_w, loc.ti.elem_signed
            elif (not loc.is_array_buf) and (not loc.mem):  # pointer in a reg
                base, ew, es = loc.reg, loc.ti.elem_w, loc.ti.elem_signed
            else:
                return None
        elif name in self.g.globals and self.g.globals[name].kind == "array":
            ti = self.g.globals[name]
            sym, ew, es = name, ti.elem_w, ti.elem_signed
        else:
            return None

        sub = ref.subscript
        if isinstance(sub, c_ast.ID) and sub.name in self.const_locals:
            kind, idx = "k", self.const_locals[sub.name]
        elif isinstance(sub, c_ast.Constant) and sub.type == "int":
            kind, idx = "k", int(sub.value, 0)
        elif isinstance(sub, c_ast.ID) and sub.name in self.locals \
                and not self.locals[sub.name].mem \
                and not self.locals[sub.name].is_array_buf:
            kind, idx = "r", self.locals[sub.name].reg
        else:
            return None

        head = ("rel " + sym) if sym else base
        if kind == "k":
            disp = idx * ew
            op = "[%s]" % head if disp == 0 else "[%s + %d]" % (head, disp)
            if sym and disp:
                op = "[rel %s + %d]" % (sym, disp)
        else:
            scale = "" if ew == 1 else " * %d" % ew
            ihead = sym if sym else base   # index present -> absolute, no `rel`
            op = "[%s + %s%s]" % (ihead, idx, scale)
        return op, ew, es

    def init_struct_local(self, loc, init):
        """Fill a local struct (base address in loc.reg) from an initializer list."""
        g = self.g
        layout = loc.ti.struct
        exprs = init.exprs if isinstance(init, c_ast.InitList) else [init]
        for i, fname in enumerate(layout["order"]):
            fti, _ = layout["fields"][fname]
            w = 8 if fti.kind == "ptr" else fti.width
            src = exprs[i] if i < len(exprs) else None
            if src is None:
                continue
            self.eval_into(src, "rcx", w)
            mem = "[%s + %s]" % (loc.reg, self.field_sym(layout, fname))
            g.e("\tmov %s, %s\t; .%s" % (mem, rsz("rcx", w), fname))

    def field_sym(self, layout, field):
        """Symbolic NASM offset for a field, e.g. `binary_fun_t.maskedn`."""
        self.g.use_struct(layout)
        return "%s.%s" % (layout["name"], field)

    def struct_field_mem(self, ref):
        """Return (nasm_mem_operand, field_TypeInfo) for s.field / p->field.
        Offsets are symbolic (STRUCT.field), not hardcoded numbers."""
        field = ref.field.name
        base = ref.name
        if ref.type == "->":
            if isinstance(base, c_ast.ID) and base.name in self.locals:
                loc = self.locals[base.name]
                layout = loc.ti.struct
                if layout is None:
                    raise Unsupported("-> on non-struct pointer %s" % base.name)
                fti, _ = layout["fields"][field]
                if loc.mem:
                    self.g.e("\tmov r10, [rbp-%d]\t; %s" % (loc.stack_off, base.name))
                    breg = "r10"
                else:
                    breg = loc.reg
                return "[%s + %s]" % (breg, self.field_sym(layout, field)), fti
            raise Unsupported("-> on a complex base")
        # '.'  on a struct lvalue
        if isinstance(base, c_ast.ID):
            if base.name in self.g.globals and self.g.globals[base.name].kind == "struct":
                layout = self.g.globals[base.name].struct
                fti, _ = layout["fields"][field]
                return ("[rel %s + %s]" % (base.name, self.field_sym(layout, field)),
                        fti)
            if base.name in self.locals and self.locals[base.name].ti.kind == "struct":
                loc = self.locals[base.name]
                layout = loc.ti.struct
                fti, _ = layout["fields"][field]
                return "[%s + %s]" % (loc.reg, self.field_sym(layout, field)), fti
        raise Unsupported("struct member access on %s" % type(base).__name__)

    def store_arrayref(self, ref, value):
        g = self.g
        ref = self.flatten_arrayref(ref)
        mo = self.mem_operand(ref)
        if mo:  # single-instruction store
            op, ew, _ = mo
            self.eval_into(value, "rcx", 4 if ew <= 4 else 8)
            g.e("\tmov %s, %s\t; store" % (op, rsz("rcx", ew)))
            return
        ew, _ = self.elem_info(ref)
        # general path: value first (saved on stack) so building the address
        # can't clobber it. Use a 16-byte aligned save if the index expression
        # contains a call, to keep RSP aligned at that call.
        self.eval_into(value, "rax", 4 if ew <= 4 else 8)
        if has_call(ref.subscript):
            g.e("\tsub rsp, 16\t; save+align across call")
            g.e("\tmov [rsp], rax")
            self.array_addr(ref, "r10")
            g.e("\tmov rcx, [rsp]")
            g.e("\tadd rsp, 16")
        else:
            g.e("\tpush rax")
            self.array_addr(ref, "r10")
            g.e("\tpop rcx")
        g.e("\tmov [r10], %s\t; store" % rsz("rcx", ew))

    def pick_scratch(self, exclude=()):
        for r in SCRATCH:
            if r not in exclude:
                return r
        return "r11"

    def pointer_scale(self, expr):
        """sizeof(*expr) if expr is a pointer/array (for C pointer arithmetic)."""
        if isinstance(expr, c_ast.ID):
            if expr.name in self.locals:
                ti = self.locals[expr.name].ti
                if ti.kind in ("ptr", "array"):
                    return ti.elem_w or 1
            elif expr.name in self.g.globals:
                ti = self.g.globals[expr.name]
                if ti.kind in ("ptr", "array"):
                    return ti.elem_w or 1
        return 1

    def eval_preserving(self, save_reg, sub_expr, target, w):
        """Evaluate sub_expr into `target` while keeping `save_reg` intact.
        Uses a cheap push/pop normally, but a 16-byte aligned save when
        sub_expr contains a call (so RSP stays 16-aligned at that call)."""
        g = self.g
        if has_call(sub_expr):
            g.e("\tsub rsp, 16\t; save+align across call")
            g.e("\tmov [rsp], %s" % save_reg)
            self.eval_into(sub_expr, target, w)
            g.e("\tmov %s, [rsp]" % save_reg)
            g.e("\tadd rsp, 16")
        else:
            g.e("\tpush %s" % save_reg)
            self.eval_into(sub_expr, target, w)
            g.e("\tpop %s" % save_reg)

    # -- evaluate expression, result into dst register (width-sized) --
    def eval_into(self, n, dst, width, sign_extend=False):
        g = self.g
        if isinstance(n, c_ast.Constant):
            if n.type == "string":
                lbl = self.g.new_str(n.value)
                g.e("\tlea %s, [rel %s]" % (rsz(dst, 8), lbl))
            else:
                v = const_int(n)
                if v is None:
                    raise Unsupported("constant of type %s" % n.type)
                g.e("\tmov %s, %s" % (rsz(dst, max(width, 4)), v))
            return
        if isinstance(n, c_ast.ID):
            if n.name in self.const_locals:
                g.e("\tmov %s, %d\t; %s" % (rsz(dst, max(width, 4)),
                                            self.const_locals[n.name], n.name))
                return
            if n.name in self.locals:
                loc = self.locals[n.name]
                if loc.is_array_buf:
                    if loc.reg:
                        g.e("\tmov %s, %s\t; %s" % (rsz(dst, 8), loc.reg, n.name))
                    else:
                        g.e("\tlea %s, [rbp-%d]\t; %s" % (rsz(dst, 8), loc.stack_off, n.name))
                else:
                    self.read_local(loc, dst, width)
                return
            if n.name in self.g.globals:
                ti = self.g.globals[n.name]
                if ti.kind == "array":
                    g.e("\tlea %s, [rel %s]\t; %s" % (rsz(dst, 8), n.name, n.name))
                else:
                    self.load_mem(dst, "[rel %s]" % n.name, ti.width, ti.signed, width)
                return
            if n.name in self.g.func_names or n.name in self.g.called:
                # a function name used as a value -> its address (e.g. passing
                # next_lcg to map). Treat it as a function pointer.
                self.g.called.add(n.name)
                g.e("\tlea %s, [rel %s]\t; &%s" % (rsz(dst, 8), n.name, n.name))
                return
            raise Unsupported("unknown identifier %s" % n.name)
        if isinstance(n, c_ast.UnaryOp) and n.op == "*":
            # pointer dereference read: load through the pointer value
            ew, es = self.ptr_elem_info(n.expr)
            preg = self.pick_scratch()
            self.eval_into(n.expr, preg, 8)
            self.load_mem(dst, "[%s]" % preg, ew, es, width)
            return
        if isinstance(n, c_ast.ArrayRef):
            n = self.flatten_arrayref(n)
            mo = self.mem_operand(n)
            if mo:  # single-instruction effective address
                op, ew, es = mo
                self.load_mem(dst, op, ew, es, width)
                return
            addr = self.pick_scratch()
            ew, es = self.array_addr(n, addr)
            self.load_mem(dst, "[%s]" % addr, ew, es, width)
            return
        if isinstance(n, c_ast.StructRef):
            op, fti = self.struct_field_mem(n)
            if fti.kind == "ptr":
                self.load_mem(dst, op, 8, False, max(width, 8))
            else:
                self.load_mem(dst, op, fti.width, fti.signed, width)
            return
        if isinstance(n, c_ast.Cast):
            self.eval_into(n.expr, dst, width)
            return
        if isinstance(n, c_ast.TernaryOp):
            uid = self.g.uid()
            false_lbl, end_lbl = ".tern_false_%d" % uid, ".tern_end_%d" % uid
            self.gen_cond_false(n.cond, false_lbl)
            self.eval_into(n.iftrue, dst, width)
            g.e("\tjmp %s" % end_lbl)
            g.label(false_lbl)
            self.eval_into(n.iffalse, dst, width)
            g.label(end_lbl)
            return
        if isinstance(n, c_ast.UnaryOp):
            self.eval_unary(n, dst, width)
            return
        if isinstance(n, c_ast.BinaryOp):
            self.eval_binary(n, dst, width)
            return
        if isinstance(n, c_ast.FuncCall):
            self.gen_call(n, want_result=True)
            if dst != "rax":
                g.e("\tmov %s, %s" % (rsz(dst, width), rsz("rax", width)))
            return
        raise Unsupported("expression %s" % n.__class__.__name__)

    def mov_reg(self, dst, src, src_w, src_signed, want_w):
        g = self.g
        if dst == src and src_w >= want_w:
            return
        if want_w > src_w:  # widen
            instr = "movsx" if src_signed else "movzx"
            if src_w == 4:  # 32->64
                if src_signed:
                    g.e("\tmovsxd %s, %s" % (rsz(dst, 8), rsz(src, 4)))
                else:
                    g.e("\tmov %s, %s" % (rsz(dst, 4), rsz(src, 4)))
            else:
                g.e("\t%s %s, %s" % (instr, rsz(dst, want_w), rsz(src, src_w)))
        else:
            g.e("\tmov %s, %s" % (rsz(dst, want_w), rsz(src, want_w)))

    def load_mem(self, dst, mem, mem_w, signed, want_w):
        g = self.g
        tw = max(want_w, 4)
        if mem_w >= tw:
            g.e("\tmov %s, %s %s" % (rsz(dst, mem_w), size_word(mem_w), mem))
        elif mem_w == 4:
            if signed:
                g.e("\tmovsxd %s, dword %s" % (rsz(dst, 8), mem))
            else:
                g.e("\tmov %s, dword %s" % (rsz(dst, 4), mem))
        else:
            instr = "movsx" if signed else "movzx"
            g.e("\t%s %s, %s %s" % (instr, rsz(dst, tw), size_word(mem_w), mem))

    # -- unary --
    def eval_unary(self, n, dst, width):
        g = self.g
        if n.op == "-":
            self.eval_into(n.expr, dst, width)
            g.e("\tneg %s" % rsz(dst, width))
        elif n.op == "~":
            self.eval_into(n.expr, dst, width)
            g.e("\tnot %s" % rsz(dst, width))
        elif n.op == "!":
            self.eval_into(n.expr, dst, width)
            g.e("\ttest %s, %s" % (rsz(dst, width), rsz(dst, width)))
            g.e("\tsete %s" % rsz(dst, 1))
            g.e("\tmovzx %s, %s" % (rsz(dst, 4), rsz(dst, 1)))
        elif n.op in ("p++", "p--", "++", "--"):
            # value-of then incdec; for exam use as statement mostly
            if isinstance(n.expr, c_ast.ID) and n.expr.name in self.locals:
                loc = self.locals[n.expr.name]
                self.read_local(loc, dst, width)
                self.gen_incdec(n)
            else:
                raise Unsupported("++ expr")
        elif n.op == "sizeof":
            g.e("\tmov %s, %s" % (rsz(dst, max(width, 4)), self.fold_sizeof(n)))
        elif n.op == "&":
            self.eval_addr(n.expr, dst)
        else:
            raise Unsupported("unary %s" % n.op)

    def eval_addr(self, expr, dst):
        """Load the address of an lvalue into dst (for &x)."""
        g = self.g
        if isinstance(expr, c_ast.ID):
            name = expr.name
            if name in self.locals:
                loc = self.locals[name]
                if loc.is_array_buf:        # struct/array base already an address
                    g.e("\tmov %s, %s\t; &%s" % (rsz(dst, 8), loc.reg, name))
                elif loc.mem:
                    g.e("\tlea %s, [rbp-%d]\t; &%s" % (rsz(dst, 8), loc.stack_off, name))
                else:
                    raise Unsupported("& of a register variable %s" % name)
                return
            if name in self.g.globals:
                g.e("\tlea %s, [rel %s]\t; &%s" % (rsz(dst, 8), name, name))
                return
        if isinstance(expr, c_ast.StructRef):
            op, _ = self.struct_field_mem(expr)
            g.e("\tlea %s, %s" % (rsz(dst, 8), op))
            return
        if isinstance(expr, c_ast.ArrayRef):
            self.array_addr(expr, rsz(dst, 8))
            return
        raise Unsupported("address-of this expression")

    def fold_sizeof(self, n):
        """Return a NASM operand for sizeof: a symbolic `NAME_size` for structs,
        else the byte count as a literal."""
        inner = n.expr
        ti = None
        if isinstance(inner, c_ast.Typename):
            ti = typeinfo(inner.type)
        elif isinstance(inner, c_ast.ID) and inner.name in self.g.globals:
            ti = self.g.globals[inner.name]
        elif isinstance(inner, c_ast.ID) and inner.name in self.locals:
            ti = self.locals[inner.name].ti
        if ti is not None and ti.kind == "struct":
            self.g.use_struct(ti.struct)
            return "%s_size" % ti.struct["name"]
        if ti is not None:
            return str(field_size(ti))
        if isinstance(inner, c_ast.ArrayRef):
            return str(self.elem_width_of(inner.name))
        raise Unsupported("sizeof of this")

    def elem_width_of(self, idnode):
        name = idnode.name
        if name in self.locals:
            return self.locals[name].ti.elem_w or 4
        if name in self.g.globals:
            return self.g.globals[name].elem_w or 4
        return 4

    # -- binary --
    # Discipline: accumulate the left operand in rax, the right in rcx, result
    # ends up in rax, then is moved to dst. Complex right operands are pushed so
    # array-index temporaries (which clobber rax) can't corrupt the left value.
    def eval_binary(self, n, dst, width):
        g = self.g
        folded = self.try_fold_len(n)
        if folded is not None:
            g.e("\tmov %s, %d\t; len" % (rsz(dst, max(width, 4)), folded))
            return

        op = n.op
        w = max(width, 4)

        # logical && / || as a value -> materialize 0/1 via short-circuit
        if op in ("&&", "||"):
            uid = g.uid()
            true_lbl, end_lbl = ".true_%d" % uid, ".bool_end_%d" % uid
            self.gen_cond_true(n, true_lbl)
            g.e("\tmov eax, 0")
            g.e("\tjmp %s" % end_lbl)
            g.label(true_lbl)
            g.e("\tmov eax, 1")
            g.label(end_lbl)
            return self.move_result(dst, w)

        # C pointer arithmetic: ptr + i scales i by sizeof(*ptr), result is 64-bit
        pscale = self.pointer_scale(n.left) if op in ("+", "-") else 1
        if pscale != 1:
            w = 8

        rconst = self.maybe_const(n.right)
        self.eval_into(n.left, "rax", w)

        # right is an immediate -> fold directly
        if rconst is not None and op in ("+", "-", "&", "|", "^"):
            mp = {"+": "add", "-": "sub", "&": "and", "|": "or", "^": "xor"}
            g.e("\t%s %s, %d" % (mp[op], rsz("rax", w), rconst * pscale))
            return self.move_result(dst, w)
        if op in ("<<", ">>") and rconst is not None:
            g.e("\t%s %s, %d" % ("shl" if op == "<<" else "shr", rsz("rax", w), rconst))
            return self.move_result(dst, w)
        if op == "*" and rconst is not None:
            g.e("\timul %s, %s, %d" % (rsz("rax", w), rsz("rax", w), rconst))
            return self.move_result(dst, w)

        # general: get right operand into rcx
        if isinstance(n.right, (c_ast.Constant, c_ast.ID)):
            self.eval_into(n.right, "rcx", w)
        else:
            self.eval_preserving("rax", n.right, "rcx", w)
        if pscale != 1:
            g.e("\timul %s, %s, %d\t; * sizeof(*ptr)" % (rsz("rcx", w), rsz("rcx", w), pscale))

        if op in ("+", "-", "&", "|", "^"):
            mp = {"+": "add", "-": "sub", "&": "and", "|": "or", "^": "xor"}
            g.e("\t%s %s, %s" % (mp[op], rsz("rax", w), rsz("rcx", w)))
        elif op == "*":
            g.e("\timul %s, %s" % (rsz("rax", w), rsz("rcx", w)))
        elif op in ("/", "%"):
            g.e("\tcdq")
            g.e("\tidiv ecx")
            if op == "%":
                g.e("\tmov eax, edx")
        elif op in ("<<", ">>"):
            g.e("\t%s %s, cl" % ("shl" if op == "<<" else "shr", rsz("rax", w)))
        elif op in ("==", "!=", "<", ">", "<=", ">="):
            g.e("\tcmp %s, %s" % (rsz("rax", w), rsz("rcx", w)))
            cc = {"==": "sete", "!=": "setne", "<": "setl", ">": "setg",
                  "<=": "setle", ">=": "setge"}[op]
            g.e("\t%s al" % cc)
            g.e("\tmovzx eax, al")
        else:
            raise Unsupported("binop %s" % op)
        self.move_result(dst, w)

    def move_result(self, dst, w):
        if dst != "rax":
            self.g.e("\tmov %s, %s" % (rsz(dst, w), rsz("rax", w)))

    def try_fold_len(self, n):
        # sizeof(a)/sizeof(a[0])
        if n.op == "/" and isinstance(n.left, c_ast.UnaryOp) and n.left.op == "sizeof" \
                and isinstance(n.right, c_ast.UnaryOp) and n.right.op == "sizeof":
            li = n.left.expr
            if isinstance(li, c_ast.ID) and li.name in self.g.globals:
                ti = self.g.globals[li.name]
                if ti.kind == "array" and ti.count is not None:
                    return ti.count
            if isinstance(li, c_ast.ID) and li.name in self.locals:
                ti = self.locals[li.name].ti
                if ti.kind == "array" and ti.count is not None:
                    return ti.count
        return None

    # -- condition: jump to false_lbl if condition is false --
    def gen_cond_false(self, n, false_lbl):
        g = self.g
        if isinstance(n, c_ast.BinaryOp) and n.op in ("==", "!=", "<", ">", "<=", ">="):
            self.gen_cmp(n.left, n.right)
            jcc = {"==": "jne", "!=": "je", "<": "jge", ">": "jle",
                   "<=": "jg", ">=": "jl"}[n.op]
            g.e("\t%s %s" % (jcc, false_lbl))
        elif isinstance(n, c_ast.UnaryOp) and n.op == "!":
            self.gen_cond_true(n.expr, false_lbl)
        elif isinstance(n, c_ast.BinaryOp) and n.op == "&&":
            self.gen_cond_false(n.left, false_lbl)
            self.gen_cond_false(n.right, false_lbl)
        elif isinstance(n, c_ast.BinaryOp) and n.op == "||":
            true_lbl = ".true_%d" % self.g.uid()
            self.gen_cond_true(n.left, true_lbl)
            self.gen_cond_false(n.right, false_lbl)
            g.label(true_lbl)
        else:
            self.eval_into(n, "rax", 4)
            g.e("\ttest eax, eax")
            g.e("\tjz %s" % false_lbl)

    def operand_loc(self, expr):
        """If expr lives somewhere directly cmp-able, return (operand, is_mem)."""
        if isinstance(expr, c_ast.Constant):
            v = const_int(expr)
            return (str(v), False) if v is not None else None
        if isinstance(expr, c_ast.UnaryOp) and expr.op == "-":
            inner = self.operand_loc(expr.expr)
            if inner and not inner[1]:
                return "-" + inner[0], False
            return None
        if isinstance(expr, c_ast.ID):
            if expr.name in self.const_locals:
                return str(self.const_locals[expr.name]), False
            if expr.name in self.locals:
                loc = self.locals[expr.name]
                if loc.is_array_buf:
                    return None
                if loc.mem:
                    return "%s [rbp-%d]" % (size_word(loc.ti.width), loc.stack_off), True
                return rsz(loc.reg, 4), False
            if expr.name in self.g.globals and self.g.globals[expr.name].kind == "scalar":
                ti = self.g.globals[expr.name]
                return "%s [rel %s]" % (size_word(ti.width), expr.name), True
        return None

    def gen_cmp(self, left, right):
        """Emit a single `cmp` when both operands are directly addressable,
        else fall back to evaluating the left side into rax."""
        lo = self.operand_loc(left)
        ro = self.operand_loc(right)
        # cmp's first operand must be reg/mem (not an immediate), and at most one
        # operand may be memory.
        left_imm = self.maybe_const(left) is not None
        if lo and ro and not left_imm and not (lo[1] and ro[1]):
            self.g.e("\tcmp %s, %s" % (lo[0], ro[0]))
            return
        self.eval_into(left, "rax", 4)
        self.emit_cmp_right(right)

    def emit_cmp_right(self, right):
        """Emit `cmp eax, <right>` (left already in rax) without clobbering rax."""
        g = self.g
        cv = self.maybe_const(right)
        if cv is not None:
            g.e("\tcmp eax, %d" % cv)
        elif isinstance(right, c_ast.ID):
            # ID evaluates with a plain mov/load -> never touches rax
            self.eval_into(right, "rcx", 4)
            g.e("\tcmp eax, ecx")
        else:
            # complex right (array ref / arithmetic) would clobber rax -> protect it
            self.eval_preserving("rax", right, "rcx", 4)
            g.e("\tcmp eax, ecx")

    def gen_cond_true(self, n, true_lbl):
        g = self.g
        if isinstance(n, c_ast.BinaryOp) and n.op in ("==", "!=", "<", ">", "<=", ">="):
            self.gen_cmp(n.left, n.right)
            jcc = {"==": "je", "!=": "jne", "<": "jl", ">": "jg",
                   "<=": "jle", ">=": "jge"}[n.op]
            g.e("\t%s %s" % (jcc, true_lbl))
        elif isinstance(n, c_ast.UnaryOp) and n.op == "!":
            self.gen_cond_false(n.expr, true_lbl)
        elif isinstance(n, c_ast.BinaryOp) and n.op == "||":
            self.gen_cond_true(n.left, true_lbl)
            self.gen_cond_true(n.right, true_lbl)
        elif isinstance(n, c_ast.BinaryOp) and n.op == "&&":
            skip = ".skip_%d" % self.g.uid()
            self.gen_cond_false(n.left, skip)
            self.gen_cond_true(n.right, true_lbl)
            g.label(skip)
        else:
            self.eval_into(n, "rax", 4)
            g.e("\ttest eax, eax")
            g.e("\tjnz %s" % true_lbl)

    # -- control flow --
    def gen_if(self, n):
        g = self.g
        uid = g.uid()
        else_lbl = ".else_%d" % uid
        end_lbl = ".endif_%d" % uid
        has_else = n.iffalse is not None
        self.gen_cond_false(n.cond, else_lbl if has_else else end_lbl)
        self.gen_stmt_or_block(n.iftrue)
        if has_else:
            g.e("\tjmp %s" % end_lbl)
            g.label(else_lbl)
            self.gen_stmt_or_block(n.iffalse)
        g.label(end_lbl)

    def gen_for(self, n):
        g = self.g
        uid = g.uid()
        top = ".for_%d" % uid
        end = ".for_end_%d" % uid
        self.enter_scope()
        if n.init is not None:
            if isinstance(n.init, c_ast.DeclList):
                for d in n.init.decls:
                    self.gen_stmt(d)
            else:
                self.gen_stmt(n.init)
        cont = ".for_cont_%d" % uid
        g.label(top)
        if n.cond is not None:
            self.gen_cond_false(n.cond, end)
        self.loop_stack.append((cont, end))
        self.gen_stmt_or_block(n.stmt)
        self.loop_stack.pop()
        g.label(cont)                       # `continue` runs the step, then loops
        if n.next is not None:
            self.gen_stmt(n.next)
        g.e("\tjmp %s" % top)
        g.label(end)
        self.exit_scope()

    def gen_while(self, n):
        g = self.g
        uid = g.uid()
        top = ".while_%d" % uid
        end = ".while_end_%d" % uid
        g.label(top)
        self.gen_cond_false(n.cond, end)
        self.loop_stack.append((top, end))   # continue -> re-test
        self.gen_stmt_or_block(n.stmt)
        self.loop_stack.pop()
        g.e("\tjmp %s" % top)
        g.label(end)

    def gen_do(self, n):
        g = self.g
        uid = g.uid()
        top = ".do_%d" % uid
        cont = ".do_cont_%d" % uid
        end = ".do_end_%d" % uid
        g.label(top)
        self.loop_stack.append((cont, end))  # continue -> the bottom test
        self.gen_stmt_or_block(n.stmt)
        self.loop_stack.pop()
        g.label(cont)
        self.gen_cond_true(n.cond, top)
        g.label(end)

    def gen_switch(self, n):
        g = self.g
        uid = g.uid()
        end = ".switch_end_%d" % uid
        # evaluate the controlling expression once into a callee-safe reg
        self.eval_into(n.cond, "rax", 4)
        ctrl = self.take_reg()
        if ctrl is None:
            raise Unsupported("switch: out of registers")
        g.e("\tmov %s, eax\t; switch value" % rsz(ctrl, 4))
        block = n.stmt
        items = block.block_items if isinstance(block, c_ast.Compound) else []
        # first pass: emit the dispatch (compare to each case, jump to its label)
        labels = {}
        default_lbl = None
        for idx, it in enumerate(items):
            if isinstance(it, c_ast.Case):
                lbl = ".case_%d_%d" % (uid, idx)
                labels[idx] = lbl
                cv = const_int(it.expr)
                g.e("\tcmp %s, %d" % (rsz(ctrl, 4), cv))
                g.e("\tje %s" % lbl)
            elif isinstance(it, c_ast.Default):
                default_lbl = ".default_%d_%d" % (uid, idx)
                labels[idx] = default_lbl
        g.e("\tjmp %s" % (default_lbl or end))
        self.free_regs.insert(0, ctrl)
        # second pass: emit the bodies in order (fallthrough preserved)
        self.loop_stack.append((None, end))   # break -> end; continue not valid
        for idx, it in enumerate(items):
            if isinstance(it, (c_ast.Case, c_ast.Default)):
                g.label(labels[idx])
                for s in (it.stmts or []):
                    self.gen_stmt(s)
        self.loop_stack.pop()
        g.label(end)

    def gen_stmt_or_block(self, n):
        if isinstance(n, c_ast.Compound):
            self.enter_scope()
            self.gen_block(n)
            self.exit_scope()
        else:
            self.gen_stmt(n)

    # -- calls --
    def gen_call(self, n, want_result):
        g = self.g
        fname = n.name.name if isinstance(n.name, c_ast.ID) else None
        args = n.args.exprs if n.args else []
        if fname in ("__builtin_popcount", "__builtin_popcountl"):
            self.eval_into(args[0], "rax", 4)
            g.e("\tpopcnt eax, eax\t; numara bitii de 1")
            return
        # Indirect call? The callee is a local variable holding a function
        # pointer (e.g. `f(...)` inside map), so we `call <reg>` instead of a
        # symbol. Otherwise it's a named function -> emit/record an extern.
        indirect = fname is not None and fname in self.locals
        if not indirect:
            self.g.called.add(fname)
        if len(args) > len(ARG_REGS):
            raise Unsupported(">6 call args")
        # If any arg's evaluation could clobber an already-placed arg register
        # (nested call or arithmetic using rcx/rax), marshal everything via the
        # stack; otherwise place directly for clean output.
        args_have_call = any(has_call(a) for a in args)
        complex_args = any(
            any(isinstance(x, (c_ast.BinaryOp, c_ast.FuncCall, c_ast.TernaryOp))
                for x in walk(a))
            for a in args)
        if args_have_call:
            # reserve a 16-aligned scratch area and stage each arg there, so the
            # stack stays 16-aligned even while a nested call runs mid-evaluation.
            space = (len(args) * 8 + 15) & ~15
            g.e("\tsub rsp, %d\t; stage args (keep rsp 16-aligned)" % space)
            for i, a in enumerate(args):
                self.eval_into(a, "rax", 8 if is_pointerish(a) else 4)
                g.e("\tmov [rsp + %d], rax" % (i * 8))
            for i in range(len(args)):
                g.e("\tmov %s, [rsp + %d]" % (ARG_REGS[i], i * 8))
            g.e("\tadd rsp, %d" % space)
        elif complex_args:
            # arithmetic args (no calls): push/pop is fine — no call runs until
            # everything is back in registers.
            for a in args:
                self.eval_into(a, "rax", 8 if is_pointerish(a) else 4)
                g.e("\tpush rax")
            for i in range(len(args) - 1, -1, -1):
                g.e("\tpop %s" % ARG_REGS[i])
        else:
            for i, a in enumerate(args):
                self.eval_into(a, ARG_REGS[i], 8 if is_pointerish(a) else 4)
        if fname == "printf":
            g.e("\txor eax, eax\t; al = 0 (fara argumente vectoriale)")
        if indirect:
            loc = self.locals[fname]
            if loc.mem:
                g.e("\tmov r11, [rbp-%d]\t; %s" % (loc.stack_off, fname))
                g.e("\tcall r11\t; %s(...)" % fname)
            else:
                g.e("\tcall %s\t; %s(...)" % (loc.reg, fname))
        else:
            g.e("\tcall %s" % fname)


def render_struc(layout):
    """Emit a NASM `struc NAME ... endstruc` block from a layout. This gives
    symbolic field offsets (NAME.field) and a size symbol (NAME_size)."""
    out = ["struc %s" % layout["name"]]
    for fname in layout["order"]:
        fti, _ = layout["fields"][fname]
        if fti.kind == "struct":
            out.append("\t.%s: resb %s_size" % (fname, fti.struct["name"]))
        elif fti.kind == "array":
            out.append("\t.%s: res%s %d" % (fname, sz_letter(fti.elem_w), fti.count or 0))
        elif fti.kind == "ptr":
            out.append("\t.%s: resq 1" % fname)
        else:
            out.append("\t.%s: res%s 1" % (fname, sz_letter(fti.width)))
    out.append("endstruc")
    return out


def peephole(lines):
    """Safe cleanup over generated text: drop unreachable code after an
    unconditional jmp/ret (until the next label), and obvious no-op movs."""
    out = []
    dead = False
    for line in lines:
        s = line.strip()
        is_label = s.endswith(":") and not line.startswith("\t")
        if is_label:
            dead = False
            out.append(line)
            continue
        if dead:
            continue
        # drop "mov X, X"
        m = re.match(r"mov (\w+), (\w+)$", s)
        if m and m.group(1) == m.group(2):
            continue
        out.append(line)
        if s.startswith("jmp ") or s == "ret":
            dead = True
    return out


def size_word(w):
    return {1: "byte", 2: "word", 4: "dword", 8: "qword"}[w]


def is_pointerish(a):
    return isinstance(a, (c_ast.Cast,)) or \
        (isinstance(a, c_ast.Constant) and a.type == "string") or \
        (isinstance(a, c_ast.ID))  # be generous; widened safely


# ----------------------------------------------------------------------------
def transpile(src):
    STRUCTS.clear()
    code = preprocess(src)
    ast = c_parser.CParser().parse(code, "<input>")
    register_structs(ast)
    g = Gen()
    funcs = []
    for ext in ast.ext:
        if isinstance(ext, c_ast.Decl) and not isinstance(ext.type, c_ast.FuncDecl):
            # a bare struct definition (`struct T {..};`) declares no storage
            if ext.name is None or isinstance(ext.type, c_ast.Struct):
                continue
            try:
                g.gen_global(ext)
            except Unsupported:
                pass
        elif isinstance(ext, c_ast.FuncDef):
            funcs.append(ext)
    g.func_names = {f.decl.name for f in funcs}
    for fn in funcs:
        g.gen_func(fn)

    # assemble final file
    out = []
    out.append("; generated by c2asm_human — idiomatic x86_64 NASM (System V)")
    out.append("; build/run:  ./asmrun.sh main.asm")
    out.append("")
    if g.defines:
        out.extend(g.defines)
        out.append("")
    # struct layouts (gives symbolic NAME.field offsets and NAME_size)
    for layout in g.used_structs:
        out.extend(render_struc(layout))
        out.append("")
    out.append("section .note.GNU-stack")
    out.append("")
    if g.data:
        out.append("section .data")
        out.extend(g.data)
        out.append("")
    if g.rodata:
        out.append("section .rodata")
        out.extend(g.rodata)
        out.append("")
    if g.bss:
        out.append("section .bss")
        out.extend(g.bss)
        out.append("")
    g.text = peephole(g.text)
    out.append("section .text")
    # globals + externs
    func_names = [f.decl.name for f in funcs]
    for fn in func_names:
        out.append("global %s" % fn)
    externs = (g.externs | g.called) - set(func_names)
    for e in sorted(externs):
        out.append("extern %s" % e)
    out.append("")
    out.extend(g.text)
    out.append("")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description="restricted C -> idiomatic x86_64 NASM")
    ap.add_argument("c")
    ap.add_argument("-o", "--output", default="main.asm")
    ap.add_argument("--run", action="store_true",
                    help="assemble + link + run the result on x86_64 (via asmrun.sh)")
    args = ap.parse_args()
    with open(args.c) as f:
        src = f.read()
    try:
        asm = transpile(src)
    except Unsupported as e:
        sys.exit("[c2asm_human] unsupported construct: %s\n"
                 "  -> this transpiler only covers the exam C subset; "
                 "use c2asm.py for full C." % e)
    with open(args.output, "w") as f:
        f.write(asm)
    print("[c2asm_human] wrote %s" % args.output)
    if args.run:
        here = os.path.dirname(os.path.abspath(__file__))
        sys.exit(subprocess.call([os.path.join(here, "asmrun.sh"),
                                  os.path.abspath(args.output)]))


if __name__ == "__main__":
    main()
