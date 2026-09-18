class Chroma:
    def __init__(self, **kwargs): self._kw = kwargs
    def add_texts(self, **kwargs): return kwargs.get("ids", [])
    def get(self, **kwargs): return {"ids": []}
    def delete(self, **kwargs): return None
    def similarity_search_with_score(self, *a, **k): return []
