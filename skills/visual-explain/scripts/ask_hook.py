#!/usr/bin/env python3
"""setup.sh が Claude Code の settings.json と Codex の hooks.json に登録するフック。

  python3 -I ask_hook.py [--agent claude|codex] [--session-start]   （--agent の既定は claude）

  質問のツールの直前（PreToolUse。Claude Code は AskUserQuestion、Codex は request_user_input）: 質問を常駐の
    配信サーバーに登録して資料のページに出し、ページで選ばれた答えを元のセッションに返す。ターミナルの選択画面は出ない。
    Claude Code には AskUserQuestion の答え（updatedInput）として、Codex にはツールの結果を差し替える手段が無いので、
    拒否（deny）の理由の文として答えを届ける。
    既定（auto）では、受け箱を一度でも開いたことがあれば常にページで待ち（always）、無ければ受け箱か質問の資料を
    ブラウザで開いているときだけページに出す（open。開いていなければすぐ終える）。off なら何もしない。
    設定は serve.py ask-config で変える。
  セッションの開始（--session-start）: 使う設定なら、常駐の配信サーバーが動いていなければ起動する（待たない）。

何も返さずに終えると、いつもどおりターミナルに選択画面が出る（使わない設定・ページを見ている人がいない・登録できない・
「ターミナルで答える」・待つ時間切れ）。待つ間は 1 秒おきに配信サーバーに問い合わせ、配信サーバーはその間を
「Claude Code が待っている」とみなす（フックが止められたら、ページから答えを送れなくなる）。
"""
import json
import os
import signal
import subprocess
import sys
import time
from urllib.parse import quote

HERE = os.path.dirname(os.path.abspath(__file__))
ASK_TOOLS = {"claude": "AskUserQuestion", "codex": "request_user_input"}
ACCOUNT_ENV = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME"}


def state_home():
    """設定と記録の置き場所（serve.ask_home と同じ。使わない設定ならすぐ終えるよう、serve を読み込まずに求める）。"""
    for name in ("VISUAL_EXPLAIN_HOME", "VISUAL_EXPLAIN_ASK_HOME"):
        if os.environ.get(name):
            return os.environ[name]
    state = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(state, "visual-explain")


def config_path():
    return os.path.join(state_home(), "ask.json")


def orca_mark(unread):
    """Orca の端末で動いているときだけ、このワークツリーに Orca のサイドバーの未読の印を付ける（外す）。
    Orca が無い環境では何もしない。待たずに戻る。"""
    worktree = os.environ.get("ORCA_WORKTREE_ID")
    cli = os.path.join(os.environ.get("ORCA_CLI_BIN_DIR") or "", "orca")
    if not worktree or not os.environ.get("ORCA_CLI_BIN_DIR") or not os.path.isfile(cli):
        return
    try:
        subprocess.Popen([cli, "worktree", "set", "--worktree", "id:" + worktree, "--unread" if unread else "--read", "--json"],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        pass


def configured_mode():
    """設定ファイルだけを見た使い方（配信サーバーのコードを読み込む前に、使わない設定ならすぐ終えるため）。"""
    try:
        with open(config_path(), encoding="utf-8") as f:
            raw = json.load(f)
    except OSError:
        return "auto"
    except ValueError:
        return "off"
    if not isinstance(raw, dict):
        return "off"
    if raw.get("mode") in ("auto", "open", "always", "off"):
        return raw["mode"]
    if "enabled" in raw:
        return "always" if raw["enabled"] else "off"
    return "auto"


def hook_agent(argv):
    return "codex" if "--agent" in argv[:-1] and argv[argv.index("--agent") + 1] == "codex" else "claude"


def codex_questions(inp):
    """request_user_input の質問を AskUserQuestion の形にする（複数選択とプレビューは無い）。id は答えの対応付けに残す。"""
    out = []
    for q in inp.get("questions") or []:
        if not isinstance(q, dict):
            return None
        out.append({"question": q.get("question"), "header": q.get("header") or "", "multiSelect": False,
                    "options": [{"label": o.get("label"), "description": o.get("description") or ""}
                                for o in q.get("options") or [] if isinstance(o, dict)], "_id": q.get("id")})
    return out


def session_start(agent):
    sys.path.insert(0, HERE)
    import serve
    home = serve.ask_home()
    conf = serve.load_ask_config(home, create=True)
    try:
        # 別のセッションが起動しているところなら待たない。自分で起動するときも、待ち受けを始めるまでの数秒だけ
        running = serve.ensure_ask_server(home, conf, wait=5, blocking=False)
    except OSError:
        running = False
    if agent == "codex":
        return 0  # Codex では SessionStart の hook の標準出力がモデルの文脈に入るので、何も出さない
    if serve.ask_effective_mode(conf) == "always":
        # 質問をページで待つ人にだけ、受け箱の URL をターミナルに出す（待っている間はフックから何も出せないため。
        # systemMessage は開発者にだけ見え、会話の文脈には入らない）
        inbox = "%s/__qa/inbox?k=%s" % (conf["public_url"], conf["token"])
        text = ("visual-explain: AskUserQuestion の質問は資料のページで答えを待ちます。受け箱: %s" % inbox if running else
                "visual-explain: 受け箱の配信サーバーを起動できませんでした（ask-config で確かめる）。質問はターミナルで答えます")
        print(json.dumps({"systemMessage": text}))
    return 0


def main():
    mode = configured_mode()
    # 使わない設定・配信サーバーが動かす claude -p（資料作り・質問欄の回答）の中では何もしない
    if mode == "off" or os.environ.get("VISUAL_EXPLAIN_ASK_CHILD"):
        return 0
    agent = hook_agent(sys.argv[1:])
    if "--session-start" in sys.argv[1:]:
        return session_start(agent)
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return 0
    inp = data.get("tool_input") if isinstance(data, dict) else None
    if data.get("tool_name") != ASK_TOOLS[agent] or not isinstance(inp, dict) or inp.get("answers"):
        return 0
    questions = codex_questions(inp) if agent == "codex" else inp.get("questions")
    if questions is None:
        return 0
    sys.path.insert(0, HERE)
    import serve

    home = serve.ask_home()
    conf = serve.load_ask_config(home)
    if not conf.get("token"):
        return 0  # まだ一度も配信サーバーを起動していない（受け箱を開いている人もいない）
    mode = serve.ask_effective_mode(conf)  # auto は、受け箱を開いたことがあれば always、無ければ open

    def log(msg):
        try:
            with open(os.path.join(home, "ask-hook.log"), "a", encoding="utf-8") as f:
                f.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
        except OSError:
            pass

    try:
        if mode == "open":
            # 受け箱か質問の資料を開いている人がいるときだけページに出す。いなければすぐターミナルに任せる
            ping = serve.ask_ping(conf)
            if not ping or not ping.get("viewers"):
                return 0
        elif not serve.ensure_ask_server(home, conf):
            log("配信サーバーを起動できなかった（%s を見る）" % os.path.join(home, "ask-server.log"))
            return 0
        status, reg = serve.ask_request(conf, "POST", "/__qa/ask", {
            "session_id": data.get("session_id"), "cwd": data.get("cwd"), "agent": agent,
            "questions": [{k: v for k, v in q.items() if k != "_id"} for q in questions if isinstance(q, dict)],
            "transcript_path": data.get("transcript_path"), "config_dir": os.environ.get(ACCOUNT_ENV[agent]),
            "wait": conf["wait"], "build": conf["build"]})
    except (OSError, ValueError) as e:
        log("質問を登録できなかった: %s" % e)
        return 0
    if status != 201:
        log("質問を登録できなかった（%s）: %s" % (status, reg.get("error")))
        return 0
    doc = reg["doc"]
    closed = []

    def close(reason):
        """締め切る。締め切る直前に答えが届いていれば、その状態（answered）が返る。"""
        if closed:
            return {}
        closed.append(reason)
        try:
            status, view = serve.ask_request(conf, "POST", "/__qa/ask-close", {"doc": doc, "reason": reason}, timeout=3)
            return view if status == 200 else {}
        except OSError:
            return {}

    def answered_codex(view, stored):
        """Codex の request_user_input の結果と同じ形の答えを、拒否の理由の文に入れて返す。
        モデルには「Tool call blocked by PreToolUse hook: <理由>」として届く。"""
        result = {}
        for q, key in zip(questions, stored):
            picked = (view.get("answers") or {}).get(key)
            if picked is None:
                continue
            values = [picked]
            note = ((view.get("annotations") or {}).get(key) or {}).get("notes")
            if note:
                values.append("user_note: %s" % note)
            result[q["_id"]] = {"answers": values}
        url = "%s%s" % (conf["public_url"], reg["url"])  # 合言葉は付けない（モデルの文脈に入る）
        reason = ("ユーザーは資料のページでこの質問に答えた。ツールは実行していないが、これが回答である。"
                  "同じ質問をし直さず、この回答で作業を続けること。資料: %s\n%s"
                  % (url, json.dumps({"answers": result}, ensure_ascii=False)))
        log("ページで答えた: %s" % doc)
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                 "permissionDecisionReason": reason},
                          "systemMessage": "visual-explain: 資料のページで答えました: %s" % url}, ensure_ascii=False))
        return 0

    def answered(view):
        orca_mark(False)  # 答えたので、付けた未読の印を外す
        # 答えは質問の文で引く。配信サーバーで切り詰めた文ではなく、元の質問の文に付け直す
        stored = [q.get("question") for q in view.get("questions") or []]
        if agent == "codex":
            return answered_codex(view, stored)
        answers, notes = {}, {}
        for orig, key in zip(inp.get("questions") or [], stored):
            if key in (view.get("answers") or {}):
                answers[orig["question"]] = view["answers"][key]
            if key in (view.get("annotations") or {}):
                notes[orig["question"]] = view["annotations"][key]
        updated = dict(inp, answers=answers)
        if notes:
            updated["annotations"] = notes
        log("ページで答えた: %s" % doc)
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "allow",
            "permissionDecisionReason": "資料のページで答えた", "updatedInput": updated},
            # 後から資料を見返せるよう、ターミナルに資料の URL を出す（開発者にだけ見え、会話の文脈には入らない）
            "systemMessage": "visual-explain: 資料のページで答えました: %s%s" % (conf["public_url"], reg["url"])}))
        return 0

    def moved():
        # 待つ間に ask-config で無効にした・待ち受け先を変えた。古い設定で配信サーバーを起動し直さない
        now_conf = serve.load_ask_config(home)
        return now_conf["mode"] == "off" or any(now_conf.get(k) != conf.get(k) for k in ("port", "bind", "token", "root"))

    def stopped(signum, frame):
        # ターミナルで止められた。ページから答えを送れないよう、締め切ってから終わる
        close("terminal")
        os._exit(0)

    for sig in (signal.SIGTERM, signal.SIGINT, getattr(signal, "SIGHUP", None)):
        if sig is not None:
            signal.signal(sig, stopped)
    log("質問をページに出した: %s%s" % (conf["public_url"], reg["url"]))
    orca_mark(True)  # Orca の端末なら、質問したワークツリーに未読の印を付ける
    path = "/__qa/ask-status?doc=" + quote(doc)
    deadline = time.time() + int(conf["wait"])
    misses, seen_viewer = 0, time.time()
    while time.time() < deadline:
        try:
            status, view = serve.ask_request(conf, "GET", path, timeout=5)
            misses = 0
        except OSError:
            misses += 1
            if misses % 10 == 0:  # 配信サーバーが止まった。設定が同じなら起動し直して待ち続ける
                if moved():
                    log("待つ間に設定が変わったため、ターミナルで答える: %s" % doc)
                    return 0
                serve.ensure_ask_server(home, conf)
            time.sleep(1)
            continue
        if status in (401, 404):
            return 0  # 合言葉が変わった、または質問の資料が無くなった
        state = view.get("status") if status == 200 else None
        if state == "answered":
            return answered(view)
        if state == "expired":
            view = close("expired")  # 資料作りを止める。直前に答えが届いていればそれを返す
            return answered(view) if view.get("status") == "answered" else 0
        if state == "terminal":
            return 0
        if view.get("viewers"):
            seen_viewer = time.time()
        elif mode == "open" and time.time() - seen_viewer > float(conf.get("viewer_grace") or serve.VIEWER_GRACE):
            # ページを閉じた。誰も見ていないページで待ち続けず、ターミナルで答えてもらう
            view = close("terminal")
            log("ページを見ている人がいなくなったため、ターミナルで答える: %s" % doc)
            return answered(view) if view.get("status") == "answered" else 0
        time.sleep(1)
    view = close("expired")
    return answered(view) if view.get("status") == "answered" else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # どの質問・セッションの前にも動くので、思わぬ失敗でも Claude Code を止めない
        try:
            with open(os.path.join(state_home(), "ask-hook.log"), "a", encoding="utf-8") as f:
                f.write("[%s] フックが失敗した: %r\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), e))
        except OSError:
            pass
        sys.exit(0)
