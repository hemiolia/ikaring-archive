"""ローカルで開く分析画面。配色は NOAHS の卓上と紙を使い、文字と線は実測した対比だけを置く。"""
import html
from pathlib import Path

# NOAHS app/src/app.css の :root。対比が足りない muted は本文に使わない。
FIELD = '#2a0063'
ON_FIELD = '#fffff5'
PAPER = '#fbf5ec'
INK = '#241a33'
GOLD = '#bd9a45'
LINK = '#5b3bb0'

def _channel(value):
    value = value / 255
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4

def contrast(foreground, background):
    def lum(color):
        red, green, blue = int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)
        return 0.2126 * _channel(red) + 0.7152 * _channel(green) + 0.0722 * _channel(blue)
    dark, light = sorted((lum(foreground), lum(background)))
    return (light + 0.05) / (dark + 0.05)

TEXT_PAIRS = ((ON_FIELD, FIELD), (INK, PAPER), (LINK, PAPER))
GRAPHIC_PAIRS = ((INK, PAPER), (LINK, PAPER))

def assert_contrast():
    for foreground, background in TEXT_PAIRS:
        if contrast(foreground, background) < 4.5:
            raise ValueError('TEXT_CONTRAST')
    for foreground, background in GRAPHIC_PAIRS:
        if contrast(foreground, background) < 3:
            raise ValueError('GRAPHIC_CONTRAST')

def _svg(points):
    width, height, pad = 720, 240, 36
    values = [point['value'] for point in points]
    low, high = min(values), max(values)
    span = high - low or 1
    def x_at(index):
        if len(points) == 1:
            return width / 2
        return pad + (width - pad * 2) * index / (len(points) - 1)
    def y_at(value):
        return pad + (height - pad * 2) * (1 - (value - low) / span)
    coords = [(x_at(index), y_at(point['value'])) for index, point in enumerate(points)]
    line = ' '.join(f'{x:.1f},{y:.1f}' for x, y in coords)
    dots = ''.join(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{INK}"><title>{html.escape(point["played_time"] or "")} {point["value"]}</title></circle>' for (x, y), point in zip(coords, points))
    return f'''<svg viewBox="0 0 {width} {height}" role="img">
<title>{html.escape(points[0]["label"])}</title>
<rect width="{width}" height="{height}" fill="{PAPER}"/>
<polyline fill="none" stroke="{INK}" stroke-width="2" points="{line}"/>
{dots}
<text x="{pad}" y="22" fill="{INK}" font-size="14">{html.escape(str(high))}</text>
<text x="{pad}" y="{height-8}" fill="{INK}" font-size="14">{html.escape(str(low))}</text>
</svg>'''

def _table(points):
    rows = ''.join(f'<tr><td>{html.escape(point["played_time"] or "")}</td><td>{html.escape(point["rule_raw"] or "")}</td><td>{point["value"]}</td></tr>' for point in points)
    return f'<table><caption>同じ数値の表</caption><thead><tr><th>日時</th><th>ルール</th><th>値</th></tr></thead><tbody>{rows}</tbody></table>'

def render(store):
    assert_contrast()
    series = {}
    for row in store.db.execute('SELECT * FROM rate_points ORDER BY genre,series_id,played_time,match_key'):
        series.setdefault(row['series_id'], []).append(dict(row))
    sections = []
    for series_id, points in series.items():
        label = points[0]['label']
        genre = points[0]['genre']
        priority = '副指標' if points[0]['priority'] == 'secondary' else '主指標'
        source = '勝敗から数えたチョーシ。APIの数値項目ではない。' if points[0]['source'] == 'derived_judgement' else '応答に含まれていた数値。'
        chart = _svg(points) if len(points) else ''
        sections.append(f'<section><h2>{html.escape(genre)} / {html.escape(label)} <span>{priority}</span></h2><p>{source}</p>{chart}{_table(points)}</section>')
    body = ''.join(sections) or '<p>まだレートの数値はありません。取得が進むと、応答に含まれるパワー・ポイント・納品数・レートがここへ並びます。</p>'
    return f'''<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8"><title>イカリング3のレート</title>
<style>
body{{margin:0;background:{PAPER};color:{INK};font-family:serif;font-size:16px;line-height:1.6}}
header{{background:{FIELD};color:{ON_FIELD};padding:20px 24px}}
header a{{color:{ON_FIELD}}}
main{{max-width:800px;margin:0 auto;padding:24px}}
section{{margin:0 0 36px;padding-bottom:12px;border-bottom:1px solid {INK}}}
h2{{font-size:22px;margin:0 0 8px}}
h2 span{{font-size:14px}}
svg{{width:100%;height:auto}}
table{{border-collapse:collapse;width:100%}}
th,td{{border-bottom:1px solid {INK};text-align:left;padding:4px 8px;font-size:15px}}
</style></head><body>
<header><h1>レートの推移</h1><p>シートやグラフをまたいで、別のジャンルを一つの数値として足さない。</p></header>
<main>{body}</main></body></html>'''

def write_gui(store, destination):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination.write_text(render(store), encoding='utf-8')
    destination.chmod(0o600)
    count = store.db.execute('SELECT count(DISTINCT series_id) FROM rate_points').fetchone()[0]
    return {'path': str(destination), 'series': count}
