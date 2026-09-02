"""生成三个方案共用的同一段 K 线 SVG（确定性，可重复）。"""
import math, json, pathlib

N, W, H = 44, 880, 360
PAD_L, PAD_R, PAD_T, PAD_B = 8, 8, 14, 8
VOL_H = 62

# 一段有回调再拉升的走势，便于摆买卖点
random_walk = []
p = 78180.0
for i in range(N):
    drift = math.sin(i / 6.5) * 26 + (i - 22) * 3.2
    p = 78180 + drift * 3 + math.sin(i * 1.9) * 22
    random_walk.append(p)

candles = []
for i, c in enumerate(random_walk):
    o = random_walk[i - 1] if i else c - 8
    hi = max(o, c) + 14 + (i % 5) * 4
    lo = min(o, c) - 12 - (i % 4) * 5
    vol = 40 + (i * 37 % 60) + (35 if i in (9, 18, 27, 36) else 0)
    candles.append({"o": round(o, 1), "h": round(hi, 1), "l": round(lo, 1),
                    "c": round(c, 1), "v": vol})

lo_all = min(x["l"] for x in candles); hi_all = max(x["h"] for x in candles)
span = hi_all - lo_all
plot_h = H - PAD_T - PAD_B - VOL_H
step = (W - PAD_L - PAD_R) / N
body_w = step * 0.62

def x_of(i): return PAD_L + step * (i + 0.5)
def y_of(price): return PAD_T + plot_h * (hi_all - price) / span

parts = []
vmax = max(x["v"] for x in candles)
for i, k in enumerate(candles):
    up = k["c"] >= k["o"]
    col = "#26a69a" if up else "#ef5350"
    x = x_of(i)
    y1, y2 = y_of(k["h"]), y_of(k["l"])
    top, bot = y_of(max(k["o"], k["c"])), y_of(min(k["o"], k["c"]))
    parts.append(f'<line x1="{x:.1f}" y1="{y1:.1f}" x2="{x:.1f}" y2="{y2:.1f}" '
                 f'stroke="{col}" stroke-width="1"/>')
    parts.append(f'<rect x="{x-body_w/2:.1f}" y="{top:.1f}" width="{body_w:.1f}" '
                 f'height="{max(bot-top,1):.1f}" fill="{col}"/>')
    vh = (k["v"] / vmax) * (VOL_H - 10)
    parts.append(f'<rect x="{x-body_w/2:.1f}" y="{H-PAD_B-vh:.1f}" width="{body_w:.1f}" '
                 f'height="{vh:.1f}" fill="{col}" opacity="0.42"/>')

grid = "".join(
    f'<line x1="0" y1="{PAD_T + plot_h*f:.1f}" x2="{W}" y2="{PAD_T + plot_h*f:.1f}" '
    f'stroke="#1b2028" stroke-width="1"/>' for f in (0, .25, .5, .75, 1))

pathlib.Path("chart.svg.frag").write_text(grid + "".join(parts), encoding="utf-8")
pathlib.Path("geom.json").write_text(json.dumps({
    "W": W, "H": H, "candles": candles,
    "x": [round(x_of(i), 1) for i in range(N)],
    "y_of": {"hi": hi_all, "lo": lo_all, "pad_t": PAD_T, "plot_h": plot_h},
}), encoding="utf-8")
print(f"K 线 {N} 根，价格 {lo_all:.0f}~{hi_all:.0f}")
for i in (8, 12, 16, 17, 18, 22, 26, 31, 35):
    print(f"  idx {i:2d}  x={x_of(i):6.1f}  close={candles[i]['c']:.1f}  y={y_of(candles[i]['c']):.1f}")
