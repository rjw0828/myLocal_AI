# C-5B: existing Console MCP management integration

The existing MCP Manager remains the only management surface.  When the
experimental Agent backend is enabled, Start/Stop/Delete and Tool Allow/Block
are routed through `MCPManagement`; ordinary Console hosts retain the existing
`/mcp` behavior.  Registration, profile approval, manual one-use execution,
Registry storage, MCPService ownership and Tool Chat are not duplicated.

## HTTP adapter

- `POST /tool-chat/management/servers/<id>/connect`: explicit connection test,
  server start and Discovery only; it never calls a Tool.
- `POST /tool-chat/management/servers/<id>/stop`
- `DELETE /tool-chat/management/servers/<id>`
- `PUT /tool-chat/management/servers/<id>/tools/<tool>/permission`
- Existing `/tool-chat/policy/<id>` routes expose and update the real
  `ApprovalPolicy` mode, exact current Tool trusts, individual revocation and
  a read-only decision preview.

All routes retain the Console control header, loopback/same-origin checks,
no-store response policy and the existing sanitized error handlers.  None of
the management or policy routes creates a Tool approval token or calls a Tool.

Execution configuration is intentionally read-only in this step.  Changing an
executable, arguments, environment, Root or timeout requires stopping, deleting
and registering a newly reviewed profile through the existing flow.  The policy
panel shows the current verified Root and accepts a trust path only inside that
Root.  Discovery and Allow never create trust automatically.

## Manual verification procedure (requires separate process-start approval)

1. Start the isolated experimental Console with an approved reviewed MCP
   executable; do not use operating databases or approval files.
2. Open **AI Serve Locals**, register an approved profile and confirm it remains
   `STOPPED`.
3. Select **Start**, review the connection-only confirmation, and confirm the
   Tool list appears without a Tool result or operation.
4. Allow one Tool.  Verify the policy panel shows the same server and current
   verified Root.
5. Exercise each mode with automatic approval first disabled.  The decision
   preview must report `ASK`.  Add an exact trust, enable automatic approval,
   and verify `AUTO_ALLOW_SAFE` only auto-approves a verified read-only scope;
   `ALLOW_ALL` only auto-approves the exact trusted scope.
6. Change the current arguments outside the trusted path and verify `ASK` or
   `BLOCK` with its policy reason.  Add a new Tool or change permission/schema
   and verify no trust is inherited.
7. Revoke the selected trust and refresh/reselect the server.  Verify the trust
   remains absent and the decision returns to `ASK`.
8. Manual **Approve & Run once** is a separate test.  If authorized, run at most
   the explicitly approved Tool/arguments and confirm the one-use token cannot
   be replayed.  Stop the server and verify owned child processes exit.

Browser/manual results must be reported separately from DOM/Fetch and Flask
fixtures.  C-5A Deny, expiry and Session Reset remain separate acceptance work.
