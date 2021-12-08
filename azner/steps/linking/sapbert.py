import logging
import traceback
from typing import List, Tuple

import pydash
import torch

from azner.data.data import Document, PROCESSING_EXCEPTION
from azner.steps import BaseStep
from azner.utils.caching import (
    EntityLinkingLookupCache,
    CachedIndexGroup,
    EmbeddingOntologyCacheManager,
)
from azner.utils.utils import (
    filter_entities_with_ontology_mappings,
)

logger = logging.getLogger(__name__)


class SapBertForEntityLinkingStep(BaseStep):
    """
    This step wraps Sapbert: Self Alignment pretraining for biomedical entity representation.
    We make use of two caches here:
    1) :class:`azner.utils.link_index.EmbeddingIndex` Since these are static and numerous, it makes sense to
    precompute them once and reload them each time. This is done automatically if no cache file is detected.
    2) :class:`azner.utils.caching.EntityLinkingLookupCache` Since certain entities will come up more frequently, we
    cache the result mappings rather than call bert repeatedly.

    Original paper https://aclanthology.org/2021.naacl-main.334.pdf
    """

    def __init__(
        self,
        depends_on: List[str],
        index_group: CachedIndexGroup,
        process_all_entities: bool = False,
        lookup_cache_size: int = 5000,
        top_n: int = 20,
        score_cutoff: float = 95.0,
    ):
        """

        :param depends_on: namespaces of dependency stes
        :param model: a pretrained Sapbert Model
        :param ontology_path: path to file to generate embeddings from. See :meth:`azner.modelling.
            ontology_preprocessing.base.OntologyParser.OntologyParser.write_ontology_metadata` for format
        :param batch_size: for inference with Pytorch
        :param trainer: a pytorch lightning Trainer to handle the inference for us
        :param dl_workers: number fo dataloader workers
        :param ontology_partition_size: when generating embeddings, process n in a partition before serialising to disk.
            (reduce if memory is an issue)
        :param embedding_index_factory: For creating Embedding Indexes
        :param entity_class_to_ontology_mappings: A Dict[str,str] that maps an entity class to the Ontology it should be
            processed against
        :param process_all_entities: if False, ignore entities that already have a mapping
        :param rebuild_ontology_cache: Force rebuild of embedding cache
        :param lookup_cache_size: size of lookup cache to maintain
        """
        super().__init__(depends_on=depends_on)

        if not all(
            [isinstance(x, EmbeddingOntologyCacheManager) for x in index_group.cache_managers]
        ):
            raise RuntimeError(
                "The CachedIndexGroup must be configured with an EmbeddingOntologyCacheManager to work"
                "correctly with the Sapbert Step"
            )

        if len(index_group.cache_managers) > 1:
            logger.warning(
                f"multiple cache managers detected for {self.namespace()}. This may mean you are loading"
                f"multiple instances of a model, which is memory inefficient. In addition, this instance will"
                f" reuse the model data associated with the first detected cache manager. This may have "
                f"unintended consequences."
            )

        self.top_n = top_n
        self.score_cutoff = score_cutoff
        self.index_group = index_group
        self.index_group.load()
        # we reuse the instance of the model associated with the cache manager, so we don't have to instantiate it twice
        self.dl_workers = index_group.cache_managers[0].dl_workers
        self.batch_size = index_group.cache_managers[0].batch_size
        self.model = index_group.cache_managers[0].model
        self.trainer = index_group.cache_managers[0].trainer
        self.process_all_entities = process_all_entities

        self.lookup_cache = EntityLinkingLookupCache(lookup_cache_size)

    def _run(self, docs: List[Document]) -> Tuple[List[Document], List[Document]]:
        """
        logic of entity linker:

        1) first obtain an entity list from all docs
        2) check the lookup LRUCache to see if it's been recently processed
        3) generate embeddings for the entities based on the value of Entity.match
        4) query this embedding against self.ontology_index_dict to determine the best matches based on cosine distance
        5) generate a new Mapping with the queried iri, and update the entity information
        :param docs:
        :return:
        """
        failed_docs = []
        try:
            entities = pydash.flatten([x.get_entities() for x in docs])
            if not self.process_all_entities:
                entities = filter_entities_with_ontology_mappings(entities)

            entities = self.lookup_cache.check_lookup_cache(entities)
            if len(entities) > 0:
                results = self.model.get_embeddings_for_strings(
                    [x.match for x in entities], trainer=self.trainer, batch_size=self.batch_size
                )
                results = torch.unsqueeze(results, 1)
                for i, result in enumerate(results):
                    entity = entities[i]
                    cache_missed_entities = self.lookup_cache.check_lookup_cache([entity])
                    if len(cache_missed_entities) == 0:
                        continue

                    mappings = self.index_group.search(
                        query=result,
                        entity_class=entity.entity_class,
                        namespace=self.namespace(),
                        top_n=self.top_n,
                        score_cutoff=self.score_cutoff,
                    )
                    for mapping in mappings:
                        entity.add_mapping(mapping)
                        self.lookup_cache.update_lookup_cache(entity, mapping)
        except Exception:
            affected_doc_ids = [doc.idx for doc in docs]
            for doc in docs:
                message = (
                    f"batch failed: affected ids: {affected_doc_ids}\n" + traceback.format_exc()
                )
                doc.metadata[PROCESSING_EXCEPTION] = message
                failed_docs.append(doc)

        return docs, failed_docs
