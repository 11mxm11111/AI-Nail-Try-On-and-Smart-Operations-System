# Nail Style Database

This folder is generated from `C:\Users\yzh\Desktop\美甲分类_打标.xlsx` and image files under `D:\AI_Project\nail\美甲图`.

## Main Files

- `nail_style.db`: SQLite database for app/backend use.
- `styles.csv`: One row per nail style. There are 25 styles.
- `style_images.csv`: Image variants for each style: `original`, `enhanced`, and `style_url`.
- `style_tags.csv`: Normalized tags for filtering, search, recommendation, and trend analysis.
- `hand_images.csv`: Hand image metadata and hand type labels.
- `tag_dictionary.csv`: Tag taxonomy from the classification sheet.
- `data_quality_report.txt`: Data checks such as missing images or mismatched style labels.

## Core Tables

- `styles`: canonical style records.
- `style_images`: original/enhanced/style_url image paths for each style.
- `style_tags`: many-to-many tag records, including nail shape, hand type, length, style, and color tags.
- `hand_images`: hand image records.
- `tag_dictionary`: taxonomy and suitability definitions.
- `tryon_events`: reserved for later user behavior events such as view, try_on, favorite, and order.

## Useful Views

- `recommendation_base`: style metadata plus original/enhanced image paths.
- `style_tag_summary`: style metadata plus concatenated tag summary.

## Example Query

```sql
SELECT *
FROM recommendation_base
WHERE recommended_hand_type = '修长手'
ORDER BY serial_no;
```

