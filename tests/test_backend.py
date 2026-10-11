import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import backend
from fastapi import HTTPException
from fastapi.testclient import TestClient

class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data_patch = patch.object(backend, 'DATA', Path(self.temp.name))
        self.data_patch.start()
        self.client = TestClient(backend.app)
        self.submit_patch = patch.object(backend.pool, 'submit')
        self.submit = self.submit_patch.start()
    def tearDown(self):
        self.submit_patch.stop()
        self.data_patch.stop()
        self.temp.cleanup()
    def fixture(self, status='done'):
        job = {'id': 'abcde1234567', 'title': '中文 文稿', 'status': status, 'source':'url', 'input':'https://example.com/video', 'created':0, 'formats':['txt']}
        directory = backend._job_dir(job['id'])
        directory.mkdir()
        backend._save_job(job)
        return job, directory
    def test_chinese_download(self):
        job, directory = self.fixture()
        (directory/'中文 文稿.txt').write_text('你好，世界。', encoding='utf-8')
        response = self.client.get('/api/jobs/'+job['id']+'/download?format=txt')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, '你好，世界。')
        self.assertIn("filename*=utf-8''%E4%B8%AD", response.headers['content-disposition'])
    def test_reject_traversal(self):
        for value in ['..', '.', '../outside', 'ABCDEF123456', 'abcde1234567/..', '0000']:
            with self.assertRaises(HTTPException): backend._job_dir(value)
        response = self.client.delete('/api/jobs/%2e%2e')
        self.assertIn(response.status_code, [400,404])
        self.assertTrue(backend.DATA.exists())
    def test_reject_symlink(self):
        with tempfile.TemporaryDirectory() as outside:
            (backend.DATA/'abcde1234567').symlink_to(outside, target_is_directory=True)
            self.assertEqual(self.client.delete('/api/jobs/abcde1234567').status_code,400)
            self.assertTrue(Path(outside).exists())
    def test_active_deletion(self):
        job, directory = self.fixture('working')
        self.assertEqual(self.client.delete('/api/jobs/'+job['id']).status_code,409)
        self.assertTrue(directory.exists())
        job['status']='done'; backend._save_job(job)
        self.assertEqual(self.client.delete('/api/jobs/'+job['id']).status_code,200)
        self.assertFalse(directory.exists())
    def test_invalid_submission(self):
        cases = [{}, {'url':'file:///tmp/test'}, {'url':'https://host:bad/video'}, {'url':'https://u:p@host/video'}, {'url':'https://host/a b'}, {'url':'https://example.com','model':'bad'}, {'url':'https://example.com','lang':'bad'}, {'url':'https://example.com','method':'bad'}, {'url':'https://example.com','initial_prompt':'中'*2001}]
        for data in cases:
            with self.subTest(data=str(data)[:100]):
                self.assertEqual(self.client.post('/api/jobs',data=data).status_code,400)
        self.assertEqual(list(backend.DATA.iterdir()),[])
        self.submit.assert_not_called()
    def test_empty_and_ambiguous_upload(self):
        self.assertEqual(self.client.post('/api/jobs',files={'file':('empty.wav',b'')}).status_code,400)
        self.assertEqual(self.client.post('/api/jobs',data={'url':'https://example.com'},files={'file':('audio.wav',b'a')}).status_code,400)
        self.assertEqual(list(backend.DATA.iterdir()),[])
    def test_options_and_defaults(self):
        response = self.client.post('/api/jobs',data={'url':' https://example.com/video ','lang':'auto','initial_prompt':' 中文术语 '})
        self.assertEqual(response.status_code,200)
        job = self.client.get('/api/jobs/'+response.json()['id']).json()
        self.assertEqual(job['model'],'large-v3-turbo')
        self.assertEqual(job['method'],'auto')
        self.assertEqual(job['requested_language'],'auto')
        self.assertEqual(job['initial_prompt'],'中文术语')
        self.assertEqual(job['input'],'https://example.com/video')
    def test_restart_marks_interrupted_job_and_preserves_files(self):
        job, directory = self.fixture('working')
        preserved = directory/'upload.wav'
        preserved.write_bytes(b'original-upload')
        with TestClient(backend.app) as client:
            restarted = client.get('/api/jobs/'+job['id']).json()
            self.assertEqual(restarted['status'],'error')
            self.assertIn('已中断',restarted['message'])
            self.assertEqual(preserved.read_bytes(),b'original-upload')
            self.assertEqual(client.delete('/api/jobs/'+job['id']).status_code,200)
    def test_restart_does_not_change_completed_job(self):
        job, directory = self.fixture('done')
        before = (directory/'job.json').read_bytes()
        with TestClient(backend.app):
            self.assertEqual((directory/'job.json').read_bytes(),before)
    def test_full_preview_and_quality_metadata(self):
        job, directory = self.fixture('queued')
        job.update(model='large-v3-turbo',method='auto',requested_language='auto',initial_prompt='术语')
        backend._save_job(job)
        segments = [{'start':n,'end':n+1,'text':f'第{n}段'} for n in range(607)]
        info = {'language':'zh','engine':'画面字幕','review_count':3,'quality_notes':['请复核'], 'quality_report':{'sampled':True}}
        with patch.object(backend.pipeline,'download_media',return_value=('source.mp4','中文 文稿'),create=True), patch.object(backend.pipeline,'transcribe_file',return_value=(segments,info)) as transcribe:
            backend._run_job(job['id'])
        actual=self.client.get('/api/jobs/'+job['id']).json()
        self.assertEqual(actual['status'],'done',actual['message'])
        self.assertEqual(len(actual['segments']),607)
        self.assertEqual(actual['quality_report'],{'sampled':True})
        self.assertEqual(actual['review_count'],3)
        self.assertEqual(len(json.loads((directory/'segments.json').read_text())['segments']),607)
        self.assertEqual(transcribe.call_args.kwargs['lang'],'auto')
        self.assertEqual(transcribe.call_args.kwargs['method'],'auto')
        self.assertNotIn('segments',self.client.get('/api/jobs').json()[0])

if __name__=='__main__': unittest.main(verbosity=2)
