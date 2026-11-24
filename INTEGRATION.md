# Pydantic AI 통합과 실행 안내

Pydantic AI가 단일 Agent 엔진이다. 기존 Chat, Voice, Session/History/Reset, MCP Registry/Manager 및 수동 실행은 유지한다. 미해결 문제는 [Known Issues](KNOWN_ISSUES.md)에 기록한다. 현재 사용자 결정은 로컬 통합이며 GitHub Push는 포함하지 않는다.

## 기본 Console

프로젝트 루트에서 기존 실행 방식을 사용한다.

```powershell
.\.venv\Scripts\python.exe app.py
```

일반 Chat/Voice와 MCP Manager는 기존 방식으로 동작한다. Tool Chat은 기본 비활성이며 `/tool-chat` 실행 Route도 기본 등록하지 않는다. 일반 실행에 Pydantic AI를 설치하거나 모델을 자동 호출하지 않는다.

## 웹에서 일반 Chat과 Voice 모델 선택

기존 `start_app.bat`으로 실행한 뒤 AI Serve Locals의 Local Ollama 행에서 모델을 선택한다. 선택 즉시 저장되며 다음 일반 Chat/Voice 요청부터 적용된다. 기존 Session History는 지우지 않고, 진행 중인 Chat 요청의 모델은 바꾸지 않는다. 이 설정은 Controller 전체의 일반 Chat/Voice 설정이며 Agent 모델 선택이나 Agent 활성화와는 별개다.

설치 목록은 로컬 Ollama의 `/api/tags` 메타데이터만 읽는다. 화면을 열어두면 30초마다 갱신하고 목록 새로고침 버튼으로 즉시 확인할 수 있다. 별도로 다운로드한 모델도 다음 목록 갱신에 표시된다. 모델을 자동 다운로드하거나 이 조회로 적재/추론하지 않는다. 삭제된 선택 모델을 다른 모델로 자동 대체하지 않는다.

선택은 Session DB와 같은 디렉터리의 `model_settings.json`에 저장하며 기본 실행에서는 `data/model_settings.json`이다. 이 파일은 Git 대상이 아니다. 서버/브라우저를 다시 열면 저장된 선택과 현재 설치 목록을 확인한다. 이 UI를 처음 적용할 때 Flask 서버를 재시작하고 브라우저를 새로고침한다. 이후 모델 선택 변경에는 서버 재시작이 필요 없다. 설치 모델 각각의 Chat 적합성이나 응답 품질을 보장하는 기능은 아니다.

## Agent 선택 활성화

검증된 Agent 의존성 환경과 설치된 로컬 Ollama 모델이 필요하다. `requirements-agent-mcp.txt`와 `requirements-agent-mcp.lock`은 Agent용 별도 환경의 의존성 자료다. 통합 작업에서는 운영 가상환경에 패키지를 설치하지 않는다. 기존 실험 Worktree의 검증된 가상환경을 계속 사용할 수 있다.

Agent 의존성이 갖춰진 Python으로, 프로젝트 루트에서 아래 두 명령을 각각 실행한다.

현재 운영 `.venv`에는 Pydantic AI가 없을 수 있다. 이 경우 아래 명령의 Python 실행 파일을 보존한 실험 Worktree의 `..\myLocalAI-agent-exp\.venv\Scripts\python.exe`로 바꿔 사용한다. 의존성을 자동 설치하거나 운영 가상환경을 변경하지 않는다.

```powershell
$agentBase = Join-Path $env:TEMP ("local-ai-agent-" + [guid]::NewGuid().ToString("N"))
```

```powershell
.\.venv\Scripts\python.exe -m agent_poc.web_console --registered-console --base $agentBase --model local-ai-c4b-gemma4-8k --port 5002
```

이 명령은 빈 Registry와 격리 Session/승인 DB의 Console을 연다. 기존 운영 DB나 승인 파일을 자동으로 사용하지 않는다. 접속 주소는 `http://127.0.0.1:5002/`이며 MCP 프로세스는 자동 시작하지 않는다. 서버 실행 설정은 기존 Add Server의 로컬 승인 절차를 거치고 Start/Discovery, Tool Allow, 승인 모드와 신뢰 범위를 명시적으로 설정한다. 등록/Discovery 성공 자체는 Tool 자동 신뢰가 아니다. 실제 서버 시작과 호출은 사용자가 검토한 범위에서만 진행한다.

`--base`는 새 디렉터리여야 한다. 현재 호스트는 실험 격리를 위해 기존 운영 Worktree 내부 경로를 거부한다. 설치되지 않은 모델을 다운로드하거나 모델을 자동 교체하지 않는다. 실행 종료는 서버 창에서 Ctrl+C로 수행한다. 실제 전송된 작업의 취소나 롤백을 가정하지 않는다.

## 선별 통합 기준

| 기존 변경 | 채택한 계약과 대체 방식 |
| --- | --- |
| MCP-5C Backend | Session 예약/revision, 원자적 History, 승인/세대/Root 경계를 유지한다. 자체 Backend/상태 루프는 공식 Agent와 `AgentWebBackend`로 대체한다. |
| MCP-5D Console | A~F 화면, Tool Chat UI, 오류 처리와 Reset/Delete 조정을 재사용한다. 기본 Agent 비활성 및 선택 활성화 검사를 유지한다. |
| MCP-5E Workspace | 검증된 Root를 매 모델 요청 직전 전달하고 History에는 저장하지 않는다. 새 Catalog/실행 attestation이 구형 Root adapter를 대체한다. |
| 보존 Commit의 selection policy | 제한된 조회 근거/시간 모순 검사를 새 Operation/Proposal 기록에 연결한다. 구형 선택 실행 루프나 별도 Registry는 이식하지 않는다. |

구형 `tool_calling` 패키지와 그 내부 엔진 전용 Python 테스트는 최종 코드에서 제거한다. 공유 Console JavaScript 및 Session/MCP API 회귀는 유지한다. 구형 엔진용 `create_app` 주입 인수와 내부 Python adapter는 새 엔진의 주입 계약으로 대체되며, 외부에서 해당 내부 API를 직접 사용한 경우 이관이 필요하다.

기존 일반 Chat 호출의 선택 인수와 `SQLiteStore.revision` 호환 메서드는 유지한다. 통합 Worktree에서 확인된 격리 회귀는 현재 소스의 저장소 경로도 보호 대상에 포함해 수정했다. 기존 운영/실험 경로의 보호는 그대로다.

## 최소 검증 범위

안전 Python 회귀 명령은 `python -B scripts/test_safe.py`다. JavaScript는 `tests/test_*.js` 여섯 파일을 각각 Node로 실행한다. 기존 `scripts/test.ps1 -Mode full`은 실제 SDK Fixture까지 포함하므로 별도 실행 승인이 필요하다.

안전 Python 회귀, JavaScript 회귀, 임시 DB 기반 Flask Route 및 Session/History 기본 동작, 기본 Chat/Voice Import/API 호환성을 확인한다. 실제 SDK/stdio Fixture와 Voice 의존성 모듈은 안전 회귀에서 제외하며 통과로 계산하지 않는다. 추가 브라우저/모델/MCP 실측은 이 로컬 Git 통합에 포함하지 않는다.

## 브랜치 운영

통합된 `master`에서 후속 `fix/*` 또는 `feature/*` 브랜치를 시작한다. 기존 실험 Worktree는 사용자 미커밋 자료를 보존한 채 남긴다. 새 기능의 결과는 Mock/Fake, Backend, 사용자 브라우저 관측 및 실제 SDK 호출을 구분한다.
