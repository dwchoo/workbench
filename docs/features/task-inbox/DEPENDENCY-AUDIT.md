# Task Inbox 의존성 audit (TI-01, 수동 기록)

작성: 2026-10-25, 근거 SPEC `docs/features/task-inbox/SPEC.md` "열린 결정 2"(수동 audit 기본안)와 설치 SPEC dependency audit 요구. 작성 당시에는 새 도구를 설치하지 않았고 온라인 advisory 확인은 `not_run`이었다. 2026-10-26에 사용자가 `uvx pip-audit`와 OSV 온라인 조회를 허용해 아래 "온라인 advisory 확인(2026-10-26)"에서 수행했다.

## 결론

- Python runtime 의존성은 D-29의 10개 고정 버전과 기존 `pyte`·`wcwidth`다. `pyproject.toml` `dependencies`가 모두 `==`로 고정한다. 새 package는 이 10개 외에 없다.
- 고정 방식: 간접 의존성도 `pyproject.toml`에 직접 `==` 고정(lock 파일 없음). `pip`가 `.venv`에 없어 resolver를 새로 돌리지 않았고, 아래 버전은 개발 `.venv`(uv 설치)의 `importlib.metadata`와 `*.dist-info/RECORD`에서 읽은 값이다. `tests/hub/test_web_assets.py`가 설치 버전, pin, wheel `Requires-Dist`의 일치를 확인한다.
- 온라인 advisory 확인은 2026-10-26에 수행했다(아래 절). Python pin 12개와 vendored xterm 2개 모두 OSV·PyPI Advisory·`pip-audit` 기준 알려진 취약점이 없다. 이 결과는 조회 시점의 DB 기준이며 취약점이 없다는 증명이 아니다.

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
| typing-extensions | 4.16.0 | py3-none-any | PSF-2.0 | `sha256=sFCEyh1Qh5hlF42f_5-r6rYb37HzYb-96VQh_8j5vkY` | aiohttp, aiosignal (python<3.13) |
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

## 온라인 advisory 확인(2026-10-26)

사용자가 2026-10-26에 `uvx pip-audit`와 OSV 온라인 조회를 허용했다. 도구: `pip-audit 2.10.1`(`uvx`로 실행, `.venv` 변경 없음), OSV API `https://api.osv.dev/v1`(`/query`, `/querybatch`).

실행한 명령(임시 파일은 `/tmp/depaudit`에 두고 작업 뒤 삭제):

1. 정확한 pin 감사. `/tmp/depaudit/req.txt`는 `pyproject.toml` `dependencies`의 `==` 12줄을 그대로 옮긴 것이다.
   `uvx pip-audit -r /tmp/depaudit/req.txt --no-deps --disable-pip` — exit 0, `No known vulnerabilities found`. 기본 vulnerability service(PyPI)와 `-s osv` 두 가지로 각각 실행했고 결과가 같다.
2. 설치본 감사(교차 확인).
   `uvx pip-audit --path .venv/lib/python3.12/site-packages` — exit 0, `No known vulnerabilities found`. 감사된 12개 버전이 pin과 일치한다.
3. Python pin OSV 교차 확인.
   `POST https://api.osv.dev/v1/querybatch`에 PyPI 12개 `{package, version}` 질의 — 12개 모두 `vulns` 없음.
4. vendored JS OSV 조회.
   `curl -s https://api.osv.dev/v1/query -d '{"package":{"ecosystem":"npm","name":"@xterm/xterm"},"version":"5.5.0"}'`와 `@xterm/addon-fit` 0.10.0 — 둘 다 `{}`(취약점 없음). 구 이름 `xterm` 5.5.0·`xterm-addon-fit` 0.10.0도 `{}`.
5. 조회 정상 동작 확인(대조군). `aiohttp` 3.9.0 질의는 84개 advisory를 돌려줘 OSV 조회가 실제로 동작함을 확인했다. `@xterm/xterm`·`@xterm/addon-fit` 이름만 질의하면(버전 없음) 둘 다 advisory가 0건이다.

| 항목 | 버전 | pip-audit | OSV | 영향 |
|---|---|---|---|---|
| aiohttp | 3.14.5 | 취약점 없음 | 없음 | 해당 없음 |
| aiohappyeyeballs | 2.7.1 | 취약점 없음 | 없음 | 해당 없음 |
| aiosignal | 1.4.0 | 취약점 없음 | 없음 | 해당 없음 |
| attrs | 26.1.0 | 취약점 없음 | 없음 | 해당 없음 |
| frozenlist | 1.8.0 | 취약점 없음 | 없음 | 해당 없음 |
| idna | 3.20 | 취약점 없음 | 없음 | 해당 없음 |
| multidict | 7.2.0 | 취약점 없음 | 없음 | 해당 없음 |
| propcache | 0.5.4 | 취약점 없음 | 없음 | 해당 없음 |
| typing-extensions | 4.16.0 | 취약점 없음 | 없음 | 해당 없음 |
| yarl | 1.25.1 | 취약점 없음 | 없음 | 해당 없음 |
| pyte | 0.8.2 | 취약점 없음 | 없음 | 해당 없음 |
| wcwidth | 0.8.4 | 취약점 없음 | 없음 | 해당 없음 |
| `@xterm/xterm` | 5.5.0 | 해당 없음(npm) | 없음(`@xterm/xterm`, 구 이름 `xterm`) | 해당 없음 |
| `@xterm/addon-fit` | 0.10.0 | 해당 없음(npm) | 없음(`@xterm/addon-fit`, 구 이름 `xterm-addon-fit`) | 해당 없음 |

- 영향받는 package가 없어 업그레이드·고정 버전 변경은 하지 않았다.
- 구 이름 `xterm` 패키지에는 `GHSA-mc23-976p-j42x`가 있으나 버전 질의(5.3.0·5.5.0)에서는 해당하지 않는다. 우리는 `@xterm/xterm`을 vendoring하므로 영향이 없다.
- 확인한 vendored 파일: `xterm.js`와 `addon-fit.js`의 sha256이 `SOURCES.md`와 일치한다. 이 조회는 npm package 버전 기준이며 내려받은 파일의 내용을 별도로 분석한 것은 아니다.

## 알려진 advisory 확인 방법과 결과

| 항목 | 방법 | 결과 |
|---|---|---|
| Python package 10+2개 | `uvx pip-audit`(pin·설치본), OSV `/v1/querybatch` | 수행(2026-10-26). 알려진 취약점 없음 |
| xterm.js 2개 | OSV `/v1/query`(npm `@xterm/xterm`·`@xterm/addon-fit`). `npm audit`는 lockfile이 없어 쓰지 않음 | 수행(2026-10-26). 알려진 취약점 없음 |
| license | METADATA와 tarball `LICENSE` 직접 확인 | 수행. 모두 permissive(MIT·BSD·Apache·PSF)이며 `pyte`만 LGPLv3(기존 의존성, 변경 없음) |
| 버전 일치 | 설치 버전 = pin = wheel `Requires-Dist` | 수행(`tests/hub/test_web_assets.py`) |
| `mcp` SDK 불포함 | `src` import 검사 | 수행(같은 테스트) |

## Gap

1. 설치 wheel 자체의 sha256 미기록(`.venv`에 wheel 없음). hash 고정 설치는 이번 범위가 아니다.
2. 대상 host(arch·libc)에서 C extension wheel 가용성 미확인.
3. 온라인 advisory 확인은 2026-10-26에 수행했다(gap 해소). 이후 새 advisory가 생길 수 있으므로 배포 전 TI-19 dependency audit에서 같은 명령으로 다시 확인한다.
