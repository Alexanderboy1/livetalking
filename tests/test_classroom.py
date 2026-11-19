"""CPU-only lecture state tests; no avatar models or API keys required."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from server.classroom.engine import Room, segments, course_context
from server.classroom.courses import CourseStore


def course():
    return {'id': 'a' * 32, 'title': '课程', 'slides': [
        {'index': 0, 'title': '第一页', 'text': '基础知识', 'script': '第一句话。第二句话。'},
        {'index': 1, 'title': '第二页', 'text': '总结', 'script': '最后一句话。'}]}


class Avatar:
    def __init__(self):
        self.msgqueues = []
        self.messages = []
        self.opt = SimpleNamespace()
    def add_msgqueue(self, q):
        self.msgqueues.append(q)
    def put_msg_txt(self, text, metadata):
        self.messages.append((text, metadata))
    def finish(self, index=-1, status='end'):
        event = dict(self.messages[index][1], status=status)
        for q in self.msgqueues:
            q.put_nowait(json.dumps(event))
    def flush_talk(self):
        pass


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(.01)


class ClassroomTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.avatar = Avatar()
        async def answer(*args):
            return '回答内容。'
        self.room = Room('room', course(), self.avatar, answer, speech_timeout=1)
        self.room.launch()
    async def asyncTearDown(self):
        await self.room.close()

    async def test_advance_only_on_matching_output_end(self):
        self.room.control('start')
        await until(lambda: len(self.avatar.messages) == 1)
        self.room.audio_events.put_nowait({'classroom_utterance': 'stale', 'status': 'end'})
        await asyncio.sleep(.06)
        self.assertEqual(self.room.page, 0)
        self.assertEqual(len(self.avatar.messages), 1)
        self.avatar.finish()
        await until(lambda: len(self.avatar.messages) == 2)
        self.assertEqual(self.room.page, 0)
        self.avatar.finish()
        await until(lambda: len(self.avatar.messages) == 3)
        self.assertEqual(self.room.page, 1)
        self.avatar.finish()
        await until(lambda: self.room.state == 'finished')

    async def test_question_answer_then_resume_exact_segment(self):
        self.room.control('start')
        await until(lambda: len(self.avatar.messages) == 1)
        q = self.room.ask('知识是什么？', '观众', 'ip1')
        self.room.moderate(q['id'], 'approve')
        self.avatar.finish()
        await until(lambda: len(self.avatar.messages) == 2)
        self.assertEqual(self.avatar.messages[1][0], '回答内容。')
        self.avatar.finish()
        await until(lambda: len(self.avatar.messages) == 3)
        self.assertEqual(self.avatar.messages[2][0], '第二句话。')
        self.assertEqual(self.room.questions[0]['status'], 'answered')

    async def test_pause_waits_for_segment_and_resumes_without_repetition(self):
        self.room.control('start')
        await until(lambda: len(self.avatar.messages) == 1)
        self.room.control('pause')
        self.assertTrue(self.room.pause_requested)
        self.avatar.finish()
        await until(lambda: self.room.state == 'paused')
        self.assertEqual(len(self.avatar.messages), 1)
        self.room.control('start')
        await until(lambda: len(self.avatar.messages) == 2)
        self.assertEqual(self.avatar.messages[1][0], '第二句话。')

    async def test_goto_defers_until_audio_done(self):
        self.room.control('start')
        await until(lambda: len(self.avatar.messages) == 1)
        self.room.control('goto', 1)
        self.assertEqual(self.room.page, 0)
        self.avatar.finish()
        await until(lambda: len(self.avatar.messages) == 2)
        self.assertEqual(self.avatar.messages[1][0], '最后一句话。')

    async def test_timeout_requires_room_recreation_and_does_not_skip(self):
        self.room.speech_timeout = .06
        self.room.control('start')
        await until(lambda: self.room.state == 'error')
        self.assertEqual(self.room.page, 0)
        self.assertEqual(self.room.segment, 0)
        with self.assertRaises(ValueError):
            self.room.control('start')

    async def test_moderation_rate_limit_duplicate_and_private_pending(self):
        q = self.room.ask('什么是知识？', 'A', 'ip1')
        self.assertEqual(self.room.snapshot()['questions'], [])
        self.assertEqual(len(self.room.snapshot(host=True)['questions']), 1)
        with self.assertRaises(ValueError):
            self.room.ask('另一个问题？', 'A', 'ip1')
        with self.assertRaises(ValueError):
            self.room.ask('什么是知识？', 'B', 'ip2')
        self.room.moderate(q['id'], 'reject')
        self.assertEqual(self.room.questions[0]['status'], 'rejected')

    async def test_close_cancels_llm_and_removes_listener(self):
        waiting = asyncio.Event()
        async def blocked(*args):
            waiting.set()
            await asyncio.Event().wait()
        self.room.answer_fn = blocked
        q = self.room.ask('提问内容？', '', 'ip1')
        self.room.moderate(q['id'], 'approve')
        self.room.control('start')
        await waiting.wait()
        await self.room.close()
        self.assertEqual(self.room.questions[0]['status'], 'cancelled')
        self.assertEqual(self.avatar.messages, [])
        self.assertEqual(self.avatar.msgqueues, [])

    def test_split_and_retrieval(self):
        self.assertEqual(segments('第一句。\n第二句！'), ['第一句。', '第二句！'])
        self.assertTrue(all(len(s) <= 160 for s in segments('a'*400)))
        self.assertIn('基础知识', course_context(course(), 1, '基础知识？'))


class CourseStoreTest(unittest.TestCase):
    def test_atomic_scripts_and_path_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            store = CourseStore(directory)
            c = course()
            (Path(directory)/c['id']).mkdir()
            store.save(c)
            result = store.update(c['id'], ['新讲稿', '结束'])
            self.assertEqual(result, store.get(c['id']))
            with self.assertRaises(ValueError):
                store.get('../elsewhere')
            with self.assertRaises(ValueError):
                store.get(None)
            with self.assertRaises(ValueError):
                store.update(c['id'], ['missing page'])


if __name__ == '__main__':
    unittest.main()
