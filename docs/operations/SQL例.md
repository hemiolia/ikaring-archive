# SQLクエリ例

本ドキュメントでは、本ツールで生成されるSQLiteデータベース（`schema.sql` に基づくテーブルおよびビュー）に対する実践的なSQLクエリ例を掲載します。

CLIの `archive.py sql "<QUERY>"` コマンドや、任意のSQLiteクライアント（sqlite3 CLI、GUIツール等）から直接実行できます。

---

## 1. 対戦時系列の取得

分析はジャンル別のビューを使う。`battles` は所在確認用で、オープンとXマッチとイベマをまとめて集計しない。次はオープンの4対4相当（回線落ちで人数が欠けた試合も含む）だけを新しい順に見る例である。

```sql
SELECT
    account,
    match_key,
    played_time,
    rule_raw,
    rule_name,
    stage,
    judgement,
    knockout,
    duration
FROM analysis_bankara_open
ORDER BY played_time DESC
LIMIT 50;
```

---

## 2. 本人のブキ別成績

`analysis_bankara_challenge` と `battle_players` を `account` および `match_key` で結合し、チャレンジに限って本人（`is_myself = 1`）のブキごとの試合数と勝敗を集計する。他ジャンルとは足さない。

※ 注意: `kills` 列はイカリング3のAPIレスポンスに含まれる raw 値（`$.result.kill`）そのものを格納したものであり、ゲーム内UIにおける「キル数」や「アシスト合算値」などの具体的な定義や推定値と断定せず、取得された生の数値として扱います。

```sql
SELECT
    p.weapon,
    COUNT(*) AS total_matches,
    SUM(CASE WHEN b.judgement = 'WIN' THEN 1 ELSE 0 END) AS wins,
    SUM(CASE WHEN b.judgement = 'LOSE' OR b.judgement = 'DEFEAT' THEN 1 ELSE 0 END) AS losses,
    AVG(p.kills) AS avg_kills_raw,
    AVG(p.deaths) AS avg_deaths,
    AVG(p.assists) AS avg_assists,
    AVG(p.paint) AS avg_paint
FROM analysis_bankara_challenge b
JOIN battle_players p
    ON b.account = p.account
   AND b.match_key = p.match_key
WHERE p.is_myself = 1
GROUP BY p.weapon
ORDER BY total_matches DESC;
```

---

## 3. サーモンランのWAVEおよびオオモノ情報

`salmon_runs`、`salmon_waves`、`salmon_bosses` ビューを用いて、バイトごとのWAVE詳細およびオオモノシャケの出現・討伐状況を抽出します。

### (A) 各試合のWAVE詳細一覧

```sql
SELECT
    r.account,
    r.match_key,
    r.played_time,
    r.stage,
    r.danger_rate,
    r.result_wave,
    w.wave_index,
    json_extract(w.json_text, '$.waveNumber') AS wave_number,
    json_extract(w.json_text, '$.waterLevel') AS water_level,
    json_extract(w.json_text, '$.eventWave.name') AS event_name,
    json_extract(w.json_text, '$.teamDeliverCount') AS team_deliver_count,
    json_extract(w.json_text, '$.goldenPopCount') AS golden_pop_count
FROM salmon_runs r
JOIN salmon_waves w
    ON r.account = w.account
   AND r.match_key = w.match_key
ORDER BY r.played_time DESC, w.wave_index ASC
LIMIT 50;
```

### (B) オオモノシャケごとの討伐・出現集計

```sql
SELECT
    json_extract(b.json_text, '$.enemy.name') AS boss_name,
    SUM(json_extract(b.json_text, '$.defeatCount')) AS my_defeat_count,
    SUM(json_extract(b.json_text, '$.teamDefeatCount')) AS team_defeat_count,
    SUM(json_extract(b.json_text, '$.popCount')) AS pop_count
FROM salmon_bosses b
GROUP BY json_extract(b.json_text, '$.enemy.name')
ORDER BY team_defeat_count DESC;
```

---

## 4. 任意項目の横断検索 (all_fields)

`all_fields` ビューは全レスポンスのJSONを `json_tree` で展開したビューです。スキーマで個別に列定義されていないプロパティや将来追加された項目をパスやキー名で検索できます。

例: バッジに関連するプロパティを検索

```sql
SELECT
    response_id,
    account,
    operation,
    fullkey,
    path,
    type,
    atom,
    value
FROM all_fields
WHERE fullkey LIKE '%badge%'
LIMIT 100;
```

---

## 5. 未取得の詳細レコード確認 (pending_details)

`pending_details` ビューは一覧クエリ（`*BattleHistoriesQuery` や `CoopHistoryQuery`）で発見されたものの、詳細取得クエリがまだ実行・紐付けされていない試合レコードを示します。

```sql
SELECT
    account,
    kind,
    match_key,
    first_seen,
    last_seen,
    detail_response_id
FROM pending_details
ORDER BY first_seen DESC;
```

---

## 6. 取得失敗および異常イベントの確認 (issues)

`issues` テーブルには、ネットワークエラー、部分応答、必須フィールド欠落、履歴の連続性断絶などの監査イベントが記録されます。

```sql
SELECT
    i.id,
    i.run_id,
    i.response_id,
    i.code,
    i.context,
    i.created_at
FROM issues i
ORDER BY i.created_at DESC
LIMIT 50;
```
