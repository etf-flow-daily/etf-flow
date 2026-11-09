#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
全市场权益类 ETF 累计净流入（亿元）· 每日更新 / 历史回填
=============================================================================
数据全部来自交易所官方披露，两条路径共用同一套采集、计算与绘图代码：

  沪市：https://query.sse.com.cn/commonQuery.do
        sqlId=COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L, STAT_DATE=YYYY-MM-DD
        返回当日全量沪市 ETF，TOT_VOL 单位「万份」→ ×10000 = 份

  深市：https://www.szse.cn/api/report/ShowReport
        CATALOGID=scsj_fund_jjgm&TABKEY=tab1&txtStart=&txtEnd=&jjlb=ETF
        日期区间 ≤ 6 个月，返回 日期/基金代码/基金简称/基金份额(份)

  价格：东方财富行情（日线历史 + 当日快照）
  指数：上证指数日线

计算口径
--------
  当日净流入 = Σ (份额_t − 份额_{t−k}) × 收盘价_t / 1e8     单位：亿元
  累计净流入 = 自 START（2024-09-24，基准 0）逐日累加
  份额变动 >60% 视为拆分/折算/异常，剔除该只当日贡献
  非权益类（货币/债/黄金/商品/期货/原油等）按名称剔除

用法
----
    python3 etf_flow.py --backfill          # 首次：回填 START 至今全部历史
    python3 etf_flow.py --daily             # 每日：增量抓取当日并重绘
    python3 etf_flow.py --render            # 只按库内数据重算重绘（不联网）
    python3 etf_flow.py --daily --force     # 忽略周末/休市判定
=============================================================================
"""
import argparse, json, os, re, sqlite3, sys, time
import urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "etf.db")
OUT = os.path.join(HERE, "data.json")
PNG = os.path.join(HERE, "chart.png")

START = "2024-09-24"
SSE_SQLID = "COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L"
SZSE_URL = "https://www.szse.cn/api/report/ShowReport"
INDEX_SECID = "1.000001"
CLIST = "https://push2.eastmoney.com/api/qt/clist/get"

# 注意：现金(?!流) —— "自由现金流/现金流"ETF 属权益类，不能误杀
SKIP = re.compile(r"货币|现金(?!流)|理财|债|黄金|白银|豆粕|原油|能源化工|商品|"
                  r"期货|油气|白糖|铜|铝|豆一|豆二")

# 交易所简称被截断时（如 "XD30年国" 丢了"债"字），名称过滤会漏掉债券 ETF。
# 用代码段兜底：511xxx（上证债券 ETF）、551xxx（上证科创债 ETF）、
# 159xxx 中的债券类由名称判定。
BOND_PREFIX = ("511", "551")

CST = timezone(timedelta(hours=8))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def now_cst():
    return datetime.now(CST)


def _open(url, referer, timeout=30, tries=4):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA, "Referer": referer})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:
            last = e
            if i < tries - 1:
                time.sleep(1.2 * (i + 1))
    raise last


# ------------------------------------------------------------------ 采集
def fetch_sse_day(date):
    """沪市某日全量 ETF 份额 -> [(d, code, name, shares)]"""
    p = {"sqlId": SSE_SQLID, "STAT_DATE": date, "isPagination": "true",
         "pageHelp.pageSize": "10000", "pageHelp.pageNo": "1",
         "pageHelp.beginPage": "1", "pageHelp.cacheSize": "1",
         "pageHelp.endPage": "1"}
    url = "https://query.sse.com.cn/commonQuery.do?" + urllib.parse.urlencode(p)
    j = json.loads(_open(url, "https://www.sse.com.cn/").decode("utf-8", "ignore"))
    data = (j.get("pageHelp") or {}).get("data") or []
    rows = []
    for x in data:
        code = (x.get("SEC_CODE") or "").strip()
        name = (x.get("SEC_NAME") or "").strip()
        try:
            sh = float(x.get("TOT_VOL")) * 10000.0     # 万份 -> 份
        except (TypeError, ValueError):
            continue
        if code and sh > 0 and not SKIP.search(name):
            rows.append((date, code, name, sh))
    return rows


def fetch_szse_range(a, b):
    """深市 [a,b] 区间 ETF 份额 -> [(d, code, name, shares)]"""
    def dash(x):
        x = str(x).replace("-", "")
        return "%s-%s-%s" % (x[:4], x[4:6], x[6:8])

    q = {"SHOWTYPE": "xlsx", "CATALOGID": "scsj_fund_jjgm", "TABKEY": "tab1",
         "txtStart": dash(a), "txtEnd": dash(b), "jjlb": "ETF", "random": "0.5"}
    raw = _open(SZSE_URL + "?" + urllib.parse.urlencode(q),
                "https://www.szse.cn/market/product/list/etfList/index.html")
    try:
        import io

        import pandas as pd          # 仅在解析深市 xlsx 时需要
        df = pd.read_excel(io.BytesIO(raw))
        # 交易所列名为「基金规模(份)」，统一成内部命名
        df = df.rename(columns={"基金规模(份)": "基金份额",
                                "基金规模（份）": "基金份额"})
    except Exception as e:
        print("  深市解析失败: %s" % e)
        return []
    rows = []
    for _, r in df.iterrows():
        try:
            d = str(r["日期"])[:10]
            code = str(r["基金代码"]).strip().zfill(6)
            name = str(r["基金简称"]).strip()
            sh = float(r["基金份额"])
        except (KeyError, TypeError, ValueError):
            continue
        if sh > 0 and not SKIP.search(name):
            rows.append((d, code, name, sh))
    return rows


def fetch_px_history(codes, beg=START, workers=12):
    """全市场 ETF 日线收盘 -> [(code, d, close)]"""
    from concurrent.futures import ThreadPoolExecutor

    def one(c):
        sid = ("1." if c[0] in "56" else "0.") + c
        url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=%s"
               "&fields1=f1,f2,f3&fields2=f51,f53&klt=101&fqt=0&beg=%s"
               "&end=20500101&lmt=0&_=%d" % (sid, beg.replace("-", ""),
                                             int(time.time() * 1000)))
        try:
            s = _open(url, "https://quote.eastmoney.com/", timeout=20, tries=3)
            kl = (json.loads(s.decode("utf-8", "ignore")).get("data") or {}).get("klines")
        except Exception:
            return c, []
        out = []
        for line in kl or []:
            p = line.split(",")
            if len(p) >= 2:
                try:
                    out.append((c, p[0], float(p[1])))
                except ValueError:
                    pass
        return c, out

    rows = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for _, r in ex.map(one, codes):
            rows += r
    return rows


def fetch_px_today():
    """当日全市场 ETF 快照价格 -> [(code, d, close)]"""
    today = now_cst().strftime("%Y-%m-%d")

    def page(pn):
        url = (CLIST + "?po=1&np=1&fltt=2&invt=2&fid=f20&fs=b:MK0021"
               "&fields=f12,f14,f2&pn=%d&pz=100&_=%d" % (pn, int(time.time() * 1000)))
        return json.loads(_open(url, "https://quote.eastmoney.com/").decode("utf-8", "ignore"))

    d = page(1).get("data") or {}
    total = d.get("total") or 0
    rows = list(d.get("diff") or [])
    for pn in range(2, (total + 99) // 100 + 1):
        try:
            rows += (page(pn).get("data") or {}).get("diff") or []
        except Exception:
            pass
        time.sleep(0.15)

    out = []
    for x in rows:
        code, px = x.get("f12"), x.get("f2")
        if code and isinstance(px, (int, float)) and px > 0:
            out.append((code, today, float(px)))
    return out


def fetch_index(beg=START):
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=%s"
           "&fields1=f1&fields2=f51,f53&klt=101&fqt=0&beg=%s&end=20500101"
           "&lmt=0&_=%d" % (INDEX_SECID, beg.replace("-", ""),
                           int(time.time() * 1000)))
    s = _open(url, "https://quote.eastmoney.com/")
    out = []
    for line in (json.loads(s.decode("utf-8", "ignore")).get("data") or {}).get("klines") or []:
        p = line.split(",")
        if len(p) >= 2:
            try:
                out.append((p[0], float(p[1])))
            except ValueError:
                pass
    return out


# ------------------------------------------------------------------ 存储
def connect():
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS sse_share(
        d TEXT, code TEXT, name TEXT, shares REAL, PRIMARY KEY(d,code))""")
    con.execute("""CREATE TABLE IF NOT EXISTS szse_share(
        d TEXT, code TEXT, name TEXT, shares REAL, PRIMARY KEY(d,code))""")
    con.execute("""CREATE TABLE IF NOT EXISTS px(
        code TEXT, d TEXT, close REAL, PRIMARY KEY(code,d))""")
    con.execute("""CREATE TABLE IF NOT EXISTS idx(d TEXT PRIMARY KEY, close REAL)""")
    return con


def save(con, table, rows):
    if not rows:
        return 0
    con.executemany("INSERT OR REPLACE INTO %s VALUES(%s)"
                    % (table, ",".join("?" * len(rows[0]))), rows)
    con.commit()
    return len(rows)


# ------------------------------------------------------------------ 计算
def compute(con):
    # 先按代码聚合全部名称变体：交易所简称有时被截断（"XD30年国" 丢"债"），
    # 只要任一历史名称命中 SKIP，就把该代码整体判为非权益类。
    nvars = {}
    for t in ("sse_share", "szse_share"):
        for code, name in con.execute(
                "SELECT DISTINCT code, name FROM %s" % t):
            nvars.setdefault(code, set()).add((name or "").strip())

    bad = set()
    for code, vs in nvars.items():
        if code.startswith(BOND_PREFIX):
            bad.add(code)
            continue
        if any(SKIP.search(v) for v in vs):
            bad.add(code)

    shares, names = {}, {}
    for t in ("sse_share", "szse_share"):
        for d, code, name, sh in con.execute(
                "SELECT d, code, name, shares FROM %s" % t):
            if code in bad:            # 剔除货币/债券/黄金/商品等非权益类
                continue
            shares.setdefault(code, {})[d] = sh
            # 取该代码最长的名称作为展示名，避免截断名
            nm = (name or "").strip()
            if len(nm) > len(names.get(code, "")):
                names[code] = nm
    price = {}
    for code, d, c in con.execute("SELECT code, d, close FROM px"):
        price.setdefault(code, {})[d] = c

    days = [r[0] for r in con.execute(
        "SELECT DISTINCT d FROM sse_share WHERE d>=? ORDER BY d", (START,))]

    # 每只代码的最早价格日：仅当份额首现与最早价格日接近时，才认定为真实新发。
    # 否则视为历史数据缺口（老基金某日才补录），不计入当日流量。
    first_px = {}
    for code, m in price.items():
        if m:
            first_px[code] = min(m)
    daypos = {d: i for i, d in enumerate(days)}
    NEW_ISSUE_TOL = 5                      # 容忍 5 个交易日内的错位

    cum, pts, last = 0.0, [], {}
    first_day = days[0] if days else None
    for d in days:
        day = 0.0
        for code, m in shares.items():
            v = m.get(d)
            if v is None:
                continue
            p = last.get(code)
            pr = price.get(code, {}).get(d)
            if pr and pr > 0:
                if p is None:
                    # 新发/新入池：仅当该代码价格历史确实从近期开始，才计为一笔真实流入
                    fp = first_px.get(code)
                    near = (fp is not None
                            and abs(daypos.get(d, 0) - daypos.get(fp, 0)) <= NEW_ISSUE_TOL)
                    if d != first_day and near:
                        day += v * pr
                elif p > 0 and abs((v - p) / p) < 0.6:
                    day += (v - p) * pr
            last[code] = v
        day /= 1e8
        cum += day
        pts.append({"d": d, "day": round(day, 1), "v": round(cum, 1)})
    return pts, len([c for c in shares if shares[c]])


def write_outputs(con, note=None):
    pts, n = compute(con)
    if not pts:
        print("库内无数据，跳过输出")
        return None

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"updated": now_cst().strftime("%Y-%m-%d %H:%M"),
                   "unit": "亿元", "start": START, "etf_count": n,
                   "points": pts}, f, ensure_ascii=False, indent=1)

    if note is None:
        note = ("数据源：上交所 / 深交所官方基金份额，东方财富行情    "
                "覆盖全市场 %d 只权益类 ETF" % n)

    stamp = now_cst().strftime("%Y-%m-%d %H:%M")
    caption = ("数据截至 %s ｜ 本图生成 %s（北京时间） ｜ 最新 %+.1f 亿元"
               % (pts[-1]["d"], stamp, pts[-1]["v"]))

    png = None
    try:
        from chart import render
        png = render(pts, [(d, c) for d, c in con.execute(
            "SELECT d, close FROM idx WHERE d>=? ORDER BY d", (START,))],
            PNG, note=note, caption=caption)
    except Exception as e:
        print("  出图跳过（%s）" % e)

    print("覆盖 %d 只 ETF · %d 个交易日 · 最新 %s 累计 %.1f 亿"
          % (n, len(pts), pts[-1]["d"], pts[-1]["v"]))
    print("  图表: %s" % png if png else "  图表: 未生成")
    print("  数据: %s" % OUT)
    return pts


# ------------------------------------------------------------------ 主流程
def do_backfill(con):
    print("== 沪市逐日回填 ==")
    days = [d for d, in con.execute(
        "SELECT DISTINCT d FROM px WHERE d>=? ORDER BY d", (START,))]
    if not days:
        print("先在 px 表准备交易日轴（run --backfill 会自动抓价格）")
        return
    tot, empty = 0, []
    t0 = time.time()
    for i, d in enumerate(days):
        try:
            rows = fetch_sse_day(d)
        except Exception as e:
            rows = []
            print("  %s 失败 %s" % (d, e))
        if not rows:
            empty.append(d)
        tot += save(con, "sse_share", rows)
        if (i + 1) % 60 == 0:
            print("  %d/%d 累计 %d 行 %.0fs" % (i + 1, len(days), tot, time.time() - t0))
            sys.stdout.flush()
        time.sleep(0.12)
    print("  沪市完成 %d 行，空日 %d" % (tot, len(empty)))

    print("== 深市分段回填（≤6 个月/段）==")
    a = datetime.strptime(days[0], "%Y-%m-%d")
    end = datetime.strptime(days[-1], "%Y-%m-%d")
    tot2 = 0
    while a <= end:
        b = min(a + timedelta(days=180), end)
        try:
            rows = fetch_szse_range(a.strftime("%Y%m%d"), b.strftime("%Y%m%d"))
            tot2 += save(con, "szse_share", rows)
            print("  %s~%s -> %d 行" % (a.date(), b.date(), len(rows)))
        except Exception as e:
            print("  %s~%s 失败 %s" % (a.date(), b.date(), e))
        a = b + timedelta(days=1)
        time.sleep(0.8)
    print("  深市完成 %d 行" % tot2)


def run(mode, force=False):
    con = connect()

    if mode == "render":
        write_outputs(con, note=None)
        con.close()
        return

    if mode == "backfill":
        print("== 抓取历史价格（全市场日线）==")
        codes = set()
        try:
            for _, r in ((201,),):
                pass
        except Exception:
            pass
        # 用当日快照拿到全市场代码表
        for code, _, _ in fetch_px_today():
            codes.add(code)
        print("  ETF 代码 %d 只" % len(codes))
        rows = fetch_px_history(sorted(codes))
        print("  价格记录 %d 条" % save(con, "px", rows))
        save(con, "idx", fetch_index())
        do_backfill(con)
        write_outputs(con)
        con.close()
        return

    # ---- 每日增量 ----
    today = now_cst().strftime("%Y-%m-%d")
    if not force and now_cst().weekday() >= 5:
        print("周末，仅重算已有数据")
        write_outputs(con)
        con.close()
        return

    # 最新可用交易日：以沪市接口能否返回数据为准
    target, sse_rows = today, []
    for back in range(0, 5):
        d = (now_cst() - timedelta(days=back)).strftime("%Y-%m-%d")
        if now_cst().weekday() >= 5 and not force:
            break
        try:
            rows = fetch_sse_day(d)
        except Exception:
            rows = []
        if rows:
            target, sse_rows = d, rows
            break
    if not sse_rows:
        print("未取到最新交易日份额，本轮不写入（沿用历史数据重绘）")
        write_outputs(con)
        con.close()
        return

    prev = con.execute("SELECT MAX(d) FROM sse_share").fetchone()[0]
    save(con, "sse_share", sse_rows)
    try:
        sz = fetch_szse_range(target.replace("-", ""), target.replace("-", ""))
        save(con, "szse_share", sz)
    except Exception as e:
        print("  深市当日失败: %s" % e)

    px = fetch_px_today()
    save(con, "px", px)
    try:
        save(con, "idx", fetch_index())
    except Exception as e:
        print("  指数更新失败: %s" % e)

    print("写入 %s：沪 %d 只 · 深 %d 只 · 价格 %d 条"
          % (target, len(sse_rows), len(px), len(px)))
    write_outputs(con)
    con.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--daily", action="store_true")
    ap.add_argument("--render", action="store_true")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    mode = "backfill" if a.backfill else ("render" if a.render else "daily")
    run(mode, force=a.force)
