import json

import pytest

from boss_hr.config_manager import ConfigManager
from database.connection import Database
from database.repositories import UnitOfWork
from feishu.auth import FeishuAuth
from feishu.client import FeishuBitableClient
from feishu.sync_service import FeishuSyncService


class FakeBitableClient:
    def __init__(self):
        self.records = {}
        self.created = []
        self.updated = []
        self.table_id = "tbl001"
        self.job_tables = {}

    def ensure_candidate_table(self, *, job=None):
        return self.job_tables.get(job, self.table_id)

    def list_records(self, table_id, *, page_size=500):
        return list(self.records.get(table_id, {}).values())

    def create_record(self, table_id, fields):
        table_records = self.records.setdefault(table_id, {})
        record_id = f"rec{len(table_records) + 1:03d}"
        record = {"record_id": record_id, "fields": dict(fields)}
        table_records[record_id] = record
        self.created.append((table_id, fields))
        return record_id

    def update_record(self, table_id, record_id, fields):
        table_records = self.records.setdefault(table_id, {})
        table_records[record_id] = {"record_id": record_id, "fields": dict(fields)}
        self.updated.append((table_id, record_id, fields))

    def find_record(self, table_id, fields):
        for record in self.records.get(table_id, {}).values():
            got = record["fields"]
            if got.get("同步键") == fields.get("同步键"):
                return record
            if got.get("BOSS候选人ID") == fields.get("BOSS候选人ID"):
                return record
            if got.get("姓名") == fields.get("姓名") and got.get("岗位") == fields.get("岗位"):
                return record
        return None


def _config(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "app.yaml").write_text(
        "app:\n  database:\n    path: data/hr_agent.db\n",
        encoding="utf-8",
    )
    (config_dir / "feishu.yaml").write_text(
        "feishu:\n"
        "  app_id: app-id\n"
        "  app_secret: app-secret\n"
        "  base_url: https://open.feishu.test\n"
        "  auto_create_table: true\n"
        "  tenant_access_token_cache: data/token.json\n"
        "  bitable:\n"
        "    app_token: app-token\n"
        "    table_id: ''\n"
        "    table_name: 招聘候选人\n"
        "  fields:\n"
        "    boss_id: BOSS候选人ID\n"
        "    name: 姓名\n"
        "    age: 年龄\n"
        "    degree: 学历\n"
        "    work_years: 工作年限\n"
        "    job: 岗位\n"
        "    sync_key: 同步键\n"
        "    company: 公司\n"
        "    current_title: 当前/期望岗位\n"
        "    card_prescreen_status: 卡片筛选状态\n"
        "    card_prescreen_passed: 卡片筛选结果\n"
        "    card_prescreen_reason: 卡片筛选原因\n"
        "    screening_stage: 筛选阶段\n"
        "    rejection_reason: 未通过原因\n"
        "    passed: 简历筛选结果\n"
        "    reason: 简历筛选原因\n"
        "    decision: 筛选结论\n"
        "    greeting_status: 发送状态\n"
        "    was_greeted: 是否已打招呼\n"
        "    greeting_message: 招呼语\n"
        "    greeting_date: 打招呼日期\n"
        "    reply_status: 回复状态\n"
        "    updated_time: 时间\n",
        encoding="utf-8",
    )
    return ConfigManager(config_dir=config_dir, runtime_dir=tmp_path)


def _database_with_candidate(tmp_path):
    cfg = _config(tmp_path)
    db = Database(cfg)
    db.init()
    with UnitOfWork(db.session_factory) as uow:
        candidate = uow.candidates.upsert(
            boss_id="geek-1",
            job="Python工程师",
            name="张三",
            company="候选人公司",
            work_years="5年",
            current_title="Python工程师",
            card_prescreen_reason="卡片条件全部满足",
            card_prescreen_status="passed",
            card_prescreen_passed=True,
        )
        uow.session.flush()
        candidate_id = candidate.id
        uow.analyses.add(
            candidate_id=candidate_id,
            passed=True,
            reason="匹配度高",
            decision="recommend",
            model="test-model",
        )
        uow.greetings.add(
            candidate_id=candidate_id,
            message="您好，方便聊一下吗？",
            status="greeted",
        )
    return cfg, db, candidate_id


def test_feishu_auth_fetches_and_caches_tenant_token(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def read(self):
            return json.dumps({
                "code": 0,
                "tenant_access_token": "tenant-token",
                "expire": 7200,
            }).encode("utf-8")

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["payload"] = json.loads(req.data.decode("utf-8"))
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    auth = FeishuAuth(cfg)
    assert auth.get_tenant_access_token() == "tenant-token"
    assert auth.get_tenant_access_token() == "tenant-token"
    assert captured["url"] == "https://open.feishu.test/open-apis/auth/v3/tenant_access_token/internal"
    assert captured["payload"] == {"app_id": "app-id", "app_secret": "app-secret"}


def test_client_creates_candidate_table_when_missing(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    calls = []

    class FakeAuth:
        def get_tenant_access_token(self):
            return "tenant-token"

    class FakeResponse:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def read(self):
            return json.dumps(self.body, ensure_ascii=False).encode("utf-8")

    def fake_urlopen(req, timeout):
        calls.append((req.get_method(), req.full_url, req.data))
        if req.get_method() == "GET":
            return FakeResponse({"code": 0, "data": {"items": []}})
        payload = json.loads(req.data.decode("utf-8"))
        assert payload["table"]["name"] == "招聘候选人"
        assert [f["field_name"] for f in payload["table"]["fields"]] == [
            "BOSS候选人ID", "姓名", "年龄", "学历", "工作年限", "城市", "期望薪资",
            "岗位", "同步键", "公司", "当前/期望岗位", "卡片筛选状态", "卡片筛选结果", "卡片筛选原因",
            "筛选阶段", "未通过原因", "简历筛选结果", "简历筛选原因", "筛选结论",
            "发送状态", "是否已打招呼", "招呼语", "打招呼日期", "回复状态", "时间",
        ]
        return FakeResponse({"code": 0, "data": {"table": {"table_id": "tbl001"}}})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    client = FeishuBitableClient(cfg, auth=FakeAuth())
    assert client.ensure_candidate_table() == "tbl001"
    assert calls[0][0] == "GET"
    assert calls[1][0] == "POST"


def test_configured_table_id_recovers_by_exact_job_table_name(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    cfg._config["feishu"]["bitable"]["job_tables"] = {
        "初级剪拍": {"table_id": "tbl-stale", "table_name": "初级剪拍候选人"},
    }
    client = FeishuBitableClient(cfg)
    checked = []

    def ensure_fields(table_id):
        checked.append(table_id)
        if table_id == "tbl-stale":
            raise RuntimeError("Feishu API error: {'code': 1254041, 'msg': 'TableIdNotFound'}")

    monkeypatch.setattr(client, "ensure_candidate_fields", ensure_fields)
    monkeypatch.setattr(client, "list_tables", lambda: [
        {"name": "客服候选人", "table_id": "tbl-customer"},
        {"name": "初级剪拍候选人", "table_id": "tbl-current"},
    ])
    assert client.ensure_candidate_table(job="初级剪拍") == "tbl-current"
    assert checked == ["tbl-stale", "tbl-current"]


def test_configured_table_id_does_not_recover_from_permission_error(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    cfg._config["feishu"]["bitable"]["job_tables"] = {
        "初级剪拍": {"table_id": "tbl-locked", "table_name": "初级剪拍候选人"},
    }
    client = FeishuBitableClient(cfg)
    def permission_denied(_table_id):
        raise RuntimeError("Feishu API error: permission denied")
    monkeypatch.setattr(client, "ensure_candidate_fields", permission_denied)
    def unexpected_list():
        raise AssertionError("must not list tables after permission failure")
    monkeypatch.setattr(client, "list_tables", unexpected_list)
    import pytest
    with pytest.raises(RuntimeError, match="permission denied"):
        client.ensure_candidate_table(job="初级剪拍")


def test_sync_service_creates_and_updates_records(tmp_path):
    cfg, db, candidate_id = _database_with_candidate(tmp_path)
    fake_client = FakeBitableClient()
    service = FeishuSyncService(config=cfg, database=db, client=fake_client)

    assert service.sync_candidate(candidate_id) == "created"
    assert len(fake_client.created) == 1
    fields = fake_client.created[0][1]
    assert fields["姓名"] == "张三"
    assert fields["同步键"] == "geek-1|Python工程师"
    assert fields["工作年限"] == "5年"
    assert fields["当前/期望岗位"] == "Python工程师"
    assert fields["卡片筛选状态"] == "passed"
    assert fields["卡片筛选结果"] == "通过"
    assert fields["卡片筛选原因"] == "卡片条件全部满足"
    assert fields["筛选阶段"] == "打招呼"
    assert fields["未通过原因"] == ""
    assert fields["简历筛选结果"] == "通过"
    assert fields["简历筛选原因"] == "匹配度高"
    assert fields["筛选结论"] == "recommend"
    assert fields["发送状态"] == "greeted"
    assert fields["是否已打招呼"] == "是"

    assert service.sync_candidate(candidate_id) == "updated"
    assert len(fake_client.updated) == 1

    with UnitOfWork(db.session_factory) as uow:
        sync = uow.sync_records.latest_for_candidate(candidate_id)
        assert sync.status == "synced"
        assert sync.feishu_record_id == "rec001"


def test_sync_service_routes_candidates_to_job_tables(tmp_path):
    cfg, db, candidate_id = _database_with_candidate(tmp_path)
    cfg._config["feishu"]["bitable"]["job_tables"] = {
        "Python工程师": {"table_id": "tbl-python", "table_name": "Python工程师候选人"},
        "运营": {"table_id": "tbl-operation", "table_name": "运营候选人"},
    }
    with UnitOfWork(db.session_factory) as uow:
        candidate = uow.candidates.upsert(
            boss_id="geek-2",
            job="运营",
            name="李四",
            company="候选人公司",
        )
        uow.session.flush()
        operation_candidate_id = candidate.id
        uow.greetings.add(
            candidate_id=operation_candidate_id,
            message="您好，方便聊一下吗？",
            status="greeted",
        )

    fake_client = FakeBitableClient()
    fake_client.job_tables = {
        "Python工程师": "tbl-python",
        "运营": "tbl-operation",
    }
    service = FeishuSyncService(config=cfg, database=db, client=fake_client)

    assert service.sync_candidate(candidate_id) == "created"
    assert service.sync_candidate(operation_candidate_id) == "created"
    assert [item[0] for item in fake_client.created] == ["tbl-python", "tbl-operation"]

    with UnitOfWork(db.session_factory) as uow:
        python_sync = uow.sync_records.latest_for_candidate(
            candidate_id,
            feishu_table_id="tbl-python",
        )
        operation_sync = uow.sync_records.latest_for_candidate(
            operation_candidate_id,
            feishu_table_id="tbl-operation",
        )
        wrong_table_sync = uow.sync_records.latest_for_candidate(
            candidate_id,
            feishu_table_id="tbl-operation",
        )
        assert python_sync is not None
        assert operation_sync is not None
        assert wrong_table_sync is None


def test_sync_run_candidates_includes_rejected_candidates(tmp_path):
    cfg = _config(tmp_path)
    db = Database(cfg)
    db.init()
    with UnitOfWork(db.session_factory) as uow:
        rejected = uow.candidates.upsert(
            boss_id="geek-reject",
            job="Python工程师",
            run_id="run-1",
            name="未通过候选人",
            card_prescreen_reason="年龄不符合；期望岗位不匹配",
            card_prescreen_status="rejected",
            card_prescreen_passed=False,
        )
        recommended = uow.candidates.upsert(
            boss_id="geek-recommend",
            job="Python工程师",
            run_id="run-1",
            name="推荐候选人",
            card_prescreen_status="passed",
            card_prescreen_passed=True,
        )
        uow.session.flush()
        uow.analyses.add(
            candidate_id=recommended.id,
            run_id="run-1",
            passed=True,
            reason="简历匹配",
            decision="recommend",
        )

    fake_client = FakeBitableClient()
    service = FeishuSyncService(config=cfg, database=db, client=fake_client)

    stats = service.sync_run_candidates(job="Python工程师", run_id="run-1")

    assert stats["total"] == 2
    by_name = {fields["姓名"]: fields for _, fields in fake_client.created}
    assert by_name["未通过候选人"]["筛选阶段"] == "卡片预筛"
    assert by_name["未通过候选人"]["未通过原因"] == "年龄不符合；期望岗位不匹配"
    assert by_name["未通过候选人"]["是否已打招呼"] == "否"
    assert by_name["推荐候选人"]["筛选阶段"] == "简历筛选"


def test_client_rejects_unknown_job_when_job_tables_are_configured(tmp_path):
    cfg = _config(tmp_path)
    cfg._config["feishu"]["bitable"]["job_tables"] = {
        "运营": {"table_id": "tbl-operation", "table_name": "运营候选人"},
    }
    client = FeishuBitableClient(cfg, auth=object())

    with pytest.raises(ValueError, match="拒绝写入默认表"):
        client.table_config_for_job("全职主播")


def test_client_rejects_duplicate_job_table_ids(tmp_path):
    cfg = _config(tmp_path)
    cfg._config["feishu"]["bitable"]["job_tables"] = {
        "运营": {"table_id": "tbl-shared", "table_name": "运营候选人"},
        "全职主播": {"table_id": "tbl-shared", "table_name": "全职主播候选人"},
    }
    client = FeishuBitableClient(cfg, auth=object())

    with pytest.raises(ValueError, match="同时绑定了岗位"):
        client.table_config_for_job("运营")
