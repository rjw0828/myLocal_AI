# Local AI Lab — Base Framework v1.0

## MCP-1/2/3 개발 브랜치 추가 기능

MCP-5C Backend 통합과 MCP-5D-3 Tool Chat UI 연결을 추가했습니다. D의 **Tool Chat OFF/ON**은 기본 OFF이며, ON 상태에서 Enter를 누르면 `/tool-chat/jobs`를 사용합니다. 승인 Dialog에서 서버·도구·인수·실제 Root 범위를 확인한 뒤 Allow/Deny를 선택합니다. Close/Esc는 승인·거부가 아니라 Dialog만 닫습니다. 승인 내용이 바뀌면 Review approval로 다시 확인해야 합니다. E에는 진행 상태와 완료된 최종 답변, F에는 민감한 인수를 제외한 기술 로그를 표시합니다. HTTP 202는 접수일 뿐 완료가 아닙니다. 성공한 질문·최종 답변만 SQLite에 저장하며 History로 동기화합니다. Root 확인 실패 시 Filesystem은 실행하지 않으며 전체 단일 Root 승인 제한을 유지합니다. 계약과 운영 제한은 [MCP-5C API 문서](tool_calling/API.md), UI·복구 정책은 [클라이언트 문서](tool_calling/CLIENT.md)를 참고하세요.

MCP-5B에서 Job·Session 예약 인터페이스·TurnGrant·도구 Catalog·보수적인 Windows 경로 정책의 독립 Python 코어를 구현했습니다. 이후 MCP-5C에서 Backend·Root 확인 어댑터·세션 이벤트, MCP-5D-3에서 수동 Tool Chat 전송·Web 승인을 연결했습니다. 일반 Chat·수동 STT·기존 Play TTS는 유지합니다. Tool Chat과 자동 Voice Conversation은 동시에 시작할 수 없으며 Tool Chat에 자동 TTS는 추가하지 않았습니다. MCP-5D-4에서는 Reset/Delete/Delete All 사용자 확인 후 대상 Job의 Cancel과 새로운 GET 상태 확인을 먼저 수행합니다. 예약 또는 미확정 작업이 남거나 검증이 실패하면 데이터 변경 요청을 보내지 않습니다. 취소 확인 후에도 `reservation_owned=false`, `unsettled=false` 확인이 필요하며 Backend의 기존 예약/revision 검증은 유지됩니다. 코어 계약과 제한은 [MCP-5B 코어 문서](tool_calling/README.md)를 참고하세요.

MCP-5C 검증(2026-11-13): 신규 Backend 테스트 34개 통과. 지정 quick 306개(305 통과/1 건너뜀, 23.34초), integration 356개(355 통과/1 건너뜀, 52.61초)를 확인했습니다. 건너뜀은 기존 Windows symlink 생성 권한 제한이며 junction은 실제 검증했습니다. 별도 임시 Root에서 실제 Filesystem 서버의 Root 밖 거부와 승인 후 파일 읽기·History 저장도 확인했습니다(Ollama만 Fake). 이번 단계의 실 Qwen 재호출·Full·Edge 검증은 수행하지 않았습니다.

MCP-5D-3 검증(2026-11-13): 신규 Console 연결 테스트 26개, Tracker/Client 테스트 43개 통과. UI quick는 기존 Voice·MCP UI를 포함해 204개 통과(0.95초)했습니다. 실제 JS 이벤트 처리 코드에 Mock Fetch·브라우저 API 모형을 사용했으며 실제 DOM/Edge·Ollama·MCP 서버 검증이나 Full 테스트는 수행하지 않았습니다. 자동 Polling 종료 후 버튼의 요청 중 상태가 해제되지 않던 문제도 재현 테스트와 함께 수정했습니다.

MCP-5D-4 검증(2026-11-13): Console 43개, Tracker/Client 44개 통과. 신규 방어 검증은 JS 18개 및 Backend 3개입니다. 지정 quick `-Module ui,mcp`는 392개 중 391 통과/1 건너뜀(13.23초), integration `-Module app,sessions,storage,mcp,ui`는 522개 중 521 통과/1 건너뜀(54.30초)입니다. 건너뜀은 기존 Windows symlink 권한 제한입니다. 새 테스트는 브라우저 API 모형/Fake Ollama·MCP/임시 SQLite 기반이며 integration에는 기존 실제 SDK stdio fixture 검증도 포함됩니다. Full·Edge·실 Qwen 검증은 하지 않았습니다.

새로고침 후에는 알려진 Job ID만 GET으로 확인합니다. 승인·Job 생성 POST는 재실행하지 않으며 과거 승인은 복원하지 않습니다. 완료된 기록은 History로 복원합니다. Job ID 유실, 404, 통신 실패, UNCERTAIN은 자동 해제하지 않습니다. Cancel 실패 후 Retry Cancel은 명시적인 사용자 재시도입니다. Backend API/DB Schema 확장은 하지 않았으며 Job ID가 없는 경우의 복구 API는 없습니다. 동일 MCP 서버에 대한 동시 Discovery는 기존 Runtime의 `mcp_busy` 제한으로 실패할 수 있습니다. Full과 실제 MCP 검증은 아래 MCP-5D-5B 결과를 참고하며, Edge 실기기 검증은 남아 있습니다.

MCP-5D-5B(2026-11-13): 승인 HTTP 오류가 E에서 다음 render/Polling에 덮어써지던 문제와 Tracker·Console·Session 처리 catch의 동일 오류 중복 로그를 재현해 최소 수정했습니다. E는 실제 Job 상태·예약과 마지막 요청 오류를 함께 표시합니다. 동일 오류 객체의 재기록만 막고, 서로 다른 요청의 같은 오류 코드는 합치지 않습니다. 잘못된 경로의 승인 거부는 WAITING_APPROVAL을 유지할 수 있으며 Cancel/Deny 또는 기존 승인 대기 제한으로 종료됩니다. FAILED의 Polling 종료·예약 해제, 다른 세션 사용, Cancel/상태 확인을 모형 테스트로 검증했습니다. 안전 정책과 Backend API/SQLite Schema는 변경하지 않았습니다.

검증: 신규 회귀 테스트 5개 추가 후 Console 47/47, quick ui,mcp 397개(396 통과/1 건너뜀, 14.85초), integration app,sessions,storage,mcp,ui 527개(526 통과/1 건너뜀, 59.49초), 최종 Full **1회** 552개(551 통과/1 건너뜀, 60.79초)입니다. 건너뜀은 Windows symlink 권한 제한입니다. 실제 Qwen + Time/Filesystem에서 Time 호출, 파일 Allow/Deny, Time→Filesystem 순차 호출과 History 저장을 확인했습니다. Root 밖/상대경로 질문의 실제 모델 선택은 실패하거나 호출을 생략했으므로 차단 성공으로 계산하지 않았습니다. 별도의 Fake Ollama + 실제 Filesystem/SDK/Flask/임시 DB 검증에서 Backend의 두 경로 거부와 예약 유지→Cancel 해제, 실제 MCP 서버의 Root 밖 읽기 거부를 확인했습니다. 테스트 파일과 운영 DB·승인 파일은 보존했습니다.

릴리스 판정은 **CONDITIONAL**입니다. 재현한 코드 결함은 회귀 검증을 통과했지만 Edge 직접 조작·실제 마이크/스피커 검증은 미실행입니다. 이전 Computer Use URL 식별 보안 제한을 우회하지 않았습니다. Dialog/Esc/포커스, Cancel/Retry, 세션 전환, Reset/Delete, 새로고침, Chat/Voice, 작은 창 배치의 사용자 확인이 필요합니다. 자동 도구 선택 품질은 MCP-5E 과제로 남기며 이번 단계에서 개선하지 않았습니다. 자세한 증거 구분과 수동 절차는 [클라이언트 문서](tool_calling/CLIENT.md)를 참고하세요.

Base Framework v1.0 이후 `feature/mcp-manager`에서 MCP-1 Core Runtime과 MCP-2 Registry/Flask API를 추가했습니다. MCP 전용 DB는 `data/mcp_registry.sqlite3`이며 기존 Session DB와 스키마는 변경하지 않습니다. 로컬 소유자가 승인한 stdio 실행 프로필만 등록하고, 도구별 기본 거부 권한과 인수에 묶인 일회용 승인 토큰으로 수동 호출합니다. 재시작 시 등록·권한만 복원하고 서버는 자동 실행하지 않습니다.

이 브랜치의 Flask 실행 환경에는 `.\.venv\Scripts\python.exe -m pip install -r requirements-mcp.txt`로 MCP 의존성도 설치해야 합니다. 기존 환경에는 MCP-1에서 설치한 SDK 2.3.0을 재사용합니다. MCP 검증은 실제 SDK stdio 테스트와 Mock/임시 SQLite 테스트를 구분하며 Edge UI 검증을 뜻하지 않습니다.

MCP-2 검증 결과: 기존 249개에 41개를 추가하여 총 290개(Python 214, JavaScript 76)가 통과했습니다. 신규 실제 SDK/Flask stdio 통합 5개는 등록·수동 호출, 별도 Python 프로세스의 등록/권한 복원과 자동 실행 없음, Timeout, Stop 및 종료 시 자식 프로세스 정리를 확인합니다. 운영 Session DB는 검증 전후 SHA-256이 동일합니다.

실행 승인 파일, API 경로·요청 형식, Timeout/종료 정책과 테스트 범위는 [MCP-2 API 문서](mcp_manager/API.md)를 따릅니다. MCP-3는 아래 관리 UI를 추가하며 Streamable HTTP 및 LLM 자동 Tool Calling은 미구현입니다. STEP 8 전체 완료를 의미하지 않습니다. 기본 Flask 실행 명령과 기존 Session/Voice/Ollama 사용 방법은 아래와 같습니다.

### MCP-3 — MCP Server Manager

B의 **AI Serve Locals**를 열고 Ollama 관리 아래로 스크롤하면 MCP Server Manager가 표시됩니다. 등록 목록은 이름, 승인 프로필 설명, stdio 연결 방식, 현재 런타임 상태를 표시합니다. 기존 MCP-2에는 독립적인 서버 설명 필드가 없어 승인 프로필 ID를 설명으로 표시합니다. 새 실행 설정은 웹에서 제안할 수 있지만 별도 로컬 터미널 승인 전에는 등록·실행할 수 없습니다.

1. 소유자가 검토한 승인 파일을 기존 MCP-2 문서대로 준비하고 Controller를 재시작합니다. `start_app.bat`은 기존 `LOCAL_AI_MCP_APPROVALS` 값을 우선 사용하고, 값이 없을 때만 프로젝트의 `data/mcp_approved_profiles.json`이 있으면 자동 연결합니다. 둘 다 없으면 기존처럼 승인 프로필 없이 시작합니다. 명시한 경로가 잘못돼도 다른 파일로 조용히 대체하지 않습니다. 헤더 오른쪽 Refresh MCP 옆의 **+ Add Server**를 누르면 등록 모달이 열립니다. 승인 프로필이 없으면 모달에 설정 안내를 표시하고 Register는 비활성화합니다. 운영 승인 파일을 Git에 추가하지 마세요.
2. 모달에서 Approved profile과 선택적인 Server name을 입력한 뒤 Register를 누릅니다. 등록 성공 시 목록 갱신을 마치고 모달을 닫습니다. Cancel 또는 Esc로 등록 전 취소할 수 있고, 요청 처리 중에는 중복 제출/닫기를 막습니다. 등록 오류는 모달 안에 표시하며, 등록 후 목록 갱신만 실패했을 때는 재등록을 막고 Cancel 후 Refresh MCP로 재동기화합니다. 등록만으로 서버가 시작되지 않으며 Start를 명시적으로 눌러야 합니다.
3. RUNNING 서버의 Tools를 누르면 왼쪽에 도구 이름 목록이 표시됩니다. 이름을 누르면 설명·권한·접힌 JSON Schema 상세가 열리고, 다시 누르면 닫힙니다. 다른 도구를 선택하면 이전 도구는 접힙니다. 도구는 기본 Blocked이며 Allow tool 확인 후 실행할 수 있습니다. Block tool로 다시 차단할 수 있습니다.
4. 오른쪽 자동 폼에 인수를 입력하고 Approve & Run once를 누릅니다. 서버·도구·정확한 인수를 확인해야 `/approve`와 `/call`이 각각 1회 실행됩니다. 일회용 토큰은 로그나 영구 저장소에 저장하지 않습니다. 결과는 텍스트 요약을 먼저 표시하고 전체 응답은 Raw JSON response에서 펼칩니다.
5. 실행 결과는 A의 MCP 결과창에 안전한 텍스트로 표시하고 F에는 진행·완료·오류·시간만 기록합니다. Chat/Session/Voice 기능과 데이터는 별도입니다.
6. 시간 초과 시 자동 재시도하지 않습니다. operation ID가 있으면 Check pending result로 늦은 결과를 조회합니다. 완료를 확정할 수 없으면 해당 서버 호출을 차단하고 명시적 Stop 성공 후 Start를 사용합니다. 브라우저 요청 취소는 서버 부작용의 롤백이 아닙니다. 같은 탭의 새로고침에도 미확인 호출 표식을 sessionStorage로 보존합니다.
7. 등록 삭제는 STOPPED 상태에서 확인 후 가능합니다. Stop은 진행 중 도구의 부작용을 되돌리지 않습니다. Refresh MCP는 현재 상태를 다시 읽고 Tools 재검색을 요구합니다. 관리 화면을 떠나는 동안 승인만 끝난 요청은 호출하지 않고, 이미 전송된 호출의 늦은 결과는 다른 화면에 적용하지 않습니다.

UI 이벤트 검증은 실제 `mcp-ui.js`를 DOM/Fetch 모형으로 실행하며 실제 Edge의 스크롤·포커스·확인창 동작 검증을 대신하지 않습니다. 실제 SDK stdio 검증은 임시 DB와 기존 echo/add 테스트 서버만 사용합니다. 신규 패키지나 운영 프로필·DB는 생성하지 않습니다.

본문은 **MCP Servers**와 **Selected MCP Tools** 두 영역이며 두 제목은 같은 폰트 크기·굵기·스타일을 사용합니다. 서버 미선택 안내는 제목 아래 작은 문구로 표시하고, Tools 조회 후에는 해당 서버 이름을 안내 문구로 표시합니다. 등록 모달은 A 영역의 스크롤에 잘리지 않도록 body 아래의 네이티브 dialog로 배치합니다. 실제 Edge에서 모달 포커스 이동·Esc/Cancel·작은 창 스크롤과 두 제목의 시각적 일치를 확인하세요.

### MCP UX 확장 — 로컬 승인과 자동 인수 폼

`+ Add Server`의 Registration mode에서 기존 승인 프로필 또는 Create new local profile을 선택합니다. 새 프로필은 Server name, stdio, 실행 파일의 절대 경로(바깥 따옴표 없이), Arguments를 입력합니다. Arguments는 **한 줄이 한 인수**이며 셸처럼 분리/치환하지 않습니다. 빈 줄은 생략하고, 개별 인수 내부의 공백은 그대로 보존합니다. 줄바꿈/빈 문자열 자체를 인수로 전달하는 고급 실행 설정은 이 입력 방식에서 지원하지 않습니다.

Request local approval에서 명령을 확인하면 실행하지 않는 제안 파일만 생성됩니다. 모달에 표시된 명령을 프로젝트 폴더의 로컬 PowerShell에서 실행하세요. `scripts/approve_mcp_profile.py`가 정확한 실행 파일·인수·작업 폴더와 저장 파일을 표시하고 `APPROVE <request-id>`를 직접 입력해야 승인됩니다. 이 스크립트는 프로그램을 실행하지 않습니다. 모달로 돌아와 Check approval & Register를 누르면 기존 등록 API로 STOPPED 서버가 생성됩니다. JSON 편집·환경 변수 지정·Controller 재시작은 필요 없습니다. 기존 승인 파일과 프로필은 보존합니다.

이 별도 터미널 단계는 웹 확인창으로 대체할 수 없습니다. 기존 헤더/Origin 검사는 OS 관리자 인증이 아니며, HTTP만으로 새 실행 설정을 스스로 승인하지 못하도록 분리했습니다. 여기서 로컬 관리자는 승인 파일을 관리할 권한이 있는 PC 소유자를 뜻하며 Windows UAC 인증/권한 상승을 구현한 것은 아닙니다. 완전한 웹 전용 관리자 등록은 별도 신뢰 채널/인증 설계가 필요합니다.

승인 파일은 명시한 `LOCAL_AI_MCP_APPROVALS`를 우선 사용하고, 없으면 MCP DB 폴더의 `mcp_approved_profiles.json`을 사용합니다(기본 `data/`). Refresh MCP와 승인 상태 확인은 로컬 소유자가 쓴 파일을 다시 읽습니다. 제안은 같은 폴더 아래 `mcp_profile_requests/`에 최대 64개, 유효 시간 24시간으로 보관합니다. Cancel은 해당 제안만 삭제하며 이미 승인된 프로필은 취소하지 않습니다. 완료된 등록은 제안을 정리하고, 서버 Delete도 승인 파일 자체를 지우지 않습니다. 승인 도중에는 독점 `.lock`과 임시 파일/원자 교체를 사용합니다. 비정상 종료로 남은 lock은 자동 탈취하지 않으므로 다른 승인 작업 종료를 확인한 뒤 로컬 관리자가 점검해야 합니다. 승인 파일을 동시에 수동 편집하지 마세요. 설정·제안·인수는 평문 로컬 자료이고 Git에서 제외됩니다.

자동 폼은 string/number/integer/boolean/primitive enum, required/default, 중첩 객체(최대 깊이 4), 배열(JSON 배열 입력)을 지원합니다. 선택적 필드는 Include를 켰을 때만 전송하고 기본값은 자동 포함합니다. 안전한 정수 범위·유한 숫자·기본 길이/범위를 검사하며 최종 스키마 검증은 서버가 수행합니다. pattern/multipleOf 같은 일부 세부 제약은 서버 검증에 맡깁니다. `$ref`, oneOf/anyOf/allOf, nullable 타입 조합 등 미지원 스키마와 60개를 넘는 폼 노드는 Raw JSON으로 전환합니다. 외부 참조를 내려받거나 JavaScript 정규식으로 서버 패턴을 실행하지 않습니다. Advanced Raw JSON을 직접 켤 수 있으며 자동 폼으로 돌아가면 현재 스키마/기본값으로 재구성합니다. 스키마를 다시 조회하면 이전 입력은 초기화되고 서버 권한도 스키마 fingerprint가 맞아야 유효합니다.

Help?는 전체 사용 순서를, 도구의 ?는 설명·필수 인수·예시를 표시합니다. 예시는 정답/실행 보장이 아니므로 검토 후 사용합니다. 작은 창에서는 좌우 도구/인수 레이아웃이 세로로 전환됩니다. 기존 120초 일회용 승인, 기본 거부, Timeout/불확실 상태의 자동 재시도 금지는 유지합니다. 실제 Edge의 모달/아코디언/포커스·작은 화면과 Time MCP의 timezone 기본값 폼은 사용자 수동 확인 항목입니다.

UX 확장 검증(2026-11-10): 직전 328개(레이아웃 변경과 시작 배치 테스트 포함)를 유지하고 Python 로컬 승인 경계 16개와 JS 입력 폼/등록·실행 UI 22개를 추가했다. MCP quick 135개/5.80초, integration 151개/36.51초, full 366개(Python 238 + JS 128)/51.43초가 통과했다. 실제 로컬 CLI를 임시 파일에서 실행해 확인·취소·변조 거부·동시 쓰기·원자 저장 실패를 검증했다. DOM/Fetch 모형은 실제 Edge 검증을 대신하지 않는다. 별도 임시 DB에 기존 Time 프로필을 복사해 실제 stdio 연결, 실제 스키마의 자동 폼 지원 및 `Asia/Seoul` 기본 인수, 승인 후 시간 조회와 정상 종료를 확인했다. 운영 DB 두 개와 기존 승인 파일의 SHA-256은 유지됐다. 패키지·모델 변경과 Commit/Push는 수행하지 않았다.

MCP-3 자동 검증: 기존 290개를 유지하고 Flask 화면/정적 파일 2개와 UI 이벤트 23개를 추가했다. quick MCP 84개(3.78초), integration MCP 100개(35.27초), full 315개(Python 216 + JavaScript 99, 56.00초)가 통과했다. JavaScript/PowerShell 문법과 `git diff --check`도 통과했고 운영 Session DB SHA-256은 변경되지 않았다. 실제 Edge 및 서드파티 MCP 서버 연결은 이번 자동 검증에 포함하지 않았다.

2026-11-10 레이아웃 개선 검증: 모달/제목 구조 검사 1개와 모달 이벤트 검사 7개를 추가했다. MCP quick 92개/3.94초, MCP integration 108개/33.59초, UI quick 106개/0.77초가 통과했다. 모달 취소·Esc, 등록 성공 후 목록 갱신과 닫기, 오류 복구, 중복 제출 차단 및 제목 유지 동작을 검증했다. 이번 레이아웃 변경에서는 full을 재실행하지 않았으며 위 315개 결과는 최초 MCP-3 구현 시점의 기록이다. DOM/Fetch 모형과 Flask 렌더링 검증은 실제 Edge의 시각적 배치·포커스 검증과 구분한다.

권장 명령은 개발 중 `.\scripts\test.ps1 -Mode quick -Module mcp`, 기능 완료 시 `-Mode integration -Module mcp`, 주요 체크포인트 및 Merge 전 `-Mode full`입니다. MCP UI 테스트는 MCP/ UI 모듈 선택과 full 모두에 포함됩니다. 개별 진단 명령은 `node tests/test_mcp_ui.js`입니다. 기존 Python 214개와 JS 76개는 그대로 보존합니다.

Edge 수동 검증: Controller를 `.\.venv\Scripts\python.exe app.py`로 실행하고 Ctrl+F5 후 승인된 테스트 프로필의 등록→Start→Tools→Allow→인수 입력→실행 확인/취소→결과→Block→Stop→삭제를 확인합니다. 작은 창에서 A 내부 스크롤과 키보드 접근을 확인하고, 실행 중 메뉴 전환·Timeout·Refresh 이후 재실행 차단 및 Check pending result를 확인합니다. 이후 D/E/F의 기존 텍스트 Chat·Session 전환·수동/자동 Voice가 정상인지 확인합니다. 실제 Edge 및 서드파티 MCP 서버 검증은 별도 사용자 확인 항목입니다.

신규 MCP 실행 프로필의 Arguments는 각 줄 앞뒤의 공백·탭을 등록 전에 검사합니다. 문제가 있는 줄 번호를 안내하고 제출을 차단하며 자동으로 값을 바꾸지 않습니다. 경로 중간의 공백은 허용하고 빈 줄은 생략합니다. 서버도 독립적으로 검사해 HTTP 400 `mcp_argument_whitespace`를 반환합니다. 기존 승인 프로필과 등록 정보는 자동 수정하지 않으므로, 기존에 잘못 등록된 경로는 새 프로필로 다시 승인·등록해야 합니다.

새 프로필의 승인 대기 화면에서 PowerShell을 여는 방법과 승인 순서를 확인할 수 있습니다. `승인 명령 복사`를 누르면 명령만 클립보드에 복사하고 성공 여부를 표시합니다. 프로젝트 폴더에서 연 PowerShell에 붙여넣어 실행하고 실행 설정을 검토·승인한 다음, 웹의 `승인 확인 및 등록`을 누릅니다. 복사 권한이 없으면 화면의 명령을 직접 선택해 복사할 수 있습니다. 복사 버튼은 명령 실행이나 승인을 대신하지 않습니다.

터미널에서 실행 설정을 확인한 뒤 웹의 `APPROVE 문구 복사` 버튼으로 현재 요청의 `APPROVE 요청 ID`를 복사할 수 있습니다. PowerShell이 승인 입력을 기다릴 때 붙여넣고 Enter를 누릅니다. 두 복사 버튼 모두 클립보드에만 쓰며, 자동 승인이나 명령 실행은 하지 않습니다.

## 공식 기준 버전

**Base Framework v1.0: 완료.** STEP 5, STEP 6-1~6-4, STEP 7을 완료했으며 사용자가 STEP 7 최종 Edge 실기기 검증 **7/7 통과**를 확인했습니다. 자동 테스트는 최근 실행 기준 **215개 통과**입니다(Python 139, 음성 작업 관리자 19, UI 이벤트 모형 57). 사용자 실기기 확인과 에이전트의 자동 테스트를 구분합니다. 릴리스 태그는 `v1.0.0-base`이며 이후 기능 개발의 기준으로 사용합니다.

미완료/후속: STEP 6-5 음성 품질·성능 심화 검증은 연기 상태입니다. ISSUE-002의 System Prompt 템플릿 구조 개선, STEP 8 MCP·Tool Calling, 검색 기반 장기 기억·모델 선택·주변기기 확장은 별도 계획입니다. SQLite 대화 영구 저장 완료가 이러한 후속 기능의 구현 완료를 뜻하지는 않습니다. 아래 과거 단계별 테스트 수와 당시 미검증 기록은 개발 이력이며, 현재 완료 상태는 이 절과 로드맵을 기준으로 합니다.

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
- 저장된 세션이 없으면 빈 상태를 표시합니다. 첫 접속·마지막 세션 삭제·새로고침에서 세션을 자동 생성하지 않으며, D의 `+ New Chat`을 누르면 서버에서 새 내부 ID와 표시 번호를 받습니다.
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

- 선택된 세션의 사용자 메시지와 AI 답변을 구분하여 표시하고, 새로고침·Flask 재시작·세션 전환 시 SQLite를 사용하는 서버 History API로 복원합니다.
- E 하단 `Session List`를 누르면 위로 펼쳐지는 목록에서 세션을 선택할 수 있습니다. 항목 선택·버튼 재클릭·바깥 클릭·Esc로 닫고, 목록이 많으면 팝업 안에서 스크롤합니다.
- 바로 옆 `Reset Session`은 확인 후 현재 선택한 세션의 대화·제목을 초기화합니다. UI의 `+ New Chat`으로 만든 세션은 번호와 목록 항목을 유지합니다. Locked 상태에서도 사용할 수 있으며, Reset 취소·진행 중 Chat 대기·늦은 History 무효화 정책을 유지합니다.
- Session List 각 항목의 `Delete`는 대상 번호·제목 확인 후 세션 자체와 History를 삭제합니다. Delete 클릭은 세션 선택으로 전달되지 않습니다. 현재 세션 삭제 시 다음 항목을 우선 선택하고 없으면 이전 항목을 선택합니다. 마지막 항목 삭제 후에는 빈 상태이며 `+ New Chat`으로만 새로 시작합니다. D의 작성 중인 문장은 유지합니다.
- 각 AI 답변의 `Play`를 누르면 `/voice/synthesize`로 로컬 음성을 합성해 재생합니다. 자동 재생은 하지 않습니다.
- `AI Language`는 다음 Chat 답변과 해당 답변의 TTS 언어를 선택합니다. 한국어가 기본이며 English를 선택한 뒤 새 질문을 보내면 영어 답변을 요청합니다.
- 기존 답변은 생성 당시 언어로 재생됩니다. 설정을 바꾸는 것만으로 기존 답변이 번역되지는 않습니다.
- D의 Voice와 관계없이 AI Language가 새 답변의 텍스트와 TTS 언어를 결정합니다. 서버는 시스템 지시와 현재 질문에 출력 언어를 명시하며, 요청 전용 지시는 History의 사용자 질문에 저장하지 않습니다.
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

### Session API (SQLite)

`GET /sessions`는 DB의 세션 목록을 `{"sessions":[{"session_id":"...","number":1,"title":"새 대화"}]}` 형식으로 반환하며 조회만으로 세션을 만들지 않습니다. 번호는 개별 삭제·재시작 후에도 증가합니다. 성공한 Delete All만 다음 번호를 1로 초기화합니다.

`POST /sessions`는 본문 없이 DB에 새 세션을 만들고 commit 후 HTTP 201과 `{"session_id":"s-...","number":2,"title":"새 대화"}`를 반환합니다. 내부 ID는 UUID 기반이며 화면에는 표시 번호와 제목을 사용합니다. 영구 보관 세션 60개 한도에서는 HTTP 429 `session_limit_reached`를 반환합니다. Delete로 슬롯을 회복하며 재시작으로 한도가 초기화되지 않습니다.

`GET /sessions/<session_id>/history`는 `session_id`, `number`, `title`, `messages`를 반환합니다. `messages`에는 `role`(user/assistant), `content`, AI 답변의 `language`(ko/en 또는 null)가 포함됩니다. 없는 세션은 HTTP 404 `session_not_found`입니다. 첫 번째 **성공한** 질문의 앞 40자를 제목으로 사용하고 길면 `...`을 붙입니다. Reset 후에는 제목이 `새 대화`로 돌아가며 다음 성공한 질문으로 다시 정해집니다. 세션 번호·제목·History·답변 언어와 실제 시스템 언어 지시는 Flask 재시작 후에도 복원됩니다.

기존 외부 클라이언트의 `/chat`은 전달된 임의의 유효한 `session_id`로 계속 동작합니다. 이 방식으로 생성된 기존 호환 세션도 목록에 번호가 붙으며, Reset 후 빈 상태가 되면 기존처럼 제거됩니다. `+ New Chat`으로 만든 세션은 Reset해도 빈 항목으로 남아 번호를 유지합니다.

### `DELETE /sessions/<session_id>`

본문 없이 호출합니다. 성공 시 HTTP 200과 `{"session_id":"...","status":"deleted"}`를 반환합니다. 세션·History·제목·응답 언어 메타데이터를 제거하고 활성 세션 수를 줄입니다. 다음 세션 번호는 계속 증가하며 삭제한 번호를 재사용하지 않습니다.

- 없는 세션 또는 반복 DELETE: HTTP 404 `session_not_found`; 잘못된 ID: HTTP 400 `invalid_session_id`.
- Delete 접수 후 새 Chat/History/Reset 및 중복 Delete: HTTP 409 `session_deleting`.
- 이미 삭제된 ID의 Chat/History/Reset: HTTP 410 `session_deleted`. 알 수 없는 일반 ID의 기존 Chat 생성/Reset 정책은 유지합니다.
- 이미 실행 중인 Chat은 세션 Lock 안에서 완료한 뒤 Delete가 기록을 지웁니다. 대기 중이거나 새로 도착한 같은 세션 작업은 거부합니다. 다른 세션에는 영향을 주지 않습니다.

삭제된 ID와 삭제 시각은 DB의 `deleted_sessions`에 남겨 늦은 `/chat`이 재시작 후에도 같은 ID로 세션을 다시 만들지 못하게 합니다. 제목·본문·언어는 남기지 않습니다. 표식은 60개 한도에 포함하지 않으며 삭제 횟수에 따라 누적됩니다. WAL·백업의 과거 데이터까지 완전 소거하는 기능은 아닙니다.

브라우저는 삭제 성공 전 캐시를 지우지 않습니다. 현재 세션의 마이크/STT/TTS·자동 음성 턴을 취소하고 관찰 중 Chat이 끝난 뒤 DELETE를 전송합니다. 서버 완료 여부가 불확실한 Chat/Reset/Delete에는 DELETE도 자동 재시도하지 않습니다. HTTP 오류는 기록 후 최신 목록·허용되는 History로 재확인하고, 404/410은 이미 없는 항목으로 화면을 정리합니다. 통신 단절로 서버가 실제 삭제했는지 모르는 경우에는 불확실 상태를 안내하며 해당 ID를 차단합니다. 새 세션을 자동 생성하거나 보호 상태를 시간 경과로 해제하지 않습니다.

### `DELETE /sessions` — Delete All

Session List 팝업 상단의 `Delete All`에서 현재 목록의 개수를 조회해 확인창으로 안내합니다. 취소하면 삭제 요청을 보내지 않고 기존 대화·D 초안을 유지합니다. 확인하면 녹음/STT/TTS/Voice Conversation을 취소하고 관찰 중인 Chat/Reset 완료를 기다린 후 본문 없이 `DELETE /sessions`를 한 번 보냅니다. 완료 불확실 Chat이 있으면 전송을 차단합니다.

성공은 HTTP 200과 다음 JSON입니다.

```json
{"status":"deleted_all","deleted_count":2,"deleted_session_ids":["s-...","s-..."]}
```

서버는 전체 삭제 진행 중 신규 Create/Chat/History/Reset/개별 Delete/중복 Delete All/List를 HTTP 409 `session_deleting`으로 거부합니다. 이미 세션 Lock을 잡은 Chat/Reset/History가 끝난 뒤 모든 세션의 Lock을 확보하고, 짧은 SQLite 트랜잭션에서 (1) 현재 ID의 tombstone 추가 (2) 모든 세션과 cascade History 삭제 (3) `sqlite_sequence`의 sessions 카운터 초기화를 함께 commit합니다. 기존 tombstone은 지우지 않습니다. 실패 시 세 단계 모두 rollback하며 기존 세션을 RAM으로만 삭제하지 않습니다.

성공 후 E와 Session List는 빈 상태이며 새 세션을 자동 생성하지 않습니다. D 초안은 유지합니다. 사용자가 다음 `+ New Chat`을 누르면 새로운 UUID와 Session 1이 생성됩니다. 이 초기화는 DB 재시작 후에도 유지됩니다. 개별 Delete/Reset/일반 재시작은 번호를 초기화하지 않습니다. 세션이 이미 0개여도 Delete All 성공은 카운터를 초기화합니다.

클라이언트는 삭제 승인·완료 경계에서 History/목록/세션 버전을 무효화해 늦은 응답을 버립니다. HTTP 오류가 확인되면 F에 표시하고 DB 목록/History를 다시 조회합니다. 네트워크·파싱·성공 응답 검증 실패로 Delete All 완료를 모르면 빈 화면으로 성공 처리하지 않고 전체 세션 변경을 차단합니다. 이 차단은 같은 탭의 sessionStorage에 남아 새로고침 뒤에도 유지되며 시간 경과·Health·History 조회로 해제하지 않습니다. 재동기화 도구/자동 재시도는 추가하지 않았으므로 이 경우 서버 상태를 별도로 검증해야 합니다. 클라이언트 취소가 서버의 이미 시작된 모델 추론을 즉시 멈추거나 되돌리지는 않습니다.

확인창의 개수는 조회 시점 기준입니다. 다른 탭/클라이언트가 그 사이 세션을 만들면 실제 삭제 개수는 응답의 `deleted_count`를 기준으로 합니다. Delete All은 요청 처리 시점의 모든 세션에 적용됩니다. 여러 탭 사용 시 먼저 작업을 멈추고 필요한 DB 백업을 보관하세요. 백업·WAL까지 완전 소거하는 기능은 아닙니다.

### `POST /sessions/<session_id>/reset`

지정 Session ID의 DB 대화 기록·제목·언어 정보를 원자적으로 초기화합니다. 요청 본문은 없습니다. 다른 세션의 기록은 변경하지 않습니다. UI에서 생성한 세션은 ID와 번호를 유지합니다.

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

존재하지 않는 일반 ID도 같은 응답을 반환하지만 새 세션을 만들지 않습니다. 삭제 표식이 있는 ID는 HTTP 410입니다.

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
| 영구 보관 세션 | 최대 60개 (삭제 표식 제외) |
| Session ID | Python `len()` 기준 최대 128자 |
| Message | Python `len()` 기준 최대 10,000자 |
| TTS Text | Python `len()` 기준 최대 500자 |
| HTTP 요청 본문 | 최대 1MB |
| Ollama 생성 요청 제한 시간 | 180초 |
| 브라우저 요청 제한 시간 | 190초 |

- 대화 기록은 로컬 SQLite에 저장되며 Flask 재시작 후 복원됩니다. DB는 암호화되지 않은 평문 개인 데이터입니다.
- DB 기본 경로는 `data/local_ai_lab.sqlite3`입니다. `LOCAL_AI_DB_PATH`로 별도 로컬 경로를 지정할 수 있습니다.
- UI 생성 세션의 Reset은 슬롯을 유지하며 Delete는 슬롯을 회복합니다. 기존 호환용 암묵 생성 세션의 Reset/실패 시 빈 세션 정리는 유지합니다.
- 활성 세션 수는 제한되지만 세션별 대화 메시지 수는 제한되지 않습니다.
- 대화 기록 길이와 모델 컨텍스트 길이는 다릅니다. RAM에 기록이 남아 있어도 모델의 컨텍스트 한도를 초과하면 전체 대화를 정확히 해석하지 못할 수 있습니다.
- 자동 요약·오래된 기록 절단·검색 기반 장기 기억은 아직 포함되지 않습니다. SQLite 저장 자체가 모델 컨텍스트나 의미 기억을 확장하지는 않습니다.
- Thinking은 항상 OFF입니다.

## 테스트

### Python 단위 테스트

```powershell
cd F:\LLM\AI_Project\myLocalAI
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

### Python 문법 검사

```powershell
.\.venv\Scripts\python.exe -m py_compile app.py ai_controller.py ollama_runtime.py voice\stt_service.py voice\tts_service.py storage\sqlite_store.py
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

당시 STEP 6-4-B 마이크/Edge/스피커 전체 흐름은 사용자 실기기 확인 대상으로 남겼습니다. 이후 사용자 안정화 완료 및 STEP 7 최종 Edge 7/7 통과로 Base Framework v1.0을 확정했으며, 자동 검증과 사용자 실기기 보고를 구분합니다.

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

이 절의 RAM 소멸 설명은 STEP 5 당시 기록이다. STEP 7 이후에는 아래 SQLite 재시작 절차를 사용하며 기존 RAM 전용 프로세스의 기록은 자동 이전되지 않는다.

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
- **ISSUE-002 — 기능 검증 통과 / 구조 개선 후속:** 사용자가 기능 테스트 5개 통과를 확인했습니다. System Prompt/`{{ .Prompt }}` 템플릿 구조 개선은 후속 과제로 유지합니다.

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
- **STEP 5-6-3/4 — Session List / History / Create API 및 D/E 세션 전환:** 완료, 사용자 안정화 완료 보고
- **STEP 5-6-5 — Reset 독립성·비동기 검증:** 완료, 사용자 Edge 검증 확인으로 ISSUE-001 종료
- **STEP 6-1 — 음성 AI 환경 분석:** 완료
- **STEP 6-2 — 로컬 STT:** 완료, faster-whisper CPU INT8 기반 Push-to-Talk 음성 인식
- **STEP 6-3-A~E — 로컬 TTS 환경·독립 합성·Flask API·수동 재생·음질/지연 계측:** 완료
- **STEP 6-4 — 음성 통합:** STT → 기존 `/chat` → Qwen3.5 9B → TTS
- **STEP 6-4-A/B — 음성 대화 설계·구현:** 완료 — 기본 OFF, 한 턴 순차 실행·취소·불확실 세션 차단 및 사용자 안정화 완료 보고
- **STEP 7 — SQLite 세션·대화 영구 저장:** 완료. 팝업 스크롤/Delete All을 포함한 최종 사용자 Edge 검증 7/7 통과
- **STEP 8 — MCP·LLM Tool Calling·전문 도구 연동:** 후속 개발, 미구현. allowlist와 입력 검증 기반으로 별도 설계
- **STEP 6-5 — 음성 품질·성능 심화 검증:** 미완료 / 연기. STEP 7·8 이후 진행하며 두 단계의 필수 선행 조건에서 제외
- **이후:** 장기 기억 검색, 대화 요약, 모델 선택, 페르소나, Bluetooth·게임패드·휴대전화 등 주변기기 연동은 별도 계획

기본 음성 동작을 확인했으므로 실행 순서를 STEP 5 완료→STEP 6-1~6-4 완료→STEP 7→STEP 8→STEP 6-5→장기 기억 검색·페르소나·주변기기로 조정했습니다. STEP 번호와 미완료 측정 항목은 유지하고 플랫폼 구축을 먼저 진행합니다. 기존 Flask 및 Qwen3.5 9B와 독립 STT/TTS 구조, 추가 비용 0원과 로컬 실행 원칙을 유지합니다.

ISSUE-001의 SQLite 도입 전 검증 조건은 충족했습니다. ISSUE-002는 사용자 기능 테스트 5개 통과 상태이며 템플릿 구조 개선은 별도 후속 과제입니다. STEP 7은 아래 정책으로 구현했으며 STEP 8은 별도 승인 후 진행합니다.

## STEP 7 — 영구 저장·장애 처리·백업

표준 `sqlite3`만 사용하며 추가 설치는 없습니다. `sessions`, `messages`, `deleted_sessions` 테이블과 `PRAGMA user_version=1`을 사용합니다. WAL, `synchronous=FULL`, foreign keys 및 최대 3초 busy timeout을 적용합니다. 단일 Flask 프로세스를 지원하며 동일 DB를 여러 Controller 프로세스에서 동시에 사용하는 구성은 지원하지 않습니다.

서버 시작 시 메타데이터와 Lock만 복원합니다. 전체 History를 한꺼번에 RAM에 읽지 않고 필요한 작업에서 읽은 뒤 마지막 요청 종료 시 해제합니다. Ollama 추론 동안 DB 트랜잭션을 유지하지 않으며 성공한 질문·답변·제목·언어·시스템 지시와 revision을 단일 트랜잭션으로 확정합니다. Chat/Reset/Delete는 기존 세션 Lock 순서를 유지하고 저장 revision으로 늦은 쓰기도 거부합니다. 기존 브라우저 Reset 버전·세션 세대·불확실 Chat 차단은 변경하지 않았습니다.

실행 중 DB busy는 HTTP 503 `storage_busy`, 읽기 전용·용량·IO·접근 오류는 503 `storage_unavailable`, 기타 SQL/저장 오류는 500 `storage_error`, 오래된 revision은 409 `session_conflict`입니다. 오류 JSON은 기존 `{error:{code,message}}` 구조입니다. 조용한 RAM 폴백, 성공으로 위장한 응답, 모델 자동 재시도는 없습니다. 시작 시 손상·지원하지 않는 스키마는 시작을 중단하고 원본을 덮어쓰지 않습니다. `/health`는 기존 Flask/Ollama 상태 계약을 유지하며 DB 무결성 검사 API가 아닙니다.

STEP 7 착수 당시 RAM 보존 확인에서는 `127.0.0.1:5000/sessions`에 연결되지 않았고 운영 DB도 없었습니다. 당시 서버 재시작·내보내기·이전·사용자 세션 변경은 수행하지 않았습니다. 이후 사용자가 Edge에서 SQLite 실제 동작을 확인했습니다. 이전 RAM 전용 서버에 보존할 기록이 따로 있다면 **종료 전에** 작업을 멈추고 List/History snapshot 보존 범위를 먼저 협의해야 합니다. 새 프로세스는 이미 종료된 프로세스의 RAM을 복구하지 못합니다.

### 수동 백업 (프로젝트 루트 PowerShell)

기본 DB 또는 `LOCAL_AI_DB_PATH`가 가리키는 기존 DB에서 SQLite backup API로 일관된 snapshot을 만듭니다. 열린 DB 본체만 단순 복사하지 마세요. 아래 명령은 사용자가 필요할 때 실행하며 이번 구현에서는 운영 백업을 실행하지 않았습니다.

```powershell
$backupFile = ".\backups\local_ai_lab_$(Get-Date -Format 'yyyyMMdd_HHmmss').sqlite3"
.\.venv\Scripts\python.exe -c "import os,sys; from pathlib import Path; from storage.sqlite_store import SQLiteStore,DEFAULT_DB_PATH; p=Path(os.environ.get('LOCAL_AI_DB_PATH') or DEFAULT_DB_PATH); assert p.is_file(), 'Existing DB required'; SQLiteStore(p).backup(sys.argv[1])" $backupFile
```

같은 이름의 백업은 덮어쓰지 않습니다. 백업에도 개인 대화가 포함됩니다. DB·WAL/SHM/journal·`data/`·`backups/`·`session_exports/`는 Git에서 제외합니다. 다른 위치로 수동 복사한 JSON/오디오/백업도 직접 보호해야 합니다.

### 수동 복원

1. 모든 음성·Chat 작업이 끝난 뒤 Flask를 Ctrl+C로 정상 종료합니다. Ollama/GPU 설정은 변경하지 않습니다.
2. 현재 DB가 있으면 위 명령으로 별도 백업합니다. 원본·sidecar를 임의 삭제하지 않습니다.
3. 복원할 백업의 무결성을 검사합니다. 출력이 `ok`이고 `user_version`이 `1`이어야 합니다.

```powershell
.\.venv\Scripts\python.exe -c "import sqlite3,sys; from pathlib import Path; from contextlib import closing; p=Path(sys.argv[1]).resolve(); assert p.is_file(); c=sqlite3.connect(p.as_uri()+'?mode=ro',uri=True); print(c.execute('PRAGMA integrity_check').fetchall()); print(c.execute('PRAGMA user_version').fetchone()); c.close()" ".\backups\선택한백업.sqlite3"
```

4. 백업을 **새 파일명**으로 복사하고 이번 터미널에서 복원 경로를 지정합니다. 기존 운영 DB와 WAL을 섞거나 덮어쓰지 않습니다.

```powershell
if (Test-Path -LiteralPath ".\data\restored_local_ai_lab.sqlite3") { throw "새 복원 파일명을 지정하세요." }
Copy-Item -LiteralPath ".\backups\선택한백업.sqlite3" -Destination ".\data\restored_local_ai_lab.sqlite3"
$env:LOCAL_AI_DB_PATH = (Resolve-Path ".\data\restored_local_ai_lab.sqlite3").Path
.\.venv\Scripts\python.exe app.py
```

5. List/History/언어와 삭제 ID를 확인합니다. 이후 실행에도 같은 경로를 사용하세요. 기본 DB로 돌아가려면 Flask 종료 후 `Remove-Item Env:LOCAL_AI_DB_PATH`를 실행합니다. 복원은 백업 시점으로 돌아가므로 백업 이후 생성/삭제한 기록과 표식은 복원되지 않습니다. 기록 완전 소거·암호화·자동 백업/이전은 구현하지 않았습니다.

### 자동 검증과 Edge 수동 검증

기존 174개를 유지하고 SQLite 검증 22개를 추가했습니다. 총 196개(Python 131, 작업 관리자 19, UI 모형 46)가 통과했습니다. Python/JavaScript 문법 검사와 Git 제외 확인도 통과했습니다. Python 저장 테스트는 실제 임시 SQLite 파일·연결·자식 프로세스 재시작/강제 종료를 사용하며 Ollama는 Mock입니다. 실제 DB busy·손상·rollback·백업 복원과 별개로 readonly/full/IO 장애는 예외 주입 테스트입니다. 기존 개인 DB나 실모델을 호출하지 않습니다. JS는 실제 main.js와 작업 관리자를 모형 환경에서 실행하며 Edge DOM/마이크/스피커 검증을 대신하지 않습니다.

Edge에서는 다음을 별도로 확인합니다. 이번 구현에서 직접 실행한 실기기 검증은 아닙니다.

1. `.\.venv\Scripts\python.exe app.py` 실행 후 `+ New Chat`으로 검증용 A/B를 만듭니다. 서로 다른 질문과 AI Language를 사용하고 번호·제목·답변을 기록합니다.
2. 요청 완료 후 Flask만 Ctrl+C로 종료하고 같은 DB 경로로 다시 실행합니다. Edge 새로고침 후 A/B 목록·번호·제목·History·각 답변 Play 언어가 유지되어야 합니다. 화면의 현재 AI Language 선택 자체는 기존 UI 정책이며 서버가 저장한 각 답변 언어와 구분합니다.
3. A Reset 후 재시작합니다. A 번호는 유지되고 제목은 `새 대화`, History는 비어 있으며 B는 그대로여야 합니다.
4. A Delete 후 재시작합니다. A가 복원되면 안 되며 다음 New Chat 번호는 이전 번호를 재사용하지 않아야 합니다. 마지막 삭제 후 새 항목이 자동 생성되면 안 됩니다.
5. A→B→A, Thinking 중 Cancel/Reset/Delete, 수동 STT/TTS 및 Voice Conversation을 확인합니다. 늦은 응답이 다른 세션·Reset/삭제한 기록을 되살리지 않아야 하며 완료 불확실 세션이 재시작/History 조회만으로 자동 해제되면 안 됩니다.

미완료: 실제 Edge 재시작·음성 회귀, 실물 디스크 장애/전원 차단, STEP 6-5 심화 성능 측정. 세션별 기록·삭제 표식·DB 크기 및 추론 시 전체 문맥 메모리는 계속 증가할 수 있습니다. 기본 DB는 암호화하지 않으며 자동 요약·절단은 수행하지 않습니다.

### STEP 7 후속 — Session List 스크롤 / Delete All 검증

사용자가 기본 SQLite 동작을 Edge에서 확인했습니다. 팝업이 스크롤 시 닫힌 원인은 `window` 캡처 단계의 scroll 핸들러가 팝업 내부의 non-bubbling scroll까지 받아 무조건 닫은 것이었습니다. 이벤트 대상이 팝업 안이면 닫지 않도록 수정했습니다. 기존 바깥 클릭·Esc·버튼 재클릭·외부 패널 스크롤 시 닫기는 유지합니다. 팝업은 body 직하의 fixed/z-index 구조를 유지하며 Delete All 헤더 높이를 제외한 목록만 내부 스크롤합니다.

기존 196개에 Python 8개와 UI 이벤트 모형 11개를 추가했습니다. 전체 215개(Python 139, 관리자 19, UI 모형 57)가 통과했습니다. Python/JavaScript 문법 검사와 git diff --check도 통과했습니다. 서버 검증은 실제 임시 SQLite/Lock을 사용하고 Ollama는 Mock입니다. 중간 삭제 및 번호 초기화 직후 SQL 실패 rollback, 최대 60개 삭제·새 Session 1, 새 Python/Flask 프로세스 재시작/개별 Delete의 번호 정책, Chat/History/Reset/개별 Delete 경합과 tombstone 유지를 검사합니다. UI는 취소·HTTP/통신 실패·중복 실행·늦은 응답·마이크/Audio/URL 정리를 모형으로 실행합니다. 네이티브 스크롤바/Edge DOM·음성 실기기 검증과 구분합니다. 사용자 DB에 Delete All을 실행하지 않았습니다.

사용자가 STEP 7 최종 Edge 실기기 검증 7/7 통과를 확인했습니다. 아래 절차는 향후 회귀 검증용으로 보존하며, 필요한 백업 후 **검증용 DB와 세션**에서 수행하세요.

1. 새 터미널에서 별도 DB를 지정하고 서버를 실행합니다. 기존 서버가 같은 포트를 사용 중이면 작업 완료 후 사용자가 정상 종료해야 하며, 이번 작업에서 서버를 종료하지 않았습니다.

```powershell
$env:LOCAL_AI_DB_PATH = Join-Path $env:TEMP ("local-ai-delete-all-" + (Get-Date -Format 'yyyyMMdd_HHmmss') + ".sqlite3")
.\.venv\Scripts\python.exe app.py
```

2. 세션을 충분히 만들고 창 높이를 줄입니다. Session List에서 휠·스크롤바 클릭/드래그 중 목록이 열린 상태를 유지하고 아래 항목에 접근 가능한지 확인합니다. 바깥 클릭·Esc·버튼 재클릭은 닫혀야 합니다.
3. A/B에 질문과 서로 다른 AI Language를 사용합니다. Delete All 확인창의 개수를 확인하고 취소합니다. 목록·대화·D 초안이 유지되어야 하며 Network에 DELETE가 없어야 합니다. 개별 Delete도 기존대로 동작해야 합니다.
4. Delete All 승인 후 목록/E가 비고 자동 세션 생성이 없는지 확인합니다. D 초안은 유지되어야 합니다. Flask를 같은 DB 경로로 정상 재시작한 뒤 첫 New Chat이 새로운 UUID의 Session 1인지 확인합니다. 이후 개별 Delete는 번호를 초기화하면 안 됩니다.
5. 녹음·STT·Thinking·합성·재생 중 각각 Delete All을 확인합니다. 후속 Chat/TTS 또는 늦은 텍스트·History·오디오가 살아나면 안 됩니다. Thinking/Reset 처리 중에는 완료 관찰 후 DELETE가 한 번만 나가야 하며 마이크 표시·재생은 정리되어야 합니다.
6. DB 오류/rollback과 통신 불확실은 자동 모형 테스트로 검증했습니다. 사용 중인 DB 손상·프로세스 강제 종료로 재현하지 마세요. 실제 브라우저 오류 복구는 아직 미검증입니다. 테스트 후 별도 터미널을 종료하면 임시 경로 설정은 다른 터미널에 영향을 주지 않습니다.

남은 제한: 단일 Controller 프로세스, 다른 탭의 상태는 push 동기화하지 않음, 불확실 일괄 삭제의 수동 재동기화 필요, 평문 DB/과거 백업·WAL 데이터, STEP 6-5 연기 및 ISSUE-002 템플릿 후속 과제. 신규 패키지·모델 변경·Git Commit/Push는 없습니다.

## Base Framework v1.0 완료 상태

STEP 5 UI/Session 관리, STEP 6-1~6-4 로컬 STT/TTS·수동/자동 음성 대화, STEP 7 SQLite 저장·Reset/Delete/Delete All·재시작 복원 및 기본 안정성 검증을 완료했습니다. 사용자 최종 Edge 실기기 검증은 7/7 통과이며 최근 자동 테스트는 215개 통과입니다. 검색 기반 장기 기억, MCP·자동 Tool Calling, 모델 선택 및 주변기기 확장은 포함하지 않습니다.
