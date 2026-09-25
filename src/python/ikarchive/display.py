"""画面表示用の正式名称、順序、およびラベル変換定義。"""

GENRE_LABELS = {
    'fest': 'フェスマッチ',
    'nawabari': 'レギュラーマッチ',
    'bankara_challenge': 'バンカラマッチ（チャレンジ）',
    'bankara_open': 'バンカラマッチ（オープン）',
    'xmatch': 'Xマッチ',
    'event': 'イベントマッチ',
    'private': 'プライベートマッチ',
    'private_four_vs_four': 'プライベートマッチ（4対4）',
    'private_three_vs_three': 'プライベートマッチ（3対3）',
    'private_two_vs_two': 'プライベートマッチ（2対2）',
    'private_one_vs_one': 'プライベートマッチ（1対1）',
    'private_other': 'プライベートマッチ（その他）',
    'salmon_regular': 'いつものバイト',
    'big_run': 'ビッグラン',
    'team_contest': 'バイトチームコンテスト',
    'hold': '区分保留',
}

RULE_LABELS = {
    'TURF_WAR': 'ナワバリバトル',
    'AREA': 'ガチエリア',
    'LOFT': 'ガチヤグラ',
    'GOAL': 'ガチホコバトル',
    'CLAM': 'ガチアサリ',
    'REGULAR': 'いつものバイト',
    'BIG_RUN': 'ビッグラン',
    'TEAM_CONTEST': 'バイトチームコンテスト',
}

GENRE_ORDER = (
    'fest',
    'nawabari',
    'bankara_challenge',
    'bankara_open',
    'xmatch',
    'event',
    'private',
    'private_four_vs_four',
    'private_three_vs_three',
    'private_two_vs_two',
    'private_one_vs_one',
    'private_other',
    'salmon_regular',
    'big_run',
    'team_contest',
    'hold',
)

RULE_ORDER = (
    'TURF_WAR',
    'AREA',
    'LOFT',
    'GOAL',
    'CLAM',
    'REGULAR',
    'BIG_RUN',
    'TEAM_CONTEST',
)

def format_genre(genre):
    """ジャンルコードを正式名称に変換する。未知値は消さず表示する。"""
    if not genre:
        return ''
    if genre in GENRE_LABELS:
        return GENRE_LABELS[genre]
    if genre.startswith('private_'):
        suffix = genre[len('private_'):]
        return f'プライベートマッチ（{suffix}）'
    return f'未対応のジャンル（{genre}）'

def format_rule(rule_raw):
    """ルールコードを正式名称に変換する。未知値は消さず「未対応のルール（値）」を表示する。"""
    if not rule_raw:
        return ''
    if rule_raw in RULE_LABELS:
        return RULE_LABELS[rule_raw]
    return f'未対応のルール（{rule_raw}）'

def genre_sort_index(genre):
    """ジャンルの並び替え順序インデックスを返す。"""
    if genre in GENRE_ORDER:
        return GENRE_ORDER.index(genre)
    if genre and genre.startswith('private'):
        return GENRE_ORDER.index('private_other') + 0.5
    return len(GENRE_ORDER) + 1

def rule_sort_index(rule_raw):
    """ルールの並び替え順序インデックスを返す。"""
    if rule_raw in RULE_ORDER:
        return RULE_ORDER.index(rule_raw)
    return len(RULE_ORDER) + 1
