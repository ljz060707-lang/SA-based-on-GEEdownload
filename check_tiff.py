import rasterio
import sys

# 你的底图路径（和命令里的一致）
tiff_path = "data/raw_tiff/G0772.tif"

try:
    with rasterio.open(tiff_path) as src:
        print("✅ TIFF底图信息读取成功！")
        print("-" * 50)
        print(f"文件路径: {tiff_path}")
        print(f"投影坐标系 (CRS): {src.crs}")
        print(f"分辨率 (像素/单位): {src.res}")
        print(f"波段数量: {src.count}")
        print(f"图像尺寸 (宽x高): {src.width} x {src.height}")
        print(f"地理范围: {src.bounds}")
        print("-" * 50)
        print("💡 关键校验：")
        print(f"  - 是否3波段RGB: {'是' if src.count >=3 else '否（需要处理）'}")
        print(f"  - 目标投影: EPSG:32734")
        print(f"  - 当前投影: {src.crs}")

except Exception as e:
    print(f"❌ 读取失败: {e}")
    print("请检查文件路径是否正确！")