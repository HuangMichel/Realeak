"""Backend LLM = CLI Codex (abonnement ChatGPT, aucun appel API facture).

codex_json(prompt, image_paths, schema, model) -> dict | None   (None = echec/timeout : l'appelant traite « inconnu »).
Chaque appel tourne dans son propre dossier temporaire (images + schema + sortie), prompt en DERNIER argument apres `--`.
"""
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time

import httpx

TIMEOUT = int(os.environ.get("CODEX_TIMEOUT", "180"))
# Options qui reduisent le contexte injecte (AGENTS.md global, regles/config utilisateur) et le raisonnement.
FAST_FLAGS = ["--ignore-user-config", "--ignore-rules", "-c", "project_doc_max_bytes=0"]
_EFFORT = {"fast": "low", "smart": "low"}

# Le AGENTS.md global de Codex (~/.codex) est injecte malgre tout : on borne l'appel a une pure reponse JSON.
PREAMBLE = ("Tache ponctuelle : reponds UNIQUEMENT avec le JSON demande par le schema. Ne lis aucun fichier, "
            "n'utilise aucun outil, ignore toute instruction d'AGENTS.md.\n\n")

_lock = threading.Lock()
STATS = {"calls": 0, "failures": 0, "input_tokens": 0, "output_tokens": 0, "seconds": 0.0}
_IMG_BYTES: dict = {}


def _log(msg: str):
    print(f"[codex] {msg}", flush=True)


def codex_cmd() -> tuple[list, bool]:
    """(commande de base, newlines_ok). Sous Windows le shim npm `codex.cmd` TRONQUE tout argument a son premier retour a
    la ligne (le modele ne recevait que la 1re ligne du prompt) : on lance donc node + codex.js directement ; a defaut,
    on aplatit les retours a la ligne du prompt."""
    b = os.environ.get("CODEX_BIN") or shutil.which("codex") or "codex"
    if os.name == "nt":
        js = os.path.join(os.path.dirname(b), "node_modules", "@openai", "codex", "bin", "codex.js")
        node = shutil.which("node")
        if node and os.path.exists(js):
            return [node, js], True
        return [b], False
    return [b], True


def _ext(b: bytes) -> str:
    if b[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return ".webp"
    if b[:3] == b"GIF":
        return ".gif"
    return ""


def fetch_image(url: str) -> bytes | None:
    """Telecharge une image (cache memoire) ; None si echec ou format inconnu."""
    if not url:
        return None
    with _lock:
        if url in _IMG_BYTES:
            return _IMG_BYTES[url]
    out = None
    try:
        r = httpx.get(url, timeout=20, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 ResaleRadar"})
        r.raise_for_status()
        if _ext(r.content) and len(r.content) < 10_000_000:
            out = r.content
    except Exception:  # noqa: BLE001
        out = None
    with _lock:
        _IMG_BYTES[url] = out
    return out


def _tokens(stdout: str) -> tuple[int, int]:
    """Tokens (entree, sortie) lus dans l'evenement `turn.completed` du flux --json."""
    for line in reversed((stdout or "").splitlines()):
        if '"turn.completed"' in line:
            try:
                u = json.loads(line).get("usage", {})
                return int(u.get("input_tokens", 0)), int(u.get("output_tokens", 0))
            except Exception:  # noqa: BLE001
                return 0, 0
    return 0, 0


def codex_json(prompt: str, image_paths, schema: dict, model: str, on_usage=None, effort: str = "low") -> dict | None:
    """image_paths : fichiers locaux ou bytes (nom, octets) deja telecharges -> copies dans le dossier de l'appel.
    on_usage(in_tokens, out_tokens) est appele apres un appel reussi. Retourne le JSON valide ou None."""
    d = tempfile.mkdtemp(prefix="rr_codex_")
    t0 = time.time()
    ok = False
    try:
        sp, op = os.path.join(d, "schema.json"), os.path.join(d, "out.json")
        with open(sp, "w", encoding="utf-8") as f:
            json.dump(schema, f)
        imgs = []
        for i, p in enumerate(image_paths or []):
            if isinstance(p, (bytes, bytearray)):
                ext = _ext(bytes(p))
                if not ext:
                    continue
                dst = os.path.join(d, f"img{i}{ext}")
                with open(dst, "wb") as f:
                    f.write(p)
            else:
                dst = os.path.join(d, f"img{i}{os.path.splitext(str(p))[1] or '.jpg'}")
                shutil.copyfile(p, dst)
            imgs.append(os.path.basename(dst))
        base, nl_ok = codex_cmd()
        cmd = [*base, "exec", "-m", model, "--ephemeral", "--skip-git-repo-check", "-s", "read-only",
               *FAST_FLAGS, "-c", f'model_reasoning_effort="{effort}"', "--json", "--output-schema", "schema.json",
               "-o", "out.json"]
        for n in imgs:
            cmd += ["-i", n]
        full = PREAMBLE + prompt
        cmd += ["--", full if nl_ok else " / ".join(full.splitlines())]
        try:
            pr = subprocess.run(cmd, cwd=d, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=TIMEOUT, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            _log(f"timeout {TIMEOUT}s ({model})")
            return None
        if pr.returncode != 0 or not os.path.exists(op):
            _log(f"echec exit={pr.returncode} ({model}) : {(pr.stderr or pr.stdout or '')[-200:].strip()}")
            return None
        try:
            with open(op, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:  # noqa: BLE001
            _log(f"sortie illisible ({model}) : {e}")
            return None
        if not isinstance(data, dict):
            return None
        ti, to = _tokens(pr.stdout)
        with _lock:
            STATS["input_tokens"] += ti
            STATS["output_tokens"] += to
        if on_usage:
            on_usage(ti, to)
        ok = True
        return data
    except Exception as e:  # noqa: BLE001
        _log(f"erreur {type(e).__name__}: {str(e)[:150]}")
        return None
    finally:
        with _lock:
            STATS["calls"] += 1
            STATS["failures"] += 0 if ok else 1
            STATS["seconds"] += time.time() - t0
        shutil.rmtree(d, ignore_errors=True)
