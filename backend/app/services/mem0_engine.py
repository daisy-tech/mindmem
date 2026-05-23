import os
from functools import lru_cache

from mem0 import Memory


class Mem0Engine:
    def __init__(self):
        config = {
            "custom_instructions": "MANDATORY LANGUAGE RULE: You MUST write ALL memory 'text' fields in Simplified Chinese (简体中文). This rule OVERRIDES all English examples above. Never write memory text in English. 所有记忆的text字段必须用简体中文书写，禁止使用英文。",
            "vector_store": {
                "provider": "qdrant",
                "config": {
                    "collection_name": "memories_v2",
                    "host": os.getenv("QDRANT_HOST", "localhost"),
                    "port": int(os.getenv("QDRANT_PORT", "6333")),
                    "embedding_model_dims": 1024,
                }
            },
            "llm": {
                "provider": "openai",
                "config": {
                    "model": "qwen-plus",
                    "api_key": os.getenv("OPENAI_API_KEY"),
                }
            },
            "embedder": {
                "provider": "openai",
                "config": {
                    "model": "text-embedding-v3",
                    "api_key": os.getenv("OPENAI_API_KEY"),
                }
            }
        }
        self.client = Memory.from_config(config)

    def add(self, messages, user_id: str, infer: bool = True):
        """添加记忆。
        infer=True (默认): Mem0 会用 LLM 再次抽取/改写文本
        infer=False: 把原始文本直接作为一条记忆写入（适合上游已抽取过）
        """
        return self.client.add(messages, user_id=user_id, infer=infer)

    def search(self, query: str, user_id: str, limit: int = 5):
        return self.client.search(query, top_k=limit, filters={"user_id": user_id})

    def update(self, memory_id: str, new_text: str):
        return self.client.update(memory_id, data=new_text)

    def get_all(self, user_id: str):
        return self.client.get_all(filters={"user_id": user_id}, top_k=500)

    def delete(self, memory_id: str, user_id: str):
        return self.client.delete(memory_id)


@lru_cache(maxsize=1)
def get_mem0() -> Mem0Engine:
    return Mem0Engine()
