from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
import sqlite3
import uuid


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return uuid.uuid4().hex


class Store:
    """Single-host transactional job ledger, separate from graph checkpoints."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / 'studio.sqlite3'
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS assets (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS plans (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                    plan_id TEXT NOT NULL UNIQUE, request_id TEXT NOT NULL UNIQUE,
                    body TEXT NOT NULL);
            ''')

    def connect(self):
        return sqlite3.connect(self.path, timeout=30)

    def get(self, table: str, identity: str) -> dict:
        self._table(table)
        with self.connect() as db:
            row = db.execute(f'SELECT body FROM {table} WHERE id=?', (identity,)).fetchone()
        if row is None:
            raise KeyError(identity)
        return json.loads(row[0])

    def list(self, table: str, session_id: str | None = None) -> list[dict]:
        self._table(table)
        sql = f'SELECT body FROM {table}'
        args = ()
        if session_id is not None:
            if table == 'sessions':
                raise ValueError('invalid filter')
            sql += ' WHERE session_id=?'
            args = (session_id,)
        with self.connect() as db:
            rows = db.execute(sql + ' ORDER BY rowid DESC', args).fetchall()
        return [json.loads(row[0]) for row in rows]

    @staticmethod
    def _table(table):
        if table not in ('sessions', 'assets', 'plans', 'jobs'):
            raise ValueError('unknown table')

    def put(self, table: str, item: dict) -> dict:
        self._table(table)
        if table == 'jobs':
            raise ValueError('use create_job')
        with self.connect() as db:
            if table == 'sessions':
                db.execute('INSERT OR REPLACE INTO sessions VALUES (?,?)', (item['id'], json.dumps(item, ensure_ascii=False)))
            else:
                db.execute(f'INSERT OR REPLACE INTO {table} VALUES (?,?,?)', (item['id'], item['session_id'], json.dumps(item, ensure_ascii=False)))
        return item

    def update(self, table: str, identity: str, **patch) -> dict:
        self._table(table)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute(f'SELECT body FROM {table} WHERE id=?', (identity,)).fetchone()
            if row is None:
                raise KeyError(identity)
            item = {**json.loads(row[0]), **patch, 'updated_at': now()}
            db.execute(f'UPDATE {table} SET body=? WHERE id=?', (json.dumps(item, ensure_ascii=False), identity))
        return item

    def create_session(self) -> dict:
        return self.put('sessions', dict(id=new_id(), created_at=now(), updated_at=now(),
                                         title='新视频', messages=[], plan_id=None))

    def create_job(self, plan: dict, request_id: str) -> dict:
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT body FROM jobs WHERE plan_id=? OR request_id=?', (plan['id'], request_id)).fetchone()
            if existing:
                job = json.loads(existing[0])
                if job['plan_id'] != plan['id']:
                    raise ValueError('此请求编号已经用于另一个计划')
                return job
            job = dict(id=new_id(), session_id=plan['session_id'], plan_id=plan['id'], request_id=request_id,
                       tool=plan['tool'], plan=plan, options=plan['options'], asset_ids=plan['asset_ids'],
                       state='queued', progress='等待后台执行', error='', remote_job_id='',
                       submission_intent=False, details={}, artifacts=[], quality_status='not_requested',
                       created_at=now(), updated_at=now())
            db.execute('INSERT INTO jobs VALUES (?,?,?,?,?)', (job['id'], job['session_id'], job['plan_id'], request_id,
                                                             json.dumps(job, ensure_ascii=False)))
        return job

    def recover(self):
        # Never automatically replay an in-flight external submission on restart.
        for job in self.list('jobs'):
            if job['state'] == 'running':
                self.update('jobs', job['id'], state='interrupted', progress='服务已重启，请继续已有任务')
