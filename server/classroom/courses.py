"""Disk-backed course preparation for PDF and PowerPoint decks."""
import json
import re
import shutil
import subprocess
import tempfile
import uuid
import zipfile
from pathlib import Path

MAX_UPLOAD = 50 * 1024 * 1024
MAX_PAGES = 100
MAX_SCRIPT = 12000


def valid_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
        raise ValueError("无效的 ID")
    return value


class CourseStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def get(self, course_id):
        path = self.root / valid_id(course_id) / "course.json"
        if not path.exists():
            raise ValueError("课程不存在")
        return json.loads(path.read_text(encoding="utf-8"))

    def list(self):
        result = []
        for path in self.root.glob("*/course.json"):
            course = json.loads(path.read_text(encoding="utf-8"))
            result.append({"id": course["id"], "title": course["title"], "pages": len(course["slides"])})
        return result

    def save(self, course):
        path = self.root / valid_id(course["id"]) / "course.json"
        temp = path.with_suffix("." + uuid.uuid4().hex + ".tmp")
        temp.write_text(json.dumps(course, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)

    def update(self, course_id, scripts):
        course = self.get(course_id)
        if not isinstance(scripts, list) or len(scripts) != len(course["slides"]):
            raise ValueError("讲稿数量必须与页数一致")
        if any(not isinstance(s, str) or len(s) > MAX_SCRIPT for s in scripts):
            raise ValueError(f"每页讲稿不能超过 {MAX_SCRIPT} 字")
        for slide, script in zip(course["slides"], scripts):
            slide["script"] = script.strip()
        self.save(course)
        return course

    def import_file(self, source, filename):
        import pymupdf
        suffix = Path(filename).suffix.lower()
        if suffix not in (".pdf", ".pptx"):
            raise ValueError("仅支持 PDF、PPTX；动画和视频请先转换为静态页面")
        course_id = uuid.uuid4().hex
        target = self.root / course_id
        target.mkdir()
        try:
            original = target / ("source" + suffix)
            shutil.copyfile(source, original)
            notes = []
            if suffix == ".pptx":
                with zipfile.ZipFile(original) as archive:
                    if sum(i.file_size for i in archive.infolist()) > 300 * 1024 * 1024:
                        raise ValueError("PPTX 解压后过大")
                from pptx import Presentation
                presentation = Presentation(original)
                if not 1 <= len(presentation.slides) <= MAX_PAGES:
                    raise ValueError(f"课件最多 {MAX_PAGES} 页")
                notes = [s.notes_slide.notes_text_frame.text if s.has_notes_slide else ""
                         for s in presentation.slides]
                binary = shutil.which("libreoffice") or shutil.which("soffice")
                if not binary:
                    raise ValueError("请安装 libreoffice-impress，或上传 PDF")
                with tempfile.TemporaryDirectory(prefix="classroom-office-") as profile:
                    subprocess.run([
                        binary, "-env:UserInstallation=" + Path(profile).as_uri(),
                        "--headless", "--convert-to", "pdf", "--outdir", str(target), str(original)
                    ], check=True, timeout=120, capture_output=True)
                pdf = target / "source.pdf"
                if not pdf.exists():
                    raise ValueError("PPTX 转换失败，请尝试上传 PDF")
            else:
                pdf = original
            slides = []
            with pymupdf.open(pdf) as doc:
                if doc.needs_pass or not 1 <= len(doc) <= MAX_PAGES:
                    raise ValueError(f"课件需为未加密文件，页数为 1–{MAX_PAGES}")
                for index, page in enumerate(doc):
                    scale = min(1600 / max(page.rect.width, 1), 1000 / max(page.rect.height, 1))
                    page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False).save(target / f"{index}.png")
                    content = page.get_text().strip()[:MAX_SCRIPT]
                    script = (notes[index].strip() if index < len(notes) else "") or content
                    slides.append({"index": index, "title": (content.splitlines() or [f"第 {index + 1} 页"])[0][:160],
                                   "text": content, "script": script[:MAX_SCRIPT]})
            course = {"id": course_id, "title": Path(filename).stem[:160], "slides": slides}
            self.save(course)
            return course
        except Exception:
            shutil.rmtree(target, ignore_errors=True)
            raise
