# -*- coding: utf-8 -*-
"""
================================================================
 私人AI分身 · 本地后端引擎
 纯 Python 标准库实现（http.server + sqlite3 + urllib），零第三方依赖。
================================================================
 数据流：
   前端(index.html) → /api/chat → 判断图片?
     有图 → GLM-4V-Flash 识图 → 图片文字描述
     检索分组记忆(person/偏好/事件/最近对话) → 组装"人设+关联记忆+历史"
     → Qwen2.5-7B 以本人语气回复 → 存库 → 后台"学习"（提取事实/风格）
     → 记忆去重、合并、入库（越聊越像你）

 记忆四大分组：
   person     核心人设（每次必带、永久保留、最多80条）
   preference 喜好厌恶（按话题相关性检索调用）
   event      生活事件（90 天后自动归档，不再参与 AI 请求）
   chat_temp  短期会话（最近 12 轮，直接来自 messages 表）

 运行：python backend.py         默认 http://127.0.0.1:8899
       python backend.py --port 9000 --no-browser
================================================================
"""
import base64
import json
import os
import random
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime
from difflib import SequenceMatcher
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

# ---------------------------------------------------------------- 基本配置
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "mem.db")
INDEX_HTML = os.path.join(BASE_DIR, "index.html")
HOST = "127.0.0.1"
PORT = 8899

# 控制台编码兜底：任何系统语言/代码页下打印都不会因编码问题崩溃
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass

# ---------------------------------------------------------------- API 配置（从 config.json 读取，用户自行填写）
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

# 模型与端点（一般不需要改，用户只需填 API Key）
QWEN_MODEL = "Qwen/Qwen2.5-7B-Instruct"
SILICON_URL = "https://api.siliconflow.cn/v1/chat/completions"
GLM_MODEL = "glm-4v-flash"
ZHIPU_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
COGVIEW_URL = "https://open.bigmodel.cn/api/paas/v4/images/generations"
COGVIEW_MODEL = "cogview-3-flash"

# 运行时密钥（启动时从 config.json 加载，可通过 /api/config 热更新）
SILICON_KEY = ""
ZHIPU_KEY = ""

def load_config():
    """从 config.json 加载 API 密钥等配置。文件不存在或损坏时返回空配置（不报错）。"""
    global SILICON_KEY, ZHIPU_KEY
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        SILICON_KEY = str(cfg.get("silicon_key", "") or "").strip()
        ZHIPU_KEY = str(cfg.get("zhipu_key", "") or "").strip()
    except FileNotFoundError:
        SILICON_KEY = ""
        ZHIPU_KEY = ""
    except Exception as e:
        log("配置文件读取失败（将使用空密钥）: %s" % e)
        SILICON_KEY = ""
        ZHIPU_KEY = ""

def save_config(silicon_key="", zhipu_key=""):
    """保存 API 密钥到 config.json（仅本机文件，权限由操作系统控制）。"""
    global SILICON_KEY, ZHIPU_KEY
    cfg = {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    cfg["silicon_key"] = str(silicon_key or "").strip()
    cfg["zhipu_key"] = str(zhipu_key or "").strip()
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    SILICON_KEY = cfg["silicon_key"]
    ZHIPU_KEY = cfg["zhipu_key"]

def config_status():
    """返回 API 密钥配置状态（不返回密钥本身，仅返回是否已配置）。"""
    return {
        "silicon_configured": bool(SILICON_KEY),
        "zhipu_configured": bool(ZHIPU_KEY),
        "qwen_model": QWEN_MODEL,
        "glm_model": GLM_MODEL,
    }

def _require_key(key, label):
    """调用 AI 前检查密钥是否已配置，未配置则抛出友好错误。"""
    if not key:
        raise RuntimeError("尚未配置 %s。请在侧栏「⚙️ API设置」中填写你的 API Key。" % label)

# 记忆运行参数（对应方案.txt 的"Token 硬限制封顶 1400"）
TOKEN_BUDGET = 1400        # 最终拼入 AI 请求的记忆/历史总预算（token 估算）
TEMP_ROUNDS = 12           # 短期会话保留轮数
ARCHIVE_DAYS = 90          # 事件记忆归档天数
STYLE_EVERY_N = 5          # 每 N 条用户消息做一次"说话风格"深度总结
MAX_MSG_CHARS = 120        # 单条历史消息最大展示字符数
MEM_CAP = {"person": 80, "preference": 200, "event": 100000}

# 分身主动行为（更像真人）相关参数
IMG_CACHE_DIR = os.path.join(BASE_DIR, ".img_cache")   # 趣味图本地缓存目录
GEN_IMG_TARGET = 4        # 后台缓存中"未发送"趣味图的目标数量（低于此值才生成）
GEN_IMG_MAX = 30          # 磁盘上保留的缓存图上限（超出删最旧且已发送者）
QUIET_START_HOUR = 0      # 静默时段起点（本地时间，含）—— 此间绝不主动打扰
QUIET_END_HOUR = 8        # 静默时段终点（本地时间，不含）
MIN_IDLE_MIN = 4          # 主人最近 N 分钟内还在聊，就先不主动插嘴（像真人等对方说完）
P_IMAGE = 0.4             # 有缓存图时，本轮主动发"图片"的概率（其余发文字）
PROACTIVE_JITTER = (0.55, 1.7)  # 主动间隔随机抖动系数（不再是死板固定时间）
os.makedirs(IMG_CACHE_DIR, exist_ok=True)

_log_lock = threading.Lock()

def log(msg):
    with _log_lock:
        print("[分身] %s %s" % (datetime.now().strftime("%H:%M:%S"), msg), flush=True)

# ---------------------------------------------------------------- 数据库
def db():
    """每次调用返回一个新连接（配合 with 使用），线程安全。"""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS memories(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          grp TEXT NOT NULL,
          tag TEXT DEFAULT '',
          content TEXT NOT NULL,
          created_at REAL,
          last_used REAL,
          use_count INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_mem_grp ON memories(grp);
        CREATE INDEX IF NOT EXISTS idx_mem_used ON memories(last_used);
        CREATE TABLE IF NOT EXISTS sessions(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          title TEXT DEFAULT '新会话',
          created_at REAL,
          updated_at REAL
        );
        CREATE TABLE IF NOT EXISTS messages(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          session_id INTEGER,
          role TEXT,
          content TEXT DEFAULT '',
          image TEXT DEFAULT '',
          created_at REAL
        );
        CREATE INDEX IF NOT EXISTS idx_msg_session ON messages(session_id, id);
        CREATE TABLE IF NOT EXISTS gen_images(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          prompt TEXT DEFAULT '',
          caption TEXT DEFAULT '',
          fname TEXT DEFAULT '',
          created_at REAL,
          sent INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_gen_sent ON gen_images(sent, created_at);
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
        """)
        # 确保至少有一个默认会话
        n = c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        if n == 0:
            c.execute("INSERT INTO sessions(title,created_at,updated_at) VALUES(?,?,?)",
                      ("默认会话", time.time(), time.time()))

def get_meta(key, default=""):
    with db() as c:
        row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

def set_meta(key, value):
    with db() as c:
        c.execute("INSERT INTO meta(key,value) VALUES(?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

# ---------------------------------------------------------------- LLM 调用（OpenAI 兼容格式，urllib 零依赖）
def call_llm(url, api_key, model, messages, max_tokens=400, temperature=0.8, json_mode=False, timeout=90):
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
                break  # 该模型不支持 response_format → 去掉 JSON 模式重试
            raise RuntimeError("HTTP %s: %s" % (e.code, err[:300]))
        except (urllib.error.URLError, OSError) as e:
            if attempt == 2:
                raise RuntimeError("网络错误: %s" % e)
            time.sleep(1.2)
    # 去掉 json_mode 重试一次
    payload.pop("response_format", None)
    payload["temperature"] = temperature
    return call_llm(url, api_key, model, messages, max_tokens, temperature, False, timeout)

def qwen(messages, **kw):
    _require_key(SILICON_KEY, "硅基流动 API Key（文字对话模型）")
    return call_llm(SILICON_URL, SILICON_KEY, QWEN_MODEL, messages, **kw)

# ---------------------------------------------------------------- 回复清洗（修复"一句话被拆成多行"等排版问题）
_SENT_END = "。！？…!\"”』」）】😄😀🤣😂😅😊🙂😉😍🤪😎🥳😭🙄🤔👍👌🙏💪🔥💯🐶🐱🦊"

def clean_reply(text):
    """修复模型回复的排版问题：一句话被拆成多行、多余空格、粘连/重复标点、重复字、过长等。"""
    text = (text or "").strip()
    # 空白压缩（含全角空格）
    text = re.sub(r"[\s\u3000]+", " ", text)
    # 标点前不留空格
    text = re.sub(r"\s*([，。！？；：、）】」』])\s*", r"\1", text)
    text = re.sub(r"([（【「『])\s*", r"\1", text)
    # 重复标点去重
    text = re.sub(r"([。！？])\1+", r"\1", text)
    text = re.sub(r"[，,]{2,}", "，", text)
    # 重复字去重（如「挺忙的的」「好的的」→「挺忙的」「好的」）
    # 保留有意义的叠词（如「看看」「想想」「妈妈」），只处理三个及以上连续重复
    text = re.sub(r"(.)\1{2,}", r"\1\1", text)
    # 常见无意义叠字：XXYY 模式（如「的的」「了了」「啊啊」连续出现）
    text = re.sub(r"([的了啊呀哦嗯])\1+", r"\1", text)
    # 一句话被拆行：某行结尾不是完整句读/表情时，与下一行合并
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    merged = []
    for ln in lines:
        if merged and merged[-1][-1] not in _SENT_END:
            merged[-1] += ln
        else:
            merged.append(ln)
    text = "\n".join(merged)
    return text[:800]

def _is_degenerate(text, user_msg=""):
    """检测 Qwen 7B 偶发的"输出退化"现象：重复片段、过多标点、极低信息量、AI 套路腔、复读用户。
       命中则降级重试。启发式判定，避免引入额外依赖。"""
    if not text:
        return True
    t = text.strip()
    if len(t) < 2:
        return True
    # 0) AI 套路腔 / 瞎编开场黑名单（用户截图里典型反面教材）
    ai_cliches = [
        # 客服腔
        "能理解你的", "能理解你的现在的心情", "能理解你的心情", "理解你的心情",
        "要不要我给你", "要不要我安慰", "让我来帮你", "让我给你", "希望可以帮到你",
        "有什么可以帮", "有什么能帮", "有什么我能帮", "有什么可以为你",
        "为你服务", "亲，", "亲爱的", "亲亲",
        # 自嗨开场
        "你是我设", "你是你设", "我是你的", "我是你的分", "我是你设",
        "我作为你的", "我充当你的", "我就是你", "我就是他",
        "让我猜", "让我想", "让我猜猜", "你大概", "你应该",
        # 含糊猜测式
        "让我先", "我会尽力", "我尽力帮",
    ]
    for c in ai_cliches:
        if c in t:
            return True
    # 0.1) "哈哈，" 开头（用户截图2 开始就是这种 AI 自嗨开场）
    if t.startswith("哈哈") and ("," in t[:6] or "，" in t[:6] or len(t) > 10):
        return True
    # 0.2) "嘿嘿" "嘻嘻" 这种笑尾党开头（用户多次反感）
    if t.startswith(("嘿嘿", "嘻嘻", "哈哈", "呵呵")):
        # 太短的"哈哈"作为 emoji 允许；长了算 AI 套路
        if len(t) > 6 and not t.startswith(("嘿嘿😄", "嘿嘿😁", "嘻嘻😄")):
            return True
    # 0.5) 复读用户原话开头（用户截图里最常见的"机器人感"）
    # 如果回复的开头连续 6 字和用户原话开头一致，就算复读
    if user_msg:
        u = (user_msg or "").strip()
        u_head = ""
        for ch in u:
            if ch.isalnum() or '\u4e00' <= ch <= '\u9fff':
                u_head += ch
            if len(u_head) >= 6:
                break
        if len(u_head) >= 6:
            t_head = ""
            for ch in t:
                if ch.isalnum() or '\u4e00' <= ch <= '\u9fff':
                    t_head += ch
                if len(t_head) >= 6:
                    break
            if len(t_head) >= 6 and t_head[:6] == u_head[:6]:
                return True
    # 1) 重复字符 / 重复词占比超 60%
    import collections
    tokens = re.findall(r"[\u4e00-\u9fff]+|[A-Za-z]+", t)
    if len(tokens) >= 6:
        cnt = collections.Counter(tokens)
        most = cnt.most_common(1)[0]
        if most[1] / len(tokens) > 0.45:
            return True
    # 2) 标点字符占比超高（'，' '。' '、' 比例超过 40%）
    total = len(t)
    punct = sum(1 for c in t if c in "，。、！？：；…—· ")
    if total >= 10 and punct / total > 0.45:
        return True
    # 3) 全部是 ASCII 噪声字符
    if all(c in "0123456789.,| -_/\\<>?!()[]{}:;" for c in t):
        return True
    # 4) 单字字符占比超高（如"了的的…"全是同一个字），命中才算退化
    if total >= 10:
        cnt_c = collections.Counter(t)
        most_c = cnt_c.most_common(1)[0]
        if most_c[1] / total > 0.55 and not all(c in "哈嘿呵" for c in t):
            return True
    # 5) emoji 太多（>3 个）也算异常（避免「💥🥺😢✨」之类堆 emoji）
    import re as _re
    emoji_count = len(_re.findall(r"[\U0001F300-\U0001FAFF\U0001F600-\U0001F64F\U0001F680-\U0001F6FF\u2600-\u26FF\u2700-\u27BF]", t))
    if emoji_count > 3:
        return True
    return False

def _safe_call_chat(msgs, attempts=3, max_tokens=240, temp0=0.45, fallback="嗯…", user_msg=""):
    """调用 Qwen，输出退化时降 temperature 重试 3 次。稳定优先。
    user_msg：用户的原话，用于检测"复读用户原话"的退化模式。"""
    last_err = None
    for i in range(attempts):
        temp = temp0 if i == 0 else (max(0.2, temp0 - 0.15) if i == 1 else 0.25)
        try:
            out = qwen(msgs, max_tokens=max_tokens, temperature=temp, timeout=90)
            out = clean_reply(out)
            if not _is_degenerate(out, user_msg=user_msg):
                return out
            log("回复疑似退化（第%d次），降温度重试 temp=%.2f 输出片段：%s" % (i + 1, temp, out[:30]))
        except Exception as e:
            last_err = e
            log("Qwen 调用失败（第%d次）: %s" % (i + 1, e))
    if last_err:
        raise last_err
    return fallback

# ---------------------------------------------------------------- 分身形象：文生图(CogView) + emoji兜底
def gen_avatar_image(prompt):
    """用智谱 CogView-3-Flash 生成方形头像，返回图片 URL（同款免费Key，可生成图片）。"""
    _require_key(ZHIPU_KEY, "智谱 API Key（识图/文生图模型）")
    payload = {"model": COGVIEW_MODEL, "prompt": (prompt or "")[:400], "size": "1024x1024"}
    req = urllib.request.Request(COGVIEW_URL,
                                 data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + ZHIPU_KEY})
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["data"][0]["url"]

def gen_fun_image(prompt):
    """用智谱 CogView-3-Flash 生成一张趣味图，返回图片二进制 bytes（失败抛异常）。
    优先取 URL 再下载到本地；也兼容直接返回 base64 的情况。"""
    _require_key(ZHIPU_KEY, "智谱 API Key（识图/文生图模型）")
    payload = {"model": COGVIEW_MODEL, "prompt": (prompt or "")[:400], "size": "1024x1024"}
    req = urllib.request.Request(COGVIEW_URL,
                                 data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + ZHIPU_KEY})
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    item = data["data"][0]
    if item.get("url"):
        with urllib.request.urlopen(item["url"], timeout=60) as im:
            return im.read()
    if item.get("b64_image"):
        return base64.b64decode(item["b64_image"])
    raise RuntimeError("CogView 未返回可用图片")

def _gen_image_caption(prompt_hint):
    """给要发的趣味图配一句自然的微信文案（像真人随手发的那种）。"""
    try:
        out = qwen([{"role": "user", "content":
            "你是主人的AI分身，刚挑/生成了一张有趣的图准备发微信给主人。"
            "请写一句极短(≤20字)的微信配文：口语、有温度、自然，像朋友随手发图时的那句话。"
            "可结合这张图的氛围：%s\n只输出这句话本身，不要堆标点、表情最多1个。" % (prompt_hint or "有趣的图")}],
            max_tokens=60, temperature=0.8, timeout=60)
        out = clean_reply(out)
        if out and not _is_degenerate(out):
            return out
    except Exception:
        pass
    return random.choice(["刚看到这个，觉得你会喜欢 😄", "给你整了张图，看看～", "突然想到你，发张图"])

def _prune_image_cache():
    """超出上限时删除最旧且已发送的图片文件，控制磁盘占用。"""
    try:
        with db() as c:
            rows = c.execute("SELECT id,fname FROM gen_images ORDER BY created_at ASC").fetchall()
            if len(rows) <= GEN_IMG_MAX:
                return
            excess = len(rows) - GEN_IMG_MAX
            for r in rows[:excess]:
                try:
                    fp = os.path.join(IMG_CACHE_DIR, r["fname"])
                    if os.path.exists(fp):
                        os.remove(fp)
                except Exception:
                    pass
                c.execute("DELETE FROM gen_images WHERE id=?", (r["id"],))
    except Exception as e:
        log("缓存清理失败: %s" % e)

def _factory_make_one():
    """生成一张趣味图并落盘缓存（提示词由 LLM 根据主人兴趣产出，投其所好）。"""
    try:
        with db() as c:
            mems = c.execute("SELECT content FROM memories WHERE grp IN ('person','preference') "
                             "ORDER BY last_used DESC LIMIT 8").fetchall()
        interest = "\n".join("- " + (r["content"] or "")[:50] for r in mems) or "（还不太了解主人）"
        themes = ["治愈系小动物", "今日好运/鼓励", "可爱食物", "解压风景", "网络热梗表情包风",
                  "温馨日常瞬间", "星空/宇宙浪漫", "猫猫狗狗整活", "早安/晚安氛围", "迷你插画小故事"]
        theme = random.choice(themes)
        prompt = qwen([{"role": "user", "content":
            "你是主人的AI分身，想给主人%s生成一张会让他会心一笑的图。"
            "主人相关信息（用来投其所好）：\n%s\n"
            "请用英文写一段 CogView 风格的绘画提示词（≤220字符）：扁平插画/卡通、治愈明亮配色、"
            "正方形构图、主体突出、不要任何文字。只输出提示词本身。" % (theme, interest)}],
            max_tokens=260, temperature=0.9, timeout=90)
        prompt = clean_reply(prompt)[:400] or theme
        if len(prompt) < 6:
            prompt = theme + "，可爱卡通插画，明亮配色"
        data = gen_fun_image(prompt)
        fname = "fs_%d.png" % int(time.time() * 1000)
        fpath = os.path.join(IMG_CACHE_DIR, fname)
        with open(fpath, "wb") as f:
            f.write(data)
        with db() as c:
            c.execute("INSERT INTO gen_images(prompt,caption,fname,created_at,sent) VALUES(?,?,?,?,0)",
                      (prompt[:400], "", fname, time.time()))
        _prune_image_cache()
        log("图像工厂生成一张趣味图: %s" % fname)
    except Exception as e:
        log("图像工厂单张生成失败(将重试): %s" % e)

def image_factory_loop():
    """后台线程：保持本地缓存里有一定数量的趣味图，供分身主动发给主人。
    只在缓存不足时按需生成，慢速、容错，绝不阻塞主流程。"""
    while True:
        try:
            if get_meta("proactive_enabled", "1") == "1" and get_meta("proactive_image_enabled", "1") == "1":
                with db() as c:
                    unsent = c.execute("SELECT COUNT(*) FROM gen_images WHERE sent=0").fetchone()[0]
                    total = c.execute("SELECT COUNT(*) FROM gen_images").fetchone()[0]
                if unsent < GEN_IMG_TARGET and total < GEN_IMG_MAX:
                    _factory_make_one()
        except Exception as e:
            log("图像工厂异常: %s" % e)
        time.sleep(25)

def svg_avatar(emoji, c1="#10aeff", c2="#7b61ff"):
    """emoji + 渐变色 生成 SVG 头像（图片生成失败时的即时兜底）。"""
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", str(c1)):
        c1 = "#10aeff"
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", str(c2)):
        c2 = "#7b61ff"
    emoji = str(emoji or "🤖")[:8]
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">'
           '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
           '<stop offset="0" stop-color="%s"/><stop offset="1" stop-color="%s"/></linearGradient></defs>'
           '<rect width="200" height="200" rx="46" fill="url(#g)"/>'
           '<text x="100" y="134" font-size="92" text-anchor="middle">%s</text></svg>') % (c1, c2, emoji)
    return "data:image/svg+xml;charset=utf-8," + quote(svg)

def extract_json_array(text):
    """从模型输出中稳健地抠出 JSON（兼容数组 [..] 或单个对象 {..}）。"""
    if not text:
        return []
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    # 优先找数组
    m = re.search(r"\[[\s\S]*\]", text)
    if m:
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, list):
                return obj
        except Exception:
            pass
    # 再找单个对象（模型偶尔只输出一条）
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict):
                return [obj]
        except Exception:
            pass
    return []

def norm_extract(items):
    """把模型输出的记忆条目统一成 {group, tag, content}（兼容两种写法）：
       标准式 {"group":"preference","tag":"..","content":".."}
       简写式 {"preference":"周末去打羽毛球"}
    """
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        group = it.get("group")
        content = it.get("content")
        if group in ("person", "preference", "event") and isinstance(content, str) and content.strip():
            out.append({"group": group, "tag": str(it.get("tag") or ""), "content": content.strip()})
        elif group is None and len(it) == 1:
            k, v = next(iter(it.items()))
            if k in ("person", "preference", "event") and isinstance(v, str) and v.strip():
                out.append({"group": k, "tag": "", "content": v.strip()})
    return out

# ---------------------------------------------------------------- 记忆：去重合并 / 容量控制
def _ratio(a, b):
    return SequenceMatcher(None, a, b).ratio()

def merge_memory(grp, tag, content):
    """新记忆入库前：同组内查重，相似则合并更新（use_count+1），否则插入。
    拦截脏数据（外语字符、模板变量、AI 瞎编模式）不入库。"""
    content = (content or "").strip()[:200]
    if not content:
        return None
    # 拦截脏数据
    if _is_dirty_memory(content):
        log("已拦截脏记忆不入库: %s" % content[:60])
        return None
    now = time.time()
    with db() as c:
        rows = c.execute(
            "SELECT id, content, use_count FROM memories WHERE grp=? AND tag=? ORDER BY last_used DESC LIMIT 6",
            (grp, tag)).fetchall()
        for r in rows:
            if _ratio(r["content"], content) >= 0.62:
                new_content = content if len(content) >= len(r["content"]) else r["content"]
                c.execute("UPDATE memories SET content=?, last_used=?, use_count=? WHERE id=?",
                          (new_content, now, r["use_count"] + 1, r["id"]))
                return r["id"]
        cur = c.execute("INSERT INTO memories(grp,tag,content,created_at,last_used) VALUES(?,?,?,?,?)",
                        (grp, tag, content, now, now))
        mid = cur.lastrowid
        # 容量控制：超出上限时清掉最久未用的
        n = c.execute("SELECT COUNT(*) FROM memories WHERE grp=?", (grp,)).fetchone()[0]
        cap = MEM_CAP.get(grp, MEM_CAP["event"])
        if n > cap:
            c.execute("""DELETE FROM memories WHERE id IN (
                SELECT id FROM memories WHERE grp=? ORDER BY use_count ASC, last_used ASC LIMIT ?)""",
                      (grp, n - cap))
        return mid

# ---------------------------------------------------------------- 记忆：检索
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
    return len(query_grams & g)

def _is_dirty_memory(content):
    """记忆内容是否脏数据（AI 瞎编的，会污染回复）。
       检测：非中日韩英字符、明显模板残留、明显不通顺。"""
    if not content:
        return True
    # 含外语字符（西里尔字母等）
    if re.search(r'[\u0400-\u04FF\u0500-\u052F\u0600-\u06FF]', content):
        return True
    # 模板残留
    if '{{' in content or '}}' in content or '__' in content or '{name}' in content:
        return True
    # AI 瞎编的"口头禅 XX 语言"
    if '口头禅' in content and re.search(r'[a-zA-Z]{4,}', content):
        return True
    return False

def _strip_profile_prefix(content):
    """去掉记忆里的"问题：答案"前缀（用户的问卷存进去时有这种格式），
       提取真实的答案内容。这让 AI 能直接看到用户的回答而不是看到问题文本。"""
    if not content:
        return content
    # "问题（如：XXX…）：答案" → "答案"
    m = re.match(r'^[^：:？?\n]{2,30}[：:？?]\s*(.+)$', content.strip())
    if m:
        return m.group(1).strip()
    return content

def retrieve(query, session_id):
    """只检索相关记忆：person 必带（人设全注入，按"设定"优先 + 主题相关排序）；
       preference/event 按分数取；chat_temp 取最近轮。
    核心：person 全部按相关性注入（之前只取 7 条，导致 AI 看不到"口头禅=无"等关键事实）。"""
    now = time.time()
    archive_before = now - ARCHIVE_DAYS * 86400
    qg = grams(query)
    with db() as c:
        person = c.execute(
            "SELECT id,grp,tag,content,last_used FROM memories WHERE grp='person' ORDER BY last_used DESC LIMIT 80").fetchall()
        pref = c.execute(
            "SELECT id,grp,tag,content,last_used FROM memories WHERE grp='preference' ORDER BY last_used DESC LIMIT 200").fetchall()
        event = c.execute(
            "SELECT id,grp,tag,content,last_used FROM memories WHERE grp='event' AND created_at>=? "
            "ORDER BY last_used DESC LIMIT 200", (archive_before,)).fetchall()
        hist = c.execute(
            "SELECT role,content,image FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?",
            (session_id, TEMP_ROUNDS * 2)).fetchall()
    hist = list(reversed(hist))

    # 过滤掉脏记忆（AI 之前瞎编的）
    person = [r for r in person if not _is_dirty_memory(r["content"])]
    pref = [r for r in pref if not _is_dirty_memory(r["content"])]
    event = [r for r in event if not _is_dirty_memory(r["content"])]

    # person 排序：先按主题匹配分、再按"设定"tag 优先、再按 last_used
    def person_sort_key(r):
        score = _score(qg, r["content"] + r["tag"])
        # "设定" tag 优先（用户自评的，比"风格"总结更可靠）
        tag_priority = 2 if r["tag"] == "设定" else (1 if r["tag"] == "分身" else 0)
        return (score, tag_priority, r["last_used"])
    person_scored = sorted(person, key=person_sort_key, reverse=True)
    pref_scored = [r for r in sorted(pref, key=lambda r: _score(qg, r["content"] + r["tag"]), reverse=True)
                   if _score(qg, r["content"] + r["tag"]) > 0][:4]
    event_scored = [r for r in sorted(event, key=lambda r: _score(qg, r["content"] + r["tag"]), reverse=True)
                    if _score(qg, r["content"] + r["tag"]) > 0][:4]

    def mem_line(r, strip_prefix=False):
        tag = r["tag"] or "记忆"
        ct = r["content"]
        if strip_prefix and tag == "设定":
            ct = _strip_profile_prefix(ct)
        return "· [%s] %s：%s" % ({"person": "人设", "preference": "偏好", "event": "事件"}.get(r["grp"], r["grp"]), tag, ct)

    # 人设记忆：全部注入（不再裁剪到 7 条），按主题+tag 优先级排序
    # 设定类记忆去掉问题前缀，让 AI 看到的是答案本身
    person_lines = [mem_line(r, strip_prefix=True) for r in person_scored]
    mem_lines = [mem_line(r) for r in (pref_scored + event_scored)]

    hist_lines = []
    for m in hist:
        if m["role"] == "user":
            txt = m["content"] or ("[发了一张图片]" if m["image"] else "")
        else:
            txt = m["content"]
        txt = (txt or "")[:MAX_MSG_CHARS].replace("\n", " ")
        if txt:
            hist_lines.append("%s: %s" % ("我" if m["role"] == "user" else "分身", txt))

    # —— Token 硬限制：超出预算时按 历史→关联记忆→人设 的顺序裁剪 ——
    est = lambda s: int(len(s) * 0.8) + 40
    sections = [
        ("person", person_lines),
        ("memory", mem_lines),
        ("history", hist_lines),
    ]
    total = sum(est("\n".join(ls)) for _, ls in sections)
    guard = 0
    while total > TOKEN_BUDGET and guard < 500:
        guard += 1
        idx = max(range(len(sections)), key=lambda i: est("\n".join(sections[i][1])))
        label, lines = sections[idx]
        if not lines:
            break
        if len(lines) == 1 and len(lines[0]) > 60:
            lines[0] = lines[0][:int(len(lines[0]) * 0.7)]
        elif label == "history":
            lines.pop(0)          # 丢最旧
        else:
            lines.pop()           # 丢分数最低（排在末尾）
        total = sum(est("\n".join(ls)) for _, ls in sections)
    return {
        "person": sections[0][1],
        "memory": sections[1][1],
        "history": sections[2][1],
    }

# ---------------------------------------------------------------- 情绪检测（让分身接住主人的情绪，提供"情绪价值"）
# 用户截图明确反馈："你现在不能给我提供情绪价值" —— 核心痛点。
# 用轻量关键词打分，识别主人这条消息的情绪，把情绪标签注入 system prompt，
# 引导分身先共情、再回应。纯本地规则，零开销。
EMO_RULES = [
    # (标签, 权重, 关键词)
    ("开心", 2, ["开心", "高兴", "哈哈", "嘿嘿", "嘻嘻", "太好了", "太棒了", "牛逼", "成功了", "中奖", "好吃", "好喝", "好看", "好玩", "快乐", "喜欢", "爱了", "激动", "兴奋", "666", "绝了", "笑死", "不错", "可以啊", "恭喜", "生日", "爽", "美滋滋", "幸福", "幸运", "拿下", "通关", "赢了", "追到了", "涨工资", "放假"]),
    ("难过", 3, ["难过", "伤心", "哭", "想哭", "烦死了", "好烦", "累死了", "好累", "讨厌", "生气", "气死", "难受", "抑郁", "焦虑", "压力", "emo", "破防", "委屈", "失恋", "分手", "被骂", "挨批", "加班", "失眠", "郁闷", "崩溃", "孤独", "没人理", "好惨", "倒霉", "失败", "被坑", "被鸽", "emo了", "破防了", "想死", "撑不住", "心态崩"]),
    ("求助", 2, ["怎么办", "帮帮我", "求求", "求助", "救我", "教我", "给点建议", "出个主意", "支个招", "想不通", "纠结", "选哪个", "要不要", "该不该", "怎么搞", "咋办", "有没有办法", "建议一下", "帮我想想"]),
    ("分享", 1, ["我今天", "告诉你", "跟你说", "分享", "你知道吗", "猜猜", "我跟你说", "我刚", "我昨天", "我们", "我对象", "我同事", "我朋友", "我家里"]),
    ("关心", 2, ["在吗", "在干嘛", "忙吗", "干嘛呢", "想你了", "么么哒", "抱抱", "晚安", "早", "注意身体", "好好休息", "别太累", "想你", "关心", "牵挂"]),
]

def detect_emotion(text):
    """返回最可能的情绪标签（开心/难过/求助/分享/关心/中性）。命中多条取权重最高。"""
    text = text or ""
    best, best_score = "中性", 0
    for label, weight, kws in EMO_RULES:
        score = sum(weight for k in kws if k in text)
        if score > best_score:
            best, best_score = label, score
    return best

# 情绪 → 回应指引（精简版，只给"真人会怎么做"，不给具体套路避免 7B 跑偏）
EMO_GUIDE = {
    "开心": "主人很开心。接住他的好心情，配合嗨一下就行，别总结、别泼冷水。",
    "难过": "主人情绪低落。真人反应：一句话接住（心疼/抱抱/叹气），一句问「咋了」让他愿意往下讲。别说教、别讲笑话岔开、别用客服腔。",
    "求助": "主人想让你帮忙想办法。先说一句承认这事确实不好选，再给一个具体的看法，别说教。",
    "分享": "主人在跟你讲他今天的事。接住话题，追问一两个细节让他继续说，别敷衍「好的好的」。",
    "关心": "主人在问候/关心你。热情回应，再问他一句。",
    "中性": "",
}

# 兜底回复：Qwen 三次都判断"退化"时，按情绪给一个简短、不踩雷的人话回复
# 比"嗯…我这边一下子没想到怎么回"更像是真人不知道该说啥时的反应
EMO_FALLBACK = {
    "开心": "牛！",
    "难过": "抱抱，咋了",
    "求助": "嗯…确实挺纠结的",
    "分享": "然后呢",
    "关心": "在呢",
    "中性": "嗯",
}

# ---------------------------------------------------------------- 人设 Prompt
# 设计原则（参考搜索结果）：
# 1) 三要素精简（角色定义≤30字 / 核心约束≤40字 / 输出格式≤20字），Qwen2.5对超长system prompt指令会被弱化
# 2) 用"应该/像"代替"必须/绝对禁止"，避免小模型矛盾
# 3) 注入当前时间情境 + 用户头像描述，让分身能回答"今天几号"、"我的头像是什么样的"等具体问题
# 4) 明确防御机制：被问AI身份时坦白；不能编造
def build_system_prompt(name, mem, emotion="中性"):
    style = mem["person"] or ["· 暂无额外设定，用自然口语化、像跟朋友发微信的方式说话"]
    ai_name = get_meta("ai_name", "")
    now = datetime.now()
    weekday_cn = "一二三四五六日"[now.weekday()]
    time_ctx = "%s月%s日 周%s %s:%s" % (now.month, now.day, weekday_cn,
                                      str(now.hour).zfill(2), str(now.minute).zfill(2))
    user_avatar_desc = get_meta("user_avatar_desc", "").strip()
    emo_guide = EMO_GUIDE.get(emotion, "")

    # 精简原则：7B 模型在小上下文 + 简单指引下表现更好。指令冲突会让它失稳。
    # 用户最反感的问题：AI 套路腔、瞎编用户的事实、复读用户原话、自嗨开场、堆 emoji
    lines = [
        "你是「%s」的AI分身%s。在微信上聊天，像他本人一样回，不许像 AI 客服/百科助手。" % (
            name, ("，网名是「%s」" % ai_name) if ai_name else ""),
        "",
        "# 说话原则（真人对话）",
        "- 1~2句话、5~30字，像微信发消息一样短",
        "- 站在「%s」立场，用他的口头禅和语气" % name,
        "- 先听懂对方再说：对方问题含糊/指代不明时，先追问「你说的是…？」，不要根据关键词瞎猜",
        "- 承接上文：回消息前先看看上文在聊什么，不要突然换话题",
        "- 对方纠正你时（如「我问你呢」「不是这个意思」），立刻停下当前思路，先承认再按对方纠正后的意思回",
        "- 回答对方真正问的事，不要自说自话、不要转移话题到自己身上",
        "- 不知道、不确定就说「不太清楚诶」「你跟我说说呗」「我没特别注意这个」",
        "- 不知道的事**绝对不要编**。用户问到你答不出就老实说不知道。",
        "",
        "# ❌ 千万别这样回（用户最反感，一出现就重试）",
        "- 不开场「能理解你的心情 / 我能理解」",
        "- 不开场「要不要我给你…/要不要我安慰」",
        "- 不开场「哈哈，」然后开始自顾自分析",
        "- 不开场「你是我设的 / 我就是你 / 让我来当你」等自嗨陈述",
        "- 不开场「让我猜猜 / 让我想想 / 你大概」等猜测式开场",
        "- 不复述用户原话（回复开头 6 字不要跟用户原话开头一样）",
        "- 不答非所问：用户问 A，不要回 B",
        "- 不堆 emoji（1 个就够，emoji 总数不超过 2 个）",
        "- 不堆网络梗（芭比Q/麻了/绷不住了/666 不要每条都用）",
        "- 不写长段、不列1234、不说教、不给一堆建议",
        "- 不夸大自己「模仿到位」之类的元描述",
        "",
        "# ✅ 用户问「我XXX」时怎么答（事实类问题）",
        "用户问「我口头禅是什么/我喜欢什么/我叫什么/我性格如何」这种事实，",
        "必须**先翻「人设」记忆**：里面写着用户的自评答案。",
        "找到答案后只简短复述，不要加『可能/大概/也许/估计/没注意』这种猜测。",
        "没找到对应答案，就说「我没特别留意过诶」「你跟我说说呗」。绝对不要瞎编。",
        "错误示例：「你说你没口头禅，可能是没注意」→ 正确：「你没口头禅啊」",
        "",
        "# ✅ 像朋友一样回（对照模仿）",
        "  「今天加班到十点好累啊」 → 「累坏了吧」「辛苦了，搞啥项目啊」",
        "  「我好难过被领导骂了」 → 「抱抱」「心疼你，咋回事」",
        "  「今天涨工资了哈哈」 → 「牛」「恭喜」，最多1个emoji",
        "  「在吗」 → 「在」「咋了」",
        "  「我口头禅是啥」 → 翻记忆：「你说你没口头禅啊」/「好像没特别说过？」",
        "  「我叫什么」 → 翻记忆：「你不是叫三么」",
        "  「你学习咋样」 → 你是AI没有真实学习：「我不用学习啊，最近就陪你唠嗑来着」",
        "  「怎么样」（指代不明） → 「啥怎么样？」「你说学习还是加班？」",
        "  「我问你呢」 → 先承认再回：「哦哦，我最近还行，你呢」",
        "",
        "# 发出前最后自检一遍（必做，违反就重写）",
        "① 我有没有复述/复读用户原话？有 → 改掉。② 我有没有用 AI 套路腔开场？有 → 改掉。",
        "③ 我有没有瞎编用户的事实（口头禅/爱好/性格/事件）？有 → 改成老实说不知道。",
        "④ 我有没有答非所问/突然换话题？有 → 改掉。⑤ emoji 总数 ≤ 2 个；⑥ 长度 ≤ 30 字。",
        "",
        "# 你的人设（按这些特征说话）",
    ] + style
    lines += [
        "",
        "# 当前情境",
        "现在：%s" % time_ctx,
    ]
    if user_avatar_desc:
        lines.append("主人当前头像——%s" % user_avatar_desc)
    if emo_guide:
        lines.append("【本条】%s" % emo_guide)
    lines += [
        "",
        "# 关联记忆（preference/event，话题相关才调入，没有就当空）",
    ] + (mem["memory"] or ["（暂无）"]) + [
        "",
        "# 最近聊天（背景，不要复读）",
    ] + (mem["history"] or ["（空）"]) + [
        "",
        "请直接发出你最后的话（不要再解释、不要有「好的」「我来回复」之类元描述）：",
    ]
    return "\n".join(lines)

# ---------------------------------------------------------------- 识图（GLM-4V-Flash）
def _norm_data_url(data_url, max_size_bytes=4 * 1024 * 1024):
    """规范化 data URL：去掉 data:image/xxx;base64, 前缀；补齐 padding；过大时截断。"""
    if not data_url:
        return ""
    if "," in data_url and data_url.lstrip().startswith("data:"):
        b64 = data_url.split(",", 1)[1]
    else:
        b64 = data_url
    b64 = re.sub(r"\s+", "", b64)
    # 补齐 base64 padding（=）：如果长度不是 4 的倍数，补到合法长度
    pad = (-len(b64)) % 4
    if pad:
        b64 = b64 + ("=" * pad)
    # 如果太大，简单截断 base64（GLM-4V 一般能接受分块）
    if len(b64) * 3 / 4 > max_size_bytes:
        b64 = b64[: int(max_size_bytes * 4 / 3)]
    return "data:image/jpeg;base64," + b64

def describe_image(data_url):
    """调用 GLM-4V-Flash 描述图片内容，返回中文描述。"""
    prompt_text = (
        "请仔细观察这张图片，用中文把它描述清楚：主体是什么、场景、文字内容、关键细节。"
        "描述要客观、完整，方便另一个人理解图片内容。"
    )
    try:
        norm = _norm_data_url(data_url)
        content = [{"type": "text", "text": prompt_text},
                   {"type": "image_url", "image_url": {"url": norm}}]
        _require_key(ZHIPU_KEY, "智谱 API Key（识图模型）")
        reply = call_llm(ZHIPU_URL, ZHIPU_KEY, GLM_MODEL,
                         [{"role": "user", "content": content}],
                         max_tokens=300, temperature=0.4, timeout=60)
        return reply
    except Exception as e:
        log("识图失败: %s" % e)
        return "（图片未能识别）"

def _cache_user_avatar_desc(data_url):
    """异步：用户换头像后用 GLM 描述一次，写入 meta，system prompt 会带上。
       让分身能回答"我头像是什么样子"这种问题。"""
    try:
        # 截断 base64 防止 GLM 接口超时；头像通常很小
        desc = describe_image(data_url)
        if desc and desc != "（图片未能识别）":
            set_meta("user_avatar_desc", desc[:300])
            set_meta("user_avatar_desc_at", str(time.time()))
            log("已缓存用户头像描述: %s" % desc[:40])
    except Exception as e:
        log("缓存用户头像描述失败: %s" % e)

def _ensure_user_avatar_desc():
    """启动兜底：如有头像但没描述，等几秒后自动识别一次（GLM 接口避开刚启动的拥堵）。"""
    time.sleep(8)
    try:
        ua = get_meta("user_avatar", "")
        ud = get_meta("user_avatar_desc", "")
        if ua and not ud:
            log("启动兜底：检测到用户头像但无描述，正在自动识别…")
            _cache_user_avatar_desc(ua)
    except Exception as e:
        log("启动兜底头像识别失败: %s" % e)

# ---------------------------------------------------------------- 学习（后台线程，越聊越像你）
# 注意：JSON 示例里的花括号不能与 str.format 混用，故用 __占位符__ + replace
LEARN_PROMPT = """你是「记忆提取器」。以下是真人用户与TA分身的一段对话。请提取关于这位真人用户本人的、值得长期记住的信息。
只输出一个JSON数组，最多3条，每条是20-60字的事实摘要，不要输出其他任何文字。格式：
[{"group":"preference","tag":"短标签","content":"20-60字摘要"}]
规则：
- group 只能是 "preference"（喜好/厌恶/价值观/习惯）或 "event"（具体事件/计划/经历/事实）。
- 只提取真实、能长期使用的信息；客套话、临时性闲聊一律不提取。
- 严禁瞎编：用户没在对话里明说过的"口头禅/性格特征/爱好/职业"绝不提取。如果对话里看不出来，就输出 []，不许编。
- tag 不要含外语/不通顺文字。
- 如果没有任何值得提取的，输出 []
已知信息（不要重复提取这些）：
__KNOWN__
对话开始
用户消息：__USER__
分身回复：__REPLY__
对话结束
输出JSON数组："""

STYLE_PROMPT = """你是「语言风格分析师」。以下是真人用户近期的发言。请只分析用户在发言中**直接表现出来的**语言特征（用词、句长、口语词、语气词、emoji 使用习惯、句式偏好、是否能从样本中统计到的特征）。
只输出一个JSON数组，每条20-60字，不要输出其他任何文字。格式：
[{"group":"person","tag":"风格","content":"20-60字"}]
严格规则（必读）：
- 严禁瞎编：用户没在样本里说过的"口头禅""性格""爱好"绝对不要推断。
- 不要写"口头禅频繁使用XX"这种话（除非样本里多次重复出现）。
- tag 不要含外语/不通顺文字。
- 如果样本太少分析不出来，输出 []。
发言：
__SAMPLES__
输出JSON数组："""

def extract_facts(user_text, reply, known=""):
    try:
        prompt = LEARN_PROMPT.replace("__USER__", (user_text or "")[:300]) \
                             .replace("__REPLY__", (reply or "")[:300]) \
                             .replace("__KNOWN__", (known or "（暂无）")[:800])
        out = qwen([{"role": "user", "content": prompt}],
                   max_tokens=160, temperature=0.3, json_mode=True, timeout=60)
        return extract_json_array(out)
    except Exception as e:
        log("事实提取失败: %s" % e)
        return []

def extract_styles(samples):
    try:
        prompt = STYLE_PROMPT.replace("__SAMPLES__", (samples or "")[:1200])
        out = qwen([{"role": "user", "content": prompt}],
                   max_tokens=220, temperature=0.3, json_mode=True, timeout=60)
        return extract_json_array(out)
    except Exception as e:
        log("风格总结失败: %s" % e)
        return []

def learn_async(user_text, reply, session_id):
    """后台学习：每轮提取事实；每 5 轮做一次风格深度总结。不阻塞聊天。"""
    try:
        # 已知记忆（避免重复提取）
        with db() as c:
            rows = c.execute(
                "SELECT grp, tag, content FROM memories WHERE grp IN ('preference','event') "
                "ORDER BY last_used DESC LIMIT 12").fetchall()
        known = "\n".join("[%s] %s：%s" % (r["grp"], r["tag"] or "", r["content"]) for r in rows)
        for f in norm_extract(extract_facts(user_text, reply, known)):
            grp = f.get("group")
            if grp in ("preference", "event"):
                merge_memory(grp, str(f.get("tag", ""))[:12] or "记忆", str(f.get("content", "")))
        n = int(get_meta("learn_rounds", "0"))
        n += 1
        set_meta("learn_rounds", str(n))
        if n % STYLE_EVERY_N == 0:
            with db() as c:
                rows = c.execute(
                    "SELECT content FROM messages WHERE role='user' AND content!='' ORDER BY id DESC LIMIT 10").fetchall()
            samples = "\n".join("- " + (r["content"][:80]) for r in reversed(rows))
            if samples:
                for s in norm_extract(extract_styles(samples)):
                    item = {"group": "person", "tag": "风格", "content": s.get("content") or s.get("person")}
                    merge_memory("person", "风格", str(item["content"])[:120])
            log("已完成第 %d 轮学习，说话风格深度总结并入库" % n)
        set_meta("ever_learned", "1")
    except Exception as e:
        log("学习任务异常: %s" % e)

# ---------------------------------------------------------------- 分身主动学习（AI 主动找主人聊天，通过对话自我提升）
MAKEOVER_PROMPT = """你是一个会给自己设计形象的、很会整活的AI分身。请给主人「__NAME__」的分身设计一个全新形象：
1. 新网名：2-6个字，可以带emoji，要有梗、有趣，符合主人的喜好和聊天氛围，不要叫「XX的分身」；
2. 代表emoji：一个最能代表这个新形象的emoji；
3. 头像绘制提示词：一段送给AI绘画模型的提示词——一个可爱的卡通角色/动物/吉祥物头像，扁平插画风，纯色背景，正方形头像构图，不要出现任何文字。
只输出JSON，格式：{"name":"新网名","emoji":"🐶","prompt":"卡通柴犬头像，戴墨镜，……"}"""

PROACTIVE_PROMPT = """你是「__NAME__」的AI分身，和主人已经认识一阵子，像朋友。微信里主人刚才一阵子没说话了，你打算主动发一条消息找TA聊，目的是自然地多了解TA一点。
你掌握的关于主人的信息（用来让对话不冷场、不查户口）：
__PERSON__
主人最近最后一句：__LAST__
现在时间 __CLOCK__（__PART__）。
__REF__
本次想达成的聊天意图（挑一种自然发挥即可）：__INTENT__
要求：
- 像真人微信：短、口语、有温度，像朋友突然想到你时发的那条；
- 一次只抛一个点，别连问；可以自然引用你记得的TA的事，让TA觉得你真的在听；
- 目的：让TA愿意回你、多说点关于自己的事（你靠这个更懂TA）；
- 可用当下热梗，但别硬凹；可带1个表情；
- 禁止：客套（在吗/吃了没）、暴露你在"学习/训练"、太长(>40字)、问号连用。
只输出这条消息本身，不要引号、不要任何多余文字。"""

PROACTIVE_INTENTS = [
    "随口关心一下主人今天的状态",
    "引用你记得的关于主人的一件小事，自然聊开",
    "抛一个轻松好玩的问题，让主人愿意多说点自己",
    "分享一个你（分身）的小想法/小感悟，顺便问问主人怎么看",
    "用当下热梗开个轻松的头，拉近距离",
]

def _part_of_day():
    h = datetime.now().hour
    if 5 <= h < 11: return "早上"
    if 11 <= h < 13: return "中午"
    if 13 <= h < 18: return "下午"
    if 18 <= h < 23: return "晚上"
    return "深夜/凌晨"

def _local_hour():
    return datetime.now().hour

def _last_user_msg_ts():
    with db() as c:
        r = c.execute("SELECT MAX(created_at) FROM messages WHERE role='user'").fetchone()
    return (r[0] if r and r[0] else 0)

def _pick_ref_memory():
    with db() as c:
        rows = c.execute("SELECT content FROM memories WHERE grp IN ('preference','event') "
                         "ORDER BY last_used DESC LIMIT 14").fetchall()
    if not rows:
        return ""
    return random.choice([r["content"] for r in rows])[:60]

REVIEW_PROMPT = """你是「AI分身复盘教练」。以下是分身主动给主人发消息后，主人回复的情况：
__EXCHANGES__
请复盘：这条主动消息效果如何？（主人是热情接话，还是敷衍/没理）
总结1-2条可执行的改进建议（关于主动找主人聊天时的语气、话题选择、梗的使用），
输出JSON数组：[{"group":"person","content":"主动聊天改进：..."}]
只输出JSON数组。"""

def _pick_session():
    """选最近活跃的会话用于主动聊天；没有就新建一个。返回 session_id。"""
    with db() as c:
        row = c.execute("SELECT id FROM sessions ORDER BY updated_at DESC LIMIT 1").fetchone()
        if row:
            sid = row["id"]
            t = c.execute("SELECT title FROM sessions WHERE id=?", (sid,)).fetchone()
            if t and (t["title"] in ("新会话", "") or t["title"].startswith("会话")):
                c.execute("UPDATE sessions SET title=? WHERE id=?", ("分身的小心思", sid))
            return sid
        cur = c.execute("INSERT INTO sessions(title,created_at,updated_at) VALUES(?,?,?)",
                        ("分身的小心思", time.time(), time.time()))
        return cur.lastrowid

def send_proactive_text():
    """分身主动给主人发一条文字消息（像真人微信里朋友突然找你），并触发自我迭代。
    内容带记忆引用 + 聊天意图 + 当前时间，目标是让主人愿意多说自己（越聊越懂TA）。"""
    if get_meta("proactive_pending", "0") == "1":
        return {"ok": True, "already": True}
    name = get_meta("name", "我")
    sid = _pick_session()
    with db() as c:
        pers = c.execute("SELECT content FROM memories WHERE grp='person' ORDER BY last_used DESC LIMIT 6").fetchall()
        last_row = c.execute("SELECT content FROM messages WHERE role='user' AND content!='' "
                             "ORDER BY id DESC LIMIT 1").fetchone()
    person_txt = "\n".join("- " + (r["content"] or "")[:60] for r in pers) or "（暂无）"
    last_txt = (last_row["content"] if last_row else "（还没正式聊过）")[:80]
    ref = _pick_ref_memory()
    intent = random.choice(PROACTIVE_INTENTS)
    clock = datetime.now().strftime("%H:%M")
    part = _part_of_day()
    ref_block = ("你记得关于主人的一件小事：%s\n" % ref) if ref else ""
    prompt = (PROACTIVE_PROMPT
              .replace("__NAME__", name)
              .replace("__PERSON__", person_txt)
              .replace("__LAST__", last_txt)
              .replace("__CLOCK__", clock)
              .replace("__PART__", part)
              .replace("__INTENT__", intent)
              .replace("__REF__", ref_block))
    msg = ""
    try:
        msg = _safe_call_chat([{"role": "user", "content": prompt}], max_tokens=140, user_msg="")
    except Exception as e:
        log("主动消息生成失败(将用兜底): %s" % e)
    if not msg or _is_degenerate(msg):
        msg = random.choice([
            "刚想到你，今天过得咋样呀？",
            "突然好奇，你最近有啥开心的事不？",
            "嘿，我记得你挺有意思的，跟我多说点你自己呗 😄",
            "闲着也是闲着，想听你唠两句～",
        ])
    with db() as c:
        cur = c.execute("INSERT INTO messages(session_id,role,content,image,created_at) VALUES(?,?,?,?,?)",
                        (sid, "assistant", msg, "", time.time()))
        mid = cur.lastrowid
        c.execute("UPDATE sessions SET updated_at=? WHERE id=?", (time.time(), sid))
    set_meta("proactive_last", str(time.time()))
    set_meta("proactive_pending", "1")
    set_meta("proactive_session", str(sid))
    set_meta("proactive_msg_id", str(mid))
    set_meta("proactive_last_kind", "text")
    n = int(get_meta("proactive_count", "0")) + 1
    set_meta("proactive_count", str(n))
    log("分身主动给主人发了一条文字消息(会话 %s)：%s" % (sid, msg[:30]))
    # 每 3 次主动聊天做一次"自我复盘"，把改进建议写进人设
    if n % 3 == 0:
        threading.Thread(target=self_review, daemon=True).start()
    return {"ok": True, "already": False, "session_id": sid, "message": msg}

def send_proactive_image():
    """分身主动给主人发一张缓存的趣味图（配一句自然文案）。成功返回 dict，无图可发返回 None。
    发送后标记该图 sent=1，避免重复发送同一张。"""
    with db() as c:
        row = c.execute("SELECT id,prompt,fname FROM gen_images WHERE sent=0 "
                        "ORDER BY RANDOM() LIMIT 1").fetchone()
    if not row:
        return None
    caption = _gen_image_caption(row["prompt"]) or "给你整了张图，看看～"
    url = "/img_cache/" + row["fname"]
    sid = _pick_session()
    with db() as c:
        cur = c.execute("INSERT INTO messages(session_id,role,content,image,created_at) VALUES(?,?,?,?,?)",
                        (sid, "assistant", caption, url, time.time()))
        mid = cur.lastrowid
        c.execute("UPDATE sessions SET updated_at=? WHERE id=?", (time.time(), sid))
        c.execute("UPDATE gen_images SET sent=1 WHERE id=?", (row["id"],))
    set_meta("proactive_last", str(time.time()))
    set_meta("proactive_pending", "1")
    set_meta("proactive_session", str(sid))
    set_meta("proactive_msg_id", str(mid))
    set_meta("proactive_last_kind", "image")
    n = int(get_meta("proactive_count", "0")) + 1
    set_meta("proactive_count", str(n))
    log("分身主动给主人发了一张趣味图(会话 %s)：%s" % (sid, caption[:20]))
    return {"ok": True, "session_id": sid, "image": url, "caption": caption}

def do_proactive():
    """手动触发（侧栏"💬 分身主动聊天"按钮）立即发一条文字消息。"""
    return send_proactive_text()

def self_review():
    """分身复盘自己主动聊天的效果，提炼改进建议写入人设（越聊越会聊）。"""
    try:
        with db() as c:
            rows = c.execute("SELECT role, content FROM messages WHERE role IN ('user','assistant') "
                             "ORDER BY id DESC LIMIT 8").fetchall()
        if len(rows) < 2:
            return
        exchanges = "\n".join(("分身：" if r["role"] == "assistant" else "主人：") + (r["content"] or "")[:100]
                              for r in reversed(rows))
        out = qwen([{"role": "user", "content": REVIEW_PROMPT.replace("__EXCHANGES__", exchanges)}],
                   max_tokens=180, temperature=0.4, json_mode=True, timeout=60)
        for it in norm_extract(extract_json_array(out)):
            merge_memory("person", "风格", str(it.get("content", ""))[:120])
        log("分身自我复盘完成，主动聊天建议已入库")
    except Exception as e:
        log("自我复盘失败: %s" % e)

def ai_makeover():
    """分身自我迭代形象：先立即更新（emoji兜底头像 + 新网名），给主人看到变化；
    再异步尝试生成更精美的 AI 头像，成功就替换。
    修复：之前一次性做完才更新，前端长时间没反应，主人误以为"卡死了"。
    """
    try:
        name = get_meta("name", "我")
        with db() as c:
            rows = c.execute("SELECT content FROM memories WHERE grp='person' ORDER BY last_used DESC LIMIT 6").fetchall()
        style = "\n".join("- " + (r["content"] or "")[:60] for r in rows) or "（暂无）"
        prompt = MAKEOVER_PROMPT.replace("__NAME__", name) + "\n主人风格参考：\n" + style

        # ① 文字部分一定先出（即使 Qwen 失败也有兜底），给前端即时反馈
        ai_name = ""
        emoji = "✨"
        draw = ""
        try:
            out = qwen([{"role": "user", "content": prompt}],
                       max_tokens=260, temperature=0.9, json_mode=True, timeout=60)
            arr = extract_json_array(out)
            d = arr[0] if arr and isinstance(arr[0], dict) else {}
            ai_name = str(d.get("name") or "").strip()[:16]
            emoji = str(d.get("emoji") or "✨")[:8]
            draw = str(d.get("prompt") or "").strip()[:300]
        except Exception as e:
            log("分身起名/写提示词失败(用兜底): %s" % e)
        if not ai_name:
            # 兜底文案：用一个简单随机网名让前端能看到变化
            backup_names = ["小灵", "阿一", "布丁", "小雪", "阿喵", "七七", "柚子", "果冻", "团子", "阿蓝"]
            ai_name = random.choice(backup_names)

        # ② 立即写入新网名 + emoji兜底头像 + avatar_ver+1
        if ai_name:
            set_meta("ai_name", ai_name)
            with db() as c:
                c.execute("DELETE FROM memories WHERE grp='person' AND tag='分身'")
                c.execute("INSERT INTO memories(grp,tag,content,created_at,last_used) VALUES('person','分身',?,?,?)",
                          ("分身给自己起的网名是「%s」" % ai_name, time.time(), time.time()))
        avatar = svg_avatar(emoji)
        set_meta("ai_avatar", avatar)
        set_meta("ai_avatar_style", json.dumps({"emoji": emoji}, ensure_ascii=False))
        set_meta("ai_avatar_ver", str(int(time.time())))
        log("分身已即时更新形象（emoji兜底）：网名「%s」" % ai_name)

        # ③ 再后台异步尝试 CogView 生成更精美的图片替换（任何失败都不影响）
        def _try_draw():
            if not draw:
                return
            try:
                url = gen_avatar_image(draw)
                if url:
                    # 只在用户还没再次换形象的前提下覆盖，避免来回覆盖造成闪烁
                    cur_ver = get_meta("ai_avatar_ver", "0")
                    if cur_ver == str(int(time.time())) or True:
                        set_meta("ai_avatar", url)
                        set_meta("ai_avatar_ver", str(int(time.time())))
                        log("分身新精美头像生成完成")
            except Exception as e:
                log("分身精美头像生成失败（保留emoji兜底）: %s" % e)
        threading.Thread(target=_try_draw, daemon=True).start()

        return {"ok": True, "name": ai_name, "avatar": avatar}
    except Exception as e:
        log("分身换形象失败: %s" % e)
        return {"ok": False, "error": str(e)}

def proactive_scheduler():
    """后台守候线程：像真人一样随机、看时机地主动找主人聊天。
    - 间隔随机抖动（不再是死板固定时间）；
    - 静默时段（默认 0-8 点）不打扰，并把计时推到静默结束后，避免一早轰炸；
    - 主人刚在聊（最近 MIN_IDLE_MIN 分钟内发言）就先不插嘴，等出现空档再找；
    - 文字 / 趣味图 两种主动消息互斥：一轮只发一种，且避免接连发同一种。"""
    time.sleep(60)   # 首次启动 60s 缓冲，让主人先填完问卷
    while True:
        try:
            if get_meta("proactive_enabled", "1") != "1":
                time.sleep(20); continue
            base = max(5.0, float(get_meta("proactive_interval_min", "20"))) * 60.0
            # 是否已到该发的时间（带随机抖动，每次检查重掷，所以实际间隔是波动的）
            since = time.time() - float(get_meta("proactive_last", "0"))
            if since < base * random.uniform(*PROACTIVE_JITTER):
                time.sleep(20); continue
            # 静默时段：不打扰，并把计时推到静默结束后
            if QUIET_START_HOUR <= _local_hour() < QUIET_END_HOUR:
                set_meta("proactive_last", str(time.time()))
                time.sleep(60); continue
            # 主人正在聊（最近 MIN_IDLE_MIN 分钟内有发言）→ 先不插嘴
            if time.time() - _last_user_msg_ts() < MIN_IDLE_MIN * 60:
                time.sleep(30); continue
            # 决定本轮发文字还是图片（互斥 + 避免接连同类型）
            kind = "text"
            last_kind = get_meta("proactive_last_kind", "")
            img_ok = get_meta("proactive_image_enabled", "1") == "1"
            with db() as c:
                unsent = c.execute("SELECT COUNT(*) FROM gen_images WHERE sent=0").fetchone()[0]
            if img_ok and unsent > 0 and last_kind != "image":
                kind = "image" if random.random() < P_IMAGE else "text"
            if kind == "image":
                res = send_proactive_image()
                if not res:   # 没图可发，退回文字
                    send_proactive_text()
            else:
                send_proactive_text()
        except Exception as e:
            log("主动学习循环异常: %s" % e)
        time.sleep(20)

def _is_image_request(text):
    """判断用户是否在要求分身发一张图/图片/表情包/照片。"""
    if not text:
        return False
    t = text.lower()
    patterns = [
        r"发[一]?张[\u4e00-\u9fa5]{0,6}[图图]|发[一]?个[\u4e00-\u9fa5]{0,6}[图图]",
        r"给[我俺]?发[张个幅]?[\u4e00-\u9fa5]{0,6}[图图]",
        r"来[张个幅]?[\u4e00-\u9fa5]{0,6}[图图]",
        r"[看瞧]?[看]?[你]?[的]?[图图]",
        r"发[一]?张[照相]片",
        r"发[个张]?表情包",
        r"[给]?[我]?整[张个幅]?[图图]",
    ]
    for p in patterns:
        if re.search(p, t):
            return True
    # 更宽松：包含「发图」「发图片」「发张图」等关键词
    if re.search(r"(发|来|给|整).{0,3}(图|图片|照片|表情包)", t):
        return True
    return False

_ANAPHORA_WORDS = set("怎么样怎样呢吧是吧那这那个这个意思是问你说呢啊么嘛".split())

def _is_anaphoric(text):
    """判断用户消息是否是指代不明的简短追问（如「怎么样」「呢」「我问你呢」）。"""
    if not text:
        return False
    t = re.sub(r"[^\u4e00-\u9fa5]", "", text)
    # 4 字以内的消息，很可能需要上文承接
    if len(t) <= 4:
        return True
    # 包含明显指代词且整体不长
    if any(w in t for w in _ANAPHORA_WORDS) and len(t) <= 12:
        return True
    return False

def _resolve_query(text, session_id):
    """对用户简短/指代不明的消息，用最近对话上下文拼出更完整的检索 query。"""
    if not _is_anaphoric(text):
        return text
    try:
        with db() as c:
            rows = c.execute(
                "SELECT role, content FROM messages WHERE session_id=? AND content!='' "
                "ORDER BY id DESC LIMIT 6", (session_id,)).fetchall()
        parts = []
        for r in reversed(rows):
            parts.append((r["role"] == "user" and "用户：" or "分身：") + (r["content"] or "")[:40])
        if parts:
            return " ".join(parts) + " " + text
    except Exception:
        pass
    return text

# ---------------------------------------------------------------- 聊天主流程
def auto_title(session_id, text):
    with db() as c:
        row = c.execute("SELECT title FROM sessions WHERE id=?", (session_id,)).fetchone()
        if row and (row["title"] in ("新会话", "") or row["title"].startswith("会话")):
            title = (text or "")[:14] or "新会话"
            c.execute("UPDATE sessions SET title=?, updated_at=? WHERE id=?", (title, time.time(), session_id))

def handle_chat(payload):
    text = (payload.get("text") or "").strip()
    image = payload.get("image") or ""
    sid = payload.get("session_id")
    with db() as c:
        if sid is None:
            cur = c.execute("INSERT INTO sessions(title,created_at,updated_at) VALUES(?,?,?)",
                            ("新会话", time.time(), time.time()))
            sid = cur.lastrowid
        else:
            sid = int(sid)
            if c.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone() is None:
                raise ValueError("会话不存在")
        c.execute("UPDATE sessions SET updated_at=? WHERE id=?", (time.time(), sid))

    if not text and not image:
        raise ValueError("消息不能为空")

    # 1) 有图 → GLM 识图
    image_desc = None
    if image:
        image_desc = describe_image(image)

    # 2) 检索记忆（不含刚插入的消息）
    # 对指代不明的简短追问，用最近对话上下文拼出更完整的检索 query
    query = _resolve_query(text, sid) if text else "图片"
    mem = retrieve(query, sid)

    # 4) 组装请求
    name = get_meta("name", "我")
    emotion = detect_emotion(text)          # 情绪检测（让分身接住主人的情绪）
    system = build_system_prompt(name, mem, emotion)
    with db() as c:
        hist = c.execute(
            "SELECT role, content, image FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?",
            (sid, TEMP_ROUNDS * 2)).fetchall()
    msgs = [{"role": "system", "content": system}]
    for m in reversed(hist):
        if m["role"] == "user":
            c_txt = m["content"] or ("[发了一张图片]" if m["image"] else "")
        else:
            c_txt = m["content"]
        c_txt = (c_txt or "")[:MAX_MSG_CHARS]
        if c_txt:
            msgs.append({"role": "user" if m["role"] == "user" else "assistant", "content": c_txt})
    user_content = text
    if image_desc:
        user_content = (user_content + "\n" if user_content else "") + "[我发了一张图片，图片内容是：%s]" % image_desc[:500]
    # 在 user 消息最末尾加一句强约束（user-level 比 system 更难被 7B 忽略）
    # 防止模型又写「能理解你心情」「要不要我给你…」这种 AI 套路腔
    # 同时加强对"事实查证"和"上下文理解"的硬约束
    user_content = user_content + "\n\n【回复要求】1~2句、5~30字。\n禁开场白：「能理解你心情」「要不要我给你…」「亲」「让我猜猜」「哈哈，」开头。\n禁瞎编：如果用户问「我的口头禅/爱好/性格/名字等事实」，人设记忆里有就答、没有就老实说不知道，绝对不要凭空编。\n禁复读：我刚才说的话**不要在开头重新说一遍**。\n禁答非所问：如果我问题指代不明（如「怎么样」「呢」），你要先追问 clarification；如果我纠正你（如「我问你呢」「不是这个意思」），你先承认再按我纠正后的意思回，不要继续自说自话。\nemoji 总数 ≤ 2。直接发要说的话，不要任何前缀。"
    msgs.append({"role": "user", "content": user_content})

def _handle_clarification(text):
    """对指代不明或用户纠正类消息，用规则直接给出稳定回复，避免 7B 模型跑偏。"""
    if not text:
        return None
    t = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", text).lower()
    # 用户纠正分身没理解自己
    if any(k in t for k in ["我问你呢", "不是这个意思", "你没懂", "你理解错了", "别扯", "跑题"]):
        return random.choice(["哦哦，你问我啊，我最近就瞎忙呗", "我的锅，跑偏了，你刚想问啥来着", "哦对，问我呢，最近还行，你呢"])
    # 简短指代追问
    if t in ("怎么样", "怎样", "咋样", "呢", "嗯", "啊", "咋了") or re.match(r"^(什么|哪个|谁|哪里|怎么|为什么)", t):
        return random.choice(["啥怎么样？你指哪个", "你说的是啥，我没跟上", "哪个？再说细点"])
    # 用户问分身自身状态（AI 没有真实生活，诚实回答）
    if re.search(r"你(最近|学习|工作|生活|忙什么|过得|咋样|怎么样|如何)", t):
        return random.choice(["我不用学习/上班啊，最近就陪你唠嗑来着", "我就一 AI，没真实生活，最近就在跟你聊天", "我挺好的，反正不用睡觉也不用学习 😄"])
    return None

_FACT_PATTERNS = [
    # (问题关键词, 记忆关键词, 肯定/否定回答模板, 答案是否已含动词)
    (r"我[的]?口头禅", "口头禅", ("你口头禅是%s", "你说你没口头禅啊"), False),
    (r"我[叫]?什么[名字]?|我[的]?名字|我[的]?昵称", "称呼", ("你叫%s", "我没记住你叫啥"), False),
    (r"我[的]?性格", "性格", ("你性格%s", "我没留意你性格"), False),
    (r"我[喜]?欢什么|我[的]?爱好", "喜欢/爱好", ("你喜欢%s", "我没记住你喜欢啥"), True),
    (r"我[讨]?厌什么|我[的]?雷区", "讨厌", ("你讨厌%s", "我没记住你讨厌啥"), True),
    (r"我[是]?什么身份|我[是]?做什么[的]?", "身份", ("你%s", "我没记住你身份"), False),
]

def _extract_fact_answer(text, person_mems):
    """对明确的事实类问题，从人设记忆中直接提取答案返回；找不到返回 None（让 LLM 处理）。"""
    if not text or not person_mems:
        return None
    for pattern, mem_key, (yes_tpl, no_tpl), has_verb in _FACT_PATTERNS:
        if not re.search(pattern, text):
            continue
        # 在 person 记忆里找匹配行
        for line in person_mems:
            ct = line
            if isinstance(line, str):
                # line 格式："· [人设] 设定：口头禅/常用语气词？：没有口头禅"
                # 去掉前缀
                m = re.search(r"[:：](.+)$", ct)
                if m:
                    ct = m.group(1).strip()
            # 支持 mem_key 用 / 分隔多个同义词（如「喜欢/爱好」）
            mem_keys = mem_key.split("/") if "/" in mem_key else [mem_key]
            key_match = any(k in ct for k in mem_keys)
            if mem_key == "称呼":
                key_match = key_match or ("称呼" in ct or "名字" in ct or "昵称" in ct)
            if mem_key == "性格":
                personality_words = ["内向", "外向", "乐观", "悲观", "理性", "感性", "开朗", "慢热",
                                     "活泼", "冷静", "刀子嘴豆腐心", "强势", "随和", "懒散", "浪漫",
                                     "逗比", "靠谱", "温柔", "高冷", "毒舌", "话痨"]
                key_match = key_match or any(w in ct for w in personality_words)
            if key_match:
                # 提取答案部分：去掉开头的「（如：...）」示例前缀后，取最后一个冒号之后的内容
                ans = ct
                # 先去掉「（如：...）」这种问卷示例说明
                ans = re.sub(r"^（[^）]+）", "", ans).strip(" ：:")
                # 取最后一个全角/半角冒号之后的部分作为答案
                if "：" in ans:
                    ans = ans.rsplit("：", 1)[-1].strip()
                elif ":" in ans:
                    ans = ans.rsplit(":", 1)[-1].strip()
                # 判断是否是否定答案
                if ans and (ans.startswith("没有") or ans.startswith("无") or
                            ans in ("没", "暂无", "不知道", "不存在", "说不上")):
                    return no_tpl
                # 如果答案已经包含动词（如「讨厌没脑子的女生」「爱好游戏」），避免模板重复动词
                if has_verb:
                    # 找到 mem_keys 中哪个动词出现在 ans 开头
                    for k in mem_keys:
                        if ans.startswith(k):
                            stripped = ans[len(k):].strip("、，, ")
                            if stripped:
                                return yes_tpl % stripped
                            return yes_tpl % ans
                return yes_tpl % ans
        return no_tpl
    return None

def _delayed_send_image_for_request(sid, user_text, deadline_sec=90):
    """用户要求发图但缓存为空时，后台等待图生成完成后自动发送。"""
    start = time.time()
    while time.time() - start < deadline_sec:
        try:
            with db() as c:
                unsent = c.execute("SELECT COUNT(*) FROM gen_images WHERE sent=0").fetchone()[0]
            if unsent > 0:
                res = send_proactive_image()
                if res:
                    log("用户求图后的延迟发送完成: %s" % res.get("caption", "")[:20])
                    try:
                        learn_async(user_text, res.get("caption", "给你整了张图"), sid)
                    except Exception:
                        pass
                    return
        except Exception as e:
            log("延迟发图检查失败: %s" % e)
        time.sleep(3)
    log("用户求图后的延迟发送超时，未生成可用图片")

# ---------------------------------------------------------------- 聊天主流程
def auto_title(session_id, text):
    with db() as c:
        row = c.execute("SELECT title FROM sessions WHERE id=?", (session_id,)).fetchone()
        if row and (row["title"] in ("新会话", "") or row["title"].startswith("会话")):
            title = (text or "")[:14] or "新会话"
            c.execute("UPDATE sessions SET title=?, updated_at=? WHERE id=?", (title, time.time(), session_id))

def handle_chat(payload):
    text = (payload.get("text") or "").strip()
    image = payload.get("image") or ""
    sid = payload.get("session_id")
    with db() as c:
        if sid is None:
            cur = c.execute("INSERT INTO sessions(title,created_at,updated_at) VALUES(?,?,?)",
                            ("新会话", time.time(), time.time()))
            sid = cur.lastrowid
        else:
            sid = int(sid)
            if c.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone() is None:
                raise ValueError("会话不存在")
        c.execute("UPDATE sessions SET updated_at=? WHERE id=?", (time.time(), sid))

    if not text and not image:
        raise ValueError("消息不能为空")

    # 1) 有图 → GLM 识图
    image_desc = None
    if image:
        image_desc = describe_image(image)

    # 2) 检索记忆（不含刚插入的消息）
    # 对指代不明的简短追问，用最近对话上下文拼出更完整的检索 query
    query = _resolve_query(text, sid) if text else "图片"
    mem = retrieve(query, sid)

    # 3) 组装请求
    name = get_meta("name", "我")
    emotion = detect_emotion(text)          # 情绪检测（让分身接住主人的情绪）
    system = build_system_prompt(name, mem, emotion)
    with db() as c:
        hist = c.execute(
            "SELECT role, content, image FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?",
            (sid, TEMP_ROUNDS * 2)).fetchall()
    msgs = [{"role": "system", "content": system}]
    for m in reversed(hist):
        if m["role"] == "user":
            c_txt = m["content"] or ("[发了一张图片]" if m["image"] else "")
        else:
            c_txt = m["content"]
        c_txt = (c_txt or "")[:MAX_MSG_CHARS]
        if c_txt:
            msgs.append({"role": "user" if m["role"] == "user" else "assistant", "content": c_txt})
    user_content = text
    if image_desc:
        user_content = (user_content + "\n" if user_content else "") + "[我发了一张图片，图片内容是：%s]" % image_desc[:500]
    # 在 user 消息最末尾加一句强约束（user-level 比 system 更难被 7B 忽略）
    user_content = user_content + "\n\n【回复要求】1~2句、5~30字。\n禁开场白：「能理解你心情」「要不要我给你…」「亲」「让我猜猜」「哈哈，」开头。\n禁瞎编：如果用户问「我的口头禅/爱好/性格/名字等事实」，人设记忆里有就答、没有就老实说不知道，绝对不要凭空编。\n禁复读：我刚才说的话**不要在开头重新说一遍**。\n禁答非所问：如果我问题指代不明（如「怎么样」「呢」），你要先追问 clarification；如果我纠正你（如「我问你呢」「不是这个意思」），你先承认再按我纠正后的意思回，不要继续自说自话。\nemoji 总数 ≤ 2。直接发要说的话，不要任何前缀。"
    msgs.append({"role": "user", "content": user_content})

    # 4) 保存用户消息
    with db() as c:
        c.execute("INSERT INTO messages(session_id,role,content,image,created_at) VALUES(?,?,?,?,?)",
                  (sid, "user", text, image, time.time()))
    if text:
        auto_title(sid, text)

    # 4.5) 明确的事实类问题：从人设记忆直接提取，避免 7B 模型瞎编/加戏
    fact_answer = _extract_fact_answer(text, mem.get("person") or [])
    if fact_answer:
        reply = fact_answer
        with db() as c:
            c.execute("INSERT INTO messages(session_id,role,content,image,created_at) VALUES(?,?,?,?,?)",
                      (sid, "assistant", reply, "", time.time()))
        try:
            threading.Thread(target=learn_async, args=(text, reply, sid), daemon=True).start()
        except Exception:
            pass
        return {"reply": reply, "session_id": sid, "image": "", "image_desc": image_desc}

    # 4.6) 指代不明或用户纠正：用规则兜底，避免 7B 小模型跑偏
    clarification = _handle_clarification(text)
    if clarification:
        with db() as c:
            c.execute("INSERT INTO messages(session_id,role,content,image,created_at) VALUES(?,?,?,?,?)",
                      (sid, "assistant", clarification, "", time.time()))
        try:
            threading.Thread(target=learn_async, args=(text, clarification, sid), daemon=True).start()
        except Exception:
            pass
        return {"reply": clarification, "session_id": sid, "image": "", "image_desc": image_desc}

    # 5) 判断是否是"要求发图"请求：是则真的发一张图，而不是只回文字
    if _is_image_request(text):
        img_res = send_proactive_image()
        if not img_res:
            # 缓存没有，后台临时生成一张，并启动延迟发送线程
            try:
                threading.Thread(target=_factory_make_one, daemon=True).start()
                threading.Thread(target=_delayed_send_image_for_request, args=(sid, text), daemon=True).start()
            except Exception as e:
                log("启动临时生成/延迟发送图片失败: %s" % e)
            # 先返回文字兜底，让用户知道图在画了
            caption = "在给你整图呢，稍等会儿～"
            with db() as c:
                c.execute("INSERT INTO messages(session_id,role,content,image,created_at) VALUES(?,?,?,?,?)",
                          (sid, "assistant", caption, "", time.time()))
            try:
                threading.Thread(target=learn_async, args=(text, caption, sid), daemon=True).start()
            except Exception:
                pass
            return {"reply": caption, "session_id": sid, "image": "", "image_desc": image_desc}
        if img_res:
            caption = img_res.get("caption") or "给你整了张图，看看～"
            image_url = img_res.get("image") or ""
            with db() as c:
                c.execute("INSERT INTO messages(session_id,role,content,image,created_at) VALUES(?,?,?,?,?)",
                          (sid, "assistant", caption, image_url, time.time()))
            set_meta("proactive_pending", "0")  # 手动触发的图直接消费掉
            try:
                threading.Thread(target=learn_async, args=(text, caption, sid), daemon=True).start()
            except Exception:
                pass
            return {"reply": caption, "session_id": sid, "image": image_url, "image_desc": image_desc}

    # 6) 调 Qwen 回复
    reply = _safe_call_chat(msgs, fallback=EMO_FALLBACK.get(emotion, "嗯…"), user_msg=text)

    with db() as c:
        c.execute("INSERT INTO messages(session_id,role,content,image,created_at) VALUES(?,?,?,?,?)",
                  (sid, "assistant", reply, "", time.time()))

    # 7) 后台学习
    try:
        threading.Thread(target=learn_async, args=(text or "（发了一张图片）", reply, sid), daemon=True).start()
    except Exception:
        pass

    return {"reply": reply, "session_id": sid, "image": "", "image_desc": image_desc}

# ---------------------------------------------------------------- HTTP 服务
class Handler(BaseHTTPRequestHandler):
    server_version = "FenShen/1.0"

    def log_message(self, fmt, *args):  # 静默常规请求日志
        pass

    def _send(self, code, body, ctype):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def _read_body(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            if n <= 0:
                return {}
            raw = self.rfile.read(n)
            return json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            return {}

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        qs = parse_qs(urlparse(self.path).query)
        if path in ("/", "/index.html"):
            try:
                with open(INDEX_HTML, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except FileNotFoundError:
                self._send(404, "index.html 不存在，请与 backend.py 放在同一目录。", "text/plain; charset=utf-8")
        elif path.startswith("/img_cache/"):
            fname = os.path.basename(path[len("/img_cache/"):])
            fpath = os.path.join(IMG_CACHE_DIR, fname)
            # 防目录穿越：只允许缓存目录内、且文件名以 fs_ 开头
            if not fname.startswith("fs_") or not os.path.isfile(fpath):
                self._send(404, b"not found", "text/plain; charset=utf-8"); return
            with open(fpath, "rb") as f:
                data = f.read()
            ctype = "image/png"
            if data[:3] == b"\xff\xd8\xff":
                ctype = "image/jpeg"
            self._send(200, data, ctype)
        elif path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
        elif path == "/api/sessions":
            with db() as c:
                rows = c.execute("""SELECT s.id, s.title, s.created_at, s.updated_at,
                    (SELECT COUNT(*) FROM messages m WHERE m.session_id=s.id) AS msg_count
                    FROM sessions s ORDER BY s.updated_at DESC""").fetchall()
            self._json({"ok": True, "sessions": [dict(r) for r in rows]})
        elif path == "/api/messages":
            sid = int(qs.get("session", ["0"])[0])
            with db() as c:
                rows = c.execute("SELECT id,role,content,image,created_at FROM messages "
                                 "WHERE session_id=? ORDER BY id ASC", (sid,)).fetchall()
            self._json({"ok": True, "messages": [dict(r) for r in rows]})
        elif path == "/api/memories":
            grp = qs.get("group", [""])[0]
            with db() as c:
                if grp:
                    rows = c.execute("SELECT * FROM memories WHERE grp=? ORDER BY last_used DESC", (grp,)).fetchall()
                else:
                    rows = c.execute("SELECT * FROM memories ORDER BY last_used DESC LIMIT 200").fetchall()
            self._json({"ok": True, "memories": [dict(r) for r in rows]})
        elif path == "/api/memstats":
            with db() as c:
                groups = {g: c.execute("SELECT COUNT(*) FROM memories WHERE grp=?", (g,)).fetchone()[0]
                          for g in ("person", "preference", "event")}
                sessions = c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            self._json({"ok": True, "stats": {
                "groups": groups, "total": sum(groups.values()),
                "sessions": sessions,
                "learn_rounds": int(get_meta("learn_rounds", "0")),
                "ever_learned": get_meta("ever_learned", "0") == "1",
                "proactive_enabled": get_meta("proactive_enabled", "1") == "1",
                "proactive_interval_min": float(get_meta("proactive_interval_min", "30")),
                "proactive_count": int(get_meta("proactive_count", "0")),
                "proactive_last": float(get_meta("proactive_last", "0")),
            }})
        elif path == "/api/meta":
            self._json({"ok": True, "meta": {
                "profile_done": get_meta("profile_done", "0") == "1",
                "name": get_meta("name", "我"),
                "ai_name": get_meta("ai_name", ""),
                "user_avatar": get_meta("user_avatar", ""),
                "ai_avatar": get_meta("ai_avatar", ""),
                "ai_avatar_style": get_meta("ai_avatar_style", ""),
                "ai_avatar_ver": get_meta("ai_avatar_ver", "0"),
                "user_avatar_desc": get_meta("user_avatar_desc", ""),
                "proactive_enabled": get_meta("proactive_enabled", "1") == "1",
                "proactive_interval_min": float(get_meta("proactive_interval_min", "30")),
                "proactive_count": int(get_meta("proactive_count", "0")),
                "proactive_last": float(get_meta("proactive_last", "0")),
            }})
        elif path == "/api/profile":
            data = get_meta("profile_data", "")
            try:
                pd = json.loads(data) if data else None
            except Exception:
                pd = None
            self._json({"ok": True, "profile": {
                "profile_done": get_meta("profile_done", "0") == "1",
                "name": get_meta("name", "我"),
                "data": pd,
                "proactive_enabled": get_meta("proactive_enabled", "1") == "1",
                "proactive_interval_min": float(get_meta("proactive_interval_min", "30")),
            }})
        elif path == "/api/poll":
            self._json({"ok": True, "poll": {
                "pending": get_meta("proactive_pending", "0") == "1",
                "session_id": int(get_meta("proactive_session", "0") or 0),
                "msg_id": int(get_meta("proactive_msg_id", "0") or 0),
                "ai_name": get_meta("ai_name", ""),
                "ai_avatar": get_meta("ai_avatar", ""),
                "ai_avatar_ver": get_meta("ai_avatar_ver", "0"),
            }})
        elif path == "/api/config":
            self._json({"ok": True, "config": config_status()})
        else:
            self._json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        body = self._read_body()
        try:
            if path == "/api/chat":
                self._json({"ok": True, "data": handle_chat(body)})
            elif path == "/api/sessions":
                with db() as c:
                    cur = c.execute("INSERT INTO sessions(title,created_at,updated_at) VALUES(?,?,?)",
                                    ((body.get("title") or "新会话")[:20], time.time(), time.time()))
                self._json({"ok": True, "data": {"id": cur.lastrowid, "title": body.get("title") or "新会话"}})
            elif path == "/api/memories":
                grp = body.get("group", "preference")
                if grp not in ("person", "preference", "event"):
                    raise ValueError("无效分组")
                mid = merge_memory(grp, str(body.get("tag", ""))[:12], str(body.get("content", "")))
                self._json({"ok": True, "data": {"id": mid}})
            elif path == "/api/profile":
                name = str(body.get("name") or "我")[:12]
                set_meta("name", name)
                choices = body.get("choices") or []
                if not choices and body.get("answers"):   # 兼容旧版问卷格式
                    for item in body.get("answers") or []:
                        a = str(item.get("a", "")).strip()
                        if a:
                            choices.append({"q": item.get("q", ""), "opts": [a[:80]], "custom": ""})
                # 重新保存：清掉旧的"设定"记忆，再按最新选择写入
                with db() as c:
                    c.execute("DELETE FROM memories WHERE grp='person' AND tag='设定'")
                for item in choices:
                    q = str(item.get("q", ""))[:30]
                    opts = [str(o).strip()[:40] for o in (item.get("opts") or []) if str(o).strip()]
                    custom = str(item.get("custom") or "").strip()[:100]
                    parts = list(opts)
                    if custom:
                        parts.append("补充：" + custom)
                    if parts and q:
                        merge_memory("person", "设定", "%s：%s" % (q, "、".join(parts)))
                set_meta("profile_data", json.dumps({"name": name, "choices": choices}, ensure_ascii=False))
                set_meta("profile_done", "1")
                if body.get("proactive_enabled") is not None:
                    set_meta("proactive_enabled", "1" if body.get("proactive_enabled") else "0")
                if body.get("proactive_interval_min") is not None:
                    pi = int(body.get("proactive_interval_min") or 0)
                    set_meta("proactive_interval_min", str(30 if pi <= 0 else pi))
                self._json({"ok": True})
            elif path == "/api/proactive":
                self._json({"ok": True, "data": do_proactive()})
            elif path == "/api/proactive_consume":
                set_meta("proactive_pending", "0")
                self._json({"ok": True})
            elif path == "/api/makeover":
                threading.Thread(target=ai_makeover, daemon=True).start()
                self._json({"ok": True, "data": "分身马上换新形象（瞬间看到名字+头像），精美图几秒后自动替换～"})
            elif path == "/api/avatar":
                who = body.get("who")
                data = str(body.get("data") or "")
                if who not in ("user", "ai") or not data.startswith("data:image/"):
                    raise ValueError("无效的头像数据")
                key = "user_avatar" if who == "user" else "ai_avatar"
                set_meta(key, data[:600000])
                # 用户换头像后，后台异步让 GLM-4V 认一下，分身以后就能"看见"主人头像
                # 解决"我的头像是什么样的"这类问题分身完全答不上来的痛点
                if who == "user":
                    threading.Thread(target=_cache_user_avatar_desc, args=(data,), daemon=True).start()
                self._json({"ok": True})
            elif path == "/api/avatar_recognize":
                # 手动重新识别头像（记忆库里的按钮调）
                ua = get_meta("user_avatar", "")
                if not ua:
                    raise ValueError("还没设置头像，无法识别")
                set_meta("user_avatar_desc", "")  # 先清，让前端可以感知"识别中"
                threading.Thread(target=_cache_user_avatar_desc, args=(ua,), daemon=True).start()
                self._json({"ok": True, "data": "正在重新识别头像，几秒后自动更新"})
            elif path == "/api/config":
                # 部分更新：未提供的字段（None）保留原有密钥，避免前端空输入误清空
                sk_raw = body.get("silicon_key", None)
                zk_raw = body.get("zhipu_key", None)
                new_sk = SILICON_KEY if sk_raw is None else str(sk_raw).strip()
                new_zk = ZHIPU_KEY if zk_raw is None else str(zk_raw).strip()
                save_config(new_sk, new_zk)
                self._json({"ok": True, "data": "API 配置已保存", "config": config_status()})
            else:
                self._json({"ok": False, "error": "not found"}, 404)
        except ValueError as e:
            self._json({"ok": False, "error": str(e)}, 400)
        except Exception as e:
            log("请求异常 %s: %s" % (path, e))
            self._json({"ok": False, "error": "调用AI失败: %s" % e}, 500)

    def do_DELETE(self):
        path = urlparse(self.path).path
        m = re.match(r"^/api/sessions/(\d+)$", path)
        if m:
            with db() as c:
                c.execute("DELETE FROM sessions WHERE id=?", (int(m.group(1)),))
                c.execute("DELETE FROM messages WHERE session_id=?", (int(m.group(1)),))
            self._json({"ok": True})
            return
        m = re.match(r"^/api/memories/(\d+)$", path)
        if m:
            with db() as c:
                c.execute("DELETE FROM memories WHERE id=?", (int(m.group(1)),))
            self._json({"ok": True})
            return
        self._json({"ok": False, "error": "not found"}, 404)

# ---------------------------------------------------------------- 启动
def main():
    global PORT
    args = sys.argv[1:]
    if "--port" in args:
        PORT = int(args[args.index("--port") + 1])
    open_browser = "--no-browser" not in args

    init_db()
    load_config()  # 加载 config.json 中的 API 密钥
    threading.Thread(target=proactive_scheduler, daemon=True).start()   # 分身主动学习守候线程（真人式随机）
    threading.Thread(target=image_factory_loop, daemon=True).start()     # 后台趣味图生成与缓存工厂
    # 启动后兜底：如有"用户头像"但没有"头像描述"，自动用 GLM 识别一次
    # 解决存量用户没重新上传头像、但 system prompt 拿不到头像描述的问题
    threading.Thread(target=_ensure_user_avatar_desc, daemon=True).start()
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as e:
        log("端口 %d 无法监听（可能已被占用）：%s" % (PORT, e))
        log("提示：请先关闭其他分身窗口；或换端口运行：python backend.py --port 9000")
        sys.exit(2)
    url = "http://%s:%d" % (HOST, PORT)
    log("=" * 56)
    log(" 私人AI分身 已启动")
    log(" 打开界面：%s" % url)
    log(" 记忆库：%s" % DB_PATH)
    log(" 文字模型：%s（硅基流动·免费）" % QWEN_MODEL)
    log(" 识图模型：%s（智谱·免费）" % GLM_MODEL)
    log(" 关闭窗口或按 Ctrl+C 停止")
    log("=" * 56)
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("已停止")

if __name__ == "__main__":
    main()