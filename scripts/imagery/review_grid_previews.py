"""
Lightweight local review UI for grid preview batches.

Usage:
  python scripts/review_grid_previews.py --batch-dir results/grid_previews/batch_001

Then open the printed URL in a browser. Decisions are written to:
  <batch-dir>/grid_review_decisions.csv
  <batch-dir>/grid_preview_review.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import socket
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import pandas as pd

DECISION_VALUES = {"keep", "exclude", "review", ""}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def is_wsl_environment() -> bool:
    if os.environ.get("WSL_INTEROP") or os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        return "microsoft" in Path("/proc/version").read_text(encoding="utf-8").lower()
    except Exception:  # noqa: BLE001
        return False


def get_local_ipv4_addresses() -> list[str]:
    ips: set[str] = set()
    try:
        hostname = socket.gethostname()
        for entry in socket.getaddrinfo(hostname, None, family=socket.AF_INET):
            ip = entry[4][0]
            if not ip.startswith("127."):
                ips.add(ip)
    except Exception:  # noqa: BLE001
        pass

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            ip = sock.getsockname()[0]
            if not ip.startswith("127."):
                ips.add(ip)
    except Exception:  # noqa: BLE001
        pass

    return sorted(ips)


def build_access_urls(host: str, port: int) -> list[str]:
    if host == "0.0.0.0":
        urls = [f"http://127.0.0.1:{port}"]
        urls.extend(f"http://{ip}:{port}" for ip in get_local_ipv4_addresses())
        deduped: list[str] = []
        seen: set[str] = set()
        for url in urls:
            if url not in seen:
                deduped.append(url)
                seen.add(url)
        return deduped
    return [f"http://{host}:{port}"]


class ReviewStore:
    def __init__(self, batch_dir: Path):
        self.batch_dir = batch_dir
        self.metrics_path = batch_dir / "grid_preview_metrics.csv"
        self.decisions_path = batch_dir / "grid_review_decisions.csv"
        self.review_export_path = batch_dir / "grid_preview_review.csv"

        if not self.metrics_path.exists():
            raise FileNotFoundError(f"metrics CSV not found: {self.metrics_path}")

        self.metrics_df = pd.read_csv(self.metrics_path).fillna("")
        if "grid_id" not in self.metrics_df.columns:
            raise ValueError(f"metrics CSV missing grid_id column: {self.metrics_path}")
        self.metrics_df["grid_id"] = self.metrics_df["grid_id"].astype(str)
        self.decisions = self._load_decisions()
        self._write_review_export()

    def _load_decisions(self) -> dict[str, dict[str, str]]:
        if not self.decisions_path.exists():
            return {}

        rows: dict[str, dict[str, str]] = {}
        with self.decisions_path.open("r", newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                grid_id = str(row.get("grid_id", "")).strip()
                if not grid_id:
                    continue
                rows[grid_id] = {
                    "decision": str(row.get("decision", "")).strip(),
                    "notes": str(row.get("notes", "")).strip(),
                    "updated_at": str(row.get("updated_at", "")).strip(),
                }
        return rows

    def _write_decisions(self) -> None:
        self.decisions_path.parent.mkdir(parents=True, exist_ok=True)
        with self.decisions_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(
                fh,
                fieldnames=["grid_id", "decision", "notes", "updated_at"],
            )
            writer.writeheader()
            for grid_id in sorted(self.decisions):
                row = self.decisions[grid_id]
                writer.writerow(
                    {
                        "grid_id": grid_id,
                        "decision": row.get("decision", ""),
                        "notes": row.get("notes", ""),
                        "updated_at": row.get("updated_at", ""),
                    }
                )

    def _write_review_export(self) -> None:
        decisions_df = pd.DataFrame(
            [
                {
                    "grid_id": grid_id,
                    "decision": row.get("decision", ""),
                    "notes": row.get("notes", ""),
                    "updated_at": row.get("updated_at", ""),
                }
                for grid_id, row in self.decisions.items()
            ]
        )
        merged = self.metrics_df.copy()
        if len(decisions_df) > 0:
            merged = merged.merge(decisions_df, on="grid_id", how="left")
        else:
            merged["decision"] = ""
            merged["notes"] = ""
            merged["updated_at"] = ""
        merged.to_csv(self.review_export_path, index=False)

    def get_records(self) -> list[dict[str, object]]:
        records: list[dict[str, object]] = []
        for row in self.metrics_df.to_dict(orient="records"):
            grid_id = str(row["grid_id"])
            decision_row = self.decisions.get(grid_id, {})
            row["decision"] = decision_row.get("decision", "")
            row["notes"] = decision_row.get("notes", "")
            row["updated_at"] = decision_row.get("updated_at", "")
            records.append(row)
        return sorted(records, key=lambda row: str(row["grid_id"]))

    def save_decision(self, grid_id: str, decision: str, notes: str = "") -> dict[str, str]:
        grid_id = str(grid_id).strip()
        decision = str(decision).strip().lower()
        notes = str(notes).strip()
        if decision not in DECISION_VALUES:
            raise ValueError(f"invalid decision: {decision}")
        if grid_id not in set(self.metrics_df["grid_id"]):
            raise KeyError(f"unknown grid_id: {grid_id}")

        if decision == "" and notes == "":
            self.decisions.pop(grid_id, None)
        else:
            self.decisions[grid_id] = {
                "decision": decision,
                "notes": notes,
                "updated_at": utc_now_iso(),
            }

        self._write_decisions()
        self._write_review_export()
        return self.decisions.get(grid_id, {"decision": "", "notes": "", "updated_at": ""})


def build_html() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Grid Preview Review</title>
  <style>
    :root {
      --bg: #f4f0e8;
      --panel: #fffaf2;
      --ink: #1e2328;
      --muted: #6a6f75;
      --line: #d8cdbd;
      --accent: #0f766e;
      --keep: #1d8348;
      --exclude: #c0392b;
      --review: #b9770e;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Georgia, "Times New Roman", serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, #fff8ea 0, #f4f0e8 42%, #e8e3d7 100%);
    }
    .wrap {
      max-width: 1280px;
      margin: 0 auto;
      padding: 20px;
    }
    .bar, .panel {
      background: rgba(255, 250, 242, 0.92);
      border: 1px solid var(--line);
      border-radius: 14px;
      box-shadow: 0 10px 30px rgba(60, 52, 41, 0.08);
    }
    .bar {
      display: grid;
      grid-template-columns: 1fr auto auto;
      gap: 12px;
      align-items: center;
      padding: 14px 16px;
      margin-bottom: 16px;
    }
    .title {
      font-size: 24px;
      font-weight: 700;
      letter-spacing: 0.02em;
    }
    .meta {
      color: var(--muted);
      font-size: 14px;
    }
    .controls {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }
    button, select, input {
      font: inherit;
    }
    button {
      border: 1px solid var(--line);
      border-radius: 10px;
      background: #fff;
      color: var(--ink);
      padding: 10px 14px;
      cursor: pointer;
    }
    button:hover { border-color: #bfa78c; }
    .btn-keep { border-color: color-mix(in srgb, var(--keep) 35%, white); }
    .btn-exclude { border-color: color-mix(in srgb, var(--exclude) 35%, white); }
    .btn-review { border-color: color-mix(in srgb, var(--review) 35%, white); }
    .grid {
      display: grid;
      grid-template-columns: minmax(320px, 1fr) 340px;
      gap: 16px;
    }
    .panel {
      padding: 16px;
    }
    .image-wrap {
      min-height: 70vh;
      display: flex;
      align-items: center;
      justify-content: center;
      background: linear-gradient(180deg, #f9f5ed, #f1eadf);
      border-radius: 12px;
      overflow: hidden;
    }
    img {
      max-width: 100%;
      max-height: 78vh;
      display: block;
      border-radius: 8px;
    }
    .stat {
      display: grid;
      grid-template-columns: 110px 1fr;
      gap: 8px;
      padding: 6px 0;
      border-bottom: 1px solid #efe5d6;
      font-size: 15px;
    }
    .stat:last-child { border-bottom: 0; }
    .muted { color: var(--muted); }
    .decision {
      font-size: 22px;
      font-weight: 700;
      margin: 8px 0 16px;
    }
    .decision.keep { color: var(--keep); }
    .decision.exclude { color: var(--exclude); }
    .decision.review { color: var(--review); }
    textarea {
      width: 100%;
      min-height: 92px;
      resize: vertical;
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 10px 12px;
      background: #fffdf9;
      font: inherit;
    }
    .kbd {
      display: inline-block;
      min-width: 1.7em;
      padding: 2px 6px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      text-align: center;
      font-size: 13px;
    }
    @media (max-width: 980px) {
      .bar { grid-template-columns: 1fr; }
      .grid { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="bar">
      <div>
        <div class="title">Grid Preview Review</div>
        <div class="meta" id="summary">Loading…</div>
      </div>
      <div class="controls">
        <button id="prevBtn">Prev</button>
        <button id="nextBtn">Next</button>
      </div>
      <div class="controls">
        <label class="meta" for="filterSelect">Filter</label>
        <select id="filterSelect">
          <option value="all">All</option>
          <option value="unresolved">Unresolved</option>
          <option value="keep">Keep</option>
          <option value="exclude">Exclude</option>
          <option value="review">Review</option>
          <option value="likely_blank">Likely Blank</option>
        </select>
      </div>
    </div>

    <div class="grid">
      <div class="panel">
        <div class="image-wrap">
          <img id="previewImg" alt="Grid preview">
        </div>
      </div>

      <div class="panel">
        <div class="muted">Current Grid</div>
        <div class="title" id="gridId">-</div>
        <div class="decision" id="decisionText">Unresolved</div>

        <div class="controls" style="margin-bottom: 16px;">
          <button class="btn-keep" id="keepBtn">Keep <span class="kbd">K</span></button>
          <button class="btn-exclude" id="excludeBtn">Exclude <span class="kbd">X</span></button>
          <button class="btn-review" id="reviewBtn">Review <span class="kbd">R</span></button>
          <button id="clearBtn">Clear <span class="kbd">C</span></button>
        </div>

        <div class="stat"><div class="muted">Imagery</div><div id="imageryHint">-</div></div>
        <div class="stat"><div class="muted">Valid Ratio</div><div id="validRatio">-</div></div>
        <div class="stat"><div class="muted">White Ratio</div><div id="whiteRatio">-</div></div>
        <div class="stat"><div class="muted">Status</div><div id="statusText">-</div></div>
        <div class="stat"><div class="muted">Updated</div><div id="updatedAt">-</div></div>

        <div style="margin-top: 18px; margin-bottom: 8px;" class="muted">Notes</div>
        <textarea id="notesInput" placeholder="Optional notes, for borderline coastal or mountain cases"></textarea>
        <div class="controls" style="margin-top: 10px;">
          <button id="saveNotesBtn">Save Notes</button>
        </div>

        <div style="margin-top: 18px;" class="meta">
          Shortcuts:
          <span class="kbd">K</span> keep,
          <span class="kbd">X</span> exclude,
          <span class="kbd">R</span> review,
          <span class="kbd">C</span> clear,
          <span class="kbd">←</span>/<span class="kbd">→</span> prev/next
        </div>
      </div>
    </div>
  </div>

  <script>
    let records = [];
    let visible = [];
    let currentIndex = 0;

    const summaryEl = document.getElementById("summary");
    const gridIdEl = document.getElementById("gridId");
    const decisionEl = document.getElementById("decisionText");
    const imageryHintEl = document.getElementById("imageryHint");
    const validRatioEl = document.getElementById("validRatio");
    const whiteRatioEl = document.getElementById("whiteRatio");
    const statusTextEl = document.getElementById("statusText");
    const updatedAtEl = document.getElementById("updatedAt");
    const notesInputEl = document.getElementById("notesInput");
    const previewImgEl = document.getElementById("previewImg");
    const filterSelectEl = document.getElementById("filterSelect");

    function fmtPct(value) {
      const n = Number(value);
      if (!Number.isFinite(n)) return "-";
      return `${Math.round(n * 100)}%`;
    }

    function currentRecord() {
      return visible[currentIndex] || null;
    }

    function applyFilter() {
      const mode = filterSelectEl.value;
      if (mode === "all") {
        visible = [...records];
      } else if (mode === "unresolved") {
        visible = records.filter(r => !r.decision);
      } else if (mode === "likely_blank") {
        visible = records.filter(r => r.imagery_hint === "likely_blank" || r.imagery_hint === "mostly_blank");
      } else {
        visible = records.filter(r => r.decision === mode);
      }
      currentIndex = Math.min(currentIndex, Math.max(visible.length - 1, 0));
      render();
    }

    function updateSummary() {
      const keep = records.filter(r => r.decision === "keep").length;
      const exclude = records.filter(r => r.decision === "exclude").length;
      const review = records.filter(r => r.decision === "review").length;
      const unresolved = records.length - keep - exclude - review;
      const pos = visible.length ? currentIndex + 1 : 0;
      summaryEl.textContent = `${pos}/${visible.length} in view | total ${records.length} | keep ${keep} | exclude ${exclude} | review ${review} | unresolved ${unresolved}`;
    }

    function render() {
      updateSummary();
      const row = currentRecord();
      if (!row) {
        gridIdEl.textContent = "No records";
        decisionEl.textContent = "No records";
        previewImgEl.removeAttribute("src");
        imageryHintEl.textContent = "-";
        validRatioEl.textContent = "-";
        whiteRatioEl.textContent = "-";
        statusTextEl.textContent = "-";
        updatedAtEl.textContent = "-";
        notesInputEl.value = "";
        return;
      }

      gridIdEl.textContent = row.grid_id;
      decisionEl.textContent = row.decision || "Unresolved";
      decisionEl.className = `decision ${row.decision || ""}`;
      imageryHintEl.textContent = row.imagery_hint || "-";
      validRatioEl.textContent = fmtPct(row.valid_imagery_ratio);
      whiteRatioEl.textContent = fmtPct(row.white_ratio);
      statusTextEl.textContent = row.status || "-";
      updatedAtEl.textContent = row.updated_at || "-";
      notesInputEl.value = row.notes || "";
      previewImgEl.src = `/previews/${encodeURIComponent(PathBasename(row.preview_path))}`;
      previewImgEl.alt = row.grid_id;
    }

    function PathBasename(path) {
      const parts = String(path || "").split("/");
      return parts[parts.length - 1] || "";
    }

    async function loadData() {
      const resp = await fetch("/api/data");
      records = await resp.json();
      visible = [...records];
      render();
    }

    async function saveDecision(decision) {
      const row = currentRecord();
      if (!row) return;
      const resp = await fetch("/api/decision", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          grid_id: row.grid_id,
          decision,
          notes: notesInputEl.value
        })
      });
      const payload = await resp.json();
      if (!resp.ok) {
        alert(payload.error || "Failed to save");
        return;
      }
      row.decision = payload.decision || "";
      row.notes = payload.notes || "";
      row.updated_at = payload.updated_at || "";
      render();
    }

    function move(delta) {
      if (!visible.length) return;
      currentIndex = Math.max(0, Math.min(visible.length - 1, currentIndex + delta));
      render();
    }

    document.getElementById("prevBtn").addEventListener("click", () => move(-1));
    document.getElementById("nextBtn").addEventListener("click", () => move(1));
    document.getElementById("keepBtn").addEventListener("click", () => saveDecision("keep"));
    document.getElementById("excludeBtn").addEventListener("click", () => saveDecision("exclude"));
    document.getElementById("reviewBtn").addEventListener("click", () => saveDecision("review"));
    document.getElementById("clearBtn").addEventListener("click", () => saveDecision(""));
    document.getElementById("saveNotesBtn").addEventListener("click", () => {
      const row = currentRecord();
      saveDecision(row ? row.decision || "" : "");
    });
    filterSelectEl.addEventListener("change", applyFilter);

    document.addEventListener("keydown", (event) => {
      const tag = document.activeElement ? document.activeElement.tagName : "";
      if (tag === "TEXTAREA" || tag === "INPUT") return;
      if (event.key === "ArrowLeft") move(-1);
      if (event.key === "ArrowRight") move(1);
      if (event.key === "k" || event.key === "K") saveDecision("keep");
      if (event.key === "x" || event.key === "X") saveDecision("exclude");
      if (event.key === "r" || event.key === "R") saveDecision("review");
      if (event.key === "c" || event.key === "C") saveDecision("");
    });

    loadData();
  </script>
</body>
</html>
"""


def make_handler(store: ReviewStore):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:  # noqa: A003
            return

        def _send_json(self, payload: object, status: int = 200) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_text(self, body: str, status: int = 200) -> None:
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _serve_preview(self, image_name: str) -> None:
            image_name = Path(unquote(image_name)).name
            image_path = store.batch_dir / "previews" / image_name
            if not image_path.exists():
                self.send_error(HTTPStatus.NOT_FOUND, "Preview not found")
                return
            data = image_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/":
                self._send_text(build_html())
                return
            if parsed.path == "/api/data":
                query = parse_qs(parsed.query)
                records = store.get_records()
                if query.get("decision"):
                    wanted = query["decision"][0]
                    records = [r for r in records if str(r.get("decision", "")) == wanted]
                self._send_json(records)
                return
            if parsed.path.startswith("/previews/"):
                self._serve_preview(parsed.path[len("/previews/"):])
                return
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path != "/api/decision":
                self.send_error(HTTPStatus.NOT_FOUND, "Not found")
                return

            content_length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(content_length)
            try:
                payload = json.loads(raw.decode("utf-8"))
                result = store.save_decision(
                    grid_id=payload.get("grid_id", ""),
                    decision=payload.get("decision", ""),
                    notes=payload.get("notes", ""),
                )
                self._send_json(result)
            except Exception as exc:  # noqa: BLE001
                self._send_json({"error": str(exc)}, status=400)

    return Handler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local browser review UI for grid preview batches")
    parser.add_argument("--batch-dir", required=True, help="Batch directory, e.g. results/grid_previews/batch_001")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind")
    parser.add_argument("--port", type=int, default=8765, help="Port to bind")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    batch_dir = Path(args.batch_dir).resolve()
    store = ReviewStore(batch_dir)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(store))
    urls = build_access_urls(args.host, args.port)

    print(f"[REVIEW] batch_dir={batch_dir}")
    for url in urls:
        print(f"[OPEN] {url}")
    if is_wsl_environment() and args.host in {"127.0.0.1", "localhost"}:
        print("[NOTE] WSL detected. Windows browser may not reach this loopback binding.")
        print(f"[NOTE] Try: python scripts/review_grid_previews.py --batch-dir {batch_dir} --host 0.0.0.0 --port {args.port}")
    elif is_wsl_environment() and args.host == "0.0.0.0":
        lan_urls = [url for url in urls if "127.0.0.1" not in url]
        if lan_urls:
            print(f"[NOTE] From Windows, try {lan_urls[0]}")
    print(f"[SAVE] {store.decisions_path}")
    print(f"[MERGED] {store.review_export_path}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
