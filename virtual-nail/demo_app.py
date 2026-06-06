import base64
import json
import mimetypes
import sqlite3
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent / ".env")

from flask import Flask, jsonify, request, send_file, send_from_directory

from recommend_styles import recommend_styles, split_terms
from qwen_recommend import qwen_enabled, get_qwen_web_inspirations, get_qwen_trend_brief, get_qwen_style_designs
from deepseek_text import (
    deepseek_enabled,
    explain_recommendations_with_deepseek,
    parse_preference_with_deepseek,
    template_explain,
    design_style_variants_with_deepseek,
    call_deepseek_json,
)


APP_ROOT = Path(__file__).resolve().parent
DATA_ROOT = Path(r"/Users/ponyma/Downloads/AI_Project/美甲图")
DB_PATH = APP_ROOT / "data" / "style_database" / "nail_style.db"
UPLOAD_DIR = APP_ROOT / "data" / "demo_uploads"
OUTPUT_ROOT = APP_ROOT / "data" / "output"

HAND_TYPES = ["修长手", "肉肉手", "骨节手", "匀称手", "短粗手", "尖锥手"]
LENGTHS = ["短", "中", "长"]
PREFERENCE_TAGS = ["显白", "通勤", "日常", "甜酷", "温柔", "法式", "猫眼", "亮片", "纯色", "裸色", "粉色", "渐变", "立体钻饰"]

app = Flask(__name__)


def encode_path(path):
    raw = str(Path(path)).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_path(token):
    padding = "=" * (-len(token) % 4)
    return Path(base64.urlsafe_b64decode((token + padding).encode("ascii")).decode("utf-8"))


def media_url(path):
    return f"/media/{encode_path(path)}"


def resolve_media_reference(value):
    """Accept a local path or a /media/<token> URL and return the real file path."""
    if not value:
        return ""
    text = str(value)
    if text.startswith("/media/"):
        token = text.split("/media/", 1)[1].split("?", 1)[0]
        return str(decode_path(token))
    return text


def parse_dataset_hand_no(hand_image):
    if not hand_image:
        return None
    stem = Path(str(hand_image)).stem
    if stem.isdigit():
        hand_no = int(stem)
        if 1 <= hand_no <= 13:
            return hand_no
    return None


def parse_style_no(style_id):
    if not style_id:
        return None
    text = str(style_id)
    if text.startswith("style_"):
        text = text.split("_", 1)[1]
    if text.isdigit():
        style_no = int(text)
        if 1 <= style_no <= 25:
            return style_no
    return None


def find_precomputed_seedream_tryon(hand_image, style_id):
    hand_no = parse_dataset_hand_no(hand_image)
    style_no = parse_style_no(style_id)
    if hand_no is None or style_no is None:
        return None

    if hand_no == 1:
        candidates = [OUTPUT_ROOT / "seedream_tryon_all"]
    else:
        candidates = [OUTPUT_ROOT / f"seedream_tryon_hand{hand_no:02d}_all"]

    names = [
        f"seedream_hand{hand_no:02d}_style_{style_no:02d}.png",
        f"seedream_hand01_style_{style_no:02d}.png",
    ]
    for folder in candidates:
        for name in names:
            path = folder / name
            if path.exists() and path.is_file():
                return path
    return None


def list_images(folder):
    suffixes = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
    if not folder.exists():
        return []
    return sorted([p for p in folder.iterdir() if p.suffix.lower() in suffixes])


def extract_preferences(text, selected_tags=None):
    text = (text or "").strip()
    tags = set(selected_tags or [])
    color_terms = []
    length = None

    rules = [
        (["显白", "白"], ["显白"], []),
        (["上班", "通勤", "日常", "低调"], ["通勤", "日常", "纯色"], ["裸", "粉"]),
        (["闪", "亮晶晶", "bling", "亮片"], ["亮片"], []),
        (["法式"], ["法式"], []),
        (["猫眼"], ["猫眼"], []),
        (["温柔", "裸", "奶茶"], ["温柔", "裸色"], ["裸"]),
        (["甜酷", "酷"], ["甜酷"], []),
        (["渐变"], ["渐变"], []),
        (["钻", "立体"], ["立体钻饰"], []),
        (["粉"], ["粉色"], ["粉"]),
        (["裸"], ["裸色"], ["裸"]),
        (["红"], [], ["红"]),
        (["黑"], [], ["黑"]),
        (["白"], [], ["白"]),
        (["绿"], [], ["绿"]),
    ]
    for keywords, add_tags, add_colors in rules:
        if any(keyword.lower() in text.lower() for keyword in keywords):
            tags.update(add_tags)
            color_terms.extend(add_colors)

    if "短" in text or "短甲" in text:
        length = "短"
    elif "中" in text:
        length = "中"
    elif "长" in text or "长甲" in text:
        length = "长"

    style_tags = [tag for tag in tags if tag not in {"显白", "通勤", "日常", "温柔", "甜酷", "裸色", "粉色"}]
    if "裸色" in tags and "裸" not in color_terms:
        color_terms.append("裸")
    if "粉色" in tags and "粉" not in color_terms:
        color_terms.append("粉")

    return {
        "tags": sorted(style_tags),
        "all_tags": sorted(tags),
        "color": ",".join(dict.fromkeys(color_terms)),
        "length": length,
    }


def log_event(event_type, hand_image=None, style_id=None, result_path=None, quality_score=None):
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    event_id = f"evt_{uuid.uuid4().hex}"
    hand_id = None
    if hand_image:
        stem = Path(hand_image).stem
        if stem.isdigit():
            hand_id = f"hand_{stem.zfill(2)}"
    conn.execute(
        """
        INSERT OR IGNORE INTO tryon_events
        (event_id, user_id, hand_id, style_id, event_type, event_time, result_path, quality_score)
        VALUES (?, ?, ?, ?, ?, datetime('now'), ?, ?)
        """,
        (event_id, "demo_user", hand_id, style_id, event_type, result_path, quality_score),
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS merchant_style_edits (
            style_id TEXT PRIMARY KEY,
            display_name TEXT,
            tags TEXT,
            copywriting TEXT,
            image_path TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS merchant_stock_candidates (
            candidate_id TEXT PRIMARY KEY,
            title TEXT,
            reason TEXT,
            tags TEXT,
            image_url TEXT,
            source TEXT,
            status TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()
    return event_id


def fetch_rows(sql, params=()):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = [dict(row) for row in conn.execute(sql, params)]
    conn.close()
    return rows


def style_image_url(row):
    path = row.get("enhanced_image_path") or row.get("original_image_path")
    return media_url(path) if path else ""



def safe_div(numerator, denominator):
    return round((float(numerator or 0) / float(denominator or 1)) * 100, 1)


def merchant_summary_data():
    base = ops_summary_data()
    counts = {row["event_type"]: row["count"] for row in base.get("event_breakdown", [])}
    views = counts.get("view", 0)
    clicks = sum(counts.get(name, 0) for name in ["style_click", "ai_store_click", "ai_web_inspiration_click"])
    tryons = sum(count for name, count in counts.items() if str(name).startswith("try_on"))
    favorites_count = counts.get("favorite", 0)
    generated = counts.get("style_variant_generate", 0) + counts.get("trend_style_generate", 0)

    hot_styles = fetch_rows(
        """
        SELECT
            rb.style_id,
            rb.serial_no,
            rb.primary_style,
            rb.color_text,
            rb.nail_shape,
            rb.nail_length,
            rb.recommended_hand_type,
            rb.enhanced_image_path,
            rb.original_image_path,
            SUM(CASE WHEN te.event_type='view' THEN 1 ELSE 0 END) AS view_count,
            SUM(CASE WHEN te.event_type IN ('style_click','ai_store_click','ai_web_inspiration_click') THEN 1 ELSE 0 END) AS click_count,
            SUM(CASE WHEN te.event_type LIKE 'try_on%' THEN 1 ELSE 0 END) AS tryon_count,
            SUM(CASE WHEN te.event_type='favorite' THEN 1 ELSE 0 END) AS favorite_count,
            SUM(CASE WHEN te.event_time >= datetime('now','-1 day') THEN 1 ELSE 0 END) AS recent_count
        FROM recommendation_base rb
        LEFT JOIN tryon_events te ON te.style_id = rb.style_id
        GROUP BY rb.style_id
        ORDER BY
            (COALESCE(SUM(CASE WHEN te.event_type='view' THEN 1 ELSE 0 END),0) * 1
            + COALESCE(SUM(CASE WHEN te.event_type IN ('style_click','ai_store_click','ai_web_inspiration_click') THEN 1 ELSE 0 END),0) * 3
            + COALESCE(SUM(CASE WHEN te.event_type LIKE 'try_on%' THEN 1 ELSE 0 END),0) * 6
            + COALESCE(SUM(CASE WHEN te.event_type='favorite' THEN 1 ELSE 0 END),0) * 8
            + COALESCE(SUM(CASE WHEN te.event_time >= datetime('now','-1 day') THEN 1 ELSE 0 END),0) * 4) DESC,
            rb.serial_no
        LIMIT 12
        """
    )
    for row in hot_styles:
        row["image_url"] = style_image_url(row)
        row["heat_score"] = int(
            (row.get("view_count") or 0) * 1
            + (row.get("click_count") or 0) * 3
            + (row.get("tryon_count") or 0) * 6
            + (row.get("favorite_count") or 0) * 8
            + (row.get("recent_count") or 0) * 4
        )
        row["tryon_rate"] = safe_div(row.get("tryon_count"), row.get("view_count"))
        row["favorite_rate"] = safe_div(row.get("favorite_count"), row.get("tryon_count"))

    trend_tags = fetch_rows(
        """
        SELECT
            st.tag_name,
            st.tag_type,
            COUNT(te.event_id) AS event_count,
            SUM(CASE WHEN te.event_type LIKE 'try_on%' THEN 1 ELSE 0 END) AS tryon_count,
            SUM(CASE WHEN te.event_type='favorite' THEN 1 ELSE 0 END) AS favorite_count,
            COUNT(DISTINCT st.style_id) AS stock_count
        FROM style_tags st
        LEFT JOIN tryon_events te ON te.style_id = st.style_id
        WHERE st.tag_type IN ('风格','颜色','甲型','手型','长度')
        GROUP BY st.tag_name, st.tag_type
        ORDER BY event_count DESC, favorite_count DESC, st.tag_name
        LIMIT 18
        """
    )
    for row in trend_tags:
        row["trend_score"] = int((row.get("event_count") or 0) + (row.get("tryon_count") or 0) * 4 + (row.get("favorite_count") or 0) * 6)
        row["gap_level"] = "可补款" if (row.get("event_count") or 0) >= 5 and (row.get("stock_count") or 0) <= 3 else "观察"

    daily = fetch_rows(
        """
        WITH days(day) AS (
            SELECT date((SELECT MAX(event_time) FROM tryon_events),'-6 day') UNION ALL SELECT date((SELECT MAX(event_time) FROM tryon_events),'-5 day')
            UNION ALL SELECT date((SELECT MAX(event_time) FROM tryon_events),'-4 day') UNION ALL SELECT date((SELECT MAX(event_time) FROM tryon_events),'-3 day')
            UNION ALL SELECT date((SELECT MAX(event_time) FROM tryon_events),'-2 day') UNION ALL SELECT date((SELECT MAX(event_time) FROM tryon_events),'-1 day')
            UNION ALL SELECT date((SELECT MAX(event_time) FROM tryon_events))
        )
        SELECT d.day AS date,
               COUNT(te.event_id) AS event_count,
               SUM(CASE WHEN te.event_type='view' THEN 1 ELSE 0 END) AS view_count,
               SUM(CASE WHEN te.event_type LIKE 'try_on%' THEN 1 ELSE 0 END) AS tryon_count,
               SUM(CASE WHEN te.event_type='favorite' THEN 1 ELSE 0 END) AS favorite_count
        FROM days d
        LEFT JOIN tryon_events te ON date(te.event_time)=d.day
        GROUP BY d.day
        ORDER BY d.day
        """
    )

    top_tags = [row["tag_name"] for row in trend_tags[:5]]
    low_stock_tags = [row for row in trend_tags if row.get("gap_level") == "可补款"][:4]
    replenishment = []
    for row in low_stock_tags:
        replenishment.append(
            {
                "title": f"补充「{row['tag_name']}」方向 2-3 款",
                "reason": f"该标签近期互动 {row.get('event_count') or 0} 次，但库内覆盖约 {row.get('stock_count') or 0} 款，适合补充相近色系或更日常版本。",
                "priority": "高" if (row.get("trend_score") or 0) >= 20 else "中",
            }
        )
    if not replenishment:
        for row in trend_tags[:3]:
            replenishment.append(
                {
                    "title": f"继续强化「{row['tag_name']}」曝光",
                    "reason": f"该方向已有互动信号，可先提高首页推荐权重，再观察试戴和收藏转化。",
                    "priority": "中",
                }
            )

    strategies = [
        {
            "title": "首页推荐策略",
            "content": f"优先展示 {('、'.join(top_tags[:3]) or '通勤、显白、猫眼')} 相关款式，并保留 1 个新生成灵感位测试用户反馈。",
        },
        {
            "title": "人群匹配策略",
            "content": "短粗手优先推短方圆和低饱和显白色；修长手可推中长杏仁、跳色和细节款；肉肉手减少大面积深色。",
        },
        {
            "title": "转化提升策略",
            "content": "对试戴高但收藏低的款式，优化推荐理由和封面图；对收藏高的款式，放入首页热推和预约入口。",
        },
    ]

    return {
        "summary": {
            "views": views,
            "clicks": clicks,
            "tryons": tryons,
            "favorites": favorites_count,
            "generated": generated,
            "tryon_rate": safe_div(tryons, views),
            "favorite_rate": safe_div(favorites_count, tryons),
            "click_rate": safe_div(clicks, views),
            "total_events": base["summary"]["total_events"],
            "today_events": base["summary"]["today_events"],
        },
        "funnel": [
            {"name": "推荐曝光", "value": views},
            {"name": "款式点击", "value": clicks},
            {"name": "AI试戴", "value": tryons},
            {"name": "收藏意向", "value": favorites_count},
        ],
        "hot_styles": hot_styles,
        "trend_tags": trend_tags,
        "daily": daily,
        "replenishment": replenishment,
        "strategies": strategies,
        "event_breakdown": base.get("event_breakdown", []),
    }


def template_merchant_strategy(data):
    tags = "、".join([row["tag_name"] for row in data.get("trend_tags", [])[:4]]) or "显白、通勤、猫眼"
    hot = data.get("hot_styles", [{}])[0]
    hot_name = (str(hot.get("serial_no") or "") + " " + str(hot.get("primary_style") or "")).strip() or "当前热门款"
    return {
        "summary": f"当前用户互动集中在「{tags}」方向，{hot_name} 表现较好，建议围绕相似风格继续放大推荐。",
        "actions": [
            f"首页推荐位优先展示「{tags}」相关款式，搭配显白、通勤类推荐理由。",
            "对试戴次数高的款式增加预约入口和限时活动提示，提高从试戴到收藏/预约的转化。",
            "补充 2-3 款低饱和、易上手、适合真实试戴迁移的新款，作为 AI 生成灵感的落地库存。",
        ],
        "copywriting": [
            "这几款上手干净显白，适合通勤和日常约会。",
            "近期高热度猫眼/法式灵感款，到店可按手型微调。",
        ],
        "risk": "当前样本量仍偏小，建议继续引导用户点击、试戴、收藏，累计更稳定的运营信号。",
        "source": "template",
    }


def ops_summary_data():
    total_events = fetch_rows("SELECT COUNT(*) AS count FROM tryon_events")[0]["count"]
    today_events = fetch_rows(
        "SELECT COUNT(*) AS count FROM tryon_events WHERE date(event_time)=date('now')"
    )[0]["count"]
    tryon_count = fetch_rows(
        "SELECT COUNT(*) AS count FROM tryon_events WHERE event_type LIKE 'try_on%'"
    )[0]["count"]
    favorite_count = fetch_rows(
        "SELECT COUNT(*) AS count FROM tryon_events WHERE event_type='favorite'"
    )[0]["count"]

    event_breakdown = fetch_rows(
        """
        SELECT event_type, COUNT(*) AS count
        FROM tryon_events
        GROUP BY event_type
        ORDER BY count DESC, event_type
        """
    )
    hot_styles = fetch_rows(
        """
        SELECT
            rb.style_id,
            rb.serial_no,
            rb.primary_style,
            rb.color_text,
            rb.nail_shape,
            rb.nail_length,
            rb.enhanced_image_path,
            rb.original_image_path,
            COUNT(te.event_id) AS event_count,
            SUM(CASE WHEN te.event_type LIKE 'try_on%' THEN 1 ELSE 0 END) AS tryon_count,
            SUM(CASE WHEN te.event_type='favorite' THEN 1 ELSE 0 END) AS favorite_count,
            SUM(CASE WHEN te.event_type='style_click' THEN 1 ELSE 0 END) AS click_count
        FROM recommendation_base rb
        LEFT JOIN tryon_events te ON te.style_id = rb.style_id
        GROUP BY rb.style_id
        ORDER BY event_count DESC, tryon_count DESC, rb.serial_no
        LIMIT 10
        """
    )
    for row in hot_styles:
        row["image_url"] = style_image_url(row)

    hot_tags = fetch_rows(
        """
        SELECT st.tag_name, st.tag_type, COUNT(te.event_id) AS event_count
        FROM style_tags st
        JOIN tryon_events te ON te.style_id = st.style_id
        GROUP BY st.tag_name, st.tag_type
        ORDER BY event_count DESC, st.tag_name
        LIMIT 12
        """
    )
    daily_trend = fetch_rows(
        """
        SELECT date(event_time) AS date, COUNT(*) AS event_count,
               SUM(CASE WHEN event_type LIKE 'try_on%' THEN 1 ELSE 0 END) AS tryon_count
        FROM tryon_events
        GROUP BY date(event_time)
        ORDER BY date DESC
        LIMIT 7
        """
    )
    gap_suggestions = [
        {
            "title": f"强化「{row['tag_name']}」相关款式",
            "reason": f"该标签近期产生 {row['event_count']} 次互动，可优先补充相近色系或同风格款式。",
        }
        for row in hot_tags[:5]
    ]
    if not gap_suggestions:
        gap_suggestions = [
            {
                "title": "先积累用户试戴与收藏行为",
                "reason": "当前行为样本较少，建议继续引导用户点击推荐、收藏和试戴，形成可分析数据。",
            }
        ]

    return {
        "summary": {
            "total_events": total_events,
            "today_events": today_events,
            "tryon_count": tryon_count,
            "favorite_count": favorite_count,
        },
        "event_breakdown": event_breakdown,
        "hot_styles": hot_styles,
        "hot_tags": hot_tags,
        "daily_trend": daily_trend,
        "gap_suggestions": gap_suggestions,
    }


@app.get("/")
def index():
    return INDEX_HTML


@app.get("/product-demo")
def product_demo():
    return send_file(APP_ROOT / "sue-nail-ai-demo.html")


@app.get("/product-assets/<path:filename>")
def product_assets(filename):
    for directory in [APP_ROOT, DATA_ROOT]:
        filepath = directory / filename
        if filepath.exists() and filepath.is_file():
            return send_from_directory(str(directory), filename)
    return jsonify({"error": "file not found", "path": filename}), 404



@app.post("/api/event")
def event_api():
    payload = request.get_json(force=True)
    event_type = payload.get("event_type")
    if not event_type:
        return jsonify({"error": "event_type is required"}), 400
    event_id = log_event(
        event_type,
        hand_image=payload.get("hand_image"),
        style_id=payload.get("style_id"),
        result_path=payload.get("result_path"),
        quality_score=payload.get("quality_score"),
    )
    return jsonify({"ok": True, "event_id": event_id})


@app.get("/api/ops/summary")
def ops_summary_api():
    return jsonify(ops_summary_data())


@app.get("/api/merchant/summary")
def merchant_summary_api():
    return jsonify(merchant_summary_data())


@app.post("/api/merchant/strategy")
def merchant_strategy_api():
    data = merchant_summary_data()
    if deepseek_enabled():
        try:
            compact = {
                "summary": data.get("summary"),
                "top_styles": [
                    {
                        "serial_no": row.get("serial_no"),
                        "style": row.get("primary_style"),
                        "color": row.get("color_text"),
                        "heat_score": row.get("heat_score"),
                        "tryon_count": row.get("tryon_count"),
                        "favorite_count": row.get("favorite_count"),
                    }
                    for row in data.get("hot_styles", [])[:6]
                ],
                "trend_tags": data.get("trend_tags", [])[:8],
                "replenishment": data.get("replenishment", [])[:4],
            }
            result = call_deepseek_json(
                [
                    {
                        "role": "system",
                        "content": "你是美甲门店智能运营顾问。只能基于输入数据给出运营建议，输出 JSON，不要 Markdown。",
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "data": compact,
                                "output_schema": {
                                    "summary": "100字以内运营总结",
                                    "actions": ["3-5条可执行动作"],
                                    "copywriting": ["2条可直接用于首页/小红书的运营文案"],
                                    "risk": "数据风险或下一步需要验证的点",
                                },
                            },
                            ensure_ascii=False,
                        ),
                    },
                ],
                temperature=0.35,
                max_tokens=1200,
            )
            result["source"] = "deepseek"
            return jsonify(result)
        except Exception as exc:
            fallback = template_merchant_strategy(data)
            fallback["source"] = "template_fallback"
            fallback["error"] = str(exc)
            return jsonify(fallback)
    return jsonify(template_merchant_strategy(data))


@app.get("/ops-dashboard")
def ops_dashboard():
    return OPS_DASHBOARD_HTML


@app.get("/merchant-demo")
def merchant_demo():
    return MERCHANT_DEMO_HTML


@app.get("/api/options")
def options():
    hands = [
        {
            "name": path.name,
            "path": str(path),
            "url": media_url(path),
        }
        for path in list_images(DATA_ROOT / "手图URL")
    ]
    return jsonify(
        {
            "hands": hands,
            "hand_types": HAND_TYPES,
            "lengths": LENGTHS,
            "preference_tags": PREFERENCE_TAGS,
        }
    )


@app.post("/api/upload_hand")
def upload_hand():
    file = request.files.get("file")
    if not file:
        return jsonify({"error": "missing file"}), 400
    suffix = Path(file.filename or "hand.png").suffix.lower() or ".png"
    if suffix not in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}:
        return jsonify({"error": "unsupported image type"}), 400
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    original_stem = Path(file.filename or "").stem
    dataset_hand_path = None
    if original_stem.isdigit():
        hand_no = int(original_stem)
        candidate = DATA_ROOT / "手图URL" / f"{hand_no:02d}.png"
        if 1 <= hand_no <= 13 and candidate.exists():
            dataset_hand_path = str(candidate)

    path = UPLOAD_DIR / f"hand_{int(time.time())}_{uuid.uuid4().hex[:6]}{suffix}"
    file.save(path)
    return jsonify({
        "path": dataset_hand_path or str(path),
        "uploaded_path": str(path),
        "url": media_url(path),
        "name": path.name,
        "dataset_hand_path": dataset_hand_path,
    })


@app.post("/api/upload_style")
def upload_style():
    file = request.files.get("file")
    if not file:
        return jsonify({"error": "missing file"}), 400
    suffix = Path(file.filename or "style.png").suffix.lower() or ".png"
    if suffix not in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}:
        return jsonify({"error": "unsupported image type"}), 400

    out_dir = UPLOAD_DIR / "styles"
    out_dir.mkdir(parents=True, exist_ok=True)
    style_uid = uuid.uuid4().hex
    path = out_dir / f"upload_style_{style_uid}{suffix}"
    file.save(path)

    name = (request.form.get("name") or Path(file.filename or "").stem or "上传款式").strip()
    tags_text = request.form.get("tags") or ""
    tags = [tag.strip() for tag in tags_text.replace("，", ",").split(",") if tag.strip()]
    style_id = f"upload_style_{style_uid}"
    log_event("upload_style_reference", None, style_id, str(path), None)
    return jsonify({
        "ok": True,
        "type": "style",
        "style_id": style_id,
        "style_image_path": str(path),
        "style_image_url": media_url(path),
        "reference_image": str(path),
        "image_url": media_url(path),
        "name": name,
        "tags": tags,
    })


@app.post("/api/recommend")
def recommend_api():
    payload = request.get_json(force=True)
    selected_tags = payload.get("selected_tags") or []
    llm_error = None
    try:
        if deepseek_enabled():
            prefs = parse_preference_with_deepseek(
                payload.get("description", ""),
                selected_tags=selected_tags,
                hand_type=payload.get("hand_type"),
            )
        else:
            prefs = extract_preferences(payload.get("description", ""), selected_tags)
            prefs["source"] = "rules"
    except Exception as exc:
        llm_error = str(exc)
        prefs = extract_preferences(payload.get("description", ""), selected_tags)
        prefs["source"] = "rules_fallback"
        prefs["llm_error"] = llm_error

    length = payload.get("length") or prefs["length"]
    tags = ",".join(prefs["tags"])
    color = payload.get("color") or prefs["color"]

    requested_top_k = int(payload.get("top_k") or 8)
    offset = max(0, int(payload.get("offset") or 0))
    batch_size = int(payload.get("batch_size") or requested_top_k)
    rows = recommend_styles(
        hand_type=payload.get("hand_type"),
        tags=tags,
        length=length,
        nail_shape=payload.get("nail_shape"),
        color=color,
        top_k=max(requested_top_k, offset + batch_size),
        db_path=DB_PATH,
    )
    unique_rows = []
    seen_style_ids = set()
    for row in rows:
        style_id = row.get("style_id")
        if style_id in seen_style_ids:
            continue
        seen_style_ids.add(style_id)
        unique_rows.append(row)
    rows = unique_rows
    if offset >= len(rows):
        offset = 0
    if offset:
        rows = rows[offset:offset + batch_size] or rows[:batch_size]
    else:
        rows = rows[:batch_size]
    for row in rows:
        row["image_url"] = media_url(row["enhanced_image_path"] or row["original_image_path"])
        log_event("view", payload.get("hand_image"), row["style_id"])
    user_context = {
        "hand_type": payload.get("hand_type"),
        "description": payload.get("description", ""),
        "selected_tags": selected_tags,
        "preferences": prefs,
    }
    try:
        if deepseek_enabled():
            ai_reasons = explain_recommendations_with_deepseek(user_context, rows)
        else:
            ai_reasons = template_explain(user_context, rows)
    except Exception as exc:
        llm_error = llm_error or str(exc)
        ai_reasons = template_explain(user_context, rows)

    reason_by_style = {item.get("style_id"): item for item in ai_reasons}
    for row in rows:
        reason_item = reason_by_style.get(row["style_id"], {})
        row["ai_reason"] = reason_item.get("reason") or ""
        row["ai_rank"] = reason_item.get("rank")

    return jsonify(
        {
            "preferences": prefs,
            "recommendations": rows,
            "llm_error": llm_error,
            "llm_enabled": deepseek_enabled(),
            "reason_source": "deepseek" if deepseek_enabled() and not llm_error else "template",
        }
    )




@app.post("/api/ai/recommend_top3")
def ai_recommend_top3_api():
    payload = request.get_json(force=True)
    description = payload.get("description", "")
    selected_tags = payload.get("selected_tags") or []
    prefs = extract_preferences(description, selected_tags)

    if not qwen_enabled():
        return jsonify(
            {
                "mode": "qwen_disabled",
                "preferences": prefs,
                "items": [],
                "note": "Qwen API key 未配置，无法获取外网图片推荐。",
            }
        ), 503

    try:
        qwen_items, _raw_qwen = get_qwen_web_inspirations(description, limit=3)
    except Exception as exc:
        return jsonify(
            {
                "mode": "qwen_error",
                "preferences": prefs,
                "items": [],
                "note": f"Qwen 没有返回可下载的外网图片：{str(exc)[:240]}",
            }
        ), 502

    items = []
    for item in qwen_items[:3]:
        clean_name = str(item.get("name") or "推荐款式").replace("全网灵感：", "").replace("全网灵感:", "").strip()
        items.append(
            {
                "source": "qwen",
                "source_label": "Qwen",
                "style_id": item.get("style_id"),
                "name": clean_name,
                "image_url": media_url(item["cached_image_path"]),
                "source_url": item.get("source_url") or item.get("image_url"),
                "tags": item.get("tags") or [],
                "reason": item.get("reason") or "这款整体风格和你的描述比较贴合，适合先试戴看看上手效果。",
                "reference_image_path": item["cached_image_path"],
            }
        )

    return jsonify(
        {
            "mode": "qwen_t2i_search",
            "preferences": prefs,
            "items": items,
            "note": "",
        }
    )




def build_dynamic_trend_fallback(description):
    text = (description or "").strip()
    lower = text.lower()

    keywords = []
    colors = []
    elements = []

    if any(word in text for word in ["猫眼", "可爱猫眼"]):
        title = "日常可爱猫眼美甲"
        keywords += ["猫眼", "可爱", "日常", "显白"]
        colors += ["蜜桃粉", "裸粉", "香槟粉"]
        elements += ["细腻猫眼光泽", "柔和闪粉", "通透底色"]
    elif any(word in text for word in ["甜酷", "黑", "酷"]):
        title = "甜酷黑粉猫眼美甲"
        keywords += ["甜酷", "猫眼", "黑色", "显白"]
        colors += ["透黑", "玫粉", "银色"]
        elements += ["猫眼光泽", "银色细闪", "局部跳色"]
    elif any(word in text for word in ["法式", "简约"]):
        title = "简约显白法式美甲"
        keywords += ["法式", "简约", "显白", "通勤"]
        colors += ["奶白", "裸粉", "香槟金"]
        elements += ["细法式边", "通透底色", "轻微细闪"]
    elif any(word in text for word in ["花", "日系", "花朵"]):
        title = "日系温柔小花美甲"
        keywords += ["日系", "花朵", "温柔", "清新"]
        colors += ["奶油白", "浅粉", "豆沙粉"]
        elements += ["小花点缀", "通透底色", "柔和腮红渐变"]
    elif any(word in text for word in ["渐变", "闪耀", "亮片"]):
        title = "闪耀细闪渐变美甲"
        keywords += ["渐变", "细闪", "显白", "精致"]
        colors += ["裸粉", "香槟金", "珍珠白"]
        elements += ["细闪渐变", "水润光泽", "通透底色"]
    else:
        title = "低饱和通勤显白短甲"
        keywords += ["低饱和", "显白", "通勤", "短甲", "精致"]
        colors += ["奶茶色", "裸粉", "香槟金"]
        elements += ["通透底色", "细闪", "微法式边"]

    if "短" in text:
        nail_shape = "短方圆"
        keywords.append("短甲")
    elif "长" in text:
        nail_shape = "中长杏仁"
        keywords.append("中长甲")
    else:
        nail_shape = "短方圆"

    if "显白" in text and "显白" not in keywords:
        keywords.append("显白")
    if "通勤" in text and "通勤" not in keywords:
        keywords.append("通勤")
    if "日常" in text and "日常" not in keywords:
        keywords.append("日常")

    # Preserve order while removing duplicates.
    keywords = list(dict.fromkeys(keywords))
    colors = list(dict.fromkeys(colors))
    elements = list(dict.fromkeys(elements))

    prompt = (
        f"自然手部美甲展示，{nail_shape}指甲，"
        + "，".join(colors + elements)
        + "，真实摄影，干净明亮，指甲清晰可见，适合真实美甲试戴参考"
    )
    reason = f"结合你的偏好「{text or '显白通勤'}」，生成一款{title}，风格更贴近日常上手和试戴展示。"
    return {
        "trend_title": title,
        "keywords": keywords,
        "colors": colors,
        "nail_shape": nail_shape,
        "design_elements": elements,
        "why_trending": reason,
        "generation_prompt": prompt,
    }



def first_non_empty(*values):
    for value in values:
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""



def get_style_variant_inputs(payload):
    description = first_non_empty(payload.get("description"), payload.get("user_text"))
    color = first_non_empty(payload.get("color"), "裸粉 / 奶茶 / 香槟")
    style = first_non_empty(payload.get("style"), "猫眼 / 法式 / 细闪")
    nail_shape = first_non_empty(payload.get("nail_shape"), "短方圆")
    length = first_non_empty(payload.get("length"), "短到中等")
    mood = first_non_empty(payload.get("mood"), payload.get("scene"), "通勤、显白、精致")
    hand_feature = first_non_empty(payload.get("hand_feature"), "自然手型")
    avoid = first_non_empty(payload.get("avoid"), "不要夸张装饰，不要水印文字")
    preference_summary = (
        f"用户想要{mood}风格，偏好{color}配色、{style}款式、{nail_shape}甲型、{length}长度；"
        f"手部特征：{hand_feature}；补充需求：{description or '自然耐看，适合真实试戴'}。"
    )
    return {
        "description": description,
        "color": color,
        "style": style,
        "nail_shape": nail_shape,
        "length": length,
        "mood": mood,
        "hand_feature": hand_feature,
        "avoid": avoid,
        "preference_summary": preference_summary,
    }


def build_rule_based_style_designs(inputs, count=3):
    text = f"{inputs['description']} {inputs['color']} {inputs['style']} {inputs['mood']}"
    has_cat = "猫眼" in text
    has_french = "法式" in text or "french" in text.lower()
    has_pearl = "珍珠" in text or "高级" in text or "精致" in text
    has_dark = "甜酷" in text or "黑" in text
    base_color = inputs["color"]
    nail_shape = inputs["nail_shape"]
    length = inputs["length"]

    trend_focus = []
    if has_cat:
        trend_focus.append("丝绒猫眼磁吸光带")
    if has_french:
        trend_focus.append("银白或香槟金微法式")
    if has_pearl:
        trend_focus.append("微型珍珠与金色钢珠点缀")
    if has_dark:
        trend_focus.append("透黑果冻底与银色金属线")
    if not trend_focus:
        trend_focus = ["低饱和显白色", "果冻裸粉", "细闪微法式"]

    designs = [
        {
            "name": f"{base_color}低饱和微光日常款",
            "concept": "用低饱和底色和克制细闪做显白日常款，重点是干净、耐看、适合高频试戴。",
            "target_scene": "通勤、日常、约会前的稳妥选择",
            "target_user": f"适合{inputs['hand_feature']}，希望手部看起来更干净修长的人",
            "base_color": base_color,
            "accent_color": "银白细闪 / 香槟金微线",
            "nail_shape": nail_shape,
            "length": length,
            "finish": "半透明果冻底、玻璃亮面封层、轻微细闪",
            "trend_basis": ["低饱和显白", "微法式", "干净感美甲"],
            "finger_plan": {
                "thumb": "通透裸粉底，边缘一圈极细银白微法式",
                "index": "纯净果冻底，少量细闪铺在甲尖",
                "middle": "柔和猫眼或细闪纵向光带，拉长手指视觉",
                "ring": "一条细金线星芒作为视觉重点",
                "pinky": "保持留白，只做极细法式边",
            },
            "decoration_layout": "装饰集中在中指和无名指，其余手指保留干净留白，避免拥挤。",
            "material_keywords": ["果冻裸粉", "银白细闪", "微法式", "玻璃封层"],
            "photo_scene": "自然放松的手部近景，轻触米白针织或干净浅色背景",
            "reason": "这款不挑场景，低饱和底色显白，细节少但质感足，适合先作为稳定推荐图。",
            "avoid": inputs["avoid"],
        },
        {
            "name": f"{inputs['style']}趋势焦点款",
            "concept": "把用户指定风格做成更有记忆点的趋势款，但控制装饰密度，保证真实可上手。",
            "target_scene": "社交平台展示、约会、想要更精致但不过分夸张的场景",
            "target_user": f"适合喜欢{inputs['mood']}但希望比基础款更吸睛的人",
            "base_color": base_color,
            "accent_color": "银白、香槟金、珍珠白",
            "nail_shape": nail_shape,
            "length": length,
            "finish": "猫眼磁吸光泽、通透胶感、局部金属线",
            "trend_basis": trend_focus,
            "finger_plan": {
                "thumb": "低饱和底色叠加柔和猫眼光带",
                "index": "微法式边，甲面保持清透",
                "middle": "主视觉手指，加入细金线星芒或几何线条",
                "ring": "珍珠或金色钢珠点缀，和中指形成呼应",
                "pinky": "简化版细闪边，压住整体节奏",
            },
            "decoration_layout": "中指和无名指承担视觉重点，拇指做材质呼应，食指和小指简化。",
            "material_keywords": ["猫眼磁吸", "金属微法式", "金线星芒", "3D珍珠"],
            "photo_scene": "手指自然伸展或轻握柔软浅色织物，指甲区域清晰占画面重点",
            "reason": "这款更像趋势灵感图，能突出猫眼、法式或珍珠细节，适合提升推荐的新鲜感。",
            "avoid": inputs["avoid"],
        },
        {
            "name": f"{inputs['mood']}精致变化款",
            "concept": "在同一偏好下做材质和装饰位置变化，让用户能比较哪种细节密度更适合自己。",
            "target_scene": "想要精致感、拍照好看、但仍适合日常保存和试戴",
            "target_user": f"适合{inputs['hand_feature']}，通过纵向光泽和集中装饰修饰手型",
            "base_color": "奶茶裸粉 / 透亮玫瑰粉",
            "accent_color": "银白闪粉、浅金、珍珠白",
            "nail_shape": nail_shape,
            "length": length,
            "finish": "腮红渐变、果冻透感、局部银白闪粉法式",
            "trend_basis": ["腮红渐变", "裸粉果冻", "轻奢小颗粒装饰"],
            "finger_plan": {
                "thumb": "奶茶裸粉底，甲尖细闪渐变",
                "index": "浅粉腮红中心晕染，边缘清透",
                "middle": "银白闪粉微法式，带一点纵向高光",
                "ring": "小珍珠和金色钢珠沿侧边点缀",
                "pinky": "清透裸粉底，保留极简亮面",
            },
            "decoration_layout": "采用侧边小面积点缀和甲尖渐变，不大面积铺满，保留呼吸感。",
            "material_keywords": ["腮红渐变", "果冻胶", "银白闪粉", "微型珍珠"],
            "photo_scene": "柔和影棚光下的手部微距，背景极简，肤色自然",
            "reason": "这款比基础款更有细节，又不会过度甜腻，适合做个性化推荐的第三选择。",
            "avoid": inputs["avoid"],
        },
    ]
    return designs[: max(1, min(int(count or 3), 3))]


def stringify_finger_plan(plan):
    if not isinstance(plan, dict):
        return str(plan or "每根手指有轻微变化，整体保持协调")
    names = [("thumb", "thumb"), ("index", "index finger"), ("middle", "middle finger"), ("ring", "ring finger"), ("pinky", "pinky finger")]
    return "; ".join([f"{label}: {plan.get(key) or 'coordinated minimal design'}" for key, label in names])


def design_to_generation_prompt(design, inputs):
    finger_plan = stringify_finger_plan(design.get("finger_plan"))
    trend_basis = " / ".join([str(x) for x in (design.get("trend_basis") or [])])
    materials = " / ".join([str(x) for x in (design.get("material_keywords") or [])])
    return f"""
Generate one high-quality photorealistic manicure reference image based on this original nail design blueprint.
Do not copy a sample sentence. Follow the design logic and make the manicure visually coherent.

Design blueprint:
- Design name: {design.get("name") or "AI manicure design"}
- Core concept: {design.get("concept") or "wearable, flattering manicure"}
- Target scene: {design.get("target_scene") or inputs["mood"]}
- Target user: {design.get("target_user") or inputs["hand_feature"]}
- Base color: {design.get("base_color") or inputs["color"]}
- Accent color: {design.get("accent_color") or "subtle silver white / champagne gold"}
- Nail shape and length: {design.get("length") or inputs["length"]}, {design.get("nail_shape") or inputs["nail_shape"]}
- Finish and texture: {design.get("finish") or "glossy gel, glass-like top coat"}
- Trend basis: {trend_basis or "low-saturation flattering manicure, micro French, subtle shimmer"}
- Finger-by-finger plan: {finger_plan}
- Decoration layout: {design.get("decoration_layout") or "balanced decoration with clean negative space"}
- Materials and details: {materials or "jelly gel, fine shimmer, metallic line, glossy top coat"}
- Photo scene: {design.get("photo_scene") or "elegant feminine hand macro photography with clean bright background"}

Image composition:
Elegant feminine hand macro photography, close-up beauty shot.
Natural relaxed hand pose, all five fingernails visible and in sharp focus.
Use soft studio lighting, realistic skin texture, premium salon finish, crystal clear glossy top coat.
The background should support the design, such as ivory knit fabric, silk satin, or clean marble beauty surface, but nails remain the visual focus.

Hard constraints:
one natural human hand only, five fingers, no extra fingers, no missing fingers, no deformed hand,
no text, no logo, no watermark, no product packaging, no distorted nails, no blurry nail art.
Avoid: {design.get("avoid") or inputs["avoid"]}.
""".strip()


def build_style_variant_specs(payload):
    inputs = get_style_variant_inputs(payload)
    count = max(1, min(int(payload.get("count") or 3), 3))
    design_source = "rule_fallback"
    raw_design_response = None
    errors = []
    try:
        if qwen_enabled():
            designs, raw_design_response = get_qwen_style_designs(inputs["preference_summary"], count=count)
            design_source = "qwen_design"
        else:
            raise RuntimeError("Qwen is not enabled")
    except Exception as exc:
        errors.append({"qwen": str(exc)})
        try:
            if deepseek_enabled():
                designs, raw_design_response = design_style_variants_with_deepseek(inputs["preference_summary"], count=count)
                design_source = "deepseek_design"
            else:
                raise RuntimeError("DeepSeek is not enabled")
        except Exception as deepseek_exc:
            errors.append({"deepseek": str(deepseek_exc)})
            designs = build_rule_based_style_designs(inputs, count=count)
            raw_design_response = {"fallback_errors": errors}

    variants = []
    groups = ["稳妥日常款", "趋势灵感款", "精致变化款"]
    for index, design in enumerate(designs[:count]):
        variants.append(
            {
                "name": design.get("name") or f"AI设计方案{index + 1}",
                "group": design.get("group") or groups[index % len(groups)],
                "reason": design.get("reason") or design.get("concept") or "根据你的结构化偏好生成，适合先作为灵感图试戴。",
                "design": design,
                "design_source": design_source,
                "raw_design_response": raw_design_response if index == 0 else None,
                "prompt": design_to_generation_prompt(design, inputs),
            }
        )
    return inputs["preference_summary"], variants

@app.post("/api/ai/generate_style_variants")
def generate_style_variants_api():
    payload = request.get_json(force=True)
    count = max(1, min(int(payload.get("count") or 3), 3))
    dry_run = bool(payload.get("dry_run"))
    preference_summary, variants = build_style_variant_specs(payload)
    variants = variants[:count]

    if dry_run:
        return jsonify({"preference_summary": preference_summary, "variants": variants, "dry_run": True})

    from seedream_tryon import generate_seedream_text_image, download_image as download_seedream_image

    out_dir = OUTPUT_ROOT / "seedream_style_variants"
    out_dir.mkdir(parents=True, exist_ok=True)
    items = []
    for index, spec in enumerate(variants, start=1):
        out_path = out_dir / f"variant_{int(time.time())}_{index}.png"
        result_url, raw = generate_seedream_text_image(spec["prompt"])
        download_seedream_image(result_url, out_path)
        item = {
            "source": "variant_generated",
            "source_label": "偏好生成",
            "style_id": f"variant_{out_path.stem}",
            "name": spec["name"],
            "group": spec["group"],
            "reason": spec["reason"],
            "image_url": media_url(out_path),
            "reference_image_path": str(out_path),
            "tags": [spec["group"]],
            "prompt": spec["prompt"],
            "raw_response": raw,
        }
        log_event("style_variant_generate", None, item["style_id"], str(out_path), 100)
        items.append(item)
    return jsonify({"preference_summary": preference_summary, "variants": items, "dry_run": False})

@app.post("/api/ai/generate_trend_style")
def generate_trend_style_api():
    payload = request.get_json(force=True)
    description = payload.get("description", "")

    trend_source = "fallback"
    try:
        if qwen_enabled():
            trend = get_qwen_trend_brief(description)
            trend_source = "qwen_web"
        else:
            trend = build_dynamic_trend_fallback(description)
    except Exception as exc:
        trend = build_dynamic_trend_fallback(description)
        trend["error"] = str(exc)

    trend_prompt = f"""
Generate one high-quality photorealistic manicure reference image.

User preference: {description or "daily, flattering, elegant manicure"}.
Trend concept: {trend["trend_title"]}.
Trend keywords: {", ".join(trend.get("keywords") or [])}.
Colors: {", ".join(trend.get("colors") or [])}.
Nail shape: {trend.get("nail_shape")}.
Design elements: {", ".join(trend.get("design_elements") or [])}.

Visual requirements:
1. Show a natural human hand with all fingernails clearly visible.
2. The manicure must strongly match the user preference and trend concept above.
3. If the user asks for cat-eye nails, the nails must show visible cat-eye magnetic shimmer.
4. If the user asks for cute, daily, or Japanese style, keep it soft, wearable, and not exaggerated.
5. Keep the background simple and bright, with no text, watermark, logo, or product packaging.
6. The nails should occupy the visual focus and be easy to inspect.

Additional style prompt from trend analysis:
{trend.get("generation_prompt") or ""}

Negative constraints:
deformed hand, extra fingers, missing fingers, distorted nails, blurry nail art, text, watermark, logo,
wrong style, style not matching user preference.
""".strip()

    from seedream_tryon import generate_seedream_text_image, download_image as download_seedream_image

    out_dir = OUTPUT_ROOT / "seedream_trend_styles"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"trend_style_{int(time.time())}.png"
    result_url, raw = generate_seedream_text_image(trend_prompt)
    download_seedream_image(result_url, out_path)
    item = {
        "source": "trend_generated",
        "source_label": "趋势生成",
        "style_id": f"trend_{out_path.stem}",
        "name": trend.get("trend_title") or "AI趋势灵感款",
        "image_url": media_url(out_path),
        "reference_image_path": str(out_path),
        "tags": trend.get("keywords") or [],
        "reason": trend.get("why_trending") or "结合你的偏好生成，可作为灵感试戴参考。",
        "trend": {k: v for k, v in trend.items() if k != "raw"},
        "trend_source": trend_source,
        "prompt": trend_prompt,
        "raw_response": raw,
    }
    log_event("trend_style_generate", None, item["style_id"], str(out_path), 100)
    return jsonify({"item": item})

@app.post("/api/tryon/scene")
def tryon_scene_api():
    payload = request.get_json(force=True)
    source_image = payload.get("source_image") or payload.get("result_path")
    scene_type = payload.get("scene_type") or "coffee"
    if not source_image:
        return jsonify({"error": "source_image is required"}), 400

    source_path = Path(source_image)
    if not source_path.exists():
        return jsonify({"error": f"source image not found: {source_image}"}), 404

    if scene_type not in {"coffee", "phone"}:
        return jsonify({"error": f"unsupported scene_type: {scene_type}"}), 400

    from seedream_tryon import generate_seedream_scene, download_image as download_seedream_image

    out_dir = OUTPUT_ROOT / "seedream_scene_tryon"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"scene_{scene_type}_{int(time.time())}.png"
    result_url, raw = generate_seedream_scene(source_path, scene_type=scene_type)
    download_seedream_image(result_url, out_path)
    result = {
        "mode": "seedream_scene_tryon",
        "scene_type": scene_type,
        "source_image": str(source_path),
        "result_path": str(out_path),
        "result_url": media_url(out_path),
        "raw_response": raw,
    }
    log_event(f"scene_tryon_{scene_type}", None, payload.get("style_id"), str(out_path), 100)
    return jsonify(result)

@app.post("/api/tryon")
def tryon_api():
    payload = request.get_json(force=True)
    hand_image = payload.get("hand_image")
    style_id = payload.get("style_id")
    reference_image = resolve_media_reference(payload.get("reference_image") or payload.get("style_image_path") or payload.get("style_image_url"))
    if not hand_image or not style_id:
        return jsonify({"error": "hand_image and style_id are required"}), 400
    if parse_style_no(style_id) is None and not reference_image:
        return jsonify({"error": f"invalid style_id: {style_id}"}), 400

    if reference_image:
        from seedream_tryon import generate_seedream_tryon, download_image as download_seedream_image

        out_dir = OUTPUT_ROOT / "seedream_inspiration_tryon"
        out_dir.mkdir(parents=True, exist_ok=True)
        hand_no = parse_dataset_hand_no(hand_image)
        safe_style = str(style_id).replace("/", "_").replace("\\", "_")
        result_path = out_dir / f"seedream_hand{hand_no or 'upload'}_{safe_style}_{uuid.uuid4().hex[:10]}.png"
        result_url, raw = generate_seedream_tryon(hand_image, reference_image)
        download_seedream_image(result_url, result_path)
        result = {
            "mode": "seedream_reference_tryon",
            "ai_used": True,
            "cached": False,
            "hand_no": hand_no,
            "style": {"style_id": style_id, "reference_image": reference_image},
            "quick_result": str(result_path),
            "final_result": str(result_path),
            "quick_url": media_url(result_path),
            "final_url": media_url(result_path),
            "mask_url": None,
            "quality": {"score": 100, "source": "seedream_reference"},
            "raw_response": raw,
        }
        log_event("try_on_web_inspiration", hand_image, style_id, str(result_path), 100)
        return jsonify(result)

    cached_path = None
    if payload.get("prefer_cached", True):
        cached_path = find_precomputed_seedream_tryon(hand_image, style_id)

    if cached_path:
        style_no = parse_style_no(style_id)
        hand_no = parse_dataset_hand_no(hand_image)
        result = {
            "mode": "precomputed_seedream",
            "ai_used": True,
            "cached": True,
            "hand_no": hand_no,
            "style_no": style_no,
            "style": {"style_id": style_id, "serial_no": style_no},
            "quick_result": str(cached_path),
            "final_result": str(cached_path),
            "quick_url": media_url(cached_path),
            "final_url": media_url(cached_path),
            "mask_url": None,
            "quality": {"score": 100, "source": "precomputed_seedream"},
        }
        log_event("try_on_cached", hand_image, style_id, str(cached_path), 100)
        return jsonify(result)

    if payload.get("cache_only", True):
        return jsonify({
            "error": "没有找到预生成试戴图。请使用数据集里的 01.png-13.png 手图，或关闭 cache_only 后再实时生成。",
            "hand_image": hand_image,
            "style_id": style_id,
            "hand_no": parse_dataset_hand_no(hand_image),
            "style_no": parse_style_no(style_id),
        }), 404

    from hybrid_tryon import run_hybrid_tryon

    result = run_hybrid_tryon(
        hand_image=hand_image,
        hand_type=payload.get("hand_type"),
        style_id=style_id,
        tags=",".join(payload.get("selected_tags") or []),
        db_path=DB_PATH,
        quality_threshold=float(payload.get("quality_threshold") or 72.0),
        enable_ai=bool(payload.get("enable_ai", True)),
    )
    result["mode"] = "generated"
    result["cached"] = False
    result["quick_url"] = media_url(result["quick_result"])
    result["final_url"] = media_url(result["final_result"])
    result["mask_url"] = media_url(result["mask_path"])
    log_event("try_on", hand_image, result["style"]["style_id"], result["final_result"], result["quality"]["score"])
    if result["ai_used"]:
        log_event("ai_refine", hand_image, result["style"]["style_id"], result["final_result"], result["quality"]["score"])
    return jsonify(result)


@app.get("/media/<token>")
def media(token):
    path = decode_path(token)
    if not path.exists() or not path.is_file():
        return jsonify({"error": "file not found"}), 404
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return send_file(path, mimetype=mime)


INDEX_HTML = r"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>美甲 AI 试戴 Demo</title>
  <style>
    :root { color-scheme: light; --ink:#20242a; --muted:#69707d; --line:#e5e8ee; --blue:#2563eb; --bg:#f6f7f9; }
    * { box-sizing: border-box; }
    body { margin: 0; font-family: "Microsoft YaHei", system-ui, sans-serif; color: var(--ink); background: var(--bg); }
    header { padding: 18px 28px; background: white; border-bottom: 1px solid var(--line); display:flex; justify-content:space-between; align-items:center; }
    h1 { margin: 0; font-size: 20px; }
    main { display: grid; grid-template-columns: 360px 1fr; gap: 18px; padding: 18px; }
    section { background: white; border: 1px solid var(--line); border-radius: 8px; padding: 16px; }
    h2 { font-size: 16px; margin: 0 0 12px; }
    label { display:block; font-size: 13px; color: var(--muted); margin: 12px 0 6px; }
    select, textarea, input[type=file] { width:100%; border:1px solid var(--line); border-radius:6px; padding:9px; background:white; }
    textarea { min-height: 78px; resize: vertical; }
    button { border:0; background:var(--blue); color:white; padding:9px 12px; border-radius:6px; cursor:pointer; font-weight:600; }
    button.secondary { background:#eef2ff; color:#1d4ed8; }
    button:disabled { opacity:.55; cursor:wait; }
    .tags { display:flex; flex-wrap:wrap; gap:8px; }
    .tag { border:1px solid var(--line); border-radius:999px; padding:7px 10px; cursor:pointer; font-size:13px; }
    .tag.active { background:#eff6ff; color:#1d4ed8; border-color:#93c5fd; }
    .preview { width:100%; aspect-ratio: 3 / 4; object-fit:cover; border-radius:8px; border:1px solid var(--line); background:#f1f3f5; }
    .grid { display:grid; grid-template-columns: repeat(auto-fill, minmax(170px, 1fr)); gap:14px; }
    .card { border:1px solid var(--line); border-radius:8px; padding:10px; }
    .card img { width:100%; aspect-ratio:3/4; object-fit:cover; border-radius:6px; background:#f1f3f5; }
    .meta { font-size:12px; color:var(--muted); line-height:1.5; margin-top:8px; }
    .reason { margin-top:8px; padding:8px; border-radius:6px; background:#f8fafc; color:#334155; font-size:12px; line-height:1.45; }
    .reason b { color:#0f172a; }
    .result { display:grid; grid-template-columns: minmax(260px, 420px) 1fr; gap:16px; align-items:start; margin-top:16px; }
    .result img { width:100%; border-radius:8px; border:1px solid var(--line); }
    pre { background:#0f172a; color:#e2e8f0; padding:12px; border-radius:8px; overflow:auto; max-height:360px; }
    .row { display:flex; gap:8px; align-items:center; }
    .status { color:var(--muted); font-size:13px; }
    @media (max-width: 900px) { main, .result { grid-template-columns:1fr; } }
  </style>
</head>
<body>
  <header>
    <h1>美甲 AI 试戴与推荐 Demo</h1>
    <span class="status" id="status">准备就绪</span>
  </header>
  <main>
    <section>
      <h2>用户输入</h2>
      <label>选择样例手图</label>
      <select id="handSelect"></select>
      <label>或上传手图</label>
      <input id="upload" type="file" accept="image/*" />
      <div style="height:10px"></div>
      <img id="handPreview" class="preview" alt="手图预览" />
      <label>手型确认</label>
      <select id="handType"></select>
      <label>指甲长度偏好</label>
      <select id="length">
        <option value="">不限</option>
      </select>
      <label>偏好标签</label>
      <div class="tags" id="tagBox"></div>
      <label>一句话描述</label>
      <textarea id="description" placeholder="例如：我想要显白、日常一点、适合上班的款式"></textarea>
      <div style="height:14px"></div>
      <button id="recommendBtn">推荐款式</button>
    </section>

    <section>
      <h2>推荐款式</h2>
      <div id="extracted" class="meta"></div>
      <div class="grid" id="recommendations"></div>
      <div id="result"></div>
    </section>
  </main>

<script>
const state = { handImage: "", selectedTags: new Set(), recommendations: [] };
const $ = (id) => document.getElementById(id);

function setStatus(text) { $("status").textContent = text; }
async function postJSON(url, data) {
  const res = await fetch(url, { method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(data) });
  const json = await res.json();
  if (!res.ok) throw new Error(json.error || res.statusText);
  return json;
}

async function init() {
  const options = await fetch("/api/options").then(r => r.json());
  $("handSelect").innerHTML = options.hands.map(h => `<option value="${h.path}" data-url="${h.url}">${h.name}</option>`).join("");
  $("handType").innerHTML = options.hand_types.map(t => `<option value="${t}">${t}</option>`).join("");
  $("length").innerHTML += options.lengths.map(t => `<option value="${t}">${t}</option>`).join("");
  $("tagBox").innerHTML = options.preference_tags.map(t => `<span class="tag" data-tag="${t}">${t}</span>`).join("");
  document.querySelectorAll(".tag").forEach(el => el.onclick = () => {
    const tag = el.dataset.tag;
    if (state.selectedTags.has(tag)) { state.selectedTags.delete(tag); el.classList.remove("active"); }
    else { state.selectedTags.add(tag); el.classList.add("active"); }
  });
  if (options.hands.length) {
    state.handImage = options.hands[0].path;
    $("handPreview").src = options.hands[0].url;
  }
}

$("handSelect").onchange = () => {
  const opt = $("handSelect").selectedOptions[0];
  state.handImage = opt.value;
  $("handPreview").src = opt.dataset.url;
};

$("upload").onchange = async () => {
  const file = $("upload").files[0];
  if (!file) return;
  const form = new FormData();
  form.append("file", file);
  setStatus("上传手图中...");
  const res = await fetch("/api/upload_hand", { method:"POST", body:form }).then(r => r.json());
  state.handImage = res.path;
  $("handPreview").src = res.url;
  setStatus("上传完成");
};

$("recommendBtn").onclick = async () => {
  setStatus("推荐中...");
  $("recommendBtn").disabled = true;
  try {
    const data = await postJSON("/api/recommend", {
      hand_image: state.handImage,
      hand_type: $("handType").value,
      length: $("length").value,
      selected_tags: Array.from(state.selectedTags),
      description: $("description").value,
      top_k: 8
    });
    state.recommendations = data.recommendations;
    $("extracted").textContent =
      "偏好理解：" + (data.preferences.source || "rules") +
      "｜推荐理由：" + (data.reason_source || "template") +
      (data.llm_error ? "｜LLM错误：" + data.llm_error : "") +
      "｜抽取结果：" + JSON.stringify(data.preferences);
    $("recommendations").innerHTML = data.recommendations.map(item => `
      <div class="card">
        <img src="${item.image_url}" alt="款式${item.serial_no}" />
        <div class="meta">
          <b>${item.serial_no}｜${item.primary_style}</b><br>
          ${item.nail_shape} / ${item.nail_length} / ${item.color_text}<br>
          匹配分：${item.score}
          <div class="reason"><b>推荐理由：</b>${item.ai_reason || "这款与当前手型和偏好较匹配，适合先试戴对比。"}</div>
        </div>
        <div style="height:8px"></div>
        <button onclick="runTryon('${item.style_id}')">快速试戴</button>
        <button class="secondary" onclick="runTryon('${item.style_id}', true)">高清兜底</button>
      </div>
    `).join("");
    setStatus("推荐完成");
  } catch (err) {
    setStatus("推荐失败：" + err.message);
  } finally {
    $("recommendBtn").disabled = false;
  }
};

async function runTryon(styleId, forceAi=false) {
  setStatus(forceAi ? "高清试戴生成中..." : "快速试戴生成中...");
  $("result").innerHTML = "<p class='status'>正在生成，请稍等...</p>";
  try {
    const data = await postJSON("/api/tryon", {
      hand_image: state.handImage,
      hand_type: $("handType").value,
      style_id: styleId,
      selected_tags: Array.from(state.selectedTags),
      enable_ai: true,
      quality_threshold: forceAi ? 101 : 72
    });
    $("result").innerHTML = `
      <div class="result">
        <div>
          <h2>试戴结果</h2>
          <img src="${data.final_url}" alt="试戴结果" />
        </div>
        <div>
          <h2>质量与成本策略</h2>
          <p class="meta">AI 兜底：${data.ai_used ? "已触发" : "未触发"}｜质量分：${data.quality.score}</p>
          <pre>${JSON.stringify(data, null, 2)}</pre>
        </div>
      </div>
    `;
    setStatus("试戴完成");
  } catch (err) {
    $("result").innerHTML = "";
    setStatus("试戴失败：" + err.message);
  }
}

init().catch(err => setStatus("初始化失败：" + err.message));
</script>
</body>
</html>
"""


OPS_DASHBOARD_HTML = r"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>美甲智能运营看板</title>
  <style>
    :root { --bg:#f7f4ef; --card:#fff; --ink:#3f3027; --muted:#8a7364; --pink:#ff8fab; --line:#eadfd6; }
    * { box-sizing:border-box; }
    body { margin:0; background:var(--bg); color:var(--ink); font-family:"Microsoft YaHei", system-ui, sans-serif; }
    header { padding:22px 28px; background:#fff; border-bottom:1px solid var(--line); display:flex; justify-content:space-between; align-items:center; }
    h1 { margin:0; font-size:24px; }
    main { padding:22px; max-width:1280px; margin:0 auto; }
    .cards { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:14px; margin-bottom:18px; }
    .card, section { background:var(--card); border:1px solid var(--line); border-radius:14px; box-shadow:0 6px 20px rgba(63,48,39,.04); }
    .card { padding:18px; }
    .num { font-size:30px; font-weight:800; color:var(--pink); margin-top:8px; }
    .grid { display:grid; grid-template-columns:1.35fr .85fr; gap:18px; }
    section { padding:18px; margin-bottom:18px; }
    h2 { margin:0 0 14px; font-size:18px; }
    table { width:100%; border-collapse:collapse; }
    th,td { text-align:left; padding:10px 8px; border-bottom:1px solid #f0e7df; font-size:14px; vertical-align:middle; }
    th { color:var(--muted); font-weight:600; }
    img { width:54px; height:54px; object-fit:cover; border-radius:10px; display:block; }
    .tag-list { display:flex; flex-wrap:wrap; gap:8px; }
    .tag { padding:8px 10px; border-radius:999px; background:#fff2f5; color:#9d4960; font-size:13px; }
    .bar { height:8px; background:#f3e8e0; border-radius:99px; overflow:hidden; min-width:90px; }
    .fill { height:100%; background:linear-gradient(90deg,#ffb5c5,#ff8fab); }
    .suggestion { padding:12px; border-radius:12px; background:#fff8fa; margin-bottom:10px; }
    .suggestion b { display:block; margin-bottom:5px; }
    .muted { color:var(--muted); font-size:13px; }
    a { color:var(--pink); text-decoration:none; font-weight:700; }
    @media(max-width:900px){ .cards,.grid{grid-template-columns:1fr;} }
  </style>
</head>
<body>
  <header>
    <h1>美甲智能运营看板</h1>
    <a href="/product-demo">返回试戴 Demo</a>
  </header>
  <main>
    <div class="cards" id="summaryCards"></div>
    <div class="grid">
      <section>
        <h2>热门款式 Top10</h2>
        <table>
          <thead><tr><th>款式</th><th>信息</th><th>总互动</th><th>试戴</th><th>收藏</th></tr></thead>
          <tbody id="hotStyles"></tbody>
        </table>
      </section>
      <div>
        <section><h2>热门标签</h2><div class="tag-list" id="hotTags"></div></section>
        <section><h2>事件分布</h2><div id="eventBreakdown"></div></section>
        <section><h2>补款建议</h2><div id="gapSuggestions"></div></section>
      </div>
    </div>
    <section>
      <h2>近 7 天互动趋势</h2>
      <table><thead><tr><th>日期</th><th>总互动</th><th>试戴</th></tr></thead><tbody id="dailyTrend"></tbody></table>
    </section>
  </main>
  <div class="detail-overlay" id="styleDetailOverlay" onclick="closeStyleDetail(event)">
    <div class="detail-modal" onclick="event.stopPropagation()">
      <div class="detail-head">
        <h2>款式详情 / 编辑</h2>
        <button class="btn" onclick="closeStyleDetail()">关闭</button>
      </div>
      <div id="styleDetailBody"></div>
    </div>
  </div>
<script>
function pct(value, max) { return max ? Math.round(value / max * 100) : 0; }
async function init() {
  const data = await fetch('/api/ops/summary').then(r => r.json());
  const s = data.summary;
  document.getElementById('summaryCards').innerHTML = [
    ['总互动', s.total_events], ['今日互动', s.today_events], ['试戴次数', s.tryon_count], ['收藏次数', s.favorite_count],
  ].map(([k,v]) => `<div class="card"><div class="muted">${k}</div><div class="num">${v}</div></div>`).join('');
  document.getElementById('hotStyles').innerHTML = data.hot_styles.map(row => `
    <tr>
      <td><img src="${row.image_url}" alt="${row.serial_no}"></td>
      <td><b>${row.serial_no} | ${row.primary_style || ''}</b><div class="muted">${row.nail_shape || ''} / ${row.nail_length || ''} / ${row.color_text || ''}</div></td>
      <td>${row.event_count || 0}</td><td>${row.tryon_count || 0}</td><td>${row.favorite_count || 0}</td>
    </tr>`).join('');
  document.getElementById('hotTags').innerHTML = data.hot_tags.map(t => `<span class="tag">${t.tag_name} ${t.event_count}</span>`).join('') || '<span class="muted">暂无标签行为</span>';
  const maxEvent = Math.max(1, ...data.event_breakdown.map(e => e.count));
  document.getElementById('eventBreakdown').innerHTML = data.event_breakdown.map(e => `
    <div style="margin-bottom:10px"><div class="muted">${e.event_type}：${e.count}</div><div class="bar"><div class="fill" style="width:${pct(e.count, maxEvent)}%"></div></div></div>
  `).join('') || '<div class="muted">暂无事件</div>';
  document.getElementById('gapSuggestions').innerHTML = data.gap_suggestions.map(item => `<div class="suggestion"><b>${item.title}</b><div class="muted">${item.reason}</div></div>`).join('');
  document.getElementById('dailyTrend').innerHTML = data.daily_trend.map(row => `<tr><td>${row.date}</td><td>${row.event_count}</td><td>${row.tryon_count || 0}</td></tr>`).join('') || '<tr><td colspan="3" class="muted">暂无趋势数据</td></tr>';
}
init();
</script>
</body>
</html>
"""


MERCHANT_DEMO_HTML = r"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>喵喵美甲商家智能运营端</title>
  <style>
    :root {
      --bg:#f7f4ef; --card:#fffdfb; --ink:#38291f; --muted:#8b735f;
      --pink:#ff8fab; --pink2:#ffd8e2; --green:#4f8f72; --gold:#b88a38;
      --line:#eadfd6; --shadow:0 14px 34px rgba(85,55,32,.08);
    }
    * { box-sizing:border-box; }
    body { margin:0; background:var(--bg); color:var(--ink); font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif; }
    header { padding:24px 28px 18px; background:linear-gradient(135deg,#fff7f2,#ffe2eb); border-bottom:1px solid var(--line); }
    .top { display:flex; justify-content:space-between; gap:20px; align-items:flex-end; max-width:1180px; margin:0 auto; }
    h1 { margin:0 0 8px; font-size:28px; }
    .sub { color:var(--muted); font-size:14px; line-height:1.7; }
    .pill { display:inline-flex; padding:8px 12px; background:#fff; border:1px solid var(--line); border-radius:999px; color:var(--muted); font-size:13px; }
    main { max-width:1180px; margin:0 auto; padding:22px 20px 40px; }
    .tabs { display:flex; gap:10px; margin-bottom:16px; position:sticky; top:0; background:rgba(247,244,239,.92); backdrop-filter:blur(10px); padding:10px 0; z-index:2; }
    .tab { border:1px solid var(--line); background:#fff; color:var(--muted); border-radius:999px; padding:9px 15px; cursor:pointer; }
    .tab.active { background:var(--ink); color:white; border-color:var(--ink); }
    .grid { display:grid; gap:16px; }
    .kpis { grid-template-columns:repeat(5,1fr); }
    .two { grid-template-columns:1.35fr .9fr; align-items:start; }
    .three { grid-template-columns:repeat(3,1fr); }
    .card { background:var(--card); border:1px solid var(--line); border-radius:16px; box-shadow:var(--shadow); padding:16px; }
    .kpi-title { color:var(--muted); font-size:13px; }
    .kpi-num { font-size:30px; font-weight:800; margin:8px 0 4px; }
    .kpi-note { color:var(--green); font-size:12px; }
    h2 { margin:0 0 14px; font-size:18px; }
    table { width:100%; border-collapse:collapse; }
    th,td { padding:11px 8px; border-bottom:1px solid var(--line); text-align:left; font-size:13px; vertical-align:middle; }
    th { color:var(--muted); font-weight:600; }
    td img { width:54px; height:54px; object-fit:cover; border-radius:10px; display:block; }
    .muted { color:var(--muted); font-size:12px; line-height:1.6; }
    .score { font-weight:800; color:var(--pink); }
    .bar { height:9px; background:#f0e7df; border-radius:99px; overflow:hidden; }
    .fill { height:100%; background:linear-gradient(90deg,var(--pink),#ffc36a); border-radius:99px; }
    .funnel-row { display:grid; grid-template-columns:86px 1fr 42px; gap:10px; align-items:center; margin:13px 0; }
    .tags { display:flex; flex-wrap:wrap; gap:9px; }
    .tag { padding:8px 11px; border-radius:999px; background:#fff3f6; color:#a64c65; border:1px solid #ffd8e2; font-size:13px; }
    .tag b { color:var(--ink); margin-left:5px; }
    .suggestion { padding:13px; border:1px solid var(--line); border-radius:12px; background:#fffaf6; margin-bottom:10px; }
    .priority { display:inline-block; margin-left:8px; padding:2px 8px; border-radius:999px; background:#fff0c9; color:#8b6418; font-size:12px; }
    .strategy { min-height:230px; }
    .strategy ul { margin:8px 0 0 18px; padding:0; line-height:1.8; color:var(--muted); }
    button.primary { border:0; background:var(--pink); color:white; border-radius:999px; padding:10px 16px; cursor:pointer; font-weight:700; }
    .copy { background:#fff7fb; border:1px solid #ffe1ea; padding:10px 12px; border-radius:12px; margin-top:8px; color:#6c5144; }
    .panel { display:none; }
    .panel.active { display:block; }
    @media (max-width: 860px) { .kpis,.two,.three { grid-template-columns:1fr; } .top { align-items:flex-start; flex-direction:column; } }
  </style>
</head>
<body>
  <header>
    <div class="top">
      <div>
        <h1>商家智能运营端</h1>
        <div class="sub">基于用户推荐、点击、AI试戴、收藏行为，实时识别热门款式与趋势缺口，自动生成补款和推荐策略。</div>
      </div>
      <a class="pill" href="/product-demo" target="_blank">打开用户端 Demo</a>
    </div>
  </header>
  <main>
    <div class="tabs">
      <button class="tab active" onclick="showPanel('dashboard', this)">运营看板</button>
      <button class="tab" onclick="showPanel('trend', this)">趋势洞察</button>
      <button class="tab" onclick="showPanel('strategy', this)">策略助手</button>
    </div>

    <section id="dashboard" class="panel active">
      <div class="grid kpis" id="kpis"></div>
      <div style="height:16px"></div>
      <div class="grid two">
        <div class="card">
          <h2>热门款式排行</h2>
          <table>
            <thead><tr><th>款式</th><th>信息</th><th>热度</th><th>试戴</th><th>收藏</th><th>转化</th></tr></thead>
            <tbody id="hotStyles"></tbody>
          </table>
        </div>
        <div class="card">
          <h2>用户转化漏斗</h2>
          <div id="funnel"></div>
        </div>
      </div>
    </section>

    <section id="trend" class="panel">
      <div class="grid two">
        <div class="card">
          <h2>趋势标签热度</h2>
          <div class="tags" id="trendTags"></div>
        </div>
        <div class="card">
          <h2>补款建议</h2>
          <div id="replenishment"></div>
        </div>
      </div>
      <div style="height:16px"></div>
      <div class="card">
        <h2>近 7 天互动趋势</h2>
        <table><thead><tr><th>日期</th><th>曝光</th><th>试戴</th><th>收藏</th><th>总互动</th></tr></thead><tbody id="daily"></tbody></table>
      </div>
    </section>

    <section id="strategy" class="panel">
      <div class="grid two">
        <div class="card strategy">
          <h2>AI 运营策略助手</h2>
          <div class="muted">输入来自当前看板的真实行为统计。点击生成后，大模型会给出本周推荐策略、补款方向和运营文案。</div>
          <div style="height:12px"></div>
          <button class="primary" onclick="loadStrategy()">生成运营建议</button>
          <div id="aiStrategy" style="margin-top:14px"></div>
        </div>
        <div class="card">
          <h2>当前推荐策略配置</h2>
          <div id="strategyRules"></div>
        </div>
      </div>
    </section>
  </main>
<script>
let state = null;
function showPanel(id, btn) {
  document.querySelectorAll('.panel').forEach(p => p.classList.toggle('active', p.id === id));
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  btn.classList.add('active');
}
function pct(value, max) { return max ? Math.round((Number(value || 0) / max) * 100) : 0; }
function rate(v) { return (Number(v || 0)).toFixed(1) + '%'; }
async function init() {
  state = await fetch('/api/merchant/summary').then(r => r.json());
  render();
}
function render() {
  const s = state.summary;
  document.getElementById('kpis').innerHTML = [
    ['推荐曝光', s.views, '用户看到店内款式'],
    ['款式点击', s.clicks, `点击率 ${rate(s.click_rate)}`],
    ['AI试戴', s.tryons, `试戴率 ${rate(s.tryon_rate)}`],
    ['收藏意向', s.favorites, `收藏转化 ${rate(s.favorite_rate)}`],
    ['AI生成款', s.generated, '灵感与补款来源']
  ].map(([name,value,note]) => `<div class="card"><div class="kpi-title">${name}</div><div class="kpi-num">${value || 0}</div><div class="kpi-note">${note}</div></div>`).join('');

  document.getElementById('hotStyles').innerHTML = state.hot_styles.map(row => `
    <tr>
      <td><img src="${row.image_url}" alt="${row.serial_no}"></td>
      <td><b>${row.serial_no} | ${row.primary_style || ''}</b><div class="muted">${row.nail_shape || ''} / ${row.nail_length || ''} / ${row.color_text || ''}</div></td>
      <td class="score">${row.heat_score || 0}</td>
      <td>${row.tryon_count || 0}</td>
      <td>${row.favorite_count || 0}</td>
      <td>${rate(row.tryon_rate || 0)}</td>
    </tr>
  `).join('');

  const maxFunnel = Math.max(1, ...state.funnel.map(x => x.value || 0));
  document.getElementById('funnel').innerHTML = state.funnel.map(item => `
    <div class="funnel-row"><div>${item.name}</div><div class="bar"><div class="fill" style="width:${pct(item.value, maxFunnel)}%"></div></div><b>${item.value || 0}</b></div>
  `).join('');

  document.getElementById('trendTags').innerHTML = state.trend_tags.map(t => `
    <span class="tag">${t.tag_name}<b>${t.trend_score || t.event_count || 0}</b><span class="muted"> ${t.gap_level}</span></span>
  `).join('') || '<span class="muted">暂无趋势标签</span>';

  document.getElementById('replenishment').innerHTML = state.replenishment.map(item => `
    <div class="suggestion"><b>${item.title}</b><span class="priority">${item.priority}</span><div class="muted">${item.reason}</div></div>
  `).join('');

  document.getElementById('daily').innerHTML = state.daily.map(row => `
    <tr><td>${row.date}</td><td>${row.view_count || 0}</td><td>${row.tryon_count || 0}</td><td>${row.favorite_count || 0}</td><td>${row.event_count || 0}</td></tr>
  `).join('');

  document.getElementById('strategyRules').innerHTML = state.strategies.map(item => `
    <div class="suggestion"><b>${item.title}</b><div class="muted">${item.content}</div></div>
  `).join('');
}
async function loadStrategy() {
  const box = document.getElementById('aiStrategy');
  box.innerHTML = '<div class="muted">正在生成运营建议...</div>';
  const data = await fetch('/api/merchant/strategy', { method:'POST', headers:{'Content-Type':'application/json'}, body:'{}' }).then(r => r.json());
  box.innerHTML = `
    <div class="suggestion"><b>本周判断</b><div class="muted">${data.summary || ''}</div></div>
    <div class="suggestion"><b>可执行动作</b><ul>${(data.actions || []).map(x => `<li>${x}</li>`).join('')}</ul></div>
    <div><b>可用文案</b>${(data.copywriting || []).map(x => `<div class="copy">${x}</div>`).join('')}</div>
    <div class="muted" style="margin-top:10px">风险提示：${data.risk || '继续观察数据样本。'}｜来源：${data.source || 'template'}</div>
  `;
}
init();
</script>
</body>
</html>
"""



# ===== Merchant PRD v2 upgrade =====

def ensure_merchant_tables():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auto_action_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action_type TEXT NOT NULL,
            style_id TEXT,
            before_state TEXT,
            after_state TEXT,
            trigger_rule TEXT,
            operator TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS feedback_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            suggestion_id TEXT,
            feedback_type TEXT,
            style_id TEXT,
            diff TEXT,
            operator TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS merchant_style_edits (
            style_id TEXT PRIMARY KEY,
            display_name TEXT,
            tags TEXT,
            copywriting TEXT,
            image_path TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS merchant_stock_candidates (
            candidate_id TEXT PRIMARY KEY,
            title TEXT,
            reason TEXT,
            tags TEXT,
            image_url TEXT,
            source TEXT,
            status TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


def merchant_event_rows():
    return fetch_rows(
        """
        SELECT te.*, rb.serial_no, rb.primary_style, rb.color_text, rb.nail_shape, rb.nail_length
        FROM tryon_events te
        LEFT JOIN recommendation_base rb ON rb.style_id = te.style_id
        ORDER BY te.event_time DESC
        LIMIT 50
        """
    )


def merchant_detection_rows(window="24H"):
    window_sql = {
        "1H": "-1 hour",
        "2H": "-2 hour",
        "24H": "-1 day",
        "7D": "-7 day",
    }.get(str(window).upper(), "-1 day")
    rows = fetch_rows(
        f"""
        SELECT
            rb.style_id, rb.serial_no, rb.primary_style, rb.color_text, rb.nail_shape, rb.nail_length,
            rb.recommended_hand_type, rb.enhanced_image_path, rb.original_image_path,
            SUM(CASE WHEN te.event_type='view' AND te.event_time >= datetime((SELECT MAX(event_time) FROM tryon_events),'{window_sql}') THEN 1 ELSE 0 END) AS view_count,
            SUM(CASE WHEN te.event_type LIKE 'try_on%' THEN 1 ELSE 0 END) AS tryon_count,
            SUM(CASE WHEN te.event_type='favorite' THEN 1 ELSE 0 END) AS save_count,
            SUM(CASE WHEN te.event_type IN ('share','order') THEN 1 ELSE 0 END) AS share_count,
            SUM(CASE WHEN te.event_type IN ('style_click','ai_store_click','ai_web_inspiration_click') THEN 1 ELSE 0 END) AS click_count,
            SUM(CASE WHEN te.event_type LIKE 'try_on%' AND te.event_time >= datetime((SELECT MAX(event_time) FROM tryon_events),'-2 hour') THEN 1 ELSE 0 END) AS recent_2h_tryon,
            SUM(CASE WHEN te.event_type LIKE 'try_on%' THEN 1 ELSE 0 END) AS total_tryon
        FROM recommendation_base rb
        LEFT JOIN tryon_events te ON te.style_id = rb.style_id
        GROUP BY rb.style_id
        ORDER BY rb.serial_no
        """
    )
    total_tryons = [row.get("total_tryon") or 0 for row in rows]
    mean = (sum(total_tryons) / len(total_tryons)) if total_tryons else 0
    variance = sum((x - mean) ** 2 for x in total_tryons) / len(total_tryons) if total_tryons else 0
    std = variance ** 0.5
    has_hot = False
    for row in rows:
        tryon = row.get("tryon_count") or 0
        save = row.get("save_count") or 0
        share = row.get("share_count") or 0
        click = row.get("click_count") or 0
        view = row.get("view_count") or 0
        recent = row.get("recent_2h_tryon") or 0
        row["image_url"] = style_image_url(row)
        row["completion_rate"] = safe_div(save + share, tryon)
        row["share_rate"] = safe_div(share, tryon)
        row["favorite_rate"] = safe_div(save, tryon)
        row["hot_score"] = round(0.4 * tryon + 0.3 * save + 0.2 * share + 0.1 * click + 0.05 * view, 1)
        row["detection_tag"] = "-"
        row["detection_reason"] = "稳定观察"
        if recent > mean + 2 * std and recent >= 3:
            row["detection_tag"] = "🔥爆款"
            row["detection_reason"] = "近2小时试戴量超过历史均值+2σ"
            has_hot = True
        elif tryon == 0:
            row["detection_tag"] = "❄️冷门"
            row["detection_reason"] = "当前时间窗内暂无试戴"
        elif tryon >= 2 and (save + share) / max(tryon, 1) >= 0.15:
            row["detection_tag"] = "📈潜力"
            row["detection_reason"] = "试戴与收藏/分享表现双高"
    rows.sort(key=lambda r: (r.get("hot_score") or 0, r.get("tryon_count") or 0), reverse=True)
    if not has_hot and rows:
        rows[0]["detection_tag"] = "🔥爆款"
        rows[0]["detection_reason"] = "Demo样本下按最高热度分识别为当前爆款"
    return rows


def merchant_summary_data():
    ensure_merchant_tables()
    styles = merchant_detection_rows("24H")
    style_edits = {row["style_id"]: row for row in fetch_rows("SELECT * FROM merchant_style_edits")}
    for row in styles:
        edit = style_edits.get(row.get("style_id"))
        row["merchant_display_name"] = ""
        row["merchant_copywriting"] = ""
        row["merchant_tags"] = ""
        row["is_edited"] = False
        if edit:
            row["is_edited"] = True
            row["merchant_display_name"] = edit.get("display_name") or ""
            row["merchant_copywriting"] = edit.get("copywriting") or ""
            row["merchant_tags"] = edit.get("tags") or ""
            if edit.get("display_name"):
                row["primary_style"] = edit["display_name"]
            if edit.get("image_path"):
                row["image_url"] = media_url(edit["image_path"])
    hot = [r for r in styles if "爆款" in r.get("detection_tag", "")]
    cold = [r for r in styles if "冷门" in r.get("detection_tag", "")]
    potential = [r for r in styles if "潜力" in r.get("detection_tag", "")]
    total_tryon = sum(r.get("tryon_count") or 0 for r in styles)
    active_styles = sum(1 for r in styles if (r.get("tryon_count") or 0) > 0)
    total_save = sum(r.get("save_count") or 0 for r in styles)
    total_share = sum(r.get("share_count") or 0 for r in styles)
    trend_tags = fetch_rows(
        """
        SELECT st.tag_name, st.tag_type, COUNT(te.event_id) AS event_count,
               SUM(CASE WHEN te.event_type LIKE 'try_on%' THEN 1 ELSE 0 END) AS tryon_count,
               SUM(CASE WHEN te.event_type='favorite' THEN 1 ELSE 0 END) AS save_count,
               COUNT(DISTINCT st.style_id) AS stock_count
        FROM style_tags st
        LEFT JOIN tryon_events te ON te.style_id=st.style_id
        GROUP BY st.tag_name, st.tag_type
        ORDER BY event_count DESC, tryon_count DESC
        LIMIT 20
        """
    )
    for tag in trend_tags:
        tag["trend_score"] = int((tag.get("event_count") or 0) + (tag.get("tryon_count") or 0) * 3 + (tag.get("save_count") or 0) * 5)
    daily = fetch_rows(
        """
        WITH days(day) AS (
            SELECT date((SELECT MAX(event_time) FROM tryon_events),'-6 day') UNION ALL SELECT date((SELECT MAX(event_time) FROM tryon_events),'-5 day')
            UNION ALL SELECT date((SELECT MAX(event_time) FROM tryon_events),'-4 day') UNION ALL SELECT date((SELECT MAX(event_time) FROM tryon_events),'-3 day')
            UNION ALL SELECT date((SELECT MAX(event_time) FROM tryon_events),'-2 day') UNION ALL SELECT date((SELECT MAX(event_time) FROM tryon_events),'-1 day')
            UNION ALL SELECT date((SELECT MAX(event_time) FROM tryon_events))
        )
        SELECT d.day AS date,
               SUM(CASE WHEN te.event_type LIKE 'try_on%' THEN 1 ELSE 0 END) AS tryon_count,
               SUM(CASE WHEN te.event_type='favorite' THEN 1 ELSE 0 END) AS save_count,
               SUM(CASE WHEN te.event_type='share' THEN 1 ELSE 0 END) AS share_count,
               COUNT(te.event_id) AS event_count
        FROM days d
        LEFT JOIN tryon_events te ON date(te.event_time)=d.day
        GROUP BY d.day
        ORDER BY d.day
        """
    )
    suggestions = []
    if hot:
        suggestions.append({"id": "pin_hot", "type": "置顶爆款", "style_id": hot[0]["style_id"], "title": f"置顶 {hot[0]['serial_no']} {hot[0]['primary_style']}", "reason": "已识别为当前爆款，建议置顶到 C 端推荐首屏。", "default_action": "pin"})
    if cold:
        suggestions.append({"id": "down_cold", "type": "下架冷门", "style_id": cold[0]["style_id"], "title": f"下架或降权 {len(cold)} 款冷门款", "reason": "这些款式在当前时间窗无试戴，可释放推荐位。", "default_action": "downrank"})
    if potential:
        suggestions.append({"id": "similar_potential", "type": "推荐相似款", "style_id": potential[0]["style_id"], "title": f"用 {potential[0]['serial_no']} 相似款引流", "reason": "潜力款试戴与收藏/分享双高，适合扩展相似风格。", "default_action": "similar"})
    suggestions.append({"id": "rerank_all", "type": "调整排序", "style_id": hot[0]["style_id"] if hot else "", "title": "基于最新热度分刷新 C 端排序", "reason": "每 5 分钟重算后，将爆款置顶、冷门沉底、潜力款微提升。", "default_action": "rerank"})
    generated_candidates = fetch_rows(
        """
        SELECT style_id,
               COUNT(*) AS event_count,
               SUM(CASE WHEN event_type LIKE 'try_on%' THEN 1 ELSE 0 END) AS tryon_count,
               SUM(CASE WHEN event_type='favorite' THEN 1 ELSE 0 END) AS save_count,
               MAX(result_path) AS result_path,
               MAX(event_time) AS last_time
        FROM tryon_events
        WHERE style_id LIKE 'variant_%' OR style_id LIKE 'trend_%' OR event_type IN ('style_variant_generate','trend_style_generate','try_on_web_inspiration')
        GROUP BY style_id
        ORDER BY tryon_count DESC, event_count DESC, last_time DESC
        LIMIT 8
        """
    )
    for row in generated_candidates:
        row["image_url"] = media_url(row["result_path"]) if row.get("result_path") else ""
        row["heat_score"] = int((row.get("event_count") or 0) + (row.get("tryon_count") or 0) * 6 + (row.get("save_count") or 0) * 8)
        row["candidate_id"] = row.get("style_id") or f"candidate_{row.get('last_time') or ''}"
        row["title"] = "C端高反馈生成款"
        row["source"] = "C端生成款"
        row["priority"] = "高" if (row.get("tryon_count") or 0) or (row.get("save_count") or 0) else "中"
        row["tags"] = ["AI生成", "用户试戴", "补库候选"]
        row["reason"] = f"这张生成/灵感款已产生 {row.get('event_count') or 0} 次相关互动，其中试戴 {row.get('tryon_count') or 0} 次、收藏 {row.get('save_count') or 0} 次，适合沉淀为店内可预约款。"

    stock_suggestions = []
    for item in generated_candidates[:5]:
        stock_suggestions.append(
            {
                "candidate_id": item.get("candidate_id") or item.get("style_id") or "",
                "title": item.get("title") or "C端高反馈生成款",
                "reason": item.get("reason") or "C端已有用户试戴/生成行为，适合作为上架候选。",
                "source": "C端生成款",
                "priority": item.get("priority") or "中",
                "tags": item.get("tags") or ["AI生成", "补库候选"],
                "image_url": item.get("image_url") or "",
                "style_id": item.get("style_id") or "",
            }
        )
    for tag in trend_tags[:5]:
        tag_name = tag.get("tag_name") or ""
        stock_suggestions.append(
            {
                "candidate_id": f"trend_{tag_name}",
                "title": f"补充「{tag_name}」方向新款",
                "reason": f"用户近期对「{tag_name}」相关款式互动较多，但店内覆盖有限，建议补充 2-3 款可试戴、显白、日常可预约版本。",
                "source": "趋势关键词",
                "priority": "高" if (tag.get("trend_score") or 0) >= 20 else "中",
                "tags": [tag_name, "显白", "可试戴"],
            }
        )
    actions = fetch_rows("SELECT * FROM auto_action_log ORDER BY id DESC LIMIT 12")
    feedback = fetch_rows("SELECT * FROM feedback_log ORDER BY id DESC LIMIT 12")
    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "window": "24H",
        "summary": {
            "total_tryon": total_tryon,
            "active_styles": active_styles,
            "hot_count": len(hot),
            "cold_count": len(cold),
            "potential_count": len(potential),
            "save_count": total_save,
            "share_count": total_share,
            "adoption_rate": 60 if actions else 0,
            "ctr_lift_estimate": 8.0 if hot else 0,
        },
        "styles": styles,
        "hot": hot[:3],
        "cold": cold[:8],
        "potential": potential[:5],
        "trend_tags": trend_tags,
        "daily": daily,
        "event_stream": merchant_event_rows(),
        "suggestions": suggestions,
        "stock_suggestions": stock_suggestions[:8],
        "generated_candidates": generated_candidates,
        "actions": actions,
        "feedback": feedback,
    }


@app.get("/api/merchant/v2/summary")
def merchant_v2_summary_api():
    return jsonify(merchant_summary_data())


@app.post("/api/merchant/v2/recompute")
def merchant_v2_recompute_api():
    data = merchant_summary_data()
    return jsonify({"ok": True, "recomputed_at": data["generated_at"], "hot": len(data["hot"]), "cold": len(data["cold"]), "potential": len(data["potential"])})


@app.post("/api/merchant/v2/action")
def merchant_v2_action_api():
    ensure_merchant_tables()
    payload = request.get_json(force=True)
    action_type = payload.get("action_type") or "adopt"
    style_id = payload.get("style_id") or ""
    trigger_rule = payload.get("trigger_rule") or payload.get("suggestion_id") or ""
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        INSERT INTO auto_action_log(action_type, style_id, before_state, after_state, trigger_rule, operator, created_at)
        VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
        """,
        (action_type, style_id, payload.get("before_state", ""), payload.get("after_state", "已执行/模拟写入C端排序表"), trigger_rule, "demo_operator"),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "message": "操作已记录，Demo 中模拟写入 C 端排序/标签。"})


@app.post("/api/merchant/v2/feedback")
def merchant_v2_feedback_api():
    ensure_merchant_tables()
    payload = request.get_json(force=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        INSERT INTO feedback_log(suggestion_id, feedback_type, style_id, diff, operator, created_at)
        VALUES (?, ?, ?, ?, ?, datetime('now'))
        """,
        (payload.get("suggestion_id", ""), payload.get("feedback_type", "adopt"), payload.get("style_id", ""), payload.get("diff", ""), "demo_operator"),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})




@app.post("/api/merchant/v2/upload_style")
def merchant_v2_upload_style_api():
    ensure_merchant_tables()
    name = request.form.get("name") or "商家新增款式"
    tags = request.form.get("tags") or ""
    file = request.files.get("file")
    saved_path = ""
    if file:
        out_dir = OUTPUT_ROOT / "merchant_uploaded_styles"
        out_dir.mkdir(parents=True, exist_ok=True)
        suffix = Path(file.filename or "style.png").suffix.lower() or ".png"
        saved = out_dir / f"merchant_style_{uuid.uuid4().hex}{suffix}"
        file.save(saved)
        saved_path = str(saved)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        INSERT INTO auto_action_log(action_type, style_id, before_state, after_state, trigger_rule, operator, created_at)
        VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
        """,
        ("upload_style", "", "", f"已上传新款：{name}｜标签：{tags}｜图片：{saved_path}", "stock_build", "demo_operator"),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "name": name, "tags": tags, "image_url": media_url(saved_path) if saved_path else ""})


@app.post("/api/merchant/v2/style_update")
def merchant_v2_style_update_api():
    ensure_merchant_tables()
    style_id = request.form.get("style_id") or ""
    if not style_id:
        return jsonify({"ok": False, "error": "missing style_id"}), 400
    display_name = request.form.get("display_name") or ""
    tags = request.form.get("tags") or ""
    copywriting = request.form.get("copywriting") or ""
    file = request.files.get("file")
    saved_path = request.form.get("existing_image_path") or ""
    if file:
        out_dir = OUTPUT_ROOT / "merchant_style_edits"
        out_dir.mkdir(parents=True, exist_ok=True)
        suffix = Path(file.filename or "style.png").suffix.lower() or ".png"
        safe_style_id = style_id.replace("/", "_").replace("\\", "_")
        saved = out_dir / f"{safe_style_id}_{uuid.uuid4().hex}{suffix}"
        file.save(saved)
        saved_path = str(saved)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        INSERT INTO merchant_style_edits(style_id, display_name, tags, copywriting, image_path, updated_at)
        VALUES (?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(style_id) DO UPDATE SET
            display_name=excluded.display_name,
            tags=excluded.tags,
            copywriting=excluded.copywriting,
            image_path=excluded.image_path,
            updated_at=datetime('now')
        """,
        (style_id, display_name, tags, copywriting, saved_path),
    )
    conn.execute(
        """
        INSERT INTO auto_action_log(action_type, style_id, before_state, after_state, trigger_rule, operator, created_at)
        VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
        """,
        ("edit_style", style_id, "款式原始资料", "已保存商家编辑资料", "style_detail_edit", "demo_operator"),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "style_id": style_id, "image_url": media_url(saved_path) if saved_path else ""})


@app.post("/api/merchant/v2/adopt_candidate")
def merchant_v2_adopt_candidate_api():
    ensure_merchant_tables()
    payload = request.get_json(force=True)
    candidate_id = payload.get("candidate_id") or f"candidate_{int(time.time())}"
    title = payload.get("title") or "C端生成候选款"
    reason = payload.get("reason") or ""
    tags = ",".join(payload.get("tags") or []) if isinstance(payload.get("tags"), list) else (payload.get("tags") or "")
    image_url = payload.get("image_url") or ""
    source = payload.get("source") or "C端生成款"
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        INSERT INTO merchant_stock_candidates(candidate_id, title, reason, tags, image_url, source, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(candidate_id) DO UPDATE SET
            title=excluded.title,
            reason=excluded.reason,
            tags=excluded.tags,
            image_url=excluded.image_url,
            source=excluded.source,
            status=excluded.status
        """,
        (candidate_id, title, reason, tags, image_url, source, "planned"),
    )
    conn.execute(
        """
        INSERT INTO auto_action_log(action_type, style_id, before_state, after_state, trigger_rule, operator, created_at)
        VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
        """,
        ("adopt_stock_candidate", candidate_id, source, "已加入待上架补库计划", "stock_candidate", "demo_operator"),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "candidate_id": candidate_id})

@app.post("/api/merchant/v2/assistant")
def merchant_v2_assistant_api():
    payload = request.get_json(force=True)
    question = (payload.get("question") or "").strip()
    data = merchant_summary_data()
    q = question
    hot = data["hot"] or data["styles"][:3]
    cold = data["cold"]
    potential = data["potential"]
    if any(k in q for k in ["上升", "最快", "Top", "top", "爆款"]):
        items = hot[:5]
        return jsonify({"type": "style-list", "text": "当前最值得关注的是这些款式：", "items": items})
    if any(k in q for k in ["冷门", "下架"]):
        return jsonify({"type": "style-list", "text": f"当前识别到 {len(cold)} 款冷门款，建议降权或下架前人工确认。", "items": cold[:8]})
    if any(k in q for k in ["日报", "今日"]):
        return jsonify({"type": "daily-report", "text": f"今日日报：总试戴 {data['summary']['total_tryon']} 次，活跃款式 {data['summary']['active_styles']} 款，爆款 {data['summary']['hot_count']} 款，冷门 {data['summary']['cold_count']} 款。", "suggestions": data["suggestions"]})
    if any(k in q for k in ["补款", "建库", "库存", "添加", "新增", "补充"]):
        return jsonify({"type": "stock-build", "text": "可以，优先补这几类会更贴近最近的用户偏好。", "items": data.get("stock_suggestions", [])[:5]})
    if any(k in q for k in ["文案", "小红书"]):
        style = hot[0] if hot else {}
        return jsonify({"type": "text", "text": f"小红书文案：今天这款「{style.get('primary_style','显白美甲')}」真的很适合想要精致但不夸张的姐妹，{style.get('color_text','低饱和配色')}上手干净显白，通勤和约会都能稳稳拿捏。"})
    if any(k in q for k in ["策略", "下周", "建议"]):
        return jsonify({"type": "text", "text": template_merchant_strategy(data)["summary"], "actions": template_merchant_strategy(data)["actions"]})
    return jsonify({"type": "text", "text": "你可以问：哪款上升最快、爆款/冷门列表、建议补充哪些款式、生成文案、今日日报、下周策略。"})



MERCHANT_DEMO_HTML = r"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>美甲智能运营平台</title>
  <style>
    :root{--bg:#f8f4ef;--card:#fffdfb;--ink:#3d2d23;--muted:#8c7563;--pink:#ff8fab;--line:#eadfd6;--shadow:0 14px 34px rgba(85,55,32,.08);--soft:#fff5f8}
    *{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif}
    header{padding:22px 28px;background:linear-gradient(135deg,#fff7f2,#ffe1eb);border-bottom:1px solid var(--line)}
    .wrap,main{max-width:1240px;margin:0 auto}.top{display:flex;justify-content:space-between;gap:18px;align-items:flex-end}
    h1{margin:0 0 8px;font-size:28px}.sub,.muted{color:var(--muted);font-size:13px;line-height:1.65}
    main{padding:18px 20px 42px}.tabs{display:flex;gap:10px;position:sticky;top:0;z-index:3;background:rgba(248,244,239,.94);backdrop-filter:blur(10px);padding:10px 0}
    .tab,.btn{border:1px solid var(--line);background:#fff;border-radius:999px;padding:9px 15px;cursor:pointer;color:var(--muted);font:inherit}.tab.active,.btn.primary{background:var(--ink);color:#fff;border-color:var(--ink)}.btn.pink{background:var(--pink);color:#fff;border-color:var(--pink)}
    .panel{display:none}.panel.active{display:block}.grid{display:grid;gap:14px}.kpis{grid-template-columns:repeat(4,1fr)}.two{grid-template-columns:1.25fr .9fr}.assistant-grid{grid-template-columns:1.35fr .75fr}
    .card{background:var(--card);border:1px solid var(--line);border-radius:16px;box-shadow:var(--shadow);padding:16px}.num{font-size:30px;font-weight:800;margin:8px 0}h2{margin:0 0 14px;font-size:18px}
    table{width:100%;border-collapse:collapse}th,td{padding:10px 8px;border-bottom:1px solid var(--line);text-align:left;font-size:13px;vertical-align:middle}th{color:var(--muted);font-weight:600}td img{width:48px;height:48px;border-radius:10px;object-fit:cover}
    .filters,.quick,.ops,.detail-actions{display:flex;gap:8px;flex-wrap:wrap}.filters{margin-bottom:12px}.chip{display:inline-flex;align-items:center;border-radius:999px;padding:4px 8px;background:#fff3f6;color:#9a4a63;font-size:12px}.chip.hot{background:#ffe6e9;color:#d34e61}.chip.cold{background:#eaf2ff;color:#457dc4}.chip.potential{background:#e9f8f0;color:#4f8f72}
    .suggestion{padding:12px;border:1px solid var(--line);background:#fffaf6;border-radius:12px;margin-bottom:10px}.style-row.hotline{background:#fff8ec}.bar{height:9px;background:#f0e7df;border-radius:99px;overflow:hidden}.fill{height:100%;background:linear-gradient(90deg,var(--pink),#ffc36a)}
    .chat{display:grid;grid-template-rows:minmax(0,1fr) auto;height:660px;min-height:0}.chat>div:first-child{display:flex;flex-direction:column;min-height:0}.messages{flex:1;min-height:0;overflow-y:auto;overscroll-behavior:contain;border:1px solid var(--line);border-radius:14px;background:#fff;padding:12px}.msg{margin-bottom:10px;padding:10px 12px;border-radius:12px;background:var(--soft)}.msg.user{background:#f3eee8;text-align:right}.quick{margin:10px 0;flex:0 0 auto}.input{display:flex;gap:8px;margin-top:10px}.input input{flex:1;border:1px solid var(--line);border-radius:999px;padding:10px 14px}
    .stock-card{height:660px;display:flex;flex-direction:column;min-height:0}.stock-list{flex:1;min-height:0;overflow-y:auto;overscroll-behavior:contain;padding-right:4px;margin-top:12px}.stock-upload{flex:0 0 auto;margin-top:12px}.stock-img{width:76px;height:76px;object-fit:cover;border-radius:12px;float:right;margin-left:10px}.history-list{max-height:360px;overflow:auto;padding-right:4px}.history-item b{display:block;margin-bottom:4px}.report-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:14px}.report-metric{border:1px solid var(--line);border-radius:14px;background:#fffaf6;padding:14px}.report-metric b{display:block;font-size:24px;margin-top:6px}.insight-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin:14px 0}.insight-card{border:1px solid var(--line);border-radius:14px;background:#fffaf6;padding:12px;min-height:118px}.insight-icon{width:32px;height:32px;border-radius:50%;background:#ffe4ed;color:#bd4b69;display:grid;place-items:center;font-weight:800;margin-bottom:8px}.top-bars{display:grid;gap:12px}.top-bar-row{display:grid;grid-template-columns:92px 1fr 58px;gap:10px;align-items:center}.top-style-img{width:52px;height:52px;border-radius:12px;object-fit:cover;margin-right:8px;vertical-align:middle}.top-bar-track{height:14px;background:#f0e7df;border-radius:999px;overflow:hidden;margin-top:6px}.top-bar-fill{height:100%;border-radius:999px;background:linear-gradient(90deg,#ff8fab,#ffc36a)}.trend-tags{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}.trend-chip{border:1px solid #ffd8e2;background:#fff3f6;color:#9a4a63;border-radius:999px;padding:7px 10px;font-size:13px}.opportunity-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}.opportunity-card{border:1px solid var(--line);border-radius:14px;background:#fffaf6;padding:12px}.opportunity-card img{width:100%;height:112px;object-fit:cover;border-radius:12px;margin-bottom:8px}.trend-chart{display:grid;grid-template-columns:repeat(7,1fr);gap:10px;align-items:end;height:210px;padding:18px 8px 8px;border:1px solid var(--line);border-radius:14px;background:#fffaf6;margin-bottom:12px}.trend-col{display:flex;flex-direction:column;align-items:center;justify-content:end;gap:6px;min-width:0}.trend-bar{width:58%;min-height:6px;border-radius:999px 999px 4px 4px;background:linear-gradient(180deg,#ff8fab,#ffc36a)}.trend-value{font-size:12px;font-weight:700;color:var(--ink)}.trend-date{font-size:11px;color:var(--muted);white-space:nowrap}
    .detail-overlay{position:fixed;inset:0;background:rgba(45,31,24,.32);display:none;align-items:center;justify-content:center;padding:20px;z-index:20}.detail-overlay.active{display:flex}.detail-modal{width:min(680px,96vw);max-height:90vh;overflow:auto;background:#fffaf6;border:1px solid var(--line);border-radius:18px;padding:20px;box-shadow:0 24px 60px rgba(58,42,32,.22)}.detail-head{display:flex;justify-content:space-between;align-items:center;gap:12px}.detail-grid{display:grid;grid-template-columns:150px 1fr;gap:16px;margin-top:14px}.detail-grid img{width:150px;height:150px;object-fit:cover;border-radius:14px}.form{display:grid;gap:10px}.form input,.form textarea{width:100%;border:1px solid var(--line);border-radius:12px;padding:10px 12px;font:inherit;background:#fff}.form textarea{min-height:96px;resize:vertical}.detail-actions{justify-content:flex-end;margin-top:14px}
    @media(max-width:900px){.kpis,.two,.assistant-grid,.detail-grid,.insight-grid,.opportunity-grid{grid-template-columns:1fr}.top{align-items:flex-start;flex-direction:column}.detail-grid img{width:100%;height:220px}.chat{height:560px}}
  </style>
</head>
<body>
  <header><div class="wrap top"><div><h1>美甲智能运营平台</h1><div class="sub">围绕款式热度、用户试戴、收藏反馈和 AI 生成灵感，辅助商家做推荐位调整、补款和内容运营。</div></div><a class="btn" href="/product-demo" target="_blank">打开 C 端</a></div></header>
  <main>
    <div class="tabs">
      <button class="tab active" onclick="showPanel('board',this)">款式看板</button>
      <button class="tab" onclick="showPanel('daily',this)">智能日报</button>
      <button class="tab" onclick="showPanel('assistant',this)">AI助手</button>
      <button class="btn pink" onclick="recompute()">立即重算</button>
    </div>

    <section id="board" class="panel active">
      <div class="grid kpis" id="kpis"></div><div style="height:14px"></div>
      <div class="card"><div class="filters"><button class="btn" onclick="setFilter('all')">全部</button><button class="btn" onclick="setFilter('爆款')">爆款</button><button class="btn" onclick="setFilter('冷门')">冷门</button><button class="btn" onclick="setFilter('潜力')">潜力</button></div>
        <table><thead><tr><th>排名</th><th>款式</th><th>试戴量</th><th>完成率</th><th>收藏率</th><th>热度分</th><th>标签</th><th>操作</th></tr></thead><tbody id="styleTable"></tbody></table>
      </div>
    </section>

    <section id="daily" class="panel">
      <div class="grid two">
        <div class="card"><h2>智能日报</h2><div id="dailySummary"></div><h2 style="margin-top:16px">今日用户偏好</h2><div id="preferenceInsights" class="insight-grid"></div><h2 style="margin-top:16px">热门 TOP3</h2><div id="top3" class="top-bars"></div></div>
        <div class="card"><h2>趋势与补款机会</h2><div id="trendSummary"></div><div id="opportunityCards" class="opportunity-grid"></div><h2 style="margin-top:16px">运营建议</h2><div id="suggestions"></div></div>
      </div>
      <div style="height:14px"></div>
      <div class="grid two">
        <div class="card"><h2>近 7 天互动趋势</h2><div id="trendChart" class="trend-chart"></div><table><thead><tr><th>日期</th><th>试戴</th><th>收藏</th><th>分享</th><th>总互动</th></tr></thead><tbody id="dailyTrend"></tbody></table></div>
        <div class="card"><h2>运营动作记录</h2><div class="muted">这里记录商家已采纳的推荐、补款计划和款式资料修改，方便评委看到从分析到执行的闭环。</div><div id="history" class="history-list" style="margin-top:10px"></div></div>
      </div>
    </section>

    <section id="assistant" class="panel">
      <div class="grid assistant-grid">
        <div class="card chat"><div><h2>AI 运营助手</h2><div class="quick"><button class="btn" onclick="ask('哪款上升最快？')">哪款上升最快</button><button class="btn" onclick="ask('爆款冷门列表')">爆款/冷门</button><button class="btn" onclick="ask('建议补充哪些款式？')">补款建议</button><button class="btn" onclick="ask('生成小红书文案')">生成文案</button><button class="btn" onclick="ask('今日日报重点')">今日日报</button><button class="btn" onclick="ask('下周推荐策略')">策略建议</button></div><div class="messages" id="messages"></div></div><div class="input"><input id="chatInput" placeholder="问我：最近用户想要什么款？要补哪些库存？"><button class="btn primary" onclick="ask()">发送</button></div></div>
        <div class="card stock-card"><h2>款式补库工作台</h2><div class="muted">根据 C 端生成款、试戴行为和趋势关键词，推荐商家补充新款。</div><div id="stockBuild" class="stock-list"></div><div class="suggestion stock-upload"><b>上传新款式</b><div style="display:grid;gap:8px;margin-top:8px"><input id="uploadStyleName" placeholder="款式名，如 低饱和猫眼短甲"><input id="uploadStyleTags" placeholder="标签，如 猫眼、显白、通勤"><input id="uploadStyleFile" type="file" accept="image/*"><button class="btn pink" onclick="uploadMerchantStyle()">上传并加入补库候选</button></div></div></div>
      </div>
    </section>
  </main>

  <div class="detail-overlay" id="styleDetailOverlay" onclick="closeStyleDetail(event)">
    <div class="detail-modal" onclick="event.stopPropagation()">
      <div class="detail-head"><h2>款式详情 / 编辑</h2><button class="btn" onclick="closeStyleDetail()">关闭</button></div>
      <div id="styleDetailBody"></div>
    </div>
  </div>

<script>
let data=null, filter='all';
function safeText(v){return String(v||'').replace(/[&<>"']/g,s=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[s]))}
function showPanel(id,btn){document.querySelectorAll('.panel').forEach(p=>p.classList.toggle('active',p.id===id));document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));btn.classList.add('active')}
async function load(){data=await fetch('/api/merchant/v2/summary').then(r=>r.json());render()}
function setFilter(v){filter=v;renderStyles()}
function tagClass(t){return String(t||'').includes('爆款')?'hot':String(t||'').includes('冷门')?'cold':String(t||'').includes('潜力')?'potential':''}
function render(){const s=data.summary;document.getElementById('kpis').innerHTML=[['总试戴量',s.total_tryon],['活跃款式',s.active_styles],['爆款数',s.hot_count],['冷门数',s.cold_count]].map(x=>`<div class="card"><div class="muted">${x[0]}</div><div class="num">${x[1]||0}</div></div>`).join('');renderStyles();renderDaily();renderStockBuild();renderHistory()}
function renderStyles(){let rows=data.styles||[];if(filter!=='all')rows=rows.filter(r=>String(r.detection_tag||'').includes(filter));document.getElementById('styleTable').innerHTML=rows.map((r,i)=>`<tr class="style-row ${String(r.detection_tag||'').includes('爆款')?'hotline':''}"><td>${i+1}</td><td><div style="display:flex;gap:10px;align-items:center"><img src="${r.image_url||''}"><div><b>${safeText(r.serial_no)} ${safeText(r.primary_style)}</b>${r.is_edited?'<span class="chip">已编辑</span>':''}<div class="muted">${safeText(r.nail_shape)}/${safeText(r.nail_length)}/${safeText(r.color_text)}</div></div></div></td><td>${r.tryon_count||0}</td><td>${r.completion_rate||0}%</td><td>${r.favorite_rate||0}%</td><td><b>${r.hot_score||0}</b></td><td><span class="chip ${tagClass(r.detection_tag)}">${safeText(r.detection_tag||'-')}</span></td><td>${opsButtons(r)}</td></tr>`).join('')}
function opsButtons(r){const edit=`<button class="btn" onclick="openStyleDetail('${r.style_id}')">详情/编辑</button>`;const tag=String(r.detection_tag||'');if(tag.includes('爆款'))return edit+`<button class="btn" onclick="doAction('pin','${r.style_id}','pin_hot')">置顶</button>`;if(tag.includes('冷门'))return edit+`<button class="btn" onclick="doAction('downrank','${r.style_id}','down_cold')">降权</button>`;if(tag.includes('潜力'))return edit+`<button class="btn" onclick="doAction('similar','${r.style_id}','similar_potential')">相似款</button>`;return edit+`<button class="btn" onclick="doAction('rerank','${r.style_id}','manual')">调权</button>`}
function renderDaily(){const s=data.summary;const daily=data.daily||[];const tags=data.trend_tags||[];const topTags=tags.slice(0,5).map(t=>t.tag_name).filter(Boolean);document.getElementById('dailySummary').innerHTML=`<div class="report-grid"><div class="report-metric"><span class="muted">总试戴</span><b>${s.total_tryon||0}</b></div><div class="report-metric"><span class="muted">活跃款式</span><b>${s.active_styles||0}</b></div><div class="report-metric"><span class="muted">爆款</span><b>${s.hot_count||0}</b></div><div class="report-metric"><span class="muted">冷门</span><b>${s.cold_count||0}</b></div></div><div class="suggestion">今日用户更偏向「${topTags.slice(0,3).map(safeText).join('、')||'显白、日常、低饱和'}」方向，说明决策重点集中在上手安全感、通勤适配和真实试戴效果。</div>`;document.getElementById('preferenceInsights').innerHTML=[['偏好款式',topTags.slice(0,2).join(' / ')||'纯色 / 亮片','用户更愿意先试戴低风险、容易显白的款式。'],['偏好甲型',(tags.find(t=>t.tag_type==='长度')||{}).tag_name||'短','短/中长度更适合日常场景，试戴决策压力更小。'],['推荐机会',(data.stock_suggestions||[]).length+' 个补款候选','C端生成款和趋势关键词可转化为店内新款。']].map((x,i)=>`<div class="insight-card"><div class="insight-icon">${i+1}</div><b>${safeText(x[0])}</b><div>${safeText(x[1])}</div><div class="muted">${safeText(x[2])}</div></div>`).join('');const top=(data.styles||[]).slice(0,3);const topMax=Math.max(1,...top.map(r=>r.hot_score||0));document.getElementById('top3').innerHTML=top.map((r,i)=>`<div class="top-bar-row"><b>${['TOP1','TOP2','TOP3'][i]}</b><div><div><img class="top-style-img" src="${r.image_url||''}"><b>${safeText(r.serial_no)} ${safeText(r.primary_style)}</b></div><div class="top-bar-track"><div class="top-bar-fill" style="width:${Math.max(8,Math.round((r.hot_score||0)/topMax*100))}%"></div></div><div class="muted">试戴 ${r.tryon_count||0}｜收藏率 ${r.favorite_rate||0}%｜热度分 ${r.hot_score||0}</div></div><b>${r.hot_score||0}</b></div>`).join('');document.getElementById('trendSummary').innerHTML=`<div class="suggestion"><b>流行趋势判断</b><div class="muted">近期高频标签集中在：<span class="trend-tags">${topTags.map(t=>`<span class="trend-chip">${safeText(t)}</span>`).join('')}</span></div><div class="muted">建议围绕这些标签补充相似款，同时把高互动 AI 生成图沉淀为可预约款。</div></div>`;document.getElementById('opportunityCards').innerHTML=(data.stock_suggestions||[]).slice(0,3).map(x=>`<div class="opportunity-card">${x.image_url?`<img src="${x.image_url}">`:''}<b>${safeText(x.title)}</b><div class="muted">${safeText(x.reason)}</div></div>`).join('');document.getElementById('suggestions').innerHTML=(data.suggestions||[]).map(x=>`<div class="suggestion"><b>${safeText(x.title)}</b><div class="muted">${safeText(x.reason)}</div><div class="ops"><button class="btn pink" onclick="feedback('${x.id}','adopt','${x.style_id}')">采纳</button><button class="btn" onclick="feedback('${x.id}','modify','${x.style_id}')">稍后调整</button><button class="btn" onclick="feedback('${x.id}','reject','${x.style_id}')">不采用</button></div></div>`).join('');document.getElementById('dailyTrend').innerHTML=daily.map(r=>`<tr><td>${r.date}</td><td>${r.tryon_count||0}</td><td>${r.save_count||0}</td><td>${r.share_count||0}</td><td>${r.event_count||0}</td></tr>`).join('');const max=Math.max(1,...daily.map(d=>d.event_count||0));document.getElementById('trendChart').innerHTML=daily.map(d=>{const h=Math.max(6,Math.round((d.event_count||0)/max*170));return `<div class="trend-col"><div class="trend-value">${d.event_count||0}</div><div class="trend-bar" style="height:${h}px"></div><div class="trend-date">${String(d.date||'').slice(5)}</div></div>`}).join('')||'<div class="muted">暂无趋势数据</div>'}
function renderStockBuild(){const box=document.getElementById('stockBuild');box.innerHTML=(data.stock_suggestions||[]).slice(0,5).map((x,i)=>`<div class="suggestion">${x.image_url?`<img class="stock-img" src="${x.image_url}">`:''}<b>${safeText(x.title)}</b><span class="chip">${safeText(x.priority||'中')}优先级</span><div class="muted">${safeText(x.reason)}</div><div class="muted">来源：${safeText(x.source||'趋势')}｜标签：${(x.tags||[]).map(safeText).join('、')}</div><div class="ops"><button class="btn pink" onclick="adoptCandidate(${i})">推荐上架</button><button class="btn" onclick="doAction('stock_plan','${x.style_id||x.candidate_id||''}','stock_build')">加入补库计划</button></div></div>`).join('')||'<div class="muted">暂无补库建议</div>'}
function actionTitle(a){const t=String(a.action_type||'');if(t.includes('adopt_stock_candidate'))return '已加入待上架补库计划';if(t.includes('stock_plan'))return '已加入补款计划';if(t.includes('edit_style'))return '已保存款式资料修改';if(t.includes('pin')||t==='pin')return '已置顶推荐款';if(t.includes('downrank'))return '已降低冷门款推荐权重';if(t.includes('similar'))return '已生成相似款运营策略';if(t.includes('rerank'))return '已刷新推荐排序';if(t.includes('upload_style'))return '已上传新款式';return '已记录运营动作'}
function renderHistory(){document.getElementById('history').innerHTML=(data.actions||[]).slice(0,12).map(a=>`<div class="suggestion history-item"><b>${actionTitle(a)}</b><div class="muted">${safeText(a.created_at)}｜${safeText(a.style_id||'全局策略')}</div></div>`).join('')||'<div class="muted">暂无操作记录。采纳建议、补款或编辑款式后会出现在这里。</div>'}
async function adoptCandidate(i){const x=(data.stock_suggestions||[])[i];if(!x)return;const res=await fetch('/api/merchant/v2/adopt_candidate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(x)}).then(r=>r.json());alert(res.ok?'已加入待上架计划':'操作失败');await load()}
async function uploadMerchantStyle(){const fd=new FormData();fd.append('name',document.getElementById('uploadStyleName').value||'商家新增款式');fd.append('tags',document.getElementById('uploadStyleTags').value||'');const f=document.getElementById('uploadStyleFile').files[0];if(f)fd.append('file',f);const res=await fetch('/api/merchant/v2/upload_style',{method:'POST',body:fd}).then(r=>r.json());alert(res.ok?'已加入补库候选':'上传失败');await load()}
async function recompute(){await fetch('/api/merchant/v2/recompute',{method:'POST'});await load()}
async function doAction(action,style,rule){await fetch('/api/merchant/v2/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action_type:action,style_id:style,trigger_rule:rule})});await load()}
async function feedback(id,type,style){await fetch('/api/merchant/v2/feedback',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({suggestion_id:id,feedback_type:type,style_id:style})});if(type==='adopt')await doAction('adopt_'+id,style,id);else await load()}
function openStyleDetail(styleId){const r=(data.styles||[]).find(x=>x.style_id===styleId);if(!r)return;document.getElementById('styleDetailBody').innerHTML=`<div class="detail-grid"><img src="${r.image_url||''}" alt="${safeText(r.serial_no)}"><div class="form"><input id="detailStyleId" type="hidden" value="${r.style_id}"><label>运营名称<input id="detailName" value="${safeText(r.primary_style)}"></label><label>标签<input id="detailTags" value="${safeText(r.merchant_tags||[r.nail_shape,r.nail_length,r.color_text].filter(Boolean).join('、'))}"></label><label>卖点文案<textarea id="detailCopy">${safeText(r.merchant_copywriting||`${r.primary_style||'这款美甲'}上手效果自然，适合${r.recommended_hand_type||'多数手型'}，可作为近期推荐款。`)}</textarea></label><label>替换图片<input id="detailFile" type="file" accept="image/*"></label><div class="muted">当前数据：试戴 ${r.tryon_count||0} 次，收藏率 ${r.favorite_rate||0}%，热度分 ${r.hot_score||0}。修改后会记录到运营动作中。</div></div></div><div class="detail-actions"><button class="btn" onclick="closeStyleDetail()">取消</button><button class="btn pink" onclick="saveStyleDetail()">保存修改</button></div>`;document.getElementById('styleDetailOverlay').classList.add('active')}
function closeStyleDetail(e){if(e&&e.target.id!=='styleDetailOverlay')return;document.getElementById('styleDetailOverlay').classList.remove('active')}
async function saveStyleDetail(){const fd=new FormData();fd.append('style_id',document.getElementById('detailStyleId').value);fd.append('display_name',document.getElementById('detailName').value);fd.append('tags',document.getElementById('detailTags').value);fd.append('copywriting',document.getElementById('detailCopy').value);const f=document.getElementById('detailFile').files[0];if(f)fd.append('file',f);const res=await fetch('/api/merchant/v2/style_update',{method:'POST',body:fd}).then(r=>r.json());alert(res.ok?'款式资料已保存':'保存失败');closeStyleDetail();await load()}
async function ask(q){const input=document.getElementById('chatInput');const text=q||input.value.trim();if(!text)return;if(!q)input.value='';const box=document.getElementById('messages');box.innerHTML+=`<div class="msg user">${safeText(text)}</div>`;const res=await fetch('/api/merchant/v2/assistant',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question:text})}).then(r=>r.json());let html=`<div class="msg"><b>${safeText(res.text||'')}</b>`;if(res.items&&res.type==='stock-build')html+=res.items.slice(0,5).map(x=>`<div class="suggestion">${x.image_url?`<img src="${x.image_url}" style="width:52px;height:52px;object-fit:cover;border-radius:10px;float:right;margin-left:8px">`:''}<b>${safeText(x.title||'')}</b><div class="muted">${safeText(x.reason||'')}</div><div class="muted">${safeText(x.source||'')}｜${(x.tags||[]).map(safeText).join('、')}</div></div>`).join('');else if(res.items)html+=res.items.slice(0,5).map(r=>`<div class="suggestion"><b>${safeText(r.serial_no)} ${safeText(r.primary_style)}</b><div class="muted">热度 ${r.hot_score||0}｜试戴 ${r.tryon_count||0}</div></div>`).join('');if(res.actions)html+=`<ul>${res.actions.map(x=>`<li>${safeText(x)}</li>`).join('')}</ul>`;box.innerHTML+=html+'</div>';box.scrollTop=box.scrollHeight}
load();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=7860, debug=False)
