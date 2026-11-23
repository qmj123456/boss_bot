from shared import boss_job_switcher as switcher


class _Mouse:
    def __init__(self):
        self.clicks = []

    def click(self, x, y):
        self.clicks.append((x, y))


class _Root:
    url = "https://www.zhipin.com/web/frame/recommend"

    def __init__(self, *, option_point=None):
        self.option_point = option_point

    def evaluate(self, script, arg=None):
        if "targetNorm" in script:
            return self.option_point
        return False

    def get_by_text(self, *_args, **_kwargs):
        raise RuntimeError("not found")

    def locator(self, *_args, **_kwargs):
        raise RuntimeError("not found")


class _Page(_Root):
    url = "https://www.zhipin.com/web/chat/recommend"
    main_frame = object()

    def __init__(self, *, option_point=None):
        super().__init__(option_point=option_point)
        self.mouse = _Mouse()
        self.frames = []


def test_click_job_option_searches_main_page_overlay_when_root_misses():
    root = _Root(option_point=None)
    page = _Page(option_point={"x": 321, "y": 123, "text": "短视频剪辑"})

    clicked = switcher._click_job_option(
        root,
        page,
        {"x": 10.0, "y": 20.0},
        "短视频剪辑（可转正/晋升、带教成熟/3C）",
        timeout_ms=100,
    )

    assert clicked is True
    assert page.mouse.clicks == [(321, 123)]


def test_visible_job_option_texts_collects_main_page_overlay_options():
    class Root(_Root):
        def evaluate(self, script, arg=None):
            return []

    class Page(_Page):
        def evaluate(self, script, arg=None):
            return ["短视频剪辑（可转正/晋升、带教成熟/3C）"]

    values = switcher._visible_job_option_texts(Root(), Page())

    assert values == ["短视频剪辑（可转正/晋升、带教成熟/3C）"]


def test_current_jobid_uses_visible_iframe_instead_of_stale_frame():
    class Frame:
        def __init__(self, job_id):
            self.url = f"https://www.zhipin.com/web/frame/recommend/?jobid={job_id}"

    class Iframe:
        def bounding_box(self):
            return {"x": 0, "y": 0, "width": 800, "height": 600}

        def content_frame(self):
            return active

    old, active = Frame("customer-service"), Frame("video-editor")
    page = _Page()
    page.frames = [old, active]
    page.query_selector = lambda _selector: Iframe()

    assert switcher._current_recommend_jobid(page) == "video-editor"


def test_job_option_does_not_click_matching_text_outside_dropdown():
    class Locator:
        @property
        def first(self):
            return self

        def click(self, **_kwargs):
            raise RuntimeError("no job option")

        def filter(self, **_kwargs):
            return self

    class Root(_Root):
        def evaluate(self, _script, _arg=None):
            return None

        def locator(self, selector):
            assert selector.startswith("li.job-item")
            return Locator()

    page = _Page()
    assert not switcher._click_job_option(
        Root(), page, {"x": 0.0, "y": 0.0}, "初级拍剪", timeout_ms=10,
    )
    assert page.mouse.clicks == []


def test_job_option_with_id_never_falls_back_to_same_name_wrong_id():
    class Root(_Root):
        def locator(self, _selector):
            raise RuntimeError("id not present")

    page = _Page()
    assert not switcher._click_job_option(
        Root(), page, {"x": 0.0, "y": 0.0}, "初级拍剪",
        encrypt_job_id="expected-id", timeout_ms=10,
    )
    assert page.mouse.clicks == []


def test_apply_job_filters_selects_configured_gender_before_scanning(monkeypatch):
    selected_keywords = []

    class Element:
        def click(self, **_kwargs):
            return None

    class Root:
        def query_selector(self, _selector):
            return Element()

        def wait_for_selector(self, _selector, **_kwargs):
            return None

        def evaluate(self, script, arg=None):
            if arg and "keywords" in arg:
                selected_keywords.append(arg["keywords"])
                return {
                    "text": arg["keywords"][0],
                    "cls": "option",
                    "isActive": False,
                }
            if "data-boss-filter-confirm" in script and "return {text:" in script:
                return {"text": "确定", "cls": "btn"}
            if "return s.display === 'none'" in script:
                return True
            return False

    class Page:
        mouse = _Mouse()

    monkeypatch.setattr(switcher, "_recommend_content_root", lambda _page: (Root(), {}))
    monkeypatch.setattr(switcher.time, "sleep", lambda _seconds: None)

    result = switcher.apply_job_filters(
        Page(),
        job_filter_config={"not_viewed_14d": False, "gender": "女"},
    )

    assert result["ok"] is True
    assert selected_keywords == [["女"], ["刚刚活跃"], ["在线"]]


def test_apply_job_filters_fails_closed_when_required_gender_is_missing(monkeypatch):
    class Element:
        def click(self, **_kwargs):
            return None

    class Root:
        def query_selector(self, _selector):
            return Element()

        def wait_for_selector(self, _selector, **_kwargs):
            return None

        def evaluate(self, _script, arg=None):
            if arg and arg.get("keywords") == ["女"]:
                return None
            return False

    monkeypatch.setattr(switcher, "_recommend_content_root", lambda _page: (Root(), {}))
    monkeypatch.setattr(switcher.time, "sleep", lambda _seconds: None)

    result = switcher.apply_job_filters(
        _Page(),
        job_filter_config={"not_viewed_14d": False, "gender": "女"},
    )

    assert result == {
        "ok": False,
        "reason": "required gender filter not found: 女",
    }
