# 受够了AI客服腔？我用1900行纯Python零依赖代码，写了个越聊越像我的本地AI分身

> 本文介绍一个完全本地运行的 AI 分身聊天系统的设计思路与核心代码实现。整个后端仅用 Python 标准库完成（http.server + sqlite3 + urllib），不依赖任何第三方包；前端是单个 HTML 文件，仿微信聊天界面。项目的核心亮点在于**分组记忆系统**、**语气复刻学习机制**和**双免费AI模型调度**。

---

## 一、项目背景与定位

市面上的 AI 聊天产品大多是云端服务，数据不在自己手里，也很难做到"模仿某个人的说话风格"。这个项目的目标是做一个**完全跑在自己电脑上的数字分身**：

- 用你自己的说话风格、口头禅、思维习惯回消息
- 记得你的喜好、经历、讨厌的事，越聊越懂你
- 能发图片、看懂截图
- 会主动找你聊天，在对话中自我提升
- 所有数据只存在本地 SQLite，零公网、零服务器、零封号风险

技术选型上刻意追求极简：后端纯标准库，前端单文件 HTML，不需要 `pip install` 任何东西，拿到代码就能跑。

---

## 二、整体架构

系统采用三层架构，全部在本机 `127.0.0.1` 上通信：

```
前端 index.html（仿微信 UI）
      │  HTTP（仅本机）
      ▼
后端 backend.py（http.server + sqlite3 + urllib）
      │  ① 判断文字/图片  ② 检索分组记忆  ③ 组装人设+记忆+历史
      │  ④ 情绪检测  ⑤ 调用AI生成回复（退化检测+重试）  ⑥ 后台异步学习
      ▼
双AI模型调度
  纯文字  → Qwen2.5-7B-Instruct（硅基流动，免费）→ 模仿语气回复
  带图片  → GLM-4V-Flash（智谱，免费额度）→ 识图转文字 → 再交给Qwen回复
      ▼
本地 SQLite（mem.db）—— 记忆永不丢失
```

### 2.1 为什么用双模型

两个模型分工明确，各自用免费额度：

| 用途 | 模型 | 服务商 | 特点 |
|---|---|---|---|
| 文字对话主力 | Qwen2.5-7B-Instruct | 硅基流动 | 中文拟人度高，永久免费 |
| 图片识别 | GLM-4V-Flash | 智谱AI | 多模态，每月免费额度 |
| 文生图（头像/趣味图） | CogView-3-Flash | 智谱AI | 同上Key，免费额度 |

纯文字消息直接走 Qwen；带图片的消息先让 GLM-4V 把图片内容描述成文字，再拼进上下文交给 Qwen 用你的语气回复。

---

## 三、核心模块一：零依赖 LLM 调用

整个项目不引入 `openai`、`requests` 等第三方库，用标准库 `urllib` 直接调用 OpenAI 兼容格式的接口。这样做的好处是**零安装成本**，也避免了依赖版本冲突。

```python
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
            # 部分模型不支持 response_format，自动降级重试
            if e.code == 400 and json_mode and attempt == 1:
                break
            raise RuntimeError("HTTP %s: %s" % (e.code, err[:300]))
        except (urllib.error.URLError, OSError) as e:
            if attempt == 2:
                raise RuntimeError("网络错误: %s" % e)
            time.sleep(1.2)

    # 去掉 JSON 模式再试一次
    payload.pop("response_format", None)
    return call_llm(url, api_key, model, messages, max_tokens, temperature, False, timeout)
```

这段代码的几个设计细节：

- **JSON 模式自动降级**：有些模型不支持 `response_format`，遇到 400 时自动去掉该参数重试，不会直接报错
- **网络重试**：`URLError` 时 sleep 1.2 秒再试一次，应对偶发网络抖动
- **统一封装**：上层只需 `qwen(messages)` 一行调用，不用关心 URL、Key、模型名

API Key 通过本地 `config.json` 管理，不硬编码在代码里，运行时可热更新：

```python
def load_config():
    global SILICON_KEY, ZHIPU_KEY
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        SILICON_KEY = str(cfg.get("silicon_key", "") or "").strip()
        ZHIPU_KEY = str(cfg.get("zhipu_key", "") or "").strip()
    except FileNotFoundError:
        SILICON_KEY = ZHIPU_KEY = ""

def _require_key(key, label):
    if not key:
        raise RuntimeError("尚未配置 %s。请在侧栏「API设置」中填写你的 API Key。" % label)
```

---

## 四、核心模块二：分组记忆系统

这是整个项目最有技术含量的部分。如果每次对话都把全部历史塞给 AI，不仅 Token 消耗大，还会让小模型"注意力分散"。项目设计了四大分组 + 按需检索的机制。

### 4.1 四大分组

| 分组 | 存什么 | 加载策略 | 容量 |
|---|---|---|---|
| `person` 人设 | 称呼、说话风格、口头禅、性格 | **每次必带**，按相关性排序 | ≤80条 |
| `preference` 喜好 | 喜欢/讨厌/价值观/习惯 | 仅话题相关才调入（前4条） | ≤200条 |
| `event` 事件 | 具体经历/计划/事实 | 仅相关调入，90天前自动归档 | ≤10万条 |
| `chat_temp` 短期 | 最近12轮对话 | 直接取当前会话记录 | 12轮 |

### 4.2 中文二元组相关性检索

不用向量数据库，用轻量的中文二元组（bigram）做相关性打分，零额外依赖：

```python
def grams(text):
    """中文分词近似：取汉字串/单词的二元组用于相关性打分。"""
    out = set()
    for m in re.findall(r"[\u4e00-\u9fffA-Za-z0-9]+", (text or "").lower()):
        if len(m) == 1:
            out.add(m)
        else:
            out.add(m)
            for i in range(len(m) - 1):
                out.add(m[i:i + 2])  # 如"喜欢"→{"喜","喜欢","欢"}
    return out

def _score(query_grams, content):
    g = grams(content)
    return len(query_grams & g)  # 交集大小 = 相关分
```

检索时，`person` 人设全部按相关性 + tag 优先级排序注入；`preference` 和 `event` 只取分数大于 0 的前 4 条。这样既保证了人设不丢失，又不会把不相关的记忆塞进上下文。

### 4.3 Token 硬限制裁剪

无论记忆多少，最终拼入 AI 请求的总预算锁死在 1400 token，超出时按 **历史 → 关联记忆 → 人设** 的顺序裁剪，保证最核心的人设永远保留：

```python
TOKEN_BUDGET = 1400
sections = [("person", person_lines), ("memory", mem_lines), ("history", hist_lines)]
total = sum(est("\n".join(ls)) for _, ls in sections)
while total > TOKEN_BUDGET:
    idx = max(range(len(sections)), key=lambda i: est("\n".join(sections[i][1])))
    label, lines = sections[idx]
    if label == "history":
        lines.pop(0)    # 丢最旧的历史
    else:
        lines.pop()     # 丢分数最低的记忆
    total = sum(est("\n".join(ls)) for _, ls in sections)
```

### 4.4 去重合并与脏数据拦截

新记忆入库前，同组内用 `difflib.SequenceMatcher` 查重，相似度 ≥0.62 自动合并更新，避免堆积垃圾数据。同时拦截"脏记忆"（外语字符、模板残留、AI 瞎编模式），防止污染回复：

```python
def _is_dirty_memory(content):
    if not content:
        return True
    if re.search(r'[\u0400-\u04FF\u0600-\u06FF]', content):  # 西里尔/阿拉伯字母
        return True
    if '{{' in content or '}}' in content or '{name}' in content:  # 模板残留
        return True
    return False
```

---

## 五、核心模块三：语气复刻与抗退化

Qwen2.5-7B 是 7B 小模型，偶发"复读用户原话"、"AI 客服腔"、"堆 emoji"等退化现象。项目内置了一套启发式检测 + 降温度重试机制。

### 5.1 退化检测

```python
def _is_degenerate(text, user_msg=""):
    # ① AI 套路腔黑名单："能理解你的心情"、"要不要我给你"、"让我猜猜"...
    ai_cliches = ["能理解你的", "要不要我给你", "让我来帮你",
                  "我是你的", "让我猜猜", "你大概", ...]
    for c in ai_cliches:
        if c in text:
            return True
    # ② 复读用户原话开头（连续6字相同）
    # ③ 重复词占比 >45%
    # ④ 标点占比 >45%
    # ⑤ emoji >3个
    # ⑥ 单字占比 >55%
    ...
```

### 5.2 降温度重试

检测到退化时，逐步降低 `temperature` 重试最多 3 次，三次都退化则按情绪给一个简短的人话兜底回复：

```python
def _safe_call_chat(msgs, attempts=3, max_tokens=240, temp0=0.45, fallback="嗯…", user_msg=""):
    for i in range(attempts):
        temp = temp0 if i == 0 else (max(0.2, temp0 - 0.15) if i == 1 else 0.25)
        try:
            out = qwen(msgs, max_tokens=max_tokens, temperature=temp, timeout=90)
            out = clean_reply(out)  # 清洗排版/重复字/拆行
            if not _is_degenerate(out, user_msg=user_msg):
                return out
        except Exception as e:
            last_err = e
    if last_err:
        raise last_err
    return fallback  # 按情绪兜底：开心→"牛！"，难过→"抱抱，咋了"
```

### 5.3 回复清洗

模型输出经过 `clean_reply()` 处理，修复"一句话被拆成多行"、"多余空格"、"粘连重复标点"、"重复字"等排版问题：

```python
def clean_reply(text):
    text = re.sub(r"[\s\u3000]+", " ", text)           # 空白压缩
    text = re.sub(r"\s*([，。！？；：、）】」』])\s*", r"\1", text)  # 标点前不留空格
    text = re.sub(r"([。！？])\1+", r"\1", text)         # 重复标点去重
    text = re.sub(r"(.)\1{2,}", r"\1\1", text)          # 三字以上重复去重
    # 一句话被拆行：行尾不是完整句读时与下一行合并
    ...
    return text[:800]
```

---

## 六、核心模块四：主动学习机制

分身不是被动等你发消息，它会像真人一样**主动找你聊**，并在对话中自我提升。

### 6.1 真人式主动聊天

不是死板的"每20分钟发一条"，而是：

- **随机抖动**：实际间隔 = 基准 × 随机系数（0.55~1.7倍）
- **静默时段**：0~8 点绝不打扰
- **看时机**：你最近 4 分钟内发过言就先不插嘴，等出现空档才找你
- **像人说话**：每条主动消息带「当前时间 + 你最近一句 + 它记住的你的一件小事 + 随机聊天意图」

### 6.2 后台学习线程

每轮对话回复完的同一刻，后台线程用 Qwen 做"记忆提取"，从你说的内容里提炼偏好/事件事实（最多3条），压缩成 20-60 字摘要后去重入库。每 5 轮还会做一次"说话风格深度总结"，提炼口头禅/句式/语气写入人设库。

每 3 次主动聊天后，分身会**自我复盘**聊得怎么样，把改进建议写进人设，越聊越会聊。

---

## 七、前端设计

前端是单个 `index.html` 文件（HTML + CSS + JS），仿微信手机聊天界面：

- 手机式窄屏聊天框，桌面端居中显示为手机样式
- 左侧抽屉式侧栏：会话列表、本体设定、记忆库、API设置
- 聊天气泡（左AI右自己）、日期分隔、图片消息
- 输入框支持 Enter 发送、Shift+Enter 换行、图片上传自动压缩
- 发送后按钮显示"思考中…"，回复完成自动聚焦输入框

头像支持 AI 生成（CogView）+ emoji 渐变兜底，换形象时先立即显示 emoji 头像给用户反馈，精美图片在后台异步生成后自动替换。

---

## 八、数据存储

全部数据存在本地 SQLite（`mem.db`），5 张表：

```sql
CREATE TABLE memories(id, grp, tag, content, created_at, last_used, use_count);
CREATE TABLE sessions(id, title, created_at, updated_at);
CREATE TABLE messages(id, session_id, role, content, image, created_at);
CREATE TABLE gen_images(id, prompt, caption, fname, created_at, sent);
CREATE TABLE meta(key TEXT PRIMARY KEY, value);  -- 头像/网名/设置等
```

HTTP 服务用 `http.server.ThreadingHTTPServer`，仅绑定 `127.0.0.1`，外部无法访问。图片缓存有目录穿越防护（只允许 `fs_` 前缀的文件名）。

---

## 九、技术亮点总结

| 亮点 | 实现方式 |
|---|---|
| **零第三方依赖** | 后端纯标准库（urllib/sqlite3/http.server），前端单文件 HTML |
| **永不卡顿** | 记忆按需检索 + Token 硬限制 1400，数据量增长不影响速度 |
| **越聊越像你** | 五层学习机制：问卷设定 + 每轮事实提取 + 每5轮风格总结 + 主动聊天复盘 + 手动微调 |
| **小模型也稳定** | 退化检测 + 降温度重试3次 + 回复清洗 + 情绪兜底 |
| **隐私安全** | 仅监听 127.0.0.1，数据全本地，API Key 本地配置不硬编码 |
| **主动互动** | 随机间隔 + 静默时段 + 看时机不打断，像真人一样找你聊 |
| **双模型协作** | Qwen 管文字、GLM-4V 管识图、CogView 管生图，各用免费额度 |

---

## 十、可扩展方向

- 语音输入输出（接入免费 ASR/TTS 模型）
- 表情包发送与识别
- 记忆相册（图片内容检索）
- 多分身管理（不同人设切换）
- 打包为桌面 exe（PyInstaller）

---

> 这个项目的核心价值不在于用了多复杂的技术，而在于**用极简的技术栈解决了"AI 不像人"和"数据不在自己手里"两个真实痛点**。7B 小模型 + 精心设计的提示词 + 持续学习机制，完全可以做到一个"越聊越像你"的私人分身。
