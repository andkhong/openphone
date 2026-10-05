"""Memory: pinned notes per account, about the user or about an app, with proposals."""

import pytest

from openmob.memory import MAX_TEXT, REMEMBER_INTENT, Memory, NoteError, render_app_notes, render_user_notes


@pytest.fixture
def mem(tmp_path):
    return Memory(tmp_path / "memory.sqlite")


def test_notes_are_per_account(mem):
    a = mem.add("alice", "I prefer dark mode")
    mem.add("bob", "Use my work account")
    assert [n["text"] for n in mem.list("alice")] == ["I prefer dark mode"]
    assert mem.get("bob", a["id"]) is None  # someone else's note doesn't exist for you
    assert mem.update("bob", a["id"], text="hacked") is None
    assert mem.delete("bob", a["id"]) is False
    assert mem.get("alice", a["id"])["text"] == "I prefer dark mode"
    assert "account_id" not in a


def test_user_notes_and_app_notes_are_separate(mem):
    mem.add("alice", "Call me Al")
    mem.add("alice", "Quests are under the scroll icon", package="com.netease.yysls")
    assert [n["text"] for n in mem.user_notes("alice")] == ["Call me Al"]
    assert [n["text"] for n in mem.app_notes("alice", "com.netease.yysls")] == ["Quests are under the scroll icon"]
    assert mem.app_notes("alice", "com.other") == []


def test_proposals_are_not_used_until_approved(mem):
    p = mem.add("alice", "The user likes lo-fi music", status="proposed", source="agent")
    assert mem.user_notes("alice") == []
    assert [n["id"] for n in mem.list("alice", status="proposed")] == [p["id"]]
    mem.update("alice", p["id"], status="active")
    assert [n["text"] for n in mem.user_notes("alice")] == ["The user likes lo-fi music"]


def test_repeats_dont_duplicate_and_confirm_a_proposal(mem):
    p = mem.add("alice", "Use my second account", status="proposed", source="agent")
    again = mem.add("alice", "use my  second account")  # the user says it themselves
    assert again["id"] == p["id"] and again["status"] == "active"
    assert len(mem.list("alice")) == 1


def test_validation(mem):
    with pytest.raises(NoteError):
        mem.add("alice", "   ")
    with pytest.raises(NoteError):
        mem.add("alice", "x" * (MAX_TEXT + 1))
    with pytest.raises(NoteError):
        mem.add("alice", "ok", status="sneaky")


def test_delete_all_only_touches_that_account(mem):
    mem.add("alice", "one")
    mem.add("alice", "two", package="com.x")
    mem.add("bob", "three")
    assert mem.delete_all("alice") == 2
    assert mem.list("alice") == [] and len(mem.list("bob")) == 1


def test_rendering_is_plain_text_for_any_model():
    assert render_user_notes([]) == ""
    text = render_user_notes([{"text": "I prefer dark mode"}])
    assert "- I prefer dark mode" in text and "remember" in text
    assert "com.x" in render_app_notes("com.x", [{"text": "Menu is top-left"}])


def test_remember_intent():
    assert REMEMBER_INTENT.search("Remember that I use my second account")
    assert REMEMBER_INTENT.search("from now on open YouTube Music, not YouTube")
    assert not REMEMBER_INTENT.search("open YouTube and play lo-fi")
