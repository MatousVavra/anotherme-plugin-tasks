"""Unit tests for the tasks plugin: migration, CRUD, tools, diary extraction."""
import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from conftest import load_plugin_module
from fake_plugin_context import FakePluginContext


class _DbModule:
    def __init__(self, conn):
        self._conn = conn

    def get_db(self):
        return self._conn

    def now_utc(self):
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class _RecordingCtx(FakePluginContext):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.migrations = []

    def register_migration(self, version, sql):
        super().register_migration(version, sql)
        self.migrations.append((version, sql))


class _FakeLLM:
    def __init__(self, content=None, error=None):
        self.content = content
        self.error = error
        self.calls = []

    async def chat(self, messages, system_prompt=None, caller="core", **kwargs):
        self.calls.append(
            {"messages": messages, "system_prompt": system_prompt, "caller": caller}
        )
        if self.error:
            raise self.error
        return self.content


def _make_db():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _make_plugin_and_client(conn=None, llm=None):
    conn = conn or _make_db()
    ctx = _RecordingCtx(db_module=_DbModule(conn), llm_client=llm)
    plugin = load_plugin_module().Plugin()
    plugin.on_load(ctx)
    for version, sql in ctx.migrations:
        conn.executescript(sql)
    app = FastAPI()
    for name, router in ctx.registry.routers:
        app.include_router(router, prefix=f"/plugins/{name}")
    return plugin, TestClient(app), conn, ctx


def _db_tasks(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM tasks").fetchall()]


async def _drain(plugin):
    for _ in range(200):
        if not plugin._bg_tasks:
            return
        await asyncio.sleep(0.01)


def _days(n):
    return (datetime.now(timezone.utc) + timedelta(days=n)).strftime("%Y-%m-%d")


# --- Migration ---


def test_migration_is_idempotent():
    _, _, conn, ctx = _make_plugin_and_client()
    assert ctx.migrations and ctx.migrations[0][0] == 1
    for version, sql in ctx.migrations:
        conn.executescript(sql)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    assert cols == {
        "id", "vault_name", "title", "status", "due_date", "notes",
        "source", "created_at", "updated_at", "completed_at",
    }


# --- Routes: create ---


def test_create_task_returns_full_dict():
    _, client, conn, _ = _make_plugin_and_client()
    resp = client.post(
        "/plugins/fake/tasks",
        json={"title": "Buy milk", "due_date": "2026-01-02", "notes": "2L"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert set(data) == {
        "id", "title", "status", "due_date", "notes", "source",
        "created_at", "updated_at", "completed_at",
    }
    assert data["title"] == "Buy milk"
    assert data["status"] == "inbox"
    assert data["source"] == "manual"
    assert data["completed_at"] is None
    row = conn.execute("SELECT * FROM tasks").fetchone()
    assert row["title"] == "Buy milk"


def test_create_task_requires_title():
    _, client, _, _ = _make_plugin_and_client()
    assert client.post("/plugins/fake/tasks", json={}).status_code == 422
    assert client.post("/plugins/fake/tasks", json={"title": ""}).status_code == 422


def test_create_task_rejects_long_title():
    _, client, _, _ = _make_plugin_and_client()
    resp = client.post("/plugins/fake/tasks", json={"title": "x" * 501})
    assert resp.status_code == 422


def test_create_task_rejects_bad_date():
    _, client, _, _ = _make_plugin_and_client()
    resp = client.post("/plugins/fake/tasks", json={"title": "T", "due_date": "tomorrow"})
    assert resp.status_code == 422


# --- Routes: list ---


def test_list_tasks_orders_due_nulls_last():
    _, client, _, _ = _make_plugin_and_client()
    client.post("/plugins/fake/tasks", json={"title": "Later", "due_date": "2026-01-05"})
    client.post("/plugins/fake/tasks", json={"title": "Sooner", "due_date": "2026-01-01"})
    client.post("/plugins/fake/tasks", json={"title": "No date"})
    data = client.get("/plugins/fake/tasks").json()
    titles = [t["title"] for t in data["tasks"]]
    assert titles == ["Sooner", "Later", "No date"]


def test_list_tasks_status_filter():
    _, client, conn, _ = _make_plugin_and_client()
    client.post("/plugins/fake/tasks", json={"title": "Inbox one"})
    task_id = conn.execute("SELECT id FROM tasks").fetchone()["id"]
    client.put(f"/plugins/fake/tasks/{task_id}", json={"status": "done"})
    client.post("/plugins/fake/tasks", json={"title": "Inbox two"})
    data = client.get("/plugins/fake/tasks", params={"status": "inbox"}).json()
    assert [t["title"] for t in data["tasks"]] == ["Inbox two"]
    data = client.get("/plugins/fake/tasks", params={"status": "done"}).json()
    assert [t["title"] for t in data["tasks"]] == ["Inbox one"]


def test_list_tasks_overdue_filter():
    _, client, _, _ = _make_plugin_and_client()
    client.post("/plugins/fake/tasks", json={"title": "Overdue", "due_date": _days(-2)})
    client.post("/plugins/fake/tasks", json={"title": "Today", "due_date": _days(0)})
    client.post("/plugins/fake/tasks", json={"title": "Future", "due_date": _days(2)})
    data = client.get("/plugins/fake/tasks", params={"overdue": "1"}).json()
    assert [t["title"] for t in data["tasks"]] == ["Overdue"]


def test_list_tasks_rejects_bad_status():
    _, client, _, _ = _make_plugin_and_client()
    assert client.get("/plugins/fake/tasks", params={"status": "bogus"}).status_code == 400


# --- Routes: update / delete ---


def test_update_task_status_transitions():
    _, client, _, _ = _make_plugin_and_client()
    task_id = client.post("/plugins/fake/tasks", json={"title": "T"}).json()["id"]

    done = client.put(f"/plugins/fake/tasks/{task_id}", json={"status": "done"})
    assert done.status_code == 200
    assert done.json()["status"] == "done"
    assert done.json()["completed_at"] is not None

    reopened = client.put(f"/plugins/fake/tasks/{task_id}", json={"status": "todo"})
    assert reopened.status_code == 200
    assert reopened.json()["status"] == "todo"
    assert reopened.json()["completed_at"] is None


def test_update_task_fields():
    _, client, _, _ = _make_plugin_and_client()
    task_id = client.post(
        "/plugins/fake/tasks", json={"title": "Old", "due_date": "2026-01-01"}
    ).json()["id"]
    resp = client.put(
        f"/plugins/fake/tasks/{task_id}",
        json={"title": "New", "notes": "ctx", "due_date": None},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["title"] == "New"
    assert data["notes"] == "ctx"
    assert data["due_date"] is None


def test_update_task_404_and_422():
    _, client, _, _ = _make_plugin_and_client()
    assert client.put("/plugins/fake/tasks/nope", json={"title": "x"}).status_code == 404
    assert client.put(
        "/plugins/fake/tasks/nope", json={"status": "bogus"}
    ).status_code == 422
    assert client.put(
        "/plugins/fake/tasks/nope", json={"due_date": "soon"}
    ).status_code == 422


def test_delete_task():
    _, client, conn, _ = _make_plugin_and_client()
    task_id = client.post("/plugins/fake/tasks", json={"title": "T"}).json()["id"]
    assert client.delete(f"/plugins/fake/tasks/{task_id}").status_code == 200
    assert _db_tasks(conn) == []
    assert client.delete(f"/plugins/fake/tasks/{task_id}").status_code == 404


# --- Tools ---


def test_tool_task_create():
    plugin, _, conn, _ = _make_plugin_and_client()
    result = plugin.task_create({"title": "Write report", "due_date": "2026-01-02"})
    assert result == "Task created: Write report (due 2026-01-02)"
    row = conn.execute("SELECT * FROM tasks").fetchone()
    assert row["status"] == "todo"
    assert row["source"] == "manual"


def test_tool_task_create_validation():
    plugin, _, _, _ = _make_plugin_and_client()
    assert plugin.task_create({"title": ""}).startswith("Error:")
    assert plugin.task_create(
        {"title": "T", "due_date": "2026-02-30"}
    ).startswith("Error:")


def test_tool_task_list():
    plugin, _, conn, _ = _make_plugin_and_client()
    assert plugin.task_list({}) == "No tasks."
    plugin.task_create({"title": "Write report", "due_date": "2026-01-02"})
    plugin.task_create({"title": "No date task"})
    conn.execute("UPDATE tasks SET status = 'done' WHERE due_date IS NULL")
    conn.commit()
    out = plugin.task_list({})
    assert "- 2026-01-02: Write report" in out
    assert "- (no date) No date task [done]" in out


def test_tool_task_complete_by_id_then_title():
    plugin, _, conn, _ = _make_plugin_and_client()
    plugin.task_create({"title": "Write report"})
    task_id = conn.execute("SELECT id FROM tasks").fetchone()["id"]
    assert plugin.task_complete({"id": task_id}) == "Task completed: Write report"
    plugin.task_create({"title": "Buy milk"})
    assert plugin.task_complete({"title": "buy milk"}) == "Task completed: Buy milk"


def test_tool_task_complete_prefix_and_contains():
    plugin, _, conn, _ = _make_plugin_and_client()
    plugin.task_create({"title": "Write annual report"})
    assert plugin.task_complete({"title": "write"}) == "Task completed: Write annual report"
    plugin.task_create({"title": "Quarterly report"})
    assert plugin.task_complete({"title": "quarter"}) == "Task completed: Quarterly report"


def test_tool_task_complete_ambiguous_and_missing():
    plugin, _, _, _ = _make_plugin_and_client()
    plugin.task_create({"title": "Buy milk"})
    plugin.task_create({"title": "Buy bread"})
    assert plugin.task_complete({"title": "buy"}).startswith("Error:")
    assert plugin.task_complete({"title": "no such thing"}).startswith("Error:")
    assert plugin.task_complete({}).startswith("Error:")


def test_tool_task_update():
    plugin, _, _, _ = _make_plugin_and_client()
    plugin.task_create({"title": "Old title"})
    task_id = plugin.db.get_db().execute("SELECT id FROM tasks").fetchone()["id"]
    assert plugin.task_update({"id": task_id, "title": "New title", "status": "done"}).startswith("Task updated:")
    assert plugin.task_update({"id": "nope"}).startswith("Error:")
    assert plugin.task_update({"id": task_id, "status": "bogus"}).startswith("Error:")
    assert plugin.task_update({}).startswith("Error:")


# --- diary_saved extraction ---


@pytest.mark.asyncio
async def test_diary_extraction_happy_path():
    llm = _FakeLLM(
        content='[{"title": "Buy milk", "due_date": "2027-01-02", "notes": "from store"}]'
    )
    plugin, _, conn, ctx = _make_plugin_and_client(llm=llm)
    handler = ctx.event_bus.handlers["diary_saved"][0]
    handler({"content": "Tomorrow I need to buy milk."})
    await _drain(plugin)
    rows = _db_tasks(conn)
    assert len(rows) == 1
    assert rows[0]["source"] == "ai"
    assert rows[0]["status"] == "inbox"
    assert rows[0]["title"] == "Buy milk"
    assert rows[0]["due_date"] == "2027-01-02"
    assert rows[0]["notes"] == "from store"
    assert llm.calls and llm.calls[0]["caller"] == "tasks"


@pytest.mark.asyncio
async def test_diary_extraction_non_list_json_no_crash():
    for payload in ('{"facts": 1}', '"hello"', "not json at all"):
        llm = _FakeLLM(content=payload)
        plugin, _, conn, ctx = _make_plugin_and_client(llm=llm)
        ctx.event_bus.handlers["diary_saved"][0]({"content": "text"})
        await _drain(plugin)
        assert _db_tasks(conn) == []


@pytest.mark.asyncio
async def test_diary_extraction_handles_fenced_json():
    llm = _FakeLLM(content='```json\n[{"title": "Fenced task"}]\n```')
    plugin, _, conn, ctx = _make_plugin_and_client(llm=llm)
    ctx.event_bus.handlers["diary_saved"][0]({"content": "text"})
    await _drain(plugin)
    rows = _db_tasks(conn)
    assert len(rows) == 1
    assert rows[0]["title"] == "Fenced task"


@pytest.mark.asyncio
async def test_diary_extraction_dedupes_against_existing():
    llm = _FakeLLM(
        content='[{"title": "buy milk"}, {"title": "totally unrelated"}]'
    )
    plugin, _, conn, ctx = _make_plugin_and_client(llm=llm)
    plugin.task_create({"title": "Buy milk"})
    ctx.event_bus.handlers["diary_saved"][0]({"content": "I will buy milk tomorrow"})
    await _drain(plugin)
    rows = _db_tasks(conn)
    titles = [r["title"] for r in rows]
    assert titles.count("Buy milk") == 1
    assert "totally unrelated" in titles


@pytest.mark.asyncio
async def test_diary_extraction_dedupes_containment():
    llm = _FakeLLM(content='[{"title": "buy milk and eggs"}]')
    plugin, _, conn, ctx = _make_plugin_and_client(llm=llm)
    plugin.task_create({"title": "Buy milk"})
    ctx.event_bus.handlers["diary_saved"][0]({"content": "text"})
    await _drain(plugin)
    assert len(_db_tasks(conn)) == 1


@pytest.mark.asyncio
async def test_diary_extraction_dedupes_against_recently_created():
    llm = _FakeLLM(content='[{"title": "Recently finished"}]')
    plugin, _, conn, ctx = _make_plugin_and_client(llm=llm)
    plugin.task_create({"title": "Recently finished"})
    task_id = conn.execute("SELECT id FROM tasks").fetchone()["id"]
    plugin.task_complete({"id": task_id})
    ctx.event_bus.handlers["diary_saved"][0]({"content": "text"})
    await _drain(plugin)
    assert len(_db_tasks(conn)) == 1


@pytest.mark.asyncio
async def test_diary_extraction_llm_exception_logged_not_raised():
    llm = _FakeLLM(error=RuntimeError("network down"))
    plugin, _, conn, ctx = _make_plugin_and_client(llm=llm)
    ctx.event_bus.handlers["diary_saved"][0]({"content": "text"})
    await _drain(plugin)
    assert _db_tasks(conn) == []


@pytest.mark.asyncio
async def test_diary_extraction_skips_empty_content():
    llm = _FakeLLM(content='[{"title": "Ghost task"}]')
    plugin, _, conn, ctx = _make_plugin_and_client(llm=llm)
    ctx.event_bus.handlers["diary_saved"][0]({"content": ""})
    ctx.event_bus.handlers["diary_saved"][0]({"mood": "ok"})
    await _drain(plugin)
    assert _db_tasks(conn) == []
    assert llm.calls == []


# --- Context provider / API / registration ---


def test_context_provider_lists_due_and_overdue_capped():
    plugin, _, _, _ = _make_plugin_and_client()
    for i in range(12):
        plugin.task_create({"title": f"Overdue {i}", "due_date": _days(-1)})
    plugin.task_create({"title": "Today thing", "due_date": _days(0)})
    plugin.task_create({"title": "Future thing", "due_date": _days(3)})
    plugin.task_create({"title": "No date thing"})
    done_id = plugin.db.get_db().execute(
        "SELECT id FROM tasks WHERE title = 'Today thing'"
    ).fetchone()["id"]
    plugin.task_complete({"id": done_id})
    out = plugin._tasks_context_provider("hi", "t1", "main")
    assert out.startswith("## Tasks")
    assert out.count("- ") == 10  # 12 overdue capped by LIMIT 10
    assert "Today thing" not in out
    assert "Future thing" not in out
    assert "No date thing" not in out


def test_context_provider_empty_returns_none():
    plugin, _, _, _ = _make_plugin_and_client()
    assert plugin._tasks_context_provider("hi", "t1", "main") is None


def test_api_get_open_tasks():
    plugin, _, conn, _ = _make_plugin_and_client()
    plugin.task_create({"title": "Overdue", "due_date": _days(-1)})
    plugin.task_create({"title": "Today", "due_date": _days(0)})
    plugin.task_create({"title": "Soon", "due_date": _days(3)})
    plugin.task_create({"title": "Far", "due_date": _days(10)})
    plugin.task_create({"title": "No date"})
    task_id = conn.execute("SELECT id FROM tasks WHERE title = 'Today'").fetchone()["id"]
    plugin.task_complete({"id": task_id})
    api = plugin._ctx.registry.apis["tasks"]
    open_tasks = api.get_open_tasks("main", days=7)
    titles = [t["title"] for t in open_tasks]
    assert titles == ["Overdue", "Soon"]
    assert set(open_tasks[0]) == {"id", "title", "status", "due_date", "notes"}


def test_registration_surface():
    _, _, _, ctx = _make_plugin_and_client()
    assert set(ctx.registry.tools) == {
        "fake.task_create", "fake.task_list", "fake.task_complete", "fake.task_update",
    }
    assert ctx.registry.apis.get("tasks") is not None
    fragments = ctx.registry.prompt_fragments.get("chat", [])
    assert any("task tools" in f for f in fragments)
