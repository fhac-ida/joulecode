import json

import httpx
import pytest

from joulecode.llm import LLMError, OllamaLLM


def _transport(stream_lines, seen):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        seen.append((request.url.path, body))
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen2.5-coder:7b"}]})
        if body.get("keep_alive") == 0:
            return httpx.Response(200, json={"done": True})
        return httpx.Response(200, content="\n".join(json.dumps(x) for x in stream_lines).encode())

    return httpx.MockTransport(handler)


def test_streamed_json_is_assembled_and_schema_sent():
    seen = []
    llm = OllamaLLM(
        "http://ollama",
        "qwen2.5-coder:7b",
        transport=_transport(
            [
                {"response": '{"a": '},
                {"response": "1}"},
                {"response": "", "done": True, "done_reason": "stop"},
            ],
            seen,
        ),
    )
    schema = {"type": "object"}
    assert llm.generate_json("sys", "prompt", schema) == {"a": 1}
    path, body = seen[-1]
    assert path == "/api/generate" and body["format"] == schema and body["stream"] is True


def test_truncated_answer_gives_hint():
    llm = OllamaLLM(
        "http://ollama",
        "m",
        transport=_transport(
            [{"response": '{"a": '}, {"response": "", "done": True, "done_reason": "length"}], []
        ),
    )
    with pytest.raises(LLMError, match="cut off"):
        llm.generate_json("s", "p", {})


def test_check_and_unload():
    seen = []
    llm = OllamaLLM("http://ollama", "qwen2.5-coder:7b", transport=_transport([], seen))
    assert llm.check() == []
    assert OllamaLLM("http://ollama", "other", transport=_transport([], [])).check()
    llm.unload()
    assert seen[-1][1] == {"model": "qwen2.5-coder:7b", "keep_alive": 0}
