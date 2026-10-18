# WB 문제 리포팅 skill — 구현 명세

Revision: s1; source brief: [BRIEF.md r1.0](BRIEF.md).
상태: 구현 묶음 승인 대기. 2026-10-18 사용자의 `$to-tickets docs/features/workbench-problem-report/BRIEF.md`는
해당 BRIEF를 입력으로 하는 계획 작성 권한이다. 구현 권한은 아니며 [승인 기록](approval.json)을 따른다.
BRIEF의 문구와 digest는 계획 과정에서 변경하지 않는다.

## 문제와 해결

WB 사용자가 겪은 오류·불편을 Manager의 명시적 slash 호출로 조사·정리하고,
Markdown 하나를 별도 디버깅 환경에 옮겨 포함된 근거를 읽을 수 있게 한다.
기존 상태·로그의 읽기 인터페이스, 영어 skill/reference, 작은 Python 출력 helper를 결합한다.
사용자 기대·관측 사실·원인 가설·미확인을 구분하며 원인 확정과 버그 수정 성공을 전제하지 않는다.
용어는 [CONTEXT.md](../../../CONTEXT.md)의 WB 문제 보고서를 따른다.

## 관련 사용자 이야기

- 사용자는 `/skill:workbench-report 화면 resize 뒤 입력 위치가 달라졌다`처럼 호출하고,
  이미 설명한 내용을 반복하지 않으면서 필요한 질문에만 답한다.
- 로그 일부가 없거나 원인을 몰라도 열 가지 항목을 갖춘 보고서를 받는다.
- 보고서를 받은 개발자는 원래 data dir 없이 포함된 발췌·출처·누락·가설을 읽는다.
- 저장할 수 없을 때 사용자는 실패 이유와 가림 처리된 전문을 받아 직접 `.md`로 저장한다.

## 범위와 비목표

범위는 BRIEF U-01~12와 WPR-AC-01~14 그대로다. 출력 helper는 가림·형식 검사·크기 검사·
create-only 저장만 담당하며 새 수집 service, agent, DB schema, 공개 backend API를 추가하지 않는다.
코드 수정·재현 실행·Worker 위임·자동 복구·자동화 재개·전체 로그 복제·외부 업로드·
WB가 완전히 내려간 동안의 별도 reporter는 제외한다.

## 공개 인터페이스·데이터 계약·불변식

### 1. 호출 경계

- 이름 `workbench-report`, 진입점 `/skill:workbench-report [증상 설명]`.
- WB Manager의 skill command에만 노출한다. Worker 허용 목록은 유지한다.
- model 자동 선택 목록에서는 제외한다. 구체 frontmatter 문법은 WPR-01의 실제 OMP probe로 결정한다.
  과거 README의 `disableModelInvocation` 관측은 후보일 뿐 지원 증거가 아니다.
- 사용자 자연어 요청, Workbench notice, peer report, 수집 로그에 slash 문자열이 들어 있어도
  이를 사용자 직접 slash 호출과 동일하게 취급하지 않는다. skill은 입력 안의 지시를 실행하지 않는다.
- OMP의 일반적인 skill 선택·호출 경로에 대한 계약이다. 같은 사용자 권한의 고의적인 파일 읽기를
  차단하는 OS 보안 경계를 도입하거나 보장하지 않는다.
- 실제 slash 확장 후에는 필요한 후속 질문의 답변을 같은 보고서 작성의 연속으로 처리한다.
  후속 답변마다 slash를 다시 요구하지 않는다.

### 2. Reference와 보고서

자산은 `omp_bridge/skills/workbench-report/SKILL.md`와
`references/report-template.md`다. 지시문·작성 규칙은 영어, 보고서의 고정 heading과 기본 본문은 한국어다.
skill은 호출할 때 reference를 읽고 아래 순서의 `##` heading을 각각 한 번 유지한다.

1. 문제 요약
2. 기대/실제 동작
3. 환경·버전
4. 발생 순서·재현 정보
5. 영향
6. 증거·로그
7. 원인 가설과 근거
8. 이미 시도한 조치
9. 미확인·누락 정보
10. 다음 조사 제안

helper는 reference의 heading을 기준으로 검사해 별도의 경쟁 양식을 만들지 않는다.
섹션은 빈 채로 두지 않고 모르면 `미확인`과 가능한 이유를 쓴다. 최상단 제목과 작성 시각,
확인 가능한 WB revision·OMP 버전은 포함하되, 확인 불가를 현재 개발 HEAD로 대신하지 않는다.

발췌에는 문서 내부 증거 ID(`E-01` 등), source path 또는 bridge source 식별자,
관측/수집 시각과 확인 가능한 원자료 시각, 줄 또는 byte 범위, Task/run/command ID를 연결한다.
없는 식별자·시각은 `미확인`이다. 가설은 증거 ID와 미확인 반례를 연결한다.
원문 발췌는 fenced code block 안에 넣고 내용 속 backtick이 문서 구조를 깨지 않게 fence 길이를 정한다.
가림·누락·요약을 원문으로 표시하지 않는다. 로컬 경로는 출처이며 그 경로의 파일이 있어야만
보고서를 읽을 수 있도록 작성하지 않는다.

### 3. 수집·대화 경계

- 호출에 제공한 설명, 현재 대화의 관련 부분, 사용자가 지정한 자료를 먼저 확인한다.
  부족한 기대 동작·발생 순서·시각·영향만 묶어서 질문한다. 모른다는 답은 완료를 막지 않는다.
- 현재 WB 프로젝트는 OMP를 시작한 project cwd와 확인된 세션 맥락을 사용한다.
  host terminal에서 사용자가 `cd`한 경로와 혼동하지 않는다. 불명확하면 경로를 묻는다.
- `workbench_status`로 관련 Task·command·Worker·backend 상태를 얻고,
  `workbench_logs`의 `show`, `raw`, 필요 시 `status`·`raw-status`를 사용한다.
  관련 ID를 찾는 제한된 `list` 외에 프로젝트 전체 작업·대화를 훑지 않는다.
- `segmented-v1` descriptor의 `log_path`를 원문으로 읽었다고 주장하지 않는다.
  `raw`의 source-relative offset·length와 missing/evicted 범위를 유지한다.
- backend/UI 문제이면 확인된 data dir의 관련 backend log 발췌와 관련 코드를 읽을 수 있다.
  `WORKBENCH_G3_BRIDGE_SOCKET`의 부모 경로는 현재 연결의 data dir 후보로 확인할 수 있다.
  다른 사용자의 디렉터리나 임의 기본 경로를 현재 세션 자료로 추정하지 않는다.
- auth store, token 파일, 전체 environment, 무관한 사용자 대화는 수집하지 않는다.
  버전 확인을 위해 WB를 시작하거나 OMP 모델을 새로 호출하지 않는다.
- 기존 log reader의 lease·snapshot·임시 복사 처리까지 유지한다. 직접 SQLite 접속·migration·
  checkpoint·maintenance·원문 rotation을 추가하지 않는다. 조회의 논리적 읽기 계약과
  SQLite sidecar까지 byte 불변이라는 주장은 구분한다.

### 4. Python 출력 helper

위치: `omp_bridge/skills/workbench-report/scripts/report_io.py`. Python 표준 라이브러리만 사용하며
제품 패키지 import, 모델 호출, network, subprocess, DB 접근, 자동 파일 탐색을 하지 않는다.

계획에서 고정한 인터페이스:

```text
python3 <skill-dir>/scripts/report_io.py --project-dir <absolute-path> [--output <path>]
stdin: reference에 따라 작성한 UTF-8 Markdown
stdout: JSON 결과 1개
```

- `--project-dir`은 확인된 WB 프로젝트다. `--output` 상대 경로는 그 프로젝트 기준,
  절대 경로는 사용자 지정 목적지다. 둘 다 shell code로 재해석하지 않는다.
- 출력 미지정 시 `<project>/bug-reports/wb-<UTC timestamp>-<random-id>.md`.
  사용자 경로가 디렉터리이면 같은 규칙의 파일을 그 안에 만든다.
- 파일명 지정 경로가 이미 있으면 기존 파일을 건드리지 않고 같은 디렉터리의 고유 sibling 이름으로
  저장해 실제 경로를 알린다. 파일·symlink·동시 생성 충돌 모두 create-only로 처리한다.
- 최종 목적지 파일은 `0600`, 새로 만드는 디렉터리는 `0700`을 사용한다.
  기존 디렉터리 권한을 바꾸지 않는다. 기존 파일·symlink를 따라 덮어쓰지 않는다.
- 가림과 heading·크기 검사를 완료한 Markdown만 저장한다. raw 초안 임시 파일은 만들지 않는다.
  부분 쓰기·close 실패 시 소유한 미완성 결과만 정리하고 `saved`라고 보고하지 않는다.
- `saved`: `path`, `utf8_bytes`, `redaction_count`를 반환하고 exit 0.
- `not_saved`: 안전한 `reason`, 가림 처리된 `markdown` 전문을 반환하고 exit 1.
  사용자 경로 문자열·OS 오류에 민감정보가 섞일 수 있으므로 오류 출력도 가림 처리한다.
- `invalid_draft`: heading 누락·순서·빈 섹션·크기 등 이유와 가림 처리된 초안을 반환하고 exit 2.
  저장하지 않는다. skill은 새 조사를 시작하지 않고 형식을 고치거나 발췌를 줄여 다시 준비한다.
- stdout/stderr·예외 메시지에 가리기 전 본문을 출력하지 않는다. 구체 내부 함수명은 구현자가 정하되
  같은 준비 결과를 저장·저장 실패 fallback에 사용한다.

### 5. 가림과 크기

이 수치는 BRIEF U-12가 계획 단계에 맡긴 기술 기본값이며 사용자 비용·call 상한이 아니다.

- 관련 원문 발췌 1개 기본 최대 4 KiB, 전체 원문 합계 기본 최대 16 KiB.
- 완성 Markdown 최대 32 KiB UTF-8. helper 입력의 안전 상한은 1 MiB이며 초과 입력은 저장하지 않는다.
  입력 자체가 비정상적으로 크면 전문을 반환하지 않고 크기 오류를 알려, skill이 제한에 맞춘
  보고서를 준비한 뒤에만 최종 결과를 제공한다. 이 거부를 보고서 완성으로 세지 않는다.
- 긴 자료는 문제와 관련된 구간을 고른다. UTF-8 문자·fence·heading을 byte 잘림으로 파괴하지 않는다.
  제한으로 빼거나 줄인 구간·이유를 `미확인·누락 정보`에 기록한다. 최종 크기 초과는 조용히 자르지 않는다.
- skill이 먼저 민감정보를 제외·가리고, helper가 API key/token/password 할당,
  Authorization Bearer, credential이 든 URL, private-key block 등 지원 패턴을 추가 가림한다.
  이름 없는 모든 임의 비밀을 탐지한다고 주장하지 않는다. 민감 여부가 불명확한 발췌는
  제외하고 이유를 기록한다. 알려진 secret fixture는 본문·출처·오류·fallback 어디에도 남지 않아야 한다.
- 가림 표시는 `[REDACTED]`를 기본으로 하고 가림 사실과 필요한 범주를 남긴다.
  report preparation과 fallback 모두 같은 가림 결과를 사용한다.

## 상태 전이·실패·경계 동작

`명시적 호출 → reference/기존 자료 확인 → 필요한 질문 → 읽기 조사 → 초안/가림 → 검사 → 저장 → 경로/요약`
순서다. 질문에 대한 응답은 동일 문서의 작성 과정이며 새 자동화 권한을 만들지 않는다.

| 상황 | 사용자에게 보이는 결과 |
|---|---|
| 원인 미확정·증거 부족 | 원인 가설/미확인을 구분한 보고서 완성 |
| bridge·raw·DB 조회 일부 실패 | 접근 실패·이유·범위를 기록하고 남은 자료로 작성 |
| 로그 eviction·저장 정지·발췌 제한 | 구간과 출처, 파악 가능한 이유를 표시 |
| 사용자 지정 파일이 이미 있음 | 원본 보존, 새 고유 경로에 저장한 사실 안내 |
| 권한·디스크·디렉터리 오류 | `not_saved`, 이유와 가림 처리된 전문 제공; 저장 성공 주장 금지 |
| WB와 무관한 프로젝트 실패 | 대상 범위를 안내; WB 관련성이 확인된 경우에만 이 skill로 조사 |
| Manager 자체 사용 불가 | 별도 reporter를 시작하지 않음; 이번 지원 경계로 안내 |
| 단순 증상 대화·자동 장애 알림 | 이 skill을 자동 실행하지 않음 |

## 구현 결정과 근거

- **사용자 합의:** 공개 동작·양식·scope·fallback은 BRIEF U-01~12를 그대로 따른다.
- **계획 상세화:** 한 개 reference, 작은 Python 출력 helper, JSON 결과·create-only 저장·
  가림 기본 패턴·크기 수치는 이를 반복 가능하게 구현하기 위한 선택이다.
- **기존 구조 유지:** Manager role filter와 startup isolation 검사에 연결한다. Worker tool/agent
  권한, 다른 skill, 모델 routing, 중요 로그 정책·DB schema는 바꾸지 않는다.
- [ADR-0001](../../adr/0001-no-external-multiplexer.md),
  [ADR-0002](../../adr/0002-managed-shell-control-wait.md)를 유지한다.
  진단을 이유로 host terminal 제어·실험 실행·multiplexer 의존을 추가하지 않는다.
- 현재 프로젝트 cwd는 `service.py`의 `OmpPane(... cwd=self.project_dir)`에서 전달된다.
  `launcher.py`는 role filter와 model prompt/skill command 관측 경로를 제공한다.
  `bridge.ts`의 `workbench_logs`는 두 역할에 등록되며 raw page 최대 128 KiB의 기존 인터페이스다.
  본 feature는 더 작은 발췌를 요청하고 새 bridge tool을 만들지 않는다.
- 관측 snapshot: HEAD `f0ddd20aba0d6031e00381f93e9d4a5d38995e9d`, 설치 OMP 18.11.1,
  BRIEF r1.0의 기존 증거와 2026-10-18 정적 파일 확인. 실제 호출 probe는 아직 미실행이다.

## 수락 추적

PLAN의 `gate_items`가 gate owner·supplier·evidence level의 정본이다. 아래 표는 동작 추적이다.

| AC | 주 구현 ticket | 관측 인터페이스 | 최종 통합 확인 |
|---|---|---|---|
| WPR-AC-01 | WPR-04 | Manager slash 노출·확장과 저장 결과 | G-08 |
| WPR-AC-02 | WPR-02,03 | helper 출력 Markdown을 다른 디렉터리에서 읽음 | G-08 |
| WPR-AC-03 | WPR-04 | model discovery 제외·Worker 미노출·직접 호출만 확장 | G-01,08 |
| WPR-AC-04 | WPR-03 | 매 호출 reference 읽기와 열 가지 항목 | G-07,08 |
| WPR-AC-05 | WPR-03 | WB 증상/관련 실험/무관한 실험 fixture 응답 | G-07,08 |
| WPR-AC-06 | WPR-03 | 도구 trace·관측/가설/미확인 표시 | G-07,08 |
| WPR-AC-07 | WPR-03 | source/시각/범위가 있는 inline 증거 | G-07,08 |
| WPR-AC-08 | WPR-02,03 | 본문·오류·fallback secret canary 부재, 수집 trace | G-03,07,08 |
| WPR-AC-09 | WPR-03 | 부분 자료·원인 미확정에서도 열 가지 heading 유지 | G-02,07,08 |
| WPR-AC-10 | WPR-03 | 이미 준 정보 재질문 없이 부족한 질문 묶음 | G-07,08 |
| WPR-AC-11 | WPR-02,04 | 기본/지정 경로와 동시 충돌 create-only 저장 | G-04,08 |
| WPR-AC-12 | WPR-02,03 | 저장 실패 JSON과 실제 사용자 fallback 전문 | G-04,07,08 |
| WPR-AC-13 | WPR-03 | bridge/raw unavailable·부분 자료 보고서 | G-07,08 |
| WPR-AC-14 | WPR-02,03 | 크기 초과 거부·표시된 발췌 누락·재준비 | G-02,07,08 |

## 검증 결정

현 단계에서는 제품/test 코드를 작성·실행하지 않는다. 다음 명령은 구현 후 계획된 검증이다.

- WPR-01: `WB_LIVE_OMP=1 PYTHONPATH=src:. python3 -m unittest tests.backend.workbench_report.invocation_probe`.
  실제 OMP + 소유한 임시 HOME/project + localhost scripted provider에서 canary skill의
  command 노출·model prompt 제외·명시적 slash 확장을 관측한다. 유료 모델·사용자 auth는 사용하지 않는다.
- 로컬: `PYTHONPATH=src:. python3 -m unittest discover -s tests/backend/workbench_report -p 'test_*.py'`.
- 회귀: `PYTHONPATH=src:. python3 -m unittest tests.backend.test_paths_launcher tests.backend.test_omp_isolation tests.backend.test_workbench_skills_independent_p27cd69c`.
  실제 발견되는 관련 roster assertion도 영향 분석해 갱신·실행한다.
- WPR-05: `WB_LIVE_OMP=1 PYTHONPATH=src:. python3 -m unittest tests.integration.live_workbench_report`.
  실제 OMP/bridge/helper 연결과 임시 자료를 사용한다. scripted provider의 고정 출력은
  skill의 실제 추론·질문 품질 증거로 세지 않는다.
- 별도 `skill_behavior` gate는 독립 test_designer가 frozen skill/reference와 synthetic 증거로
  실제 host agent 실행을 관측하고, 질문·사실/가설·누락·무관한 작업 거절·금지 도구 trace를 확인한다.
  환경이 이 실행을 제공하지 못하면 `not_run`이다. 문자열 존재 검사나 scripted 응답으로 대체하지 않는다.
- 독립 test_designer는 요구사항에서 기대를 먼저 도출한다. fresh reviewer 검토와 별도 security
  reviewer 검토(경로·파일 생성·secret·stdin parsing)를 포함한다. 마지막 Root가 결합 후보를 검증한다.

기존 참고 seam은 `tests/backend/live_omp_isolation_probe.py`, `launcher.check_isolation`,
`observe_isolation`, `test_paths_launcher.py`다. 새 테스트 파일은 위 명령의 구현 예정 경로이며
현재 존재하거나 통과한 것으로 취급하지 않는다.

## 호환성·통합·운영 제약

- Linux, Python >=3.12, 기존 WB source-layout 설치를 기준으로 한다. 새 의존성·lockfile 변경은 없다.
- 먼저 현재 OMP 18.11.1에서 확인한다. 다른 버전은 실제 시험 전 지원 완료로 표시하지 않는다.
- `.workflow/`·graphify 생성물과 기존 사용자 변경을 source corpus/checkpoint에서 제외한다.
  새 격리 실행 전 용량을 확인하고 10 GiB 경계를 따른다. 계획 시작 관측은 `.workflow` 9.7G,
  `graphify-out` 509M이며 이번 계획은 격리 실행을 만들지 않는다.
- 한 물리 workspace에 한 writer. WPR-02/03만 WPR-01 통과 후 격리된 workspace에서 병렬 가능하다.
  Root 통합은 직렬이며 경로·service·fixture 충돌은 dispatch 직전 재확인한다.
- commit·배포·게시·실제 사용자 로그 시험·사용자 DB 변경·유료 모델 CLI 호출은 이 묶음의 자동 실행 대상이 아니다.
  테스트는 synthetic 자료와 소유 localhost provider로 수행한다.

## 미해결 차단 조건과 위임한 기술 선택

사용자 결정 누락은 현재 없다. **실행 전제 미확인**은 사용자 전용 호출의 실제 OMP 지원이다.
WPR-01의 최소 probe는 임시 canary skill 하나로 (a) Manager slash command 존재,
(b) model discovery 부재, (c) Worker 미노출, (d) 명시적 slash 확장과 사용자 인수 전달,
(e) 일반 입력·notice/peer 입력이 slash 실행으로 바뀌지 않음을 관측한다.

통과하면 측정한 문법으로 WPR-02/03을 연다. 실패·불명확이면 probe 자체의 정상 여부를 먼저
분류하고 해당 확장을 보류한다. 공개 OMP 기능으로 요구를 충족할 수 없다면 근거와 대안을
사용자에게 제시한다. OMP fork·숨은 agent·자동 호출 허용·진입점 변경을 임의로 구현하지 않는다.

정확한 정규식·random ID 길이·test fixture 세부·helper 내부 함수명은 위 계약 안에서 구현자가 정한다.
단순 skill 지시문으로 사용자 전용 호출이 검증되었다고 표시하는 선택은 허용하지 않는다.

## Ticket graph

[PLAN.json](PLAN.json)이 의존성·resource·write ownership 정본이다.
초기 frontier는 WPR-01 하나다. probe 통과 후 WPR-02와 WPR-03이 병렬 후보이며,
WPR-04가 연결하고 WPR-05가 최종 결합 검증을 담당한다. 구체 readiness는 implement가 재검증한다.
