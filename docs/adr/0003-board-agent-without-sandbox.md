# ADR-0003: Board agent는 sandbox 없이 사용자 권한으로 실행한다

상태: 사용자 결정 수용, 2026-10-25. 문서 전체 승인과 구현은 별개다.
근거: [Task Inbox & Web Board BRIEF](../features/task-inbox/BRIEF.md) r0.3의 D-08, D-10, D-11, D-25, D-30. [Hub BRIEF](../features/workbench-hub/BRIEF.md) HB-D02.

## 배경

Web Board에는 소유자가 브라우저에서 쓰는 Board agent(Hub가 실행하는 OMP)와 일반 shell이 있다. 사용자는 Board agent로 등록된 WB 프로젝트들을 찾아 요청을 정리하는 것 외에 코드 수정, 사용자 gh/git 설정을 쓰는 GitHub 작업, `wb init`으로 새 WB 프로젝트 만들기, GPU 사용까지 하기를 원한다.

Hub listener는 LAN에 공개되며 HTTP도 허용한다(VPN 전제, D-07). 그래서 Board agent와 웹 shell의 권한 범위가 곧 웹으로 접근할 수 있는 범위가 된다.

[관측 사실] 개발 host의 bwrap 0.9.0은 비특권으로 동작하지만 `--tmp-overlay`가 없다. 개발 host에는 NVIDIA GPU가 없다. 기존 manager·worker OMP는 MCP가 꺼져 있고(`omp_bridge/omp-isolation.yml:54`, `:68`), launcher가 MCP tool 누출을 검사한다(`src/workbench/backend/launcher.py:822-857`).

## 결정

- Board agent와 웹 shell은 sandbox 없이 사용자의 일반 shell과 같은 OS 권한으로 실행한다.
- 보호는 세 가지에 의존한다: (1) 웹 로그인(scrypt 비밀번호, HttpOnly 세션, CSRF 방어, 반복 실패 지연, 24시간 만료), (2) listener를 설정한 주소에만 bind(`0.0.0.0`은 명시 설정), 비밀번호가 없으면 listener를 열지 않음, (3) 감사 기록(로그인, terminal 열기·닫기·인수, API key 발급·폐기. 입력 내용은 기록하지 않음).
- API key로는 terminal을 열 수 없다. Board agent 세트는 로그인한 소유자만 쓴다.
- Sandbox를 두지 않는 대신 OMP 설정의 노출은 좁힌다. Board agent의 MCP는 Hub MCP(editor 등급) 하나만 허용하고, 다른 MCP·프로젝트 설정·사용자 `~/.omp` 설정은 막는다. WB 프로젝트의 manager·worker MCP는 계속 비활성이다. 이 제한은 설정 노출의 범위이며 OS 권한 경계가 아니다.

## 검토한 대안

| 대안 | 판단 |
|---|---|
| bwrap 읽기 전용 mount + scratch 복사본 | 채택하지 않는다. 사용자가 원하는 코드 수정, GitHub 인증, GPU, 새 WB 프로젝트 생성을 막는다. 개발 host의 bwrap 0.9에는 `--tmp-overlay`가 없어 겹쳐 쓰기 방식도 쓸 수 없다. |
| 읽기 전용 mount만 | 채택하지 않는다. 조회는 안전해지지만 Board agent의 주된 쓸모(코드·요청 정리, 프로젝트 생성)를 잃는다. |
| Sandbox 없음 + 로그인·bind 제한·감사 | 채택한다. 사용자의 작업 범위를 그대로 제공한다. 위험은 아래에 정직하게 남긴다. |

## 결과

- 웹 로그인이 계정 전체를 지키는 유일한 장벽이다. 비밀번호나 세션이 새면 공격자는 소유자의 OS 사용자 권한으로 host에서 무엇이든 할 수 있다(코드·자격 증명·다른 WB 프로젝트 자료 포함).
- 평문 HTTP는 VPN 등 네트워크 경계에 의존한다. Loopback이 아닌 주소에서 평문 HTTP면 웹과 `wb hub status`에 경고를 보인다.
- Board agent와 웹 shell은 요청 흐름 밖에서 host 명령을 실행할 수 있다. P2-AC-07("검토 전 외부 client 호출만으로 실행되지 않음")은 MCP·요청 경로에 대한 보장이며, 로그인한 소유자의 Board agent·shell 사용을 막지 않는다.
- P2-AC-14(임의 host 파일 노출 금지)는 이 결정과 D-22로 의도적으로 완화된다.
- GPU 사용 안내(D-27)는 개발 host에서 검증할 수 없고 대상 host에서만 확인한다.
- 다중 사용자나 외부 identity를 도입하면(현재 비목표) 이 결정을 다시 검토해야 한다.

## 검증 요구

- 비밀번호 미설정 시 listener 미개방, 설정 주소 외 bind 없음, 평문 HTTP 경고를 확인한다.
- API key로 terminal 열기·인수 시도가 거절되는지 확인한다.
- Board agent의 tool 목록에 Hub MCP 외 MCP tool이 없고, manager·worker의 기존 MCP 누출 검사가 계속 통과하는지 확인한다.
- 감사 기록에 정해진 사건이 남고 terminal 입력 내용은 남지 않는지 확인한다.
