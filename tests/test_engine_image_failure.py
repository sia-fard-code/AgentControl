import json
import re

import pytest

from test_engine_images import (  # noqa: F401
    _answer,
    _calls,
    _tmp_index,
    engine,
    harness,
    make_png,
    no_pillow,
    write,
)


@pytest.mark.parametrize('mode', ['native', 'prompt'])
@pytest.mark.parametrize('wrapup', [False, True])
def test_image_agent_rejection_finishes_failed(harness, tmp_path, no_pillow,
                                              capsys, mode, wrapup):
    write(tmp_path, 'a.png', make_png())
    model = 'gpt-6-sol' if mode == 'native' else 'anthropic_tool_call.claude-x'
    view = (_calls(('v1', 'view_image', {'path': 'a.png'})) if mode == 'native'
            else _answer('```tool {"name":"view_image","args":{"path":"a.png"}}```'))
    fatal = {'_fatal': True, '_status': 400,
             '_error_detail': 'synthetic unsupported image input'}
    # Wrap-up: 2 = one work turn + the reserved report turn, which is rejected.
    # The run is incomplete (exit 3, stop_reason kept), not an operational failure.
    extra = ['--max-iters', '2'] if wrapup else []
    if mode == 'prompt':
        extra.append('--allow-prompt-tools')
    with pytest.raises(SystemExit) as caught:
        harness.run('-t', 'inspect', '-m', model, '--tool-mode', mode, *extra,
                    replies=[view, fatal])
    assert caught.value.code == (3 if wrapup else 1)
    rows = [json.loads(x) for x in (tmp_path / 'index.jsonl').read_text().splitlines()]
    assert rows[-1]['event'] == 'end' and rows[-1]['status'] == 'failed'
    assert rows[-1].get('stop_reason') == ('max_iters' if wrapup else None)
    assert rows[-1]['tool_calls'] == 1
    transcript = harness.transcript_path.read_text(encoding='utf-8')
    assert re.fullmatch(r'\[[^\]]*: [\d,]+ tool calls[^\]]*\]', transcript.splitlines()[-1])
    assert model in capsys.readouterr().err
    assert 'base64,' not in harness.artifacts()
    assert 'not' in transcript.lower() and 'image' in transcript.lower()


@pytest.mark.parametrize('mode', ['native', 'prompt'])
@pytest.mark.parametrize('wrapup', [False, True])
def test_image_agent_text_cap_finishes_failed(engine, harness, tmp_path, no_pillow,
                                             monkeypatch, capsys, mode, wrapup):
    write(tmp_path, 'a.png', make_png())
    monkeypatch.setattr(engine, 'IMAGE_MAX_TEXT_CHARS', 1)
    model = 'gpt-6-sol' if mode == 'native' else 'anthropic_tool_call.claude-x'
    view = (_calls(('v1', 'view_image', {'path': 'a.png'})) if mode == 'native'
            else _answer('```tool {"name":"view_image","args":{"path":"a.png"}}```'))
    extra = ['--max-iters', '2'] if wrapup else []
    if mode == 'prompt':
        extra.append('--allow-prompt-tools')
    with pytest.raises(SystemExit) as caught:
        harness.run('-t', 'inspect', '-m', model, '--tool-mode', mode, *extra,
                    replies=[view])
    assert caught.value.code == (3 if wrapup else 1)
    rows = [json.loads(x) for x in (tmp_path / 'index.jsonl').read_text().splitlines()]
    assert rows[-1]['event'] == 'end' and rows[-1]['status'] == 'failed'
    assert rows[-1].get('stop_reason') == ('max_iters' if wrapup else None)
    assert rows[-1]['tool_calls'] == 1
    transcript = harness.transcript_path.read_text(encoding='utf-8')
    assert re.fullmatch(r'\[[^\]]*: [\d,]+ tool calls[^\]]*\]', transcript.splitlines()[-1])
    assert model in capsys.readouterr().err
    assert 'base64,' not in harness.artifacts()
    assert 'not' in transcript.lower() and 'image' in transcript.lower()


@pytest.mark.parametrize('mode', ['native', 'prompt'])
def test_text_only_fatal_keeps_original_exit(harness, tmp_path, mode):
    model = 'gpt-6-sol' if mode == 'native' else 'anthropic_tool_call.claude-x'
    fatal = {'_fatal': True, '_status': 400,
             '_error_detail': 'synthetic text-only rejection'}
    compatibility = ['--allow-prompt-tools'] if mode == 'prompt' else []
    with pytest.raises(SystemExit) as caught:
        harness.run('-t', 'inspect', '-m', model, '--tool-mode', mode, *compatibility,
                    replies=[fatal])
    assert isinstance(caught.value.code, str)
    assert 'synthetic text-only rejection' in caught.value.code
    rows = [json.loads(x) for x in (tmp_path / 'index.jsonl').read_text().splitlines()]
    assert not any(row['event'] == 'end' for row in rows)
    transcript = harness.transcript_path.read_text(encoding='utf-8')
    assert not re.fullmatch(r'\[[^\]]*: [\d,]+ tool calls[^\]]*\]', transcript.splitlines()[-1])
