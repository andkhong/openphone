from types import SimpleNamespace

from openmob.usage import Usage


def test_counts_and_summary():
    u = Usage()
    u.add_claude(SimpleNamespace(input_tokens=1200, output_tokens=80, cache_read_input_tokens=0), "claude-sonnet-5-5")
    u.add_claude(None)  # responses without usage (fakes) are ignored
    u.add_jev(300)
    assert u.as_dict() == {"model_input": 1200, "model_output": 80, "model_cache_read": 0, "model_calls": 1,
                           "jev_input": 300, "jev_calls": 1,
                           "by_model": {"claude-sonnet-5-5": {"input": 1200, "output": 80, "calls": 1}}}
    total = Usage()
    total.add(u)
    total.add(u)
    assert total.model_input == 2400 and total.jev_calls == 2
    assert total.by_model["claude-sonnet-5-5"] == {"input": 2400, "output": 160, "calls": 2}
    assert u.summary() == "claude-sonnet-5-5: 1,200 in / 80 out"  # no Jev in what users see
