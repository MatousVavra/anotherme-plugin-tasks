import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

_DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"
_STATUSES = ("inbox", "todo", "done")
_UNSET = object()

TASK_EXTRACT_SYSTEM = """You extract actionable tasks from a diary entry.

Find things the user committed to doing or clearly plans to do in the future —
intentions, todos, plans, and commitments.

Rules:
- ONLY include actionable items the user is going to do (future plans, todos, commitments).
- Do NOT include past events, feelings, opinions, or vague ideas.
- due_date must be YYYY-MM-DD or null. Use dates mentioned in the entry; otherwise null.
- Keep titles short and concrete, starting with a verb when possible.
- Maximum 10 tasks.
- If there is nothing actionable, return an empty array.

Respond ONLY with a JSON array like:
[{"title": "Buy milk", "due_date": "2026-01-02", "notes": "from the corner store"}]

Both "due_date" and "notes" may be null.
"""


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    due_date: Optional[str] = Field(default=None, pattern=_DATE_PATTERN)
    notes: Optional[str] = None


class TaskUpdate(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=500)
    due_date: Optional[str] = Field(default=None, pattern=_DATE_PATTERN)
    notes: Optional[str] = None
    status: Optional[str] = None

    @field_validator("status")
    @classmethod
    def _check_status(cls, v):
        if v is not None and v not in _STATUSES:
            raise ValueError("status must be one of: inbox, todo, done")
        return v


def _validate_date(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, f"Invalid date format: {value!r}, expected YYYY-MM-DD")
    return value


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _parse_ts(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _row_to_dict(r):
    d = dict(r)
    return {
        "id": d.get("id"),
        "title": d.get("title"),
        "status": d.get("status"),
        "due_date": d.get("due_date"),
        "notes": d.get("notes"),
        "source": d.get("source"),
        "created_at": d.get("created_at"),
        "updated_at": d.get("updated_at"),
        "completed_at": d.get("completed_at"),
    }


def _list_tasks(dbm, vault_name, status=None, overdue=False, today=None,
                from_date=None, to_date=None):
    conn = dbm.get_db()
    sql = "SELECT * FROM tasks WHERE vault_name = ?"
    params = [vault_name]
    if status:
        sql += " AND status = ?"
        params.append(status)
    if from_date:
        sql += " AND due_date IS NOT NULL AND due_date >= ?"
        params.append(from_date)
    if to_date:
        sql += " AND due_date IS NOT NULL AND due_date <= ?"
        params.append(to_date)
    if overdue and today:
        sql += " AND status != 'done' AND due_date IS NOT NULL AND due_date < ?"
        params.append(today)
    sql += " ORDER BY due_date IS NULL, due_date, created_at"
    return [_row_to_dict(r) for r in conn.execute(sql, params).fetchall()]


def _get_task(dbm, vault_name, task_id):
    conn = dbm.get_db()
    row = conn.execute(
        "SELECT * FROM tasks WHERE id = ? AND vault_name = ?",
        (task_id, vault_name),
    ).fetchone()
    return _row_to_dict(row) if row is not None else None


def _create_task(dbm, vault_name, task_id, title, status, due_date, notes, now,
                 source="manual"):
    conn = dbm.get_db()
    conn.execute(
        "INSERT INTO tasks (id, vault_name, title, status, due_date, notes, source, "
        "created_at, updated_at, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
        (task_id, vault_name, title, status, due_date, notes, source, now, now),
    )
    conn.commit()


def _update_task(dbm, vault_name, task_id, title=None, due_date=_UNSET, notes=None,
                 status=None, now=None):
    conn = dbm.get_db()
    row = conn.execute(
        "SELECT * FROM tasks WHERE id = ? AND vault_name = ?",
        (task_id, vault_name),
    ).fetchone()
    if row is None:
        return None
    fields = {"updated_at": now}
    if title is not None:
        fields["title"] = title
    if due_date is not _UNSET:
        fields["due_date"] = due_date
    if notes is not None:
        fields["notes"] = notes
    if status is not None:
        fields["status"] = status
        fields["completed_at"] = now if status == "done" else None
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE tasks SET {sets} WHERE id = ? AND vault_name = ?",
        (*fields.values(), task_id, vault_name),
    )
    conn.commit()
    return _row_to_dict(conn.execute(
        "SELECT * FROM tasks WHERE id = ? AND vault_name = ?",
        (task_id, vault_name),
    ).fetchone())


def _delete_task(dbm, vault_name, task_id):
    conn = dbm.get_db()
    cur = conn.execute(
        "DELETE FROM tasks WHERE id = ? AND vault_name = ?",
        (task_id, vault_name),
    )
    conn.commit()
    return cur.rowcount > 0


def _parse_task_items(raw):
    """Parse the LLM response into a list of task dicts. Never raises."""
    try:
        content = (raw or "").strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1].rsplit("\n", 1)[0]
            if content.startswith("json"):
                content = content[4:].strip()
        items = json.loads(content)
    except Exception:
        return []
    if not isinstance(items, list):
        return []
    out = []
    for item in items[:10]:
        if not isinstance(item, dict):
            continue
        title = item.get("title")
        if not isinstance(title, str) or not title.strip():
            continue
        due_date = item.get("due_date")
        if not isinstance(due_date, str):
            due_date = None
        else:
            try:
                datetime.strptime(due_date, "%Y-%m-%d")
            except ValueError:
                due_date = None
        notes = item.get("notes") if isinstance(item.get("notes"), str) else None
        out.append({
            "title": title.strip()[:500],
            "due_date": due_date,
            "notes": notes,
        })
    return out


def _titles_match(a, b):
    a, b = a.lower().strip(), b.lower().strip()
    return a == b or a in b or b in a


def _insert_ai_tasks(dbm, vault_name, items, now):
    """Dedupe extracted tasks against existing non-done tasks and tasks created
    in the last 24h, then insert the rest with source='ai'. Returns titles created."""
    conn = dbm.get_db()
    try:
        cutoff = _parse_ts(now) - timedelta(hours=24)
    except Exception:
        cutoff = None
    rows = conn.execute(
        "SELECT title, status, created_at FROM tasks WHERE vault_name = ?",
        (vault_name,),
    ).fetchall()
    existing = []
    for r in rows:
        if r["status"] != "done":
            existing.append(r["title"])
        elif cutoff is not None:
            try:
                if _parse_ts(r["created_at"]) >= cutoff:
                    existing.append(r["title"])
            except Exception:
                continue
    created = []
    for item in items:
        if any(_titles_match(item["title"], e) for e in existing):
            continue
        _create_task(
            dbm, vault_name, str(uuid.uuid4()), item["title"], "inbox",
            item["due_date"], item["notes"], now, source="ai",
        )
        existing.append(item["title"])
        created.append(item["title"])
    return created


class TaskApi:
    def __init__(self, db_module):
        self._db = db_module

    def get_open_tasks(self, vault_name, days=7):
        """Open (not done) tasks due within the next `days` days, including overdue."""
        end = (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%d")
        rows = self._db.get_db().execute(
            "SELECT id, title, status, due_date, notes FROM tasks "
            "WHERE vault_name = ? AND status != 'done' AND due_date IS NOT NULL "
            "AND due_date <= ? ORDER BY due_date",
            (vault_name, end),
        ).fetchall()
        return [dict(r) for r in rows]


class Plugin:
    def on_load(self, ctx):
        self.db = ctx.db_module
        self.plugin_name = ctx.plugin_name
        self._ctx = ctx
        self._llm = ctx.llm_client
        self._bg_tasks: set = set()

        ctx.register_migration(1, """
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                vault_name TEXT NOT NULL,
                title TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'inbox',
                due_date TEXT,
                notes TEXT,
                source TEXT DEFAULT 'manual',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_tasks_vault ON tasks(vault_name, status, due_date);
        """)

        ctx.register_tool(
            "task_create",
            {
                "type": "function",
                "function": {
                    "description": "Create a task the user needs to do. Use this whenever "
                                   "the user mentions an intention, todo, or something they "
                                   "plan to do. Optional due_date (YYYY-MM-DD) and notes.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "due_date": {"type": "string"},
                            "notes": {"type": "string"},
                        },
                        "required": ["title"],
                    },
                },
            },
            self.task_create,
        )
        ctx.register_tool(
            "task_list",
            {
                "type": "function",
                "function": {
                    "description": "List tasks, optionally filtered by status "
                                   "(inbox/todo/done) and due date range (YYYY-MM-DD).",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "status": {"type": "string", "enum": list(_STATUSES)},
                            "from_date": {"type": "string"},
                            "to_date": {"type": "string"},
                        },
                        "required": [],
                    },
                },
            },
            self.task_list,
        )
        ctx.register_tool(
            "task_complete",
            {
                "type": "function",
                "function": {
                    "description": "Mark a task as done. Match by id first, then by "
                                   "case-insensitive title (exact, prefix, or contains).",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "title": {"type": "string"},
                        },
                        "required": [],
                    },
                },
            },
            self.task_complete,
        )
        ctx.register_tool(
            "task_update",
            {
                "type": "function",
                "function": {
                    "description": "Update a task by id (title, due_date, notes, status).",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "title": {"type": "string"},
                            "due_date": {"type": "string"},
                            "notes": {"type": "string"},
                            "status": {"type": "string", "enum": list(_STATUSES)},
                        },
                        "required": ["id"],
                    },
                },
            },
            self.task_update,
        )

        dbm = self.db

        router = APIRouter()

        @router.get("/tasks")
        async def list_tasks(
            status: Optional[str] = Query(default=None),
            overdue: Optional[str] = Query(default=None),
        ):
            if status is not None and status not in _STATUSES:
                raise HTTPException(
                    400, f"Invalid status: {status!r}, expected one of: inbox, todo, done"
                )
            tasks = await asyncio.to_thread(
                _list_tasks, dbm, self._ctx.vault_name, status, overdue == "1", _today()
            )
            return {"tasks": tasks}

        @router.post("/tasks")
        async def create_task(body: TaskCreate):
            task_id = str(uuid.uuid4())
            now = self.db.now_utc()
            await asyncio.to_thread(
                _create_task, dbm, self._ctx.vault_name, task_id, body.title,
                "inbox", body.due_date, body.notes, now, source="manual",
            )
            return {
                "id": task_id,
                "title": body.title,
                "status": "inbox",
                "due_date": body.due_date,
                "notes": body.notes,
                "source": "manual",
                "created_at": now,
                "updated_at": now,
                "completed_at": None,
            }

        @router.put("/tasks/{task_id}")
        async def update_task(task_id: str, body: TaskUpdate):
            provided = body.model_fields_set
            now = self.db.now_utc()
            task = await asyncio.to_thread(
                _update_task,
                dbm,
                self._ctx.vault_name,
                task_id,
                body.title if "title" in provided else None,
                body.due_date if "due_date" in provided else _UNSET,
                body.notes if "notes" in provided else None,
                body.status,
                now,
            )
            if task is None:
                raise HTTPException(404, "Task not found")
            return task

        @router.delete("/tasks/{task_id}")
        async def delete_task(task_id: str):
            ok = await asyncio.to_thread(
                _delete_task, dbm, self._ctx.vault_name, task_id
            )
            if not ok:
                raise HTTPException(404, "Task not found")
            return {"detail": "Task deleted"}

        ctx.register_api("tasks", TaskApi(dbm))
        ctx.register_router(router)
        ctx.add_prompt_fragment(
            "chat",
            "You have task tools (tasks.task_create, tasks.task_list, tasks.task_complete, "
            "tasks.task_update). Use them when the user mentions something they need to do.",
        )

        ctx.register_context_provider("chat", self._tasks_context_provider)

        ctx.on_event("diary_saved", self._on_diary_saved)

    # --- Context provider ---

    def _tasks_context_provider(self, user_text, thread_id, vault_name):
        """Return pending tasks due today or overdue for chat context."""
        try:
            rows = self.db.get_db().execute(
                "SELECT title, due_date FROM tasks "
                "WHERE vault_name = ? AND status != 'done' "
                "AND due_date IS NOT NULL AND due_date <= ? "
                "ORDER BY due_date LIMIT 10",
                (vault_name, _today()),
            ).fetchall()
            if rows:
                lines = "\n".join(f"- {r['due_date']}: {r['title']}" for r in rows)
                return f"## Tasks\n\n{lines}"
        except Exception:
            logger.exception("Failed to build tasks context")
        return None

    # --- diary_saved extraction ---

    def _spawn(self, coro):
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task

    def _on_diary_saved(self, payload):
        content = payload.get("content", "")
        if not content:
            return
        self._spawn(self._extract_tasks_from_diary(self._ctx.vault_name, content))

    async def _extract_tasks_from_diary(self, vault_name, content):
        try:
            if self._llm is None:
                return
            raw = await self._llm.chat(
                messages=[{"role": "user", "content": content}],
                system_prompt=TASK_EXTRACT_SYSTEM,
                caller="tasks",
            )
            items = _parse_task_items(raw)
            if not items:
                return
            created = await asyncio.to_thread(
                _insert_ai_tasks, self.db, vault_name, items, self.db.now_utc()
            )
            if created:
                logger.info("Extracted %d task(s) from diary: %s",
                            len(created), ", ".join(created))
        except Exception:
            logger.exception("Task extraction from diary failed")

    # --- Tool handlers ---

    def task_create(self, args: dict) -> str:
        title = (args.get("title") or "").strip()
        if not title or len(title) > 500:
            return "Error: title is required (1-500 characters)."
        due_date = args.get("due_date")
        if due_date:
            try:
                datetime.strptime(due_date, "%Y-%m-%d")
            except ValueError:
                return "Error: invalid due_date format, expected YYYY-MM-DD."
        notes = args.get("notes", "")
        _create_task(
            self.db, self._ctx.vault_name, str(uuid.uuid4()), title, "todo",
            due_date, notes, self.db.now_utc(), source="manual",
        )
        suffix = f" (due {due_date})" if due_date else ""
        return f"Task created: {title}{suffix}"

    def task_list(self, args: dict) -> str:
        status = args.get("status")
        if status is not None and status not in _STATUSES:
            return "Error: status must be one of: inbox, todo, done."
        for key in ("from_date", "to_date"):
            if args.get(key):
                try:
                    datetime.strptime(args[key], "%Y-%m-%d")
                except ValueError:
                    return f"Error: invalid {key} format, expected YYYY-MM-DD."
        tasks = _list_tasks(
            self.db, self._ctx.vault_name, status,
            from_date=args.get("from_date"), to_date=args.get("to_date"),
        )
        if not tasks:
            return "No tasks."
        lines = []
        for t in tasks:
            line = f"- {t['due_date']}: {t['title']}" if t["due_date"] else f"- (no date) {t['title']}"
            if t["status"] == "done":
                line += " [done]"
            lines.append(line)
        return "\n".join(lines)

    def task_complete(self, args: dict) -> str:
        ident = (args.get("id") or args.get("title") or "").strip()
        if not ident:
            return "Error: id or title is required."
        vn = self._ctx.vault_name
        task = _get_task(self.db, vn, ident)
        if task is None:
            open_tasks = [t for t in _list_tasks(self.db, vn) if t["status"] != "done"]
            lowered = ident.lower()
            matches = [t for t in open_tasks if t["title"].lower() == lowered]
            if not matches:
                matches = [t for t in open_tasks if t["title"].lower().startswith(lowered)]
            if not matches:
                matches = [t for t in open_tasks if lowered in t["title"].lower()]
            if len(matches) > 1:
                return f"Error: multiple tasks match {ident!r} — be more specific."
            task = matches[0] if matches else None
        if task is None:
            return f"Error: task not found: {ident!r}."
        _update_task(self.db, vn, task["id"], status="done", now=self.db.now_utc())
        return f"Task completed: {task['title']}"

    def task_update(self, args: dict) -> str:
        task_id = (args.get("id") or "").strip()
        if not task_id:
            return "Error: id is required."
        status = args.get("status")
        if status is not None and status not in _STATUSES:
            return "Error: status must be one of: inbox, todo, done."
        due_date = args.get("due_date")
        if due_date:
            try:
                datetime.strptime(due_date, "%Y-%m-%d")
            except ValueError:
                return "Error: invalid due_date format, expected YYYY-MM-DD."
        title = args.get("title")
        if title is not None and (not title.strip() or len(title) > 500):
            return "Error: title must be 1-500 characters."
        task = _update_task(
            self.db, self._ctx.vault_name, task_id,
            title=title, due_date=due_date if due_date is not None else _UNSET,
            notes=args.get("notes"),
            status=status, now=self.db.now_utc(),
        )
        if task is None:
            return "Error: task not found."
        return f"Task updated: {task['title']}"
