"""
scripts/ingest_xhs_mcp.py
--------------------------
通过 xhs-mcp MCP HTTP 服务采集小红书笔记，写入 samples.json。

设计原则：
  - 浏览器会话全程复用（不重复开关页面）
  - 人类行为模拟：对数正态停留时长 + 偶发长停顿 + 预热浏览
  - 自适应限流（出错后自动延长间隔）

用法（PowerShell）：
  python scripts\ingest_xhs_mcp.py --keyword "护肤" --limit 20
  python scripts\ingest_xhs_mcp.py --keyword "减脂,健身,穿搭" --limit 20
  python scripts\ingest_xhs_mcp.py --keyword "健身" --limit 10 --dry-run

前提：npx xhs-mcp login 已完成登录
"""

import argparse
import hashlib
import io
import json
import math
import random
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ── Windows 控制台强制 UTF-8 ─────────────────────────────────────────────────
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# ── 路径 ─────────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent
SAMPLES_PATH = ROOT / "openclaw" / "data" / "xhs-feed" / "samples.json"
LOG_PATH = ROOT / "logs" / "ingest_xhs_mcp.log"
MCP_PORT = 3979


# ── 日志 ─────────────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    try:
        print(line)
    except UnicodeEncodeError:
        print(line.encode("ascii", errors="replace").decode("ascii"))
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


# ══════════════════════════════════════════════════════════════════════════════
# 人类行为模拟层
# ══════════════════════════════════════════════════════════════════════════════

class HumanBehavior:
    """
    模拟真实用户的浏览节律，使请求时序分布接近人类行为。

    核心行为链（每次查看一条笔记）：
      鼠标游走 → 滚轮下滑列表 → 点击进入 → 阅读停留 → 滚轮浏览正文 → 退出

    特殊事件（低概率）：
      2%  深度阅读  45-90s
      5%  走神停顿  15-45s
      8%  回头翻阅（scroll_up 再 scroll_down）
      整体节奏随错误率自适应变慢
    """

    # 标准视口高度（px），决定一次滚动多少"屏"
    _VIEWPORT_H = 844

    def __init__(self, base_sec: float = 4.0) -> None:
        self.base_sec = base_sec
        self._error_count = 0
        self._request_count = 0

    # ── 核心概率分布 ─────────────────────────────────────────────────────────

    def _lognormal_sleep(self, median: float, sigma: float = 0.4) -> float:
        """对数正态停留，median=中位数，上限 median×8，下限 1s。"""
        raw = random.lognormvariate(math.log(median), sigma)
        return max(1.0, min(raw, median * 8))

    def _micro(self, lo: float = 0.08, hi: float = 0.35) -> None:
        """微停顿：模拟鼠标移动或 DOM 渲染完成后的自然停顿。"""
        time.sleep(random.uniform(lo, hi))

    def _roll_special_event(self) -> float | None:
        r = random.random()
        if r < 0.02:
            p = random.uniform(45, 90)
            log(f"  [行为] 深度阅读停顿 {p:.0f}s ...")
            return p
        if r < 0.07:
            p = random.uniform(15, 45)
            log(f"  [行为] 走神停顿 {p:.0f}s ...")
            return p
        return None

    # ── 鼠标指针游走 ──────────────────────────────────────────────────────────

    def mouse_drift(self, label: str = "") -> None:
        """
        模拟鼠标在页面上的随机游走停顿。
        每次"移动"产生 0.05-0.25s 的微停，模拟指针从上一位置
        移向目标控件的路径耗时（贝塞尔曲线运动在时域上的投影）。
        """
        steps = random.randint(2, 5)          # 鼠标经过几个"中途点"
        total = 0.0
        for _ in range(steps):
            t = random.uniform(0.05, 0.25)
            time.sleep(t)
            total += t
        if label:
            log(f"  [鼠标] 移向 [{label}]  {total:.2f}s  ({steps}步)")

    # ── 滚轮行为 ──────────────────────────────────────────────────────────────

    def scroll_down_list(self, n_items: int) -> None:
        """
        浏览结果列表：模拟用户用滚轮从上往下扫一遍。
        每滚动约 1 屏（约 3 条卡片）停顿一次，眼睛扫视标题。
        """
        screens = max(1, math.ceil(n_items / 3))
        log(f"  [滚轮] 下滑列表 {screens} 屏（共约 {n_items} 条）")
        for i in range(screens):
            # 每屏滚动分 2-4 次滚轮事件（不是一次到底）
            sub_scrolls = random.randint(2, 4)
            for _ in range(sub_scrolls):
                self._micro(0.10, 0.30)       # 每次滚轮事件间隔
            # 扫视卡片的眼动停顿
            gaze = random.uniform(0.4, 1.2)
            time.sleep(gaze)

    def scroll_read_body(self, body_len: int = 200) -> None:
        """
        阅读笔记正文：按正文字数估算阅读时长，分段滚动。
        中文阅读速度约 400-600 字/分钟。
        """
        if body_len <= 0:
            self._micro(0.5, 1.2)
            return
        read_sec = body_len / random.uniform(400, 600) * 60  # 字 → 秒
        # 分 2-5 段滚动，模拟边读边划
        segments = random.randint(2, min(5, max(2, body_len // 80)))
        log(f"  [滚轮] 阅读正文 {body_len}字 ~{read_sec:.0f}s  分{segments}段")
        seg_time = read_sec / segments
        for _ in range(segments):
            time.sleep(max(0.3, seg_time + random.uniform(-0.3, 0.5)))
            self._micro(0.05, 0.15)   # 每次滚动后的短暂暂停

        # 8% 概率"往上翻了一下再往下"
        if random.random() < 0.08:
            up_t = random.uniform(0.8, 2.0)
            log(f"  [滚轮] 回翻 {up_t:.1f}s")
            time.sleep(up_t)
            self._micro(0.1, 0.3)

    # ── 翻页行为 ──────────────────────────────────────────────────────────────

    def page_turn(self, page_num: int) -> None:
        """
        翻到下一页：
          1. 鼠标移向翻页区域（drift）
          2. 点击/触发翻页（micro）
          3. 等待新一页加载（lognormal 2s 中位）
        """
        self.mouse_drift("翻页按钮")
        self._micro(0.1, 0.3)                 # 点击延迟
        load = self._lognormal_sleep(2.0, sigma=0.35)
        log(f"  [翻页] 第 {page_num} 页加载 {load:.1f}s")
        time.sleep(load)

    # ── 复合动作 ─────────────────────────────────────────────────────────────

    def dwell_after_note(self, body_len: int = 0) -> None:
        """
        查看完一条笔记后的完整退出-停留动作：
          滚轮读正文 → 特殊事件检查 → 鼠标移回列表 → 间隔停留
        """
        self.scroll_read_body(body_len)

        special = self._roll_special_event()
        if special:
            time.sleep(special)
            return

        self.mouse_drift("返回列表")

        multiplier = min(2 ** self._error_count, 8)
        median = self.base_sec * multiplier
        delay = self._lognormal_sleep(median, sigma=0.45)
        log(f"  [行为] 间隔停留 {delay:.1f}s  (backoff x{multiplier})")
        time.sleep(delay)

    def dwell_between_searches(self) -> None:
        """关键词切换前的"思考+移回搜索框"。"""
        self.mouse_drift("搜索框")
        delay = self._lognormal_sleep(self.base_sec * 2.5, sigma=0.5)
        log(f"[行为] 关键词切换停顿 {delay:.1f}s")
        time.sleep(delay)

    def warmup_browse(self, n: int = 2) -> None:
        """预热：搜索前随手刷几条 feeds，让入口行为自然。"""
        log(f"[行为] 预热浏览 {n} 次 feeds（模拟自然入口）...")
        for _ in range(n):
            self.scroll_down_list(random.randint(4, 8))
            delay = self._lognormal_sleep(3.0, sigma=0.6)
            time.sleep(delay)

    def typing_pause(self, keyword: str) -> None:
        """模拟把光标移到搜索框、逐字打字的耗时。"""
        self.mouse_drift("搜索框")
        typing_time = len(keyword) * random.uniform(0.15, 0.35)
        delay = typing_time + random.uniform(0.4, 1.2)
        time.sleep(delay)

    # ── 状态 ─────────────────────────────────────────────────────────────────

    def record_success(self) -> None:
        self._error_count = max(0, self._error_count - 1)
        self._request_count += 1

    def record_error(self) -> None:
        self._error_count += 1
        self._request_count += 1

    @property
    def status(self) -> str:
        return f"req={self._request_count} err_backoff=x{min(2**self._error_count, 8)}"


# ══════════════════════════════════════════════════════════════════════════════
# MCP 会话层（全程复用，不重复开关浏览器）
# ══════════════════════════════════════════════════════════════════════════════

class McpSession:
    """
    管理与 xhs-mcp HTTP 服务器的单一长连接会话。
    全程只启动一次浏览器，复用同一个 session_id。
    """

    def __init__(self, port: int = MCP_PORT) -> None:
        self.port = port
        self._proc: subprocess.Popen | None = None
        self._session_id: str | None = None
        self._req_id = 0

    # ── 启动/停止 ─────────────────────────────────────────────────────────────

    def _port_open(self) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            return s.connect_ex(("127.0.0.1", self.port)) == 0

    def start(self) -> bool:
        if self._port_open():
            log(f"[MCP] 端口 {self.port} 已就绪，直接复用")
        else:
            log(f"[MCP] 启动 HTTP 服务 port={self.port}...")
            cmd = f"npx xhs-mcp mcp --mode http --port {self.port}"
            self._proc = subprocess.Popen(
                cmd, shell=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            for _ in range(25):
                time.sleep(0.8)
                if self._port_open():
                    log("[MCP] 服务就绪")
                    break
            else:
                log("[MCP] 服务启动超时")
                return False

        # 初始化 MCP 握手，拿 session_id
        self._session_id = self._initialize()
        if self._session_id:
            log(f"[MCP] 会话建立 session={self._session_id[:16]}...")
        return True

    def stop(self) -> None:
        if self._proc:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except Exception:
                self._proc.kill()
            self._proc = None
            log("[MCP] 服务已停止")

    # ── HTTP 通信 ──────────────────────────────────────────────────────────────

    def _post(self, payload: dict) -> tuple[str, dict]:
        self._req_id += 1
        payload["id"] = self._req_id
        data = json.dumps(payload).encode()
        headers = {
            "Content-Type": "application/json",
            "Accept":       "application/json, text/event-stream",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id

        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/mcp",
            data=data, headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                resp_headers = dict(resp.headers)
                return body, resp_headers
        except urllib.error.HTTPError as e:
            return "", {"_error": f"HTTP {e.code}"}
        except urllib.error.URLError as e:
            return "", {"_error": str(e)}

    def _parse(self, body: str) -> dict | list | None:
        """从 SSE 流或直接 JSON 里提取 result.content[0].text 并解析。"""
        lines = body.strip().splitlines()
        for line in lines:
            line = line.strip()
            if line.startswith("data:"):
                line = line[5:].strip()
            if not line or line == "[DONE]":
                continue
            try:
                obj = json.loads(line, strict=False)
                result = obj.get("result", {})
                content = result.get("content", [])
                if content and isinstance(content, list):
                    text = content[0].get("text", "")
                    if text:
                        try:
                            return json.loads(text, strict=False)
                        except Exception:
                            return {"raw_text": text}
                if result:
                    return result
                # error 字段
                if obj.get("error"):
                    return None
            except Exception:
                continue
        return None

    # ── MCP 握手 ──────────────────────────────────────────────────────────────

    def _initialize(self) -> str | None:
        body, headers = self._post({
            "jsonrpc": "2.0",
            "method":  "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "ingest-script", "version": "2.0"},
            },
        })
        if headers.get("_error"):
            log(f"[MCP] initialize 失败: {headers['_error']}")
            return None
        return (headers.get("Mcp-Session-Id")
                or headers.get("mcp-session-id")
                or None)

    # ── 工具调用 ──────────────────────────────────────────────────────────────

    def call(self, tool: str, args: dict) -> dict | list | None:
        body, headers = self._post({
            "jsonrpc": "2.0",
            "method":  "tools/call",
            "params":  {"name": tool, "arguments": args},
        })
        if headers.get("_error"):
            return None
        return self._parse(body)

    # ── 高层接口 ──────────────────────────────────────────────────────────────

    def _extract_feeds(self, data) -> list[dict]:
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for k in ("feeds", "notes", "items", "data", "result"):
                if isinstance(data.get(k), list):
                    return data[k]
        return []

    def search(self, keyword: str, page: int = 1) -> list[dict]:
        """
        搜索关键词，支持翻页（page >= 1）。
        优先走 MCP 工具以复用同一浏览器会话；失败时降级 CLI。
        """
        args: dict = {"keyword": keyword}
        if page > 1:
            args["page"] = page          # xhs-mcp 若支持分页则生效
        data = self.call("xhs_search_note", args)
        if data is None:
            log("[MCP] search 工具失败，降级 CLI")
            return _cli_search(keyword)
        return self._extract_feeds(data)

    def get_detail(self, feed_id: str, xsec_token: str) -> dict | None:
        """拉取笔记详情（正文）。"""
        return self.call("xhs_get_note_detail", {
            "feedId": feed_id,
            "xsecToken": xsec_token,
        })

    def warmup_feeds(self) -> None:
        """预热：浏览 discover feeds，让账号有"正常入口"行为。"""
        self.call("xhs_discover_feeds", {})


# ══════════════════════════════════════════════════════════════════════════════
# CLI 降级方案（MCP 失败时备用）
# ══════════════════════════════════════════════════════════════════════════════

def _cli_search(keyword: str) -> list[dict]:
    cmd = f'npx xhs-mcp search -k "{keyword}"'
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, timeout=60)
        raw = r.stdout.decode("utf-8", errors="replace")
        for i, ch in enumerate(raw):
            if ch in ("{", "["):
                raw = raw[i:]
                break
        data = json.loads(raw, strict=False)
        if isinstance(data, dict):
            return data.get("feeds", [])
        return data if isinstance(data, list) else []
    except Exception as e:
        log(f"[CLI] search 失败: {e}")
        return []


# ══════════════════════════════════════════════════════════════════════════════
# 字段映射
# ══════════════════════════════════════════════════════════════════════════════

def _count(val) -> int:
    if val is None:
        return 0
    if isinstance(val, int):
        return val
    s = str(val).replace(",", "").strip()
    if "万" in s:
        try:
            return int(float(s.replace("万", "")) * 10000)
        except ValueError:
            return 0
    try:
        return int(float(s))
    except ValueError:
        return 0


def _sop_tag(text: str) -> str:
    t = text.lower()
    if any(w in t for w in ["教程", "步骤", "怎么", "如何", "方法", "攻略", "技巧"]):
        return "tutorial"
    if any(w in t for w in ["测评", "推荐", "好用", "踩雷", "避坑", "值不值"]):
        return "review"
    if any(w in t for w in ["第一次", "故事", "经历", "那天", "记录"]):
        return "story"
    if any(w in t for w in ["盘点", "合集", "清单"]):
        return "list"
    return "other"


def _emotion_tag(text: str) -> str:
    t = text.lower()
    neg = sum(1 for w in ["踩雷", "后悔", "失望", "差评", "烂", "垃圾", "坑"] if w in t)
    pos = sum(1 for w in ["爱了", "绝了", "yyds", "推荐", "好用", "好吃", "棒"] if w in t)
    if pos > neg:
        return "positive"
    if neg > pos:
        return "negative"
    if pos and neg:
        return "mixed"
    return "neutral"


def map_item(feed: dict, detail: dict | None, keyword: str) -> dict | None:
    note_card = feed.get("noteCard", feed)
    interact = note_card.get("interactInfo", {}) or {}

    title = (note_card.get("displayTitle") or note_card.get("title")
             or feed.get("title") or "").strip()
    body = ""
    if detail:
        body = (detail.get("desc") or detail.get("content")
                or detail.get("note_card", {}).get("desc") or "").strip()
    if not title and not body:
        return None

    like    = _count(interact.get("likedCount")    or interact.get("liked_count")    or 0)
    collect = _count(interact.get("collectedCount") or interact.get("collect_count") or 0)
    comment = _count(interact.get("commentCount")   or interact.get("comment_count") or 0)
    share   = _count(interact.get("sharedCount")    or interact.get("share_count")   or 0)

    published_at = None
    for tag in note_card.get("cornerTagInfo", []):
        if tag.get("type") == "publish_time":
            published_at = tag.get("text")
            break

    item: dict = {
        "title_hint":  title[:500],
        "body_hint":   body[:2000],
        "like_proxy":  max(like, 1),
        "sop_tag":     _sop_tag(title + " " + body),
        "emotion_tag": _emotion_tag(title + " " + body),
        "source":      "xhs_mcp",
        "keyword":     keyword,
        "ingested_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    if collect:  item["collect_proxy"]  = collect
    if comment:  item["comment_proxy"]  = comment
    if share:    item["share_proxy"]    = share
    if published_at: item["published_at"] = published_at

    note_id = feed.get("id") or feed.get("note_id")
    if note_id:   item["note_id"]    = str(note_id)
    xsec = feed.get("xsecToken") or feed.get("xsec_token", "")
    if xsec:      item["xsec_token"] = xsec

    return item


# ══════════════════════════════════════════════════════════════════════════════
# 样本文件
# ══════════════════════════════════════════════════════════════════════════════

def _dedup_key(item: dict) -> str:
    raw = (item.get("title_hint", "") + "|" + item.get("body_hint", ""))[:300]
    return hashlib.md5(raw.encode()).hexdigest()


def load_samples(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log(f"[WARN] 读取 samples.json 失败: {e}")
        return []


def save_samples(path: Path, items: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


# ══════════════════════════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(description="xhs-mcp 人类化采集 -> samples.json")
    parser.add_argument("--keyword", "-k", required=True,
                        help="关键词，多个用英文逗号分隔")
    parser.add_argument("--limit", "-l", type=int, default=20,
                        help="每关键词最多保存条数（默认 20）")
    parser.add_argument("--output", "-o", default=str(SAMPLES_PATH))
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印预览，不写文件")
    parser.add_argument("--skip-detail", action="store_true",
                        help="跳过正文拉取（更快，body_hint 为空）")
    parser.add_argument("--base-dwell", type=float, default=4.0,
                        help="基础停留时长中位数秒（默认 4.0）")
    args = parser.parse_args()

    out_path = Path(args.output)
    keywords = [k.strip() for k in args.keyword.split(",") if k.strip()]

    existing = load_samples(out_path)
    seen = {_dedup_key(i) for i in existing}
    log(f"[初始化] 已有 {len(existing)} 条，去重 key={len(seen)}")

    # ── 启动会话 ──────────────────────────────────────────────────────────────
    session = McpSession(MCP_PORT)
    human = HumanBehavior(base_sec=args.base_dwell)

    if not session.start():
        log("[ERROR] MCP 服务无法启动，退出")
        sys.exit(1)

    new_items: list[dict] = []

    try:
        # 预热：搜索前随手刷几条 feeds，让入口行为自然
        human.warmup_browse(n=random.randint(1, 3))
        session.warmup_feeds()

        for kw_idx, kw in enumerate(keywords):
            log(f"\n{'='*58}")
            log(f"[关键词 {kw_idx+1}/{len(keywords)}] {kw}")

            # 模拟打字后发起搜索
            human.typing_pause(kw)

            # ── 多页采集 ────────────────────────────────────────────────────
            all_feeds: list[dict] = []
            page = 1
            # 每页约 20 条，按 limit 决定翻几页（最多 5 页）
            max_pages = min(5, math.ceil(args.limit / 20))

            while len(all_feeds) < args.limit and page <= max_pages:
                if page > 1:
                    human.page_turn(page)    # 翻页行为（鼠标→点击→等加载）

                feeds_page = session.search(kw, page=page)
                log(f"  [P{page}] 返回 {len(feeds_page)} 条")

                if not feeds_page:
                    log(f"  [P{page}] 无更多结果，停止翻页")
                    break

                # 滚轮浏览这一页的结果列表
                human.scroll_down_list(len(feeds_page))

                all_feeds.extend(feeds_page)
                page += 1

            # 去重后随机乱序（真实用户不总是从上到下点）
            take = all_feeds[:args.limit * 2]   # 多取一些，去重后再截
            random.shuffle(take)
            log(f"[处理] 共 {len(all_feeds)} 条，乱序处理前 {len(take)} 条")

            processed = 0
            for idx, feed in enumerate(take, 1):
                if processed >= args.limit:
                    break

                note_id   = feed.get("id", "")
                xsec      = feed.get("xsecToken", "")
                note_card = feed.get("noteCard", feed)
                title_raw = (note_card.get("displayTitle")
                             or note_card.get("title", ""))[:25]

                # 鼠标移向卡片（模拟选中目标）
                human.mouse_drift(f"卡片[{idx}]")

                # 拉取详情（正文）
                detail = None
                if not args.skip_detail and note_id and xsec:
                    detail = session.get_detail(note_id, xsec)
                    if detail:
                        human.record_success()
                    else:
                        human.record_error()

                item = map_item(feed, detail, kw)
                if item is None:
                    log(f"  [{idx:02d}] 跳过（无文本）")
                    human.dwell_after_note(body_len=0)
                    continue

                dk = _dedup_key(item)
                if dk in seen:
                    log(f"  [{idx:02d}] 重复: {title_raw}")
                    human.dwell_after_note(body_len=0)
                    continue

                seen.add(dk)
                new_items.append(item)
                processed += 1

                body_len = len(item.get("body_hint", ""))
                body_flag = "Y" if body_len else "N"
                log(
                    f"  [{idx:02d}] {title_raw:26s}"
                    f"  like={item['like_proxy']:>6}"
                    f"  collect={item.get('collect_proxy', 0):>5}"
                    f"  body={body_flag}({body_len}字)"
                    f"  sop={item['sop_tag']}"
                    f"  [{human.status}]"
                )

                # 阅读正文 + 滚轮 + 间隔停留
                if processed < args.limit:
                    human.dwell_after_note(body_len=body_len)

            # 关键词之间的停顿
            if kw_idx < len(keywords) - 1:
                human.dwell_between_searches()

    except KeyboardInterrupt:
        log("\n[中断] Ctrl+C，保存已采集数据")
    finally:
        session.stop()

    total = len(existing) + len(new_items)
    log(f"\n{'='*58}")
    log(f"[汇总] 新增 {len(new_items)} 条 -> 合计 {total} 条")

    if args.dry_run:
        log("[DRY-RUN] 预览模式，不写文件")
        return
    if not new_items:
        log("[跳过] 无新增，samples.json 不变")
        return

    save_samples(out_path, existing + new_items)
    log(f"[完成] -> {out_path}")
    log("\n下一步：")
    log("  python scripts\\compute_feed_metrics_v0.py")
    log("  python scripts\\export_features_v0.py")


if __name__ == "__main__":
    main()
