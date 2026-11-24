/* Session permission only. No server/tool trust registration or Tool execution. */
document.addEventListener('DOMContentLoaded', () => {
  const get = id => document.getElementById(id);
  const session = get('session-id'), mode = get('agent-policy-mode');
  if (!session || !mode) return;
  const save = get('agent-policy-save'), refreshButton = get('agent-policy-refresh');
  const status = get('agent-policy-status');
  let generation = 0, busy = false, loadedSession = null;
  const valid = data => data && ['MANUAL','ALLOW_ALL'].includes(data.mode);
  function update() {
    mode.disabled = save.disabled = busy || !session.value || loadedSession !== session.value;
    refreshButton.disabled = !session.value;
  }
  async function request(id, method = 'GET', selected) {
    const response = await fetch(`/sessions/${encodeURIComponent(id)}/permission`, {
      method, credentials:'same-origin', cache:'no-store',
      headers:{'X-Local-AI-Control':'web-console', ...(method === 'PUT' ? {'Content-Type':'application/json'} : {})},
      ...(method === 'PUT' ? {body:JSON.stringify({mode:selected})} : {}),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error?.code || `HTTP ${response.status}`);
    if (!valid(data) || data.session_id !== id) throw new Error('invalid_session_permission_response');
    return data;
  }
  async function refresh() {
    const id = session.value, version = ++generation;
    loadedSession = null; mode.value = 'MANUAL'; update();
    if (!id) { status.textContent = '대화를 선택해주세요.'; return; }
    try {
      const data = await request(id);
      if (version !== generation || session.value !== id) return;
      mode.value = data.mode; loadedSession = id;
      status.textContent = `${data.mode === 'MANUAL' ? '호출마다 승인' : '이 Session에서 자동 승인'} · Enabled와 Root 제한은 유지`;
    } catch (error) {
      if (version === generation && session.value === id) status.textContent = `Permission 확인 실패 · ${error.message}`;
    } finally { update(); }
  }
  save.addEventListener('click', async () => {
    if (busy || loadedSession !== session.value || !session.value) return;
    const id = session.value, selected = mode.value;
    if (!['MANUAL','ALLOW_ALL'].includes(selected)) return;
    busy = true; update();
    try {
      await request(id, 'PUT', selected);
    } catch (error) {
      if (session.value === id) status.textContent = `저장 확인 필요 · ${error.message} · 자동 재시도하지 않습니다.`;
    } finally {
      // Read after success or a lost response; never repeat a PUT automatically.
      busy = false; await refresh();
    }
  });
  session.addEventListener('input', refresh);
  refreshButton.addEventListener('click', refresh);
  refresh();
});
