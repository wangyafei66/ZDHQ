#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
蒙西招标监测器 (Inner Mongolia West Tender Monitor)
==================================================
轮询配置的超市/商铺招租站点，发现新公告后通过 WorkBuddy 平台自动通知用户（无需任何配置、免费），
并生成本地看板快照 tenders.html 与每日简报 daily_report.txt。

用法:
  python monitor.py            # 正常运行（首次运行会自动"播种"，不刷屏）
  python monitor.py --test     # 单次运行并打印明细（不推送）
  python monitor.py --no-push  # 运行但不推送（仅入库+看板）
  python monitor.py --init     # 强制重新播种（清空并重置已见记录）

依赖: requests, beautifulsoup4   (见 requirements.txt)
"""

import json
import sqlite3
import os
import sys
import re
import datetime
import urllib.parse
from urllib.parse import urljoin

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    sys.stderr.write("缺少依赖，请先运行: pip install -r requirements.txt\n")
    sys.exit(2)

import warnings
warnings.filterwarnings("ignore")
try:
    import urllib3
    urllib3.disable_warnings()
except Exception:
    pass

# Playwright 为可选依赖：本地/VPS 装了 chromium 后可渲染 JS 站点（蒙西多数平台为 JS 单页）。
# 若未安装，脚本自动降级为 requests 模式（仅能抓服务端渲染的站点，如全国平台）。
try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except Exception:
    HAS_PLAYWRIGHT = False

_browser = None
def get_browser():
    """懒加载一个无头浏览器（进程内单例）。"""
    global _browser
    if not HAS_PLAYWRIGHT:
        return None
    if _browser is None:
        try:
            p = sync_playwright().start()
            _browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        except Exception as e:
            print(f"  [提示] Playwright 浏览器启动失败（通常未安装 chromium）: {e}")
            return None
    return _browser

def render_js(url):
    """用无头浏览器渲染 JS 站点并返回 HTML；不可用返回空字符串。"""
    b = get_browser()
    if not b:
        return ""
    try:
        page = b.new_page()
        page.goto(url, timeout=30000, wait_until="domcontentloaded")
        page.wait_for_timeout(4000)  # 等待列表接口返回（部分平台异步加载较慢）
        html = page.content()
        page.close()
        return html
    except Exception as e:
        print(f"  [JS渲染异常] {url}: {e}")
        return ""

if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
SITES_PATH = os.path.join(APP_DIR, "sites.json")
DB_PATH = os.path.join(APP_DIR, "tenders.db")

DATE_RE = re.compile(
    r"(20\d{2})[-/年.]?\s*(\d{1,2})[-/月.]?\s*(\d{1,2})"
)

# ----------------------------------------------------------------------------
# 配置加载
# ----------------------------------------------------------------------------
def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

CONFIG = load_json(CONFIG_PATH)
SITES = load_json(SITES_PATH)

ACTIONS = CONFIG.get("actions", ["招标", "采购", "中标"])
CATEGORIES = CONFIG.get("categories", [])
INSTITUTIONS = CONFIG.get("institutions", [])
EXCLUDE = CONFIG.get("exclude_keywords", [])
REGION_KW = CONFIG.get("region_keywords", [])
MAX_ANCHORS = int(CONFIG.get("max_anchors_per_site", 150))
TIMEOUT = int(CONFIG.get("request_timeout", 15))
LOOKBACK = int(CONFIG.get("lookback_days", 7))
UA = CONFIG.get("user_agent", "Mozilla/5.0")
PUSH_TOKEN = CONFIG.get("pushplus_token", "").strip()
PUSH_TOPIC = CONFIG.get("pushplus_topic", "").strip()
DASHBOARD = os.path.join(APP_DIR, CONFIG.get("dashboard_path", "tenders.html"))
WEB_DIR = os.path.join(APP_DIR, "web")
WEB_INDEX = os.path.join(WEB_DIR, "index.html")


# ----------------------------------------------------------------------------
# 数据库
# ----------------------------------------------------------------------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS tenders (
               id TEXT PRIMARY KEY,
               site TEXT,
               region TEXT,
               title TEXT,
               link TEXT,
               date TEXT,
               tags TEXT,
               first_seen TEXT
           )"""
    )
    conn.commit()
    return conn


def db_count(conn):
    return conn.execute("SELECT COUNT(*) FROM tenders").fetchone()[0]


def is_seen(conn, tid):
    return conn.execute("SELECT 1 FROM tenders WHERE id=?", (tid,)).fetchone() is not None


def save(conn, rows):
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    for r in rows:
        conn.execute(
            "INSERT OR IGNORE INTO tenders (id,site,region,title,link,date,tags,first_seen) VALUES (?,?,?,?,?,?,?,?)",
            (r["id"], r["site"], r["region"], r["title"], r["link"], r["date"], r.get("tags", ""), now),
        )
    conn.commit()


def recent(conn, limit=300):
    cur = conn.execute(
        "SELECT site,region,title,link,date,first_seen FROM tenders ORDER BY first_seen DESC, date DESC LIMIT ?",
        (limit,),
    )
    return cur.fetchall()


# ----------------------------------------------------------------------------
# 抓取与解析
# ----------------------------------------------------------------------------
def fetch(url):
    headers = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}
    # verify=False: 部分政府站点证书链不全，避免 SSL 报错
    resp = requests.get(url, headers=headers, timeout=TIMEOUT, verify=False)
    resp.encoding = resp.apparent_encoding or "utf-8"
    return resp.text


def detect_date(text):
    m = DATE_RE.search(text)
    if not m:
        return ""
    y, mo, d = m.group(1), int(m.group(2)), int(m.group(3))
    if 1 <= mo <= 12 and 1 <= d <= 31:
        return f"{y}-{mo:02d}-{d:02d}"
    return ""


def make_id(site, key):
    import hashlib
    return hashlib.md5((site + "\u0001" + key).encode("utf-8")).hexdigest()


def parse_site(site):
    """返回该站点解析出的候选公告列表（已去站内重复）。"""
    out = []
    seen_links = set()
    mode = site.get("mode", "html")
    try:
        if mode == "js":
            html = render_js(site["url"])
            if not html:
                html = render_js(site["url"])  # 偶发列表未加载，重试一次
            if not html:
                print(f"  [跳过] {site['name']}：JS渲染不可用（需本地安装Playwright），本轮跳过")
                return out
        else:
            html = fetch(site["url"])
    except Exception as e:
        print(f"  [跳过] {site['name']} 抓取失败: {e}")
        return out

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True)
        if not text or len(text) < 6:
            continue
        href = a["href"].strip()
        if not href or href.lower().startswith(("#", "mailto:")):
            continue
        # ejy365 等平台：招租链接常是 javascript:void(0) 弹窗，真实 ID 在 data-id / data-infoid
        if href.lower().startswith("javascript:"):
            did = (a.get("data-id") or a.get("data-infoid") or "").strip()
            if did:
                href = ("https://www.ejy365.com/info/" + did) if did.lower().startswith("ejy") \
                    else ("https://nmgcqjy.ejy365.com/home/Edetail?infoid=" + did)
            else:
                oc = a.get("onclick", "") or ""
                m = re.search(r'infoid\s*[=:]\s*[\'"]([^\'"]+)[\'"]', oc, re.I)
                href = ("https://nmgcqjy.ejy365.com/home/Edetail?infoid=" + m.group(1)) if m \
                    else ""
            if not href:
                continue

        # 动作 + 经营/商铺类 + (蒙西 或 学校/医院/火车站) 三重过滤
        has_action = any(k in text for k in ACTIONS)
        if not has_action:
            continue
        if any(k in text for k in EXCLUDE):
            continue
        parent_text = a.parent.get_text(" ", strip=True) if a.parent else ""
        cat_text = text + " " + parent_text
        has_business = (not CATEGORIES) or any(c in cat_text for c in CATEGORIES)
        if not has_business:
            continue
        has_inst = any(c in cat_text for c in INSTITUTIONS)
        has_region = (not REGION_KW) or any(k in cat_text for k in REGION_KW)
        # 命中条件：属于蒙西本地铺面招租，或属于学校/医院/火车站内部的超市/商铺招标
        if not (has_region or has_inst):
            continue

        link = urljoin(site["url"], href)
        if link in seen_links:
            continue
        seen_links.add(link)

        title = re.sub(r"\s+", " ", text).strip()
        date = detect_date(cat_text)
        out.append({
            "site": site["name"],
            "region": site.get("region", ""),
            "title": title,
            "link": link,
            "date": date,
            "tags": ",".join(site.get("tags", [])),
            "id": make_id(site["name"], link if link else title),
        })
        if len(out) >= MAX_ANCHORS:
            break
    return out


# ----------------------------------------------------------------------------
# 推送
# ----------------------------------------------------------------------------
def pushplus_send(title, content):
    if not PUSH_TOKEN:
        print("  [提示] 未配置 pushplus_token，跳过微信推送。请在 config.json 填写。")
        return False
    payload = {
        "token": PUSH_TOKEN,
        "title": title,
        "content": content,
        "template": "markdown",
    }
    if PUSH_TOPIC:
        payload["topic"] = PUSH_TOPIC
    try:
        r = requests.post("https://www.pushplus.plus/send", json=payload, timeout=TIMEOUT)
        data = r.json()
        if data.get("code") == 200:
            print("  [OK] 微信推送成功")
            return True
        print(f"  [失败] PushPlus 返回: {data}")
    except Exception as e:
        print(f"  [失败] 推送异常: {e}")
    return False


def build_digest(new_rows):
    lines = ["### 蒙西新招标监测", f"> 共发现 **{len(new_rows)}** 条新公告（{datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}）", ""]
    for r in new_rows:
        d = r["date"] or "日期未知"
        lines.append(f"- **[{r['region']}]** {d} 　[{r['title']}]({r['link']})  *(来源:{r['site']})*")
    lines.append("")
    lines.append("> 由 WorkBuddy 招标监测器自动推送")
    return "\n".join(lines)


def write_daily_report(new_rows):
    """把当日结果写成纯文本简报，便于自动化抓取并作为每日通知投递给用户。"""
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    if new_rows:
        lines = [f"蒙西超市/商铺招租监测日报  {today}", "=" * 42, ""]
        for r in new_rows:
            lines.append(f"[{r['region']}] {r['date'] or '日期未知'}")
            lines.append(f"  {r['title']}")
            lines.append(f"  {r['link']}")
            lines.append("")
        lines.append(f"共 {len(new_rows)} 条新公告")
    else:
        lines = [f"蒙西超市/商铺招租监测日报  {today}", "=" * 42, "", "今日无新超市/商铺招租公告。"]
    path = os.path.join(APP_DIR, "daily_report.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path


# ----------------------------------------------------------------------------
# 看板（手机友好、自包含、可部署为公网网址）
# ----------------------------------------------------------------------------
def build_dashboard(conn):
    rows = recent(conn, 500)
    # 统计各地区数量，生成筛选标签
    from collections import Counter, OrderedDict
    cnt = Counter(r[1] or "其他" for r in rows)
    regions = list(OrderedDict.fromkeys([r[1] or "其他" for r in rows]))

    cards = []
    for site, region, title, link, date, first_seen in rows:
        region = region or "其他"
        d = date or "日期未知"
        cards.append(
            f'<div class="card" data-region="{region}">'
            f'<div class="top"><span class="badge">{region}</span>'
            f'<span class="date">{d}</span></div>'
            f'<a class="title" href="{link}" target="_blank" rel="noopener">{title}</a>'
            f'<div class="meta">来源：{site}　·　发现于 {first_seen}</div>'
            f'</div>'
        )
    chips = "".join(
        f'<button class="chip" data-region="{r}">{r}<i>{cnt.get(r,0)}</i></button>'
        for r in regions
    )
    updated = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>蒙西超市/商铺招租监测看板</title>
<style>
 *{{box-sizing:border-box}}
 body{{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;
   background:#f2f4f7;color:#1f2329;margin:0;padding:0 0 30px}}
 .head{{position:sticky;top:0;background:#1f6feb;color:#fff;padding:14px 16px;z-index:10}}
 .head h1{{font-size:18px;margin:0 0 2px;font-weight:600}}
 .head .sub{{font-size:12px;opacity:.85}}
 .wrap{{padding:12px}}
 .bar{{display:flex;flex-wrap:wrap;gap:8px;margin:6px 0 12px}}
 .chip{{border:1px solid #d4dbe5;background:#fff;color:#334;font-size:13px;
   padding:5px 11px;border-radius:16px;cursor:pointer;line-height:1.4}}
 .chip i{{font-style:normal;color:#1f6feb;font-weight:700;margin-left:4px}}
 .chip.on{{background:#1f6feb;color:#fff;border-color:#1f6feb}}
 .chip.on i{{color:#fff}}
 input.sch{{flex:1;min-width:140px;border:1px solid #d4dbe5;border-radius:16px;
   padding:7px 13px;font-size:13px;outline:none}}
 .card{{background:#fff;border-radius:12px;padding:12px 13px;margin-bottom:10px;
   box-shadow:0 1px 3px rgba(0,0,0,.05)}}
 .card .top{{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px}}
 .badge{{background:#eaf2ff;color:#1f6feb;font-size:12px;padding:2px 9px;border-radius:10px;font-weight:600}}
 .date{{color:#8a93a0;font-size:12px}}
 .title{{display:block;color:#1f2937;font-size:15px;line-height:1.5;text-decoration:none;font-weight:500}}
 .meta{{color:#9aa3af;font-size:12px;margin-top:6px}}
 .empty{{text-align:center;color:#9aa3af;padding:40px 0;font-size:14px}}
 .foot{{text-align:center;color:#aab2bd;font-size:12px;margin-top:10px}}
 .actions{{margin-top:10px}}
 .btn{{border:none;border-radius:18px;padding:8px 18px;font-size:14px;font-weight:600;
   background:#fff;color:#1f6feb;cursor:pointer}}
 .toast{{position:fixed;left:50%;bottom:30px;transform:translateX(-50%) translateY(20px);
   background:#111;color:#fff;padding:10px 16px;border-radius:10px;font-size:13px;
   opacity:0;pointer-events:none;transition:.25s;z-index:50}}
 .toast.show{{opacity:1;transform:translateX(-50%) translateY(0)}}
</style></head><body>
<div class="head"><h1>蒙西超市/商铺招租监测</h1>
<div class="sub">监测 {len(SITES)} 个平台 · 已收录 {db_count(conn)} 条 · 更新于 {updated}</div>
<div class="actions"><button class="btn" id="mfbtn">📲 手动抓取</button></div></div>
<div class="wrap">
 <div class="bar">
   <button class="chip on" data-region="__all">全部<i>{len(rows)}</i></button>
   {chips}
 </div>
 <input class="sch" id="kw" placeholder="搜索关键词，如 超市 / 底店 / 招租…">
 <div id="list">{''.join(cards)}</div>
 <div class="foot">数据由 WorkBuddy 招标监测器每日自动更新 · 点标题可跳转原公告<br>
想立刻刷新？点上方【手动抓取】复制指令发我，或电脑双击 TenderMonitor.exe → 手动抓取</div>
</div>
<script>
 var chips=document.querySelectorAll('.chip'),kw=document.getElementById('kw'),
     list=document.getElementById('list'),cur='__all';
 function apply(){{
   var k=kw.value.trim(),n=0;
   list.querySelectorAll('.card').forEach(function(c){{
     var okR=(cur==='__all'||c.dataset.region===cur),
         okK=(!k||c.textContent.indexOf(k)>=0);
     c.style.display=(okR&&okK)?'':'none'; if(okR&&okK)n++;
   }});
   var e=document.getElementById('emp'); if(e)e.remove();
   if(n===0){{var d=document.createElement('div');d.className='empty';
     d.id='emp';d.textContent='没有匹配的招标';list.appendChild(d);}}
 }}
 chips.forEach(function(c){{c.onclick=function(){{
   chips.forEach(function(x)x.classList.remove('on'));c.classList.add('on');
   cur=c.dataset.region;apply();}};}});
 kw.oninput=apply;
 var mf=document.getElementById('mfbtn'),toast=document.getElementById('toast');
 if(mf){{mf.onclick=function(){{
   var t='请帮我抓一次最新蒙西超市/商铺招租招标，并刷新看板';
   var done=function(){{toast.className='toast show';setTimeout(function(){{toast.className='toast';}},2800);}};
   if(navigator.clipboard&&navigator.clipboard.writeText){{navigator.clipboard.writeText(t).then(done,done);}}else{{done();}}
 }};}}
</script>
<div id="toast" class="toast">已复制刷新指令，去微信发给我即可 ✅</div>
</body></html>"""
    with open(DASHBOARD, "w", encoding="utf-8") as f:
        f.write(html)
    os.makedirs(WEB_DIR, exist_ok=True)
    with open(WEB_INDEX, "w", encoding="utf-8") as f:
        f.write(html)


# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------
def main():
    import argparse
    p = argparse.ArgumentParser(description="蒙西招标监测器")
    p.add_argument("--test", action="store_true", help="单次运行并打印明细，不推送")
    p.add_argument("--no-push", action="store_true", help="运行但不推送")
    p.add_argument("--init", action="store_true", help="清空并重新播种")
    args = p.parse_args()

    conn = init_db()
    if args.init:
        conn.execute("DELETE FROM tenders")
        conn.commit()

    print(f"[开始] 监测 {len(SITES)} 个站点 ...")
    all_candidates = []
    for site in SITES:
        rows = parse_site(site)
        print(f"  - {site['name']}: 命中 {len(rows)} 条")
        all_candidates.extend(rows)

    # 跨站点按标题去重（同一条可能在多个平台发布）
    seen_titles = set()
    unique = []
    for r in all_candidates:
        if r["title"] in seen_titles:
            continue
        seen_titles.add(r["title"])
        unique.append(r)

    first_run = db_count(conn) == 0 and not args.init

    # 判定新公告
    new_rows = [r for r in unique if not is_seen(conn, r["id"])]

    if first_run:
        # 首次运行：只播种，不刷屏推送
        save(conn, unique)
        print(f"[播种] 首次运行，已收录 {len(unique)} 条历史公告作为基线，后续新发布将推送。")
        title = "招标监测已启动"
        content = f"监控已覆盖 {len(SITES)} 个蒙西站点，已收录 {len(unique)} 条超市/商铺招租类历史公告作为基线。\n后续有新发布的超市、便利店、商铺招租等招标，将自动推送本条微信。"
    else:
        save(conn, new_rows)
        if new_rows:
            print(f"[发现] {len(new_rows)} 条新公告")
            print("=== 今日新超市/商铺招租 ===")
            for r in new_rows:
                print(f"· [{r['region']}] {r['date'] or '日期未知'}  {r['title']}")
                print(f"  {r['link']}")
            write_daily_report(new_rows)
            if PUSH_TOKEN and not args.no_push and not args.test:
                pushplus_send(f"蒙西新招标 {len(new_rows)} 条", build_digest(new_rows))
            else:
                print("  [平台通知] 新公告已记录，将由 WorkBuddy 每天自动发给你（无需微信 token）")
        else:
            print("[平静] 本轮无新公告")
            write_daily_report([])

    build_dashboard(conn)
    print(f"[看板] 已更新 {DASHBOARD}（已收录 {db_count(conn)} 条）")


if __name__ == "__main__":
    main()
