import argparse
import csv
import random
import shutil
from pathlib import Path

from PIL import Image, ImageDraw

from color_nail_full_pipeline_adapter import run_full_pipeline


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


def list_images(path):
    return sorted([p for p in Path(path).iterdir() if p.suffix.lower() in IMAGE_SUFFIXES])


def copy_if_exists(src, dst):
    src = Path(src)
    if src.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def make_contact_sheet(records, out_path, thumb_w=260, thumb_h=360):
    if not records:
        return None

    cols = 4
    rows = (len(records) + cols - 1) // cols
    label_h = 56
    pad = 18
    sheet = Image.new("RGB", (cols * (thumb_w + pad) + pad, rows * (thumb_h + label_h + pad) + pad), "white")
    draw = ImageDraw.Draw(sheet)

    for idx, record in enumerate(records):
        result = Image.open(record["result_path"]).convert("RGB")
        result.thumbnail((thumb_w, thumb_h), Image.Resampling.LANCZOS)
        col = idx % cols
        row = idx // cols
        x = pad + col * (thumb_w + pad)
        y = pad + row * (thumb_h + label_h + pad)
        bg = Image.new("RGB", (thumb_w, thumb_h), (245, 245, 245))
        bg.paste(result, ((thumb_w - result.width) // 2, (thumb_h - result.height) // 2))
        sheet.paste(bg, (x, y))
        label = f'{record["case_id"]}\nhand {record["hand"]} | {record["style_type"]} {record["style"]}'
        draw.multiline_text((x, y + thumb_h + 8), label, fill=(20, 20, 20), spacing=4)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out_path)
    return out_path


def run_batch(data_dir, out_dir, count_per_type=4, seed=20260527, ai_fill_edges=False):
    data_dir = Path(data_dir)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    hand_dir = data_dir / "手图URL"
    style_dirs = [
        ("original", data_dir / "原始款式图URL"),
        ("enhanced", data_dir / "增强后款式图URL"),
    ]

    hands = list_images(hand_dir)
    if not hands:
        raise RuntimeError(f"No hand images found in {hand_dir}")

    rng = random.Random(seed)
    records = []
    failures = []

    for style_type, style_dir in style_dirs:
        styles = list_images(style_dir)
        if not styles:
            failures.append((style_type, "", "no style images"))
            continue
        for idx in range(count_per_type):
            hand = rng.choice(hands)
            style = rng.choice(styles)
            case_id = f"{style_type}_{idx + 1:02d}_{hand.stem}_style_{style.stem}"
            print(f"[BATCH] {case_id}: hand={hand.name}, style={style.name}")
            try:
                final_path = Path(run_full_pipeline(hand, style, Path("data/test_masks") / f"{hand.stem}_mask.png", task_id=case_id, ai_fill_edges=ai_fill_edges))
                result_path = out_dir / f"{case_id}_result.png"
                copy_if_exists(final_path, result_path)

                debug_pixel = Path("data/output/debug") / f"{hand.stem}_pixel_transplant.png"
                debug_mask = Path("data/output/debug") / f"{hand.stem}_pixel_transplant_mask.png"
                copy_if_exists(debug_pixel, out_dir / f"{case_id}_pixel_transplant.png")
                copy_if_exists(debug_mask, out_dir / f"{case_id}_mask.png")
                copy_if_exists(hand, out_dir / f"{case_id}_hand{hand.suffix.lower()}")
                copy_if_exists(style, out_dir / f"{case_id}_style{style.suffix.lower()}")

                records.append(
                    {
                        "case_id": case_id,
                        "style_type": style_type,
                        "hand": hand.name,
                        "style": style.name,
                        "hand_path": str(hand),
                        "style_path": str(style),
                        "result_path": str(result_path),
                    }
                )
            except Exception as exc:
                failures.append((style_type, f"{hand.name} + {style.name}", repr(exc)))
                print(f"[BATCH][ERROR] {case_id}: {exc!r}")

    csv_path = out_dir / "batch_manifest.csv"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["case_id", "style_type", "hand", "style", "hand_path", "style_path", "result_path"],
        )
        writer.writeheader()
        writer.writerows(records)

    if failures:
        fail_path = out_dir / "batch_failures.txt"
        fail_path.write_text("\n".join([f"{a}\t{b}\t{c}" for a, b, c in failures]), encoding="utf-8")

    sheet_path = make_contact_sheet(records, out_dir / "contact_sheet.png")
    print(f"[BATCH] saved {len(records)} results to {out_dir}")
    print(f"[BATCH] manifest: {csv_path}")
    if sheet_path:
        print(f"[BATCH] contact sheet: {sheet_path}")
    if failures:
        print(f"[BATCH] failures: {len(failures)}")
    return out_dir


def main():
    parser = argparse.ArgumentParser(description="Random batch nail try-on for contest data")
    parser.add_argument("--data-dir", default=r"D:\AI_Project\nail\美甲图")
    parser.add_argument("--out-dir", default=r"D:\AI_Project\virtual-nail\data\output\batch_random_tryon")
    parser.add_argument("--count-per-type", type=int, default=4)
    parser.add_argument("--seed", type=int, default=20260527)
    parser.add_argument("--ai-fill-edges", action="store_true")
    args = parser.parse_args()
    run_batch(args.data_dir, args.out_dir, args.count_per_type, args.seed, args.ai_fill_edges)


if __name__ == "__main__":
    main()
