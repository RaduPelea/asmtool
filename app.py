#!/usr/bin/env python3
import os, sys, tempfile, subprocess, json
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

app = Flask(__name__, static_folder=str(HERE), static_url_path="")
CORS(app)

OBJCONV = HERE / "objconv"


# ── utilitare ────────────────────────────────────────────────────────────────

def run(cmd: list, cwd=None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)


def asm2c(code: str) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        asm = Path(tmp) / "in.asm"
        out = Path(tmp) / "in.c"
        asm.write_text(code)
        r = run([sys.executable, str(HERE / "asm2c.py"), str(asm), "-o", str(out)])
        if out.exists():
            return {"ok": True, "code": out.read_text()}
        return {"ok": False, "error": (r.stderr or r.stdout).strip()}


def c2asm_human(code: str) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        c   = Path(tmp) / "main.c"
        out = Path(tmp) / "main.asm"
        c.write_text(code)
        r = run([sys.executable, str(HERE / "c2asm_human.py"), str(c), "-o", str(out)])
        if out.exists():
            return {"ok": True, "code": out.read_text(), "mode": "human"}
        return {"ok": False, "error": (r.stderr or r.stdout).strip()}


def c2asm_gcc(code: str) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        c   = Path(tmp) / "main.c"
        obj = Path(tmp) / "main.o"
        out = Path(tmp) / "main.asm"
        c.write_text(code)

        # 1) Compileaza cu gcc
        r = run(["gcc", "-m64", "-no-pie", "-fno-pie", "-O0",
                 "-fno-asynchronous-unwind-tables", "-fno-stack-protector",
                 "-fcf-protection=none", "-c", str(c), "-o", str(obj)])
        if r.returncode != 0:
            return {"ok": False, "error": "gcc: " + (r.stderr or r.stdout).strip()}

        # 2) Converteste cu objconv
        r = run([str(OBJCONV), "-fnasm", str(obj), str(out)])
        if out.exists():
            return {"ok": True, "code": out.read_text(), "mode": "gcc"}
        return {"ok": False, "error": "objconv: " + (r.stderr or r.stdout).strip()}


# ── routes ───────────────────────────────────────────────────────────────────

@app.get("/")
def index():
    return send_from_directory(str(HERE), "index.html")


@app.post("/api/asm_to_c")
def route_asm2c():
    code = (request.get_json(silent=True) or {}).get("code", "").strip()
    if not code:
        return jsonify({"ok": False, "error": "missing code"}), 400
    return jsonify(asm2c(code))


@app.post("/api/c_to_asm")
def route_c2asm():
    body = request.get_json(silent=True) or {}
    code = body.get("code", "").strip()
    mode = body.get("mode", "human")
    if not code:
        return jsonify({"ok": False, "error": "missing code"}), 400
    result = c2asm_gcc(code) if mode == "gcc" else c2asm_human(code)
    return jsonify(result)


@app.post("/api/c_to_asm_auto")
def route_c2asm_auto():
    code = (request.get_json(silent=True) or {}).get("code", "").strip()
    if not code:
        return jsonify({"ok": False, "error": "missing code"}), 400
    result = c2asm_human(code)
    if not result["ok"]:
        result = c2asm_gcc(code)
        if result["ok"]:
            result["fallback"] = True
    return jsonify(result)


# ── main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
