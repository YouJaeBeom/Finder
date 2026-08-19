# 연구실 단위 Paper Digest — 개발 플랜 (v2)

> 개인용 다이제스트를 연구실 인원이 함께 쓰는 형태로 확장하는 계획서.
> v2 재작성 2026-08-19 · **멤버별 하위 페이지 + 개별 DB** 구조로 전환

---

## 0. 확정된 결정

| # | 항목 | 결정 |
|---|---|---|
| 1 | Notion 구조 | **멤버별 하위 페이지 + 그 안에 개인 논문 DB** (공유 DB 아님) |
| 2 | 뉴스 | **공용 1회**, 워크스페이스 메인 페이지 직속 DB |
| 3 | 멤버 정의 | `members/<id>.yaml` 한 명당 한 파일 |
| 4 | 연구 주제 | **한 명당 1개** (복수 주제는 보류) |
| 5 | 처리 방식 | 수집 1회 → 멤버 **순차** 처리 |
| 6 | 비용 | 연구실 공용 API 키 하나 (Anthropic + Notion) |
| 7 | 멤버 등록 | 운영자가 YAML 추가 — Notion 수작업 **0** |
| 8 | 논문 소스 | Semantic Scholar 단일 — venue 화이트리스트 |

### v1에서 바뀐 것과 그 이유

v1은 **공유 DB 하나 + `Member` 컬럼 + 멤버별 linked view**였다. 그 구조의 치명적
결함은 **Notion API가 view를 만들 수 없다**는 것이다. 멤버가 늘 때마다 운영자가
Notion을 열어 linked view를 손으로 만들어야 했고, 그게 온보딩의 유일한 수작업이자
자동화 불가능한 지점이었다.

**멤버마다 자기 페이지에 자기 DB를 두면 그 문제가 통째로 사라진다.** Notion API는
페이지 생성도 DB 생성도 된다. 멤버 추가 = YAML 파일 하나 추가, 나머지는 코드가 한다.

부수적으로 얻는 것:

- `Member` / `Overlap` 컬럼이 불필요해진다 — DB 자체가 그 사람 것이다
- 개인 DB는 기본 뷰가 곧 개인 뷰다. 필터를 안 걸어도 자기 것만 보인다
- 중복 방지의 진실 계층이 자연스러워진다 — "내 DB에 이 논문이 있나"를 그냥 조회하면 된다
- 멤버 한 명의 실패가 다른 멤버의 DB에 아무 영향이 없다 (물리적 격리)

잃는 것은 `Overlap`(몇 명이 같은 논문을 받았나) 집계인데, 이건 실행 리포트에
찍으면 된다. Notion 컬럼일 필요가 없다.

---

## 1. Notion 구조

```
📄 Finder                                  ← notion_parent_page_id (워크스페이스 메인)
│
├── 📰 IT 뉴스                             ← DB. 공용 1회, 메인 페이지 직속
│
├── 📄 유재범                               ← 멤버 하위 페이지 (코드가 생성)
│   └── 📚 논문                             ← 그 사람 전용 DB (코드가 생성)
│
├── 📄 샘플-검색RAG
│   └── 📚 논문
│
└── 📄 샘플-웹데이터
    └── 📚 논문
```

### 📚 논문 DB (멤버당 1개)

| 컬럼 | 타입 | 비고 |
|---|---|---|
| Title | title | |
| Summary | rich_text | 노트 한 줄 요약 — 열지 않고 표에서 읽는 용도 |
| Venue | select | ACL / SIGIR / TPAMI |
| Score | number | **그 멤버 기준** 관련도 0~10 |
| Kind | select | conference / journal |
| Status | select | published / accepted |
| Published | date | 게재일 |
| Tags | multi_select | 실제로 매치된 키워드 |
| URL | url | |
| Collected | date | 수집일 |

`Type` 컬럼은 없다 — 뉴스가 다른 DB로 갔으므로 논문/뉴스를 구분할 필요가 없다.
`Member` / `Overlap` 컬럼도 없다 — DB 자체가 그 사람 것이다.

### 📰 IT 뉴스 DB (공용 1개)

| 컬럼 | 타입 |
|---|---|
| Title | title |
| Summary | rich_text |
| Source | select (Hacker News / TechCrunch / The Verge / Ars Technica) |
| Points | number (HN 커뮤니티 점수, RSS는 빈 칸) |
| Published | date |
| URL | url |
| Collected | date |

`Score` / `Status` / `Tags`가 없다. 뉴스는 LLM 관련도 채점을 안 거치고
(소스가 이미 큐레이션됨), 심사 상태 개념이 없다.

### 해석 순서 (모든 페이지·DB 공통)

1. `state.json`의 캐시된 ID
2. 부모 페이지 아래에서 **제목으로 검색**
3. 없으면 생성

2번이 CI 안전장치다. 러너는 매번 깨끗한 체크아웃이라 `state.json`이 없고, 조회가
없으면 실행할 때마다 DB가 하나씩 늘어난다. 그리고 `state.json`이 유실돼도 제목
조회가 같은 곳을 다시 찾아내므로 **상태 파일은 최적화이지 진실이 아니다.**

---

## 2. 멤버 정의 — `members/<id>.yaml`

파일 하나 = 사람 하나. 파일명(stem)이 내부 ID이고, 상태 파일 이름에 쓰인다.

```yaml
name: "유재범"                    # Notion 하위 페이지 제목
enabled: true
# top_n 은 선택입니다. 적지 않으면 상한 없음 — 컷오프를 넘은 논문이 전부 들어옵니다

research_profile: |
  (2~6문장. 관련도 채점과 "내 연구와의 연결점" 작성에 함께 쓰임)

keywords:
  # 기존 DSL 그대로 — 구절 / all / any / not / 중첩 리스트
  - "political bias"
  - all: [["LLM", "language model"], ["robustness", "consistency"]]
    not: ["survey"]
```

**연구 주제는 한 명당 1개.** `research_profile` 하나, `keywords` 한 묶음.
복수 주제(한 사람이 축 1과 축 3을 따로 받는 형태)는 보류한다 — 필요해지면
`topics:` 리스트로 감싸고 DB를 주제별로 나누는 방향이 되겠지만, 지금은 YAGNI다.

**멤버가 못 바꾸는 것**: venue 목록(`conferences` / `journals`)은 lab 소유다.
수집이 공용 1회이므로 구조적으로 개인화가 불가능하다. 개인이 조절하는 건
키워드·프로필·`top_n`(선택) 셋뿐이고, 그게 맞는 경계다.

---

## 3. 파이프라인 — 단일 잡, 순차 처리

```
1. preflight                    토큰·페이지 ID 검증. 수집 전, LLM 호출 전
2. 뉴스 DB 확보                  메인 페이지 직속
3. 수집 1회                      학회 + 저널 (venue·날짜 기준, 인원과 무관)
4. 런 내부 중복 제거              소스 간 병합
5. 뉴스 공용 1회                  수집 → 선별 → 노트 → 뉴스 DB 작성
6. 멤버 순차 루프 ────────────────────────────────────────────┐
     6-1 멤버 페이지 + 개인 DB 확보                            │
     6-2 키워드 필터   (그 멤버 키워드)          로컬, 무료     │
     6-3 이미 받은 것 제외 (개인 DB 조회 + 채점 캐시)          │
     6-4 관련도 채점   (haiku, 그 멤버 프로필)   후보 수 비례   │
     6-5 top_n 컷 (설정한 경우에만 — 기본은 전부)              │
     6-6 노트 작성     (sonnet, 그 멤버 프로필)  비용의 95%    │
     6-7 개인 DB 작성                                          │
     한 명의 실패는 try/except로 격리 — 나머지는 계속 ─────────┘
7. 리포트                        멤버별 편수, Overlap 상위, 실패 목록
```

### 왜 GitHub Actions matrix가 아니라 단일 잡인가

matrix가 주는 건 장애 격리 하나인데 그건 `try/except`로 똑같이 된다. 반면:

- 잡마다 checkout + setup-python + `pip install` → 인당 ~1.5분의 순수 오버헤드
- 후보 풀을 artifact로 넘겨야 하고, 상태 파일을 다시 모아 커밋해야 한다
- **Notion 3 req/s는 토큰 단위 버킷이다.** 잡을 병렬로 돌리면 같은 버킷을 나눠 쓰는
  것이라 429가 줄지 않고 **늘어난다**

단일 프로세스면 Notion 요청이 직렬로 나가므로 스로틀을 정확히 걸 수 있다.
분리가 정말 필요해지면 그때 나눠도 되고, 그때는 후보 풀 직렬화만 추가하면 된다.

---

## 4. 중복 방지 — 2계층

2026-08-15 백필에서 `git push` 실패로 `seen_ids.json`이 유실됐다 (현재 로컬 21건).
파일이 진실인 구조는 이 사고를 구조적으로 허용한다.

| 계층 | 저장소 | 답하는 질문 | 유실 시 손실 |
|---|---|---|---|
| **진실** | 멤버 개인 DB 조회 | "이 멤버에게 이미 썼나?" | **없음** — 재조회하면 복구됨 |
| **최적화** | `state/scored/<id>.json` | "이미 채점하고 떨어뜨렸나?" | 재채점 비용만 (haiku, 센트 단위) |

- 진실 계층: 멤버 처리 시작 시 개인 DB를 조회해 `(URL, 정규화 제목)` 인덱스를 만든다.
  개인 DB는 작다 — 월 20편이면 1년에 240행, 3요청.
- 최적화 계층: "채점했지만 컷오프에서 떨어진" 논문. Notion에 없으므로 별도 기록이 필요.
  유실돼도 중복 페이지는 **생기지 않는다** — 진실이 Notion이기 때문.

뉴스도 같은 구조 (뉴스 DB 조회가 진실).

`days_back: 60`이므로 매달 60일 창을 다시 훑는다. 후보 대부분이 "이미 본 것"이고,
그걸 걸러내는 정확도가 곧 비용이다.

---

## 5. 설정 파일 구조

```
config.yaml              lab 소유 — 소스, 모델, 뉴스, 상한, Notion 부모 페이지
members/
  jaebeom.yaml
  sample-search.yaml
  sample-webdata.yaml
state.json               Notion 좌표 캐시 (뉴스 DB + 멤버별 페이지/DB)
state/scored/<id>.json   멤버별 채점 캐시
```

`config.yaml`에서 **제거**되는 것: `keywords`, `research_profile`, `top_n`,
`notion_database_id` — 전부 멤버 소유이거나 `state.json`이 관리한다.

`config.yaml`에 **추가**되는 것:

```yaml
members_dir: "members"

limits:                  # 공용 키를 쓰므로 신뢰가 아니라 코드로 강제
  max_members: 15
  max_top_n_per_member: 30
  max_notes_per_run: 400
```

상한 위반은 **경고가 아니라 실행 거부**다. 공용 키에서 한 명의 오타(`top_n: 300`)가
전원의 예산을 태우는 걸 막는 유일한 방법이다.

---

## 6. 비용

| 항목 | 계산 | 월간 |
|---|---|---|
| 노트 (멤버별) | 10명 × 20편 × $0.0148 | $2.96 |
| 채점 (멤버별) | 10명 × 5배치 × $0.0061 | $0.31 |
| 뉴스 (공용) | 20편 × $0.0148 | $0.30 |
| **합계** | | **≈ $3.6 / 월** |

주 1회에서 월 1회로 바뀌면서 실행 횟수가 1/4이 됐다. 인당 상한을 없앤 만큼 편수는
늘 수 있지만, 컷오프를 넘는 논문 자체가 월 수십 편 규모라 상한이 있던 때와 크게
다르지 않다. 폭주 방지는 `limits.max_notes_per_run`이 실행 중에 건다.

겹치는 논문은 사람 수만큼 노트 비용이 나간다. 아낄 수 있지만 **아끼지 않는다** —
월 $14에서 몇 달러 줄이자고 공용 요약/개인 연결점 분리 구조를 넣을 이유가 없다.

신규 멤버 백필(1년, 상한 200편): 인당 약 $3.6 (1회성).

---

## 7. 구현 범위

### 신규

| 파일 | 역할 |
|---|---|
| `paper_digest/members.py` | 멤버 YAML 로딩·검증·상한, 멤버별 설정 주입 |
| `paper_digest/notion_api.py` | Notion 전송 계층 — 스로틀 + 429/5xx 재시도 + 상태 캐시 |
| `paper_digest/notion_query.py` | 개인 DB / 뉴스 DB 조회 → 기존 항목 인덱스 (진실 계층) |
| `members/*.yaml` | 멤버 정의 |
| `tests/notion_fake.py` | 인메모리 Notion — 실제 상태를 들고 있어서 "각자 자기 DB"를 검증 가능 |
| `.github/workflows/ci.yml` | push 시 테스트 + 린트 + 멤버 파일 검증 |

### 수정

| 파일 | 변경 |
|---|---|
| `config.py` | `keywords`/`research_profile`/`top_n` 제거, `members_dir` + `limits` 추가 |
| `notion_writer.py` | 멤버 페이지·개인 DB·뉴스 DB 생성, 스키마 2종 (논문/뉴스) |
| `keywords.py` | `select_for_keywords` 추가 — 멤버별 **독립 복사본** 반환 |
| `pipeline.py` | `run_monthly` = 수집 1회 + 뉴스 1회 + 멤버 순차 루프 |
| `dedup.py` | 채점 캐시 전용으로 축소 (경로 파라미터화) |
| `__main__.py` | `members list` / `members validate`, `run --member <id>` |
| `reporter.py` | 멤버별 집계 |
| `monthly.yml` | 상태 커밋 경로 변경, 커밋 게이트 `if: always()` |

### 변경 없음

`venues.py` · `ranking.py` · `notes.py` · `news_select.py` · `llm/*` —
멤버별 설정은 `dataclasses.replace(cfg, ...)`로 주입하므로 이 모듈들은
멀티테넌트를 몰라도 된다.

`collectors/semantic_scholar.py`는 한 곳만 손봤다. 배치가 실패해 통째로 빠질 때
**어떤 venue를 잃었는지 이름을 대도록** 했다. 기존 로그는 `gave up on SIGGRAPH…`
였고 같은 요청에 들어있던 나머지 24곳을 숨겼는데, venue 목록이 곧 커버리지인
구조에서 그건 "조용한 주"와 "SIGIR가 응답하지 않은 주"를 구분할 수 없게 만든다.
(무인증 Semantic Scholar는 짧은 시간에 반복 수집하면 스로틀을 404로 돌려준다.)

### 제거

`run_batch` / `batch.yml` — 프리프린트 소스가 없으므로 "프리프린트에 게재 확정을
찍는" 모드가 할 일이 없다.

---

## 8. 단계

### Phase 1 — 멀티테넌트 골격 ✅ (이번 작업)

- [x] `members.py` + 샘플 멤버 3명 (연구축 1 / 3 / 5)
- [x] `config.py` 분리, `limits` 강제 (경고 아니라 실행 거부)
- [x] `notion_api.py` — 스로틀 + 429/5xx 재시도
- [x] `notion_writer.py` — 멤버 페이지/개인 DB, 뉴스 DB, 스키마 2종
- [x] `notion_query.py` — 진실 계층
- [x] `pipeline.py` — 수집 1회 + 뉴스 1회 + 멤버 순차 (실패 격리)
- [x] CLI (`members list` / `validate`, `run --member`) + `ci.yml`
- [x] 테스트 338개 / 커버리지 88% / 린트 통과

**검증 결과**

| 무엇을 | 어떻게 | 결과 |
|---|---|---|
| 수집이 인원과 무관하게 1회인가 | 실제 Semantic Scholar 수집 | 675편 (학회 475 + 저널 200), 요청 6회 |
| 멤버별 후보가 갈리는가 | 실제 members 3명 × 실제 풀 | 유재범 54 / 검색RAG 188 / 웹데이터 20 |
| 한 멤버의 결과가 다른 멤버로 새지 않는가 | 공유 풀 오염 검사 + 점수 주입 검사 | 675/675 미오염, 객체 공유 0 |
| 각자 자기 DB에 들어가는가 | 인메모리 Notion 통합 테스트 | 멤버 3명 각자 페이지·DB, 교차 0 |
| 한 명 실패가 격리되는가 | 1명 Notion 실패 주입 | 나머지 2명 정상 수신, exit 1 |
| 상태 파일 유실이 중복을 만드는가 | 채점 캐시·state.json 삭제 후 재실행 | 재작성 0건 |

> **주의**: 검색RAG 샘플이 풀의 28%(188/675)를 잡습니다. `"information retrieval"`
> 단독 키워드가 SIGIR 논문 대부분에 걸리기 때문입니다. 랭킹이 top_n으로 잘라내므로
> 결과 품질은 유지되지만 채점 비용이 4배입니다 — Phase 2 `validate`가 잡아야 할
> 정확히 이 패턴입니다.

### Phase 2 — 운영자 도구

- [ ] `members validate` 드라이런 — 최근 후보 수, 상위 10편, 예상 비용
- [ ] `members draft` — `research_profile`만 받아 LLM이 키워드 DSL 초안 생성

**근거**: 운영자가 남의 연구 주제를 듣고 키워드 DSL을 감으로 쓰면 절반은 어긋난다.
`config.yaml`의 기존 키워드 주석이 그 증거다 — "speculative decoding 논문까지
통과했습니다", "representation learning이 통째로 걸립니다". 멤버에게 요구할 것은
연구 소개 문단 하나여야 한다.

### Phase 3 — 실전환

- [ ] 토큰 회전 + 리포지토리 private 전환 **(코드보다 먼저)**
- [ ] 연구실 워크스페이스에 통합 생성 → 메인 페이지 연결
- [ ] 멤버 10명 YAML 등록 (validate로 다듬어서)
- [ ] 전원 백필 1회 (순차, 인당 상한 200)
- [ ] `ci.yml` 추가 — 테스트가 push 시 돌지 않는 상태

### Phase 4 — 참여 유지

- [ ] **Slack 월간 알림** ("이번 달 N편, 2명 이상이 받은 논문은 …")
- [ ] 👍/👎 체크박스 → 다음 주 랭킹 프롬프트에 few-shot 주입

Slack을 선택 사항으로 두지 않는다. 개인용일 땐 본인이 만든 거니까 보지만, 남이
만들어준 Notion DB는 알림이 없으면 아무도 열지 않는다. 첫 주에 알림이 없으면
그 다음은 없다. **참여 저하가 이 프로젝트의 최대 리스크다.**

---

## 9. 리스크

| 리스크 | 심각도 | 대응 |
|---|---|---|
| **참여 저하** — 조용히 쌓이면 2주 만에 사장 | 높음 | Phase 4 Slack 알림. 기술보다 이게 성패를 가름 |
| 키워드 품질 (절반이 어긋남) | 높음 | Phase 2 `validate` / `draft`가 유일한 방어선 |
| **venue 목록이 곧 커버리지** | 중 | 화이트리스트에 없는 곳은 영영 안 들어온다. 멤버 등록 시 후보 수로 드러남 — 0에 가까우면 키워드가 아니라 `venues.csv` 문제 |
| Notion 429 | 중 | 단일 프로세스 직렬 + 0.34초 스로틀 + `Retry-After` 존중 |
| S2 초록 커버리지 불균일 | 중 | Elsevier 계열(IPM 256편 중 초록 7편)은 볼륨 대비 기여가 작다. 구조적 한계이고 대체 소스가 없다 |
| 공용 키 남용 | 중 | `limits`를 코드로 강제, 위반 시 실행 거부 |
| 멤버 수 증가에 따른 실행 시간 | 낮 | 순차 처리라 인원에 선형. 10명 ≈ 25분, Actions 상한 45분. 15명 넘으면 그때 분할 |

---

## 10. 미결정

1. **멤버 졸업/퇴사** — `enabled: false`로 수집만 중단하고 페이지는 남기는 방향
2. **복수 연구 주제** — 보류. 필요해지면 `topics:` 리스트 + 주제별 DB
3. **venue 목록 소유권** — "내 분야 저널 추가" 요청을 PR로 받을지 이슈로 받을지
4. **백필 시점** — Phase 3에서 10명 일괄인지, 등록될 때마다 개별인지
