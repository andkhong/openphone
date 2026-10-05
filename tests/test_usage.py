from types import SimpleNamespace

from openmob.usage import Usage


def test_counts_and_summary():
    u = Usage()
    u.add_claude(SimpleNamespace(input_tokens=1200, output_tokens=80, cache_read_input_tokens=0))
    u.add_claude(None)  # responses without usage (fakes) are ignored
    u.add_jev(300)
    assert u.as_dict() == {"claude_input": 1200, "claude_output": 80, "claude_cache_read": 0,
                           "claude_calls": 1, "jev_input": 300, "jev_calls": 1}
    total = Usage()
    total.add(u)
    total.add(u)
    assert total.claude_input == 2400 and total.jev_calls == 2
    assert u.summary() == "Claude 1,200 in / 80 out (1 calls) · Jev 300 in (1 calls)"
