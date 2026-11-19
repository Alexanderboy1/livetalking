"""HTTP APIs for course authoring, presenter controls and audience questions."""
import asyncio
import hashlib
import hmac
import json
import os
import secrets
import tempfile
from pathlib import Path

from aiohttp import web

from server.classroom.courses import CourseStore, MAX_UPLOAD
from server.classroom.engine import Room

_store = None
_rooms = {}
_secret = os.getenv("LIVETALKING_CLASSROOM_ADMIN_TOKEN", "")


def _admin(request):
    supplied = request.headers.get("Authorization", "").removeprefix("Bearer ")
    if not _secret or not hmac.compare_digest(supplied, _secret):
        raise web.HTTPUnauthorized(text="主持人密钥无效")


def _room(room_id):
    room = _rooms.get(room_id)
    if not room or room.closed:
        raise web.HTTPNotFound(text="课堂不存在或已结束")
    return room


def _json(data):
    return web.json_response({"code": 0, "data": data})


async def _payload(request):
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError
        return body
    except (json.JSONDecodeError, ValueError):
        raise web.HTTPBadRequest(text="需要 JSON 请求体")


async def list_courses(request):
    _admin(request)
    return _json(await asyncio.to_thread(_store.list))


async def get_course(request):
    _admin(request)
    try:
        return _json(await asyncio.to_thread(_store.get, request.match_info["course_id"]))
    except ValueError as exc:
        raise web.HTTPNotFound(text=str(exc))


async def upload_course(request):
    _admin(request)
    reader = await request.multipart()
    filepart = None
    title = ""
    while part := await reader.next():
        if part.name == "file":
            filepart = part
            break
        elif part.name == "title":
            title = (await part.text())[:160]
    if filepart is None:
        raise web.HTTPBadRequest(text="缺少课件文件")
    filename = Path(filepart.filename or "").name
    if Path(filename).suffix.lower() not in (".pdf", ".pptx"):
        raise web.HTTPBadRequest(text="仅支持 PDF 和 PPTX")
    fd, temp_path = tempfile.mkstemp(prefix="classroom-upload-")
    size = 0
    try:
        with os.fdopen(fd, "wb") as out:
            while chunk := await filepart.read_chunk(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD:
                    raise web.HTTPRequestEntityTooLarge(max_size=MAX_UPLOAD, actual_size=size)
                out.write(chunk)
        course = await asyncio.to_thread(_store.import_file, temp_path, filename)
        if title.strip():
            course["title"] = title.strip()
            await asyncio.to_thread(_store.save, course)
        return _json(course)
    except web.HTTPException:
        raise
    except Exception as exc:
        from utils.logger import logger
        logger.exception("Course import failed")
        raise web.HTTPBadRequest(text=f"课件处理失败: {str(exc)[:200]}")
    finally:
        Path(temp_path).unlink(missing_ok=True)


async def save_scripts(request):
    _admin(request)
    payload = await _payload(request)
    try:
        course = await asyncio.to_thread(_store.update, request.match_info["course_id"], payload.get("scripts"))
        return _json(course)
    except ValueError as exc:
        raise web.HTTPBadRequest(text=str(exc))


async def create_room(request):
    _admin(request)
    payload = await _payload(request)
    try:
        course = await asyncio.to_thread(_store.get, payload.get("course_id", ""))
    except ValueError as exc:
        raise web.HTTPBadRequest(text=str(exc))
    if request.app['opt'].transport != 'webrtc':
        raise web.HTTPConflict(text='网页课堂请以 --transport webrtc 启动')
    if any(not slide['script'].strip() for slide in course['slides']):
        raise web.HTTPBadRequest(text='请先为每一页填写讲稿并保存')
    from server.session_manager import session_manager, MaxSessionError
    from server.classroom.media import ClassroomMedia
    try:
        session_id = await session_manager.create_session({})
    except MaxSessionError as exc:
        raise web.HTTPServiceUnavailable(text=str(exc))
    avatar = session_manager.get_session(session_id)
    avatar.classroom_session = True
    room_id = secrets.token_urlsafe(24)
    try:
        room = Room(room_id, course, avatar)
        room.media = ClassroomMedia(avatar)
        room.launch()
        _rooms[room_id] = room
    except BaseException:
        session_manager.remove_session(session_id)
        raise
    return _json(room.snapshot(host=True))



async def get_room(request):
    room = _room(request.match_info["room_id"])
    host = request.headers.get("Authorization", "").removeprefix("Bearer ") == _secret and bool(_secret)
    return _json(room.snapshot(host=host))


async def control_room(request):
    _admin(request)
    room = _room(request.match_info["room_id"])
    payload = await _payload(request)
    try:
        room.control(payload.get("action"), payload.get("page", payload.get("enabled")))
        return _json(room.snapshot(host=True))
    except ValueError as exc:
        raise web.HTTPBadRequest(text=str(exc))


async def moderate_question(request):
    _admin(request)
    room = _room(request.match_info["room_id"])
    payload = await _payload(request)
    try:
        room.moderate(request.match_info["question_id"], payload.get("action"))
        return _json(room.snapshot(host=True))
    except ValueError as exc:
        raise web.HTTPBadRequest(text=str(exc))


async def ask_question(request):
    room = _room(request.match_info["room_id"])
    payload = await _payload(request)
    # The trusted reverse proxy must overwrite X-Real-IP, not append to it.
    address = request.remote or 'unknown'
    if os.getenv('LIVETALKING_TRUST_PROXY') == '1':
        address = request.headers.get('X-Real-IP', address)
    identity = hashlib.sha256(address.encode()).hexdigest()
    try:
        question = room.ask(payload.get("text", ""), payload.get("name", "观众"), identity)
        return _json(question)
    except ValueError as exc:
        raise web.HTTPTooManyRequests(text=str(exc))


async def room_events(request):
    room = _room(request.match_info["room_id"])
    host = request.headers.get("Authorization", "").removeprefix("Bearer ") == _secret and bool(_secret)
    response = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    await response.prepare(request)
    event_queue = asyncio.Queue(maxsize=1)
    room.listeners.add(event_queue)
    revision = -1
    try:
        while True:
            if room.revision != revision:
                revision = room.revision
                data = json.dumps(room.snapshot(host=host), ensure_ascii=False)
                message = f"id: {revision}\nevent: state\ndata: {data}\n\n"
                await response.write(message.encode())
            if room.closed:
                await response.write(b"event: ended\ndata: {}\n\n")
                break
            try:
                await asyncio.wait_for(event_queue.get(), timeout=15)
            except asyncio.TimeoutError:
                await response.write(b": keepalive\n\n")
    except (asyncio.CancelledError, ConnectionResetError):
        pass
    finally:
        room.listeners.discard(event_queue)
    return response


async def course_slide(request):
    _admin(request)
    try:
        course = await asyncio.to_thread(_store.get, request.match_info['course_id'])
        index = int(request.match_info['index'])
        if not 0 <= index < len(course['slides']):
            raise ValueError
    except ValueError:
        raise web.HTTPNotFound()
    path = _store.root / course['id'] / f"{index}.png"
    return web.FileResponse(path, headers={'Cache-Control': 'no-store'})


async def room_slide(request):
    room = _room(request.match_info["room_id"])
    try:
        index = int(request.match_info["index"])
        if not 0 <= index < len(room.course["slides"]):
            raise ValueError
    except ValueError:
        raise web.HTTPNotFound()
    path = _store.root / room.course["id"] / f"{index}.png"
    if not path.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={"Cache-Control": "public, max-age=3600"})


async def room_offer(request):
    room = _room(request.match_info["room_id"])
    payload = await _payload(request)
    try:
        return _json(await room.media.offer(payload, request.app['opt'].stun))
    except (ValueError, KeyError) as exc:
        raise web.HTTPBadRequest(text=str(exc))


async def dispose(room):
    from server.session_manager import session_manager
    _rooms.pop(room.id, None)
    try:
        await room.close()
        await room.media.close()
    finally:
        session_manager.remove_session(room.avatar.sessionid)


async def close_room(request):
    _admin(request)
    await dispose(_room(request.match_info['room_id']))
    return _json({'closed': True})


async def shutdown(app):
    await asyncio.gather(*(dispose(room) for room in list(_rooms.values())))


async def list_rooms(request):
    _admin(request)
    return _json([{'id': r.id, 'title': r.course['title']} for r in _rooms.values()])


async def classroom_page(request):
    return web.FileResponse(Path(__file__).resolve().parents[2] / 'web/classroom/index.html')


def setup_classroom_routes(app):
    global _store, _secret
    _secret = os.getenv("LIVETALKING_CLASSROOM_ADMIN_TOKEN", "")
    app.on_shutdown.append(shutdown)
    _store = CourseStore(os.getenv("LIVETALKING_COURSE_DIR", "data/courses"))
    app.router.add_get("/classroom", classroom_page)
    app.router.add_get("/classroom/view", classroom_page)
    app.router.add_get("/api/courses", list_courses)
    app.router.add_post("/api/courses", upload_course)
    app.router.add_get("/api/courses/{course_id}", get_course)
    app.router.add_put("/api/courses/{course_id}/scripts", save_scripts)
    app.router.add_get("/api/courses/{course_id}/slides/{index}.png", course_slide)
    app.router.add_get("/api/classrooms", list_rooms)
    app.router.add_post("/api/classrooms", create_room)
    app.router.add_get("/api/classrooms/{room_id}", get_room)
    app.router.add_post("/api/classrooms/{room_id}/control", control_room)
    app.router.add_post("/api/classrooms/{room_id}/questions", ask_question)
    app.router.add_post("/api/classrooms/{room_id}/questions/{question_id}", moderate_question)
    app.router.add_get("/api/classrooms/{room_id}/events", room_events)
    app.router.add_post("/api/classrooms/{room_id}/offer", room_offer)
    app.router.add_get("/api/classrooms/{room_id}/slides/{index}.png", room_slide)
    app.router.add_delete("/api/classrooms/{room_id}", close_room)
