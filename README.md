# 蒙西超市/商铺招租监测 · 云端版

每天自动抓取内蒙古西部（包头 / 乌海 / 巴彦淖尔 / 鄂尔多斯 / 阿拉善）及全区高校的
超市、商铺、经营权招租公告，生成可分享的在线看板网页。

## 为什么要用云端版
本地版依赖你的电脑开机才能跑。云端版跑在 GitHub 服务器上，
**你的电脑关不关都照常更新**，手机随时打开都是最新的。

## 工作原理
GitHub Actions 每天 UTC 01:00（= 北京时间 09:00）自动运行一次：
1. 安装 Python 依赖和 Chromium 浏览器
2. 运行 `monitor.py` 抓取全部站点
3. 生成看板 `web/index.html`
4. 把看板发布到 GitHub Pages

## 启用步骤
1. 在 GitHub 新建一个仓库（**必须选 Public**，否则免费账号无法开启 Pages）
2. 把本目录全部内容推送到该仓库
3. 仓库 → Settings → Pages → Source 选 `Deploy from a branch`
   → 分支选 `gh-pages` → 目录选 `/ (root)` → Save
4. 等第一次 Actions 跑完，网址形如：
   `https://<你的用户名>.github.io/<仓库名>/`

## 手动立即跑一次
仓库 → Actions → 选「蒙西招租监测 · 每日更新」→ Run workflow

## 文件说明
| 文件 | 作用 |
| --- | --- |
| `monitor.py` | 抓取与看板生成主程序 |
| `config.json` | 关键词过滤规则（动作词/类目词/机构词） |
| `sites.json` | 监测站点列表（11 个） |
| `tenders.db` | 已收录公告的去重库，每次运行后回写仓库 |
| `web/index.html` | 生成的看板，发布到 Pages |
| `.github/workflows/daily.yml` | 每日定时任务配置 |

## 已知问题
`daily_report.txt` 的判定按"公告日期 = 今天"，因此抓到往日发布的新公告时
仍会写「今日无新公告」。看板内容不受影响，仅日报文字口径待修正。
