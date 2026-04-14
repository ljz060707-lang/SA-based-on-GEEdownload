"""
Build a properly georeferenced VRT mosaic from the G1238 tiles.

The tiles were downloaded via WMS GetMap, which does not embed georeference
info. This script reconstructs the correct EPSG:4326 bounding box for each
tile from the same grid parameters used during download, writes the
geotransform + CRS into each TIF in-place, and then calls gdalbuildvrt.
"""

import os
import glob
import subprocess
import struct

# ── Download grid parameters (must match the download script) ──────────
xmin, ymin = 18.370850, -34.203447
xmax, ymax = 18.381972, -34.194205
tile_size  = 0.0016          # degrees per tile
pixel_size = 2000            # pixels per tile

n_cols = 7   # 0..6
n_rows = 6   # 0..5

GDAL_TRANSLATE = r"C:\Program Files\QGIS 3.44.7\bin\gdal_translate.exe"
GDALBUILDVRT   = r"C:\Program Files\QGIS 3.44.7\bin\gdalbuildvrt.exe"

tiles_dir = r"D:\capetown_solar\tiles"

# ── Step 1: Georeference every tile ───────────────────────────────────
georef_tifs = []

for i in range(n_cols):
    for j in range(n_rows):
        src = os.path.join(tiles_dir, f"G1238_{i}_{j}.tif")
        if not os.path.exists(src):
            print(f"  [skip] {src} not found")
            continue

        # Reproduce the exact bbox from the download script
        txmin = xmin + i * tile_size
        txmax = min(txmin + tile_size, xmax)
        tymax = ymax - j * tile_size
        tymin = ymax - (j + 1) * tile_size

        # upper-left-x, upper-left-y, lower-right-x, lower-right-y
        ullr = [str(txmin), str(tymax), str(txmax), str(tymin)]

        dst = os.path.join(tiles_dir, f"G1238_{i}_{j}_geo.tif")

        cmd = [
            GDAL_TRANSLATE,
            "-of", "GTiff",
            "-a_srs", "EPSG:4326",
            "-a_ullr", *ullr,
            src, dst
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"  [ERROR] {src}: {result.stderr.strip()}")
            continue

        georef_tifs.append(dst)
        print(f"  [ok] {dst}")

print(f"\nGeoreferenced {len(georef_tifs)} / {n_cols * n_rows} tiles.\n")

# ── Step 2: Build VRT mosaic ──────────────────────────────────────────
vrt_path = os.path.join(tiles_dir, "G1238_mosaic.vrt")

cmd = [GDALBUILDVRT, vrt_path] + georef_tifs
result = subprocess.run(cmd, capture_output=True, text=True)
print(result.stdout)
if result.returncode != 0:
    print(f"[ERROR] gdalbuildvrt failed:\n{result.stderr}")
else:
    print(f"✅  VRT 已生成: {vrt_path}")
    print("    可以直接拖入 QGIS 查看，坐标系为 EPSG:4326 (WGS 84)。")

# ── Step 3: Optionally replace originals and clean up ─────────────────
# Uncomment the block below if you want to overwrite the original TIFs
# with the georeferenced versions and remove the *_geo.tif files.
#
# for gf in georef_tifs:
#     orig = gf.replace("_geo.tif", ".tif")
#     os.replace(gf, orig)
# print("Replaced originals with georeferenced versions.")
