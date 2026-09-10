# -*- coding: utf-8 -*-
"""
蒙西超市/商铺招租监测 · 腾讯云通用版
==================================
同一份代码既可在【本地】运行（若有 Playwright 则连剑鱼也能抓全），
也可部署到【腾讯云函数 SCF】定时运行，结果上传到腾讯云 COS 静态看板。

抓取策略：
  - mode="html" 的站点（各高校官网）直接用 requests 抓。
  - mode="js" 的站点：先试 requests，拿不到列表且有 Playwright 时再用浏览器渲染。
    因此：云端 SCF（未装浏览器）能抓到产权/高校/政府采购等，剑鱼类纯 JS 站由
    你本地（国内 IP + 已装浏览器）运行本脚本补抓并覆盖同一看板。

配置（环境变量，均可选）：
  COS_SECRET_ID / COS_SECRET_KEY / COS_BUCKET / COS_REGION
  配齐后，每次运行会把 index.html / daily_report.txt / tenders.db 上传到 COS。
"""
import os
import re
import sys
import json
import sqlite3
import datetime
import urllib3
urllib3.disable_warnings()

import requests
from bs4 import BeautifulSoup

# ---- Playwright 可选（本地有则抓全，SCF 无则降级）----
try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except Exception:
    HAS_PLAYWRIGHT = False

APP_DIR = "/tmp" if os.environ.get("TENCENTCLOUD_RUNENV") else os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(APP_DIR, "tenders.db")
WEB_DIR = os.path.join(APP_DIR, "web")
WEB_INDEX = os.path.join(WEB_DIR, "index.html")
DASHBOARD = os.path.join(APP_DIR, "tenders.html")
DAILY_REPORT = os.path.join(APP_DIR, "daily_report.txt")

DATE_RE = re.compile(r"(20\d{2})[-/年.]?\s*(\d{1,2})[-/月.]?\s*(\d{1,2})")

# 站点清单（与 sites.json 对齐；js 站在云端降级，本地补齐）
SITES = [
    {"name": "包头产权交易服务平台（蒙西·包头商铺招租）", "region": "包头",
     "url": "https://www.btcqjy.com/", "mode": "js",
     "tags": ["产权", "商铺招租", "包头"]},
    {"name": "乌海产权交易市场（蒙西·乌海商铺招租）", "region": "乌海",
     "url": "https://whcqjy.ejy365.com/", "mode": "js",
     "tags": ["产权", "商铺招租", "乌海"]},
    {"name": "内蒙古产权交易中心·资产招租聚合", "region": "内蒙古",
     "url": "https://nmgcqjy.ejy365.com/LendLease/index", "mode": "js",
     "tags": ["产权", "资产招租", "全区", "高校", "中学", "学校", "医院", "巴彦淖尔", "鄂尔多斯", "呼和浩特"]},
    {"name": "内蒙古自治区公共资源交易网·交易信息", "region": "内蒙古",
     "url": "https://ggzyjy.nmg.gov.cn/jyxx/", "mode": "js",
     "tags": ["公共资源", "学校", "医院", "综合"]},
    {"name": "内蒙古自治区政府采购网", "region": "内蒙古",
     "url": "https://www.ccgp-neimenggu.gov.cn/", "mode": "js",
     "tags": ["政府采购", "学校", "中职", "医院", "经营权", "招租"]},
    {"name": "剑鱼标讯·巴彦淖尔市", "region": "巴彦淖尔",
     "url": "https://wx.jianyu360.cn/list/city/NMG_BYZE.html", "mode": "js",
     "tags": ["剑鱼", "巴彦淖尔", "临河", "学校", "超市承包", "综合"]},
    {"name": "剑鱼标讯·巴彦淖尔市临河区", "region": "巴彦淖尔",
     "url": "https://wx.jianyu360.cn/list/city/NMG_BYZE_LHQ.html", "mode": "js",
     "tags": ["剑鱼", "临河区", "学校", "超市承包", "招租"]},
    {"name": "内蒙古工业大学后勤管理处", "region": "呼和浩特",
     "url": "https://hqglc.imut.edu.cn/xwgg/zbgg.htm", "mode": "html",
     "tags": ["高校", "后勤", "超市", "商铺", "食堂档口", "金川"]},
    {"name": "内蒙古工业大学招标采购处", "region": "呼和浩特",
     "url": "https://zbcg.imut.edu.cn/", "mode": "html",
     "tags": ["高校", "招标采购", "商铺", "食堂档口", "经营权"]},
    {"name": "内蒙古大学招标采购中心", "region": "呼和浩特",
     "url": "https://zbcg.imu.edu.cn/xwtz.htm", "mode": "html",
     "tags": ["高校", "招标采购", "呼和浩特"]},
    {"name": "内蒙古大学后勤保障处", "region": "呼和浩特",
     "url": "https://hqc.imu.edu.cn/tzgg.htm", "mode": "html",
     "tags": ["高校", "后勤", "呼和浩特"]},
    {"name": "内蒙古农业大学国有资产管理处", "region": "呼和浩特",
     "url": "https://gzc.imau.edu.cn/", "mode": "html",
     "tags": ["高校", "国资", "校园超市", "经营用房", "呼和浩特"]},
    {"name": "内蒙古农业大学后勤管理处", "region": "呼和浩特",
     "url": "https://hqc.imau.edu.cn/tzgg.htm", "mode": "html",
     "tags": ["高校", "后勤", "食堂档口", "呼和浩特"]},
    {"name": "内蒙古师范大学后勤管理处", "region": "呼和浩特",
     "url": "https://hqfw.imnu.edu.cn/tzgg.htm", "mode": "html",
     "tags": ["高校", "后勤", "呼和浩特"]},
    # 国信招投标公共服务平台：静态搜索页，requests 可直接抓（city=30 为内蒙古）
    # 用于替代/补充剑鱼（剑鱼需浏览器，云端抓不到）
    {"name": "国信招标·内蒙古超市", "region": "内蒙古",
     "url": "https://www.gxzbcg.cn/search/keyword/超市/city/30/datetime/0/page/1", "mode": "html",
     "tags": ["国信招标", "内蒙古", "超市", "学校", "招租"]},
    {"name": "国信招标·内蒙古便利店", "region": "内蒙古",
     "url": "https://www.gxzbcg.cn/search/keyword/便利店/city/30/datetime/0/page/1", "mode": "html",
     "tags": ["国信招标", "内蒙古", "便利店", "学校"]},
    {"name": "国信招标·内蒙古招租", "region": "内蒙古",
     "url": "https://www.gxzbcg.cn/search/keyword/招租/city/30/datetime/0/page/1", "mode": "html",
     "tags": ["国信招标", "内蒙古", "招租", "商铺"]},
    {"name": "国信招标·内蒙古承包", "region": "内蒙古",
     "url": "https://www.gxzbcg.cn/search/keyword/承包/city/30/datetime/0/page/1", "mode": "html",
     "tags": ["国信招标", "内蒙古", "承包", "经营"]},
    {"name": "国信招标·内蒙古档口", "region": "内蒙古",
     "url": "https://www.gxzbcg.cn/search/keyword/档口/city/30/datetime/0/page/1", "mode": "html",
     "tags": ["国信招标", "内蒙古", "档口", "食堂"]},
    # 内蒙古招投标网（招标投标公共服务平台）：iVX 单页应用，接口返回密文，
    # 只能用浏览器打开后点击"招标信息"栏目再取内容 → 云端无浏览器会降级跳过，本地可抓。
    {"name": "内蒙古招投标网·招标信息", "region": "内蒙古",
     "url": "https://www.nmgztb.com.cn/", "mode": "js", "click_text": "招标信息",
     "tags": ["内蒙古招投标网", "招标信息", "学校", "食堂", "超市", "招租"]},
]

# 关键词过滤（与本地 config.json 对齐）
ACTIONS = ["招标", "采购", "中标", "招租", "出租", "承包", "竞价", "挂牌"]
CATEGORIES = ["超市", "便利店", "小卖部", "商铺", "底店", "门店", "档口", "经营", "商业", "租赁", "招租", "食堂", "餐厅"]
INSTITUTIONS = ["大学", "学院", "学校", "中学", "高中", "初中", "小学", "职高", "职校", "职业学校", "附中", "附校", "一中", "二中", "三中", "医院", "火车站", "高铁站", "校区"]
EXCLUDE = ["工程", "施工", "监理", "设计", "勘察", "测绘", "软件", "硬件", "教材", "物业外包"]
REGION_KW = ["蒙西", "乌海", "海勃湾", "包头", "巴彦淖尔", "临河", "五原", "鄂尔多斯", "呼和浩特", "内蒙", "阿拉善", "磴口", "杭锦后旗"]
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
TIMEOUT = 20
MAX_ANCHORS = 120

_browser = None
def get_browser():
    global _browser
    if not HAS_PLAYWRIGHT:
        return None
    if _browser is None:
        try:
            p = sync_playwright().start()
            _browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        except Exception as e:
            print("  [提示] 浏览器启动失败:", e)
            return None
    return _browser

def render_js(url, click_text=None):
    """渲染 JS 页面；若传 click_text 则渲染后点击该文字再取 HTML。
    用于 SPA 站点：切栏目时 URL 不变，必须模拟点击（如内蒙古招投标网）。"""
    b = get_browser()
    if not b:
        return ""
    try:
        page = b.new_page()
        page.goto(url, timeout=30000, wait_until="domcontentloaded")
        page.wait_for_timeout(4000)
        if click_text:
            try:
                page.get_by_text(click_text, exact=True).first.click(timeout=6000)
                page.wait_for_timeout(4000)
            except Exception as e:
                print("  [点击异常]", click_text, repr(e)[:80])
        html = page.content()
        page.close()
        return html
    except Exception as e:
        print("  [JS渲染异常]", url, e)
        return ""

def fetch(url, retry=2):
    """带完整浏览器请求头 + 重试；部分站点（如 gxzbcg）会拒绝头不全的请求。"""
    import time as _t
    from urllib.parse import urlparse as _up
    host = _up(url).netloc
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Referer": "https://%s/" % host,
    }
    last = None
    for i in range(retry + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=TIMEOUT, verify=False)
            resp.encoding = resp.apparent_encoding or "utf-8"
            return resp.text
        except Exception as e:
            last = e
            _t.sleep(2)
    raise last

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
    out, seen_links = [], set()
    mode = site.get("mode", "html")
    html = ""
    try:
        if mode == "js":
            html = fetch(site["url"])
            # 拿到有效链接就用；否则若本地有浏览器再渲染
            if len(BeautifulSoup(html, "html.parser").find_all("a", href=True)) < 3 and HAS_PLAYWRIGHT:
                html2 = render_js(site["url"], site.get("click_text"))
                if html2:
                    html = html2
            if not html:
                print(f"  [降级跳过] {site['name']}：云端无浏览器且 requests 未取到数据")
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
        if href.lower().startswith("javascript:"):
            did = (a.get("data-id") or a.get("data-infoid") or "").strip()
            if did:
                href = ("https://www.ejy365.com/info/" + did) if did.lower().startswith("ejy") \
                    else ("https://nmgcqjy.ejy365.com/home/Edetail?infoid=" + did)
            else:
                oc = a.get("onclick", "") or ""
                m = re.search(r'infoid\s*[=:]\s*[\'"]([^\'"]+)[\'"]', oc, re.I)
                href = ("https://nmgcqjy.ejy365.com/home/Edetail?infoid=" + m.group(1)) if m else ""
            if not href:
                continue
        has_action = any(k in text for k in ACTIONS)
        if not has_action:
            continue
        if any(k in text for k in EXCLUDE):
            continue
        li = a.find_parent("li")
        parent_text = (li.get_text(" ", strip=True) if li
                       else (a.parent.get_text(" ", strip=True) if a.parent else ""))
        cat_text = text + " " + parent_text
        has_business = any(c in cat_text for c in CATEGORIES)
        if not has_business:
            continue
        has_inst = any(c in cat_text for c in INSTITUTIONS)
        has_region = any(k in cat_text for k in REGION_KW)
        if not (has_region or has_inst):
            continue
        link = requests.compat.urljoin(site["url"], href)
        if link in seen_links:
            continue
        seen_links.add(link)
        title = re.sub(r"\s+", " ", text).strip()
        date = detect_date(cat_text)
        out.append({
            "site": site["name"], "region": site.get("region", ""),
            "title": title, "link": link, "date": date,
            "tags": ",".join(site.get("tags", [])),
            "id": make_id(site["name"], link if link else title),
        })
        if len(out) >= MAX_ANCHORS:
            break

    # 降级：部分 SPA 站点完全没有 <a> 标签（如内蒙古招投标网，列表项是 div，
    # 详情靠 JS 事件跳转），此时退化为"文本模式"：从叶子文本提取标题，链接回站点本身。
    if not out:
        seen_text = set()
        for el in soup.find_all(["div", "li", "span", "p"]):
            if el.find_all(["div", "li", "span", "p"], recursive=False):
                continue  # 只取叶子节点，避免父子重复收录
            text = el.get_text(" ", strip=True)
            if not text or not (12 <= len(text) <= 120):
                continue
            if not any(k in text for k in ACTIONS):
                continue
            if any(k in text for k in EXCLUDE):
                continue
            if not any(c in text for c in CATEGORIES):
                continue
            if not (any(c in text for c in INSTITUTIONS) or any(k in text for k in REGION_KW)):
                continue
            t = re.sub(r"\s+", " ", text).strip()
            if t in seen_text:
                continue
            seen_text.add(t)
            out.append({
                "site": site["name"], "region": site.get("region", ""),
                "title": t, "link": site["url"], "date": detect_date(t),
                "tags": ",".join(site.get("tags", [])),
                "id": make_id(site["name"], t),
            })
            if len(out) >= MAX_ANCHORS:
                break
    return out

# ---------------- DB ----------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS tenders (
        id TEXT PRIMARY KEY, site TEXT, region TEXT, title TEXT,
        link TEXT, date TEXT, tags TEXT, first_seen TEXT)""")
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
            (r["id"], r["site"], r["region"], r["title"], r["link"], r["date"], r.get("tags", ""), now))
    conn.commit()

def recent(conn, limit=500):
    cur = conn.execute(
        "SELECT site,region,title,link,date,first_seen FROM tenders ORDER BY first_seen DESC, date DESC LIMIT ?",
        (limit,))
    return cur.fetchall()

# ---------------- 看板 ----------------
def build_dashboard(conn):
    rows = recent(conn, 500)
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
            f'<div class="meta">来源：{site}　·　发现于 {first_seen}</div></div>')
    chips = "".join(f'<button class="chip" data-region="{r}">{r}<i>{cnt.get(r,0)}</i></button>' for r in regions)
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
 .head .fresh{{margin-top:7px;display:inline-block;padding:4px 11px;border-radius:12px;
   font-size:13px;font-weight:600;background:rgba(255,255,255,.22)}}
 .wrap{{padding:12px}}
 .bar{{display:flex;flex-wrap:wrap;gap:8px;margin:6px 0 12px}}
 .chip{{border:1px solid #d4dbe5;background:#fff;color:#334;font-size:13px;padding:5px 11px;
   border-radius:16px;cursor:pointer;line-height:1.4}}
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
</style></head><body>
<div class="head"><h1>蒙西超市/商铺招租监测</h1>
<div class="sub">监测 {len(SITES)} 个平台 · 已收录 {db_count(conn)} 条</div>
<div class="fresh">最后检查 {updated} · 每 3 小时自动更新</div></div>
<div class="wrap">
 <div class="bar">
   <button class="chip on" data-region="__all">全部<i>{len(rows)}</i></button>
   {chips}
 </div>
 <input class="sch" id="kw" placeholder="搜索关键词，如 超市 / 底店 / 招租…">
 <div id="list">{''.join(cards)}</div>
 <div class="foot">数据由 WorkBuddy 招标监测器自动更新 · 点标题可跳转原公告<br>
 说明：产权/高校/政府采购等由云端 7×24 自动抓；剑鱼类纯 JS 站由本地电脑补抓后同步。</div>
</div>
<script>
 var chips=document.querySelectorAll('.chip'),kw=document.getElementById('kw'),
     list=document.getElementById('list'),cur='__all';
 function apply(){{var k=kw.value.trim(),n=0;
   list.querySelectorAll('.card').forEach(function(c){{
     var okR=(cur==='__all'||c.dataset.region===cur),okK=(!k||c.textContent.indexOf(k)>=0);
     c.style.display=(okR&&okK)?'':'none';if(okR&&okK)n++;}});
   var e=document.getElementById('emp');if(e)e.remove();
   if(n===0){{var d=document.createElement('div');d.className='empty';d.id='emp';
     d.textContent='没有匹配的招标';list.appendChild(d);}}}}
 chips.forEach(function(c){{c.onclick=function(){{
   chips.forEach(function(x)x.classList.remove('on'));c.classList.add('on');
   cur=c.dataset.region;apply();}};}});
 kw.oninput=apply;
</script></body></html>"""
    os.makedirs(WEB_DIR, exist_ok=True)
    with open(DASHBOARD, "w", encoding="utf-8") as f:
        f.write(html)
    with open(WEB_INDEX, "w", encoding="utf-8") as f:
        f.write(html)
    return html

def write_daily_report(new_rows):
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    if new_rows:
        lines = [f"蒙西超市/商铺招租监测日报  {today}", "=" * 42, ""]
        for r in new_rows:
            lines += [f"[{r['region']}] {r['date'] or '日期未知'}", f"  {r['title']}", f"  {r['link']}", ""]
        lines.append(f"共 {len(new_rows)} 条新公告")
    else:
        lines = [f"蒙西超市/商铺招租监测日报  {today}", "=" * 42, "", "今日无新超市/商铺招租公告。"]
    with open(DAILY_REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return DAILY_REPORT

# ---------------- COS（自写 v2 签名上传，纯标准库，跨平台）----------------
def _cos_auth(secret_id, secret_key, method, key, host, extra_headers=None):
    import hmac as _hmac, hashlib as _hl, time as _time
    now = int(_time.time())
    sign_time = "%d;%d" % (now - 60, now + 600)
    sign_headers = {"host": host}
    if extra_headers:
        for k, v in extra_headers.items():
            sign_headers[k.lower()] = v
    hl = sorted(sign_headers.keys())
    header_list = ";".join(hl)
    header_string = "&".join("%s=%s" % (k, sign_headers[k]) for k in hl)
    http_string = "%s\n/%s\n\n%s\n" % (method.lower(), key, header_string)
    string_to_sign = "sha1\n%s\n%s\n" % (sign_time, _hl.sha1(http_string.encode("utf-8")).hexdigest())
    sign_key = _hmac.new(secret_key.encode("utf-8"), sign_time.encode("utf-8"), _hl.sha1).digest()
    signature = _hmac.new(sign_key, string_to_sign.encode("utf-8"), _hl.sha1).hexdigest()
    return ("q-sign-algorithm=sha1&q-ak=%s&q-sign-time=%s&q-key-time=%s&q-header-list=%s&q-url-param-list=&q-signature=%s"
            % (secret_id, sign_time, sign_time, header_list, signature))


def _cos_host(bucket, region):
    return "%s.cos.%s.myqcloud.com" % (bucket, region)


def upload_cos(files):
    sid = os.environ.get("COS_SECRET_ID")
    skey = os.environ.get("COS_SECRET_KEY")
    bucket = os.environ.get("COS_BUCKET")
    region = os.environ.get("COS_REGION", "ap-beijing")
    if not (sid and skey and bucket):
        print("[COS] 未配置密钥/桶，跳过上传（仅本地生成文件）")
        return False
    from qcloud_cos import CosConfig, CosS3Client
    config = CosConfig(Region=region, SecretId=sid, SecretKey=skey)
    client = CosS3Client(config)
    for local, key in files:
        if not os.path.exists(local):
            continue
        try:
            client.upload_file(Bucket=bucket, LocalFilePath=local, Key=key, EnableMD5=False, ACL="public-read")
            print(f"[COS] 已上传 {key}")
        except Exception as e:
            print(f"[COS] 上传 {key} 失败：{e}")
    return True


def download_remote_db():
    # 本地补抓时用 SKIP_REMOTE_DB=1：不拉取远端库，保留本地更全的历史再整体上传
    if os.environ.get("SKIP_REMOTE_DB"):
        print("[COS] 跳过拉取远端 db（保留本地完整库）")
        return
    sid = os.environ.get("COS_SECRET_ID")
    skey = os.environ.get("COS_SECRET_KEY")
    bucket = os.environ.get("COS_BUCKET")
    region = os.environ.get("COS_REGION", "ap-beijing")
    if not (sid and skey and bucket):
        return
    from qcloud_cos import CosConfig, CosS3Client
    client = CosS3Client(CosConfig(Region=region, SecretId=sid, SecretKey=skey))
    try:
        client.download_file(Bucket=bucket, Key="tenders.db", DestFilePath=DB_PATH)
        print("[COS] 已拉取远端 tenders.db 用于去重")
    except Exception:
        print("[COS] 无远端 db（首次运行）")

# ---------------- 主流程 ----------------
def run():
    download_remote_db()
    conn = init_db()
    first_run = db_count(conn) == 0
    print(f"[开始] 监测 {len(SITES)} 个站点（浏览器可用={HAS_PLAYWRIGHT}）…")
    all_c = []
    for site in SITES:
        rows = parse_site(site)
        print(f"  - {site['name']}: 命中 {len(rows)} 条")
        all_c.extend(rows)
    seen_titles = set(); unique = []
    for r in all_c:
        if r["title"] in seen_titles:
            continue
        seen_titles.add(r["title"]); unique.append(r)
    new_rows = [r for r in unique if not is_seen(conn, r["id"])]
    if first_run:
        save(conn, unique)
        print(f"[播种] 首次运行，已收录 {len(unique)} 条作为基线。")
    else:
        save(conn, new_rows)
        if new_rows:
            print(f"[发现] {len(new_rows)} 条新公告：")
            for r in new_rows:
                print(f"  · [{r['region']}] {r['date'] or '日期未知'} {r['title']}")
        else:
            print("[平静] 本轮无新公告")
    write_daily_report(new_rows)
    build_dashboard(conn)
    files = [(WEB_INDEX, "index.html"), (DAILY_REPORT, "daily_report.txt"), (DB_PATH, "tenders.db")]
    upload_cos(files)
    print(f"[完成] 已收录 {db_count(conn)} 条，看板已更新。")

def main_handler(event, context):
    run()
    return {"status": "ok"}

if __name__ == "__main__":
    run()
