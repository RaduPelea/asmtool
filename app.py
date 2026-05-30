#!/usr/bin/env python3
"""
asmtool API — Flask wrapper peste asm2c / c2asm_human / c2asm
"""
import os, sys, tempfile, subprocess
from pathlib import Path
from flask import Flask, request, jsonify
from flask_cors import CORS

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

app = Flask(__name__)
CORS(app)

# ── utilitare ────────────────────────────────────────────────────────────────

def run_asm2c(asm_code: str) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        asm_file = Path(tmp) / "input.asm"
        c_file   = Path(tmp) / "input.c"
        asm_file.write_text(asm_code)
        r = subprocess.run(
            [sys.executable, str(HERE / "asm2c.py"), str(asm_file), "-o", str(c_file)],
            capture_output=True, text=True
        )
        if c_file.exists():
            return {"ok": True, "code": c_file.read_text()}
        return {"ok": False, "error": r.stderr or r.stdout}

def run_c2asm_human(c_code: str) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        c_file   = Path(tmp) / "main.c"
        asm_file = Path(tmp) / "main.asm"
        c_file.write_text(c_code)
        r = subprocess.run(
            [sys.executable, str(HERE / "c2asm_human.py"), str(c_file), "-o", str(asm_file)],
            capture_output=True, text=True
        )
        if asm_file.exists():
            return {"ok": True, "code": asm_file.read_text(), "mode": "human"}
        return {"ok": False, "error": r.stderr or r.stdout}

def run_c2asm_gcc(c_code: str) -> dict:
    objconv = str(HERE / "objconv")
    with tempfile.TemporaryDirectory() as tmp:
        c_file   = Path(tmp) / "main.c"
        asm_file = Path(tmp) / "main.asm"
        c_file.write_text(c_code)
        r = subprocess.run(
            [sys.executable, str(HERE / "c2asm.py"), str(c_file), "-o", str(asm_file)],
            capture_output=True, text=True,
            env={**os.environ, "PATH": f"{HERE}:{os.environ.get('PATH','')}"}
        )
        if asm_file.exists():
            return {"ok": True, "code": asm_file.read_text(), "mode": "gcc"}
        return {"ok": False, "error": r.stderr or r.stdout}

# ── endpoints ────────────────────────────────────────────────────────────────

@app.get("/")
def index():
    return jsonify({
        "service": "asmtool API",
        "endpoints": {
            "POST /asm-to-c":  "body: {code: '<asm>'}  → {ok, code}",
            "POST /c-to-asm":  "body: {code: '<c>', mode: 'human'|'gcc'}  → {ok, code, mode}",
            "POST /c-to-asm-auto": "incearca human, fallback la gcc automat",
        }
    })

@app.post("/asm-to-c")
def asm_to_c():
    body = request.get_json(silent=True) or {}
    code = body.get("code", "").strip()
    if not code:
        return jsonify({"ok": False, "error": "camp 'code' lipsa"}), 400
    return jsonify(run_asm2c(code))

@app.post("/c-to-asm")
def c_to_asm():
    body = request.get_json(silent=True) or {}
    code = body.get("code", "").strip()
    mode = body.get("mode", "human")
    if not code:
        return jsonify({"ok": False, "error": "camp 'code' lipsa"}), 400
    if mode == "gcc":
        return jsonify(run_c2asm_gcc(code))
    return jsonify(run_c2asm_human(code))

@app.post("/c-to-asm-auto")
def c_to_asm_auto():
    body = request.get_json(silent=True) or {}
    code = body.get("code", "").strip()
    if not code:
        return jsonify({"ok": False, "error": "camp 'code' lipsa"}), 400
    result = run_c2asm_human(code)
    if not result["ok"]:
        result = run_c2asm_gcc(code)
        result["fallback"] = True
    return jsonify(result)

# ── main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
