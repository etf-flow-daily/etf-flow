# -*- coding: utf-8 -*-
"""按参考图样式渲染：全市场权益类ETF累计净流入(亿元) + 上证指数（双轴折线图）"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib import font_manager
from matplotlib.lines import Line2D
from datetime import datetime, timedelta

BLUE = "#2E5C8A"
ORANGE = "#E07B39"

_FONT = None


def pick_font():
    global _FONT
    if _FONT:
        return _FONT
    for name in ["Noto Sans CJK SC", "Noto Sans SC", "Noto Sans CJK JP",
                 "Source Han Sans SC", "WenQuanYi Zen Hei", "Microsoft YaHei", "SimHei"]:
        try:
            font_manager.findfont(name, fallback_to_default=False)
            _FONT = name
            return name
        except Exception:
            continue
    _FONT = "sans-serif"
    return _FONT


def render(points, index_series, out_path,
           title="全市场权益类ETF累计净流入(亿元)",
           note=None, legend1="ETF累计净流入", legend2="上证指数", caption=None):
    """points: [{'d':'2024-09-24','v':0.0}, ...]   index_series: [('2024-09-24', 2863.13), ...]"""
    plt.rcParams["font.family"] = pick_font()
    plt.rcParams["axes.unicode_minus"] = False

    xs = [datetime.strptime(p["d"], "%Y-%m-%d") for p in points]
    ys = [p["v"] for p in points]
    if len(xs) == 1:                      # 只有一天时补一个前值，避免 xlim 退化
        xs = [xs[0] - timedelta(days=1)] + xs
        ys = [ys[0]] + ys

    fig, ax = plt.subplots(figsize=(10.2, 6.6), dpi=160)
    fig.subplots_adjust(left=0.105, right=0.885, top=0.875, bottom=0.245)

    ax.plot(xs, ys, color=BLUE, linewidth=3.0, solid_capstyle="round", zorder=5)
    ax.tick_params(axis="y", colors="#4A4A4A", labelsize=11.5, length=0)
    ax.grid(axis="y", linestyle=(0, (7, 5)), linewidth=1.1, color="#C9C9C9", zorder=0)
    ax.set_axisbelow(True)
    for s in ["top", "right", "left"]:
        ax.spines[s].set_visible(False)
    ax.spines["bottom"].set_color("#BDBDBD")
    ax.spines["bottom"].set_linewidth(1.1)

    # ---- X 轴：按实际交易日等距抽 ~20 个刻度，标签用该日真实日期 ----
    n = len(xs)
    step = max(1, n // 20)
    idx = list(range(0, n, step))
    if idx and idx[-1] != n - 1:          # 末位刻度必须落在最后一个交易日
        if n - 1 - idx[-1] < step * 0.6:  # 太近则替换，避免标签重叠
            idx[-1] = n - 1
        else:
            idx.append(n - 1)
    pos, labs = [], []
    for i in idx:
        pos.append(xs[i])
        labs.append("%d-%d-%d" % (xs[i].year, xs[i].month, xs[i].day))
    ax.set_xticks(pos)
    ax.set_xticklabels(labs, rotation=90, fontsize=10.5, color="#4A4A4A")
    ax.tick_params(axis="x", length=0, pad=6)
    ax.set_xlim(xs[0], xs[-1])

    # ---- 右轴：上证指数 ----
    ax2 = ax.twinx()
    if index_series:
        ax2.plot([datetime.strptime(d, "%Y-%m-%d") for d, _ in index_series],
                 [v for _, v in index_series], color=ORANGE, linewidth=2.6, zorder=4)
    ax2.tick_params(axis="y", colors="#4A4A4A", labelsize=11.5, length=0)
    for s in ["top", "left", "bottom"]:
        ax2.spines[s].set_visible(False)
    ax2.spines["right"].set_color("#BDBDBD")
    ax2.spines["right"].set_linewidth(1.1)

    # ---- 坐标轴范围按参考图固定：左轴 ±10000（步长 2000），右轴 2800~4400（步长 200） ----
    from matplotlib.ticker import MultipleLocator, FuncFormatter
    ax.set_ylim(-10000, 10000)
    ax.yaxis.set_major_locator(MultipleLocator(2000))
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, p: "%d" % v))
    ax2.set_ylim(2800, 4400)
    ax2.yaxis.set_major_locator(MultipleLocator(200))
    ax2.yaxis.set_major_formatter(FuncFormatter(lambda v, p: "%d" % v))

    ax.set_title(title, fontsize=19.5, fontweight="bold", color="#333333", pad=24)

    # ---- 顶部左上角：数据截至日 + 生成时间 + 最新值，便于确认是否已更新 ----
    if caption:
        fig.text(0.105, 0.912, caption, ha="left", va="top",
                 fontsize=11, color="#6A6A6A")

    fig.legend(handles=[Line2D([0], [0], color=BLUE, linewidth=3.6, label=legend1),
                        Line2D([0], [0], color=ORANGE, linewidth=3.0, label=legend2)],
               loc="lower center", bbox_to_anchor=(0.5, 0.045), ncol=2,
               frameon=False, fontsize=11.5, handlelength=1.7, columnspacing=2.6)

    if note:
        fig.text(0.985, 0.012, note, ha="right", va="bottom", fontsize=8.5, color="#9A9A9A")

    fig.savefig(out_path, facecolor="white")
    plt.close(fig)
    return out_path
