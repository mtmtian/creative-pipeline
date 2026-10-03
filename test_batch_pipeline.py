#!/usr/bin/env python3
"""batch_pipeline.py：批次契约、素材 ID、审核门与交付 manifest。

python3 -m unittest test_batch_pipeline
"""
from __future__ import annotations

import contextlib
import io
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import batch_pipeline as pipeline
import config

PC_BATCH = "20261004-test-batch"
GOOGLE_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")  # google-ads-pc-campaign-launch

VALID_PROBE = {
    "streams": [
        {"codec_type": "video", "codec_name": "h264", "pix_fmt": "yuv420p",
         "width": 1920, "height": 1080, "sample_aspect_ratio": "1:1",
         "avg_frame_rate": "30/1", "r_frame_rate": "30/1", "tags": {"rotate": "0"}},
        {"codec_type": "audio", "codec_name": "aac", "sample_rate": "48000",
         "channels": 2, "channel_layout": "stereo"},
    ],
    "format": {"duration": "12.5"},
}


def probe_for(width: int, height: int) -> dict:
    probe = json.loads(json.dumps(VALID_PROBE))
    probe["streams"][0].update(width=width, height=height)
    return probe


def fake_prober(path: Path) -> dict:
    """Probe by the format encoded in the snapshot's bytes (test files hold their size as text)."""
    width, height = (int(v) for v in path.read_text().split("x"))
    return probe_for(width, height)


def fake_preflight(status: str = "PASS", issues: list[dict] | None = None):
    """Stand-in for preflight.run_preflight: same call shape, fixed verdict for every file."""
    def runner(paths, profile, locale, channel, out_dir):
        return {"videos": [{"file": str(p), "status": status, "results": issues or []} for p in paths]}
    return runner


class BatchCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        patcher = mock.patch.object(pipeline, "WORK_ROOT", self.root / "work")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def make_batch(self, profile: str = "hakko-pc", batch_id: str = PC_BATCH) -> Path:
        batch = self.root / "batches" / batch_id
        pipeline.initialize_batch(batch, profile)
        return batch

    def add_creative(self, batch: Path, locale: str, format_name: str, hook: str,
                     version: int = 1, profile: str = "hakko-pc") -> Path:
        value = pipeline.creative_id(profile, batch.name, locale, format_name, hook, version)
        width, height, format_dir = pipeline.FORMAT_SPECS[format_name]
        path = batch / "50_delivery" / format_dir / locale / f"{value}.mp4"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{width}x{height}")
        return path


class ProfileTests(unittest.TestCase):
    def test_batch_profiles_cover_pc_mobile_luddi_and_cuddler_stays_default(self) -> None:
        self.assertIs(config.get_product_profile(), config.PRODUCT)
        self.assertEqual(config.batch_profiles(), ["hakko-pc", "hakko-mobile", "luddi"])
        pc, mobile = config.get_product_profile("hakko-pc"), config.get_product_profile("hakko-mobile")
        luddi = config.get_product_profile("luddi")
        self.assertEqual((pc["code"], mobile["code"], luddi["code"]), ("hakkopc", "hakkomobile", "luddi"))
        self.assertTrue(pc["batch_root"].endswith("PC端/04_agent产出/生产批次"))
        self.assertTrue(mobile["batch_root"].endswith("移动端/04_agent产出/生产批次"))
        self.assertEqual(Path(luddi["batch_root"]), Path(config.LUDDI_ROOT) / "04_agent产出/生产批次")
        self.assertTrue(mobile["brief_path"].endswith("AGENTS.md"))
        self.assertNotIn("endcard_logo_path", mobile)
        self.assertNotIn("endcard_logo_path", luddi)
        self.assertEqual(mobile["default_formats"], ["9:16", "16:9"])
        self.assertEqual(luddi["default_formats"], ["9:16", "16:9"])

    def test_agent_batches_never_land_in_human_output(self) -> None:
        for name in config.batch_profiles():
            with self.subTest(profile=name):
                self.assertNotIn("人力产出", config.get_product_profile(name)["batch_root"])


class CreativeIdTests(unittest.TestCase):
    def test_round_trip_and_launch_compatibility(self) -> None:
        value = pipeline.creative_id("hakko-pc", "20260727-ai-girlfriend-hooks", "US", "16:9", "anytime", 1)
        self.assertEqual(value, "hakkopc_20260727-ai-girlfriend-hooks_us_16x9_anytime_v1")
        self.assertRegex(value, GOOGLE_RUN_ID_RE)
        parts = pipeline.parse_creative_id(value)
        self.assertEqual((parts["format"], parts["locale"], parts["hook"], parts["version"]),
                         ("16:9", "us", "anytime", 1))
        mobile = pipeline.creative_id("hakko-mobile", "20261004-call-hooks", "JP", "9:16", "late-night-call", 3)
        self.assertEqual(mobile, "hakkomobile_20261004-call-hooks_jp_9x16_late-night-call_v3")
        luddi = pipeline.creative_id("luddi", "20261004-playable-feed", "US", "9:16", "brainrot", 1)
        self.assertEqual(pipeline.parse_creative_id(luddi)["product"], "luddi")

    def test_rejects_values_that_would_break_parsing(self) -> None:
        cases = [
            dict(hook="ai_girlfriend"), dict(hook="Anytime"), dict(locale="USA"), dict(locale="us"),
            dict(batch_id="9.28"), dict(version=0), dict(hook="x" * 70), dict(format_name="21:9"),
        ]
        base = dict(profile_name="hakko-pc", batch_id=PC_BATCH, locale="US", format_name="16:9",
                    hook="anytime", version=1)
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ValueError):
                pipeline.creative_id(**{**base, **case})
        with self.assertRaises(ValueError):
            pipeline.creative_id("cuddler", PC_BATCH, "US", "16:9", "anytime", 1)

    def test_legacy_delivery_names_are_not_creative_ids(self) -> None:
        for legacy in ("hakko_new_jared_ai_girlfriend_16x9", "hakko_20260727_dota_ai_coach_1x1",
                       "hakkopc_20260727-x_us_16x10_hook_v1"):
            with self.subTest(legacy=legacy), self.assertRaises(ValueError):
                pipeline.parse_creative_id(legacy)


class BatchInitializationTests(BatchCase):
    def test_creates_convention_tree_and_snapshot(self) -> None:
        batch = self.make_batch()
        self.assertEqual(sorted(p.name for p in batch.iterdir()),
                         ["00_input", "20_plan", "50_delivery", "batch.json"])
        metadata = json.loads((batch / "batch.json").read_text())
        self.assertEqual(metadata["profile"], "hakko-pc")
        self.assertEqual(metadata["product"]["code"], "hakkopc")

    def test_rejects_batch_ids_that_cannot_enter_creative_ids(self) -> None:
        for name in ("cli-batch", "9.28", "20261004_Topic"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "batch id"):
                pipeline.initialize_batch(self.root / name, "hakko-pc")

    def test_rejects_profiles_without_batch_support(self) -> None:
        with self.assertRaisesRegex(ValueError, "不能建生产批次"):
            pipeline.initialize_batch(self.root / PC_BATCH, "cuddler")

    def test_reinitializing_preserves_metadata_and_profile(self) -> None:
        batch = self.make_batch()
        first = json.loads((batch / "batch.json").read_text())
        self.assertEqual(pipeline.initialize_batch(batch, "hakko-pc"), first)
        with self.assertRaisesRegex(ValueError, "profile does not match"):
            pipeline.initialize_batch(batch, "hakko-mobile")

    def test_real_batch_json_with_locale_policy_and_old_snapshot_validates(self) -> None:
        batch = self.root / "20260727-ai-girlfriend-hooks"
        for name in ("00_input", "20_plan", "30_source_hooks", "50_delivery"):
            (batch / name).mkdir(parents=True)
        old_snapshot = {k: v for k, v in config.get_product_profile("hakko-pc").items()
                        if k not in {"code", "batch_root"}}
        (batch / "batch.json").write_text(json.dumps({
            "schema_version": 1, "batch_id": batch.name, "profile": "hakko-pc", "product": old_snapshot,
            "locale_policy": {"target_locale": "US", "strict_match": True,
                              "source_manifest": "20_plan/source-manifest.json"},
            "created_at": "2026-07-27T12:25:00.214914+00:00",
        }))
        self.assertEqual(pipeline._validate_batch(batch)["locale_policy"]["target_locale"], "US")

    def test_unknown_batch_json_fields_and_missing_plan_dir_are_rejected(self) -> None:
        batch = self.make_batch()
        metadata = json.loads((batch / "batch.json").read_text())
        (batch / "batch.json").write_text(json.dumps({**metadata, "status": "done"}))
        with self.assertRaisesRegex(ValueError, "invalid fields"):
            pipeline._validate_batch(batch)
        (batch / "batch.json").write_text(json.dumps(metadata))
        (batch / "20_plan").rmdir()
        with self.assertRaisesRegex(ValueError, "missing batch directory: 20_plan"):
            pipeline._validate_batch(batch)

    def test_rejects_symlinked_root_and_symlinked_subdirectory(self) -> None:
        real_root = self.root / "real-root"
        real_root.mkdir()
        linked = self.root / "linked-root"
        linked.symlink_to(real_root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            pipeline.initialize_batch(linked / PC_BATCH, "hakko-pc")
        self.assertFalse((real_root / PC_BATCH).exists())

        batch = self.root / "safe" / PC_BATCH
        batch.mkdir(parents=True)
        outside = self.root / "outside"
        outside.mkdir()
        (batch / "50_delivery").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            pipeline.initialize_batch(batch, "hakko-pc")
        self.assertFalse((batch / "batch.json").exists())


class ReviewTests(BatchCase):
    def test_scans_delivery_attaches_tags_and_reports_gaps(self) -> None:
        batch = self.make_batch()
        self.add_creative(batch, "US", "16:9", "anytime")
        self.add_creative(batch, "US", "1:1", "anytime")
        self.add_creative(batch, "JP", "16:9", "win-lose")
        (batch / "50_delivery/16x9/US/.DS_Store").write_text("")
        (batch / "50_delivery/00_说明").mkdir()
        (batch / "20_plan/creatives.json").write_text(json.dumps(
            {"anytime": {"selling_point": "实时陪玩", "channel": ["google"]}}))
        result = pipeline.create_review_manifest(batch)
        rows = {row["creative_id"]: row for row in result["manifest"]["candidates"]}
        self.assertEqual(len(rows), 3)
        row = rows[f"hakkopc_{PC_BATCH}_us_1x1_anytime_v1"]
        self.assertEqual((row["format"], row["locale"]), ("1:1", "US"))
        self.assertEqual(row["tags"]["channel"], ["google"])
        self.assertEqual(rows[f"hakkopc_{PC_BATCH}_jp_16x9_win-lose_v1"]["tags"], {})
        self.assertEqual(result["ignored"], ["00_说明"])
        self.assertEqual(result["untagged_hooks"], ["win-lose"])
        self.assertTrue((batch / "20_plan/review_manifest.json").is_file())

    def test_rejects_files_that_do_not_belong_to_the_contract(self) -> None:
        cases = {
            "legacy name": ("16x9/US", "hakko_new_jared_ai_girlfriend_16x9.mp4", "不是素材 ID"),
            "wrong locale dir": ("16x9/JP", f"hakkopc_{PC_BATCH}_us_16x9_anytime_v1.mp4", "locale"),
            "wrong batch": ("16x9/US", "hakkopc_20260101-other_us_16x9_anytime_v1.mp4", "batch"),
            "wrong product": ("16x9/US", f"hakkomobile_{PC_BATCH}_us_16x9_anytime_v1.mp4", "product"),
            "wrong ratio dir": ("1x1/US", f"hakkopc_{PC_BATCH}_us_16x9_anytime_v1.mp4", "ratio"),
            "not mp4": ("16x9/US", "notes.txt", ".mp4"),
            "lowercase locale dir": ("16x9/us", f"hakkopc_{PC_BATCH}_us_16x9_anytime_v1.mp4", "地区目录"),
        }
        for label, (folder, name, message) in cases.items():
            with self.subTest(label=label):
                batch = self.make_batch(batch_id=f"20261004-case-{abs(hash(label)) % 10_000}")
                fixed = name.replace(PC_BATCH, batch.name)
                target = batch / "50_delivery" / folder / fixed
                target.parent.mkdir(parents=True)
                target.write_text("1920x1080")
                with self.assertRaisesRegex(ValueError, message):
                    pipeline.create_review_manifest(batch)

    def test_rejects_symlinks_and_empty_delivery(self) -> None:
        batch = self.make_batch()
        with self.assertRaisesRegex(ValueError, "没有成片"):
            pipeline.create_review_manifest(batch)
        real = self.add_creative(batch, "US", "16:9", "anytime")
        link = real.with_name(real.name.replace("anytime", "copy"))
        link.symlink_to(real)
        with self.assertRaisesRegex(ValueError, "普通文件"):
            pipeline.create_review_manifest(batch)
        link.unlink()
        outside = self.root / "outside-format"
        outside.mkdir()
        (batch / "50_delivery/1x1").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "普通目录"):
            pipeline.create_review_manifest(batch)

    def test_new_review_clears_approval_qc_and_handoff(self) -> None:
        batch = self.make_batch()
        self.add_creative(batch, "US", "16:9", "anytime")
        pipeline.create_review_manifest(batch)
        pipeline.run_spec_qc(batch, prober=fake_prober)
        pipeline.run_batch_preflight(batch, runner=fake_preflight())
        pipeline.approve_review(batch, "reviewer")
        pipeline.deliver(batch)
        pipeline.create_review_manifest(batch)
        for relative in (pipeline.APPROVAL, pipeline.QC_REPORT, pipeline.PREFLIGHT_REPORT, pipeline.HANDOFF):
            self.assertFalse((batch / relative).exists(), relative)

    def test_bad_tags_file_is_rejected(self) -> None:
        batch = self.make_batch()
        self.add_creative(batch, "US", "16:9", "anytime")
        (batch / "20_plan/creatives.json").write_text(json.dumps({"anytime": {"channel": 3}}))
        with self.assertRaisesRegex(ValueError, "字符串"):
            pipeline.create_review_manifest(batch)


class ApprovalTests(BatchCase):
    def test_approval_is_bound_to_the_exact_manifest(self) -> None:
        batch = self.make_batch()
        self.add_creative(batch, "US", "16:9", "anytime")
        self.assertFalse(pipeline.is_approved(batch))
        pipeline.create_review_manifest(batch)
        approval = pipeline.approve_review(batch, "马天")
        self.assertTrue(pipeline.is_approved(batch))
        manifest_path = batch / pipeline.REVIEW_MANIFEST
        manifest_path.write_text(manifest_path.read_text().replace("pending_human_review", "pending_human_review "))
        self.assertFalse(pipeline.is_approved(batch))
        self.assertEqual(approval["reviewer"], "马天")

    def test_truthy_or_malformed_approvals_fail_closed(self) -> None:
        batch = self.make_batch()
        self.add_creative(batch, "US", "16:9", "anytime")
        pipeline.create_review_manifest(batch)
        good = pipeline.approve_review(batch, "reviewer")
        for bad in ({**good, "approved": "yes"}, {**good, "reviewer": " "}, {**good, "extra": 1},
                    {**good, "review_manifest_sha256": "0" * 63}):
            with self.subTest(bad=bad):
                (batch / pipeline.APPROVAL).write_text(json.dumps(bad))
                self.assertFalse(pipeline.is_approved(batch))
        with self.assertRaisesRegex(ValueError, "reviewer"):
            pipeline.approve_review(batch, "  ")


class SpecQcTests(BatchCase):
    def test_enforces_delivery_media_specs_for_every_format(self) -> None:
        self.assertEqual(pipeline.check_media_spec(VALID_PROBE, "16:9"), [])
        for format_name, (width, height, _) in pipeline.FORMAT_SPECS.items():
            with self.subTest(format_name=format_name):
                self.assertEqual(pipeline.check_media_spec(probe_for(width, height), format_name), [])
                low = probe_for(width // 2, height // 2)
                self.assertTrue(any("at least" in e for e in pipeline.check_media_spec(low, format_name)))
        self.assertTrue(any("aspect ratio" in e
                            for e in pipeline.check_media_spec(probe_for(1920, 1200), "16:9")))
        self.assertTrue(any("aspect ratio" in e
                            for e in pipeline.check_media_spec(probe_for(1080, 1920), "4:5")))
        mutations = (
            (0, "codec_name", "hevc", "video codec"), (0, "pix_fmt", "yuv444p", "pixel format"),
            (0, "avg_frame_rate", "24/1", "avg_frame_rate"), (0, "r_frame_rate", "60/1", "r_frame_rate"),
            (0, "sample_aspect_ratio", "4:3", "SAR"), (1, "codec_name", "mp3", "audio codec"),
            (1, "sample_rate", "44100", "48000"), (1, "channels", 1, "stereo"),
        )
        for index, field, value, expected in mutations:
            with self.subTest(field=field):
                probe = json.loads(json.dumps(VALID_PROBE))
                probe["streams"][index][field] = value
                self.assertTrue(any(expected in e for e in pipeline.check_media_spec(probe, "16:9")))
        rotated = json.loads(json.dumps(VALID_PROBE))
        rotated["streams"][0]["side_data_list"] = [{"rotation": 90}]
        self.assertTrue(any("rotation" in e for e in pipeline.check_media_spec(rotated, "16:9")))
        extra = json.loads(json.dumps(VALID_PROBE))
        extra["streams"].append({"codec_type": "subtitle", "codec_name": "mov_text"})
        self.assertTrue(any("exactly 1 video" in e for e in pipeline.check_media_spec(extra, "16:9")))

    def test_qc_records_media_summary_and_detects_changes_after_review(self) -> None:
        batch = self.make_batch()
        path = self.add_creative(batch, "US", "16:9", "anytime")
        self.add_creative(batch, "US", "1:1", "anytime")
        pipeline.create_review_manifest(batch)
        report = pipeline.run_spec_qc(batch, prober=fake_prober)
        self.assertTrue(report["passed"])
        row = next(r for r in report["candidates"] if r["format"] == "16:9")
        self.assertEqual((row["width"], row["height"], row["duration_sec"]), (1920, 1080, 12.5))
        path.write_text("1920x1080 ")
        report = pipeline.run_spec_qc(batch, prober=fake_prober)
        self.assertFalse(report["passed"])
        self.assertIn("candidate changed after review manifest",
                      next(r for r in report["candidates"] if r["format"] == "16:9")["errors"])
        self.assertEqual([p.name for p in (batch / "20_plan").iterdir() if p.name.startswith(".")], [])


class DeliverTests(BatchCase):
    def ready_batch(self) -> Path:
        batch = self.make_batch()
        self.add_creative(batch, "US", "16:9", "anytime")
        self.add_creative(batch, "US", "1:1", "anytime")
        (batch / "20_plan/creatives.json").write_text(json.dumps({"anytime": {"persona": "teammate"}}))
        pipeline.create_review_manifest(batch)
        return batch

    def test_blocked_until_approved_and_qc_passes_on_the_current_review(self) -> None:
        batch = self.ready_batch()
        with self.assertRaisesRegex(RuntimeError, "approved.json"):
            pipeline.deliver(batch)
        pipeline.approve_review(batch, "reviewer")
        with self.assertRaisesRegex(RuntimeError, "QC report"):
            pipeline.deliver(batch)
        bad = lambda path: probe_for(1280, 720)  # noqa: E731
        pipeline.run_spec_qc(batch, prober=bad)
        with self.assertRaisesRegex(RuntimeError, "failed or stale"):
            pipeline.deliver(batch)
        pipeline.run_spec_qc(batch, prober=fake_prober)
        with self.assertRaisesRegex(RuntimeError, "preflight report is required"):
            pipeline.deliver(batch)
        failing = [{"check": "black", "status": "FAIL", "detail": "开场 0.0-1.0s 黑屏"}]
        self.assertFalse(pipeline.run_batch_preflight(batch, runner=fake_preflight("FAIL", failing))["passed"])
        with self.assertRaisesRegex(RuntimeError, "preflight has FAIL"):
            pipeline.deliver(batch)
        pipeline.run_batch_preflight(batch, runner=fake_preflight())
        self.assertFalse((batch / pipeline.HANDOFF).exists())
        handoff = pipeline.deliver(batch)
        self.assertEqual(handoff, batch / "50_delivery/manifest.json")

    def test_handoff_lists_every_creative_without_touching_files(self) -> None:
        batch = self.ready_batch()
        before = {p: p.read_bytes() for p in (batch / "50_delivery").rglob("*.mp4")}
        pipeline.run_spec_qc(batch, prober=fake_prober)
        warning = [{"check": "endcard", "status": "WARN", "detail": "结尾没认出品牌"}]
        pipeline.run_batch_preflight(batch, runner=fake_preflight("WARN", warning))
        pipeline.approve_review(batch, "马天")
        data = json.loads(pipeline.deliver(batch).read_text())
        self.assertEqual(data["creatives"][0]["preflight"], {"status": "WARN", "warnings": ["结尾没认出品牌"]})
        self.assertEqual({p: p.read_bytes() for p in (batch / "50_delivery").rglob("*.mp4")}, before)
        self.assertEqual((data["batch_id"], data["profile"], data["product_code"], data["approved_by"]),
                         (PC_BATCH, "hakko-pc", "hakkopc", "马天"))
        creative = next(c for c in data["creatives"] if c["format"] == "1:1")
        self.assertEqual(creative["creative_id"], f"hakkopc_{PC_BATCH}_us_1x1_anytime_v1")
        self.assertEqual(creative["path"], f"50_delivery/1x1/US/{creative['creative_id']}.mp4")
        self.assertEqual((creative["hook"], creative["version"], creative["width"], creative["height"]),
                         ("anytime", 1, 1080, 1080))
        self.assertEqual(creative["tags"], {"persona": "teammate"})
        self.assertEqual(creative["bytes"], len("1080x1080"))
        self.assertRegex(creative["sha256"], r"^[0-9a-f]{64}$")

    def test_blocked_when_a_file_changes_after_qc_or_approval_is_for_an_old_review(self) -> None:
        batch = self.ready_batch()
        pipeline.run_spec_qc(batch, prober=fake_prober)
        pipeline.run_batch_preflight(batch, runner=fake_preflight())
        pipeline.approve_review(batch, "reviewer")
        target = next((batch / "50_delivery").rglob("*.mp4"))
        target.write_text("1920x1080!")
        with self.assertRaisesRegex(RuntimeError, "file changed after review"):
            pipeline.deliver(batch)
        old_approval = (batch / pipeline.APPROVAL).read_text()
        pipeline.create_review_manifest(batch)
        (batch / pipeline.APPROVAL).write_text(old_approval)
        with self.assertRaisesRegex(RuntimeError, "approval does not match"):
            pipeline.deliver(batch)


class PreflightGateTests(BatchCase):
    def test_preflight_fails_a_file_changed_after_review_and_is_cleared_by_review(self) -> None:
        batch = self.make_batch()
        path = self.add_creative(batch, "US", "16:9", "anytime")
        pipeline.create_review_manifest(batch)
        path.write_text("1920x1080 changed")
        result = pipeline.run_batch_preflight(batch, runner=fake_preflight())
        self.assertFalse(result["passed"])
        self.assertEqual(result["candidates"][0]["issues"][-1]["check"], "changed")
        self.assertTrue(result["report_html"].endswith("preflight/report.html"))
        pipeline.create_review_manifest(batch)
        self.assertFalse((batch / pipeline.PREFLIGHT_REPORT).exists())

    def test_normalize_transcodes_without_touching_the_picture(self) -> None:
        command = pipeline.build_normalize_command(Path("in.mov"), Path("out.mp4"))
        for flag, value in (("-c:v", "libx264"), ("-pix_fmt", "yuv420p"), ("-r", "30"), ("-ar", "48000"),
                            ("-ac", "2"), ("-vf", "setsar=1"), ("-fps_mode", "cfr")):
            self.assertEqual(command[command.index(flag) + 1], value)
        with self.assertRaisesRegex(ValueError, "覆盖输入"):
            pipeline.build_normalize_command(Path("same.mp4"), Path("same.mp4"))


class AnalyzeAndRenderCommandTests(BatchCase):
    def test_analyze_writes_process_files_outside_the_batch(self) -> None:
        batch = self.make_batch()
        seen = []
        output = pipeline.run_local_analyze(batch, runner=seen.append)
        command = seen[0]
        self.assertTrue(command[1].endswith("analyze.py"))
        self.assertEqual(Path(command[2]), (batch / "00_input").resolve())
        self.assertEqual(Path(command[4]), output)
        self.assertEqual(output, self.root / "work" / PC_BATCH / "analysis")
        (batch / "00_input").rmdir()
        with self.assertRaisesRegex(ValueError, "00_input"):
            pipeline.run_local_analyze(batch, runner=seen.append)

    def test_burn_srt_normalizes_then_burns_subtitles_last(self) -> None:
        for format_name, canvas in (("16:9", "1920:1080"), ("9:16", "1080:1920"), ("1:1", "1080:1080")):
            with self.subTest(format_name=format_name):
                command = pipeline.build_burn_srt_command(
                    Path("in.mp4"), Path("sub.srt"), Path("out.mp4"), format_name, "source", None)
                vf = command[command.index("-vf") + 1]
                self.assertIn(f"scale={canvas}", vf)
                self.assertTrue(vf.split(",")[-1].startswith("subtitles="), vf)
                self.assertIn("[0:a:0]apad[aout]", command)
        with self.assertRaisesRegex(ValueError, "voiceover"):
            pipeline.build_burn_srt_command(Path("i"), Path("s"), Path("o"), "1:1", "voiceover", None)

    def test_endcard_per_format_and_profile_limits(self) -> None:
        command = pipeline.build_endcard_command("9:16", "JP", Path("end.mp4"))
        self.assertTrue(any("color=c=#101014:s=1080x1920" in arg for arg in command))
        self.assertIn("今すぐダウンロード", command[command.index("-filter_complex") + 1])
        with self.assertRaisesRegex(ValueError, "不生成尾帧"):
            pipeline.build_endcard_command("9:16", "US", Path("end.mp4"), "hakko-mobile")
        with self.assertRaisesRegex(ValueError, "ES"):
            pipeline.build_endcard_command("16:9", "ES", Path("end.mp4"))


class CliTests(BatchCase):
    def run_cli(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = pipeline.main(list(argv))
        return code, out.getvalue()

    def test_init_defaults_to_profile_root_and_name_prints_id_and_path(self) -> None:
        profile = {**config.get_product_profile("hakko-mobile"), "batch_root": str(self.root / "mobile")}
        with mock.patch.dict(config.PRODUCT_PROFILES, {"hakko-mobile": profile}):
            code, _ = self.run_cli("init", "--profile", "hakko-mobile", "--batch-id", "20261004-call-hooks")
            self.assertEqual(code, 0)
            batch = self.root / "mobile" / "20261004-call-hooks"
            code, out = self.run_cli("name", str(batch), "--locale", "US", "--format", "9:16",
                                     "--hook", "late-night-call")
        data = json.loads(out)
        self.assertEqual(data["creative_id"], "hakkomobile_20261004-call-hooks_us_9x16_late-night-call_v1")
        self.assertEqual(Path(data["path"]),
                         batch / "50_delivery/9x16/US/hakkomobile_20261004-call-hooks_us_9x16_late-night-call_v1.mp4")

    def test_init_requires_profile_and_valid_batch_id(self) -> None:
        for argv in (["init", "--batch-id", PC_BATCH], ["init", "--profile", "hakko-pc", "--batch-id", "../x"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit):
                pipeline.main(argv)


MEDIA_TOOLS = all(Path(p or "").is_file() for p in (config.FFMPEG, config.FFPROBE, config.WHISPER_CLI,
                                                      config.TESSERACT, config.WHISPER_MODEL))


@unittest.skipUnless(MEDIA_TOOLS, "needs ffmpeg, whisper-cli + model and tesseract (preflight runs in the flow)")
class EndToEndTests(BatchCase):
    """Real ffmpeg/ffprobe through the CLI: produce, review, QC, approve, deliver."""

    def encode(self, path: Path, size: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([config.FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=s={size}:r=30:d=6",
                        "-f", "lavfi", "-i", "sine=f=440:d=6:sample_rate=48000", "-ac", "2",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)],
                       check=True)

    def test_full_cli_flow_with_real_media(self) -> None:
        batch = self.make_batch()
        for locale, format_name, size in (("US", "16:9", "1920x1080"), ("US", "1:1", "1080x1080")):
            value = pipeline.creative_id("hakko-pc", batch.name, locale, format_name, "anytime", 1)
            self.encode(batch / "50_delivery" / pipeline.FORMAT_SPECS[format_name][2] / locale / f"{value}.mp4",
                        size)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(pipeline.main(["review", str(batch)]), 0)
            self.assertEqual(pipeline.main(["qc", str(batch)]), 0)
            self.assertEqual(pipeline.main(["preflight", str(batch)]), 0)
            self.assertEqual(pipeline.main(["check-approved", str(batch)]), 1)
            self.assertEqual(pipeline.main(["approve", str(batch), "--reviewer", "e2e"]), 0)
            self.assertEqual(pipeline.main(["deliver", str(batch)]), 0)
        data = json.loads((batch / pipeline.HANDOFF).read_text())
        self.assertEqual(sorted((c["format"], c["width"], c["height"]) for c in data["creatives"]),
                         [("16:9", 1920, 1080), ("1:1", 1080, 1080)])
        self.assertTrue(all(abs(c["duration_sec"] - 6.0) < 0.1 for c in data["creatives"]))
        self.assertTrue(all(c["preflight"]["status"] in {"PASS", "WARN"} for c in data["creatives"]))
        self.assertTrue((self.root / "work" / PC_BATCH / "preflight" / "report.html").is_file())
        self.assertFalse(any((self.root / "work").rglob(".qc-snapshot-*")))
        shutil.rmtree(batch / "50_delivery" / "1x1")
        with self.assertRaises((RuntimeError, ValueError)):
            pipeline.deliver(batch)


if __name__ == "__main__":
    unittest.main()
