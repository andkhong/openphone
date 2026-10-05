"""The developer platform: one enforcement point (V1Service) behind several front doors.

- principal.py: who is calling (an om_live_ API key today; OAuth tokens later).
- service.py: V1Service, transport-free. Scopes, 404 isolation, quotas, audit and kill switches live here once.
- rest.py: the /v1 REST routes, the developer console and the admin switches, mapping errors to HTTP.
- mcp.py: the remote MCP server at /mcp, calling the same V1Service.
"""
