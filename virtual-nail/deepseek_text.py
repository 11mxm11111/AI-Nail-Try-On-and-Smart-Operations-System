import json
import os

import requests


DEFAULT_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
DEFAULT_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")


def deepseek_enabled():
    return bool(os.getenv("DEEPSEEK_API_KEY"))


def _extract_json(text):
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    start = min([idx for idx in [text.find("{"), text.find("[")] if idx >= 0], default=0)
    end_obj = text.rfind("}")
    end_arr = text.rfind("]")
    end = max(end_obj, end_arr)
    if end >= start:
        text = text[start : end + 1]
    return json.loads(text)


def call_deepseek_json(messages, temperature=0.2, max_tokens=900):
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY is not set")

    url = DEFAULT_BASE_URL.rstrip("/") + "/chat/completions"
    payload = {
        "model": DEFAULT_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
        "stream": False,
    }
    resp = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=45,
    )
    if not resp.ok:
        raise RuntimeError(f"DeepSeek API error {resp.status_code}: {resp.text[:500]}")
    content = resp.json()["choices"][0]["message"]["content"]
    return _extract_json(content)


def parse_preference_with_deepseek(user_text, selected_tags=None, hand_type=None):
    selected_tags = selected_tags or []
    messages = [
        {
            "role": "system",
            "content": (
                "你是美甲推荐系统的偏好解析器。"
                "只输出 JSON，不要输出解释。"
                "可选手型：修长手、肉肉手、骨节手、匀称手、短粗手、尖锥手。"
                "常见款式标签：通勤、日常、显白、温柔、甜酷、法式、猫眼、亮片、纯色、渐变、镜面、手绘、豹纹、格纹、立体钻饰、金箔、珍珠。"
                "颜色尽量输出短词，如 裸、粉、白、黑、红、绿、银、金、棕。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "user_text": user_text,
                    "selected_tags": selected_tags,
                    "confirmed_hand_type": hand_type,
                    "output_schema": {
                        "style_tags": ["款式/风格标签"],
                        "all_tags": ["包含场景和诉求的全部标签"],
                        "colors": ["颜色关键词"],
                        "length": "短/中/长/null",
                        "avoid_tags": ["用户明确不想要的元素"],
                        "intent_summary": "一句话总结用户偏好",
                    },
                },
                ensure_ascii=False,
            ),
        },
    ]
    data = call_deepseek_json(messages)
    return {
        "tags": data.get("style_tags") or [],
        "all_tags": data.get("all_tags") or data.get("style_tags") or [],
        "color": ",".join(data.get("colors") or []),
        "length": data.get("length") or None,
        "avoid_tags": data.get("avoid_tags") or [],
        "intent_summary": data.get("intent_summary") or user_text,
        "source": "deepseek",
    }


def explain_recommendations_with_deepseek(user_context, candidates):
    compact_candidates = []
    for item in candidates:
        compact_candidates.append(
            {
                "style_id": item.get("style_id"),
                "serial_no": item.get("serial_no"),
                "score": item.get("score"),
                "nail_shape": item.get("nail_shape"),
                "recommended_hand_type": item.get("recommended_hand_type"),
                "nail_length": item.get("nail_length"),
                "primary_style": item.get("primary_style"),
                "color_text": item.get("color_text"),
                "rule_reasons": item.get("reasons", []),
            }
        )
    messages = [
        {
            "role": "system",
            "content": (
                "你是专业美甲顾问。你只能基于候选款式生成推荐理由，不能编造不存在的款式、颜色或标签。"
                "输出 JSON，格式为 {\"recommendations\":[{\"style_id\":\"...\",\"rank\":1,\"reason\":\"60字以内\"}]}。"
                "推荐理由要自然、简短，解释为什么适合用户手型、场景和偏好。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "user_context": user_context,
                    "candidate_styles": compact_candidates,
                },
                ensure_ascii=False,
            ),
        },
    ]
    data = call_deepseek_json(messages, temperature=0.3, max_tokens=1200)
    return data.get("recommendations") or []


def template_explain(user_context, candidates):
    preferences = user_context.get("preferences", {})
    all_tags = "、".join(preferences.get("all_tags") or preferences.get("tags") or [])
    results = []
    for rank, item in enumerate(candidates, start=1):
        reason = (
            f"这款{item.get('color_text') or ''}{item.get('primary_style') or '款式'}"
            f"适合{item.get('recommended_hand_type') or '当前手型'}，"
            f"{item.get('nail_length') or ''}长度更容易上手"
        )
        if all_tags:
            reason += f"，也贴合你想要的{all_tags}"
        reason += "。"
        results.append({"style_id": item.get("style_id"), "rank": rank, "reason": reason[:80]})
    return results



def design_style_variants_with_deepseek(preference_summary, count=3):
    count = max(1, min(int(count or 3), 3))
    messages = [
        {
            "role": "system",
            "content": (
                "你是资深美甲设计师。你的任务是先做款式设计，不是改写生图提示词。"
                "必须输出 JSON 对象，格式为 {\"designs\":[...]}。"
                "每个方案要有明确设计逻辑、手指分配、材质、趋势依据和推荐理由。"
                "不要模仿固定句式，不要写成一整段漂亮形容词。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "preference_summary": preference_summary,
                    "count": count,
                    "output_schema": {
                        "designs": [
                            {
                                "name": "款式名",
                                "concept": "设计核心",
                                "target_scene": "适合场景",
                                "target_user": "适合手型/肤色/需求",
                                "base_color": "主底色",
                                "accent_color": "点缀色",
                                "nail_shape": "甲型",
                                "length": "长度",
                                "finish": "质感",
                                "trend_basis": ["趋势依据"],
                                "finger_plan": {
                                    "thumb": "拇指",
                                    "index": "食指",
                                    "middle": "中指",
                                    "ring": "无名指",
                                    "pinky": "小指",
                                },
                                "decoration_layout": "装饰布局",
                                "material_keywords": ["材质关键词"],
                                "photo_scene": "适合生图的手部姿态和背景",
                                "reason": "推荐理由",
                                "avoid": "避坑点",
                            }
                        ]
                    },
                    "design_rules": [
                        "三个方案要有差异：稳妥日常、趋势亮点、精致变化。",
                        "每根手指不能完全一样，无名指或中指可以做视觉重点。",
                        "设计必须适合真实上手和后续试戴迁移，不要夸张玄幻。",
                        "优先考虑显白、手型修饰、场景匹配，再决定装饰。",
                    ],
                },
                ensure_ascii=False,
            ),
        },
    ]
    data = call_deepseek_json(messages, temperature=0.45, max_tokens=2200)
    designs = data.get("designs") if isinstance(data, dict) else []
    normalized = []
    for item in designs or []:
        if isinstance(item, dict):
            normalized.append(item)
        if len(normalized) >= count:
            break
    if not normalized:
        raise RuntimeError("DeepSeek returned no usable style designs")
    return normalized, data
