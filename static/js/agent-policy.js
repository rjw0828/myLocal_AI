/* ApprovalPolicy controls inside the existing MCP Manager. No Tool execution. */
document.addEventListener("DOMContentLoaded", () => {
  const panel = document.getElementById("agent-policy");
  if (!panel) return;
  const get = (name) => document.getElementById(`agent-policy-${name}`);
  const mode = get("mode"), enabled = get("auto"), tools = get("tools");
  const resource = get("resource"), readOnly = get("readonly"), trusts = get("trusts");
  const status = get("status"), info = get("info");
  let server = null, data = null, selectedTool = null, argumentsReader = null;
  let generation = 0, busy = false;

  async function request(path, method = "GET", body) {
    const response = await fetch(path, { method, credentials: "same-origin", cache: "no-store",
      headers: { "X-Local-AI-Control": "web-console", ...(body === undefined ? {} : { "Content-Type": "application/json" }) },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
    let packet;
    try { packet = await response.json(); } catch (_) { throw new Error(`HTTP ${response.status} · invalid response`); }
    if (!response.ok) throw new Error(packet.error?.code || `HTTP ${response.status}`);
    return packet;
  }

  function option(value, label = value) {
    const item = document.createElement("option"); item.value = value; item.textContent = label; return item;
  }

  function showSummary(result) {
    const root = result.server.options?.root || "None (execution configuration is read-only here)";
    status.textContent = `Server: ${result.server.name} · ${result.server.status?.state || "UNKNOWN"} · Root: ${root}`;
    info.textContent = JSON.stringify({ approval_mode: result.server.approval_mode,
      automatic_enabled: result.server.auto_enabled, trusts: result.server.trusts,
      limitation: result.limitation }, null, 2);
  }

  async function refresh() {
    const id = server, version = ++generation;
    const result = await request(`/tool-chat/policy/${encodeURIComponent(id)}`);
    if (id !== server || version !== generation) return;
    data = result; mode.value = result.server.approval_mode; enabled.checked = result.server.auto_enabled;
    tools.replaceChildren();
    result.tools.forEach((tool) => tools.append(option(tool.name)));
    if (selectedTool && result.tools.some((tool) => tool.name === selectedTool)) tools.value = selectedTool;
    trusts.replaceChildren(option("", result.server.trusts.length ? "Select trust" : "No explicit trust"));
    result.server.trusts.forEach((trust) => trusts.append(option(trust.trust_id,
      `${trust.tool_name} · ${trust.resource || "server/tool scope"}`)));
    showSummary(result); panel.hidden = false;
  }

  document.addEventListener("local-ai-mcp-selected", (event) => {
    server = event.detail.serverId; selectedTool = null; argumentsReader = null;
    refresh().catch((error) => { info.textContent = error.message; panel.hidden = false; });
  });
  document.addEventListener("local-ai-mcp-tool-selected", (event) => {
    if (event.detail.serverId !== server) return;
    selectedTool = event.detail.tool; argumentsReader = event.detail.readArguments;
    if ([...tools.options].some((item) => item.value === selectedTool)) tools.value = selectedTool;
  });

  async function act(callback) {
    if (busy) return;
    busy = true;
    const controls = [...panel.querySelectorAll("button, input, select")];
    controls.forEach((control) => { control.disabled = true; });
    try { await callback(); await refresh(); }
    catch (error) { info.textContent = error.message; }
    finally { controls.forEach((control) => { control.disabled = false; }); busy = false; }
  }

  get("save").addEventListener("click", () => act(async () => {
    if (!server || !confirm(`Set ${mode.value} for this server? Automatic approval enabled: ${enabled.checked}`)) return;
    await request(`/tool-chat/policy/${encodeURIComponent(server)}/mode`, "PUT",
      { mode: mode.value, enable_auto: enabled.checked });
  }));
  get("trust").addEventListener("click", () => act(async () => {
    const tool = data?.tools.find((item) => item.name === tools.value);
    if (!tool || !confirm(`Trust the exact current schema and scope for '${tool.name}'?`)) return;
    const body = { tool: tool.name, catalog_fingerprint: tool.catalog_fingerprint,
      confirmed: true, verified_read_only: readOnly.checked };
    if (resource.value.trim()) body.resource = resource.value.trim();
    await request(`/tool-chat/policy/${encodeURIComponent(server)}/trust`, "POST", body);
  }));
  get("revoke").addEventListener("click", () => act(async () => {
    if (!server || !trusts.value || !confirm("Revoke only the selected trust?")) return;
    await request(`/tool-chat/policy/${encodeURIComponent(server)}/trust/${encodeURIComponent(trusts.value)}`, "DELETE");
  }));
  get("check").addEventListener("click", async () => {
    if (!server || !tools.value) return;
    try {
      const args = argumentsReader && tools.value === selectedTool ? argumentsReader() : {};
      const result = await request(`/tool-chat/policy/${encodeURIComponent(server)}/decision`, "POST",
        { tool: tools.value, arguments: args });
      info.textContent = JSON.stringify(result.decision, null, 2);
    } catch (error) { info.textContent = error.message; }
  });
});
