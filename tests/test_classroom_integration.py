"""Real PDF/HTTP and media-track tests, without GPU or paid services."""
import asyncio
import io
import os
import tempfile
import unittest
from pathlib import Path
import pymupdf
from types import SimpleNamespace
from unittest.mock import patch

import aiohttp
import numpy as np
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from server.classroom import routes
from server.classroom.engine import Room
from server.classroom.media import ViewerTrack, ClassroomMedia


class FakeAvatar:
    def __init__(self):
        self.msgqueues = []
        self.output = SimpleNamespace(_player=None)
    def add_msgqueue(self, q):
        self.msgqueues.append(q)
    def flush_talk(self):
        pass
    def render(self, quit):
        while not quit.is_set():
            self.output._player.push_video(np.zeros((32, 32, 3), dtype=np.uint8))
            for _ in range(2):
                self.output._player.push_audio(np.zeros(320, dtype=np.int16))
    def notify(self, event):
        pass


class HttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'LIVETALKING_COURSE_DIR': self.temp.name,
                                          'LIVETALKING_CLASSROOM_ADMIN_TOKEN': 'test-secret'})
        self.env.start()
        app = web.Application(client_max_size=60*1024*1024)
        routes.setup_classroom_routes(app)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.headers = {'Authorization': 'Bearer test-secret'}
    async def asyncTearDown(self):
        routes._rooms.clear()
        await self.client.close()
        self.env.stop()
        self.temp.cleanup()

    async def import_pdf(self):
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((40, 70), 'Welcome to the classroom')
        doc.new_page().insert_text((40, 70), 'Second page')
        data = doc.tobytes()
        doc.close()
        form = aiohttp.FormData()
        form.add_field('file', data, filename='lesson.pdf', content_type='application/pdf')
        response = await self.client.post('/api/courses', headers=self.headers, data=form)
        self.assertEqual(response.status, 200, await response.text())
        return (await response.json())['data']

    async def test_import_preview_and_save_and_unauthorized(self):
        response = await self.client.get('/api/courses')
        self.assertEqual(response.status, 401)
        c = await self.import_pdf()
        self.assertEqual(len(c['slides']), 2)
        self.assertIn('Welcome', c['slides'][0]['script'])
        preview = '/api/courses/' + c['id'] + '/slides/0.png'
        self.assertEqual((await self.client.get(preview)).status, 401)
        response = await self.client.get(preview, headers=self.headers)
        self.assertEqual(response.status, 200)
        self.assertTrue((await response.read()).startswith(b'\x89PNG'))
        response = await self.client.put('/api/courses/' + c['id'] + '/scripts',
                                        headers=self.headers, json={'scripts': ['新内容', '结束']})
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())['data']['slides'][0]['script'], '新内容')
        for url in ['/classroom','/classroom/view']:
            self.assertEqual((await self.client.get(url)).status, 200)

    async def test_sse_public_redaction_question_control(self):
        c = await self.import_pdf()
        room = Room('test-room', c, FakeAvatar())
        routes._rooms[room.id] = room
        r = await self.client.post('/api/classrooms/test-room/questions', json={'text':'测试问题','name':'观众'})
        self.assertEqual(r.status, 200)
        q = (await r.json())['data']
        r = await self.client.get('/api/classrooms/test-room/events')
        block = await asyncio.wait_for(r.content.readuntil(b'\n\n'),2)
        self.assertIn(b'event: state\n',block)
        self.assertIn(b'"questions": []',block)
        r.close()
        r = await self.client.post('/api/classrooms/test-room/control', json={'action':'start'})
        self.assertEqual(r.status,401)
        r = await self.client.post('/api/classrooms/test-room/questions/'+q['id'],
                                   headers=self.headers,json={'action':'approve'})
        self.assertEqual(r.status,200)
        self.assertEqual(room.questions[0]['status'],'approved')
        await room.close()

    async def test_invalid_upload_is_cleaned(self):
        form=aiohttp.FormData()
        form.add_field('file',b'not pdf',filename='bad.pdf',content_type='application/pdf')
        response=await self.client.post('/api/courses',headers=self.headers,data=form)
        self.assertEqual(response.status,400)
        self.assertEqual(list(Path(self.temp.name).iterdir()),[])


class MediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_viewers_independent_frames_and_bounded_queue(self):
        source = SimpleNamespace(tracks={'video':set(),'audio':set()})
        a, b = ViewerTrack(source,'video'), ViewerTrack(source,'video')
        pixels = np.zeros((16,16,3), dtype=np.uint8)
        for i in range(50):
            a.offer((pixels,i*3600));b.offer((pixels,i*3600))
        self.assertEqual(a.frames.qsize(),6)
        f1,f2=await a.recv(),await b.recv()
        self.assertIsNot(f1,f2)
        self.assertEqual(f1.pts,f2.pts)
        a.stop()
        self.assertEqual(b.readyState,'live')
        audio=ViewerTrack(source,'audio')
        audio.offer((np.zeros(320,dtype=np.int16),320))
        frame=await audio.recv()
        self.assertEqual(frame.sample_rate,16000)
        self.assertEqual(frame.samples,320)
        b.stop();audio.stop()

    async def test_shared_render_continues_without_viewers_and_stops(self):
        avatar=FakeAvatar()
        source=ClassroomMedia(avatar)
        await asyncio.sleep(.13)
        self.assertGreater(source.video_pts,3600)
        track=ViewerTrack(source,'video')
        frame=await asyncio.wait_for(track.recv(),1)
        self.assertEqual(frame.width,32)
        track.stop()
        await source.close()
        self.assertFalse(source.thread.is_alive())


if __name__=='__main__':
    unittest.main()
