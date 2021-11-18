import logging
from typing import List, Tuple, Optional, Dict

from azner.data.data import Document, CharSpan, Section
from steps import BaseStep

logger = logging.getLogger(__name__)


class StringPreprocessorStep(BaseStep):
    """
    A special class of Base Step, that involves destructive string preprocessing (e.g. for abbreviation expansion,
    slash expansion etc). Since these types of process change offsets, this step keeps a map of modified:original
    offsets for all changes, such that offsets can be recalculated back to the original string

    simple implementations need only override create_modifications.
    """

    def __init__(self, depends_on: Optional[List[str]]):
        super().__init__(depends_on)

    def recalculate_offset_maps(
        self, offset_map: Dict[CharSpan, CharSpan], shifts: Dict[CharSpan, int]
    ) -> Dict[CharSpan, CharSpan]:
        """
        after all modifications are processed, we need to recalculate the expanded offset locations based on
        the number of characters added by previous modifications in the string
        :param offset_map: map of modified: original offsets. usually Section.offset_map
        :param shifts: a dict of [CharSpan:int], representing the shift direction caused by the charspan
        :return:
        """
        recalc_offset_map = {}
        for modified_char_span in offset_map:
            # get all the shifts before the modified span
            reverse_shifts = [shifts[key] for key in shifts if key < modified_char_span]
            new_start = modified_char_span.start + sum(reverse_shifts)
            new_end = modified_char_span.end + sum(reverse_shifts)
            recalc_offset_map[CharSpan(start=new_start, end=new_end)] = offset_map[
                modified_char_span
            ]
        return recalc_offset_map

    def modify_string(
        self, section: Section, modifications: List[Tuple[CharSpan, str]]
    ) -> Tuple[str, Dict[CharSpan, CharSpan]]:
        """
        processes a document for modifications, returning a new string with all modifications processed
        :param section: section to modify
        :return: modifications list of Tuples of the charspan to change, and the string to change it with
        """

        # must be processed in reverse order
        modifications_sorted = sorted(modifications, key=lambda x: x[0].start, reverse=True)
        offset_map = {}
        shifts: Dict[CharSpan:int] = {}
        result = section.get_text()
        for i, (char_span, new_text) in enumerate(modifications_sorted):
            before = result[0 : char_span.start]
            after = result[char_span.end :]
            result = f"{before}{new_text}{after}"

            new_char_span = CharSpan(start=char_span.start, end=(char_span.start + len(new_text)))
            offset_map[new_char_span] = CharSpan(start=char_span.start, end=char_span.end)
            shifts[new_char_span] = len(new_text) - len(result[char_span.start : char_span.end])

        # merge old and new offset maps before recalculation
        if section.offset_map is not None:
            self.merge_offset_maps(offset_map, section.offset_map)
        recalc_offset_map = self.recalculate_offset_maps(offset_map, shifts)
        return result, recalc_offset_map

    def merge_offset_maps(self, map1: Dict[CharSpan, CharSpan], map2: Dict[CharSpan, CharSpan]):
        """
        when merging maps, we need to check if any of the keys are overlapping spans. If so, we need merge them into a
        new span
        :param map1:
        :param map2:
        :return:
        """

        new_overlaps_map = {}
        new_uniques_map = {}
        for key1 in map1:
            for key2 in map2:
                if key1.is_overlapped(key2):
                    new_mod_span = CharSpan(
                        start=min([key1.start, key2.start]), end=max([key1.end, key2.end])
                    )
                    new_original_span = CharSpan(
                        start=min([map1[key1].start, map2[key2].start]),
                        end=max([map1[key1].end, map2[key2].end]),
                    )
                    new_overlaps_map[new_mod_span] = new_original_span
                else:
                    new_uniques_map[key2] = map2[key2]
                    new_uniques_map[key1] = map1[key1]

        new_uniques_map.update(new_overlaps_map)
        return new_uniques_map

    def create_modifications(self, section: Section) -> List[Tuple[CharSpan, str]]:
        """
        implementations should return a List[Tuple[Charspan,str]] of the modifications you want to make
        the Charspan refers to the span in the Section.get_text() that you want to modify. The str is the text that you
         want to insert (i.e. use '' for deletion).Note, that Section.get_text() is an accessor for Section.text,
         returning either an already preprocessed string if available, or the original if not. This allows you to chain
         together multiple modifiers, while retaining a reference to the original text via Section.offset_map
        :param section:
        :return:
        """
        raise NotImplementedError()

    def _run(self, docs: List[Document]) -> Tuple[List[Document], List[Document]]:
        for doc in docs:
            for section in doc.sections:
                modifications = self.create_modifications(section)
                new_string, new_offset_map = self.modify_string(
                    section=section, modifications=modifications
                )
                section.preprocessed_text = new_string
                section.offset_map = new_offset_map
        return docs, []
