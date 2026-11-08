CREATE TABLE mcp_servers (
    server_id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    config_json TEXT NOT NULL,
    config_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE mcp_tool_permissions (
    server_id TEXT NOT NULL REFERENCES mcp_servers(server_id) ON DELETE CASCADE,
    tool_name TEXT NOT NULL,
    schema_hash TEXT NOT NULL,
    allowed INTEGER NOT NULL CHECK(allowed IN (0,1)),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (server_id, tool_name)
);
PRAGMA application_id = 1296257074;
PRAGMA user_version = 1;
