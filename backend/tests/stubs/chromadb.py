"""
离线测试用的 chromadb 桩实现

说明：
    langchain-chroma 在导入时会 `import chromadb`，因此本机缺少 chromadb 时，
    连 `from langchain_chroma import Chroma` 都会失败。
    这个桩提供 chromadb 的最小可用接口（仅满足导入与基本调用），
    让离线自检能够跑通。

⚠️ 仅用于离线自检，不实现任何真实的向量存储与检索能力。
"""

from __future__ import annotations

__version__ = "0.0.0-stub"


class Collection:
    """最小集合实现（内存字典，不落盘）。"""

    def __init__(self, name: str = "default", metadata: dict | None = None):
        self.name = name
        self.metadata = metadata or {}
        self._store: dict[str, dict] = {}

    def add(self, ids=None, documents=None, metadatas=None, embeddings=None, **kwargs):
        ids = ids or []
        documents = documents or []
        metadatas = metadatas or []
        for index, doc_id in enumerate(ids):
            self._store[doc_id] = {
                "document": documents[index] if index < len(documents) else None,
                "metadata": metadatas[index] if index < len(metadatas) else {},
            }

    def get(self, where=None, ids=None, **kwargs):
        """返回 {ids, documents, metadatas} 结构（不做 where 过滤）。"""
        selected_ids = list(self._store.keys()) if ids is None else [i for i in ids if i in self._store]
        return {
            "ids": selected_ids,
            "documents": [self._store[i]["document"] for i in selected_ids],
            "metadatas": [self._store[i]["metadata"] for i in selected_ids],
        }

    def delete(self, ids=None, where=None, **kwargs):
        if ids:
            for doc_id in ids:
                self._store.pop(doc_id, None)
        else:
            self._store.clear()

    def count(self) -> int:
        return len(self._store)

    def query(self, query_texts=None, n_results=10, where=None, **kwargs):
        """空实现的相似度检索（永远返回空结果）。"""
        return {"ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]]}


class _Client:
    def __init__(self, path: str | None = None, **kwargs):
        self.path = path
        self._collections: dict[str, Collection] = {}

    def get_or_create_collection(self, name: str, metadata: dict | None = None, **kwargs) -> Collection:
        if name not in self._collections:
            self._collections[name] = Collection(name, metadata)
        return self._collections[name]

    def create_collection(self, name: str, metadata: dict | None = None, **kwargs) -> Collection:
        self._collections[name] = Collection(name, metadata)
        return self._collections[name]

    def get_collection(self, name: str, **kwargs) -> Collection:
        return self._collections.get(name) or self.get_or_create_collection(name)

    def delete_collection(self, name: str, **kwargs) -> None:
        self._collections.pop(name, None)

    def list_collections(self):
        return list(self._collections.values())

    def heartbeat(self) -> int:
        return 0


class PersistentClient(_Client):
    """持久化客户端桩（实际不落盘）。"""


class EphemeralClient(_Client):
    """内存客户端桩。"""


def Client(**kwargs) -> _Client:  # noqa: N802  保持与 chromadb 一致的命名
    return _Client(**kwargs)
