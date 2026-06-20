import logging
import httpx
from google import genai

logger = logging.getLogger(__name__)

_EMBED_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:embedContent"


class GeminiService:
    def __init__(self, api_key: str, chat_model: str = "gemini-2.0-flash", embedding_model: str = "text-embedding-004"):
        self._api_key = api_key
        self._client = genai.Client(api_key=api_key, http_options={"api_version": "v1"})
        self.chat_model_name = chat_model
        self.embedding_model_name = embedding_model.replace("models/", "")

    async def _embed(self, text: str, task_type: str) -> list[float]:
        url = _EMBED_URL.format(model=self.embedding_model_name)
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                url,
                params={"key": self._api_key},
                json={
                    "model": f"models/{self.embedding_model_name}",
                    "content": {"parts": [{"text": text}]},
                    "taskType": task_type,
                },
            )
            resp.raise_for_status()
            return resp.json()["embedding"]["values"]

    async def embed_document(self, text: str) -> list[float]:
        return await self._embed(text[:8000], "RETRIEVAL_DOCUMENT")

    async def embed_query(self, text: str) -> list[float]:
        return await self._embed(text[:2000], "RETRIEVAL_QUERY")

    async def generate(self, prompt: str) -> str:
        response = await self._client.aio.models.generate_content(
            model=self.chat_model_name,
            contents=prompt,
        )
        return response.text

    async def generate_answer(self, question: str, context_chunks: list[dict]) -> str:
        ctx_parts = []
        for i, c in enumerate(context_chunks, 1):
            section_info = f" | 섹션: {c['section']}" if c.get("section") else ""
            table_flag = " [표 포함]" if c.get("has_table") else ""
            ctx_parts.append(
                f"[{i}] {c['corp_name']} | {c['report_nm']}{section_info}{table_flag}\n"
                f"{c['chunk_text']}"
            )
        context = "\n\n".join(ctx_parts)
        prompt = f"""당신은 한국 금융 공시 분석 전문가입니다.
아래 DART 공시 자료를 바탕으로 사용자의 질문에 정확하고 상세하게 한국어로 답변하세요.

## 공시 자료
{context}

## 질문
{question}

## 답변 지침
- 반드시 제공된 공시 자료에 근거하여 답변하세요.
- 자료에 없는 내용은 "공시 자료에서 확인되지 않습니다"라고 명확히 밝히세요.
- 수치·날짜를 인용할 때는 해당 출처 번호를 [1], [2] 형식으로 표시하세요.
- 답변은 명확하고 구조적으로 작성하세요.
"""
        return await self.generate(prompt)

    async def generate_debate(self, question: str, context_chunks: list[dict]) -> str:
        ctx = "\n\n".join([c["chunk_text"] for c in context_chunks[:5]])
        prompt = f"""공시 데이터를 비판적으로 분석하는 전문가입니다.
아래 질문과 관련 공시 자료를 바탕으로 **반박 시각**에서 분석하세요.

## 관련 공시 자료
{ctx}

## 분석 질문
{question}

## 반박 분석 지침
- 낙관적 해석에 의문을 제기하세요
- 공시에서 누락되거나 모호한 부분을 지적하세요
- 투자자가 주의해야 할 리스크를 강조하세요
- 한국어로 답변하세요"""
        return await self.generate(prompt)
