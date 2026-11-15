# MCP-5E-3 — Model presentation comparison

2026-11-14, branch `feature/mcp-manager`, baseline HEAD
`574e3416fce2f0b1dd67fd56999b5ea43c7212fc`.

## 범위와 판정

실제 로컬 Qwen의 **첫 Tool 선택 및 인수 생성** 비교다. 실행 승인을 자동화하지
않았으므로 실제 Tool 실행 성공률, 실행 후 최종 답변 정확성은 N/A다.
Registry Allow는 명시적 Turn 승인과 다르다. 모든 승인 대기는 Cancel로 정리했다.

Alias + 검증된 Root 문맥을 Tool Chat Backend에 반영했다. Description과 추가
Prompt는 독립 비교에서 이득이 확인되지 않아 서비스 기본 경로에는 반영하지 않았다.
이들은 `model_context.py`의 명시적 실험용 함수/상수로만 남는다. 외부 설정이나 API로
자동 활성화하지 않는다. 기존 일반 Chat, Voice, UI 및 실행 권한은 변경하지 않았다.

**탐색적 결과이며 자연어 Tool 선택 문제의 해결 선언이 아니다.** 특히 T1 선택 실패,
잘못된 경로, 근거 없는 시각 답변과 응답 지연 증가가 남는다. Full/Edge는 MCP-5E-5 범위다.

## 환경과 재현 조건

- Python 3.11.9, Flask 3.1.2, MCP SDK 2.3.0, Ollama 0.40.14.
- `qwen3.5:9b`, digest `56671c2ab9385f9cfcb404638e32cd62d88e3501d44822208363c010179a3c90`.
- `stream:false`, `think:false`. 요청별 seed/temperature override 없음.
  모델 기본 temperature 1, top_k 20, top_p .95, presence_penalty 1.5 유지.
- 원본 템플릿 `{{ .Prompt }}` 유지. 별도 모델 생성/템플릿 변경 없음.
- 실제 Time/Filesystem stdio 서버, 독립 SQLite DB, 테스트 Root와 합성 fixture만 사용.
  Time `get_current_time`, Filesystem `read_file`/`list_directory`만 Allow.
  MCP Discovery/RootAdapter 검증은 실제 서버를 사용했다. 실행 dispatch 방어 장치의 호출은 0회.
- 각 질문은 새 세션으로 3회. History 비교 12회는 MCP-5E-2의 실제 Qwen 인사 응답 쌍을 그대로 재사용.
- MCP-5E-2 원본 기록을 보존했다. 기존 fixture가 정리되어 새로운 임시 Root를 사용했고
  절대경로 질문만 치환했다. 이번 실험의 모든 조건은 동일한 새 Root/서버 ID를 사용했다.
  과거 기준선과는 Root/서버 ID가 다르므로 해시 Alias 및 token 구성도 달라질 수 있다.
- 대표 질문 T1/T2/F1/F4/N1을 B0~B4에 교차 적용해 총 75회.
  B5는 기존 전체 세트 60회(기본 36 + 안전 12 + History 12).
  역할 대조 18회와 제외한 warm-up 3회를 합해 실제 모델 요청은 156회다.
- 1차 선별 실험은 임시 세션 정리를 빠뜨려 60회 후 세션 한도에서 중단됐다.
  이 인프라 실패를 모델 결과에 포함하지 않았고, 완료된 결과는 재실행하지 않았다.
  같은 Root/서버 ID와 조건을 복원해 남은 15회만 수행했다. 이후 측정 완료 세션은
  예약 해제 확인 후 삭제했다. 제한 완화나 운영 데이터 삭제는 하지 않았다.

## System 역할 대조

고정 문자열을 지시하는 무해한 대조에서 System-only 6/6, User-only 6/6 준수.
System/User 지시 충돌은 System 준수 3/6(새 History 없음 2/3, 중립 History 있음 1/3).
이는 System 내용이 출력에 영향을 줄 수 있다는 증거지만 템플릿의 내부 렌더링이나
Tool 지시의 안정적 우선순위를 입증하지는 않는다. 모델의 불준수와 템플릿 효과를
완전히 분리하지 못했다. 원본 템플릿을 수정하지 않았다.

## 독립 비교

각 행 15회, 이 중 Tool 필요 질문 12회, 인사 3회. 목표 Tool은 파일 읽기에는
`read_file`, 목록에는 `list_directory`다. 읽기 질문의 보조 목록 조회는 별도이며
파일 읽기 성공으로 계산하지 않는다. 경로 일치도 실행 성공을 뜻하지 않는다.

| 조건 | Time 목표 /6 | Filesystem 목표 /6 | 목표 Tool 전체 /12 | Filesystem 목표 + 올바른 경로 /6 | 불필요 호출 /3 | HTTP 평균 / 중앙값(초) | 입력 / 출력 token 평균 |
|---|---:|---:|---:|---:|---:|---:|---:|
| B0 기존 | 2 | 3 | 5 | 0 | 0 | 1.377 / .925 | 788.6 / 48.1 |
| B1 Description만 | 2 | 3 | 5 | 0 | 0 | 1.659 / 1.506 | 854.6 / 50.9 |
| B2 Alias만 | 2 | 4 | 6 | 0 | 0 | 1.885 / 1.951 | 792.6 / 61.1 |
| B3 Root만 | 4 | 4 | 8 | 0 | 0 | 2.159 / 1.958 | 1046.4 / 72.7 |
| B4 추가 Prompt만 | 1 | 1 | 2 | 0 | 0 | 1.243 / .850 | 941.6 / 40.0 |

B3의 Root 내부 경로 2건은 읽기 요청의 보조 디렉터리 조회였다. 목적 Tool과 경로를
동시에 만족한 결과는 0/6이다. Root 하나만으로 정확한 파일 읽기가 해결되지 않았다.
B1은 선택률 이득이 없고 지연이 증가했으며 B4는 선택률이 낮아 채택하지 않았다.
B2/B3의 표본상 가능성을 확인한 뒤 B5를 Alias+Root 조합으로 고정했다. Prompt/Description은 기존값이다.

## B5 전체 비교

| 항목 | MCP-5E-2 | B5 |
|---|---:|---:|
| Time 목적 Tool 선택 | 3/12 | 7/12 |
| Time 미선택 | 9/12 | 5/12 |
| Filesystem 목적 Tool 선택 | 5/15 | 10/15 |
| Filesystem 목적 Tool + 올바른 경로 | 5/15 | 7/15 |
| Filesystem 질문에 Time 오선택 | 4/15 | 0/15 |
| Filesystem 보조 목록 조회 | 2/15 | 2/15 |
| Filesystem 미선택 | 4/15 | 3/15 |
| 불필요한 호출 | 0/9 | 0/9 |
| 전체 생성 Tool 인수 Schema 적합 | 26/26 | 32/32 |
| History 조건 목적 Tool 선택 | 7/12 | 7/12 |
| History 조건 목적 Tool + 경로 적합 | 5/12 | 5/12 |
| HTTP 평균 / 중앙값 / 최대(초), 각 60회 | 2.261 / 1.778 / 10.314 | 2.621 / 2.016 / 10.720 |

### 질문별 목적 Tool 선택 (각 3회)

| 질문 | 기존 | B5 | B5 목적 Tool + 경로 |
|---|---:|---:|---:|
| T1 지금 몇 시야? | 1 | 0 | N/A |
| T2 현재 한국 시간 알려줘. | 0 | 2 | N/A |
| T3 뉴욕은 지금 몇 시야? | 1 | 2 | N/A |
| T4 Time MCP 도구를 사용해서 현재 서울 시간 알려줘. | 1 | 3 | N/A |
| F1 작업 폴더의 test.txt 읽어줘. | 0 | 1 | 1 |
| F2 test.txt 내용 보여줘. | 0 | 3 | 1 |
| F3 절대경로 파일 읽기 | 2 | 2 | 2 |
| F4 작업 폴더에 어떤 파일이 있어? | 0 | 2 | 1 |
| F5 Filesystem MCP + 절대경로 읽기 | 3 | 2 | 2 |

T1과 F5는 이전 표본보다 나빠졌다. 작은 표본과 temperature 1, 실험 시점 차이 때문에
특정 요소의 인과 효과 또는 통계적 유의성을 주장하지 않는다. 안정적 선택이 완료된 상태가 아니다.
동일 5개 대표 질문으로 B5를 제한하면 HTTP 평균 2.159초, 입력 1049.6/출력 77.3 token이다.
B0 대표 질문 대비 입력 문맥과 출력량이 증가했다. 기존 60회 기록에는 token 수가 보존되지
않아 그 기준선 대비 token 증감은 미측정이다. B5 전체 평균은 입력 1037.75/출력 97.65 token이다.

## 오류·안전·답변 검토

- B5 일반 Filesystem 15회: 생성 호출 12회 중 Root 내부 기존 경로 7, Root 밖 3,
  상대/부적합 경로 2. 잘못된 경로를 보정하거나 실행하지 않았다.
- 없는 파일 요청 3회: 파일 Tool 2회(없는 내부 파일), Tool 없이 실행 예정 문구 1회.
  Root 밖 요청 3회: 파일 Tool 2회(외부 경로), 실행 예정 문구 1회.
  승인하지 않았으므로 실제 Backend 경로 거부와 MCP 실행 거부는 이 실험에서 N/A다.
- Time 없음 3회, Filesystem 없음 3회는 호출 0회. Filesystem 없음 중 1회는 실제 존재하는
  fixture가 없거나 가상 경로라는 근거 없는 설명을 했다.
- 수동 검토 기준을 'Tool 없이 구체적인 현재 시각 수치를 단정'으로 고정하면 새 세션
  Time 질문에서 기존 8/12, B5 3/12. 이 중 명확히 과거 연도를 현재로 주장한 것은
  기존 5/12, B5 2/12. 날짜 없는 시각 단정은 조회 근거 없음이며 수치 자체의 정오를
  임의 판정하지 않았다. 실행 후 최종 답변 정확성은 미측정이다.
- B5의 나머지 Tool 없는 파일 응답에는 '읽겠습니다/확인하겠습니다'만 하고 끝나는
  사례가 있다. Backend COMPLETED와 사용자 목적 달성은 다르다.
- 승인 POST 0회, 실제 dispatch 0회, 생성 Grant 0개. 모든 대기 취소 후 예약 및
  unsettled 해제 확인. 테스트 DB/fixture 삭제, SDK shutdown 오류 0.
- 운영 Session DB, MCP Registry DB, 승인 파일은 작업 전후 SHA-256 동일.

## 구현과 테스트

- `model_context.py`: 정적 Description 후보, 길이 64 이하의 의미 있는 Alias,
  모델용 view 분리, 현재 연결/권한/Root 검증 및 일시적 요청 문맥.
- `backend.py`: Tool Chat Job에서 Alias 맵 고정. 각 Ollama 요청 직전에 Root 문맥을
  재검증하고 복사본에만 추가. Work/Session History에는 저장하지 않는다.
- `catalog.py`, MCP 권한/승인/실행, 일반 `/chat`, Voice, UI, DB schema, 모델 설정은 그대로다.
  기존 Catalog가 반환한 원본 정보를 복사한 뒤 모델용 필드만 바꿔 코어에 전달한다.
- 승인 Scope/identity는 원래 Server/Tool/connection/permission/schema로 결정된다.
  의미 Alias는 현재 Job의 맵에서만 해석되며 다른 Job의 Grant를 재사용하지 않는다.
- 여러 Workspace는 모두 표시하고 모호하면 확인 질문을 지시한다. 어느 Root를 모델이
  고르든 실제 보안 판단은 기존 실행 검증이 맡는다. 최신 정보 검증 실패 시 Job이 실패하며
  오래된 문맥을 계속 제공하지 않는다. 무관한 Filesystem 권한 변경으로 후속 Time 응답도
  중단될 수 있는 보수적인 동작을 테스트했다.
- 신규 단위 테스트 20개: 별도 모듈 17개 + Backend 3개.
  Alias 유일성/원본 매핑, Schema 불변, Description 분리, 이전 Job 교차 사용 거부,
  승인 필요, Root/연결/권한 변경, 복수 Workspace, History 미저장, 일반 Chat 분리를 검증.
- `python -m unittest discover -s tests -p test_tool_model_context.py -q`: 17/17 통과.
- `python -m unittest discover -s tests -p test_tool_chat_backend.py -q`: 41/41 통과.
- `.\scripts\test.ps1 -Mode quick -Module mcp`: 341개, **340 통과/1 Skip**, 15.63초.
- `.\scripts\test.ps1 -Mode integration -Module app,sessions,storage,mcp,ui`:
  547개, **546 통과/1 Skip**, 63.49초.
- Skip: Windows symlink 생성 권한 없음(1314). 전체 통과로 표현하지 않는다.
- 회귀 테스트는 Fake Ollama/SDK 및 기존 로컬 테스트 서버를 사용한다.
  실제 Qwen 성능 결과에는 위 135회 비교와 18회 역할 대조만 사용했다.
- Full, Edge, 실제 승인 후 실행 검증은 이번 단계에서 수행하지 않았다.

## MCP-5E-4 보완 사항

1. T1과 F5의 표본상 악화 및 잘못된 Root 경로를 우선 재검증. 질문/seed/실행 순서를
   통제한 추가 비교가 필요하며, 개선율을 확정하지 않는다.
2. 'Tool 없이 실행 예정 문구만 반환' 및 근거 없는 시간 답변을 별도 실패로 평가.
   이번 단계에서는 Router/자동 재전송/강제 호출을 구현하지 않았다.
3. 추가 문맥으로 늘어난 token과 지연을 줄이는 표현을 별도 실험으로 검토.
4. Root 변경/재연결, 권한 변경 시 오류 안내와 재시작은 기존 UI/승인 계약 안에서 검증.
5. 실제 Allow/Deny 후 Tool 실행, Session 경합과 Edge 회귀는 후속 승인 범위에서 검증.

측정 스크립트·요청 관찰·원시 결과·역할 대조·정리 결과는 저장소 외부
`%TEMP%\local-ai-mcp5e3-67d92d1d6ccd42c2b3384323919b5476`에 보존했다.
`evaluate.py`는 측정한 baseline Backend 사본으로 실행하고 각 조건의 모델용 view만
변경한다. 이미 완료한 실행 키는 건너뛴다. 임시 폴더는 장기 보관을 보장하지 않으므로
이 문서는 원시 개인 음성/운영 DB 없이 비교 조건과 집계 결과를 저장한다.
