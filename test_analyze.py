#!/usr/bin/env python3
"""analyze.py 的共享转写 / 画面文字识别：时间戳校正、OCR 结果形状与真实引擎端到端。

python3 -m unittest test_analyze
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import analyze
import config

FFMPEG_READY = Path(config.FFMPEG or "").is_file()
WHISPER_READY = FFMPEG_READY and all(Path(p or "").is_file() for p in (config.WHISPER_CLI, config.WHISPER_MODEL))
VISION = bool(config.SWIFTC)
OCR_READY = VISION or Path(config.TESSERACT or "").is_file()
CAPTION_FONT = "/System/Library/Fonts/Supplemental/Arial Rounded Bold.ttf"
JAPANESE_FONT = "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc"


def token(text: str, t_dtw: int) -> dict:
    return {"text": text, "t_dtw": t_dtw}


class TranscriptTimingTests(unittest.TestCase):
    def test_dtw_preset_follows_the_model_file_name(self) -> None:
        self.assertEqual(analyze.dtw_preset("/models/ggml-base.bin"), "base")
        self.assertEqual(analyze.dtw_preset("ggml-medium.en.bin"), "medium.en")
        self.assertEqual(analyze.dtw_preset("ggml-large-v3-turbo-q5_0.bin"), "large.v3.turbo")
        self.assertIsNone(analyze.dtw_preset("ggml-large.bin"))  # 没有这个预设，不加 -dtw，转写照常
        self.assertIsNone(analyze.dtw_preset("whisper.bin"))

    def test_word_times_replace_whisper_segment_offsets(self) -> None:
        # 真实案例（Cuddler 04）：whisper 把 “Talk to any character.” 的段首放在 2.80s，实际 5.6s 才开口。
        data = {"transcription": [
            {"offsets": {"from": 2800, "to": 7160}, "text": " Talk to any character.",
             "tokens": [token("[_BEG_]", 280), token(" Talk", 574), token(" to", 590), token(" any", 608),
                        token(" character", 650), token(".", 690)]},
            {"offsets": {"from": 16000, "to": 19000}, "text": " just a little flirting",
             "tokens": [token(" just", 1660), token(" flirting", 1900)]},
            {"offsets": {"from": 20000, "to": 21000}, "text": " (music)", "tokens": [token(" (", -1)]},
        ]}
        first, clipped, no_dtw = analyze._whisper_segments(data)
        self.assertAlmostEqual(first["start"], 5.54)
        self.assertAlmostEqual(first["end"], 7.16)
        self.assertAlmostEqual(clipped["end"], 19.3)  # 段尾不能切掉最后一个词
        self.assertEqual((no_dtw["start"], no_dtw["end"]), (20.0, 21.0))


class OcrShapeTests(unittest.TestCase):
    def test_tesseract_words_merge_into_lines_and_noise_is_dropped(self) -> None:
        tsv = ("level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
               "4\t1\t2\t1\t1\t0\t21\t1631\t1050\t140\t-1\t\n"
               "5\t1\t2\t1\t1\t1\t21\t1631\t220\t122\t96\tneed\n"
               "5\t1\t2\t1\t1\t2\t260\t1640\t200\t110\t90\tmore\n"
               "5\t1\t2\t1\t1\t3\t480\t1640\t90\t100\t12\t~~\n"
               "5\t1\t3\t1\t1\t1\t400\t200\t100\t40\t80\tHUD\n")
        self.assertEqual(analyze.tsv_lines(tsv), [
            {"text": "need more", "conf": 93.0, "left": 21, "top": 1631, "width": 439, "height": 122},
            {"text": "HUD", "conf": 80.0, "left": 400, "top": 200, "width": 100, "height": 40},
        ])


@unittest.skipUnless(FFMPEG_READY and OCR_READY and Path(CAPTION_FONT).is_file(),
                     "needs ffmpeg, an OCR engine and the macOS caption font")
class OcrEndToEndTests(unittest.TestCase):
    """合成 3 秒竖版视频：第 1–2 秒有白字描边的烧录字幕，全程底部有一行日文。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="analyze-ocr-"))
        cls.video = cls.tmp / "caption.mp4"
        caption = (f"drawtext=fontfile='{CAPTION_FONT}':text='Who are you texting':fontsize=64:fontcolor=white:"
                   "borderw=4:bordercolor=black:x=(w-text_w)/2:y=h*0.84:enable='between(t,1,2.5)'")
        japanese = (f",drawtext=fontfile='{JAPANESE_FONT}':text='今すぐダウンロード':fontsize=64:fontcolor=white:"
                    "x=(w-text_w)/2:y=h*0.3") if Path(JAPANESE_FONT).is_file() else ""
        subprocess.run([config.FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i",
                        "testsrc2=s=1080x1920:r=30:d=3", "-vf", caption + japanese, "-pix_fmt", "yuv420p",
                        str(cls.video)], check=True)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_burned_caption_is_read_at_its_timestamp(self) -> None:
        frames = analyze.ocr_video(self.video, self.tmp, 1.0)
        self.assertEqual([ts for ts, _ in frames], [0.0, 1.0, 2.0])
        texts = [analyze.frame_text(lines).lower() for _, lines in frames]
        self.assertNotIn("texting", texts[0])
        self.assertIn("who are you texting", texts[1])
        self.assertFalse(list(self.tmp.glob(".ocr-*")))  # 抽出来的帧不留在 scratch 里

    @unittest.skipUnless(VISION and Path(JAPANESE_FONT).is_file(), "Japanese OCR needs Apple Vision")
    def test_japanese_is_read_only_when_asked_for(self) -> None:
        def text(language: str) -> str:
            return " ".join(analyze.frame_text(lines) for _, lines in analyze.ocr_video(self.video, self.tmp, 1.0, language))

        self.assertIn("ダウンロード", text("ja"))
        self.assertNotIn("ダウンロード", text("en"))


@unittest.skipUnless(WHISPER_READY and shutil.which("say"), "needs whisper-cli + model and macOS say")
class WhisperTimingEndToEndTests(unittest.TestCase):
    def test_segment_starts_when_the_voice_starts_not_at_zero(self) -> None:
        # Given 2 秒背景音后才开口的口播；When 转写；Then 段首落在开口前后，而不是 whisper 默认的 0 秒。
        with tempfile.TemporaryDirectory(prefix="analyze-asr-") as tmp:
            voice, wav = Path(tmp) / "voice.aiff", Path(tmp) / "speech.wav"
            subprocess.run(["say", "-o", str(voice), "Talk to any character. See where your conversation leads."],
                           check=True)
            subprocess.run([config.FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=220:d=2:sample_rate=16000",
                            "-i", str(voice), "-filter_complex",
                            "[0:a]volume=0.05[bed];[1:a]aresample=16000,aformat=channel_layouts=mono[v];"
                            "[bed][v]concat=n=2:v=0:a=1", "-ar", "16000", "-ac", "1", str(wav)], check=True)
            segments = analyze.whisper_transcribe(wav)
        self.assertTrue(segments and "character" in segments[0]["text"].lower(), segments)
        self.assertGreater(segments[0]["start"], 1.6)
        self.assertLess(segments[0]["start"], 2.3)


if __name__ == "__main__":
    unittest.main()
