# Local AI Lab — Web Control Console v0.1

Local AI Lab은 개인 Windows PC에서 로컬 AI를 학습·실험하고 제어하기 위한 프로젝트입니다. 이 저장소는 Ollama의 Qwen3.5 9B 모델을 Flask HTTP Controller 및 브라우저 기반 Web Control Console과 연결합니다.

모델 추론과 대화 기록 처리는 로컬에서 이루어지며 외부 유료 API에 의존하지 않습니다. 서버는 기본적으로 loopback 주소에만 바인딩되어 LAN이나 인터넷에 공개되지 않습니다.

## 실행 환경

현재 개발·검증 기준 환경은 다음과 같습니다.

- OS: Windows 10 22H2
- CPU: AMD Ryzen 5 3600
- RAM: 32GB
- GPU: AMD Radeon RX 9060 XT, VRAM 16GB
- LLM runtime: Ollama
- Model: `qwen3.5:9b` (`qwen3.5:9b-q4_K_M`은 같은 모델 데이터의 별도 태그)
- Python: 3.11
- Flask: 3.1.2
- Werkzeug: 3.1.5
- Ollama API: `http://127.0.0.1:11434`
- Flask Web UI/API: `http://127.0.0.1:5000`

프런트엔드는 HTML5, CSS, Vanilla JavaScript로 작성되어 별도 빌드 도구나 패키지가 필요하지 않습니다.

## 설치

Python과 Ollama가 설치되어 있어야 합니다. Python 패키지는 Flask 하나만 직접 설치하면 되며 Werkzeug 등 필요한 의존성은 함께 설치됩니다.

```powershell
cd F:\LLM\AI_Project\myLocalAI
python -m pip install Flask==3.1.2
```

설치된 모델을 확인합니다.

```powershell
ollama list
```

목록에 다음 모델이 있어야 합니다.

```text
qwen3.5:9b
```

모델 저장소를 다른 드라이브에서 관리하려면 사용자 환경 변수 `OLLAMA_MODELS`를 설정하고 Ollama를 완전히 재시작해야 합니다.

```powershell
[Environment]::SetEnvironmentVariable("OLLAMA_MODELS", "F:\LLM\Models", "User")
```

## 실행 및 종료

### 1. Ollama 실행

Ollama 애플리케이션을 실행하거나 터미널에서 다음 명령을 사용합니다.

```powershell
ollama serve
```

Ollama 상태와 모델 적재 상태는 각각 다음 명령으로 확인합니다.

```powershell
ollama list
ollama ps
```

`ollama ps`의 `100% GPU`는 모델이 GPU에 전부 배치됐다는 의미이며 GPU 사용률이 항상 100%라는 의미는 아닙니다.

### 2. Flask Controller 실행

새 PowerShell을 열고 다음 명령을 실행합니다.

```powershell
cd F:\LLM\AI_Project\myLocalAI
python app.py
```

브라우저에서 다음 주소를 엽니다.

```text
http://127.0.0.1:5000
```

Flask Controller를 정상 종료하려면 실행한 PowerShell에서 `Ctrl+C`를 누릅니다.

Ollama 서버 종료는 Web UI의 상태 버튼 또는 Windows 프로세스 종료를 사용할 수 있습니다. 현재 Web UI의 종료 기능은 모든 `ollama.exe` 프로세스를 대상으로 하므로 다른 프로그램의 생성 작업도 중단할 수 있습니다. 종료 확인창의 경고를 확인한 후 사용하세요.

## Web UI 사용법

### A — Main Content

- 첫 화면에서 Local AI Lab과 Ollama 사용 설명서를 제공합니다.
- `Local AI Lab`을 누르면 안내 화면으로 돌아옵니다.
- `AI Serve Locals`를 누르면 서버 관리 화면으로 전환됩니다.

### B — Navigation

- `AI Serve Locals`: Flask Controller, Ollama 및 모델 상태 확인
- `Reserved 1~3`: 향후 확장용 비활성 메뉴

### C — Tool Calling

- `Health Check`: 선택 즉시 `/health` 호출
- `Chat`: Chat 입력 모드 선택
- `Reset Session`: 확인 대화상자 승인 후 현재 세션 초기화

### D — Input

- 최초 상태는 Locked입니다.
- Chat은 Unlock 후 사용할 수 있습니다.
- 기본 Session ID는 `default`입니다.
- Message 입력 후 `Enter`를 누르면 전송합니다.
- `Shift+Enter`는 줄바꿈입니다.
- Health와 Reset은 Locked 상태에서도 실행할 수 있습니다.
- 잠금은 오조작 방지 기능이며 인증이나 접근 제어가 아닙니다.

### E — Status / Output

- 요청 시각, API, Running/Success/Error 상태 표시
- AI 답변과 전체 응답 시간 표시
- 입력·생성 토큰 수와 tokens/s 표시
- Ollama 나노초 지표를 초 단위로 변환
- 로그 자동 스크롤
- `Clear`는 브라우저 로그만 삭제하며 서버 대화 문맥은 유지

## 주요 기능

- Flask Controller와 Ollama 상태 구분
- Qwen3.5 9B 로컬 Chat
- Session ID별 대화 문맥
- 세션 Reset
- 같은 세션의 동시 Chat/Reset 직렬화
- 서로 다른 세션의 독립 실행
- `think: false`, `stream: false`
- 응답 시간과 Ollama 생성 통계
- 구조화된 JSON 오류 응답
- Ollama 서버 시작·종료
- localhost 전용 바인딩 및 Flask Debug 비활성

## API

정상 API 응답 구조는 v0.1에서 유지됩니다. 오류는 다음 공통 형식을 사용합니다.

```json
{
  "error": {
    "code": "error_code",
    "message": "오류 설명"
  }
}
```

### `GET /health`

Flask Controller 및 Ollama 연결 상태를 확인합니다. 모델을 적재하거나 생성 요청을 실행하지 않습니다.

정상 응답, HTTP 200:

```json
{
  "controller": "ok",
  "ollama_reachable": true,
  "model": "qwen3.5:9b"
}
```

Flask는 실행 중이지만 Ollama에 연결할 수 없으면 같은 JSON 구조로 `ollama_reachable: false`와 HTTP 503을 반환합니다.

### `POST /chat`

요청:

```json
{
  "session_id": "default",
  "message": "안녕하세요"
}
```

정상 응답, HTTP 200:

```json
{
  "session_id": "default",
  "model": "qwen3.5:9b",
  "message": "AI 응답",
  "elapsed_seconds": 1.234,
  "metrics": {
    "eval_count": 20,
    "prompt_eval_count": 10,
    "eval_duration": 1000000000,
    "tokens_per_second": 20.0
  }
}
```

메트릭은 Ollama가 제공한 필드만 포함되며 `tokens_per_second`를 계산할 수 없으면 `null`입니다.

### `POST /sessions/<session_id>/reset`

지정 Session ID의 RAM 대화 기록을 초기화합니다. 요청 본문은 없습니다.

```powershell
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:5000/sessions/default/reset"
```

정상 응답, HTTP 200:

```json
{
  "session_id": "default",
  "status": "reset"
}
```

존재하지 않는 세션도 같은 응답을 반환하지만 새로운 RAM 세션 객체는 만들지 않습니다.

### `POST /ollama/start`

고정된 `ollama serve` 명령으로 Ollama 서버를 시작합니다. 요청 본문은 없으며 다음 헤더가 필요합니다.

```text
X-Local-AI-Control: web-console
```

PowerShell 예시:

```powershell
Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:5000/ollama/start" `
  -Headers @{ "X-Local-AI-Control" = "web-console" }
```

### `POST /ollama/stop`

Windows에서 고정된 `taskkill /IM ollama.exe /F` 명령으로 Ollama 프로세스를 종료합니다. 요청 본문은 없으며 `/ollama/start`와 같은 제어 헤더가 필요합니다.

```powershell
Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:5000/ollama/stop" `
  -Headers @{ "X-Local-AI-Control" = "web-console" }
```

정상 제어 응답:

```json
{
  "action": "start",
  "status": "started",
  "ollama_reachable": true
}
```

제어 헤더는 강력한 인증 수단이 아닙니다. 이 API는 `127.0.0.1` 전용이라는 전제에서만 사용해야 합니다.

## 사용 제한

| 항목 | 제한 |
|---|---:|
| 활성 RAM 세션 | 최대 60개 |
| Session ID | Python `len()` 기준 최대 128자 |
| Message | Python `len()` 기준 최대 10,000자 |
| HTTP 요청 본문 | 최대 1MB |
| Ollama 생성 요청 제한 시간 | 180초 |
| 브라우저 요청 제한 시간 | 190초 |

- 대화 기록은 현재 Flask 프로세스 RAM에만 저장됩니다.
- Flask를 종료하거나 재시작하면 모든 대화 기록이 사라집니다.
- Reset된 비활성 세션은 RAM 활성 세션 슬롯에서 제거될 수 있습니다.
- 활성 세션 수는 제한되지만 세션별 대화 메시지 수는 제한되지 않습니다.
- 대화 기록 길이와 모델 컨텍스트 길이는 다릅니다. RAM에 기록이 남아 있어도 모델의 컨텍스트 한도를 초과하면 전체 대화를 정확히 해석하지 못할 수 있습니다.
- 자동 요약, 오래된 기록 절단 및 SQLite 영구 저장은 v0.1에 포함되지 않습니다.
- Thinking은 항상 OFF입니다.

## 테스트

### Python 단위 테스트

```powershell
cd F:\LLM\AI_Project\myLocalAI
python -m unittest discover -s tests -v
```

### Python 문법 검사

```powershell
python -m py_compile app.py ai_controller.py ollama_runtime.py
```

### JavaScript 문법 검사

```powershell
node --check static/js/main.js
```

Python 단위 테스트는 Ollama 통신과 프로세스 제어를 Mock으로 대체합니다. 실제 모델, GPU, VRAM 또는 `ollama.exe`를 변경하지 않고 API 계약·오류 처리·세션 동시성·입력 경계를 검증합니다.

실제 Ollama 통합 테스트는 별도로 수행해야 합니다.

1. `ollama list`에서 `qwen3.5:9b`를 확인합니다.
2. `ollama serve`와 `python app.py`를 실행합니다.
3. 브라우저에서 Health Check를 실행합니다.
4. Chat을 Unlock하고 같은 Session ID로 연속 질문하여 문맥을 확인합니다.
5. 다른 Session ID가 기존 문맥을 공유하지 않는지 확인합니다.
6. Reset 확인창에서 취소하면 요청이 발생하지 않는지 확인합니다.
7. Reset 승인 후 동일 세션의 이전 문맥이 제거되는지 확인합니다.
8. Clear가 출력 로그만 지우고 대화 문맥은 유지하는지 확인합니다.
9. 긴 답변의 E 패널 스크롤과 작은 창의 반응형 레이아웃을 확인합니다.
10. HTML·Script 문자열이 코드로 실행되지 않고 텍스트로 표시되는지 확인합니다.

## Known Issues

### 실제 관찰된 이슈

- **ISSUE-001 — Reset Session 영향 범위:** 사용자 수동 테스트에서 한 세션의 Reset이 다른 세션에도 영향을 주는 것처럼 보이는 현상이 있었습니다. 자동 테스트에서는 Session ID별 분리와 개별 Reset이 통과했으며 실제 원인은 아직 확인되지 않았습니다. **STEP 6 SQLite 영구 저장 구현 전에 실제 브라우저와 복수 Session ID로 반드시 재검증해야 합니다.**
- **ISSUE-002 — Qwen3.5 9B 문맥 해석:** 이전 메시지 배열이 정상 전달돼도 모델이 과거 문맥을 부정확하게 해석하는 사례가 관찰됐습니다. 이는 기록 전달과 모델의 의미 해석이 서로 다른 문제이므로 추가 관찰이 필요합니다.

### 코드 구조상 잠재 위험 및 현재 제한

- **세션별 기록 무제한 증가:** 활성 세션은 60개로 제한되지만 각 세션의 메시지 수는 제한되지 않습니다. RAM, 직렬화 시간, 입력 토큰과 지연이 계속 증가할 수 있습니다.
- **다중 요청 자원 경쟁:** 서로 다른 세션의 Ollama 요청은 동시에 실행될 수 있어 CPU, RAM, GPU VRAM 및 Ollama 대기열이 경쟁할 수 있습니다.
- **전체 Ollama 프로세스 종료:** Web UI의 종료 기능은 모든 `ollama.exe` 프로세스를 강제로 종료하므로 다른 프로그램의 Ollama 작업도 중단할 수 있습니다.
- **브라우저 로그 무제한 증가:** E 패널 로그는 Clear 또는 새로고침 전까지 계속 누적되어 브라우저 메모리를 사용할 수 있습니다.
- **Flask 개발 서버:** 현재 서버는 localhost 개발·실험용이며 고가용성, 프로세스 감독 또는 높은 동시 접속용이 아닙니다.
- **Reset API의 로컬 CSRF 가능성:** Reset API에는 별도 제어 헤더나 인증이 없습니다. loopback 전용으로 위험 범위가 제한되지만, 신뢰할 수 없는 웹페이지가 localhost 요청을 시도할 가능성은 남아 있습니다.
- **Ollama 시작 실패 후 잔여 프로세스:** `ollama serve`가 제한 시간 안에 Online이 되지 않으면 오류를 반환하지만 시작된 프로세스가 남을 가능성이 있습니다.
- **AI 응답 크기 제한 없음:** 브라우저 요청은 1MB로 제한되지만 Ollama 응답 크기는 제한하지 않아 긴 응답이 RAM, 대화 기록 및 브라우저 로그를 증가시킬 수 있습니다.

위 잠재 위험은 모두 실제 장애로 재현됐다는 의미가 아닙니다. 실제 관찰된 ISSUE-001 및 ISSUE-002와 구분해서 관리합니다.

## 보안 및 운영 주의사항

- Flask와 Ollama는 `127.0.0.1`에서만 사용합니다.
- Flask Debugger는 활성화하지 않습니다.
- Web UI의 Lock은 인증이나 보안 기능이 아닙니다.
- 대화나 토큰 같은 개인 데이터를 로그·Git 저장소에 커밋하지 않습니다.
- Web UI에 임의 CMD 또는 PowerShell 명령 실행 기능은 없습니다.
- 서버 오류는 구조화된 JSON으로 반환되며 HTTP 응답에 traceback을 포함하지 않습니다.
- 동적 브라우저 출력은 `textContent`를 사용해 HTML과 Script 문자열을 실행하지 않습니다.

## 로드맵

- **STEP 6:** SQLite 기반 대화 영구 저장
- **STEP 7:** allowlist와 입력 검증을 적용한 LLM Tool Calling
- **STEP 8:** 로컬 STT/TTS 기반 음성 AI
- **이후:** 장기 기억 검색, 대화 요약, 페르소나, Bluetooth·게임패드·휴대전화 등 주변기기 연동

STEP 6을 시작하기 전에 ISSUE-001의 실제 세션 초기화 범위를 복수 Session ID와 실제 브라우저 환경에서 반드시 재검증해야 합니다. SQLite에 잘못된 Reset 의미를 영구 저장 구조로 옮기지 않는 것이 중요합니다.

## v0.1 상태

Web Control Console v0.1은 로컬 Ollama 상태 확인, Chat, Session Reset, 입력 잠금, 응답 성능 출력, 제한된 Ollama 프로세스 제어 및 기본 안정성 검증을 완료한 상태입니다. 장기 기억과 자동 Tool Calling은 포함되지 않습니다.
