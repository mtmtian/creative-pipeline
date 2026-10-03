#!/usr/bin/env python3
"""preflight.py：可用性判定规则（纯函数）与合成缺陷视频端到端。

python3 -m unittest test_preflight
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import batch_pipeline
import config
import preflight

MEDIA_LOG = """
[Parsed_blackdetect_0 @ 0x6000] black_start:0 black_end:1.2 black_duration:1.2
[Parsed_blackdetect_0 @ 0x6000] black_start:8.5 black_end:10.1 black_duration:1.6
[Parsed_freezedetect_1 @ 0x6000] lavfi.freezedetect.freeze_start: 4.0
[Parsed_freezedetect_1 @ 0x6000] lavfi.freezedetect.freeze_duration: 3.5
[Parsed_freezedetect_1 @ 0x6000] lavfi.freezedetect.freeze_end: 7.5
[Parsed_freezedetect_1 @ 0x6000] lavfi.freezedetect.freeze_start: 17.2
[silencedetect @ 0x6001] silence_start: 0
[silencedetect @ 0x6001] silence_end: 2.1 | silence_duration: 2.1
[h264 @ 0x6002] error while decoding MB 12 30, bytestream -5
[Parsed_ebur128_3 @ 0x6003] Summary:

  Integrated loudness:
    I:         -27.4 LUFS
    Threshold: -38.0 LUFS

  True peak:
    Peak:        1.6 dBFS
"""


def statuses(results: list[dict], check: str) -> list[str]:
    return [r["status"] for r in results if r["check"] == check]


def scan(**overrides) -> dict:
    base = {"black": [], "freeze": [], "silence": [], "lufs": -14.0, "true_peak": -1.5, "errors": []}
    return {**base, **overrides}


class MediaLogTests(unittest.TestCase):
    def test_parses_every_detector_and_closes_open_intervals_at_the_end(self) -> None:
        parsed = preflight.parse_media_log(MEDIA_LOG, duration=20.0)
        self.assertEqual(parsed["black"], [(0.0, 1.2), (8.5, 10.1)])
        self.assertEqual(parsed["freeze"], [(4.0, 7.5), (17.2, 20.0)])
        self.assertEqual(parsed["silence"], [(0.0, 2.1)])
        self.assertEqual((parsed["lufs"], parsed["true_peak"]), (-27.4, 1.6))
        self.assertEqual(len(parsed["errors"]), 1)

    def test_silent_track_reports_negative_infinity_peak(self) -> None:
        log = "[Parsed_ebur128_0 @ 0x1] Summary:\n  Integrated loudness:\n    I: -70.0 LUFS\n  True peak:\n    Peak: -inf dBFS\n"
        parsed = preflight.parse_media_log(log, 10.0)
        self.assertEqual(parsed["lufs"], -70.0)
        self.assertEqual(parsed["true_peak"], float("-inf"))


class JudgeMediaTests(unittest.TestCase):
    def test_parsed_log_yields_the_expected_verdicts(self) -> None:
        results = preflight.judge_media(preflight.parse_media_log(MEDIA_LOG, 20.0), 20.0, True)
        self.assertEqual(statuses(results, "decode"), ["FAIL"])
        self.assertEqual(statuses(results, "black"), ["FAIL", "WARN"])  # opening 1.2s, mid 1.6s
        self.assertEqual(statuses(results, "freeze"), ["WARN"])  # 4.0-7.5s; the final freeze is the endcard
        self.assertEqual(statuses(results, "silence"), ["WARN"])  # 2.1s silent opening
        self.assertEqual(sorted(statuses(results, "loudness")), ["WARN", "WARN"])  # too quiet, clipped

    def test_clean_media_passes_and_mentions_hot_peaks_without_warning(self) -> None:
        results = preflight.judge_media(scan(true_peak=0.0), 20.0, True)
        self.assertEqual(preflight.overall(results), "PASS")
        self.assertIn("峰值顶格", next(r["detail"] for r in results if r["check"] == "loudness"))

    def test_mostly_static_or_fully_silent_files_fail(self) -> None:
        self.assertIn("FAIL", statuses(preflight.judge_media(scan(freeze=[(0.5, 12.0)]), 20.0, True), "freeze"))
        self.assertEqual(statuses(preflight.judge_media(scan(lufs=-70.0), 20.0, True), "silence"), ["FAIL"])
        self.assertEqual(statuses(preflight.judge_media(scan(silence=[(0.0, 19.8)]), 20.0, True), "silence"),
                         ["FAIL"])

    def test_short_black_or_freeze_and_endcard_stillness_are_fine(self) -> None:
        results = preflight.judge_media(scan(black=[(0.0, 0.3), (12.0, 12.6)], freeze=[(16.5, 20.0)]), 20.0, True)
        self.assertEqual(preflight.overall(results), "PASS")

    def test_no_audio_leaves_sound_checks_to_the_spec_check(self) -> None:
        results = preflight.judge_media(scan(), 20.0, False)
        self.assertFalse([r for r in results if r["check"] in {"silence", "loudness"}])


class SpecAndDurationTests(unittest.TestCase):
    def probe(self, **video) -> dict:
        base = {"codec_type": "video", "codec_name": "h264", "pix_fmt": "yuv420p", "width": 1920, "height": 1080,
                "sample_aspect_ratio": "1:1", "avg_frame_rate": "30/1", "r_frame_rate": "30/1"}
        audio = {"codec_type": "audio", "codec_name": "aac", "sample_rate": "48000", "channels": 2,
                 "channel_layout": "stereo"}
        return {"streams": [{**base, **video}, audio], "format": {"duration": "20"}}

    def test_encoding_is_recorded_not_judged(self) -> None:
        hevc = self.probe(codec_name="hevc", avg_frame_rate="6190080/207191")
        hevc["streams"][1]["sample_rate"] = "44100"
        verdict = preflight.judge_platform(hevc, 40_000_000)[0]
        self.assertEqual(verdict["status"], "PASS")
        self.assertIn("hevc", verdict["detail"])
        self.assertIn("44100Hz", verdict["detail"])
        self.assertEqual(preflight.judge_platform(self.probe(width=1280, height=720), 1)[0]["status"], "PASS")

    def test_only_what_platforms_refuse_fails_and_odd_aspect_warns(self) -> None:
        self.assertEqual(preflight.judge_platform(self.probe(width=1920, height=1200), 1)[0]["status"], "WARN")
        self.assertEqual(preflight.judge_platform(self.probe(), preflight.VIDEO_MAX_BYTES + 1)[0]["status"], "FAIL")
        audio_only = {"streams": [self.probe()["streams"][1]], "format": {"duration": "20"}}
        self.assertEqual(preflight.judge_platform(audio_only, 1)[0]["status"], "FAIL")

    def test_duration_and_av_sync(self) -> None:
        info = {"duration": 20.0, "video_duration": 20.0, "audio_duration": 20.0}
        self.assertEqual(preflight.overall(preflight.judge_duration(info, "google")), "PASS")
        self.assertEqual(statuses(preflight.judge_duration({**info, "duration": 4.0}, None), "duration"), ["FAIL"])
        self.assertEqual(statuses(preflight.judge_duration({**info, "duration": 8.0}, "google"), "duration"), ["WARN"])
        self.assertEqual(statuses(preflight.judge_duration({**info, "duration": 70.0}, "tiktok"), "duration"), ["WARN"])
        self.assertEqual(statuses(preflight.judge_duration({**info, "audio_duration": 18.0}, None), "av_sync"), ["FAIL"])
        self.assertEqual(statuses(preflight.judge_duration({**info, "audio_duration": 19.2}, None), "av_sync"), ["WARN"])


class SpeechTests(unittest.TestCase):
    SPEECH = [{"start": 0.0, "end": 3.0, "text": "you just need someone to play with tonight"}]

    def test_competitor_names_in_speech_fail_only_for_products_with_a_list(self) -> None:
        segments = [{"start": 12.3, "end": 14.0, "text": "Download Polybus now, honey"}]
        mobile = config.get_product_profile("hakko-mobile")["competitor_brands"]
        result = preflight.judge_speech(segments, "en", "US", mobile)
        self.assertEqual(statuses(result, "speech_brand"), ["FAIL"])
        self.assertIn("12.3s", result[0]["detail"])
        pc = config.get_product_profile("hakko-pc")["competitor_brands"]
        skipped = preflight.judge_speech(segments, "en", "US", pc)
        self.assertEqual(statuses(skipped, "speech_brand"), ["PASS"])
        self.assertIn("未扫描", skipped[0]["detail"])

    def test_language_must_match_the_locale(self) -> None:
        self.assertEqual(statuses(preflight.judge_speech(self.SPEECH, "en", "US", []), "speech_language"), ["PASS"])
        self.assertEqual(statuses(preflight.judge_speech(self.SPEECH, "ja", "US", []), "speech_language"), ["FAIL"])
        self.assertEqual(statuses(preflight.judge_speech(self.SPEECH, "en", "JP", []), "speech_language"), ["FAIL"])
        self.assertEqual(statuses(preflight.judge_speech(self.SPEECH, "en", None, []), "speech_language"), ["PASS"])
        self.assertEqual(statuses(preflight.judge_speech(self.SPEECH, "en", "IT", []), "speech_language"), ["WARN"])

    def test_music_only_is_not_judged_by_language(self) -> None:
        self.assertEqual(statuses(preflight.judge_speech([{"start": 0, "end": 1, "text": "(music)"}], "ja", "US", []),
                                  "speech_language"), ["PASS"])


def line(text, left, width=600, top=900, height=60, conf=100.0) -> dict:
    return {"text": text, "conf": conf, "left": left, "top": top, "width": width, "height": height}


class ScreenTextTests(unittest.TestCase):
    W, H, D = 1080, 1920, 20.0

    def frame(self, ts, lines):
        return ts, lines

    def test_caption_line_cut_at_the_edge_warns(self) -> None:
        cut = [line("the idea behind", 10)]
        frames = [self.frame(1, cut), self.frame(2, cut), self.frame(19, [line("Hakko", 400, width=200)])]
        results = preflight.judge_ocr(frames, self.W, self.H, self.D, ["hakko"], [])
        self.assertEqual(statuses(results, "text_edge"), ["WARN"])
        self.assertEqual(statuses(results, "endcard"), ["PASS"])

    def test_single_labels_or_small_hud_text_at_the_edge_are_ignored(self) -> None:
        label = line("RGR29", 15, width=150, height=80)
        hud = line("mahdi spotted enemy", 900, width=180, height=20)
        corner = line("League of Legends", 20, width=231)  # 字幕大小的角落标签，但不跨中线
        frames = [self.frame(t, [label, hud, corner]) for t in range(5)]
        self.assertEqual(statuses(preflight.judge_ocr(frames, self.W, self.H, self.D, [], []), "text_edge"), ["PASS"])

    def test_japanese_caption_without_spaces_counts_as_a_caption_line(self) -> None:
        cut = [line("誰もが自分の物語の主人公", 20, width=900)]
        frames = [self.frame(1, cut), self.frame(2, cut)]
        self.assertEqual(statuses(preflight.judge_ocr(frames, self.W, self.H, self.D, [], []), "text_edge"), ["WARN"])

    def test_competitor_name_on_screen_fails_and_missing_endcard_warns(self) -> None:
        frames = [self.frame(3, [line("Try PolyBuzz today", 300)])]
        results = preflight.judge_ocr(frames, self.W, self.H, self.D, ["hakko", "download"], config.BRANDS)
        self.assertEqual(statuses(results, "screen_brand"), ["FAIL"])
        self.assertEqual(statuses(results, "endcard"), ["WARN"])
        pc = preflight.judge_ocr(frames, self.W, self.H, self.D, [], [])
        self.assertEqual(statuses(pc, "screen_brand"), ["PASS"])

    def test_multi_word_endcard_tokens_are_recognised(self) -> None:
        frames = [self.frame(18, [line("Download on the", 200), line("App Store", 250, conf=50.0)]),
                  self.frame(19, [line("Google Play", 250, conf=30.0)])]
        results = preflight.judge_ocr(frames, self.W, self.H, self.D, ["app store", "google play"], [])
        self.assertEqual(results[-1]["detail"], "结尾识别到 app store")  # conf 30 的行当噪声


class MetadataTests(unittest.TestCase):
    def test_locale_comes_from_creative_id_then_folder_then_flag(self) -> None:
        cid = "hakkopc_20261004-test_jp_16x9_anytime_v1.mp4"
        self.assertEqual(preflight.infer_locale(Path("/x/16x9/US") / cid, "US"), "JP")
        self.assertEqual(preflight.infer_locale(Path("/x/16x9/ES/legacy.mp4"), "US"), "ES")
        self.assertEqual(preflight.infer_locale(Path("/x/9.28/01.mp4"), "US"), "US")
        self.assertIsNone(preflight.infer_locale(Path("/x/9.28/01.mp4"), None))

    def test_format_inference_and_sheet_layout(self) -> None:
        self.assertEqual(preflight.infer_format(1080, 1350), "4:5")
        self.assertEqual(preflight.infer_format(1080, 1352), "4:5")  # within 1%
        self.assertIsNone(preflight.infer_format(1920, 1200))
        times = preflight.sheet_times(20.0)
        self.assertEqual(len(times), 18)
        self.assertEqual(times[:6], [0.0, 0.5, 1.0, 1.5, 2.0, 2.5])
        self.assertTrue(all(16.9 <= t <= 19.95 for t in times[12:]))
        self.assertTrue(all(0 <= t <= 2.95 for t in preflight.sheet_times(3.0)))


TOOLS = all(Path(p or "").is_file() for p in (config.FFMPEG, config.FFPROBE, config.WHISPER_CLI, config.WHISPER_MODEL))
OCR_READY = bool(config.SWIFTC) or Path(config.TESSERACT or "").is_file()


@unittest.skipUnless(TOOLS and OCR_READY, "needs ffmpeg, whisper-cli + model and Apple Vision or tesseract")
class EndToEndTests(unittest.TestCase):
    """Synthetic clips with one planted defect each; the defect must be the verdict."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="preflight-e2e-"))
        enc = ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "30", "-c:a", "aac", "-ac", "2"]

        def make(name, video, audio, extra=(), rate="48000"):
            subprocess.run([config.FFMPEG, "-v", "error", "-y", *video, *audio, *extra, *enc, "-ar", rate,
                            "-t", "12", str(cls.tmp / name)], check=True)

        tone = ["-f", "lavfi", "-i", "sine=f=440:d=12:sample_rate=48000"]
        bars = ["-f", "lavfi", "-i", "testsrc2=s=1920x1080:r=30:d=12"]
        make("good.mp4", bars, tone)
        make("black_open.mp4", ["-f", "lavfi", "-i", "color=c=black:s=1920x1080:r=30:d=1.5",
                                "-f", "lavfi", "-i", "testsrc2=s=1920x1080:r=30:d=10.5"], tone,
             ["-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]", "-map", "[v]", "-map", "2:a"])
        make("silent.mp4", bars, ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"])
        make("cd_audio.mp4", bars, tone, rate="44100")
        cls.report = preflight.run_preflight([cls.tmp], "hakko-pc", "US", "google", cls.tmp / "report")
        cls.by_name = {e["name"]: e for e in cls.report["videos"]}

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def failing_checks(self, name: str) -> set[str]:
        return {r["check"] for r in self.by_name[name]["results"] if r["status"] == "FAIL"}

    def test_each_planted_defect_is_the_failure(self) -> None:
        self.assertEqual(self.failing_checks("good.mp4"), set())
        self.assertEqual(self.failing_checks("black_open.mp4"), {"black"})
        self.assertEqual(self.failing_checks("silent.mp4"), {"silence"})
        self.assertEqual(self.failing_checks("cd_audio.mp4"), set())  # 44.1kHz still plays on every platform
        self.assertEqual(self.report["counts"]["FAIL"], 2)

    def test_report_files_and_review_sheets_exist(self) -> None:
        out = self.tmp / "report"
        self.assertTrue((out / "report.html").is_file())
        data = json.loads((out / "report.json").read_text())
        self.assertEqual(len(data["videos"]), 4)
        for entry in data["videos"]:
            self.assertTrue((out / entry["sheet"]).stat().st_size > 10_000, entry["name"])
        self.assertFalse(list(out.glob(".preflight-*")))

    def test_normalize_brings_new_output_to_the_unified_spec(self) -> None:
        fixed = self.tmp / "fixed" / "cd_audio_fixed.mp4"
        fixed.parent.mkdir()
        subprocess.run(batch_pipeline.build_normalize_command(self.tmp / "cd_audio.mp4", fixed),
                       check=True, capture_output=True)
        probe = json.loads(subprocess.run([config.FFPROBE, "-v", "error", "-print_format", "json", "-show_streams",
                                           str(fixed)], capture_output=True, text=True, check=True).stdout)
        self.assertEqual(batch_pipeline.check_media_spec(probe, "16:9"), [])
        original = json.loads(subprocess.run([config.FFPROBE, "-v", "error", "-print_format", "json", "-show_streams",
                                              str(self.tmp / "cd_audio.mp4")], capture_output=True, text=True,
                                             check=True).stdout)
        self.assertTrue(batch_pipeline.check_media_spec(original, "16:9"))  # the batch gate still unifies new output


if __name__ == "__main__":
    unittest.main()
