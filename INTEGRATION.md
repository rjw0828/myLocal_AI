# Pydantic AI 통합과 실행 안내

Pydantic AI가 단일 Agent 엔진이다. 기존 Chat, Voice, Session/History/Reset, MCP Registry/Manager 및 수동 실행은 유지한다. 미해결 문제는 [Known Issues](KNOWN_ISSUES.md)에 기록한다. 현재 사용자 결정은 로컬 통합이며 GitHub Push는 포함하지 않는다.

## 기본 Console

프로젝트 루트에서 기존 실행 방식을 사용한다.

```powershell
.\.venv\Scripts\python.exe app.py
```

일반 Chat/Voice와 MCP Manager는 기존 방식으로 동작한다. Tool Chat은 기본 비활성이며 `/tool-chat` 실행 Route도 기본 등록하지 않는다. 일반 실행에 Pydantic AI를 설치하거나 모델을 자동 호출하지 않는다.

## 웹에서 일반 Chat과 Voice 모델 선택

기존 `start_app.bat`으로 실행한 뒤 AI Serve Locals의 Local Ollama 행에서 모델을 선택한다. 선택 즉시 저장되며 다음 일반 Chat/Voice 요청부터 적용된다. 기존 Session History는 지우지 않고, 진행 중인 Chat 요청의 모델은 바꾸지 않는다. 이 선택은 일반 Chat·Voice·Tool Chat에 공통 적용한다. Agent 활성화 여부는 별도다.

설치 목록은 로컬 Ollama의 `/api/tags` 메타데이터만 읽는다. 화면을 열어두면 30초마다 갱신하고 목록 새로고침 버튼으로 즉시 확인할 수 있다. 별도로 다운로드한 모델도 다음 목록 갱신에 표시된다. 모델을 자동 다운로드하거나 이 조회로 적재/추론하지 않는다. 삭제된 선택 모델을 다른 모델로 자동 대체하지 않는다.

선택은 Session DB와 같은 디렉터리의 `model_settings.json`에 저장하며 기본 실행에서는 `data/model_settings.json`이다. 이 파일은 Git 대상이 아니다. 서버/브라우저를 다시 열면 저장된 선택과 현재 설치 목록을 확인한다. 이 UI를 처음 적용할 때 Flask 서버를 재시작하고 브라우저를 새로고침한다. 이후 모델 선택 변경에는 서버 재시작이 필요 없다. 설치 모델 각각의 Chat 적합성이나 응답 품질을 보장하는 기능은 아니다.

## Agent 선택 활성화

기존 Console에서 선택 활성화하려면 프로젝트 루트에서 실행한다. 일반 Chat은 계속 기본 경로이며 Tool Chat ON은 별도로 선택한다.

```powershell
.\start_app.bat --agent
```

현재 운영 `.venv`에는 Pydantic AI가 없으므로, 보존된 Agent 환경을 사용하려면 아래 명령을 **한 줄씩** 실행한다. 새 환경이나 패키지를 자동 설치하지 않는다.

```powershell
$env:LOCAL_AI_AGENT_PYTHON = (Resolve-Path '..\myLocalAI-agent-exp\.venv\Scripts\python.exe').Path
```

```powershell
.\start_app.bat --agent
```

주소는 `http://127.0.0.1:5000/`이다. `start_app.bat --no-agent`는 상속된 활성화 설정도 명시적으로 끈다. 인수 없는 실행은 기본 비활성이며, 소유자가 환경변수 `LOCAL_AI_AGENT_ENABLED=1`을 지정한 경우에만 활성화를 요청한다. 의존성이 없거나 Agent 시작이 실패하면 화면에 이유를 표시하고 일반 Console을 유지한다. 활성화만으로 MCP 서버를 시작하거나 Tool을 호출하지 않는다.

AI Serve Locals의 Local Ollama 행에 있는 모델 선택란 하나를 사용한다. 일반 Chat·Voice·Tool Chat 모두 같은 저장 설정(model_settings.json)을 사용한다. 모델 변경은 다음 요청부터 적용하며 진행 중인 Agent Job은 최초 모델을 유지한다. 설치 목록 새로고침과 30초 갱신을 재사용한다. 이전 agent_model_settings.json은 읽거나 삭제하지 않는다. 선택 모델이 삭제되거나 목록을 확인할 수 없으면 새 Agent Job을 차단하며 자동 대체하지 않는다. 설치 여부는 Tool Calling 품질 보장이 아니다.

기존 MCP Manager의 등록·Start/Discovery·서버 Enabled·승인 정책을 사용한다. Console과 Agent는 동일한 MCPService/Registry를 공유한다. 기존 DB와 승인 프로필을 복사하지 않는다. Agent 승인 정책은 기존 경로 보호 규칙을 유지하면서 `%LOCALAPPDATA%/LocalAILab/agent-console/<Session 경로 해시>/approval_policy.sqlite3`에 별도로 보관한다. 과거 서버·Tool 신뢰 기록은 보존하지만 현재 실행 승인에는 사용하지 않는다.

상태 표시는 기존 Job/Operation 기록으로 모델 준비·응답 생성, 승인 대기, Tool 실행, 최종 답변 생성을 구분한다. 짧은 단계는 Poll 사이에 지나갈 수 있다. 서버 터미널의 `local_ai.agent` 로그에는 Job ID, Run ID, 고정 모델, 단계, 상태 전이, 예외 유형만 남긴다. 오류 원문·Tool 결과·토큰·Workspace 경로를 진단 로그에 추가하지 않는다. `agent_run_failed`는 자동 재시도하지 않으며 기존 미확정 결함의 해결을 의미하지 않는다.

## 기존 격리 Agent 검증 호스트

검증된 Agent 의존성 환경과 설치된 로컬 Ollama 모델이 필요하다. `requirements-agent-mcp.txt`와 `requirements-agent-mcp.lock`은 Agent용 별도 환경의 의존성 자료다. 통합 작업에서는 운영 가상환경에 패키지를 설치하지 않는다. 기존 실험 Worktree의 검증된 가상환경을 계속 사용할 수 있다.

Agent 의존성이 갖춰진 Python으로, 프로젝트 루트에서 아래 두 명령을 각각 실행한다.

현재 운영 `.venv`에는 Pydantic AI가 없을 수 있다. 이 경우 아래 명령의 Python 실행 파일을 보존한 실험 Worktree의 `..\myLocalAI-agent-exp\.venv\Scripts\python.exe`로 바꿔 사용한다. 의존성을 자동 설치하거나 운영 가상환경을 변경하지 않는다.

```powershell
$agentBase = Join-Path $env:TEMP ("local-ai-agent-" + [guid]::NewGuid().ToString("N"))
```

```powershell
.\.venv\Scripts\python.exe -m agent_poc.web_console --registered-console --base $agentBase --model local-ai-c4b-gemma4-8k --port 5002
```

이 명령은 빈 Registry와 격리 Session/승인 DB의 Console을 연다. 기존 운영 DB나 승인 파일을 자동으로 사용하지 않는다. 접속 주소는 `http://127.0.0.1:5002/`이며 MCP 프로세스는 자동 시작하지 않는다. 서버 실행 설정은 기존 Add Server의 로컬 승인 절차를 거치고 Start/Discovery, 서버 Enabled, 승인 모드와 신뢰 범위를 명시적으로 설정한다. 등록/Discovery 성공 자체는 Tool 자동 신뢰가 아니다. 실제 서버 시작과 호출은 사용자가 검토한 범위에서만 진행한다.

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

## Architecture Re-Alignment STEP A

MCP Manager의 서버 Enabled ON/OFF를 사용한다. 신규 및 v1 Registry의 기존 서버는 기본 Disabled다. v1→v2 Migration은 기존 등록·설정·과거 Tool 권한 행을 보존하며 반복 실행해도 활성화 설정을 초기화하지 않는다. 기존 Tool 권한 행은 실행 판단에 사용하지 않고 이전 Tool 권한 변경 API는 409로 종료한다. 내부 allowed 필드는 ApprovalPolicy 호환을 위해 서버 Enabled를 나타낸다.

Enabled는 연결 상태와 독립적이다. 이번 단계에서는 Start/Discovery가 여전히 필요하며 자동 연결·Voice 연결은 후속 단계다. 연결 및 Configuration이 유효한 Enabled 서버의 발견된 Tool을 다음 Run에 노출하고 실행 직전에 Enabled와 기존 계약을 재검증한다. 기존 Filesystem 읽기 전용 Root 계약은 유지하며 Configuration 확장은 STEP D 범위다. 운영 DB에 Migration을 실행하거나 Console을 재시작하지 않았다.

## Architecture Re-Alignment STEP B

Input 패널의 Session Permission에서 Manual / Allow All을 선택하고 Save한다. Manual이 기본이며 Session 전환 시 저장된 값을 다시 읽는다. Session SQLite v1→v2 Migration은 History를 보존하고 permission 및 Manual 축소 이력만 추가한다. Permission 변경은 History revision을 변경하지 않는다. 실행 중 Job은 시작 시 Permission을 유지하고, 이후 Manual로 축소한 경우에는 해당 Job의 미전송 자동 승인 호출을 재검증한다. Manual→Allow All은 현재 Manual Job을 자동 승인하지 않으며, Allow All→Manual→Allow All로 되돌려도 이전 Job의 자동 승인이 복원되지 않는다.

기존 ApprovalPolicy와 Single Dispatch를 사용한다. 서버·Tool별 신뢰 등록, AUTO_ALLOW_SAFE, 별도 자동 승인 체크는 실행 경로에서 제거했다. 과거 기록은 보존하며 이전 서버 정책 API는 410을 반환한다. 연결 시작 버튼의 별도 확인창은 제거했지만 자동 연결은 STEP D에서 다룬다. Manual 축소가 모델 응답 생성 중이면 Allow/Deny 대기로 전환한다. 이미 자동 승인된 호출의 전송 직전 경합에서는 실행 0회로 차단하고 session_permission_requires_manual로 실패 종료한다. 자동 재실행하지 않으며 새 요청에서 승인한다.

Enabled OFF·Root 외부·Schema/연결 세대 불일치는 Permission과 관계없이 차단한다. 일반 대화의 Tool 근거 강제 및 응답당 1회 제한은 STEP C에서 해제했다. 이번 변경은 격리 Fake/Flask Quick에서만 검증했으며 운영 DB Migration 및 호스트 재시작은 실행하지 않았다.

## Architecture Re-Alignment STEP C

Tool Chat ON도 일반 인사·설명·코딩 답변을 처리한다. Catalog가 비어 있어도 MCP를 시작하지 않고 모델을 사용할 수 있다. Tool Chat OFF의 기존 /chat API와 공통 모델 선택은 유지한다. 공식 Pydantic AI Agent Loop, 기존 Deferred 승인·MCPService·Single Dispatch를 사용하며 별도 라우터를 추가하지 않았다.

한 요청의 여러 Tool 및 여러 모델 라운드를 지원한다. Manual은 호출마다 별도 Proposal로 승인하며 다음 호출은 Review approval에서 검토한다. 한 호출의 Deny는 다른 호출까지 거부하지 않는다. Allow All은 Session 설정과 Enabled·Root·Schema·연결 세대 검증을 유지하면서 자동 승인한다. 실행된 호출별 Operation ID와 거부된 Proposal을 구분하고 최종 답변만 History에 한 번 저장한다. request_limit=8, tool_calls_limit=16 등 기존 자원 제한과 별도 실험 호스트의 명시적 실행 예산은 유지한다.

정상 답변에는 기존의 제한된 조회 근거·시간 모순 검사를 적용한다. 새 질문 키워드 분류기나 모델별 Prompt를 추가하지 않았다. 명시적인 Tool 오류 응답과 Deny가 섞인 경우 모델의 최종 성공 주장을 그대로 저장하지 않고, 실제 호출별 성공·실패·거부 및 결과 데이터로 답변을 구성한다. coverage=partial 및 rendering=ledger_results_only는 전체 사실 검증 성공을 뜻하지 않는다. Timeout·전송 여부 불확실·안전 검증 실패는 계속 중단하며, 거부·오류에 따른 자동 Job 재실행은 하지 않는다.

검증은 FunctionModel·메모리 SDK·격리 Flask/SQLite와 DOM doubles만 사용했다. 실제 MCP·모델·브라우저 및 Full 회귀는 실행하지 않았다. 자동 연결/Configuration은 STEP D, Voice Agent 연결은 후속 단계다. 운영 DB·승인 파일은 변경하지 않았고 실행 중 Console도 재시작하지 않았다.
