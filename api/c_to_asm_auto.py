import json, sys, tempfile, subprocess
from pathlib import Path
from http.server import BaseHTTPRequestHandler

ROOT = Path(__file__).parent.parent


def human(code, tmp):
    c = Path(tmp) / "main.c"; out = Path(tmp) / "main.asm"
    c.write_text(code)
    r = subprocess.run([sys.executable, str(ROOT / "c2asm_human.py"), str(c), "-o", str(out)],
                       capture_output=True, text=True)
    return ({"ok": True, "code": out.read_text(), "mode": "human"} if out.exists()
            else {"ok": False, "error": (r.stderr or r.stdout).strip()})


def gcc(code, tmp):
    c = Path(tmp) / "main2.c"; out = Path(tmp) / "main2.asm"
    c.write_text(code)
    r = subprocess.run([sys.executable, str(ROOT / "c2asm.py"), str(c), "-o", str(out)],
                       capture_output=True, text=True)
    return ({"ok": True, "code": out.read_text(), "mode": "gcc"} if out.exists()
            else {"ok": False, "error": (r.stderr or r.stdout).strip()})


def convert(code: str) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        result = human(code, tmp)
        if not result["ok"]:
            result = gcc(code, tmp)
            if result["ok"]:
                result["fallback"] = True
        return result


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(200); self._cors(); self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}
        code = body.get("code", "").strip()
        if not code:
            self._respond(400, {"ok": False, "error": "lipseste codul"}); return
        self._respond(200, convert(code))

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _respond(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._cors(); self.end_headers(); self.wfile.write(body)

    def log_message(self, *_):
        pass
