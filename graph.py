from __future__ import annotations

from pathlib import Path
import os
import sqlite3
import threading

from typing_extensions import TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command, interrupt

from .planner import Planner, finalize
from .store import Store, new_id, now


class AgentState(TypedDict, total=False):
    session_id: str
    plan_id: str
    messages: list[dict]
    assets: list[dict]
    selected_tool: str
    previous: dict | None
    draft: dict
    plan: dict
    job_id: str
    request_id: str


class VideoAgent:
    def __init__(self, store: Store, settings, planner: Planner | None = None):
        self.store, self.settings = store, settings
        self.planner = planner or Planner(settings)
        self.lock = threading.RLock()
        # This app runs locally; tracing of briefs/assets to external services is
        # deliberately disabled. Credentials are never included in AgentState.
        os.environ['LANGSMITH_TRACING'] = 'false'
        os.environ['LANGCHAIN_TRACING_V2'] = 'false'
        self.connection = sqlite3.connect(str(store.root / 'checkpoints.sqlite3'), check_same_thread=False)
        self.checkpointer = SqliteSaver(self.connection)
        builder = StateGraph(AgentState)
        builder.add_node('understand', self._understand)
        builder.add_node('validate', self._validate)
        builder.add_node('review', self._review)
        builder.add_node('enqueue', self._enqueue)
        builder.add_edge(START, 'understand')
        builder.add_edge('understand', 'validate')
        builder.add_conditional_edges('validate', lambda s: 'review' if s['plan']['ready'] else 'end',
                                      {'review': 'review', 'end': END})
        builder.add_edge('review', 'enqueue')
        builder.add_edge('enqueue', END)
        self.graph = builder.compile(checkpointer=self.checkpointer)

    def _understand(self, state):
        return {'draft': self.planner.draft(state['messages'], state['assets'], state['selected_tool'], state.get('previous'))}

    def _validate(self, state):
        plan = finalize(state['draft'], state['assets'], self.settings)
        plan.update(id=state['plan_id'], session_id=state['session_id'], created_at=now(), planner=self.planner.mode)
        self.store.put('plans', plan)
        return {'plan': plan}

    @staticmethod
    def _review(state):
        decision = interrupt({'plan_id': state['plan_id'], 'message': '计划已准备，确认后提交生成任务。'})
        if not isinstance(decision, dict) or not decision.get('request_id'):
            raise ValueError('缺少提交请求编号')
        return {'request_id': decision['request_id']}

    def _enqueue(self, state):
        # No cloud API here. Replaying this node returns the same ledger job.
        return {'job_id': self.store.create_job(state['plan'], state['request_id'])['id']}

    def plan(self, session, assets, text, selected_tool='auto'):
        with self.lock:
            session = self.store.get('sessions', session['id'])
            identity = new_id()
            previous = self.store.get('plans', session['plan_id']) if session.get('plan_id') else None
            messages = list(session['messages']) + [{'role': 'user', 'content': text, 'at': now()}]
            result = self.graph.invoke({'session_id': session['id'], 'plan_id': identity,
                                        'messages': messages, 'assets': [{k: a.get(k) for k in ('id', 'name', 'role', 'kind')} for a in assets],
                                        'selected_tool': selected_tool, 'previous': previous},
                                       {'configurable': {'thread_id': identity}})
            plan = result['plan']
            reply = plan['summary']
            if plan['missing']:
                reply += '\n\n' + '\n'.join(plan['missing'])
            else:
                reply += '\n\n计划已准备，请检查右侧设置后点击“确认并生成”。'
            messages.append({'role': 'assistant', 'content': reply, 'at': now()})
            self.store.update('sessions', session['id'], messages=messages, plan_id=identity,
                              title=plan['title'][:40])
            return plan, reply

    def execute(self, plan_id, request_id):
        with self.lock:
            plan = self.store.get('plans', plan_id)
            if not plan['ready']:
                raise ValueError('计划尚缺少素材或信息')
            session = self.store.get('sessions', plan['session_id'])
            if session.get('plan_id') != plan_id:
                existing = next((j for j in self.store.list('jobs', session['id']) if j['plan_id'] == plan_id), None)
                if existing:
                    return existing
                raise ValueError('此计划已被更新，请使用当前计划')
            existing = next((j for j in self.store.list('jobs', session['id']) if j['plan_id'] == plan_id), None)
            if existing:
                return existing
            result = self.graph.invoke(Command(resume={'request_id': request_id}),
                                       {'configurable': {'thread_id': plan_id}})
            if not result.get('job_id'):
                raise ValueError('未能创建执行任务')
            return self.store.get('jobs', result['job_id'])

    def close(self):
        self.connection.close()
