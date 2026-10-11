"""Small deterministic regressions; no model download or paid API required."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pipeline


class PipelineTests(unittest.TestCase):
    def speech_model(self, **overrides):
        data = dict(start=0.0, end=1.0, text="  示例文稿  ",
                    avg_logprob=-0.8, no_speech_prob=0.1, compression_ratio=1.0)
        data.update(overrides)
        model = MagicMock()
        model.transcribe.return_value = (
            iter([SimpleNamespace(**data)]),
            SimpleNamespace(language="zh", language_probability=0.98))
        return model

    def test_timestamp_carry(self):
        self.assertEqual(pipeline.fmt_srt_time(59.9996), "00:01:00,000")
        self.assertEqual(pipeline.fmt_vtt_time(3599.9996), "01:00:00.000")
        self.assertEqual(pipeline.fmt_srt_time(-0.1), "00:00:00,000")
        with self.assertRaises(ValueError):
            pipeline.fmt_srt_time(float("nan"))

    def test_native_audio_and_merged_video_downloads(self):
        for video in (False, True):
            with self.subTest(video=video), tempfile.TemporaryDirectory() as directory:
                stem, ext = ("media", "mp4") if video else ("audio", "m4a")
                final = Path(directory) / f"{stem}.{ext}"
                final.write_bytes(b"media")
                (Path(directory) / f"{stem}.f123.m4a").write_bytes(b"fragment")
                (Path(directory) / f"{stem}.webm.part").write_bytes(b"partial")
                downloader = MagicMock()
                downloader.extract_info.return_value = {"title": "中文标题"}
                downloader.prepare_filename.return_value = str(final)
                with patch.object(pipeline.yt_dlp, "YoutubeDL") as factory:
                    factory.return_value.__enter__.return_value = downloader
                    function = pipeline.download_media if video else pipeline.download_audio
                    result = function("https://example.com/video", directory)
                self.assertEqual(result, (str(final), "中文标题"))
                options = factory.call_args.args[0]
                self.assertTrue(options["noplaylist"])
                self.assertNotIn("postprocessors", options)
                if video:
                    self.assertEqual(options["merge_output_format"], "mp4")
                    self.assertIn("height<=720", options["format"])

    def test_reject_unfinished_download(self):
        with tempfile.TemporaryDirectory() as directory:
            partial = Path(directory) / "audio.m4a.part"
            partial.write_bytes(b"incomplete")
            downloader = MagicMock()
            downloader.extract_info.return_value = {"title": "Incomplete"}
            downloader.prepare_filename.return_value = str(Path(directory) / "audio.m4a")
            with patch.object(pipeline.yt_dlp, "YoutubeDL") as factory:
                factory.return_value.__enter__.return_value = downloader
                with self.assertRaisesRegex(RuntimeError, "完整"):
                    pipeline.download_audio("https://example.com/video", directory)

    def test_caption_success_does_not_load_speech_model(self):
        segments = [{"start": 0, "end": 1, "text": "画面字幕"}]
        info = {"engine": "画面字幕", "quality_report": {"accuracy": None}}
        module = SimpleNamespace(extract=MagicMock(return_value=(segments, info)))
        with patch.dict(sys.modules, burned_captions=module), patch.object(pipeline, "get_model") as get_model:
            actual = pipeline.transcribe_file("video.mp4")
        self.assertEqual(actual, (segments, info))
        get_model.assert_not_called()

    def test_caption_failure_falls_back_with_reason_and_options(self):
        module = SimpleNamespace(extract=MagicMock(side_effect=RuntimeError("没有字幕")))
        model = self.speech_model()
        with patch.dict(sys.modules, burned_captions=module), patch.object(pipeline, "get_model", return_value=model):
            segments, info = pipeline.transcribe_file(
                "video.mp4", lang="auto", initial_prompt="  术语  ")
        self.assertIsNone(model.transcribe.call_args.kwargs["language"])
        self.assertEqual(model.transcribe.call_args.kwargs["initial_prompt"], "术语")
        self.assertIn("没有字幕", info["quality_notes"][0])
        self.assertEqual(segments[0]["text"], "示例文稿")
        self.assertEqual(info["review_count"], 1)
        self.assertIsNone(info["quality_report"]["accuracy"])
        self.assertNotIn("accuracy", segments[0])

    def test_invalid_caption_result_falls_back(self):
        module = SimpleNamespace(extract=MagicMock(return_value=(
            [{"start": 2, "end": 1, "text": "无效"}], {})))
        with patch.dict(sys.modules, burned_captions=module), patch.object(pipeline, "get_model", return_value=self.speech_model()):
            _, info = pipeline.transcribe_file("video.mp4")
        self.assertIn("无效", info["quality_notes"][0])

    def test_speech_only_skips_caption_extraction(self):
        module = SimpleNamespace(extract=MagicMock())
        with patch.dict(sys.modules, burned_captions=module), patch.object(pipeline, "get_model", return_value=self.speech_model()):
            pipeline.transcribe_file("audio.m4a", method="speech")
        module.extract.assert_not_called()

    def test_empty_speech_is_an_error(self):
        with patch.object(pipeline, "get_model", return_value=self.speech_model(text=" ")):
            with self.assertRaisesRegex(RuntimeError, "未识别"):
                pipeline.transcribe_file("silent.wav", method="speech")

    def test_packaged_model_is_preferred_and_cached(self):
        with tempfile.TemporaryDirectory() as directory:
            local = Path(directory) / "runtime" / "models" / "large-v3-turbo"
            local.mkdir(parents=True)
            (local / "model.bin").write_bytes(b"model")
            pipeline._models.clear()
            try:
                with patch.object(pipeline, "BASE", Path(directory)), patch.object(pipeline, "WhisperModel") as constructor:
                    self.assertIs(pipeline.get_model(), pipeline.get_model())
                constructor.assert_called_once_with(str(local), device="auto", compute_type="int8", cpu_threads=4)
            finally:
                pipeline._models.clear()

    def test_exports_have_valid_rounded_timestamps(self):
        with tempfile.TemporaryDirectory() as directory:
            segments = [{"start": 59.9996, "end": 61, "text": "中文文稿"}]
            outputs = pipeline.write_outputs(segments, "中文标题", str(Path(directory) / "new"))
            self.assertEqual(set(outputs), {"txt", "md", "srt", "vtt"})
            self.assertIn("00:01:00,000 --> 00:01:01,000", Path(outputs["srt"]).read_text())
            self.assertIn("00:01:00.000 --> 00:01:01.000", Path(outputs["vtt"]).read_text())


if __name__ == "__main__":
    unittest.main()
