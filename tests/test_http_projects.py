from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import MagicMock, patch

import requests

from video_agent_studio.adapters.base import (
    AdapterError, Cancelled, ExecutionContext, NeedsReview, SubmissionUnknown,
)
from video_agent_studio.adapters.http_projects import MotionAdapter, ProductAdapter


def response(payload=None, status=200, chunks=None):
    result = MagicMock()
    result.status_code = status
    result.json.return_value = payload
    result.iter_content.return_value = iter(chunks if chunks is not None else [b'video-data'])
    result.__enter__.return_value = result
    return result


class HTTPProjectAdaptersTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        assets = {}
        for role, filename in [('image', 'product.png'), ('reference', 'reference.png'), ('driver', 'driver.mp4')]:
            path = self.root / filename
            path.write_bytes(b'input')
            assets[role] = {'path': str(path)}
        self.job = dict(id='local', tool='product', options={}, plan={}, remote_job_id='',
                        submission_intent=False, details={}, artifacts=[])
        self.updates = []

        def update(**patch):
            self.updates.append(deepcopy(patch))
            self.job.update(patch)
            return deepcopy(self.job)

        self.ctx = ExecutionContext(
            settings=SimpleNamespace(product_url='http://127.0.0.1:8008',
                                     motion_url='http://127.0.0.1:8001', rh_key='rh-secret',
                                     ds_key='ds-secret', poll_interval=0, http_timeout=2),
            workdir=self.root / 'result', assets=assets, update=update,
            stop_event=threading.Event(),
        )
        self.session = MagicMock()
        self.session.__enter__.return_value = self.session
        factory = patch('video_agent_studio.adapters.http_projects.requests.Session', return_value=self.session)
        factory.start()
        self.addCleanup(factory.stop)

    def test_product_handshake_whitelist_submission_and_fixed_download(self):
        self.job['options'] = {'pipeline': 'multiview', 'notes': '窗边特写', 'automatic': False,
                               'creative_mode': 'ad',
                               'api_key': 'bad', 'download_url': 'https://untrusted.example'}
        self.session.post.return_value = response({'id': 'product-123'})
        self.session.get.side_effect = [response({'token': 'session-token'}),
                                        response({'state': 'completed', 'url': 'https://untrusted.example'}),
                                        response()]
        result = ProductAdapter().execute(self.job, self.ctx)
        post = self.session.post.call_args
        self.assertEqual(post.args[0], 'http://127.0.0.1:8008/api/jobs')
        self.assertEqual(post.kwargs['headers']['X-App-Token'], 'session-token')
        self.assertEqual(post.kwargs['data'], {'pipeline': 'multiview', 'notes': '窗边特写',
                                             'creative_mode': 'commercial', 'automatic': 'true'})
        self.assertIn('image', post.kwargs['files'])
        self.assertEqual(self.updates[0], {'submission_intent': True})
        self.assertEqual(self.updates[1]['remote_job_id'], 'product-123')
        self.assertEqual(self.job['remote_job_id'], 'product-123')
        self.assertEqual(result['quality_status'], 'not_checked')
        self.assertEqual(Path(result['artifacts'][0]['path']).read_bytes(), b'video-data')
        self.assertEqual(self.session.get.call_args.args[0], 'http://127.0.0.1:8008/api/jobs/product-123/video')
        self.assertFalse(self.session.get.call_args.kwargs['allow_redirects'])
        self.assertTrue(self.session.get.call_args.kwargs['stream'])
        self.assertFalse(self.session.trust_env)
        self.assertNotIn('secret', repr(self.updates))

    def test_unknown_initial_submission_is_not_repeated(self):
        self.job['submission_intent'] = True
        for adapter in (ProductAdapter(), MotionAdapter()):
            with self.assertRaises(SubmissionUnknown):
                adapter.execute(self.job, self.ctx)
        self.session.get.assert_not_called()
        self.session.post.assert_not_called()

    def test_create_timeout_preserves_intent_and_never_exposes_response(self):
        self.session.get.return_value = response({'token': 'token'})
        self.session.post.side_effect = requests.Timeout('ds-secret request details')
        with self.assertRaises(SubmissionUnknown) as raised:
            ProductAdapter().execute(self.job, self.ctx)
        self.assertNotIn('secret', str(raised.exception))
        self.assertTrue(self.job['submission_intent'])
        with self.assertRaises(SubmissionUnknown):
            ProductAdapter().execute(self.job, self.ctx)
        self.assertEqual(self.session.post.call_count, 1)

    def test_definite_create_rejection_clears_intent(self):
        self.session.get.return_value = response({'token': 'token'})
        self.session.post.return_value = response({'detail': 'ds-secret'}, status=400)
        with self.assertRaises(AdapterError) as raised:
            ProductAdapter().execute(self.job, self.ctx)
        self.assertFalse(self.job['submission_intent'])
        self.assertNotIn('secret', str(raised.exception))

    def test_5xx_and_invalid_success_are_ambiguous(self):
        for payload, status in [({'detail': 'hidden'}, 503), ({'state': 'running'}, 200)]:
            with self.subTest(status=status):
                self.job.update(submission_intent=False, remote_job_id='')
                self.session.get.return_value = response({'token': 'token'})
                self.session.post.return_value = response(payload, status)
                with self.assertRaises(SubmissionUnknown):
                    ProductAdapter().execute(self.job, self.ctx)
                self.assertTrue(self.job['submission_intent'])

    def test_product_failed_review_unknown_and_interrupted_never_auto_resume(self):
        self.job.update(remote_job_id='original', submission_intent=True)
        for state in ('failed', 'awaiting_review', 'interrupted', 'submission_unknown'):
            with self.subTest(state=state):
                self.session.get.return_value = response({'state': state, 'error': 'rh-secret'})
                with self.assertRaises(NeedsReview) as raised:
                    ProductAdapter().execute(self.job, self.ctx)
                self.assertNotIn('secret', str(raised.exception))
        self.session.post.assert_not_called()
        self.assertNotIn('secret', repr(self.updates))

    def test_product_explicit_resume_targets_original_job(self):
        self.job.update(remote_job_id='original', submission_intent=True, details={'resume_requested': True})
        self.session.post.return_value = response({'id': 'original'})
        self.session.get.side_effect = [response({'state': 'interrupted'}), response({'token': 'new-token'}),
                                        response({'state': 'completed'}), response()]
        ProductAdapter().execute(self.job, self.ctx)
        self.assertEqual(self.session.post.call_args.args[0], 'http://127.0.0.1:8008/api/jobs/original/continue')
        self.assertEqual(self.session.post.call_args.kwargs['json'], {})
        self.assertFalse(self.job['details']['resume_requested'])

    def test_poll_timeout_preserves_id_and_does_not_create(self):
        self.job.update(remote_job_id='known', submission_intent=True)
        self.session.get.side_effect = requests.Timeout('rh-secret')
        with self.assertRaises(AdapterError):
            ProductAdapter().execute(self.job, self.ctx)
        self.assertEqual(self.job['remote_job_id'], 'known')
        self.session.post.assert_not_called()

    def test_cancellation_during_download_removes_partial_and_keeps_id(self):
        self.job.update(remote_job_id='known', submission_intent=True)

        def chunks(_):
            yield b'partial'
            self.ctx.stop_event.set()
            yield b'more'

        download = response()
        download.iter_content.side_effect = chunks
        self.session.get.side_effect = [response({'state': 'completed'}), download]
        with self.assertRaises(Cancelled):
            ProductAdapter().execute(self.job, self.ctx)
        self.assertEqual(list(self.ctx.workdir.iterdir()), [])
        self.assertEqual(self.job['remote_job_id'], 'known')
        self.session.post.assert_not_called()

    def test_download_timeout_is_resumable_without_recreation(self):
        self.job.update(remote_job_id='known', submission_intent=True)
        download = response()
        download.iter_content.side_effect = requests.Timeout('rh-secret')
        self.session.get.side_effect = [response({'state': 'completed'}), download]
        with self.assertRaises(AdapterError) as raised:
            ProductAdapter().execute(self.job, self.ctx)
        self.assertNotIn('secret', str(raised.exception))
        self.assertFalse((self.ctx.workdir / 'product.part').exists())
        self.assertEqual(self.job['remote_job_id'], 'known')
        self.session.post.assert_not_called()

    def test_motion_wan_creation_and_uncertified_output(self):
        self.job.update(tool='motion', options={'motion_prompt': '连贯动作', 'unrecognized': 'ignore',
                                              'seam_transition_mode': 'minimax_h3_all'})
        self.session.post.return_value = response({'success': True, 'task': {'id': 'motion-123'}})
        self.session.get.side_effect = [response({'status': 'RUNNING'}), response({'status': 'SUCCESS'}), response()]
        result = MotionAdapter().execute(self.job, self.ctx)
        post = self.session.post.call_args
        self.assertEqual(post.args[0], 'http://127.0.0.1:8001/api/tasks')
        self.assertEqual(set(post.kwargs['files']), {'reference_image', 'driver_video'})
        self.assertEqual(post.kwargs['data']['quality_audit_enabled'], 'false')
        self.assertEqual(post.kwargs['data']['seam_transition_mode'], 'minimax_h3')
        self.assertNotIn('unrecognized', post.kwargs['data'])
        self.assertEqual(self.job['remote_job_id'], 'motion-123')
        self.assertEqual(result['quality_status'], 'not_checked')
        self.assertNotIn('secret', repr(self.updates))

    def test_motion_h3_action_uses_independent_endpoint_and_quality_options(self):
        self.job.update(tool='motion', options={'backend': 'h3_action', 'quality_enabled': True,
                                              'quality_max_rounds': 2, 'motion_prompt': 'unsupported'})
        self.session.post.return_value = response({'task': {'id': 'h3-123'}})
        self.session.get.side_effect = [response({'status': 'SUCCESS', 'quality_audit_enabled': True,
                                                 'quality_audit_reports': [{'defects': []}]}), response()]
        result = MotionAdapter().execute(self.job, self.ctx)
        post = self.session.post.call_args
        self.assertEqual(post.args[0], 'http://127.0.0.1:8001/api/minimax-h3-action-video-tasks')
        self.assertEqual(post.kwargs['data']['quality_audit_enabled'], 'true')
        self.assertEqual(post.kwargs['data']['quality_audit_max_rounds'], '2')
        self.assertNotIn('motion_prompt', post.kwargs['data'])
        self.assertEqual(result['quality_status'], 'passed')

    def test_motion_success_with_remaining_defects_is_reviewable(self):
        self.job.update(tool='motion', remote_job_id='existing', options={'quality_enabled': True})
        self.session.get.side_effect = [response({'status': 'SUCCESS', 'quality_audit_enabled': True,
                                                 'quality_audit_reports': [{'defects': [{'bad': True}]}],
                                                 'quality_audit_last_defects': {}}), response()]
        result = MotionAdapter().execute(self.job, self.ctx)
        self.assertEqual(result['quality_status'], 'needs_review')
        self.assertEqual(result['details']['quality_defect_count'], 1)
        self.assertTrue(Path(result['artifacts'][0]['path']).is_file())
        self.session.post.assert_not_called()

    def test_clean_final_audit_supersedes_stale_earlier_defects(self):
        self.job.update(tool='motion', remote_job_id='existing', options={'quality_enabled': True})
        self.session.get.side_effect = [response({'status': 'SUCCESS', 'quality_audit_enabled': True,
                                                 'quality_audit_reports': [{'defects': [{'bad': True}]}, {'defects': []}],
                                                 'quality_audit_last_defects': {'segment:0': ['old']}}), response()]
        result = MotionAdapter().execute(self.job, self.ctx)
        self.assertEqual(result['quality_status'], 'passed')

    def test_audit_enabled_without_report_needs_review(self):
        self.job.update(tool='motion', remote_job_id='existing', options={'quality_enabled': True})
        self.session.get.side_effect = [response({'status': 'SUCCESS'}), response()]
        result = MotionAdapter().execute(self.job, self.ctx)
        self.assertEqual(result['quality_status'], 'needs_review')

    def test_motion_failure_requires_explicit_resume_of_original(self):
        self.job.update(tool='motion', remote_job_id='existing')
        self.session.get.return_value = response({'status': 'FAILED', 'error': 'rh-secret'})
        with self.assertRaises(NeedsReview):
            MotionAdapter().execute(self.job, self.ctx)
        self.session.post.assert_not_called()
        self.job['details']['resume_requested'] = True
        self.session.post.return_value = response({'success': True})
        self.session.get.side_effect = [response({'status': 'FAILED'}), response({'status': 'SUCCESS'}), response()]
        MotionAdapter().execute(self.job, self.ctx)
        self.assertEqual(self.session.post.call_args.args[0], 'http://127.0.0.1:8001/api/tasks/existing/resume')
        self.assertFalse(self.job['details']['resume_requested'])

    def test_resume_request_does_not_start_second_active_motion_worker(self):
        self.job.update(tool='motion', remote_job_id='existing', details={'resume_requested': True})
        self.session.get.side_effect = [response({'status': 'RUNNING'}), response({'status': 'SUCCESS'}), response()]
        MotionAdapter().execute(self.job, self.ctx)
        self.session.post.assert_not_called()
        self.assertFalse(self.job['details']['resume_requested'])

    def test_resume_unknown_product_requires_original_workbench_binding(self):
        self.job.update(remote_job_id='existing', details={'resume_requested': True})
        self.session.get.return_value = response({'state': 'submission_unknown'})
        with self.assertRaises(NeedsReview) as raised:
            ProductAdapter().execute(self.job, self.ctx)
        self.assertIn('原工作台', str(raised.exception))
        self.session.post.assert_not_called()
        self.assertFalse(self.job['details']['resume_requested'])

    def test_redirect_is_not_followed_or_treated_as_video(self):
        self.job.update(remote_job_id='known')
        self.session.get.side_effect = [response({'state': 'completed'}), response(status=302)]
        with self.assertRaises(AdapterError):
            ProductAdapter().execute(self.job, self.ctx)
        self.assertFalse(self.session.get.call_args.kwargs['allow_redirects'])
        self.assertFalse((self.ctx.workdir / 'product.mp4').exists())


if __name__ == '__main__':
    unittest.main()
