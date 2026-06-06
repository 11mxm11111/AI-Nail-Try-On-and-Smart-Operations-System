import argparse
import base64
import json
import mimetypes
import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv


APP_ROOT = Path(__file__).resolve().parent
load_dotenv(APP_ROOT / ".env")

DEFAULT_PROVIDER = os.getenv("SEEDREAM_PROVIDER", "ark")
DEFAULT_MODEL = os.getenv("SEEDREAM_MODEL", "doubao-seedream-5-0-lite-260128")
DEFAULT_BASE_URL = os.getenv("SEEDREAM_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3").rstrip("/")
DEFAULT_IMAGE_ENDPOINT = os.getenv("SEEDREAM_IMAGE_ENDPOINT", "/images/generations")




SCENE_PROMPTS = {
    "coffee": """
Create a photorealistic lifestyle image where the same manicured hand is HOLDING a takeaway coffee cup.

The output must clearly show the hand physically holding the cup, not just placed near it.

Pose requirements:
1. Transform the hand into a natural coffee-holding pose if needed.
2. The thumb should be on one side of the cup and the other fingers should wrap gently around the cup sleeve.
3. Keep the wrist relaxed and anatomically believable.
4. The cup should be medium-sized and realistic relative to the hand.
5. The nails must remain clearly visible; do not let the cup hide most of the manicure.
6. If a full grip would hide the nails, use a partial side grip where the fingertips and nail surfaces are still visible.

Manicure preservation requirements:
1. Preserve the exact nail style from the input image: color, pattern, gloss, nail length, and nail shape.
2. Preserve finger-to-finger nail mapping as much as possible.
3. Do not redesign, repaint, shorten, or lengthen the nails.
4. Keep the hand skin tone natural and realistic.

Scene requirements:
Cafe table, soft daylight, realistic phone-camera photo, shallow depth of field, clean commercial beauty style.

Negative constraints:
hand beside cup without holding it, floating cup, impossible grip, twisted wrist, extra fingers, missing fingers,
deformed fingers, cup covering all nails, changed manicure, changed nail color, changed nail pattern,
text, watermark, logo.
""".strip(),
    "phone": """
Create a photorealistic lifestyle image where the same manicured hand is HOLDING a smartphone.

The output must clearly show the hand physically holding the phone, not just placed near it.

Pose requirements:
1. Transform the hand into a natural phone-holding pose if needed.
2. The thumb should rest along one edge or lightly touch the screen.
3. The other fingers should support the back/side of the phone naturally.
4. Keep the wrist relaxed and anatomically believable.
5. The phone should be slim and realistic relative to the hand.
6. The nails must remain clearly visible; do not let the phone hide most of the manicure.
7. If a full grip would hide the nails, use a side grip or mirror-selfie style grip with the nail surfaces visible.

Manicure preservation requirements:
1. Preserve the exact nail style from the input image: color, pattern, gloss, nail length, and nail shape.
2. Preserve finger-to-finger nail mapping as much as possible.
3. Do not redesign, repaint, shorten, or lengthen the nails.
4. Keep the hand skin tone natural and realistic.

Scene requirements:
Clean lifestyle setting, soft daylight, realistic phone-camera photo, elegant social-media beauty style.

Negative constraints:
hand beside phone without holding it, floating phone, impossible grip, twisted wrist, extra fingers, missing fingers,
deformed fingers, phone covering all nails, changed manicure, changed nail color, changed nail pattern,
text, watermark, logo.
""".strip(),
}



TRYON_PROMPT = """
Generate a photorealistic virtual nail try-on image for an e-commerce manicure preview.

Input rules:
- The first image is the target user's hand photo. Treat it as the locked base image.
- The second image is only the manicure style reference. Transfer only the nail design.
- The reference may contain a different hand pose, finger angle, lighting, jewelry, sleeve, or background; ignore those non-nail elements.

Strict editing requirements:
1. Edit only the visible fingernail plates of the target hand.
2. Keep the original target hand anatomy, finger count, finger length, skin tone, wrinkles, pose, lighting, background, camera angle, and composition unchanged.
3. Do not redraw, reshape, add, remove, rotate, or move any fingers.
4. Preserve finger-to-finger correspondence exactly: thumb to thumb, index to index, middle to middle, ring to ring, pinky to pinky.
5. Do not swap accent nails or patterns between fingers. If the reference ring finger has an accent design, put that accent on the target ring finger.
6. Fully cover every visible nail from cuticle line to free edge while respecting the natural nail boundary and cuticle curve.
7. If the reference nail area is partially occluded or too small, infer the missing pattern/color consistently so the target nail is fully filled.
8. Preserve the reference manicure's color palette, pattern placement, glitter density, decals, gradients, chrome/cat-eye effects, 3D decorations, and glossy gel texture.
9. Blend naturally with the target photo: correct perspective, nail curvature, highlights, shadows, and edge softness.
10. The final image should look like a real salon-quality try-on photo, not a sticker overlay.

Negative constraints:
extra fingers, missing fingers, deformed hand, changed hand pose, changed skin tone, changed background,
wrong finger mapping, swapped accent nails, incomplete nail coverage, exposed natural nail, nail art outside nail boundaries,
copied jewelry/sleeves/background from the reference image, blurry nail art, distorted nail shape, text, watermark.
""".strip()


def get_api_key():
    key = os.getenv("SEEDREAM_API_KEY") or os.getenv("ARK_API_KEY") or os.getenv("DOUBAO_API_KEY")
    if not key:
        raise RuntimeError("Please set SEEDREAM_API_KEY, ARK_API_KEY, or DOUBAO_API_KEY in .env.")
    return key


def image_to_data_url(path):
    path = Path(path)
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def request_json(method, url, **kwargs):
    resp = requests.request(method, url, timeout=180, **kwargs)
    if not resp.ok:
        raise RuntimeError(f"{method} {url} failed: {resp.status_code} {resp.text[:1200]}")
    try:
        return resp.json()
    except Exception as exc:
        raise RuntimeError(f"{method} {url} returned non-JSON response: {resp.text[:500]}") from exc


def find_image_urls(obj):
    urls = []
    if isinstance(obj, str):
        if obj.startswith("http") or obj.startswith("data:image"):
            urls.append(obj)
        return urls
    if isinstance(obj, list):
        for item in obj:
            urls.extend(find_image_urls(item))
        return urls
    if isinstance(obj, dict):
        for key in ("url", "image_url", "output_url", "result_url"):
            value = obj.get(key)
            if isinstance(value, str) and (value.startswith("http") or value.startswith("data:image")):
                urls.append(value)
        for key in ("data", "images", "output", "result"):
            urls.extend(find_image_urls(obj.get(key)))
    return urls


def find_task_id(obj):
    if not isinstance(obj, dict):
        return None
    payload = obj.get("data", obj)
    if not isinstance(payload, dict):
        return None
    for key in ("id", "task_id", "generation_id", "request_id"):
        value = payload.get(key)
        if value:
            return str(value)
    return None


def download_image(url, out_path):
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if url.startswith("data:image"):
        encoded = url.split(",", 1)[1]
        out_path.write_bytes(base64.b64decode(encoded))
        return out_path
    resp = requests.get(url, timeout=180)
    if not resp.ok:
        raise RuntimeError(f"Download failed: {resp.status_code} {resp.text[:500]}")
    out_path.write_bytes(resp.content)
    return out_path


def build_openai_compatible_payload(hand_image, style_image, prompt):
    return {
        "model": DEFAULT_MODEL,
        "prompt": prompt,
        "image": [image_to_data_url(hand_image), image_to_data_url(style_image)],
        "response_format": "url",
        "size": os.getenv("SEEDREAM_SIZE", "1536x2400"),
        "watermark": False,
    }


def build_ark_multimodal_payload(hand_image, style_image, prompt):
    # Some Ark image models expose OpenAI-compatible chat/completions for image-to-image.
    # Keep this payload available via SEEDREAM_PROVIDER=ark_chat if your console sample uses chat.
    return {
        "model": DEFAULT_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_to_data_url(hand_image)}},
                    {"type": "image_url", "image_url": {"url": image_to_data_url(style_image)}},
                ],
            }
        ],
    }


def generate_seedream_tryon(hand_image, style_image, prompt=TRYON_PROMPT):
    key = get_api_key()
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    if DEFAULT_PROVIDER == "ark_chat":
        url = DEFAULT_BASE_URL + "/chat/completions"
        payload = build_ark_multimodal_payload(hand_image, style_image, prompt)
    else:
        url = DEFAULT_BASE_URL + DEFAULT_IMAGE_ENDPOINT
        payload = build_openai_compatible_payload(hand_image, style_image, prompt)

    print(f"[REQUEST] {url}")
    data = request_json("POST", url, headers=headers, json=payload)
    urls = find_image_urls(data)
    if urls:
        return urls[0], data

    task_id = find_task_id(data)
    if task_id:
        return poll_seedream_task(task_id)

    raise RuntimeError(f"Cannot find image URL or task id in response: {json.dumps(data, ensure_ascii=False)[:1200]}")


def poll_seedream_task(task_id, max_wait=300, interval=5):
    key = get_api_key()
    headers = {"Authorization": f"Bearer {key}"}
    candidates = [
        f"{DEFAULT_BASE_URL}/images/generations/{task_id}",
        f"{DEFAULT_BASE_URL}/tasks/{task_id}",
        f"{DEFAULT_BASE_URL}/generations/{task_id}",
    ]
    deadline = time.time() + max_wait
    last_data = None
    while time.time() < deadline:
        for url in candidates:
            try:
                data = request_json("GET", url, headers=headers)
            except Exception:
                continue
            last_data = data
            urls = find_image_urls(data)
            status = str((data.get("data", data) or {}).get("status", "")).lower() if isinstance(data, dict) else ""
            print(f"[POLL] {task_id} status={status or 'unknown'}")
            if urls:
                return urls[0], data
            if status in {"failed", "error", "cancelled"}:
                raise RuntimeError(f"Seedream task failed: {json.dumps(data, ensure_ascii=False)[:1200]}")
        time.sleep(interval)
    raise TimeoutError(f"Timed out waiting for Seedream task {task_id}; last={last_data}")



def build_single_image_payload(image_path, prompt):
    return {
        "model": DEFAULT_MODEL,
        "prompt": prompt,
        "image": [image_to_data_url(image_path)],
        "response_format": "url",
        "size": os.getenv("SEEDREAM_SIZE", "1536x2400"),
        "watermark": False,
    }


def generate_seedream_scene(image_path, scene_type="coffee", prompt=None):
    scene_prompt = prompt or SCENE_PROMPTS.get(scene_type) or SCENE_PROMPTS["coffee"]
    key = get_api_key()
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    url = DEFAULT_BASE_URL + DEFAULT_IMAGE_ENDPOINT
    payload = build_single_image_payload(image_path, scene_prompt)
    print(f"[SCENE REQUEST] {url} scene={scene_type}")
    data = request_json("POST", url, headers=headers, json=payload)
    urls = find_image_urls(data)
    if urls:
        return urls[0], data
    task_id = find_task_id(data)
    if task_id:
        return poll_seedream_task(task_id)
    raise RuntimeError(f"Cannot find image URL or task id in scene response: {json.dumps(data, ensure_ascii=False)[:1200]}")



def build_text_image_payload(prompt):
    return {
        "model": DEFAULT_MODEL,
        "prompt": prompt,
        "response_format": "url",
        "size": os.getenv("SEEDREAM_TREND_SIZE", os.getenv("SEEDREAM_SIZE", "1536x2400")),
        "watermark": False,
    }


def generate_seedream_text_image(prompt):
    key = get_api_key()
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    url = DEFAULT_BASE_URL + DEFAULT_IMAGE_ENDPOINT
    payload = build_text_image_payload(prompt)
    print(f"[TEXT IMAGE REQUEST] {url}")
    data = request_json("POST", url, headers=headers, json=payload)
    urls = find_image_urls(data)
    if urls:
        return urls[0], data
    task_id = find_task_id(data)
    if task_id:
        return poll_seedream_task(task_id)
    raise RuntimeError(f"Cannot find image URL or task id in text image response: {json.dumps(data, ensure_ascii=False)[:1200]}")


def run_batch(hand_image, style_dir, out_dir, limit=3, start=1):
    hand_image = Path(hand_image)
    style_dir = Path(style_dir)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    styles = sorted(
        p for p in style_dir.iterdir() if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
    )
    selected = styles[start - 1 : start - 1 + limit]
    if not selected:
        raise RuntimeError(f"No style images found in {style_dir}")

    manifest = []
    for style_image in selected:
        sid = style_image.stem
        print(f"[CASE] hand={hand_image.name} style={style_image.name}")
        result_url, raw = generate_seedream_tryon(hand_image, style_image)
        result_path = out_dir / f"seedream_hand01_style_{sid}.png"
        download_image(result_url, result_path)
        report = {
            "hand_image": str(hand_image),
            "style_image": str(style_image),
            "model": DEFAULT_MODEL,
            "provider": DEFAULT_PROVIDER,
            "result_url": result_url,
            "result_path": str(result_path),
            "raw_response": raw,
        }
        report_path = out_dir / f"seedream_hand01_style_{sid}.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        manifest.append(report)
        print(f"[SAVED] {result_path}")

    manifest_path = out_dir / "seedream_tryon_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest_path


def main():
    parser = argparse.ArgumentParser(description="Test Doubao Seedream 5.0 image-model nail try-on.")
    parser.add_argument("--hand-image", default=r"D:\AI_Project\nail\美甲图\手图URL\01.png")
    parser.add_argument("--style-dir", default=r"D:\AI_Project\nail\美甲图\款式图URL")
    parser.add_argument("--out-dir", default=r"D:\AI_Project\virtual-nail\data\output\seedream_tryon")
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--start", type=int, default=1)
    args = parser.parse_args()
    manifest = run_batch(args.hand_image, args.style_dir, args.out_dir, args.limit, args.start)
    print(f"manifest: {manifest}")


if __name__ == "__main__":
    main()
