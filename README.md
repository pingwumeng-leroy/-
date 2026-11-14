# 抖音内容工作台（合胜宏企服）

给老板用的内部工具网站：**输入一段选题/关键词 → 批量采集同行抖音视频并生成结构化分析报告入库 → 基于报告生成原创口播文案 → 老板按稿拍摄并上传 → 自动剪辑成 9:16 竖屏成片下载**。

技术上是一个 Flask + SQLite 的单机应用，零外部数据库，后台线程跑采集与剪辑任务，前端轮询进度。

## 五步流程

| 步骤 | 页面 | 做了什么 |
| --- | --- | --- |
| 1 找素材 | 抖音链接采集 / 上传本地视频 / 手填文案 | 提取口播内容 |
| 2 看分析 | 单条分析 + 批量采集报告 | 结构化报告写入 `reports` 表 |
| 3 出文案 | 输入要求，一键生成原创脚本 | 只复用抽象结论，不复用同行原文 |
| 4 拍口播 | 定稿文案 + 上传口播视频 | 老板只需按稿拍（可一键跳过，直接去剪辑） |
| 5 剪成片 | 后台自动剪辑 | 三种出片：口播剪辑 / 无原片 TTS 成片 / MotionGit 模板 |

## 三种出片方式（第 5 步）

| 模式 | 需要什么 | 说明 |
| --- | --- | --- |
| 口播剪辑 | 第 4 步上传的口播原片 | 内置 FFmpeg 模板：片头/片尾、ASS 中文字幕、水印、竖屏 9:16 |
| 无原片·TTS 成片 | 只要一份定稿文案 | edge-tts 念文案当配音 + PIL 卡片 + ffmpeg 合成，零成本、零外部依赖 |
| MotionGit 模板 | 官网下载的 `.motion` 工程包 | 真对接：包自带渲染管线（adapter.py / render.mjs / headless Chrome），动效 100% 还原 |

### MotionGit Creator API（配音 / 生图 / 素材）

Creator API 只准备「素材 + 配音 + 编排」，**成片仍在本机渲染**。在设置页填 MotionGit Key（`mgk_` 开头，[motiongit.com/settings/api](https://motiongit.com/settings/api) 生成）：

- 配音：`volc-tts-2.0` 火山引擎高品质，5 积分/100 字，446 个音色、31 种语言、带词级时间轴（比 edge-tts 强很多）
- 生图：4 积分/张
- 素材/贴纸/音乐检索：**0 积分**（每账号每天 30 次，上海时间午夜重置）

调用链：`POST /api/v1/quotes`（报价）→ `POST /api/v1/generations`（提交，带 `Idempotency-Key`）→ `GET /api/v1/tasks/{id}`（轮询）→ `GET /api/v1/assets/{id}/download`（下载）。鉴权统一 `Authorization: Bearer <Key>`。

**API 不负责剪辑/渲染。** 边界很清楚：

| 环节 | 谁来做 | 是否耗积分 |
|---|---|---|
| 找图/找视频/贴纸/音乐库检索 | Creator API | 0（每账号每天 30 次） |
| 配音 TTS 2.0 | Creator API | 5 / 100 字 |
| 生图、生成视频 | Creator API | 4/张、20/条 |
| 编排（文案→分镜参数） | Creator API `motion.compose` | 12 / 次，**目前仅受邀账号** |
| **出成片（渲染 mp4）** | 本机，包内引擎 | 0 |

所以「直接调 API 出片」这条路不存在——API 只准备素材、配音、编排，最后一帧一帧的渲染必须在本机跑。好处是渲染免费、可无限次重跑；代价是服务器要能吃下 Node 22 + 无头渲染。

> 没有 `.motion` 包也能用 TTS 模式先出片；有了真包再切「MotionGit 模板」出精细动效。未配置 Key 时，配音自动回落 edge-tts（免费）。

### .motion 包运行前提

模板包有两代结构，工作台会自动识别（上传即可，无需手动选类型）：

| 包类型 | 特征 | 执行方式 |
|---|---|---|
| **motion-kit**（官网现行） | `motion.json` + `assistant/motion.mjs` + `content/release.bin` | 调用包内官方助手 `start→new→setup→check→plan→prepare→preview→render`，本机出片 |
| **poster-kit**（竖屏海报版式） | 同上，但 `guide.json` 里有 `workflowRouting.textOnly`，且附 `poster-kit.spec.json` | 同一套助手子命令，但**参数合同不同**（见下） |
| **python-adapter**（旧） | `adapter.py` + `params.schema.json` | 直接跑 `adapter.py plan/prepare/render` |

motion-kit / poster-kit 包的要点：

- **渲染在本机完成，不消耗任何积分**；账户积分为 0 也能出片。
- 原包整份保留为 `data/motion/<名>/package.motion`，只解出 `assistant/` 供执行，`personal/`（含 Key）与 `content/release.bin` 不落地成明文。
- 镜头由文案自动编排（关键词→控件：标题/要点/步骤/对比/名词解释/提示/数字/CTA/片尾），一条片子至少 6 种控件。
- 官网现行包需要 **Node 22+**；旧的 adapter 包需要 Python 依赖（见下）。
- 模板右上角的时间码与 `.m 角标` 是模板官方设计（time corner），不是可去的水印。
- 安装时会读 `guide.json` 把模板画像（工作流、原生画幅、画幅尺寸、控件清单）固化进 `data/motion/<名>/profile.json`，出片时按画像自动选路线与画幅，不会再出现「横版构图硬塞竖屏造成大面积留白」。

### 两条工作流的参数差异（踩过的坑）

竖屏海报包（`vibrant-poster-portrait` 这类）走的是 **poster-kit**，不是 motion-kit。两者共用助手，但参数合同完全不同：

| 环节 | motion-kit | poster-kit |
|---|---|---|
| `contract` | `contract <pkg> motion-kit` | `contract <pkg> poster-kit`（传 motion-kit 会 `WORKFLOW_UNAVAILABLE`） |
| `setup` | 可选 `--cache` | **必须** `--cache <dir>`，否则 `RUNTIME_FAILED: Choose an explicit dependency cache` |
| `check-params` | `check-params <json> --inputs <dir>` | `check-params --workflow poster-kit --timeline <json>`，**素材要放在实例根目录**（与 `timeline.json` 同级），放 `inputs/` 会报 `Missing media voiceover` |
| 时间轴骨架 | `{workflow, header, timeline:{…}}` | 时间轴本体在顶层，必填 `duration / segments / voiceover / music / presenter` |
| 渲染 | `render <project> --out` | `render --workflow poster-kit --timeline @vault --plan <dir> --preview-review <dir> --out` |

poster-kit 的校验器有几道硬关卡，任一不过直接拒：

1. **最短时长**：每个控件有 `minDur`（`steps 3.5`、`definition/compare/outro 4`、`list/tip 3`、`opener 2`…），低于就报 `时间轴不合格`。
2. **音频时钟**：`AUDIO_CLOCK_MISMATCH` —— 成片总时长必须等于配音时长（±0.02s）。配音比模板下限短时，工作台会补尾部静音并把最后一条字幕拉长。
3. **无空洞**：`RECOMPOSE_REQUIRED` —— 段与段不能重叠、不能留缝，末段必须正好到 `duration`。
4. **旁白归属**：`narrationMap` 必须**每个镜头一行、按输出顺序**，且每个镜头都有 `spokenText`（`ALIGNMENT_REQUIRED`）。
5. **旁白连续**：`NARRATION_CONTINUITY` —— `captionIndices` 必须是**覆盖整屏的连续一组**字幕（首尾相接盖满 `[t0,t1]`），只挑「相交」的会因贴边和短句漏项而盖不满。
6. **字幕两行**：单条字幕超两行，渲染器直接抛 `Caption exceeds two explicit lines`（预览阶段才炸，本地 `check-params` 查不出）。工作台把每条字幕压在 20 字宽内（中文计 1、ASCII 计 0.55），按标点优先切句、时间按字数比例分配。

所以竖屏出片的时长不是「文案多长就多长」，而是「配音时长 ⇄ 镜头时长 ⇄ 字幕 ⇄ narrationMap 四者同一时钟」，由 `bind_poster_audio()` 一次算齐；配音太短塞不满模板下限时，宁可整体放慢 + 补尾静音，也不让画面被砍。

### 竖屏排版的三个实测结论

1. **镜头数比镜头时长重要**。一句话硬撑 5 秒会变成「一屏停尸」。工作台会按配音时长反推该有几屏（`(配音时长 - 6s 片头片尾) / 2.8s`），句子不够就把长句按逗号拆开，多铺几屏而不是拉长单屏。
2. **opener 标题限 9 字**。1080 宽下可用区 960px，一个中文字约 96px，10 字就顶到 965px 压线。超出部分自动落到副标题。
3. **上下留白是平台预留，不是 bug**。`captionSafeArea: short-video` 会按 `captionPosition`（默认底 24%、左 8%、右 16%）给抖音底部文案和右侧按钮让位。工作台改成底 20% / 左 8% / 右 10%，多留版面给内容且左右对称。

判断「排版是否真的撑破」不能直接数 `geometry.json` 的违例条数：模板每 0.05s 采一帧，文字砸入飞出时本来就会短暂出画，一个正常片头能数出 200 多处。工作台只统计**每段镜头后半程**（动画已停）、且横向溢出超过可用宽度 4% 的文字，才触发缩字重排。

旧 adapter 包的依赖：

```bash
sudo python3 -m pip install --break-system-packages \
  -i https://mirrors.aliyun.com/pypi/simple/ \
  numpy scipy soundfile pyloudnorm jsonschema
```

**ffmpeg 必须带 `libx264`**（kit 包的官方助手固定用 `libx264` 编码；Ubuntu 自带的精简构建常常没有）。自检：

```bash
ffmpeg -hide_banner -encoders | grep libx264     # 有输出才合格
```

如果缺，最快的补法（走国内镜像，约 30 秒）：

```bash
sudo python3 -m pip install --break-system-packages -i https://mirrors.aliyun.com/pypi/simple/ imageio-ffmpeg
sudo cp /usr/local/lib/python3.12/dist-packages/imageio_ffmpeg/binaries/ffmpeg-linux-x86_64-* /usr/local/bin/ffmpeg
```

> 若包显示「⚠ 不完整」，说明 .motion 下载被截断（常见于网络断流），重新下载完整包再上传即可。


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

> **服务器已有 Caddy（80 端口被占）时，直接跳到下方「已有 Caddy 的服务器」**：不要装 nginx、不要执行任何 nginx 命令，也不要跑 certbot。

```bash
sudo apt install -y python3-venv ffmpeg fonts-noto-cjk nginx   # nginx 仅在服务器上没有 Caddy 时才装

# /opt 属主是 root，普通用户不能在里面建目录：先建好，再把属主改成自己
sudo mkdir -p /opt/douyin-studio
sudo chown -R "$(id -un):$(id -gn)" /opt/douyin-studio

# 方式 A｜代码包
tar -xzf douyin-studio.tar.gz -C /opt/douyin-studio

# 方式 B｜Public 仓库 clone（目标目录必须为空）
# git clone https://github.com/pingwumeng-leroy/-.git /opt/douyin-studio

cd /opt/douyin-studio
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config/settings.example.json config/settings.json       # 不复制也行，首启自动生成

# systemd 以 www-data 运行，data/ 和 config/ 都必须可写：
#   data/   —— SQLite、上传视频、成片
#   config/ —— 设置页保存 settings.json 与剪辑模板；漏掉会报 Permission denied
# 属主=你、组=www-data、组可写，服务与手动运行 app.py 两种情况都能写
sudo mkdir -p data
sudo chown -R "$(id -un):www-data" data config
sudo chmod -R g+rwX data config     # 大写 X：只给目录加执行位，目录没有 x 照样进不去
sudo chmod o+rx /opt/douyin-studio  # 服务用户至少要能进入项目根目录

sudo cp deploy/douyin-studio.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now douyin-studio

# /etc/nginx/sites-available 只有装了 nginx 才存在
sudo mkdir -p /etc/nginx/sites-available /etc/nginx/sites-enabled
curl -s -o /dev/null -w '后端状态 %{http_code}\n' http://127.0.0.1:8080/healthz   # 应为 200，否则反代会 502
sudo cp deploy/nginx.conf.example /etc/nginx/sites-available/douyin-studio
# 按下方「证书前置」处理后再启用：
sudo ln -s /etc/nginx/sites-available/douyin-studio /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl enable --now nginx    # 首次部署必须 start；reload 只对已运行的服务有效
# 之后再改配置：sudo nginx -t && sudo systemctl reload nginx
```

`nginx.conf.example` 已配置 `client_max_body_size 1024m`（口播视频上传）与 `proxy_read_timeout 600s`（剪辑任务耗时）。

### 网站打开只显示一段 JSON 怎么处理

若页面返回 `{"error":"[Errno 13] Permission denied: '.../data'","ok":false}`，不是服务崩了，是 **`data/` 或项目根目录对服务用户不可写/不可进入**。`get_settings()` 每个请求都会检查这些目录，所以权限一坏，连首页都会被全局异常处理转成 JSON。常见于部署后以 `sudo` 跑过 `mkdir` / `cp` / `chown`，把属主改回了 root。

```bash
SVC=$(systemctl show -p User --value douyin-studio); SVC=${SVC:-www-data}
stat -c '%U:%G %a %n' /opt/douyin-studio /opt/douyin-studio/data /opt/douyin-studio/config
sudo chgrp -R "$SVC" /opt/douyin-studio/data /opt/douyin-studio/config
sudo chmod -R g+rwX /opt/douyin-studio/data /opt/douyin-studio/config
sudo chmod o+rx /opt/douyin-studio
sudo systemctl restart douyin-studio
curl -s -o /dev/null -w '后端 %{http_code}\n' http://127.0.0.1:8080/healthz   # 应为 200
```

启动日志里 `[BOOT] 目录可写： data=True config=True` 也能直接确认。`journalctl -u douyin-studio -n 30` 可看 restart 是否成功。

**证书前置**：示例的 443 段引用 `/etc/letsencrypt/live/YOUR_DOMAIN/fullchain.pem`，没签证书时 `nginx -t` 会直接失败。两条路：

1. **先签证书（推荐）**：先放一份只监听 80 的最小配置，`nginx -t && sudo systemctl restart nginx` 后执行 `sudo apt install -y certbot python3-certbot-nginx && sudo certbot --nginx -d 你的域名`，certbot 会自动补 SSL 与跳转；最后把 `client_max_body_size 1024m`、`proxy_read_timeout 600s` 补进 443 段。最小配置：

   ```nginx
   server {
       listen 80;
       server_name 你的域名;
       client_max_body_size 1024m;
       proxy_read_timeout 600s;
       location / {
           proxy_pass http://127.0.0.1:8080;
           proxy_set_header Host $host;
           proxy_set_header X-Real-IP $remote_addr;
           proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
           proxy_set_header X-Forwarded-Proto $scheme;
       }
   }
   ```

2. **已有证书**：把示例里 3 处 `YOUR_DOMAIN` 和两行证书路径改成实际值即可。

配置 nginx 前先确认后端活着：`curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/healthz` 应返回 200，否则浏览器会看到 502。

### 获取代码：clone 或代码包

仓库是 **Public**，服务器上直接 clone 即可，不需要任何凭证：

```bash
cd /opt
git clone https://github.com/pingwumeng-leroy/-.git douyin-studio
```

> `/opt` 属主是 root，普通用户不能在其中新建目录。若报 `could not create work tree dir ... Permission denied`，先执行
> `sudo mkdir -p /opt/douyin-studio && sudo chown -R "$(id -un):$(id -gn)" /opt/douyin-studio`，
> 再重跑上面的 clone（git 允许 clone 进已存在的空目录）。

不想用 git，也可以用 `douyin-studio.tar.gz` 代码包，内容完全一致：

```bash
sudo mkdir -p /opt/douyin-studio && sudo chown -R "$(id -un):$(id -gn)" /opt/douyin-studio
tar -xzf douyin-studio.tar.gz -C /opt/douyin-studio
```

> **仓库公开后尤其注意**：`config/settings.json`、`data/`、`*.db` 已被 `.gitignore` 排除，其中会存 TikHub Token、访问密码和抖音 Cookie。任何情况下都不要用 `git add -f` 把它们提交上去——一旦 push 就是公开泄露。

## 已有 Caddy 的服务器（推荐）

服务器上已经有 Caddy 占着 80 端口时，不要跟它抢：把 douyin-studio 作为**一个新站点块追加**到现有 Caddy 配置即可。Caddy 自带自动 HTTPS，比 nginx + certbot 省事得多。

```bash
# 1. 先确认 Caddy 的形态与配置文件位置
systemctl status caddy --no-pager | head -12
sudo cat /etc/caddy/Caddyfile
docker ps --format '{{.Names}} {{.Image}} {{.Ports}}' | grep -i caddy   # 若上面显示 not found

# 2. 确认后端活着（应为 200）
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8080/healthz

# 3. 备份后追加站点块，把「你的域名」换成实际域名（完整示例见 deploy/Caddyfile.example）
sudo cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.bak
sudo tee -a /etc/caddy/Caddyfile >/dev/null <<'EOF'

你的域名 {
    reverse_proxy 127.0.0.1:8080
}
EOF

# 4. 校验并热加载，不会中断现有站点
sudo caddy fmt /etc/caddy/Caddyfile >/dev/null && sudo systemctl reload caddy

# 5. 清理之前为 nginx 做的准备，避免以后误启动抢 80
sudo systemctl disable --now nginx
sudo rm -f /etc/nginx/sites-enabled/douyin-studio
```

验证：`curl -s -o /dev/null -w '%{http_code}\n' -H 'Host: 你的域名' http://127.0.0.1/healthz` 应返回 200；浏览器打开 `https://你的域名/healthz`，证书由 Caddy 自动签发。

- **Caddy 跑在 Docker 里**：改的是容器挂载的 Caddyfile（`docker inspect <容器名>` 找挂载路径），然后 `docker exec <容器名> caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile`；容器里没有 `systemctl`。
- **Caddy 是云平台托管的反代**（配置文件不可编辑，也不是你能改的容器）：去平台控制台加一条「域名 / 端口转发」指向 `127.0.0.1:8080`，本机不用改任何东西。

> 无论用 nginx 还是 Caddy，公网部署都必须在设置页开启 `auth.enabled` 访问密码。

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
- 搜索翻页使用上一次响应带回的 `cursor` / `search_id` / `backtrace`，不用硬编码页码；时间范围 `0/1/7/180` 天映射到 TikHub 的 `publish_time=0/1/7/180`，选「不限」传 `0`。

## 接入 TikHub（首次拿真实数据）

前置条件：注册 [TikHub](https://tikhub.io) → **验证邮箱**（未验证会 403）→ 充值或确认该端点有免费额度（余额不足会 402）→ 在用户中心创建 API Token。Token 只放请求头 `Authorization: Bearer <token>`。

1. 打开网站右上角「设置」→「数据源 · 批量采集报告」：
   - 默认数据源改选 `TikHub（真实抖音数据）`
   - Base URL 保持 `https://api.tikhub.io`（官方主域名，不要用 `api.tikhub.dev`）
   - 填入 API Key，点「保存设置」
2. 点「**测试数据源连接**」。这一步会先保存设置，再发 **1 次**真实搜索请求，返回条数、样例标题和点赞数。它读的是已落盘的配置，所以必须先保存。
3. 第 2 步「看分析」里先小额试跑：要几份报告填 `5`、时间范围选近半年、数据源选 TikHub、点「一键采集并分析」。任务面板能看到每一步日志和调用次数。确认报告列表里 `来源` 显示 `TikHub` 再放大批量。

**计费口径**：每页搜索 1 次调用，每条视频取评论再 1 次调用。一次任务大致是「关键词数 × 每关键词页数 + 入库报告数 」，`max_calls` 是硬上限；评论取不到只影响报告里的「评论区高频提问」，不中断主流程。

**错误码对照**（测试按钮和任务日志里都能看到，均已脱敏）：

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `AuthFailed` / HTTP 401、403 | Token 无效、邮箱未验证、账户被禁用 | 检查 Token 与邮箱验证状态 |
| `QuotaExceeded` / HTTP 402 | 余额不足，或该端点不接受免费额度 | 充值 |
| `RateLimited` / HTTP 429 | 请求过快 | 降低 `pages_per_keyword`，稍后重试 |
| `ProviderUnavailable` | 上游抓取失败 / 超时 | 可重试；失败会自动降级到演示数据 |

> 降级后报告来源会带 `fallback` 标记，**不要把演示数据当成真实采集结果**。测试按钮和 `GET /api/providers` 都能确认当前生效的数据源。

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
| POST | `/api/providers/test` | 数据源自检（1 次真实搜索调用，读已保存的配置） |
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

`search.provider` 默认 `mock`；`llm.enabled` 默认 `false`（用内置规则引擎）；`use_asr` 默认关闭（轻量档）。三者都不影响五步流程跑通，属于按预算逐步升级的开关。

## 轻量 AI 十维分析（DeepSeek）

批量采集的报告在配好 LLM 后自动切换成「AI 十维」，网页内点「详情」看完整报告；未配置或调用失败自动回落到规则引擎，报告字段结构完全一致，第 3 步生成文案无需改动。

**配置**：设置 → 大模型接口 → 启用 / Base URL `https://api.deepseek.com` / 填 API Key / 模型名 `deepseek-flash` → 保存 → 点「测试大模型连接」确认模型名被上游接受。模型名不被接受时改成上游实际名称即可，其它代码不动。

**评论区数据边界**（本次核实的结论）：

- 评论端点 `GET /api/v1/douyin/web/fetch_video_comments`，参数 `aweme_id` / `cursor` / `count`；官方明确要求 `count` 保持默认 **20**（原文「请保持默认，否则会出现 BUG」）。
- 翻页用上一次响应里的 `cursor`，每翻一页计费一次。默认 `harvest.comment_pages = 3`，即每条约 60 条；与搜索共用 `max_calls` 预算，预算用完自动跳过评论采集。
- **官方 OpenAPI 只定义了参数，没有承诺单条评论的字段**，因此 `core/providers.py::_comment_row()` 对 `text` / `digg_count` / `reply_comment_total` / `create_time` / `cid` 做多键名兜底，缺失降级为空值，不假设必有。
- 评论回复需另调 `fetch_video_comment_replies`（`item_id` + `comment_id`），轻量档不取；评论者画像不取。

**报告章节**（按确认结果）：一句话结论 / 关键爆款因素 / 一互动数据结构 / 二时长策略 / 三标题与话题 / 四开头钩子与讲述节奏 / 六评论区 / 八 AI 十维拆解。**第五章「画面与剪辑」、第七章「与博主最近作品对比」不生成**。第四章在轻量档标注「无逐字稿」；开启 `use_asr` 后改为展示真实前 3 秒 / 15 秒原话、语速与信息密度曲线（见下节）。

**防幻觉**：所有量化数字（互动三率、时长、标题特征、评论加权情绪、@ 占比、时间跨度，以及有稿时的语速与密度曲线）由本地规则层计算；提示词逐条禁止模型编造镜头切换、播放量、完播率、博主近期中位数与具体政策条款。轻量档额外禁止编造前 3 秒原话与语速；有逐字稿时反过来要求**逐字引用给定原话，不得改写或补写**。完整报告 JSON 存在 `reports.report_json` 的 `facts` / `ai_analysis` 两个节点，**表结构未改**。

**新增接口**：`POST /api/llm/test`（模型连通性自检）。设置项新增「每条视频评论翻页数」「逐字稿（完整档）」「本地转写模型」。

## 完整档：本地逐字稿（faster-whisper）

做法移植自 `dy-/analyze_douyin.py`：`transcribe_with_whisper()` → `load_transcript()` → `analyze_transcript()`。逐字稿必须是**带时间戳的 SRT**，纯文本算不出前 3 秒原话 / 语速 / 信息密度曲线。

**开启**：设置 → 数据源 · 批量采集报告 → 「逐字稿（完整档）」选开启 → 选模型（`small` 与 dy- 一致）→ 保存。服务器先装依赖：

```bash
cd /opt/douyin-studio && sudo .venv/bin/pip install faster-whisper
sudo systemctl restart douyin-studio
```

首次运行会自动下载模型（约 500MB）。国内服务器默认走 `HF_ENDPOINT=https://hf-mirror.com`，模型缓存放在 `data/hf_cache`（已 gitignore），两者都可用环境变量覆盖，不需要改 systemd。

**链路**：`harvest.use_asr=true` 时，对本次要入库的每条视频 → `TikHub play_url` 下载（普通 HTTP，不消耗 TikHub 调用次数）→ `ffmpeg` 抽 16k 单声道音频 → 本地 `faster-whisper`（`int8` / `vad_filter` / 简体中文引导词）→ 带时间戳 `transcript.srt` → `core/transcript.py` 本地算指标 → 与 `facts` 一起喂 DeepSeek。视频、音频、SRT 全放临时目录，**用完即删**，只有逐字稿文本进入 `reports.report_json`。

**模型按规格缓存在进程内**，批量 5 条只加载一次；44 秒视频 `small/int8` 约 20–60 秒，5 条约 2–5 分钟，任务日志会打印逐字稿字数。

**失败自动回落**：没装 `faster-whisper`、`play_url` 防盗链 / 过期、下载返回网页、转写为空或异常，任一情况都只让这一条退回轻量档并写日志，**不中断整批任务**，其余报告照常入库。建议首次启用只采 1 条验证 `play_url` 可用性。

**仍不生成**：第五章画面 / 关键帧 / 镜头切换、第七章博主作品对比、播放量与完播率（TikHub 不提供）。DeepSeek 没有 `/audio/transcriptions`，所以转写不走 `settings.asr` 那套云端接口。
