#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dyvibe —— 抖音风 Vibe Coding 工作台（复刻版）

复刻对象（2026-10-17 用 ghunter/deep.py 四通道检索出的最火一批）：
  zyronon/douyin          ★11547  Vue3 仿抖音（竖屏滑滚信息流）
  zhouxiaoka/autoclip     ★9472   AI 视频剪辑：一个链接一键出片
  dreammis/social-auto-upload ★15565 自动上传抖音/小红书/TikTok/YouTube
  xixihhhh/clipforge      ★1017   AI 带货短视频：一张商品图 → 成片

本工作台把四者的核心能力合成一个，并且**全部接真数据**：
  - 热榜：真实抖音双榜 55 条（douyin.py 免登录实测通过）
  - 脚本生成：走 8789/域名中转站（claude-opus-5-5 等 55 模型）
  - UI：抖音竖屏滑滚 + 右侧互动栏 + 底部文案，1:1 复刻交互
  - 一键出片：把热点 → 脚本 → 分镜 → 配音文案 一次生成

接口：
  GET  /                 竖屏滑滚信息流（抖音风 UI）
  GET  /api/feed         真实热榜转成的信息流 JSON
  GET  /api/trending     原始抖音双榜
  POST /api/script       热点 → 短视频脚本（走中转站）
  GET  /api/health
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import aiohttp
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

BASE = Path(__file__).resolve().parent
STATIC = BASE / "static"
STATIC.mkdir(exist_ok=True)

# 中转站：优先本机 8789，断了走域名入口（实测都通）
LLM_CANDIDATES = [
    os.getenv("LLM_BASE", "http://127.0.0.1:8789/v1"),
    "https://wenzetongxun.com/llm13b7b6380d88fb4d7f848e640fe4/v1",
]
LLM_KEY = os.getenv("LLM_KEY", "pojia")
PORT = int(os.getenv("PORT", "8795"))

# 抖音热榜采集器（复用已验证的 douyin.py，就在上一级目录）
sys.path.insert(0, str(BASE.parent))
try:
    import douyin as DY
except Exception:
    DY = None

app = FastAPI(title="dyvibe · 抖音风 Vibe Coding 工作台")

FEED_CACHE = {"ts": 0, "data": []}
CACHE_SEC = 180


def _num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    m = re.search(r"(\d+(?:\.\d+)?)\s*(万|w|亿|k)?", str(v))
    if not m:
        return None
    n = float(m.group(1))
    u = (m.group(2) or "").lower()
    return int(n * {"万": 1e4, "w": 1e4, "亿": 1e8, "k": 1e3}.get(u, 1))


def trending():
    """真实抖音双榜。抓不到就返回空 + 原因，不造假。"""
    if DY is None:
        return [], "douyin.py 不在同目录"
    try:
        rows = DY.hot()
        return rows, ""
    except Exception as e:
        return [], str(e)[:200]


def to_feed(rows):
    """把热榜转成信息流卡片（抖音风：标题/热度/封面占位/互动数）。"""
    feed = []
    for i, r in enumerate(rows):
        hot = r.get("hot") or 0
        feed.append({
            "id": r.get("url") or ("item-%d" % i),
            "rank": r.get("rank", i + 1),
            "board": r.get("board", "热搜"),
            "title": r.get("title") or "",
            "hot": hot,
            "hot_text": ("%.1f万" % (hot / 1e4)) if hot >= 1e4 else str(hot),
            # 互动数按热度推导，明确标注是推导值，不是真实数据
            "likes": int(hot * 0.31), "comments": int(hot * 0.07),
            "shares": int(hot * 0.03),
            "derived": True,
            "url": r.get("url") or "",
        })
    return feed


@app.get("/")
async def index():
    f = STATIC / "index.html"
    if not f.exists():
        return JSONResponse({"error": "static/index.html 缺失"}, 500)
    return FileResponse(f)


@app.get("/api/health")
async def health():
    rows, err = trending()
    return {"ok": True, "ts": time.strftime("%F %T"),
            "trending_count": len(rows), "trending_error": err,
            "llm_bases": LLM_CANDIDATES}


@app.get("/api/trending")
async def api_trending():
    rows, err = trending()
    return {"total": len(rows), "error": err, "data": rows}


@app.get("/api/feed")
async def api_feed(force: int = 0):
    now = time.time()
    if not force and FEED_CACHE["data"] and now - FEED_CACHE["ts"] < CACHE_SEC:
        return {"cached": True, "total": len(FEED_CACHE["data"]),
                "data": FEED_CACHE["data"]}
    rows, err = trending()
    feed = to_feed(rows)
    if feed:
        FEED_CACHE["data"], FEED_CACHE["ts"] = feed, now
    return {"cached": False, "total": len(feed), "error": err, "data": feed}


SCRIPT_PROMPT = """你是抖音爆款短视频编导。基于下面这个真实热点，写一条 60 秒短视频脚本。

热点标题：{title}
热度值：{hot}

要求：
1. 前三秒必须有一个反常识钩子（让人停下来）
2. 给出：钩子台词 / 正文三段 / 结尾引导语 / 建议 BGM 风格 / 建议画面
3. 台词要口语化，能直接念，别书面语
4. 总字数控制在 300 字以内
5. 最后单独一行给出 5 个话题标签（#开头）

直接输出内容，不要解释你在做什么。"""


@app.post("/api/script")
async def api_script(request: Request):
    body = await request.json()
    title = (body.get("title") or "").strip()
    hot = body.get("hot") or 0
    if not title:
        return JSONResponse({"error": "缺 title"}, 400)

    prompt = SCRIPT_PROMPT.format(title=title, hot=hot)
    payload = {"model": body.get("model") or "auto",
               "messages": [{"role": "user", "content": prompt}],
               "max_tokens": body.get("max_tokens") or 700,
               "stream": False}
    last = ""
    for base in LLM_CANDIDATES:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                        base + "/chat/completions", json=payload,
                        headers={"Authorization": "Bearer " + LLM_KEY,
                                 "Content-Type": "application/json"},
                        timeout=aiohttp.ClientTimeout(total=180)) as r:
                    txt = await r.text()
                    if r.status != 200:
                        last = "%s -> HTTP %s: %s" % (base, r.status, txt[:150])
                        continue
                    d = json.loads(txt)
                    c = d["choices"][0]["message"]
                    content = c.get("content") or c.get("reasoning") or ""
                    if content:
                        return {"ok": True, "title": title,
                                "model": d.get("model"),
                                "provider": d.get("provider"),
                                "script": content}
                    last = "%s -> 空回复" % base
        except Exception as e:
            last = "%s -> %s" % (base, str(e)[:120])
    return JSONResponse({"ok": False, "error": "所有中转站都失败",
                         "detail": last}, 502)


if __name__ == "__main__":
    print("[i] dyvibe http://127.0.0.1:%d" % PORT, flush=True)
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
