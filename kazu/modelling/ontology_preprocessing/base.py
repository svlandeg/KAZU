import dataclasses
import json
import logging
from abc import ABC
from collections import defaultdict
from enum import auto
from typing import (
    cast,
    List,
    Tuple,
    Dict,
    Iterable,
    Set,
    Optional,
    FrozenSet,
    DefaultDict,
    Literal,
)

import pandas as pd

from kazu.data.data import (
    EquivalentIdSet,
    EquivalentIdAggregationStrategy,
    SynonymTerm,
    SimpleValue,
    Curation,
    ParserBehaviour,
    SynonymTermBehaviour,
    SynonymTermAction,
    AssociatedIdSets,
    GlobalParserActions,
    AutoNameEnum,
    MentionConfidence,
)
from kazu.modelling.database.in_memory_db import (
    MetadataDatabase,
    SynonymDatabase,
    NormalisedSynonymStr,
    Idx,
)
from kazu.modelling.language.string_similarity_scorers import StringSimilarityScorer
from kazu.modelling.ontology_preprocessing.synonym_generation import CombinatorialSynonymGenerator
from kazu.utils.caching import kazu_disk_cache
from kazu.utils.string_normalizer import StringNormalizer
from kazu.utils.utils import PathLike, as_path

# dataframe column keys
DEFAULT_LABEL = "default_label"
IDX = "idx"
SYN = "syn"
MAPPING_TYPE = "mapping_type"
SOURCE = "source"
DATA_ORIGIN = "data_origin"
IdsAndSource = Set[Tuple[str, str]]

logger = logging.getLogger(__name__)


class CurationException(Exception):
    pass


def load_curated_terms(
    path: PathLike,
) -> List[Curation]:
    """
    Load :class:`kazu.data.data.Curation`\\ s from a file path.

    :param path: path to json lines file that map to :class:`kazu.data.data.Curation`
    :return:
    """
    curations_path = as_path(path)
    if curations_path.exists():
        with curations_path.open(mode="r") as jsonlf:
            curations = [Curation.from_json(line) for line in jsonlf]
    else:
        raise ValueError(f"curations do not exist at {path}")
    return curations


def load_global_actions(
    path: PathLike,
) -> GlobalParserActions:
    """
    Load an instance of GlobalParserActions  from a file path.

    :param path: path to a json serialised GlobalParserActions`
    :return:
    """
    global_actions_path = as_path(path)
    if global_actions_path.exists():
        with global_actions_path.open(mode="r") as jsonlf:
            global_actions = GlobalParserActions.from_json(json.load(jsonlf))
    else:
        raise ValueError(f"curations do not exist at {path}")
    return global_actions


class CurationModificationResult(AutoNameEnum):
    ID_SET_MODIFIED = auto()
    SYNONYM_TERM_ADDED = auto()
    SYNONYM_TERM_DROPPED = auto()
    NO_ACTION = auto()


class CurationProcessor:
    """
    A CurationProcessor is responsible for modifying the set of :class:`.SynonymTerm`\\s produced by an :class:`OntologyParser`
    with any relevant :class:`.GlobalParserActions` and/or :class:`.Curation` associated with the parser. That is to say,
    this class modifies the raw data produced by a parser with any a posteriori observations about the data (such as bad
    synonyms, mismapped terms etc. Is also identifies curations that should be used for dictionary based NER.
    This class should be used before instances of :class:`.SynonymTerm`\\s are loaded into the internal database
    representation

    """

    def __init__(
        self,
        parser_name: str,
        entity_class: str,
        global_actions: Optional[GlobalParserActions],
        curations: List[Curation],
        synonym_terms: Set[SynonymTerm],
    ):
        """

        :param parser_name: name of parser to process (typically :attr:`Ontology_parser.name`)
        :param entity_class: name of parser entity_class to process (typically :attr:`Ontology_parser.entity_class`
        :param global_actions:
        :param curations:
        :param synonym_terms:
        """
        self.global_actions = global_actions
        self.entity_class = entity_class
        self.parser_name = parser_name
        self._terms_by_term_norm: Dict[NormalisedSynonymStr, SynonymTerm] = {}
        self._terms_by_id: DefaultDict[str, Set[SynonymTerm]] = defaultdict(set)
        for term in synonym_terms:
            self._update_term_lookups(term, False)
        self.curations: Set[Curation] = set(curations)
        self._curations_by_id: DefaultDict[Optional[str], Set[Curation]] = defaultdict(set)
        for curation in self.curations:
            for action in curation.actions:
                if action.associated_id_sets is None:
                    self._curations_by_id[None].add(curation)
                else:
                    for equiv_id_set in action.associated_id_sets:
                        for idx in equiv_id_set.ids:
                            self._curations_by_id[idx].add(curation)

    def _update_term_lookups(
        self, term: SynonymTerm, override: bool
    ) -> Literal[
        CurationModificationResult.SYNONYM_TERM_ADDED, CurationModificationResult.NO_ACTION
    ]:
        assert term.original_term is None

        safe_to_add = False
        maybe_existing_term = self._terms_by_term_norm.get(term.term_norm)
        if maybe_existing_term is None:
            logger.debug("adding new term %s", term)
            safe_to_add = True
        elif override:
            safe_to_add = True
            logger.debug("overriding existing term %s", maybe_existing_term)
        elif (
            len(
                term.associated_id_sets.symmetric_difference(maybe_existing_term.associated_id_sets)
            )
            > 0
        ):
            logger.debug(
                "conflict on term norms \n%s\n%s\nthe latter will be ignored",
                maybe_existing_term,
                term,
            )
        if safe_to_add:
            self._terms_by_term_norm[term.term_norm] = term
            for equiv_ids in term.associated_id_sets:
                for idx in equiv_ids.ids:
                    self._terms_by_id[idx].add(term)
            return CurationModificationResult.SYNONYM_TERM_ADDED
        else:
            return CurationModificationResult.NO_ACTION

    def _drop_synonym_term(self, synonym: NormalisedSynonymStr):
        """
        Remove a synonym term from the database, so that it cannot be
        used as a linking target

        :param name:
        :param synonym:
        :return:
        """
        try:
            term_to_remove = self._terms_by_term_norm.pop(synonym)
            for equiv_id_set in term_to_remove.associated_id_sets:
                for idx in equiv_id_set.ids:
                    terms_by_id = self._terms_by_id.get(idx)
                    if terms_by_id is not None:
                        terms_by_id.remove(term_to_remove)
            logger.debug(
                "successfully dropped %s from database for %s",
                synonym,
                self.entity_class,
            )
        except KeyError:
            logger.warning(
                "tried to drop %s from database, but key doesn't exist for %s",
                synonym,
                self.parser_name,
            )

    def _drop_id_from_all_synonym_terms(self, id_to_drop: Idx) -> Tuple[int, int]:
        """
        Remove a given id from all :class:`~kazu.data.data.SynonymTerm`\\ s.
        Drop any :class:`~kazu.data.data.SynonymTerm`\\ s with no remaining ID after removal.


        :param name:
        :param id_to_drop:
        :return: terms modified count, terms dropped count
        """
        terms_modified = 0
        terms_dropped = 0
        maybe_terms_to_modify = self._terms_by_id.pop(id_to_drop)
        if maybe_terms_to_modify is not None:
            for term_to_modify in maybe_terms_to_modify:
                result = self._drop_id_from_synonym_term(
                    id_to_drop=id_to_drop, term_to_modify=term_to_modify
                )

                if result is CurationModificationResult.SYNONYM_TERM_DROPPED:
                    terms_dropped += 1
                elif result is CurationModificationResult.ID_SET_MODIFIED:
                    terms_modified += 1
        return terms_modified, terms_dropped

    def _drop_id_from_synonym_term(
        self, id_to_drop: str, term_to_modify: SynonymTerm
    ) -> Literal[
        CurationModificationResult.ID_SET_MODIFIED,
        CurationModificationResult.SYNONYM_TERM_DROPPED,
        CurationModificationResult.NO_ACTION,
    ]:
        new_assoc_id_frozenset = self._drop_id_from_associated_id_sets(
            id_to_drop, term_to_modify.associated_id_sets
        )
        if len(new_assoc_id_frozenset.symmetric_difference(term_to_modify.associated_id_sets)) == 0:
            return CurationModificationResult.NO_ACTION
        else:
            return self._modify_or_drop_synonym_term_after_id_set_change(
                new_associated_id_sets=new_assoc_id_frozenset, synonym_term=term_to_modify
            )

    def _drop_id_from_associated_id_sets(
        self, id_to_drop: str, associated_id_sets: AssociatedIdSets
    ) -> AssociatedIdSets:
        new_assoc_id_set = set()
        for equiv_id_set in associated_id_sets:
            updated_ids_and_source = frozenset(
                id_tup for id_tup in equiv_id_set.ids_and_source if id_tup[0] != id_to_drop
            )
            if len(updated_ids_and_source) > 0:
                updated_equiv_id_set = EquivalentIdSet(updated_ids_and_source)
                new_assoc_id_set.add(updated_equiv_id_set)
        new_assoc_id_frozenset = frozenset(new_assoc_id_set)
        return new_assoc_id_frozenset

    def _drop_equivalent_id_set_from_synonym_term(
        self, synonym: NormalisedSynonymStr, id_set_to_drop: EquivalentIdSet
    ) -> Literal[
        CurationModificationResult.ID_SET_MODIFIED, CurationModificationResult.SYNONYM_TERM_DROPPED
    ]:
        """
        Remove an :class:`~kazu.data.data.EquivalentIdSet` from a :class:`~kazu.data.data.SynonymTerm`\\ ,
        dropping the term altogether if no others remain.


        :param name:
        :param synonym:
        :param id_set_to_drop:
        :return:
        """

        synonym_term = self._terms_by_term_norm[synonym]
        modifiable_id_sets = set(synonym_term.associated_id_sets)
        modifiable_id_sets.discard(id_set_to_drop)
        result = self._modify_or_drop_synonym_term_after_id_set_change(
            frozenset(modifiable_id_sets), synonym_term
        )
        return result

    def _modify_or_drop_synonym_term_after_id_set_change(
        self, new_associated_id_sets: AssociatedIdSets, synonym_term: SynonymTerm
    ) -> Literal[
        CurationModificationResult.ID_SET_MODIFIED, CurationModificationResult.SYNONYM_TERM_DROPPED
    ]:
        result: Literal[
            CurationModificationResult.ID_SET_MODIFIED,
            CurationModificationResult.SYNONYM_TERM_DROPPED,
        ]
        if len(new_associated_id_sets) > 0:
            if new_associated_id_sets == synonym_term.associated_id_sets:
                raise ValueError(
                    "function called inappropriately where the id sets haven't changed. This"
                    "has failed as it will otherwise modify the value of aggregated_by, when"
                    "nothing has changed"
                )
            new_term = dataclasses.replace(
                synonym_term,
                associated_id_sets=new_associated_id_sets,
                aggregated_by=EquivalentIdAggregationStrategy.MODIFIED_BY_CURATION,
            )
            add_result = self._update_term_lookups(new_term, True)
            assert add_result is CurationModificationResult.SYNONYM_TERM_ADDED
            result = CurationModificationResult.ID_SET_MODIFIED
        else:
            # if there are no longer any id sets associated with the record, remove it completely
            self._drop_synonym_term(synonym_term.term_norm)
            result = CurationModificationResult.SYNONYM_TERM_DROPPED
        return result

    def export_ner_curations_and_final_terms(
        self,
    ) -> Tuple[Optional[List[Curation]], Set[SynonymTerm]]:
        """
        Perform any updates required to the synonym terms as specified in the
        curations/global actions


        :param synonym_terms:
        :return:
        """
        self._process_global_actions()
        curations_for_ner = self._process_curations()
        return curations_for_ner, set(self._terms_by_term_norm.values())

    def _process_curations(self) -> List[Curation]:
        safe_curations, conflicts = self.analyse_conflicts_in_curations(self.curations)
        for conflict_lst in conflicts:
            message = (
                "\n\nconflicting curations detected\n\n"
                + "\n".join(curation.to_json() for curation in conflict_lst)
                + "\n"
            )

            logger.warning(message)

        curation_for_ner = []
        for curation in sorted(safe_curations, key=lambda x: x.source_term is not None):
            maybe_curation_with_term_norm_actions = self._process_curation(curation)
            if maybe_curation_with_term_norm_actions is not None:
                curation_for_ner.append(maybe_curation_with_term_norm_actions)
        return curation_for_ner

    def _drop_id_from_curation(self, idx: str):
        """
        Remove an ID from the curation. If the curation is no longer valid after this action, it will be discarded

        :param idx: the id to remove
        :param curations: the curations that contain this id
        :return: a list of modified curations
        """
        affected_curations = set(self._curations_by_id.get(idx))
        if affected_curations is not None:
            for affected_curation in affected_curations:
                new_actions = []
                for action in affected_curation.actions:
                    if action.associated_id_sets is None:
                        new_actions.append(action)
                    else:

                        updated_assoc_id_set = self._drop_id_from_associated_id_sets(
                            id_to_drop=idx, associated_id_sets=action.associated_id_sets
                        )
                        if len(updated_assoc_id_set) == 0:
                            logger.warning(
                                "curation id %s has had all linking target ids removed by a global action, and will be"
                                " ignored. Parser name: %s",
                                affected_curation._id,
                                self.parser_name,
                            )
                            continue
                        if len(updated_assoc_id_set) < len(action.associated_id_sets):
                            logger.info(
                                "curation found with ids that have been removed via a global action. These will be filtered"
                                " from the curation action. Parser name: %s, new ids: %s, curation id: %s",
                                self.parser_name,
                                updated_assoc_id_set,
                                affected_curation._id,
                            )
                        new_actions.append(
                            dataclasses.replace(action, associated_id_sets=updated_assoc_id_set)
                        )
                if len(new_actions) > 0:
                    new_curation = dataclasses.replace(
                        affected_curation, actions=tuple(new_actions)
                    )
                    self.curations.add(new_curation)
                    self._curations_by_id[idx].add(new_curation)
                else:
                    logger.info(
                        "curation no longer has any relevant actions, and will be discarded"
                        " Parser name: %s, curation id: %s",
                        self.parser_name,
                        affected_curation._id,
                    )
                self.curations.remove(affected_curation)
                self._curations_by_id[idx].remove(affected_curation)

    def analyse_conflicts_in_curations(
        self, curations: Set[Curation]
    ) -> Tuple[Set[Curation], List[Set[Curation]]]:
        """
        Check to see if a list of curations contain conflicts.

        Conflicts can occur if two or more curations normalise to the same NormalisedSynonymStr,
        but refer to different AssociatedIdSets, and one of their actions is attempting to add
        a SynonymTerm to the database. This would create an ambiguity over which AssociatedIdSets
        is appropriate for the normalised term

        :param curations:
        :return: safe curations set, list of conflicting curations sets
        """
        curations_by_term_norm = defaultdict(set)
        conflicts = []
        safe = set()

        for curation in curations:
            if curation.source_term is not None:
                # inherited curations cannot conflict as they use term norm of source term
                safe.add(curation)
            else:
                curations_by_term_norm[curation.curated_synonym_norm(self.entity_class)].add(
                    curation
                )

        for term_norm, potentially_conflicting_curations in curations_by_term_norm.items():
            conflicting_id_sets = set()
            curations_by_assoc_id_set = {}

            for curation in potentially_conflicting_curations:
                for action in curation.actions:
                    if (
                        action.behaviour is SynonymTermBehaviour.ADD_FOR_NER_AND_LINKING
                        or action.behaviour is SynonymTermBehaviour.ADD_FOR_LINKING_ONLY
                    ):
                        conflicting_id_sets.add(action.associated_id_sets)
                        curations_by_assoc_id_set[action.associated_id_sets] = curation

            if len(conflicting_id_sets) > 1:
                conflicts.append(potentially_conflicting_curations)
            else:
                safe.update(potentially_conflicting_curations)
        return safe, conflicts

    def _process_curation(self, curation: Curation) -> Optional[Curation]:
        term_norm = curation.term_norm_for_linking(self.entity_class)
        for action in curation.actions:
            if action.behaviour is SynonymTermBehaviour.IGNORE:
                logger.debug("ignoring unwanted curation: %s for %s", curation, self.parser_name)
                return None
            elif action.behaviour is SynonymTermBehaviour.INHERIT_FROM_SOURCE_TERM:
                logger.debug(
                    "action inherits linking behaviour from %s for %s",
                    curation.source_term,
                    self.parser_name,
                )
                if term_norm not in self._terms_by_term_norm:
                    logger.warning(
                        "curation %s is has no linking target in the synonym database, and will be ignored",
                        curation,
                    )
                    return None
                else:
                    return curation
            elif action.behaviour is SynonymTermBehaviour.DROP_SYNONYM_TERM_FOR_LINKING:
                self._drop_synonym_term(term_norm)
                return None

            assert action.associated_id_sets is not None
            if action.behaviour is SynonymTermBehaviour.DROP_ID_SET_FROM_SYNONYM_TERM:
                self._drop_id_set_from_synonym_term(
                    action.associated_id_sets,
                    term_norm=term_norm,
                )
            elif (
                action.behaviour is SynonymTermBehaviour.ADD_FOR_LINKING_ONLY
                or action.behaviour is SynonymTermBehaviour.ADD_FOR_NER_AND_LINKING
            ):
                self._attempt_to_add_database_entry_for_curation(
                    curation_associated_id_set=action.associated_id_sets,
                    curated_synonym=curation.curated_synonym,
                    curation_term_norm=term_norm,
                )
                return curation
            else:
                raise ValueError(f"unknown behaviour for parser {self.parser_name}, {action}")
        return None

    def _process_global_actions(self) -> Set[str]:
        dropped_ids: Set[str] = set()
        if self.global_actions is None:
            return dropped_ids

        for action in self.global_actions.parser_behaviour(self.parser_name):
            if action.behaviour is ParserBehaviour.DROP_IDS_FROM_PARSER:
                ids = action.parser_to_target_id_mappings[self.parser_name]
                terms_modified, terms_dropped = 0, 0
                for idx in ids:
                    (
                        terms_modified_this_id,
                        terms_dropped_this_id,
                    ) = self._drop_id_from_all_synonym_terms(idx)
                    terms_modified += terms_modified_this_id
                    terms_dropped += terms_dropped_this_id
                    if terms_modified_this_id == 0 and terms_dropped_this_id == 0:
                        logger.warning("failed to drop %s from %s", idx, self.parser_name)
                    else:
                        dropped_ids.add(idx)
                        logger.debug(
                            "dropped ID %s from %s. SynonymTerm modified count: %s, SynonymTerm dropped count: %s",
                            idx,
                            self.parser_name,
                            terms_modified_this_id,
                            terms_dropped_this_id,
                        )
                    self._drop_id_from_curation(idx=idx)

            else:
                raise ValueError(f"unknown behaviour for parser {self.parser_name}, {action}")
        return dropped_ids

    def _drop_id_set_from_synonym_term(
        self, equivalent_id_sets_to_drop: AssociatedIdSets, term_norm: NormalisedSynonymStr
    ):
        """Remove an id set from a :class:`.SynonymTerm`.

        :param id_set: ids that should be removed from the :class:`.SynonymTerm`
        :param curated_synonym: passed to :class:`.StringNormalizer` to look up the :class:`.SynonymTerm`
        :return:
        """
        target_term_to_modify = self._terms_by_term_norm[term_norm]
        for equiv_id_set_to_drop in equivalent_id_sets_to_drop:
            if equiv_id_set_to_drop in target_term_to_modify.associated_id_sets:
                drop_equivalent_id_set_from_synonym_term_result = (
                    self._drop_equivalent_id_set_from_synonym_term(term_norm, equiv_id_set_to_drop)
                )
                if (
                    drop_equivalent_id_set_from_synonym_term_result
                    is CurationModificationResult.ID_SET_MODIFIED
                ):
                    logger.debug(
                        "dropped an EquivalentIdSet containing an id from %s for key %s for %s",
                        equivalent_id_sets_to_drop,
                        term_norm,
                        self.parser_name,
                    )
                else:
                    logger.debug(
                        "dropped a SynonymTerm containing an id from %s for key %s for %s",
                        equivalent_id_sets_to_drop,
                        term_norm,
                        self.parser_name,
                    )
            else:
                logger.warning(
                    "%s was asked to remove ids %s from a SynonymTerm (key: <%s>), but the ids were not found on this term",
                    self.parser_name,
                    equiv_id_set_to_drop,
                    term_norm,
                )

    def _attempt_to_add_database_entry_for_curation(
        self,
        curation_term_norm: NormalisedSynonymStr,
        curation_associated_id_set: AssociatedIdSets,
        curated_synonym: str,
    ) -> Literal[
        CurationModificationResult.SYNONYM_TERM_ADDED, CurationModificationResult.NO_ACTION
    ]:
        """
        Insert a new :class:`~kazu.data.data.SynonymTerm` into the database, or return an existing
        matching one if already present.


        :param curation_associated_id_set: if a :class:`.SynonymTerm` already exists for the normalised version of curated_synonym, this should be
            a subset of one of the :class:`.EquivalentIdSet`\\s assocaited with that term. If a :class:`.SynonymTerm` does not
            exist, the parser will attempt to find an existing instance of :class:`.EquivalentIdSet` that matches the ids in
            id_set. If no appropriate :class:`.EquivalentIdSet` exists, a new instance of :class:`kazu.data.data.AssociatedIdSets`
            will be created, containing an instance of :class:`.EquivalentIdSet` for each id in id_set
        :param curated_synonym: passed to :class:`.StringNormalizer` to see if a suitable :class:`.SynonymTerm` exists
        :return:
        """
        log_prefix = f"{self.parser_name} attempting to create synonym term for <{curated_synonym}> term_norm: <{curation_term_norm}> IDs: {curation_associated_id_set}"

        # look up the term norm in the db

        maybe_existing_synonym_term = self._terms_by_term_norm.get(curation_term_norm)

        if maybe_existing_synonym_term is not None:
            if curation_associated_id_set.issubset(maybe_existing_synonym_term.associated_id_sets):
                logger.debug(
                    f"{log_prefix} but term_norm <{curation_term_norm}> already exists in synonym database."
                    f"since this SynonymTerm includes all ids in id_set, no action is required. {maybe_existing_synonym_term.associated_id_sets}"
                )
                return CurationModificationResult.NO_ACTION
            else:
                raise CurationException(
                    f"{log_prefix} but term_norm <{curation_term_norm}> already exists in synonym database, and its\n"
                    f"associated_id_sets don't contain all the ids in id_set. Creating a new\n"
                    f"SynonymTerm would override an existing entry, resulting in inconsistencies.\n"
                    f"This can happen if a synonym appears twice in the underlying ontology,\n"
                    f"with multiple identifiers attached\n"
                    f"Possible mitigations:\n"
                    f"1) use a SynonymTermAction to drop the existing SynonymTerm from the database first.\n"
                    f"2) change the target id set of the curation to match the existing entry\n"
                    f"\t(i.e. {maybe_existing_synonym_term.associated_id_sets}\n"
                    f"3) Change the string normalizer function to generate unique term_norms\n"
                )

        # see if we've already had to group all the ids in this id_set in some way for a different synonym
        # we need the sort as we want to try to match to the smallest instance of AssociatedIdSets first.
        # This is because this is the least ambiguous - if we don't sort, we're potentially matching to
        # a larger, more ambiguous one than we need, and are potentially creating a disambiguation problem
        # where none exists
        maybe_reusable_id_set = None
        for equiv_id_set in curation_associated_id_set:
            for idx in equiv_id_set.ids:

                maybe_assoc_id_sets_for_this_id = set()
                for term in self._terms_by_id.get(idx, []):
                    maybe_assoc_id_sets_for_this_id.add(term.associated_id_sets)

                if len(maybe_assoc_id_sets_for_this_id) == 0:
                    raise CurationException(
                        f"{log_prefix} but could not find {idx} for this parser"
                    )
                else:
                    if maybe_reusable_id_set is None:
                        maybe_reusable_id_set = min(maybe_assoc_id_sets_for_this_id, key=len)
                    else:
                        smallest_set_this_iteration = min(maybe_assoc_id_sets_for_this_id, key=len)
                        if len(smallest_set_this_iteration) < len(maybe_reusable_id_set):
                            maybe_reusable_id_set = smallest_set_this_iteration

        if maybe_reusable_id_set is not None:
            logger.debug(
                f"using smallest AssociatedIDSet that matches all IDs for new SynonymTerm: {curation_associated_id_set}"
            )
            associated_id_set_for_new_synonym_term = maybe_reusable_id_set
        else:
            # something to be careful about here: we assume that if no appropriate AssociatedIdSet can be
            # reused, we need to create a new one. If one cannot be found, we 'assume' that the input
            # id_sets must relate to different concepts (i.e. - we create a new equivalent ID set for each
            # id, which must later be disambiguated). This assumption may be inappropriate in cases. This is
            # best avoided by having the curation contain as few IDs as possible, such that the chances
            # that an existing AssociatedIdSet can be reused are higher.
            logger.debug(
                f"no appropriate AssociatedIdSets exist for the set {curation_associated_id_set}, so a new one will be created"
            )
            associated_id_set_for_new_synonym_term = curation_associated_id_set

        is_symbolic = StringNormalizer.classify_symbolic(curated_synonym, self.entity_class)
        new_term = SynonymTerm(
            term_norm=curation_term_norm,
            terms=frozenset((curated_synonym,)),
            is_symbolic=is_symbolic,
            mapping_types=frozenset(("kazu_curated",)),
            associated_id_sets=associated_id_set_for_new_synonym_term,
            parser_name=self.parser_name,
            aggregated_by=EquivalentIdAggregationStrategy.MODIFIED_BY_CURATION,
        )
        return self._update_term_lookups(new_term, False)


class OntologyParser(ABC):
    """
    Parse an ontology (or similar) into a set of outputs suitable for NLP entity linking.
    Implementations should have a class attribute 'name' to something suitably representative.
    The key method is parse_to_dataframe, which should convert an input source to a dataframe suitable
    for further processing.

    The other important method is find_kb. This should parse an ID string (if required) and return the underlying
    source. This is important for composite resources that contain identifiers from different seed sources

    Generally speaking, when parsing a data source, synonyms that are symbolic (as determined by
    the StringNormalizer) that refer to more than one id are more likely to be ambiguous. Therefore,
    we assume they refer to unique concepts (e.g. COX 1 could be 'ENSG00000095303' OR
    'ENSG00000198804', and thus they will yield multiple instances of EquivalentIdSet. Non symbolic
    synonyms (i.e. noun phrases) are far less likely to refer to distinct entities, so we might
    want to merge the associated ID's non-symbolic ambiguous synonyms into a single EquivalentIdSet.
    The result of StringNormalizer.is_symbolic forms the is_symbolic parameter to .score_and_group_ids.

    If the underlying knowledgebase contains more than one entity type, muliple parsers should be
    implemented, subsetting accordingly (e.g. MEDDRA_DISEASE, MEDDRA_DIAGNOSTIC).
    """

    # the synonym table should have these (and only these columns)
    all_synonym_column_names = [IDX, SYN, MAPPING_TYPE]
    # the metadata table should have at least these columns (note, IDX will become the index)
    minimum_metadata_column_names = [DEFAULT_LABEL, DATA_ORIGIN]

    def __init__(
        self,
        in_path: str,
        entity_class: str,
        name: str,
        string_scorer: Optional[StringSimilarityScorer] = None,
        synonym_merge_threshold: float = 0.70,
        data_origin: str = "unknown",
        synonym_generator: Optional[CombinatorialSynonymGenerator] = None,
        curations: Optional[List[Curation]] = None,
        global_actions: Optional[GlobalParserActions] = None,
    ):
        """
        :param in_path: Path to some resource that should be processed (e.g. owl file, db config, tsv etc)
        :param entity_class: The entity class to associate with this parser throughout the pipeline.
            Also used in the parser when calling StringNormalizer to determine the class-appropriate behaviour.
        :param name: A string to represent a parser in the overall pipeline. Should be globally unique
        :param string_scorer: Optional protocol of StringSimilarityScorer.  Used for resolving ambiguous symbolic
            synonyms via similarity calculation of the default label associated with the conflicted labels. If no
            instance is provided, all synonym conflicts will be assumed to refer to different concepts. This is not
            recommended!
        :param synonym_merge_threshold: similarity threshold to trigger a merge of conflicted synonyms into a single
            EquivalentIdSet. See docs for score_and_group_ids for further details
        :param data_origin: The origin of this dataset - e.g. HGNC release 2.1, MEDDRA 24.1 etc. Note, this is different
            from the parser.name, as is used to identify the origin of a mapping back to a data source
        :param synonym_generator: optional CombinatorialSynonymGenerator. Used to generate synonyms for dictionary
            based NER matching
        :param curations: Curations to apply to the parser
        """

        self.in_path = in_path
        self.entity_class = entity_class
        self.name = name
        if string_scorer is None:
            logger.warning(
                "no string scorer configured for %s. Synonym resolution disabled.", self.name
            )
        self.string_scorer = string_scorer
        self.synonym_merge_threshold = synonym_merge_threshold
        self.data_origin = data_origin
        self.synonym_generator = synonym_generator
        self.curations = curations
        self.global_actions = global_actions
        self.parsed_dataframe: Optional[pd.DataFrame] = None
        self.metadata_db = MetadataDatabase()
        self.synonym_db = SynonymDatabase()

    def find_kb(self, string: str) -> str:
        """
        split an IDX somehow to find the ontology SOURCE reference

        :param string: the IDX string to process
        :return:
        """
        raise NotImplementedError()

    def resolve_synonyms(self, synonym_df: pd.DataFrame) -> Set[SynonymTerm]:

        result = set()
        synonym_df["syn_norm"] = synonym_df[SYN].apply(
            StringNormalizer.normalize, entity_class=self.entity_class
        )

        for i, row in (
            synonym_df[["syn_norm", SYN, IDX, MAPPING_TYPE]]
            .groupby(["syn_norm"])
            .agg(set)
            .reset_index()
            .iterrows()
        ):

            syn_set = row[SYN]
            mapping_type_set: FrozenSet[str] = frozenset(row[MAPPING_TYPE])
            syn_norm = row["syn_norm"]
            if len(syn_set) > 1:
                logger.debug("normaliser has merged %s into a single term: %s", syn_set, syn_norm)

            is_symbolic = all(
                StringNormalizer.classify_symbolic(x, self.entity_class) for x in syn_set
            )

            ids: Set[str] = row[IDX]
            ids_and_source = set(
                (
                    idx,
                    self.find_kb(idx),
                )
                for idx in ids
            )
            associated_id_sets, agg_strategy = self.score_and_group_ids(
                ids_and_source, is_symbolic, syn_set
            )

            synonym_term = SynonymTerm(
                term_norm=syn_norm,
                terms=frozenset(syn_set),
                is_symbolic=is_symbolic,
                mapping_types=mapping_type_set,
                associated_id_sets=associated_id_sets,
                parser_name=self.name,
                aggregated_by=agg_strategy,
            )

            result.add(synonym_term)

        return result

    def score_and_group_ids(
        self,
        ids_and_source: IdsAndSource,
        is_symbolic: bool,
        original_syn_set: Set[str],
    ) -> Tuple[AssociatedIdSets, EquivalentIdAggregationStrategy]:
        """
        for a given data source, one normalised synonym may map to one or more id. In some cases, the ID may be
        duplicate/redundant (e.g. there are many chembl ids for paracetamol). In other cases, the ID may refer to
        distinct concepts (e.g. COX 1 could be 'ENSG00000095303' OR 'ENSG00000198804').


        Since synonyms from data sources are confused in such a manner, we need to decide some way to cluster them into
        a single SynonymTerm concept, which in turn is a container for one or more EquivalentIdSet (depending on
        whether the concept is ambiguous or not)

        The job of score_and_group_ids is to determine how many EquivalentIdSet's for a given set of ids should be
        produced.

        The default algorithm (which can be overridden by concrete parser implementations) works as follows:

        1. If no StringScorer is configured, create an EquivalentIdSet for each id (strategy NO_STRATEGY -
           not recommended)
        2. If only one ID is referenced, or the associated normalised synonym string is not symbolic, group the
           ids into a single EquivalentIdSet (strategy UNAMBIGUOUS)
        3. otherwise, compare the default label associated with each ID to every other default label. If it's above
           self.synonym_merge_threshold, merge into one EquivalentIdSet, if not, create a new one

        recommendation: Use the SapbertStringSimilarityScorer for comparison

        IMPORTANT NOTE: any calls to this method requires the metadata DB to be populated, as this is the store of
        DEFAULT_LABEL

        :param ids_and_source: ids to determine appropriate groupings of, and their associated sources
        :param is_symbolic: is the underlying synonym symbolic?
        :param original_syn_set: original synonyms associated with ids
        :return:
        """
        if self.string_scorer is None:
            # the NO_STRATEGY aggregation strategy assumes all synonyms are ambiguous
            return (
                frozenset(
                    EquivalentIdSet(ids_and_source=frozenset((single_id_and_source,)))
                    for single_id_and_source in ids_and_source
                ),
                EquivalentIdAggregationStrategy.NO_STRATEGY,
            )
        else:

            if len(ids_and_source) == 1:
                return (
                    frozenset((EquivalentIdSet(ids_and_source=frozenset(ids_and_source)),)),
                    EquivalentIdAggregationStrategy.UNAMBIGUOUS,
                )

            if not is_symbolic:
                return (
                    frozenset((EquivalentIdSet(ids_and_source=frozenset(ids_and_source)),)),
                    EquivalentIdAggregationStrategy.MERGED_AS_NON_SYMBOLIC,
                )
            else:
                # use similarity to group ids into EquivalentIdSets

                DefaultLabels = Set[str]
                id_list: List[Tuple[IdsAndSource, DefaultLabels]] = []
                for id_and_source_tuple in ids_and_source:
                    default_label = cast(
                        str,
                        self.metadata_db.get_by_idx(self.name, id_and_source_tuple[0])[
                            DEFAULT_LABEL
                        ],
                    )
                    most_similar_id_set = None
                    best_score = 0.0
                    for id_and_default_label_set in id_list:
                        sim = max(
                            self.string_scorer(default_label, other_label)
                            for other_label in id_and_default_label_set[1]
                        )
                        if sim > self.synonym_merge_threshold and sim > best_score:
                            most_similar_id_set = id_and_default_label_set
                            best_score = sim

                    # for the first label, the above for loop is a no-op as id_sets is empty
                    # and the below if statement will be true.
                    # After that, it will be True if the id under consideration should not
                    # merge with any existing group and should get its own EquivalentIdSet
                    if not most_similar_id_set:
                        id_list.append(
                            (
                                {id_and_source_tuple},
                                {default_label},
                            )
                        )
                    else:
                        most_similar_id_set[0].add(id_and_source_tuple)
                        most_similar_id_set[1].add(default_label)

                return (
                    frozenset(
                        EquivalentIdSet(ids_and_source=frozenset(ids_and_source))
                        for ids_and_source, _ in id_list
                    ),
                    EquivalentIdAggregationStrategy.RESOLVED_BY_SIMILARITY,
                )

    def _attempt_to_add_database_entry_for_curation(
        self, id_set: Set[str], curated_synonym: str
    ) -> SynonymTerm:
        """
        Insert a new :class:`~kazu.data.data.SynonymTerm` into the database, or return an existing
        matching one if already present.



        :param id_set: if a :class:`.SynonymTerm` already exists for the normalised version of curated_synonym, this should be
            a subset of one of the :class:`.EquivalentIdSet`\\s assocaited with that term. If a :class:`.SynonymTerm` does not
            exist, the parser will attempt to find an existing instance of :class:`.EquivalentIdSet` that matches the ids in
            id_set. If no appropriate :class:`.EquivalentIdSet` exists, a new instance of :class:`kazu.data.data.AssociatedIdSets`
            will be created, containing an instance of :class:`.EquivalentIdSet` for each id in id_set
        :param curated_synonym: passed to :class:`.StringNormalizer` to see if a suitable :class:`.SynonymTerm` exists
        :return:
        """

        term_norm = StringNormalizer.normalize(curated_synonym, entity_class=self.entity_class)
        log_prefix = "%(parser_name)s attempting to create synonym term for <%(synonym)s> term_norm: <%(term_norm)s> IDs: %(ids)s}"
        log_formatting_dict: Dict[str, Any] = {
            "parser_name": self.name,
            "synonym": curated_synonym,
            "term_norm": term_norm,
            "ids": id_set,
        }

        # look up the term norm in the db
        try:
            maybe_existing_synonym_term = self.synonym_db.get(self.name, term_norm)
        except KeyError:
            maybe_existing_synonym_term = None

        if maybe_existing_synonym_term is not None:
            all_ids_on_existing_syn_term = set(
                id_
                for equiv_id_set in maybe_existing_synonym_term.associated_id_sets
                for id_ in equiv_id_set.ids
            )
            if id_set.issubset(all_ids_on_existing_syn_term):
                log_formatting_dict[
                    "existing_id_set"
                ] = maybe_existing_synonym_term.associated_id_sets
                logger.debug(
                    log_prefix
                    + " but term_norm <%(term_norm)s> already exists in synonym database."
                    + "since this SynonymTerm includes all ids in id_set, no action is required. %(existing_id_set)s",
                    log_formatting_dict,
                )
                return maybe_existing_synonym_term
            else:
                formatted_log_prefix = log_prefix % log_formatting_dict
                raise CurationException(
                    f"{formatted_log_prefix} but term_norm <{term_norm}> already exists in synonym database, and its\n"
                    f"associated_id_sets don't contain all the ids in id_set. Creating a new\n"
                    f"SynonymTerm would override an existing entry, resulting in inconsistencies.\n"
                    f"This can happen if a synonym appears twice in the underlying ontology,\n"
                    f"with multiple identifiers attached\n"
                    f"Possible mitigations:\n"
                    f"1) use a ParserAction to drop the existing SynonymTerm from the database first.\n"
                    f"2) change the target id set of the curation to match the existing entry\n"
                    f"\t(i.e. {all_ids_on_existing_syn_term}\n"
                    f"3) Change the string normalizer function to generate unique term_norms\n"
                )

        logger.debug(
            "no appropriate AssociatedIdSets exist for the set %s, so a new one will be created",
            id_set,
        )
        # see if we've already had to group all the ids in this id_set in some way for a different synonym
        set_of_assoc_id_set = set()
        for idx in id_set:
            assoc_id_sets_for_this_id = self.synonym_db.get_associated_id_sets_for_id(
                self.name, idx
            )
            if len(assoc_id_sets_for_this_id) == 0:
                formatted_log_prefix = log_prefix % log_formatting_dict
                raise CurationException(
                    f"{formatted_log_prefix} but could not find element {idx} of id_set {id_set} in synonym database"
                )
            set_of_assoc_id_set.update(assoc_id_sets_for_this_id)

        associated_id_set_for_new_synonym_term = None

        # see if an existing AssociatedIdSet contains all the relevant IDs
        # we need the sort as we want to try to match to the smallest instance of AssociatedIdSets first.
        # This is because this is the least ambiguous - if we don't sort, we're potentially matching to
        # a larger, more ambiguous one than we need, and are potentially creating a disambiguation problem
        # where none exists
        for associated_id_set in sorted(set_of_assoc_id_set, key=len, reverse=False):
            all_ids_in_assoc_id_set = set(
                id_ for equiv_id_set in associated_id_set for id_ in equiv_id_set.ids
            )
            if id_set.issubset(all_ids_in_assoc_id_set):
                associated_id_set_for_new_synonym_term = associated_id_set
                logger.debug(
                    "using smallest AssociatedIDSet that matches all IDs for new SynonymTerm: %s",
                    associated_id_set,
                )
                break

        if associated_id_set_for_new_synonym_term is None:
            # something to be careful about here: we assume that if no appropriate AssociatedIdSet can be
            # reused, we need to create a new one. If one cannot be found, we 'assume' that the input
            # id_sets must relate to different concepts (i.e. - we create a new equivalent ID set for each
            # id, which must later be disambiguated). This assumption may be inappropriate in cases. This is
            # best avoided by having the curation contain as few IDs as possible, such that the chances
            # that an existing AssociatedIdSet can be reused are higher.
            logger.debug(
                "no appropriate AssociatedIdSets exist for the set %s, so a new one will be created",
                id_set,
            )
            associated_id_set_for_new_synonym_term = frozenset(
                EquivalentIdSet(
                    ids_and_source=frozenset(
                        (
                            (
                                idx,
                                self.find_kb(idx),
                            ),
                        )
                    )
                )
                for idx in id_set
            )

        is_symbolic = StringNormalizer.classify_symbolic(curated_synonym, self.entity_class)
        new_term = SynonymTerm(
            term_norm=term_norm,
            terms=frozenset((curated_synonym,)),
            is_symbolic=is_symbolic,
            mapping_types=frozenset(("kazu_curated",)),
            associated_id_sets=associated_id_set_for_new_synonym_term,
            parser_name=self.name,
            aggregated_by=EquivalentIdAggregationStrategy.MODIFIED_BY_CURATION,
        )
        self.synonym_db.add(self.name, synonyms=(new_term,))
        logger.debug("%s created", new_term)
        return new_term

    def _drop_synonym_term_for_linking(self, curated_synonym: str):
        """
        Remove a :class:`.SynonymTerm` from the database.

        :param curated_synonym: passed to :class:`.StringNormalizer` to look up the :class:`.SynonymTerm`
        :return:
        """
        affected_term_key = StringNormalizer.normalize(
            curated_synonym, entity_class=self.entity_class
        )
        try:
            self.synonym_db.drop_synonym_term(self.name, affected_term_key)
            logger.debug(
                "successfully dropped %s from database for %s", affected_term_key, self.name
            )
        except KeyError:
            logger.warning(
                "tried to drop %s from database, but key doesn't exist for %s",
                affected_term_key,
                self.name,
            )

    def _drop_id_set_from_synonym_term(self, id_set: Set[str], curated_synonym: str):
        """
        Remove an id set from a :class:`.SynonymTerm`.

        :param id_set: ids that should be removed from the :class:`.SynonymTerm`
        :param curated_synonym: passed to :class:`.StringNormalizer` to look up the :class:`.SynonymTerm`
        :return:
        """
        # make a mutable copy so we can discard as we go
        mutable_id_set = set(id_set)
        affected_term_key = StringNormalizer.normalize(
            curated_synonym, entity_class=self.entity_class
        )
        target_term_to_modify = self.synonym_db.get(self.name, affected_term_key)
        for equiv_id_set in target_term_to_modify.associated_id_sets:
            if len(mutable_id_set) == 0:
                break

            if len(mutable_id_set.intersection(equiv_id_set.ids)) > 0:
                drop_equivalent_id_set_from_synonym_term_result = (
                    self.synonym_db.drop_equivalent_id_set_from_synonym_term(
                        self.name, affected_term_key, equiv_id_set
                    )
                )
                if (
                    drop_equivalent_id_set_from_synonym_term_result
                    is DBModificationResult.ID_SET_MODIFIED
                ):
                    logger.debug(
                        "dropped an EquivalentIdSet containing an id from %s for key %s for %s",
                        id_set,
                        affected_term_key,
                        self.name,
                    )
                else:
                    logger.debug(
                        "dropped a SynonymTerm containing an id from %s for key %s for %s",
                        id_set,
                        affected_term_key,
                        self.name,
                    )
                mutable_id_set.difference_update(equiv_id_set.ids)
        else:
            logger.warning(
                "Was asked to remove ids associated with a SynonymTerm (key: <%s>). However, after inspecting all"
                " EquivalentIdSets, the following ids were not found in any of them: %s. Parser name: %s",
                affected_term_key,
                mutable_id_set,
                self.name,
            )

    def process_actions(self) -> Optional[List[Curation]]:
        """
        Process any global actions or curations associated with this parser.

        :return: curations that are suitable for dictionary based NER for this parser.
        """
        if self.global_actions is not None:
            ids_dropped_through_global_actions = self._process_global_actions(self.global_actions)
        else:
            ids_dropped_through_global_actions = set()
        if self.curations is not None:
            curation_with_term_norm_actions = []
            for curation in self.curations:
                maybe_curation_with_term_norm_actions = self._process_curation(
                    curation, ids_dropped_through_global_actions
                )
                if maybe_curation_with_term_norm_actions is not None:
                    curation_with_term_norm_actions.append(maybe_curation_with_term_norm_actions)
            return curation_with_term_norm_actions
        else:
            logger.info("No curations provided for %s", self.name)
            return None

    def _update_action_for_globally_dropped_ids(
        self,
        curation_id: Dict[str, str],
        action: SynonymTermAction,
        ids_dropped_through_global_actions: Set[str],
    ) -> Optional[SynonymTermAction]:
        """
        Checks the action to see if it's id has been dropped by a global action elsewhere. If so, it's
        modified accordingly and returned. If the action will no longer work after modification, None is
        returned.

        :param curation_id:
        :param action:
        :param ids_dropped_through_global_actions:
        :return:
        """
        original_ids = action.parser_to_target_id_mappings[self.name]

        filtered_ids = original_ids.difference(ids_dropped_through_global_actions)
        if len(filtered_ids) == 0:
            logger.warning(
                "curation id %s has had all linking target ids removed by a global action, and will be"
                " ignored. Parser name: %s",
                curation_id,
                self.name,
            )
            return None
        if len(filtered_ids) < len(original_ids):
            logger.warning(
                "curation found with ids that have been removed via a global action. These will be filtered"
                "from the curation action. Parser name: %s, new ids: %s, curation id: %s",
                self.name,
                filtered_ids,
                curation_id,
            )
            action.parser_to_target_id_mappings[self.name] = filtered_ids

        return action

    def _process_curation(
        self, curation: Curation, ids_dropped_through_global_actions: Set[str]
    ) -> Optional[Curation]:
        """
        Handle any parser specific behaviour associated with a :class:`.Curation`\\.

        :param curation:
        :param ids_dropped_through_global_actions:
        :return:
        """
        maybe_updated_curation = None
        for action in curation.parser_behaviour(self.name):
            if action.behaviour is SynonymTermBehaviour.IGNORE:
                logger.debug("ignoring unwanted curation: %s for %s", curation, self.name)
            elif action.behaviour is SynonymTermBehaviour.DROP_SYNONYM_TERM_FOR_LINKING:
                self._drop_synonym_term_for_linking(curated_synonym=curation.curated_synonym)
            elif action.behaviour is SynonymTermBehaviour.DROP_ID_SET_FROM_SYNONYM_TERM:
                self._drop_id_set_from_synonym_term(
                    action.parser_to_target_id_mappings[self.name],
                    curated_synonym=curation.curated_synonym,
                )
            elif (
                action.behaviour is SynonymTermBehaviour.ADD_FOR_LINKING_ONLY
                or action.behaviour is SynonymTermBehaviour.ADD_FOR_NER_AND_LINKING
            ):
                updated_action = self._update_action_for_globally_dropped_ids(
                    curation._id, action, ids_dropped_through_global_actions
                )
                if updated_action is not None:
                    new_or_existing_term = self._attempt_to_add_database_entry_for_curation(
                        action.parser_to_target_id_mappings[self.name],
                        curated_synonym=curation.curated_synonym,
                    )
                    if action.behaviour is SynonymTermBehaviour.ADD_FOR_NER_AND_LINKING:
                        action.term_norm = new_or_existing_term.term_norm
                        maybe_updated_curation = curation
            else:
                raise ValueError(f"unknown behaviour for parser {self.name}, {action}")

        return maybe_updated_curation

    def _process_global_actions(self, global_actions: GlobalParserActions) -> Set[str]:
        """
        Process global actions associated with this parser, returning a set of any ids
        that have been dropped.

        :param global_actions:
        :return:
        """
        dropped_ids = set()
        for action in global_actions.parser_behaviour(self.name):
            if action.behaviour is ParserBehaviour.DROP_IDS_FROM_PARSER:
                ids = action.parser_to_target_id_mappings[self.name]
                terms_modified, terms_dropped = 0, 0
                for idx in ids:
                    terms_modified_this_id, terms_dropped_this_id = self.synonym_db.drop_id_from_all_synonym_terms(self.name, idx)  # type: ignore[arg-type]
                    terms_modified += terms_modified_this_id
                    terms_dropped += terms_dropped_this_id
                    if terms_modified_this_id == 0 and terms_dropped_this_id == 0:
                        logger.warning("failed to drop %s from %s", idx, self.name)
                    else:
                        dropped_ids.add(idx)
                        logger.debug(
                            "dropped ID %s from %s. SynonymTerm modified count: %s, SynonymTerm dropped count: %s",
                            idx,
                            self.name,
                            terms_modified_this_id,
                            terms_dropped_this_id,
                        )
            else:
                raise ValueError(f"unknown behaviour for parser {self.name}, {action}")
        return dropped_ids

    def _parse_df_if_not_already_parsed(self):
        if self.parsed_dataframe is None:
            self.parsed_dataframe = self.parse_to_dataframe()
            self.parsed_dataframe[DATA_ORIGIN] = self.data_origin
            self.parsed_dataframe[IDX] = self.parsed_dataframe[IDX].astype(str)
            self.parsed_dataframe.loc[
                pd.isnull(self.parsed_dataframe[DEFAULT_LABEL]), DEFAULT_LABEL
            ] = self.parsed_dataframe[IDX]

    @kazu_disk_cache.memoize(ignore={0})
    def export_metadata(self, parser_name: str) -> Dict[str, Dict[str, SimpleValue]]:
        """Export the metadata from the ontology.

        :param parser_name: name of this parser. Required for correct operation of cache
            (Note, we cannot pass self to the disk cache as the constructor consumes too much
            memory
        :return: {idx:{metadata_key:metadata_value}}
        """
        self._parse_df_if_not_already_parsed()
        assert isinstance(self.parsed_dataframe, pd.DataFrame)
        metadata_columns = self.parsed_dataframe.columns
        metadata_columns.drop([MAPPING_TYPE, SYN])
        metadata_df = self.parsed_dataframe[metadata_columns]
        metadata_df = metadata_df.drop_duplicates(subset=[IDX]).dropna(axis=0)
        metadata_df.set_index(inplace=True, drop=True, keys=IDX)
        assert set(OntologyParser.minimum_metadata_column_names).issubset(metadata_df.columns)
        metadata = metadata_df.to_dict(orient="index")
        return cast(Dict[str, Dict[str, SimpleValue]], metadata)

    def process_curations(
        self, terms: Set[SynonymTerm]
    ) -> Tuple[Optional[List[Curation]], Set[SynonymTerm]]:
        if self.curations is None and self.synonym_generator is not None:
            logger.warning(
                "%s is configured to use synonym generators. This may result in noisy NER performance.",
                self.name,
            )
            (
                original_curations,
                generated_curations,
            ) = self.generate_curations_from_synonym_generators(terms)
            curations = original_curations + generated_curations
        elif self.curations is None and self.synonym_generator is None:
            logger.warning(
                "%s is configured to use raw ontology synonyms. This may result in noisy NER performance.",
                self.name,
            )
            curations = []
            for term in terms:
                curations.extend(self.synonym_term_to_putative_curation(term))
        else:
            assert self.curations is not None
            logger.info(
                "%s is configured to use curations. Synonym generation will be ignored",
                self.name,
            )
            curations = self.curations

        curation_processor = CurationProcessor(
            global_actions=self.global_actions,
            curations=curations,
            parser_name=self.name,
            entity_class=self.entity_class,
            synonym_terms=terms,
        )
        return curation_processor.export_ner_curations_and_final_terms()

    @kazu_disk_cache.memoize(ignore={0})
    def export_synonym_terms(self, parser_name: str) -> Set[SynonymTerm]:
        """Export :class:`.SynonymTerm` from the parser.

        :param parser_name: name of this parser. Required for correct operation of cache
            (Note, we cannot pass self to the disk cache as the constructor consumes too much
            memory
        :return:
        """
        self._parse_df_if_not_already_parsed()
        assert isinstance(self.parsed_dataframe, pd.DataFrame)
        # ensure correct order
        syn_df = self.parsed_dataframe[self.all_synonym_column_names].copy()
        syn_df = syn_df.dropna(subset=[SYN])
        syn_df[SYN] = syn_df[SYN].apply(str.strip)
        syn_df.drop_duplicates(subset=self.all_synonym_column_names)
        assert set(OntologyParser.all_synonym_column_names).issubset(syn_df.columns)
        synonym_terms = self.resolve_synonyms(synonym_df=syn_df)
        return synonym_terms

    def populate_metadata_database(self):
        """Populate the metadata database with this ontology."""
        self.metadata_db.add_parser(self.name, self.export_metadata(self.name))

    def generate_synonyms(self) -> Set[SynonymTerm]:
        """Generate synonyms based on configured synonym generator.

        Note, this method also calls populate_databases(), as the metadata db must be populated
        for appropriate synonym resolution.
        """
        self.populate_databases()
        synonym_data = set(self.synonym_db.get_all(self.name).values())
        generated_synonym_data = set()
        if self.synonym_generator:
            generated_synonym_data = self.synonym_generator(synonym_data)
        generated_synonym_data.update(synonym_data)
        logger.info(
            f"{len(synonym_data)} original synonyms and {len(generated_synonym_data)} generated synonyms produced"
        )
        return generated_synonym_data

    def populate_synonym_database(self):
        """Populate the synonym database."""

        self.synonym_db.add(self.name, self.export_synonym_terms(self.name))

    def populate_databases(self, force: bool = False) -> Optional[List[Curation]]:
        """Populate the databases with the results of the parser.

        Also calculates the term norms associated with
        any curations (if provided) which can then be used for Dictionary based NER

        :param force: normally, this call does nothing if databases already have an entry for this parser.
            this can be forced by setting this param to True
        :return: curations with term norms
        """

        if self.name in self.synonym_db.loaded_parsers and not force:
            logger.debug("will not repopulate databases as already populated for %s", self.name)
            return None
        else:
            logger.info("populating database for %s", self.name)
            self.populate_metadata_database()
            self.populate_synonym_database()
            curations_with_term_norms = self.process_actions()
            self.parsed_dataframe = None  # clear the reference to save memory
            return curations_with_term_norms

    def parse_to_dataframe(self) -> pd.DataFrame:
        """
        implementations should override this method, returning a 'long, thin' pd.DataFrame of at least the following
        columns:


        [IDX, DEFAULT_LABEL, SYN, MAPPING_TYPE]

        IDX: the ontology id
        DEFAULT_LABEL: the preferred label
        SYN: a synonym of the concept
        MAPPING_TYPE: the type of mapping from default label to synonym - e.g. xref, exactSyn etc. Usually defined by the ontology

        Note: It is the responsibility of the implementation of parse_to_dataframe to add default labels as synonyms.

        Any 'extra' columns will be added to the :class:`~kazu.modelling.database.in_memory_db.MetadataDatabase` as metadata fields for the
        given id in the relevant ontology.
        """
        raise NotImplementedError()


class JsonLinesOntologyParser(OntologyParser):
    """
    A parser for a jsonlines dataset. Assumes one kb entry per line (i.e. json object)
    implemetations should implement json_dict_to_parser_dict (see method notes for details
    """

    def read(self, path: str) -> Iterable[Dict[str, Any]]:
        for json_path in Path(path).glob("*.json"):
            with json_path.open(mode="r") as f:
                for line in f:
                    yield json.loads(line)

    def parse_to_dataframe(self):
        return pd.DataFrame.from_records(self.json_dict_to_parser_records(self.read(self.in_path)))

    def json_dict_to_parser_records(
        self, jsons_gen: Iterable[Dict[str, Any]]
    ) -> Iterable[Dict[str, Any]]:
        """
        for a given input json (represented as a python dict), yield dictionary record(s) compatible with the expected
        structure of the Ontology Parser superclass - i.e. should have keys for SYN, MAPPING_TYPE, DEFAULT_LABEL and
        IDX. All other keys are used as mapping metadata

        :param jsons_gen: iterator of python dict representing json objects
        :return:
        """
        raise NotImplementedError()


class OpenTargetsDiseaseOntologyParser(JsonLinesOntologyParser):
    # Just use IDs that are in MONDO, since that's all people in general care about.
    # if we want to expand this out, other sources are:
    # "OGMS", "FBbt", "Orphanet", "EFO", "OTAR"
    # but we did have these in previously, and EFO introduced a lot of noise as it
    # has non-disease terms like 'dose' that occur frequently.
    # we could make the allowed sources a config option but we don't need to configure
    # currently, and easy to change later (and provide the current value as a default if
    # not present in config)
    allowed_sources = {"MONDO", "HP"}

    def find_kb(self, string: str) -> str:
        return string.split("_")[0]

    def json_dict_to_parser_records(
        self, jsons_gen: Iterable[Dict[str, Any]]
    ) -> Iterable[Dict[str, Any]]:
        # we ignore related syns for now until we decide how the system should handle them
        for json_dict in jsons_gen:
            idx = self.look_for_mondo(json_dict["id"], json_dict.get("dbXRefs", []))
            if any(allowed_source in idx for allowed_source in self.allowed_sources):
                synonyms = json_dict.get("synonyms", {})
                exact_syns = synonyms.get("hasExactSynonym", [])
                exact_syns.append(json_dict["name"])
                def_label = json_dict["name"]
                dbXRefs = json_dict.get("dbXRefs", [])
                for syn in exact_syns:
                    yield {
                        SYN: syn,
                        MAPPING_TYPE: "hasExactSynonym",
                        DEFAULT_LABEL: def_label,
                        IDX: idx,
                        "dbXRefs": dbXRefs,
                    }

    def look_for_mondo(self, ot_id: str, db_xrefs: List[str]):
        if "MONDO" in ot_id:
            return ot_id
        for x in db_xrefs:
            if "MONDO" in x:
                return x.replace(":", "_")
        return ot_id


class OpenTargetsTargetOntologyParser(JsonLinesOntologyParser):

    annotation_fields = {
        "subcellularLocations",
        "tractability",
        "constraint",
        "functionDescriptions",
        "go",
        "hallmarks",
        "chemicalProbes",
        "safetyLiabilities",
        "pathways",
        "targetClass",
    }

    def score_and_group_ids(
        self,
        ids_and_source: IdsAndSource,
        is_symbolic: bool,
        original_syn_set: Set[str],
    ) -> Tuple[AssociatedIdSets, EquivalentIdAggregationStrategy]:
        """
        since non symbolic gene symbols are also frequently ambiguous, we override this method accordingly to disable
        all synonym resolution, and rely on disambiguation to decide on 'true' mappings. Answers on a postcard if anyone
        has a better idea on how to do this!

        :param id_and_source:
        :param is_symbolic:
        :param original_syn_set:
        :return:
        """

        return (
            frozenset(
                EquivalentIdSet(ids_and_source=frozenset((single_id_and_source,)))
                for single_id_and_source in ids_and_source
            ),
            EquivalentIdAggregationStrategy.CUSTOM,
        )

    def find_kb(self, string: str) -> str:
        return "ENSEMBL"

    def json_dict_to_parser_records(
        self, jsons_gen: Iterable[Dict[str, Any]]
    ) -> Iterable[Dict[str, Any]]:
        for json_dict in jsons_gen:
            # due to a bug in OT data, TEC genes have "gene" as a synonym. Sunce they're uninteresting, we just filter
            # them
            biotype = json_dict.get("biotype")
            if biotype == "" or biotype == "tec" or json_dict["id"] == json_dict["approvedSymbol"]:
                continue

            annotation_score = sum(
                1
                for annotation_field in self.annotation_fields
                if len(json_dict.get(annotation_field, [])) > 0
            )

            shared_values = {
                IDX: json_dict["id"],
                DEFAULT_LABEL: json_dict["approvedSymbol"],
                "dbXRefs": json_dict.get("dbXRefs", []),
                "approvedName": json_dict["approvedName"],
                "annotation_score": annotation_score,
            }

            for key in ["synonyms", "obsoleteSymbols", "obsoleteNames", "proteinIds"]:
                synonyms_and_sources_lst = json_dict.get(key, [])
                for record in synonyms_and_sources_lst:
                    if "label" in record and "id" in record:
                        raise RuntimeError(f"record: {record} has both id and label specified")
                    elif "label" in record:
                        record[SYN] = record.pop("label")
                    elif "id" in record:
                        record[SYN] = record.pop("id")
                    record[MAPPING_TYPE] = record.pop("source")
                    record.update(shared_values)
                    yield record

            for key in ("approvedSymbol", "approvedName", "id"):
                if key == "id":
                    mapping_type = "opentargets_id"
                else:
                    mapping_type = key

                res = {SYN: json_dict[key], MAPPING_TYPE: mapping_type}
                res.update(shared_values)
                yield res


class OpenTargetsMoleculeOntologyParser(JsonLinesOntologyParser):
    def find_kb(self, string: str) -> str:
        return "CHEMBL"

    def json_dict_to_parser_records(
        self, jsons_gen: Iterable[Dict[str, Any]]
    ) -> Iterable[Dict[str, Any]]:
        for json_dict in jsons_gen:
            cross_references = json_dict.get("crossReferences", {})
            default_label = json_dict["name"]
            idx = json_dict["id"]

            synonyms = json_dict.get("synonyms", [])
            main_name = json_dict["name"]
            synonyms.append(main_name)

            for syn in synonyms:
                yield {
                    SYN: syn,
                    MAPPING_TYPE: "synonyms",
                    "crossReferences": cross_references,
                    DEFAULT_LABEL: default_label,
                    IDX: idx,
                }

            for trade_name in json_dict.get("tradeNames", []):
                yield {
                    SYN: trade_name,
                    MAPPING_TYPE: "tradeNames",
                    "crossReferences": cross_references,
                    DEFAULT_LABEL: default_label,
                    IDX: idx,
                }


RdfRef = Union[rdflib.paths.Path, rdflib.term.Node, str]
# Note - lists are actually normally provided here through hydra config
# but there's apparently no way of type hinting
# 'any iterable of length two where the items have these types'
PredicateAndValue = Tuple[RdfRef, rdflib.term.Node]


class RDFGraphParser(OntologyParser):
    """
    Parser for Owl files.
    """

    def __init__(
        self,
        in_path: str,
        entity_class: str,
        name: str,
        uri_regex: Union[str, re.Pattern],
        synonym_predicates: Iterable[RdfRef],
        string_scorer: Optional[StringSimilarityScorer] = None,
        synonym_merge_threshold: float = 0.7,
        data_origin: str = "unknown",
        synonym_generator: Optional[CombinatorialSynonymGenerator] = None,
        include_entity_patterns: Optional[Iterable[PredicateAndValue]] = None,
        exclude_entity_patterns: Optional[Iterable[PredicateAndValue]] = None,
        curations: Optional[List[Curation]] = None,
        global_actions: Optional[GlobalParserActions] = None,
        label_predicate: RdfRef = rdflib.RDFS.label,
    ):
        super().__init__(
            in_path=in_path,
            entity_class=entity_class,
            name=name,
            string_scorer=string_scorer,
            synonym_merge_threshold=synonym_merge_threshold,
            data_origin=data_origin,
            synonym_generator=synonym_generator,
            curations=curations,
            global_actions=global_actions,
        )

        if isinstance(uri_regex, re.Pattern):
            self._uri_regex = uri_regex
        else:
            self._uri_regex = re.compile(uri_regex)

        self.synonym_predicates = tuple(
            self.convert_to_rdflib_ref(pred) for pred in synonym_predicates
        )
        self.label_predicate = self.convert_to_rdflib_ref(label_predicate)

        if include_entity_patterns is not None:
            self.include_entity_patterns = tuple(
                (self.convert_to_rdflib_ref(pred), self.convert_to_rdflib_ref(val))
                for pred, val in include_entity_patterns
            )
        else:
            self.include_entity_patterns = tuple()

        if exclude_entity_patterns is not None:
            self.exclude_entity_patterns = tuple(
                (self.convert_to_rdflib_ref(pred), self.convert_to_rdflib_ref(val))
                for pred, val in exclude_entity_patterns
            )
        else:
            self.exclude_entity_patterns = tuple()

    def find_kb(self, string: str) -> str:
        # By default, just return the name of the parser.
        # If more complex behaviour is necessary, write a custom subclass and override this method.
        return self.name

    @overload
    @staticmethod
    def convert_to_rdflib_ref(pred: rdflib.paths.Path) -> rdflib.paths.Path:
        ...

    @overload
    @staticmethod
    def convert_to_rdflib_ref(pred: rdflib.term.Node) -> rdflib.term.Node:
        ...

    @overload
    @staticmethod
    def convert_to_rdflib_ref(pred: str) -> rdflib.URIRef:
        ...

    @staticmethod
    def convert_to_rdflib_ref(pred):
        if isinstance(pred, (rdflib.term.Node, rdflib.paths.Path)):
            return pred
        else:
            return rdflib.URIRef(pred)

    def parse_to_dataframe(self) -> pd.DataFrame:
        g = rdflib.Graph()
        g.parse(self.in_path)
        default_labels = []
        iris = []
        syns = []
        mapping_type = []

        label_pred_str = str(self.label_predicate)

        for sub, obj in g.subject_objects(self.label_predicate):
            if not self.is_valid_iri(str(sub)):
                continue

            # type ignore is necessary because rdflib's typing thinks that for Graph.__contains__ can't use an rdflib.paths.Path
            # as a predicate, but you can, because __contains__ calls Graph.triples(), which is type hinted to allow Paths (and
            # reading the implementation it clearly handles Paths).
            if any((sub, pred, value) not in g for pred, value in self.include_entity_patterns):  # type: ignore[operator]
                continue

            # as above
            if any((sub, pred, value) in g for pred, value in self.exclude_entity_patterns):  # type: ignore[operator]
                continue

            default_labels.append(str(obj))
            iris.append(str(sub))
            syns.append(str(obj))
            mapping_type.append(label_pred_str)
            for syn_predicate in self.synonym_predicates:
                for other_syn_obj in g.objects(subject=sub, predicate=syn_predicate):
                    default_labels.append(str(obj))
                    iris.append(str(sub))
                    syns.append(str(other_syn_obj))
                    mapping_type.append(str(syn_predicate))

        df = pd.DataFrame.from_dict(
            {DEFAULT_LABEL: default_labels, IDX: iris, SYN: syns, MAPPING_TYPE: mapping_type}
        )
        return df

    def is_valid_iri(self, text: str) -> bool:
        """
        Check if input string is a valid IRI for the ontology being parsed.

        Uses `self._uri_regex` to define valid IRIs
        """
        match = self._uri_regex.match(text)
        return bool(match)


SKOS_XL_PREF_LABEL_PATH: rdflib.paths.Path = rdflib.URIRef(
    "http://www.w3.org/2008/05/skos-xl#prefLabel"
) / rdflib.URIRef("http://www.w3.org/2008/05/skos-xl#literalForm")
SKOS_XL_ALT_LABEL_PATH: rdflib.paths.Path = rdflib.URIRef(
    "http://www.w3.org/2008/05/skos-xl#altLabel"
) / rdflib.URIRef("http://www.w3.org/2008/05/skos-xl#literalForm")


class SKOSXLGraphParser(RDFGraphParser):
    """Parse SKOS-XL RDF Files.

    Note that this just sets a default label predicate and synonym predicate to SKOS-XL
    appropriate paths, and then passes to the parent RDFGraphParser class. This class is just a convenience
    to make specifying a SKOS-XL parser easier, this functionality is still available via RDFGraphParser
    directly.
    """

    def __init__(
        self,
        in_path: str,
        entity_class: str,
        name: str,
        uri_regex: Union[str, re.Pattern],
        synonym_predicates: Iterable[RdfRef] = (SKOS_XL_ALT_LABEL_PATH,),
        string_scorer: Optional[StringSimilarityScorer] = None,
        synonym_merge_threshold: float = 0.7,
        data_origin: str = "unknown",
        synonym_generator: Optional[CombinatorialSynonymGenerator] = None,
        include_entity_patterns: Optional[Iterable[PredicateAndValue]] = None,
        exclude_entity_patterns: Optional[Iterable[PredicateAndValue]] = None,
        curations: Optional[List[Curation]] = None,
        global_actions: Optional[GlobalParserActions] = None,
        label_predicate: RdfRef = SKOS_XL_PREF_LABEL_PATH,
    ):
        super().__init__(
            in_path=in_path,
            entity_class=entity_class,
            name=name,
            uri_regex=uri_regex,
            synonym_predicates=synonym_predicates,
            string_scorer=string_scorer,
            synonym_merge_threshold=synonym_merge_threshold,
            data_origin=data_origin,
            synonym_generator=synonym_generator,
            include_entity_patterns=include_entity_patterns,
            exclude_entity_patterns=exclude_entity_patterns,
            curations=curations,
            global_actions=global_actions,
            label_predicate=label_predicate,
        )


class GeneOntologyParser(OntologyParser):
    _uri_regex = re.compile("^http://purl.obolibrary.org/obo/GO_[0-9]+$")

    instances: Set[str] = set()
    instances_in_dbs: Set[str] = set()

    def __init__(
        self,
        in_path: str,
        entity_class: str,
        name: str,
        query: str,
        string_scorer: Optional[StringSimilarityScorer] = None,
        synonym_merge_threshold: float = 0.70,
        data_origin: str = "unknown",
        synonym_generator: Optional[CombinatorialSynonymGenerator] = None,
        curations: Optional[List[Curation]] = None,
        global_actions: Optional[GlobalParserActions] = None,
    ):
        super().__init__(
            in_path=in_path,
            entity_class=entity_class,
            name=name,
            string_scorer=string_scorer,
            synonym_merge_threshold=synonym_merge_threshold,
            data_origin=data_origin,
            synonym_generator=synonym_generator,
            curations=curations,
            global_actions=global_actions,
        )
        self.instances.add(name)
        self.query = query

    def populate_databases(self, force: bool = False) -> Optional[List[Curation]]:
        curations_with_term_norms = super().populate_databases(force=force)
        self.instances_in_dbs.add(self.name)

        if self.instances_in_dbs >= self.instances:
            # all existing instances are in the database, so we can free up
            # the memory used by the cached parsed gene ontology, which is significant.
            self.load_go.cache_clear()
        return curations_with_term_norms

    def __del__(self):
        GeneOntologyParser.instances.discard(self.name)

    @staticmethod
    @cache
    def load_go(in_path: PathLike) -> rdflib.Graph:
        g = rdflib.Graph()
        g.parse(in_path)
        return g

    def find_kb(self, string: str) -> str:
        return self.name

    def parse_to_dataframe(self) -> pd.DataFrame:
        g = self.load_go(self.in_path)
        result = g.query(self.query)
        default_labels = []
        iris = []
        syns = []
        mapping_type = []

        # there seems to be a bug in rdflib that means the iterator sometimes exits early unless we convert to list first
        # type cast is necessary because iterating over an rdflib query result gives different types depending on the kind
        # of query, so rdflib gives a Union here, but we know it should be a ResultRow because we know we should have a
        # select query
        list_res = cast(List[rdflib.query.ResultRow], list(result))
        for row in list_res:
            idx = str(row.goid)
            label = str(row.label)
            if "obsolete" in label:
                logger.debug("skipping obsolete id: %s, %s", idx, label)
                continue
            if self._uri_regex.match(idx):
                default_labels.append(label)
                iris.append(idx)
                syns.append(str(row.synonym))
                mapping_type.append("hasExactSynonym")
        df = pd.DataFrame.from_dict(
            {DEFAULT_LABEL: default_labels, IDX: iris, SYN: syns, MAPPING_TYPE: mapping_type}
        )
        default_labels_df = df[[IDX, DEFAULT_LABEL]].drop_duplicates().copy()
        default_labels_df[SYN] = default_labels_df[DEFAULT_LABEL]
        default_labels_df[MAPPING_TYPE] = "label"

        return pd.concat([df, default_labels_df])


class BiologicalProcessGeneOntologyParser(GeneOntologyParser):
    def __init__(
        self,
        in_path: str,
        entity_class: str,
        name: str,
        string_scorer: Optional[StringSimilarityScorer] = None,
        synonym_merge_threshold: float = 0.70,
        data_origin: str = "unknown",
        synonym_generator: Optional[CombinatorialSynonymGenerator] = None,
        curations: Optional[List[Curation]] = None,
        global_actions: Optional[GlobalParserActions] = None,
    ):
        super().__init__(
            in_path=in_path,
            entity_class=entity_class,
            string_scorer=string_scorer,
            synonym_merge_threshold=synonym_merge_threshold,
            data_origin=data_origin,
            synonym_generator=synonym_generator,
            name=name,
            curations=curations,
            global_actions=global_actions,
            query="""
                    PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
                    PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
                    PREFIX oboinowl: <http://www.geneontology.org/formats/oboInOwl#>

                    SELECT DISTINCT ?goid ?label ?synonym
                            WHERE {

                                ?goid oboinowl:hasExactSynonym ?synonym .
                                ?goid rdfs:label ?label .
                                ?goid oboinowl:hasOBONamespace "biological_process" .

                  }
            """,
        )


class MolecularFunctionGeneOntologyParser(GeneOntologyParser):
    def __init__(
        self,
        in_path: str,
        entity_class: str,
        name: str,
        string_scorer: Optional[StringSimilarityScorer] = None,
        synonym_merge_threshold: float = 0.70,
        data_origin: str = "unknown",
        synonym_generator: Optional[CombinatorialSynonymGenerator] = None,
        curations: Optional[List[Curation]] = None,
        global_actions: Optional[GlobalParserActions] = None,
    ):
        super().__init__(
            in_path=in_path,
            entity_class=entity_class,
            string_scorer=string_scorer,
            synonym_merge_threshold=synonym_merge_threshold,
            data_origin=data_origin,
            synonym_generator=synonym_generator,
            name=name,
            curations=curations,
            global_actions=global_actions,
            query="""
                    PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
                    PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
                    PREFIX oboinowl: <http://www.geneontology.org/formats/oboInOwl#>

                    SELECT DISTINCT ?goid ?label ?synonym
                            WHERE {

                                ?goid oboinowl:hasExactSynonym ?synonym .
                                ?goid rdfs:label ?label .
                                ?goid oboinowl:hasOBONamespace "molecular_function".

                    }
            """,
        )


class CellularComponentGeneOntologyParser(GeneOntologyParser):
    def __init__(
        self,
        in_path: str,
        entity_class: str,
        name: str,
        string_scorer: Optional[StringSimilarityScorer] = None,
        synonym_merge_threshold: float = 0.70,
        data_origin: str = "unknown",
        synonym_generator: Optional[CombinatorialSynonymGenerator] = None,
        curations: Optional[List[Curation]] = None,
        global_actions: Optional[GlobalParserActions] = None,
    ):
        super().__init__(
            in_path=in_path,
            entity_class=entity_class,
            string_scorer=string_scorer,
            synonym_merge_threshold=synonym_merge_threshold,
            data_origin=data_origin,
            synonym_generator=synonym_generator,
            name=name,
            curations=curations,
            global_actions=global_actions,
            query="""
                    PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
                    PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
                    PREFIX oboinowl: <http://www.geneontology.org/formats/oboInOwl#>

                    SELECT DISTINCT ?goid ?label ?synonym
                            WHERE {

                                ?goid oboinowl:hasExactSynonym ?synonym .
                                ?goid rdfs:label ?label .
                                ?goid oboinowl:hasOBONamespace "cellular_component" .

                    }
            """,
        )


class UberonOntologyParser(RDFGraphParser):
    """
    input should be an UBERON owl file
    e.g.
    https://www.ebi.ac.uk/ols/ontologies/uberon
    """

    def __init__(
        self,
        in_path: str,
        entity_class: str,
        name: str,
        string_scorer: Optional[StringSimilarityScorer] = None,
        synonym_merge_threshold: float = 0.70,
        data_origin: str = "unknown",
        synonym_generator: Optional[CombinatorialSynonymGenerator] = None,
        curations: Optional[List[Curation]] = None,
        global_actions: Optional[GlobalParserActions] = None,
    ):

        super().__init__(
            in_path=in_path,
            entity_class=entity_class,
            name=name,
            uri_regex=re.compile("^http://purl.obolibrary.org/obo/UBERON_[0-9]+$"),
            synonym_predicates=(
                rdflib.URIRef("http://www.geneontology.org/formats/oboInOwl#hasExactSynonym"),
            ),
            string_scorer=string_scorer,
            synonym_merge_threshold=synonym_merge_threshold,
            data_origin=data_origin,
            synonym_generator=synonym_generator,
            curations=curations,
            global_actions=global_actions,
        )

    def find_kb(self, string: str) -> str:
        return "UBERON"


class MondoOntologyParser(OntologyParser):
    _uri_regex = re.compile("^http://purl.obolibrary.org/obo/(MONDO|HP)_[0-9]+$")
    """
    input should be a MONDO json file
    e.g.
    https://www.ebi.ac.uk/ols/ontologies/mondo
    """

    def find_kb(self, string: str):
        path = parse.urlparse(string).path
        # just the final bit, e.g. MONDO_0000123
        path_end = path.split("/")[-1]
        # we don't want the underscore or digits for the unique ID, just the ontology bit
        return path_end.split("_")[0]

    def parse_to_dataframe(self) -> pd.DataFrame:
        x = json.load(open(self.in_path, "r"))
        graph = x["graphs"][0]
        nodes = graph["nodes"]
        ids = []
        default_label_list = []
        all_syns = []
        mapping_type = []
        for i, node in enumerate(nodes):
            if not self.is_valid_iri(node["id"]):
                continue

            idx = node["id"]
            default_label = node.get("lbl")
            if default_label is None:
                # skip if no default label is available
                continue
            # add default_label to syn type
            all_syns.append(default_label)
            default_label_list.append(default_label)
            mapping_type.append("lbl")
            ids.append(idx)

            syns = node.get("meta", {}).get("synonyms", [])
            for syn_dict in syns:

                pred = syn_dict["pred"]
                if pred in {"hasExactSynonym"}:
                    mapping_type.append(pred)
                    syn = syn_dict["val"]
                    ids.append(idx)
                    default_label_list.append(default_label)
                    all_syns.append(syn)

        df = pd.DataFrame.from_dict(
            {IDX: ids, DEFAULT_LABEL: default_label_list, SYN: all_syns, MAPPING_TYPE: mapping_type}
        )
        return df

    def is_valid_iri(self, text: str) -> bool:
        match = self._uri_regex.match(text)
        return bool(match)


class EnsemblOntologyParser(OntologyParser):
    """
    input is a json from HGNC
    e.g. http://ftp.ebi.ac.uk/pub/databases/genenames/hgnc/json/hgnc_complete_set.json

    :return:
    """

    def __init__(
        self,
        in_path: str,
        entity_class: str,
        name: str,
        string_scorer: Optional[StringSimilarityScorer] = None,
        synonym_merge_threshold: float = 0.70,
        data_origin: str = "unknown",
        synonym_generator: Optional[CombinatorialSynonymGenerator] = None,
        curations: Optional[List[Curation]] = None,
        global_actions: Optional[GlobalParserActions] = None,
    ):
        super().__init__(
            in_path=in_path,
            entity_class=entity_class,
            string_scorer=string_scorer,
            synonym_merge_threshold=synonym_merge_threshold,
            data_origin=data_origin,
            synonym_generator=synonym_generator,
            name=name,
            curations=curations,
            global_actions=global_actions,
        )

    def find_kb(self, string: str) -> str:
        return "ENSEMBL"

    def parse_to_dataframe(self) -> pd.DataFrame:

        keys_to_check = [
            "name",
            "symbol",
            "uniprot_ids",
            "alias_name",
            "alias_symbol",
            "prev_name",
            "lncipedia",
            "prev_symbol",
            "vega_id",
            "refseq_accession",
            "hgnc_id",
            "mgd_id",
            "rgd_id",
            "ccds_id",
            "pseudogene.org",
        ]

        with open(self.in_path, "r") as f:
            data = json.load(f)
        ids = []
        default_label = []
        all_syns = []
        all_mapping_type: List[str] = []
        docs = data["response"]["docs"]
        for doc in docs:

            def get_with_default_list(key: str):
                found = doc.get(key, [])
                if not isinstance(found, list):
                    found = [found]
                return found

            ensembl_gene_id = doc.get("ensembl_gene_id", None)
            name = doc.get("name", None)
            if ensembl_gene_id is None or name is None:
                continue
            else:
                # find synonyms
                synonyms: List[Tuple[str, str]] = []
                for hgnc_key in keys_to_check:
                    synonyms_this_entity = get_with_default_list(hgnc_key)
                    for potential_synonym in synonyms_this_entity:
                        synonyms.append((potential_synonym, hgnc_key))

                synonyms = list(set(synonyms))
                synonyms_strings = []
                for synonym_str, mapping_t in synonyms:
                    all_mapping_type.append(mapping_t)
                    synonyms_strings.append(synonym_str)

                num_syns = len(synonyms_strings)
                ids.extend([ensembl_gene_id] * num_syns)
                default_label.extend([name] * num_syns)
                all_syns.extend(synonyms_strings)

        df = pd.DataFrame.from_dict(
            {IDX: ids, DEFAULT_LABEL: default_label, SYN: all_syns, MAPPING_TYPE: all_mapping_type}
        )
        return df


class ChemblOntologyParser(OntologyParser):
    """
    input is a sqllite dump from Chembl, e.g.
    https://ftp.ebi.ac.uk/pub/databases/chembl/ChEMBLdb/latest/chembl_29_sqlite.tar.gz
    """

    def find_kb(self, string: str) -> str:
        return "CHEMBL"

    def parse_to_dataframe(self) -> pd.DataFrame:
        conn = sqlite3.connect(self.in_path)
        query = f"""\
            SELECT chembl_id AS {IDX}, pref_name AS {DEFAULT_LABEL}, synonyms AS {SYN}, syn_type AS {MAPPING_TYPE}
            FROM molecule_dictionary AS md
                     JOIN molecule_synonyms ms ON md.molregno = ms.molregno
            UNION ALL
            SELECT chembl_id AS {IDX}, pref_name AS {DEFAULT_LABEL}, pref_name AS {SYN}, 'pref_name' AS {MAPPING_TYPE}
            FROM molecule_dictionary
        """
        df = pd.read_sql(query, conn)
        # eliminate anything without a pref_name, as will be too big otherwise
        df = df.dropna(subset=[DEFAULT_LABEL])

        df.drop_duplicates(inplace=True)

        return df


class CLOOntologyParser(RDFGraphParser):
    """
    input is a CLO Owl file
    https://www.ebi.ac.uk/ols/ontologies/clo
    """

    def __init__(
        self,
        in_path: str,
        entity_class: str,
        name: str,
        string_scorer: Optional[StringSimilarityScorer] = None,
        synonym_merge_threshold: float = 0.70,
        data_origin: str = "unknown",
        synonym_generator: Optional[CombinatorialSynonymGenerator] = None,
        curations: Optional[List[Curation]] = None,
        global_actions: Optional[GlobalParserActions] = None,
    ):
        super().__init__(
            in_path=in_path,
            entity_class=entity_class,
            name=name,
            uri_regex=re.compile("^http://purl.obolibrary.org/obo/CLO_[0-9]+$"),
            synonym_predicates=(
                rdflib.URIRef("http://www.geneontology.org/formats/oboInOwl#hasExactSynonym"),
            ),
            string_scorer=string_scorer,
            synonym_merge_threshold=synonym_merge_threshold,
            data_origin=data_origin,
            synonym_generator=synonym_generator,
            curations=curations,
            global_actions=global_actions,
        )

    def find_kb(self, string: str) -> str:
        return "CLO"


class CellosaurusOntologyParser(OntologyParser):
    """
    input is an obo file from cellosaurus, e.g.
    https://ftp.expasy.org/databases/cellosaurus/cellosaurus.obo
    """

    cell_line_re = re.compile("cell line", re.IGNORECASE)

    def find_kb(self, string: str) -> str:
        return "CELLOSAURUS"

    def score_and_group_ids(
        self,
        ids_and_source: IdsAndSource,
        is_symbolic: bool,
        original_syn_set: Set[str],
    ) -> Tuple[AssociatedIdSets, EquivalentIdAggregationStrategy]:
        """
        treat all synonyms as seperate cell lines

        :param ids:
        :param id_to_source:
        :param is_symbolic:
        :param original_syn_set:
        :return:
        """

        return (
            frozenset(
                EquivalentIdSet(
                    ids_and_source=frozenset((single_id_and_source,)),
                )
                for single_id_and_source in ids_and_source
            ),
            EquivalentIdAggregationStrategy.CUSTOM,
        )

    def _remove_cell_line_text(self, text: str):
        return self.cell_line_re.sub("", text).strip()

    def parse_to_dataframe(self) -> pd.DataFrame:

        ids = []
        default_labels = []
        all_syns = []
        mapping_type = []
        with open(self.in_path, "r") as f:
            id = ""
            for line in f:
                text = line.rstrip()
                if text.startswith("id:"):
                    id = text.split(" ")[1]
                elif text.startswith("name:"):
                    default_label = text[5:].strip()
                    ids.append(id)
                    # we remove "cell line" because they're all cell lines and it confuses mapping
                    default_label_no_cell_line = self._remove_cell_line_text(default_label)
                    default_labels.append(default_label_no_cell_line)
                    all_syns.append(default_label_no_cell_line)
                    mapping_type.append("name")
                # synonyms in cellosaurus are a bit of a mess, so we don't use this field for now. Leaving this here
                # in case they improve at some point
                # elif text.startswith("synonym:"):
                #     match = self._synonym_regex.match(text)
                #     if match is None:
                #         raise ValueError(
                #             """synonym line does not match our synonym regex.
                #             Either something is wrong with the file, or it has updated
                #             and our regex is not correct/general enough."""
                #         )
                #     ids.append(id)
                #     default_labels.append(default_label)
                #
                #     all_syns.append(self._remove_cell_line_text(match.group("syn")))
                #     mapping_type.append(match.group("mapping"))
                else:
                    pass
        df = pd.DataFrame.from_dict(
            {IDX: ids, DEFAULT_LABEL: default_labels, SYN: all_syns, MAPPING_TYPE: mapping_type}
        )
        return df

    _synonym_regex = re.compile(
        r"""^synonym:      # line that begins synonyms
        \s*                # any amount of whitespace (standardly a single space)
        "(?P<syn>[^"]*)"   # a quoted string - capture this as a named match group 'syn'
        \s*                # any amount of separating whitespace (standardly a single space)
        (?P<mapping>\w*)   # a sequence of word characters representing the mapping type
        \s*                # any amount of separating whitespace (standardly a single space)
        \[\]               # an open and close bracket at the end of the string
        $""",
        re.VERBOSE,
    )


class MeddraOntologyParser(OntologyParser):
    """
    input is an unzipped directory to a Meddra release (Note, requires licence). This
    should contain the files 'mdhier.asc' and 'llt.asc'.
    """

    def __init__(
        self,
        in_path: str,
        entity_class: str,
        name: str,
        string_scorer: Optional[StringSimilarityScorer] = None,
        synonym_merge_threshold: float = 0.70,
        data_origin: str = "unknown",
        synonym_generator: Optional[CombinatorialSynonymGenerator] = None,
        curations: Optional[List[Curation]] = None,
        global_actions: Optional[GlobalParserActions] = None,
        exclude_socs: Iterable[str] = (
            "Surgical and medical procedures",
            "Social circumstances",
            "Investigations",
        ),
    ):
        super().__init__(
            in_path=in_path,
            entity_class=entity_class,
            string_scorer=string_scorer,
            synonym_merge_threshold=synonym_merge_threshold,
            data_origin=data_origin,
            synonym_generator=synonym_generator,
            curations=curations,
            global_actions=global_actions,
            name=name,
        )

        self.exclude_socs = exclude_socs

    _mdhier_asc_col_names = (
        "pt_code",
        "hlt_code",
        "hlgt_code",
        "soc_code",
        "pt_name",
        "hlt_name",
        "hlgt_name",
        "soc_name",
        "soc_abbrev",
        "null_field",
        "pt_soc_code",
        "primary_soc_fg",
        "NULL",
    )

    _llt_asc_column_names = (
        "llt_code",
        "llt_name",
        "pt_code",
        "llt_whoart_code",
        "llt_harts_code",
        "llt_costart_sym",
        "llt_icd9_code",
        "llt_icd9cm_code",
        "llt_icd10_code",
        "llt_currency",
        "llt_jart_code",
        "NULL",
    )

    def find_kb(self, string: str) -> str:
        return "MEDDRA"

    def parse_to_dataframe(self) -> pd.DataFrame:
        # hierarchy path
        mdheir_path = os.path.join(self.in_path, "mdhier.asc")
        # low level term path
        llt_path = os.path.join(self.in_path, "llt.asc")
        hier_df = pd.read_csv(
            mdheir_path,
            sep="$",
            header=None,
            names=self._mdhier_asc_col_names,
            dtype="string",
        )
        hier_df = hier_df[~hier_df["soc_name"].isin(self.exclude_socs)]

        llt_df = pd.read_csv(
            llt_path,
            sep="$",
            header=None,
            names=self._llt_asc_column_names,
            usecols=("llt_name", "pt_code"),
            dtype="string",
        )
        llt_df = llt_df.dropna(axis=1)

        ids = []
        default_labels = []
        all_syns = []
        mapping_type = []
        soc_names = []
        soc_codes = []

        for i, row in hier_df.iterrows():
            idx = row["pt_code"]
            pt_name = row["pt_name"]
            soc_name = row["soc_name"]
            soc_code = row["soc_code"]
            llts = llt_df[llt_df["pt_code"] == idx]
            ids.append(idx)
            default_labels.append(pt_name)
            all_syns.append(pt_name)
            soc_names.append(soc_name)
            soc_codes.append(soc_code)
            mapping_type.append("meddra_link")
            for j, llt_row in llts.iterrows():
                ids.append(idx)
                default_labels.append(pt_name)
                soc_names.append(soc_name)
                soc_codes.append(soc_code)
                all_syns.append(llt_row["llt_name"])
                mapping_type.append("meddra_link")

        for i, row in (
            hier_df[["hlt_code", "hlt_name", "soc_name", "soc_code"]].drop_duplicates().iterrows()
        ):
            ids.append(row["hlt_code"])
            default_labels.append(row["hlt_name"])
            soc_names.append(row["soc_name"])
            soc_codes.append(row["soc_code"])
            all_syns.append(row["hlt_name"])
            mapping_type.append("meddra_link")
        for i, row in (
            hier_df[["hlgt_code", "hlgt_name", "soc_name", "soc_code"]].drop_duplicates().iterrows()
        ):
            ids.append(row["hlgt_code"])
            default_labels.append(row["hlgt_name"])
            soc_names.append(row["soc_name"])
            soc_codes.append(row["soc_code"])
            all_syns.append(row["hlgt_name"])
            mapping_type.append("meddra_link")
        df = pd.DataFrame.from_dict(
            {
                IDX: ids,
                DEFAULT_LABEL: default_labels,
                SYN: all_syns,
                MAPPING_TYPE: mapping_type,
                "soc_name": soc_names,
                "soc_code": soc_codes,
            }
        )
        return df


class CLOntologyParser(RDFGraphParser):
    """
    input should be an CL owl file
    e.g.
    https://www.ebi.ac.uk/ols/ontologies/cl
    """

    def __init__(
        self,
        in_path: str,
        entity_class: str,
        name: str,
        string_scorer: Optional[StringSimilarityScorer] = None,
        synonym_merge_threshold: float = 0.7,
        data_origin: str = "unknown",
        synonym_generator: Optional[CombinatorialSynonymGenerator] = None,
        include_entity_patterns: Optional[Iterable[PredicateAndValue]] = None,
        exclude_entity_patterns: Optional[Iterable[PredicateAndValue]] = None,
        curations: Optional[List[Curation]] = None,
        global_actions: Optional[GlobalParserActions] = None,
    ):

        super().__init__(
            in_path=in_path,
            entity_class=entity_class,
            name=name,
            uri_regex=re.compile("^http://purl.obolibrary.org/obo/CL_[0-9]+$"),
            synonym_predicates=(
                rdflib.URIRef("http://www.geneontology.org/formats/oboInOwl#hasExactSynonym"),
            ),
            string_scorer=string_scorer,
            synonym_merge_threshold=synonym_merge_threshold,
            data_origin=data_origin,
            synonym_generator=synonym_generator,
            include_entity_patterns=include_entity_patterns,
            exclude_entity_patterns=exclude_entity_patterns,
            curations=curations,
            global_actions=global_actions,
        )

    def find_kb(self, string: str) -> str:
        return "CL"


class HGNCGeneFamilyParser(OntologyParser):

    syn_column_keys = {"Family alias", "Common root gene symbol"}

    def find_kb(self, string: str) -> str:
        return "HGNC_GENE_FAMILY"

    def parse_to_dataframe(self) -> pd.DataFrame:
        df = pd.read_csv(self.in_path, sep="\t")
        data = []
        for family_id, row in (
            df.groupby(by="Family ID").agg(lambda col_series: set(col_series.dropna())).iterrows()
        ):
            # in theory, there should only be one family name per ID
            assert len(row["Family name"]) == 1
            default_label = next(iter(row["Family name"]))
            data.append(
                {
                    SYN: default_label,
                    MAPPING_TYPE: "Family name",
                    DEFAULT_LABEL: default_label,
                    IDX: family_id,
                }
            )
            data.extend(
                {
                    SYN: syn,
                    MAPPING_TYPE: key,
                    DEFAULT_LABEL: default_label,
                    IDX: family_id,
                }
                for key in self.syn_column_keys
                for syn in row[key]
            )
        return pd.DataFrame.from_records(data)


class TabularOntologyParser(OntologyParser):
    """
    For already tabulated data.
    """

    def __init__(
        self,
        in_path: str,
        entity_class: str,
        name: str,
        string_scorer: Optional[StringSimilarityScorer] = None,
        synonym_merge_threshold: float = 0.7,
        data_origin: str = "unknown",
        synonym_generator: Optional[CombinatorialSynonymGenerator] = None,
        curations: Optional[List[Curation]] = None,
        global_actions: Optional[GlobalParserActions] = None,
        **kwargs,
    ):
        """

        :param in_path:
        :param entity_class:
        :param name:
        :param string_scorer:
        :param synonym_merge_threshold:
        :param data_origin:
        :param synonym_generator:
        :param curations:
        :param kwargs: passed to pandas.read_csv
        """
        super().__init__(
            in_path=in_path,
            entity_class=entity_class,
            name=name,
            string_scorer=string_scorer,
            synonym_merge_threshold=synonym_merge_threshold,
            data_origin=data_origin,
            synonym_generator=synonym_generator,
            curations=curations,
            global_actions=global_actions,
        )
        self._raw_dataframe = pd.read_csv(self.in_path, **kwargs)

    def parse_to_dataframe(self) -> pd.DataFrame:
        """
        Assume input file is already in correct format.

        Inherit and override this method if different behaviour is required.

        :return:
        """
        return self._raw_dataframe

    def find_kb(self, string: str) -> str:
        return self.name


class ATCDrugClassificationParser(TabularOntologyParser):
    """
    Parser for the ATC Drug classification dataset.

    This requires a licence from WHO, available at
    https://www.who.int/tools/atc-ddd-toolkit/atc-classification .

    """

    def __init__(
        self,
        in_path: str,
        entity_class: str,
        name: str,
        string_scorer: Optional[StringSimilarityScorer] = None,
        synonym_merge_threshold: float = 0.70,
        data_origin: str = "unknown",
        synonym_generator: Optional[CombinatorialSynonymGenerator] = None,
        curations: Optional[List[Curation]] = None,
        global_actions: Optional[GlobalParserActions] = None,
    ):
        super().__init__(
            in_path=in_path,
            entity_class=entity_class,
            name=name,
            string_scorer=string_scorer,
            synonym_merge_threshold=synonym_merge_threshold,
            data_origin=data_origin,
            synonym_generator=synonym_generator,
            curations=curations,
            global_actions=global_actions,
            sep="     ",
            header=None,
            names=["code", "level_and_description"],
            # Because the c engine can't handle multi-char sep
            # removing this results in the same behaviour, but
            # pandas logs a warning.
            engine="python",
        )

    levels_to_ignore = {"1", "2", "3"}

    def parse_to_dataframe(self) -> pd.DataFrame:
        # for some reason, the level and description codes are merged, so we need to fix this here
        df = self._raw_dataframe.applymap(str.strip)
        res_df = pd.DataFrame()
        res_df[[MAPPING_TYPE, DEFAULT_LABEL]] = df.apply(
            lambda row: [row["level_and_description"][0], row["level_and_description"][1:]],
            axis=1,
            result_type="expand",
        )
        res_df[IDX] = df["code"]
        res_df = res_df[~res_df[MAPPING_TYPE].isin(self.levels_to_ignore)]
        res_df[SYN] = res_df[DEFAULT_LABEL]
        return res_df


class StatoParser(RDFGraphParser):
    """
    Parse stato: input should be an owl file.

    Available at e.g.
    https://www.ebi.ac.uk/ols/ontologies/stato .
    """

    def __init__(
        self,
        in_path: str,
        entity_class: str,
        name: str,
        string_scorer: Optional[StringSimilarityScorer] = None,
        synonym_merge_threshold: float = 0.7,
        data_origin: str = "unknown",
        synonym_generator: Optional[CombinatorialSynonymGenerator] = None,
        include_entity_patterns: Optional[Iterable[PredicateAndValue]] = None,
        exclude_entity_patterns: Optional[Iterable[PredicateAndValue]] = None,
        curations: Optional[List[Curation]] = None,
        global_actions: Optional[GlobalParserActions] = None,
    ):

        super().__init__(
            in_path=in_path,
            entity_class=entity_class,
            name=name,
            uri_regex=re.compile("^http://purl.obolibrary.org/obo/(OBI|STATO)_[0-9]+$"),
            synonym_predicates=(rdflib.URIRef("http://purl.obolibrary.org/obo/IAO_0000111"),),
            string_scorer=string_scorer,
            synonym_merge_threshold=synonym_merge_threshold,
            data_origin=data_origin,
            synonym_generator=synonym_generator,
            include_entity_patterns=include_entity_patterns,
            exclude_entity_patterns=exclude_entity_patterns,
            curations=curations,
            global_actions=global_actions,
        )

    def find_kb(self, string: str) -> str:
        return "OBI" if "OBI" in string else "STATO"
