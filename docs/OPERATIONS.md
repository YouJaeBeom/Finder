# 운영 가이드

> 이 도구를 굴리는 사람이 알아야 할 것들. 처음 설치는 [README](../README.md)에, 프로그램 소개는 [OVERVIEW.md](OVERVIEW.md)에 있습니다.

---

## 0. 한 줄 답 — 자동으로 만들어지는가

**네.** Notion 토큰과 부모 페이지 ID가 한 번 설정돼 있으면, 그 뒤로 운영자가 Notion을 열 일은 없습니다.

```
members/minsu.yaml 추가  →  push  →  다음 실행에서 자동으로:
                                      📄 김민수 페이지 생성
                                      └── 📚 논문 DB 생성 (컬럼 9개까지 포함)
                                          그 사람 논문이 여기에 쌓임
```

Notion에서 **사람이 손으로 하는 일은 최초 1회 두 가지뿐**입니다: 빈 페이지 하나 만들기, 그 페이지를 통합(integration)에 연결하기. 그 아래 구조는 전부 코드가 만듭니다.

| 자동으로 되는 것 | 안 되는 것 (사람이 함) |
|---|---|
| 멤버 페이지 생성 (`👤 이름`) | 부모 페이지 생성 + 통합 연결 (최초 1회) |
| 멤버별 `📚 논문` DB 생성 | `members/<id>.yaml` 작성 |
| 공용 `📰 IT 뉴스` DB 생성 | GitHub Secrets 등록 (최초 1회) |
| DB 컬럼(스키마) 생성·보완 | `venues.csv`에 없는 분야 추가 |
| 이미 받은 논문 중복 방지 | Notion 뷰·정렬·필터 취향 설정 (각자) |

> 왜 이렇게 만들었나: Notion API는 **뷰(view)를 만들 수 없습니다.** 공용 DB + 개인별 linked view 구조를 쓰면 사람이 늘 때마다 운영자가 Notion UI를 열어야 합니다. 사람마다 DB를 따로 두는 지금 구조는 API만으로 끝나서, 멤버 추가가 파일 하나로 완결됩니다.

---

## 1. 최초 1회 준비물

| 항목 | 어디에 넣나 | 비고 |
|---|---|---|
| Notion 통합 토큰 | GitHub Secrets `NOTION_TOKEN` | https://www.notion.so/my-integrations |
| 부모 페이지 ID | `config.yaml`의 `notion_parent_page_id` | 페이지 링크를 그대로 붙여넣어도 됨 |
| 부모 페이지 ↔ 통합 연결 | Notion 페이지 `···` → Connections | **이걸 빼먹는 게 가장 흔한 실수** |
| LLM 키 | GitHub Secrets `OPENAI_API_KEY` | `llm.provider: "anthropic"`이면 `ANTHROPIC_API_KEY` |

GitHub Secrets 경로: 저장소 → Settings → Secrets and variables → Actions → New repository secret.

준비가 됐는지 확인하는 가장 싼 방법 — Notion만 건드리고 LLM은 부르지 않습니다:

```bash
NOTION_TOKEN=... python -m paper_digest init
```

부모 페이지가 통합에 연결돼 있지 않으면 **2초 만에** 그렇게 말하고 멈춥니다. 수집을 다 하고 비용을 쓴 뒤에 알게 되는 것을 막으려고 일부러 맨 앞에 둔 검사입니다.

---

## 2. 멤버 등록 — 처음부터 끝까지

### 2-1. 구성원에게 받을 것 (셋)

1. **표시 이름** — Notion 페이지 제목이 됩니다. 예: `김민수`
2. **연구 소개 2~6문장** — 아래 세 가지가 들어가야 합니다.
   - 무엇을 연구하는지
   - 구체적으로 어떤 세부 주제에 관심이 있는지 (번호로 3~4개)
   - **관련도가 낮은 것은 무엇인지** ← 이게 빠지면 채점이 헐거워집니다
3. **1차 필터 검색식** — 본인이 논문 검색할 때 쓰는 검색식. 못 쓰겠다고 하면 운영자가 초안을 써서 확인만 받아도 됩니다.

> 구성원에게 그대로 보내도 되는 요청 양식:
>
> ```
> 1) Notion에 표시될 이름:
> 2) 연구 소개 (2~6문장, "이런 건 관련 없다"까지 포함):
> 3) 논문 검색할 때 쓰는 검색어들 (구/단어 나열이면 충분합니다):
> ```

### 2-2. 파일 만들기

`members/<id>.yaml` — 파일명(확장자 제외)이 **멤버 ID**입니다. 영문 소문자·숫자·`-`·`_`만 씁니다. Notion에는 안 나타나고, 캐시 파일 이름과 `--member` 옵션에만 쓰입니다.

```yaml
name: "김민수"

research_profile: |
  (받은 연구 소개를 그대로. 관련도 채점과 "내 연구와의 연결점" 작성에 함께 쓰입니다)

  관련도가 낮은 것: (여기까지 꼭)

query: |
  # 이 분야 고유 표현 — 단독으로도 충분히 좁습니다
  "counterfactual explanation" OR "model interpretability"

  # 넓은 단어는 주제어와 묶습니다
  OR ( (LLM OR "large language model")
       AND (explanation OR interpretab* OR transparency) )
```

선택 항목: `top_n: 20`(월 상한, 없으면 무제한) · `min_relevance: 6`(컷오프 상향) · `enabled: false`(일시 중단).

검색식 문법은 [README의 "1차 필터 쿼리"](../README.md#1차-필터-쿼리-and--or--not) 또는 `members/jaebeom.yaml` 상단 주석에 전부 있습니다. 요약하면 `AND`/`OR`/`NOT`(대문자만) · 괄호 · 붙은 단어는 구 · `polariz*` 잘라 쓰기 · `#` 주석.

**검색식은 느슨하게.** 정밀하게 쓰면 채점 모델이 8~9점을 줬을 논문이 여기서 잘립니다(실측 사례가 각 멤버 파일 주석에 있습니다). 애매하면 넣는 쪽이 맞습니다.

### 2-3. 검증 (시크릿 없이 됩니다)

```bash
python -m paper_digest members validate
```

- 문제가 있으면 **파일 이름과 함께 전부** 알려주고 종료 코드 1. 하나 고칠 때마다 다음 문제를 발견하는 일이 없습니다.
- 문제가 없으면 **파서가 실제로 묶은 형태**를 출력합니다. 괄호를 하나 빠뜨린 건 원문에선 안 보이고 여기선 보입니다.

```bash
python -m paper_digest members list      # 등록된 사람과 예상 분량
```

### 2-4. push

CI가 push마다 테스트·린트와 함께 멤버 파일을 다시 검증합니다.

> ⚠️ **깨진 멤버 파일 하나가 그 달 전원의 실행을 막습니다.** 멤버 로딩은 수집 전에 한 번에 이뤄지고, 문제가 있으면 실행이 시작조차 하지 않습니다. 이건 의도된 동작이고(조용히 한 사람을 빼고 도는 것보다 낫습니다), 그래서 CI 검증이 있습니다. 초록불을 확인하고 넘어가세요.

### 2-5. 첫 페이지 생성

셋 중 아무거나 고르면 됩니다.

| 방법 | 언제 |
|---|---|
| 그냥 기다린다 | 다음 달 1일 정기 실행이 알아서 만듭니다 |
| `python -m paper_digest init` | 지금 바로 페이지만 만들어 보여주고 싶을 때 (LLM 비용 0) |
| Actions → Monthly paper digest → Run workflow, `member`에 ID | 그 사람 것만 지금 한 번 돌릴 때 |

### 2-6. 신규 인원의 지난 논문 채우기 (백필)

정기 실행은 "지난 60일에 나온 것"만 봅니다. 새로 합류한 사람에게 지난 1년치를 채워주려면:

**Actions → Backfill → Run workflow**

| 입력 | 권장값 | 이유 |
|---|---|---|
| `days` | `365` | |
| `member` | **그 사람 ID** | 비워두면 이미 채운 사람들의 catch-up 비용을 다시 냅니다 |
| `limit` | `200` | 그 사람에게 남길 상위 편수 |
| `sources` | `conferences` | 저널은 매월 실행이 알아서 따라잡지만, 학회 프로시딩은 1년에 한 번 떨어지고 그 순간을 놓치면 복구가 안 됩니다 |

백필은 **랭킹한 논문을 전부 캐시에 기록합니다** — 쓴 것만이 아니라 떨어뜨린 것까지. 두 번 돌려도 두 번째는 아무 일도 하지 않습니다. 대신 1년치는 모델 호출 수백~수천 번이라 실행이 몇 시간 걸릴 수 있습니다(타임아웃 300분). 시작할 때 예상 비용을 로그에 찍습니다.

---

## 3. 이름·ID를 바꾸거나 사람이 나갈 때

Notion 페이지는 **제목으로 찾습니다.** 도구는 "이름 변경"과 "다른 사람 합류"를 구분할 수 없기 때문입니다.

| 하는 일 | 일어나는 일 |
|---|---|
| `name` 변경 | **새 페이지가 생깁니다.** 기존 페이지는 그대로 남고 새 글만 새 페이지로 갑니다. 이전 것을 이어가려면 Notion에서 페이지 제목을 직접 바꾼 뒤 YAML을 맞추세요 |
| 파일명(ID) 변경 | 채점 캐시가 새로 시작됩니다(`state/scored/<id>.json`). 중복 페이지는 생기지 않습니다 — 중복 판단은 Notion DB를 직접 조회하기 때문입니다 |
| 졸업·휴직 | `enabled: false`. 페이지와 지금까지 쌓인 논문은 그대로 남습니다 |
| 완전히 제거 | 파일 삭제. Notion 페이지는 코드가 지우지 않습니다 (수동으로 지우세요) |

---

## 4. 매달 결과 확인하기

정기 실행은 **매월 1일 09:00 KST**(00:00 UTC)입니다.

**Actions 탭 → 해당 실행 → Summary** 에 이 표가 붙습니다.

```
| member | candidates | new | written | error |
| 유재범   |        476 |  31 |      26 |       |
```

- `candidates` 검색식을 통과한 수 · `new` 그중 처음 보는 것 · `written` 노트를 쓴 것
- **written이 0인 사람이 있는지**가 볼 곳입니다. 조용한 달일 수도 있지만, 계속 0이면 원인이 있습니다(§6)

실행이 실패하면 GitHub이 메일을 보냅니다. 종료 코드 1은 "한 명이라도 실패"이고, 아무도 새 논문이 없는 조용한 달은 **정상 종료**입니다 — 없는 문제로 알림을 보내지 않습니다.

`run-report.json`이 실행마다 아티팩트로 올라갑니다(멤버별 수치, 실패 사유, 두 명 이상이 받은 논문 목록).

---

## 5. 비용과 상한

| 항목 | 실측 |
|---|---|
| 노트 1편 | 약 $0.0115 |
| 채점 20편(1배치) | 약 $0.006~0.013 |
| 3명 실측 | 인당 채점 $0.15~0.20 |
| 10명 기준 월 | 약 $6 |

지출은 코드가 막습니다. 넘으면 **경고가 아니라 실행 거부**입니다.

| 설정 | 기본값 | 뜻 |
|---|---|---|
| `limits.max_members` | 15 | 등록 가능 인원 |
| `limits.max_top_n_per_member` | 30 | 한 사람이 요구할 수 있는 월 상한 |
| `limits.max_notes_per_run` | 600 | 실행 전체 노트 수 |
| `max_papers_to_rank` | 5000 | 한 사람이 한 실행에서 채점할 논문 수 |

`max_notes_per_run`은 멤버들에게 **균등 배분**되고 적게 쓴 사람의 몫은 다음 사람에게 넘어갑니다. 선착순으로 두면 멤버 순서가 고정이라 매달 같은 사람이 손해를 보기 때문입니다.

---

## 6. 문제별 대응

| 증상 | 원인과 대응 |
|---|---|
| `parent page ... is not visible to this integration` | 부모 페이지를 통합에 연결하지 않았습니다. Notion 페이지 `···` → Connections |
| `NOTION_TOKEN is not set` | 저장소 Secret 누락. Actions에서 Secret은 fork된 PR에는 전달되지 않습니다 |
| 특정 멤버만 계속 `written: 0` | ① 검색식이 너무 좁다 → `members validate`로 묶인 형태 확인 ② 연구 소개가 짧아 채점이 5점을 안 준다 ③ **그 분야 venue가 목록에 없다** (§7) |
| 한 사람이 월 100편 넘게 받음 | 그 사람 파일에 `min_relevance: 6~7` 또는 `top_n` 추가 |
| `'query' could not be read` | 검색식 오류. 메시지가 캐럿(`^`)으로 자리를 짚어줍니다. 대개 소문자 `and`/`or` 또는 괄호 짝 |
| `This run would write up to N notes, over the lab limit` | 인원이 늘었습니다. 누군가의 `top_n`을 낮추거나 `limits.max_notes_per_run`을 **의식적으로** 올리세요 |
| 실행이 타임아웃(180분) | 결과물은 Notion에 이미 들어간 만큼 남습니다. 다음 실행이 이어서 하고, 중복은 생기지 않습니다 |
| 백필 push 실패 | 실행 중 다른 커밋이 들어온 경우입니다. 3회까지 rebase 재시도하고, 그래도 실패하면 채점 캐시만 잃습니다(재실행 시 재채점 비용) |

---

## 7. venue 목록 — 새 분야가 들어올 때

**목록에 없는 학회·저널의 논문은 누구에게도 영영 들어오지 않습니다.** 새 구성원이 받는 논문이 0에 가까우면 프로필 문제가 아니라 `paper_digest/data/venues.csv` 문제입니다.

추가할 때 필요한 것: `abbr,query,name,dblp,score,papers,kind`. 이 중 **`name`은 Semantic Scholar가 실제로 돌려주는 문자열이어야** 하고(다르면 약칭 변환이 깨집니다), `papers`가 0이면 그 행은 조용히 수집에서 빠집니다. 손으로 넣지 말고 API로 확인한 값을 넣으세요.

venue 목록은 **연구실 공용**입니다. 개인이 자기 파일에서 바꿀 수 없습니다 — 수집이 인원과 무관하게 1회라 구조적으로 불가능합니다.

---

## 8. 상태 파일과 git

| 파일 | 무엇 | 잃으면 |
|---|---|---|
| `state.json` | Notion 페이지·DB ID 캐시 | 제목으로 다시 찾습니다. 조회 한 번의 비용 |
| `state/scored/<id>.json` | 채점하고 떨어뜨린 논문 기록 | 다시 채점합니다. 비용만 들고 중복 페이지는 안 생깁니다 |

**진실은 Notion DB 자체입니다.** 상태 파일은 최적화이지 진실이 아니라서, 지워도 결과가 틀어지지 않습니다.

주의: 실행이 끝나면 GitHub Actions가 이 파일들을 **저장소에 커밋·push합니다.** 로컬에서 작업하기 전에 `git pull` 하세요. Monthly와 Backfill은 같은 concurrency 그룹이라 동시에 돌지 않습니다.

---

## 9. 시크릿 관리

- 토큰을 **채팅·이슈·커밋에 붙여넣지 마세요.** 들어갔다면 즉시 회전: Notion은 https://www.notion.so/my-integrations 에서 통합의 Secret을 재발급하고, 새 값을 GitHub Secret에 다시 넣습니다. 재발급하면 이전 토큰은 즉시 무효입니다.
- Notion 통합은 **연결된 페이지에만** 접근할 수 있습니다. 부모 페이지 하나만 연결하면 권한 범위가 그 하위로 제한됩니다.
- LLM 키는 provider 콘솔에서 회전합니다. 회전 후 GitHub Secret 갱신을 잊으면 다음 실행이 preflight에서 멈춥니다(수집 전이라 비용은 0).

---

## 10. 자주 바꾸는 설정

| 바꾸고 싶은 것 | 소유 | 어디 |
|---|---|---|
| 내가 받는 논문의 방향 | 개인 | `members/<id>.yaml`의 `research_profile` |
| 1차 필터 넓히기/좁히기 | 개인 | 같은 파일의 `query` |
| 내가 받는 편수 | 개인 | `top_n`, `min_relevance` |
| 잠시 안 받기 | 개인 | `enabled: false` |
| 수집 기간 | 랩 | `days_back` (기본 60. 넓혀도 LLM 비용은 안 늘어납니다 — 중복 제거가 채점 전에 돕니다) |
| 학회/저널 범위 | 랩 | `conferences.min_score` / `journals.min_score` / `include` / `exclude` |
| 뉴스 필터·개수·끄기 | 랩 | `news.query` / `news.top_n` / `news.enabled` |
| 뉴스 매체 추가 | 랩 | `news.rss_feeds`에 URL 추가 (코드 수정 불필요) |
| 모델·제공자 | 랩 | `llm.provider` / `ranking_model` / `notes_model` |
| 동시 실행 수 | 랩 | `llm.concurrency` (기본 12. 레이트 리밋이 낮으면 낮추세요) |

---

## 11. 로컬에서 확인하기

```bash
pip install -e ".[dev]"

python -m paper_digest members validate     # 시크릿 불필요
python -m paper_digest members list         # 시크릿 불필요
pytest -q                                   # 443개, 네트워크 차단됨

NOTION_TOKEN=... python -m paper_digest init                    # Notion 구조만
NOTION_TOKEN=... OPENAI_API_KEY=... \
  python -m paper_digest run --mode monthly --member <id>       # 실제 실행(비용 발생)
```

로컬 실행도 **진짜 Notion에 씁니다.** 시험만 할 거면 `init`까지만 하세요.
