"""One paced render loop per classroom; viewers get bounded, independent tracks."""
import asyncio
import threading
import time
from fractions import Fraction

from aiortc import (MediaStreamTrack, RTCConfiguration, RTCIceServer,
                    RTCPeerConnection, RTCSessionDescription)
from aiortc.mediastreams import MediaStreamError
from av import AudioFrame, VideoFrame


class ViewerTrack(MediaStreamTrack):
    def __init__(self, source, kind):
        super().__init__()
        self.source, self.kind = source, kind
        self.frames = asyncio.Queue(maxsize=6 if kind == 'video' else 12)
        source.tracks[kind].add(self)

    def offer(self, item):
        if self.frames.full():
            self.frames.get_nowait()
        self.frames.put_nowait(item)

    async def recv(self):
        if self.readyState != 'live':
            raise MediaStreamError
        item = await self.frames.get()
        if item is None:
            raise MediaStreamError
        data, pts = item
        if self.kind == 'video':
            frame = VideoFrame.from_ndarray(data, format='bgr24')
            frame.time_base = Fraction(1, 90000)
        else:
            frame = AudioFrame(format='s16', layout='mono', samples=len(data))
            frame.planes[0].update(data.tobytes())
            frame.sample_rate = 16000
            frame.time_base = Fraction(1, 16000)
        frame.pts = pts
        return frame

    def stop(self):
        super().stop()
        self.source.tracks[self.kind].discard(self)
        self.offer(None)


class ClassroomMedia:
    def __init__(self, avatar):
        self.avatar = avatar
        self.loop = asyncio.get_running_loop()
        self.quit = threading.Event()
        self.tracks = {'video': set(), 'audio': set()}
        self.peers = set()
        self.deadline = None
        self.video_pts = self.audio_pts = 0
        self.thread = threading.Thread(target=avatar.render, args=(self.quit,), daemon=True,
                                       name='classroom-render')
        # WebRTCOutput delegates the final rendered frames to this adapter.
        avatar.output._player = self
        self.thread.start()

    def deliver(self, kind, item):
        for track in self.tracks[kind]:
            track.offer(item)

    def push_video(self, frame):
        now = time.monotonic()
        self.deadline = max(self.deadline or now, now - .04)
        if self.quit.wait(max(0, self.deadline - now)):
            return
        self.deadline += .04
        self.loop.call_soon_threadsafe(self.deliver, 'video', (frame.copy(), self.video_pts))
        self.video_pts += 3600

    def push_audio(self, frame, eventpoint=None):
        if self.quit.is_set():
            return
        self.loop.call_soon_threadsafe(self.deliver, 'audio', (frame, self.audio_pts))
        self.audio_pts += len(frame)
        # The renderer calls this in playback order, after pacing the associated video frame.
        if eventpoint and eventpoint.get('status'):
            self.avatar.notify(eventpoint)

    def get_buffer_size(self):
        return 0

    async def offer(self, payload, stun):
        if payload.get('type') != 'offer' or not isinstance(payload.get('sdp'), str):
            raise ValueError('无效的 WebRTC offer')
        if len(self.peers) >= 32:
            raise ValueError('本课堂媒体连接已满')
        pc = RTCPeerConnection(configuration=RTCConfiguration(iceServers=[RTCIceServer(urls=stun)] if stun else []))
        self.peers.add(pc)
        tracks = [ViewerTrack(self, kind) for kind in ('audio', 'video')]

        @pc.on('connectionstatechange')
        async def changed():
            if pc.connectionState in ('failed', 'closed'):
                self.peers.discard(pc)
                for track in tracks:
                    track.stop()
                if pc.connectionState != 'closed':
                    await pc.close()

        try:
            for track in tracks:
                pc.addTrack(track)
            await pc.setRemoteDescription(RTCSessionDescription(sdp=payload['sdp'], type='offer'))
            await pc.setLocalDescription(await pc.createAnswer())
            return {'sdp': pc.localDescription.sdp, 'type': pc.localDescription.type}
        except BaseException:
            await pc.close()
            raise

    async def close(self):
        self.quit.set()
        await asyncio.gather(*(pc.close() for pc in list(self.peers)))
        await asyncio.to_thread(self.thread.join, 8)
