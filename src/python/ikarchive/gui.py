"""ローカルで開く分析画面。配色は NOAHS の卓上と紙を使い、文字と線は実測した対比だけを置く。"""
import base64
from datetime import datetime
import html
import math
from pathlib import Path

# NOAHS app/src/app.css の :root。対比が足りない muted は本文に使わない。
FIELD = '#2a0063'
ON_FIELD = '#fffff5'
PAPER = '#fbf5ec'
INK = '#241a33'
GOLD = '#bd9a45'
LINK = '#5b3bb0'
GRID = '#7a6a8a'
BUNDLED_FONT = Path(__file__).resolve().parents[3] / 'assets' / 'fonts' / 'Splatoon2-Unified.otf'
GENRE_LABELS = {
    'nawabari': 'ナワバリ',
    'bankara_open': 'オープン',
    'bankara_challenge': 'チャレンジ',
    'event': 'イベマ',
    'xmatch': 'Xマッチ',
    'fest': 'フェス',
    'private_four_vs_four': 'プラベ 4対4',
    'private_three_vs_three': 'プラベ 3対3',
    'private_two_vs_two': 'プラベ 2対2',
    'private_one_vs_one': 'プラベ 1対1',
    'private_other': 'プラベ その他',
    'salmon_regular': 'バイト',
    'big_run': 'ビッグラン',
    'team_contest': 'バイトチームコンテスト',
    'hold': '区分保留',
}

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
GRAPHIC_PAIRS = ((INK, PAPER), (LINK, PAPER), (GRID, PAPER))

def assert_contrast():
    for foreground, background in TEXT_PAIRS:
        if contrast(foreground, background) < 4.5:
            raise ValueError('TEXT_CONTRAST')
    for foreground, background in GRAPHIC_PAIRS:
        if contrast(foreground, background) < 3:
            raise ValueError('GRAPHIC_CONTRAST')

def _format_date(val):
    if not val or not isinstance(val, str):
        return str(val or '')
    try:
        clean = val.strip()
        if clean.endswith('Z'):
            clean = clean[:-1] + '+00:00'
        dt = datetime.fromisoformat(clean)
        return f'{dt.month:02d}/{dt.day:02d} {dt.hour:02d}:{dt.minute:02d}'
    except Exception:
        return str(val)[:16]

def _format_number(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return str(value)
    if not math.isfinite(value):
        return str(value)
    if abs(value - round(value)) < 1e-9:
        return f'{int(round(value)):,}'
    magnitude = abs(value)
    digits = 2 if magnitude >= 1 else 4
    return f'{value:,.{digits}f}'.rstrip('0').rstrip('.')

def _calc_y_ticks(low, high):
    if low == high:
        if low == 0:
            low, high = -2.0, 2.0
        else:
            delta = max(1.0, abs(low) * 0.1)
            low, high = low - delta, low + delta
    raw_span = high - low
    raw_step = raw_span / 5.0
    power = 10 ** math.floor(math.log10(raw_step))
    fraction = raw_step / power
    if fraction <= 1.0:
        step = 1.0 * power
    elif fraction <= 2.0:
        step = 2.0 * power
    elif fraction <= 2.5:
        step = 2.5 * power
    elif fraction <= 5.0:
        step = 5.0 * power
    else:
        step = 10.0 * power
    nice_min = math.floor(low / step) * step
    nice_max = math.ceil(high / step) * step
    n_steps = round((nice_max - nice_min) / step)
    if n_steps < 4:
        if abs(step / power - 5.0) < 1e-6:
            step = 2.0 * power
        elif abs(step / power - 2.0) < 1e-6:
            step = 1.0 * power
        elif abs(step / power - 10.0) < 1e-6:
            step = 5.0 * power
        nice_min = math.floor(low / step) * step
        nice_max = math.ceil(high / step) * step
        n_steps = round((nice_max - nice_min) / step)
    ticks = [round(nice_min + i * step, 6) for i in range(n_steps + 1)]
    return ticks

def _svg(points, series_id):
    width, height = 720, 280
    pad_left, pad_right = 64, 32
    pad_top, pad_bottom = 28, 44
    plot_x = pad_left
    plot_y = pad_top
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom
    x_min, x_max = plot_x, plot_x + plot_w
    y_min, y_max = plot_y, plot_y + plot_h

    values = [p['value'] for p in points]
    val_min, val_max = min(values), max(values)
    ticks = _calc_y_ticks(val_min, val_max)
    y_tick_min, y_tick_max = ticks[0], ticks[-1]
    y_span = y_tick_max - y_tick_min or 1.0

    n = len(points)
    def x_at(idx):
        if n == 1:
            return plot_x + plot_w / 2.0
        return plot_x + plot_w * idx / (n - 1)

    def y_at(val):
        return y_max - (val - y_tick_min) / y_span * plot_h

    y_elements = []
    for t in ticks:
        y = y_at(t)
        is_zero = abs(t) < 1e-6
        label = _format_number(t)
        if is_zero:
            y_elements.append(
                f'<line class="grid-line zero-line" x1="{x_min}" y1="{y:.1f}" x2="{x_max}" y2="{y:.1f}" stroke="{INK}" stroke-width="1.5" />'
            )
        else:
            y_elements.append(
                f'<line class="grid-line" x1="{x_min}" y1="{y:.1f}" x2="{x_max}" y2="{y:.1f}" stroke="{GRID}" stroke-width="1" stroke-dasharray="3,3" />'
            )
        y_elements.append(
            f'<text class="tick-label y-tick-label" x="{x_min - 8}" y="{y + 4:.1f}" text-anchor="end" fill="{INK}" font-size="12">{label}</text>'
        )

    if n <= 6:
        x_indices = list(range(n))
    else:
        target_count = 5
        x_indices = sorted(set(round(i * (n - 1) / (target_count - 1)) for i in range(target_count)))

    x_elements = []
    for idx in x_indices:
        x = x_at(idx)
        raw_time = points[idx].get('played_time') or ''
        date_str = _format_date(raw_time)
        x_elements.append(
            f'<line class="x-tick-mark" x1="{x:.1f}" y1="{y_max}" x2="{x:.1f}" y2="{y_max + 5}" stroke="{INK}" stroke-width="1.5" />'
        )
        x_elements.append(
            f'<text class="tick-label x-tick-label" x="{x:.1f}" y="{y_max + 20}" text-anchor="middle" fill="{INK}" font-size="11">{html.escape(date_str)}</text>'
        )

    axes = (
        f'<line class="axis y-axis" x1="{x_min}" y1="{y_min}" x2="{x_min}" y2="{y_max}" stroke="{INK}" stroke-width="1.5" />'
        f'<line class="axis x-axis" x1="{x_min}" y1="{y_max}" x2="{x_max}" y2="{y_max}" stroke="{INK}" stroke-width="1.5" />'
    )

    coords = [(x_at(i), y_at(p['value'])) for i, p in enumerate(points)]
    line_pts = ' '.join(f'{x:.1f},{y:.1f}' for x, y in coords)

    area_el = ''
    line_el = ''
    if n >= 2:
        area_pts = line_pts + f' {coords[-1][0]:.1f},{y_max:.1f} {coords[0][0]:.1f},{y_max:.1f}'
        area_el = f'<polygon class="chart-area" points="{area_pts}" fill="{LINK}" fill-opacity="0.12" />'
        line_el = f'<polyline class="chart-line" fill="none" stroke="{LINK}" stroke-width="2.5" points="{line_pts}" />'

    dots = []
    for i, ((x, y), p) in enumerate(zip(coords, points)):
        tooltip = f'{html.escape(str(p.get("played_time") or ""))} {_format_number(p["value"])}'
        if i == n - 1 and n > 1:
            dots.append(
                f'<circle class="dot-focus" cx="{x:.1f}" cy="{y:.1f}" r="6.5" fill="none" stroke="{LINK}" stroke-width="2" />'
                f'<circle class="dot dot-latest" cx="{x:.1f}" cy="{y:.1f}" r="3.5" fill="{INK}"><title>{tooltip} (最新)</title></circle>'
            )
        else:
            dots.append(
                f'<circle class="dot" cx="{x:.1f}" cy="{y:.1f}" r="3.5" fill="{INK}"><title>{tooltip}</title></circle>'
            )
    dots_el = ''.join(dots)

    safe_id = "".join(c if c.isalnum() else '_' for c in str(series_id))
    title_id = f'chart-title-{safe_id}'
    desc_id = f'chart-desc-{safe_id}'
    label = html.escape(str(points[0].get('label') or ''))
    genre_raw = str(points[0].get('genre') or '')
    genre = html.escape(GENRE_LABELS.get(genre_raw, genre_raw))
    latest = points[-1]['value']
    desc_text = (
        f'{GENRE_LABELS.get(genre_raw, genre_raw)} {points[0].get("label") or ""}。'
        f'データ数{n}点、最新値{_format_number(latest)}、'
        f'最小値{_format_number(val_min)}、最大値{_format_number(val_max)}。'
    )

    return f'''<svg viewBox="0 0 {width} {height}" role="img" aria-labelledby="{title_id} {desc_id}">
<title id="{title_id}">{label}の推移グラフ</title>
<desc id="{desc_id}">{html.escape(desc_text)}</desc>
<rect width="{width}" height="{height}" fill="{PAPER}" />
{''.join(y_elements)}
{axes}
{''.join(x_elements)}
{area_el}
{line_el}
{dots_el}
</svg>'''

def _table(points):
    rows = ''.join(
        f'<tr><td>{html.escape(point.get("played_time") or "")}</td>'
        f'<td>{html.escape(point.get("rule_raw") or "")}</td>'
        f'<td>{_format_number(point["value"])}</td></tr>'
        for point in points
    )
    return f'<div class="table-wrap"><table><caption>同じ数値の表</caption><thead><tr><th>日時</th><th>ルール</th><th>値</th></tr></thead><tbody>{rows}</tbody></table></div>'

def _summary_stats(points):
    n = len(points)
    latest = points[-1]['value']
    if n >= 2:
        diff = latest - points[-2]['value']
        diff_str = _format_number(diff)
        if diff > 0:
            diff_str = '+' + diff_str
    else:
        diff_str = '—'
    min_val = min(p['value'] for p in points)
    max_val = max(p['value'] for p in points)
    return f'''<div class="series-stats" aria-label="系列の要約">
  <div class="stat-item"><span class="stat-label">最新値</span><span class="stat-value">{_format_number(latest)}</span></div>
  <div class="stat-item"><span class="stat-label">直前からの増減</span><span class="stat-value">{diff_str}</span></div>
  <div class="stat-item"><span class="stat-label">最小</span><span class="stat-value">{_format_number(min_val)}</span></div>
  <div class="stat-item"><span class="stat-label">最大</span><span class="stat-value">{_format_number(max_val)}</span></div>
  <div class="stat-item"><span class="stat-label">点数</span><span class="stat-value">{n}</span></div>
</div>'''

def render(store, font_path=None):
    assert_contrast()

    font_face = ''
    font_family = 'system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif'
    if font_path is None:
        font_path = BUNDLED_FONT
    if font_path:
        p = Path(font_path)
        if p.is_file():
            font_bytes = p.read_bytes()
            b64 = base64.b64encode(font_bytes).decode('ascii')
            font_face = f'''@font-face {{
  font-family: 'Splatoon2-Unified';
  src: url('data:font/otf;base64,{b64}') format('opentype');
  font-display: swap;
}}'''
            font_family = "'Splatoon2-Unified', system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif"

    series = {}
    for row in store.db.execute('SELECT * FROM rate_points ORDER BY genre,series_id,played_time,match_key'):
        series.setdefault(row['series_id'], []).append(dict(row))

    sections = []
    for series_id, points in series.items():
        label = points[0]['label']
        genre = points[0]['genre']
        genre_label = GENRE_LABELS.get(genre, genre)
        priority = '副指標' if points[0]['priority'] == 'secondary' else '主指標'
        source = '勝敗から数えたチョーシ。APIの数値項目ではない。' if points[0]['source'] == 'derived_judgement' else '応答に含まれていた数値。'
        chart = f'<div class="chart-wrap">{_svg(points, series_id)}</div>' if len(points) else ''
        stats = _summary_stats(points) if len(points) else ''
        sections.append(
            f'<section><h2>{html.escape(genre_label)} / {html.escape(label)} <span>{priority}</span></h2>'
            f'<p class="series-desc">{source}</p>'
            f'{stats}'
            f'{chart}'
            f'{_table(points)}</section>'
        )

    body = ''.join(sections) or '<p>まだレートの数値はありません。取得が進むと、応答に含まれるパワー・ポイント・納品数・レートがここへ並びます。</p>'
    return f'''<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>イカリング3のレート</title>
<style>
*, *::before, *::after {{ box-sizing: border-box; }}
{font_face}
body {{
  margin: 0;
  background: {PAPER};
  color: {INK};
  font-family: {font_family};
  font-size: 16px;
  line-height: 1.6;
  -webkit-text-size-adjust: 100%;
}}
header {{
  background: {FIELD};
  color: {ON_FIELD};
  padding: 20px 16px;
  word-break: break-word;
}}
header h1 {{
  margin: 0 0 8px;
  font-size: 24px;
}}
header p {{
  margin: 0;
  font-size: 14px;
  opacity: 0.9;
}}
header a {{ color: {ON_FIELD}; }}
main {{
  max-width: 800px;
  margin: 0 auto;
  padding: 20px 16px;
  width: 100%;
}}
section {{
  margin: 0 0 40px;
  padding-bottom: 24px;
  border-bottom: 1px solid {INK};
}}
h2 {{
  font-size: 20px;
  margin: 0 0 8px;
  word-break: break-word;
}}
h2 span {{
  font-size: 14px;
  font-weight: normal;
  margin-left: 8px;
}}
.series-desc {{
  margin: 0 0 12px;
  font-size: 14px;
  color: {INK};
}}
.series-stats {{
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin: 12px 0 16px;
}}
.stat-item {{
  display: flex;
  flex-direction: column;
  background: rgba(36, 26, 51, 0.04);
  border: 1px solid rgba(36, 26, 51, 0.2);
  border-radius: 4px;
  padding: 6px 10px;
  min-width: 80px;
  flex: 1 1 calc(33.333% - 8px);
}}
@media (min-width: 480px) {{
  .stat-item {{
    flex: 0 1 auto;
    min-width: 96px;
  }}
}}
.stat-label {{
  font-size: 11px;
  color: {INK};
  opacity: 0.8;
}}
.stat-value {{
  font-size: 17px;
  font-weight: bold;
  color: {INK};
  margin-top: 2px;
}}
.chart-wrap {{
  width: 100%;
  margin: 16px 0;
  background: {PAPER};
}}
svg {{
  width: 100%;
  height: auto;
  display: block;
}}
.table-wrap {{
  width: 100%;
  overflow-x: auto;
  -webkit-overflow-scrolling: touch;
  margin-top: 16px;
}}
table {{
  border-collapse: collapse;
  width: 100%;
  min-width: 280px;
}}
caption {{
  text-align: left;
  font-weight: bold;
  margin-bottom: 6px;
  font-size: 14px;
}}
th, td {{
  border-bottom: 1px solid {INK};
  text-align: left;
  padding: 6px 8px;
  font-size: 14px;
  white-space: nowrap;
}}
</style></head><body>
<header><h1>レートの推移</h1><p>シートやグラフをまたいで、別のジャンルを一つの数値として足さない。</p></header>
<main>{body}</main></body></html>'''

def write_gui(store, destination, font_path=None):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination.write_text(render(store, font_path=font_path), encoding='utf-8')
    destination.chmod(0o600)
    count = store.db.execute('SELECT count(DISTINCT series_id) FROM rate_points').fetchone()[0]
    return {'path': str(destination), 'series': count}
