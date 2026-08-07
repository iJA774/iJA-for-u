import hashlib

import pytest

from adapters.web_simulator import AttachmentStore
from domain.errors import InputValidationError
from domain.models import ComponentType


def test_attachment_store_rejects_fake_mime_and_tampered_reference(tmp_path) -> None:
    store = AttachmentStore(
        uploads_root=tmp_path / "uploads",
        personas_root=tmp_path / "personas",
        max_bytes=1024,
    )
    with pytest.raises(InputValidationError, match="MIME"):
        store.save_image("fake.png", "image/png", b"not-a-png")

    png = b"\x89PNG\r\n\x1a\n" + b"phase-one"
    component = store.save_image("safe.png", "image/png", png)
    assert component.sha256 == hashlib.sha256(png).hexdigest()
    component.sha256 = "0" * 64
    with pytest.raises(InputValidationError, match="摘要"):
        store.validate_image_ref(component)


def test_attachment_store_accepts_audio_and_safe_files(tmp_path) -> None:
    store = AttachmentStore(
        uploads_root=tmp_path / "uploads",
        personas_root=tmp_path / "personas",
        max_bytes=4096,
    )
    audio = b"#!AMR\n" + b"\x00" * 20
    audio_component = store.save_attachment("voice.amr", "audio/amr", audio)
    assert audio_component.type == ComponentType.AUDIO_REF
    assert store.read_attachment_ref(audio_component) == audio

    document = b"%PDF-1.7\nminimal"
    file_component = store.save_attachment("report.pdf", "application/pdf", document)
    assert file_component.type == ComponentType.FILE_REF
    assert store.read_attachment_ref(file_component) == document


def test_attachment_store_rejects_spoofed_audio_and_invalid_json(tmp_path) -> None:
    store = AttachmentStore(
        uploads_root=tmp_path / "uploads",
        personas_root=tmp_path / "personas",
        max_bytes=4096,
    )
    with pytest.raises(InputValidationError, match="语音内容"):
        store.save_attachment("fake.mp3", "audio/mpeg", b"not-mp3")
    with pytest.raises(InputValidationError, match="JSON"):
        store.save_attachment("bad.json", "application/json", b"{bad")
