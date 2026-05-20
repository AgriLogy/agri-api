# [1.6.0](https://github.com/AgriLogy/agrilogy-back/compare/v1.5.1...v1.6.0) (2026-05-20)


### Features

* **alerts:** email on ingest when an active alert fires, throttled per sensor ([#35](https://github.com/AgriLogy/agrilogy-back/issues/35)) ([cc7a0bc](https://github.com/AgriLogy/agrilogy-back/commit/cc7a0bc6c156e2006009dcde07b53485d99b7ae8)), closes [#36](https://github.com/AgriLogy/agrilogy-back/issues/36)

## [1.5.1](https://github.com/AgriLogy/agrilogy-back/compare/v1.5.0...v1.5.1) (2026-05-20)

# [1.5.0](https://github.com/AgriLogy/agrilogy-back/compare/v1.4.2...v1.5.0) (2026-05-20)


### Features

* **agronomy:** Dr/RAW-based irrigation decision per agronomist spec ([6b72c58](https://github.com/AgriLogy/agrilogy-back/commit/6b72c58d8c105ac6b28c29d34a5eff456f4015d8))
* **agronomy:** Dr/RAW-based irrigation decision per agronomist spec ([#32](https://github.com/AgriLogy/agrilogy-back/issues/32)) ([67d8171](https://github.com/AgriLogy/agrilogy-back/commit/67d8171b4a4ef917d755c80ac6885fcb737a8a6c))

## [1.4.2](https://github.com/AgriLogy/agrilogy-back/compare/v1.4.1...v1.4.2) (2026-05-20)


### Bug Fixes

* **analytics:** serialize sensor timestamps with full ISO 8601 precision ([661b34d](https://github.com/AgriLogy/agrilogy-back/commit/661b34d061ffe79ece7009500dee2979df59dc0c)), closes [#29](https://github.com/AgriLogy/agrilogy-back/issues/29)
* **notifications:** space numeric values from units (°C, %) in email body ([b638506](https://github.com/AgriLogy/agrilogy-back/commit/b63850632ba8cb70e851c800c13c691cd8119ff6)), closes [#30](https://github.com/AgriLogy/agrilogy-back/issues/30)

## [1.4.1](https://github.com/AgriLogy/agrilogy-back/compare/v1.4.0...v1.4.1) (2026-05-17)

# [1.4.0](https://github.com/AgriLogy/agrilogy-back/compare/v1.3.1...v1.4.0) (2026-05-14)


### Features

* **admin:** backoffice CRUD + manager affirmation ([#20](https://github.com/AgriLogy/agrilogy-back/issues/20)) ([ade25ad](https://github.com/AgriLogy/agrilogy-back/commit/ade25adea5adc5d50b03641beba61770400ac32a))
* **admin:** backoffice CRUD endpoints + manager affirmation + tests ([df549f2](https://github.com/AgriLogy/agrilogy-back/commit/df549f2553ecfc31b480a85411c1fd2f0e9f217e))

## [1.3.1](https://github.com/AgriLogy/agrilogy-back/compare/v1.3.0...v1.3.1) (2026-05-12)


### Bug Fixes

* **agronomy:** apply 2026-05-10 review corrections to ET0 hourly math ([91b2e97](https://github.com/AgriLogy/agrilogy-back/commit/91b2e970a16331f8144b7efc9e4eaaaf23f930ff))
* **agronomy:** apply 2026-05-10 review corrections to ET0 hourly math ([#19](https://github.com/AgriLogy/agrilogy-back/issues/19)) ([2ac9ad9](https://github.com/AgriLogy/agrilogy-back/commit/2ac9ad9430138f05d35e1ce587354cbf8ae9e750))

# [1.3.0](https://github.com/AgriLogy/agrilogy-back/compare/v1.2.0...v1.3.0) (2026-05-11)


### Features

* **agronomy:** consolidate ET0 + irrigation math into one expert-own… ([#14](https://github.com/AgriLogy/agrilogy-back/issues/14)) ([7956830](https://github.com/AgriLogy/agrilogy-back/commit/79568306123bb42f32499fe519e421b3ca0465db))
* **agronomy:** consolidate ET0 + irrigation math into one expert-owned module ([4bb0280](https://github.com/AgriLogy/agrilogy-back/commit/4bb0280cef0e71450c956a984ab2f26157560a6d)), closes [hi#level](https://github.com/hi/issues/level)
* **alerts:** plug-and-play alert module + dev seed scripts + containerised stack ([c8855fc](https://github.com/AgriLogy/agrilogy-back/commit/c8855fc46711841ad2d000548d0d7e9669785e4e))

# [1.2.0](https://github.com/AgriLogy/agrilogy-back/compare/v1.1.0...v1.2.0) (2026-05-08)


### Features

* deliver notification emails end-to-end (smtp + endpoints + tests) ([66106e8](https://github.com/AgriLogy/agrilogy-back/commit/66106e8a29d4ce9f637539f4601aef87f74a890e))
* deliver notification emails end-to-end (smtp + endpoints + tests) ([#11](https://github.com/AgriLogy/agrilogy-back/issues/11)) ([a5590e6](https://github.com/AgriLogy/agrilogy-back/commit/a5590e6205765e3fc47f6192b1fa880faa8bb0f7))

# [1.1.0](https://github.com/AgriLogy/agrilogy-back/compare/v1.0.0...v1.1.0) (2026-05-07)


### Bug Fixes

* **deps:** drop readme path that escapes the project tree ([9fab887](https://github.com/AgriLogy/agrilogy-back/commit/9fab887a0346acd12e54b38326d91aca3b97fa56))


### Features

* **scripts:** add scripts/dev.sh launcher and `make dev` target ([fe46d0b](https://github.com/AgriLogy/agrilogy-back/commit/fe46d0ba0e05482a0f265566e7c539cb866620aa))

# 1.0.0 (2026-05-07)


### Bug Fixes

*  front domain name ([7e9c879](https://github.com/AgriLogy/agrilogy-back/commit/7e9c879680d87ad2084127ae90c12af52c160ad0))
* **ci:** drop uv cache config until uv.lock is committed ([27a4bab](https://github.com/AgriLogy/agrilogy-back/commit/27a4babdc463dd39f3eb2d040c52161dae38a231))
* **ci:** loosen ruff config to match legacy flake8 leniency ([bb81c15](https://github.com/AgriLogy/agrilogy-back/commit/bb81c159b92489a6c8d5f33e7179a894081c92e9))
* domain name issue ([9d12750](https://github.com/AgriLogy/agrilogy-back/commit/9d12750e190040bbc2bab8c810e9deecde8aa063))
* dummy insertion data ([0567885](https://github.com/AgriLogy/agrilogy-back/commit/056788584a33a2abb7fe6b3054426d34321580f2))
* env example ([9896566](https://github.com/AgriLogy/agrilogy-back/commit/98965668f5d1cb66c501fba972eeb6b5051c875e))
* update the data server ([184186b](https://github.com/AgriLogy/agrilogy-back/commit/184186b4b3fddf7a5cf91c57759761fbd798b96f))
* user-zone mapping ([9c62202](https://github.com/AgriLogy/agrilogy-back/commit/9c62202067b8db86e80321a1a47e571d0eba7c6f))


### Features

* add data forward from js server ([d423d44](https://github.com/AgriLogy/agrilogy-back/commit/d423d44820f1c97df23dfbfeca05d2d255a2edfd))

# Changelog

All notable changes to this project will be documented in this file.
This file is generated by [semantic-release](https://github.com/semantic-release/semantic-release) — do not edit manually.
