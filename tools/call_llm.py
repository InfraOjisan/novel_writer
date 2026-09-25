#!/usr/bin/env python3
"""call_llm — パケット1つを OpenRouter（OpenAI互換 Chat Completions）にストリーミングで送り、本文だけを標準出力に返す。

エージェントハーネス（Hermes / OpenCode / Goose など）に依存しない。Python標準ライブラリのみ。

  python3 tools/call_llm.py <packet> writer|reviewer   novelctl.py から呼ばれる通常の使い方
  python3 tools/call_llm.py --ping [writer|reviewer]   疎通・キー・モデル名・応答速度の確認（数十トークンだけ使う）

ストリーミングにしている理由:
  - 推論が長いモデルでも「生きているか」が分かる（NOVEL_PROGRESS_FILE に受信状況を書き出す → novelctl status に表示）
  - 無通信タイムアウト（llm.idle_timeout_sec）で、本当に詰まった接続だけを検出して再試行できる

APIキーの探し方（上から順に、最初に見つかったもの）:
  1. config.json の llm.api_key_env に並べた環境変数（既定: NOVEL_LLM_API_KEY, OPENROUTER_API_KEY, HERMES_CUSTOM_OPENROUTER_API_KEY）
  2. プロジェクト直下の .env ファイル（例: OPENROUTER_API_KEY=sk-or-...）
任意:
  NOVEL_LLM_BASE_URL   既定 https://openrouter.ai/api/v1
  NOVEL_PROGRESS_FILE  受信状況を書き出すファイル（novelctl が自動で設定）
"""
import http.client
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
LLM = CFG["llm"]
JSON_TASKS = {"canon-review", "review", "final"}
RETRY_HTTP = (408, 409, 425, 429, 500, 502, 503, 504)


def err(msg):
    print(msg, file=sys.stderr, flush=True)


def die(msg, code=1):
    err(msg)
    sys.exit(code)


class ApiError(Exception):
    pass


class Degenerate(Exception):
    pass


def degenerate(tail):
    """末尾160字が40字以下の単位の繰り返しになっていれば暴走とみなす。"""
    if len(set(tail)) <= 2:
        return True
    for unit in range(1, 41):
        pat = tail[-unit:]
        if pat * (len(tail) // unit) == tail[len(tail) % unit:]:
            return True
    return False


class Progress:
    def __init__(self, path, task):
        self.path, self.task, self.t0, self.last = path, task, time.time(), 0.0
        self.reasoning = self.content = 0
        self.last_recv = time.time()
        self.attempt = 1

    def tick(self, force=False):
        self.last_recv = time.time()
        if not self.path or (not force and time.time() - self.last < 2):
            return
        self.last = time.time()
        try:
            Path(self.path).write_text(json.dumps({
                "task": self.task, "attempt": self.attempt, "pid": os.getpid(),
                "started": self.t0, "last_recv": self.last_recv,
                "reasoning_chars": self.reasoning, "content_chars": self.content,
            }), encoding="utf-8")
        except OSError:
            pass


def request(url, key, body, idle_timeout):
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST", headers={
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "Authorization": f"Bearer {key}",
        "User-Agent": "novelctl/1.0",
        "HTTP-Referer": LLM.get("app_url", "https://localhost/novelctl"),
        "X-Title": LLM.get("app_title", "novelctl"),
    })
    # timeout はソケットの1回の読み取りごとに効く＝無通信タイムアウトとして働く
    return urllib.request.urlopen(req, timeout=idle_timeout)


def stream_once(url, key, body, prog, idle_timeout):
    parts, finish, usage, model = [], None, None, None
    with request(url, key, body, idle_timeout) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            if line.startswith(":"):  # OpenRouter のキープアライブ（処理中）
                prog.tick()
                continue
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            if obj.get("error"):
                raise ApiError(json.dumps(obj["error"], ensure_ascii=False)[:800])
            model = obj.get("model") or model
            if obj.get("usage"):
                usage = obj["usage"]
            for ch in obj.get("choices") or []:
                d = ch.get("delta") or {}
                if d.get("content"):
                    parts.append(d["content"])
                    prog.content += len(d["content"])
                    if prog.content > 200 and prog.content % 50 < len(d["content"]) + 1:
                        tail = "".join(parts)[-160:]
                        if degenerate(tail):
                            raise Degenerate(f"同じ文字列の繰り返しが止まらなくなった（末尾: {tail[-20:]!r}）")
                if d.get("reasoning"):
                    prog.reasoning += len(d["reasoning"])
                if ch.get("finish_reason"):
                    finish = ch["finish_reason"]
                    if finish == "error":
                        raise ApiError("ストリーム中にプロバイダ側でエラー（finish_reason=error）")
            prog.tick()
    return "".join(parts), finish, usage, model


def find_key():
    """エージェントのシェルに環境変数が引き継がれなくても動くよう、複数の場所からキーを探す。"""
    names = LLM.get("api_key_env", ["NOVEL_LLM_API_KEY", "OPENROUTER_API_KEY"])
    for n in names:
        if os.environ.get(n):
            return os.environ[n].strip()
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            k, _, v = line.strip().partition("=")
            if k.strip().removeprefix("export ").strip() in names and v.strip():
                return v.strip().strip('"').strip("'")
    return None


def build_body(role, task, text):
    prof = dict(LLM["profiles"][role])
    prof.update(LLM.get("task_overrides", {}).get(task, {}))
    body = {
        "model": prof["model"],
        "messages": [{"role": "system", "content": prof["system"]}, {"role": "user", "content": text}],
        "max_tokens": prof.get("max_tokens", 24000),
        "temperature": prof.get("temperature", 0.8),
        "stream": True,
        "usage": {"include": True},
    }
    if "top_p" in prof:
        body["top_p"] = prof["top_p"]
    if prof.get("reasoning_effort"):
        body["reasoning"] = {"effort": prof["reasoning_effort"]}
    if task in JSON_TASKS and LLM.get("json_mode", True):
        body["response_format"] = {"type": "json_object"}
    return body


def run(role, task, text, progress_path=None):
    key = find_key()
    if not key:
        die("APIキーが見つかりません。環境変数 " + " / ".join(LLM.get("api_key_env", [])) +
            " のいずれか、またはプロジェクト直下の .env（OPENROUTER_API_KEY=...）を設定してください。")
    base = os.environ.get("NOVEL_LLM_BASE_URL", LLM.get("base_url", "https://openrouter.ai/api/v1")).rstrip("/")
    url = f"{base}/chat/completions"
    body = build_body(role, task, text)
    idle = LLM.get("idle_timeout_sec", 180)
    prog = Progress(progress_path, task)
    prog.tick(force=True)
    last = None
    for attempt in range(1, LLM.get("retries", 4) + 1):
        prog.attempt, prog.reasoning, prog.content = attempt, 0, 0
        t0 = time.time()
        try:
            content, finish, usage, model = stream_once(url, key, body, prog, idle)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:800]
            last = f"HTTP {e.code}: {detail}"
            err(f"attempt {attempt}: {last}")
            if e.code == 400 and "response_format" in body:
                body.pop("response_format")
                continue
            if e.code in RETRY_HTTP:
                time.sleep(min(60, 5 * 2 ** attempt))
                continue
            die(last)
        except (socket.timeout, TimeoutError) as e:
            last = f"{idle}秒間なにも受信しなかった（無通信タイムアウト）: {e}"
            err(f"attempt {attempt}: {last}")
            continue
        except Degenerate as e:
            last = str(e)
            err(f"attempt {attempt}: {last}")
            t = body.get("temperature", 0.8)
            body["temperature"] = min(t, max(0.3, round(t - 0.25, 2)))
            err(f"temperature を {body['temperature']} に下げて再試行します")
            continue
        except (urllib.error.URLError, ConnectionError, http.client.HTTPException, ApiError) as e:
            last = f"{type(e).__name__}: {e}"
            err(f"attempt {attempt}: {last}")
            time.sleep(min(60, 5 * 2 ** attempt))
            continue
        if not content.strip():
            last = f"空の応答（finish_reason={finish}）"
            err(f"attempt {attempt}: {last}")
            if finish == "length" and body.get("reasoning", {}).get("effort") not in (None, "low"):
                body["reasoning"] = {"effort": "low"}  # 推論で max_tokens を使い切った → 推論を軽くして再試行
                err("推論が max_tokens を使い切ったため、reasoning effort を low に下げて再試行します")
            time.sleep(3)
            continue
        if finish == "length":
            err("WARN: max_tokens に達して出力が途中で切れた可能性があります")
        u = usage or {}
        err(f"usage: in={u.get('prompt_tokens')} out={u.get('completion_tokens')} "
            f"reasoning_chars={prog.reasoning} content_chars={len(content)} "
            f"cost={u.get('cost')} sec={time.time() - t0:.0f} model={model}")
        return content
    die(f"再試行しても失敗しました。最後のエラー: {last}")


def main():
    a = sys.argv[1:]
    if a and a[0] == "--ping":
        role = a[1] if len(a) > 1 else "writer"
        LLM["profiles"][role] = dict(LLM["profiles"][role], max_tokens=2000, reasoning_effort="low")
        t0 = time.time()
        out = run(role, "ping", "疎通確認です。「OK」とだけ返答してください。")
        print(f"PING OK ({time.time() - t0:.1f}s): {out.strip()[:40]}")
        return
    if len(a) < 2:
        die("usage: call_llm.py <packet> writer|reviewer  |  call_llm.py --ping [writer|reviewer]")
    packet, role = a[0], a[1]
    text = Path(packet).read_text(encoding="utf-8")
    m = re.match(r"<!-- TASK: (\S+)", text)
    task = m.group(1) if m else "write"
    sys.stdout.write(run(role, task, text, os.environ.get("NOVEL_PROGRESS_FILE")))


if __name__ == "__main__":
    main()
