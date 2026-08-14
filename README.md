# Paper Digest

매주 월요일 아침, 새로 나온 논문과 IT 뉴스를 모아 한국어 노트로 정리해서 Notion 하나의 데이터베이스에 쌓아주는 도구입니다.

| | 논문 | 뉴스 |
|---|---|---|
| 출처 | arXiv (cs.CL/AI/LG), OpenAlex | Hacker News, RSS 피드 |
| 거르는 방법 | 키워드 → **싼 모델로 관련도 0~10점** → 상위 N개 | 키워드만 (모델 안 씀) → 출처별 번갈아 → 상위 N개 |
| 노트 | 4섹션 (요약 / 핵심 기여 / 방법 / 내 연구와의 연결점) | 3섹션 (요약 / 핵심 내용 / 연결점) |
| Notion `Type` | `논문` | `뉴스` |

뉴스에 관련도 채점을 하지 않는 이유는 [news_select.py](paper_digest/news_select.py)에 적어뒀습니다. 요약하면, HN은 이미 커뮤니티 점수로 걸러졌고 RSS 피드는 직접 고른 것이라 소스 자체가 이미 큐레이션된 상태이기 때문입니다. 모델을 한 번 더 부르는 값만큼의 이득이 없습니다.

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

### 3. config.yaml 채우기

[config.yaml](config.yaml)에서 최소한 이 두 가지는 본인 것으로 바꿔야 합니다.

```yaml
notion_parent_page_id: "여기에_1번에서_복사한_32자리_ID"

research_profile: |
  본인 연구 주제를 2~5문장으로.
  이 내용으로 논문 관련도를 채점하고, "내 연구와의 연결점" 섹션도 씁니다.
```

`keywords`, `news.keywords`, `top_n` 같은 건 기본값으로 두고 나중에 조정해도 됩니다.

### 4. GitHub 설정 — 두 가지

리포지토리: https://github.com/YouJaeBeom/Finder

**(1) 시크릿 등록**

`Settings` → `Secrets and variables` → `Actions` → `New repository secret`

| Name | Secret |
|---|---|
| `NOTION_TOKEN` | 1번에서 복사한 통합 시크릿 |
| `ANTHROPIC_API_KEY` | 2번에서 발급한 키 |

**(2) 워크플로 쓰기 권한 켜기** ← 이걸 안 하면 실행이 실패합니다

`Settings` → `Actions` → `General` → 맨 아래 **Workflow permissions**
→ **Read and write permissions** 선택 → **Save**

매주 중복 방지 상태(`seen_ids.json`)를 봇이 커밋해야 하는데, GitHub 기본값이 읽기 전용이라 이 설정이 필요합니다.

### 5. 첫 실행 (로컬)

```bash
pip install -r requirements.txt
pip install -e .

export NOTION_TOKEN="..."          # 1번의 통합 시크릿
export ANTHROPIC_API_KEY="..."     # 2번의 키

python -m paper_digest init        # Notion DB 생성
```

성공하면 Notion 페이지 안에 `📚 Paper Digest` 데이터베이스가 생기고, 터미널에 이런 줄이 뜹니다.

```
Pin it by adding this line to config.yaml:
  notion_database_id: "2ba8a847-7019-8012-3456-7890abcdef12"
```

이 줄을 [config.yaml](config.yaml)에 그대로 붙여넣고 커밋하세요. 이러면 로컬이든 Actions든 **항상 같은 DB에 쌓입니다.**

```bash
git add config.yaml state.json
git commit -m "chore: pin notion database"
git push
```

### 6. 동작 확인

GitHub `Actions` 탭 → `Weekly paper digest` → **Run workflow** 버튼으로 수동 실행해봅니다.

- 초록 체크 → Notion DB에 페이지가 쌓였는지 확인
- 빨간 X → 로그를 열어보고, 실행 결과 요약은 `run-report.json` 아티팩트에서 확인

이후로는 **매주 월요일 08:00 (KST)** 에 자동으로 돕니다.

---

## 평소 사용법

| 하고 싶은 것 | 방법 |
|---|---|
| 관심 키워드 바꾸기 | `config.yaml`의 `keywords` / `news.keywords` 수정 후 push |
| 뉴스 소스 추가 | `config.yaml`의 `news.rss_feeds`에 피드 URL 추가 (코드 수정 불필요) |
| 뉴스 전부 받기 | `news.keywords`를 빈 리스트로 |
| 뉴스 끄기 | `news.enabled: false` |
| 논문 개수 조정 | `top_n` (기본 10), `news.top_n` (기본 5) |
| 지금 당장 한 번 돌리기 | Actions 탭 → Run workflow |
| 학회 게재 확정 반영 | Actions 탭 → `Proceedings batch update` → venue 입력 (예: `ACL 2026`) |

### 로컬에서 직접 돌리기

```bash
python -m paper_digest run --mode weekly
python -m paper_digest run --mode batch --venue "ACL 2026"
```

---

## 종료 코드 규칙

Actions 실패 알림이 언제 오는지를 정하는 규칙입니다.

| 상황 | 코드 | 의도 |
|---|---|---|
| 정상 실행 | 0 | — |
| 키워드 후보 0개 | 0 | 진짜 조용한 주. 실패가 아님 |
| 후보는 있는데 랭킹 통과 0개 | **1** | 컷오프 오설정이나 LLM API 이상. **알림이 와야 함** |
| 뉴스 쪽 문제 (피드 죽음 등) | 0 | 논문이 잘 들어간 실행을 뉴스가 망치면 안 됨 |

## 개발

```bash
python -m pytest          # 93 tests, 네트워크 호출 없음
```

주요 모듈:

| 파일 | 역할 |
|---|---|
| [pipeline.py](paper_digest/pipeline.py) | 전체 흐름 (weekly / batch / init) |
| [collectors/](paper_digest/collectors/) | arXiv, OpenAlex, Hacker News, RSS 수집 |
| [ranking.py](paper_digest/ranking.py) | 논문 관련도 채점 (싼 모델) |
| [news_select.py](paper_digest/news_select.py) | 뉴스 선별 (모델 안 씀) |
| [notes.py](paper_digest/notes.py) | 한국어 노트 생성 (좋은 모델) |
| [notion_writer.py](paper_digest/notion_writer.py) | DB 확보 + 페이지 작성 |
| [dedup.py](paper_digest/dedup.py) | 중복 제거 (arXiv ID / DOI / URL / 정규화 제목) |
