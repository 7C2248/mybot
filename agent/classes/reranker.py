"""LangChain 文档重排器。"""

from typing import Sequence, Optional

from pydantic import ConfigDict
from langchain_core.callbacks import Callbacks
from langchain_core.documents import BaseDocumentCompressor, Document
from langchain_community.cross_encoders import HuggingFaceCrossEncoder


class CrossEncoderReranker(BaseDocumentCompressor):
    """自定义的交叉编码器重排序器"""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    model: HuggingFaceCrossEncoder
    top_k: int = 4

    def compress_documents(
        self,
        documents: Sequence[Document],
        query: str,
        callbacks: Optional[Callbacks] = None,
    ) -> Sequence[Document]:
        """
        对文档进行重排序的核心方法。
        接收查询和文档列表，返回重排序后的文档列表。
        """
        if len(documents) == 0:
            return []

        # 1. 使用交叉编码器模型对 (query, doc) 对进行打分
        scores = list(self.model.score([(query, doc.page_content) for doc in documents]))

        # 2. 将文档和分数组合，并按分数降序排序
        docs_with_scores = sorted(
            zip(documents, scores), key=lambda x: x[1], reverse=True
        )

        # 3. 返回前 top_k 个最相关的文档
        return [doc for doc, _ in docs_with_scores[:self.top_k]]

    async def acompress_documents(
        self,
        documents: Sequence[Document],
        query: str,
        callbacks: Optional[Callbacks] = None,
    ):
        import asyncio
        return await asyncio.to_thread(
            self.compress_documents,
            documents,
            query,
            callbacks
        )
