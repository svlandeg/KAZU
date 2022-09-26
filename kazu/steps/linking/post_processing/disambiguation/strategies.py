import functools
import logging
from abc import ABC, abstractmethod
from collections import defaultdict
from typing import Tuple, Optional, Set, Dict, Iterable

import numpy as np

from kazu.data.data import (
    Document,
    EquivalentIdSet,
    EquivalentIdAggregationStrategy,
)
from kazu.modelling.database.in_memory_db import (
    MetadataDatabase,
    SynonymDatabase,
    NormalisedSynonymStr,
)
from kazu.steps.linking.post_processing.disambiguation.context_scoring import (
    TfIdfScorerManager,
    TfIdfDocumentScorer,
)

logger = logging.getLogger(__name__)


class DisambiguationStrategy(ABC):
    """
    The job of a DisambiguationStrategy is to filter a Set[EquivalentIdSet] into a (hopefully) smaller set.
    A .prepare method is available, which can be cached in the event of any duplicated preprocessing work that may
    be required (see StrategyRunner for how the complexities of how MappingStrategy and DisambiguationStrategy are
    coordinated).
    """

    @abstractmethod
    def prepare(self, document: Document):
        """
        perform any preprocessing required
        :param document:
        :return:
        """
        pass

    @abstractmethod
    def disambiguate(
        self, id_sets: Set[EquivalentIdSet], document: Document, parser_name: str
    ) -> Set[EquivalentIdSet]:
        """
        subset a Set[EquivalentIdSet]
        :param id_sets:
        :param document:
        :param parser_name:
        :return:
        """
        pass

    def __call__(
        self, id_sets: Set[EquivalentIdSet], document: Document, parser_name: str
    ) -> Set[EquivalentIdSet]:
        self.prepare(document)
        return self.disambiguate(id_sets, document, parser_name)


class DefinedElsewhereInDocumentDisambiguationStrategy(DisambiguationStrategy):
    """
    1) look for entities on the document that have mappings
    2) see if any of these mappings correspond to ay ids in the EquivalentIdSets on each hit
    3) if only a single hit is found, create a new mapping from the matched hit
    4) if more than one hit is found, create multiple mappings, with the AMBIGUOUS flag
    """

    def __init__(
        self,
    ):
        self.mapped_ids: Set[Tuple[str, str, str]] = set()

    def prepare(self, document: Document):
        """
        note, this method can't be cached, as the state of the document may change between executions
        :param document:
        :return:
        """
        self.mapped_ids = set()
        entities = document.get_entities()
        self.mapped_ids.update(
            (
                mapping.parser_name,
                mapping.source,
                mapping.idx,
            )
            for ent in entities
            for mapping in ent.mappings
        )

    def disambiguate(
        self, id_sets: Set[EquivalentIdSet], document: Document, parser_name: str
    ) -> Set[EquivalentIdSet]:
        found_id_sets = set()
        for id_set in id_sets:
            for idx in id_set.ids:
                if (
                    parser_name,
                    id_set.ids_to_source[idx],
                    idx,
                ) in self.mapped_ids:
                    found_id_sets.add(id_set)
                    break
        return found_id_sets


class TfIdfDisambiguationStrategy(DisambiguationStrategy):
    """
    1) retrieve all synonyms associated with an equivalent ID set, and filter out ambiguous ones
        and build a query matrix with the unambiguous ones
    2) retrieve a list of all detected entity strings from the document, regardless of source and
        build a document representation matrix of these
    3) perform TFIDF on the query vs document, and sort according to most likely synonym hit from 1)
    4) if the score is above the minimum threshold, create a mapping

    """

    CONTEXT_SCORE = "context_score"

    def __init__(
        self,
        scorer_manager: TfIdfScorerManager,
        context_threshold: float = 0.7,
        relevant_aggregation_strategies: Optional[Iterable[EquivalentIdAggregationStrategy]] = None,
    ):
        """

        :param scorer_manager: manager to handle scoring of contexts
        :param context_threshold: only consider terms above this search threshold
        :param relevant_aggregation_strategies: Only consider these strategies when selecting synonyms from the
            synonym database, when building a representation. If none, all strategies will be considered
        """
        self.context_threshold = context_threshold
        self.relevant_aggregation_strategies: Set[EquivalentIdAggregationStrategy] = (
            {EquivalentIdAggregationStrategy.UNAMBIGUOUS}
            if relevant_aggregation_strategies is None
            else set(relevant_aggregation_strategies)
        )
        self.synonym_db = SynonymDatabase()
        self.scorer_manager = scorer_manager
        self.parser_name_to_doc_representation: Dict[str, np.ndarray] = {}

    @functools.lru_cache(maxsize=1)
    def prepare(self, document: Document):
        """
        build document representations by parser names here, and store in a dict. This method is cached so
        we don't need to call it multiple times per document
        :param document:
        :return:
        """
        self.parser_name_to_doc_representation.clear()
        parser_names = set(
            term.parser_name
            for ent in document.get_entities()
            for term in ent.syn_term_to_synonym_terms
        )
        for parser_name in parser_names:
            scorer = self.scorer_manager.parser_to_scorer[parser_name]
            self.parser_name_to_doc_representation[
                parser_name
            ] = self.cacheable_build_document_representation(scorer=scorer, doc=document)

    @staticmethod
    @functools.lru_cache(maxsize=20)
    def cacheable_build_document_representation(
        scorer: TfIdfDocumentScorer, doc: Document
    ) -> np.ndarray:
        """
        static cached method so we don't need to recalculate document representation between different instances
        of TfIdfDisambiguationStrategy
        :param scorer:
        :param doc:
        :return:
        """
        strings = " ".join(x.match_norm for x in doc.get_entities())
        return scorer.transform(strings=[strings])

    def build_id_set_representation(
        self,
        parser_name: str,
        id_sets: Set[EquivalentIdSet],
    ) -> Dict[NormalisedSynonymStr, Set[EquivalentIdSet]]:
        result = defaultdict(set)
        for id_set in id_sets:
            for idx in id_set.ids:

                syns_this_id = self.synonym_db.get_syns_for_id(
                    parser_name,
                    idx,
                    self.relevant_aggregation_strategies,
                )
                for syn in syns_this_id:
                    result[syn].add(id_set)
        return result

    def disambiguate(
        self, id_sets: Set[EquivalentIdSet], document: Document, parser_name: str
    ) -> Set[EquivalentIdSet]:
        scorer = self.scorer_manager.parser_to_scorer.get(parser_name)
        if scorer is None:
            return set()

        document_query_matrix = self.parser_name_to_doc_representation[parser_name]
        id_set_representation = self.build_id_set_representation(parser_name, id_sets)
        if len(id_set_representation) == 0:
            return set()
        else:
            indexed_non_ambiguous_syns = list(id_set_representation.keys())
            for best_syn, score in scorer(indexed_non_ambiguous_syns, document_query_matrix):
                if score >= self.context_threshold and len(id_set_representation[best_syn]) == 1:
                    return id_set_representation[best_syn]
            else:
                return set()


class AnnotationLevelDisambiguationStrategy(DisambiguationStrategy):
    def prepare(self, document: Document):
        pass

    def __init__(self):
        self.metadata_db = MetadataDatabase()

    def disambiguate(
        self, id_sets: Set[EquivalentIdSet], document: Document, parser_name: str
    ) -> Set[EquivalentIdSet]:
        score_to_id_set = defaultdict(set)
        for id_set in id_sets:
            for idx in id_set.ids:
                score = self.metadata_db.get_by_idx(parser_name, idx).get("annotation_score", 0)
                score_to_id_set[score].add(id_set)
        best = max(score_to_id_set.keys())

        return score_to_id_set[best]
