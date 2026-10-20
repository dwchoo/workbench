# 작업 기록 구현 검증 근거

이 문서는 WR-07의 실제 관측을 기록한다. BRIEF r0.2·SPEC s1·PLAN p1의 승인 bytes와 수락 기준을 변경하지 않는다. 전체 완료 판단과 최종 독립 Standards·Spec·security review는 Root가 결합 후보에서 별도로 수행한다.

## 이관

WR-07은 기존 중단 상태 `wb migrate`의 inventory에 `records.sqlite3`와 rollback journal을 추가했다. 새로운 이관 방식은 만들지 않았다. DB는 원본 파일을 SQLite로 열지 않고 기존 anonymous snapshot → in-memory schema/내용·integrity 검증 → target publication 경로를 사용한다. 기록 header·모든 revision·정확한 append-only schema를 확인하며, nonempty journal과 지원하지 않는 schema는 명시적으로 거절한다.

실제 RED는 `unrecognized migration inventory entry: records.sqlite3`였다. 최소 변경 뒤 새 보존 검사와 기존 활성 owner·대상 충돌·미분류 파일·기존 history의 선별 검사 5개가 통과했다. 원본 inventory의 bytes·mode·mtime을 대조했고, target의 최신/과거 revision을 공개 Store와 실제 CLI에서 재조회했다. Task DB·기존 raw log도 보존했다.

원본 증거: `.worktrees/wr-20261020-p1-wr04/.workflow/workbench-records/I025/01-migrate-{red,green}.log`, `02-migrate-targeted.log`.

## 실제 WB와 설치본

기존 InstalledRun recipe로 현재 후보를 offline local wheel로 만들고 private noneditable tool 환경에 설치한다. checkout 밖 임시 프로젝트에서 지정 OMP 18.11.1과 localhost scripted provider만 사용한다. 설치된 Python module·bridge·records tool/context·contract·launcher·migration bytes를 현재 source와 대조한다. `build_support/assets.py`의 기존 recursive 수집이 새 asset을 포함하므로 packaging 코드는 변경하지 않았다.

47.483초의 단일 실제 실행이 통과했다. 필수 관측은 Manager create → Worker show/revise → backend/OMP 종료·재시작 → 새 provider context의 최근 미리보기·본문 조회 → literal search → 지정 revision Markdown export → 중단 후 CLI 재조회다. 두 번째 프로젝트의 반대 역할 저장과 첫 프로젝트 ID의 `not_found`로 분리를 확인한다. 일반 메모 요청이 프로젝트 `docs/` 파일을 만들지 않는지도 관측한다.

자동 요약 보존은 같은 설치본의 `store_summary`가 synthetic 요청 결과를 실제 중요 로그 DB에 저장하고, 중요 로그 maintenance를 실행한 뒤 기록 DB의 전체 hash와 최초/최신 snapshot을 대조한다. 이는 실제 유료 모델의 요약 품질 시험이 아니다. raw rotation은 활성화하지 않는다.

WR-06의 startup·first-input·switch·stale-result·reconnect·pause에 대한 실제 OMP 원본 근거는 `.worktrees/wr-20261020-p1-wr04/.workflow/workbench-records/I022/native-1792485052909186051`을 재사용한다. 이번에는 전체 WB·설치본 흐름만 추가하며 그 control을 반복하지 않는다.

원본 증거는 `.worktrees/wr-20261020-p1-wr04/.workflow/workbench-records/I025/installed-1792486223939633930/`의 wheel·installed identities·provider JSON·UI bytes·명령 stdout/stderr·DB·receipt다. 재시작 후 실제 UI bytes에서도 기록 미리보기 문구를 확인했다. 관측한 18개 process identity가 모두 ended이고, 잔여 소유 process는 없으며 private runtime은 제거했다. 최종 실행 결과와 cleanup·남은 한계는 `result-I025.json`에 정리한다.

## 근거의 한계

localhost scripted provider는 실제 OMP의 도구 roster·실행·context 전달을 관측하지만 자연어 의도 분류나 모델 품질을 입증하지 않는다. Linux의 현재 지정 OMP·현재 local build를 확인하며 모든 provider·OS·배포 버전의 동작을 보장하지 않는다. 사용자 DB·인증·실행 세션은 사용하지 않는다. 보존 근거를 남긴 뒤 정확한 PID/starttick으로 소유 process 종료를 확인하고 private runtime/cache를 정리한다.

완료된 저장·검색·export·도구·문제 보고서·skills·installation 전체 suite는 이 작업에서 반복하지 않는다. Root가 최종 결합 후보에서 한 번 수집할 검사·독립 review와 구분한다.
