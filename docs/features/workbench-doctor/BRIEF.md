# WB 읽기 전용 진단

Revision: b1. 상태: 확인용 초안. 사용자 의도와 아래 구체 계획 묶음의 승인을 구분한다.

## 사용자 요청과 목표

사용자는 wb update 개발·검증·커밋·푸시를 마친 뒤 `wb doctor`도 개발하도록 요청했다. 이번 문서는 그 다음 기능의 구체적인 최소 범위를 제안한다. 사용자는 doctor의 검사 항목·출력·자동 복구 여부를 아직 세부 지정하지 않았다. 아래의 읽기 전용·명시적 안내 중심 범위를 묶음 확인 대상으로 제시하며 합의되지 않은 승인 상태를 만들지 않는다.

## 범위와 비목표

Linux, 기존 WB 설치/프로젝트/OMP/DB/backend/update 상태에 대한 로컬 진단으로 제한한다. 자동 수리/--fix·auth 값 조회·외부 업로드·provider 연결/성능 검사·새 dependency·모든 설치 도구의 복구는 제외한다. 실행 중 backend는 기존 검증된 읽기 조회로 관측하고 종료하지 않는다. inactive DB의 검사도 schema를 설치하거나 sidecar를 만들지 않는 read-only 경로여야 한다.

## 수락 기준

| ID | 관측할 동작 |
|---|---|
| WD-AC-01 | `wb doctor [--data-dir DIR] [--json]`가 설치 출처/entrypoint·Python·uv·선택 OMP·프로젝트 context·private 권한·DB/schema·backend/bridge·update/backup 상태를 점검하고 항목별 결과·근거·다음 행동을 제공한다. |
| WD-AC-02 | 읽기 전용 진단이며 프로젝트 초기화·schema/권한 수정·복구·설치/업데이트·프로세스 종료·모델/유료 provider 호출을 하지 않는다. 인증 파일은 존재/권한 등 metadata만 보고 키·token·원문 DB/대화를 출력하지 않는다. |
| WD-AC-03 | 프로젝트가 없는 위치에서도 전역 설치를 진단한다. absent/실행중/권한오류/손상/identity 불일치/확인 불가를 구분하고 미확인을 정상으로 둔갑시키지 않는다. 새 `.wb/`·DB·진단 파일을 자동 생성하지 않는다. |
| WD-AC-04 | 텍스트와 schema_version1 JSON은 같은 진단 의미를 갖는다. 필수 error/unknown이면 실패 exit, 선택 프로젝트 부재 같은 warning만 있으면 성공 exit다. 진단 실패도 제한된 오류로 반환하고 해결 명령은 안내만 한다. |
| WD-AC-05 | update가 검증·커밋·푸시된 뒤 개발한다. 실제 비editable 설치본에서 healthy/missing/corrupt/live 상태와 진단 전후 원본 보존을 확인하고 필수 독립 검증 후 별도 commit/push한다. |

## 관측 사실

조사 HEAD `a6d877fa3d38f04d816c2f105f131a9be93fdac1`, 2026-10-20. 기존 미커밋 skill/config/AGENTS/CONTEXT 변경은 보존한다. 실제 사용자 backend·DB·auth·설치는 조작하지 않았다.

현재 `wb status`는 프로젝트 backend 상태를 읽고 `process_identity`와 UiClient가 peer 결속을 확인한다. `project.py`는 side-effect-free context 해석을 제공한다. `install_recipe.inspect_existing`은 OMP version의 제한된 실행·오류 처리를 제공한다. 현재 CLI에는 doctor가 없고 README는 이를 제공한다고 약속하지 않는다. WU 기능의 실제 공개 계약과 설치 commit은 WU 게시 완료 시 후속 개발의 검증된 기반으로 선택한다.

## 권한과 미결정

사용자는 개발·검증·commit/push 및 update→doctor 순서를 요청했다. 제품 코드/테스트 작성은 시작하지 않았다. 이 BRIEF와 SPEC·PLAN·tickets·검증 seam의 exact bundle은 아직 제시 전이므로 승인으로 표시하지 않는다. SPEC의 구체화가 합의된 의도와 맞는지 한 번의 묶음 확인이 필요하다. 모델/설정 변경은 하지 않는다.
