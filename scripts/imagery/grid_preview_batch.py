"""
Download low-resolution whole-grid preview images in batches for manual triage.

This is meant for fast screening before full tile downloads:
  - fetch one overview image per grid from the Cape Town aerial WMS
  - compute a simple non-white imagery ratio
  - save per-grid previews plus contact sheets for quick visual review

Example:
  python scripts/grid_preview_batch.py --batch-index 1 --batch-size 100
  python scripts/grid_preview_batch.py --start-grid-id G1240 --batch-index 1 --batch-size 100
  python scripts/grid_preview_batch.py --grid-ids G1189 G1190 G1238
"""

from __future__ import annotations

import argparse
import math
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import numpy as np
import pandas as pd
import requests
from PIL import Image, ImageDraw, ImageOps

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from core.grid_utils import get_task_grid, normalize_grid_id

WMS_URL = "https://cityimg.capetown.gov.za/erdas-iws/ogc/wms/GeoSpatial Datasets"
WMS_LAYER = "Aerial Imagery_Aerial Imagery 2025Jan"
WMS_FORMAT = "image/jpeg"
DEFAULT_TIMEOUT = 120
DEFAULT_BATCH_SIZE = 100
DEFAULT_PREVIEW_SIZE = 512
DEFAULT_WHITE_THRESHOLD = 245
DEFAULT_WORKERS = 6


@dataclass(frozen=True)
class PreviewJob:
    grid_id: str
    bbox: tuple[float, float, float, float]


def load_grid_jobs(
    grid_ids: list[str] | None,
    start_grid_id: str | None,
    end_grid_id: str | None,
) -> list[PreviewJob]:
    task_grid = get_task_grid().copy()
    task_grid["gridcell_id"] = task_grid["gridcell_id"].astype(str).map(normalize_grid_id)
    task_grid = task_grid.sort_values("gridcell_id").reset_index(drop=True)

    if grid_ids:
        wanted = {normalize_grid_id(grid_id) for grid_id in grid_ids}
        task_grid = task_grid[task_grid["gridcell_id"].isin(wanted)].copy()
        missing = sorted(wanted - set(task_grid["gridcell_id"]))
        if missing:
            raise KeyError(f"grid_id not found in task_grid.gpkg: {', '.join(missing)}")
    else:
        if start_grid_id:
            start_grid_id = normalize_grid_id(start_grid_id)
            task_grid = task_grid[task_grid["gridcell_id"] >= start_grid_id].copy()
        if end_grid_id:
            end_grid_id = normalize_grid_id(end_grid_id)
            task_grid = task_grid[task_grid["gridcell_id"] <= end_grid_id].copy()

    jobs: list[PreviewJob] = []
    for row in task_grid.itertuples():
        xmin, ymin, xmax, ymax = row.geometry.bounds
        jobs.append(PreviewJob(grid_id=row.gridcell_id, bbox=(xmin, ymin, xmax, ymax)))
    return jobs


def select_batch(jobs: list[PreviewJob], batch_index: int, batch_size: int) -> tuple[list[PreviewJob], int, int]:
    if batch_index < 1:
        raise ValueError("--batch-index must be 1 or larger")
    total_batches = max(1, math.ceil(len(jobs) / batch_size))
    if batch_index > total_batches:
        raise ValueError(f"--batch-index {batch_index} exceeds total batches {total_batches}")
    start = (batch_index - 1) * batch_size
    end = start + batch_size
    return jobs[start:end], start, total_batches


def fetch_preview(job: PreviewJob, width: int, height: int, timeout: int) -> Image.Image:
    xmin, ymin, xmax, ymax = job.bbox
    params = {
        "service": "WMS",
        "version": "1.1.1",
        "request": "GetMap",
        "layers": WMS_LAYER,
        "srs": "EPSG:4326",
        "bbox": f"{xmin},{ymin},{xmax},{ymax}",
        "width": width,
        "height": height,
        "format": WMS_FORMAT,
        "styles": "",
    }
    response = requests.get(WMS_URL, params=params, timeout=timeout)
    response.raise_for_status()

    content_type = response.headers.get("Content-Type", "")
    if "xml" in content_type.lower() or "html" in content_type.lower():
        raise RuntimeError(f"WMS returned non-image response: {content_type}")

    return Image.open(BytesIO(response.content)).convert("RGB")


def compute_imagery_metrics(image: Image.Image, white_threshold: int) -> dict[str, float]:
    arr = np.asarray(image, dtype=np.uint8)
    white_mask = np.all(arr >= white_threshold, axis=2)
    valid_ratio = 1.0 - float(white_mask.mean())
    mean_brightness = float(arr.mean())
    return {
        "valid_imagery_ratio": valid_ratio,
        "white_ratio": 1.0 - valid_ratio,
        "mean_brightness": mean_brightness,
    }


def imagery_hint(valid_ratio: float) -> str:
    if valid_ratio < 0.05:
        return "likely_blank"
    if valid_ratio < 0.25:
        return "mostly_blank"
    if valid_ratio < 0.60:
        return "partial"
    return "substantial"


def annotate_thumbnail(
    image: Image.Image,
    grid_id: str,
    valid_ratio: float,
    thumb_size: int,
) -> Image.Image:
    thumb = ImageOps.contain(image, (thumb_size, thumb_size))
    canvas = Image.new("RGB", (thumb_size, thumb_size + 42), color=(255, 255, 255))
    x = (thumb_size - thumb.width) // 2
    y = (thumb_size - thumb.height) // 2
    canvas.paste(thumb, (x, y))

    draw = ImageDraw.Draw(canvas)
    label = f"{grid_id}  valid={valid_ratio:.0%}"
    draw.rectangle((0, thumb_size, thumb_size, thumb_size + 42), fill=(248, 248, 248))
    draw.text((8, thumb_size + 12), label, fill=(20, 20, 20))
    return canvas


def write_contact_sheet(
    rows: list[dict[str, object]],
    out_path: Path,
    thumb_size: int,
    columns: int,
) -> None:
    if not rows:
        return

    thumbs = [row["thumb"] for row in rows if row.get("thumb") is not None]
    if not thumbs:
        return

    columns = max(1, columns)
    cell_w, cell_h = thumbs[0].size
    n_rows = math.ceil(len(thumbs) / columns)
    sheet = Image.new("RGB", (columns * cell_w, n_rows * cell_h), color=(255, 255, 255))

    for idx, thumb in enumerate(thumbs):
        x = (idx % columns) * cell_w
        y = (idx // columns) * cell_h
        sheet.paste(thumb, (x, y))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out_path, quality=92)


def process_job(
    job: PreviewJob,
    preview_dir: Path,
    width: int,
    height: int,
    white_threshold: int,
    timeout: int,
    retries: int,
    refresh: bool,
) -> dict[str, object]:
    preview_path = preview_dir / f"{job.grid_id}.jpg"
    attempts = retries + 1
    last_error = None

    for attempt in range(1, attempts + 1):
        try:
            if preview_path.exists() and not refresh:
                image = Image.open(preview_path).convert("RGB")
            else:
                image = fetch_preview(job, width=width, height=height, timeout=timeout)
                preview_dir.mkdir(parents=True, exist_ok=True)
                image.save(preview_path, quality=92)

            metrics = compute_imagery_metrics(image, white_threshold=white_threshold)
            return {
                "grid_id": job.grid_id,
                "preview_path": str(preview_path.relative_to(preview_dir.parent)),
                "status": "ok",
                **metrics,
                "imagery_hint": imagery_hint(metrics["valid_imagery_ratio"]),
                "image": image,
                "error": "",
            }
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            time.sleep(min(2 * attempt, 5))

    return {
        "grid_id": job.grid_id,
        "preview_path": str(preview_path.relative_to(preview_dir.parent)),
        "status": "error",
        "valid_imagery_ratio": 0.0,
        "white_ratio": 1.0,
        "mean_brightness": 255.0,
        "imagery_hint": "error",
        "image": None,
        "error": last_error or "unknown error",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Batch whole-grid preview downloader for manual triage")
    parser.add_argument("--grid-ids", nargs="+", help="Explicit grid IDs to preview")
    parser.add_argument("--start-grid-id", help="Inclusive lower bound when scanning ordered grid IDs")
    parser.add_argument("--end-grid-id", help="Inclusive upper bound when scanning ordered grid IDs")
    parser.add_argument("--batch-index", type=int, default=1, help="1-based batch index")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help="Grids per batch")
    parser.add_argument("--preview-size", type=int, default=DEFAULT_PREVIEW_SIZE, help="WMS preview width/height")
    parser.add_argument("--thumb-size", type=int, default=220, help="Thumbnail size in contact sheet")
    parser.add_argument("--columns", type=int, default=5, help="Contact sheet columns")
    parser.add_argument("--white-threshold", type=int, default=DEFAULT_WHITE_THRESHOLD, help="White pixel threshold")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="Per-request timeout seconds")
    parser.add_argument("--retries", type=int, default=1, help="Retries after the first attempt")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="Parallel WMS requests")
    parser.add_argument(
        "--output-dir",
        default="results/grid_previews",
        help="Output root for preview batches",
    )
    parser.add_argument("--refresh", action="store_true", help="Redownload existing previews")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    jobs = load_grid_jobs(
        grid_ids=args.grid_ids,
        start_grid_id=args.start_grid_id,
        end_grid_id=args.end_grid_id,
    )
    if not jobs:
        raise RuntimeError("No grids selected")

    if args.grid_ids:
        batch_jobs = jobs
        batch_label = "custom"
        total_batches = 1
        start_idx = 0
    else:
        batch_jobs, start_idx, total_batches = select_batch(
            jobs,
            batch_index=args.batch_index,
            batch_size=args.batch_size,
        )
        batch_label = f"batch_{args.batch_index:03d}"

    output_root = Path(args.output_dir)
    batch_dir = output_root / batch_label
    preview_dir = batch_dir / "previews"

    print(f"[BATCH] {batch_label}: {len(batch_jobs)} grid(s)")
    if not args.grid_ids:
        print(f"[BATCH] total_batches={total_batches}, start_offset={start_idx}")
    print(f"[WMS] layer={WMS_LAYER}")
    print(f"[OUT] {batch_dir}")

    rows: list[dict[str, object]] = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {
            executor.submit(
                process_job,
                job,
                preview_dir,
                args.preview_size,
                args.preview_size,
                args.white_threshold,
                args.timeout,
                args.retries,
                args.refresh,
            ): job
            for job in batch_jobs
        }
        for idx, future in enumerate(as_completed(futures), start=1):
            result = future.result()
            rows.append(result)
            if result["status"] == "ok":
                print(
                    f"[{idx}/{len(batch_jobs)}] {result['grid_id']} "
                    f"valid={result['valid_imagery_ratio']:.1%} hint={result['imagery_hint']}"
                )
            else:
                print(f"[{idx}/{len(batch_jobs)}] {result['grid_id']} error={result['error']}")

    rows.sort(key=lambda row: row["grid_id"])
    for row in rows:
        image = row.pop("image")
        if image is not None:
            row["thumb"] = annotate_thumbnail(
                image=image,
                grid_id=str(row["grid_id"]),
                valid_ratio=float(row["valid_imagery_ratio"]),
                thumb_size=args.thumb_size,
            )
        else:
            row["thumb"] = None

    metrics_df = pd.DataFrame(
        [
            {
                "grid_id": row["grid_id"],
                "status": row["status"],
                "valid_imagery_ratio": row["valid_imagery_ratio"],
                "white_ratio": row["white_ratio"],
                "mean_brightness": row["mean_brightness"],
                "imagery_hint": row["imagery_hint"],
                "preview_path": row["preview_path"],
                "error": row["error"],
            }
            for row in rows
        ]
    ).sort_values(["grid_id"], ascending=[True])

    batch_dir.mkdir(parents=True, exist_ok=True)
    metrics_path = batch_dir / "grid_preview_metrics.csv"
    metrics_df.to_csv(metrics_path, index=False)

    write_contact_sheet(
        rows=rows,
        out_path=batch_dir / "contact_sheet_by_grid.jpg",
        thumb_size=args.thumb_size,
        columns=args.columns,
    )
    rows_by_ratio = sorted(
        rows,
        key=lambda row: (row["status"] != "ok", row["valid_imagery_ratio"], row["grid_id"]),
    )
    write_contact_sheet(
        rows=rows_by_ratio,
        out_path=batch_dir / "contact_sheet_by_valid_ratio.jpg",
        thumb_size=args.thumb_size,
        columns=args.columns,
    )

    ok_count = int((metrics_df["status"] == "ok").sum())
    likely_blank = int((metrics_df["imagery_hint"] == "likely_blank").sum())
    mostly_blank = int((metrics_df["imagery_hint"] == "mostly_blank").sum())
    print(f"[DONE] ok={ok_count}/{len(metrics_df)} likely_blank={likely_blank} mostly_blank={mostly_blank}")
    print(f"[CSV] {metrics_path}")


if __name__ == "__main__":
    main()
