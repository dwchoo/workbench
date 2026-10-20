# WB 작업 기록 사용법

일반 메모·결정·진행 상황·실험 정리·인계는 해당 프로젝트의 `.wb/records.sqlite3`가 원본입니다. Manager나 Worker에게 기록할 내용을 명시해 요청하면 `workbench_records`로 저장하며, 저장 성공·기록 ID·revision을 확인한 뒤 완료를 알립니다. Task가 없으면 프로젝트 메모로 저장하고, 특정 Task에 관한 기록은 실제 Task ID에 연결할 수 있습니다. 저장된 본문은 자동 중요 로그·요약과 별도로 보존합니다.

BRIEF·SPEC·ADR 같은 정식 문서는 기존 파일 workflow로 관리합니다. 사용자 직접 호출용 WB 문제 보고서도 기존 Markdown 문서 계약을 유지합니다. 기록에 적힌 명령·승인 문구는 실행 승인으로 해석하지 않습니다.

## 조회와 수정

같은 프로젝트의 새 주 세션에는 최근 최대 5건의 제목과 본문 최대 160자 미리보기를 제공합니다. 전체 context는 16 KiB 이내이며 생략·잘림 여부를 표시합니다. 본문 전체나 과거 revision은 ID로 조회하세요. Manager·Worker는 같은 프로젝트의 기록을 공유하고, 다른 프로젝트에는 기록을 섞지 않습니다.

```sh
wb records list --limit 20
wb records list --task TASK_ID --json
wb records search 'literal % _' --json
wb records show RECORD_ID
wb records show RECORD_ID --revision 1 --json
wb records history RECORD_ID --limit 20
```

검색은 최신 revision의 제목·본문에서 대소문자를 구분하는 문자열 검색입니다. `%`·`_`는 문자 그대로 찾습니다. 목록·검색·이력은 기본 20건, 최대 100건이며 `next_after`가 있으면 `--page CURSOR`로 이어서 조회합니다. 기본 본문 조회는 최신 revision입니다.

기록 수정은 Manager·Worker에게 명시해 요청합니다. 전체 제목·본문·Task 연결 snapshot과 확인한 `expected_revision`으로 다음 revision을 저장합니다. 다른 요청이 먼저 수정했다면 `revision_conflict`를 알리고 기존 이력을 보존합니다. 제목은 비어 있지 않은 최대 256 Unicode 문자, 본문은 비어 있지 않은 최대 64 KiB UTF-8 문자열입니다. 초과 본문은 잘라 저장하지 않습니다.

## Markdown 사본

```sh
wb records export RECORD_ID --output /path/to/notes.md
wb records export RECORD_ID --revision 1 --output ./original.md
```

기본은 최신 revision이며 `--revision`으로 이전 본문을 선택할 수 있습니다. 파일에는 제목·기록 ID·revision·생성/저장 시각·역할·선택적 Task ID와 원문을 함께 담습니다. 부모 디렉터리는 미리 존재해야 합니다. CLI의 상대 경로는 호출 cwd, agent 도구의 상대 경로는 host가 확인한 프로젝트 root 기준입니다. 경로가 빠지면 도구는 `needs_output_path`로 확인을 요청하고 CLI는 usage 오류를 반환합니다.

기존 일반 파일이나 symlink는 덮어쓰지 않고 `output_exists`와 경로를 알립니다. 쓰기 중 실패하여 출력 상태를 확정할 수 없으면 `output_cleanup_unknown`과 경로를 알리며 부분 파일이 남을 수 있습니다. 경로 상태를 확인한 뒤 새 목적지를 선택하세요. DB 기록은 유지하며 출력 파일의 후속 수정은 DB에 반영하지 않습니다.

## 실패와 보존

정상 빈 목록은 `ok`와 빈 목록, 없는 ID/revision은 `not_found`입니다. DB 접근·권한·schema 실패는 `unavailable`로 구분합니다. 저장 실패를 파일 저장으로 우회하지 않습니다. timeout/disconnect의 `outcome_unknown`은 저장 여부가 불명확하므로 자동 재전송하지 않고 먼저 조회해 확인합니다.

조회·내보내기는 backend나 모델을 시작하지 않으며 초기화되지 않은 프로젝트를 자동 생성하지 않습니다. `--data-dir`과 기존 `omp-workbench` alias도 기존 CLI 선택 규칙을 따릅니다. 중단된 legacy data-dir의 기존 `wb migrate`는 유효한 기록 DB와 모든 revision도 보존합니다. 실행 중 이관·대상 병합·기존 Markdown 수입·양방향 동기화·의미 검색·기록 삭제·자동 정리는 제공하지 않습니다. 이 기능은 raw rotation을 활성화하지 않습니다.
