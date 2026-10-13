# DVL-FOLLOWUP-02 — 검증용 OMP 내부 daemon 종료 확인

상태: 검증용 소유 종료 경계의 구현·부분 검증을 마쳤다. 전체 DVL-14는 미완료다. 필수 gate 실패를 비차단 backlog로 옮기는 문서가 아니며, 새 정책·기능·수치의 구현 승인이 아니다.

## 확인된 상황

제품의 부분 priority 검증은 실제 Core QUESTION·TUI check/end·요약 저장을 관측했다. 그러나 명시적으로 등록한 process group이 종료된 뒤에도 OMP 내부 text-predict의 종료 기록 writer가 임시 root를 다시 만들었다. [이전 실패](../../../../.workflow/dev-validation-log-retention/runs/dvl14-20261014/priority-core01/root-native03/late-cleanup-disposition.json)와 [수정본의 새 실행](../../../../.workflow/dev-validation-log-retention/runs/dvl14-20261014/priority-core01/root-native04/result.json)을 보존한다.

수정본은 소유 root 내부 registry의 metadata만 읽는다. 미종료·해석 불명은 cleanup false와 임시 경로 보존으로 끝내며, 삭제 직후 root 재생성도 실패로 표시한다. Registry JSON 관측은 전체 native I/O나 모든 process의 부재를 증명하지 않는다. 전체 결과는 `inconclusive`다.

## 반영한 최소 수정

소유 검증용 extension에서 공개 `ctx.shutdown()`을 호출한다. Host는 두 OMP에 같은 종료 요청 시각과 5초 상한을 적용하고, parent 종료 뒤 관측한 소유 registry의 `exited`를 확인한다. 조건이 충족되면 즉시 반환한다. 5초는 실제 3초 실험에서 요청 후 약 3.56초에 종료 기록이 남은 관측에 따른 검증기 비용 상한이며 제품 latency·caps는 바꾸지 않는다. 시간 초과·읽기 불명·fallback은 실패로 남겨 경로를 보존한다. 기존 삭제 전 metadata 검사와 삭제 직후 root 재생성 검사도 유지한다.

[현재 runtime-only 검증](../../../../.workflow/dev-validation-log-retention/runs/dvl14-20261014/graceful-exit01/root-native01/result.json)에서 두 parent RC0·fallback 0·관측 registry terminal, 요약·receipt 각 1건, 등록한 group·thread·socket 잔여물 0과 임시 root 삭제를 확인했다. 공통 종료 요청부터 확인까지 약 3.52초였다. [독립 시험](../../../../.workflow/dev-validation-log-retention/runs/dvl14-20261014/priority-core01/graceful-exit01/test01/result.json)은 Python 11개·Node 4개가 통과했다. 초기 실패 기록은 새 결과로 덮어쓰지 않는다.

Registry JSON은 관측한 파일의 종료 상태만 증명한다. Metadata가 없는 디렉터리·미관측 writer·전체 native I/O·모든 process의 부재는 `unknown`이다. 명시 launch 수와 전체 I/O 수도 구분한다. 전체 결과는 계속 `inconclusive`다.

새 daemon 관리 서비스·전역 process scanner·패턴 kill·반복 대기 정책·추정한 설정 key를 추가하지 않는다. 사용자 설정·credential·실제 로그·유료 모델에 접근하지 않는다. LG-D22/26/28·DB-only·no-replay·caps·C2 hold·실제 raw 비활성을 유지한다.

## 완료 판별

소유 OMP의 공개 종료 근거와 identity에 결속된 종료 관측, 임시 root 재생성 없음, 영향받는 독립 시험·리뷰가 필요하다. 불명인 결과를 완료로 표시하지 않는다. 이 항목이 닫혀도 운영 MODEL·observer/child I/O·DVL-13 최종46 및 전체 DVL-14 gate는 별도로 남는다.
