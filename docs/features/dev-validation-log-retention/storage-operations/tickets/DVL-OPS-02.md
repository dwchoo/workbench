# DVL-OPS-02 — 기존 CLI의 용량·보관 정책 안내 보완

목적: 원본과 중요 기록의 실제 용량·정책·정리 대상을 사용자가 구분하게 한다.
의존·resource·기계 수락 정의는 [PLAN](../PLAN.json), 상세 출력 계약은 [SPEC](../SPEC.md)을 따른다.

## 제공할 행동

- `logs raw-status` 기본 출력을 한국어로 정리한다. legacy 실험/terminal의 다른 제한,
  순환 raw/metadata의 별도 용량, 활성화와 신규 source 정책·보호 사유, partial/unknown을 보여준다.
  `--json` 출력의 기존 구조·type·값과 exit code를 보존한다.
- `logs important status` 기본 출력에서 기존 response의 DB UUID·role·물리 크기·합계와 복수 DB
  정리 필요를 보여준다. 250MB 논리 목표/120일의 의미와 archive 수동 정리를 간단히 설명한다.
  물리 크기에서 논리 사용량을 추정하거나 확인 안 된 orphan을 삭제 후보로 안내하지 않는다.
- 기존 readonly port, 실제 정책 한도·보관 기본값을 재사용한다. 새 스캔/transport/schema/
  status mutation/자동 verify/모델 호출/새 TUI 화면은 추가하지 않는다.
- 정확한 사용법과 `--json` 호환 안내를 작성한다. 실제 운영 활성화·삭제 명령은 실행하지 않는다.

## 수락·검증

LSO-AC-01/02/03/07. 실제 CLI parser/main과 임시 응답/저장소로 기본 표시와 JSON 동등,
활성화됐으나 보호된 정책, 미생성/부분 조회, 복수 DB/unknown을 검사한다.
외부 API의 임의 고장 matrix는 추가하지 않는다. test_designer의 독립 시험은 OPS-03에서 한 단위로 수행한다.
기존 down alias와 무관한 dirty 변경을 보존하고 변경과 맞는 최소 CLI 회귀를 재사용한다.

## 쓰기·중단 경계

주 변경 위치는 기존 `src/workbench/backend/cli.py`와 한정된 CLI 시험·후속 사용법이다.
별도 writer의 test와 동시에 편집하지 않는다. raw engine·summary route·권한·활성화 gate는 변경하지 않는다.
기존 응답으로 정확히 표시할 수 없으면 unknown을 유지한다. 새 query/schema/정책 변경이 필요하면
임의 추가하지 말고 해당 미충족 항목과 최소 대안을 Root에 보고한다.
