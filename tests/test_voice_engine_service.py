"""常驻语音服务（plugins/voice/engine_service.py）的零依赖单测。

只测协议解析与默认值——不起 HTTP、不加载模型。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plugins.voice import engine_service as svc  # noqa: E402


def _multipart(boundary: str, name: str, payload: bytes) -> bytes:
    head = (f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"; filename="a.wav"\r\n'
            f"Content-Type: audio/wav\r\n\r\n").encode()
    return head + payload + f"\r\n--{boundary}--\r\n".encode()


def test_transcribe_multipart_extracts_file():
    body = _multipart("b1", "file", b"RIFFwavdata")
    assert svc._transcribe_multipart(body, "b1") == b"RIFFwavdata"


def test_transcribe_multipart_missing_file():
    body = _multipart("b1", "other", b"x")
    try:
        svc._transcribe_multipart(body, "b1")
    except ValueError as e:
        assert "file" in str(e)
    else:  # pragma: no cover
        raise AssertionError("应当报错")


def test_multipart_boundary_parse():
    assert svc._multipart_boundary('multipart/form-data; boundary="abc"') == "abc"
    assert svc._multipart_boundary("application/json") == ""
