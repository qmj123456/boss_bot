import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from boss_hr.application import fetch_service
from shared import boss_job_switcher


DOWNLOAD_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "boss-recommend-downloader"
    / "scripts"
    / "recommend_download.py"
)


def _load_download_module():
    spec = importlib.util.spec_from_file_location(
        "recommend_download_reliability", DOWNLOAD_SCRIPT
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_online_resume_timeout_allows_human_paced_capture():
    assert fetch_service._calculate_download_timeout(0) == 3600
    assert fetch_service._calculate_download_timeout(25) == 8700
    assert fetch_service._calculate_download_timeout(30) == 10200


def test_atomic_run_artifact_survives_incremental_updates(tmp_path):
    module = _load_download_module()
    target = tmp_path / "process" / "new_resumes.json"

    module._atomic_write_json(target, [{"id": "first"}])
    module._atomic_write_json(target, [{"id": "first"}, {"id": "second"}])

    assert json.loads(target.read_text(encoding="utf-8")) == [
        {"id": "first"},
        {"id": "second"},
    ]
    assert not list(target.parent.glob("*.tmp"))


def test_job_switch_does_not_accept_explicit_wrong_jobid_by_name(monkeypatch):
    class Page:
        url = "https://www.zhipin.com/web/chat/recommend"

    page = Page()
    monkeypatch.setattr(
        boss_job_switcher, "_current_recommend_jobid", lambda _page: "wrong-job-id"
    )
    monkeypatch.setattr(
        boss_job_switcher, "_recommend_content_root", lambda _page: (object(), {})
    )
    monkeypatch.setattr(
        boss_job_switcher, "_click_job_dropdown", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(
        boss_job_switcher, "_click_job_option", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(
        boss_job_switcher, "_top_bar_contains", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(boss_job_switcher.time, "sleep", lambda _seconds: None)

    result = boss_job_switcher.ensure_recommend_job_selected(
        page, encrypt_job_id="expected-job-id", job_name="同名岗位"
    )

    assert result["ok"] is False
    assert result["reason"] == "target job was clicked but jobid mismatch after switch"
    assert result["current_jobid"] == "wrong-job-id"


def test_job_switch_does_not_accept_name_only_when_jobid_is_unavailable(monkeypatch):
    class Page:
        url = "https://www.zhipin.com/web/chat/recommend"

    page = Page()
    monkeypatch.setattr(
        boss_job_switcher, "_current_recommend_jobid", lambda _page: ""
    )
    monkeypatch.setattr(
        boss_job_switcher, "_recommend_content_root", lambda _page: (object(), {})
    )
    monkeypatch.setattr(
        boss_job_switcher, "_click_job_dropdown", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(
        boss_job_switcher, "_click_job_option", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(
        boss_job_switcher, "_top_bar_contains", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(boss_job_switcher.time, "sleep", lambda _seconds: None)

    result = boss_job_switcher.ensure_recommend_job_selected(
        page, encrypt_job_id="expected-job-id", job_name="目标岗位"
    )

    assert result["ok"] is False
    assert result["reason"] == "target jobid unavailable after switch"


def test_job_switch_name_fallback_requires_explicit_opt_in(monkeypatch):
    class Page:
        url = "https://www.zhipin.com/web/chat/recommend"

    page = Page()
    monkeypatch.setattr(
        boss_job_switcher, "_current_recommend_jobid", lambda _page: ""
    )
    monkeypatch.setattr(
        boss_job_switcher, "_recommend_content_root", lambda _page: (object(), {})
    )
    monkeypatch.setattr(
        boss_job_switcher, "_click_job_dropdown", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(
        boss_job_switcher, "_click_job_option", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(
        boss_job_switcher, "_top_bar_contains", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(boss_job_switcher.time, "sleep", lambda _seconds: None)

    result = boss_job_switcher.ensure_recommend_job_selected(
        page,
        encrypt_job_id="expected-job-id",
        job_name="目标岗位",
        require_exact_job_id=False,
    )

    assert result["ok"] is True


def test_fetch_continues_with_completed_screenshots_after_timeout(tmp_path, monkeypatch):
    process_dir = tmp_path / "process"
    process_dir.mkdir()
    paths = {
        "runs_dir": str(tmp_path),
        "process_dir": str(process_dir),
        "candidate_list_file": str(process_dir / "recommend_geek_ids.json"),
        "card_prescreen_rejected_file": str(process_dir / "card_prescreen_rejected.json"),
        "card_prescreen_summary_file": str(process_dir / "card_prescreen_summary.json"),
        "new_resumes_file": str(process_dir / "new_resumes.json"),
        "failed_resumes_file": str(process_dir / "failed_resumes.json"),
    }

    class Lock:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    calls = []

    def run_cli(tool, _args, **_kwargs):
        calls.append(tool)
        if tool == "recommend_list":
            Path(paths["candidate_list_file"]).write_text(
                json.dumps([{"encryptGeekId": "gid-1"}]), encoding="utf-8"
            )
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        Path(paths["new_resumes_file"]).write_text(
            json.dumps([{"ok": True, "resume_screenshots": ["complete.jpg"]}]),
            encoding="utf-8",
        )
        Path(paths["failed_resumes_file"]).write_text("[]", encoding="utf-8")
        raise subprocess.TimeoutExpired(["recommend_download"], timeout=3600)

    monkeypatch.setattr(
        fetch_service, "_pre_check", lambda *_args: (0, None, paths)
    )
    monkeypatch.setattr(fetch_service, "BossPageLock", Lock)
    monkeypatch.setattr(
        fetch_service, "ensure_browser_ready",
        lambda **_kwargs: SimpleNamespace(ok=True),
    )
    monkeypatch.setattr(fetch_service.legacy_runner, "run_legacy_cli", run_cli)

    result = fetch_service.fetch_candidates(
        job_name="测试岗位", encrypt_job_id="job-1", run_id="run-1", count=1
    )

    assert calls == ["recommend_list", "recommend_download"]
    assert result.ok is True
    assert result.data["downloaded_count"] == 1
    assert result.data["download_partial"] is True
    assert result.next_action == "automate"


def test_transactional_fetch_runs_live_flow_without_second_card_lookup(tmp_path, monkeypatch):
    process_dir = tmp_path / "process"
    process_dir.mkdir()
    paths = {
        "runs_dir": str(tmp_path),
        "process_dir": str(process_dir),
        "candidate_list_file": str(process_dir / "recommend_geek_ids.json"),
        "card_prescreen_rejected_file": str(process_dir / "card_prescreen_rejected.json"),
        "card_prescreen_summary_file": str(process_dir / "card_prescreen_summary.json"),
        "new_resumes_file": str(process_dir / "new_resumes.json"),
        "failed_resumes_file": str(process_dir / "failed_resumes.json"),
    }

    class Lock:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    calls = []

    def run_cli(tool, args, **_kwargs):
        calls.append((tool, list(args)))
        assert tool == "recommend_list"
        assert "--transactional-live" in args
        Path(paths["candidate_list_file"]).write_text(
            json.dumps([{"encryptGeekId": "gid-live"}]), encoding="utf-8"
        )
        Path(paths["new_resumes_file"]).write_text(json.dumps([{
            "ok": True,
            "_meta": {"encrypt_geek_id": "gid-live"},
            "resume_screenshots": ["complete.jpg"],
        }]), encoding="utf-8")
        Path(paths["failed_resumes_file"]).write_text("[]", encoding="utf-8")
        (process_dir / "screening_results.json").write_text(json.dumps({
            "candidates": [{"encrypt_geek_id": "gid-live", "passed": True}],
        }), encoding="utf-8")
        (process_dir / "greet_log.json").write_text(json.dumps({
            "results": [{
                "encrypt_geek_id": "gid-live", "status": "greeted",
                "message_sent": True,
            }],
        }), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(fetch_service, "_pre_check", lambda *_args: (0, None, paths))
    monkeypatch.setattr(fetch_service, "BossPageLock", Lock)
    monkeypatch.setattr(
        fetch_service, "ensure_browser_ready", lambda **_kwargs: SimpleNamespace(ok=True)
    )
    monkeypatch.setattr(fetch_service.legacy_runner, "run_legacy_cli", run_cli)
    monkeypatch.setattr(
        "boss_hr.application.greet_service._get_greet_message", lambda _job: "固定话术"
    )

    result = fetch_service.fetch_candidates(
        job_name="测试岗位", encrypt_job_id="job-1", run_id="run-1", count=1,
        transactional=True, max_greetings=1,
    )

    assert [tool for tool, _ in calls] == ["recommend_list"]
    assert result.ok is True
    assert result.data["live_card_flow"] is True
    assert result.data["analyzed_count"] == 1
    assert result.data["greeted_count"] == 1
    assert result.data["message_sent_count"] == 1


def test_current_resume_clicks_greet_then_continue_before_sending(monkeypatch):
    module = _load_download_module()
    monkeypatch.setattr(module.time, "sleep", lambda *_args, **_kwargs: None)

    class DetailContext:
        def __init__(self):
            self.greeted = False
            self.continue_clicked = False
            self.current_action = ""

        def evaluate(self, _script, labels):
            if "打招呼" in labels and not self.greeted:
                self.current_action = "打招呼"
                return {"selector": "[data-action]", "text": "打招呼"}
            if any(label in labels for label in ("继续沟通", "继续聊")) and self.greeted:
                self.current_action = "继续沟通"
                return {"selector": "[data-action]", "text": "继续沟通"}
            return None

        def query_selector(self, _selector):
            context = self

            class Button:
                def click(self, timeout=None):
                    if context.current_action == "打招呼":
                        context.greeted = True
                    elif context.current_action == "继续沟通":
                        context.continue_clicked = True

            return Button()

    detail = DetailContext()
    send_calls = []

    def send_message(*args, **kwargs):
        send_calls.append(kwargs)
        assert detail.continue_clicked is True
        return {"sent": True, "reason": "sent"}

    monkeypatch.setattr(module, "_send_custom_message", send_message)
    result = module._greet_current_resume(
        object(), detail, name="测试候选人", geek_id="gid-1", message="固定话术"
    )

    assert detail.greeted is True
    assert detail.continue_clicked is True
    assert result["status"] == "greeted"
    assert result["message_sent"] is True
    assert result["resume_action"] == "继续沟通"
    assert send_calls[0]["open_chat_if_needed"] is False


def test_current_resume_continue_state_opens_chat_and_recovers_message(monkeypatch):
    module = _load_download_module()
    monkeypatch.setattr(module.time, "sleep", lambda *_args, **_kwargs: None)

    class Button:
        clicked = False

        def click(self, timeout=None):
            self.clicked = True

    button = Button()

    class DetailContext:
        def evaluate(self, _script, labels):
            if any(label in labels for label in ("继续沟通", "继续聊")):
                return {"selector": "[data-action]", "text": "继续沟通"}
            return None

        def query_selector(self, _selector):
            return button

    monkeypatch.setattr(
        module, "_send_custom_message",
        lambda *args, **kwargs: {"sent": True, "reason": "sent"},
    )
    result = module._greet_current_resume(
        object(), DetailContext(), name="待补话术候选人", geek_id="gid-2", message="固定话术"
    )

    assert button.clicked is True
    assert result["status"] == "greeted"
    assert result["clicked"] is False
    assert result["message_sent"] is True


def test_candidate_level_failure_does_not_stop_remaining_cards():
    module = _load_download_module()

    assert module._is_fatal_candidate_error("当前 DOM 未找到目标候选人卡片") is False
    assert module._is_fatal_candidate_error("DeepSeek 连续 3 次分析失败") is False
    assert module._is_fatal_candidate_error("固定话术发送失败：input_not_found") is False
    assert module._is_fatal_candidate_error("检测到验证码") is True
    assert module._is_fatal_candidate_error("推荐牛人页未加载出候选人 iframe") is True
