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

Python과 Ollama가 설치되어 있어야 합니다. Flask, 로컬 STT와 TTS는 프로젝트 가상환경에서 함께 실행합니다.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install faster-whisper==1.2.1 piper-tts==1.8.0 Flask==3.1.2 Werkzeug==3.1.5 Jinja2==3.1.6 itsdangerous==2.2.0 blinker==1.9.0 MarkupSafe==3.0.3
```

STT 모델은 `F:\LLM\Models\speech\whisper\faster-whisper-small`에 별도로 준비하며 Git에 추가하지 않습니다.
Piper 모델은 `F:\LLM\Models\speech\piper` 아래의 `ko_KR-kss-medium`, `en_US-lessac-medium` 디렉터리에 `.onnx`와 `.onnx.json` 파일을 함께 둡니다.

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
.\.venv\Scripts\python.exe app.py
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

### D — Input

- 최초 상태는 Locked입니다.
- Chat은 D를 Unlock하면 바로 입력·실행할 수 있습니다. C의 Health Check를 사용한 뒤에도 Enter는 Chat을 전송합니다.
- RAM 세션이 없으면 빈 상태를 표시합니다. 첫 접속·마지막 세션 삭제·새로고침에서 세션을 자동 생성하지 않으며, D의 `+ New Chat`을 누르면 서버에서 새 내부 ID와 표시 번호를 받습니다.
- D에는 `Session N — 첫 질문 일부...`가 표시되고 Session ID는 직접 입력하지 않습니다. 질문 초안은 세션 전환 시 자동으로 덮어쓰지 않습니다.
- Message 입력 후 `Enter`를 누르면 전송합니다.
- `Shift+Enter`는 줄바꿈입니다.
- Health와 Reset은 Locked 상태에서도 실행할 수 있습니다.
- 잠금은 오조작 방지 기능이며 인증이나 접근 제어가 아닙니다.
- Unlock 후 `Record`를 누르면 Push-to-Talk 녹음을 시작하고 `Stop`을 누르면 로컬 STT를 실행합니다.
- 언어는 Auto, 한국어, English 중 선택하며 최대 녹음 시간은 30초입니다.
- 수동 모드에서 인식된 문장은 Message에만 반영되며 Chat으로 자동 전송되지 않습니다.
- 기존 Message가 있으면 확인 후 교체하며, 녹음 중 세션이 바뀌면 결과를 적용하지 않습니다.
- 브라우저 마이크 권한이 필요하며 음성 원본은 브라우저 메모리와 STT 요청 처리 중에만 유지됩니다.

#### Voice Conversation (선택적 자동 음성 대화)

- 기본값은 OFF이며 새로고침하면 OFF로 돌아옵니다. ON으로 변경하는 것만으로 마이크나 API는 실행하지 않습니다.
- Unlock 후 `Voice Conversation ON`으로 바꿉니다. D의 `Voice`는 STT 인식 언어이며 기본값은 Auto입니다. 자동 감지 품질이 낮을 때 한국어 또는 English로 고정하세요. E의 `AI Language`는 Qwen 답변과 TTS 음성 언어를 정합니다.
- D Message가 비어 있을 때 Record를 누르면 한 턴을 시작합니다. `Stop & Send` 또는 30초 제한으로 녹음을 끝내면 STT → Chat → TTS → 재생을 순서대로 실행합니다. 완료 후 마이크는 꺼지고 다음 Record를 기다립니다.
- 인식된 질문과 AI 답변은 E에 표시하며 D의 초안은 덮어쓰지 않습니다. 기존 초안이 있으면 자동 녹음 시작을 거부합니다.
- 상태는 Idle / Recording(권한 요청 포함) / Transcribing / Thinking / Synthesizing / Speaking / Error / Cancelled로 표시합니다.
- Cancel, 모드 OFF, Lock, 세션 변경, Reset 승인, 관리 화면 이동 또는 페이지 종료 시 후속 단계와 재생을 중단합니다. 녹음 도중 Stop & Send는 전송, Cancel은 녹음 폐기라는 차이가 있습니다.
- 자동 턴 중 수동 Chat/Record/Play 중복 실행을 막습니다. Lock, Cancel, Reset 확인 및 세션 변경은 계속 사용할 수 있습니다.
- 음성 턴에만 1~3문장·약 400자 이내 답변을 유도합니다. 500자를 넘으면 전문을 E에 보존하고 TTS만 생략합니다. 답변을 자르거나 자동 분할하지 않습니다.
- 브라우저가 자동 재생을 거부하면 텍스트와 E의 Play를 유지합니다. 오류를 확인한 뒤 Play를 직접 누르세요.

취소된 Chat은 서버에서 계속 실행되거나 기록에 저장될 수 있습니다. Thinking 중 취소는 응답을 관찰하며 해당 세션의 새 Chat을 차단합니다. Reset을 승인하면 이 Chat의 완료 응답을 기다린 뒤 Reset을 실행합니다.

네트워크 오류·타임아웃·해석 불가능한 응답으로 Chat 완료 여부를 확인할 수 없으면 해당 세션을 차단합니다. 자동 재시도나 자동 Reset은 하지 않으며 시간 경과/Health 성공만으로 풀지 않습니다. History API는 현재 저장된 기록을 보여주지만 진행 중인 요청이 나중에 저장될 가능성까지 배제할 수 없어 차단을 자동 해제하지 않습니다. **D의 `+ New Chat`으로 새 세션을 생성**하세요. 불확실한 세션에는 Reset도 전송하지 않습니다. 진행 중/불확실 세션 ID와 마지막 선택 ID만 sessionStorage에 보관하며 대화 전문은 저장하지 않습니다. 저장소 접근이 제한된 브라우저에서는 현재 탭 메모리 안에서만 보호됩니다. 다른 탭/외부 클라이언트까지 이 브라우저 관리자가 제어하지는 않습니다.

### E — Conversation

- 선택된 RAM 세션의 사용자 메시지와 AI 답변을 구분하여 표시하고, 새로고침 또는 세션 전환 시 서버 History API로 복원합니다.
- E 하단 `Session List`를 누르면 위로 펼쳐지는 목록에서 세션을 선택할 수 있습니다. 항목 선택·버튼 재클릭·바깥 클릭·Esc로 닫고, 목록이 많으면 팝업 안에서 스크롤합니다.
- 바로 옆 `Reset Session`은 확인 후 현재 선택한 세션의 대화·제목을 초기화합니다. UI의 `+ New Chat`으로 만든 세션은 번호와 목록 항목을 유지합니다. Locked 상태에서도 사용할 수 있으며, Reset 취소·진행 중 Chat 대기·늦은 History 무효화 정책을 유지합니다.
- Session List 각 항목의 `Delete`는 대상 번호·제목 확인 후 세션 자체와 History를 삭제합니다. Delete 클릭은 세션 선택으로 전달되지 않습니다. 현재 세션 삭제 시 다음 항목을 우선 선택하고 없으면 이전 항목을 선택합니다. 마지막 항목 삭제 후에는 빈 상태이며 `+ New Chat`으로만 새로 시작합니다. D의 작성 중인 문장은 유지합니다.
- 각 AI 답변의 `Play`를 누르면 `/voice/synthesize`로 로컬 음성을 합성해 재생합니다. 자동 재생은 하지 않습니다.
- `AI Language`는 다음 Chat 답변과 해당 답변의 TTS 언어를 선택합니다. 한국어가 기본이며 English를 선택한 뒤 새 질문을 보내면 영어 답변을 요청합니다.
- 기존 답변은 생성 당시 언어로 재생됩니다. 설정을 바꾸는 것만으로 기존 답변이 번역되지는 않습니다.
- D의 Voice와 관계없이 AI Language가 새 답변의 텍스트와 TTS 언어를 결정합니다. 서버는 시스템 지시와 현재 질문에 출력 언어를 명시하며, 요청 전용 지시는 RAM History의 사용자 질문에 저장하지 않습니다.
- 합성 또는 재생 중에는 버튼이 `Stop`으로 바뀌며, 다른 답변 재생·세션 전환·화면 전환·Lock 시 기존 재생을 정리합니다.
- 한국어 Piper는 초기 권장값 `length_scale=1.15`를 사용하고, 재생 시작 손실을 줄이기 위해 WAV 앞에 약 250ms PCM 무음을 추가합니다.
- 서버 정책과 맞춘 Unicode code point 기준 500자 이하 답변만 재생하며 긴 답변을 임의로 자르지 않습니다.
- 사용자 메시지에는 음성 재생 버튼이 없습니다.

데스크톱의 C/D/E/F 행 높이는 `clamp(290px, 32vh, 380px)`이며 1200px 이하에서는 패널별 최소 높이 290px으로 줄바꿈 배치합니다. E의 대화 내용과 F의 기술 로그는 각 패널 안에서 독립 스크롤합니다. Session List 팝업은 `body` 최상위 고정 계층에 두고, 평소 버튼 위로 최대 320px까지 열리며 내부 목록만 스크롤합니다. 위쪽 공간이 거의 없는 작은 창에서는 화면 안의 8px 위치로 옮겨 잘림을 피합니다.

### F — Status / System Log

- 요청 시각, API, Running/Success/Error 상태 표시
- 전체 응답 시간, 입력·생성 토큰 수와 tokens/s 표시
- TTS 합성 언어·소요 시간·WAV 크기와 재생 상태 표시
- 브라우저 WAV 변환·요청 왕복·Audio 재생 준비 시간과 서버의 STT 디코딩·Whisper·Ollama·Piper 시간을 서로 다른 시계 기준으로 분리 표시
- Ollama 나노초 지표를 초 단위로 변환
- 로그 자동 스크롤
- `Clear Log`는 기술 로그만 삭제하며 E의 대화와 서버 대화 문맥은 유지

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

선택 필드 `response_language`에 `ko` 또는 `en`을 전달하면 해당 요청의 답변 언어를 지정합니다. 같은 세션의 기존 대화는 유지하며 시스템 언어 지시를 교체합니다. 생략 시 기존 기본값 또는 해당 세션의 이전 언어 지시를 유지합니다. 잘못된 값은 HTTP 400 `invalid_response_language`로 거부합니다. 정상 응답 JSON 구조는 동일합니다.

선택 헤더 `X-Local-AI-Response-Style: voice`는 해당 요청에만 짧은 음성 대화 스타일을 적용합니다. 이 지시는 저장된 시스템 프롬프트나 사용자 질문에 추가되지 않습니다. 헤더 생략/다른 값은 기존 일반 Chat 스타일을 유지합니다. 이 헤더는 인증이나 도구 실행 권한이 아닙니다.

AI Language 수정 검증 (2026-11-08): 현재 로컬 모델의 `/api/show` 결과가 `{{ .Prompt }}` 템플릿인 것을 확인했다. 이 구성에서는 사용자 입력만 전달되어 시스템 역할의 언어 지시가 빠질 수 있다. 수정 전 한국어→English 및 영어→한국어의 실제 요청에서 입력 언어로 답하는 오류를 재현했다. `response_language` 지정 시 현재 질문에도 요청 전용 언어 지시를 붙여 보강했고, 질문 원문·제목·History에는 이 지시를 저장하지 않는다. 모델 템플릿이나 Ollama 설정은 변경하지 않았다. 이 템플릿의 문맥/시스템 역할 지원 문제는 ISSUE-002의 후속 조사 대상이다.

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

### `POST /voice/transcribe`

로컬 음성 파일을 텍스트로 변환합니다. `multipart/form-data`로 `audio` 파일과 선택적인 `language` 값(`auto`, `ko`, `en`)을 전송합니다. 기본 언어는 `auto`입니다.

```powershell
curl.exe -X POST "http://127.0.0.1:5000/voice/transcribe" `
  -F "audio=@tests/audio_samples/ko.m4a" `
  -F "language=auto"
```

정상 응답, HTTP 200:

```json
{
  "text": "안녕하세요. 로컬 AI 음성 인식 테스트입니다.",
  "language": "ko",
  "elapsed_seconds": 4.8
}
```

음성 파일은 최대 10MiB, 디코딩 후 길이는 최대 30초입니다. WAV, M4A 및 WebM 컨테이너를 지원하며 원본 음성은 영구 저장하지 않습니다. STT는 CPU INT8로 한 번에 하나만 실행하고, 처리 중인 경우 HTTP 429를 반환합니다. `elapsed_seconds`는 Flask가 요청 처리를 시작한 뒤 업로드 읽기, 검증, 디코딩, 필요한 경우 최초 모델 로딩, 추론까지 포함합니다.

### `POST /voice/synthesize`

한국어 또는 영어 텍스트를 로컬 Piper 모델로 합성합니다. 요청은 `application/json`이며 `text`와 `language`(`ko` 또는 `en`)가 필요합니다.

```powershell
Invoke-WebRequest -Method Post `
  -Uri "http://127.0.0.1:5000/voice/synthesize" `
  -ContentType "application/json; charset=utf-8" `
  -Body '{"text":"안녕하세요. 로컬 AI가 음성으로 답변합니다.","language":"ko"}' `
  -OutFile "tts-response.wav"
```

성공하면 HTTP 200과 `Content-Type: audio/wav`로 22,050Hz·모노·16비트 PCM WAV를 반환합니다. 텍스트는 Python `len()` 기준 최대 500자이며 합성 WAV는 서버 메모리에서 생성됩니다. 언어별 모델은 최초 요청 때 CPU로 로딩하고 이후 재사용합니다. 한 프로세스에서 TTS 추론은 한 번에 하나만 실행하며 처리 중인 요청이 있으면 HTTP 429 `tts_busy`를 반환합니다.

한국어는 `length_scale=1.15`를 초기 권장 속도로 사용하며 영어는 모델 기본 속도 `1.0`을 유지합니다. Piper가 생성한 음성 프레임을 변경하지 않고 WAV 맨 앞에 약 250ms의 0값 PCM 프레임을 추가합니다. JSON 요청과 WAV 응답 계약은 이전 단계와 동일합니다.

성공한 Chat/STT/TTS 응답에는 본문 계약과 별개인 `Server-Timing` 헤더가 추가됩니다. STT는 multipart 읽기·디코딩·모델 로딩·Whisper 추론, Chat은 Ollama 전체 요청, TTS는 Piper 모델 로딩·합성 시간을 제공합니다. `/chat`은 `stream: false`이므로 첫 토큰 시간은 측정할 수 없고 전체 응답 완료 시점만 측정합니다.

오류 응답 코드는 `text_missing`, `text_empty`, `invalid_tts_text`, `text_too_long`, `invalid_tts_language`, `unsupported_media_type`, `tts_busy`, `tts_model_missing`, `tts_initialization_failed`, `tts_inference_failed`입니다.

### RAM Session API

`GET /sessions`는 현재 프로세스의 세션 목록을 `{"sessions":[{"session_id":"...","number":1,"title":"새 대화"}]}` 형식으로 반환하며 조회만으로 세션을 만들지 않습니다. 번호는 프로세스 안에서 증가하며 중복되지 않습니다.

`POST /sessions`는 본문 없이 새 RAM 세션을 만들고 HTTP 201과 `{"session_id":"s-...","number":2,"title":"새 대화"}`를 반환합니다. 내부 ID는 UUID 기반이며 화면에는 표시 번호와 제목을 사용합니다. 60개 한도에서는 HTTP 429 `session_limit_reached`를 반환합니다.

`GET /sessions/<session_id>/history`는 `session_id`, `number`, `title`, `messages`를 반환합니다. `messages`에는 `role`(user/assistant), `content`, AI 답변의 `language`(ko/en 또는 null)가 포함됩니다. 없는 세션은 HTTP 404 `session_not_found`입니다. 첫 번째 **성공한** 질문의 앞 40자를 제목으로 사용하고 길면 `...`을 붙입니다. Reset 후에는 제목이 `새 대화`로 돌아가며 다음 성공한 질문으로 다시 정해집니다. 세션 목록과 기록은 Flask 재시작 시 사라집니다.

기존 외부 클라이언트의 `/chat`은 전달된 임의의 유효한 `session_id`로 계속 동작합니다. 이 방식으로 생성된 기존 호환 세션도 목록에 번호가 붙으며, Reset 후 빈 상태가 되면 기존처럼 제거됩니다. `+ New Chat`으로 만든 세션은 Reset해도 빈 항목으로 남아 번호를 유지합니다.

### `DELETE /sessions/<session_id>`

본문 없이 호출합니다. 성공 시 HTTP 200과 `{"session_id":"...","status":"deleted"}`를 반환합니다. 세션·History·제목·응답 언어 메타데이터를 제거하고 활성 세션 수를 줄입니다. 다음 세션 번호는 계속 증가하며 삭제한 번호를 재사용하지 않습니다.

- 없는 세션 또는 반복 DELETE: HTTP 404 `session_not_found`; 잘못된 ID: HTTP 400 `invalid_session_id`.
- Delete 접수 후 새 Chat/History/Reset 및 중복 Delete: HTTP 409 `session_deleting`.
- 이미 삭제된 ID의 Chat/History/Reset: HTTP 410 `session_deleted`. 알 수 없는 일반 ID의 기존 Chat 생성/Reset 정책은 유지합니다.
- 이미 실행 중인 Chat은 세션 Lock 안에서 완료한 뒤 Delete가 기록을 지웁니다. 대기 중이거나 새로 도착한 같은 세션 작업은 거부합니다. 다른 세션에는 영향을 주지 않습니다.

삭제된 ID만 RAM 표식으로 남겨 늦은 `/chat`이 같은 ID로 세션을 다시 만들지 못하게 합니다. History·제목·Lock 객체를 유지하는 것은 아닙니다. 표식은 활성 60개 한도에 포함하지 않으며 Flask 재시작 시 사라집니다. 표식 수는 프로세스 동안 삭제 횟수에 따라 증가하는 현재 제한이 있습니다.

브라우저는 삭제 성공 전 캐시를 지우지 않습니다. 현재 세션의 마이크/STT/TTS·자동 음성 턴을 취소하고 관찰 중 Chat이 끝난 뒤 DELETE를 전송합니다. 서버 완료 여부가 불확실한 Chat/Reset/Delete에는 DELETE도 자동 재시도하지 않습니다. HTTP 오류는 기록 후 최신 목록·허용되는 History로 재확인하고, 404/410은 이미 없는 항목으로 화면을 정리합니다. 통신 단절로 서버가 실제 삭제했는지 모르는 경우에는 불확실 상태를 안내하며 해당 ID를 차단합니다. 새 세션을 자동 생성하거나 보호 상태를 시간 경과로 해제하지 않습니다.

### `POST /sessions/<session_id>/reset`

지정 Session ID의 RAM 대화 기록을 초기화합니다. 요청 본문은 없습니다. 다른 세션의 기록은 변경하지 않습니다.

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
| TTS Text | Python `len()` 기준 최대 500자 |
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
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

### Python 문법 검사

```powershell
.\.venv\Scripts\python.exe -m py_compile app.py ai_controller.py ollama_runtime.py voice\stt_service.py voice\tts_service.py
```

### JavaScript 문법 검사

```powershell
node --check static/js/main.js
node --check static/js/voice-turn.js
node tests/test_voice_turn.js
node tests/test_voice_ui.js
```

`test_voice_turn.js`는 실제 단일 턴/세션 보호 관리자의 비동기 상태·경합을 검사합니다. `test_voice_ui.js`는 실제 main.js 이벤트 핸들러를 가짜 DOM/Fetch/MediaRecorder/Audio와 실행하는 테스트입니다. 설치 패키지를 추가하지 않으며 실제 Edge DOM, 마이크 권한, 디코딩과 스피커 재생을 검증한 것으로 간주하지 않습니다.

STEP 6-4-B 수동 Edge 검증 순서:

1. `start_app.bat` 또는 `.\.venv\Scripts\python.exe app.py`로 Flask를 재시작하고 Edge의 `http://127.0.0.1:5000`에서 Ctrl+F5를 누릅니다.
2. OFF에서 기존 Record→Stop→D Message→Enter→E Play를 확인합니다. 자동 Chat/TTS 요청이 없어야 합니다.
3. D 초안을 직접 비우고 ON으로 바꾼 뒤 Voice는 한국어, E의 AI Language는 한국어로 선택하여 Record→Stop & Send를 실행합니다. STT→Qwen→Piper→스피커 출력과 E의 질문/답변, F의 단계별 시간을 확인합니다.
4. 같은 방법으로 Voice와 AI Language를 English로 선택해 영어 답변과 영어 재생을 확인합니다. 두 언어 설정은 턴 시작 시 고정되어야 합니다.
5. Recording/Transcribing/Thinking/Synthesizing/Speaking에서 각각 Cancel·Lock·모드 OFF를 시험합니다. 이후 단계가 새로 호출되지 않고 마이크 표시/오디오가 정리되어야 합니다. Thinking 취소는 서버 기록 롤백이 아닙니다.
6. D의 `+ New Chat`으로 A와 B를 만들고 E의 `Session List`에서 A→B→A를 선택합니다. 화면 이동 뒤에도 늦은 응답이 다른 세션에 적용되지 않는지 확인합니다. Thinking 중 Reset 확인 취소는 작업을 유지하고, 승인은 Chat 완료 후 해당 세션 Reset을 실행해야 합니다.
7. D 초안 보호, 긴 답변의 전문 보존/음성 생략, TTS 오류 후 수동 Play, 재생 거부 안내, 작은 창에서 Cancel/Lock 접근성을 확인합니다.

실제 STEP 6-4-B 마이크/Edge/스피커 전체 흐름은 사용자 실기기 확인이 필요합니다. 자동 검증 결과와 구분해 기록합니다.

AI Language 수동 검증: 새 세션과 기존 세션 각각에서 한국어→English, 영어→한국어, 한국어→한국어, 영어→English 네 조합을 확인합니다. 각 조합을 텍스트 입력, OFF의 Record→Stop→Enter, ON의 Record→Stop & Send로 실행합니다. E의 새 답변 텍스트와 Play/자동 재생 언어가 E 설정을 따라야 하며, D Voice는 인식 언어만 정해야 합니다. ON은 턴 시작 시 선택값을 고정하며 처리 중 변경이 막혀야 합니다. Session List 왕복·새로고침 후 History에는 언어 지시가 붙지 않은 질문 원문이 남고, AI 답변은 생성 당시 언어로 재생되어야 합니다.

자동 검증은 Python 101개, 음성 작업 관리자 19개, 브라우저 API 모형 이벤트 28개로 총 148개가 통과했다. 실제 Qwen에서는 텍스트/voice 헤더 각각 네 언어 조합을 확인했다. 추가로 기존 ko.m4a/en.m4a를 STT→Qwen→Piper로 실행한 네 조합 모두 HTTP 200, 기대 언어의 문자 구성, 22,050Hz·mono·PCM16 메모리 WAV를 확인했다. 외부 소켓을 차단하고 localhost만 허용했다. 이는 실제 Edge 녹음/스피커 청감 테스트가 아니며, 문자 구성 검사는 답변 내용의 정확성을 보장하지 않는다.

### STT 정확도 조사 — 구현 변경 없음

현재 STT는 faster-whisper 1.2.1 / CTranslate2 4.8.5의 multilingual small을 CPU INT8로 실행한다. D Voice는 `auto→None`, `ko→ko`, `en→en`으로 실제 추론에 전달된다. `beam_size=1`, `vad_filter=False`, 기본 transcribe 작업이며 힌트나 수식 후처리는 없다. Auto는 전체 다국어 후보에서 고르므로 한국어·영어로만 제한되는 설정이 아니다. 현재 업로드의 디코딩은 16kHz 기준이다.

중국어/한자 및 “오덕” 오인식이 발생한 원본 녹음이 없어 정확한 원인은 미확정이다. 짧은 발화·작은 음량·잡음·앞 음절 손실과 Auto 언어 추정 실패, 한국어 숫자/연음의 모호성은 후보 원인이다. 중국 문자 출력만으로 감지 언어가 중국어였다고 단정할 수는 없다. 이번 기존 ko/en 파일은 Auto와 고정 언어에서 각각 ko/en으로 감지됐으며, 해당 사용자 오류는 재현되지 않았다.

우선 한국어 사용 시 Voice=한국어로 고정하고 같은 원본으로 Auto/ko를 비교하는 것을 권장한다. 다음 후보는 beam 1/3/5 비교와 제한된 수학 어휘 힌트다. 힌트·beam 확장은 정확도를 보장하지 않으며 편향과 지연을 함께 측정해야 한다. 자동 언어 제한, 수식 후처리, 모델 교체는 이번에 구현하지 않았다. 비교 설계와 자원 영향은 개발 계획서의 조사 기록에 정리했다.

한국어 Piper 속도 비교 샘플과 선행 무음 검증 파일은 다음 명령으로 다시 만들 수 있습니다. 출력 위치 `tests\tts_output`은 Git에서 제외됩니다.

```powershell
.\.venv\Scripts\python.exe scripts\tts_quality_probe.py
```

Python 단위 테스트는 Ollama 통신과 프로세스 제어를 Mock으로 대체합니다. 실제 모델, GPU, VRAM 또는 `ollama.exe`를 변경하지 않고 API 계약·오류 처리·세션 동시성·입력 경계를 검증합니다.

실제 Ollama 통합 테스트는 별도로 수행해야 합니다.

1. `ollama list`에서 `qwen3.5:9b`를 확인합니다.
2. `ollama serve`와 `.\.venv\Scripts\python.exe app.py`를 실행합니다.
3. 브라우저에서 Health Check를 실행합니다.
4. Chat을 Unlock하고 현재 Session에서 연속 질문하여 문맥을 확인합니다.
5. D의 `+ New Chat`으로 다른 세션을 만들고 E의 `Session List`로 왕복하여 문맥과 History가 분리되는지 확인합니다.
6. Reset 확인창에서 취소하면 요청이 발생하지 않는지 확인합니다.
7. Reset 승인 후 동일 세션의 이전 문맥이 제거되는지 확인합니다.
8. Clear가 출력 로그만 지우고 대화 문맥은 유지하는지 확인합니다.
9. 긴 답변의 E 패널 스크롤과 작은 창의 반응형 레이아웃을 확인합니다.
10. HTML·Script 문자열이 코드로 실행되지 않고 텍스트로 표시되는지 확인합니다.

### STEP 5-6-5 — Reset 독립성·비동기 검증 결과

기준선 148개를 재실행해 모두 통과했다. 추가 재현 테스트에서는 Reset 전 시작된 History 응답과 Reset 처리 중 A→B→A 전환에서 시작된 History 응답이 늦게 도착하면, 초기화한 E의 질문·답변 및 D 제목이 다시 표시되는 결함을 확인했다. 서버 전체 기록 삭제가 재현된 것은 아니며, 초기 사용자 보고와 동일 원인이라고 확정하지 않는다.

현재 `main.js`는 세션별 Reset 버전을 검사한다. Reset 승인 시 이전 조회를 무효화하고, 서버 성공 확인 시 처리 도중 시작된 조회도 무효화한다. 다른 세션의 History와 D 초안은 유지하며, 취소한 Reset은 버전을 바꾸지 않는다. Reset이 실패하면 캐시를 성공한 것처럼 지우지 않고, 완료 여부가 불확실한 Chat/Reset은 기존처럼 재시도·자동 해제하지 않는다. 서버 API와 음성 작업 관리자, UI 레이아웃은 수정하지 않았다.

추가 자동 검증: 서버 History·제목·번호·언어 메타데이터 독립성과 성공/실패 Chat→Reset 순서 3개, JS 오래된 History/다른 세션/확인 취소/실패/음성 단계 경합 7개를 추가했다. Python 104개 + 작업 관리자 19개 + 브라우저 API 모형 35개로 총 158개가 통과했다. 서버 검증은 실제 Flask RAM 저장소와 Lock을 실행하고 Qwen 호출만 Mock으로 대체한다. JS는 실제 main.js를 API/DOM/Audio 모형으로 실행한다. 실제 Edge·마이크·스피커, 실모델 통합 검증으로 간주하지 않는다.

Edge 확인은 새 검증용 세션만 사용한다. 서버 재시작 시 RAM 세션이 사라지는 기존 정책을 고려해 기존 기록이 필요하면 먼저 보존한다.

1. Flask 실행 후 Edge에서 Ctrl+F5를 누르고 D의 `+ New Chat`으로 A/B를 만든다. A에는 `A 표식: 사과`, B에는 `B 표식: 바나나`를 질문하고 번호와 제목을 기록한다.
2. A Reset 확인을 취소하면 대화와 제목이 그대로인지 확인한다. 다시 승인하면 A만 비워지고 번호 유지·제목 `새 대화`가 되는지 확인한다. B를 선택해 질문·답변·제목·번호·AI 답변 재생 언어가 그대로인지 확인한다.
3. Session List A→B→A와 브라우저 새로고침 후 결과를 다시 확인한다. Qwen이 무엇을 기억한다고 답하는지만으로 판정하지 않고, Edge 개발자 도구 Network의 각 `/sessions/{id}/history` 응답과 E 표시를 비교한다.
4. 오래된 응답 경합은 개발자 도구에서 느린 네트워크로 History를 요청한 직후 A Reset을 승인하는 순서로 시험한다. 응답이 Reset 뒤에 도착해도 예전 기록/제목이 돌아오면 실패다. 타이밍 재현이 안 되면 통과로 처리하지 않고 자동 모형 재현 결과와 구분한다. 확인 후 네트워크 설정을 원복한다.
5. Voice Conversation의 Recording/Transcribing/Thinking/Synthesizing/Speaking 중 Reset을 승인하고 A→B→A를 선택한다. 후속 자동 요청/늦은 재생이 중단되고 B 기록이 유지돼야 한다. Thinking은 이미 보낸 Chat 완료를 기다린 뒤 Reset해야 하며, 마이크 표시와 소리가 정리돼야 한다.

후속 사용자 확인으로 **STEP 5-6-5의 실제 Edge 검증이 완료됐으며 ISSUE-001을 종료했다.** 이는 사용자의 실기기 검증 보고에 따른 종료이며 에이전트가 직접 Edge를 검증했다는 뜻은 아니다. SQLite 도입은 별도 승인 후 진행한다.

Session UI 정리: C에는 Health Check를 유지하고 Chat/Reset 항목을 제거했다. D는 Unlock 후 직접 Enter로 Chat을 전송하며 E 하단에 Session List와 Reset Session을 함께 배치했다. 기존 158개 테스트를 유지하고 D 직접 전송/Health 이후 전송 및 E의 선택 세션 Reset 2개를 추가해 총 160개(Python 104, 관리자 19, UI 모형 37)가 통과했다. 이번 버튼 이동 후 실제 Edge 배치 확인은 별도 수동 항목이다.

### Delete Session 자동 검증 및 Edge 확인

기존 160개 테스트를 유지하고 서버 5개와 UI 이벤트 모형 9개를 추가해 총 174개(Python 109, 작업 관리자 19, UI 모형 46)가 통과했습니다. 서버 테스트는 독립 Flask 앱의 실제 RAM 저장소/Lock을 실행하며 Ollama는 Mock입니다. 진행 중 Chat 성공/실패와 Delete 순서, 큐의 Chat/History/Reset 거부, 타 세션 메타데이터 보존, 60개 한도 회복과 번호 증가를 검증했습니다. UI는 확인 취소·실패·상태 불확실 보호, Chat/Reset 완료 후 Delete 순서, 선택 순서·마지막 빈 상태·새로고침, A→B→A 및 늦은 History/STT/Chat/TTS 무효화와 다른 세션 재생 보존을 검사했습니다. 실제 사용 중 서버의 세션은 삭제하지 않았으며 Edge DOM·마이크·스피커는 이번 자동 검증에 포함하지 않았습니다.

1. Edge Ctrl+F5 후 `+ New Chat`으로 검증용 A/B/C를 만들고 서로 다른 질문과 AI Language를 사용합니다. B를 선택한 뒤 Session List의 B Delete를 취소하면 선택·기록이 유지돼야 합니다. 다른 행의 Delete를 눌러도 그 세션으로 선택이 바뀌면 안 됩니다.
2. B를 삭제하면 C가 선택되고 A의 질문·제목·번호·AI 재생 언어는 그대로여야 합니다. C를 삭제하면 A로 이동합니다. A를 마지막으로 삭제하면 빈 상태이며 새로고침해도 POST /sessions가 자동 호출되지 않아야 합니다. D 초안은 그대로여야 합니다.
3. Reset은 세션 항목/번호를 남기고, Delete는 항목을 제거하는 차이를 확인합니다. 세션이 없으면 Message·Record·Reset이 비활성이고 `+ New Chat`은 사용 가능해야 합니다.
4. Chat 또는 자동 음성 턴의 각 단계에서 현재 세션을 삭제합니다. Chat은 완료를 관찰한 뒤 DELETE가 전송되고, 나머지 단계는 취소되어 늦은 텍스트/음성이 복원되면 안 됩니다. 마이크 표시·소리·재생 버튼 상태가 정리돼야 합니다. 다른 세션 Delete는 현재 재생을 끊으면 안 됩니다.
5. 느린 네트워크에서 History/목록 요청과 삭제·A→B→A를 겹쳐 이전 항목/기록이 되살아나지 않는지 확인합니다. 팝업 위쪽 열림·내부 스크롤·Esc/바깥 클릭·작은 창 Delete 버튼 접근성도 확인합니다. 실제로 재현하지 못한 경합은 수동 통과로 기록하지 않습니다.

## Known Issues

### 실제 관찰된 이슈

- **ISSUE-001 — 종료:** 오래된 History가 Reset 뒤 화면과 제목을 되살리는 클라이언트 결함을 수정했고 서버 세션 독립성을 자동 검증했습니다. 사용자가 STEP 5-6-5의 실제 Edge 검증 완료와 종료를 확인했습니다. 서버가 다른 세션 기록을 삭제하는 결함은 재현되지 않았습니다.
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

- **STEP 5-6-2 — E/F 패널 분리:** 완료
- **STEP 5-6-3/4 — Session List / History / Create API 및 D/E 세션 전환:** 구현 완료, Edge 수동 확인 대기
- **STEP 5-6-5 — Reset 독립성·비동기 검증:** 완료, 사용자 Edge 검증 확인으로 ISSUE-001 종료
- **STEP 6-1 — 음성 AI 환경 분석:** 완료
- **STEP 6-2 — 로컬 STT:** faster-whisper CPU INT8 기반 Push-to-Talk 음성 인식
- **STEP 6-3-A~E — 로컬 TTS 환경·독립 합성·Flask API·수동 재생·음질/지연 계측:** 완료
- **STEP 6-4 — 음성 통합:** STT → 기존 `/chat` → Qwen3.5 9B → TTS
- **STEP 6-4-A/B — 음성 대화 설계·구현:** 완료 — 기본 OFF, 한 턴 순차 실행·취소·불확실 세션 차단. 실제 Edge 음성 턴 확인 대기
- **STEP 6-5 — 음성 품질 및 성능 검증:** 인식·합성 품질, 지연시간과 시스템 자원 측정
- **STEP 7 — SQLite 장기 기억:** 대화 영구 저장 및 세션 복원
- **STEP 8 — LLM Tool Calling:** allowlist와 입력 검증을 적용한 전문 도구 연동
- **이후:** 장기 기억 검색, 대화 요약, 페르소나, Bluetooth·게임패드·휴대전화 등 주변기기 연동

개발 순서를 변경한 이유는 SQLite 장기 기억보다 로컬 음성 대화의 기본 동작을 먼저 검증하기 위해서입니다. 기존 Flask 및 Qwen3.5 9B 구조를 재사용하고 STT/TTS는 독립 모듈로 구성합니다. 추가 비용 0원과 로컬 실행 원칙을 유지하며, 음성 기능이 실패해도 기존 텍스트 Chat은 정상 동작해야 합니다.

ISSUE-001의 SQLite 도입 전 검증 조건은 사용자 Edge 검증 완료로 충족했습니다. ISSUE-002의 Qwen 문맥 해석 부정확 문제는 별도 품질 개선 항목으로 유지합니다. 후속 기능 구현은 별도 승인을 받습니다.

## v0.1 상태

Web Control Console v0.1은 로컬 Ollama 상태 확인, Chat, Session Reset, 입력 잠금, 응답 성능 출력, 제한된 Ollama 프로세스 제어 및 기본 안정성 검증을 완료한 상태입니다. 장기 기억과 자동 Tool Calling은 포함되지 않습니다.
