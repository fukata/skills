#!/usr/bin/env python3
"""図・録画・テロップ・文ごとの字幕と音声から説明動画を作る。音声合成はローカルで行う。"""

import argparse
import html
import http.client
import importlib.util
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import wave


SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / "scripts"))
from video_speech import SpeechError, VoicevoxUnavailable, choose_audio  # noqa: E402

WIDTH, HEIGHT, FPS, RATE = 1280, 720, 24, 48000
LEAD, GAP, TAIL = 0.35, 0.2, 0.55
IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".webp", ".svg"}
VIDEO_TYPES = {".webm", ".mp4", ".mov", ".mkv", ".m4v"}
# 録画の場面で映像を置く枠（左・上・幅・高さ）。見出しと要点を細くし、字幕の帯は図の場面と同じ位置に残す。
CLIP_BOX = (40, 58, 1200, 496)


def run(argv, timeout=300, **kwargs):
    result = subprocess.run([str(v) for v in argv], capture_output=True, timeout=timeout, **kwargs)
    if result.returncode:
        raise ValueError("%s が失敗した:\n%s" % (Path(argv[0]).name, result.stderr.decode("utf-8", "replace")[-3000:]))
    return result.stdout


def number(value, label, low=0.1, high=3600):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError("%s は %s〜%s の数値にする" % (label, low, high))
    return float(value)


def text(value, label, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError("%s は 1〜%d 文字にする" % (label, limit))
    if any(ord(c) < 32 and c != "\n" for c in value):
        raise ValueError("%s に制御文字は使えない" % label)
    return value.strip()


def local_file(base, value, label):
    value = text(value, label, 4096)
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", value):
        raise ValueError("%s は URL でなくローカルのファイルを指定する" % label)
    path = (base / value).resolve()
    if not path.is_file():
        raise ValueError("%s が無い: %s" % (label, path))
    return path


def load_story(path, speech):
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("台本は JSON のオブジェクトにする")
    title = text(data.get("title"), "title", 80)
    scenes = data.get("scenes")
    if not isinstance(scenes, list) or not 1 <= len(scenes) <= 100:
        raise ValueError("scenes は 1〜100 場面の配列にする")
    for i, scene in enumerate(scenes, 1):
        if not isinstance(scene, dict):
            raise ValueError("場面 %d はオブジェクトにする" % i)
        scene["title"] = text(scene.get("title"), "場面 %d の title" % i, 40)
        scene["telop"] = text(scene.get("telop"), "場面 %d の telop" % i, 60)
        if ("image" in scene) == ("video" in scene):
            raise ValueError("場面 %d には image か video のどちらか一つを指定する" % i)
        if "image" in scene:
            scene["image"] = local_file(path.parent, scene.get("image"), "場面 %d の image" % i)
            if scene["image"].suffix.lower() not in IMAGE_TYPES:
                raise ValueError("image は PNG・JPEG・WebP・SVG にする")
        else:
            load_clip(path.parent, scene, i)
        scene["seconds"] = number(scene.get("seconds", 1), "場面の seconds")
        narration = scene.get("narration", [])
        if not isinstance(narration, list) or len(narration) > 100:
            raise ValueError("narration は文ごとの配列にする（100 文まで）")
        for cue in narration:
            if not isinstance(cue, dict):
                raise ValueError("narration の各文はオブジェクトにする")
            cue["text"] = text(cue.get("text"), "narration の text", 100)
            if "reading" in cue:
                cue["reading"] = text(cue["reading"], "narration の reading", 200)
            cue["seconds"] = number(cue.get("seconds", max(2, len(cue["text"]) / 6)), "文の seconds")
            if "audio" in cue:
                cue["audio"] = local_file(path.parent, cue["audio"], "文の audio")
                if speech == "none":
                    raise ValueError("audio を使うには --speech recorded・open-jtalk・voicevox のいずれかを指定する")
            elif speech == "recorded":
                raise ValueError("--speech recorded ではすべての文に audio が必要")
        scene["narration"] = narration
    if speech not in {"none", "auto"} and not any(s["narration"] for s in scenes):
        raise ValueError("音声付き動画には narration が必要")
    return {"title": title, "scenes": scenes}


def load_clip(base, scene, i):
    """実際の画面の録画を使う場面の、切り出す範囲・倍率・拡大する領域を読む。"""
    scene["video"] = local_file(base, scene.get("video"), "場面 %d の video" % i)
    if scene["video"].suffix.lower() not in VIDEO_TYPES:
        raise ValueError("video は WebM・MP4・MOV・MKV にする")
    scene["start"] = number(scene.get("start", 0), "場面 %d の start" % i, 0, 36000)
    if "end" in scene:
        scene["end"] = number(scene["end"], "場面 %d の end" % i, 0, 36000)
        if scene["end"] <= scene["start"]:
            raise ValueError("場面 %d の end は start より後にする" % i)
    scene["speed"] = number(scene.get("speed", 1), "場面 %d の speed" % i, 0.25, 16)
    if "crop" in scene:
        match = re.fullmatch(r"(\d{1,5}):(\d{1,5}):(\d{1,5}):(\d{1,5})", scene["crop"]) if isinstance(scene["crop"], str) else None
        if not match or int(match.group(1)) < 16 or int(match.group(2)) < 16:
            raise ValueError("場面 %d の crop は「幅:高さ:左:上」のピクセル数にする（幅・高さは 16 以上）" % i)
        scene["crop"] = tuple(int(v) for v in match.groups())


def probe_clip(ffprobe, scene, i):
    """録画の大きさと長さを読み、切り出す範囲が録画に収まるか確かめ、映像を置く位置を決める。"""
    try:
        info = json.loads(run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                               "stream=width,height:format=duration", "-of", "json", scene["video"]], timeout=60))
        width, height = int(info["streams"][0]["width"]), int(info["streams"][0]["height"])
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise ValueError("場面 %d の video を動画として読めない: %s" % (i, scene["video"].name)) from exc
    try:
        length = float(info["format"]["duration"])
    except (KeyError, TypeError, ValueError):
        # 録画ツールの WebM は全体の長さを持たないことがあるので、映像の最後の時刻で測る。
        times = run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pts_time",
                     "-of", "csv=p=0", scene["video"]], timeout=120).decode("utf-8", "replace").split()
        length = max((float(t) for t in times if re.fullmatch(r"\d+(\.\d+)?", t)), default=0.0)
    end = scene.get("end", length)
    if not length or scene["start"] >= length or end > length + 0.05:
        raise ValueError("場面 %d の start・end が録画の長さ（%.2f 秒）に収まらない" % (i, length))
    crop = scene.get("crop", (width, height, 0, 0))
    if crop[0] + crop[2] > width or crop[1] + crop[3] > height:
        raise ValueError("場面 %d の crop が録画（%d×%d）からはみ出す" % (i, width, height))
    left, top, box_width, box_height = CLIP_BOX
    scale = min(box_width / crop[0], box_height / crop[1])
    w, h = (min(limit, max(2, round(v * scale / 2) * 2)) for v, limit in ((crop[0], box_width), (crop[1], box_height)))
    scene["clip"] = {"width": w, "height": h, "x": left + (box_width - w) // 2, "y": top + (box_height - h) // 2,
                     "crop": crop, "end": end, "seconds": (end - scene["start"]) / scene["speed"]}
    # 解説が短くても録画は最後まで見せる。解説が長ければ、録画の最後の画面を止めて待つ。
    scene["seconds"] = max(scene["seconds"], scene["clip"]["seconds"])


def clip_filter(scene):
    clip = scene["clip"]
    return "crop=%d:%d:%d:%d,scale=%d:%d,setsar=1" % (clip["crop"] + (clip["width"], clip["height"]))


def clip_poster(ffmpeg, scene, i, work):
    """場面の画像（再生前の表示と字幕の位置の確認に使う）に、録画の最初の画面を置く。"""
    poster = work / ("poster-%03d.png" % i)
    run([ffmpeg, "-v", "error", "-nostdin", "-y", "-ss", "%.3f" % scene["start"], "-i", scene["video"],
         "-frames:v", "1", "-vf", clip_filter(scene), poster], timeout=120)
    if not poster.is_file():
        raise ValueError("場面 %d の録画から最初の画面を取り出せなかった" % i)
    scene["poster"] = poster


def speed_label(speed):
    if speed == 1:
        return ""
    return "%s ×%g" % ("早送り" if speed > 1 else "スロー", speed)


def browser(explicit):
    spec = importlib.util.spec_from_file_location("ve_check", SKILL / "scripts/check-explanation.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    chrome = module.find_chrome(explicit)
    if not chrome:
        raise ValueError("ヘッドレス Chrome が必要（--chrome または VISUAL_EXPLAIN_CHROME で指定できる）")
    return [chrome] + module.headless_flag(chrome)


def render_frame(chrome, scene, index, count, work):
    """図を縮尺を保って収め、テロップと字幕の場所を別に確保する。"""
    page = work / "frame.html"
    clip = scene.get("clip")
    if clip:
        # 録画は図より大きく見せ、実際の画面であることと早送りの倍率を見出しの右に出す。
        layout = '''header { height:58px; padding:0 48px } h1 { font-size:26px }
.visual { position:relative; height:496px; background:#000; border-radius:10px }
img { position:absolute; left:%dpx; top:%dpx; width:%dpx; height:%dpx }
.badges { margin-left:auto; display:flex; gap:10px } .badge { padding:3px 12px; border:2px solid #77e4cb;
 border-radius:999px; color:#77e4cb; font-size:17px; font-weight:700; white-space:nowrap }
.telop { height:68px } .message { font-size:26px; white-space:nowrap }''' % (
            clip["x"] - CLIP_BOX[0], clip["y"] - CLIP_BOX[1], clip["width"], clip["height"])
        badges = "".join('<span class="badge">%s</span>' % html.escape(v) for v in ("実際の画面の録画", speed_label(scene["speed"])) if v)
        source, badges = scene["poster"], '<div class="badges">%s</div>' % badges
    else:
        layout = '''header { height:94px; padding:22px 48px } h1 { font-size:32px }
.visual { height:402px; display:flex; align-items:center; justify-content:center; background:#f7f9fc; border-radius:14px }
img { width:1168px; height:378px; object-fit:contain }
.telop { height:126px } .message { font-size:30px }'''
        source, badges = scene["image"], ""
    page.write_text('''<!doctype html><html lang="ja"><meta charset="utf-8">
<style>
* { box-sizing:border-box } body { margin:0; width:1280px; height:720px; background:#101d30;
 color:#f7faff; font-family:system-ui,sans-serif } header { display:flex; align-items:center; gap:24px }
.count { color:#77e4cb; font-size:22px } h1 { margin:0; line-height:1.3; white-space:nowrap }
.visual { margin:0 40px; overflow:hidden }
.telop { margin:0 48px; display:flex; gap:20px; align-items:center }
.label { color:#77e4cb; font-size:20px; flex-shrink:0 }
.message { font-weight:700; line-height:1.4 }
.subtitles { border-top:1px solid #31435b; height:98px; background:#081321 }
%s
</style><header><span class="count">%02d / %02d</span><h1>%s</h1>%s</header>
<div class="visual"><img src="%s" alt="説明の図"></div>
<div class="telop"><span class="label">要点</span><div class="message">%s</div></div><div class="subtitles"></div>
<script>addEventListener('load', async () => {
 await document.fonts.ready;
 const errors = [];
 const img = document.querySelector('img');
 if (!img.complete || !img.naturalWidth) errors.push('図を読み込めない');
 for (const selector of ['h1', '.badges', '.message']) {
  const el = document.querySelector(selector);
  if (!el) continue;
  const r = el.getBoundingClientRect();
  if (el.scrollWidth > el.clientWidth + 1 || r.right > 1232 || r.bottom > 622)
   errors.push(selector + ' の文字が表示領域からはみ出す');
 }
 const report = document.createElement('pre'); report.id = 'report'; report.hidden = true;
 report.textContent = JSON.stringify(errors); document.body.append(report);
});</script></html>''' % (layout, index, count, html.escape(scene["title"]), badges,
                         html.escape(source.as_uri(), quote=True), html.escape(scene["telop"])), encoding="utf-8")
    frame = work / "frames" / ("%03d.png" % index)
    result = run(chrome + ["--no-sandbox", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
                          "--allow-file-access-from-files", "--force-device-scale-factor=1",
                          "--user-data-dir=" + str(work / "profile"), "--window-size=1280,720",
                          "--virtual-time-budget=1500", "--screenshot=" + str(frame), "--dump-dom", page.as_uri()])
    match = re.search(r'<pre id="report"[^>]*>(.*?)</pre>', result.decode("utf-8", "replace"), re.S)
    if not match or not frame.is_file():
        raise ValueError("場面 %d の画像と表示検査の結果を取り出せなかった" % index)
    errors = json.loads(html.unescape(match.group(1)))
    if errors:
        raise ValueError("場面 %d: %s" % (index, "、".join(errors)))


def silence(out, seconds):
    out.writeframes(b"\0\0" * round(seconds * RATE))


def pcm_audio(ffmpeg, source, target, timeout=300):
    run([ffmpeg, "-v", "error", "-nostdin", "-y", "-i", source, "-vn", "-ac", "1", "-ar", RATE,
         "-c:a", "pcm_s16le", target], timeout=timeout)
    with wave.open(str(target), "rb") as wav:
        frames = wav.readframes(wav.getnframes())
    if not frames:
        raise ValueError("音声が空: %s" % source)
    return frames


def check_pronunciation(trace, speed):
    """通常の解説で、連続する音素が異常に引き延ばされる合成を検出する。"""
    consecutive = 0
    labels = re.findall(r"^(\d+) (\d+) \S*?\^\S*?-(\w+)\+", trace.read_text(encoding="utf-8"), re.M)
    if not labels:
        raise SpeechError("Open JTalk の音素長を検査できない")
    for first, last, phone in labels:
        prolonged = phone not in {"sil", "pau"} and (int(last) - int(first)) / 1e7 * speed >= 0.35
        consecutive = consecutive + 1 if prolonged else 0
        if consecutive >= 8:
            raise SpeechError("Open JTalk の発音が不自然に引き延ばされている。読点で区切るか、--speech voicevox・recorded を使う")


def voicevox_request(base, endpoint, query=None, body=None, timeout=120):
    url = base + endpoint + ("?" + urllib.parse.urlencode(query) if query else "")
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method="GET" if endpoint == "/speakers" else "POST",
                                     headers={"Content-Type": "application/json"})
    # ローカルの合成要求を、プロキシやリダイレクトで外部へ転送しない。
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise SpeechError("VOICEVOX の %s が失敗した（HTTP %s）" % (endpoint, exc.code)) from exc
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        raise VoicevoxUnavailable("VOICEVOX の %s に接続できない: %s" % (endpoint, exc)) from exc


def prepare_voicevox(args, timeout=120):
    url = urllib.parse.urlsplit(args.voicevox_url)
    try:
        local = url.hostname == "localhost" or ipaddress.ip_address(url.hostname).is_loopback
    except ValueError:
        local = False
    if url.scheme != "http" or not local or url.username or url.password or url.query or url.fragment or url.path not in {"", "/"}:
        raise ValueError("--voicevox-url はローカルの HTTP アドレスにする（例: http://127.0.0.1:50021）")
    args.voicevox_url = args.voicevox_url.rstrip("/")
    try:
        speakers = json.loads(voicevox_request(args.voicevox_url, "/speakers", timeout=timeout))
    except ValueError as exc:
        if isinstance(exc, SpeechError):
            raise
        raise SpeechError("VOICEVOX の話者一覧を読み取れない") from exc
    if not isinstance(speakers, list):
        raise SpeechError("VOICEVOX の話者一覧を読み取れない")
    for speaker in speakers:
        if not isinstance(speaker, dict) or not isinstance(speaker.get("styles"), list):
            raise SpeechError("VOICEVOX の話者一覧の形式が不正")
        for style in speaker.get("styles", []):
            if not isinstance(style, dict):
                raise SpeechError("VOICEVOX の話し方の形式が不正")
            if style.get("id") == args.voicevox_speaker:
                try:
                    args.speech_credit = "VOICEVOX:" + text(speaker.get("name"), "話者名", 60)
                    args.speech_label = "合成音声（%s / %s）" % (args.speech_credit, text(style.get("name"), "話し方", 60))
                except ValueError as exc:
                    raise SpeechError(str(exc)) from exc
                return
    raise SpeechError("VOICEVOX に話者 ID %d が無い" % args.voicevox_speaker)


def synthesize(cue, args, source):
    deadline = time.monotonic() + getattr(args, "speech_timeout", 120)

    def remaining():
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise SpeechError("文ごとの音声合成が時間切れになった")
        return seconds

    try:
        if args.speech == "voicevox":
            query = json.loads(voicevox_request(args.voicevox_url, "/audio_query",
                {"speaker": args.voicevox_speaker, "text": cue.get("reading", cue["text"])}, timeout=remaining()))
            if not isinstance(query, dict):
                raise SpeechError("VOICEVOX の合成用データの形式が不正")
            query["speedScale"] = args.speed
            source.write_bytes(voicevox_request(args.voicevox_url, "/synthesis",
                {"speaker": args.voicevox_speaker}, query, timeout=remaining()))
        else:
            trace = source.with_suffix(".trace")
            run([args.open_jtalk, "-x", args.dictionary, "-m", args.voice, "-r", args.speed,
                 "-ow", source, "-ot", trace], timeout=remaining(),
                input=(cue.get("reading", cue["text"]) + "\n").encode("utf-8"))
            check_pronunciation(trace, args.speed)
        return pcm_audio(args.ffmpeg, source, source.with_suffix(".pcm.wav"), timeout=remaining())
    except SpeechError:
        raise
    except (ValueError, OSError, subprocess.TimeoutExpired, wave.Error) as exc:
        raise SpeechError("音声の合成・読み取りに失敗した: %s" % exc) from exc


def make_audio(story, args, work):
    offset, cues, timeline = 0.0, [], []
    for index, scene in enumerate(story["scenes"], 1):
        cursor, scene_cues = LEAD, []
        path = work / ("audio-%03d.wav" % index)
        with wave.open(str(path), "wb") as out:
            out.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
            silence(out, LEAD)
            for j, cue in enumerate(scene["narration"], 1):
                if args.speech == "none":
                    duration = cue["seconds"]
                    silence(out, duration)
                else:
                    source = cue.get("audio")
                    if source is None:
                        source = work / ("speech-%03d-%03d.wav" % (index, j))
                        pcm = synthesize(cue, args, source)
                    else:
                        pcm = pcm_audio(args.ffmpeg, source, work / "normalized.wav")
                    duration = len(pcm) / (RATE * 2)
                    out.writeframes(pcm)
                item = {"start": round(offset + cursor, 6), "end": round(offset + cursor + duration, 6),
                        "text": cue["text"], "scene": index}
                cues.append(item)
                scene_cues.append(dict(item, start=cursor, end=cursor + duration))
                cursor += duration + GAP
                silence(out, GAP)
            reading_time = len(scene["telop"]) / 6 if args.speech == "none" and not scene["narration"] else 0
            duration = math.ceil(max(scene["seconds"], cursor + TAIL, reading_time) * FPS) / FPS
            silence(out, duration - cursor)
        timeline.append({"scene": index, "title": scene["title"], "telop": scene["telop"],
                         "start": round(offset, 6), "end": round(offset + duration, 6),
                         "duration": duration, "cues": scene_cues})
        if "clip" in scene:
            clip = scene["clip"]
            timeline[-1]["video"] = {"source": scene["video"].name, "start": scene["start"], "end": clip["end"],
                                     "speed": scene["speed"], "crop": "%d:%d:%d:%d" % clip["crop"],
                                     "seconds": round(clip["seconds"], 6)}
        offset += duration
    return timeline, cues


def timestamp(seconds, separator="."):
    ms = round(seconds * 1000)
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return "%02d:%02d:%02d%s%03d" % (h, m, s, separator, ms)


def wrap_caption(value, units=62):
    lines, line, width = [], "", 0
    for char in value:
        size = 2 if unicodedata.east_asian_width(char) in "WFA" else 1
        if char == "\n" or width + size > units:
            lines.append(line)
            line, width = "", 0
        if char != "\n":
            line += char
            width += size
    lines.append(line)
    if len(lines) > 2:
        raise ValueError("字幕が 2 行を超えるため文を分ける: %s" % value)
    return "\n".join(lines)


def write_subtitles(cues, work):
    vtt, srt = ["WEBVTT\n"], []
    for i, cue in enumerate(cues, 1):
        value = wrap_caption(cue["text"])
        # VTT のタグと時刻の区切りとして解釈されないようにする。
        safe = html.escape(value, quote=False).replace("-->", "--&gt;")
        vtt.append("%d\n%s --> %s\n%s\n" % (i, timestamp(cue["start"]), timestamp(cue["end"]), safe))
        srt.append("%d\n%s --> %s\n%s\n" % (i, timestamp(cue["start"], ","), timestamp(cue["end"], ","), safe))
    (work / "captions.vtt").write_text("\n".join(vtt), encoding="utf-8")
    (work / "captions.srt").write_text("\n".join(srt), encoding="utf-8")


def ass_time(seconds):
    cs = round(seconds * 100)
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return "%d:%02d:%02d.%02d" % (h, m, s, cs)


def burn_subtitles(cues, path, font):
    # 原稿に ASS の装飾命令を混ぜても実行されないよう、記号は全角にする。
    head = """[Script Info]
ScriptType: v4.00+
PlayResX: 1280
PlayResY: 720
WrapStyle: 2
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,%s,32,&H00FFFFFF,&H00FFFFFF,&H00132108,&H00132108,0,0,0,0,100,100,0,0,1,1,0,2,50,50,17,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
""" % font
    for cue in cues:
        value = wrap_caption(cue["text"]).translate(str.maketrans({"{": "｛", "}": "｝", "\\": "＼"})).replace("\n", r"\N")
        head += "Dialogue: 0,%s,%s,Caption,,0,0,0,,%s\n" % (ass_time(cue["start"]), ass_time(cue["end"]), value)
    path.write_text(head, encoding="utf-8")


def encode(story, timeline, cues, args, work):
    for scene, item in zip(story["scenes"], timeline):
        i = item["scene"]
        print("動画を作成: %d/%d %s" % (i, len(timeline), item["title"]), flush=True)
        filters = "format=yuv420p"
        if args.burn_captions and item["cues"]:
            burn_subtitles(item["cues"], work / "scene.ass", args.font)
            filters += ",ass=scene.ass"
        command = [args.ffmpeg, "-v", "error", "-nostdin", "-y", "-loop", "1", "-framerate", FPS, "-i", "frames/%03d.png" % i]
        clip = scene.get("clip")
        if clip:
            command += ["-ss", "%.3f" % scene["start"], "-t", "%.3f" % (clip["end"] - scene["start"]), "-i", scene["video"]]
        command += ["-i", "audio-%03d.wav" % i, "-t", "%.6f" % item["duration"]]
        if clip:
            # 録画を切り出して倍率どおりに速め、場面の画像の枠に重ねる。録画が先に終われば最後の画面を止めて見せる。
            command += ["-filter_complex", "[1:v]%s,setpts=(PTS-STARTPTS)/%r,fps=%d,tpad=stop_mode=clone:stop_duration=%.3f[clip];"
                        "[0:v][clip]overlay=%d:%d,%s[v]" % (clip_filter(scene), scene["speed"], FPS, item["duration"],
                                                            clip["x"], clip["y"], filters), "-map", "[v]"]
        else:
            command += ["-vf", filters]
        command += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-threads", "2", "-video_track_timescale", "24000"]
        if args.speech == "none":
            command += ["-an"]
        else:
            # 場面の接続時に AAC の先読み分が積み重ならないよう、ここでは PCM のままにする。録画の音は使わない。
            command += (["-map", "2:a"] if clip else []) + ["-c:a", "pcm_s16le"]
        run(command + ["segment-%03d.mkv" % i], cwd=work)
    (work / "segments.txt").write_text("".join("file 'segment-%03d.mkv'\n" % s["scene"] for s in timeline), encoding="utf-8")
    command = [args.ffmpeg, "-v", "error", "-nostdin", "-y", "-f", "concat", "-safe", "1", "-i", "segments.txt"]
    if cues and not args.burn_captions:
        command += ["-i", "captions.srt", "-map", "0:v:0", "-map", "0:a:0?", "-map", "1:0",
                    "-c:s", "mov_text", "-metadata:s:s:0", "language=jpn", "-disposition:s:0", "default"]
    command += ["-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
                "-metadata", "title=" + story["title"], "-metadata", "comment=" + args.speech_label, "explanation.mp4"]
    run(command, cwd=work)


def write_page(story, timeline, cues, args, work):
    esc = html.escape
    events = getattr(args, "speech_events", [])
    selection = '<details class="more"><summary>音声の選択結果</summary><ol>%s</ol></details>' % "".join(
        "<li>%s</li>" % esc(item["reason"]) for item in events)
    transcript = [story["title"], "音声: " + args.speech_label] + [item["reason"] for item in events]
    sections, recorded = [], []
    for scene, timing in zip(story["scenes"], timeline):
        i = timing["scene"]
        footage = ""
        if "clip" in scene:
            recorded.append("%02d" % i)
            footage = "実際の画面の録画" + ("（%s）" % speed_label(scene["speed"]) if scene["speed"] != 1 else "")
        sections.append('<section id="scene-%d"><h2>%02d · %s</h2><p class="key">%s</p>'
                        '<figure><img src="frames/%03d.png" alt="%s"><figcaption>%s〜%s%s</figcaption></figure>%s</section>' %
                        (i, i, esc(scene["title"]), esc(scene["telop"]), i, esc(scene["telop"]),
                         timestamp(timing["start"]), timestamp(timing["end"]), " · " + esc(footage) if footage else "",
                         "".join("<p>%s</p>" % esc(c["text"]) for c in scene["narration"])))
        transcript += ["", "[%s] %s" % (timestamp(timing["start"]), scene["title"]), "要点: " + scene["telop"]]
        transcript += ["映像: " + footage] if footage else []
        transcript += [cue["text"] for cue in scene["narration"]]
    if recorded:
        selection = ("<p>場面 %s は実際の画面の録画です。早送り・スローにした場面は、映像の上に倍率を表示しています。</p>"
                     % "・".join(recorded)) + selection
    css = re.search(r"<style>(.*?)</style>", (SKILL / "assets/explanation.html").read_text(encoding="utf-8"), re.S).group(1)
    track = ('<track kind="captions" src="captions.vtt" srclang="ja" label="日本語" default>'
             if not args.burn_captions else "")
    body = '''<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>%s</title><style>%s
video { width:100%%; height:auto; background:#101d30; border-radius:12px }
@media print { video { display:none } }
</style></head><body><div class="page"><main>
<header><div class="toolbar"><label><input type="checkbox" id="wide">横幅いっぱいに表示</label></div>
<h1>%s</h1><p class="lead">%s</p></header>
<nav class="toc" aria-label="資料の目次"><a href="#context">前提</a><a href="#movie">説明動画</a>%s</nav>
<section id="context"><h2>前提</h2><p>図の要点をテロップで示し、文ごとの解説を字幕%sで伝える説明動画です。
再生ボタンを押すと始まります。%s</p>%s</section>
<section id="movie"><h2>説明動画 · %.1f 秒</h2>
<video controls preload="metadata" poster="frames/001.png" aria-label="%s">
<source src="explanation.mp4" type="video/mp4">%s
動画を再生できない場合は、下のリンクから保存してください。</video>
<p><a href="explanation.mp4" download>動画を保存</a> · <a href="captions.vtt" download>字幕</a> ·
<a href="transcript.txt" download>解説原稿</a></p></section>%s</main></div></body></html>''' % (
        esc(story["title"]), css, esc(story["title"]), esc(story["scenes"][0]["telop"]),
        "".join('<a href="#scene-%d">%02d · %s</a>' % (i, i, esc(s["title"])) for i, s in enumerate(story["scenes"], 1)),
        "と音声" if args.speech != "none" else "",
        ("音声はありません。" if args.speech == "none" else esc(args.speech_label) + "を使用しています。") +
        ("字幕は動画に焼き込まれています。" if args.burn_captions else "字幕は再生プレイヤーで切り替えられます。"),
        selection,
        timeline[-1]["end"], esc(story["title"]), track, "".join(sections))
    (work / "explanation.html").write_text(body, encoding="utf-8")
    (work / "transcript.txt").write_text("\n".join(transcript) + "\n", encoding="utf-8")
    (work / "timeline.json").write_text(json.dumps({"title": story["title"], "speech": args.speech, "speech_label": args.speech_label,
        "requested_speech": getattr(args, "requested_speech", args.speech), "speech_events": events,
        "burn_captions": args.burn_captions, "duration": timeline[-1]["end"], "scenes": timeline, "cues": cues},
        ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("story", type=Path, help="台本 JSON。画像・録画・音声の相対パスはこのファイルを基準にする")
    parser.add_argument("--out", type=Path, required=True, help="新しく作る出力フォルダ（既存のフォルダは上書きしない）")
    parser.add_argument("--speech", choices=("auto", "none", "open-jtalk", "voicevox", "recorded"), default="auto",
                        help="既定 auto: VOICEVOX → Open JTalk → 無音の順で自動選択")
    parser.add_argument("--voicevox-url", default="http://127.0.0.1:50021", help="ローカルの VOICEVOX エンジン")
    parser.add_argument("--voicevox-speaker", type=int, default=11, help="VOICEVOX のスタイル ID（既定11: 玄野武宏・ノーマル）")
    parser.add_argument("--voicevox-setup-timeout", type=float, default=300, help="Docker による取得・起動の上限秒数（既定300）")
    parser.add_argument("--speech-timeout", type=float, default=120, help="文ごとの音声合成の上限秒数（既定120）")
    parser.add_argument("--docker", default="docker", help="Docker の実行ファイル。自動選択でのみ利用")
    parser.add_argument("--open-jtalk", default="open_jtalk", help="Open JTalk の実行ファイル")
    parser.add_argument("--dictionary", type=Path, help="Open JTalk の UTF-8 辞書フォルダ")
    parser.add_argument("--voice", type=Path, help="Open JTalk の .htsvoice ファイル")
    parser.add_argument("--speed", type=float, default=1.05, help="読み上げ速度（0.5〜2.0、既定1.05）")
    parser.add_argument("--chrome", help="ヘッドレス Chrome の実行ファイル")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe", help="録画の場面の大きさと長さを読む ffprobe")
    parser.add_argument("--burn-captions", action="store_true", help="全文字幕を動画の下部に焼き込む（既定は切替式）")
    parser.add_argument("--font", default="sans-serif", help="焼き込み字幕のフォント名")
    args = parser.parse_args()
    try:
        number(args.speed, "--speed", 0.5, 2.0)
        number(args.voicevox_setup_timeout, "--voicevox-setup-timeout", 0.1, 3600)
        number(args.speech_timeout, "--speech-timeout", 0.1, 3600)
        if not re.fullmatch(r"[^,\r\n]+", args.font):
            raise ValueError("--font に改行やカンマは使えない")
        args.out = args.out.absolute()
        if args.out.exists():
            raise ValueError("出力先は既に存在する。別の --out を指定する: %s" % args.out)
        story = load_story(args.story.resolve(), args.speech)
        # 高さを超える字幕は、音声合成と動画化の前に見つける。
        for scene in story["scenes"]:
            for cue in scene["narration"]:
                wrap_caption(cue["text"])
        if not shutil.which(args.ffmpeg):
            raise ValueError("FFmpeg が必要（--ffmpeg で指定できる）")
        args.ffmpeg = str(Path(shutil.which(args.ffmpeg)).resolve())
        clips = [(i, s) for i, s in enumerate(story["scenes"], 1) if "video" in s]
        if clips:
            ffprobe = shutil.which(args.ffprobe) or shutil.which("ffprobe", path=str(Path(args.ffmpeg).parent))
            if not ffprobe:
                raise ValueError("録画の場面には ffprobe が必要（--ffprobe で指定できる）")
            for i, scene in clips:
                probe_clip(ffprobe, scene, i)
        else:
            print("注意: 録画の場面（video）が無い。動く機能・画面の説明なら、冒頭に機能の全体が分かる"
                  "実際の操作の録画を置く（references/video.md の「冒頭のデモ」）", flush=True)
        chrome = browser(args.chrome)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".ve-video-", dir=args.out.parent) as folder:
            work = Path(folder)
            (work / "frames").mkdir()
            for i, scene in clips:
                clip_poster(args.ffmpeg, scene, i, work)
            # 台本・画像の不備を、音声の自動切り替えや Docker の準備より先に見つける。
            for i, scene in enumerate(story["scenes"], 1):
                render_frame(chrome, scene, i, len(story["scenes"]), work)
            # 話者名（VOICEVOX のクレジット）は画面に出さず、再生ページ・解説原稿・MP4 のメタデータに残す。
            timeline, cues = choose_audio(story, args, work, prepare_voicevox, make_audio)
            write_subtitles(cues, work)
            encode(story, timeline, cues, args, work)
            write_page(story, timeline, cues, args, work)
            for path in list(work.iterdir()):
                if path.name not in {"frames", "explanation.mp4", "explanation.html", "captions.vtt", "captions.srt", "transcript.txt", "timeline.json"}:
                    shutil.rmtree(path) if path.is_dir() else path.unlink()
            os.rename(work, args.out)
        print("作成完了: %s（%.1f 秒、音声: %s）" % (args.out / "explanation.mp4", timeline[-1]["end"], args.speech))
    except (ValueError, OSError, subprocess.TimeoutExpired, wave.Error) as exc:
        print("エラー: %s" % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
