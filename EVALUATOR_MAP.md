# EVALUATOR_MAP（書類選考担当マッピング）

`talentio_client.py stagesync` が「どのポジションの候補者を、誰の書類選考として
セットするか」を決めるための対応表。

## 保存場所

**リポジトリではなく、クラウド環境の環境変数 `EVALUATOR_MAP`**（1行の JSON 文字列）。
社員のメールアドレスを含むため、実際の値はここにコミットしない。

変更手順: claude.ai/code/routines → 対象ルーティン → 鉛筆アイコン → **Instructions**
欄の下の環境アイコン（`Default` 等）→ 環境の設定 → **Environment variables** の
`EVALUATOR_MAP` を差し替えて保存。次回実行から反映される。

## 形式

キーは requisition 名の先頭にある `【nnn】` の数字（文字列）。

```json
{
  "509": { "evaluators": [ { "name": "氏名", "email": "x_yyy@uluru.jp" } ] },
  "519": { "evaluators": [ { "name": "氏名A", "email": "a@uluru.jp" },
                           { "name": "氏名B", "email": "b@uluru.jp" } ] }
}
```

- `evaluators` は配列。複数名を入れると全員に評価が割り当てられる（例 519）。
- `email` は Talentio API の `evaluations[].employee` にそのまま渡る。
  **メールが誤っていると POST が失敗するか、別人に割り当たる。**
- メールの規則は不統一（`s_majima_wh6@`, `k_asano_3wk@` のように不規則な接尾辞が
  付く人がいる）。**推測せず Slack のプロフィール等で実値を確認する。**
- `evaluators` 以外のキー（`tag` 等）はスクリプトが無視するので、メモとして
  足しても動作に影響はない。

## ポジションを追加する手順

1. requisition 名から番号を確認（例 `【525】fondesk_カスタマーサクセス` → `525`）。
2. 担当者の実際のメールアドレスを確認する（Slack ユーザー検索が確実。同姓の別人に
   注意し、プロフィールの部署／タイトルが対象ポジションと整合するか確認する）。
3. `EVALUATOR_MAP` に追記し、環境変数を更新。
4. `--dry-run` で構文と解決を確認（書き込み・Slack 投稿は発生しない）:
   ```
   python3 talentio_client.py stagesync --hours 72 --dry-run
   ```

## タグ（中途／新卒）について

スクリプトはタグで絞り込んでいない。対象になる条件は以下のみ:

- 流入経路が **ビズリーチ** または **channelType == agent**
- ステージ未設定 かつ `status == ongoing`
- **requisition 名に `【nnn】` があり、その番号が `EVALUATOR_MAP` にある**

新卒系（`【28卒】ビジネス職サマーインターン` 等）は `【nnn】` 形式の番号を持たない
ため `_pos_num` が `None` を返し、自動的に対象外になる。結果として中途のみが
処理される。中途／新卒の出し分けのために設定は不要。

## 未登録のポジション（要確認）

`--dry-run` の結果に `(no evaluator map for req nnn)` として出る。番号を持つのに
未登録のポジションは、新規候補者が来ても**セットされずスキップされる**。

- **`524` 徳島_BPOディレクター** — 未登録。現時点の候補者は既にステージ設定済みの
  ため実害は出ていないが、新規流入時にスキップされる。担当者を確認のうえ追加を検討。
