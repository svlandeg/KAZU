"""Common data and parametrizations used by both string matching methods.

We have two methods of string_matching:

* the :class:`~.ExplosionStringMatchingStep`
  backed by spaCy's `PhraseMatcher <https://spacy.io/api/phrasematcher>`_\\ .
* the :class:`~.MemoryEfficientStringMatchingStep`
  backed by pyahocorasick's `Automaton <https://pyahocorasick.readthedocs.io/en/latest/#automaton-class>`_\\ .

We test both of these with basically the same set of tests - store the
different parametrizations here for re-use between the two.

The key things in this class are ``PARAM_NAMES`` and ``PARAM_VALUES``, for
use in a parametrized pytest function.
"""

import dataclasses

import pytest

from kazu.data.data import (
    CuratedTerm,
    MentionConfidence,
    CuratedTermBehaviour,
    EquivalentIdSet,
)
from kazu.database.in_memory_db import ParserName, NormalisedSynonymStr
from kazu.ontology_preprocessing.base import (
    IDX,
    DEFAULT_LABEL,
    SYN,
    MAPPING_TYPE,
)
from kazu.steps.joint_ner_and_linking.memory_efficient_string_matching import (
    EntityClass,
)

FIRST_MOCK_PARSER = "first_mock_parser"
SECOND_MOCK_PARSER = "second_mock_parser"
COMPLEX_7_DISEASE_ALPHA_NORM = "COMPLEX 7 DISEASE ALPHA"
TARGET_IDX = "http://my.fake.ontology/complex_disease_123"
ENT_TYPE_1 = "ent_type_1"
ENT_TYPE_2 = "ent_type_2"
PARSER_1_DEFAULT_DATA = {
    IDX: [
        "http://my.fake.ontology/synonym_term_id_123",
        TARGET_IDX,
        TARGET_IDX,
        "http://my.fake.ontology_amongst_id_123",
        "http://my.fake.ontology_amongst_id_124",
    ],
    DEFAULT_LABEL: [
        "SynonymTerm",
        "SynonymTerm",
        "Complex Disease Alpha VII",
        "Amongst",
        "Amongst Us",
    ],
    SYN: [
        "SynonymTerm",
        "SynonymTerm",
        "complexVII disease\u03B1",
        "amongst",
        "amongst us",
    ],
    MAPPING_TYPE: ["test", "test", "test", "test", "test"],
}

PARSER_2_DEFAULT_DATA = {
    IDX: [
        "http://my.fake.ontology/synonym_term_id_123",
        "http://my.fake.ontology/synonym_term_id_456",
        TARGET_IDX,
        "http://my.fake.ontology_amongst_id_123",
    ],
    DEFAULT_LABEL: [
        "SynonymTerm",
        "SynonymTerm",
        "Complex Disease Alpha VII",
        "Amongst",
    ],
    SYN: ["SynonymTerm", "SynonymTerm", "complexVII disease\u03B1", "amongst"],
    MAPPING_TYPE: ["test", "test", "test", "test"],
}

FIRST_MOCK_PARSER_DEFAULT_COMPLEX7_TERM = CuratedTerm(
    mention_confidence=MentionConfidence.HIGHLY_LIKELY,
    curated_synonym="complexVII disease\u03B1",
    behaviour=CuratedTermBehaviour.ADD_FOR_NER_AND_LINKING,
    associated_id_sets=frozenset(
        [
            EquivalentIdSet(
                ids_and_source=frozenset(
                    [
                        (
                            TARGET_IDX,
                            FIRST_MOCK_PARSER,
                        )
                    ]
                )
            )
        ]
    ),
    case_sensitive=False,
)

SECOND_MOCK_PARSER_DEFAULT_COMPLEX7_TERM = dataclasses.replace(
    FIRST_MOCK_PARSER_DEFAULT_COMPLEX7_TERM,
    associated_id_sets=frozenset(
        [
            EquivalentIdSet(
                ids_and_source=frozenset(
                    [
                        (
                            TARGET_IDX,
                            SECOND_MOCK_PARSER,
                        )
                    ]
                )
            )
        ]
    ),
)


MatchOntologyData = set[tuple[EntityClass, ParserName, NormalisedSynonymStr, MentionConfidence]]


@dataclasses.dataclass
class StringMatchingTestCase:
    id: str
    parser_1_curations: list[CuratedTerm]
    parser_2_curations: list[CuratedTerm]
    match_len: int
    match_texts: set[str]
    match_ontology_data: MatchOntologyData
    parser_1_data: dict[str, list[str]] = dataclasses.field(
        default_factory=lambda: PARSER_1_DEFAULT_DATA
    )
    parser_2_data: dict[str, list[str]] = dataclasses.field(
        default_factory=lambda: PARSER_2_DEFAULT_DATA
    )
    parser_1_ent_type: str = ENT_TYPE_1
    parser_2_ent_type: str = ENT_TYPE_2


# this gives us back the field names defined above, in the same order (and skipping 'id')
PARAM_NAMES = tuple(field.name for field in dataclasses.fields(StringMatchingTestCase)[1:])

TESTCASES = [
    StringMatchingTestCase(
        id="Two curated case insensitive terms from two parsers Both should hit",
        parser_1_curations=[FIRST_MOCK_PARSER_DEFAULT_COMPLEX7_TERM],
        parser_2_curations=[SECOND_MOCK_PARSER_DEFAULT_COMPLEX7_TERM],
        match_len=2,
        match_texts={"ComplexVII Disease\u03B1"},
        match_ontology_data={
            (
                ENT_TYPE_1,
                FIRST_MOCK_PARSER,
                COMPLEX_7_DISEASE_ALPHA_NORM,
                MentionConfidence.HIGHLY_LIKELY,
            ),
            (
                ENT_TYPE_2,
                SECOND_MOCK_PARSER,
                COMPLEX_7_DISEASE_ALPHA_NORM,
                MentionConfidence.HIGHLY_LIKELY,
            ),
        },
    ),
    StringMatchingTestCase(
        id="Two curated terms from two parsers One should hit to test case sensitivity",
        parser_1_curations=[FIRST_MOCK_PARSER_DEFAULT_COMPLEX7_TERM],
        parser_2_curations=[
            dataclasses.replace(SECOND_MOCK_PARSER_DEFAULT_COMPLEX7_TERM, case_sensitive=True)
        ],
        match_len=1,
        match_texts={"ComplexVII Disease\u03B1"},
        match_ontology_data={
            (
                ENT_TYPE_1,
                FIRST_MOCK_PARSER,
                COMPLEX_7_DISEASE_ALPHA_NORM,
                MentionConfidence.HIGHLY_LIKELY,
            )
        },
    ),
    StringMatchingTestCase(
        id="Two curated terms from two parsers One should hit to test ignore logic",
        parser_1_curations=[FIRST_MOCK_PARSER_DEFAULT_COMPLEX7_TERM],
        parser_2_curations=[
            dataclasses.replace(
                SECOND_MOCK_PARSER_DEFAULT_COMPLEX7_TERM, behaviour=CuratedTermBehaviour.IGNORE
            )
        ],
        match_len=1,
        match_texts={"ComplexVII Disease\u03B1"},
        match_ontology_data={
            (
                ENT_TYPE_1,
                FIRST_MOCK_PARSER,
                COMPLEX_7_DISEASE_ALPHA_NORM,
                MentionConfidence.HIGHLY_LIKELY,
            )
        },
    ),
    StringMatchingTestCase(
        id="One curated term with a novel synonym This should be added to the synonym DB and hit",
        parser_1_curations=[
            dataclasses.replace(
                FIRST_MOCK_PARSER_DEFAULT_COMPLEX7_TERM,
                curated_synonym="This sentence is just to test",
            )
        ],
        parser_2_curations=[],
        match_len=1,
        match_texts={"This sentence is just to test"},
        match_ontology_data={
            (
                ENT_TYPE_1,
                FIRST_MOCK_PARSER,
                "THIS SENTENCE IS JUST TO TEST",
                MentionConfidence.HIGHLY_LIKELY,
            )
        },
    ),
]

PARAM_VALUES = [
    pytest.param(*tuple(getattr(tc, fieldname) for fieldname in PARAM_NAMES), id=tc.id)
    for tc in TESTCASES
]
