# Pydantic AI 통합 Known Issues

2026-11-24 기준. Pydantic AI를 최신 Agent 엔진으로 채택한다. 아래 제한은 사용자가 수용한 개인 개발 환경의 Known Issues이며, 모든 사전 실측 완료를 로컬 Merge 조건으로 요구하지 않는다. 일반 Console의 Agent 기능은 기본 비활성이고 별도 호스트에서 선택 활성화한다.

## 최근 확인된 문제

- C-5G Filesystem Allow 이후 `agent_run_failed`: 승인 POST는 HTTP 202였지만 실제 Operation 생성 전 실패했다. 실제 SDK Tool Call 0회, History 저장 없음, Session 예약 해제. 원인은 미확정이다. 동일 Schema와 revision의 FunctionModel/Fake 및 Mock HTTP Ollama Provider 재생은 통과했으나 실제 호스트 실패를 재현하지 못했다. 해결된 것으로 취급하지 않는다.
- Qwen의 짧은 자연어 질문 Tool 선택은 불안정하다. Gemma와 모델별 Prompt 분기를 추가하지 않았다.
- 정상 복합 Tool 실행은 기존 응답당 1회 제한으로 미지원이다. 일부 근거만으로 전체 성공을 저장하는 것은 차단한다. 근거 검사 Fixture의 정상 복합 근거 일치는 실제 복합 실행 지원을 의미하지 않는다.
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
