"""Synthetic output assertions, not Course Compiler's complete import validator."""
import json
from pathlib import PurePosixPath
from types import SimpleNamespace
import zipfile


def inspect(path):
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        assert len(names) == len(set(names)), "duplicate ZIP member"
        for name in names:
            part = PurePosixPath(name)
            assert not part.is_absolute() and ".." not in part.parts and "\\" not in name
            assert not name.startswith("private/")
        manifest = json.loads(archive.read("handouter-input.json"))
        assert manifest["schema_version"] == 1
        assert set(manifest) <= {"schema_version", "lecture", "media", "slides"}
        assert set(manifest["lecture"]) == {"course_title", "lecture_date"}
        media = manifest["media"]["path"]
        assert media in names and media in {"media/lecture.mp4", "media/lecture.ts"}
        header = archive.read(media)[:12]
        assert header[4:8] == b"ftyp" or header[:1] == b"G"
        images = []
        expected = {"handouter-input.json", media}
        if "slides" in manifest:
            slides = manifest["slides"]
            assert slides == {"images_dir": "slides/images", "metadata_path": "slides/slides_meta.json"}
            events = json.loads(archive.read(slides["metadata_path"]))
            expected.add(slides["metadata_path"])
            for event in events:
                assert set(event) == {"filename", "source_index", "start_ms", "end_ms"}
                assert PurePosixPath(event["filename"]).name == event["filename"]
                for key in ["start_ms", "end_ms"]:
                    assert event[key] is None or type(event[key]) is int and event[key] >= 0
                name = slides["images_dir"] + "/" + event["filename"]
                data = archive.read(name)
                assert data.startswith((b"\xff\xd8\xff", b"\x89PNG", b"RIFF"))
                expected.add(name)
                if name not in images:
                    images.append(name)
        assert set(names) == expected, "unexpected output member"
        return SimpleNamespace(source_kind="asset_zip", slide_images=len(images))
