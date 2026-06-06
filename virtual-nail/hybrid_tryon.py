import argparse
import json
import shutil
import uuid
from pathlib import Path

from color_nail_full_pipeline_adapter import run_full_pipeline
from recommend_styles import recommend_styles
from tryon_quality import evaluate_tryon_quality


DEFAULT_DB = Path("data/style_database/nail_style.db")


def ensure_env_for_local_models():
    import os

    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    os.environ.setdefault("HF_HOME", os.path.expanduser("~/.cache/huggingface"))
    os.environ.setdefault("HUGGINGFACE_HUB_CACHE", os.path.expanduser("~/.cache/huggingface/hub"))
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")


def copy_if_exists(src, dst):
    src = Path(src)
    if src.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return dst
    return None


def select_style(db_path, style_id=None, hand_type=None, tags=None, length=None, nail_shape=None, color=None):
    if style_id:
        rows = recommend_styles(top_k=999, db_path=db_path)
        for row in rows:
            if row["style_id"] == style_id or row["serial_no"] == style_id.replace("style_", ""):
                return row
        raise ValueError(f"Style not found: {style_id}")

    rows = recommend_styles(
        hand_type=hand_type,
        tags=tags,
        length=length,
        nail_shape=nail_shape,
        color=color,
        top_k=1,
        db_path=db_path,
    )
    if not rows:
        raise RuntimeError("No recommended style found.")
    return rows[0]


def run_hybrid_tryon(
    hand_image,
    hand_type=None,
    style_id=None,
    tags=None,
    length=None,
    nail_shape=None,
    color=None,
    db_path=DEFAULT_DB,
    out_dir=Path("data/output/hybrid_tryon"),
    image_variant="enhanced",
    quality_threshold=72.0,
    enable_ai=True,
):
    ensure_env_for_local_models()
    db_path = Path(db_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    selected = select_style(db_path, style_id, hand_type, tags, length, nail_shape, color)
    style_image = selected["enhanced_image_path"] if image_variant == "enhanced" else selected["original_image_path"]
    if not style_image:
        raise RuntimeError(f"Selected style has no {image_variant} image path: {selected['style_id']}")

    case_id = f"hybrid_{Path(hand_image).stem}_{selected['serial_no']}_{uuid.uuid4().hex[:8]}"
    local_path = Path(
        run_full_pipeline(
            Path(hand_image),
            Path(style_image),
            Path("data/test_masks") / f"{Path(hand_image).stem}_mask.png",
            task_id=case_id,
            ai_fill_edges=False,
        )
    )

    mask_path = Path("data/output/debug") / f"{Path(hand_image).stem}_pixel_transplant_mask.png"
    quality = evaluate_tryon_quality(local_path, mask_path)
    quality["needs_ai_refine"] = quality["score"] < quality_threshold

    final_path = local_path
    ai_used = False
    if enable_ai and quality["needs_ai_refine"]:
        from sdxl_refine_nails import refine_with_sdxl

        ai_path = out_dir / f"{case_id}_ai_refined.png"
        refine_with_sdxl(
            local_path,
            mask_path,
            ai_path,
            size=768,
            steps=12,
            strength=0.18,
            guidance=3.8,
            seed=123,
            mask_expand=8,
            mask_feather=4,
            debug_mask_path=out_dir / f"{case_id}_ai_mask.png",
        )
        final_path = ai_path
        ai_used = True

    saved_local = copy_if_exists(local_path, out_dir / f"{case_id}_quick.png")
    saved_mask = copy_if_exists(mask_path, out_dir / f"{case_id}_mask.png")
    saved_final = copy_if_exists(final_path, out_dir / f"{case_id}_final.png")

    result = {
        "case_id": case_id,
        "hand_image": str(Path(hand_image)),
        "style": selected,
        "style_image": str(style_image),
        "quick_result": str(saved_local or local_path),
        "final_result": str(saved_final or final_path),
        "mask_path": str(saved_mask or mask_path),
        "quality": quality,
        "ai_used": ai_used,
    }
    report_path = out_dir / f"{case_id}_report.json"
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    result["report_path"] = str(report_path)
    return result


def main():
    parser = argparse.ArgumentParser(description="Run cost-controlled hybrid nail try-on.")
    parser.add_argument("--hand-image", required=True)
    parser.add_argument("--hand-type", default=None)
    parser.add_argument("--style-id", default=None)
    parser.add_argument("--tags", default="")
    parser.add_argument("--length", default=None)
    parser.add_argument("--nail-shape", default=None)
    parser.add_argument("--color", default="")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--out-dir", default="data/output/hybrid_tryon")
    parser.add_argument("--image-variant", choices=["enhanced", "original"], default="enhanced")
    parser.add_argument("--quality-threshold", type=float, default=72.0)
    parser.add_argument("--disable-ai", action="store_true")
    args = parser.parse_args()

    result = run_hybrid_tryon(
        hand_image=args.hand_image,
        hand_type=args.hand_type,
        style_id=args.style_id,
        tags=args.tags,
        length=args.length,
        nail_shape=args.nail_shape,
        color=args.color,
        db_path=Path(args.db),
        out_dir=Path(args.out_dir),
        image_variant=args.image_variant,
        quality_threshold=args.quality_threshold,
        enable_ai=not args.disable_ai,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
