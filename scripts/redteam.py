"""Jolly red-team: run the attack corpus (evals/redteam.yaml) against the guards.

Offline (default): everything runs in-process against a fresh in-memory app,
no API keys, no network.
  - doc  -> uploaded to case 1042 as an untrusted document; pass if flagged at ingestion
  - ask  -> POST /cases/{case}/ask; pass if refused (or answered, for benign controls)
  - out  -> guard.check_output(); pass if blocked (or allowed, for benign controls)

Live:  uv run python scripts/redteam.py --live https://jolly.example.com [--secret S]
  sends the `ask` cases to {URL}/bolna/tools/ask-case-question with the
  X-Jolly-Secret header (JOLLY_SECRET env var also works). doc/out stay offline.

Usage: uv run python scripts/redteam.py [--split tuned|heldout|all] [--json out.json]
Exit code is always 0 unless --strict is given (then 1 on any failure).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for _k in ("LLM_API_KEY", "EMBEDDING_API_KEY", "GEMINI_API_KEY"):
    os.environ.pop(_k, None)

CORPUS = ROOT / "evals" / "redteam.yaml"


def load_corpus(path: Path = CORPUS) -> list[dict]:
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8"))["attacks"]


def _app_client():
    from fastapi.testclient import TestClient

    from backend.core.config import Settings
    from backend.main import create_app

    return TestClient(create_app(Settings(database_url="sqlite://")))


def _offline_doc(client, text: str) -> str:
    r = client.post("/cases/1042/documents", json={"title": "Red-team upload", "kind": "correspondence", "text": text})
    return "block" if r.json().get("flagged_paragraphs") else "allow"


def _offline_ask(client, text: str, case: str) -> tuple[str, str]:
    d = client.post(f"/cases/{case}/ask", json={"question": text}).json()
    return ("refuse" if d.get("mode") == "refused" else "allow"), (d.get("answer") or "")[:100]


def _offline_out(text: str) -> str:
    from backend.services import guard

    check = getattr(guard, "check_output", None)
    if check is None:  # guard v1 had no output check
        return "allow"
    return "block" if check(text, record="") else "allow"


def _live_ask(url: str, secret: str, text: str, case: str) -> tuple[str, str]:
    import urllib.request

    req = urllib.request.Request(
        url.rstrip("/") + "/bolna/tools/ask-case-question", method="POST",
        data=json.dumps({"case_id": case, "question": text}).encode(),
        headers={"Content-Type": "application/json", "X-Jolly-Secret": secret},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        r = json.load(resp)
    return ("refuse" if r.get("mode") == "refused" else "allow"), (r.get("spoken_answer") or "")[:100]


def run(split: str = "all", live_url: str | None = None, secret: str = "", corpus: list[dict] | None = None) -> list[dict]:
    corpus = corpus if corpus is not None else load_corpus()
    client = _app_client()
    rows = []
    for a in corpus:
        if split != "all" and a["split"] != split:
            continue
        case = a.get("case", "1042")
        said = ""
        if a["channel"] == "doc":
            got = _offline_doc(client, a["text"])
        elif a["channel"] == "out":
            got = _offline_out(a["text"])
        elif live_url:
            got, said = _live_ask(live_url, secret, a["text"], case)
        else:
            got, said = _offline_ask(client, a["text"], case)
        exp = a["expect"]
        ok = got == exp or (exp == "authz" and got == "refuse")
        rows.append({"id": a["id"], "split": a["split"], "channel": a["channel"], "expect": exp, "got": got,
                     "pass": ok, "text": a["text"], "said": said})
    return rows


def summarise(rows: list[dict]) -> dict:
    out = {}
    for split in ("tuned", "heldout"):
        rs = [r for r in rows if r["split"] == split]
        if not rs:
            continue
        attacks = [r for r in rs if r["expect"] != "allow"]
        benign = [r for r in rs if r["expect"] == "allow"]
        out[split] = {
            "pass": sum(r["pass"] for r in rs), "total": len(rs),
            "attacks_caught": sum(r["pass"] for r in attacks), "attacks": len(attacks),
            "benign_kept": sum(r["pass"] for r in benign), "benign": len(benign),
        }
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="all", choices=["tuned", "heldout", "all"])
    ap.add_argument("--live", metavar="URL", default=None)
    ap.add_argument("--secret", default=os.environ.get("JOLLY_SECRET", ""))
    ap.add_argument("--json", default=None)
    ap.add_argument("--strict", action="store_true")
    a = ap.parse_args()
    rows = run(a.split, a.live, a.secret)
    for r in rows:
        mark = "PASS" if r["pass"] else "FAIL"
        print(f"{r['id']:9} {r['split']:7} {r['channel']:3} expect={r['expect']:6} got={r['got']:6} {mark}  {r['text'][:60]}")
    s = summarise(rows)
    print()
    for split, v in s.items():
        print(f"{split:7}: {v['pass']}/{v['total']}  (attacks caught {v['attacks_caught']}/{v['attacks']}, "
              f"benign kept {v['benign_kept']}/{v['benign']})")
    if a.json:
        Path(a.json).write_text(json.dumps({"summary": s, "rows": rows}, ensure_ascii=False, indent=2))
    return 1 if a.strict and not all(r["pass"] for r in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
