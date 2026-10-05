"""Developer console endpoints: sessions (API/MCP tasks), the account's own audit log, and the public
docs. Each account sees only its own rows; signed-out callers get 401; docs are an allowlist."""

import httpx
from test_api_v1 import LEAKS, api, run_task, wait_status  # noqa: F401  (api is a fixture)


def test_sessions_list_this_accounts_tasks_with_key_and_phone(api):
    task = run_task(api.alice, "open settings")
    wait_status(api.alice, task["id"], "succeeded")
    run_task(api.bob, "open settings")

    sessions = api.alice.console.get("/api/developer/sessions").json()["sessions"]
    assert [s["id"] for s in sessions] == [task["id"]]  # never bob's
    s = sessions[0]
    assert s["status"] == "succeeded" and s["instruction"] == "open settings"
    assert s["key_id"] == api.alice.key_id and s["key_name"] == "test"
    assert s["device_id"] == api.alice.device and s["device_name"]
    assert s["credits"] == s["steps"] * 3 and s["outcome"]["success"] is True


def test_sessions_filter_by_status_and_key(api):
    done = run_task(api.alice, "open settings")
    wait_status(api.alice, done["id"], "succeeded")
    asking = run_task(api.alice, "ask me something")
    wait_status(api.alice, asking["id"], "needs_input")
    get = lambda **q: [s["id"] for s in api.alice.console.get("/api/developer/sessions", params=q).json()["sessions"]]
    assert get(status="needs_input") == [asking["id"]]  # live status, not the ledger's "queued"
    assert get(status="succeeded") == [done["id"]]
    assert set(get(key_id=api.alice.key_id)) == {done["id"], asking["id"]}
    assert get(key_id=api.bob.key_id) == []
    assert get(limit=1) == [asking["id"]]
    api.alice.v1.post(f"/v1/tasks/{asking['id']}/cancel")


def test_session_detail_has_public_events_and_is_private(api):
    task = run_task(api.alice, "ask me something")
    wait_status(api.alice, task["id"], "needs_input")
    api.alice.v1.post(f"/v1/tasks/{task['id']}/answer", json={"answer": "work"})
    wait_status(api.alice, task["id"], "succeeded")

    r = api.alice.console.get(f"/api/developer/sessions/{task['id']}")
    assert r.status_code == 200
    detail = r.json()
    types = [e["type"] for e in detail["events"]]
    assert "question" in types and types[-1] == "outcome"
    assert detail["key_name"] == "test" and detail["status"] == "succeeded"
    assert not LEAKS.search(r.text)  # no model, provider or token counts
    assert api.bob.console.get(f"/api/developer/sessions/{task['id']}").status_code == 404
    assert api.alice.console.get("/api/developer/sessions/nope").status_code == 404


def test_session_events_survive_a_restart_from_history(api):
    task = run_task(api.alice, "open settings")
    wait_status(api.alice, task["id"], "succeeded")
    api.manager.get(api.alice.device).coordinator.task_info.clear()  # as after a restart: only the stores remain
    detail = api.alice.console.get(f"/api/developer/sessions/{task['id']}").json()
    assert detail["events"] and detail["events"][-1]["type"] == "outcome"


def test_logs_are_this_accounts_own_and_filterable(api):
    task = run_task(api.alice, "open settings")
    wait_status(api.alice, task["id"], "succeeded")
    api.bob.console.post("/api/developer/keys", json={"name": "bobs"})

    logs = api.alice.console.get("/api/developer/logs").json()["logs"]
    actions = {row["action"] for row in logs}
    assert {"key.create", "task.create", "task.finished"} <= actions
    assert all("instruction_hash" not in row and "account_id" not in row for row in logs)
    bob_keys = {k["id"] for k in api.bob.console.get("/api/developer/keys").json()}
    assert not any(row["key_id"] in bob_keys for row in logs)

    only_tasks = api.alice.console.get("/api/developer/logs", params={"action": "task"}).json()["logs"]
    assert only_tasks and all(row["action"].startswith("task.") for row in only_tasks)
    assert api.alice.console.get("/api/developer/logs", params={"limit": 1}).json()["logs"].__len__() == 1


def test_console_endpoints_need_a_signed_in_owner(api):
    anonymous = httpx.Client(base_url=api.base)
    for path in ("/api/developer/sessions", "/api/developer/sessions/x", "/api/developer/logs"):
        assert anonymous.get(path).status_code == 401
        assert api.alice.v1.get(path).status_code == 401  # an API key is not a console session


def test_docs_serve_only_the_allowlist(api):
    anonymous = httpx.Client(base_url=api.base)
    for name in ("quickstart", "concepts", "phone-setup", "api", "mcp", "examples", "memory", "webhooks", "safety",
                 "conventions", "errors", "testing", "self-hosting", "api.md"):
        r = anonymous.get(f"/docs/{name}")
        assert r.status_code == 200, name
        assert r.headers["content-type"].startswith("text/markdown") and r.text.startswith("# ")
    for name in ("architecture", "phone-protocol", "..%2Fpyproject.toml", "../README.md", "%2e%2e%2fREADME",
                 "research", "api.md.bak", ""):
        assert anonymous.get(f"/docs/{name}").status_code == 404, name


def test_every_public_doc_is_in_the_console_and_links_only_to_public_docs():
    import re
    from pathlib import Path

    from openmob.server import PUBLIC_DOCS

    html = (Path(__file__).parent.parent / "openmob" / "static" / "developer.html").read_text()
    docs_map = re.search(r"const DOCS = \{(.*?)\};", html, re.S).group(1)
    assert set(re.findall(r"\"?([\w-]+)\"?:", docs_map)) == set(PUBLIC_DOCS)
    for name in PUBLIC_DOCS:
        assert f'data-route="docs/{name}"' in html, name
    docs = Path(__file__).parent.parent / "docs"
    for filename in PUBLIC_DOCS.values():
        for target in re.findall(r"\]\((?:\./)?([\w-]+)\.md", (docs / filename).read_text()):
            assert target in PUBLIC_DOCS, f"{filename} links to non-public {target}.md"
    # The Docker image serves docs from its own copy: it must ship every public page.
    dockerfile = (Path(__file__).parent.parent / "Dockerfile").read_text()
    for filename in PUBLIC_DOCS.values():
        assert f"docs/{filename}" in dockerfile, f"Dockerfile doesn't copy docs/{filename}"
    # …and .dockerignore must let them into the build context.
    ignore = (Path(__file__).parent.parent / ".dockerignore").read_text().splitlines()
    if "docs" in ignore or "docs/*" in ignore:
        for filename in PUBLIC_DOCS.values():
            assert f"!docs/{filename}" in ignore, f".dockerignore keeps docs/{filename} out of the image"
