# 개발용 실행과 증거 경계

이 문서는 전용 개발 검증의 실행법과 증거 경계를 설명한다.
현재 실행 결과와 남은 검증은 [VERIFICATION](VERIFICATION.md) 및 해당 run의 원본 기록을 따른다.
이 문서 자체는 완료 판정이나 C2 hold 해제가 아니다.
현재 요구와 후속 합의는 [SPEC](SPEC.md), 필수 46개 gate와 supplier는
[PLAN](PLAN.json), 미확인 사항은 [FOLLOWUPS](FOLLOWUPS.md)를 따른다.

## 실행

저장소 root에서 의존성이 설치된 Python을 사용한다.

```sh
PYTHONPATH=src python3 tools/dev_validate.py --mode fast --report-dir <새 전용 절대 경로>
PYTHONPATH=src python3 tools/dev_validate.py --mode precise --jobs 1 --report-dir <다른 새 전용 절대 경로>
```

`--omp <설치된 실행 파일 절대 경로>`로 실행 파일을 명시할 수 있다.
`--jobs 2`는 기존 tmux Bash/sh 두 셀만 겹치며 다른 셀과 feature 항목은 직렬이다.
기존 fast 3개와 precise core 17개, 총 20개 실행 항목 및 precise의 exact test ID 19개는 보존한다.
새 feature 항목 네 개는 별도 실행 대상으로 연결하며, 미실행 항목의 이름만 지워 success로 집계하지 않는다.
Feature 실행 순서는 실제 Backend/storage/raw producer, frozen Q01 recheck, 실제 configured metadata,
product record consumer다. 공유 근거는 같은 invocation·현재 input digest·원 record/log SHA를 검증한다.
누락되거나 오래된 producer는 통과하지 않는다.

2분/10분은 목표다. 실제 60초 점검·65초 detach·300초 요약 간격을 FakeClock으로 대체하거나
목표 시간을 지키기 위해 생략하지 않는다. 전체 precise와 500MB 검사는 국소 수정마다 반복하지 않고
독립 테스트와 리뷰 뒤 최종 후보에서 실행한다.
새 storage/raw 항목은 실제 기본 100MB × 5 eviction을 확인하려고 전용 terminal에서 500MB를 넘는 합성 출력을
생성한다. 이 출력과 DB·HOME은 소유한 임시 경로에만 존재하고, 실제 사용자 raw 정책을 바꾸지 않는다.

## 격리와 결과

runner는 credential-free allowlist 환경과 짧은 전용 `/tmp` namespace를 만든다.
원래 HOME·인증·DB·socket·세션을 사용하지 않는다. Feature fixture의 output 환경도 Python 시작 전에
전용 절대 경로로 지정하며, 과거 `.workflow` 기본 경로로 fallback하지 않는다.

보고서의 input 전후 digest·도구 버전·실제 수집 수·원래 assertion ID·최초 실패·attempt chain·소유 자원 정리를
함께 확인한다. 0건·전부 skip·required unknown은 성공이 아니다. Assertion 불일치는 failed,
준비·provider·관측 불명은 inconclusive로 남긴다. 일부 fixture 통과를 runtime 통과로 올리지 않는다.

`--baseline-report <report.json>`은 동일한 후보·도구·선정 항목·수집 수·정리 근거가 있을 때만 비용을 비교한다.
기존 20개 core 범위의 비용 근거와 새 feature를 포함한 전체 범위의 비용은 구분한다.
항목 구성이 다른 과거 보고서를 새 전체 precise의 동일 범위 baseline으로 표시하지 않는다.

## 현재 알려진 한계

- 제품 verifier의 owned scripted 8개 판별과 실제 configured getter 관측은 별도 근거다.
  Controlled provider descriptor를 실제 운영 Worker.default의 관측으로 바꾸지 않는다.
- LG-D30의 부모 context 점유량·output enforce·reasoning 의미 unknown과 LG-D32의 native 내부 I/O 귀속 한계를
  그대로 기록한다. 관측하지 않은 수치를 0/pass로 표시하지 않는다.
- 실제 Backend의 summary runtime은 `814fb71`의 feature 통합에서 저장·조회와 실제 314.627초 주기를 확인했다.
  Readiness만으로 성공을 추정하지 않으며, 최종 28 AC/46 gate는 [VERIFICATION](VERIFICATION.md)의 현재 상태를 따른다.
- 전용 reasoning fixture의 metadata readback은 성공했다. 발견한 기본 Backend supplier의 고정 writer 누락은
  host 경로 공급과 scheduler의 count/예약/전송 전 검사로 수정했다. 새 runtime은 callback을 주입하지 않고
  기본 Backend 경로를 사용하며, 실제 요약·저장 동작과 소유 정리는 최신 feature 실행에서 확인했다.
- Q01은 기존 SDK controls의 원문 관측을 보존한다. 원 delegate의 synthetic origin은 판정에서 제외하고,
  별도 actual Backend Task/command/receipt trace의 아홉 사실을 public port의 CompositeRuntime이 공급한다.
  관측하지 못한 사실은 unknown으로 남기며, 다른 그룹의 사실을 일괄 true로 바꾸지 않는다.
- Nonreasoning scripted 부모의 실제 `thinkingLevel`이 `undefined`이면 현재 observer는 metadata를 보류한다.
  이를 `off` 값으로 바꾸거나 실모델 실패로 해석하지 않는다.
- 실제 사용자 raw rotation은 활성화 결정됨, 실행 대기다([BRIEF LG-D34](BRIEF.md#lg-d34--raw-rotation-활성화-결정-2026-10-24)). 실행 전까지 실제 사용자 raw는 기존 정책이다. C2 hold는 유지한다. 이미 완료한 작은 실제 요약 시험은 반복하지 않는다.
  인증 원본을 symlink로 연결하는 새 경로를 추가하지 않는다.

실패한 제품 verify·activation에서 생성된 record와 그 record가 지정한 log도 크기·hash를 확인해 전용 공유 근거에 원본 bytes로 보존한다. 이 archive는 성공 producer나 activation 근거가 아니다. Runner는 실행 중에도 이미 귀속한 종료 프로세스를 정확한 PID identity로 수거하며, 정리 판정은 실제 잔여 자원의 부재를 요구한다.
