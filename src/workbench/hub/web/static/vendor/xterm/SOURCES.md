# Vendored xterm.js assets

Third-party browser assets served by the Workbench Hub web board from its own origin (no CDN).
License: MIT for both packages (`LICENSE`, `LICENSE.addon-fit`; copied unmodified from the npm tarballs).

## Packages

| Package | Version | License | Source |
|---|---|---|---|
| `@xterm/xterm` | 5.5.0 | MIT | https://registry.npmjs.org/@xterm/xterm/-/xterm-5.5.0.tgz (project: https://github.com/xtermjs/xterm.js) |
| `@xterm/addon-fit` | 0.10.0 | MIT | https://registry.npmjs.org/@xterm/addon-fit/-/addon-fit-0.10.0.tgz (project: https://github.com/xtermjs/xterm.js) |

Tarball sha256 (as downloaded with `npm pack --ignore-scripts`, 2026-10-25):

- `xterm-xterm-5.5.0.tgz`: `bd954fa721872170188cc5d7e83e88db3c83c9a18a4e8d24c2783d26491f59d2`
- `xterm-addon-fit-0.10.0.tgz`: `917ac44972453d5eed52edc1e50260c76398ce48cf2290c2e60671102bba0b33`

npm registry integrity (sha512):

- `@xterm/xterm@5.5.0`: `sha512-hqJHYaQb5OptNunnyAnkHyM8aCjZ1MEIDTQu1iIbbTD/xops91NB5yq1ZK/dC2JDbVWtF23zUtl9JE2NqwT87A==`
- `@xterm/addon-fit@0.10.0`: `sha512-UFYkDm4HUahf2lnEyHvio51TNGiLK66mqP2JoATy7hRZeXaGMRDr00JiSF7m63vR5WKATF605yEggJKsw0JpMQ==`

`@xterm/addon-fit@0.10.0` declares peer `@xterm/xterm ^5.0.0`. The 6.x line was not chosen (ticket TI-01 specifies 5.x).

## Files in this directory

| File | Origin (tarball path) | sha256 of the vendored file | Changed |
|---|---|---|---|
| `xterm.js` | `@xterm/xterm` `package/lib/xterm.js` | `7a88e46a58847d3f17109e3d48cc8aa056a01997d00ceb76035f63370712d858` | trailing source map comment removed |
| `xterm.css` | `@xterm/xterm` `package/css/xterm.css` | `ba8e6985669488981ccf40c0cefe3aba80722cb6c92de7ad628b0bd717faf2b6` | no |
| `addon-fit.js` | `@xterm/addon-fit` `package/lib/addon-fit.js` | `3e9ae6f6965ee1269d5574b905cf1bf559c2f44c784255b0034a71387e7719c9` | trailing source map comment removed |
| `LICENSE` | `@xterm/xterm` `package/LICENSE` | `b569f629d00f2626a8100df2a1798210535621e42164dfd426a6fe5aac7b0ccd` | no |
| `LICENSE.addon-fit` | `@xterm/addon-fit` `package/LICENSE` | `e256f01188af527e4d06d21d06fbf785ae9c50d4b328bf03cbe0ba7f0aa4228f` | no |

Original (unmodified) sha256 of the two edited files, from the tarball:

- `xterm.js`: `1f991ac3b4b283ebf96e60ae23a00a52765dd3a2e46fa6fdda9f1aab032f7495`
- `addon-fit.js`: `bdaefa370b1bfc42ee88d46fe6072400902a4d4b2d45cd93438dda9b23c97089`

## Modifications and external references

- The only edit is deleting the last line `//# sourceMappingURL=<name>.map` from the two JS files. The `.map` files are not shipped, so the reference would only produce a failed request in browser devtools and is removed (P2-AC-37 no external runtime reference).
- `xterm.css` mentions `http://bellard.org/jslinux/` and `https://github.com/chjj/term.js` only inside comments (credits); no `url()` or `@import` reference.
- The JS files contain no URL literal.
- The tarballs were downloaded and extracted into separate temporary directories, never executed, and the temporary directories were removed.

To update: download the new tarball the same way, replace the files, drop the source map line, and refresh every version and hash above; `tests/hub/test_web_assets.py` checks that this notice contains the sha256 of each vendored file.
