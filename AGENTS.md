# AGENTS.md — 進行役マニュアル（短編SF「宇宙船のボーイミーツガール」生成）

このファイルは、このディレクトリで動く**進行役エージェント**（Hermes Agent / OpenCode など、シェルコマンドを実行できるエージェント）向けの指示です。
あなたは作者ではありません。小説は Aion 3.5 Mini（OpenRouter 経由）が書きます。あなたの仕事は、進行エンジン `tools/novelctl.py` を動かし、止まったときに正しく対応し、完成まで見届けることです。

---

## 0. 最重要ルール（毎回読むこと）

1. **状態はディスクにしかない。** 自分の会話の記憶を信用しない。迷ったら `python3 tools/novelctl.py status` を実行し、表示された `NEXT:` に従う。
2. **進行は `novelctl.py` だけで行う（通常は `start`）。** パケット（モデルへの指示書）の組み立て、モデル呼び出し、検査、LEDGER更新、状態保存はすべてこのコマンドが行う。あなた自身がモデルとして本文・設定書・審査結果を書いてはいけない。
3. **本文・設定を自分で書かない・直さない。** アイデアも足さない。物語の中身を決めるのは Aion と `canon/CANON.md` だけ。
4. **ファイルを編集しない。** `canon/`・`chapters/`・`state/`・`BRIEF.md`・`prompts/`・`templates/`・`config.json` は読むだけ。変更が必要なときは人に頼む。
5. **章の本文を丸ごと会話に読み込まない。** 自分のコンテキストを小説で埋めると手順を忘れる。読んでよいのは `status` の出力、`work/*_feedback.md`、`work/*_verdict_*.json`、`output/final_report.md`、`logs/run.log` の末尾（`tail -20`）だけ。
6. `ESCALATE` が出たら、**第5節の対応表だけ**に従う。判断がつかなければ人に報告して止まる。
7. `BUSY` が出たら、別の novelctl が動いている。新しく起動せず、数分おいて `status` を見る。
8. **進めるときは `python3 tools/novelctl.py start` を使う。** `step` / `run` を制限時間のあるツール（Pythonカーネル、300秒制限のターミナル等）から直接実行すると、途中で殺されて呼び出しが無駄になる。

---

## 1. ファイル構成

```
./                           ← プロジェクトディレクトリ（エージェントはここで起動する）
├── AGENTS.md                ← このファイル
├── BRIEF.md                 ← 執筆指示書（章立て・伏線配置・結末の方向性）
├── config.json              ← 字数・試行回数・モデル設定（llm.profiles）
├── templates/               ← CANON（設定書）と LEDGER（進行台帳）の雛形
├── prompts/                 ← 各工程の依頼文
├── tools/
│   ├── novelctl.py          ← 進行エンジン（状態管理・パケット組み立て・検査）
│   └── call_llm.py          ← OpenRouter 呼び出し（標準ライブラリのみ）
├── canon/CANON.md           ← 【生成物】設定書。第0フェーズ後にロック
├── state/progress.json      ← 【生成物】進行状態。「現在地」の唯一の正
├── state/LEDGER.md          ← 【生成物】章ごとの要約・確定事実・伏線状態・数値
├── chapters/chNN.md         ← 【生成物】確定した各章
├── work/                    ← パケット、生の出力、下書き、審査結果、修正指示
├── output/final.md          ← 【完成品】結合済みの短編
├── output/final_report.md   ← 【完成品】最終審査レポート
└── logs/run.log             ← 全ステップの記録（APIのトークン使用量も）
```

---

## 2. セットアップ（人が最初に1回）

1. OpenRouter の API キーを用意する。`call_llm.py` は次の順に探すので、どれか1つでよい。
   - 環境変数 `NOVEL_LLM_API_KEY` / `OPENROUTER_API_KEY` / `HERMES_CUSTOM_OPENROUTER_API_KEY`（`config.json` の `llm.api_key_env`）
   - プロジェクト直下の `.env` ファイル（1行 `OPENROUTER_API_KEY=sk-or-...`）。**エージェントのシェルに環境変数が引き継がれない場合はこちらが確実。** 人が作成し、共有・コミットしない。
2. `python3 tools/call_llm.py --ping` が `PING OK` を返すことを確認する（キー・モデル名・通信の確認。費用はごくわずか）。
3. `python3 tools/novelctl.py init`
4. `python3 tools/novelctl.py status` で `NEXT: ... step` が出ることを確認する。

モデルは `config.json` の `llm.profiles` で指定する（既定：執筆・審査とも `aion-labs/aion-3.5-mini`）。審査役だけ別のモデルにすると、自己採点の甘さを減らせる。

> **なぜモデル呼び出しを毎回別にするのか**：同じ会話の中で8章を書かせると、会話が長くなるにつれて前半の設定が押し出され、物語が破綻する。`call_llm.py` は毎回「CANON＋LEDGER＋前章の結び＋この章の指示」だけを詰めた新しいリクエストを送るので、どの章もプロットを完全に持った状態で書かれる。あなた（進行役）の会話履歴は執筆側に一切渡らない。

---

## 3. メインループ

### 推奨：start（切り離して実行）
```
python3 tools/novelctl.py start      # すぐ戻る。本体はシェルから切り離されて最後まで走る
その後は2〜5分おきに：
  python3 tools/novelctl.py status
status が DONE → 第6節 / ESCALATE → 第5節 / RUNNING → 待つ / STALE → もう一度 start
```
- `start` は `run` を別セッションのプロセスとして起動する。エージェントのツールに制限時間（例：300秒）があっても、殺されない。
- `status` の `CALLING:` 行が呼び出し中の様子を示す。`推論` や `本文` の字数が増えていれば正常。
- 推論が長いモデルなので、1回の呼び出しに5〜20分かかることがある。`最終受信` が数十秒以内なら待つ。
- 無通信が `llm.idle_timeout_sec`（既定180秒）続いた接続、同じ文字列の繰り返しが止まらなくなった出力は、`call_llm.py` が自動で切って再試行する。進行役が殺す必要はない。
- **`step` や `run` を、制限時間のあるツールから手前で実行しない。** 途中で殺されると、その呼び出しに使った時間と費用が無駄になる。

### 1ステップずつ確かめたいとき
```
python3 tools/novelctl.py step    （コマンドの制限時間を 30 分以上にできる場合だけ）
```
- 中断・再起動・コンテキスト圧縮が起きても、`status` → `step`（または `run`）で正確に再開できる。

---

## 4. 工程の中身（理解のため。手で実行する必要はない）

### 第0フェーズ：設計（CANON）
| 手順 | 内容 | 失敗したら |
|---|---|---|
| design | BRIEF＋CANONテンプレートを渡し、Aion が CANON を創作 | 機械検査（未記入・章カード欠落・スロット欠落・字数超過）で不合格なら修正指示つきで再設計 |
| canon-review | BRIEF の要件に照らして CANON を審査（JSON） | high 指摘があれば修正指示つきで再設計（指摘箇所だけ直す） |
| lock | 合格した CANON のハッシュを保存 | 以後の変更は `CANON_MODIFIED` で検知 |

`config.json` の `pause_after_canon` を `true` にすると、ロック後に人の確認待ちで止まる（`approve-canon` で再開）。

### 第1〜8章：章ループ
| 状態 | step がすること |
|---|---|
| pending / rewrite | 執筆パケットを組み立てて Aion に渡す。本文を抽出。途中で切れていれば「続き」を1回だけ依頼して連結 |
| drafted | 機械検査（字数・途中切れ・管理用語の混入・書式）→ 審査（CANON矛盾・第三の人物・早すぎる回収・伏線の実施・章の機能・連続性）→ 合否 |
| 不合格 | 修正指示（`work/chNN_feedback.md`）つきで書き直し。最大 `max_attempts` 稿 |
| accepted | `chapters/chNN.md` に確定。LEDGER の `## CHnn` 節を作らせ、書式と伏線の記録を検査して追記 |
| done | 次の章へ |

**執筆パケットの中身（固定順）**：共通執筆ルール → CANON（章カード以外）→ この章の機能 → この章と次章のカード → LEDGER（直近2章は全項目、それ以前は要約・事実・伏線のみ）→ 前章の最後600字 → 必須の伏線操作と「まだ明かさないこと」→ 依頼文。

### 最終工程
| 手順 | 内容 |
|---|---|
| assemble | 全章を `output/final.md` に結合。合計字数と、全伏線操作が LEDGER にあるかを機械検査 |
| final review | CANON＋LEDGER全体＋第8章で最終審査 → `output/final_report.md` |
| 第8章のみの重大指摘 | 第8章を1回だけ自動で書き直す |
| それ以外の重大指摘 | ESCALATE（人の判断） |

---

## 5. エスカレーション対応表

| コード | 意味 | あなたがすること |
|---|---|---|
| `CALL_NOT_FOUND` / `CALL_FAILED` | モデル呼び出しの失敗 | `work/*.out.stderr` の最後の数行を人に報告（APIキーが見つからない＝第2節の1、HTTP 401=キー、404=モデル名、402=残高、429=混雑）。`python3 tools/call_llm.py --ping` が通るようになったら `resolve` |
| `CALL_TIMEOUT` | 応答が遅すぎる | 人に報告。`config.json` の `timeout_sec` を増やしてもらったら `resolve` |
| `DESIGN_EXHAUSTED` | CANON が規定回数で完成しない | `work/canon_feedback.md` を人に見せる。人が直したら `resolve --retry` |
| `CHAPTER_EXHAUSTED` | 章が規定稿数で合格しない | `work/chNN_feedback.md` の要点を人に報告。(a) 人が下書きを直した → `resolve --accept`、(b) 人が修正指示を書き換えた → `resolve --retry` |
| `LEDGER_FAILED` | 章の記録が書式を満たさない | `work/chNN_ledger_a*.out` を人に見せる。人が `state/LEDGER.md` に節を追記したら `resolve --ledger-done` |
| `CANON_MODIFIED` | ロック後に CANON が変わった | 人に確認。意図した変更なら `resolve --relock`、そうでなければ元に戻してもらって `resolve` |
| `FINAL_REVIEW_FAILED` | 最終審査で第8章以外に重大指摘 | `output/final_report.md` を人に見せる。人の判断で `reset-chapter N` か `resolve --done` |

**やってはいけない対応**：本文や CANON を自分で書き換える／`state/progress.json` を手で編集する／試行回数の上限を勝手に上げる／自分でモデルの代わりに原稿や審査JSONを作る。

---

## 6. 完了報告

`step` が終了コード3（または `status` が `DONE`）になったら、次だけを短く報告して終了する。
- `output/final.md` のタイトルと合計字数
- `output/final_report.md` の「最終審査」「講評」「各章の警告」
- 書き直しが多かった章（`status` の稿数が2以上の章）

---

## 7. 調整のつまみ（config.json、人が変更する）

| キー | 既定 | 目安 |
|---|---|---|
| `chapter_chars` | 2,300〜2,900（目標2,500） | 章の字数レンジ |
| `max_attempts` | 3 | 1章あたりの最大稿数 |
| `ledger_recent_full` | 2 | LEDGER を全項目で渡す直近章数 |
| `prev_tail_chars` | 600 | 前章の結びとして渡す字数 |
| `pause_after_canon` | false | true で CANON 確定後に人の確認を挟む |
| `llm.profiles.writer` | temperature 0.85 / reasoning high | 文体が荒れるなら temperature を下げる |
| `llm.profiles.reviewer` | temperature 0.2 | 別モデルにすると審査が厳しくなる |
| `slot_plan` | BRIEF 4-2 と同じ | 伏線の配置表。**BRIEF.md と必ず同時に変更する** |

---

## 8. 手動操作（人に頼まれたときだけ）

```
python3 tools/novelctl.py packet write 3     # 第3章の執筆パケットを作って確認（呼び出しはしない）
python3 tools/novelctl.py check 3            # 第3章の下書き（なければ確定稿）を機械検査
python3 tools/novelctl.py reset-chapter 5    # 第5章以降を未執筆に戻す（旧ファイルは work/archive/ へ）
python3 tools/novelctl.py assemble           # 確定済みの章だけで output/final.md を作る
python3 tools/novelctl.py epub               # 縦書きEPUB と output/book_meta.json を作る（校正後は --proofread）
```

## 9. 出版と読者評価（完成後、人の判断で）

1. Claude が `output/final.md` を校正し、`output/final_proofread.md` と `output/proofread_notes.md` を作る。
2. 人が校正内容を確認し、`python3 tools/novelctl.py epub --proofread` で縦書きEPUBを作る（`output/<タイトル>.epub`）。各章末に評価ページ（`config.json` の `publish.rating_url`）へのリンクが入る。
3. 評価ページを開き、「作品情報を読み込む」に `output/book_meta.json` の中身を貼り付けて保存する（編集者のみ）。
4. 読者（家族など）に評価ページを共有する。評価を保存するには「コントリビューター」以上の権限が必要。
