#!/usr/bin/env python3
"""動画の音声・字幕・場面の同期と、失敗時に成果物を残さないことを確かめる。"""

from array import array
import http.server
import importlib.util
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.parse
import wave


HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / "scripts/make-video.py"
spec = importlib.util.spec_from_file_location("video", SCRIPT)
video = importlib.util.module_from_spec(spec)
spec.loader.exec_module(video)


def tone(path, duration):
    with wave.open(str(path), "wb") as out:
        out.setparams((1, 2, video.RATE, 0, "NONE", "not compressed"))
        out.writeframes(array("h", (round(8000 * math.sin(2 * math.pi * 440 * i / video.RATE))
                                  for i in range(round(duration * video.RATE)))).tobytes())


class StoryFixture:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.image = self.root / "図 ' 日本語.svg"
        self.image.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="400" height="180">'
                              '<rect width="400" height="180" fill="#abefcd"/></svg>', encoding="utf-8")
        self.story = {"title": "動画の試験", "scenes": [
            {"title": "最初の場面", "image": self.image.name, "telop": "短い要点", "narration": [
                {"text": "最初の解説です。", "seconds": 0.5}]},
            {"title": "次の場面", "image": self.image.name, "telop": "次の要点", "narration": [
                {"text": "次の解説です。", "seconds": 0.8}]}]}
        self.path = self.root / "story.json"

    def save(self):
        self.path.write_text(json.dumps(self.story, ensure_ascii=False), encoding="utf-8")

    def fake_voicevox(self, seconds, requests):
        """話者「試験話者」が、どの文も同じ長さの音で読む VOICEVOX エンジンの代役を起動し、URL を返す。"""
        audio = self.root / "voice.wav"
        tone(audio, seconds)
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                body = json.dumps([{"name": "試験話者", "styles": [{"id": 11, "name": "ノーマル"}]}]).encode()
                self.send_response(200); self.end_headers(); self.wfile.write(body)

            def do_POST(self):
                url = urllib.parse.urlsplit(self.path)
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                requests.append((url.path, urllib.parse.parse_qs(url.query), json.loads(body) if body else None))
                self.send_response(200); self.end_headers()
                self.wfile.write(audio.read_bytes() if url.path == "/synthesis" else b'{"speedScale":1}')
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return "http://127.0.0.1:%d" % server.server_port


class StoryTest(StoryFixture, unittest.TestCase):
    def test_bad_inputs_fail_before_creating_output(self):
        for invalid in [[], {"title": "試験", "scenes": []},
                        {"title": "試験", "scenes": [{"title": "場面", "image": "https://example.com/x.png", "telop": "要点"}]}]:
            with self.subTest(invalid=invalid):
                self.path.write_text(json.dumps(invalid), encoding="utf-8")
                result = subprocess.run([sys.executable, "-I", str(SCRIPT), str(self.path), "--out", str(self.root / "out"), "--speech", "none"],
                                        capture_output=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.root / "out").exists())

    def test_existing_directory_is_preserved(self):
        self.save()
        result = subprocess.run([sys.executable, "-I", str(SCRIPT), str(self.path), "--out", str(self.root), "--speech", "none"], capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(self.path.read_text()), self.story)

    def test_invalid_durations_and_missing_recording(self):
        for value in [-1, 0, True, float("nan"), float("inf"), "3"]:
            self.story["scenes"][0]["seconds"] = value
            self.save()
            with self.subTest(value=value), self.assertRaises(ValueError):
                video.load_story(self.path, "none")
        del self.story["scenes"][0]["seconds"]
        self.save()
        with self.assertRaisesRegex(ValueError, "audio"):
            video.load_story(self.path, "recorded")

    def test_recording_scene_inputs_are_checked(self):
        clip = self.root / "録画.webm"
        clip.write_bytes(b"")
        base = {"title": "録画", "video": clip.name, "telop": "要点"}
        for extra, message in [({"image": self.image.name}, "どちらか一つ"), ({"video": self.image.name}, "WebM"),
                               ({"start": 3, "end": 2}, "end は start"), ({"speed": 32}, "speed"),
                               ({"crop": "100x100"}, "crop"), ({"crop": "8:8:0:0"}, "crop"), ({"crop": [1, 2, 3, 4]}, "crop")]:
            self.story["scenes"][0] = dict(base, **extra)
            self.save()
            with self.subTest(extra=extra), self.assertRaisesRegex(ValueError, message):
                video.load_story(self.path, "none")
        self.story["scenes"][0] = dict(base, crop="160:90:8:4", start=1, speed=2.5)
        self.save()
        scene = video.load_story(self.path, "none")["scenes"][0]
        self.assertEqual((scene["crop"], scene["start"], scene["speed"]), ((160, 90, 8, 4), 1.0, 2.5))
        self.assertEqual(video.speed_label(2.5), "早送り ×2.5")
        self.assertEqual(video.speed_label(0.5), "スロー ×0.5")

    def test_caption_escapes_markup_and_rejects_overlong_text(self):
        video.write_subtitles([{"start": 0.9996, "end": 2, "text": "<b>文字</b> & 矢印 -->"}], self.root)
        value = (self.root / "captions.vtt").read_text()
        self.assertIn("00:00:01.000 --> 00:00:02.000", value)
        self.assertIn("&lt;b&gt;文字&lt;/b&gt; &amp;", value)
        self.assertNotIn("<b>", value)
        with self.assertRaisesRegex(ValueError, "2 行"):
            video.wrap_caption("あ" * 63)
        video.burn_subtitles([{"start": 0, "end": 1, "text": r"{\pos(0,0)}注釈"}], self.root / "test.ass", "sans-serif")
        self.assertNotIn(r"{\pos", (self.root / "test.ass").read_text())

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg がない")
    def test_synthesis_uses_text_on_stdin_and_measures_audio(self):
        # 本物の読み上げや外部サービスの代わりに、一定の長さの WAV を出す。
        fake = self.root / "fake-tts"
        fake.write_text("#!" + sys.executable + "\nimport sys,wave\nfrom pathlib import Path\n"
                        "Path(__file__ + '.text').write_bytes(sys.stdin.buffer.read())\n"
                        "p=sys.argv[sys.argv.index('-ow')+1]\n"
                        "with wave.open(p,'wb') as w:\n w.setparams((1,2,16000,0,'NONE',''))\n w.writeframes(b'\\x01\\x01'*11200)\n"
                        "Path(sys.argv[sys.argv.index('-ot')+1]).write_text('0 1000000 xx^sil-k+a=x\\n')\n")
        fake.chmod(0o755)
        self.save()
        story = video.load_story(self.path, "open-jtalk")
        args = SimpleNamespace(speech="open-jtalk", open_jtalk=str(fake), dictionary="辞書", voice="音声", speed=1.05, ffmpeg="ffmpeg")
        timeline, cues = video.make_audio(story, args, self.root)
        self.assertAlmostEqual(cues[0]["end"] - cues[0]["start"], 0.7, places=5)
        self.assertGreaterEqual(timeline[0]["duration"], video.LEAD + 0.7 + video.GAP + video.TAIL)
        self.assertEqual(Path(str(fake) + ".text").read_text().strip(), "次の解説です。")

    def test_abnormally_stretched_phonemes_are_rejected(self):
        trace = self.root / "speech.trace"
        trace.write_text("\n".join("%d %d xx^a-k+a=x" % (i * 5000000, (i+1) * 5000000) for i in range(13)))
        with self.assertRaisesRegex(ValueError, "引き延ばされ"):
            video.check_pronunciation(trace, 1.05)
        # 余白や息継ぎは、発音の引き延ばしとして扱わない。
        trace.write_text("\n".join("%d %d xx^a-sil+a=x" % (i * 5000000, (i+1) * 5000000) for i in range(13)))
        video.check_pronunciation(trace, 1.05)

    def test_voicevox_rejects_external_addresses_before_sending_text(self):
        for url in ["https://example.com", "http://192.168.1.2:50021", "http://user:pass@127.0.0.1", "http://127.0.0.1/?token=x"]:
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, "ローカル"):
                video.prepare_voicevox(SimpleNamespace(voicevox_url=url, voicevox_speaker=11))

    def test_startup_connection_reset_is_retried_as_an_unavailable_engine(self):
        with patch("urllib.request.OpenerDirector.open", side_effect=ConnectionResetError("起動途中の切断")):
            with self.assertRaises(video.VoicevoxUnavailable):
                video.prepare_voicevox(SimpleNamespace(voicevox_url="http://127.0.0.1:0", voicevox_speaker=11))

    def test_sentence_timeout_is_shared_by_query_synthesis_and_audio_decoding(self):
        args = SimpleNamespace(speech="voicevox", speech_timeout=0.1, voicevox_url="http://127.0.0.1:0",
                               voicevox_speaker=11, speed=1.05, ffmpeg="ffmpeg")
        with patch.object(video.time, "monotonic", side_effect=[100, 100, 100.04, 100.12]), \
             patch.object(video, "voicevox_request", side_effect=[b'{}', b'wave']) as request, \
             patch.object(video, "pcm_audio") as decoder:
            with self.assertRaisesRegex(video.SpeechError, "時間切れ"):
                video.synthesize({"text": "解説"}, args, self.root / "voice.wav")
        self.assertAlmostEqual(request.call_args_list[0].kwargs["timeout"], 0.1)
        self.assertAlmostEqual(request.call_args_list[1].kwargs["timeout"], 0.06)
        decoder.assert_not_called()

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg がない")
    def test_voicevox_uses_local_synthesis_reading_and_credit(self):
        requests = []
        args = SimpleNamespace(speech="voicevox", voicevox_url=self.fake_voicevox(0.9, requests),
                               voicevox_speaker=11, speed=1.05, ffmpeg="ffmpeg")
        video.prepare_voicevox(args)
        self.assertEqual(args.speech_credit, "VOICEVOX:試験話者")
        self.story["scenes"][0]["narration"][0]["reading"] = "さいしょの、かいせつです。"
        self.save()
        timeline, cues = video.make_audio(video.load_story(self.path, "voicevox"), args, self.root)
        self.assertAlmostEqual(cues[0]["end"] - cues[0]["start"], 0.9)
        self.assertEqual(cues[0]["text"], "最初の解説です。")
        self.assertEqual(requests[0][1]["text"], ["さいしょの、かいせつです。"])
        self.assertEqual(requests[1][2]["speedScale"], 1.05)


class RenderTest(StoryFixture, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            raise unittest.SkipTest("FFmpeg・ffprobe がない")
        try:
            video.browser(None)
        except ValueError:
            raise unittest.SkipTest("ヘッドレス Chrome がない")

    def generate(self, speech, burn=False, extra=()):
        self.save()
        out = self.root / "再生 ' フォルダ"
        argv = [sys.executable, "-I", str(SCRIPT), str(self.path), "--out", str(out), "--speech", speech]
        if burn:
            argv.append("--burn-captions")
        argv.extend(extra)
        result = subprocess.run(argv, capture_output=True, timeout=180)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.stdout = result.stdout.decode()
        data = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(out / "explanation.mp4")]))
        return out, data, json.loads((out / "timeline.json").read_text())

    def make_clip(self, seconds=4):
        # 秒数の表示と流れる帯が動く、録画の代わりの映像。
        clip = self.root / "録画 ' 画面.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=30:duration=%d" % seconds,
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip)], check=True)
        return clip

    def frame(self, movie, at):
        """録画を置く枠の中の画面を、明るさだけの画素の並びで取り出す。"""
        left, top, width, height = video.CLIP_BOX
        return subprocess.check_output(["ffmpeg", "-v", "error", "-ss", str(at), "-i", str(movie), "-frames:v", "1",
            "-vf", "crop=%d:%d:%d:%d,format=gray" % (width, height, left, top), "-f", "rawvideo", "-"])

    def changed_pixels(self, first, second):
        return sum(abs(a - b) > 40 for a, b in zip(first, second))

    def test_recording_scene_is_sped_up_cropped_and_held_until_narration_ends(self):
        clip = self.make_clip()
        for i, duration in enumerate([3.0, 0.5]):
            audio = self.root / ("解説 %d.wav" % i)
            tone(audio, duration)
            self.story["scenes"][i]["narration"][0]["audio"] = audio.name
        self.story["scenes"][0] = dict(self.story["scenes"][0], video=clip.name, start=0.5, end=3.5, speed=2, crop="160:180:160:0")
        del self.story["scenes"][0]["image"]
        out, data, timing = self.generate("recorded")
        self.assertNotIn("録画の場面（video）が無い", self.stdout)
        self.assertEqual(timing["scenes"][0]["video"], {"source": clip.name, "start": 0.5, "end": 3.5, "speed": 2.0,
                                                         "crop": "160:180:160:0", "seconds": 1.5})
        self.assertNotIn("video", timing["scenes"][1])
        # 1.5 秒の録画より解説（3 秒）が長いので、場面は解説が終わるまで続く。
        self.assertGreaterEqual(timing["scenes"][0]["duration"], video.LEAD + 3.0 + video.GAP + video.TAIL)
        self.assertAlmostEqual(float(data["format"]["duration"]), timing["duration"], delta=0.08)
        self.assertIn("audio", {s["codec_type"] for s in data["streams"]})
        movie = out / "explanation.mp4"
        first, second, held, later = (self.frame(movie, at) for at in (0.2, 1.3, 2.2, 3.6))
        self.assertGreater(self.changed_pixels(first, second), 200)  # 録画が動いている
        # 録画が終わった後も、黒い画面ではなく録画の最後の画面を止めて見せる。
        self.assertLess(self.changed_pixels(held, later), 20)
        self.assertGreater(sum(v > 60 for v in later), len(later) // 5)
        page = (out / "explanation.html").read_text()
        self.assertIn("場面 01 は実際の画面の録画", page)
        self.assertIn("実際の画面の録画（早送り ×2）", page)
        self.assertIn("映像: 実際の画面の録画（早送り ×2）", (out / "transcript.txt").read_text())
        self.assertFalse(list(out.glob("poster-*")))

    def test_recording_scene_outside_the_clip_fails_before_output(self):
        clip = self.make_clip(2)
        for extra, message in [({"crop": "400:100:0:0"}, "はみ出す"), ({"end": 9}, "収まらない"), ({"start": 5}, "収まらない")]:
            self.story["scenes"][0] = dict({"title": "録画", "video": clip.name, "telop": "要点"}, **extra)
            self.save()
            out = self.root / "failed"
            result = subprocess.run([sys.executable, "-I", str(SCRIPT), str(self.path), "--out", str(out), "--speech", "none"],
                                    capture_output=True, timeout=60)
            with self.subTest(extra=extra):
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr.decode())
                self.assertFalse(out.exists())

    def test_recorded_audio_is_not_cut_and_captions_follow_actual_durations(self):
        for i, duration in enumerate([0.7, 1.3]):
            audio = self.root / ("音声 %d.wav" % i)
            tone(audio, duration)
            self.story["scenes"][i]["narration"][0]["audio"] = audio.name
        out, data, timing = self.generate("recorded")
        streams = {s["codec_type"]: s for s in data["streams"]}
        self.assertEqual(streams["video"]["codec_name"], "h264")
        self.assertEqual((streams["video"]["width"], streams["video"]["height"]), (1280, 720))
        self.assertEqual(streams["audio"]["codec_name"], "aac")
        self.assertEqual(streams["subtitle"]["codec_name"], "mov_text")
        self.assertAlmostEqual(float(data["format"]["duration"]), timing["duration"], delta=0.08)
        for cue, duration in zip(timing["cues"], [0.7, 1.3]):
            self.assertAlmostEqual(cue["end"] - cue["start"], duration, places=5)
        decoded = self.root / "decoded.wav"
        subprocess.run(["ffmpeg", "-v", "error", "-i", str(out / "explanation.mp4"), "-ac", "1", "-ar", str(video.RATE), str(decoded)], check=True)
        with wave.open(str(decoded), "rb") as wav:
            for cue in timing["cues"]:
                # 文の最後まで音が残り、場面の境界で途切れていないことを確かめる。
                wav.setpos(round((cue["end"] - 0.1) * video.RATE))
                samples = array("h", wav.readframes(round(0.05 * video.RATE)))
                rms = math.sqrt(sum(x*x for x in samples) / len(samples))
                self.assertGreater(rms, 1000)
        self.assertIn('<track kind="captions"', (out / "explanation.html").read_text())
        self.assertEqual(len(list((out / "frames").glob("*.png"))), 2)
        self.assertFalse(list(out.glob("*.wav")))

    def test_auto_without_speech_environment_outputs_video_and_selection_reasons(self):
        out, data, timing = self.generate("auto", extra=("--docker", "/nonexistent/docker",
            "--open-jtalk", "/nonexistent/open_jtalk", "--voicevox-url", "http://127.0.0.1:0"))
        self.assertNotIn("audio", {s["codec_type"] for s in data["streams"]})
        self.assertEqual((timing["requested_speech"], timing["speech"]), ("auto", "none"))
        failures = [e for e in timing["speech_events"] if e["status"] == "failed"]
        self.assertEqual([e["method"] for e in failures], ["voicevox", "open-jtalk"])
        page = (out / "explanation.html").read_text()
        self.assertIn("Docker が見つからない", page)
        self.assertIn("音声はありません", page)
        self.assertEqual(len(timing["cues"]), 2)

    def test_voicevox_credit_is_kept_off_the_frames(self):
        # 話者名は画面に描かず、再生ページ・解説原稿・MP4 のメタデータにクレジットとして残す。
        silent, _, _ = self.generate("none")
        silent.rename(self.root / "silent")
        out, data, timing = self.generate("voicevox", extra=("--voicevox-url", self.fake_voicevox(0.4, [])))
        self.assertEqual(timing["speech"], "voicevox")
        for i in (1, 2):
            frame = "frames/%03d.png" % i
            self.assertEqual((out / frame).read_bytes(), (self.root / "silent" / frame).read_bytes(), frame)
        self.assertIn("VOICEVOX:試験話者", (out / "explanation.html").read_text())
        self.assertIn("VOICEVOX:試験話者", (out / "transcript.txt").read_text())
        self.assertIn("VOICEVOX:試験話者", data["format"]["tags"]["comment"])

    def test_auto_preserves_complete_recordings_without_preparing_synthesis(self):
        for i, scene in enumerate(self.story["scenes"]):
            audio = self.root / ("提供音声%d.wav" % i)
            tone(audio, 0.3)
            scene["narration"][0]["audio"] = audio.name
        out, data, timing = self.generate("auto", extra=("--docker", "/nonexistent/docker",
            "--open-jtalk", "/nonexistent/open_jtalk", "--voicevox-url", "http://127.0.0.1:0"))
        self.assertIn("audio", {s["codec_type"] for s in data["streams"]})
        self.assertEqual((timing["requested_speech"], timing["speech"]), ("auto", "recorded"))
        self.assertEqual(len(timing["speech_events"]), 1)

    def test_silent_burned_video_has_no_audio_or_duplicate_caption_track(self):
        out, data, timing = self.generate("none", burn=True)
        self.assertIn("冒頭に機能の全体が分かる", self.stdout)
        self.assertEqual({s["codec_type"] for s in data["streams"]}, {"video"})
        self.assertNotIn("<track", (out / "explanation.html").read_text())
        self.assertIn("音声はありません", (out / "explanation.html").read_text())
        self.assertGreater(timing["cues"][1]["start"], timing["scenes"][0]["end"])

    def test_broken_image_fails_without_publishing_partial_output(self):
        self.image.write_text("画像ではない")
        self.save()
        out = self.root / "failed"
        result = subprocess.run([sys.executable, "-I", str(SCRIPT), str(self.path), "--out", str(out),
            "--docker", "/nonexistent/docker", "--open-jtalk", "/nonexistent/open_jtalk",
            "--voicevox-url", "http://127.0.0.1:0"], capture_output=True, timeout=90)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("図を読み込めない", result.stderr.decode())
        self.assertFalse(out.exists())
        self.assertFalse(list(self.root.glob(".ve-video-*")))


class AutoSpeechTest(StoryFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.save()
        self.work = self.root / "work"
        self.work.mkdir()
        self.docker = self.root / "docker"
        self.docker.write_text("#!" + sys.executable + "\n" + (HERE / "fixtures/fake_video_docker.py").read_text())
        self.docker.chmod(0o755)
        self.tts = self.root / "open_jtalk"
        self.tts.write_text("#!/bin/sh\nexit 0\n")
        self.tts.chmod(0o755)
        self.voice = self.root / "voice.htsvoice"
        self.voice.touch()
        self.env = patch.dict("os.environ", {"VISUAL_EXPLAIN_FAKE_DOCKER": str(self.root), "DOCKER_HOST": "", "DOCKER_CONTEXT": ""})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.set_state()
        self.args = SimpleNamespace(speech="auto", docker=str(self.docker), open_jtalk=str(self.tts),
            dictionary=self.root, voice=self.voice, voicevox_setup_timeout=10, speech_timeout=2,
            voicevox_url="http://127.0.0.1:0", voicevox_speaker=11, speed=1.05)
        self.existing, self.never_ready = False, False
        self.synthesis_failure, self.failure_at = None, 2
        self.calls = []

    def set_state(self, image=True, failure=None, endpoint=None):
        state = {"image": image, "failure": failure, "containers": {"existing-user-container": {"owner": "user"}}}
        if endpoint:
            state["endpoint"] = endpoint
        (self.root / "state.json").write_text(json.dumps(state))

    def state(self):
        return json.loads((self.root / "state.json").read_text())

    def docker_calls(self):
        path = self.root / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def prepare(self, args, timeout=120):
        if self.never_ready or (not self.existing and args.voicevox_url != "http://127.0.0.1:50599"):
            raise video.VoicevoxUnavailable("エンジンに接続できない")
        args.speech_credit = "VOICEVOX:試験話者"
        args.speech_label = "合成音声（VOICEVOX:試験話者）"

    def synthesize(self, cue, args, source):
        self.calls.append(args.speech)
        if args.speech == self.synthesis_failure and self.calls.count(args.speech) >= self.failure_at:
            raise video.SpeechError("発音の検査に失敗した")
        value = 1000 if args.speech == "voicevox" else 2000
        return array("h", [value] * round(0.4 * video.RATE)).tobytes()

    def choose(self):
        story = video.load_story(self.path, self.args.speech)
        with patch.object(video, "synthesize", self.synthesize):
            return video.choose_audio(story, self.args, self.work, self.prepare, video.make_audio)

    def assert_cleaned(self):
        self.assertEqual(self.state()["containers"], {"existing-user-container": {"owner": "user"}})
        for call in self.docker_calls():
            if call[0] == "rm":
                self.assertTrue(call[-1].startswith("visual-explain-video-"))

    def test_existing_engine_never_touches_docker_or_stops_other_services(self):
        self.existing = True
        self.choose()
        self.assertEqual(self.args.speech, "voicevox")
        self.assertEqual(self.docker_calls(), [])
        self.assert_cleaned()

    def test_docker_downloads_only_missing_image_and_cleans_own_container(self):
        for image in (True, False):
            with self.subTest(image=image):
                self.set_state(image=image)
                (self.root / "calls.jsonl").unlink(missing_ok=True)
                self.args.voicevox_url = "http://127.0.0.1:0"
                self.args.speech = "auto"
                self.choose()
                self.assertEqual(self.args.speech, "voicevox")
                self.assertEqual(any(c[0] == "pull" for c in self.docker_calls()), not image)
                create = next(c for c in self.docker_calls() if c[0] == "create")
                self.assertIn("127.0.0.1::50021", create)
                self.assertTrue(self.state()["image"])
                self.assert_cleaned()

    def test_unavailable_docker_or_start_failure_uses_open_jtalk(self):
        for failure in ("daemon", "pull", "start"):
            with self.subTest(failure=failure):
                self.set_state(image=failure != "pull", failure=failure)
                self.args.speech = "auto"
                self.args.voicevox_url = "http://127.0.0.1:0"
                self.choose()
                self.assertEqual(self.args.speech, "open-jtalk")
                self.assert_cleaned()

    def test_pull_timeout_and_startup_timeout_are_bounded_and_cleaned(self):
        for failure in ("pull-timeout", "startup-timeout"):
            with self.subTest(failure=failure):
                self.set_state(image=failure != "pull-timeout", failure=failure)
                self.args.speech = "auto"
                self.args.voicevox_url = "http://127.0.0.1:0"
                self.args.voicevox_setup_timeout = 1
                self.never_ready = failure == "startup-timeout"
                start = time.monotonic()
                self.choose()
                self.assertLess(time.monotonic() - start, 5)
                self.assertEqual(self.args.speech, "open-jtalk")
                self.assertTrue(any("時間切れ" in e["reason"] for e in self.args.speech_events))
                self.assert_cleaned()

    def test_partial_synthesis_failure_regenerates_all_scenes_without_mixing_voices(self):
        self.synthesis_failure = "voicevox"
        self.choose()
        self.assertEqual(self.calls, ["voicevox", "voicevox", "open-jtalk", "open-jtalk"])
        self.assertEqual(self.args.speech_credit, "")
        for i in (1, 2):
            with wave.open(str(self.work / ("audio-%03d.wav" % i))) as audio:
                audio.setpos(round((video.LEAD + 0.05) * video.RATE))
                self.assertEqual(array("h", audio.readframes(10)).tolist(), [2000] * 10)
        self.assertEqual(len(list(self.work.iterdir())), 2)
        self.assert_cleaned()

    def test_all_speech_unavailable_or_failed_quality_check_uses_silent_captions(self):
        for missing in (True, False):
            with self.subTest(missing=missing):
                self.args.speech = "auto"
                self.args.docker = "/nonexistent/docker"
                self.args.open_jtalk = "/nonexistent/open_jtalk" if missing else str(self.tts)
                self.synthesis_failure, self.failure_at = "open-jtalk", 1
                _, cues = self.choose()
                self.assertEqual(self.args.speech, "none")
                self.assertEqual(self.args.speech_credit, "")
                self.assertAlmostEqual(cues[0]["end"] - cues[0]["start"], 0.5)
                with wave.open(str(self.work / "audio-001.wav")) as audio:
                    self.assertEqual(set(audio.readframes(audio.getnframes())), {0})

    def test_remote_docker_is_not_used_and_foreign_container_is_not_removed(self):
        for failure in ("remote", "owner", "cleanup"):
            with self.subTest(failure=failure):
                self.set_state(failure=failure, endpoint="ssh://other-host" if failure == "remote" else None)
                self.args.speech, self.args.voicevox_url = "auto", "http://127.0.0.1:0"
                (self.root / "calls.jsonl").unlink(missing_ok=True)
                self.choose()
                if failure == "remote":
                    self.assertEqual(self.args.speech, "open-jtalk")
                    self.assertFalse(any(c[0] == "create" for c in self.docker_calls()))
                else:
                    self.assertTrue(any(e["status"] == "cleanup-failed" for e in self.args.speech_events))
                if failure == "owner":
                    self.assertFalse(any(c[0] == "rm" for c in self.docker_calls()))

    def test_explicit_silent_and_strict_modes_do_not_start_docker(self):
        self.args.speech = "none"
        self.choose()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.docker_calls(), [])
        self.args.speech = "voicevox"
        with self.assertRaises(video.VoicevoxUnavailable):
            self.choose()
        self.assertEqual(self.docker_calls(), [])

    def test_story_without_narration_does_not_prepare_audio_environment(self):
        for scene in self.story["scenes"]:
            scene["narration"] = []
        self.story["scenes"][0]["telop"] = "字幕が無い場面でも要点を読むための時間を確保する"
        self.save()
        timeline, cues = self.choose()
        self.assertEqual(self.args.speech, "none")
        self.assertEqual(cues, [])
        self.assertGreaterEqual(timeline[0]["duration"], len(self.story["scenes"][0]["telop"]) / 6)
        self.assertEqual(self.docker_calls(), [])

    def test_partial_recordings_are_not_silently_discarded(self):
        self.story["scenes"][0]["narration"][0]["audio"] = self.voice.name
        self.save()
        with self.assertRaisesRegex(ValueError, "全ての文に audio"):
            self.choose()
        self.assertEqual(self.docker_calls(), [])


if __name__ == "__main__":
    unittest.main()
