"""The removed saved-notes feature has no HTTP, scope, or agent-tool surface."""

from test_api_v1 import api  # noqa: F401 (shared fake-phone server fixture)

from openmob.agent.tools import TOOLS, system_prompt


def test_saved_notes_routes_and_scopes_are_removed(api):
    for path in ("/memory", "/api/memory", "/v1/memory", "/docs/memory"):
        assert api.alice.console.get(path).status_code == 404
    for scope in ("memory:read", "memory:write"):
        response = api.alice.console.post("/api/developer/keys", json={"scopes": [scope]})
        assert response.status_code == 400
    for path in ("/", "/developer"):
        page = api.alice.console.get(path)
        assert page.status_code == 200
        assert "What I've learned" not in page.text
        assert "/api/memory" not in page.text
        assert "loadPendingNotes" not in page.text


def test_agent_cannot_offer_the_removed_remember_tool():
    assert "remember" not in {tool["name"] for tool in TOOLS}
    assert "call remember" not in system_prompt("android")
