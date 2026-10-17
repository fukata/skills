"""説明動画の音声を選び、必要な場合だけ専用の VOICEVOX コンテナーを準備する。"""

import ipaddress
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import urllib.parse
import uuid


IMAGE = "voicevox/voicevox_engine:cpu-latest"
OWNER_LABEL = "io.fukata.visual-explain.video"
LABELS = {"none": "なし", "open-jtalk": "合成音声（Open JTalk）", "recorded": "提供音声", "voicevox": "合成音声（VOICEVOX）"}


class SpeechError(ValueError):
    """音声環境・合成の失敗。自動選択では次の方式を試せる。"""


class VoicevoxUnavailable(SpeechError):
    """既存エンジンへ接続できず、Docker による起動を試せる。"""


def event(args, method, status, reason):
    args.speech_events.append({"method": method, "status": status, "reason": reason})
    print("音声: " + reason, flush=True)


def prepare_open_jtalk(args):
    executable = shutil.which(args.open_jtalk)
    if not executable:
        raise SpeechError("Open JTalk が見つからない")
    args.open_jtalk = str(Path(executable).resolve())
    if args.dictionary is None:
        args.dictionary = next((p for p in map(Path, (
            "/var/lib/mecab/dic/open-jtalk/naist-jdic", "/usr/share/open-jtalk/naist-jdic",
            "/usr/local/share/open_jtalk/open_jtalk_dic_utf_8-1.11",
            "/opt/homebrew/share/open_jtalk/open_jtalk_dic_utf_8-1.11")) if p.is_dir()), None)
    if args.voice is None:
        args.voice = next((p for p in map(Path, (
            "/usr/share/hts-voice/nitech-jp-atr503-m001/nitech_jp_atr503_m001.htsvoice",
            "/usr/local/share/hts-voice/nitech-jp-atr503-m001/nitech_jp_atr503_m001.htsvoice")) if p.is_file()), None)
    if not args.dictionary or not args.dictionary.is_dir() or not args.voice or not args.voice.is_file():
        raise SpeechError("Open JTalk の辞書・音声モデルが無い（--dictionary と --voice で指定できる）")


class ManagedVoicevox:
    def __init__(self, args):
        self.args = args
        self.deadline = time.monotonic() + args.voicevox_setup_timeout
        self.token = uuid.uuid4().hex
        self.name = "visual-explain-video-" + self.token
        self.created = False
        self.docker = None

    def __enter__(self):
        return self

    def command(self, *argv, check=True, limit=None, cleanup=False):
        remaining = 10 if cleanup else self.deadline - time.monotonic()
        if remaining <= 0:
            raise SpeechError("VOICEVOX の準備が時間切れになった")
        try:
            result = subprocess.run([self.docker, *argv], capture_output=True, text=True,
                                    timeout=min(remaining, limit) if limit else remaining)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SpeechError("Docker の %s を実行できない（時間切れを含む）" % argv[0]) from exc
        if check and result.returncode:
            raise SpeechError("Docker の %s が失敗: %s" % (argv[0], result.stderr.strip()[-1000:]))
        return result

    def start(self, prepare_voicevox):
        self.docker = shutil.which(self.args.docker)
        if not self.docker:
            raise SpeechError("Docker が見つからない")
        # 別マシンの Docker で起動しても、この端末のループバックから利用できない。
        endpoint = os.environ.get("DOCKER_HOST") if not os.environ.get("DOCKER_CONTEXT") else None
        if not endpoint:
            raw = self.command("context", "inspect", "--format", "{{json .Endpoints.docker.Host}}", limit=10).stdout
            try:
                endpoint = json.loads(raw)
            except ValueError as exc:
                raise SpeechError("Docker の接続先を確認できない") from exc
        if not isinstance(endpoint, str):
            raise SpeechError("Docker の接続先の形式が不正")
        url = urllib.parse.urlsplit(endpoint)
        try:
            local_tcp = url.scheme == "tcp" and (url.hostname == "localhost" or ipaddress.ip_address(url.hostname).is_loopback)
        except ValueError:
            local_tcp = False
        if url.scheme not in {"unix", "npipe"} and not local_tcp:
            raise SpeechError("この端末の Docker ではないため、自動起動を省略した")
        if self.command("info", "--format", "{{.OSType}}", limit=10).stdout.strip() != "linux":
            raise SpeechError("Linux コンテナーを利用できる Docker ではない")
        if self.command("image", "inspect", IMAGE, check=False, limit=10).returncode:
            event(self.args, "docker", "progress", "VOICEVOX の Docker イメージを取得する（初回のみ）")
            self.command("pull", IMAGE)
        else:
            event(self.args, "docker", "progress", "取得済みの VOICEVOX イメージを再利用する")
        # 応答が時間切れになっても、自分の印で作成済みか確認して後片付けできる。
        self.created = True
        self.command("create", "--name", self.name, "--label", OWNER_LABEL + "=" + self.token,
                     "--rm", "--pull", "never", "-p", "127.0.0.1::50021", IMAGE)
        self.command("start", self.name, limit=30)
        raw = self.command("container", "inspect", "--format", "{{json .NetworkSettings.Ports}}", self.name, limit=10).stdout
        try:
            binding = json.loads(raw)["50021/tcp"][0]
            port = int(binding["HostPort"])
            if binding["HostIp"] != "127.0.0.1" or not 1 <= port <= 65535:
                raise ValueError("ループバックのポートではない")
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise SpeechError("VOICEVOX の待ち受け先を確認できない") from exc
        self.args.voicevox_url = "http://127.0.0.1:%d" % port
        event(self.args, "docker", "progress", "専用の VOICEVOX エンジンが応答するまで待つ")
        while True:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise SpeechError("VOICEVOX の起動待ちが時間切れになった")
            try:
                prepare_voicevox(self.args, timeout=min(2, remaining))
                return
            except VoicevoxUnavailable:
                time.sleep(min(0.2, max(0, self.deadline - time.monotonic())))

    def __exit__(self, *exc):
        if not self.created:
            return
        try:
            result = self.command("container", "inspect", "--format", "{{json .Config.Labels}}", self.name,
                                  check=False, cleanup=True)
            if result.returncode:
                if "No such" in result.stderr:
                    return  # --rm により既に終了・削除された。
                raise SpeechError("Docker にコンテナーの状態を確認できない")
            if json.loads(result.stdout).get(OWNER_LABEL) != self.token:
                raise SpeechError("自分が作成したコンテナーの印がないため削除しない")
            self.command("rm", "-f", self.name, cleanup=True)
            event(self.args, "docker", "cleaned", "自分で起動した VOICEVOX コンテナーを削除した")
        except (SpeechError, ValueError, TypeError, AttributeError) as error:
            event(self.args, "docker", "cleanup-failed", "%s。後片付けの対象: %s" % (error, self.name))


def choose_audio(story, args, work, prepare_voicevox, make_audio):
    """全場面の合成に成功した方式だけを採用し、失敗した音声は残さない。"""
    requested = args.speech
    args.requested_speech, args.speech_events = requested, []
    cues = [c for s in story["scenes"] for c in s["narration"]]
    if requested == "auto" and any("audio" in c for c in cues):
        if not all("audio" in c for c in cues):
            raise ValueError("自動選択で提供音声を使う場合は全ての文に audio を指定する。混在させる場合は音声方式を明示する")
        methods = ["recorded"]
    else:
        methods = (["voicevox", "open-jtalk", "none"] if cues else ["none"]) if requested == "auto" else [requested]
    for method in methods:
        args.speech, args.speech_label, args.speech_credit = method, LABELS[method], ""
        try:
            with tempfile.TemporaryDirectory(prefix="speech-", dir=work) as folder:
                attempt = Path(folder)
                with ManagedVoicevox(args) as engine:
                    if method == "voicevox":
                        try:
                            prepare_voicevox(args, timeout=2 if requested == "auto" else args.speech_timeout)
                        except VoicevoxUnavailable:
                            if requested != "auto":
                                raise
                            event(args, "voicevox", "progress", "起動済みの VOICEVOX に接続できないため Docker を試す")
                            engine.start(prepare_voicevox)
                    elif method == "open-jtalk":
                        prepare_open_jtalk(args)
                    timeline, captions = make_audio(story, args, attempt)
                for path in attempt.glob("audio-*.wav"):
                    path.replace(work / path.name)
            event(args, method, "selected", "音声方式を選択した: " + args.speech_label)
            return timeline, captions
        except SpeechError as exc:
            if requested != "auto":
                raise
            event(args, method, "failed", "%s を利用できない: %s" % (LABELS[method], exc))
    raise SpeechError("音声方式を選択できなかった")
