# 抖音内容工作台（合胜宏企服）

给老板用的内部工具网站：**输入一段选题/关键词 → 批量采集同行抖音视频并生成结构化分析报告入库 → 基于报告生成原创口播文案 → 老板按稿拍摄并上传 → 自动剪辑成 9:16 竖屏成片下载**。

技术上是一个 Flask + SQLite 的单机应用，零外部数据库，后台线程跑采集与剪辑任务，前端轮询进度。

## 五步流程

| 步骤 | 页面 | 做了什么 |
| --- | --- | --- |
| 1 找素材 | 抖音链接采集 / 上传本地视频 / 手填文案 | 提取口播内容 |
| 2 看分析 | 单条分析 + 批量采集报告 | 结构化报告写入 `reports` 表 |
| 3 出文案 | 输入要求，一键生成原创脚本 | 只复用抽象结论，不复用同行原文 |
| 4 拍口播 | 定稿文案 + 上传口播视频 | 老板只需按稿拍 |
| 5 剪成片 | 后台自动剪辑 | 片头/片尾卡片、ASS 中文字幕、水印、竖屏 9:16、H.264/AAC |

## 本地快速启动

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
sudo apt install -y ffmpeg fonts-noto-cjk   # 剪辑与中文渲染依赖
.venv/bin/python app.py                     # 默认 8080，可用 PORT 环境变量覆盖
# 浏览器打开 http://127.0.0.1:8080
```

首次启动会自动生成 `config/settings.json`、`config/edit_templates.json` 和 `data/studio.db`，**无需手工建表**。健康检查：`GET /healthz`。

## 服务器部署

```bash
sudo apt install -y python3-venv ffmpeg fonts-noto-cjk

# /opt 属主是 root，普通用户不能在里面建目录：先建好，再把属主改成自己
sudo mkdir -p /opt/douyin-studio
sudo chown -R "$(id -un):$(id -gn)" /opt/douyin-studio

# 方式 A｜代码包（最省事，不需要 GitHub 凭证）
tar -xzf douyin-studio.tar.gz -C /opt/douyin-studio

# 方式 B｜从私有仓库 clone（需要 PAT，见下一节；目标目录必须为空）
# git clone https://pingwumeng-leroy:<PAT>@github.com/pingwumeng-leroy/-.git /opt/douyin-studio

cd /opt/douyin-studio
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config/settings.example.json config/settings.json       # 不复制也行，首启自动生成

# systemd 以 www-data 运行，只有 data/ 需要写权限（SQLite / 上传 / 成片）
sudo mkdir -p data
sudo chown -R www-data:www-data data

sudo cp deploy/douyin-studio.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now douyin-studio

sudo cp deploy/nginx.conf.example /etc/nginx/sites-available/douyin-studio
# 修改 server_name 与 SSL 证书路径后：
sudo ln -s /etc/nginx/sites-available/douyin-studio /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

`nginx.conf.example` 已配置 `client_max_body_size 1024m`（口播视频上传）与 `proxy_read_timeout 600s`（剪辑任务耗时）。域名解析到服务器后，建议用 certbot 签证书。

### 私有仓库 clone 的认证

仓库是私有的，GitHub 自 2021 年起不接受账号密码登录。要在服务器上 `git clone`，先在 GitHub → Settings → Developer settings → Personal access tokens 建一个 **Fine-grained token**，只勾这一个仓库的 `Contents: Read`：

```bash
git clone https://pingwumeng-leroy:<PAT>@github.com/pingwumeng-leroy/-.git /opt/douyin-studio
```

不带 Token 直接 clone 会提示输入 Username / Password，填账号密码必然失败（报 `Authentication failed`）。嫌麻烦就直接用 `douyin-studio.tar.gz` 代码包，内容完全一致，也不需要任何凭证。

> Token 会留在命令行历史里，用完建议 `history -d` 清掉，或改用 deploy key / `git credential` 缓存。

## 配置说明

所有配置在网站「设置」抽屉里改，也可直接编辑 `config/settings.json`（**该文件已被 `.gitignore` 排除，绝不入库**）。

| 段 | 关键项 | 说明 |
| --- | --- | --- |
| `llm` | `enabled` / `base_url` / `api_key` / `model` | OpenAI 兼容接口；未配置时分析与生成走内置规则引擎 |
| `asr` | `enabled` / `base_url` / `api_key` | 口播音频转写，可选 |
| `fetch` | `douyin_cookie` | yt-dlp 采集抖音时用的 Cookie，可选 |
| `company` | `name` / `short` / `city` | 片头片尾与文案里的主体信息 |
| `auth` | `enabled` / `username` / `password` | **公网部署必须开启**，HTTP Basic 保护页面与全部 API（`/healthz` 除外） |
| `search` | `provider` / `tikhub.base_url` / `tikhub.api_key` | `mock`（默认，无密钥）或 `tikhub`（真实抖音数据） |
| `harvest` | `default_count` / `max_calls` / `use_asr` | 采集条数、单次任务调用预算、是否走完整档转写 |

## 数据源与分析档位

- **Mock**：内置模拟数据，用来跑通全链路，不需要任何密钥。
- **TikHub**：真实抖音搜索，端点 `POST /api/v1/douyin/search/fetch_video_search_v2`，评论 `GET /api/v1/douyin/web/fetch_video_comments`。返回的是**视频清单 + 播放地址**，不是 mp4 文件。
- **轻量档（当前默认）**：不下载视频，用 `desc` + 公开评论构造可分析文本，直接产出结构化报告。
- **完整档**：`harvest.use_asr` 预留开关，接自托管 dtk / FunASR 下载音轨并转写口播逐字稿后替换文本来源。
- 采集链路有关键词展开、`aweme_id` 唯一索引去重、`search_cache` 缓存、`max_calls` 预算与额度耗尽自动降级，避免把 API 余额跑爆。TikHub 错误响应会做**密钥脱敏**后再写日志，页面只回传掩码。

## API 速查

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/healthz` | 健康检查（跳过访问密码） |
| GET | `/api/state` | 全局状态与统计 |
| GET/POST | `/api/settings` | 配置读写（密钥掩码） |
| GET/POST | `/api/templates` | 剪辑模板 |
| POST | `/api/admin/reset` | 清空业务数据，保留配置与种子账号 |
| POST | `/api/sensitive` | 敏感词检查 |
| GET | `/api/videos` | 素材列表 |
| POST | `/api/videos/upload`、`/api/videos/fetch` | 上传 / 采集素材 |
| POST | `/api/videos/<id>/transcript` | 提取或填写口播文本 |
| POST | `/api/videos/<id>/analyze` | 生成分析报告 |
| GET | `/api/reports`、`/api/reports/<id>` | 报告列表 / 详情 |
| POST | `/api/reports/manual` | 手动录入报告 |
| GET | `/api/providers` | 可用数据源 |
| POST | `/api/harvest/start` | 启动批量采集 |
| GET | `/api/harvest`、`/api/harvest/<id>` | 任务列表 / 状态 |
| POST | `/api/harvest/<id>/stop` | 停止任务 |
| GET/POST | `/api/seeds` | 种子账号池 |
| POST | `/api/scripts/generate` | 生成原创文案 |
| GET/PUT | `/api/scripts/<id>` | 文案读取 / 编辑保存 |
| POST | `/api/clips/upload`、`/api/clips/render` | 上传口播 / 启动剪辑 |
| GET | `/api/jobs/<id>` | 剪辑进度 |
| GET | `/api/outputs/<name>`、`/api/outputs/<name>/download` | 预览 / 下载成片 |

## 数据与备份

单文件 SQLite：`data/studio.db`。**备份 = 复制这一个文件**。上传视频在 `data/uploads/`，成片在 `data/outputs/`，剪辑中间产物在 `data/assets/`。设置页的「清空演示数据」会清掉业务表与上述文件，但保留 `config/` 与种子账号。

## 安全清单

- `config/settings.json`、`data/`、`*.db` 全部在 `.gitignore` 里，**不要把 Cookie、API Key、Token、用户上传内容提交进仓库**。
- 公网必须开 `auth.enabled` + HTTPS；内网可不开。
- 内容侧内置敏感词库（`config/sensitive_words.txt`）与业务边界规则（`config/generate_prompt.txt`），「地址托管」等平台禁限业务不得宣传。
- 生成环节只把结构化结论喂给模型，不投喂同行口播原文，避免洗稿式改写。

## 当前边界

`search.provider` 默认是 `mock`，接真实数据需在设置里填 TikHub API Key 并切换数据源；未配 LLM 时文案由规则引擎生成；`use_asr` 默认关闭（轻量档）。这些都不影响五步流程跑通，属于按预算逐步升级的开关。
