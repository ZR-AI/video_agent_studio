"""Real LangGraph/checkpoint and durable ledger regression tests, without APIs."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
from unittest.mock import Mock, patch

import pytest

from video_agent_studio.config import Settings
from video_agent_studio.engine import Engine
from video_agent_studio.graph import VideoAgent
from video_agent_studio.planner import finalize
from video_agent_studio.store import Store


@pytest.fixture
def setup(tmp_path):
    settings = Settings(data_dir=tmp_path, planner_mode='rules', max_workers=1)
    store = Store(tmp_path)
    session = store.create_session()
    asset = {'id': 'product-image', 'session_id': session['id'], 'name': 'product.png',
             'role': 'product', 'kind': 'image', 'path': str(tmp_path / 'product.png')}
    Path(asset['path']).write_bytes(b'image')
    store.put('assets', asset)
    return settings, store, session, [asset]


def _plan(setup):
    settings, store, session, assets = setup
    agent = VideoAgent(store, settings)
    plan, _ = agent.plan(session, assets, '制作10秒商品广告', 'product')
    return agent, plan


def test_interrupt_confirmation_survives_new_store_and_graph_instances(setup):
    settings, store, session, _ = setup
    agent, plan = _plan(setup)
    try:
        pending = agent.graph.get_state({'configurable': {'thread_id': plan['id']}})
        assert pending.next == ('review',)
        assert store.list('jobs') == []
    finally:
        agent.close()

    restarted_store = Store(settings.data_dir)
    restarted = VideoAgent(restarted_store, settings)
    try:
        job = restarted.execute(plan['id'], 'confirmation-after-restart')
        assert job['state'] == 'queued'
        assert job['plan_id'] == plan['id']
        assert job['session_id'] == session['id']
        assert len(restarted_store.list('jobs')) == 1
        assert restarted.graph.get_state({'configurable': {'thread_id': plan['id']}}).next == ()
    finally:
        restarted.close()


def test_enqueue_failure_before_ledger_write_resumes_pending_node_after_restart(setup):
    settings, store, _, _ = setup
    agent, plan = _plan(setup)
    try:
        with patch.object(store, 'create_job', side_effect=OSError('simulated local disk interruption')):
            with pytest.raises(OSError):
                agent.execute(plan['id'], 'original-request')
        assert store.list('jobs') == []
        pending = agent.graph.get_state({'configurable': {'thread_id': plan['id']}})
        assert pending.next == ('enqueue',)
    finally:
        agent.close()

    restarted_store = Store(settings.data_dir)
    restarted = VideoAgent(restarted_store, settings)
    try:
        job = restarted.execute(plan['id'], 'retry-request')
        # The approved request was checkpointed before enqueue failed.
        assert job['request_id'] == 'original-request'
        assert len(restarted_store.list('jobs')) == 1
    finally:
        restarted.close()


def test_enqueue_failure_after_ledger_write_does_not_create_second_job(setup):
    settings, store, _, _ = setup
    agent, plan = _plan(setup)
    original = store.create_job

    def persist_then_fail(*args):
        original(*args)
        raise OSError('interruption before graph checkpoint')

    try:
        with patch.object(store, 'create_job', side_effect=persist_then_fail):
            with pytest.raises(OSError):
                agent.execute(plan['id'], 'first-confirmation')
        original_id = store.list('jobs')[0]['id']
    finally:
        agent.close()

    restarted_store = Store(settings.data_dir)
    restarted = VideoAgent(restarted_store, settings)
    try:
        assert restarted.execute(plan['id'], 'second-confirmation')['id'] == original_id
        assert len(restarted_store.list('jobs')) == 1
    finally:
        restarted.close()


def test_same_plan_with_different_requests_has_one_ledger_job(setup):
    _, store, _, _ = setup
    agent, plan = _plan(setup)
    try:
        first = agent.execute(plan['id'], 'request-a')
        second = agent.execute(plan['id'], 'request-b')
        assert first['id'] == second['id']
        assert first['request_id'] == second['request_id'] == 'request-a'
        assert len(store.list('jobs')) == 1
    finally:
        agent.close()


def test_request_id_cannot_be_reused_for_a_different_plan(setup):
    _, store, session, assets = setup
    agent, first = _plan(setup)
    try:
        agent.execute(first['id'], 'one-request')
        second, _ = agent.plan(session, assets, '改成6秒商品广告', 'product')
        with pytest.raises(ValueError, match='另一个计划'):
            agent.execute(second['id'], 'one-request')
        assert len(store.list('jobs')) == 1
    finally:
        agent.close()


def test_concurrent_dialogue_preserves_both_turns_using_stale_session_snapshots(setup):
    settings, store, session, assets = setup
    agent = VideoAgent(store, settings)
    start = threading.Barrier(3)

    def plan(text):
        start.wait(timeout=5)
        # Both callers deliberately hold the same old session snapshot.
        return agent.plan(session, assets, text, 'product')[0]

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(plan, '制作6秒商品广告')
            second = pool.submit(plan, '制作10秒商品广告')
            start.wait(timeout=5)
            plans = [first.result(timeout=10), second.result(timeout=10)]
        saved = store.get('sessions', session['id'])
        assert len(saved['messages']) == 4
        assert [message['role'] for message in saved['messages']] == ['user', 'assistant', 'user', 'assistant']
        assert {message['content'] for message in saved['messages'] if message['role'] == 'user'} == {
            '制作6秒商品广告', '制作10秒商品广告',
        }
        latest = next(plan for plan in plans if plan['id'] == saved['plan_id'])
        assert latest['summary'] == saved['messages'][-2]['content']
        assert agent.execute(latest['id'], 'latest-confirmation')['plan_id'] == latest['id']
    finally:
        agent.close()


@pytest.mark.parametrize('configured_mode', ['legacy', 'two_pass', 'veda_two_pass', 'latent_two_pass'])
def test_talking_plan_inherits_configured_video_mode(configured_mode):
    settings = Settings(fl2va_video_mode=configured_mode)
    plan = finalize(
        {'tool': 'talking', 'title': '人物口播', 'summary': '按完整文案朗读',
         'options': {'script': '欢迎使用我们的商品。'}, 'questions': []},
        [{'id': 'person', 'role': 'reference', 'name': 'person.png', 'kind': 'image'}], settings,
    )
    assert plan['ready'] is True
    assert plan['options']['video_mode'] == configured_mode


@pytest.mark.parametrize('requested_mode', ['legacy', 'unknown-video-mode'])
def test_incompatible_or_invalid_talking_mode_is_missing_before_confirmation(requested_mode):
    plan = finalize(
        {'tool': 'talking', 'title': '口播', 'summary': '完整文案',
         'options': {'script': '完整文案。', 'video_mode': requested_mode}, 'questions': []},
        [{'id': 'person', 'role': 'reference', 'name': 'person.png', 'kind': 'image'}],
        Settings(fl2va_video_mode='two_pass'),
    )
    assert plan['ready'] is False
    assert any('video_mode' in message or 'two_pass' in message for message in plan['missing'])


def test_latest_asset_per_role_is_bound_and_product_image_has_role_priority(setup):
    settings, store, session, _ = setup
    for identity, role in [('older-person', 'reference'), ('older-product', 'product'),
                           ('latest-product', 'product'), ('latest-person', 'reference'),
                           ('latest-driver', 'driver')]:
        store.put('assets', {'id': identity, 'session_id': session['id'], 'name': identity,
                             'role': role, 'kind': 'video' if role == 'driver' else 'image'})
    assets = store.list('assets', session['id'])
    product = finalize({'tool': 'product', 'title': '商品', 'summary': '广告', 'options': {}}, assets, settings)
    assert product['asset_ids']['image'] == 'latest-product'
    motion = finalize({'tool': 'motion', 'title': '动作', 'summary': '迁移', 'options': {}}, assets, settings)
    assert motion['asset_ids']['reference'] == 'latest-person'
    assert motion['asset_ids']['driver'] == 'latest-driver'
    assert product['ready'] and motion['ready']


def test_product_plan_can_fall_back_to_reference_when_no_product_asset_exists():
    plan = finalize({'tool': 'product', 'title': '商品', 'summary': '广告', 'options': {}},
                    [{'id': 'fallback', 'role': 'reference', 'name': 'image.png', 'kind': 'image'}], Settings())
    assert plan['asset_ids'] == {'image': 'fallback'}
    assert plan['ready'] is True


@pytest.mark.parametrize('remote_job_id', ['', 'backend-already-accepted'])
def test_ledger_restart_retains_submission_record_and_does_not_run_adapter(setup, remote_job_id):
    settings, store, _, _ = setup
    agent, plan = _plan(setup)
    try:
        job = agent.execute(plan['id'], 'approved-once')
    finally:
        agent.close()
    store.update('jobs', job['id'], state='running', submission_intent=True,
                 remote_job_id=remote_job_id, details={'backend_status': 'running'})

    restarted_store = Store(settings.data_dir)
    restarted_store.recover()
    adapter = Mock()
    engine = Engine(restarted_store, settings, adapters={'product': adapter})
    try:
        engine.kick()
    finally:
        engine.close()
    recovered = restarted_store.get('jobs', job['id'])
    assert recovered['state'] == 'interrupted'
    assert recovered['submission_intent'] is True
    assert recovered['remote_job_id'] == remote_job_id
    assert recovered['options'] == plan['options']
    assert recovered['asset_ids'] == plan['asset_ids']
    assert recovered['plan'] == plan
    assert recovered['details']['backend_status'] == 'running'
    adapter.execute.assert_not_called()
