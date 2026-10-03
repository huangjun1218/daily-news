#!/usr/bin/env python3
"""每日热点头条推送：抓取 RSS 源 -> 去重整理 -> 生成 HTML 日报 -> 推送到微信/邮件/Telegram 等。

用法：
  python news_digest.py            # 抓取、生成 output/ 下的日报并推送
  python news_digest.py --no-push  # 只生成日报，不推送（本地预览用）

推送渠道由环境变量决定，配置了哪个就推哪个（可同时多个）：
  PUSHPLUS_TOKEN                 PushPlus（微信公众号推送）
  SERVERCHAN_KEY                 Server酱（微信推送）
  WECOM_WEBHOOK                  企业微信群机器人 webhook
  DINGTALK_WEBHOOK               钉钉群机器人 webhook（关键词需包含“日报”）
  TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID
  SMTP_HOST SMTP_PORT SMTP_USER SMTP_PASS MAIL_TO   邮件（MAIL_TO 可逗号分隔多人）
其他：
  RSSHUB_BASE   自建或可用的 RSSHub 地址，默认 https://rsshub.app
  REPORT_URL    日报网页地址（如 GitHub Pages），会附在推送消息里
"""
import argparse
import html
import json
import os
import re
import smtplib
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.header import Header
from email.mime.text import MIMEText
from pathlib import Path

import feedparser
import requests

BJ = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parent
UA = "Mozilla/5.0 (compatible; DailyNewsDigest/1.0)"
MAX_AGE_HOURS = 36  # 只保留最近 36 小时内的新闻（无发布时间的条目保留）


# ---------------- 官方接口直连（热榜、快讯，不依赖 RSSHub） ----------------
from urllib.parse import quote


def get_json(url, referer=None):
    h = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
         "Accept": "application/json, text/plain, */*"}
    if referer:
        h["Referer"] = referer
    r = requests.get(url, headers=h, timeout=20)
    r.raise_for_status()
    return r.json()


def clip(text, n=60):
    text = re.sub(r"<[^>]+>", "", html.unescape(str(text or "")))
    text = re.sub(r"\s+", " ", text).strip()
    m = re.match(r"^【([^】]{4,60})】", text)  # 快讯常见的【标题】格式
    if m:
        return m.group(1)
    return text if len(text) <= n else text[:n] + "…"


def bj_time(v):
    try:
        if isinstance(v, (int, float)) or str(v).isdigit():
            v = int(v)
            return datetime.fromtimestamp(v / 1000 if v > 1e11 else v, BJ)
        return datetime.strptime(str(v)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=BJ)
    except Exception:  # noqa: BLE001
        return None


def api_weibo_hot():
    d = get_json("https://weibo.com/ajax/side/hotSearch", "https://weibo.com/")
    out = []
    for x in d.get("data", {}).get("realtime", []):
        if x.get("is_ad") or x.get("ad_type"):
            continue
        w = x.get("word") or x.get("note")
        if w:
            num = x.get("num") or x.get("raw_hot")
            out.append({"title": w, "link": "https://s.weibo.com/weibo?q=" + quote(f"#{w}#"),
                        "summary": f"热度 {num}" if num else ""})
    return out


def api_baidu_hot():
    d = get_json("https://top.baidu.com/api/board?platform=wise&tab=realtime", "https://top.baidu.com/")
    out, seen = [], set()

    def walk(o):
        if isinstance(o, dict):
            w = o.get("word") or o.get("query")
            if w and isinstance(w, str) and w not in seen and ("url" in o or "rawUrl" in o or "hotScore" in o):
                seen.add(w)
                out.append({"title": w, "link": o.get("url") or o.get("rawUrl")
                            or "https://www.baidu.com/s?wd=" + quote(w), "summary": clip(o.get("desc"), 80)})
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(d.get("data", d))
    return out


def api_toutiao_hot():
    d = get_json("https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc", "https://www.toutiao.com/")
    return [{"title": x["Title"], "link": x.get("Url") or "https://www.toutiao.com/", "summary": ""}
            for x in d.get("data", []) if x.get("Title")]


def api_zhihu_hot():
    d = get_json("https://www.zhihu.com/api/v3/feed/topstory/hot-lists/total?limit=50&desktop=true",
                 "https://www.zhihu.com/hot")
    out = []
    for x in d.get("data", []):
        t = x.get("target", {})
        if not t.get("title"):
            continue
        link = re.sub(r"https?://api\.zhihu\.com/questions/(\d+)", r"https://www.zhihu.com/question/\1", t.get("url", ""))
        out.append({"title": t["title"], "link": link or "https://www.zhihu.com/hot", "summary": clip(t.get("excerpt"), 80)})
    return out


def api_cls_telegraph():
    d = get_json("https://www.cls.cn/nodeapi/telegraphList?app=CailianpressWeb&os=web&rn=30&sv=8.4.6",
                 "https://www.cls.cn/telegraph")
    out = []
    for x in d.get("data", {}).get("roll_data", []):
        title = clip(x.get("title") or x.get("brief") or x.get("content"))
        if title:
            out.append({"title": title, "link": f"https://www.cls.cn/detail/{x.get('id')}" if x.get("id")
                        else "https://www.cls.cn/telegraph", "pub": bj_time(x.get("ctime")), "summary": ""})
    return out


def api_sina_7x24():
    d = get_json("https://zhibo.sina.com.cn/api/zhibo/feed?zhibo_id=152&page=1&page_size=30",
                 "https://finance.sina.com.cn/7x24/")
    lst = d.get("result", {}).get("data", {}).get("feed", {}).get("list", [])
    out = []
    for x in lst:
        title = clip(x.get("rich_text"))
        if title:
            out.append({"title": title, "link": x.get("docurl") or "https://finance.sina.com.cn/7x24/",
                        "pub": bj_time(x.get("create_time")), "summary": ""})
    return out


def api_wallstreetcn():
    d = get_json("https://api-one-wscn.awtmt.com/apiv1/content/information-flow?channel=global&accept=article&limit=30",
                 "https://wallstreetcn.com/")
    out = []
    for it in d.get("data", {}).get("items", []):
        r = it.get("resource") or {}
        if r.get("title"):
            out.append({"title": clip(r["title"], 80), "link": r.get("uri") or "https://wallstreetcn.com/",
                        "pub": bj_time(r.get("display_time")), "summary": clip(r.get("content_short"), 80)})
    return out


API_FETCHERS = {
    "weibo_hot": api_weibo_hot, "baidu_hot": api_baidu_hot, "toutiao_hot": api_toutiao_hot,
    "zhihu_hot": api_zhihu_hot, "cls_telegraph": api_cls_telegraph, "sina_7x24": api_sina_7x24,
    "wallstreetcn": api_wallstreetcn,
}


def fetch_api_source(src):
    fn = API_FETCHERS.get(src["type"])
    if not fn:
        return src["name"], [], f"未知类型 {src['type']}"
    try:
        raw = fn()
    except Exception as e:  # noqa: BLE001
        return src["name"], [], f"{type(e).__name__}: {str(e)[:80]}"
    if not raw:
        return src["name"], [], "没有返回条目"
    items = [{"title": x["title"], "link": x["link"], "pub": x.get("pub"), "summary": x.get("summary", ""),
              "source": src["name"], "rank": n} for n, x in enumerate(raw)]
    return src["name"], items, None


# ---------------- 抓取 ----------------
def fetch_source(src):
    if src.get("type"):
        return fetch_api_source(src)
    url = src["url"].replace("{RSSHUB}", (os.getenv("RSSHUB_BASE") or "https://rsshub.app").rstrip("/"))
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=20)
        r.raise_for_status()
        feed = feedparser.parse(r.content)
        if not feed.entries:
            return src["name"], [], "没有返回条目"
    except Exception as e:  # noqa: BLE001
        return src["name"], [], f"{type(e).__name__}: {str(e)[:80]}"

    now = datetime.now(BJ)
    items = []
    for rank, e in enumerate(feed.entries):
        title = re.sub(r"\s+", " ", html.unescape(e.get("title", ""))).strip()
        link = e.get("link", "")
        if not title or not link:
            continue
        ts = e.get("published_parsed") or e.get("updated_parsed")
        pub = datetime.fromtimestamp(time.mktime(ts), timezone.utc).astimezone(BJ) if ts else None
        if pub and now - pub > timedelta(hours=MAX_AGE_HOURS):
            continue
        summary = re.sub(r"<[^>]+>", "", html.unescape(e.get("summary", "")))
        summary = re.sub(r"\s+", " ", summary).strip()[:90]
        items.append({"title": title, "link": link, "pub": pub, "summary": summary,
                      "source": src["name"], "rank": rank})
    return src["name"], items, None


def norm(t):
    return re.sub(r"[\W_]+", "", t.lower())[:40]


def collect(config):
    jobs = [(c["name"], s) for c in config["categories"] for s in c["sources"]]
    results = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        for (cat, src), res in zip(jobs, pool.map(lambda j: fetch_source(j[1]), jobs)):
            results.setdefault(cat, []).append(res)

    per = int(config.get("per_category", 8))
    seen, digest, failed = set(), [], []
    for c in config["categories"]:
        pools = []
        for name, items, err in results.get(c["name"], []):
            if err:
                failed.append(f"{c['name']} / {name}：{err}")
            pools.append(items)
        # 各源轮流取条目，保证一个分类里不被单一媒体刷屏；热榜保持原排名
        picked, i = [], 0
        limit = int(c.get("limit", per))
        while len(picked) < limit and any(i < len(p) for p in pools):
            for p in pools:
                if i < len(p) and len(picked) < limit:
                    key = norm(p[i]["title"])
                    if key and key not in seen:
                        seen.add(key)
                        picked.append(p[i])
            i += 1
        digest.append({"name": c["name"], "items": picked})
    return digest, failed


# ---------------- 渲染 ----------------
def render_html(digest, failed, day):
    total = sum(len(c["items"]) for c in digest)
    nav = "".join(f'<a href="#c{i}">{html.escape(c["name"])}<span>{len(c["items"])}</span></a>'
                  for i, c in enumerate(digest) if c["items"])
    secs = []
    for i, c in enumerate(digest):
        if not c["items"]:
            continue
        lis = []
        for n, it in enumerate(c["items"], 1):
            t = it["pub"].strftime("%H:%M") if it["pub"] else ""
            meta = " · ".join(x for x in [it["source"], t] if x)
            summ = f'<p class="s">{html.escape(it["summary"])}</p>' if it["summary"] and it["summary"] != it["title"] else ""
            lis.append(f'<li><span class="n">{n}</span><div><a href="{html.escape(it["link"])}" target="_blank" rel="noopener">'
                       f'{html.escape(it["title"])}</a>{summ}<p class="m">{html.escape(meta)}</p></div></li>')
        secs.append(f'<section id="c{i}"><h2>{html.escape(c["name"])}</h2><ol>{"".join(lis)}</ol></section>')
    fail_html = ""
    if failed:
        fail_html = "<details class='f'><summary>未能获取的源（%d）</summary><ul>%s</ul></details>" % (
            len(failed), "".join(f"<li>{html.escape(x)}</li>" for x in failed))
    wk = "一二三四五六日"[day.weekday()]
    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>每日头条 {day:%Y-%m-%d}</title><style>
:root{{--bg:#F3F4F1;--card:#fff;--ink:#1D2321;--mut:#6A736F;--line:#DCE0DB;--acc:#1F5E4E;box-sizing:border-box;
padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)}}
@media (prefers-color-scheme:dark){{:root{{--bg:#131715;--card:#1B201E;--ink:#E7ECE9;--mut:#98A29D;--line:#2C3431;--acc:#7CC4AE}}}}
*{{box-sizing:inherit}}body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 "Noto Sans SC","PingFang SC","Microsoft YaHei",sans-serif}}
.w{{max-width:720px;margin:0 auto;padding:24px 16px 48px}}
header h1{{font-size:28px;margin:0;letter-spacing:.02em}}header p{{margin:4px 0 0;color:var(--mut)}}
nav{{position:sticky;top:env(safe-area-inset-top,0px);background:var(--bg);display:flex;gap:6px;overflow-x:auto;padding:12px 0;margin:8px 0 4px;border-bottom:1px solid var(--line)}}
nav a{{flex:none;color:var(--ink);text-decoration:none;border:1px solid var(--line);border-radius:999px;padding:3px 12px;font-size:14px;background:var(--card)}}
nav a span{{color:var(--mut);margin-left:4px;font-size:12px}}
section{{margin-top:22px}}h2{{font-size:18px;margin:0 0 8px;padding-left:10px;border-left:4px solid var(--acc)}}
ol{{list-style:none;margin:0;padding:0;background:var(--card);border:1px solid var(--line);border-radius:10px}}
li{{display:flex;gap:12px;padding:12px 14px;border-top:1px solid var(--line)}}li:first-child{{border-top:none}}
.n{{flex:none;width:22px;color:var(--acc);font-weight:700;text-align:right}}
li a{{color:var(--ink);text-decoration:none;font-weight:500}}li a:hover{{color:var(--acc);text-decoration:underline}}
.s{{margin:2px 0 0;color:var(--mut);font-size:13px}}.m{{margin:2px 0 0;color:var(--mut);font-size:12px}}
.f{{margin-top:28px;color:var(--mut);font-size:13px}}footer{{margin-top:24px;color:var(--mut);font-size:12px}}
</style></head><body><div class="w">
<header><h1>每日头条</h1><p>{day:%Y年%m月%d日} 星期{wk} · 共 {total} 条 · 生成于 {day:%H:%M}</p></header>
<nav>{nav}</nav>{"".join(secs)}{fail_html}
<footer>标题与链接来自各媒体公开 RSS，点击查看原文。</footer></div></body></html>"""


def render_markdown(digest, day, limit=5, url=None):
    lines = [f"## 每日头条 {day:%m月%d日}"]
    for c in digest:
        if not c["items"]:
            continue
        lines.append(f"\n**{c['name']}**")
        for n, it in enumerate(c["items"][:limit], 1):
            lines.append(f"{n}. [{it['title']}]({it['link']})")
    if url:
        lines.append(f"\n[查看完整日报]({url})")
    return "\n".join(lines)


def render_push_html(digest, day, url=None, max_chars=18000):
    """推送用的精简版：只保留标题和链接，自动控制在 PushPlus 2 万字上限内。"""
    def build(limit, with_src):
        parts = [f'<p style="color:#666;font-size:13px">{day:%Y年%m月%d日} 每日头条</p>']
        for c in digest:
            if not c["items"]:
                continue
            parts.append(f'<h4 style="margin:14px 0 6px;border-left:3px solid #1F5E4E;padding-left:6px">{html.escape(c["name"])}</h4>')
            for n, it in enumerate(c["items"][:limit], 1):
                src = f' <span style="color:#999;font-size:12px">{html.escape(it["source"])}</span>' if with_src else ""
                parts.append(f'<p style="margin:4px 0">{n}. <a href="{html.escape(it["link"])}">{html.escape(it["title"])}</a>{src}</p>')
        if url:
            parts.append(f'<p style="margin-top:16px"><a href="{html.escape(url)}">查看完整日报网页 &gt;</a></p>')
        return "".join(parts)

    for limit, with_src in ((99, True), (99, False), (8, False), (6, False), (5, False), (3, False)):
        body = build(limit, with_src)
        if len(body) <= max_chars:
            return body
    return body[:max_chars]


# ---------------- 推送 ----------------
def push_all(digest, page, day):
    title = f"每日头条 {day:%m月%d日}"
    url = os.getenv("REPORT_URL")
    md = render_markdown(digest, day, url=url)
    sent = []

    def post(name, fn):
        try:
            fn()
            sent.append(name)
        except Exception as e:  # noqa: BLE001
            print(f"[推送失败] {name}: {e}", file=sys.stderr)

    if tok := os.getenv("PUSHPLUS_TOKEN"):
        def pushplus():
            r = requests.post("https://www.pushplus.plus/send", timeout=20, json={
                "token": tok, "title": title, "content": render_push_html(digest, day, url), "template": "html"})
            r.raise_for_status()
            res = r.json()
            print(f"PushPlus 返回：{res}")
            if res.get("code") != 200:
                raise RuntimeError(f"code={res.get('code')} msg={res.get('msg')}")
        post("PushPlus", pushplus)
    if key := os.getenv("SERVERCHAN_KEY"):
        def serverchan():
            r = requests.post(f"https://sctapi.ftqq.com/{key}.send", timeout=20, data={"title": title, "desp": md})
            r.raise_for_status()
            res = r.json()
            if res.get("code") not in (0, None):
                raise RuntimeError(f"code={res.get('code')} msg={res.get('message')}")
        post("Server酱", serverchan)
    if hook := os.getenv("WECOM_WEBHOOK"):
        body = render_markdown(digest, day, limit=3, url=url).encode()[:4000].decode(errors="ignore")
        post("企业微信", lambda: requests.post(hook, timeout=20, json={
            "msgtype": "markdown", "markdown": {"content": body}}).raise_for_status())
    if hook := os.getenv("DINGTALK_WEBHOOK"):
        post("钉钉", lambda: requests.post(hook, timeout=20, json={
            "msgtype": "markdown", "markdown": {"title": title, "text": md}}).raise_for_status())
    if (bot := os.getenv("TELEGRAM_BOT_TOKEN")) and (chat := os.getenv("TELEGRAM_CHAT_ID")):
        parts = [f"<b>{html.escape(title)}</b>"]
        for c in digest:
            if c["items"]:
                parts.append(f"\n<b>{html.escape(c['name'])}</b>")
                parts += [f'{n}. <a href="{html.escape(i["link"])}">{html.escape(i["title"])}</a>'
                          for n, i in enumerate(c["items"][:5], 1)]
        if url:
            parts.append(f'\n<a href="{html.escape(url)}">查看完整日报</a>')
        post("Telegram", lambda: requests.post(f"https://api.telegram.org/bot{bot}/sendMessage", timeout=20, json={
            "chat_id": chat, "text": "\n".join(parts)[:4000], "parse_mode": "HTML",
            "disable_web_page_preview": True}).raise_for_status())
    if os.getenv("SMTP_HOST") and os.getenv("MAIL_TO"):
        def mail():
            msg = MIMEText(page, "html", "utf-8")
            msg["Subject"] = Header(title, "utf-8")
            msg["From"] = os.getenv("SMTP_USER")
            msg["To"] = os.getenv("MAIL_TO")
            port = int(os.getenv("SMTP_PORT", "465"))
            cls = smtplib.SMTP_SSL if port == 465 else smtplib.SMTP
            with cls(os.getenv("SMTP_HOST"), port, timeout=30) as s:
                if port != 465:
                    s.starttls()
                s.login(os.getenv("SMTP_USER"), os.getenv("SMTP_PASS"))
                s.sendmail(os.getenv("SMTP_USER"), [x.strip() for x in os.getenv("MAIL_TO").split(",")], msg.as_string())
        post("邮件", mail)
    return sent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-push", action="store_true", help="只生成日报，不推送")
    ap.add_argument("--config", default=str(ROOT / "sources.json"))
    args = ap.parse_args()

    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    day = datetime.now(BJ)
    digest, failed = collect(config)
    page = render_html(digest, failed, day)

    out = ROOT / "output"
    out.mkdir(exist_ok=True)
    (out / f"{day:%Y-%m-%d}.html").write_text(page, encoding="utf-8")
    (out / "index.html").write_text(page, encoding="utf-8")
    total = sum(len(c["items"]) for c in digest)
    print(f"共抓取 {total} 条，失败源 {len(failed)} 个，日报已写入 {out}")
    for f in failed:
        print("  -", f)

    if total == 0:
        print("没有抓到任何内容，跳过推送。检查网络或 sources.json 里的源。", file=sys.stderr)
        sys.exit(1)
    if not args.no_push:
        sent = push_all(digest, page, day)
        print("已推送：" + ("、".join(sent) if sent else "无（未配置渠道或全部推送失败，见上方报错）"))
        configured = any(os.getenv(k) for k in ("PUSHPLUS_TOKEN", "SERVERCHAN_KEY", "WECOM_WEBHOOK",
                                                 "DINGTALK_WEBHOOK", "TELEGRAM_BOT_TOKEN", "SMTP_HOST"))
        if configured and not sent:
            # 不中断任务，保证网页日报照常更新；在 Actions 页面标出错误提示
            print("::error::所有推送渠道都失败了，请查看“抓取并推送”步骤的日志")


if __name__ == "__main__":
    main()
