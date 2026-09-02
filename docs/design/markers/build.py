import json, pathlib
G = json.loads(pathlib.Path("geom.json").read_text())
FRAG = pathlib.Path("chart.svg.frag").read_text()
W, H = G["W"], G["H"]
X, C = G["x"], G["candles"]
yi = G["y_of"]
def y(p): return yi["pad_t"] + yi["plot_h"] * (yi["hi"] - p) / (yi["hi"] - yi["lo"])
def px(v): return f"{v:,.1f}"

UP, DOWN, ACC, DIM, FG, WARN = "#26a69a", "#ef5350", "#58a6ff", "#8b949e", "#e6edf3", "#d29922"
MONO = "ui-monospace,SFMono-Regular,Menlo,Consolas,monospace"

# 同一批标记，三个方案共用
SIG = [(8,"B"),(16,"B"),(17,"B"),(18,"B"),(22,"S"),(31,"B")]
FILL = [(12,"开仓",ACC),(26,"止损",DOWN),(35,"止盈",UP)]

def tri(x, yy, up, col, s=5):
    d = (f"M{x-s},{yy} L{x+s},{yy} L{x},{yy+s*1.5} Z" if up
         else f"M{x-s},{yy} L{x+s},{yy} L{x},{yy-s*1.5} Z")
    return f'<path d="{d}" fill="{col}"/>'

def shell(name, sub, trade, body, extra=""):
    return f'''<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <script src="./support.js"></script>
</head>
<body>
<x-dc>
<helmet>
  <style>
    body {{ margin:0; background:#0e1116; color:#e6edf3;
      font:14px/1.5 system-ui,-apple-system,"Segoe UI","PingFang SC",sans-serif; }}
    a {{ color:#58a6ff; }} a:hover {{ color:#79bbff; }}
    .mono {{ font-family:{MONO}; font-variant-numeric:tabular-nums; }}
  </style>
</helmet>
<div style="display:flex;flex-direction:column;gap:14px;padding:20px 20px 18px;
            background:#0e1116;width:920px">
  <div style="display:flex;flex-direction:column;gap:5px">
    <div style="display:flex;align-items:baseline;gap:10px">
      <span style="font-size:17px;font-weight:600;letter-spacing:.2px">{name}</span>
      <span style="font-size:12px;color:{DIM}">{sub}</span>
    </div>
    <div style="font-size:12px;color:{DIM};line-height:1.6">
      <span style="color:{UP}">取</span> {trade[0]}　
      <span style="color:{DOWN}">舍</span> {trade[1]}
    </div>
  </div>

  <div style="position:relative;background:#0e1116;border:1px solid #262c36;
              border-radius:8px;overflow:hidden">
    <svg width="{W}" height="{H}" viewBox="0 0 {W} {H}" style="display:block">
      {FRAG}
      {body}
    </svg>
  </div>
  {extra}
</div>
</x-dc>
<script data-dc-script data-props='{{"$preview":{{"width":920,"height":520}}}}'>
class Component extends DCLogic {{}}
</script>
</body>
</html>
'''

# ── A：引线胶囊标签 ────────────────────────────────────────────
a = []
def pill(x, yy, text, col, side_up=True, dy=26):
    ty = yy - dy if side_up else yy + dy
    w = 7.4 * len(text) + 16
    a.append(f'<line x1="{x}" y1="{yy}" x2="{x}" y2="{ty + (7 if side_up else -7)}" '
             f'stroke="{col}" stroke-width="1" opacity=".55"/>')
    a.append(f'<rect x="{x-w/2:.1f}" y="{ty-9:.1f}" width="{w:.1f}" height="18" rx="9" '
             f'fill="#0e1116" stroke="{col}" stroke-width="1.2"/>')
    a.append(f'<text x="{x:.1f}" y="{ty+4:.1f}" text-anchor="middle" font-family="{MONO}" '
             f'font-size="11" font-weight="600" fill="{col}">{text}</text>')
for i, kind in SIG:
    col = UP if kind == "B" else DOWN
    up = kind == "B"
    base = y(C[i]["l"]) + 10 if up else y(C[i]["h"]) - 10
    a.append(tri(X[i], base, not up, col))
    # 密集区错开：同一簇内逐个抬高
    off = {16: 26, 17: 48, 18: 70}.get(i, 26)
    pill(X[i], base, f'{kind} {px(C[i]["c"])}', col, side_up=up, dy=off)
# 成交标签一律走**下方通道**：信号胶囊占上方，两层分开就不会互撞
for i, label, col in FILL:
    yy = y(C[i]["c"])
    ly = y(C[i]["l"]) + 34
    a.append(f'<rect x="{X[i]-4:.1f}" y="{yy-4:.1f}" width="8" height="8" rx="1.5" '
             f'fill="#0e1116" stroke="{col}" stroke-width="1.6"/>')
    a.append(f'<line x1="{X[i]}" y1="{yy+5:.1f}" x2="{X[i]}" y2="{ly-8:.1f}" stroke="{col}" '
             f'stroke-width="1" opacity=".5"/>')
    t = f'{label} {px(C[i]["c"])}'
    wpx = 7.0 * len(t) + 16
    a.append(f'<rect x="{X[i]-wpx/2:.1f}" y="{ly-8:.1f}" width="{wpx:.1f}" height="17" '
             f'rx="3.5" fill="#0e1116" stroke="{col}" stroke-width="1" stroke-dasharray="3 2"/>')
    a.append(f'<text x="{X[i]:.1f}" y="{ly+4:.1f}" text-anchor="middle" font-family="{MONO}" '
             f'font-size="10.5" fill="{col}">{t}</text>')

legend_a = f'''<div style="display:flex;gap:22px;align-items:center;font-size:11.5px;color:{DIM}">
  <span style="display:flex;align-items:center;gap:7px">
    <svg width="14" height="14" viewBox="0 0 14 14"><path d="M2,9 L12,9 L7,3 Z" fill="{UP}"/></svg>
    信号 · 胶囊内是触发价</span>
  <span style="display:flex;align-items:center;gap:7px">
    <svg width="14" height="14" viewBox="0 0 14 14"><rect x="3" y="3" width="8" height="8" rx="1.5"
      fill="none" stroke="{ACC}" stroke-width="1.6"/></svg>
    成交 · 虚线拉出成交价</span>
  <span>密集处垂直错开，胶囊不重叠</span>
</div>'''
pathlib.Path("Main.dc.html").write_text(shell(
    "方案 A · 引线胶囊", "每个点都带价格，引线连回 K 线",
    ("价格贴着标记，一眼对得上，不用去右侧价格轴找",
     "标签占空间；一屏几十个时要靠错开，缩得很密时得退化成只留三角"),
    "".join(a), legend_a), encoding="utf-8")

# ── B：极简徽章 + 选中展开 ──────────────────────────────────────
b = []
for i, kind in SIG:
    col = UP if kind == "B" else DOWN
    up = kind == "B"
    base = y(C[i]["l"]) + 11 if up else y(C[i]["h"]) - 11
    b.append(f'<circle cx="{X[i]}" cy="{base}" r="7.5" fill="{col}"/>')
    b.append(f'<text x="{X[i]}" y="{base+3.6}" text-anchor="middle" font-family="{MONO}" '
             f'font-size="10" font-weight="700" fill="#0e1116">{kind}</text>')
for i, label, col in FILL:
    yy = y(C[i]["c"])
    b.append(f'<rect x="{X[i]-4.5:.1f}" y="{yy-4.5:.1f}" width="9" height="9" rx="2" '
             f'fill="{col}"/>')
# 选中态：一张卡
si = 31
sx, sy = X[si], y(C[si]["l"]) + 11
card_w, card_h = 158, 76
cx0 = sx - card_w / 2
cy0 = sy + 16
b.append(f'<circle cx="{sx}" cy="{sy}" r="12" fill="none" stroke="{UP}" stroke-width="1.5" opacity=".5"/>')
b.append(f'<line x1="0" y1="{y(C[si]["c"]):.1f}" x2="{W}" y2="{y(C[si]["c"]):.1f}" '
         f'stroke="{UP}" stroke-width="1" stroke-dasharray="3 3" opacity=".5"/>')
b.append(f'<rect x="{cx0:.1f}" y="{cy0:.1f}" width="{card_w}" height="{card_h}" rx="7" '
         f'fill="#161b22" stroke="{UP}" stroke-width="1"/>')
rows = [("触发价", px(C[si]["c"]), FG), ("规则", "kdzx-long", DIM), ("时间", "09-01 14:35", DIM)]
for k, (lab, val, colr) in enumerate(rows):
    yy = cy0 + 22 + k * 19
    b.append(f'<text x="{cx0+12:.1f}" y="{yy}" font-size="10.5" fill="{DIM}">{lab}</text>')
    b.append(f'<text x="{cx0+card_w-12:.1f}" y="{yy}" text-anchor="end" font-family="{MONO}" '
             f'font-size="11" fill="{colr}">{val}</text>')

legend_b = f'''<div style="display:flex;gap:22px;align-items:center;font-size:11.5px;color:{DIM}">
  <span style="display:flex;align-items:center;gap:7px">
    <svg width="15" height="15" viewBox="0 0 15 15"><circle cx="7.5" cy="7.5" r="7" fill="{UP}"/>
    <text x="7.5" y="11" text-anchor="middle" font-family="{MONO}" font-size="9"
      font-weight="700" fill="#0e1116">B</text></svg>
    信号 · 常态只有字母</span>
  <span style="display:flex;align-items:center;gap:7px">
    <svg width="14" height="14" viewBox="0 0 14 14"><rect x="3" y="3" width="8" height="8" rx="2"
      fill="{ACC}"/></svg> 成交</span>
  <span>点中才展开价格卡与价位线</span>
</div>'''
pathlib.Path("DirectionB.dc.html").write_text(shell(
    "方案 B · 极简徽章", "常态只有 B/S，点中才展开",
    ("几十个标记也不糊；K 线本身完全不被遮挡",
     "价格要多一次交互才看得到；扫一眼看不出成交在什么价位"),
    "".join(b), legend_b), encoding="utf-8")

# ── C：成对交易带 ──────────────────────────────────────────────
c = []
# (开仓下标, 平仓下标, 颜色, 盈亏)
TRADES = [(12, 26, DOWN, "-0.31%"), (31, 35, UP, "+0.42%")]
for ent_i, e_out, col, pnl in TRADES:
    x1, x2 = X[ent_i], X[e_out]
    y1, y2 = y(C[ent_i]["c"]), y(C[e_out]["c"])
    # 价差小的时候矩形会细到看不见，给个最小高度
    band_h = max(abs(y2 - y1), 14)
    c.append(f'<rect x="{x1:.1f}" y="{min(y1,y2) - (band_h-abs(y2-y1))/2:.1f}" '
             f'width="{x2-x1:.1f}" height="{band_h:.1f}" fill="{col}" opacity=".13"/>')
    c.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="{col}" '
             f'stroke-width="1.4" stroke-dasharray="4 3" opacity=".85"/>')
    for xx, yy, lab, price in ((x1, y1, "开仓", C[ent_i]["c"]), (x2, y2, "平仓", C[e_out]["c"])):
        c.append(f'<circle cx="{xx:.1f}" cy="{yy:.1f}" r="4.5" fill="#0e1116" stroke="{col}" stroke-width="2"/>')
        t = f'{lab} {px(price)}'
        wpx = 6.9 * len(t) + 14
        tx = xx - wpx - 8 if xx > W * 0.6 else xx + 8
        c.append(f'<rect x="{tx:.1f}" y="{yy-9:.1f}" width="{wpx:.1f}" height="18" rx="4" '
                 f'fill="#161b22" stroke="{col}" stroke-width="1"/>')
        c.append(f'<text x="{tx+7:.1f}" y="{yy+4:.1f}" font-family="{MONO}" font-size="10.5" '
                 f'fill="{col}">{t}</text>')
    # 盈亏放在交易带正上方，加一块底片压住 K 线，否则会被烛身糊掉
    mx, my = (x1 + x2) / 2, min(y1, y2) - 26
    pw = 8.2 * len(pnl) + 14
    c.append(f'<rect x="{mx-pw/2:.1f}" y="{my-13:.1f}" width="{pw:.1f}" height="19" rx="4" '
             f'fill="#0e1116" stroke="{col}" stroke-width="1"/>')
    c.append(f'<text x="{mx:.1f}" y="{my+1:.1f}" text-anchor="middle" font-family="{MONO}" '
             f'font-size="12" font-weight="700" fill="{col}">{pnl}</text>')
for i, kind in SIG:
    col = UP if kind == "B" else DOWN
    up = kind == "B"
    base = y(C[i]["l"]) + 10 if up else y(C[i]["h"]) - 10
    c.append(tri(X[i], base, not up, col, 4.5))
    c.append(f'<text x="{X[i]:.1f}" y="{base + (16 if up else -9):.1f}" text-anchor="middle" '
             f'font-family="{MONO}" font-size="9.5" font-weight="700" fill="{col}">{kind}</text>')

legend_c = f'''<div style="display:flex;gap:22px;align-items:center;font-size:11.5px;color:{DIM}">
  <span style="display:flex;align-items:center;gap:7px">
    <svg width="16" height="14" viewBox="0 0 16 14"><path d="M3,10 L13,10 L8,4 Z" fill="{UP}"/></svg>
    信号 · 只有 B/S</span>
  <span style="display:flex;align-items:center;gap:7px">
    <svg width="26" height="12" viewBox="0 0 26 12">
      <circle cx="3" cy="8" r="3" fill="none" stroke="{UP}" stroke-width="1.6"/>
      <line x1="6" y1="7" x2="20" y2="4" stroke="{UP}" stroke-width="1.4" stroke-dasharray="3 2"/>
      <circle cx="23" cy="3" r="3" fill="none" stroke="{UP}" stroke-width="1.6"/></svg>
    一笔交易 · 两端带成交价，中间是盈亏</span>
</div>'''
pathlib.Path("DirectionC.dc.html").write_text(shell(
    "方案 C · 成对交易带", "把开仓与平仓连成一笔",
    ("直接看出每笔交易走了多远、赚还是亏；复盘时最省脑子",
     "只对已成交的有效；纯信号（没进场的）仍需另一套标记，两套并存"),
    "".join(c), legend_c), encoding="utf-8")

pathlib.Path("canvas.json").write_text(json.dumps({
    "artboards": [
        {"file": "Main.dc.html", "x": 0, "y": 0, "w": 920, "h": 520},
        {"file": "DirectionB.dc.html", "x": 0, "y": 660, "w": 920, "h": 520},
        {"file": "DirectionC.dc.html", "x": 0, "y": 1320, "w": 920, "h": 520},
    ],
    "annotations": [{
        "id": "brief", "x": -300, "y": 0, "w": 260,
        "text": "三个方案画的是同一段 K 线、同样的标记位置\n"
                "（含 idx16-18 一处三连密集区，专看拥挤时的表现）。\n\n"
                "配色取自应用真实 token：\n涨 #26a69a / 跌 #ef5350 / 强调 #58a6ff\n"
                "数字一律等宽 + tabular-nums。",
    }],
    "launch": {"view": "canvas"},
}, ensure_ascii=False), encoding="utf-8")
print("已生成 Main / DirectionB / DirectionC + canvas.json")
