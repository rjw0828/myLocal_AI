# Pydantic AI 통합 Known Issues

## STEP D/E 현재 상태 — 2026-11-27

- 실사용 환경의 분리 문제를 정리했다. 프로젝트 `.venv`에는 Voice가, 실험 `.venv`에는 Agent만 있어 정상 WAV가 `numpy` 누락 때문에 `invalid_audio`로 처리됐다. 같은 정상 WAV의 디코딩으로 원인을 재현했다. 기존 Voice 패키지가 있는 프로젝트 `.venv`에 검증된 Agent 패키지를 추가하고 배치를 해당 환경 하나로 고정했다. `pip check`, 관련 Quick 34개 및 실제 배치 격리 시작 검사는 통과했다. 아래의 `piper` 누락 실패는 당시 실험 환경의 과거 결과이며 PASS로 소급 변경하지 않는다. 실제 마이크·스피커·Agent Tool 통합 실측은 여전히 별도다.

- 00:30의 Gemma `stage=CATALOG / ValueError`는 기존 정책 DB의 `AUTO_ALLOW_SAFE` 값을 새 Session Permission enum으로 읽지 못한 호환성 결함으로 재현했다. 구형 `ASK_EVERY_TIME` / `AUTO_ALLOW_SAFE`는 첫 정책 트랜잭션에서 Manual 메타데이터로 원자적으로 전환하고, 기존 신뢰·서버 override 기록은 보존하되 실행 승인에는 사용하지 않는다. 반복 조회는 재이관하지 않으며 Session Allow All은 해당 Session 설정만 따른다. 운영 DB 직접 수정 없이 복사본에서 Catalog 통과·Gemma Run 생성까지 검증했고, 실제 추론 직전에 중단했다. 시작 배치 격리 실행과 관련 Fake/Flask 4개도 통과했다. 이 수정의 실제 시간 조회·최종 답변은 아직 미검증이다.

- MCP 자동 연결·Configuration 및 Voice → Agent → 최종 응답 → TTS 연결은 Fake/Flask/JavaScript 모형으로 확인했다. 이번 변경의 실제 Voice 장치·모델·MCP 통합 실측은 미검증이며, 과거 단계의 실서버 성공 기록으로 대체하지 않는다.
- 기존 Quick 결과: STEP D Python 84 PASS / JavaScript 62 PASS, STEP E Python 37 PASS / 1 환경 실패 및 JavaScript 130 PASS, 시작 스크립트 5 PASS. 단계별 테스트가 겹치므로 누적 총계로 합산하지 않는다. Commit/Push 작업에서는 테스트를 재실행하지 않았다.
- STEP E의 `test_concurrent_api_request_returns_busy` 실패는 테스트용 Agent 가상환경의 `piper` 누락으로 Fake 합성에 진입하지 못한 환경 제한이다. 같은 환경의 `faster_whisper`도 없다. 실패를 PASS나 Skip으로 바꾸지 않았으며 의존성을 설치하지 않았다.
- 현재 `start_app.bat`은 프로젝트 `.venv` 하나로 Agent 활성화를 기본 시도한다. 형제 실험 환경 자동 탐색과 `LOCAL_AI_AGENT_PYTHON` 우회는 제거했다. 기본 실행은 과거 Agent 환경변수에도 영향을 받지 않으며, `--no-agent`는 일반 Chat 모드를 유지한다. Agent·Voice 패키지가 없는 환경의 기능 제한은 시작 경고로 표시된다.
- 앞서 보고된 MCP 서버 목록 미표시 문제는 원인 미확정으로 남아 있다. 이번 Commit에서 DB 복구·서버 재등록·운영 승인 파일 변경은 수행하지 않았다.
- 기존 500자 자동 TTS 제한과 Qwen의 짧은 질문 Tool 선택 불안정 등 아래 미해결 항목은 유지한다. 아래의 기본 비활성화 설명 등은 이전 단계 기록이며, 현재 시작 방식은 위 항목을 따른다.

2026-11-24 기준. Pydantic AI를 최신 Agent 엔진으로 채택한다. 아래 제한은 사용자가 수용한 개인 개발 환경의 Known Issues이며, 모든 사전 실측 완료를 로컬 Merge 조건으로 요구하지 않는다. 일반 Console의 Agent 기능은 기본 비활성이고 `start_app.bat --agent`로 선택 활성화한다. 기존 격리 검증 호스트도 유지한다.

## Agent Console UX-1 검증 범위

공통 모델 선택·저장/복원·Job 모델 고정·선택 모델 삭제·선택 활성화·시작 실패의 일반 Chat 유지·민감 정보 없는 오류 진단은 Fake/Flask 및 JavaScript doubles로 검증한다. 이번 작업에서는 실제 Ollama 추론, MCP 프로세스, 브라우저 실측을 수행하지 않는다. 설치된 모든 모델의 Tool Calling 지원/품질을 보장하지 않으며 기존 `agent_run_failed`와 Qwen 선택 불안정은 미해결이다. 실제 사용 시 Agent 의존성을 갖춘 Python 지정, 화면 활성 상태, 모델 재선택/재시작 복원 및 실제 승인 이후 결과를 확인해야 한다.

## 최근 확인된 문제

- C-5G Filesystem Allow 이후 `agent_run_failed`: 승인 POST는 HTTP 202였지만 실제 Operation 생성 전 실패했다. 실제 SDK Tool Call 0회, History 저장 없음, Session 예약 해제. 원인은 미확정이다. 동일 Schema와 revision의 FunctionModel/Fake 및 Mock HTTP Ollama Provider 재생은 통과했으나 실제 호스트 실패를 재현하지 못했다. 해결된 것으로 취급하지 않는다.
- Qwen의 짧은 자연어 질문 Tool 선택은 불안정하다. Gemma와 모델별 Prompt 분기를 추가하지 않았다.
- STEP C에서 응답당 1회 제한을 제거하고 복수 Tool·여러 라운드 및 부분 실패를 Fake/Flask로 검증했다. 실제 모델·MCP의 복합 요청은 미실측이며 성공으로 표시하지 않는다. 일부 Tool만 호출하고 전체 조회에 성공했다고 답하는 경우의 기존 근거 검사는 유지한다.
- 일부 브라우저 후속 Run, Stop, 승인 모드 및 신뢰 설정의 전체 UI 경로는 미검증이다. C-5G의 추가 실측은 사용자 결정으로 종료했다.
- 요일, 파일 내용의 일반 의미 일치, 해석 불가능한 시간 표현, 복수 시간대 및 혼합 답변의 개별 주장 정렬은 검증 범위 밖이다. `not_checked` 또는 `not_verified`는 진위 검증 성공이 아니다. 조회 요구 판별도 기존의 제한된 규칙이며 자연어 전체를 보장하지 않는다.
- Voice Python 의존성 누락은 별도 환경 문제다. 안전 회귀에서 제외한 STT/TTS 모듈을 PASS나 Skip 처리한 것으로 간주하지 않는다. 실제 장치 검증은 이 통합 범위 밖이다.

## 실측 기록의 구분

| 기록 | 확인된 범위 |
| --- | --- |
| C-4B 및 C-5A/C-5C | 실제 Time 호출 성공 사례가 있다. ASK 및 AUTO 경로와 History 저장을 확인한 과거 기록이며 현재 모든 조건의 성공을 보장하지 않는다. |
| C-5E Filesystem | 실제 `list_directory`와 `read_file` 각각 1회 성공, 중복 0건, 결과/Fixture/최종 답변/History 대조 성공. |
| C-5G 승인 수명주기 | 브라우저 Deny, 120초 만료, 승인 대기 Reset 확인. 각 실제 호출 0회, 허위 성공 History 없음. 중복/늦은 승인은 Backend HTTP 409로 차단. |
| C-5G 동적 등록 | 브라우저 등록, Start/Discovery, 실행 계약/Root 확인, `read_file` Allow/Block 및 Backend Catalog 반영 확인. Block 이후 새 Run 및 Stop 실측은 종료 결정으로 미검증. |
| C-5G Allow | HTTP 202의 RUNNING 응답에서 `invalid_job_response`는 발생하지 않았으나 이후 `agent_run_failed`로 끝났다. 정상 읽기 성공이나 Reset 후 전체 정상 복귀 PASS가 아니다. |

## 유지되는 안전 계약

공식 Pydantic Agent Loop, MCPService/Single Dispatch, 실행 설정/Schema/연결/권한 세대 검증, 검증된 Filesystem Root와 임시 모델 문맥, 명시적 승인 및 일회 실행, Session revision과 원자적 History 저장을 유지한다. Deny에 Root 실행 인수를 요구하지 않으며 Allow의 Root 일치 검사는 유지한다.

기본 실행에서 Agent를 자동 활성화하거나 새 MCP/Tool을 자동 신뢰하지 않는다. 기존 자체 `tool_calling` 엔진은 제거하고 기존 UI 및 수동 MCP API는 유지한다. 구형 엔진에 종속된 테스트는 해당 엔진과 함께 퇴역하며 새 엔진의 안전 회귀가 대체한다. 이전 코드와 실험 기록은 Git 부모 이력에 남는다.

## 이후 수정 범위

실사용 중 재현되는 문제는 별도 `fix/*` 브랜치에서 수정한다. 최근 Allow 실패를 진단할 때는 비밀값을 남기지 않는 예외 관측부터 확보하고, 실제 MCP 재실행은 별도 승인 범위에서만 수행한다. 원인 미확정 상태에서 승인 정책이나 실행 경계를 완화하지 않는다.
