# 私人AI分身（数字孪生）

> 完全自用 · 本地部署 · 零第三方依赖 · 双免费AI模型 · 分组记忆 · **越聊越像你**
>
> 一个跑在你自己电脑上的 AI 分身：用你的说话风格、口头禅、思维习惯回消息，记得你的喜好经历，还能看懂图片。所有数据只存在本地，永不外泄。

[![Python](https://img.shields.io/badge/Python-3.8%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green)](#许可证)
[![Zero Dep](https://img.shields.io/badge/依赖-0-orange)](#技术栈)
[![Local Only](https://img.shields.io/badge/仅本地-127.0.0.1-red)](#隐私与安全)

---

## 目录

- [它是什么](#它是什么)
- [功能亮点](#功能亮点)
- [快速开始](#快速开始)
- [API Key 配置](#api-key-配置首次使用必填)
- [文件结构](#文件结构)
- [整体架构](#整体架构)
- [核心代码解析](#核心代码解析)
- [API 接口文档](#api-接口文档)
- [记忆系统](#记忆系统)
- [学习机制](#学习机制)
- [使用技巧](#使用技巧)
- [常见问题](#常见问题)
- [自定义与扩展](#自定义与扩展)
- [隐私与安全](#隐私与安全)
- [许可证](#许可证)

---

## 它是什么

不是问答机器人，而是 **"学习你、模仿你"** 的数字分身：

- 用你自己的说话风格、口头禅、思维习惯回应
- 记得你的喜好、经历、讨厌的事，越聊越懂你
- 能发图片、看懂截图、分析照片
- 会主动找你聊天（像真人一样随机、看时机），在对话中自我提升
- 全部本地运行，零服务器、零公网、零封号风险

## 功能亮点

| 功能 | 说明 |
|---|---|
| 🗣️ **语气复刻** | 本体设定问卷 + 每5轮风格深度总结，口头禅/句式/语气自动入库 |
| 🧠 **分组记忆** | 人设/喜好/事件/短期 四大分组，按需检索，Token 硬限制 1400 永不卡顿 |
| 🖼️ **图片识别** | GLM-4V-Flash 识图，先发图再用你的语气聊 |
| 🎨 **AI 生图** | CogView-3-Flash 生成头像 + 后台趣味图工厂，主动发给你 |
| 💬 **主动聊天** | 随机间隔 + 静默时段 + 看时机不打断，每3次自我复盘改进 |
| 😊 **情绪感知** | 自动识别开心/难过/求助/分享/关心，先共情再回应 |
| 👤 **头像识别** | 上传头像后 AI "认"一次，能回答"我的头像是什么样的" |
| 📱 **仿微信 UI** | 手机式窄屏聊天框，侧栏抽屉，多会话管理，右键删除 |
| 🔑 **API 手动配置** | 侧栏可视化配置 Key，`config.json` 本地存储，`.gitignore` 防泄露 |

---

## 快速开始

### 环境要求

- Python 3.8+（[官网下载](https://www.python.org/downloads/)，安装时勾选 **Add to PATH**）
- 无需安装任何第三方库（纯标准库实现）

### 三步启动

```bash
# 1. 进入项目目录
cd 分身系统

# 2. 启动后端（默认 http://127.0.0.1:8899）
python backend.py

# 或指定端口 / 不自动打开浏览器
python backend.py --port 9000 --no-browser
```

Windows 用户也可以直接双击 **`启动分身.bat`**。

```
========================================================
 私人AI分身 已启动
 打开界面：http://127.0.0.1:8899
 文字模型：Qwen/Qwen2.5-7B-Instruct（硅基流动·免费）✓ 已配置
 识图模型：glm-4v-flash（智谱·免费）✓ 已配置
========================================================
```

3. 浏览器自动打开后，**首次会弹出 API 设置** → 填入 Key → 填本体设定问卷 → 开始聊天。

---

## API Key 配置（首次使用必填）

本项目不内置任何 API Key，所有密钥仅保存在本地 `config.json`。

| 用途 | 服务商 | 模型 | 获取地址 | 费用 |
|---|---|---|---|---|
| 文字对话 | 硅基流动 | Qwen/Qwen2.5-7B-Instruct | [cloud.siliconflow.cn](https://cloud.siliconflow.cn) | 永久免费 |
| 图片识别 + 文生图 | 智谱 AI | glm-4v-flash / cogview-3-flash | [open.bigmodel.cn](https://open.bigmodel.cn) | 每月免费额度 |

### 方式一：网页端配置（推荐）

启动后点侧栏 **「⚙️ API设置」**，分别填入两个 Key 后保存，**无需重启**即生效。

### 方式二：手动编辑配置文件

复制 `config.example.json` 为 `config.json`：

```json
{
  "silicon_key": "你的硅基流动 API Key",
  "zhipu_key": "你的智谱 API Key"
}
```

> `config.json` 已在 `.gitignore` 中，不会被 git 上传到 GitHub/CSDN。

---

## 文件结构

```
分身系统/
├── backend.py          # 本地后端（纯 Python 标准库，零第三方依赖，~1900行）
├── index.html          # 仿微信聊天界面（HTML+CSS+JS 单文件，~760行）
├── 启动分身.bat        # Windows 一键启动脚本
├── config.example.json # API Key 配置模板
├── .gitignore          # 排除 config.json / mem.db / 缓存等隐私文件
├── mem.db              # （首次运行自动生成）SQLite 记忆库
├── .img_cache/         # （自动生成）趣味图本地缓存
└── README.md
```

---

## 整体架构

```
┌─────────────────────────────────────────────────┐
│  前端 index.html（仿微信 UI）                      │
│  聊天框 / 侧栏 / 记忆库 / 本体设定 / API设置        │
└───────────────────┬─────────────────────────────┘
                    │ HTTP（仅监听 127.0.0.1）
                    ▼
┌─────────────────────────────────────────────────┐
│  后端 backend.py（http.server + sqlite3 + urllib）│
│                                                   │
│  ① 判断 文字 / 图片                               │
│  ② 检索分组记忆（中文二元组打分，Token≤1400）       │
│  ③ 组装「人设 + 关联记忆 + 最近12轮对话」           │
│  ④ 情绪检测 → 注入共情指引                         │
│  ⑤ 调用 AI 生成回复（退化检测+降温度重试3次）       │
│  ⑥ 后台线程异步学习（提取事实/总结风格→去重入库）   │
└───────────────────┬─────────────────────────────┘
                    ▼
        ┌───────────┴───────────┐
        ▼                       ▼
  硅基流动 Qwen2.5-7B      智谱 GLM-4V / CogView
  （文字对话·永久免费）      （识图·文生图·免费额度）
        │                       │
        └───────────┬───────────┘
                    ▼
        本地 SQLite (mem.db)
        记忆永不丢失、永不泄露
```

---

## 核心代码解析

### 1. 零依赖 LLM 调用

整个项目不依赖 `openai` / `requests` 等任何第三方库，用标准库 `urllib` 直接调用 OpenAI 兼容接口：

```python
# backend.py — 核心 LLM 调用（~30行，零依赖）
def call_llm(url, api_key, model, messages,
             max_tokens=400, temperature=0.8, json_mode=False, timeout=90):
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
        payload["temperature"] = 0.3
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + api_key,
    })
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"].strip()
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", "ignore")
            if e.code == 400 and json_mode and attempt == 1:
                break  # 模型不支持 response_format → 去掉 JSON 模式重试
            raise RuntimeError("HTTP %s: %s" % (e.code, err[:300]))
        except (urllib.error.URLError, OSError) as e:
            if attempt == 2:
                raise RuntimeError("网络错误: %s" % e)
            time.sleep(1.2)
    # 去掉 json_mode 再试一次
    payload.pop("response_format", None)
    return call_llm(url, api_key, model, messages, max_tokens, temperature, False, timeout)
```

### 2. 配置化 API Key（无硬编码）

密钥从 `config.json` 加载，支持运行时热更新，未配置时返回友好提示：

```python
# backend.py — API 配置管理
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
SILICON_KEY = ""
ZHIPU_KEY = ""

def load_config():
    """从 config.json 加载 API 密钥，文件不存在时静默使用空密钥。"""
    global SILICON_KEY, ZHIPU_KEY
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        SILICON_KEY = str(cfg.get("silicon_key", "") or "").strip()
        ZHIPU_KEY = str(cfg.get("zhipu_key", "") or "").strip()
    except FileNotFoundError:
        SILICON_KEY = ZHIPU_KEY = ""

def _require_key(key, label):
    """调用 AI 前检查密钥，未配置则抛出用户友好的错误。"""
    if not key:
        raise RuntimeError("尚未配置 %s。请在侧栏「⚙️ API设置」中填写你的 API Key。" % label)

def qwen(messages, **kw):
    _require_key(SILICON_KEY, "硅基流动 API Key（文字对话模型）")
    return call_llm(SILICON_URL, SILICON_KEY, QWEN_MODEL, messages, **kw)
```

### 3. 中文记忆检索（二元组相关性打分）

不用向量数据库，用轻量中文二元组做相关性打分，按需检索从不全量加载：

```python
# backend.py — 记忆检索核心
def grams(text):
    """中文分词近似：取汉字串/单词的二元组用于相关性打分。"""
    out = set()
    for m in re.findall(r"[\u4e00-\u9fffA-Za-z0-9]+", (text or "").lower()):
        if len(m) == 1:
            out.add(m)
        else:
            out.add(m)
            for i in range(len(m) - 1):
                out.add(m[i:i + 2])
    return out

def _score(query_grams, content):
    g = grams(content)
    return len(query_grams & g)  # 交集大小 = 相关分

# 检索时：person 人设全部按相关性排序注入；
# preference/event 只取分数>0的前4条；chat_temp 取最近12轮
```

### 4. SQLite 数据库 Schema

```python
# backend.py — init_db() 中的表结构
CREATE TABLE memories(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  grp TEXT NOT NULL,          -- person / preference / event
  tag TEXT DEFAULT '',        -- 设定 / 风格 / 爱好 / ...
  content TEXT NOT NULL,      -- 20-60字事实摘要
  created_at REAL,
  last_used REAL,
  use_count INTEGER DEFAULT 0
);
CREATE TABLE sessions(id, title, created_at, updated_at);
CREATE TABLE messages(id, session_id, role, content, image, created_at);
CREATE TABLE gen_images(id, prompt, caption, fname, created_at, sent);
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);  -- 头像/网名/设置等
```

### 5. 抗退化回复机制

Qwen 7B 小模型偶发"复读/套路腔/堆emoji"，系统自动检测并降温度重试：

```python
# backend.py — 退化检测（启发式，零额外依赖）
def _is_degenerate(text, user_msg=""):
    # ① AI 套路腔黑名单：能理解你的心情 / 要不要我给你 / 让我猜猜 ...
    # ② 复读用户原话开头（连续6字相同）
    # ③ 重复词占比 >45%
    # ④ 标点占比 >45%
    # ⑤ emoji >3个
    # ⑥ 单字占比 >55%
    ...

def _safe_call_chat(msgs, attempts=3, ...):
    for i in range(attempts):
        temp = 0.45 if i == 0 else (0.30 if i == 1 else 0.25)
        out = qwen(msgs, max_tokens=240, temperature=temp)
        out = clean_reply(out)          # 清洗排版/重复字/拆行
        if not _is_degenerate(out, user_msg=text):
            return out
    return fallback                     # 三次都退化 → 按情绪给简短人话兜底
```

---

## API 接口文档

所有接口仅监听 `127.0.0.1`，返回 JSON 格式 `{"ok": true/false, ...}`。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 聊天界面（index.html） |
| GET | `/api/config` | 获取 API Key 配置状态（不返回密钥本身） |
| POST | `/api/config` | 保存 API Key（`{"silicon_key":"...", "zhipu_key":"..."}`，支持部分更新） |
| GET | `/api/meta` | 获取元信息（名字/头像/主动学习设置等） |
| GET | `/api/sessions` | 会话列表 |
| POST | `/api/sessions` | 新建会话 `{"title":"..."}` |
| DELETE | `/api/sessions/{id}` | 删除会话及全部消息 |
| GET | `/api/messages?session={id}` | 获取某会话的消息列表 |
| POST | `/api/chat` | 发送消息 `{"session_id":1, "text":"你好", "image":""}` |
| GET | `/api/memories?group={grp}` | 记忆列表（person/preference/event） |
| POST | `/api/memories` | 手动添加记忆 `{"group":"preference","tag":"爱好","content":"..."}` |
| DELETE | `/api/memories/{id}` | 删除一条记忆 |
| GET | `/api/memstats` | 记忆统计（各分组数量/学习轮数/主动聊天状态） |
| GET | `/api/profile` | 获取本体设定问卷数据 |
| POST | `/api/profile` | 保存本体设定 |
| POST | `/api/proactive` | 手动触发分身主动发一条消息 |
| POST | `/api/makeover` | 触发分身换形象（网名+AI头像） |
| POST | `/api/avatar` | 上传头像 `{"who":"user"/"ai", "data":"data:image/..."}` |
| POST | `/api/avatar_recognize` | 重新识别用户头像 |
| GET | `/api/poll` | 轮询主动消息/形象更新 |

### 聊天请求示例

```bash
curl -X POST http://127.0.0.1:8899/api/chat \
  -H "Content-Type: application/json" \
  -d '{"session_id":1,"text":"今天好累啊","image":""}'
```

```json
{
  "ok": true,
  "data": {
    "reply": "累坏了吧，搞啥项目啊",
    "session_id": 1,
    "image": "",
    "image_desc": null
  }
}
```

---

## 记忆系统

四大分组，工业级设计，解决"越用越慢、遗忘、混乱"三大痛点：

| 分组 | 内容 | 加载策略 | 容量上限 |
|---|---|---|---|
| `person` 人设 | 称呼、说话风格、口头禅、性格、本体设定 | **每次必带**，按相关性+设定tag排序 | ≤80条 |
| `preference` 喜好 | 喜欢/讨厌/价值观/习惯 | 仅话题**相关**才检索调入（前4条） | ≤200条 |
| `event` 事件 | 具体经历/计划/事实 | 仅相关才调入；**90天前自动归档** | ≤10万条 |
| `chat_temp` 短期 | 最近 12 轮对话 | 直接取当前会话最近记录 | 12轮 |

**关键机制：**
- **按需检索**：中文二元组相关性打分，从不全量加载 → 永远不会越用越慢
- **入库前压缩**：学习提取强制输出 20-60 字事实摘要，禁止大段原文
- **去重合并**：`difflib` 相似度 ≥0.62 自动合并更新，不堆垃圾
- **Token 硬限制 1400**：超预算时按 历史 → 关联记忆 → 人设 顺序裁剪
- **脏数据拦截**：外语字符/模板残留/AI瞎编模式不入库
- **容量封顶**：超出自动清理最久未用的

---

## 学习机制

分身的核心是"慢慢变成你"，通过五层学习实现：

1. **本体设定问卷**：12+ 组点选（说话风格/性格/爱好/口头禅/雷区等），直接写入人设库，可随时修改
2. **每轮对话后台学习**：回复完消息的同一刻，后台线程用 Qwen 做"记忆提取"，从你说的内容里提炼偏好/事件事实（最多3条）→ 压缩 → 去重 → 入库
3. **每 5 轮风格深度总结**：翻看你的近期发言，提炼口头禅/句式/语气/性格，写入人设库（tag=风格）
4. **分身主动学习**：像真人一样随机找你聊，每条消息带「当前时间 + 你最近一句 + 记住的你的小事 + 随机聊天意图」；每3次主动聊天后自我复盘，把改进建议写进人设
5. **手动微调**：侧栏"记忆库"可随时查看/添加/删除任何记忆

> 模仿精度 = 喂给它的素材质量。想让它像你，就多聊、多说真实的自己。

---

## 使用技巧

- **识图**：点输入框左侧 📷 发图片，分身先"看懂"再按你的语气聊
- **多会话**：侧栏"＋ 新会话"分主题聊，各会话短期记忆独立
- **右键删除会话**：会话列表上右键该条目可删除整个会话
- **分身主动聊天**：点侧栏"💬 分身主动聊天"立即来一条；节奏在"本体设定"里调
- **分身换形象**：点"🎨 分身换形象"或聊天框顶部分身头像，自动起网名 + AI 生成新头像
- **换你的头像**：点侧栏左上角你的头像，选择图片上传即可
- **换机/备份**：直接拷贝 `mem.db` 就是带着全部记忆搬家
- **重置分身**：关闭服务后删除 `mem.db`，重启即全新分身

---

## 常见问题

**Q：启动报"不是内部或外部命令 Python"？**
A：未装 Python 或未加入 PATH，去 [python.org](https://www.python.org/downloads/) 重装并勾选 Add to PATH。

**Q：聊天提示"尚未配置 API Key"？**
A：点侧栏「⚙️ API设置」填入硅基流动和智谱的 Key 后保存即可，无需重启。两个 Key 都可免费申请，详见 [API Key 配置](#api-key-配置首次使用必填)。

**Q：API Key 存在哪里？会泄露吗？**
A：仅保存在本地 `config.json`，该文件已加入 `.gitignore`，不会被 git 上传。整个系统只监听 `127.0.0.1`，外部无法访问。

**Q：分身回答有时词不达意？**
A：Qwen2.5-7B 是 7B 小模型（免费），复杂推理是它的短板。系统已内置"抗退化"机制：精简人设提示词 + 回复质量自动检测 + 降温度重试 3 次。需要更强推理可改 `backend.py` 里 `QWEN_MODEL` 为 `Qwen/Qwen2.5-72B-Instruct`（可能按量计费，慎用）。

**Q：分身答不上"我的头像是什么样的"？**
A：需要让它"认"一次你的头像：上传头像后分身会自动用识图模型记住；也可在记忆库弹窗点"识别头像"手动触发。

**Q：聊天时会显示加载状态吗？**
A：发送后「发送」按钮变为「思考中…」并禁用，回复完成后自动恢复并聚焦输入框。不显示打字气泡，避免打扰。

**Q：点击分身头像"换形象"没反应？**
A：点击后会立即换成新网名 + emoji 头像，精美的 AI 图片头像在后台继续生成并自动替换。几秒内没变化多半是模型太慢，稍等重试。

**Q：记忆存在哪里？安全吗？**
A：全部存在本地 `mem.db`（SQLite），系统只监听 `127.0.0.1`（仅本机），不依赖任何第三方接口回调，无封号风险。

**Q：可以换模型/加语音吗？**
A：代码全开源可改。扩展方向：语音输入输出（ASR/TTS）、表情包、情绪分析、记忆相册等。

---

## 自定义与扩展

### 更换模型

编辑 `backend.py` 顶部的模型常量：

```python
QWEN_MODEL = "Qwen/Qwen2.5-7B-Instruct"   # 文字模型（硅基流动）
GLM_MODEL = "glm-4v-flash"                 # 识图模型（智谱）
COGVIEW_MODEL = "cogview-3-flash"          # 文生图模型（智谱）
```

### 调整记忆参数

```python
TOKEN_BUDGET = 1400       # 记忆/历史总 Token 预算
TEMP_ROUNDS = 12          # 短期会话保留轮数
ARCHIVE_DAYS = 90         # 事件记忆归档天数
STYLE_EVERY_N = 5         # 每 N 轮做一次风格总结
MEM_CAP = {"person": 80, "preference": 200, "event": 100000}
```

### 调整主动聊天行为

```python
QUIET_START_HOUR = 0      # 静默时段起点（绝不打扰）
QUIET_END_HOUR = 8        # 静默时段终点
MIN_IDLE_MIN = 4          # 主人最近 N 分钟内还在聊就先不插嘴
P_IMAGE = 0.4             # 主动发图的概率
PROACTIVE_JITTER = (0.55, 1.7)  # 间隔随机抖动系数
```

### 前端 UI

`index.html` 是单文件应用（HTML+CSS+JS），可直接用浏览器打开调试。配色变量在 `:root` 中：

```css
:root{
  --side-bg:#2e2e2e; --chat-bg:#ededed;
  --user-bubble:#95ec69; --ai-bubble:#ffffff;
  --green:#07c160; --blue:#10aeff;
}
```

---

## 隐私与安全

- **零公网**：HTTP 服务仅绑定 `127.0.0.1`，局域网和互联网都无法访问
- **零回调**：不依赖微信/QQ/企业微信任何第三方接口，无封号风险
- **本地存储**：对话记录、记忆、头像全部存在本地 `mem.db`（SQLite）
- **密钥隔离**：API Key 存在 `config.json`，已加入 `.gitignore`，不会被误传
- **可审计**：全部代码开源可读，无隐藏上传、无遥测、无埋点

---

## 许可证

MIT License — 可自由使用、修改、分发。使用本项目产生的任何后果由使用者自行承担。

---

> 如果这个项目对你有帮助，欢迎 Star ⭐ / 分享给更多人。
