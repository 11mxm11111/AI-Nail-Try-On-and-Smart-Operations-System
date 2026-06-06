import sys
from pathlib import Path
from PIL import Image
import subprocess
import os
from color_transfer_pixel_level_transplant import process_one_pixel_transplant_auto, ensure_mask_exists, imread_unicode, resize_image_long_edge
from color_transfer_pixel_level_refine_sdxl import refine_sdxl_pipeline
from color_nail_highlight_fill import add_highlight_to_image

def refine_nail_edge_artifacts(result_path, original_img_path, mask_path, output_path, edge_width=9):
    """Clean color bleeding and jagged transition around nail borders.

    This is a conservative postprocess: keep the generated nail center, restore skin
    outside the nail mask, and softly blend the nail border back to the original hand.
    It gives AI-inpaint-like edge cleanup without changing the whole hand.
    """
    import cv2
    import numpy as np

    result = imread_unicode(result_path)
    original = imread_unicode(original_img_path)
    mask = cv2.imread(str(mask_path), cv2.IMREAD_GRAYSCALE)

    if result is None or original is None or mask is None:
        raise Exception(f"??????????: result={result_path}, original={original_img_path}, mask={mask_path}")

    original = resize_image_long_edge(original)
    if original.shape[:2] != result.shape[:2]:
        original = cv2.resize(original, (result.shape[1], result.shape[0]), interpolation=cv2.INTER_LANCZOS4)
    if mask.shape[:2] != result.shape[:2]:
        mask = cv2.resize(mask, (result.shape[1], result.shape[0]), interpolation=cv2.INTER_LINEAR)

    _, mask_bin = cv2.threshold(mask, 128, 255, cv2.THRESH_BINARY)
    contours, _ = cv2.findContours(mask_bin, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    alpha = np.zeros(mask_bin.shape, dtype=np.float32)
    for contour in contours:
        if cv2.contourArea(contour) < 100:
            continue
        single = np.zeros_like(mask_bin)
        cv2.drawContours(single, [contour], -1, 255, cv2.FILLED)
        dist = cv2.distanceTransform((single > 0).astype(np.uint8), cv2.DIST_L2, 5)
        local_alpha = np.clip(dist / float(edge_width), 0.0, 1.0)
        alpha = np.maximum(alpha, local_alpha)

    # Feather slightly so the transition is not visible at high zoom.
    alpha = cv2.GaussianBlur(alpha, (0, 0), 1.2)
    alpha = np.clip(alpha, 0.0, 1.0)[..., None]

    cleaned = original.astype(np.float32) * (1.0 - alpha) + result.astype(np.float32) * alpha

    # A tiny bilateral pass only inside generated nails removes blocky edge artifacts
    # while keeping glitter/highlights in the nail center.
    cleaned_u8 = np.clip(cleaned, 0, 255).astype(np.uint8)
    smooth = cv2.bilateralFilter(cleaned_u8, d=5, sigmaColor=18, sigmaSpace=5)
    inner = (alpha[..., 0] > 0.35).astype(np.float32)[..., None]
    cleaned_u8 = np.clip(cleaned_u8 * (1.0 - inner * 0.18) + smooth * (inner * 0.18), 0, 255).astype(np.uint8)

    cv2.imwrite(str(output_path), cleaned_u8)
    return output_path


def run_full_pipeline(img_path, ref_path, mask_path, task_id=None, ai_fill_edges=False):
    """
    复用主流程：像素迁移 + 高光 + AI精炼。
    img_path/ref_path/mask_path: 输入图片、参考色、掩码路径
    task_id: 任务ID，用于生成唯一的中间文件名
    返回: 精炼后图片路径
    """
    import shutil

    img_path = Path(img_path)
    ref_path = Path(ref_path)
    mask_path = Path(mask_path)

    # 1. 像素迁移
    ensure_mask_exists(str(img_path))
    transplanted_img_path = process_one_pixel_transplant_auto(str(img_path), str(ref_path))

    if transplanted_img_path is None:
        raise Exception("像素迁移阶段失败")

    debug_dir = Path("data/output/debug")
    debug_dir.mkdir(parents=True, exist_ok=True)

    stem = img_path.stem

    # 2. 兼容高光阶段需要的 mask 文件名
    # 实际生成的 mask 通常是：{stem}_mask.png
    actual_mask_path = debug_dir / f"{stem}_mask.png"

    # 高光阶段可能默认读取：{stem}_pixel_transplant_mask.png
    expected_highlight_mask_path = debug_dir / f"{stem}_pixel_transplant_mask.png"

    if actual_mask_path.exists() and not expected_highlight_mask_path.exists():
        shutil.copy(str(actual_mask_path), str(expected_highlight_mask_path))
        print(f"[DEBUG] 已复制高光阶段所需mask: {expected_highlight_mask_path}")

    # 如果 actual_mask_path 不存在，则尝试用接口传进来的 mask_path
    if not expected_highlight_mask_path.exists() and mask_path.exists():
        shutil.copy(str(mask_path), str(expected_highlight_mask_path))
        print(f"[DEBUG] 使用输入mask补充高光阶段mask: {expected_highlight_mask_path}")

    if not expected_highlight_mask_path.exists():
        raise Exception(f"高光阶段所需mask不存在: {expected_highlight_mask_path}")

    # 3. 高光叠加，强制指定输出文件名
    if task_id:
        highlight_out_path = debug_dir / f"{task_id}_with_antialiased_highlight.png"
    else:
        highlight_out_path = debug_dir / f"{stem}_with_antialiased_highlight.png"

    add_highlight_to_image(str(transplanted_img_path), str(highlight_out_path))

    if not highlight_out_path.exists():
        raise Exception(f"高光阶段输出失败: {highlight_out_path}")

    # 4. AI精炼
    #img = Image.open(highlight_out_path)
    #refined_img_path = refine_sdxl_pipeline(img, stem)
    # 4. 暂时跳过 SDXL 精炼，直接返回高光后的结果
    final_dir = Path("data/output/final")
    final_dir.mkdir(parents=True, exist_ok=True)

    final_path = final_dir / f"{stem}_final.png"

    mask_for_cleanup = debug_dir / f"{stem}_pixel_transplant_mask.png"
    if mask_for_cleanup.exists():
        refine_nail_edge_artifacts(highlight_out_path, img_path, mask_for_cleanup, final_path)
        print(f"[DEBUG] edge artifact cleanup saved final image: {final_path}")
    else:
        img = Image.open(highlight_out_path)
        img.save(final_path)
        print(f"[DEBUG] cleanup mask not found, saved highlight image directly: {final_path}")

    if ai_fill_edges and mask_for_cleanup.exists():
        from sdxl_refine_nails import refine_with_sdxl

        ai_final_path = final_dir / f"{stem}_final_ai_fill.png"
        debug_ai_mask_path = debug_dir / f"{stem}_ai_fill_mask.png"
        refine_with_sdxl(
            final_path,
            mask_for_cleanup,
            ai_final_path,
            size=768,
            steps=12,
            strength=0.18,
            guidance=3.8,
            seed=123,
            mask_expand=8,
            mask_feather=4,
            debug_mask_path=debug_ai_mask_path,
        )
        print(f"[DEBUG] AI fill edge refinement saved final image: {ai_final_path}")
        return ai_final_path

    return final_path

    #return refined_img_path
