#!/usr/bin/env python3
"""Download Johannesburg 2023 aerial imagery tiles for the johnberg area."""

from __future__ import annotations

import argparse
import json
import math
import os
import struct
import time
import urllib.parse
import urllib.request
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = PROJECT_ROOT / "johnberg"
USER_AGENT = "Mozilla/5.0"
SAFE_MAX_WIDTH = 2500
SAFE_MAX_HEIGHT = 2500

TYPE_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 11: 4, 12: 8}

DATASETS = {
    "aerial_2023": {
        "service_url": "https://ags.joburg.org.za/server/rest/services/AerialPhotography/2023/ImageServer",
        "output_dir": SOURCE_DIR / "official_aerial_2023_fullres",
        "prefix": "jhb_2023_aerial",
    },
    "satellite_2025": {
        "service_url": "https://ags.joburg.org.za/server/rest/services/SatelliteImagery/2025/ImageServer",
        "output_dir": SOURCE_DIR / "official_satellite_2025_fullres",
        "prefix": "jhb_2025_satellite",
    },
}


def read_tiff_tags(path: Path) -> dict[int, tuple]:
    with path.open("rb") as f:
        endian = f.read(2)
        if endian == b"II":
            order = "<"
        elif endian == b"MM":
            order = ">"
        else:
            raise ValueError(f"{path} is not a TIFF")

        version = struct.unpack(order + "H", f.read(2))[0]
        big_tiff = version == 43
        if version not in (42, 43):
            raise ValueError(f"{path} has unsupported TIFF version {version}")

        if big_tiff:
            f.read(4)
            first_ifd = struct.unpack(order + "Q", f.read(8))[0]
            f.seek(first_ifd)
            num_entries = struct.unpack(order + "Q", f.read(8))[0]
        else:
            first_ifd = struct.unpack(order + "I", f.read(4))[0]
            f.seek(first_ifd)
            num_entries = struct.unpack(order + "H", f.read(2))[0]

        tags: dict[int, tuple] = {}
        for _ in range(num_entries):
            if big_tiff:
                tag, field_type = struct.unpack(order + "HH", f.read(4))
                count = struct.unpack(order + "Q", f.read(8))[0]
                value_or_offset = f.read(8)
                inline_size = 8
            else:
                tag, field_type, count = struct.unpack(order + "HHI", f.read(8))
                value_or_offset = f.read(4)
                inline_size = 4

            value_size = TYPE_SIZES.get(field_type, 1) * count
            current_pos = f.tell()
            if value_size <= inline_size:
                raw = value_or_offset[:value_size]
            else:
                offset = struct.unpack(order + ("Q" if big_tiff else "I"), value_or_offset)[0]
                f.seek(offset)
                raw = f.read(value_size)
                f.seek(current_pos)

            if field_type == 3:
                value = struct.unpack(order + str(count) + "H", raw)
            elif field_type == 4:
                value = struct.unpack(order + str(count) + "I", raw)
            elif field_type == 12:
                value = struct.unpack(order + str(count) + "d", raw)
            else:
                continue

            tags[tag] = value
        return tags


def geotiff_bounds(path: Path) -> tuple[float, float, float, float]:
    tags = read_tiff_tags(path)
    width = tags[256][0]
    height = tags[257][0]
    scale = tags[33550]
    tie = tags[33922]
    xmin = tie[3]
    ymax = tie[4]
    xmax = xmin + width * scale[0]
    ymin = ymax - height * scale[1]
    return xmin, ymin, xmax, ymax


def derive_source_bounds() -> tuple[float, float, float, float]:
    tif_paths = sorted(SOURCE_DIR.glob("JHB-*_solar.tif"))
    if not tif_paths:
        raise FileNotFoundError(f"No source TIFFs found in {SOURCE_DIR}")

    bounds = [geotiff_bounds(path) for path in tif_paths]
    xmin = min(item[0] for item in bounds)
    ymin = min(item[1] for item in bounds)
    xmax = max(item[2] for item in bounds)
    ymax = max(item[3] for item in bounds)
    return xmin, ymin, xmax, ymax


def lonlat_to_web_mercator(lon: float, lat: float) -> tuple[float, float]:
    radius = 6378137.0
    x = radius * math.radians(lon)
    y = radius * math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))
    return x, y


def fetch_json(url: str, referer: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Referer": referer})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def download_binary(url: str, destination: Path, referer: str) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Referer": referer})
    with urllib.request.urlopen(request, timeout=300) as response:
        data = response.read()
    if not (data[:4] in (b"II*\x00", b"MM\x00*")):
        preview = data[:200].decode("utf-8", "ignore")
        raise RuntimeError(f"Expected GeoTIFF, got non-TIFF response: {preview}")
    destination.write_bytes(data)


def is_tiff(path: Path) -> bool:
    if not path.exists() or path.stat().st_size < 8:
        return False
    signature = path.read_bytes()[:4]
    return signature in (b"II*\x00", b"MM\x00*")


def download_with_retries(url: str, destination: Path, referer: str, retries: int = 6) -> None:
    last_error: Exception | None = None
    temp_path = destination.with_suffix(destination.suffix + ".part")
    for attempt in range(1, retries + 1):
        try:
            download_binary(url, temp_path, referer=referer)
            temp_path.replace(destination)
            return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if temp_path.exists():
                temp_path.unlink()
            print(f"  attempt {attempt}/{retries} failed: {exc}", flush=True)
            time.sleep(min(20, attempt * 3))
    raise RuntimeError(f"Failed to download {destination.name}") from last_error


def build_vrt(tile_records: list[dict], output_path: Path, width: int, height: int, xmin: float, ymax: float, pixel_size: float) -> None:
    lines = [
        f'<VRTDataset rasterXSize="{width}" rasterYSize="{height}">',
        "  <SRS>EPSG:3857</SRS>",
        f"  <GeoTransform>{xmin}, {pixel_size}, 0.0, {ymax}, 0.0, -{pixel_size}</GeoTransform>",
    ]

    for band, interp in enumerate(("Red", "Green", "Blue"), start=1):
        lines.append(f'  <VRTRasterBand dataType="Byte" band="{band}">')
        lines.append(f"    <ColorInterp>{interp}</ColorInterp>")
        for tile in tile_records:
            rel_path = os.path.relpath(tile["path"], output_path.parent)
            lines.extend(
                [
                    "    <SimpleSource>",
                    f'      <SourceFilename relativeToVRT="1">{rel_path}</SourceFilename>',
                    f"      <SourceBand>{band}</SourceBand>",
                    (
                        f'      <SourceProperties RasterXSize="{tile["width"]}" RasterYSize="{tile["height"]}" '
                        'DataType="Byte" />'
                    ),
                    f'      <SrcRect xOff="0" yOff="0" xSize="{tile["width"]}" ySize="{tile["height"]}" />',
                    (
                        f'      <DstRect xOff="{tile["xoff"]}" yOff="{tile["yoff"]}" '
                        f'xSize="{tile["width"]}" ySize="{tile["height"]}" />'
                    ),
                    "    </SimpleSource>",
                ]
            )
        lines.append("  </VRTRasterBand>")
    lines.append("</VRTDataset>")
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dataset",
        choices=sorted(DATASETS),
        default="aerial_2023",
        help="Official Johannesburg imagery dataset to download.",
    )
    args = parser.parse_args()

    dataset = DATASETS[args.dataset]
    output_dir = dataset["output_dir"]
    service_url = dataset["service_url"]
    prefix = dataset["prefix"]

    output_dir.mkdir(parents=True, exist_ok=True)

    lon_min, lat_min, lon_max, lat_max = derive_source_bounds()
    x_min, y_min = lonlat_to_web_mercator(lon_min, lat_min)
    x_max, y_max = lonlat_to_web_mercator(lon_max, lat_max)

    metadata = fetch_json(f"{service_url}?f=pjson", referer=service_url)
    pixel_size = float(metadata["pixelSizeX"])
    max_width = int(metadata["maxImageWidth"])
    max_height = int(metadata["maxImageHeight"])

    total_width = math.ceil((x_max - x_min) / pixel_size)
    total_height = math.ceil((y_max - y_min) / pixel_size)
    x_max = x_min + total_width * pixel_size
    y_min = y_max - total_height * pixel_size

    effective_max_width = min(max_width, SAFE_MAX_WIDTH)
    effective_max_height = min(max_height, SAFE_MAX_HEIGHT)
    cols = math.ceil(total_width / effective_max_width)
    rows = math.ceil(total_height / effective_max_height)

    base_tile_width = total_width // cols
    extra_width = total_width % cols
    base_tile_height = total_height // rows
    extra_height = total_height % rows

    tile_records: list[dict] = []

    yoff = 0
    for row in range(rows):
        tile_height = base_tile_height + (1 if row < extra_height else 0)
        xoff = 0
        for col in range(cols):
            tile_width = base_tile_width + (1 if col < extra_width else 0)
            tile_xmin = x_min + xoff * pixel_size
            tile_ymax = y_max - yoff * pixel_size
            tile_xmax = tile_xmin + tile_width * pixel_size
            tile_ymin = tile_ymax - tile_height * pixel_size

            params = {
                "bbox": f"{tile_xmin},{tile_ymin},{tile_xmax},{tile_ymax}",
                "bboxSR": "3857",
                "imageSR": "3857",
                "size": f"{tile_width},{tile_height}",
                "format": "tiff",
                "interpolation": "RSP_NearestNeighbor",
                "f": "image",
            }
            tile_path = output_dir / f"{prefix}_r{row + 1:02d}_c{col + 1:02d}.tif"
            if not is_tiff(tile_path):
                url = f"{service_url}/exportImage?{urllib.parse.urlencode(params)}"
                print(f"Downloading {tile_path.name} ({tile_width}x{tile_height})", flush=True)
                download_with_retries(url, tile_path, referer=service_url)
            else:
                print(f"Keeping existing {tile_path.name}", flush=True)

            tile_records.append(
                {
                    "path": tile_path,
                    "width": tile_width,
                    "height": tile_height,
                    "xoff": xoff,
                    "yoff": yoff,
                }
            )
            xoff += tile_width
        yoff += tile_height

    build_vrt(
        tile_records=tile_records,
        output_path=output_dir / f"{prefix}_mosaic.vrt",
        width=total_width,
        height=total_height,
        xmin=x_min,
        ymax=y_max,
        pixel_size=pixel_size,
    )

    manifest = {
        "dataset": args.dataset,
        "service_url": service_url,
        "pixel_size_m": pixel_size,
        "grid": {"cols": cols, "rows": rows},
        "total_size_px": {"width": total_width, "height": total_height},
        "source_bounds_epsg4326": {
            "xmin": lon_min,
            "ymin": lat_min,
            "xmax": lon_max,
            "ymax": lat_max,
        },
        "download_bounds_epsg3857": {
            "xmin": x_min,
            "ymin": y_min,
            "xmax": x_max,
            "ymax": y_max,
        },
        "tiles": [tile["path"].name for tile in tile_records],
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    print(f"Wrote {output_dir / f'{prefix}_mosaic.vrt'}", flush=True)


if __name__ == "__main__":
    main()
