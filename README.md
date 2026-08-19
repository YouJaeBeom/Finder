# Paper Digest

매주 월요일 아침, 새로 나온 논문과 IT 뉴스를 모아 한국어 노트로 정리해서 Notion에 쌓아주는 도구입니다. **연구실 여러 명이 각자의 연구 주제로 함께 씁니다** — 논문은 사람마다 자기 페이지의 자기 DB에, 뉴스는 공용으로 메인 페이지에 들어갑니다.

```
📄 워크스페이스 메인 페이지
├── 📰 IT 뉴스            공용 DB — 실행당 1회, 전원 공유
├── 📄 유재범              멤버 페이지 (코드가 생성)
│   └── 📚 논문            그 사람 전용 DB (코드가 생성)
├── 📄 김민수
│   └── 📚 논문
└── ...
```

멤버 추가는 **`members/<id>.yaml` 파일 하나 추가**로 끝납니다. Notion에서 손으로 할 일이 없습니다 — 공유 DB + 개인별 linked view 구조를 쓰지 않은 이유가 이것입니다. Notion API는 view를 만들 수 없어서, 그 구조에서는 멤버가 늘 때마다 운영자가 Notion UI를 열어야 합니다.

| | 논문 | 뉴스 |
|---|---|---|
| 출처 | **상위 학회 + 저널** (Semantic Scholar) | Hacker News, RSS 피드 |
| 범위 | **멤버별** (각자 키워드·프로필) | **공용 1회** |
| 거르는 방법 | 키워드 → **싼 모델로 관련도 0~10점** → 상위 N개 | 키워드만 (모델 안 씀) → 출처별 번갈아 → 상위 N개 |
| 노트 | 4섹션 (요약 / 핵심 기여 / 방법 / 내 연구와의 연결점) | 3섹션 (요약 / 핵심 내용 / 연결점) |
| 들어가는 곳 | 멤버 페이지 안의 `📚 논문` | 메인 페이지의 `📰 IT 뉴스` |

**수집은 인원과 무관하게 1회입니다.** Semantic Scholar에는 venue와 날짜만 물어보고, 키워드는 그 뒤에 로컬 문자열 매칭으로만 씁니다. 멤버가 1명이든 10명이든 외부 요청 수가 같습니다 — 늘어나는 건 멤버별 채점·노트 비용뿐입니다.

뉴스에 관련도 채점을 하지 않는 이유는 [news_select.py](paper_digest/news_select.py)에 적어뒀습니다. 요약하면, HN은 이미 커뮤니티 점수로 걸러졌고 RSS 피드는 직접 고른 것이라 소스 자체가 이미 큐레이션된 상태이기 때문입니다. 모델을 한 번 더 부르는 값만큼의 이득이 없습니다.

### 논문은 어디서 오나 — venue 화이트리스트가 전부입니다

논문은 **Semantic Scholar 한 곳**에서만 가져옵니다. API 키가 필요 없고, venue와 초록과 식별자를 한 번의 요청으로 함께 줍니다.

| 소스 | 무엇을 가져오나 | `min_score: 0.1` 기준 |
|---|---|---|
| 학회 | [venues.csv](paper_digest/data/venues.csv)의 학회 프로시딩 | 246곳 통과 → **228곳 조회** |
| 저널 | 같은 표의 저널 (TPAMI, TOIS, TACL, TKDE, JASIST, Political Communication …) | **28곳** |

**품질 장치는 이 화이트리스트 하나입니다.** 목록에 있는 곳의 논문만 들어옵니다. 주제 필터로 대신하려던 시도는 실패했습니다 — 자세한 건 아래 "왜 OpenAlex와 arXiv를 걷어냈나".

venue 목록은 **랩 소유**입니다. 멤버가 개인화할 수 없습니다 — 수집이 공용 1회라 구조적으로 불가능합니다. 그래서 화이트리스트에 없는 분야는 **누구에게도** 영영 들어오지 않습니다. 새 멤버의 후보 수가 0에 가까우면 키워드 문제가 아니라 `venues.csv` 문제입니다.

점수를 통과한 246곳 중 228곳만 실제로 조회합니다. Semantic Scholar 에 논문이 한 편도 없는 venue는 요청만 쓰고 0건을 돌려주므로 [venues.py](paper_digest/venues.py)가 제외합니다.

**`min_score`가 0.5가 아니라 0.1인 이유.** 점수는 한국 CS 종합 순위를 정규화한 값이라 NLP·IR·계산사회과학 학회를 낮게 잡습니다. 0.5 컷에서는 ICWSM(0.20) · NAACL(0.20) · EACL(0.30) · COLING(0.30) · RecSys(0.25) · CoNLL(0.20)이 **전부** 잘려나갔습니다. 편향·정보접근을 다루는 랩에서 이건 커버리지 구멍입니다. 넓힌 대신 정밀도는 전적으로 멤버별 키워드가 책임집니다.

FAccT · AIES · ECIR은 순위표에 아예 없어서 손으로 추가했습니다 (점수도 손으로 매김). `query`/`name`/`papers`는 전부 라이브 API로 실측한 값입니다 — `name`이 S2가 돌려주는 문자열과 다르면 약칭 변환이 깨지고, `papers`가 0이면 그 행은 조용히 수집에서 빠집니다. **TMLR은 추가할 수 없었습니다**: Semantic Scholar에 어떤 명칭으로도 venue가 존재하지 않습니다(OpenReview 전용).

> 학회 논문은 프로시딩이 나올 때 한꺼번에 들어옵니다. 조용한 주가 대부분이고, 어느 주에 갑자기 한 학회가 통째로 들어오는 게 정상입니다. 저널이 그 빈 주를 채웁니다.

### 왜 OpenAlex와 arXiv를 걷어냈나

**OpenAlex** — 2026년에 `from_created_date`와 `from_updated_date`가 유료 플랜 전용이 됐습니다. 주간 다이제스트에 필요한 건 "지난주 이후 색인된 것"이지 "지난주 이후 게재된 것"이 아닌데, 그 필터가 막혔습니다. 무료로 남은 게재일 창은 레거시 `concepts` 필터가 너무 헐거워서(30일에 724,000건, `primary_topic.field.id:17`은 71,000건) ML을 쓰는 응용과학이 전부 걸립니다. 실측했더니 통과 논문 1위 venue가 NLP 논문을 싣는 *Advanced Electromagnetics* 였습니다. venue 화이트리스트를 걸 거라면 Semantic Scholar 대비 남는 이점이 없어서 제거했습니다.

**arXiv** — 하루 수백 편 대 학회의 연 1회이므로, 켜두면 `top_n` 자리를 사실상 프리프린트가 전부 가져갑니다. 몇 달간 설정으로 꺼둔 상태였고, OpenAlex의 `is_core` 필터가 arXiv를 primary로 하는 논문을 이미 배제하고 있었기 때문에 코드를 지워도 잃는 경로가 없었습니다.

근거는 [collectors/semantic_scholar.py](paper_digest/collectors/semantic_scholar.py) 상단에 정리돼 있습니다.

---

## 처음 세팅하기

전체 20분 정도 걸립니다. 1~4번은 브라우저에서, 5번은 터미널에서 합니다.

### 1. Notion 준비

**(1) 통합(integration) 만들기**

1. https://www.notion.so/my-integrations 접속 → **New integration**
2. 이름은 아무거나 (예: `Paper Digest`), 워크스페이스 선택 → **Save**
3. **Internal Integration Secret** 을 복사 → 이게 `NOTION_TOKEN` 입니다

> ⚠️ 이 토큰은 채팅창이나 코드에 붙여넣지 마세요. 아래 5번에서 GitHub Secrets에만 넣습니다.

**(2) 부모 페이지 만들고 연결하기**

1. Notion에서 빈 페이지를 하나 만듭니다 (예: `연구 다이제스트`)
2. 페이지 우측 상단 `···` → **Connections**(연결) → 방금 만든 통합을 추가
3. 페이지 URL에서 ID를 복사합니다

```
https://www.notion.so/myworkspace/연구-다이제스트-2ba8a8477019801234567890abcdef12
                                                └──────────── 이 32자리가 페이지 ID ────────────┘
```

### 2. Anthropic API 키 발급

https://console.anthropic.com/settings/keys 에서 키를 만듭니다. 이게 `ANTHROPIC_API_KEY` 입니다.

> OpenAI를 쓰고 싶다면 [config.yaml](config.yaml)의 `llm.provider`를 `openai`로 바꾸고 `OPENAI_API_KEY`를 대신 등록하면 됩니다.

### 3. config.yaml — 랩 공용 설정

[config.yaml](config.yaml)에는 **랩 전체에 적용되는 것만** 있습니다. 바꿔야 하는 건 한 줄입니다.

```yaml
# 페이지 링크를 그대로 붙여넣어도 되고, 32자리 ID만 넣어도 됩니다
notion_parent_page_id: "https://www.notion.so/.../Finder-3bc1256e0561..."
```

DB ID를 적을 필요는 없습니다. `state.json`이 캐시하고, 캐시가 없으면 제목으로 다시 찾습니다.

### 4. members/ — 사람마다 파일 하나

개인의 연구 주제와 키워드는 여기 있습니다. 파일명(확장자 제외)이 **멤버 ID**이고, `name`이 **Notion 하위 페이지 제목**입니다.

```yaml
# members/jaebeom.yaml
name: "유재범"
enabled: true
top_n: 20                        # 주당 최대 페이지 수

research_profile: |
  본인 연구 주제를 2~6문장으로.
  이 내용으로 논문 관련도를 채점하고, "내 연구와의 연결점" 섹션도 씁니다.

keywords:
  - "political bias"
  - all: [["LLM", "language model"], ["robustness", "consistency"]]
    not: ["survey"]
```

**연구 주제는 한 명당 1개입니다.** 프로필 하나, 키워드 한 묶음. 등록하고 나면 확인:

```bash
python -m paper_digest members list        # 등록된 멤버와 예상 분량
python -m paper_digest members validate    # 전 파일 검사 — 문제를 한 번에 전부
```

`validate`는 Notion도 LLM도 건드리지 않으므로 시크릿 없이 돌아갑니다. 문제가 있으면 파일 이름과 함께 **전부** 알려줍니다 — 하나 고칠 때마다 다음 문제를 발견하는 건 주간 스케줄에서 한 달을 날리는 방법입니다.

동봉된 샘플 3명([jaebeom](members/jaebeom.yaml) · [sample-search](members/sample-search.yaml) · [sample-webdata](members/sample-webdata.yaml))을 복사해서 쓰면 됩니다.

### 5. GitHub 설정 — 두 가지

리포지토리: https://github.com/YouJaeBeom/Finder

**(1) 시크릿 등록**

`Settings` → `Secrets and variables` → `Actions` → `New repository secret`

| Name | Secret |
|---|---|
| `NOTION_TOKEN` | 1번에서 복사한 통합 시크릿 |
| `ANTHROPIC_API_KEY` | 2번에서 발급한 키 |

> 논문 수집(Semantic Scholar)에는 API 키가 필요 없습니다. 필요한 시크릿은 위 두 개뿐입니다.

**(2) 워크플로 쓰기 권한 켜기** ← 이걸 안 하면 실행이 실패합니다

`Settings` → `Actions` → `General` → 맨 아래 **Workflow permissions**
→ **Read and write permissions** 선택 → **Save**

매주 채점 캐시(`state/scored/*.json`)를 봇이 커밋해야 하는데, GitHub 기본값이 읽기 전용이라 이 설정이 필요합니다.

> 캐시가 유실돼도 **중복 페이지는 생기지 않습니다.** "이미 받았나"의 진실은 멤버의 Notion DB를 조회해서 답합니다 ([notion_query.py](paper_digest/notion_query.py)). 캐시는 "이미 채점하고 떨어뜨렸나"만 기억하므로, 잃으면 싼 모델 재채점 비용(센트 단위)만 듭니다.

### 6. Notion 구조 만들기 (로컬, 1회)

```bash
pip install -r requirements.txt
pip install -e .

export NOTION_TOKEN="..."          # 1번의 통합 시크릿

python -m paper_digest init        # 뉴스 DB + 멤버 페이지·DB 전부 생성
```

`init`은 LLM 키가 필요 없고, 몇 번 돌려도 같은 것을 다시 찾습니다. 성공하면 이렇게 나옵니다.

```
News database ready: 2ba8a847-7019-...
유재범 → page 2ba8a847-..., database 2ba8a847-...
샘플-검색RAG → page ..., database ...
샘플-웹데이터 → page ..., database ...
```

Notion을 열어보면 메인 페이지에 `📰 IT 뉴스` 하나와 멤버 페이지들이 생겨 있고, 각 멤버 페이지 안에 `📚 논문`이 있습니다. **여기까지 손으로 만든 게 하나도 없습니다.**

첫 스케줄 실행 전에 `init`을 돌려두는 걸 권합니다 — "월요일에 아무것도 안 생겼는데 이유를 모르겠다"를 지금 바로 읽는 에러로 바꿉니다. 수집도 지출도 없습니다.

### 7. 동작 확인

GitHub `Actions` 탭 → `Weekly paper digest` → **Run workflow**.

- 초록 체크 → 실행 페이지의 **Summary**에 멤버별 표(후보 / 신규 / 작성)가 뜹니다. 두 명 이상이 받은 논문도 여기 나옵니다
- 빨간 X → Summary의 실패 사유 또는 `run-report.json` 아티팩트의 `error`

한 명만 다시 돌리려면 Run workflow의 `member` 입력에 그 사람 ID를 넣으면 됩니다.

이후로는 **매주 월요일 08:00 (KST)** 에 자동으로 돕니다.

---

## 평소 사용법

무엇이 **랩 소유**이고 무엇이 **개인 소유**인지가 이 표의 축입니다.

| 하고 싶은 것 | 어디서 | 방법 |
|---|---|---|
| **멤버 추가** | 개인 | `members/<id>.yaml` 추가 → `members validate` → push. Notion 작업 없음 |
| **멤버 졸업/휴지** | 개인 | 그 파일에 `enabled: false`. 기존 페이지는 그대로 남습니다 |
| 내 키워드 바꾸기 | 개인 | `members/<id>.yaml`의 `keywords` (아래 문법 참고) |
| 내 연구 소개 바꾸기 | 개인 | `members/<id>.yaml`의 `research_profile` |
| 내가 받는 논문 수 | 개인 | `members/<id>.yaml`의 `top_n` (랩 상한 `limits.max_top_n_per_member`) |
| 학회/저널 범위 넓히기·좁히기 | 랩 | `conferences.min_score` / `journals.min_score` (기본 0.5, 1.0이면 최상위만) |
| 특정 venue 강제 포함/제외 | 랩 | `conferences.include` / `journals.exclude` 등에 약자 추가 |
| 저널만 끄기 (학회만 보기) | 랩 | `journals.enabled: false` |
| 수집 기간 조정 | 랩 | `days_back` (기본 30). 넓혀도 LLM 비용은 안 늘어납니다 — 중복 제거가 랭킹 전에 돌기 때문입니다 |
| LLM을 ChatGPT로 바꾸기 | 랩 | `llm.provider: "openai"` + `OPENAI_API_KEY` 시크릿 등록 |
| 뉴스 소스 추가 | 랩 | `news.rss_feeds`에 피드 URL 추가 (코드 수정 불필요) |
| 뉴스 키워드 / 개수 / 끄기 | 랩 | `news.keywords` (빈 리스트 = 전부) / `news.top_n` / `news.enabled: false` |
| 뉴스 브리핑의 관점 | 랩 | `lab_profile` — 뉴스는 공용이라 "누구의 연구와의 연결점"을 쓸 대상이 없습니다. 이 글이 그 자리입니다 (비우면 멤버 프로필을 이어붙임) |
| 지출 상한 조정 | 랩 | `limits.max_notes_per_run` 등. 넘으면 **경고가 아니라 실행 거부** |
| 지금 당장 한 번 돌리기 | — | Actions 탭 → `Weekly paper digest` → Run workflow |
| **한 사람만** 다시 돌리기 | — | 같은 화면의 `member` 입력에 멤버 ID |
| **지난 1년치 한 번에 채우기** | — | Actions 탭 → `Backfill` (아래 참고) |

`keywords`는 멤버 소유이지만 `conferences`/`journals`는 랩 소유입니다. 수집이 공용 1회이기 때문에 venue는 구조적으로 개인화할 수 없습니다 — 개인이 조절하는 건 키워드·프로필·`top_n` 셋뿐이고, 그게 맞는 경계입니다.

### 상한은 왜 거부인가

랩은 API 키 하나를 공유합니다. 그래서 누군가의 `top_n: 300`은 그 사람 비용이 아니라 **전원의 비용**입니다. 조용히 30으로 깎으면 그 사람은 파일에 쓴 것보다 적게 받으면서 그 사실을 알 방법이 없으므로, 상한 위반은 파일 이름을 대며 실행을 멈춥니다.

### 처음 한 번 — 백필 (지난 논문 채우기)

주간 실행은 "지난주 이후에 나온 것"만 묻습니다. 도구를 8월에 세팅했다면 봄에 나온 ACL·CHI 프로시딩은 영영 안 들어옵니다. 백필은 긴 기간을 한 번에 랭킹해서 그중 상위 N편만 남깁니다.

`Actions` → `Backfill` → Run workflow:

| 입력 | 뜻 | 추천 |
|---|---|---|
| `days` | 며칠 전까지 볼지 | `365` |
| `member` | 한 사람만 채울지 (비우면 전원) | 신입 1명이면 그 ID |
| `limit` | **멤버당** 상위 몇 편을 남길지 | `200` |
| `sources` | `conferences` / `journals` / `both` | **`conferences`** |

`sources`를 `conferences`로 두는 걸 권합니다. 저널은 꾸준히 나오니 주간 실행이 알아서 따라잡지만, 학회 프로시딩은 1년에 한 번 떨어지고 그 순간을 놓치면 주간 실행으로는 복구가 안 됩니다.

`member`를 비워두면 전원을 순차로 채웁니다. **한 명이 기존 랩에 합류할 때는 그 사람 ID를 넣으세요** — 비워두면 이미 채운 사람들의 catch-up 비용을 한 번 더 냅니다.

백필은 **랭킹한 논문을 전부 캐시에 기록합니다** — 쓴 것만이 아니라 떨어뜨린 것까지. 그래서 두 번 돌려도 두 번째는 아무 일도 안 하고, 이후 주간 실행은 진짜 새 논문만 봅니다. 대신 1년치를 멤버 수만큼 랭킹하는 건 수백~수천 번의 모델 호출이라 실행이 몇 시간 걸릴 수 있습니다 (워크플로 타임아웃 300분). 실행 시작 시 예상 비용을 로그에 찍습니다.

### 키워드 문법 (AND / OR / 제외)

목록의 **항목끼리는 OR**입니다. 항목 하나는 아래 네 형태 중 하나입니다.

```yaml
keywords:
  - "instruction tuning"           # 이 구절이 있으면 매치

  - all: ["LLM", "alignment"]      # 둘 다 있어야 매치

  - any: ["RLHF", "DPO"]           # 하나라도 있으면 매치

  - all:                           # 중첩 리스트 = "이 중 하나"
      - ["LLM", "large language model"]   #   (모델 용어 중 하나)
      - ["alignment", "safety"]           #   AND (주제 용어 중 하나)
    not: ["survey"]                # 단, survey 가 있으면 제외
```

대소문자는 무시하고, **제목과 초록을 합쳐서** 봅니다.

`"LLM"` 하나만 넣으면 요즘 NLP 논문 대부분이 걸립니다 — 넓은 단어는 `all`로 주제어와 묶으세요. 실제로 지금 설정에서:

```
통과  Scaling LLM alignment with human feedback      ['LLM', 'alignment']
제외  LLM inference speedup with quantization        []
```

Notion `Tags` 컬럼에는 **실제로 맞은 단어**가 들어가므로, 왜 걸렸는지 나중에 확인할 수 있습니다.

### Venue 컬럼 (학회/저널 약칭)

Semantic Scholar는 학회를 정식 명칭으로 줍니다. Notion에는 약칭으로 들어갑니다.

| 원본 이름 | Venue 컬럼 |
|---|---|
| Proceedings of the 32nd ACM International Conference on Information and Knowledge Management | `CIKM` |
| Proceedings of the 47th International ACM SIGIR Conference on Research and Development... | `SIGIR` |
| IEEE Transactions on Knowledge and Data Engineering | `TKDE` |
| Annual Meeting of the Association for Computational Linguistics | `ACL` |

ACL · EMNLP · NAACL · EACL · TACL · COLING · SIGIR · CIKM · WSDM · KDD · RecSys · WWW · ECIR · TOIS · SIGMOD · VLDB · ICDE · TKDE · NeurIPS · ICML · ICLR · AAAI · IJCAI · JMLR · TMLR · CVPR · ICCV · ECCV · TPAMI 가 내장돼 있습니다 ([venues.py](paper_digest/venues.py)).

표에 없는 곳은 `config.yaml`에 추가하세요. 왼쪽은 정식 명칭의 **일부**입니다.

```yaml
venue_aliases:
  "Korea Software Congress": "KSC"
  "Workshop on Machine Learning for Systems": "MLSys Workshop"
```

무심사 저장소를 이름으로 걸러낼 필요는 없습니다 — 화이트리스트에 없으면 애초에 들어오지 않습니다.

저널은 **정확한 이름**을 요구합니다. Semantic Scholar의 venue 필터가 느슨해서, "Big Data & Society"를 요청하면 다른 저널인 "Big Data"가 섞여 오고 "Artificial Intelligence"는 "Artificial Intelligence Review"까지 끌고 옵니다. 학회는 반대로 정식 명칭에 회차·연도·개최지가 붙으므로 느슨한 매칭이 필요합니다. 둘을 다르게 다룹니다.

Venue에는 이름만 들어가고, 나머지는 별도 컬럼입니다.

| 컬럼 | 값 |
|---|---|
| `Venue` | `ACL`, `SIGIR`, `TKDE`, `TOIS` … |
| `Kind` | `conference` / `journal` |
| `Status` | `published` (화이트리스트의 모든 것이 심사를 거친 것이므로) |

> `preprint`가 사라진 것은 소스 통합의 결과입니다. arXiv를 제거하면서 프리프린트가 들어올 경로가 없어졌고, 그래서 "프리프린트에 게재 확정을 찍는" batch 모드도 함께 제거했습니다.

### LLM 제공자 바꾸기

Claude와 ChatGPT 둘 다 지원합니다. `config.yaml`에서:

```yaml
llm:
  provider: "openai"
  ranking_model: "gpt-4o-mini"    # 대량 랭킹 (싼 모델)
  notes_model: "gpt-4o"           # 노트 작성 (좋은 모델)
```

그리고 GitHub Secrets에 `OPENAI_API_KEY`를 등록하면 됩니다. "싼 모델로 거르고 좋은 모델로 정리한다"는 구조는 어느 쪽이든 같습니다.

### 로컬에서 직접 돌리기

```bash
# 시크릿 없이 — 멤버 설정만 검사
python -m paper_digest members list
python -m paper_digest members validate

# Notion만 필요
python -m paper_digest init

# 전부 필요 (NOTION_TOKEN + ANTHROPIC_API_KEY)
python -m paper_digest run --mode weekly
python -m paper_digest run --mode weekly --member jaebeom
python -m paper_digest run --mode backfill --days 365 --limit 200 --sources conferences
python -m paper_digest run --mode backfill --member newbie --days 365 --limit 200
```

---

## 종료 코드 규칙

Actions 실패 알림이 언제 오는지를 정하는 규칙입니다.

| 상황 | 코드 | 의도 |
|---|---|---|
| 전원 정상 | 0 | — |
| 어떤 멤버의 키워드 후보 0개 | 0 | 그 사람에게 조용한 주. 실패가 아님 |
| 채점은 됐는데 컷오프 통과 0개 | 0 | 모델이 읽고 낮게 매긴 것. 답이지 고장이 아님 (아래 참고) |
| 점수가 **전부 정확히 0.0** | **1** | 응답 파싱 실패나 API 이상. 모델이 실제로 내린 판단이 아님 |
| 후보는 있는데 초록이 전부 없음 | **1** | 초록 커버리지 붕괴. 옛 OpenAlex가 몇 주간 0건을 성공으로 보고했던 실패 |
| 멤버 1명 실패, 나머지 정상 | **1** | 나머지는 그대로 받습니다. 실패한 사람만 `member` 입력으로 재실행 |
| 멤버 파일에 문제 | **1** | 수집 전에 멈춥니다 — 지출 0 |
| 상한 초과 | **1** | 수집 전에 멈춥니다 — 지출 0 |
| 뉴스 쪽 문제 (피드 죽음 등) | 0 | 논문이 잘 들어간 실행을 뉴스가 망치면 안 됨 |

한 멤버의 실패는 **격리**됩니다. 그 사람의 Notion이 문제여도 나머지는 자기 DB에 정상적으로 받습니다.

### "컷오프 통과 0개"가 실패가 아닌 이유

예전에는 이것도 exit 1 이었습니다. 창이 7일이고 그 안의 모든 것이 새것이던 시절에는 말이 됐지만, `days_back: 30` 에 멤버별 중복 제거가 붙으면서 전제가 깨졌습니다 — 보통의 한 주는 남은 후보가 1~2편이고, 그중 하나가 3점인 건 사고가 아닙니다. 매주 빨간불이 뜨면 사람들은 알림을 무시하게 되고, 그게 원래 잡으려던 사고보다 비쌉니다.

대신 **고장의 형태**를 봅니다. `_parse_scores` 는 실패할 때마다 `[0.0] * count` 를 돌려줍니다 — JSON 이 깨졌든, 배열이 없든, 형태가 다르든 전부. 정상 모델이 관련 없는 논문을 받으면 작은 값들이 **퍼져서** 옵니다. 그래서 전부 0.0 이면 알리고, 퍼져 있으면 조용한 주로 넘깁니다.

초록 붕괴(OpenAlex 시나리오)는 별개 가드가 잡습니다. 초록 없는 논문은 채점 전에 걸러지므로 이 판정에 도달하지도 않습니다.

## Actions 실행이 실패할 때

실행이 시작 직후 죽었다면 대부분 설정 문제입니다. 로그의 `Cannot start:` 줄, 또는 `run-report.json` 아티팩트의 `error` 필드를 먼저 보세요.

| 로그 메시지 | 원인과 해결 |
|---|---|
| `N problem(s) in the member configuration` | `members/*.yaml` 문제. 메시지가 파일과 필드를 전부 나열합니다. `members validate`로 로컬에서 먼저 확인 |
| `would write up to N notes, over the lab limit` | 멤버 `top_n` 합계가 `limits.max_notes_per_run` 초과 |
| `No enabled member with id '...'` | `member` 입력의 ID가 틀렸거나 그 멤버가 `enabled: false` |
| `parent page ... is not visible to this integration` | 메인 페이지에 통합을 연결하지 않았습니다. 페이지 `···` → Connections |
| `notion_parent_page_id ... is still the placeholder` | config.yaml에 본인 Notion 페이지 링크를 안 넣었습니다 |
| `notion_parent_page_id does not contain a Notion ID` | 넣은 값에 32자리 ID가 없습니다. 페이지 링크를 다시 복사하세요 |
| `NOTION_TOKEN is not set` | 리포 Settings → Secrets에 `NOTION_TOKEN`이 없거나 이름이 다릅니다 |
| `ANTHROPIC_API_KEY is not set` | 위와 동일 (`llm.provider`가 `openai`면 `OPENAI_API_KEY`) |
| `Notion page lookup ... failed: ... share ... with your integration` | Notion 페이지에 통합을 연결하지 않았습니다. 페이지 `···` → Connections |
| `Notion ... failed: API token is invalid` | 토큰이 잘못됐거나 폐기됐습니다 |
| `Permission denied ... git push` (커밋 단계) | Settings → Actions → General → **Read and write permissions** |

Notion 관련 설정은 **수집·LLM 호출 전에 먼저 검사**하므로, 잘못돼 있으면 몇 초 만에 실패하고 API 비용이 나가지 않습니다.

## 개발

```bash
python -m pytest          # 303 tests, 네트워크 호출 없음 (소켓 차단 검증 포함)
python -m ruff check paper_digest tests
```

Notion은 [tests/notion_fake.py](tests/notion_fake.py)의 인메모리 가짜 서버로 대체됩니다. 실제 상태를 들고 있어서 "각 멤버가 자기 DB를 갖는다"를 실제로 검증할 수 있습니다 — 예전의 MagicMock 방식은 모든 부모에 같은 children을 돌려줘서, 코드가 DB 하나에 전원을 몰아넣어도 테스트가 통과했습니다.

주요 모듈:

| 파일 | 역할 |
|---|---|
| [pipeline.py](paper_digest/pipeline.py) | 전체 흐름 — 수집 1회 → 뉴스 1회 → 멤버 순차 |
| [members.py](paper_digest/members.py) | 멤버 YAML 로딩·검증·상한, 멤버별 설정 주입 |
| [collectors/semantic_scholar.py](paper_digest/collectors/semantic_scholar.py) | 논문 수집 — 학회와 저널 |
| [collectors/hackernews.py](paper_digest/collectors/hackernews.py) · [rss.py](paper_digest/collectors/rss.py) | 뉴스 수집 |
| [venues.py](paper_digest/venues.py) | venue 선별(학회/저널) + 정식 명칭 → 약칭 변환 |
| [ranking.py](paper_digest/ranking.py) | 논문 관련도 채점 (싼 모델) |
| [news_stage.py](paper_digest/news_stage.py) | 뉴스 단계 전체 — 공용 1회, 메인 페이지 |
| [news_select.py](paper_digest/news_select.py) | 뉴스 선별 (모델 안 씀) |
| [notes.py](paper_digest/notes.py) | 한국어 노트 생성 (좋은 모델) |
| [notion_api.py](paper_digest/notion_api.py) | Notion 전송 계층 — 스로틀 + 429 재시도 |
| [notion_writer.py](paper_digest/notion_writer.py) | 멤버 페이지·개인 DB·뉴스 DB 확보 + 페이지 작성 |
| [notion_query.py](paper_digest/notion_query.py) | **진실 계층** — "이미 받았나"를 DB에 조회 |
| [dedup.py](paper_digest/dedup.py) | 런 내부 병합 + 멤버별 채점 캐시 |

### 왜 멤버를 순차로 처리하나

GitHub Actions matrix로 병렬 처리하지 않습니다. matrix가 주는 건 장애 격리 하나인데 그건 `try`/`except`로 똑같이 되고, 반면 **Notion의 레이트 리밋은 통합 토큰 단위**입니다. 잡을 병렬로 돌리면 같은 버킷을 나눠 쓰면서 각자 전체를 가진 듯 행동하므로 429가 줄지 않고 늘어납니다. 단일 프로세스면 요청이 직렬로 나가서 스로틀을 정확히 걸 수 있습니다.

멤버당 약 3분, 10명이면 25분 내외입니다 (워크플로 타임아웃 90분).
