# -*- coding: utf-8 -*-
"""轻量每日增量更新

不依赖 512 天全量回填：以仓库里的 data.json 已有曲线为起点，
只抓「最后一天之后」的新交易日，算出当日净流入后追加，再重绘。

每次运行开销：沪市份额 (1~3 次) + 深市份额 (1 次) + 指数日线 (1 次)
            + 价格（约 1600 只，16 并发，1~2 分钟）
"""
import io
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data.json")
PNG = os.path.join(HERE, "chart.png")

SSE_SQLID = "COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

# 现金(?!流)：自由现金流 ETF 属权益类，不能误杀
SKIP = re.compile(r"货币|现金(?!流)|理财|债|黄金|白银|豆粕|原油|能源化工|商品|"
                  r"期货|油气|白糖|铜|铝|豆一|豆二")
BOND_PREFIX = ("511", "551")      # 上证债券 / 科创债 ETF（简称被截断时名称过滤会漏）
MAX_NEW_DAYS = 40
MIN_ROWS = 500                    # 单日份额条数下限，低于此视为抓取异常


def now_cst():
    return datetime.now(timezone.utc) + timedelta(hours=8)


def http(url, headers=None, timeout=90, tries=4):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=headers or {"User-Agent": UA})
            return urllib.request.urlopen(req, timeout=timeout).read()
        except Exception as e:
            last = e
            time.sleep(2 + 3 * i)
    raise RuntimeError("HTTP 失败 %s: %s" % (url[:110], last))


# ------------------------------------------------------------------ 份额
def sse_shares(date):
    """沪市单日全量 -> {code: (name, shares)}"""
    q = urllib.parse.urlencode({
        "sqlId": SSE_SQLID, "STAT_DATE": date, "isPagination": "false",
        "pageHelp.pageSize": "10000"})
    url = "https://query.sse.com.cn/commonQuery.do?" + q
    js = json.loads(http(url, {"User-Agent": UA,
                               "Referer": "https://www.sse.com.cn/"}).decode("utf-8", "ignore"))
    out = {}
    for r in (js.get("result") or []):
        code = str(r.get("SEC_CODE") or "").strip()
        nm = str(r.get("SEC_NAME") or "").strip()
        vol = r.get("TOT_VOL")
        if not code or vol is None:
            continue
        try:
            out[code] = (nm, float(vol) * 10000.0)      # 万份 -> 份
        except Exception:
            continue
    return out


def szse_shares(a, b):
    """深市区间 -> {date: {code: (name, shares)}}"""
    q = urllib.parse.urlencode({
        "SHOWTYPE": "xlsx", "CATALOGID": "scsj_fund_jjgm", "TABKEY": "tab1",
        "txtStart": a, "txtEnd": b, "jjlb": "ETF", "random": "0.5"})
    url = "https://www.szse.cn/api/report/ShowReport?" + q
    raw = http(url, {"User-Agent": UA,
                      "Referer": "https://www.szse.cn/market/product/list/etfList/index.html"},
               timeout=150)
    df = pd.read_excel(io.BytesIO(raw))
    df = df.rename(columns={"基金规模(份)": "基金份额", "基金规模（份）": "基金份额"})
    out = {}
    if "基金份额" not in df.columns:
        return out
    df["d"] = df["日期"].astype(str).str[:10]
    df["code"] = df["基金代码"].astype(str).str.strip().str.zfill(6)
    df["nm"] = df["基金简称"].astype(str).str.strip()
    df["sh"] = pd.to_numeric(df["基金份额"], errors="coerce")
    for r in df[df.sh > 0].itertuples():
        out.setdefault(r.d, {})[r.code] = (r.nm, float(r.sh))
    return out


# ------------------------------------------------------------------ 行情
def secid(code):
    # 上证指数 000001 必须用 1.000001，否则命中 0.000001（平安银行，约 11 元）
    if code == "000001":
        return "1.000001"
    return ("0." if code[:2] in ("15", "16", "00", "12") else "1.") + code


def _kline_em(code, beg, end):
    q = urllib.parse.urlencode({
        "secid": secid(code), "fields1": "f1,f2,f3", "fields2": "f51,f53",
        "klt": "101", "fqt": "0", "beg": beg.replace("-", ""),
        "end": end.replace("-", ""), "lmt": "1000"})
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get?" + q
    js = json.loads(http(url, {"User-Agent": UA}, timeout=40, tries=2)
                    .decode("utf-8", "ignore"))
    kl = (js.get("data") or {}).get("klines") or []
    out = {}
    for line in kl:
        p = line.split(",")
        if len(p) >= 2:
            out[p[0]] = float(p[1])
    return out


def _kline_sina(code, beg, end):
    sym = ("sh" if code == "000001" or code.startswith(("5", "6")) else "sz") + code
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           "CN_MarketData.getKLineData?symbol=%s&scale=240&ma=no&datalen=1023" % sym)
    txt = http(url, {"User-Agent": UA, "Referer": "https://finance.sina.com.cn/"},
               timeout=40, tries=2).decode("utf-8", "ignore").strip()
    if not txt or txt in ("null", "[]"):
        return {}
    if txt[0] not in "[{":
        i, j = txt.find("["), txt.rfind("]")
        if i < 0 or j < 0:
            return {}
        txt = txt[i:j + 1]
    arr = json.loads(txt)
    out = {}
    for r in arr:
        d = str(r.get("day") or "")[:10]
        c = r.get("close")
        if d and c is not None:
            try:
                v = float(c)
            except Exception:
                continue
            if beg <= d <= end:
                out[d] = v
    return out


def _kline_tx(code, beg, end):
    sym = ("sh" if code == "000001" or code.startswith(("5", "6")) else "sz") + code
    url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
           "param=%s,day,%s,%s,640,qfq" % (sym, beg, end))
    js = json.loads(http(url, {"User-Agent": UA}, timeout=40, tries=2)
                    .decode("utf-8", "ignore"))
    node = (js.get("data") or {}).get(sym) or {}
    arr = node.get("day") or node.get("qfqday") or []
    out = {}
    for row in arr:
        if len(row) >= 3:
            try:
                out[str(row[0])[:10]] = float(row[2])
            except Exception:
                continue
    return out


def kline(code, beg, end):
    """返回 (code, {date: close})。东方财富在海外 runner 上常被拦，
    因此依次尝试 东方财富 -> 新浪 -> 腾讯，任一成功即用。"""
    for name, fn in (("eastmoney", _kline_em), ("sina", _kline_sina), ("tencent", _kline_tx)):
        try:
            m = fn(code, beg, end)
            if m:
                print("  %s 日线取自 %s（%d 个交易日）" % (code, name, len(m)))
                return code, m
            print("  %s 源 %s 返回空" % (code, name))
        except Exception as e:
            print("  %s 源 %s 失败：%s" % (code, name, str(e)[:100]))
    return code, {}


def candidate_days(last, today):
    """last 之后到 today 之间的工作日，用于指数全挂时探测交易日。"""
    d = datetime.strptime(last, "%Y-%m-%d") + timedelta(days=1)
    end = datetime.strptime(today, "%Y-%m-%d")
    out = []
    while d <= end:
        if d.weekday() < 5:
            out.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return out


# ------------------------------------------------------------------ 主流程
def redraw_only(pts, idx_map, doc, tag=""):
    """没有新交易日/无有效新增时，也用已有曲线重绘一次，保证 chart.png 存在"""
    try:
        sys.path.insert(0, HERE)
        from chart import render
        idx_series = [(d, idx_map[d]) for d in sorted(idx_map) if d >= pts[0]["d"]]
        # 用「本次运行时间」而非 data.json 里的旧时间，否则无新增日时
        # 图上会一直显示旧的生成时间，看起来像没更新
        gen = now_cst().strftime("%Y-%m-%d %H:%M")
        caption = ("数据截至 %s ｜ 本图生成 %s（北京时间） ｜ 最新 %+.1f 亿元"
                   % (pts[-1]["d"], gen, pts[-1]["v"]))
        render(pts, idx_series, PNG,
               note=("数据源：上交所 / 深交所官方基金份额，东方财富行情    "
                     "覆盖全市场 %d 只权益类 ETF" % doc.get("etf_count", 0)),
               caption=caption)
        print("图已重绘（%s）：%s" % (tag or "无新增", PNG))
    except Exception as e:
        print("重绘失败：%s" % e)


def main():
    with open(DATA, encoding="utf-8") as f:
        doc = json.load(f)
    pts = doc["points"]
    last = pts[-1]["d"]
    today = now_cst().strftime("%Y-%m-%d")
    print("曲线最后一日 %s | 今天 %s" % (last, today))

    # 指数日线：既当交易日历，也当右轴数据（一次请求覆盖全区间）
    _, idx_map = kline("000001", pts[0]["d"], today)
    if idx_map:
        new_days = sorted(d for d in idx_map if d > last)
    else:
        # 三个数据源都失败：改用交易所份额探测交易日，不中断任务
        print("！指数全源失败，改用交易所份额判断交易日（本次图可能缺橙线）")
        new_days = []
        for d in candidate_days(last, today):
            try:
                if len(sse_shares(d)) >= MIN_ROWS:
                    new_days.append(d)
                    print("  探测到交易日 %s" % d)
            except Exception as e:
                print("  %s 探测失败：%s" % (d, str(e)[:80]))
        if not new_days:
            print("没有新交易日，仅重绘")
            redraw_only(pts, idx_map, doc, tag="无新交易日")
            return 0
    if not new_days:
        print("没有新交易日，仅重绘")
        redraw_only(pts, idx_map, doc, tag="无新交易日")
        return 0
    if len(new_days) > MAX_NEW_DAYS:
        new_days = new_days[-MAX_NEW_DAYS:]
    print("新交易日 %d 个：%s .. %s" % (len(new_days), new_days[0], new_days[-1]))

    # 1) 份额（含最后一天基线）
    per_day = {}
    for d in [last] + new_days:
        try:
            per_day[d] = sse_shares(d)
            print("  沪 %s -> %d 只" % (d, len(per_day[d])))
        except Exception as e:
            per_day[d] = {}
            print("  沪 %s 失败：%s" % (d, e))
    try:
        for d, m in szse_shares(last, today).items():
            per_day.setdefault(d, {}).update(m)
            print("  深 %s -> 累计 %d 只" % (d, len(per_day[d])))
    except Exception as e:
        print("  深市失败：%s" % e)

    base = per_day.get(last, {})
    if len(base) < MIN_ROWS:
        print("❗基线 %s 只取到 %d 条（<%d），判定抓取异常，中止以免污染历史"
              % (last, len(base), MIN_ROWS))
        return 2

    # 2) 非权益类剔除
    nvars = {}
    for dd in per_day.values():
        for c, (nm, _) in dd.items():
            nvars.setdefault(c, set()).add(nm or "")
    bad = {c for c, vs in nvars.items()
           if c.startswith(BOND_PREFIX) or any(SKIP.search(v) for v in vs)}
    print("剔除非权益类 %d 只" % len(bad))

    # 3) 价格
    codes = set()
    for d in [last] + new_days:
        codes |= set(per_day.get(d, {}))
    codes -= bad
    print("取价格 %d 只" % len(codes))
    px = {}
    with ThreadPoolExecutor(max_workers=16) as ex:
        for i, (c, m) in enumerate(ex.map(lambda c: kline(c, last, today), sorted(codes)), 1):
            px[c] = m
            if i % 500 == 0:
                print("   进度 %d/%d" % (i, len(codes)))

    # 4) 逐日计算
    cum = pts[-1]["v"]
    last_share = {c: sh for c, (nm, sh) in base.items() if c not in bad}
    added = 0
    for d in new_days:
        # 份额尚未披露时该日条数会异常少（甚至 0），必须跳过，否则写入 +0.0 假点污染曲线
        if len(per_day.get(d, {})) < MIN_ROWS:
            print("  %s 份额尚未披露（仅 %d 条 <%d），跳过该日"
                  % (d, len(per_day.get(d, {})), MIN_ROWS))
            continue
        flow, cnt = 0.0, 0
        for c, (nm, sh) in per_day.get(d, {}).items():
            if c in bad:
                continue
            pr = px.get(c, {}).get(d)
            if not pr:
                continue
            p = last_share.get(c)
            if p is None:
                hist = px.get(c, {})
                earliest = min(hist) if hist else None
                if earliest and earliest >= last:      # 真新发才计入
                    flow += sh * pr
            elif p > 0 and abs((sh - p) / p) < 0.6:    # 排除拆分/折算
                flow += (sh - p) * pr
            last_share[c] = sh
            cnt += 1
        flow /= 1e8
        cum += flow
        pts.append({"d": d, "day": round(flow, 1), "v": round(cum, 1)})
        added += 1
        print("  %s 净流入 %+.1f 亿 | 累计 %.1f 亿 | 覆盖 %d 只" % (d, flow, cum, cnt))

    if not added:
        print("无有效新增，仅重绘")
        redraw_only(pts, idx_map, doc, tag="无有效新增")
        return 0

    doc["points"] = pts
    doc["updated"] = now_cst().strftime("%Y-%m-%d %H:%M")
    doc["etf_count"] = len(last_share)
    with open(DATA, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)

    # 5) 重绘
    sys.path.insert(0, HERE)
    from chart import render
    idx_series = [(d, idx_map[d]) for d in sorted(idx_map) if d >= pts[0]["d"]]
    caption = ("数据截至 %s ｜ 本图生成 %s（北京时间） ｜ 最新 %+.1f 亿元"
               % (pts[-1]["d"], doc["updated"], pts[-1]["v"]))
    render(pts, idx_series, PNG,
           note=("数据源：上交所 / 深交所官方基金份额，东方财富行情    "
                 "覆盖全市场 %d 只权益类 ETF" % len(last_share)),
           caption=caption)
    print("图已更新：%s" % PNG)
    print("完成：新增 %d 个交易日，最新 %s 累计 %.1f 亿（%d 只）"
          % (added, pts[-1]["d"], pts[-1]["v"], len(last_share)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
