"""試合詳細を分析母集団へ分類する。取得の省略はしない。

上位階層は互いに混ぜない。
ナワバリ / オープン / チャレンジ / プラベ / イベマ / Xマッチ / フェス / バイト / ビッグラン / バイトチームコンテスト。

人数差で分けるのはプラベだけ。公開戦の人数差は回線落ちであり、別母集団にしない。
フェスのトリカラもフェスの中に置く。タグは母集団を動かさない。
"""

TOP_LEVEL = (
    'nawabari', 'bankara_open', 'bankara_challenge', 'private', 'event', 'xmatch',
    'fest', 'salmon_regular', 'big_run', 'team_contest',
)
PUBLIC_VS = ('nawabari', 'bankara_open', 'bankara_challenge', 'event', 'xmatch', 'fest')
MODE_GENRE = {
    'REGULAR': 'nawabari',
    'X_MATCH': 'xmatch',
    'LEAGUE': 'event',
    'PRIVATE': 'private',
    'FEST': 'fest',
}

def _mode(detail):
    vs = detail.get('vsMode') if isinstance(detail, dict) else None
    if isinstance(vs, dict) and isinstance(vs.get('mode'), str):
        return vs['mode']
    return None

def _bankara_mode(detail):
    bankara = detail.get('bankaraMatch') if isinstance(detail, dict) else None
    if isinstance(bankara, dict) and isinstance(bankara.get('mode'), str):
        return bankara['mode']
    return None

def genre_of(detail):
    mode = _mode(detail)
    bankara = _bankara_mode(detail)
    if mode == 'BANKARA':
        if bankara == 'OPEN':
            return 'bankara_open'
        if bankara == 'CHALLENGE':
            return 'bankara_challenge'
        return 'bankara_unspecified'
    return MODE_GENRE.get(mode, 'unknown')

def _count(team):
    if not isinstance(team, dict) or not isinstance(team.get('players'), list):
        return None
    return len(team['players'])

def roster_of(detail):
    """開始時点の名簿人数。切断で result が空でも配列に残っていれば人数に数える。"""
    if not isinstance(detail, dict):
        return 'unknown', None, None, None
    mine = _count(detail.get('myTeam'))
    others = detail.get('otherTeams')
    if mine is None or not isinstance(others, list) or not others:
        return 'unknown', mine, None, None
    counts = []
    for team in others:
        count = _count(team)
        if count is None:
            return 'unknown', mine, None, None
        counts.append(count)
    teams = 1 + len(counts)
    even = {4: 'four_vs_four', 3: 'three_vs_three', 2: 'two_vs_two', 1: 'one_vs_one'}
    if teams == 2 and counts == [mine] and mine in even:
        roster = even[mine]
    else:
        roster = 'other'
    return roster, mine, counts, teams

def analysis_set(genre, roster):
    if genre in PUBLIC_VS:
        return genre
    if genre == 'private' and roster in ('four_vs_four', 'three_vs_three', 'two_vs_two', 'one_vs_one', 'other'):
        return 'private_' + roster
    if genre in ('salmon_regular', 'big_run', 'team_contest'):
        return genre
    return 'hold'

def classify_coop(detail):
    rule = detail.get('rule') if isinstance(detail, dict) and isinstance(detail.get('rule'), str) else None
    genre = {'REGULAR': 'salmon_regular', 'BIG_RUN': 'big_run', 'TEAM_CONTEST': 'team_contest'}.get(rule, 'unknown')
    return {
        'genre': genre,
        'mode_raw': None,
        'bankara_mode': None,
        'rule_raw': rule,
        'rule_name': None,
        'roster_class': 'not_applicable',
        'analysis_set': genre if genre != 'unknown' else 'hold',
        'team_count': None,
        'my_player_count': None,
        'opponent_counts': None,
    }

def classify_detail(detail):
    genre = genre_of(detail)
    roster, mine, counts, teams = roster_of(detail)
    rule = detail.get('vsRule') if isinstance(detail, dict) and isinstance(detail.get('vsRule'), dict) else {}
    return {
        'genre': genre,
        'mode_raw': _mode(detail),
        'bankara_mode': _bankara_mode(detail),
        'rule_raw': rule.get('rule') if isinstance(rule.get('rule'), str) else None,
        'rule_name': rule.get('name') if isinstance(rule.get('name'), str) else None,
        'roster_class': roster,
        'analysis_set': analysis_set(genre, roster),
        'team_count': teams,
        'my_player_count': mine,
        'opponent_counts': counts,
    }
