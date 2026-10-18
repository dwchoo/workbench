# 로그 용량 상태 확인과 수동 운영 안내

이 안내는 기존 로그 상태 명령과 승인 경계를 설명합니다. 상태 조회는 용량만 확인하며 로그 삭제, 보관 정책 활성화, 검증 실행을 하지 않습니다.

## 상태 확인

```sh
omp-workbench logs raw-status
omp-workbench logs important status
```

`raw-status` 기본 출력은 사람이 읽는 한국어 안내입니다. 이전처럼 JSON을 기본 출력으로 파싱하던 스크립트는 다음처럼 `--json`을 명시해야 합니다. JSON key, type, value와 종료 코드는 그대로 유지됩니다.

```sh
omp-workbench logs raw-status --json
omp-workbench logs important status --json
```

raw 상태는 legacy 실험 로그의 project 합계와 run별 한도, terminal의 명령별 한도, segmented raw 용량과 metadata 사용량을 각각 표시합니다. terminal 한도는 명령마다 적용되며 terminal 전체 합계 한도를 뜻하지 않습니다. segmented 저장소가 미생성이거나 부분 조회 상태이면 확인하지 못한 bytes를 0으로 간주하지 않습니다. 이들 값은 전체 data directory, 중요 DB, 기타 파일을 합친 용량 상한이 아닙니다.

활성화 결정과 실제 신규 `source` 정책은 별도로 표시됩니다. 활성화된 기록이 있어도 보호 또는 재검증 상태라면 `legacy-stop`과 사유가 표시될 수 있습니다. 조회만으로 verify나 활성화를 하지 않습니다.

중요 로그 상태는 DB 전체 물리 합계와 각 DB의 UUID, role, 물리 크기, 상태를 표시합니다. 활성 DB의 250,000,000 bytes는 논리 용량 목표이며 전체 디스크나 전체 DB의 물리 상한이 아닙니다. 120일 보관 기간은 subject의 보관 종료 시점부터 계산하며, 용량 압박에 따른 정리는 더 일찍 일어날 수 있습니다. orphan은 catalog와의 관계를 확인할 수 없는 상태이므로 archive나 삭제 대상으로 추정하지 않습니다.

## 수동 정리 경계

legacy 파일에는 후보 목록을 조회하거나 일괄 삭제하는 별도 제품 명령이 없습니다. 종료가 확인된 run/terminal 후보의 정확한 경로와 bytes를 선택하고, 정리 직전에 해당 작업의 종료 상태를 다시 확인한 뒤 별도 승인으로 수동 처리해야 합니다. 상태 조회 결과만으로 삭제하지 마세요.

archive DB는 자동 삭제되지 않습니다. 필요한 경우 현재 상태에서 보존 대상과 영향을 검토하고, 사용자가 선택한 archive UUID 하나를 명시하는 기존 명령을 사용합니다. 실제 삭제에는 해당 UUID에 대한 별도 사용자 승인이 필요하며, status 조회나 이 문서의 예시는 그 승인이 아닙니다.

```sh
omp-workbench logs important delete-db --db <archive-UUID> --yes
```

legacy 후보 정리와 archive UUID 삭제는 서로 다른 저장 영역과 절차입니다. 모호하거나 orphan으로 표시된 DB는 archive로 취급하지 말고 정리하지 않습니다.

## 신규 raw 순환 활성화

신규 source의 segmented raw 순환은 기본적으로 활성화되지 않습니다. 실제 활성화에는 기존 Q-01 및 필수 gate, 현재 checkout의 유효한 local product record, 사용자의 별도 활성화 결정이 모두 필요합니다. CLI 명령을 알거나 status를 조회했다는 사실은 실행 권한이 아닙니다.

```sh
omp-workbench logs raw-rotation activate --yes --evidence <local-product-record-UUID>
```

위 명령은 gate와 현재 record를 확인한 뒤 별도 승인된 운영 단계에서만 사용합니다. 현재 조건을 확인할 수 없거나 보호 상태이면 활성화하지 말고 `logs raw-status`의 신규 source 정책과 사유를 따릅니다.

## 시점이 있는 사전 관측

승인된 OPS-01 readonly inventory는 **2026-10-18 09:04 UTC** 시점에 legacy 실험 0 bytes, terminal 27,459 bytes/18 files, 미생성 segmented store, 비활성 raw 순환, 중요 DB 1개/196,608 physical bytes/active 1개를 기록했습니다. 그 시점에 archive와 orphan은 없었고 local summary compatibility record는 사용할 수 없어 실제 활성화는 보류 상태였습니다. 이것은 당시 관측값이며 현재 용량이나 활성화 상태를 보장하지 않습니다. 운영 작업 전에 현재 상태를 다시 확인하세요.
