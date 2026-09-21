"""分析ビューを人が開く xlsx にする。正本ではない。編集はデータベースへ戻らない。"""
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

CELL_LIMIT = 32767
NS_MAIN = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
NS_REL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
NS_PKG = 'http://schemas.openxmlformats.org/package/2006/relationships'
NS_CT = 'http://schemas.openxmlformats.org/package/2006/content-types'

VS_HEADERS = ('対戦日時','ルール','ルールコード','ステージ','勝敗','ノックアウト','試合秒','自分側人数','相手人数','ブキ','キル','アシスト','デス','スペシャル','塗り','タグ','試合ID')
SALMON_HEADERS = ('プレイ日時','ルールコード','ステージ','危険度','WAVE','試合ID')
HOLD_HEADERS = ('種別','区分','APIモード','ルール','人数区分','試合ID')

SHEETS = (
    ('ナワバリ','analysis_nawabari','vs'),
    ('オープン','analysis_bankara_open','vs'),
    ('チャレンジ','analysis_bankara_challenge','vs'),
    ('イベマ','analysis_event','vs'),
    ('Xマッチ','analysis_xmatch','vs'),
    ('フェス','analysis_fest','vs'),
    ('プラベ4対4','analysis_private_four_vs_four','vs'),
    ('プラベ3対3','analysis_private_three_vs_three','vs'),
    ('プラベ2対2','analysis_private_two_vs_two','vs'),
    ('プラベ1対1','analysis_private_one_vs_one','vs'),
    ('プラベその他','analysis_private_other','vs'),
    ('バイト','analysis_salmon_regular','salmon'),
    ('ビッグラン','analysis_big_run','salmon'),
    ('バイトチームコンテスト','analysis_team_contest','salmon'),
    ('保留','analysis_hold','hold'),
)

def _col(n):
    s=''
    while n:
        n,r=divmod(n-1,26)
        s=chr(65+r)+s
    return s

def _cell(ref,value):
    if value is None or value=='':return ''
    if isinstance(value,(int,float)) and not isinstance(value,bool):
        return f'<c r="{ref}"><v>{value}</v></c>'
    text=escape(str(value))
    if len(text)>CELL_LIMIT:raise ValueError('CELL_TOO_LONG')
    return f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{text}</t></is></c>'

def _sheet(rows):
    body=['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',f'<worksheet xmlns="{NS_MAIN}"><sheetData>']
    for r,row in enumerate(rows,1):
        cells=''.join(_cell(f'{_col(c)}{r}',value) for c,value in enumerate(row,1))
        body.append(f'<row r="{r}">{cells}</row>')
    body.append('</sheetData></worksheet>')
    return ''.join(body)

def _vs_rows(db,view):
    sql=f'''SELECT a.played_time,a.rule_name,a.rule_raw,a.stage,a.judgement,a.knockout,a.duration,a.my_player_count,a.opponent_counts,
        (SELECT p.weapon FROM battle_players p WHERE p.account=a.account AND p.match_key=a.match_key AND p.is_myself=1 LIMIT 1),
        (SELECT p.kills FROM battle_players p WHERE p.account=a.account AND p.match_key=a.match_key AND p.is_myself=1 LIMIT 1),
        (SELECT p.assists FROM battle_players p WHERE p.account=a.account AND p.match_key=a.match_key AND p.is_myself=1 LIMIT 1),
        (SELECT p.deaths FROM battle_players p WHERE p.account=a.account AND p.match_key=a.match_key AND p.is_myself=1 LIMIT 1),
        (SELECT p.specials FROM battle_players p WHERE p.account=a.account AND p.match_key=a.match_key AND p.is_myself=1 LIMIT 1),
        (SELECT p.paint FROM battle_players p WHERE p.account=a.account AND p.match_key=a.match_key AND p.is_myself=1 LIMIT 1),
        (SELECT group_concat(t.tag,'、') FROM match_tags t WHERE t.account=a.account AND t.match_key=a.match_key),
        a.match_key
        FROM {view} a ORDER BY a.played_time,a.match_key'''
    return [VS_HEADERS]+[tuple(row) for row in db.execute(sql)]

def _salmon_rows(db,view):
    sql=f'''SELECT played_time,rule_raw,stage,danger_rate,result_wave,match_key FROM {view} ORDER BY played_time,match_key'''
    return [SALMON_HEADERS]+[tuple(row) for row in db.execute(sql)]

def _hold_rows(db):
    sql='''SELECT kind,genre,mode_raw,rule_raw,roster_class,match_key FROM analysis_hold ORDER BY kind,match_key'''
    return [HOLD_HEADERS]+[tuple(row) for row in db.execute(sql)]

def _rows(db,kind,view):
    if kind=='vs':return _vs_rows(db,view)
    if kind=='salmon':return _salmon_rows(db,view)
    return _hold_rows(db)

def export_xlsx(store,destination):
    destination=Path(destination)
    destination.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    tables={row[0] for row in store.db.execute("SELECT name FROM sqlite_master WHERE type='view'")}
    missing=[view for _,view,_ in SHEETS if view not in tables]
    if missing:raise ValueError('MISSING_ANALYSIS_VIEW')
    counts={}
    sheets=[('説明',_sheet([
        ('このファイルはデータベースから作り直します。',),
        ('セルを編集してもデータベースには戻りません。',),
        ('シートをまたいで集計しないでください。',),
        ('原文の応答は Excel のセルに入らないため、ここには入れていません。',),
        ('プラベの2対2を、自動でイカップルとは記録していません。',),
    ]))]
    counts['説明']=5
    for name,view,kind in SHEETS:
        rows=_rows(store.db,kind,view)
        sheets.append((name,_sheet(rows)))
        counts[name]=max(0,len(rows)-1)
    tmp=destination.with_suffix(destination.suffix+'.tmp')
    with zipfile.ZipFile(tmp,'w',compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml',f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="{NS_CT}"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>{''.join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(1,len(sheets)+1))}</Types>''')
        z.writestr('_rels/.rels',f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{NS_PKG}"><Relationship Id="rId1" Type="{NS_REL}/officeDocument" Target="xl/workbook.xml"/></Relationships>''')
        workbook_sheets=''.join(f'<sheet name="{escape(name)}" sheetId="{i}" r:id="rId{i}"/>' for i, (name,_) in enumerate(sheets,1))
        z.writestr('xl/workbook.xml',f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="{NS_MAIN}" xmlns:r="{NS_REL}"><sheets>{workbook_sheets}</sheets></workbook>''')
        rels=''.join(f'<Relationship Id="rId{i}" Type="{NS_REL}/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in range(1,len(sheets)+1))
        z.writestr('xl/_rels/workbook.xml.rels',f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{NS_PKG}">{rels}</Relationships>''')
        for i,(_,xml) in enumerate(sheets,1):z.writestr(f'xl/worksheets/sheet{i}.xml',xml)
    tmp.replace(destination)
    destination.chmod(0o600)
    return {'path':str(destination),'rows':counts,'canonical':'sqlite','edits_return_to_database':False}
