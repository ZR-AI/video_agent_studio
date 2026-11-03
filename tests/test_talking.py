import copy
import io
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import patch

from video_agent_studio.adapters.base import AdapterError, Cancelled, ExecutionContext, NeedsReview, SubmissionUnknown
from video_agent_studio.adapters.talking import TalkingAdapter, _Run


class TalkingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.reference = root / 'reference.png'
        self.reference.write_bytes(b'image')
        self.job = {'options': {'script': '欢迎使用我们的商品。', 'aspect': '9:16'}, 'details': {}}
        self.saved = []
        self.settings = SimpleNamespace(
            fl2va_url='http://localhost:8900', rh_key='test-secret', ds_key='test-ds',
            rh_base='https://www.runninghub.cn', ds_base='https://api.deepseek.com',
            ds_model='deepseek-chat', http_timeout=1, poll_interval=0,
            fl2va_workflow_id='', fl2va_video_mode='legacy', seedvc_workflow_id='',
            repo_root=Path(__file__).resolve().parents[2],
        )
        self.ctx = ExecutionContext(self.settings, root / 'work',
            {'reference': {'path': str(self.reference)}}, self.update, threading.Event())

    def update(self, **patches):
        self.job.update(copy.deepcopy(patches))
        self.saved.append(copy.deepcopy(self.job))
        return copy.deepcopy(self.job)

    def test_paid_intent_precedes_request_and_task_id_precedes_poll(self):
        run = _Run(self.job, self.ctx)
        def api(*args):
            self.assertTrue(self.job['details']['steps']['segment:1']['intent'])
            return 200, {'taskId': 'paid-1'}
        with patch.object(run, 'api', side_effect=api):
            self.assertEqual(run.paid('segment:1', 'run', {}), 'paid-1')
        self.assertEqual(self.job['details']['steps']['segment:1']['task_id'], 'paid-1')
        self.assertFalse(self.job['details']['steps']['segment:1']['intent'])

    def test_ambiguous_submission_survives_restart_without_resubmitting(self):
        run = _Run(self.job, self.ctx)
        with patch.object(run, 'api', side_effect=TimeoutError):
            with self.assertRaises(SubmissionUnknown):
                run.paid('segment:1', 'run', {})
        resumed = _Run(copy.deepcopy(self.job), self.ctx)
        with patch.object(resumed, 'api') as api:
            with self.assertRaises(SubmissionUnknown):
                resumed.paid('segment:1', 'run', {})
            api.assert_not_called()

    def test_rejected_queue_can_be_retried(self):
        run = _Run(self.job, self.ctx)
        with patch.object(run, 'api', side_effect=[(429, {'code': 'QUEUE_LIMIT'}), (200, {'taskId': 'queued'})]) as api:
            with self.assertRaises(AdapterError):
                run.paid('segment:1', 'run', {})
            self.assertFalse(self.job['details']['steps']['segment:1']['intent'])
            self.assertEqual(run.paid('segment:1', 'run', {}), 'queued')
            self.assertEqual(api.call_count, 2)

    def test_cancel_during_post_still_saves_accepted_task_id(self):
        run = _Run(self.job, self.ctx)
        def api(*args):
            self.ctx.stop_event.set()
            return 200, {'taskId': 'accepted'}
        with patch.object(run, 'api', side_effect=api):
            run.paid('segment:1', 'run', {})
        self.assertEqual(self.job['details']['steps']['segment:1']['task_id'], 'accepted')
        with self.assertRaises(Cancelled):
            run.wait('segment:1', 'accepted')

    def test_cancel_before_connection_clears_intent_without_post(self):
        def update(**patches):
            result = self.update(**patches)
            self.ctx.stop_event.set()
            return result
        self.ctx.update = update
        run = _Run(self.job, self.ctx)
        with patch.object(run.opener, 'open') as connection:
            with self.assertRaises(Cancelled):
                run.paid('segment:1', 'run', {})
            connection.assert_not_called()
        self.assertFalse(self.job['details']['steps']['segment:1']['intent'])

    def test_remote_failure_never_creates_replacement(self):
        run = _Run(self.job, self.ctx)
        run.steps['segment:1'] = {'task_id': 'failed-1', 'state': 'failed'}
        with patch.object(run, 'api') as api:
            self.assertEqual(run.paid('segment:1', 'run', {}), 'failed-1')
            with self.assertRaises(AdapterError):
                run.wait('segment:1', 'failed-1')
            api.assert_not_called()

    def test_downloaded_segment_is_reused_without_network(self):
        run = _Run(self.job, self.ctx)
        output = self.ctx.workdir / 'seg_001.mp4'
        output.write_bytes(b'existing video')
        run.steps['segment:1'] = {'task_id': 'done-1', 'state': 'downloaded'}
        with patch.object(run, 'api') as api:
            self.assertEqual(run.download('segment:1', 'done-1', output, 'video'), output)
            api.assert_not_called()

    def test_product_composite_requires_review_and_reuses_task(self):
        run = _Run(self.job, self.ctx)
        holding = self.ctx.workdir / 'holding.png'
        holding.write_bytes(b'holding image')
        with patch.object(run, 'paid', return_value='image-task') as paid, \
                patch.object(run, 'download', return_value=holding), \
                patch.object(run, 'upload', return_value='uploaded-holding'):
            with self.assertRaises(NeedsReview):
                run.composite('person', 'product', '9:16 (Portrait Widescreen)')
            self.assertEqual(len(self.job['artifacts']), 2)
            run.details['frames_approved'] = True
            self.assertEqual(run.composite('person', 'product', '9:16 (Portrait Widescreen)'), 'uploaded-holding')
            self.assertEqual(paid.call_count, 2)  # paid() itself reuses the persisted task.

    def test_complete_server_worker_and_resume_do_not_repeat_paid_steps(self):
        self.settings.repo_root = Path(self.temp.name) / 'no_original_projects'
        calls = []
        def api(run, endpoint, body=None, raw=None, content_type=None):
            calls.append((endpoint, body))
            if endpoint == 'split':
                return 200, {'segments': [{'text': '欢迎使用我们的商品。', 'product': False}]}
            if endpoint == 'upload':
                return 200, {'fileName': 'uploaded-image'}
            if endpoint == 'run':
                self.assertEqual(body['aspect'], '4')
                return 200, {'taskId': 'video-1'}
            if endpoint == 'query':
                return 200, {'status': 'SUCCESS'}
            self.fail('unexpected endpoint: ' + endpoint)
        def ffmpeg(run, args):
            Path(args[-1]).write_bytes(b'merged video')
        def transfer(run, url, path, headers):
            path.write_bytes(b'video bytes')
        with patch.object(_Run, 'api', new=api), patch.object(_Run, 'ffmpeg', new=ffmpeg), \
                patch.object(_Run, 'transfer', new=transfer):
            result = TalkingAdapter().execute(copy.deepcopy(self.job), self.ctx)
            self.assertTrue(Path(result['artifacts'][0]['path']).is_file())
            self.assertEqual(result['quality_status'], 'not_requested')
            TalkingAdapter().execute(copy.deepcopy(self.job), self.ctx)
        self.assertEqual(sum(endpoint == 'run' for endpoint, _ in calls), 1)
        self.assertEqual(sum(endpoint == 'split' for endpoint, _ in calls), 1)

    def test_model_cannot_drop_or_rewrite_script(self):
        run = _Run(self.job, self.ctx)
        with patch.object(run, 'api', return_value=(200, {'segments': [{'text': '购买商品。'}]})):
            with self.assertRaisesRegex(AdapterError, '改变了原文'):
                run.plan(self.job['options']['script'], None)
        self.assertNotIn('segments', self.job['details'])

    def test_all_false_product_tags_get_explicit_fallback(self):
        run = _Run(self.job, self.ctx)
        with patch.object(run, 'api', return_value=(200, {'segments': [{'text': self.job['options']['script'], 'product': False}]})):
            result = run.plan(self.job['options']['script'], self.reference)
        self.assertTrue(result[0]['product'])
        self.assertIn('product_tag_warning', self.job['details'])

    def test_nonconfigured_video_mode_is_rejected_before_paid_work(self):
        self.job['options']['video_mode'] = 'latent_two_pass'
        with patch.object(_Run, 'api') as api:
            with self.assertRaisesRegex(AdapterError, '当前服务配置'):
                TalkingAdapter().execute(self.job, self.ctx)
            api.assert_not_called()

    def test_streamed_download_never_reads_unbounded_bytes(self):
        run = _Run(self.job, self.ctx)
        sizes = []
        class Response(io.BytesIO):
            status = 200
            def read(self, size=-1):
                sizes.append(size)
                return super().read(size)
        path = self.ctx.workdir / 'download.mp4'
        with patch.object(run.opener, 'open', return_value=Response(b'x' * (2 * 1024 * 1024 + 3))):
            run.transfer('http://local/api/media', path, {})
        self.assertEqual(path.stat().st_size, 2 * 1024 * 1024 + 3)
        self.assertTrue(all(0 < size <= 1024 * 1024 for size in sizes))

    def test_provider_media_urls_reject_private_hosts(self):
        for url in ('file:///etc/passwd', 'http://127.0.0.1/x', 'https://127.0.0.1/x', 'https://user:pass@example.com/x'):
            with self.subTest(url=url), self.assertRaises(AdapterError):
                _Run.validate_media_url(url)

    def test_signed_result_urls_are_not_persisted(self):
        run = _Run(self.job, self.ctx)
        run.steps['segment:1'] = {'task_id': 'paid-1'}
        with patch.object(run, 'api', return_value=(200, {'status': 'SUCCESS', 'results': [{'url': 'https://cdn.test/x?token=secret'}]})):
            run.wait('segment:1', 'paid-1')
        self.assertNotIn('token=secret', str(self.job))

    def test_direct_workflow_uses_native_nodes_and_requeries_download_url(self):
        self.settings.fl2va_url = ''
        self.settings.fl2va_video_mode = 'two_pass'
        self.settings.fl2va_workflow_id = '123456'
        self.job['details']['segments'] = [{'text': self.job['options']['script'], 'seconds': 4, 'product': False}]
        calls = []
        def api(run, endpoint, body=None, raw=None, content_type=None):
            calls.append((endpoint, body))
            if endpoint.endswith('upload/binary'):
                return 200, {'data': {'fileName': 'uploaded'}}
            if endpoint.endswith('workflow/123456'):
                fields = {(n['nodeId'], n['fieldName']) for n in body['nodeInfoList']}
                self.assertEqual(fields, {('4', 'image'), ('37', 'image'), ('6', 'value'), ('7', 'value'), ('24', 'select')})
                return 200, {'taskId': 'native-video'}
            if endpoint.endswith('query'):
                return 200, {'status': 'SUCCESS', 'results': [{'url': 'https://cdn.test/video.mp4?token=signed'}]}
            self.fail(endpoint)
        def local_file(run, *args):
            path = args[1] if len(args) == 3 else Path(args[0][-1])
            path.write_bytes(b'video')
        with patch.object(_Run, 'api', new=api), patch.object(_Run, 'transfer', new=local_file), \
                patch.object(_Run, 'ffmpeg', new=local_file), patch.object(_Run, 'validate_media_url'):
            TalkingAdapter().execute(self.job, self.ctx)
        self.assertEqual(sum(endpoint.endswith('query') for endpoint, _ in calls), 2)
        self.assertNotIn('token=signed', str(self.job))

    def test_all_five_aspect_mappings(self):
        for aspect, select in [('9:16', '4'), ('16:9', '5'), ('1:1', '1'), ('3:4', '2'), ('4:3', '3')]:
            with self.subTest(aspect=aspect):
                job = {'options': {**self.job['options'], 'aspect': aspect}, 'details': {}}
                run = _Run(job, self.ctx)
                def paid(name, endpoint, body):
                    self.assertEqual(body['aspect'], select)
                    self.assertNotIn('{product_action}', body['prompt'])
                    self.assertNotIn('{speech}', body['prompt'])
                    return 'video'
                def download(name, task_id, path, kind):
                    path.write_bytes(b'video')
                    return path
                def ffmpeg(args):
                    Path(args[-1]).write_bytes(b'merged')
                with patch.object(run, 'plan', return_value=[{'text': '欢迎使用我们的商品。', 'seconds': 4, 'product': False}]), \
                        patch.object(run, 'upload', return_value='uploaded'), patch.object(run, 'paid', side_effect=paid), \
                        patch.object(run, 'download', side_effect=download), patch.object(run, 'ffmpeg', side_effect=ffmpeg):
                    run.execute()

    def test_unsupported_frame_branch_stops_before_remote_work(self):
        self.job['options']['frame_mode'] = 'background_match'
        with patch.object(_Run, 'api') as api:
            with self.assertRaisesRegex(AdapterError, 'original'):
                TalkingAdapter().execute(self.job, self.ctx)
            api.assert_not_called()

    def test_no_paid_post_when_persistence_fails(self):
        run = _Run(self.job, self.ctx)
        with patch.object(self.ctx, 'update', side_effect=OSError('disk full')), patch.object(run, 'api') as api:
            with self.assertRaises(OSError):
                run.paid('segment:1', 'run', {})
            api.assert_not_called()


if __name__ == '__main__':
    unittest.main()
