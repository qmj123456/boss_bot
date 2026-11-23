import importlib.util
from io import BytesIO
import json
from pathlib import Path

from PIL import Image, ImageDraw


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "boss-recommend-downloader"
    / "scripts"
    / "recommend_download.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("recommend_download_screenshot", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_resume_record_keeps_card_metadata_and_screenshot_paths():
    module = _load_module()
    record = module._resume_record_from_card(
        {
            "geekCard": {
                "geekName": "张三",
                "ageDesc": "26岁",
                "degreeName": "本科",
                "workYearDesc": "3年",
                "expectLocationName": "杭州",
                "expectPositionName": "运营",
                "lowSalary": 10,
                "highSalary": 15,
                "geekWorks": [{"company": "示例公司", "positionName": "运营专员"}],
            }
        },
        ["C:/shots/page_01.jpg", "C:/shots/page_02.jpg"],
    )

    assert record["ok"] is True
    assert record["source"] == "online_resume_screenshot"
    assert record["name"] == "张三"
    assert record["expected_salary"] == "10-15"
    assert record["work_experience"][0]["company"] == "示例公司"
    assert len(record["resume_screenshots"]) == 2


class _FakeScrollContext:
    def __init__(self, height=1600, viewport=600):
        self.height = height
        self.viewport = viewport
        self.top = 0

    def evaluate(self, script, arg=None):
        if arg is None:
            self.top = 0
        elif arg == "bounds":
            return {"ok": True, "x": 100, "y": 0, "width": 1000, "height": 600}
        elif arg is True:
            pass
        else:
            self.top = min(arg, self.height - self.viewport)
        return {"ok": True, "height": self.height, "viewport": self.viewport, "top": self.top}


class _FakeScreenshotPage:
    def __init__(self):
        self.clips = []

    def screenshot(self, *, clip, type, quality):
        assert type == "jpeg"
        assert quality == 85
        self.clips.append(clip)
        image = Image.new("RGB", (int(clip["width"]), int(clip["height"])), "white")
        draw = ImageDraw.Draw(image)
        for y in range(40, image.height - 20, 45):
            draw.rectangle((60, y, image.width - 100, y + 8), fill="#444444")
        output = BytesIO()
        image.save(output, format="JPEG")
        return output.getvalue()


def test_capture_scrolls_until_bottom_and_returns_all_images(tmp_path, monkeypatch):
    module = _load_module()
    monkeypatch.setattr(module.time, "sleep", lambda _: None)

    page = _FakeScreenshotPage()
    paths = module._capture_all_resume_screens(
        _FakeScrollContext(), page, tmp_path / "shots",
        iframe_box={"x": 168, "y": 40},
    )

    assert len(paths) == 1
    assert all(Path(path).is_file() for path in paths)
    assert Path(paths[0]).name == "online_resume_long.jpg"
    with Image.open(paths[0]) as image:
        assert image.width == 1000
        assert image.height > 600
    assert page.clips[0] == {
        "x": 268.0, "y": 40.0, "width": 1000.0, "height": 600.0,
    }
    manifest = json.loads(
        (tmp_path / "shots" / "capture_complete.json").read_text(encoding="utf-8")
    )
    assert manifest["capture_complete"] is True
    assert manifest["reached_bottom"] is True
    assert manifest["covered_until"] >= manifest["content_height"]


def test_capture_continues_when_resume_height_grows_lazily(tmp_path, monkeypatch):
    module = _load_module()
    monkeypatch.setattr(module.time, "sleep", lambda _: None)

    class LazyGrowingContext(_FakeScrollContext):
        def __init__(self):
            super().__init__(height=1200, viewport=600)
            self.grown = False

        def evaluate(self, script, arg=None):
            result = super().evaluate(script, arg)
            if arg is True and self.top + self.viewport >= self.height and not self.grown:
                self.height += 700
                self.grown = True
                result = {
                    "ok": True,
                    "height": self.height,
                    "viewport": self.viewport,
                    "top": self.top,
                }
            return result

    page = _FakeScreenshotPage()
    paths = module._capture_all_resume_screens(
        LazyGrowingContext(), page, tmp_path / "lazy"
    )

    assert paths
    assert len(page.clips) >= 3
    manifest = json.loads(
        (tmp_path / "lazy" / "capture_complete.json").read_text(encoding="utf-8")
    )
    assert manifest["content_height"] == 1900
    assert manifest["covered_until"] >= 1900


def test_screenshot_failure_retries_only_once(tmp_path, monkeypatch):
    module = _load_module()
    monkeypatch.setattr(module.time, "sleep", lambda _: None)

    class FailingPage:
        def __init__(self):
            self.calls = 0

        def screenshot(self, **_kwargs):
            self.calls += 1
            raise RuntimeError("capture failed")

    page = FailingPage()
    try:
        module._capture_all_resume_screens(
            _FakeScrollContext(), page, tmp_path / "failed"
        )
    except RuntimeError as exc:
        assert "连续 2 次失败" in str(exc)
    else:
        raise AssertionError("capture failure must be terminal after two attempts")
    assert page.calls == 2


def test_capture_rejects_blank_image(tmp_path, monkeypatch):
    module = _load_module()
    monkeypatch.setattr(module.time, "sleep", lambda _: None)

    class BlankPage(_FakeScreenshotPage):
        def screenshot(self, *, clip, type, quality):
            image = Image.new("RGB", (int(clip["width"]), int(clip["height"])), "white")
            output = BytesIO()
            image.save(output, format="JPEG")
            return output.getvalue()

    try:
        module._capture_all_resume_screens(
            _FakeScrollContext(), BlankPage(), tmp_path / "blank",
        )
    except RuntimeError as exc:
        assert "疑似空白" in str(exc)
    else:
        raise AssertionError("blank resume screenshot must fail")


def test_capture_restores_inner_scroll_and_outer_page_even_on_failure(
        tmp_path, monkeypatch):
    """截图失败也必须清理简历滚动状态并恢复外层 BOSS 页面。"""
    module = _load_module()
    monkeypatch.setattr(module.time, "sleep", lambda _: None)

    class GuardedScrollContext(_FakeScrollContext):
        def __init__(self):
            super().__init__()
            self.cleaned = False

        def evaluate(self, script, arg=None):
            if script == module.CLEANUP_SCROLL_CAPTURE_JS:
                self.cleaned = True
                self.top = 0
                return {"ok": True, "restored": True}
            return super().evaluate(script, arg)

    class GuardedBlankPage(_FakeScreenshotPage):
        def __init__(self):
            super().__init__()
            self.outer_restored = False

        def evaluate(self, script, arg=None):
            if script == module.CAPTURE_OUTER_PAGE_STATE_JS:
                return {
                    "url": "https://www.zhipin.com/web/chat/recommend",
                    "scrollX": 0,
                    "scrollY": 120,
                    "innerWidth": 1600,
                    "innerHeight": 900,
                }
            if script == module.RESTORE_OUTER_PAGE_STATE_JS:
                self.outer_restored = True
                assert arg["scrollY"] == 120
                return {"ok": True}
            raise AssertionError("unexpected evaluate call")

        def screenshot(self, *, clip, type, quality, **_kwargs):
            image = Image.new(
                "RGB", (int(clip["width"]), int(clip["height"])), "white"
            )
            output = BytesIO()
            image.save(output, format="JPEG")
            return output.getvalue()

    scroll = GuardedScrollContext()
    page = GuardedBlankPage()
    try:
        module._capture_all_resume_screens(scroll, page, tmp_path / "guarded")
    except RuntimeError as exc:
        assert "疑似空白" in str(exc)
    else:
        raise AssertionError("blank resume screenshot must fail")

    assert scroll.cleaned is True
    assert page.outer_restored is True


def test_validate_accepts_contentful_tall_resume_after_thumbnailing(tmp_path):
    module = _load_module()
    path = tmp_path / "tall_resume.jpg"
    image = Image.new("RGB", (1410, 5548), "white")
    draw = ImageDraw.Draw(image)
    for y in range(100, 5450, 120):
        draw.rectangle((100, y, 1250, y + 24), fill="black")
    image.save(path, format="JPEG", quality=88)

    assert module._validate_resume_screenshot_files([path]) == [str(path.resolve())]


def test_capture_rejects_truncated_resume(tmp_path, monkeypatch):
    module = _load_module()
    monkeypatch.setattr(module.time, "sleep", lambda _: None)

    try:
        module._capture_all_resume_screens(
            _FakeScrollContext(height=10000, viewport=500),
            _FakeScreenshotPage(),
            tmp_path / "shots",
            max_screenshots=2,
        )
    except RuntimeError as exc:
        assert "避免遗漏信息" in str(exc)
    else:
        raise AssertionError("truncated resume capture must fail")


def test_capture_rejects_when_resume_scroll_position_does_not_change(tmp_path, monkeypatch):
    module = _load_module()
    monkeypatch.setattr(module.time, "sleep", lambda _: None)

    class StuckScrollContext(_FakeScrollContext):
        def evaluate(self, script, arg=None):
            if arg == "bounds":
                return {"ok": True, "x": 0, "y": 0, "width": 1000, "height": 600}
            return {
                "ok": True,
                "height": self.height,
                "viewport": self.viewport,
                "top": 0,
            }

    try:
        module._capture_all_resume_screens(
            StuckScrollContext(), _FakeScreenshotPage(), tmp_path / "shots"
        )
    except RuntimeError as exc:
        assert "scrollTop 未变化" in str(exc)
    else:
        raise AssertionError("capture must reject repeated screenshots from a stuck scroller")


def test_capture_accepts_small_browser_rounding_gap_at_real_bottom(tmp_path, monkeypatch):
    module = _load_module()
    monkeypatch.setattr(module.time, "sleep", lambda _: None)

    class RoundedBottomContext(_FakeScrollContext):
        def __init__(self):
            super().__init__(height=1606, viewport=600)

        def evaluate(self, script, arg=None):
            if arg is None:
                self.top = 0
            elif arg == "bounds":
                return {"ok": True, "x": 0, "y": 0, "width": 1000, "height": 600}
            elif arg is True:
                pass
            else:
                # Browser reports a 1606px scrollHeight but clamps its maximum
                # integer scrollTop to 1000 instead of the theoretical 1006.
                self.top = min(arg, 1000)
            return {
                "ok": True,
                "height": self.height,
                "viewport": self.viewport,
                "top": self.top,
            }

    paths = module._capture_all_resume_screens(
        RoundedBottomContext(), _FakeScreenshotPage(), tmp_path / "rounded"
    )

    assert paths
    manifest = json.loads(
        (tmp_path / "rounded" / "capture_complete.json").read_text(encoding="utf-8")
    )
    assert manifest["reached_bottom"] is True
    assert manifest["content_height"] - manifest["covered_until"] == 6


def test_capture_prefers_boss_online_resume_scroll_container():
    module = _load_module()
    assert "'.resume-detail-wrap'" in module.INIT_SCROLL_CAPTURE_JS
    assert "boss-layer__lock-scroll" in module.INIT_SCROLL_CAPTURE_JS
    assert "staticBest" in module.INIT_SCROLL_CAPTURE_JS
    assert "singleScreen" in module.INIT_SCROLL_CAPTURE_JS
    assert "'.resume-middle-wrap'" in module.GET_RESUME_CAPTURE_BOUNDS_JS
