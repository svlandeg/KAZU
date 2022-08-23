import abc
from abc import abstractmethod
from functools import cached_property
from typing import Tuple, List, Dict, Optional, Iterable, Set
import pandas as pd

from kazu.modelling.database.in_memory_db import SynonymDatabase
from kazu.utils.string_normalizer import StringNormalizer


class AnnotationLookup:
    def __init__(self, annotations_path: str):
        self.annotations = self.df_to_dict(pd.read_csv(annotations_path))

    def df_to_dict(self, df: pd.DataFrame) -> Dict[str, Dict]:
        return df.set_index("match").to_dict(orient="index")

    def __call__(self, synonym: str) -> Optional[Tuple[bool, str]]:
        annotation_info = self.annotations.get(synonym)
        if annotation_info:
            action = annotation_info["action"]
            if action == "keep":
                return True, "annotated_keep"
            elif action == "drop":
                return False, "annotated_drop"
            else:
                raise ValueError(f"{action} is not valid")
        else:
            return None


class BlackLister(abc.ABC):
    """
    applies entity class specific rules to a synonym, to see if it should be blacklisted or not
    """

    # def _collect_syn_set

    @abstractmethod
    def __call__(self, synonym: str) -> Tuple[bool, str]:
        """

        :param synonym: synonym to test
        :return: tuple of whether synoym is good True|False, and the reason for the decision
        """
        raise NotImplementedError()

    @abstractmethod
    def clear_caches(self):
        """Delete caches that aren't needed when synonym generation is done.

        At the moment, this just refers to caches of synonyms for other entity types. However, we
        want to be able to call this on all blacklisters, so we need a no-op implementation in the base
        class to be overriden.

        It would seem like this is a good use case for context managers, but python doesn't support a variable
        number of context managers based on an iterable as we would want in this case."""
        raise NotImplementedError()


def _build_synonym_set(database: SynonymDatabase, synonym_sources: Iterable[str]) -> Set[str]:
    syns = set()
    for synonym_source in synonym_sources:
        syns.update(set(database.get_all(synonym_source).keys()))
    return syns


class DrugBlackLister:
    # CHEMBL drug names are often confused with genes and anatomy, for some reason
    def __init__(
        self,
        annotation_lookup: AnnotationLookup,
        anatomy_synonym_sources: List[str],
        gene_synonym_sources: List[str],
    ):
        self.annotation_lookup = annotation_lookup
        self.syn_db = SynonymDatabase()
        self.anatomy_synonym_sources = anatomy_synonym_sources
        self.gene_synonym_sources = gene_synonym_sources

    @cached_property
    def gene_syns(self):
        return _build_synonym_set(self.db, self.gene_synonym_sources)

    @cached_property
    def anat_syns(self):
        return _build_synonym_set(self.db, self.anatomy_synonym_sources)

    def clear_caches(self):
        del self.gene_syns
        del self.anat_syns

    def __call__(self, synonym: str) -> Tuple[bool, str]:
        lookup_result = self.annotation_lookup(synonym)
        if lookup_result:
            return lookup_result
        else:
            norm = StringNormalizer.normalize(synonym)
            if norm in self.anat_syns:
                return False, "likely_anatomy"
            elif norm in self.gene_syns:
                return False, "likely_gene"
            elif len(synonym) <= 3 and not StringNormalizer.is_symbol_like(False, synonym):
                return False, "likely_bad_synonym"
            else:
                return True, "not_blacklisted"


class GeneBlackLister:
    # OT gene names are often confused with diseases,
    def __init__(
        self,
        annotation_lookup: AnnotationLookup,
        disease_synonym_sources: List[str],
        gene_synonym_sources: List[str],
    ):
        self.annotation_lookup = annotation_lookup
        self.syn_db = SynonymDatabase()
        self.disease_synonym_sources = disease_synonym_sources
        self.gene_synonym_sources = gene_synonym_sources

    @cached_property
    def disease_syns(self):
        return _build_synonym_set(self.db, self.disease_synonym_sources)

    @cached_property
    def gene_syns(self):
        return _build_synonym_set(self.db, self.gene_synonym_sources)

    def clear_caches(self):
        del self.disease_syns
        del self.gene_syns

    def __call__(self, synonym: str) -> Tuple[bool, str]:
        lookup_result = self.annotation_lookup(synonym)
        if lookup_result:
            return lookup_result
        else:
            if synonym in self.gene_syns:
                return True, "not_blacklisted"
            elif StringNormalizer.normalize(synonym) in self.disease_syns:
                return False, "likely_disease"
            elif len(synonym) <= 3 and not StringNormalizer.is_symbol_like(False, synonym):
                return False, "likely_bad_synonym"
            else:
                return True, "not_blacklisted"


class DiseaseBlackLister:
    def __init__(self, annotation_lookup: AnnotationLookup, disease_synonym_sources: List[str]):
        self.annotation_lookup = annotation_lookup
        self.syn_db = SynonymDatabase()
        self.disease_synonym_sources = disease_synonym_sources

    @cached_property
    def disease_syns(self):
        return _build_synonym_set(self.db, self.disease_synonym_sources)

    def clear_caches(self):
        del self.disease_syns

    def __call__(self, synonym: str) -> Tuple[bool, str]:
        lookup_result = self.annotation_lookup(synonym)
        if lookup_result:
            return lookup_result
        else:

            is_symbol_like = StringNormalizer.is_symbol_like(False, synonym)
            if synonym in self.disease_syns:
                return True, "not_blacklisted"
            elif is_symbol_like:
                return True, "not_blacklisted"
            elif len(synonym) <= 3 and not is_symbol_like:
                return False, "likely_bad_synonym"
            else:
                return True, "not_blacklisted"
