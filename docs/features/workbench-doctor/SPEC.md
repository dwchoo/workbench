# WB 읽기 전용 진단 — 구현 명세

Revision: s1; source: BRIEF.md b1. 상태: 확인용 초안. 승인 정본은 별도 approval.json과 실제 사용자 답변이다.

## 문제와 해결 방향

사용자는 wb update 개발·검증·커밋·푸시를 마친 뒤 `wb doctor`도 개발하도록 요청했다. 이번 문서는 그 다음 기능의 구체적인 최소 범위를 제안한다. 사용자는 doctor의 검사 항목·출력·자동 복구 여부를 아직 세부 지정하지 않았다. 아래의 읽기 전용·명시적 안내 중심 범위를 묶음 확인 대상으로 제시하며 합의되지 않은 승인 상태를 만들지 않는다.

## 범위와 비목표

Linux, 기존 WB 설치/프로젝트/OMP/DB/backend/update 상태에 대한 로컬 진단으로 제한한다. 자동 수리/--fix·auth 값 조회·외부 업로드·provider 연결/성능 검사·새 dependency·모든 설치 도구의 복구는 제외한다. 실행 중 backend는 기존 검증된 읽기 조회로 관측하고 종료하지 않는다. inactive DB의 검사도 schema를 설치하거나 sidecar를 만들지 않는 read-only 경로여야 한다.

## 공개 계약과 불변 조건

`wb doctor [--data-dir DIR] [--json]`와 기존 alias의 같은 parser. cwd/명시 data-dir는 현재 프로젝트 해석을 따르되 초기화하지 않는다. 설치 관련 검사는 실행 중인 distribution과 실제 entrypoint/interpreter를 기준으로 한다. uv는 설치/출처 진단 대상일 뿐 누락 시 자동 설치하지 않는다. OMP는 기존의 bounded `--version` 검사/선택 metadata를 재사용하되 로그인·SDK 모델 호출·setup은 실행하지 않는다. 외부 실행 결과는 bounded capture/timeout과 정해진 version 형식으로 처리한다.

JSON은 `schema_version:1`, `overall:ok|warning|error`, `checks:[{id,status:ok|warning|error|unknown,summary,hint}]`를 제공한다. 필요 경로·관측 버전·current commit 같은 비밀 아닌 근거만 추가할 수 있다. 필수 점검의 error/unknown은 overall error·exit1, 선택 프로젝트 없음 등 warning만 있으면 exit0이다. 인자 오류는 기존 argparse exit2다. stdout JSON에 진행 로그/대화 prompt를 섞지 않는다.

현재 프로젝트가 없으면 project-specific 항목은 경고/해당 없음으로 표시하고 global 결과는 계속한다. 살아 있는 backend의 identity는 기존 process/UID/start ticks와 endpoint binding을 대조한 뒤 읽는다. 살아 있는 DB의 무결성을 확정할 수 없으면 제한을 표시하며 직접 schema를 수정하지 않는다. 실행 중 업데이트·실패/부분 backup·재개 안내가 있으면 그것을 표시하고 자동 중단/재개/복원하지 않는다.

실제 수정 명령은 사용자가 별도로 선택한다. credential URL·환경변수 값·auth JSON·record 본문·raw 대화를 진단 출력/파일에 넣지 않는다. 기본 진단 파일 저장·원격 전송 surface를 만들지 않는다.

## 현재 구현의 접점

현재 `wb status`는 프로젝트 backend 상태를 읽고 `process_identity`와 UiClient가 peer 결속을 확인한다. `project.py`는 side-effect-free context 해석을 제공한다. `install_recipe.inspect_existing`은 OMP version의 제한된 실행·오류 처리를 제공한다. 현재 CLI에는 doctor가 없고 README는 이를 제공한다고 약속하지 않는다. WU 기능의 실제 공개 계약과 설치 commit은 WU 게시 완료 시 후속 개발의 검증된 기반으로 선택한다.

## 수락 기준

| ID | 동작 |
|---|---|
| WD-AC-01 | `wb doctor [--data-dir DIR] [--json]`가 설치 출처/entrypoint·Python·uv·선택 OMP·프로젝트 context·private 권한·DB/schema·backend/bridge·update/backup 상태를 점검하고 항목별 결과·근거·다음 행동을 제공한다. |
| WD-AC-02 | 읽기 전용 진단이며 프로젝트 초기화·schema/권한 수정·복구·설치/업데이트·프로세스 종료·모델/유료 provider 호출을 하지 않는다. 인증 파일은 존재/권한 등 metadata만 보고 키·token·원문 DB/대화를 출력하지 않는다. |
| WD-AC-03 | 프로젝트가 없는 위치에서도 전역 설치를 진단한다. absent/실행중/권한오류/손상/identity 불일치/확인 불가를 구분하고 미확인을 정상으로 둔갑시키지 않는다. 새 `.wb/`·DB·진단 파일을 자동 생성하지 않는다. |
| WD-AC-04 | 텍스트와 schema_version1 JSON은 같은 진단 의미를 갖는다. 필수 error/unknown이면 실패 exit, 선택 프로젝트 부재 같은 warning만 있으면 성공 exit다. 진단 실패도 제한된 오류로 반환하고 해결 명령은 안내만 한다. |
| WD-AC-05 | update가 검증·커밋·푸시된 뒤 개발한다. 실제 비editable 설치본에서 healthy/missing/corrupt/live 상태와 진단 전후 원본 보존을 확인하고 필수 독립 검증 후 별도 commit/push한다. |

## 검증 결정

공개 seam은 subprocess `wb doctor` 텍스트/JSON·exit와 원본 inventory, 실제 installed CLI다. 독립 기대값은 직접 만든 private fixture의 설치/ref·UID/mode·schema 오류·고정 상태·파일 hash와 명시 check ID다. 생산 formatter로 expected 결과를 계산하지 않는다. healthy/no-project/missing-OMP/corrupt-DB/unreachable-identity/실행중-update의 서로 다른 실패를 대표 사례로 다루고 같은 동작을 형태만 바꿔 늘리지 않는다.

각 테스트 가능한 동작은 RED→최소 GREEN, independent test_designer 평가 후 final Standards/Spec 새 두 context·별도 Security/secret scan과 설치본 검증을 진행한다. 기존 설치/records/OMP 근거는 입력 불변이면 재사용한다. 전체 관련 검사는 최종 결합 후보에서 한 번만 수집한다.

예정 명령: `PYTHONPATH=src python3 -m unittest discover -s tests/backend/maintenance -p 'test_doctor*.py'`; `WB_LIVE_DOCTOR=1 PYTHONPATH=src python3 -m unittest tests.integration.live_workbench_doctor`. 설치본은 기존 private wheel/uv recipe, 실제 OMP version 및 owned DB/backend fixture를 사용한다. 모델/provider/auth 검증은 하지 않는다.

## 통합·게시 순서와 미결정

PLAN.json p1이 ticket dependency/resource/gate supplier의 정본이다. update 전체 완료·게시 이전에는 doctor 제품 코드를 작성하지 않는다. 사용자에게 실제 업데이트·실험 종료를 수행하는 것은 이번 개발용 private 검증과 구분한다. 이번 권한으로 사용자 DB/auth/global tool 환경을 실제 업데이트하지 않는다. source code commit/push는 사용자 요청에 포함된다.

구체 helper 배치·lock 구현·runner 방식은 합의한 공개 동작 안에서 판별한다. source/프로세스/설치 identity·일관된 backup을 확인하지 못하면 보류하며 성공으로 가정하지 않는다. 승인 전 product/test 작성 금지, 필수 runtime 부재는 not_run이다.
