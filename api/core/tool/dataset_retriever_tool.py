import json
import threading
from typing import Type, Optional, List

from flask import current_app
from langchain.tools import BaseTool
from pydantic import Field, BaseModel

from core.callback_handler.index_tool_callback_handler import DatasetIndexToolCallbackHandler
from core.conversation_message_task import ConversationMessageTask
from core.embedding.cached_embedding import CacheEmbedding
from core.index.keyword_table_index.keyword_table_index import KeywordTableIndex, KeywordTableConfig
from core.index.vector_index.vector_index import VectorIndex
from core.model_providers.error import LLMBadRequestError, ProviderTokenNotInitError
from core.model_providers.model_factory import ModelFactory
from extensions.ext_database import db
from models.dataset import Dataset, DocumentSegment, Document
from services.retrieval_service import RetrievalService

default_retrieval_model = {
    'search_method': 'semantic_search',
    'reranking_enable': False,
    'reranking_model': {
        'reranking_provider_name': '',
        'reranking_model_name': ''
    },
    'top_k': 2,
    'score_threshold_enable': False
}


class DatasetRetrieverToolInput(BaseModel):
    query: str = Field(..., description="Query for the dataset to be used to retrieve the dataset.")


class DatasetRetrieverTool(BaseTool):
    """Tool for querying a Dataset."""
    name: str = "dataset"
    args_schema: Type[BaseModel] = DatasetRetrieverToolInput
    description: str = "use this to retrieve a dataset. "

    tenant_id: str
    dataset_id: str
    top_k: int = 2
    score_threshold: Optional[float] = None
    conversation_message_task: ConversationMessageTask
    return_resource: bool
    retriever_from: str

    @classmethod
    def from_dataset(cls, dataset: Dataset, **kwargs):
        description = dataset.description
        if not description:
            description = 'useful for when you want to answer queries about the ' + dataset.name

        description = description.replace('\n', '').replace('\r', '')
        return cls(
            name=f'dataset-{dataset.id}',
            tenant_id=dataset.tenant_id,
            dataset_id=dataset.id,
            description=description,
            **kwargs
        )

    def _run(self, query: str) -> str:
        dataset = db.session.query(Dataset).filter(
            Dataset.tenant_id == self.tenant_id,
            Dataset.id == self.dataset_id
        ).first()

        if not dataset:
            return ''
        # get retrieval model , if the model is not setting , using default
        retrieval_model = dataset.retrieval_model if dataset.retrieval_model else default_retrieval_model

        if dataset.indexing_technique == "economy":
            # use keyword table query
            kw_table_index = KeywordTableIndex(
                dataset=dataset,
                config=KeywordTableConfig(
                    max_keywords_per_chunk=5
                )
            )

            documents = kw_table_index.search(query, search_kwargs={'k': self.top_k})
            return str("\n".join([document.page_content for document in documents]))
        else:

            try:
                embedding_model = ModelFactory.get_embedding_model(
                    tenant_id=dataset.tenant_id,
                    model_provider_name=dataset.embedding_model_provider,
                    model_name=dataset.embedding_model
                )
            except LLMBadRequestError:
                return ''
            except ProviderTokenNotInitError:
                return ''

            embeddings = CacheEmbedding(embedding_model)

            documents = []
            threads = []
            if self.top_k > 0:
                # retrieval source with semantic
                if retrieval_model['search_method'] == 'semantic_search' or retrieval_model['search_method'] == 'hybrid_search':
                    embedding_thread = threading.Thread(target=RetrievalService.embedding_search, kwargs={
                        'flask_app': current_app._get_current_object(),
                        'dataset_id': str(dataset.id),
                        'query': query,
                        'top_k': self.top_k,
                        'score_threshold': retrieval_model['score_threshold'] if retrieval_model[
                            'score_threshold_enable'] else None,
                        'reranking_model': retrieval_model['reranking_model'] if retrieval_model[
                            'reranking_enable'] else None,
                        'all_documents': documents,
                        'search_method': retrieval_model['search_method'],
                        'embeddings': embeddings
                    })
                    threads.append(embedding_thread)
                    embedding_thread.start()

                # retrieval_model source with full text
                if retrieval_model['search_method'] == 'full_text_search' or retrieval_model['search_method'] == 'hybrid_search':
                    full_text_index_thread = threading.Thread(target=RetrievalService.full_text_index_search, kwargs={
                        'flask_app': current_app._get_current_object(),
                        'dataset_id': str(dataset.id),
                        'query': query,
                        'search_method': retrieval_model['search_method'],
                        'embeddings': embeddings,
                        'score_threshold': retrieval_model['score_threshold'] if retrieval_model[
                            'score_threshold_enable'] else None,
                        'top_k': self.top_k,
                        'reranking_model': retrieval_model['reranking_model'] if retrieval_model[
                            'reranking_enable'] else None,
                        'all_documents': documents
                    })
                    threads.append(full_text_index_thread)
                    full_text_index_thread.start()

                for thread in threads:
                    thread.join()
                # hybrid search: rerank after all documents have been searched
                if retrieval_model['search_method'] == 'hybrid_search':
                    hybrid_rerank = ModelFactory.get_reranking_model(
                        tenant_id=dataset.tenant_id,
                        model_provider_name=retrieval_model['reranking_model']['reranking_provider_name'],
                        model_name=retrieval_model['reranking_model']['reranking_model_name']
                    )
                    documents = hybrid_rerank.rerank(query, documents,
                                                     retrieval_model['score_threshold'] if retrieval_model['score_threshold_enable'] else None,
                                                     self.top_k)
            else:
                documents = []

            hit_callback = DatasetIndexToolCallbackHandler(self.conversation_message_task)
            hit_callback.on_tool_end(documents)
            document_score_list = {}
            if dataset.indexing_technique != "economy":
                for item in documents:
                    document_score_list[item.metadata['doc_id']] = item.metadata['score']
            document_context_list = []
            index_node_ids = [document.metadata['doc_id'] for document in documents]
            segments = DocumentSegment.query.filter(DocumentSegment.dataset_id == self.dataset_id,
                                                    DocumentSegment.completed_at.isnot(None),
                                                    DocumentSegment.status == 'completed',
                                                    DocumentSegment.enabled == True,
                                                    DocumentSegment.index_node_id.in_(index_node_ids)
                                                    ).all()

            if segments:
                index_node_id_to_position = {id: position for position, id in enumerate(index_node_ids)}
                sorted_segments = sorted(segments,
                                         key=lambda segment: index_node_id_to_position.get(segment.index_node_id,
                                                                                           float('inf')))
                for segment in sorted_segments:
                    if segment.answer:
                        document_context_list.append(f'question:{segment.content} answer:{segment.answer}')
                    else:
                        document_context_list.append(segment.content)
                if self.return_resource:
                    context_list = []
                    resource_number = 1
                    for segment in sorted_segments:
                        context = {}
                        document = Document.query.filter(Document.id == segment.document_id,
                                                         Document.enabled == True,
                                                         Document.archived == False,
                                                         ).first()
                        if dataset and document:
                            source = {
                                'position': resource_number,
                                'dataset_id': dataset.id,
                                'dataset_name': dataset.name,
                                'document_id': document.id,
                                'document_name': document.name,
                                'data_source_type': document.data_source_type,
                                'segment_id': segment.id,
                                'retriever_from': self.retriever_from,
                                'score': document_score_list.get(segment.index_node_id, None)

                            }
                            if self.retriever_from == 'dev':
                                source['hit_count'] = segment.hit_count
                                source['word_count'] = segment.word_count
                                source['segment_position'] = segment.position
                                source['index_node_hash'] = segment.index_node_hash
                            if segment.answer:
                                source['content'] = f'question:{segment.content} \nanswer:{segment.answer}'
                            else:
                                source['content'] = segment.content
                            context_list.append(source)
                        resource_number += 1
                    hit_callback.return_retriever_resource_info(context_list)

            return str("\n".join(document_context_list))

    async def _arun(self, tool_input: str) -> str:
        raise NotImplementedError()
