# DVL-14 — checkout 제품 근거·호환 운영과 명시적 raw 활성화

계획 revision p5 / SPEC s5 / BRIEF r0.9 (r0.8 승인 + 운영 원칙 개별 합의).
상태·의존성·resources·scope는 [PLAN.json](../PLAN.json)의 DVL-14를 따른다.
계획 검토안이며 구현·모델 실행·실제 raw 삭제 활성화 권한은 없다.

## 제공할 행동

DVL-10의 검증된 adapter/scheduler와 DVL-12의 disabled policy/reader를 소비한다.
제품은 현재 repo checkout 실행형이다. Runtime identity는 src/workbench/**, omp_bridge/**, pyproject.toml이다.
tools/tests/docs/PLAN/.workflow·개발 manifest는 제품 필수 입력에서 제외하고 packaging 변경은 하지 않는다.
Summary_compatibility·좁은 사용자 CLI logs summary-compat verify --scripted·private product record 검증기를 제공한다.
검증용 local provider와 일회성 OMP는 credential-free temp home/project/data/socket의 기존 agent/skill을 사용하고
종료/정리한다. 새 agent/상주 프로세스/유료 모델을 추가하거나 일회성 운영 요약 route를 자동 채택하지 않는다.
MODEL-metadata는 요청이 실제 사용할 대상 context의 해석된 model/thinking/tier/context/tokenizer 또는 검증 estimator·현재context·output ceiling·reasoning 의미/예산
출처를 읽기 전용으로 관측한다. Observer/credential-free verify는 모델 호출·대화 입력·네트워크·auth store open·설정 쓰기0이고 sanitized 정보만 기록한다.
필수unknown이면 inconclusive/preflight 차단, 임의 fallback/새 설정 없이 관측 뒤 Q-S3-05 결정을 요청한다.

OMP 소유 binary/version/bundled asset만 바뀌면 매 요청의 유효 guard·기존 권한·preflight·활성화 아래 운영을 계속하며 version_drift_unverified로 기록한다.
이전 pass를 현재 버전 pass로 표시하거나 업데이트마다 verify 요구·반복 안내·자동검증하지 않는다.
확인된 호환 문제·관련 제품/skill/route/host/model-config 변경 때 affected 새 요약을 보류하고 신규 source만
legacy-stop, 진행 중 cohort는 유지한다. 원인당 알림1회·상태 표시와 명시적 수동verify로 복구한다.
정상 지연/일시적 통신·요금 오류를 비호환으로 오인하지 않고 기존 보호 상태를 버전 변경으로 해제하지 않는다.
검증만으로 요약 권한/rotation을 활성화하지 않는다. 최초 raw-rotation activate --yes --evidence <record UUID>는
현재8개 pass·metadata/preflight·채택·명시 사용자 호출·경고를 모두 요구한다. 기존 활성화 뒤 verify pass는
새 admission만 회복한다. 임의 외부 JSON/pass flag·개발 결과 복사를 받아 통과하지 않는다.
Q-S2-01/02/04는 합의되었으며 공유 route의03·metadata fallback의05만 필요 시 관측 후 결정한다.

## 수락·검증 계약

수락 ID: LG-AC-01, LG-AC-02, LG-AC-04, LG-AC-06, LG-AC-07, LG-AC-08, LG-AC-22.
G-LG-01, PRODUCT-evidence, MODEL-metadata owner. Supplier/level은 PLAN, 교차 수락은 INT-runtime.
Tools/tests/docs/.workflow 없는 checkout 복사본에서 실행해 development import·원 checkout 자산 누출을 거부한다.
제품 record identity·8개·실제 model metadata를 따로 관측하고 no-model-call/설정·원본 파일 불변을 확인한다.
Version-only 미검증 실행 유지/자동verify0/요구알림0, 확인된 문제/제품변경 보호·사건중복알림0·재시작·명시verify
복구·이전보호유지·provider 일시오류 분류를 시험한다. Metadata 불능은 의도된 음성 거부만 통과하고
실제 configured model의 성공 관측 부재는 전체 gate inconclusive다. Fake metadata로 성공을 대신하지 않는다.
Mixed run/command100MB×5, 빠른eviction 긴급추가호출0/buffer bound·proof/summary gap·legacy불변도 시험한다.
공유 route/대체 상한의 미결정이 필요한 경우 해당 확대/전체 완료는 차단한다. 실제 사용자 raw 활성화는 하지 않는다.

## 구현 경계·작업 인계

Runtime-only checkout에서 안전 격리가 불가능하면 unavailable로 보고하고 개발 산출물을 읽어 우회하지 않는다.
Core 계약·기존 로그/DB·사용자 namespace를 보존한다. 일반 fast/precise 제품 runner/packaging 이전은 범위 밖이다.
독립 test_designer → frozen reviewer → 필요한 수정·delta review → 검사 및 scope audit를 포함한다.
추천 worker: worker_senior. PLAN의 scope/claims/verified workspace를 실제 dispatch 때 재확인한다.
Root만 중앙 PLAN/state/승인을 변경한다. Candidate delta·필수 근거·관측/정책 blocker를 반환한다.

## p4 운영 identity·guard·계약 보호 (P1~P7)

전체 runtime digest는 기록/최초exact 검증용,운영 재검증은08이 동결한 제품 관련 closure/policy로 판정한다.
OMP bundled agent/tool/prompt는OMP경계,Workbench의agents/skills/modeltable은제품경계다. 안정host는OS계열/
arch/Pythonminor이며kernel/patch와무관UI변경은기록만하고요약을막지않는다.영향불명은보수적으로보호한다.
Guard 확인 불능/stale receipt/epoch교체는전송0·즉시보호/newsourcelegacy·진행cohort유지·사건당알림1회다.
등록되나미작동hook 음성도 포함한다. 공개 확인 불가능route는후속채택 전blocked_adoption이다.
10의연속3계약거부보호를실제admission에연결하되원인불명과OMPcompat실패를구분한다.명시verify복구를검증한다.
MODEL-metadata의현재session/모델교체/누락catalog·reasoning 포함/예산을관측하고네트워크0/auth-store open0/
설정쓰기0를음성assertion으로확인한다.없는offline정보는예상inconclusive→Q-S3-05다. thinking/tier변경은
Q-S3-06 관측뒤결정이며현재pin불변. Guard/예산값의fake성공은실제route나모델의pass가아니다.

## p5 운영 감시·복구 시도·정책 대조 (R1~R4)

실제 adapter의 trusted tool event 위반은 결과거부·즉시보호/LG-D13으로 연결한다. 정상 receipt 뒤 위반 음성과
상위turn 오인0을 시험한다. 이미 실행된 부작용을 예방/rollback했다고 보고하지 않는다. 추가 모델 호출은0이다.
Scripted record는 plumbing_verified/real_model_contract=unverified를 분리한다. 보호 뒤 명시 verify는
recovery_attempt이며 다음 새입력 계약거부1회에 재보호·새episode 알림1회다. Restart/재접속 중복0·
최신 정상stored 확인·no replay·기존 승인/활성화/pause를 함께 시험한다. 자동 유료모델시험/설정변경은 없다.
제품 collector는08의 canonical JSON 파일을 그대로 읽고 core version/digest를08freeze와 비교한다.
PRODUCT-evidence는 digest 불일치/필수 closure 누락 invalidated와backend/mailbox/tasks/storage/workflow 변경의
개발·제품 판정 동일성을 관측한다. 제품은tools/tests/.workflow에의존하지 않으며 별도policy상수를 만들지 않는다.
R4는 공유현재session/격리생성context 해석의 전송전 read-back으로관측하며role상속출처도 기록한다.
