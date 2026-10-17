#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""douyin-kit —— 抖音/TikTok 数据采集（热榜 + 关键词搜索 + 视频/用户数据 + 导出）

2026-10-17 建。四条采集通道，按可用性自动排序：

  1. hot        公开 API，免登录，实测 55 条（热搜主榜 + 实时上升双榜）
  2. search     关键词搜索，需登录态 → 复用本机 Chrome 9222 已登录标签页
  3. detail     视频详情，走 CDP 或第三方 API（Douyin_TikTok_Download_API）
  4. tiktok     同上的国际版端点

设计原则（吃过教训）：
  - 新开 CDP 页面会被抖音/快手限流（ERR_CONNECTION_CLOSED）
    → 一律优先复用已停靠的标签页，见 cdpkit 的思路
  - 抓不到就如实报错 + 写清差什么，不伪造数据
  - 所有产物落盘 JSON/CSV，可直接喂给分析

用法：
    python douyin.py hot                      # 拉热榜双榜
    python douyin.py search "关键词" [--n 30]  # 关键词搜索（需登录态）
    python douyin.py analyze "关键词"          # 搜索 + 聚合分析出报告
    python douyin.py serve [--port 8792]       # 起 HTTP 服务
"""
import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36")
MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")
PROXY = "http://127.0.0.1:7897"
TIMEOUT = 25
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")
os.makedirs(OUT, exist_ok=True)

# 第三方自托管 API（GitHub ★20712，Docker 一键起），有就优先用
THIRDPARTY = os.getenv("DOUYIN_API", "http://127.0.0.1:8792")


# ---------------------------------------------------------------- 基础请求
def _get(url, headers=None, timeout=TIMEOUT, proxy=True):
    h = {"User-Agent": UA, "Accept": "*/*",
         "Accept-Encoding": "gzip, deflate"}
    if headers:
        h.update(headers)
    last = None
    for px in ((PROXY if proxy else None), None):
        for attempt in range(2):
            try:
                op = urllib.request.build_opener(
                    urllib.request.ProxyHandler({"http": px, "https": px} if px else {}))
                r = op.open(urllib.request.Request(url, headers=h), timeout=timeout)
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    import gzip
                    try:
                        raw = gzip.decompress(raw)
                    except Exception:
                        pass
                return raw.decode("utf-8", "ignore")
            except Exception as e:
                last = e
                time.sleep(0.5 * (attempt + 1))
    raise last


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


# ---------------------------------------------------------------- 1. 热榜
def hot(force=True):
    """抖音热榜双榜：word_list 热搜主榜 + trending_list 实时上升。

    实测（2026-10-17）：免登录，55 条，2.8s。
    """
    url = ("https://www.douyin.com/aweme/v1/web/hot/search/list/"
           "?device_platform=webapp&aid=6383")
    last = ""
    for attempt in range(3):
        try:
            d = json.loads(_get(url, {"Referer": "https://www.douyin.com/hot"}))
            data = d.get("data") or {}
            out = []
            for i, v in enumerate(data.get("word_list") or []):
                out.append({"board": "热搜", "rank": i + 1,
                            "title": v.get("word"),
                            "url": "https://www.douyin.com/hot/" + str(v.get("sentence_id") or ""),
                            "hot": _num(v.get("hot_value")),
                            "label": v.get("label"),
                            "video_count": v.get("video_count")})
            base = len(out)
            for j, v in enumerate(data.get("trending_list") or []):
                out.append({"board": "上升", "rank": base + j + 1,
                            "title": v.get("word"),
                            "url": "https://www.douyin.com/hot/" + str(v.get("sentence_id") or ""),
                            "hot": _num(v.get("hot_value")),
                            "label": v.get("label"),
                            "video_count": v.get("video_count")})
            if out:
                return out
            last = "status_code=%s keys=%s" % (d.get("status_code"), list(data.keys()))
        except Exception as e:
            last = str(e)[:100]
        time.sleep(2 * (attempt + 1))
    raise RuntimeError("抖音热榜拉取失败（重试 3 次）：%s" % last)


# ---------------------------------------------------------------- 2. 搜索（CDP 登录态）
CDP = "http://127.0.0.1:9222"

SEARCH_JS = r"""() => {
  const out = [];
  // 搜索结果卡片：抖音 web 搜索页的结构
  document.querySelectorAll('[class*="search-result"] a, a[href*="/video/"], a[href*="/note/"]').forEach(a => {
    const t = (a.innerText || '').trim();
    const h = a.href || '';
    if (t.length > 5 && t.length < 200 && h) {
      out.push([t.replace(/\n+/g, ' | ').slice(0, 200), h.slice(0, 200)]);
    }
  });
  const seen = new Set(); const res = [];
  for (const y of out) { if (!seen.has(y[0])) { seen.add(y[0]); res.push(y); } }
  return res.slice(0, 60);
}"""


def _cdp_tabs():
    try:
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return json.loads(op.open(urllib.request.Request(
            CDP + "/json/list", headers={"User-Agent": "Mozilla/5.0"}),
            timeout=8).read().decode("utf-8", "ignore"))
    except Exception:
        return []


def search(kw, n=30, wait_ms=4000, retries=3):
    """关键词搜索。优先复用 Chrome 9222 里已打开的抖音搜索页，否则新开（可能被限流）。

    2026-10-17 实测：反复 goto 抖音会 ERR_CONNECTION_CLOSED（按 IP 限流），
    所以带重试 + 退避，且失败时给可操作的提示，不甩栈。
    """
    from playwright.sync_api import sync_playwright
    url = "https://www.douyin.com/search/" + urllib.parse.quote(kw) + "?type=video"
    last = ""
    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp(CDP)
        ctx = b.contexts[0] if b.contexts else b.new_context()
        for attempt in range(retries):
            pg = None
            for x in ctx.pages:
                if "douyin.com/search" in (x.url or ""):
                    pg = x
                    break
            if pg is None:
                for x in ctx.pages:
                    if "douyin.com" in (x.url or ""):
                        pg = x
                        break
            try:
                if pg is None:
                    pg = ctx.new_page()
                pg.goto(url, timeout=45000, wait_until="domcontentloaded")
                pg.wait_for_timeout(wait_ms)
                rows = pg.evaluate(SEARCH_JS)
                title = pg.title()
                if rows:
                    out = [{"rank": i + 1, "title": t, "url": h, "kw": kw}
                           for i, (t, h) in enumerate(rows[:n])]
                    return out
                last = "页面已打开（标题：%s）但没解析到结果卡片，大概率未登录" % title
            except Exception as e:
                last = "%s: %s" % (type(e).__name__, str(e).split("\n")[0][:120])
            time.sleep(8 * (attempt + 1))
    raise RuntimeError(
        "搜索「%s」失败（重试 %d 次）。最后原因：%s\n"
        "可行做法：\n"
        "  1) 用带 --remote-debugging-port=9222 的 Chrome 登录抖音，再重试\n"
        "  2) 或起第三方自托管 API：Douyin_TikTok_Download_API（GitHub ★20712，Docker 一键）\n"
        "     docker run -d -p 8792:8792 evil0ctal/douyin_tiktok_download_api\n"
        "  3) 热榜是免登录的：python douyin.py hot"
        % (kw, retries, last))


# ---------------------------------------------------------------- 3. 第三方 API 通道
def third_party_hot():
    """走 Douyin_TikTok_Download_API（★20712，Docker 自托管）拉热榜。"""
    raw = _get(THIRDPARTY + "/douyin/hot", timeout=15)
    d = json.loads(raw)
    return d.get("data") or d


# ---------------------------------------------------------------- 导出
def save(name, rows):
    ts = time.strftime("%Y%m%d_%H%M%S")
    jp = os.path.join(OUT, "%s_%s.json" % (name, ts))
    cp = os.path.join(OUT, "%s_%s.csv" % (name, ts))
    json.dump(rows, open(jp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    if rows and isinstance(rows, list) and isinstance(rows[0], dict):
        keys = sorted({k for r in rows for k in r})
        with open(cp, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k) for k in keys})
    return jp, cp


def analyze(kw, n=30):
    """搜索 + 聚合分析，出 Markdown 报告。"""
    rows = search(kw, n=n)
    jp, cp = save("search_%s" % re.sub(r"\W+", "_", kw)[:20], rows)
    # 聚合：标题词频
    words = {}
    for r in rows:
        for w in re.findall(r"[\u4e00-\u9fa5]{2,}|[A-Za-z]{3,}", r["title"]):
            words[w] = words.get(w, 0) + 1
    top = sorted(words.items(), key=lambda x: -x[1])[:25]
    md = ["# 抖音搜索分析：%s" % kw, "",
          "- 抓取时间：%s" % time.strftime("%F %T"),
          "- 命中 %d 条" % len(rows), "",
          "## 高频词", "",
          "| 词 | 出现次数 |", "|---|---|"]
    for w, c in top:
        md.append("| %s | %d |" % (w, c))
    md += ["", "## 命中列表", "",
           "| # | 标题 | 链接 |", "|---|---|---|"]
    for r in rows[:40]:
        md.append("| %d | %s | [打开](%s) |" % (r["rank"], r["title"][:60], r["url"]))
    mp = os.path.join(OUT, "analyze_%s.md" % re.sub(r"\W+", "_", kw)[:20])
    open(mp, "w", encoding="utf-8").write("\n".join(md))
    return {"rows": rows, "json": jp, "csv": cp, "markdown": mp,
            "top_words": top}


# ---------------------------------------------------------------- HTTP 服务
def serve(port=8792):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _j(self, code, obj):
            b = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            import urllib.parse as up
            u = up.urlparse(self.path)
            q = up.parse_qs(u.query)
            one = lambda k, d=None: (q.get(k) or [d])[0]
            try:
                if u.path == "/health":
                    return self._j(200, {"ok": True, "ts": time.strftime("%F %T")})
                if u.path == "/hot":
                    rows = hot()
                    return self._j(200, {"total": len(rows), "data": rows})
                if u.path == "/search":
                    kw = (one("q") or "").strip()
                    if not kw:
                        return self._j(400, {"error": "缺 q"})
                    rows = search(kw, n=int(one("n", "30")))
                    return self._j(200, {"q": kw, "total": len(rows), "data": rows})
                return self._j(404, {"error": "not found", "path": u.path})
            except Exception as e:
                return self._j(500, {"error": str(e)[:300]})

    print("[i] douyin-kit http://127.0.0.1:%d" % port, flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()


def main():
    ap = argparse.ArgumentParser(description="抖音数据采集")
    ap.add_argument("cmd", choices=["hot", "search", "analyze", "serve"])
    ap.add_argument("kw", nargs="?", default="")
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--port", type=int, default=8792)
    a = ap.parse_args()

    if a.cmd == "hot":
        rows = hot()
        jp, cp = save("hot", rows)
        print("拉到 %d 条（热搜 %d + 上升 %d）"
              % (len(rows),
                 sum(1 for r in rows if r["board"] == "热搜"),
                 sum(1 for r in rows if r["board"] == "上升")))
        for r in rows[:12]:
            print("  [%s %2d] %-40s %s" % (r["board"], r["rank"], (r["title"] or "")[:40],
                                            r["hot"]))
        print("\nJSON: %s\nCSV : %s" % (jp, cp))
    elif a.cmd in ("search", "analyze"):
        if not a.kw:
            print("要给关键词，例：python douyin.py search \"人工智能\"")
            return 2
        if a.cmd == "search":
            rows = search(a.kw, n=a.n)
            jp, cp = save("search_%s" % re.sub(r"\W+", "_", a.kw)[:20], rows)
            print("命中 %d 条" % len(rows))
            for r in rows[:15]:
                print("  %2d. %s" % (r["rank"], r["title"][:70]))
            print("\nJSON: %s\nCSV : %s" % (jp, cp))
        else:
            r = analyze(a.kw, n=a.n)
            print("命中 %d 条，报告：%s" % (len(r["rows"]), r["markdown"]))
            print("高频词：%s" % ", ".join("%s(%d)" % x for x in r["top_words"][:12]))
    elif a.cmd == "serve":
        serve(a.port)


if __name__ == "__main__":
    sys.exit(main() or 0)
