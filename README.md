# 喵喵美甲试戴间 ｜ 美甲 AI 试戴与智能运营系统

> 一套面向 **C 端用户美甲试戴** 与 **B 端商家智能运营** 的 AI 闭环系统：用户在线试戴美甲款式，平台沉淀真实交互数据，反向驱动商家选款、补库与运营策略。

团队：**Maximum**（微信 19129215453）

---

## 一、项目简介

在传统美甲服务里，用户挑款"靠想象"、商家选款"靠直觉"，两端都缺乏低成本的试错与决策手段。本项目围绕两条主线构建数据闭环：

- **C 端：AI 推荐 + AI 试戴** — 用户上传手图、表达偏好（结构化标签 + 自然语言），系统给出 *店内可预约款* 与 *AI 创意生成款*，并支持双方案试戴（本地快速试戴 / Seedream 高清试戴）。
- **B 端：智能运营看板** — 用户的浏览、试戴、收藏、生成行为汇总为款式热度分、趋势标签和补库建议，由大模型生成可读的智能日报与运营文案。

形成 **"款式推荐 → AI 试戴 → 用户反馈 → 商家运营 → 推荐优化"** 的完整闭环。

完整产品设计文档见：[`喵喵美甲试戴间 _ 美甲 AI 试戴与智能运营系统.md`](./喵喵美甲试戴间%20_%20美甲%20AI%20试戴与智能运营系统.md)

---

## 二、项目结构

```
AI_Project_meituan/
├── README.md                              # 本文件
├── 喵喵美甲试戴间_美甲 AI 试戴与智能运营系统.md   # 完整产品 / 技术方案文档
│
├── virtual-nail/                          # 【主工程】Flask 后端 + C/B 端前端 + AI 能力
│   ├── demo_app.py                        # Flask 主应用（端口 7860），承载所有 API 与页面
│   ├── sue-nail-ai-demo.html              # 【前端 Demo ①】C 端：AI 推荐 + AI 试戴（接后端 API）
│   ├── recommend_styles.py                # 店内款式数据库 Top-K 推荐
│   ├── qwen_recommend.py                  # Qwen：联网获取趋势灵感、款式设计
│   ├── deepseek_text.py                   # DeepSeek：偏好解析、推荐理由、文案生成
│   ├── seedream_tryon.py                  # Seedream 多模态：高清 AI 试戴
│   ├── hybrid_tryon.py                    # 本地快速试戴入口（U²-Net + 像素级颜色迁移）
│   ├── color_transfer_pixel_level_*.py    # 像素级颜色迁移与 SDXL 精修
│   ├── color_nail_highlight_*.py          # 高光 / 着色器算法
│   ├── generate_initial_masks.py          # U²-Net 指甲分割掩码生成
│   ├── build_style_database.py            # 构建款式 SQLite 数据库
│   ├── batch_random_tryon.py              # 批量随机试戴脚本
│   ├── editor_image_server.py             # 图像编辑辅助服务
│   ├── data/                              # 数据库 / 上传 / 输出 / 推理缓存
│   │   ├── style_database/nail_style.db   # 款式 SQLite
│   │   ├── demo_uploads/                  # 用户上传手图
│   │   ├── output/                        # 试戴输出
│   │   └── ...
│   ├── models/                            # U²-Net / SDXL 等本地模型
│   ├── .env                               # API Key 配置（DeepSeek / Qwen / Seedream）
│   ├── API_DOCUMENTATION.md               # 后端 API 文档
│   ├── API_FLOW_DOCUMENTATION.md          # 接口调用时序与流程
│   └── GitHubreadme.md                    # 算法侧详细说明
│
├── 美甲图/                                # 【前端 Demo ②】静态展示版 + 全量素材库
│   ├── sue-nail-ai-demo.html              # 纯静态 HTML（无需后端，直接打开即可演示）
│   ├── 手图URL/                           # 真人手图素材
│   ├── 款式图URL/                         # 美甲款式图（成品）
│   ├── 原始款式图URL/                     # 原始款式图（增强前）
│   ├── 增强后款式图URL/                   # 增强后款式图
│   ├── 美甲分类_打标.xlsx                 # 款式标签库（颜色 / 风格 / 甲型 / 长度 ...）
│   └── _thumbs/                           # 缩略图缓存
│
└── nail/美甲图/                           # 备用素材（手图 / 款式图 / 试戴图）
    ├── 手图URL/
    ├── 款式图URL/
    └── 试戴图/
```

> 两个前端 Demo 的差异：
> - **`virtual-nail/sue-nail-ai-demo.html`**：完整版，调用 Flask 后端的 `/api/recommend`、`/api/ai/recommend_top3`、`/api/tryon/scene` 等接口，依赖 DeepSeek / Qwen / Seedream，可跑通真实推荐与试戴。
> - **`美甲图/sue-nail-ai-demo.html`**：纯静态展示版，所有数据写死在 HTML 内，浏览器直接打开即可，用于产品形态演示与早期评审。

---

## 三、核心模块

### 3.1 C 端 — 美甲 AI 推荐

混合策略：**店内数据库匹配（1–3s）** + **AI 创意生成款（5–20s）**，最终以 *2 席店内热卖 + 2 席 AI 创意* 的矩阵展示。

| 推荐类型 | 来源 | 作用 |
| :--- | :--- | :--- |
| 店内数据库推荐 | `recommend_styles.py` + SQLite | 真实可预约 / 可试戴 / 可收藏的店内款 |
| AI 创意生成款 | `qwen_recommend.py` + Seedream / SDXL | 根据用户偏好生成个性化灵感款 |

支持的偏好维度：风格（猫眼 / 法式 / 日系 / 韩系 / 简约 / 甜酷 …）、颜色（裸 / 粉 / 黑 / 奶茶 …）、甲型与长度（短 / 中 / 长 × 杏仁 / 方圆 …）、手型（修长 / 肉肉 / 短粗 …），以及自然语言诉求（"显白、通勤、不要太夸张"）。

### 3.2 C 端 — AI 试戴（双方案）

| 方案 | 定位 | 技术路线 | 适用场景 | 耗时 |
| :--- | :--- | :--- | :--- | :--- |
| **快速试戴** | 低成本预览 | U²-Net 指甲分割 + 像素级颜色迁移 + 物理高光 | 初筛、批量预览 | 1–5s |
| **高清试戴** | 高真实感 | Seedream 多模态图像大模型（强约束 prompt） | 最终确认、复杂款式 | 10–60s |

关键工程文件：
- `generate_initial_masks.py` — U²-Net 指甲掩码
- `color_transfer_pixel_level_transplant.py` / `color_transfer_pixel_level_refine_sdxl.py` — 颜色迁移与 SDXL 精修
- `color_nail_highlight_shader.py` — 高光着色器
- `seedream_tryon.py` — Seedream 接入与防变形 prompt 约束

### 3.3 B 端 — 智能运营

将 C 端行为数据加权计算为款式热度分：

```
热度分 = 浏览×1 + 点击×3 + 试戴×6 + 收藏×8 + 近期增长×4
```

四大功能：
- **款式看板** — 浏览 / 点击 / 试戴 / 收藏 / 收藏率 / 热度分
- **智能日报** — 大模型基于结构化统计生成今日趋势解读（颜色 / 风格 / 甲型 / 热门款）
- **AI 运营助手** — 自然语言问答（"哪款最热？" / "帮我写一条主推文案"）
- **补库工作台** — 基于 AI 生成款互动 + 趋势关键词推荐补款方向

入口路由（见 `demo_app.py`）：`/merchant-demo`、`/ops-dashboard`、`/api/merchant/v2/*`。

---

## 四、快速开始

### 方式 A：仅查看静态 Demo（最快）

```bash
# 浏览器直接打开
open "美甲图/sue-nail-ai-demo.html"
```

无需任何依赖，适合演示产品形态。

### 方式 B：跑通完整后端 + 前端

#### 1. 环境准备

- Python 3.8+
- 推荐用虚拟环境：`python -m venv .venv && source .venv/bin/activate`

```bash
cd virtual-nail
pip install flask python-dotenv requests pillow
# 试戴算法侧依赖：
pip install torch torchvision opencv-python numpy scikit-image
```

#### 2. 配置 API Key

编辑 `virtual-nail/.env`，填入实际密钥：

```dotenv
DEEPSEEK_API_KEY=...
DEEPSEEK_MODEL=...
DEEPSEEK_BASE_URL=...

SEEDREAM_API_KEY=...
SEEDREAM_MODEL=...
SEEDREAM_BASE_URL=...
SEEDREAM_IMAGE_ENDPOINT=...

QWEN_API_KEY=...
QWEN_MODEL=...
QWEN_BASE_URL=...
```

#### 3. 准备款式数据库（首次运行）

```bash
cd virtual-nail
python build_style_database.py
# 如需 U²-Net 模型权重：
python download_models.py
```

#### 4. 启动服务

```bash
python demo_app.py
# Flask 运行在 http://127.0.0.1:7860
```

#### 5. 访问页面

| 路由 | 说明 |
| :--- | :--- |
| `http://127.0.0.1:7860/` | C 端首页 |
| `http://127.0.0.1:7860/product-demo` | C 端完整 Demo（即 `sue-nail-ai-demo.html`） |
| `http://127.0.0.1:7860/merchant-demo` | B 端商家运营 Demo |
| `http://127.0.0.1:7860/ops-dashboard` | 运营看板 |

---

## 五、主要 API（Flask）

| Method | Path | 说明 |
| :--- | :--- | :--- |
| GET | `/api/options` | 获取手型 / 长度 / 偏好标签等下拉选项 |
| POST | `/api/upload_hand` | 上传用户手图 |
| POST | `/api/upload_style` | 上传外部款式图（用于试戴） |
| POST | `/api/recommend` | 店内库 Top-K 推荐 |
| POST | `/api/ai/recommend_top3` | AI 综合 Top3 推荐（DeepSeek + 数据库混合） |
| POST | `/api/ai/generate_style_variants` | 基于已选款生成变体 |
| POST | `/api/ai/generate_trend_style` | 联网趋势驱动的 AI 创意生成款 |
| POST | `/api/tryon` | 本地快速试戴 |
| POST | `/api/tryon/scene` | 场景化试戴（自动选方案） |
| POST | `/api/event` | 行为埋点（浏览 / 点击 / 试戴 / 收藏 / 分享） |
| GET  | `/api/merchant/v2/summary` | 商家运营汇总（看板数据） |
| POST | `/api/merchant/v2/recompute` | 重新计算热度分 |
| POST | `/api/merchant/v2/assistant` | 商家 AI 助手对话 |
| POST | `/api/merchant/v2/adopt_candidate` | 采纳补款建议 |
| GET  | `/media/<token>` | 媒体文件代理（base64 编码路径） |

详见 [`virtual-nail/API_DOCUMENTATION.md`](./virtual-nail/API_DOCUMENTATION.md) 与 [`virtual-nail/API_FLOW_DOCUMENTATION.md`](./virtual-nail/API_FLOW_DOCUMENTATION.md)。

---

## 六、技术栈

| 层 | 技术 |
| :--- | :--- |
| 后端 | Python 3.8+、Flask、SQLite |
| 大模型 | DeepSeek（偏好解析 / 文案）、Qwen（联网趋势 / 款式设计）、Seedream（多模态高清试戴） |
| 视觉算法 | U²-Net（指甲分割）、像素级颜色迁移、TPS 形变、Phong 高光、SDXL 图像精修 |
| 前端 | 原生 HTML / CSS / JavaScript（移动端 H5 风格） |
| 数据 | SQLite + Excel（`美甲分类_打标.xlsx`）+ 静态图片资源 |

---

## 七、成本控制策略

1. **技术降本** — 分层调用：缓存优先 → 本地快速试戴 → Seedream 高清试戴
2. **产品限额** — AI 生成款每日 2–3 次免费额度
3. **商业变现** — 会员 / 单次付费 / 预约返还 / 广告补贴 / 商家 SaaS 套餐

---

## 八、未来优化方向

- **U²-Net 指甲分割精度** — 扩充多肤色 / 多角度数据集，加入边界修正与高光融合
- **AI 生成款保真链路** — LLM 设计方案 → 图像 prompt 的结构化映射，降低生成随机性
- **热门款 IP 二创去风险化** — 自有"去中心化灵感版权库" + AI 风格化再重构
- **打通穿戴甲电商** — 3D 测算定制尺码、个人手艺人公域化、O2O 复购链路

---

## 九、团队

- **团队名称**：Maximum
- **联系方式**：19129215453（微信同号）
