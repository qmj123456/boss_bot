import errno
import importlib.util
import json
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from PIL import Image
from tests.test_card_prescreen import _complete_candidate


ROOT = Path(__file__).resolve().parents[1]
LIST_SCRIPT = ROOT / "boss-recommend-downloader" / "scripts" / "recommend_list.py"
DOWNLOAD_SCRIPT = ROOT / "boss-recommend-downloader" / "scripts" / "recommend_download.py"


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_api_candidates_are_only_metadata_until_a_rendered_card_matches():
    module = _load(LIST_SCRIPT, "recommend_list_live_transaction")
    by_id = {}
    by_name = {}
    candidate = {
        "encryptGeekId": "api-gid-1",
        "geekCard": {
            "encryptJobId": "job-1",
            "geekName": "张三",
        },
    }

    assert module._cache_api_candidate(by_id, by_name, candidate, "job-1") is True
    assert module._match_rendered_candidate(
        {"dom_gid": "api-gid-1", "name": "张三"}, by_id, by_name
    ) is candidate
    # 未真实渲染的 API 候选人不会自行产生任何待处理结果。
    assert module._match_rendered_candidate(
        {"dom_gid": "missing", "name": "李四"}, by_id, by_name
    ) is None


def test_rendered_card_scanner_skips_hot_search_recommendation_module():
    module = _load(LIST_SCRIPT, "recommend_list_skip_hot_search_module")
    source = module.SCAN_RENDERED_CARDS_JS

    assert "isNonStandardRecommendation" in source
    assert "热搜牛人推荐" in source
    assert "首项推荐" in source
    assert ".filter(card => !isNonStandardRecommendation(card))" in source


def test_api_candidate_without_job_id_is_not_accepted_for_multi_job_flow():
    module = _load(LIST_SCRIPT, "recommend_list_missing_job_id_guard")
    by_id = {}
    by_name = {}
    candidate = {
        "encryptGeekId": "gid-without-job",
        "geekCard": {"geekName": "张三"},
    }

    assert module._cache_api_candidate(
        by_id, by_name, candidate, "expected-job-id"
    ) is False
    assert by_id == {}
    assert by_name == {}


def test_slim_candidate_preserves_raw_boss_employment_status_fields():
    module = _load(LIST_SCRIPT, "recommend_list_raw_employment_status")
    candidate = {
        "encryptGeekId": "gid-status",
        "applyStatus": 0,
        "geekCard": {
            "geekName": "张三",
            "applyStatus": 3,
            "applyStatusDesc": "",
            "employmentStatus": 8,
            "employmentStatusDesc": "离职-随时到岗",
            "jobSeekingStatus": 2,
            "jobSeekingStatusDesc": "正在找工作",
        },
    }

    slim = module._slim_candidate_card(
        candidate,
        {"status": "pending_confirmation", "allow_full_resume": True},
    )

    card = slim["geekCard"]
    assert card["applyStatus"] == 3
    assert card["employmentStatus"] == 8
    assert card["employmentStatusDesc"] == "离职-随时到岗"
    assert card["jobSeekingStatus"] == 2
    assert slim["_raw_employment_status"] == {
        "candidate": {"applyStatus": 0},
        "geekCard": {
            "applyStatus": 3,
            "applyStatusDesc": "",
            "employmentStatus": 8,
            "employmentStatusDesc": "离职-随时到岗",
            "jobSeekingStatus": 2,
            "jobSeekingStatusDesc": "正在找工作",
        },
    }


def test_prescreen_summary_counts_pending_candidates_that_proceeded(tmp_path):
    module = _load(LIST_SCRIPT, "recommend_list_pending_audit_summary")
    rejected_path = tmp_path / "rejected.json"
    summary_path = tmp_path / "summary.json"
    accepted = [{"_card_prescreen": {"status": "pending_confirmation"}}]
    rejected = [{"status": "rejected"}]

    module._save_card_prescreen_artifacts(
        rejected_path, summary_path, rejected, accepted
    )

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary == {
        "accepted_count": 1,
        "rejected_count": 1,
        "pending_confirmation_count": 1,
        "pending_proceeded_count": 1,
        "pending_blocked_count": 0,
    }


def test_live_match_uses_unique_name_only_when_boss_dom_id_differs():
    module = _load(LIST_SCRIPT, "recommend_list_live_name_match")
    by_id = {}
    by_name = {}
    first = {
        "encryptGeekId": "api-gid-1",
        "geekCard": {"encryptJobId": "job-1", "geekName": "张三"},
    }
    module._cache_api_candidate(by_id, by_name, first, "job-1")

    assert module._match_rendered_candidate(
        {"dom_gid": "dom-version-of-id", "name": "张三刚刚活跃"}, by_id, by_name
    ) is first

    second = {
        "encryptGeekId": "api-gid-2",
        "geekCard": {"encryptJobId": "job-1", "geekName": "张三"},
    }
    module._cache_api_candidate(by_id, by_name, second, "job-1")
    assert module._match_rendered_candidate(
        {"dom_gid": "unknown", "name": "张三"}, by_id, by_name
    ) is None


def test_thirty_rendered_cards_each_match_one_transaction_candidate():
    module = _load(LIST_SCRIPT, "recommend_list_live_thirty_cards")
    by_id = {}
    by_name = {}
    expected = []
    for index in range(30):
        geek_id = f"gid-{index:02d}"
        candidate = {
            "encryptGeekId": geek_id,
            "geekCard": {
                "encryptJobId": "job-1",
                "geekName": f"候选人{index:02d}",
            },
        }
        expected.append(candidate)
        module._cache_api_candidate(by_id, by_name, candidate, "job-1")

    matched = [
        module._match_rendered_candidate(
            {"dom_gid": f"gid-{index:02d}", "name": f"候选人{index:02d}"},
            by_id,
            by_name,
        )
        for index in range(30)
    ]

    assert matched == expected
    assert len({item["encryptGeekId"] for item in matched}) == 30


def test_live_candidate_click_never_scrolls_to_search_again(monkeypatch):
    module = _load(DOWNLOAD_SCRIPT, "recommend_download_live_transaction")
    processor = module.LiveCandidateProcessor.__new__(module.LiveCandidateProcessor)
    processor.processed_ids = set()
    processor.greet_results = []
    processor.failed = []
    processor.analysis_candidates = []
    processor.max_greetings = 10
    processor.job_id = "job-1"
    processor.run_id = "run-1"
    processor.greet_message = "固定话术"
    processor._commit = lambda: None
    processor._log = lambda _message: None

    class Store:
        def get_status(self, _job_id, _geek_id):
            return None

        def mark_failed(self, *_args):
            return None

    processor.store = Store()

    captured = {}

    def fail_after_recording(*_args, **kwargs):
        captured.update(kwargs)
        raise RuntimeError("forced test failure")

    monkeypatch.setattr(module, "_find_and_open_resume", fail_after_recording)

    class Iframe:
        def content_frame(self):
            return object()

        def bounding_box(self):
            return {"x": 0, "y": 0}

    class Page:
        url = "https://www.zhipin.com/web/chat/recommend"

        def query_selector(self, _selector):
            return Iframe()

    result = processor.process({
        "encryptGeekId": "api-gid-1",
        "geekCard": dict(_complete_candidate()["geekCard"], geekName="张三"),
        "_live_dom_card": {
            "dom_gid": "dom-gid-1", "name": "张三", "token": "live-token-1"
        },
    }, Page(), object())

    assert result["ok"] is False
    assert captured["max_scroll_steps"] == 0
    assert captured["reset_to_top"] is False
    assert captured["live_card_token"] == "live-token-1"


def test_resume_screenshot_retries_in_place_and_reuses_completed_capture(
    monkeypatch, tmp_path
):
    module = _load(DOWNLOAD_SCRIPT, "recommend_download_screenshot_retry")
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(module.random, "uniform", lambda low, _high: low)

    image = Image.new("RGB", (500, 500), "white")
    for x in range(50, 450):
        image.putpixel((x, 200), (0, 0, 0))
        image.putpixel((x, 300), (0, 0, 0))
    encoded = BytesIO()
    image.save(encoded, format="JPEG")
    image_bytes = encoded.getvalue()

    class ScrollContext:
        def evaluate(self, script, _arg=None):
            if script == module.INIT_SCROLL_CAPTURE_JS:
                return {"ok": True, "viewport": 500}
            if script == module.GET_RESUME_CAPTURE_BOUNDS_JS:
                return {
                    "ok": True, "x": 0, "y": 0, "width": 500,
                    "height": 500, "contentWidth": 500,
                }
            if script == module.READ_CAPTURE_METRICS_JS:
                return {"top": 0, "height": 500, "viewport": 500}
            return None

    class ScreenshotPage:
        def __init__(self):
            self.calls = 0

        def screenshot(self, **kwargs):
            self.calls += 1
            assert kwargs["timeout"] == 30000
            assert kwargs["animations"] == "disabled"
            assert kwargs["caret"] == "hide"
            if self.calls < 2:
                raise TimeoutError("forced screenshot timeout")
            return image_bytes

    screenshot_page = ScreenshotPage()
    first = module._capture_all_resume_screens(
        ScrollContext(), screenshot_page, tmp_path, max_screenshots=1
    )

    assert screenshot_page.calls == 2
    assert len(first) == 1
    assert Path(first[0]).is_file()
    assert (tmp_path / "capture_complete.json").is_file()

    class MustNotCaptureAgain:
        def screenshot(self, **_kwargs):
            raise AssertionError("completed screenshot should have been reused")

    second = module._capture_all_resume_screens(
        object(), MustNotCaptureAgain(), tmp_path, max_screenshots=1
    )
    assert second == first


def test_deepseek_retries_only_the_same_saved_screenshots(monkeypatch, tmp_path):
    module = _load(DOWNLOAD_SCRIPT, "recommend_download_saved_ai_retry")
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    screenshot = tmp_path / "resume.jpg"
    screenshot.write_bytes(b"validated-by-test")
    monkeypatch.setattr(
        module,
        "_validate_resume_screenshot_files",
        lambda paths: [str(Path(path).resolve()) for path in paths],
    )

    resume = {"resume_screenshots": [str(screenshot)]}

    class Analyzer:
        def __init__(self):
            self.calls = []

        def analyze(self, *, job_requirement, resume):
            self.calls.append((job_requirement, tuple(resume["resume_screenshots"])))
            if len(self.calls) < 3:
                raise ValueError("empty JSON")
            return {"passed": False, "reason": "not matched"}

    analyzer = Analyzer()
    result, error, attempts = module._analyze_saved_resume(
        analyzer, {"job_name": "主播"}, resume, attempts=3
    )

    assert result["passed"] is False
    assert error is None
    assert attempts == 3
    assert len(analyzer.calls) == 3
    assert len({call[1] for call in analyzer.calls}) == 1


def test_deepseek_default_stops_after_two_retries(monkeypatch, tmp_path):
    module = _load(DOWNLOAD_SCRIPT, "recommend_download_ai_two_attempts")
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    screenshot = tmp_path / "resume.jpg"
    screenshot.write_bytes(b"validated-by-test")
    monkeypatch.setattr(
        module,
        "_validate_resume_screenshot_files",
        lambda paths: [str(Path(path).resolve()) for path in paths],
    )

    class Analyzer:
        def __init__(self):
            self.calls = 0

        def analyze(self, **_kwargs):
            self.calls += 1
            raise TimeoutError("vision timeout")

    analyzer = Analyzer()
    result, error, attempts = module._analyze_saved_resume(
        analyzer,
        {"job_name": "主播"},
        {"resume_screenshots": [str(screenshot)]},
    )

    assert result is None
    assert isinstance(error, TimeoutError)
    assert attempts == 3
    assert analyzer.calls == 3


def test_deepseek_final_failure_closes_once_and_does_not_request_card_retry(
    monkeypatch, tmp_path
):
    module = _load(DOWNLOAD_SCRIPT, "recommend_download_ai_terminal")
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    processor = module.LiveCandidateProcessor.__new__(module.LiveCandidateProcessor)
    processor.processed_ids = set()
    processor.greet_results = []
    processor.failed = []
    processor.analysis_candidates = []
    processor.analysis_failures = []
    processor.new_resumes = []
    processor.max_greetings = 10
    processor.job_id = "job-1"
    processor.run_id = "run-1"
    processor.greet_message = "固定话术"
    processor.requirement = {"job_name": "主播"}
    processor.analyzer = object()
    processor.output = SimpleNamespace(run_dir=str(tmp_path))
    processor._commit = lambda: None
    processor._log = lambda _message: None

    class Store:
        def get_status(self, *_args):
            return None

        def save_resume(self, *_args):
            return True

        def mark_failed(self, *_args):
            return None

    processor.store = Store()
    opens = []
    closes = []
    detail = object()
    monkeypatch.setattr(module, "_ensure_no_resume_open", lambda *_args: True)
    monkeypatch.setattr(
        module,
        "_find_and_open_resume",
        lambda *_args, **_kwargs: (opens.append(True) or detail, False, "recommend"),
    )
    monkeypatch.setattr(module, "_verify_open_resume_identity", lambda *_args, **_kwargs: "张三")
    monkeypatch.setattr(
        module, "_capture_all_resume_screens",
        lambda *_args, **_kwargs: [str(tmp_path / "resume.jpg")],
    )
    monkeypatch.setattr(
        module, "_resume_record_from_card",
        lambda _candidate, paths: {"resume_screenshots": paths},
    )
    monkeypatch.setattr(
        module, "_analyze_saved_resume",
        lambda *_args, **_kwargs: (None, ValueError("empty JSON"), 3),
    )
    monkeypatch.setattr(
        module, "_close_resume",
        lambda *_args, **_kwargs: closes.append(True) or True,
    )

    class Iframe:
        def content_frame(self):
            return object()

        def bounding_box(self):
            return {"x": 0, "y": 0}

    class Page:
        url = "recommend"

        def query_selector(self, _selector):
            return Iframe()

    result = processor.process({
        "encryptGeekId": "gid-1",
        "geekCard": dict(_complete_candidate()["geekCard"], geekName="张三"),
        "_live_dom_card": {"token": "card-token"},
    }, Page(), object())

    assert result == {
        "ok": True, "stop": False,
        "terminal_status": "analysis_unavailable",
    }
    assert len(opens) == 1
    assert len(closes) == 1
    assert "gid-1" in processor.processed_ids


def test_screenshot_final_failure_is_recorded_and_skips_card_retry(monkeypatch, tmp_path):
    module = _load(DOWNLOAD_SCRIPT, "recommend_download_capture_terminal")
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    processor = module.LiveCandidateProcessor.__new__(module.LiveCandidateProcessor)
    processor.processed_ids = set()
    processor.greet_results = []
    processor.failed = []
    processor.analysis_candidates = []
    processor.analysis_failures = []
    processor.new_resumes = []
    processor.max_greetings = 10
    processor.job_id = "job-1"
    processor.run_id = "run-1"
    processor.greet_message = "固定话术"
    processor.requirement = {"job_name": "主播"}
    processor.analyzer = object()
    processor.output = SimpleNamespace(run_dir=str(tmp_path))
    processor._commit = lambda: None
    processor._log = lambda _message: None

    class Store:
        def get_status(self, *_args):
            return None

        def mark_failed(self, *_args):
            return None

    processor.store = Store()
    closes = []
    detail = object()
    monkeypatch.setattr(module, "_ensure_no_resume_open", lambda *_args: True)
    monkeypatch.setattr(
        module, "_find_and_open_resume",
        lambda *_args, **_kwargs: (detail, False, "recommend"),
    )
    monkeypatch.setattr(module, "_verify_open_resume_identity", lambda *_args, **_kwargs: "张三")
    monkeypatch.setattr(
        module, "_capture_all_resume_screens",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("capture failed")),
    )
    monkeypatch.setattr(
        module, "_close_resume",
        lambda *_args, **_kwargs: closes.append(True) or True,
    )

    class Iframe:
        def content_frame(self):
            return object()

        def bounding_box(self):
            return {"x": 0, "y": 0}

    class Page:
        url = "recommend"

        def query_selector(self, _selector):
            return Iframe()

    result = processor.process({
        "encryptGeekId": "gid-shot",
        "geekCard": dict(_complete_candidate()["geekCard"], geekName="张三"),
        "_live_dom_card": {"token": "card-token"},
    }, Page(), object())

    assert result == {
        "ok": True, "stop": False, "terminal_status": "screenshot_failed",
    }
    assert processor.failed[0]["stage"] == "resume_capture"
    assert processor.failed[0]["screenshot_attempts"] == 2
    assert "gid-shot" in processor.processed_ids
    assert len(closes) == 1


def test_live_list_records_three_failures_as_terminal_instead_of_aborting():
    source = LIST_SCRIPT.read_text(encoding="utf-8")

    assert "'status': 'failed_after_retries'" in source
    assert "已记录失败终态并继续下一张" in source
    assert "单卡闭环连续失败" not in source


def test_atomic_json_write_tolerates_windows_fsync_einval(monkeypatch, tmp_path):
    module = _load(DOWNLOAD_SCRIPT, "recommend_download_windows_fsync")

    def unsupported_fsync(_fd):
        raise OSError(errno.EINVAL, "Invalid argument")

    monkeypatch.setattr(module.os, "fsync", unsupported_fsync)
    destination = tmp_path / "result.json"
    module._atomic_write_json(destination, {"ok": True})

    assert json.loads(destination.read_text(encoding="utf-8")) == {"ok": True}


def test_resume_identity_mismatch_blocks_downstream_actions(monkeypatch):
    module = _load(DOWNLOAD_SCRIPT, "recommend_download_identity_guard")
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)

    class WrongResume:
        def evaluate(self, script, expected):
            assert script == module.VERIFY_RESUME_IDENTITY_JS
            assert expected == {"name": "郭贤", "gid": "gid-guo"}
            return {
                "open": True,
                "matched": False,
                "actual": "",
                "evidence": ["周志扬", "25岁", "大专"],
            }

    try:
        module._verify_open_resume_identity(WrongResume(), "郭贤", "gid-guo")
    except RuntimeError as exc:
        assert "在线简历身份不一致" in str(exc)
        assert "卡片=郭贤" in str(exc)
        assert "周志扬" in str(exc)
    else:
        raise AssertionError("mismatched resume identity must be rejected")


def test_resume_identity_matches_expected_name_not_action_button(monkeypatch):
    module = _load(DOWNLOAD_SCRIPT, "recommend_download_identity_exact_name")
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)

    class CorrectResume:
        def evaluate(self, script, expected):
            assert script == module.VERIFY_RESUME_IDENTITY_JS
            assert expected["name"] == "宋蓉"
            return {
                "open": True,
                "matched": True,
                "actual": "宋蓉",
                "matchBy": "header_text",
                "evidence": ["宋蓉", "帮我联系"],
            }

    assert module._verify_open_resume_identity(
        CorrectResume(), "宋蓉", "gid-song"
    ) == "宋蓉"


def test_open_resume_prefers_origin_frame_over_empty_parent_shell(monkeypatch):
    module = _load(DOWNLOAD_SCRIPT, "recommend_download_origin_frame")
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(module, "human_move", lambda *_args, **_kwargs: None)

    class Frame:
        def evaluate(self, script, _arg=None):
            if script == module.OPEN_LIVE_CARD_JS:
                return {"ok": True, "x": 100, "y": 120}
            if script == module.READ_VISIBLE_RESUME_JS:
                return {"open": True, "name": "赵明跃"}
            raise AssertionError("unexpected frame script")

    class Mouse:
        def click(self, _x, _y):
            return None

    class Page:
        url = "https://www.zhipin.com/web/chat/recommend"
        mouse = Mouse()

        def evaluate(self, script, expected=None):
            if script == module.VERIFY_RESUME_IDENTITY_JS:
                assert expected == {"name": "赵明跃", "gid": "gid-zhao"}
                return {"open": True, "matched": False, "evidence": []}
            raise AssertionError("unexpected page script")

    page = Page()

    class Context:
        pages = [page]

    frame = Frame()
    detail_context, opened_new_page, original_url = module._find_and_open_resume(
        frame, page, Context(), "gid-zhao", "赵明跃",
        iframe_box={"x": 0, "y": 0}, max_scroll_steps=0,
        reset_to_top=False, live_card_token="token-zhao",
    )

    assert detail_context is frame
    assert opened_new_page is False
    assert original_url == page.url


def test_identity_script_searches_expected_name_without_header_coordinates():
    module = _load(DOWNLOAD_SCRIPT, "recommend_download_identity_layout_free")

    assert "normalizedText.includes(expectedName)" in module.VERIFY_RESUME_IDENTITY_JS
    assert "r.top <= rr.top + 220" not in module.VERIFY_RESUME_IDENTITY_JS


def test_canvas_resume_uses_live_card_token_identity(monkeypatch):
    module = _load(DOWNLOAD_SCRIPT, "recommend_download_canvas_identity")
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)

    class CanvasResume:
        def evaluate(self, script, expected):
            if script == module.VERIFY_RESUME_IDENTITY_JS:
                return {
                    "open": True, "matched": False, "actual": "",
                    "evidence": ["收藏", "帮我联系", "经历概览"],
                }
            if script == module.VERIFY_LIVE_CARD_RESUME_JS:
                assert expected == {
                    "name": "罗运祥", "gid": "gid-luo", "token": "token-luo"
                }
                return {
                    "open": True, "matched": True, "actual": "罗运祥",
                    "matchBy": "live_dom_token_canvas",
                }
            raise AssertionError("unexpected script")

    assert module._verify_open_resume_identity(
        CanvasResume(), "罗运祥", "gid-luo", "token-luo"
    ) == "罗运祥"


def test_visible_live_card_does_not_scroll_to_center():
    module = _load(DOWNLOAD_SCRIPT, "recommend_download_no_card_jump")

    assert "rect.top < 0 || rect.bottom > window.innerHeight" in module.OPEN_LIVE_CARD_JS
    assert "block: 'center'" not in module.OPEN_LIVE_CARD_JS


def test_close_resume_waits_until_detail_dom_disappears(monkeypatch):
    module = _load(DOWNLOAD_SCRIPT, "recommend_download_strict_close")
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)

    class Context:
        def __init__(self):
            self.open = True
            self.close_clicks = 0

        def evaluate(self, script):
            if script == module.READ_VISIBLE_RESUME_JS:
                return {"open": self.open, "name": "周志扬" if self.open else ""}
            if script == module.CLOSE_VISIBLE_RESUME_JS:
                self.close_clicks += 1
                self.open = False
                return {"open": True, "clicked": True}
            raise AssertionError("unexpected script")

    class Keyboard:
        def press(self, _key):
            raise AssertionError("Escape fallback should not be needed")

    class Page:
        url = "https://www.zhipin.com/web/chat/recommend"
        keyboard = Keyboard()

    context = Context()
    assert module._close_resume(context, Page(), False, Page.url) is True
    assert context.close_clicks == 1
    assert context.open is False


def test_resume_records_upsert_by_nested_meta_id():
    module = _load(DOWNLOAD_SCRIPT, "recommend_download_nested_meta_upsert")
    records = []
    first = {"name": "张三", "_meta": {"encrypt_geek_id": "gid-1"}}
    second = {"name": "李四", "_meta": {"encrypt_geek_id": "gid-2"}}
    updated_first = {"name": "张三新", "_meta": {"encrypt_geek_id": "gid-1"}}

    module._upsert_candidate_result(records, first)
    module._upsert_candidate_result(records, second)
    module._upsert_candidate_result(records, updated_first)

    assert records == [second, updated_first]
