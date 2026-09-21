"""レート系の数値を試合から拾う。挙げられた指標だけの閉じた一覧にはしない。"""
LABELS = {
    'weaponPower': 'ブキチャレパワー',
    'lastXPower': '直前のXパワー',
    'entireXPower': '全体のXパワー',
    'xPowerAfter': '計測後のXパワー',
    'myLeaguePower': 'イベントパワー',
    'myFestPower': 'フェスパワー',
    'earnedUdemaePoint': 'ウデマエポイント増減',
    'jobRate': 'バイトレート',
    'jobScore': 'バイトスコア',
    'jobPoint': 'バイトポイント',
    'dangerRate': 'キケン度',
    'afterGradePoint': '評価ポイント',
    'goldenDeliverCount': 'キンシャケ納品数',
    'deliverCount': '納品数',
    'teamDeliverCount': 'チーム納品数',
    'teamDeliverCountSum': 'チーム納品数',
    'rankPercentile': '順位の百分位',
    'highestJobScore': '最高スコア',
    'highestGradePoint': '最高評価ポイント',
    'maxBestNinePower': 'ベストナインパワー',
    'maxWeaponPowerTotal': 'ブキパワー合計の最高',
    'regularGradePoint': 'バイト評価ポイント',
    'gradePoint': '評価ポイント',
    'limitedPoint': '限定ポイント',
    'regularPoint': '通常ポイント',
    'totalPoint': '累計ポイント',
}
SKIP_KEYS = {
    'kill', 'death', 'assist', 'special', 'paint', 'width', 'height', 'duration', 'order',
    'waveNumber', 'waterLevel', 'goldenPopCount', 'defeatEnemyCount', 'defeatCount',
    'goldenAssistCount', 'rescueCount', 'rescuedCount', 'noroshi', 'noroshiTry',
    'paintPoint', 'paintRatio', 'score', 'festUniformBonusRate', 'contribution',
}
SKIP_PATH = {'memberResults', 'enemyResults', 'badges', 'additionalGearPowers'}

def _is_rate_key(key):
    if key in SKIP_KEYS or key in LABELS:
        return key in LABELS
    return key.endswith(('Power', 'Rate', 'Point', 'Score', 'Percentile'))

def _label(path):
    key = path[-1]
    if key == 'weaponPower':
        return 'ブキチャレパワー'
    if key == 'power' and 'bankaraPower' in path:
        return 'バンカラパワー'
    if key == 'power':
        return 'パワー'
    return LABELS.get(key, key)

def _priority(genre, key):
    deliver = key in ('goldenDeliverCount', 'deliverCount', 'teamDeliverCount', 'teamDeliverCountSum', 'jobScore')
    if genre in ('big_run', 'team_contest') and key == 'jobRate':
        return 'secondary'
    if genre in ('big_run', 'team_contest') and deliver:
        return 'primary'
    return 'primary'

def _walk(value, path=()):
    if isinstance(value, dict):
        for key, child in value.items():
            if key.startswith('__') or key in SKIP_PATH:
                continue
            yield from _walk(child, path + (key,))
        return
    if isinstance(value, list):
        if path and path[-1] == 'waveResults':
            total = 0
            seen = False
            for item in value:
                count = item.get('teamDeliverCount') if isinstance(item, dict) else None
                if isinstance(count, (int, float)) and not isinstance(count, bool):
                    total += count
                    seen = True
            if seen:
                yield path + ('teamDeliverCountSum',), float(total)
            return
        for index, child in enumerate(value):
            yield from _walk(child, path + (str(index),))
        return
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        yield path, float(value)

def observations(detail, genre, rule_raw):
    found = []
    if not isinstance(detail, dict):
        return found
    for path, value in _walk(detail):
        key = path[-1]
        if not _is_rate_key(key) and not (key == 'power'):
            continue
        if key == 'power' or _is_rate_key(key):
            found.append({
                'series_id': genre + '|' + (rule_raw or '') + '|' + '.'.join(path),
                'label': _label(path),
                'genre': genre,
                'rule_raw': rule_raw,
                'value': value,
                'source': 'api',
                'priority': _priority(genre, key),
            })
    return found

def turf_form(judgements):
    """ナワバリのチョーシ。APIにその数値は無いので、勝敗の連なりから数える。"""
    streak = 0
    points = []
    for judgement in judgements:
        if judgement == 'WIN':
            streak = streak + 1 if streak > 0 else 1
        elif judgement in ('LOSE', 'EXEMPTED_LOSE', 'DEEMED_LOSE'):
            streak = streak - 1 if streak < 0 else -1
        else:
            streak = 0
        points.append(streak)
    return points
