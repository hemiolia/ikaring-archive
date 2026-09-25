"""レート系の数値を試合から拾う。挙げられた指標だけの閉じた一覧にはしない。"""
LABELS = {
    'weaponPower': 'ブキチャレパワー',
    'lastXPower': 'Xパワー',
    'entireXPower': 'Xパワー',
    'xPowerAfter': 'Xパワー',
    'myLeaguePower': 'イベントパワー',
    'myFestPower': 'フェスパワー',
    'earnedUdemaePoint': 'ウデマエポイント増減',
    'jobRate': '評価レート',
    'jobScore': 'バイトスコア',
    'jobPoint': '獲得ポイント',
    'dangerRate': 'キケン度',
    'afterGradePoint': '評価ポイント',
    'goldenDeliverCount': '集めた金イクラ',
    'deliverCount': '集めたイクラ',
    'teamDeliverCount': '集めた金イクラ',
    'teamDeliverCountSum': '集めた金イクラ',
    'rankPercentile': '順位の百分位',
    'highestJobScore': 'ハイスコア',
    'highestGradePoint': '最高評価ポイント',
    'maxBestNinePower': 'ベストナイン合計パワー',
    'maxWeaponPowerTotal': '最高ブキチャレパワー合計',
    'regularGradePoint': 'バイト評価ポイント',
    'gradePoint': '評価ポイント',
    'limitedPoint': '現在の期間限定ポイント',
    'regularPoint': '現在のポイント',
    'totalPoint': 'るいけいポイント',
    'vibes': 'チョーシ',
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
    """連勝・連敗数。公式のチョーシ（Weapon.stats.vibes）とは異なる。"""
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

def weapon_snapshots(data):
    """本人のブキ記録のみ。ランキング内の他プレイヤーは走査しない。"""
    found={}
    if not isinstance(data,dict):return []
    for root in ('weapons','weaponRecords','allWeapons'):
        container=data.get(root)
        nodes=container.get('nodes',[]) if isinstance(container,dict) else []
        for weapon in nodes or []:
            if not isinstance(weapon,dict) or not weapon.get('id'):continue
            stats=weapon.get('stats')
            if not isinstance(stats,dict):continue
            wid=weapon['id'];name=weapon.get('name') or 'ブキ名未取得'
            metrics=[('vibes',stats.get('vibes'),'チョーシ','nawabari'),
                     ('maxWeaponPower',stats.get('maxWeaponPower'),'最高ブキチャレパワー','bankara_challenge')]
            current=stats.get('currentWeaponPowerOrder')
            if isinstance(current,dict):metrics.append(('weaponPower',current.get('weaponPower'),'ブキチャレパワー','bankara_challenge'))
            for key,value,label,genre in metrics:
                if isinstance(value,(int,float)) and not isinstance(value,bool):
                    found[(wid,key)]={'series_id':genre+'|weapon:'+wid+'|'+key,'label':label+' / '+name,
                        'genre':genre,'rule_raw':'TURF_WAR' if key=='vibes' else None,'value':float(value)}
    return list(found.values())
