import cv2
import numpy as np
import os
import sys
import threading
from pathlib import Path
from nail_color_transfer import U2NetMasker
import time
from datetime import datetime

# --- 确保目录存在 ---
Path("data/debug").mkdir(parents=True, exist_ok=True)
Path("data/output/final").mkdir(parents=True, exist_ok=True)

# --- 全局U2Net实例 ---
_masker_instance = None
_masker_lock = threading.Lock()

def get_masker():
    global _masker_instance
    if _masker_instance is None:
        with _masker_lock:
            if _masker_instance is None:
                print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 初始化U²-Net掩码生成器...")
                _masker_instance = U2NetMasker()
                print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] U²-Net掩码生成器初始化完成。")
    return _masker_instance



def imread_unicode(path, flags=cv2.IMREAD_COLOR):
    """Read images from paths that may contain Chinese or other non-ASCII text."""
    data = np.fromfile(str(path), dtype=np.uint8)
    if data.size == 0:
        return None
    return cv2.imdecode(data, flags)


def resize_image_long_edge(img, max_long_edge=1024):
    """
    将图像长边缩放到指定尺寸，短边等比缩放。
    """
    h, w = img.shape[:2]
    if max(h, w) > max_long_edge:
        if h > w:
            new_h = max_long_edge
            new_w = int(w * (max_long_edge / h))
        else:
            new_w = max_long_edge
            new_h = int(h * (max_long_edge / w))
        return cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_LANCZOS4)
    return img

def get_keypoints_from_mask(mask):
    """
    最终稳定版: 结合了边界框的稳定性和8点采样的塑形能力。
    """
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    
    main_contour = max(contours, key=cv2.contourArea)

    x, y, w, h = cv2.boundingRect(main_contour)
    
    # 定义8个关键点: 4个角点和4个边的中点
    keypoints = np.array([
        [x, y],                             # Top-Left
        [x + w // 2, y],                    # Top-Mid
        [x + w, y],                         # Top-Right
        [x + w, y + h // 2],                # Right-Mid
        [x + w, y + h],                     # Bottom-Right
        [x + w // 2, y + h],                # Bottom-Mid
        [x, y + h],                         # Bottom-Left
        [x, y + h // 2]                     # Left-Mid
    ], dtype=np.float32)

    return keypoints.astype(int)



def get_sorted_nail_contours(mask, min_area=100):
    """Return nail contours in a stable left-to-right order."""
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    contours = [c for c in contours if cv2.contourArea(c) >= min_area]
    contours.sort(key=lambda c: cv2.boundingRect(c)[0])
    return contours


FINGER_ROLES = ["thumb", "index", "middle", "ring", "pinky"]


def contour_center(contour):
    x, y, w, h = cv2.boundingRect(contour)
    return np.array([x + w / 2.0, y + h / 2.0], dtype=np.float32)


def select_semantic_five_contours(contours):
    """Choose the main hand's five nail contours when mirrors/noise are present."""
    contours = [c for c in contours if cv2.contourArea(c) >= 500]
    contours.sort(key=lambda c: cv2.boundingRect(c)[0])
    if len(contours) <= 5:
        return contours

    from itertools import combinations

    best_score = None
    best = None
    for subset in combinations(contours, 5):
        centers = np.array([contour_center(c) for c in subset])
        thumb_idx = int(np.argmax(centers[:, 1]))
        thumb = centers[thumb_idx]
        others = np.delete(centers, thumb_idx, axis=0)

        # The thumb should sit lower than the four long fingers and usually at one side.
        lower_margin = thumb[1] - np.median(others[:, 1])
        side_margin = max(thumb[0] - np.max(others[:, 0]), np.min(others[:, 0]) - thumb[0])
        if side_margin < 0:
            side_margin *= 3.0

        xs = np.sort(others[:, 0])
        gaps = np.diff(xs)
        regularity_penalty = float(np.std(gaps)) if len(gaps) else 0.0
        tiny_penalty = sum(max(0.0, 1200.0 - cv2.contourArea(c)) for c in subset) / 100.0

        score = lower_margin * 2.0 + side_margin - regularity_penalty * 0.2 - tiny_penalty
        if best_score is None or score > best_score:
            best_score = score
            best = subset

    return sorted(best, key=lambda c: cv2.boundingRect(c)[0]) if best else contours[:5]


def assign_finger_roles(contours):
    """Map contours to thumb/index/middle/ring/pinky using hand geometry."""
    selected = select_semantic_five_contours(contours)
    if len(selected) < 5:
        return {f"nail_{i}": c for i, c in enumerate(selected)}

    centers = np.array([contour_center(c) for c in selected])
    thumb_idx = int(np.argmax(centers[:, 1]))
    thumb_contour = selected[thumb_idx]
    other_pairs = [(c, contour_center(c)) for i, c in enumerate(selected) if i != thumb_idx]
    other_pairs.sort(key=lambda item: item[1][0])

    thumb_x = centers[thumb_idx, 0]
    other_median_x = np.median([p[1][0] for p in other_pairs])
    role_map = {"thumb": thumb_contour}
    if thumb_x > other_median_x:
        ordered_roles = ["pinky", "ring", "middle", "index"]
    else:
        ordered_roles = ["index", "middle", "ring", "pinky"]

    for role, (contour, _) in zip(ordered_roles, other_pairs):
        role_map[role] = contour
    return role_map


def extract_reference_nail_regions(ref_img, ref_path, masker, max_regions=10):
    """
    Extract individual nail patches from the reference style image.
    Each returned item contains a cropped patch and local keypoints, so target nails
    receive separate source nails instead of a compressed whole reference image.
    """
    stem = Path(ref_path).stem
    debug_dir = Path("data/output/debug/reference_regions")
    debug_dir.mkdir(parents=True, exist_ok=True)

    ref_mask_prob = masker.get_mask(ref_img, str(ref_path), disable_cache=True)
    _, ref_mask_raw = cv2.threshold(ref_mask_prob.astype(np.uint8), 128, 255, cv2.THRESH_BINARY)
    cv2.imwrite(str(debug_dir / f"{stem}_reference_mask.png"), ref_mask_raw)

    contours = get_sorted_nail_contours(ref_mask_raw, min_area=80)
    if not contours:
        ref_gray = cv2.cvtColor(ref_img, cv2.COLOR_BGR2GRAY)
        _, fallback_mask = cv2.threshold(ref_gray, 240, 255, cv2.THRESH_BINARY_INV)
        points = get_keypoints_from_mask(fallback_mask)
        if points is None:
            return []
        return [{"role": "fallback", "image": ref_img, "mask": fallback_mask, "points": points, "area": ref_img.shape[0] * ref_img.shape[1]}]

    role_contours = assign_finger_roles(contours)

    regions = []
    for idx, (role, contour) in enumerate(role_contours.items()):
        x, y, w, h = cv2.boundingRect(contour)
        pad = max(4, int(max(w, h) * 0.18))
        x0 = max(0, x - pad)
        y0 = max(0, y - pad)
        x1 = min(ref_img.shape[1], x + w + pad)
        y1 = min(ref_img.shape[0], y + h + pad)

        local_img = ref_img[y0:y1, x0:x1].copy()
        local_mask = np.zeros(ref_mask_raw.shape, dtype=np.uint8)
        cv2.drawContours(local_mask, [contour], -1, 255, thickness=cv2.FILLED)
        local_mask = local_mask[y0:y1, x0:x1]

        points = get_keypoints_from_mask(local_mask)
        if points is None:
            continue

        canonical_img, canonical_points = make_canonical_reference_patch(local_img, local_mask, points)

        cv2.imwrite(str(debug_dir / f"{stem}_region_{idx:02d}_{role}.png"), local_img)
        cv2.imwrite(str(debug_dir / f"{stem}_region_{idx:02d}_{role}_mask.png"), local_mask)
        cv2.imwrite(str(debug_dir / f"{stem}_region_{idx:02d}_{role}_canonical.png"), canonical_img)
        regions.append({
            "role": role,
            "image": canonical_img,
            "mask": None,
            "points": canonical_points,
            "area": cv2.contourArea(contour),
        })

    return regions



def canonical_nail_points(width, height, margin=8):
    x = margin
    y = margin
    w = width - margin * 2
    h = height - margin * 2
    return np.array([
        [x, y],
        [x + w // 2, y],
        [x + w, y],
        [x + w, y + h // 2],
        [x + w, y + h],
        [x + w // 2, y + h],
        [x, y + h],
        [x, y + h // 2],
    ], dtype=np.float32)


def warp_piecewise(src_img, src_points, dst_points, out_shape):
    out_h, out_w = out_shape[:2]
    channels = 1 if src_img.ndim == 2 else src_img.shape[2]
    if src_img.ndim == 2:
        src = src_img[:, :, None]
    else:
        src = src_img
    triangles = np.array([
        [0, 1, 7], [1, 2, 3], [1, 3, 7],
        [3, 4, 5], [3, 5, 7], [5, 6, 7]
    ], dtype=int)
    canvas = np.zeros((out_h, out_w, channels), dtype=src.dtype)
    for tri_indices in triangles:
        src_tri = src_points[tri_indices].astype(np.float32)
        dst_tri = dst_points[tri_indices].astype(np.float32)
        mat = cv2.getAffineTransform(src_tri, dst_tri)
        warped = cv2.warpAffine(src, mat, (out_w, out_h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REFLECT_101)
        if warped.ndim == 2:
            warped = warped[:, :, None]
        tri_mask = np.zeros((out_h, out_w), dtype=np.uint8)
        cv2.drawContours(tri_mask, [dst_tri.astype(np.int32)], 0, 255, -1)
        cv2.copyTo(warped, tri_mask, canvas)
    return canvas[:, :, 0] if channels == 1 else canvas


def make_canonical_reference_patch(local_img, local_mask, points, out_w=180, out_h=260):
    """Normalize a source nail crop into a complete upright nail texture."""
    canonical_pts = canonical_nail_points(out_w, out_h, margin=10)
    clean_img = local_img.copy()
    fill_color = np.array([180, 180, 180], dtype=np.uint8)
    if local_mask is not None:
        local_valid = local_mask > 20
        if np.any(local_valid):
            fill_color = np.median(local_img[local_valid], axis=0).astype(np.uint8)
            clean_img[~local_valid] = fill_color
            local_invalid = (~local_valid).astype(np.uint8) * 255
            clean_img = cv2.inpaint(clean_img, local_invalid, 3, cv2.INPAINT_TELEA)

    texture = warp_piecewise(clean_img, points.astype(np.float32), canonical_pts, (out_h, out_w, local_img.shape[2]))

    if local_mask is not None:
        mask_warped = warp_piecewise(local_mask, points.astype(np.float32), canonical_pts, (out_h, out_w, 1))
    else:
        mask_warped = np.ones((out_h, out_w), dtype=np.uint8) * 255

    valid = mask_warped > 20
    if np.any(valid):
        invalid = (~valid).astype(np.uint8) * 255
        texture[invalid > 0] = fill_color
        texture = cv2.inpaint(texture, invalid, 5, cv2.INPAINT_TELEA)
    else:
        texture = cv2.resize(local_img, (out_w, out_h), interpolation=cv2.INTER_CUBIC)

    return texture, canonical_pts.astype(np.int32)


def expand_keypoints(points, scale_x=1.12, scale_y=1.10):
    """Slightly overfill destination nails so style patches cover the full target mask."""
    pts = points.astype(np.float32)
    center = pts.mean(axis=0)
    expanded = pts.copy()
    expanded[:, 0] = center[0] + (pts[:, 0] - center[0]) * scale_x
    expanded[:, 1] = center[1] + (pts[:, 1] - center[1]) * scale_y
    return expanded


def process_one_pixel_transplant_auto(img_path, ref_path):
    """
    使用TPS变形和无缝融合实现全自动像素级内容搬运。
    """
    start_time = time.time()
    stem = Path(img_path).stem
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] --- 正在处理 (最终稳定版): {stem} ---")

    # 1. 读取图像
    img_orig = imread_unicode(img_path)
    ref_img_orig = imread_unicode(ref_path)

    if img_orig is None or ref_img_orig is None:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 错误: 无法读取图像文件")
        return None

    # 2. 统一尺寸 (遵循全局1024策略)
    img = resize_image_long_edge(img_orig)
    ref_img = resize_image_long_edge(ref_img_orig)
    
    # 3. 生成掩码
    masker = get_masker()
    mask_prob = masker.get_mask(img, str(img_path), disable_cache=True)
    u2net_mask_path = f"data/output/debug/{stem}_u2net_mask.png"
    cv2.imwrite(u2net_mask_path, mask_prob)
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [DEBUG] U2Net原始掩码已保存: {u2net_mask_path}")
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [DEBUG] U2Net原始掩码 min/max/mean: {mask_prob.min()}/{mask_prob.max()}/{mask_prob.mean():.2f}")

    # 掩码二值化
    _, nail_mask_raw = cv2.threshold(mask_prob.astype(np.uint8), 128, 255, cv2.THRESH_BINARY)
    mask_save_path = f"data/output/debug/{stem}_pixel_transplant_mask.png"
    cv2.imwrite(mask_save_path, nail_mask_raw)
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [DEBUG] 像素迁移阶段掩码已保存: {mask_save_path}")
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [DEBUG] 像素迁移阶段掩码 min/max/mean: {nail_mask_raw.min()}/{nail_mask_raw.max()}/{nail_mask_raw.mean():.2f}")

    # 4. Find target nail contours and assign semantic finger roles.
    contours = get_sorted_nail_contours(nail_mask_raw, min_area=100)
    if not contours:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Error: no target nail contours found.")
        return None
    target_role_contours = assign_finger_roles(contours)

    # 5. Extract individual source nails from the reference style image.
    reference_regions = extract_reference_nail_regions(ref_img, ref_path, masker)
    if not reference_regions:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Error: failed to extract reference nail regions.")
        return None
    reference_by_role = {region.get("role"): region for region in reference_regions}
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Extracted {len(reference_regions)} reference nail regions: {list(reference_by_role.keys())}.")

    # 6. ????
    output_image = img.copy()
    ordered_roles = [role for role in FINGER_ROLES if role in target_role_contours]
    fallback_regions = reference_regions
    for i, role in enumerate(ordered_roles):
        contour = target_role_contours[role]
        if cv2.contourArea(contour) < 100:
            continue
            
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ??{role}?? {i+1}/{len(ordered_roles)}...")
        ref_region = reference_by_role.get(role) or fallback_regions[i % len(fallback_regions)]
        ref_patch = ref_region["image"]
        src_points = ref_region["points"]

        # a. Create a single target nail mask
        single_nail_mask = np.zeros_like(nail_mask_raw)
        cv2.drawContours(single_nail_mask, [contour], -1, 255, thickness=cv2.FILLED)

        # b. 提取指甲关键点
        dst_points = get_keypoints_from_mask(single_nail_mask)
        if dst_points is None:
            continue
        dst_points = expand_keypoints(dst_points)

        # c. 手动分片仿射变换 (100% OpenCV)
        # 基于8个关键点定义一个固定的三角剖分
        triangles = np.array([
            [0, 1, 7], [1, 2, 3], [1, 3, 7],
            [3, 4, 5], [3, 5, 7], [5, 6, 7]
        ], dtype=int)

        # 创建一个黑色画布，用于拼接所有变形后的三角形
        warped_canvas = np.zeros_like(img)

        for tri_indices in triangles:
            src_tri = src_points[tri_indices].astype(np.float32)
            dst_tri = dst_points[tri_indices].astype(np.float32)

            # 计算从源三角形到目标三角形的仿射变换
            M = cv2.getAffineTransform(src_tri, dst_tri)
            
            # 对整个参考图进行仿射变换
            warped_img_full = cv2.warpAffine(ref_patch, M, (img.shape[1], img.shape[0]))

            # 创建一个只包含当前目标三角形的掩码
            mask = np.zeros(img.shape[:2], dtype=np.uint8)
            cv2.drawContours(mask, [dst_tri.astype(int)], 0, 255, -1)
            
            # 使用掩码，将变形后图像的相应部分复制到画布上
            cv2.copyTo(warped_img_full, mask, warped_canvas)
        
        # 经过所有三角形的拼接，画布上已经有了完整的、精确变形的参考图
        warped_ref = warped_canvas

        # d. Alpha blend with near-full nail coverage.
        # Previous erosion was too aggressive and left the original nail visible.
        # Use a distance-field alpha: full opacity in the nail body, soft only at edges.
        cover_kernel = np.ones((5, 5), np.uint8)
        hard_cover_mask = cv2.dilate(single_nail_mask, np.ones((3, 3), np.uint8), iterations=1)
        coverage_mask = cv2.dilate(single_nail_mask, cover_kernel, iterations=2)
        near_black = np.sum(warped_ref.astype(np.int16), axis=2) < 45
        valid_style = (coverage_mask > 0) & (~near_black)
        needs_fill = (coverage_mask > 0) & near_black
        if np.any(valid_style) and np.any(needs_fill):
            fill_color = np.median(warped_ref[valid_style], axis=0).astype(np.uint8)
            warped_ref[needs_fill] = fill_color
            warped_ref = cv2.inpaint(warped_ref, needs_fill.astype(np.uint8) * 255, 3, cv2.INPAINT_TELEA)

        edge_band = cv2.subtract(coverage_mask, hard_cover_mask)
        band_dist = cv2.distanceTransform((edge_band > 0).astype(np.uint8), cv2.DIST_L2, 5)
        alpha_gray = (hard_cover_mask > 0).astype(np.float32)
        if np.any(edge_band):
            band_alpha = np.clip(1.0 - band_dist / 6.0, 0.0, 0.7)
            alpha_gray = np.maximum(alpha_gray, band_alpha * (edge_band > 0))
        alpha_gray = cv2.GaussianBlur(alpha_gray, (3, 3), 0)
        alpha_gray[hard_cover_mask > 0] = 1.0
        alpha_gray = np.clip(alpha_gray, 0.0, 1.0)
        alpha_mask = np.repeat(alpha_gray[..., None], 3, axis=2)

        #    4. 定位融合区域
        x, y, w, h = cv2.boundingRect(coverage_mask)
        roi = output_image[y:y+h, x:x+w]
        warped_roi = warped_ref[y:y+h, x:x+w]
        alpha_roi = alpha_mask[y:y+h, x:x+w]

        #    5. 执行Alpha融合
        #       背景 * (1 - alpha) + 前景 * alpha
        result_roi = roi.astype(np.float32) * (1.0 - alpha_roi.astype(np.float32)) + warped_roi.astype(np.float32) * alpha_roi.astype(np.float32)

        #    6. 将结果放回输出图像
        output_image[y:y+h, x:x+w] = np.clip(result_roi, 0, 255).astype(np.uint8)

    # 7. 保存结果
    debug_dir = "data/output/debug"
    os.makedirs(debug_dir, exist_ok=True)
    final_path = f"{debug_dir}/{stem}_pixel_transplant.png"
    cv2.imwrite(final_path, output_image)

    # 后续流程如AI精炼等请用 optimized_mask 或 optimized_mask_bin
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 完成! 结果已保存: {final_path}")
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 总耗时: {time.time() - start_time:.2f}秒")
    return final_path

def generate_mask_with_u2net(input_img_path, output_mask_path):
    """
    用U²-Net自动生成掩码并保存到output_mask_path，模型和推理方式与editor_image_server_optimized_1024.py一致。
    """
    masker = get_masker()  # 复用全局U2Net实例
    img = imread_unicode(input_img_path)
    if img is None:
        raise FileNotFoundError(f"无法读取图片: {input_img_path}")
    mask = masker.get_mask(img, input_img_path, disable_cache=True)  # 返回单通道0-255掩码
    os.makedirs(os.path.dirname(output_mask_path), exist_ok=True)
    cv2.imwrite(output_mask_path, mask)

def ensure_mask_exists(orig_img_path):
    img_stem = Path(orig_img_path).stem
    mask_path = f"data/test_masks/{img_stem}_mask.png"
    if not os.path.exists(mask_path):
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 掩码不存在，自动生成: {mask_path}")
        generate_mask_with_u2net(orig_img_path, mask_path)
    else:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 掩码已存在: {mask_path}")
    return mask_path

if __name__ == "__main__":
    input_dir = Path("data/test_images")
    ref_dir = Path("data/reference")

    if not input_dir.exists():
        sys.exit(f"错误: 输入目录 '{input_dir}' 不存在。")

    ref_paths = list(ref_dir.glob("*.png")) + list(ref_dir.glob("*.jpg")) + list(ref_dir.glob("*.jpeg"))
    if not ref_paths:
        sys.exit(f"错误: 参考图目录 '{ref_dir}' 为空。")
    ref_path = ref_paths[0]
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 使用参考图: {ref_path}")

    img_files = list(input_dir.glob("*.png")) + list(input_dir.glob("*.jpg")) + list(input_dir.glob("*.jpeg"))
    if not img_files:
        sys.exit(f"错误: 在 '{input_dir}' 目录下没有找到任何图片文件。")

    for img_path in img_files:
        mask_path = ensure_mask_exists(img_path)
        process_one_pixel_transplant_auto(img_path, ref_path)
        break # 只处理一张 
