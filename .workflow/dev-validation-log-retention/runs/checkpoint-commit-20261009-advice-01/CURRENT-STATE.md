# 개발 현황 checkpoint — 승인된 계획의 부분 구현

기록 기준: 2026-10-09. 이 기록은 현재 상태를 보존하는 WIP commit용이다. 제품 완료·새 경로 채택·raw 삭제 활성화를 승인하지 않는다.

## 권한과 상태

- 승인 기준: BRIEF r0.9 / SPEC s5 / PLAN p5, bundle `78bc55919e57bbf2132d6653780c5c164a2ee1d618422e5232d4cf0d1d667c3e`.
- 계획 승인과 기존 `$implement` 권한은 유지된다. 이번 사용자 요청은 현재 상태 commit + 기존 claude-wb의 읽기 전용 조언이다. 이 턴에 구현을 재개하거나 후보를 통합하지 않는다.
- 실제 raw rotation 활성화, 기존 로그 삭제/이관, 사용자 DB 전환, 유료 모델·VM 시험, 새 agent 정의/상주 OMP, 게시·배포는 수행하지 않았다.
- 현재 live writer/test OMP/provider 없음. 기존 네 leaf 작업 종료·lease 해제. Ledger revision236, used51 / reserved4 / limit96; 미래 예약 네 건은 미실행이다.
- Root의 commit 직전 구현 후보 기록: `44b0027c9d42418cd6f165d97e16b6175ed5fa1ec2cfadb3f4ca6c5e7c573ace`. Commit 이후 HEAD/index가 달라지므로 이 후보의 과거 통과를 새 전체 candidate의 통과로 표시하지 않는다.

## 통합된 것 / 통합되지 않은 것

| 범위 | 현재 위치와 관측 | 남은 작업 |
| --- | --- | --- |
| DVL-03 | Root: log 계약, metadata/request/proof/Task 저장 경계, 중요 SQLite 기반. 관련97 tests pass, 별도 fixture31 tests pass | 전체 기능·runtime46 gates 통과는 아님 |
| DVL-08 판정기 | Root: 독립 검토·동결된 validator/recheck/policy 및 fixture | 실제 collector는 아직 별도 후보; 8개 runtime 전체 pass 아님 |
| DVL-01 fast | 별도 workspace. 실제 OMP scripted fast81 tests /26.401s; 독립·관련33 tests 및 마지막cleanup회귀 통과, 마지막 리뷰 material 없음 | Root 통합, 통합 후보 재검증; precise는 미구현 |
| DVL-04/05 | 별도 coherent 후보. 조건 근거 streaming + 승인/종결 사실의 중요DB 복사. 원자성 결함 수정 후26 tests pass | 독립 suite32 중1 fail. seq99 setup의 타당성 독립 판단·수정, 실제 cursor-fault regression 및 fresh review 필요 |
| DVL-08 runtime | OMP18.8.6 기존 explorer native executor. guard 누락·무효 hook 각 경우 loopback provider POST1 | 시험한 경로의 필수 zero-send 조건 미충족. 다른 모든 공개 경로의 불가능을 입증한 것은 아님 |
| DVL-02/06/07/09/10/11/12/13/14 | 아직 완성·통합 아님 | 아래 의존성 및 route 검증 결과에 따라 진행 |

자동 요약과 무관한 검증 runner·조건 근거·비활성 raw engine·DB 비요약 부분은 계속할 수 있다. 프로젝트 전체가 기술적으로 막힌 것은 아니다.

## 독립 후보를 복원하는 방법과 한계

이 디렉터리의 `dvl01.patch`, `dvl045.patch`, `dvl08-runtime.patch`는 현재 Root 파일에서 각 별도 후보의 scoped 파일로 가는 patch이다. `candidate-patches.json`에 파일별 base/candidate SHA-256와 mode를 기록했다. Git checkpoint checkout을 기준으로 각각 `git apply --check <patch>`를 먼저 수행한다. 자동 적용·통합을 하지 않았으며 적용은 별도의 구현 작업이다. Patch만으로 과거의 전체 candidate digest를 재현하거나 과거 tests를 새 candidate에 전가하지 않는다.

- DVL-01: `/tmp/workbench-dvl-20261009-3l1ViH/dvl01`.
- DVL-04/05: `/tmp/workbench-dvl-20261009-3l1ViH/dvl05`.
- DVL-08 collector: `/tmp/workbench-dvl-20261009-3l1ViH/dvl08`.
- 다른 파일·전용 환경·대형 복구 snapshot은 기존 위치에 그대로 남겨 두었다. 삭제하지 않았다.
- generated graph 약229MB와 대형 .workflow 복구 자료 약5.7GB는 Git commit에서 제외했다. 이 commit은 그 generated/cache bytes의 전체 백업이 아니다.

## 시험 기록

- 기존 첫 실패와 수정/재시험 chain, assignments, 결과·리뷰·scope records는 원래 의미 그대로 보존한다.
- private tmpfs에서 같은22 SQLite fixture tests80.967s→0.2s 관측은 시험 환경 최적화다. 제품의 WAL/FULL durability나 실제 디스크 수락 조건을 낮춘 결과가 아니다.
- fast26.401s는 현재 host의 fast subset 관측이다. precise10분, 실제60초 점검·65초 detach matrix, 실모델 비용/확신 시험은 아직 확인하지 않았다.
- 실제 모델 context/output budget/thinking/tier, 공유 경로 최대 차단 시간은 관측 전이다. 이들에 관한 조건부 사용자 결정은 아직 새 값을 고를 단계가 아니다.

## route 결정 이전에 필요한 조언

Root는 기존 agent 정의를 재사용하는 격리 일회성 OMP summary 실행의 최소 실험 허용을 추천했으나, 이 대안이 guard 문제를 해결한다는 근거는 없다. 사용자는 불충분한 설명을 지적하고 Claude의 조언을 먼저 요청했다. 읽기 전용 의뢰 내용은 `CLAUDE-ADVICE.md`에 있다. Claude의 조언을 받기 전 이 추천을 확정하거나 제품 경로를 채택하지 않는다.
