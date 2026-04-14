import rasterio
from rasterio.warp import reproject, Resampling, calculate_default_transform
import os

# ====================== 你的配置（已填好） ======================
GRID_NAME = "G0772"
RAW_TIFF = f"data/raw_tiff/{GRID_NAME}.tif"
OUT_TIFF = f"data/processed_tiff/{GRID_NAME}.tif"
TARGET_CRS = "EPSG:32734"  # 仓库固定投影


# ===============================================================

def process_tiff():
    print(f"开始处理 {GRID_NAME} 底图，自动转换投影...")

    # 确保输出文件夹存在
    os.makedirs("data/processed_tiff", exist_ok=True)

    with rasterio.open(RAW_TIFF) as src:
        # 计算转换后的参数
        transform, width, height = calculate_default_transform(
            src.crs, TARGET_CRS, src.width, src.height, *src.bounds
        )

        # 更新文件元数据
        meta = src.meta.copy()
        meta.update({
            "crs": TARGET_CRS,
            "transform": transform,
            "width": width,
            "height": height,
            "count": 3  # 固定3波段RGB
        })

        # 执行重投影并保存
        with rasterio.open(OUT_TIFF, "w", **meta) as dst:
            for i in range(1, 4):
                reproject(
                    source=rasterio.band(src, i),
                    destination=rasterio.band(dst, i),
                    src_transform=src.transform,
                    src_crs=src.crs,
                    dst_transform=transform,
                    dst_crs=TARGET_CRS,
                    resampling=Resampling.bilinear
                )

    print(f"✅ 预处理完成！标准化底图已保存至：{OUT_TIFF}")
    print(f"✅ 投影已从 EPSG:4326 → EPSG:32734（仓库标准）")


if __name__ == "__main__":
    process_tiff()