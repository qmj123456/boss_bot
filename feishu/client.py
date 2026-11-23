"""Feishu Bitable OpenAPI client."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Protocol

from boss_hr.config_manager import ConfigManager, get_config
from feishu.auth import FeishuAuth

# 旧评分体系在飞书表里的废弃列（默认列名）。ensure_candidate_fields 会尝试删除。
_DEPRECATED_FIELD_NAMES = {
    "卡片得分",
    "卡片得分原因",
    "打招呼得分",
    "打招呼得分原因",
    "匹配结论",
    "AI分析结果",
    "是否进入打招呼",
}


class BitableClient(Protocol):
    def ensure_candidate_table(self, *, job: str | None = None) -> str:
        ...

    def list_records(self, table_id: str, *, page_size: int = 500) -> list[dict[str, Any]]:
        ...

    def create_record(self, table_id: str, fields: dict[str, Any]) -> str:
        ...

    def batch_create_records(
        self, table_id: str, records: list[dict[str, Any]],
    ) -> list[str]:
        ...

    def update_record(self, table_id: str, record_id: str, fields: dict[str, Any]) -> None:
        ...

    def batch_update_records(
        self, table_id: str, records: list[dict[str, Any]],
    ) -> None:
        ...

    def find_record(self, table_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
        ...


class FeishuBitableClient:
    """Client for Bitable tables and records APIs."""

    def __init__(
        self,
        config: ConfigManager | None = None,
        auth: FeishuAuth | None = None,
    ) -> None:
        self.config = config or get_config()
        self.auth = auth or FeishuAuth(self.config)

    @property
    def base_url(self) -> str:
        return str(self.config.get("feishu.base_url", "https://open.feishu.cn")).rstrip("/")

    @property
    def app_token(self) -> str:
        token = str(self.config.get("feishu.bitable.app_token", "") or "")
        if not token:
            raise ValueError("Missing feishu.bitable.app_token in config/feishu.yaml")
        return token

    @property
    def table_name(self) -> str:
        return str(self.config.get("feishu.bitable.table_name", "招聘候选人") or "招聘候选人")

    def table_config_for_job(self, job: str | None = None) -> tuple[str, str]:
        """Return a job's exact ``(table_id, table_name)`` mapping."""
        job_key = str(job or "").strip()
        job_tables = self.config.get("feishu.bitable.job_tables", {}) or {}
        if job_key and isinstance(job_tables, dict) and job_tables:
            self._validate_job_tables(job_tables)
            if job_key not in job_tables:
                raise ValueError(
                    f"岗位 {job_key!r} 未配置飞书分表；拒绝写入默认表以防止串岗"
                )
        table_config = job_tables.get(job_key) if isinstance(job_tables, dict) else None
        if isinstance(table_config, str):
            return table_config, job_key or self.table_name
        if isinstance(table_config, dict):
            table_id = str(table_config.get("table_id", "") or "")
            table_name = str(table_config.get("table_name", "") or "")
            return table_id, table_name or job_key or self.table_name
        return (
            str(self.config.get("feishu.bitable.table_id", "") or ""),
            self.table_name,
        )

    @staticmethod
    def _validate_job_tables(job_tables: dict[str, Any]) -> None:
        """Reject duplicate explicit IDs or names before any Feishu write."""
        seen_ids: dict[str, str] = {}
        seen_names: dict[str, str] = {}
        for job_name, config in job_tables.items():
            if isinstance(config, str):
                table_id = config.strip()
                table_name = str(job_name).strip()
            elif isinstance(config, dict):
                table_id = str(config.get("table_id", "") or "").strip()
                table_name = str(config.get("table_name", "") or "").strip()
            else:
                raise ValueError(f"岗位 {job_name!r} 的飞书分表配置格式无效")
            if table_id and table_id in seen_ids:
                raise ValueError(
                    f"飞书 table_id {table_id!r} 同时绑定了岗位 "
                    f"{seen_ids[table_id]!r} 和 {job_name!r}"
                )
            if table_name and table_name in seen_names:
                raise ValueError(
                    f"飞书 table_name {table_name!r} 同时绑定了岗位 "
                    f"{seen_names[table_name]!r} 和 {job_name!r}"
                )
            if table_id:
                seen_ids[table_id] = str(job_name)
            if table_name:
                seen_names[table_name] = str(job_name)

    def ensure_candidate_table(self, *, job: str | None = None) -> str:
        """Return configured or discovered table_id for a job, creating it when enabled."""
        configured_table_id, table_name = self.table_config_for_job(job)
        if configured_table_id:
            try:
                self.ensure_candidate_fields(configured_table_id)
                return configured_table_id
            except RuntimeError as exc:
                # A configured ID can become stale after a table is recreated.
                # Only recover from Feishu's explicit table-not-found response;
                # permissions and other API errors must remain visible.
                if "1254041" not in str(exc) and "TableIdNotFound" not in str(exc):
                    raise

        for table in self.list_tables():
            if table.get("name") == table_name:
                table_id = str(table.get("table_id", "") or "")
                self.ensure_candidate_fields(table_id)
                return table_id

        if not bool(self.config.get("feishu.auto_create_table", False)):
            raise RuntimeError(
                "Feishu candidate table not found and feishu.auto_create_table is false"
            )
        return self.create_candidate_table(table_name=table_name)

    def ensure_candidate_fields(self, table_id: str) -> None:
        """给已有数据表补齐缺失字段，并删除旧评分体系的废弃列。"""
        data = self._request(
            "GET",
            f"/open-apis/bitable/v1/apps/{self.app_token}/tables/{table_id}/fields",
        )
        items = (data.get("data") or {}).get("items") or []
        existing = {
            str(item.get("field_name") or ""): str(item.get("field_id") or "")
            for item in items
        }
        schema_names = {field["field_name"] for field in self._candidate_fields_schema()}
        for field in self._candidate_fields_schema():
            if field["field_name"] in existing:
                continue
            self._request(
                "POST",
                f"/open-apis/bitable/v1/apps/{self.app_token}/tables/{table_id}/fields",
                field,
            )
        # 清理旧评分体系遗留在飞书表里的废弃列（卡片得分/打招呼得分等）。
        for field_name, field_id in existing.items():
            if field_name in schema_names or field_name not in _DEPRECATED_FIELD_NAMES:
                continue
            try:
                self._request(
                    "DELETE",
                    f"/open-apis/bitable/v1/apps/{self.app_token}/tables/{table_id}/fields/{field_id}",
                )
            except RuntimeError:
                # 删除失败（权限/字段被引用等）不阻塞同步；列会残留但不再写入。
                pass

    def list_tables(self) -> list[dict[str, Any]]:
        data = self._request("GET", f"/open-apis/bitable/v1/apps/{self.app_token}/tables")
        return list((data.get("data") or {}).get("items") or [])

    def create_candidate_table(self, *, table_name: str | None = None) -> str:
        """Create the default candidate table in an existing Bitable app."""
        payload = {
            "table": {
                "name": table_name or self.table_name,
                "default_view_name": "默认视图",
                "fields": self._candidate_fields_schema(),
            }
        }
        data = self._request("POST", f"/open-apis/bitable/v1/apps/{self.app_token}/tables", payload)
        table = (data.get("data") or {}).get("table") or {}
        table_id = str(table.get("table_id", "") or "")
        if not table_id:
            raise RuntimeError(f"Feishu create table response missing table_id: {data}")
        return table_id

    def list_records(self, table_id: str, *, page_size: int = 500) -> list[dict[str, Any]]:
        """List all records from a table, following page_token."""
        out: list[dict[str, Any]] = []
        page_token = ""
        while True:
            query = {"page_size": str(page_size)}
            if page_token:
                query["page_token"] = page_token
            path = (
                f"/open-apis/bitable/v1/apps/{self.app_token}/tables/{table_id}/records"
                f"?{urllib.parse.urlencode(query)}"
            )
            data = self._request("GET", path)
            node = data.get("data") or {}
            out.extend(list(node.get("items") or []))
            if not node.get("has_more"):
                break
            page_token = str(node.get("page_token", "") or "")
            if not page_token:
                break
        return out

    def create_record(self, table_id: str, fields: dict[str, Any]) -> str:
        payload = {"fields": fields}
        data = self._request(
            "POST",
            f"/open-apis/bitable/v1/apps/{self.app_token}/tables/{table_id}/records",
            payload,
        )
        record = (data.get("data") or {}).get("record") or {}
        record_id = str(record.get("record_id", "") or "")
        if not record_id:
            raise RuntimeError(f"Feishu create record response missing record_id: {data}")
        return record_id

    def batch_create_records(
        self, table_id: str, records: list[dict[str, Any]],
    ) -> list[str]:
        """批量创建记录，单次最多 500 条。返回 record_id 列表。

        Args:
            records: 每项为 {"fields": {...}} 格式的待创建记录。
        Returns:
            record_id 列表（顺序与输入对齐）。
        """
        out: list[str] = []
        BATCH = 500
        for i in range(0, len(records), BATCH):
            chunk = records[i:i + BATCH]
            data = self._request(
                "POST",
                f"/open-apis/bitable/v1/apps/{self.app_token}/tables/{table_id}/records/batch_create",
                {"records": chunk},
            )
            items = list((data.get("data") or {}).get("records") or [])
            for item in items:
                out.append(str(item.get("record_id", "") or ""))
        return out

    def update_record(self, table_id: str, record_id: str, fields: dict[str, Any]) -> None:
        self._request(
            "PUT",
            f"/open-apis/bitable/v1/apps/{self.app_token}/tables/{table_id}/records/{record_id}",
            {"fields": fields},
        )

    def batch_update_records(
        self, table_id: str, records: list[dict[str, Any]],
    ) -> None:
        """批量更新记录，单次最多 500 条。

        Args:
            records: 每项为 {"record_id": "...", "fields": {...}} 格式。
        """
        BATCH = 500
        for i in range(0, len(records), BATCH):
            chunk = records[i:i + BATCH]
            self._request(
                "POST",
                f"/open-apis/bitable/v1/apps/{self.app_token}/tables/{table_id}/records/batch_update",
                {"records": chunk},
            )

    def find_record(self, table_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
        """优先按 BOSS 唯一 ID 查找记录，兼容旧表时再按姓名和岗位兜底。"""
        boss_id_field = self.config.get("feishu.fields.boss_id", "BOSS候选人ID")
        name_field = self.config.get("feishu.fields.name", "姓名")
        job_field = self.config.get("feishu.fields.job", "岗位")
        sync_key_field = self.config.get("feishu.fields.sync_key", "同步键")
        expected_sync_key = fields.get(sync_key_field)
        expected_boss_id = fields.get(boss_id_field)
        expected_name = fields.get(name_field)
        expected_job = fields.get(job_field)
        if not expected_sync_key and not expected_boss_id and (not expected_name or not expected_job):
            return None
        for record in self.list_records(table_id):
            got = record.get("fields") or {}
            if expected_sync_key and got.get(sync_key_field) == expected_sync_key:
                return record
            if expected_boss_id and got.get(boss_id_field) == expected_boss_id:
                return record
            if got.get(name_field) == expected_name and got.get(job_field) == expected_job:
                return record
        return None

    def _candidate_fields_schema(self) -> list[dict[str, Any]]:
        fields = self.config.section("feishu").get("fields", {})
        boss_id = fields.get("boss_id", "BOSS候选人ID")
        name = fields.get("name", "姓名")
        age = fields.get("age", "年龄")
        degree = fields.get("degree", "学历")
        work_years = fields.get("work_years", "工作年限")
        city = fields.get("city", "城市")
        expected_salary = fields.get("expected_salary", "期望薪资")
        job = fields.get("job", "岗位")
        sync_key = fields.get("sync_key", "同步键")
        company = fields.get("company", "公司")
        current_title = fields.get("current_title", "当前/期望岗位")
        card_prescreen_status = fields.get("card_prescreen_status", "卡片筛选状态")
        card_prescreen_passed = fields.get("card_prescreen_passed", "卡片筛选结果")
        card_prescreen_reason = fields.get("card_prescreen_reason", "卡片筛选原因")
        screening_stage = fields.get("screening_stage", "筛选阶段")
        rejection_reason = fields.get("rejection_reason", "未通过原因")
        passed = fields.get("passed", "简历筛选结果")
        reason = fields.get("reason", "简历筛选原因")
        decision = fields.get("decision", "筛选结论")
        greeting_status = fields.get("greeting_status", "发送状态")
        was_greeted = fields.get("was_greeted", "是否已打招呼")
        greeting_message = fields.get("greeting_message", "招呼语")
        greeting_date = fields.get("greeting_date", "打招呼日期")
        reply_status = fields.get("reply_status", "回复状态")
        updated_time = fields.get("updated_time", "时间")
        return [
            {"field_name": boss_id, "type": 1},
            {"field_name": name, "type": 1},
            {"field_name": age, "type": 2},
            {"field_name": degree, "type": 1},
            {"field_name": work_years, "type": 1},
            {"field_name": city, "type": 1},
            {"field_name": expected_salary, "type": 1},
            {"field_name": job, "type": 1},
            {"field_name": sync_key, "type": 1},
            {"field_name": company, "type": 1},
            {"field_name": current_title, "type": 1},
            {"field_name": card_prescreen_status, "type": 1},
            {"field_name": card_prescreen_passed, "type": 1},
            {"field_name": card_prescreen_reason, "type": 1},
            {"field_name": screening_stage, "type": 1},
            {"field_name": rejection_reason, "type": 1},
            {"field_name": passed, "type": 1},
            {"field_name": reason, "type": 1},
            {"field_name": decision, "type": 1},
            {"field_name": greeting_status, "type": 1},
            {"field_name": was_greeted, "type": 1},
            {"field_name": greeting_message, "type": 1},
            {"field_name": greeting_date, "type": 5},
            {"field_name": reply_status, "type": 1},
            {"field_name": updated_time, "type": 5},
        ]

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        token = self.auth.get_tenant_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Feishu API HTTP {exc.code}: {detail[:500]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Feishu API request failed: {exc.reason}") from exc
        body = json.loads(raw)
        if int(body.get("code", -1)) != 0:
            raise RuntimeError(f"Feishu API error: {body}")
        return body


__all__ = ["BitableClient", "FeishuBitableClient"]
