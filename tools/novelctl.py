#!/usr/bin/env python3
"""novelctl — 短編生成パイプラインの状態管理・パケット組み立て・検証ツール

進行役エージェント（Hermes / OpenCode / Goose など、シェルを実行できるものなら何でも）はこのツールだけを使って進める。
モデル呼び出しは tools/call_llm.py が OpenRouter へ直接行うため、ハーネスには依存しない。物語の状態はすべてディスク上にあり、
会話履歴に依存しない。どこで中断しても `status` → `step` で再開できる。

  python3 tools/novelctl.py init            初期化（最初に1回）
  python3 tools/novelctl.py status          現在地と次の一手を表示
  python3 tools/novelctl.py step            次の1ステップだけ実行（モデル呼び出しは最大2回）
  python3 tools/novelctl.py start           run をシェルから切り離して起動し、すぐ戻る（推奨）
  python3 tools/novelctl.py run [--max N]   完了かエスカレーションまで step を繰り返す（手前で実行し続ける）
  python3 tools/novelctl.py packet KIND [N] パケットを組み立てて保存だけする（手動確認用）
       KIND = design | canon-review | write | review | ledger | final
  python3 tools/novelctl.py check N         第N章の下書き（work/）または確定稿を機械検査
  python3 tools/novelctl.py approve-canon   pause_after_canon=true のとき、CANON確認後に再開
  python3 tools/novelctl.py resolve [--retry]  エスカレーションを解除（--retry で試行回数をリセット）
  python3 tools/novelctl.py reset-chapter N 第N章以降を未執筆に戻す（LEDGERも巻き戻す）
  python3 tools/novelctl.py assemble        確定済みの章を output/final.md に結合
  python3 tools/novelctl.py epub [--proofread]  縦書きEPUBと評価ページ用の作品情報を output/ に作る

終了コード: 0=前進 / 2=エスカレーション（人の判断が必要） / 3=完了 / 1=エラー
"""
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
N_CH = CFG["chapters"]
PROGRESS = ROOT / "state" / "progress.json"
LEDGER = ROOT / "state" / "LEDGER.md"
CANON = ROOT / "canon" / "CANON.md"
EV_JA = {"planted": "設置する", "reinforced": "補強する（読者に再提示する）", "recovered": "回収する"}
KANJI = "〇一二三四五六七八九十"


class Escalate(Exception):
    def __init__(self, code, detail, hint=""):
        super().__init__(detail)
        self.code, self.detail, self.hint = code, detail, hint


# ---------------------------------------------------------------- 基本
def P(*a):
    return ROOT.joinpath(*a)


def rd(path):
    return Path(path).read_text(encoding="utf-8")


def wr(path, s):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(s, encoding="utf-8")


def log(msg):
    P("logs").mkdir(exist_ok=True)
    with open(P("logs", "run.log"), "a", encoding="utf-8") as f:
        f.write(f"{datetime.datetime.now().isoformat(timespec='seconds')} {msg}\n")


def nn(n):
    return f"{int(n):02d}"


def count_chars(s):
    return len(re.sub(r"\s", "", s))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load():
    if not PROGRESS.exists():
        sys.exit("未初期化です。先に `python3 tools/novelctl.py init` を実行してください。")
    return json.loads(PROGRESS.read_text(encoding="utf-8"))


def save(p):
    wr(PROGRESS, json.dumps(p, ensure_ascii=False, indent=2))


def between(text, b, e):
    i = text.find(b)
    if i < 0:
        return None
    j = text.find(e, i + len(b))
    if j < 0:
        return None
    return text[i + len(b):j].strip()


# ---------------------------------------------------------------- 素材の切り出し
def rules():
    return between(rd(P("BRIEF.md")), "<!-- RULES:BEGIN -->", "<!-- RULES:END -->")


def role(n):
    return between(rd(P("BRIEF.md")), f"<!-- ROLE:{nn(n)} -->", f"<!-- /ROLE:{nn(n)} -->")


def canon():
    return rd(CANON)


def card(n):
    return between(canon(), f"<!-- CARD:{nn(n)} -->", f"<!-- /CARD:{nn(n)} -->")


def canon_core():
    return re.sub(r"<!-- CARD:(\d\d) -->.*?<!-- /CARD:\1 -->", "", canon(), flags=re.S).strip()


def events_for(n):
    return [(sid, ev) for sid, evs in CFG["slot_plan"].items() for c, ev in evs if c == n]


def recover_ch(sid):
    rc = [c for c, ev in CFG["slot_plan"][sid] if ev == "recovered"]
    return rc[0] if rc else None


def events_text(n):
    ev = events_for(n)
    if not ev:
        return "（この章で必須の伏線操作はない）"
    return "\n".join(f"- {sid}（{CFG['slot_names'][sid]}）：{EV_JA[e]}。中身と見せ方はCANONの第7節「{sid}」の行に従う。"
                     for sid, e in ev)


def hide_text(n):
    items = [(sid, recover_ch(sid)) for sid in CFG["slot_plan"] if recover_ch(sid) and recover_ch(sid) > n]
    if not items:
        return "（なし：すべて回収済み、または本章で回収）"
    return "\n".join(f"- {sid}（{CFG['slot_names'][sid]}）の中身：第{c}章まで明かさない" for sid, c in items)


def ledger_sections(text):
    parts = re.split(r"^## CH(\d\d)\s*$", text, flags=re.M)
    secs = {}
    for k in range(1, len(parts), 2):
        secs[int(parts[k])] = parts[k + 1].strip()
    return parts[0], secs


def ledger_for_packet(n):
    _, secs = ledger_sections(rd(LEDGER))
    full = CFG["ledger_recent_full"]
    out = []
    for c in sorted(secs):
        if c >= n:
            continue
        body = secs[c]
        if c < n - full:  # 古い章は要約・事実・伏線状態だけに圧縮
            body = "\n".join(l for l in body.splitlines() if l.startswith(("SUMMARY:", "FACT:", "SLOT:")))
        out.append(f"## CH{nn(c)}\n{body}")
    return "\n\n".join(out) if out else "（まだ記録なし：これが最初の章）"


def chapter_body(text):
    lines = text.strip().splitlines()
    return "\n".join(lines[1:]).strip() if lines else ""


def prev_tail(n):
    if n <= 1:
        return "（なし：これが第1章。物語はここから始まる）"
    body = chapter_body(rd(P("chapters", f"ch{nn(n - 1)}.md")))
    k = CFG["prev_tail_chars"]
    return ("……" + body[-k:]) if len(body) > k else body


def fill(tpl, n=None):
    c = CFG["chapter_chars"]
    rep = {"TARGET": c["target"], "MINC": c["min"], "MAXC": c["max"]}
    if n is not None:
        rep.update({"N": n, "NN": nn(n), "EVENTS": events_text(n), "HIDE": hide_text(n)})
    for k, v in rep.items():
        tpl = tpl.replace("{{" + k + "}}", str(v))
    return tpl


def sec(title, body):
    # 区切りに記号の連続（==== など）を使うと、モデルがそれを真似て反復暴走することがあるため、タグで囲む
    return f"\n\n<資料 名前=\"{title}\">\n{body.strip()}\n</資料>\n"


# ---------------------------------------------------------------- パケット
def build_packet(kind, n=None):
    tag = f"<!-- TASK: {kind}" + (f" CH={nn(n)}" if n else "") + " -->"
    s = tag + "\nこれは一回きりの依頼です。ツールやファイル操作は使わず、指定された形式の文章だけを返答してください。"
    if kind == "design":
        fb = P("work", "canon_feedback.md")
        if CANON.exists() and fb.exists():
            s += sec("BRIEF", rd(P("BRIEF.md")))
            s += sec("前回のCANON", canon())
            s += sec("修正指示", rd(fb))
            s += sec("依頼", rd(P("prompts", "design_fix.md")))
        else:
            s += sec("BRIEF", rd(P("BRIEF.md")))
            s += sec("CANONテンプレート", rd(P("templates", "CANON_TEMPLATE.md")))
            s += sec("依頼", rd(P("prompts", "design.md")))
    elif kind == "canon-review":
        s += sec("BRIEF", rd(P("BRIEF.md")))
        s += sec("CANON", canon())
        s += sec("依頼", rd(P("prompts", "canon_review.md")))
    elif kind == "write":
        draft, fb = P("work", f"ch{nn(n)}_draft.md"), P("work", f"ch{nn(n)}_feedback.md")
        s += sec("共通執筆ルール", rules())
        s += sec("CANON（設定書・章カード以外）", canon_core())
        s += sec(f"この章の機能（BRIEF 第{n}章）", role(n))
        s += sec(f"この章のカード（CANON 第{n}章）", card(n))
        if n < N_CH:
            s += sec(f"参考：次章のカード（第{n + 1}章。この章では書かない。章末の引きをここへつなぐ）", card(n + 1))
        s += sec("LEDGER（これまでの記録）", ledger_for_packet(n))
        s += sec("前章の結び", prev_tail(n))
        if draft.exists() and fb.exists():
            s += sec("前回の原稿", rd(draft))
            s += sec("修正指示", rd(fb))
            s += sec("依頼", fill(rd(P("prompts", "rewrite.md")), n))
        else:
            s += sec("依頼", fill(rd(P("prompts", "write.md")), n))
    elif kind == "review":
        s += sec("共通執筆ルール", rules())
        s += sec("CANON（設定書・章カード以外）", canon_core())
        s += sec(f"この章の機能（BRIEF 第{n}章）", role(n))
        s += sec(f"この章のカード（CANON 第{n}章）", card(n))
        s += sec("LEDGER（これまでの記録）", ledger_for_packet(n))
        s += sec("前章の結び", prev_tail(n))
        s += sec("この章の伏線（必須）", events_text(n))
        s += sec("まだ明かしてはいけないこと", hide_text(n))
        s += sec(f"審査対象の原稿（第{n}章）", rd(P("work", f"ch{nn(n)}_draft.md")))
        s += sec("依頼", fill(rd(P("prompts", "review.md")), n))
    elif kind == "ledger":
        s += sec("LEDGERの書式", rd(P("templates", "LEDGER_TEMPLATE.md")))
        s += sec(f"第{n}章の本文", rd(P("chapters", f"ch{nn(n)}.md")))
        s += sec("依頼", fill(rd(P("prompts", "ledger.md")), n))
    elif kind == "final":
        s += sec("CANON", canon())
        s += sec("LEDGER（全章）", rd(LEDGER))
        s += sec("第8章の本文", rd(P("chapters", f"ch{nn(N_CH)}.md")))
        s += sec("依頼", fill(rd(P("prompts", "final_review.md"))))
    elif kind == "continue":
        raise ValueError("continue は内部専用")
    else:
        raise ValueError(f"不明なパケット種別: {kind}")
    name = f"{kind}" + (f"_ch{nn(n)}" if n else "") + ".packet.md"
    path = P("work", name)
    wr(path, s)
    if len(s) > CFG["packet_warn_chars"]:
        log(f"WARN packet {name} is {len(s)} chars (> {CFG['packet_warn_chars']})")
        print(f"警告: パケットが {len(s)} 字あります。モデルのコンテキスト長を確認してください。")
    return path


# ---------------------------------------------------------------- モデル呼び出し
def call(packet, out, role_="writer"):
    tpl = CFG[f"{role_}_cmd"]
    use_arg = any("{packet}" in a for a in tpl)
    cmd = [a.replace("{packet}", str(packet)) for a in tpl]
    if cmd and cmd[0] in ("python3", "python"):
        cmd[0] = sys.executable
    env = os.environ.copy()
    env.update(CFG.get("call_env", {}))
    inflight = P("state", "inflight.json")
    env["NOVEL_PROGRESS_FILE"] = str(inflight)
    wr(inflight, json.dumps({"task": Path(packet).name, "started": time.time(), "last_recv": time.time(),
                             "reasoning_chars": 0, "content_chars": 0, "attempt": 1}))
    log(f"CALL {role_} {Path(packet).name} -> {Path(out).name}")
    with open(packet, encoding="utf-8") as f:
        try:
            r = subprocess.run(cmd, stdin=None if use_arg else f, capture_output=True, text=True,
                               env=env, timeout=CFG["timeout_sec"], cwd=ROOT)
        except subprocess.TimeoutExpired:
            inflight.unlink(missing_ok=True)
            raise Escalate("CALL_TIMEOUT", f"モデル呼び出しが {CFG['timeout_sec']} 秒でタイムアウト",
                           "config.json の timeout_sec を延ばすか、モデルの負荷を確認して `resolve` 後に再実行")
        except FileNotFoundError as e:
            raise Escalate("CALL_NOT_FOUND", f"コマンドが見つからない: {e}", "config.json の writer_cmd / reviewer_cmd を確認")
    inflight.unlink(missing_ok=True)
    wr(out, r.stdout)
    if r.stderr.strip():
        wr(str(out) + ".stderr", r.stderr)
        for line in r.stderr.strip().splitlines()[-3:]:
            log(f"  {line[:300]}")
    if r.returncode != 0 or not r.stdout.strip():
        raise Escalate("CALL_FAILED", f"モデル呼び出し失敗 (rc={r.returncode})。stderr: {r.stderr[-500:]}",
                       "OPENROUTER_API_KEY・config.json の llm.profiles のモデル名・ネットワークを確認し `resolve` 後に再実行")
    return r.stdout


def continue_call(packet, partial, out):
    s = rd(packet) + sec("あなたの出力（途中で切れている）", partial) + sec(
        "依頼", "上の出力は途中で切れました。最後の文字の直後から続きだけを出力してください。"
                "すでに書いた部分を繰り返さないこと。見出しや前置きは不要です。")
    cp = Path(str(packet).replace(".packet.md", ".continue.packet.md"))
    wr(cp, s)
    return call(cp, out, "writer")


# ---------------------------------------------------------------- 抽出・検査
def extract_json(raw):
    raw = re.sub(r"```(?:json)?", "", raw)
    i = raw.find("{")
    while i >= 0:
        depth, instr, esc = 0, False, False
        for j in range(i, len(raw)):
            ch = raw[j]
            if instr:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    instr = False
                continue
            if ch == '"':
                instr = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(raw[i:j + 1])
                    except json.JSONDecodeError:
                        break
        i = raw.find("{", i + 1)
    return None


def extract_chapter(raw, n):
    kn = KANJI[n] if n <= 10 else str(n)
    m = re.search(rf"^#\s*第\s*(?:{n}|{kn}|{chr(0xFF10 + n) if n < 10 else n})\s*章.*$", raw, flags=re.M)
    if not m:
        return None
    text = raw[m.start():]
    text = re.sub(r"\n```.*$", "", text, flags=re.S).strip()
    first, _, rest = text.partition("\n")
    first = re.sub(r"^#\s*第\s*\S+?\s*章", f"# 第{n}章", first)
    return first + "\n" + rest.strip() + "\n"


def ends_properly(body):
    return bool(re.search(r"[。」』！？!?…―）)]\s*$", body))


META = re.compile(r"(?<![A-Za-z0-9])(?:F[1-6]|R1|C1|M1)(?![0-9])|伏線|スロット|章カード|CANON|LEDGER|承知しました|以下が本文|以上が第")


def mech_check(text, n):
    issues = []
    body = chapter_body(text)
    c = count_chars(body)
    lim = CFG["chapter_chars"]
    if c < lim["min"]:
        issues.append(("short", f"本文が{c}字で短い（{lim['min']}〜{lim['max']}字）。場面の描写を厚くして{lim['target']}字前後にする。"))
    if c > lim["max"]:
        issues.append(("long", f"本文が{c}字で長い（{lim['min']}〜{lim['max']}字）。説明的な部分を削って{lim['target']}字前後にする。"))
    if not ends_properly(body):
        issues.append(("truncated", "本文が文の途中で終わっている。最後の場面を文として完結させる。"))
    leak = sorted(set(META.findall(body)))
    if leak:
        issues.append(("meta", f"本文に管理用語や前置きが混入している：{'、'.join(leak)}。物語の文章だけにする。"))
    if "```" in body or re.search(r"^\s*#", body, flags=re.M):
        issues.append(("format", "見出し以外のMarkdown記法が本文に混ざっている。本文のみにする。"))
    return c, issues


def canon_check(text):
    issues = []
    if "（記入）" in text:
        issues.append(f"「（記入）」が {text.count('（記入）')} か所残っている。すべて具体的な内容で埋める。")
    for i in range(1, N_CH + 1):
        if f"<!-- CARD:{nn(i)} -->" not in text or f"<!-- /CARD:{nn(i)} -->" not in text:
            issues.append(f"第{i}章の章カードの目印（<!-- CARD:{nn(i)} --> と <!-- /CARD:{nn(i)} -->）がない。")
    for sid in CFG["slot_plan"]:
        if not re.search(rf"^[ \t]*(?:[-*・|][ \t]*)?(?:\*\*)?[ \t]*{sid}(?![0-9])", text, flags=re.M):
            issues.append(f"第7節に {sid} の行がない。")
    c = count_chars(text)
    if c > CFG["canon_max_chars"]:
        issues.append(f"CANONが{c}字で長すぎる（上限{CFG['canon_max_chars']}字）。各項目を短くする。")
    return issues


SLOT_RE = re.compile(r"^SLOT:\s*([A-Z]\d)\s*=\s*(planted|reinforced|recovered)", re.M)


def ledger_check(block, n):
    issues = []
    for pre in ["SUMMARY:", "FACT:", "STATE_BOY:", "STATE_GIRL:", "DISTANCE:", "CLOCK:", "LAST:"]:
        if not re.search(rf"^{pre}", block, flags=re.M):
            issues.append(f"{pre} の行がない")
    found = {sid: st for sid, st in SLOT_RE.findall(block)}
    for sid, ev in events_for(n):
        if sid not in found:
            issues.append(f"SLOT: {sid} の行がない（この章で{EV_JA[ev]}はず）")
        elif ev == "recovered" and found[sid] != "recovered":
            issues.append(f"{sid} はこの章で回収のはずだが {found[sid]} になっている")
    for sid, st in found.items():
        rc = recover_ch(sid) if sid in CFG["slot_plan"] else None
        if st == "recovered" and rc and rc > n:
            issues.append(f"PREMATURE: {sid} が第{n}章で回収扱い（予定は第{rc}章）")
    return issues


# ---------------------------------------------------------------- 進行（ステートマシン）
def fresh_progress():
    return {"phase": "design", "design_state": "draft", "design_attempts": 0, "canon_sha256": None,
            "current": 1, "final_state": "assemble", "final_reopened": False, "escalation": None,
            "chapters": {str(i): {"state": "pending", "attempts": 0, "parse_retries": 0,
                                  "ledger_attempts": 0, "warnings": []} for i in range(1, N_CH + 1)}}


def cmd_init():
    for d in ["canon", "state", "chapters", "work", "output", "logs"]:
        P(d).mkdir(exist_ok=True)
    if not LEDGER.exists():
        shutil.copy(P("templates", "LEDGER_TEMPLATE.md"), LEDGER)
    if not PROGRESS.exists():
        save(fresh_progress())
        log("INIT")
        print("初期化しました。")
    else:
        print("既に初期化済みです（progress.json は変更していません）。")
    if "call_llm.py" in " ".join(CFG["writer_cmd"]):
        names = CFG["llm"].get("api_key_env", ["OPENROUTER_API_KEY"])
        if not any(os.environ.get(n) for n in names) and not P(".env").exists():
            print("注意: APIキーが見つかりません（環境変数 " + " / ".join(names) + " も .env もなし）。"
                  "`python3 tools/call_llm.py --ping` が通ることを確認してから step してください。")


def verify_canon_lock(p):
    if p["canon_sha256"] and sha(CANON) != p["canon_sha256"]:
        raise Escalate("CANON_MODIFIED", "ロック後に canon/CANON.md が変更された",
                       "意図的な変更なら `resolve --relock`、そうでなければ git 等で元に戻して `resolve`")


def step_design(p):
    a = p["design_attempts"]
    if p["design_state"] == "draft":
        if a >= CFG["max_design_attempts"]:
            raise Escalate("DESIGN_EXHAUSTED", f"CANONの設計が{a}回失敗", "work/canon_feedback.md を読み、人がCANONを直して `resolve`")
        pk = build_packet("design")
        raw = call(pk, P("work", f"design_a{a + 1}.out"))
        m = re.search(r"^#\s*CANON.*", raw, flags=re.M | re.S)
        text = re.sub(r"\n```\s*$", "", m.group(0)).strip() + "\n" if m else ""
        issues = canon_check(text) if text else ["出力が `# CANON` で始まっていない。"]
        if text:
            wr(CANON, text)
        p["design_attempts"] = a + 1
        if issues:
            wr(P("work", "canon_feedback.md"), "\n".join(f"- {i}" for i in issues))
            log(f"DESIGN fail: {issues}")
            return f"CANON下書き不合格（機械検査）: {issues[0]} ほか{len(issues) - 1}件 → 再設計"
        p["design_state"] = "review"
        return "CANON下書きを canon/CANON.md に保存 → 次は審査"
    # review
    pk = build_packet("canon-review")
    raw = call(pk, P("work", f"canon_review_a{a}.out"), "reviewer")
    j = extract_json(raw)
    if j is None:
        p["design_parse_retries"] = p.get("design_parse_retries", 0) + 1
        if p["design_parse_retries"] > CFG["max_parse_retries"]:
            log("WARN canon review JSON unparsable; accepting on mechanical checks only")
            j = {"pass": True, "issues": []}
        else:
            return "審査結果のJSONを読めなかった → 審査をやり直す"
    high = [i for i in j.get("issues", []) if i.get("severity") == "high"]
    if j.get("pass") and not high:
        p["canon_sha256"] = sha(CANON)
        p["phase"] = "canon_hold" if CFG["pause_after_canon"] else "chapters"
        wr(P("work", "canon_review_notes.md"), json.dumps(j, ensure_ascii=False, indent=2))
        log("CANON locked")
        return "CANONが審査を通過し、ロックしました" + ("（人の確認待ち：`approve-canon`）" if CFG["pause_after_canon"] else " → 第1章へ")
    fb = "\n".join(f"- [審査項目{i.get('item')}] {i.get('detail')} → {i.get('fix')}" for i in high)
    wr(P("work", "canon_feedback.md"), fb)
    p["design_state"] = "draft"
    log(f"CANON review fail: {len(high)} high")
    return f"CANON審査不合格（high {len(high)}件）→ 修正設計"


def step_chapter(p):
    verify_canon_lock(p)
    n = p["current"]
    ch = p["chapters"][str(n)]
    draft, fb = P("work", f"ch{nn(n)}_draft.md"), P("work", f"ch{nn(n)}_feedback.md")

    if ch["state"] in ("pending", "rewrite"):
        if ch["state"] == "pending":
            for f in (draft, fb):
                if f.exists():
                    f.unlink()
        k = ch["attempts"] + 1
        pk = build_packet("write", n)
        raw = call(pk, P("work", f"ch{nn(n)}_write_a{k}.out"))
        text = extract_chapter(raw, n)
        if text and not ends_properly(chapter_body(text)) and count_chars(chapter_body(text)) < CFG["chapter_chars"]["max"]:
            more = continue_call(pk, text, P("work", f"ch{nn(n)}_write_a{k}_cont.out"))
            text = text.rstrip() + more.strip() + "\n"
            log(f"CH{nn(n)} continued after truncation")
        if not text:
            text = f"# 第{n}章\n" + raw.strip() + "\n"
            ch.setdefault("warnings", []).append(f"a{k}: 見出しが見つからず補完")
        wr(draft, text)
        wr(P("work", f"ch{nn(n)}_draft_a{k}.md"), text)
        ch["attempts"] = k
        ch["state"] = "drafted"
        ch["parse_retries"] = 0
        return f"第{n}章 下書き{k}稿を作成（{count_chars(chapter_body(text))}字）→ 次は審査"

    if ch["state"] == "drafted":
        text = rd(draft)
        c, mech = mech_check(text, n)
        hard_mech = [m for m in mech if m[0] in ("truncated", "meta", "format")]
        review = {"pass": True, "violations": [], "slots_found": {}}
        if not hard_mech:
            pk = build_packet("review", n)
            raw = call(pk, P("work", f"ch{nn(n)}_review_a{ch['attempts']}.out"), "reviewer")
            j = extract_json(raw)
            if j is None:
                ch["parse_retries"] += 1
                if ch["parse_retries"] <= CFG["max_parse_retries"]:
                    return f"第{n}章 審査JSONを読めなかった → 審査をやり直す"
                ch["warnings"].append(f"a{ch['attempts']}: 審査JSON不読のため機械検査のみで判定")
            else:
                review = j
        problems = [f"[{t}] {d}" for t, d in mech]
        high = [v for v in review.get("violations", []) if v.get("severity") == "high"]
        problems += [f"[{v.get('type')}] {v.get('detail')} → {v.get('fix')}" for v in high]
        found = review.get("slots_found", {}) or {}
        if found:
            for sid, ev in events_for(n):
                st = found.get(sid, "none")
                if st in (None, "none") or (ev == "recovered" and st != "recovered"):
                    problems.append(f"[slots] {sid}（{CFG['slot_names'][sid]}）を{EV_JA[ev]}必要があるが、原稿では確認できない。CANON第7節の{sid}の行どおりに描写する。")
            for sid, st in found.items():
                rc = recover_ch(sid) if sid in CFG["slot_plan"] else None
                if st == "recovered" and rc and rc > n:
                    problems.append(f"[premature] {sid} の中身を第{rc}章より前に明かしている。ほのめかしに留める。")
        lows = [v for v in review.get("violations", []) if v.get("severity") == "low"]
        wr(P("work", f"ch{nn(n)}_verdict_a{ch['attempts']}.json"),
           json.dumps({"chars": c, "mech": mech, "review": review, "problems": problems}, ensure_ascii=False, indent=2))
        if not problems:
            return accept(p, n, f"第{n}章 合格（{c}字）" + (f"／軽微な指摘{len(lows)}件は記録のみ" if lows else ""))
        wr(fb, "\n".join(f"- {x}" for x in problems))
        if ch["attempts"] >= CFG["max_attempts"]:
            soft_only = all(x.startswith(("[short]", "[long]")) for x in problems)
            if soft_only:
                ch["warnings"].append(f"字数が範囲外のまま採用（{c}字）")
                return accept(p, n, f"第{n}章 字数のみ範囲外（{c}字）で採用")
            raise Escalate("CHAPTER_EXHAUSTED", f"第{n}章が{ch['attempts']}稿で合格しない: " + " / ".join(problems[:3]),
                           f"work/ch{nn(n)}_feedback.md と work/ch{nn(n)}_verdict_a*.json を確認。"
                           f"人が work/ch{nn(n)}_draft.md を直して `resolve --accept`、"
                           f"または work/ch{nn(n)}_feedback.md を書き換えて `resolve --retry`")
        ch["state"] = "rewrite"
        log(f"CH{nn(n)} fail a{ch['attempts']}: {len(problems)} problems")
        return f"第{n}章 不合格（{len(problems)}件）→ 書き直し（{ch['attempts'] + 1}稿目）"

    if ch["state"] == "accepted":
        if ch["ledger_attempts"] >= CFG["max_ledger_attempts"]:
            raise Escalate("LEDGER_FAILED", f"第{n}章のLEDGER記録が書式を満たさない",
                           f"work/ch{nn(n)}_ledger_a*.out を見て state/LEDGER.md に ## CH{nn(n)} 節を手で追記し `resolve --ledger-done`")
        ch["ledger_attempts"] += 1
        pk = build_packet("ledger", n)
        raw = call(pk, P("work", f"ch{nn(n)}_ledger_a{ch['ledger_attempts']}.out"), "reviewer")
        m = re.search(rf"^## CH{nn(n)}\s*$.*", raw, flags=re.M | re.S)
        block = re.sub(r"\n```.*$", "", m.group(0), flags=re.S).strip() if m else ""
        issues = ledger_check(block, n) if block else [f"`## CH{nn(n)}` で始まっていない"]
        if issues:
            if any(i.startswith("PREMATURE") for i in issues):
                ch["warnings"].append("LEDGERが早すぎる回収を報告: " + "; ".join(issues))
            log(f"CH{nn(n)} ledger fail: {issues}")
            return f"第{n}章 LEDGER記録が不備（{issues[0]}）→ 記録をやり直す"
        write_ledger_section(n, block)
        return finish_chapter(p, n)
    raise RuntimeError(f"不明な章状態: {ch['state']}")


def accept(p, n, msg):
    shutil.copy(P("work", f"ch{nn(n)}_draft.md"), P("chapters", f"ch{nn(n)}.md"))
    p["chapters"][str(n)]["state"] = "accepted"
    log(f"CH{nn(n)} accepted")
    return msg + f" → chapters/ch{nn(n)}.md に確定 → 次はLEDGER記録"


def write_ledger_section(n, block):
    head, secs = ledger_sections(rd(LEDGER))
    secs[n] = re.sub(rf"^## CH{nn(n)}\s*\n", "", block).strip()
    out = head.rstrip() + "\n\n" + "\n\n".join(f"## CH{nn(c)}\n{secs[c]}" for c in sorted(secs)) + "\n"
    wr(LEDGER, out)


def finish_chapter(p, n):
    p["chapters"][str(n)]["state"] = "done"
    log(f"CH{nn(n)} done")
    if n >= N_CH:
        p["phase"], p["final_state"] = "final", "assemble"
        return f"第{n}章 LEDGER記録完了 → 全章そろった。最終工程へ"
    p["current"] = n + 1
    return f"第{n}章 LEDGER記録完了 → 第{n + 1}章へ"


def cmd_assemble(quiet=False):
    title = "無題"
    if CANON.exists():
        m = re.search(r"タイトル：\s*(.+)", canon())
        if m:
            title = m.group(1).strip()
    parts, total = [], 0
    for i in range(1, N_CH + 1):
        f = P("chapters", f"ch{nn(i)}.md")
        if f.exists():
            t = rd(f).strip()
            total += count_chars(chapter_body(t))
            parts.append(t.replace("# 第", "## 第", 1))
    wr(P("output", "final.md"), f"# {title}\n\n" + "\n\n".join(parts) + "\n")
    if not quiet:
        print(f"output/final.md を作成（本文合計 {total} 字）")
    return total


def step_final(p):
    verify_canon_lock(p)
    if p["final_state"] == "assemble":
        total = cmd_assemble(quiet=True)
        _, secs = ledger_sections(rd(LEDGER))
        missing = []
        for sid, evs in CFG["slot_plan"].items():
            for c, ev in evs:
                if not re.search(rf"^SLOT:\s*{sid}\s*=", secs.get(c, ""), flags=re.M):
                    missing.append(f"{sid}@第{c}章")
        rng = CFG["total_chars"]
        notes = []
        if not rng["min"] <= total <= rng["max"]:
            notes.append(f"合計字数 {total} が目安 {rng['min']}〜{rng['max']} の範囲外")
        if missing:
            notes.append("LEDGERで確認できない伏線操作: " + "、".join(missing))
        p["final_notes"] = notes
        p["final_state"] = "review"
        return f"output/final.md を結合（{total}字）" + (f"／注意: {' / '.join(notes)}" if notes else "") + " → 最終審査へ"
    pk = build_packet("final")
    raw = call(pk, P("work", "final_review.out"), "reviewer")
    j = extract_json(raw) or {"pass": True, "issues": [], "summary": "（最終審査JSONを読めなかった）"}
    high = [i for i in j.get("issues", []) if i.get("severity") == "high"]
    report = ["# 最終レポート", "", f"- 合計字数: {cmd_assemble(quiet=True)}",
              f"- 最終審査: {'合格' if j.get('pass') and not high else '要確認'}",
              f"- 講評: {j.get('summary', '')}", "", "## 指摘"]
    report += [f"- [{i.get('severity')}] 第{i.get('chapter')}章: {i.get('detail')} → {i.get('fix')}" for i in j.get("issues", [])] or ["- なし"]
    report += ["", "## 自動検査の注意"] + ([f"- {x}" for x in p.get("final_notes", [])] or ["- なし"])
    report += ["", "## 各章の警告"] + [f"- 第{k}章: {w}" for k, v in p["chapters"].items() for w in v.get("warnings", [])]
    wr(P("output", "final_report.md"), "\n".join(report) + "\n")
    if j.get("pass") and not high:
        p["phase"] = "done"
        return "最終審査合格。output/final.md と output/final_report.md が完成品です"
    only_last = all(i.get("chapter") == N_CH for i in high)
    if only_last and not p["final_reopened"]:
        p["final_reopened"] = True
        wr(P("work", f"ch{nn(N_CH)}_draft.md"), rd(P("chapters", f"ch{nn(N_CH)}.md")))
        wr(P("work", f"ch{nn(N_CH)}_feedback.md"), "\n".join(f"- [最終審査] {i.get('detail')} → {i.get('fix')}" for i in high))
        c = p["chapters"][str(N_CH)]
        c.update({"state": "rewrite", "attempts": 0, "ledger_attempts": 0})
        p["phase"], p["current"] = "chapters", N_CH
        return f"最終審査で第{N_CH}章に重大な指摘 → 第{N_CH}章を一度だけ書き直す"
    raise Escalate("FINAL_REVIEW_FAILED", "最終審査で重大な指摘あり（output/final_report.md 参照）",
                   "該当章を `reset-chapter N` で巻き戻すか、原稿のまま完成とするなら `resolve --done`")


def lock_owner():
    """ロックを持つ生きたプロセスの pid。ロックがない・持ち主が死んでいれば None。"""
    f = P("state", ".lock")
    if not f.exists():
        return None
    try:
        pid = int(f.read_text().strip() or 0)
        os.kill(pid, 0)
        return pid
    except PermissionError:
        return pid
    except (ValueError, ProcessLookupError, OSError):
        return None


class Lock:
    """同時実行の防止（エージェントが step と run を重ねて起動しても壊れないように）。"""
    path = P("state", ".lock")

    def __enter__(self):
        pid = lock_owner()
        if pid:
            sys.exit(f"BUSY: 別の novelctl（pid {pid}）が実行中です。終わるまで待ってから status を確認してください。")
        wr(self.path, str(os.getpid()))
        return self

    def __exit__(self, *a):
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def cmd_step():
    p = load()
    if p.get("escalation"):
        e = p["escalation"]
        print(f"ESCALATE [{e['code']}] {e['detail']}\n対応: {e['hint']}")
        return 2
    try:
        if p["phase"] == "design":
            msg = step_design(p)
        elif p["phase"] == "canon_hold":
            print("CANONの人間確認待ちです。canon/CANON.md を確認し `approve-canon` を実行してください。")
            return 2
        elif p["phase"] == "chapters":
            msg = step_chapter(p)
        elif p["phase"] == "final":
            msg = step_final(p)
        else:
            print("DONE: 完成しています。output/final.md / output/final_report.md")
            return 3
    except Escalate as e:
        p["escalation"] = {"code": e.code, "detail": e.detail, "hint": e.hint, "phase": p["phase"], "chapter": p["current"]}
        save(p)
        log(f"ESCALATE {e.code}: {e.detail}")
        print(f"ESCALATE [{e.code}] {e.detail}\n対応: {e.hint}")
        return 2
    save(p)
    log(f"STEP {msg}")
    print(f"OK: {msg}")
    return 3 if p["phase"] == "done" else 0


def cmd_start(args):
    """run をシェルから切り離して起動し、すぐ戻る。エージェントのツールの制限時間で殺されない。"""
    pid = lock_owner()
    if pid:
        print(f"BUSY: すでに実行中です（pid {pid}）。status で様子を見てください。")
        return 0
    P("logs").mkdir(exist_ok=True)
    out = open(P("logs", "run.out"), "a", encoding="utf-8")
    out.write(f"\n===== start {datetime.datetime.now().isoformat(timespec='seconds')} =====\n")
    out.flush()
    proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "run"] + list(args),
                            cwd=ROOT, stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                            start_new_session=True, close_fds=True)
    log(f"START detached pid={proc.pid}")
    print(f"STARTED: バックグラウンドで実行を開始しました（pid {proc.pid}）。進み具合は status、ログは logs/run.out。")
    return 0


def cmd_run(max_steps):
    for _ in range(max_steps):
        rc = cmd_step()
        if rc != 0:
            return rc
    print(f"{max_steps} ステップ実行しました。続けるには再度 run / step。")
    return 0


def cmd_status():
    p = load()
    print(f"phase: {p['phase']}   current: 第{p['current']}章   canon_locked: {bool(p['canon_sha256'])}")
    if p["phase"] == "design":
        print(f"design: {p['design_state']}（試行 {p['design_attempts']}/{CFG['max_design_attempts']}）")
    for k, v in p["chapters"].items():
        f = P("chapters", f"ch{nn(k)}.md")
        c = count_chars(chapter_body(rd(f))) if f.exists() else 0
        w = f"  警告{len(v['warnings'])}" if v.get("warnings") else ""
        print(f"  第{k}章 {v['state']:<9} 稿{v['attempts']} {c:>5}字{w}")
    if LEDGER.exists():
        _, secs = ledger_sections(rd(LEDGER))
        st = {}
        for c in sorted(secs):
            for sid, s in SLOT_RE.findall(secs[c]):
                st[sid] = f"{s}@{c}"
        print("  伏線: " + "  ".join(f"{sid}:{st.get(sid, '-')}" for sid in CFG["slot_plan"]))
    inflight = P("state", "inflight.json")
    if inflight.exists():
        try:
            f = json.loads(rd(inflight))
            now = time.time()
            print(f"CALLING: {f.get('task')}  試行{f.get('attempt')}  経過{now - f['started']:.0f}秒  "
                  f"推論{f.get('reasoning_chars', 0)}字  本文{f.get('content_chars', 0)}字  "
                  f"最終受信{now - f['last_recv']:.0f}秒前")
        except (ValueError, KeyError):
            pass
    if p.get("escalation"):
        e = p["escalation"]
        print(f"ESCALATE [{e['code']}] {e['detail']}\n対応: {e['hint']}")
        print("NEXT: 上の対応を行ってから python3 tools/novelctl.py resolve")
    elif p["phase"] == "canon_hold":
        print("NEXT: canon/CANON.md を人が確認 → python3 tools/novelctl.py approve-canon")
    elif p["phase"] == "done":
        print("DONE: output/final.md")
    elif lock_owner() and inflight.exists():
        print("RUNNING: モデル呼び出し中。『最終受信』が数十秒以内なら正常。待ってから再度 status")
    elif lock_owner():
        print("RUNNING: novelctl が実行中（モデル呼び出しの合間）。待ってから再度 status")
    elif inflight.exists():
        print("STALE: 前回の呼び出しは途中で止められました（実行中のプロセスなし）。")
        print("NEXT: python3 tools/novelctl.py start   （切り離して再開。シェルの制限時間に殺されない）")
    else:
        print("NEXT: python3 tools/novelctl.py start   （または 1 ステップだけなら step）")


def cmd_resolve(args):
    p = load()
    e = p.get("escalation")
    p["escalation"] = None
    n = p["current"]
    ch = p["chapters"][str(n)]
    if "--retry" in args:
        if p["phase"] == "design":
            p["design_attempts"] = 0
            p["design_state"] = "draft"
        else:
            ch["attempts"], ch["ledger_attempts"] = 0, 0
            if ch["state"] == "drafted":
                ch["state"] = "rewrite"
    if "--accept" in args:
        accept(p, n, "人の判断で採用")
    if "--ledger-done" in args:
        finish_chapter(p, n)
    if "--relock" in args:
        p["canon_sha256"] = sha(CANON)
    if "--done" in args:
        p["phase"] = "done"
    save(p)
    log(f"RESOLVE {e and e['code']} {args}")
    print("エスカレーションを解除しました。`status` で確認してください。")


def cmd_reset(n):
    p = load()
    arch = P("work", "archive", datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
    arch.mkdir(parents=True, exist_ok=True)
    for i in range(n, N_CH + 1):
        f = P("chapters", f"ch{nn(i)}.md")
        if f.exists():
            shutil.move(str(f), arch / f.name)
        for g in P("work").glob(f"ch{nn(i)}_*"):
            shutil.move(str(g), arch / g.name)
        p["chapters"][str(i)] = {"state": "pending", "attempts": 0, "parse_retries": 0, "ledger_attempts": 0, "warnings": []}
    head, secs = ledger_sections(rd(LEDGER))
    secs = {c: b for c, b in secs.items() if c < n}
    wr(LEDGER, head.rstrip() + "\n\n" + "\n\n".join(f"## CH{nn(c)}\n{secs[c]}" for c in sorted(secs)) + "\n")
    p.update({"phase": "chapters", "current": n, "escalation": None, "final_state": "assemble"})
    save(p)
    log(f"RESET from CH{nn(n)}")
    print(f"第{n}章以降を未執筆に戻しました（旧ファイルは {arch.relative_to(ROOT)}）。")


def main(argv):
    if not argv:
        print(__doc__)
        return 0
    c, a = argv[0], argv[1:]
    if c == "init":
        cmd_init()
    elif c == "status":
        cmd_status()
    elif c == "step":
        with Lock():
            return cmd_step()
    elif c == "start":
        return cmd_start(a)
    elif c == "run":
        with Lock():
            return cmd_run(int(a[a.index("--max") + 1]) if "--max" in a else 200)
    elif c == "packet":
        n = int(a[1]) if len(a) > 1 else None
        print(build_packet(a[0], n))
    elif c == "check":
        n = int(a[0])
        f = P("work", f"ch{nn(n)}_draft.md")
        f = f if f.exists() else P("chapters", f"ch{nn(n)}.md")
        cnt, iss = mech_check(rd(f), n)
        print(f"{f.relative_to(ROOT)}: {cnt}字")
        for t, d in iss:
            print(f"  [{t}] {d}")
        return 0 if not iss else 1
    elif c == "approve-canon":
        p = load()
        p["canon_sha256"] = sha(CANON)
        p["phase"] = "chapters"
        save(p)
        print("CANONを承認・再ロックしました。`step` で第1章から進みます。")
    elif c == "resolve":
        cmd_resolve(a)
    elif c == "reset-chapter":
        cmd_reset(int(a[0]))
    elif c == "assemble":
        cmd_assemble()
    elif c == "epub":
        sys.path.insert(0, str(ROOT / "tools"))
        import make_epub
        make_epub.build("proofread" if "--proofread" in a else "final")
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
