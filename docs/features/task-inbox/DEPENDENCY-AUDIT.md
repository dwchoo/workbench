# Task Inbox 의존성 audit (TI-01, 수동 기록)

작성: 2026-10-25, 근거 SPEC `docs/features/task-inbox/SPEC.md` "열린 결정 2"(수동 audit 기본안, 온라인 advisory `not_run`)와 설치 SPEC dependency audit 요구. 새 도구(`pip-audit`, `uvx`)는 설치하지 않았다.

## 결론

- Python runtime 의존성은 D-29의 10개 고정 버전과 기존 `pyte`·`wcwidth`다. `pyproject.toml` `dependencies`가 모두 `==`로 고정한다. 새 package는 이 10개 외에 없다.
- 고정 방식: 간접 의존성도 `pyproject.toml`에 직접 `==` 고정(lock 파일 없음). `pip`가 `.venv`에 없어 resolver를 새로 돌리지 않았고, 아래 버전은 개발 `.venv`(uv 설치)의 `importlib.metadata`와 `*.dist-info/RECORD`에서 읽은 값이다. `tests/hub/test_web_assets.py`가 설치 버전, pin, wheel `Requires-Dist`의 일치를 확인한다.
- 온라인 advisory 확인(OSV·PyPI advisory·GitHub advisory·`npm audit`)은 `not_run`이다. 이 기록은 알려진 취약점이 없다는 증명이 아니다.

## Python package

`METADATA RECORD hash`는 `.venv`의 `<name>-<ver>.dist-info/RECORD`에 적힌 `METADATA` 파일의 sha256(urlsafe base64)이며 설치된 배포본 식별용이다. 설치 시 받은 wheel 파일 자체의 sha256은 `.venv`에 남아 있지 않아 기록하지 못했다(gap 1).

| package | 고정 버전 | wheel tag | license(METADATA) | METADATA RECORD hash | 직접/간접 |
|---|---|---|---|---|---|
| aiohttp | 3.14.5 | cp312 manylinux_2_17_x86_64 | Apache-2.0 AND MIT | `sha256=ULKaImhkn3zlsiwKQbYIltJG_uWOKnS0aPZoSORpg0w` | 직접(웹 framework, D-29) |
| aiohappyeyeballs | 2.7.1 | py3-none-any | PSF-2.0 | `sha256=5o0b9IrsncwnZzQ97ewl4ShgCFy6GA4QvD073QMWAro` | aiohttp |
| aiosignal | 1.4.0 | py3-none-any | Apache 2.0 | `sha256=CSR-8dqLxpZyjUcTDnAuQwf299EB1sSFv_nzpxznAI0` | aiohttp |
| attrs | 26.1.0 | py3-none-any | MIT | `sha256=TNQOaQ8jvzfLytNO_WdY4GLfHfB8hoM_fjzpW_H6OMw` | aiohttp |
| frozenlist | 1.8.0 | cp312 manylinux_2_5_x86_64 | Apache-2.0 | `sha256=lGwi3J9-LHby1pjwCkMG2-Sagg4rPxoOIv5ShsvsNfE` | aiohttp, aiosignal |
| idna | 3.20 | py3-none-any | BSD-3-Clause | `sha256=29jBTB5MoeDJgkptvHy794iE8461LZEUjNMCX2ceS4U` | yarl |
| multidict | 7.2.0 | cp312 manylinux_2_17_x86_64 | Apache-2.0 | `sha256=-ivjhvruXPQ4CecQQMk2Xq9Wh8vPmxPSRmbL6oNMCJ0` | aiohttp, yarl |
| propcache | 0.5.4 | cp312 manylinux_2_17_x86_64 | Apache-2.0 | `sha256=btO6wK1DBQzq-L0Jh4uj1nPJTE2g1EmPkD-Gw2zG-1c` | aiohttp, yarl |
| typing-extensions | 4.16.0 | py3-none-any | PSF-2.0 | `sha256=sFCEyh1Qh5hlF42f_5-r6rYb37HzYb-96VQh_8j5vkY` | multidict, yarl 등 |
| yarl | 1.25.1 | cp312 manylinux_2_17_x86_64 | Apache-2.0 | `sha256=_WuqJYoRxNfnAIJyppGA1NNKoEpqWv2jasXlWqCqT0E` | aiohttp |
| pyte | 0.8.2 | py3-none-any | LGPLv3 (classifier) | `sha256=68Y9u6yV6GNDPsz34pMLtIudeJknOVAz57pLuzjFmJQ` | 기존 |
| wcwidth | 0.8.4 | py3-none-any | MIT | `sha256=ejh76tG45gZ8GVrS-j4cyMXQuTgDTtWW8DI7snvUcPI` | 기존 |

출처: PyPI(`https://pypi.org/project/<name>/<version>/`). 설치 도구는 uv다(`INSTALLER`). 가장 가까운 확인 방법: 새 환경에서 `uv pip install --require-hashes` 또는 `uv pip compile --generate-hashes`로 wheel hash를 얻어 이 표에 더한다(network 필요).

aiohttp의 C extension 의존(frozenlist·multidict·propcache·yarl)은 cp312 manylinux x86_64 wheel이다. 다른 arch·libc의 대상 host는 설치 시 wheel이 없을 수 있고, 그때 source build가 필요한지는 확인하지 못했다(gap 2, 대상 host 확인).

## Frontend 자산(vendored)

`src/workbench/hub/web/static/vendor/xterm/SOURCES.md`가 원본이다.

| 자산 | 버전 | 출처 | tarball sha256 | license |
|---|---|---|---|---|
| `@xterm/xterm` | 5.5.0 | `https://registry.npmjs.org/@xterm/xterm/-/xterm-5.5.0.tgz` | `bd954fa721872170188cc5d7e83e88db3c83c9a18a4e8d24c2783d26491f59d2` | MIT |
| `@xterm/addon-fit` | 0.10.0 | `https://registry.npmjs.org/@xterm/addon-fit/-/addon-fit-0.10.0.tgz` | `917ac44972453d5eed52edc1e50260c76398ce48cf2290c2e60671102bba0b33` | MIT |

- npm integrity(sha512)는 SOURCES.md에 있고, `npm view`가 돌려준 registry 값이다. 내려받은 tarball에서 `lib/xterm.js`·`css/xterm.css`·`lib/addon-fit.js`와 두 `LICENSE`만 복사했다. 두 JS 파일의 마지막 줄 `//# sourceMappingURL=...`만 삭제했다(`.map`을 싣지 않으므로). 원본·변경 후 sha256은 SOURCES.md에 모두 있다.
- 6.x(최신 `latest` 6.0.0)가 아니라 5.x 최신 안정판 5.5.0을 쓴 것은 ticket TI-01 지정에 따른다. `addon-fit@0.10.0`의 peer는 `@xterm/xterm ^5.0.0`이다. 5.x 지원 종료·6.x 이관 시점은 확인하지 않았다.
- Node·npm은 다운로드에만 쓰였고 runtime 의존성이 아니다. tarball은 실행하지 않았고 임시 디렉터리에서 풀어 확인한 뒤 지웠다. npm install 스크립트는 `--ignore-scripts`로 막았다.
- 제공 JS·CSS에서 `url()`·`@import`·URL 리터럴 외부 참조는 없다(CSS 주석의 credit URL 두 개만 존재).

## 알려진 advisory 확인 방법과 결과

| 항목 | 방법 | 결과 |
|---|---|---|
| Python package 10+2개 | `pip-audit`/`uvx pip-audit`, 또는 OSV·GitHub Advisory DB 조회 | `not_run`(도구 설치·network 사용이 이번 위임 범위 밖, SPEC 열린 결정 2) |
| xterm.js 2개 | `npm audit`(lockfile 필요)/GitHub advisory 조회 | `not_run` |
| license | METADATA와 tarball `LICENSE` 직접 확인 | 수행. 모두 permissive(MIT·BSD·Apache·PSF)이며 `pyte`만 LGPLv3(기존 의존성, 변경 없음) |
| 버전 일치 | 설치 버전 = pin = wheel `Requires-Dist` | 수행(`tests/hub/test_web_assets.py`) |
| `mcp` SDK 불포함 | `src` import 검사 | 수행(같은 테스트) |

## Gap

1. 설치 wheel 자체의 sha256 미기록(`.venv`에 wheel 없음). hash 고정 설치는 이번 범위가 아니다.
2. 대상 host(arch·libc)에서 C extension wheel 가용성 미확인.
3. 온라인 advisory 확인 `not_run`. TI-19 dependency audit 또는 사용자가 열린 결정 2를 바꿀 때 수행한다.
