import logging
from collections import defaultdict
from enum import Enum, auto
from typing import Any, Literal, Optional

import spacy
from kazu.data.data import Document, Entity
from kazu.ontology_matching.assemble_pipeline import (  # noqa: F401 # we need this import to register the spacy component
    KazuCustomEnglish,
)
from kazu.steps import Step, document_iterating_step
from spacy.matcher import Matcher
from spacy.tokens import Token, Span
from kazu.utils.spacy_pipeline import SpacyToKazuObjectMapper

logger = logging.getLogger(__name__)
TPOrFP = Literal["tp", "fp"]
TPOrFPMatcher = dict[TPOrFP, Matcher]
MatcherMentionRules = dict[str, dict[str, dict[TPOrFP, list[list[dict[str, Any]]]]]]
MatcherClassRules = dict[str, dict[TPOrFP, list[list[dict[str, Any]]]]]
MentionMatchers = dict[str, dict[str, TPOrFPMatcher]]
ClassMatchers = dict[str, TPOrFPMatcher]


class MatcherResult(Enum):
    HIT = auto()
    MISS = auto()
    NOT_CONFIGURED = auto()


class RulesBasedEntityClassDisambiguationFilterStep(Step):
    """Removes instances of :class:`.Entity` from
    :class:`.Section` that don't meet rules based
    disambiguation requirements in at least one location in the document.

    This step utilises Spacy `Matcher <https://spacy.io/api/matcher>`_
    rules to determine whether an entity class and or/mention entities are valid or not.
    These Matcher rules operate on the sentence in which each under consideration
    is located.

    Rules can have both true positive and false positive aspects. If defined, that
    aspect MUST be correct at least once in the document for all entities with the same
    key (composed of the matched string and entity class) to be valid.

    Non-contiguous entities are evaluated on the full span of the text they cover, rather
    than the specific tokens.

    """

    _tp_allowed_values = {MatcherResult.HIT, MatcherResult.NOT_CONFIGURED}

    def __init__(
        self, class_matcher_rules: MatcherClassRules, mention_matcher_rules: MatcherMentionRules
    ):
        """

        :param class_matcher_rules: these should follow the format:

            .. code-block:: python

                {
                    "<entity class>": {
                        "<tp or fp (for true positive or false positive rules respectively>": [
                            "<a list of rules>",
                            "<according to the spacy pattern matcher syntax>",
                        ]
                    }
                }
        :param mention_matcher_rules: these should follow the format:

            .. code-block:: python

                {
                    "<entity class>": {
                        "<mention to disambiguate>": {
                            "<tp or fp>": [
                                "<a list of rules>",
                                "<according to the spacy pattern matcher syntax>",
                            ]
                        }
                    }
                }
        """
        self.nlp = spacy.blank("kazu_custom_en")
        self.nlp.add_pipe("sentencizer")
        self.class_matchers: ClassMatchers = self._build_class_matchers(class_matcher_rules)
        self.mention_matchers: MentionMatchers = self._build_mention_matchers(mention_matcher_rules)

    def _build_class_matchers(self, class_rules: MatcherClassRules) -> ClassMatchers:
        result: ClassMatchers = {}
        for class_name, rules in class_rules.items():
            for rule_type, rule_instances in rules.items():
                if rule_instances is not None:
                    matcher = Matcher(self.nlp.vocab)
                    matcher.add(f"{class_name}_{rule_type}", rule_instances)
                    result.setdefault(class_name, {})[rule_type] = matcher
        return result

    def _build_mention_matchers(self, mention_rules: MatcherMentionRules) -> MentionMatchers:
        result: MentionMatchers = {}
        rule_type: TPOrFP
        for class_name, target_term_dict in mention_rules.items():
            for target_term, rules in target_term_dict.items():
                for rule_type, rule_instances in rules.items():
                    if rule_instances is not None:
                        matcher = Matcher(self.nlp.vocab)
                        matcher.add(f"{class_name}_{rule_type}", rule_instances)
                        result.setdefault(class_name, {})
                        result[class_name].setdefault(target_term, {})
                        matcher = Matcher(self.nlp.vocab)
                        matcher.add(f"{class_name}_{target_term}_{rule_type}", rule_instances)
                        result[class_name][target_term][rule_type] = matcher
        return result

    @document_iterating_step
    def __call__(self, doc: Document) -> None:
        ent_tp_class_results: defaultdict[tuple[str, str], set[bool]] = defaultdict(set)
        ent_fp_class_results: defaultdict[tuple[str, str], set[bool]] = defaultdict(set)
        ent_tp_mention_results: defaultdict[tuple[str, str], set[bool]] = defaultdict(set)
        ent_fp_mention_results: defaultdict[tuple[str, str], set[bool]] = defaultdict(set)
        key_requires_class_tp = {}
        key_requires_class_fp = {}
        key_requires_mention_tp = {}
        key_requires_mention_fp = {}
        for section in doc.sections:
            mapper = SpacyToKazuObjectMapper(self.nlp, section)
            for entity in section.entities:
                entity_class = entity.entity_class

                entity_match = entity.match
                key = (
                    entity_match,
                    entity_class,
                )
                maybe_class_matchers = self.class_matchers.get(entity_class)
                maybe_mention_matchers = self.mention_matchers.get(entity_class, {}).get(
                    entity.match
                )
                if maybe_class_matchers is None and maybe_mention_matchers is None:
                    continue

                tp_class_result, fp_class_result = self._check_tp_fp_matcher_rules(
                    entity, mapper, maybe_class_matchers
                )
                if tp_class_result is MatcherResult.NOT_CONFIGURED:
                    key_requires_class_tp[key] = False
                else:
                    key_requires_class_tp[key] = True
                if fp_class_result is MatcherResult.NOT_CONFIGURED:
                    key_requires_class_fp[key] = False
                else:
                    key_requires_class_fp[key] = True

                ent_tp_class_results[key].add(tp_class_result in self._tp_allowed_values)
                ent_fp_class_results[key].add(fp_class_result is MatcherResult.HIT)

                tp_mention_result, fp_mention_result = self._check_tp_fp_matcher_rules(
                    entity, mapper, maybe_mention_matchers
                )
                if tp_mention_result is MatcherResult.NOT_CONFIGURED:
                    key_requires_mention_tp[key] = False
                else:
                    key_requires_mention_tp[key] = True
                if fp_mention_result is MatcherResult.NOT_CONFIGURED:
                    key_requires_mention_fp[key] = False
                else:
                    key_requires_mention_fp[key] = True

                ent_tp_mention_results[key].add(tp_mention_result in self._tp_allowed_values)
                ent_fp_mention_results[key].add(fp_class_result is MatcherResult.HIT)

        for section in doc.sections:
            for ent in list(section.entities):
                key = (
                    ent.match,
                    ent.entity_class,
                )
                if (
                    (key_requires_class_fp[key] and True in ent_fp_class_results[key])
                    or (key_requires_class_tp[key] and True not in ent_tp_class_results[key])
                    or (key_requires_mention_fp[key] and True in ent_fp_mention_results[key])
                    or (key_requires_mention_tp[key] and True not in ent_tp_mention_results[key])
                ):
                    section.entities.remove(ent)

    @staticmethod
    def _check_matcher(context: Span, maybe_matcher: Optional[Matcher]) -> MatcherResult:
        if maybe_matcher is None:
            return MatcherResult.NOT_CONFIGURED
        if bool(maybe_matcher(context)):
            return MatcherResult.HIT
        else:
            return MatcherResult.MISS

    @staticmethod
    def _check_tp_fp_matcher_rules(
        entity: Entity, mapper: SpacyToKazuObjectMapper, matchers: Optional[TPOrFPMatcher]
    ) -> tuple[MatcherResult, MatcherResult]:
        if matchers is None:
            return MatcherResult.NOT_CONFIGURED, MatcherResult.NOT_CONFIGURED
        span = mapper.ent_to_span[entity]
        context = span.sent

        tp_matcher = matchers.get("tp")
        tp_result = RulesBasedEntityClassDisambiguationFilterStep._check_matcher(
            context, tp_matcher
        )
        fp_matcher = matchers.get("fp")
        fp_result = RulesBasedEntityClassDisambiguationFilterStep._check_matcher(
            context, fp_matcher
        )
        return tp_result, fp_result
