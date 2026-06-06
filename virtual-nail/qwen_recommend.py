import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse

import requests
from PIL import Image
from dotenv import load_dotenv


APP_ROOT = Path(__file__).resolve().parent
load_dotenv(APP_ROOT / ".env")

QWEN_MODEL = os.getenv("QWEN_MODEL", "qwen3.6-plus")
QWEN_BASE_URL = os.getenv("QWEN_BASE_URL", "https://api.qwen.ai/v1").rstrip("/")
QWEN_RESPONSES_ENDPOINT = os.getenv("QWEN_RESPONSES_ENDPOINT", "/responses")
QWEN_SEARCH_TOOL = os.getenv("QWEN_SEARCH_TOOL", "t2i_search")
QWEN_TIMEOUT_SECONDS = float(os.getenv("QWEN_TIMEOUT_SECONDS", "25"))
INSPIRATION_CACHE = APP_ROOT / "data" / "inspiration_cache"


def qwen_enabled():
    return bool(os.getenv("QWEN_API_KEY") or os.getenv("DASHSCOPE_API_KEY"))


def get_qwen_key():
    key = os.getenv("QWEN_API_KEY") or os.getenv("DASHSCOPE_API_KEY")
    if not key:
        raise RuntimeError("Please set QWEN_API_KEY or DASHSCOPE_API_KEY in .env")
    return key


def safe_json_from_text(text):
    if not text:
        return None
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    match = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except Exception:
            pass
    start = text.find("{")
    end = text.rfind("}")
    if 0 <= start < end:
        try:
            return json.loads(text[start : end + 1])
        except Exception:
            return None
    return None


def collect_text(obj):
    texts = []
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, list):
        for item in obj:
            texts.extend(collect_text(item))
    elif isinstance(obj, dict):
        for key in ("output_text", "text", "content"):
            value = obj.get(key)
            if isinstance(value, str):
                texts.append(value)
            elif isinstance(value, (list, dict)):
                texts.extend(collect_text(value))
        for key in ("output", "data", "message", "choices"):
            if key in obj:
                texts.extend(collect_text(obj[key]))
    return texts


def looks_like_image_url(url):
    if not isinstance(url, str) or not url.startswith("http"):
        return False
    lower = url.lower().split("?")[0]
    return lower.endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp")) or "image" in lower


def collect_image_urls(obj):
    urls = []
    if isinstance(obj, str):
        if looks_like_image_url(obj):
            urls.append(obj)
        return urls
    if isinstance(obj, list):
        for item in obj:
            urls.extend(collect_image_urls(item))
    elif isinstance(obj, dict):
        for key in ("image_url", "thumbnail_url", "thumbnail", "content_url", "url"):
            value = obj.get(key)
            if isinstance(value, str) and looks_like_image_url(value):
                urls.append(value)
            elif isinstance(value, dict):
                urls.extend(collect_image_urls(value))
        for value in obj.values():
            if isinstance(value, (list, dict)):
                urls.extend(collect_image_urls(value))
    return list(dict.fromkeys(urls))


def download_image(url, prefix="qwen"):
    INSPIRATION_CACHE.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix not in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}:
        suffix = ".jpg"
    out_path = INSPIRATION_CACHE / f"{prefix}_{digest}{suffix}"

    if out_path.exists() and out_path.stat().st_size > 4096:
        try:
            with Image.open(out_path) as img:
                img.verify()
            return out_path
        except Exception:
            out_path.unlink(missing_ok=True)

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://www.google.com/",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=30)
        if not resp.ok:
            raise RuntimeError(f"download image failed: {resp.status_code}")
        content_type = resp.headers.get("content-type", "")
        if "image" not in content_type:
            raise RuntimeError(f"downloaded content is not an image: {content_type}")
        out_path.write_bytes(resp.content)
        if out_path.stat().st_size < 4096:
            raise RuntimeError("downloaded image is too small")
        with Image.open(out_path) as img:
            img.verify()
        return out_path
    except Exception:
        out_path.unlink(missing_ok=True)
        raise


def cached_external_inspirations(user_text, limit=3):
    files = sorted(
        [p for p in INSPIRATION_CACHE.glob("qwen_nail_*") if p.is_file() and p.stat().st_size > 4096],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    results = []
    for index, image_path in enumerate(files[:limit]):
        results.append(
            {
                "source": "qwen_cache",
                "source_label": "Qwen",
                "style_id": f"inspiration_cache_{hashlib.sha1(str(image_path).encode('utf-8')).hexdigest()[:10]}",
                "name": ["低饱和显白款", "通勤精致款", "温柔短甲款"][index % 3],
                "image_url": "",
                "source_url": "",
                "cached_image_path": str(image_path),
                "tags": ["显白", "通勤", "美甲"],
                "reason": [
                    f"这款低饱和色调比较耐看，和「{user_text or '当前偏好'}」的方向贴合，上手会显得干净温柔。",
                    f"这款线条和配色都比较日常，适合先试戴看看是否衬肤色，也不容易显得夸张。",
                    f"这款细节感更强，但整体不会太重，适合想要一点精致感又保持清爽的人。"
                ][index % 3],
            }
        )
    return results

def build_prompt(user_text, limit):
    return f"""
你是专业美甲趋势顾问。请根据用户需求，使用联网图片搜索找到 {limit} 张真实可访问的美甲效果图。

用户需求：{user_text or "显白、日常、适合通勤"}

要求：
1. 只推荐外部真实美甲图片，图片应包含手部或甲片上手效果，不要纯色块、Logo、商品包装图。
2. 优先选择近期流行、适合试戴迁移的清晰图片。
3. 款式名不要带“全网灵感”“店内推荐”等来源前缀，直接写款式名称，例如“低饱和奶茶短甲”。
4. 推荐理由要像真人美甲顾问，不要机械堆标签；说明为什么适合用户需求、肤色、场景和风格。
5. 只输出 JSON，不要 Markdown。格式：
{{
  "items": [
    {{
      "name": "低饱和奶茶短甲",
      "image_url": "https://...",
      "source_url": "https://...",
      "tags": ["通勤", "显白", "短甲"],
      "reason": "这款颜色柔和但不寡淡，日常上班不会突兀，也能让手部显得更干净。"
    }}
  ]
}}
""".strip()

def call_qwen_web_search(user_text, limit=2, tool_type=None):
    url = QWEN_BASE_URL + QWEN_RESPONSES_ENDPOINT
    headers = {"Authorization": f"Bearer {get_qwen_key()}", "Content-Type": "application/json"}
    payload = {
        "model": QWEN_MODEL,
        "input": build_prompt(user_text, limit),
        "tools": [{"type": tool_type or QWEN_SEARCH_TOOL}],
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=QWEN_TIMEOUT_SECONDS)
    if not resp.ok:
        raise RuntimeError(f"Qwen request failed: {resp.status_code} {resp.text[:800]}")
    return resp.json()


def normalize_items_from_response(data, user_text, limit=2):
    texts = collect_text(data)
    parsed = None
    for text_value in texts:
        parsed = safe_json_from_text(text_value)
        if parsed:
            break

    items = []
    if isinstance(parsed, dict) and isinstance(parsed.get("items"), list):
        items.extend(parsed["items"])

    image_urls = collect_image_urls(data)
    for index, image_url in enumerate(image_urls):
        if len(items) >= limit:
            break
        items.append(
            {
                "name": f"推荐款式 {index + 1}",
                "image_url": image_url,
                "source_url": image_url,
                "tags": ["美甲"],
                "reason": f"这张图片和「{user_text or '当前偏好'}」比较贴合，可以作为试戴参考。",
            }
        )

    normalized = []
    seen = set()
    for item in items:
        image_url = item.get("image_url") or item.get("thumbnail_url") or item.get("url")
        if not image_url or image_url in seen:
            continue
        seen.add(image_url)
        name = str(item.get("name") or f"推荐款式 {len(normalized) + 1}")
        name = name.replace("全网灵感：", "").replace("全网灵感:", "").replace("店内推荐：", "").strip()
        normalized.append(
            {
                "name": name,
                "image_url": image_url,
                "source_url": item.get("source_url") or image_url,
                "tags": item.get("tags") or ["美甲"],
                "reason": item.get("reason") or "这款整体风格与用户描述接近，适合先试戴观察上手效果。",
            }
        )
        if len(normalized) >= limit:
            break
    return normalized


def get_qwen_web_inspirations(user_text, limit=2):
    tools_to_try = [QWEN_SEARCH_TOOL]
    if QWEN_SEARCH_TOOL != "web_search":
        tools_to_try.append("web_search")

    all_errors = []
    for tool in tools_to_try:
        try:
            raw = call_qwen_web_search(user_text, limit=limit, tool_type=tool)
            items = normalize_items_from_response(raw, user_text, limit=max(limit * 3, 6))
            results = []
            errors = []
            for item in items:
                if len(results) >= limit:
                    break
                try:
                    cached_path = download_image(item["image_url"], prefix="qwen_nail")
                except Exception as exc:
                    errors.append({"image_url": item.get("image_url"), "error": str(exc)})
                    continue
                results.append(
                    {
                        "source": "web",
                        "source_label": "Qwen",
                        "style_id": f"inspiration_{hashlib.sha1(str(cached_path).encode('utf-8')).hexdigest()[:10]}",
                        "name": item["name"],
                        "image_url": item["image_url"],
                        "source_url": item.get("source_url") or item["image_url"],
                        "cached_image_path": str(cached_path),
                        "tags": item.get("tags") or ["美甲"],
                        "reason": item.get("reason") or "这款整体风格与用户描述接近，适合先试戴观察上手效果。",
                        "tool": tool,
                    }
                )
            if results:
                return results, raw
            all_errors.extend(errors)
        except Exception as exc:
            all_errors.append({"tool": tool, "error": str(exc)})

    cached = cached_external_inspirations(user_text, limit=limit)
    if cached:
        return cached, {"fallback": "qwen_cache", "errors": all_errors[:5]}

    raise RuntimeError("Qwen returned no downloadable image URLs: " + json.dumps(all_errors[:5], ensure_ascii=False))




def build_trend_prompt(user_text):
    return f"""
你是美甲趋势分析师。请联网搜索最近流行的美甲趋势，并结合用户需求生成一个可用于图像生成的趋势方案。

用户需求：{user_text or "显白、通勤、日常、精致"}

请只输出 JSON，不要 Markdown。字段如下：
{{
  "trend_title": "低饱和奶茶猫眼短甲",
  "keywords": ["低饱和", "奶茶色", "猫眼", "短甲", "通勤"],
  "colors": ["奶茶色", "裸粉", "香槟金"],
  "nail_shape": "短方圆",
  "design_elements": ["细闪猫眼", "微法式边", "通透底色"],
  "why_trending": "最近低饱和通勤风和细闪猫眼热度高，适合日常又有精致感。",
  "generation_prompt": "一只自然手部展示短方圆美甲，低饱和奶茶裸粉底色，细闪猫眼光泽，微法式边，干净高级，真实摄影，美甲清晰可见"
}}

要求：
1. 趋势必须适合真实美甲上手，不要夸张玄幻元素。
2. 生成 prompt 要适合图像模型生成一张清晰美甲参考图，手部自然，指甲占画面重点。
3. 不要出现品牌、文字、水印、Logo。
""".strip()


def get_qwen_trend_brief(user_text):
    raw = call_qwen_web_search(user_text, limit=1, tool_type="web_search")
    parsed = None
    for text_value in collect_text(raw):
        parsed = safe_json_from_text(text_value)
        if parsed:
            break
    if not isinstance(parsed, dict):
        parsed = {}
    title = parsed.get("trend_title") or "低饱和通勤美甲"
    keywords = parsed.get("keywords") or ["低饱和", "显白", "通勤", "短甲"]
    colors = parsed.get("colors") or ["奶茶色", "裸粉", "香槟金"]
    nail_shape = parsed.get("nail_shape") or "短方圆"
    design_elements = parsed.get("design_elements") or ["通透底色", "细闪", "微法式"]
    why = parsed.get("why_trending") or "低饱和显白风格适合日常通勤，兼顾清爽和精致感。"
    generation_prompt = parsed.get("generation_prompt") or ""
    if not generation_prompt:
        generation_prompt = "自然手部美甲展示，" + "，".join(colors + design_elements + [nail_shape]) + "，真实摄影，干净高级，指甲清晰可见"
    return {
        "trend_title": title,
        "keywords": keywords,
        "colors": colors,
        "nail_shape": nail_shape,
        "design_elements": design_elements,
        "why_trending": why,
        "generation_prompt": generation_prompt,
        "raw": raw,
    }



def build_style_design_prompt(preference_summary, count=3):
    count = max(1, min(int(count or 3), 3))
    return f"""
你是资深美甲设计师，不是生图提示词改写器。请先做“款式设计”，不要模仿固定示范句。

用户偏好：
{preference_summary}

请输出 {count} 个彼此有明显差异、但都适合真实上手和后续试戴的美甲设计方案。
只输出 JSON，不要 Markdown，不要解释。

每个方案必须先解决设计逻辑，再给视觉元素。字段如下：
{{
  "designs": [
    {{
      "name": "低饱和裸粉微法式猫眼",
      "concept": "一句话说明这个款式的设计核心",
      "target_scene": "通勤 / 约会 / 春夏 / 轻奢 / 甜酷等",
      "target_user": "适合什么手型、肤色或需求",
      "base_color": "主底色",
      "accent_color": "点缀色",
      "nail_shape": "甲型",
      "length": "长度",
      "finish": "质感，如果冻、猫眼、镜面、雾面、玻璃封层",
      "trend_basis": ["近期主流趋势1", "趋势2", "趋势3"],
      "finger_plan": {{
        "thumb": "拇指设计",
        "index": "食指设计",
        "middle": "中指设计",
        "ring": "无名指设计，通常可作为视觉重点",
        "pinky": "小指设计"
      }},
      "decoration_layout": "装饰密度、位置和留白逻辑",
      "material_keywords": ["果冻胶", "银白细闪", "金线", "珍珠"],
      "photo_scene": "适合生成图的手部姿态和背景",
      "reason": "像真人美甲顾问一样说明为什么推荐给用户",
      "avoid": "这个方案应该避免什么"
    }}
  ]
}}

设计要求：
1. 先考虑用户手型、长度、场景和显白需求，再决定款式，不要只堆高级词。
2. 三个方案要有差异：一个稳妥日常，一个趋势亮点，一个更个性或更精致。
3. 每根手指的图案不能完全一样；无名指/中指可以做视觉重点，但要保持可落地。
4. 趋势可以包含低饱和、裸粉、奶茶、微法式、猫眼、腮红渐变、银白细闪、金属线条、珍珠、蝴蝶结、极简几何等，但要按用户偏好选择。
5. 不要设计文字、Logo、夸张长甲、恐怖元素或难以迁移到试戴图的复杂背景。
""".strip()


def call_qwen_text(prompt):
    url = QWEN_BASE_URL + QWEN_RESPONSES_ENDPOINT
    headers = {"Authorization": f"Bearer {get_qwen_key()}", "Content-Type": "application/json"}
    payload = {"model": QWEN_MODEL, "input": prompt}
    resp = requests.post(url, headers=headers, json=payload, timeout=max(QWEN_TIMEOUT_SECONDS, 30))
    if not resp.ok:
        raise RuntimeError(f"Qwen request failed: {resp.status_code} {resp.text[:800]}")
    return resp.json()


def get_qwen_style_designs(preference_summary, count=3):
    raw = call_qwen_text(build_style_design_prompt(preference_summary, count=count))
    parsed = None
    for text_value in collect_text(raw):
        parsed = safe_json_from_text(text_value)
        if parsed:
            break
    designs = []
    if isinstance(parsed, dict) and isinstance(parsed.get("designs"), list):
        designs = parsed["designs"]
    elif isinstance(parsed, list):
        designs = parsed
    normalized = []
    for item in designs:
        if not isinstance(item, dict):
            continue
        normalized.append(item)
        if len(normalized) >= count:
            break
    if not normalized:
        raise RuntimeError("Qwen returned no usable style designs")
    return normalized, raw
