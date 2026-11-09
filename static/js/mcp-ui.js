"use strict";
const mcpSchemaEditor = typeof module !== "undefined" && module.exports ? require("./mcp-schema.js") : window.MCPSchema;

// Independent UI adapter. Runtime, execution approval and validation remain in MCP-2.
function createMCPConsole({ document, fetch, confirm, storage, setTimer = setTimeout, clearTimer = clearTimeout }) {
  const node = (id) => document.getElementById(id);
  const ui = Object.fromEntries(["profile", "name", "add", "message", "servers", "tools", "tools-title",
    "call-panel", "arguments", "execute", "operation", "result", "refresh", "register", "dialog", "cancel", "submit",
    "profile-message", "register-message", "tools-hint", "registration-mode", "new-profile", "transport", "executable", "launch-arguments",
    "local-approval", "auto-form", "raw-mode", "raw-panel", "form-notice", "selected-tool", "result-summary", "result-details",
    "help", "help-dialog", "help-title", "help-content", "help-close", "argument-help"].map((id) => [id, node(`mcp-${id}`)]));
  let servers = [], tools = [], selected = null, tool = null, busy = false, visible = false, generation = 0;
  let profileCount = 0, registrationSaved = false;
  let argumentForm = null, profileRequest = null;
  const newProfileMode = () => ui["registration-mode"].value === "new";
  const pending = new Map();
  try {
    const saved = JSON.parse(storage?.getItem("local-ai-mcp-pending") || "[]");
    for (const entry of saved) if (Array.isArray(entry) && typeof entry[0] === "string") pending.set(entry[0], entry[1]);
  } catch (_) { /* Storage is optional; the server still enforces lifecycle safety. */ }
  const savePending = () => { try { storage?.setItem("local-ai-mcp-pending", JSON.stringify([...pending])); } catch (_) {} };
  const markPending = (id, operation = null) => { pending.set(id, operation); savePending(); };
  const clearPending = (id) => { pending.delete(id); savePending(); };
  const make = (tag, text = "") => { const item = document.createElement(tag); item.textContent = text; return item; };
  const base = (id) => `/mcp/servers/${encodeURIComponent(id)}`;
  const toolPath = (id, name) => `${base(id)}/tools/${encodeURIComponent(name)}`;
  const state = (server) => server.status?.state || "UNKNOWN";
  const currentServer = () => servers.find((server) => server.server_id === selected);
  const announce = (level, message) => {
    ui.message.textContent = message;
    if (ui.dialog.open) ui["register-message"].textContent = message;
    const log = node("output-log");
    if (log) {
      const entry = make("div", `${new Date().toLocaleTimeString()} [${level}] MCP · ${message}`);
      entry.className = "log-entry"; log.append(entry); log.scrollTop = log.scrollHeight;
    }
  };
  const update = () => {
    ui.refresh.disabled = busy;
    ui.add.disabled = busy;
    ui.submit.disabled = busy || (!newProfileMode() && !ui.profile.value) || registrationSaved;
    ui.cancel.disabled = busy;
    ui.profile.disabled = busy || newProfileMode(); ui.profile.required = !newProfileMode();
    ui.name.disabled = busy || Boolean(profileRequest); ui.name.required = newProfileMode();
    ui.name.placeholder = newProfileMode() ? "Required server name" : "Optional display name";
    ui["registration-mode"].disabled = busy || Boolean(profileRequest);
    ui["new-profile"].hidden = !newProfileMode();
    for (const field of ["transport", "executable", "launch-arguments"]) ui[field].disabled = busy || !newProfileMode() || Boolean(profileRequest);
    ui.executable.required = newProfileMode();
    ui.submit.textContent = profileRequest ? "Check approval & Register" : newProfileMode() ? "Request local approval" : "Register";
    ui.arguments.disabled = busy;
    ui["argument-help"].disabled = busy || !tool;
    ui["raw-mode"].disabled = busy || !argumentForm?.supported;
    if (argumentForm) argumentForm.setDisabled(busy);
    const server = currentServer();
    ui.execute.disabled = busy || !visible || !server?.execution_approved || state(server) !== "RUNNING" || !tool?.allowed || pending.has(selected);
    ui.operation.hidden = !pending.get(selected);
    ui.operation.disabled = busy;
    for (const button of ui.servers.querySelectorAll("button")) button.disabled = busy || button.dataset.unavailable === "true";
    for (const button of ui.tools.querySelectorAll("button")) button.disabled = busy || button.dataset.unavailable === "true";
  };
  const request = async (url, method = "GET", data) => {
    const controller = new AbortController();
    // Exceeds the core's normal 15s startup/30s tool wait. Expiry is not a rollback.
    const timer = setTimer(() => controller.abort(), 65000);
    try {
      const response = await fetch(url, { method, credentials: "same-origin", cache: "no-store", signal: controller.signal,
        headers: { "X-Local-AI-Control": "web-console", ...(data === undefined ? {} : { "Content-Type": "application/json" }) },
        ...(data === undefined ? {} : { body: JSON.stringify(data) }) });
      let body;
      try { body = await response.json(); }
      catch (_) { throw Object.assign(new Error("Invalid JSON response; completion may be unknown."), { uncertain: true }); }
      if (!response.ok) {
        const error = body.error || {};
        throw Object.assign(new Error(`HTTP ${response.status} · ${error.code || "mcp_http_error"} · ${error.message || "MCP request failed."}`),
          { operationId: error.operation_id, uncertain: response.status >= 500, code: error.code });
      }
      return body;
    } catch (error) {
      if (error.name === "AbortError") throw Object.assign(new Error("Browser timeout; server work may continue. Refresh status and explicitly Stop before retrying."), { uncertain: true });
      if (error instanceof TypeError) throw Object.assign(new Error("Controller connection failed; server completion may be unknown."), { uncertain: true });
      throw error;
    } finally { clearTimer(timer); }
  };
  const run = async (action) => {
    if (busy) return;
    busy = true; update();
    try { await action(); }
    catch (error) { announce("ERROR", error.message); }
    finally { busy = false; update(); }
  };
  const button = (parent, label, action, unavailable = false) => {
    const item = make("button", label); item.type = "button";
    item.dataset.unavailable = String(unavailable); item.addEventListener("click", () => run(action)); parent.append(item); return item;
  };
  const renderServers = () => {
    ui.servers.replaceChildren();
    if (!servers.length) { const row = make("tr"); const cell = make("td", "No registered MCP servers."); cell.colSpan = 5; row.append(cell); ui.servers.append(row); }
    for (const server of servers) {
      const id = server.server_id, row = make("tr"), actions = make("td");
      row.append(make("td", server.name), make("td", `Approved profile: ${server.profile_id}`), make("td", server.transport),
        make("td", `${state(server)}${pending.has(id) ? " · completion unconfirmed" : ""}${server.execution_approved ? "" : " · profile not approved"}`));
      button(actions, "Start", async () => {
        announce("RUNNING", "Starting selected MCP server.");
        await request(`${base(id)}/start`, "POST", {}); await loadServers(); announce("SUCCESS", "MCP server started.");
      }, state(server) !== "STOPPED" || !server.execution_approved || pending.has(id));
      button(actions, "Stop", async () => {
        if (!confirm(`Stop MCP server '${server.name}'? In-flight tool side effects cannot be undone.`)) return;
        await request(`${base(id)}/stop`, "POST", {}); clearPending(id);
        if (selected === id) clearTools(); await loadServers(); announce("SUCCESS", "MCP server stopped.");
      }, state(server) === "STOPPED" && !pending.has(id));
      button(actions, "Tools", async () => selectServer(id), state(server) !== "RUNNING" || !server.execution_approved);
      button(actions, "Delete", async () => {
        if (!confirm(`Delete MCP registration '${server.name}' and its tool permissions?`)) return;
        await request(base(id), "DELETE"); clearPending(id);
        if (selected === id) { selected = null; generation += 1; clearTools(); }
        await loadServers(); announce("SUCCESS", "MCP registration deleted.");
      }, state(server) !== "STOPPED" || pending.has(id));
      row.append(actions); ui.servers.append(row);
    }
    update();
  };
  const clearTools = ({ preserveResult = false } = {}) => {
    tools = []; tool = null; ui.tools.replaceChildren(); ui["call-panel"].hidden = true;
    argumentForm = null; ui["auto-form"].replaceChildren(); ui["raw-mode"].checked = false;
    ui["tools-hint"].textContent = "Select a running server to discover tools.";
    if (!preserveResult) { ui.result.textContent = ""; ui["result-summary"].textContent = "Select a tool to enter arguments and inspect results."; ui["result-details"].open = false; }
    update();
  };
  const loadServers = async () => {
    const data = await request("/mcp/servers");
    if (!Array.isArray(data.servers)) throw new Error("Invalid MCP server list.");
    servers = data.servers;
    if (selected && !currentServer()) { selected = null; generation += 1; clearTools(); }
    if (selected && state(currentServer()) !== "RUNNING") clearTools({ preserveResult: true });
    renderServers();
  };
  const renderTools = () => {
    ui.tools.replaceChildren();
    if (!tools.length) ui.tools.append(make("p", "This server exposes no tools."));
    for (const info of tools) {
      const card = make("article"); card.className = "mcp-tool-card";
      const toggle = button(card, info.name, async () => {
        if (tool?.name === info.name) {
          tool = null; argumentForm = null; ui["call-panel"].hidden = true;
        } else {
          tool = info; ui["call-panel"].hidden = false;
          ui["selected-tool"].textContent = `Tool Arguments · ${info.name}`;
          argumentForm = mcpSchemaEditor.render(document, ui["auto-form"], info.input_schema);
          ui["raw-mode"].checked = !argumentForm.supported;
          ui["auto-form"].hidden = !argumentForm.supported; ui["raw-panel"].hidden = argumentForm.supported;
          ui.arguments.value = JSON.stringify(mcpSchemaEditor.example(info.input_schema), null, 2);
          ui["form-notice"].textContent = argumentForm.supported ? "* required. Include optional fields explicitly. Server schema validation is authoritative."
            : "This schema needs Advanced Raw JSON. Review the schema and help before running.";
          ui["result-summary"].textContent = `Selected ${info.name}. ${info.allowed ? "Review arguments before approval." : "Allow this tool before execution."}`;
          ui.result.textContent = ""; ui["result-details"].open = false;
        }
        for (const row of ui.tools.querySelectorAll("article")) {
          const active = row.dataset.tool === tool?.name;
          row.querySelectorAll("button")[0].setAttribute("aria-expanded", String(active));
          row.querySelectorAll("div")[0].hidden = !active;
        }
        update();
      });
      toggle.className = "mcp-tool-toggle"; toggle.setAttribute("aria-expanded", "false"); card.dataset.tool = info.name;
      const detail = make("div"); detail.hidden = true; detail.className = "mcp-tool-details";
      detail.append(make("p", info.description || "No tool description provided."), make("p", info.allowed ? "Allowed for current schema" : "Blocked (default)"));
      const schemaDetail = make("details"); schemaDetail.append(make("summary", "Input JSON Schema"), make("pre", JSON.stringify(info.input_schema, null, 2))); detail.append(schemaDetail);
      button(detail, info.allowed ? "Block tool" : "Allow tool", async () => {
        const id = selected, version = generation;
        if (!confirm(`${info.allowed ? "Block" : "Allow"} tool '${info.name}' on '${currentServer().name}'? Each call still requires approval.`)) return;
        await request(`${toolPath(id, info.name)}/permission`, "PUT", { allowed: !info.allowed });
        if (visible && selected === id && generation === version) await selectServer(id);
        announce("SUCCESS", "MCP tool permission updated.");
      });
      card.append(detail);
      ui.tools.append(card);
    }
    update();
  };
  const selectServer = async (id) => {
    selected = id; const version = ++generation; clearTools();
    const data = await request(`${base(id)}/tools`);
    if (!visible || selected !== id || generation !== version) return;
    if (!Array.isArray(data.tools)) throw new Error("Invalid MCP tool list.");
    tools = data.tools; ui["tools-hint"].textContent = `Server: ${currentServer()?.name || id}`;
    renderTools(); announce("SUCCESS", "MCP tool discovery completed.");
  };
  const refresh = () => run(async () => {
    const data = await request("/mcp/profiles");
    if (!Array.isArray(data.profiles)) throw new Error("Invalid MCP profile list.");
    profileCount = data.profiles.length;
    ui.profile.replaceChildren();
    const placeholder = make("option", data.profiles.length ? "Select approved profile" : "No approved profiles — configure LOCAL_AI_MCP_APPROVALS and restart Controller");
    placeholder.value = ""; ui.profile.append(placeholder); ui.profile.value = "";
    for (const profile of data.profiles) { const option = make("option", profile.profile_id); option.value = profile.profile_id; ui.profile.append(option); }
    await loadServers(); clearTools({ preserveResult: true });
    if (registrationSaved) {
      registrationSaved = false; ui.name.value = ""; ui.profile.value = "";
      closeRegistration();
    }
    announce("INFO", "MCP registrations refreshed. Discover Tools again for current permissions/schema.");
  });
  const closeRegistration = () => {
    if (ui.dialog.open) ui.dialog.close();
    if (visible) ui.add.focus();
  };
  const cancelRegistration = () => run(async () => {
    if (profileRequest) {
      await request(`/mcp/profile-requests/${encodeURIComponent(profileRequest.request_id)}`, "DELETE");
      profileRequest = null; ui["local-approval"].hidden = true;
    }
    closeRegistration();
  });
  ui.add.addEventListener("click", () => {
    if (busy || !visible || ui.dialog.open) return;
    ui["register-message"].textContent = "";
    ui["profile-message"].textContent = profileCount ? "Only owner-approved local profiles can be registered."
      : "No approved profiles. Configure LOCAL_AI_MCP_APPROVALS and restart Controller, then Refresh MCP.";
    ui.dialog.showModal(); update();
  });
  ui.cancel.addEventListener("click", cancelRegistration);
  ui.dialog.addEventListener("cancel", (event) => {
    event.preventDefault();
    if (!busy) return cancelRegistration();
  });
  ui.register.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!ui.dialog.open || registrationSaved || !visible) return;
    return run(async () => {
      let profile = ui.profile.value;
      const name = ui.name.value.trim();
      if (Array.from(name).length > 128) throw new Error("Server name must be at most 128 characters.");
      if (newProfileMode()) {
        if (!name) throw new Error("A server name is required for a new profile.");
        if (!profileRequest) {
          const executable = ui.executable.value.trim();
          const args = ui["launch-arguments"].value.split(/\r?\n/).filter((item) => item !== "");
          if (!executable) throw new Error("An absolute executable path is required.");
          if (!confirm(`Request local owner approval?\nName: ${name}\nTransport: ${ui.transport.value}\nExecutable: ${JSON.stringify(executable)}\nArguments: ${JSON.stringify(args)}\nNo command starts. Approval must happen in a separate local terminal.`)) return;
          const data = await request("/mcp/profile-requests", "POST", { name, transport: ui.transport.value, executable, arguments: args });
          profileRequest = data.request;
          ui["local-approval"].textContent = "In a local PowerShell terminal, run and review:\n& " + profileRequest.approval_command.map((part) => "'" + part.replace(/'/g, "''") + "'").join(" ") + "\nThen click Check approval & Register. The command only reviews/persists this profile; it does not run it.";
          ui["local-approval"].hidden = false;
          ui["register-message"].textContent = "Pending local owner approval. Browser confirmation alone cannot approve execution.";
          return;
        }
        const data = await request(`/mcp/profile-requests/${encodeURIComponent(profileRequest.request_id)}`);
        if (data.request.status !== "approved") throw new Error("Local owner approval is still pending. Run the displayed terminal review command first.");
        profile = data.request.profile_id;
      }
      if (!profile) throw new Error("Select an approved profile.");
      await request("/mcp/servers", "POST", { profile_id: profile, ...(name ? { name } : {}) });
      registrationSaved = true;
      // If list refresh fails, keep the form open and prevent a duplicate POST.
      ui["register-message"].textContent = "Server registered. Refreshing the server list; do not register again.";
      await loadServers(); closeRegistration();
      if (profileRequest) {
        // Registration is complete even if non-executable proposal cleanup fails.
        try { await request(`/mcp/profile-requests/${encodeURIComponent(profileRequest.request_id)}`, "DELETE"); } catch (_) {}
        profileRequest = null; ui["local-approval"].hidden = true;
      }
      ui.name.value = ""; ui.profile.value = ""; registrationSaved = false;
      announce("SUCCESS", "MCP server registered; it remains stopped until Start.");
    });
  });
  ui.execute.addEventListener("click", () => run(async () => {
    const server = currentServer(), selectedTool = tool, version = generation;
    if (!visible || !server || state(server) !== "RUNNING" || !selectedTool?.allowed || pending.has(selected)) throw new Error("Discover an allowed tool on a running server; unconfirmed work requires result verification or Stop.");
    let args;
    if (!ui["raw-mode"].checked && argumentForm?.supported) args = argumentForm.read();
    else { try { args = JSON.parse(ui.arguments.value); } catch (_) { throw new Error("Arguments must be valid JSON."); } }
    if (!args || typeof args !== "object" || Array.isArray(args)) throw new Error("Arguments must be a JSON object.");
    const finiteJSON = (value) => {
      if (typeof value === "number" && !Number.isFinite(value)) throw new Error("Arguments must contain only finite numbers.");
      if (value && typeof value === "object") for (const item of Object.values(value)) finiteJSON(item);
    };
    finiteJSON(args);
    const snapshot = JSON.stringify(args);
    if (new TextEncoder().encode(snapshot).length > 64 * 1024) throw new Error("Arguments exceed 64 KiB.");
    if (!confirm(`Run once on '${server.name}'?\nTool: ${selectedTool.name}\nArguments:\n${snapshot}\nTools may have side effects. Cancellation does not undo them.`)) return;
    const id = server.server_id, path = toolPath(id, selectedTool.name);
    announce("RUNNING", "Requesting one-use MCP approval.");
    const approval = await request(`${path}/approve`, "POST", { arguments: args });
    if (!visible || selected !== id || generation !== version) { announce("INFO", "MCP call cancelled before dispatch because the view changed."); return; }
    if (typeof approval.approval_token !== "string" || !approval.approval_token) throw new Error("Missing MCP approval token; call not dispatched.");
    markPending(id);
    try {
      const data = await request(`${path}/call`, "POST", { arguments: args, approval_token: approval.approval_token });
      if (!data.result || typeof data.result.is_error !== "boolean") throw Object.assign(new Error("Invalid MCP call result; completion requires verification or Stop."), { uncertain: true });
      clearPending(id);
      if (visible && selected === id && generation === version) showResult(data);
      announce(data.result?.is_error ? "ERROR" : "SUCCESS", `MCP call ${data.result?.is_error ? "returned a tool error" : "completed"} · ${Number(data.elapsed_seconds || 0).toFixed(3)}s.`);
    } catch (error) {
      if (error.uncertain || error.operationId) markPending(id, error.operationId || null); else clearPending(id);
      if (visible && selected === id && generation === version) {
        ui.result.textContent = `${error.message}\nNo automatic retry. Check pending result, or explicitly Stop and Start.`;
        ui["result-summary"].textContent = ui.result.textContent;
      }
      throw error;
    } finally { await loadServers(); }
  }));
  ui.operation.addEventListener("click", () => run(async () => {
    const id = selected, operation = pending.get(id), version = generation;
    if (!operation) return;
    const data = await request(`${base(id)}/operations/${encodeURIComponent(operation)}`);
    if (visible && selected === id && generation === version) showResult(data);
    if (["SUCCEEDED", "FAILED", "CANCELLED"].includes(data.operation?.state)) clearPending(id);
    await loadServers(); announce("INFO", "MCP operation state checked. No tool was retried.");
  }));
  ui.profile.addEventListener("change", update);
  ui["registration-mode"].addEventListener("change", update);
  ui["raw-mode"].addEventListener("change", () => {
    if (busy) return;
    if (ui["raw-mode"].checked && argumentForm?.supported) {
      try { ui.arguments.value = JSON.stringify(argumentForm.read(), null, 2); } catch (_) { /* Raw mode can repair incomplete fields. */ }
    } else if (tool && argumentForm?.supported) {
      // Switching back rebuilds from the current schema/defaults, not stale JSON.
      argumentForm = mcpSchemaEditor.render(document, ui["auto-form"], tool.input_schema);
    }
    ui["auto-form"].hidden = ui["raw-mode"].checked; ui["raw-panel"].hidden = !ui["raw-mode"].checked; update();
  });
  const showResult = (data) => {
    ui.result.textContent = JSON.stringify(data, null, 2); ui["result-details"].open = false;
    const outcome = data.result || data.operation?.result;
    const text = (outcome?.content || []).filter((item) => item.type === "text" || typeof item.text === "string").map((item) => item.text).join("\n");
    const summary = text || (outcome?.structured_content ? JSON.stringify(outcome.structured_content, null, 2) : data.operation?.state || "Completed; open Raw JSON response for details.");
    ui["result-summary"].textContent = (outcome?.is_error ? "Tool error\n" : "") + summary.slice(0, 4000) + (summary.length > 4000 ? "\n… Open Raw JSON for the complete result." : "");
  };
  const showHelp = (info) => {
    ui["help-title"].textContent = info ? `Tool Help · ${info.name}` : "MCP Manager Help";
    ui["help-content"].replaceChildren();
    if (info) {
      ui["help-content"].append(make("p", info.description || "No description provided."),
        make("p", `Required arguments: ${(Array.isArray(info.input_schema?.required) ? info.input_schema.required : []).join(", ") || "none"}`),
        make("p", "Example only — review before use:"), make("pre", JSON.stringify(mcpSchemaEditor.example(info.input_schema), null, 2)));
    } else ui["help-content"].append(make("p", "1. Add Server: select an approved profile, or submit a new executable/argument list for review in a separate local terminal."),
      make("p", "2. Register stays STOPPED. Start explicitly, then discover Tools. Select a tool name to expand it; select again to collapse."),
      make("p", "3. Review description/schema and Allow tool. Fill required arguments; enable Include for optional fields. Use Advanced Raw JSON for unsupported schemas."),
      make("p", "4. Approve & Run once is a manual administrator diagnostic. Review exact arguments each time. One-use approvals expire after 120 seconds; side effects are not rolled back."),
      make("p", "5. Timeout/connection loss never triggers automatic retries. Check the pending result or explicitly Stop before reconnecting. Schema refresh clears inputs and permission must match the current schema."));
    ui["help-dialog"].showModal();
  };
  ui.help.addEventListener("click", () => showHelp());
  ui["argument-help"].addEventListener("click", () => { if (!busy && visible && tool) showHelp(tool); });
  ui["help-close"].addEventListener("click", () => ui["help-dialog"].close());
  ui.refresh.addEventListener("click", refresh);
  const open = () => { visible = true; generation += 1; return refresh(); };
  const leave = () => { visible = false; closeRegistration(); if (ui["help-dialog"].open) ui["help-dialog"].close(); generation += 1; tool = null; ui["call-panel"].hidden = true; update(); };
  node("server-button").addEventListener("click", open);
  node("guide-button").addEventListener("click", leave);
  update();
  return { open, leave, refresh };
}

if (typeof module !== "undefined" && module.exports) module.exports = { createMCPConsole };
if (typeof document !== "undefined") document.addEventListener("DOMContentLoaded", () => {
  let storage;
  try { storage = window.sessionStorage; } catch (_) {}
  createMCPConsole({ document, fetch: window.fetch.bind(window), confirm: window.confirm.bind(window), storage });
});
