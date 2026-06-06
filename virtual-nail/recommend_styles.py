import argparse
import json
import math
import sqlite3
from pathlib import Path


DEFAULT_DB = Path("data/style_database/nail_style.db")


def split_terms(value):
    if not value:
        return []
    if isinstance(value, str):
        raw = (
            value.replace("+", ",")
            .replace("，", ",")
            .replace("、", ",")
            .replace("/", ",")
            .split(",")
        )
        return [item.strip() for item in raw if item.strip()]
    return [str(item).strip() for item in value if str(item).strip()]


def clamp(value, low=0.0, high=100.0):
    return max(low, min(high, value))


def normalized_event_score(count, max_count, cap):
    if not count or not max_count:
        return 0.0
    # Log scaling avoids one very hot style flattening all others.
    return cap * (math.log1p(count) / math.log1p(max_count))


def load_event_stats(conn):
    stats = {}
    for row in conn.execute(
        """
        SELECT
            style_id,
            COUNT(*) AS total_events,
            SUM(CASE WHEN event_type IN ('recommend_view') THEN 1 ELSE 0 END) AS view_count,
            SUM(CASE WHEN event_type IN ('style_click') THEN 1 ELSE 0 END) AS click_count,
            SUM(CASE WHEN event_type IN ('favorite') THEN 1 ELSE 0 END) AS favorite_count,
            SUM(CASE WHEN event_type LIKE 'try_on%' THEN 1 ELSE 0 END) AS tryon_count
        FROM tryon_events
        WHERE style_id IS NOT NULL AND style_id != ''
        GROUP BY style_id
        """
    ):
        stats[row["style_id"]] = dict(row)
    return stats


def recommend_styles(
    hand_type=None,
    tags=None,
    length=None,
    nail_shape=None,
    color=None,
    top_k=8,
    db_path=DEFAULT_DB,
):
    tags = split_terms(tags)
    colors = split_terms(color)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    rows = [dict(row) for row in conn.execute("SELECT * FROM recommendation_base")]
    event_stats = load_event_stats(conn)
    max_total_events = max([s.get("total_events") or 0 for s in event_stats.values()] or [0])
    max_tryon = max([s.get("tryon_count") or 0 for s in event_stats.values()] or [0])

    tag_map = {}
    for row in conn.execute("SELECT style_id, tag_type, tag_name, weight FROM style_tags"):
        tag_map.setdefault(row["style_id"], []).append(dict(row))
    conn.close()

    recommendations = []
    for row in rows:
        style_id = row["style_id"]
        style_tags = tag_map.get(style_id, [])
        tag_names = {item["tag_name"] for item in style_tags}
        tag_names.add(row.get("primary_style") or "")
        stats = event_stats.get(style_id, {})

        score_detail = {
            "preference": 0.0,
            "hand_type": 0.0,
            "color_style": 0.0,
            "popularity": 0.0,
            "conversion": 0.0,
            "diversity": 0.0,
        }
        reasons = []
        matched_tags = []
        matched_colors = []

        if tags:
            for tag in tags:
                if tag in tag_names:
                    matched_tags.append(tag)
            score_detail["preference"] = min(35.0, len(matched_tags) * 11.5)
            if matched_tags:
                reasons.append("匹配偏好标签：" + "、".join(matched_tags[:4]))
        else:
            score_detail["preference"] = 8.0
            reasons.append("作为店内基础候选款参与推荐")

        if hand_type:
            if row.get("recommended_hand_type") == hand_type:
                score_detail["hand_type"] = 20.0
                reasons.append(f"适合{hand_type}")
            else:
                score_detail["hand_type"] = 6.0
        else:
            score_detail["hand_type"] = 8.0

        color_style_score = 0.0
        if length and row.get("nail_length") == length:
            color_style_score += 5.0
            reasons.append(f"长度符合{length}甲偏好")
        if nail_shape and row.get("nail_shape") == nail_shape:
            color_style_score += 5.0
            reasons.append(f"甲型符合{nail_shape}")
        for color_term in colors:
            if color_term and color_term in (row.get("color_text") or ""):
                matched_colors.append(color_term)
        if matched_colors:
            color_style_score += min(5.0, len(matched_colors) * 2.5)
            reasons.append("颜色匹配：" + "、".join(matched_colors[:3]))
        score_detail["color_style"] = clamp(color_style_score, 0.0, 15.0)

        weighted_popularity = (
            (stats.get("view_count") or 0) * 0.4
            + (stats.get("click_count") or 0) * 1.2
            + (stats.get("favorite_count") or 0) * 2.0
            + (stats.get("tryon_count") or 0) * 2.5
        )
        score_detail["popularity"] = normalized_event_score(weighted_popularity, max_total_events * 2.5, 15.0)
        if weighted_popularity:
            reasons.append(f"近期有{int(stats.get('total_events') or 0)}次用户互动")

        score_detail["conversion"] = normalized_event_score(stats.get("tryon_count") or 0, max_tryon, 5.0)
        if stats.get("tryon_count"):
            reasons.append(f"已有{int(stats.get('tryon_count') or 0)}次试戴行为")

        # Keep a little room for discovery so the list is not permanently locked by historical hot styles.
        total_events = stats.get("total_events") or 0
        score_detail["diversity"] = 10.0 if total_events == 0 else max(2.0, 8.0 - min(total_events, 12) * 0.5)

        score = sum(score_detail.values())
        if not reasons:
            reasons.append("综合手型、标签和热度作为候选")

        recommendations.append(
            {
                **row,
                "score": round(clamp(score), 2),
                "score_detail": {key: round(value, 2) for key, value in score_detail.items()},
                "matched_tags": matched_tags,
                "matched_colors": matched_colors,
                "event_stats": {
                    "total_events": int(stats.get("total_events") or 0),
                    "view_count": int(stats.get("view_count") or 0),
                    "click_count": int(stats.get("click_count") or 0),
                    "favorite_count": int(stats.get("favorite_count") or 0),
                    "tryon_count": int(stats.get("tryon_count") or 0),
                },
                "reasons": reasons,
            }
        )

    recommendations.sort(key=lambda item: (-item["score"], item["serial_no"]))
    return recommendations[:top_k]


def main():
    parser = argparse.ArgumentParser(description="Recommend nail styles from the normalized style database.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--hand-type", default=None)
    parser.add_argument("--tags", default="")
    parser.add_argument("--length", default=None)
    parser.add_argument("--nail-shape", default=None)
    parser.add_argument("--color", default="")
    parser.add_argument("--top-k", type=int, default=8)
    args = parser.parse_args()

    rows = recommend_styles(
        hand_type=args.hand_type,
        tags=args.tags,
        length=args.length,
        nail_shape=args.nail_shape,
        color=args.color,
        top_k=args.top_k,
        db_path=Path(args.db),
    )
    print(json.dumps(rows, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
