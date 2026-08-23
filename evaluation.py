import re
import time
import unicodedata
from typing import List
from dataclasses import dataclass, field
from cache import update_database_entry
from config import (
    CURRENT_EVALUATION_ALGORITHM_VERSION,
    DATABASE_FILE,
    MAX_QUOTES, 
    NON_HUMAN_PATTERNS,
)
from wikipedia_api import (
    get_entity_cached, 
    get_instances, 
    get_occupations, 
    get_quotes, 
    get_summary, 
    get_life_dates_from_wikidata,
)

@dataclass
class FilterResult:
    philosopher_bonus: int = 0
    human_bonus: int = 0
    content_bonus: int = 0
    nonphilosopher_penalty: int = 0
    nonhuman_penalty: int = 0
    noncontent_penalty: int = 0
    hard_rejection: bool = False
    reasons: List[str] = field(default_factory=list)


_EXACT_PHILOSOPHER_DISAMBIGUATOR_RE = re.compile(
    r"\s*\(philosopher\)\s*$",
    re.IGNORECASE,
)
_WEAK_PHILOSOPHER_DISAMBIGUATOR_RE = re.compile(
    r"\([^)]*\bphilosopher\b[^)]*\)",
    re.IGNORECASE,
)
_DISAMBIGUATION_TITLE_RE = re.compile(
    r"\s*\(disambiguation\)\s*$",
    re.IGNORECASE,
)
_TITLE_NAMESPACE_RE = re.compile(
    r"^(?:category|template|list|timeline):",
    re.IGNORECASE,
)
_TITLE_OBJECT_PHRASE_RE = re.compile(
    r"\b(?:the old philosopher|philosopher press|philosopher['’]s "
    r"(?:stone|egg))\b",
    re.IGNORECASE,
)
_LEAD_COPULAR_RE = re.compile(
    r"^\s*(?P<subject>.+?)\s+\b(?:is|was)\b\s+"
    r"(?P<predicate>.+)$",
    re.IGNORECASE,
)
_NON_ROLE_PREDICATE_TOKENS = frozenset((
    "about", "at", "book", "by", "called", "character", "creation",
    "egg", "film", "for", "from", "in", "known", "named", "novel",
    "on", "play", "portrayed", "portraying", "press", "related",
    "stone", "studied", "study", "the", "title", "to", "with", "work",
    "who", "whose", "wrote",
))

def combine_filter_results(*results):
    combined = FilterResult()

    for result in results:
        combined.philosopher_bonus += result.philosopher_bonus
        combined.human_bonus += result.human_bonus
        combined.content_bonus += result.content_bonus
        combined.nonphilosopher_penalty += (
            result.nonphilosopher_penalty
        )
        combined.nonhuman_penalty += result.nonhuman_penalty
        combined.noncontent_penalty += result.noncontent_penalty
        combined.hard_rejection = (
            combined.hard_rejection or result.hard_rejection
        )
        combined.reasons.extend(result.reasons)

    return combined


def evaluation_needs_processing(canonical_evaluation):
    """Return whether a canonical evaluation requires the current evaluator."""
    if not isinstance(canonical_evaluation, dict):
        return True

    return not (
        canonical_evaluation.get("status") in ("accepted", "rejected")
        and canonical_evaluation.get("algorithm_version")
        == CURRENT_EVALUATION_ALGORITHM_VERSION
    )


class WikidataLookupError(Exception):
    """Wikidata could not supply facts for this title in the current run."""


def prepare_entity(title, all_qids, all_entities, wikidata_errors=None):

    if wikidata_errors and title in wikidata_errors:
        raise WikidataLookupError(
            "Wikidata lookup failed for {!r}: {}".format(
                title,
                wikidata_errors[title],
            )
        )

    qid = all_qids.get(title)

    if not qid:
        return {
            "valid": False,
            "reason": "no_qid",
            "title": title
        }

    entity = get_entity_cached(qid, all_entities)

    if not entity:
        return {
            "valid": False,
            "reason": "no_entity",
            "title": title,
            "qid": qid
        }

    instances = get_instances(entity)
    occupations = get_occupations(entity)

    birth, death, death_date = get_life_dates_from_wikidata(entity)

    prepared = {
        "valid": True,
        "title": title,
        "qid": qid,

        "instances": instances,
        "occupations": occupations,

        "birth": birth,
        "death": death,
        "death_date": death_date,

        # Absence of either QID is not contradictory evidence.  A title may
        # simply have incomplete Wikidata claims, so keep unresolved facts
        # neutral rather than manufacturing False values.
        "is_human": True if "Q5" in instances else None,
        "is_philosopher": (
            True if "Q4964182" in occupations else None
        ),
    }

    return prepared

def canonical_wikidata_to_prepared(title, wikidata):
    """Adapt a resolved canonical Wikidata section for existing filters."""
    status = wikidata.get("status") if isinstance(wikidata, dict) else None

    if status == "available":
        return {
            "valid": True,
            "title": title,
            "qid": wikidata.get("qid"),
            "instances": wikidata.get("instances"),
            "occupations": wikidata.get("occupations"),
            "birth": wikidata.get("birth_year"),
            "death": wikidata.get("death_year"),
            "is_human": wikidata.get("is_human"),
            "is_philosopher": wikidata.get("is_philosopher"),
        }

    if status == "unavailable":
        prepared = {
            "valid": False,
            "title": title,
            "reason": wikidata.get("reason"),
        }
        qid = wikidata.get("qid")

        if qid is not None:
            prepared["qid"] = qid

        return prepared

    raise ValueError("Canonical Wikidata entry is not resolved")


def prepared_entity_to_canonical_wikidata(prepared):
    """Map the legacy-compatible prepared entity shape into canonical fields."""
    if prepared.get("valid") is True:
        return {
            "status": "available",
            "reason": None,
            "qid": prepared.get("qid"),
            "instances": prepared.get("instances"),
            "occupations": prepared.get("occupations"),
            "birth_year": prepared.get("birth"),
            "death_year": prepared.get("death"),
            "death_date": prepared.get("death_date"),
            "is_human": prepared.get("is_human"),
            "is_philosopher": prepared.get("is_philosopher"),
            "fetched_at": int(time.time()),
        }

    return {
        "status": "unavailable",
        "reason": prepared.get("reason"),
        "qid": prepared.get("qid"),
        "instances": [],
        "occupations": [],
        "birth_year": None,
        "death_year": None,
        "death_date": None,
        "is_human": None,
        "is_philosopher": None,
        "fetched_at": None,
    }


def prepare_entity_cached(
    title,
    database,
    all_qids,
    all_entities,
    stats,
    stats_lock,
    persistence_lock,
    data_folder=None,
    wikidata_errors=None,
):
    entry = database.get(title)
    wikidata = entry.get("wikidata") if isinstance(entry, dict) else None

    if (
        isinstance(wikidata, dict)
        and wikidata.get("status") in ("available", "unavailable")
    ):
        with stats_lock:
            stats["cached_entities"] += 1

        return canonical_wikidata_to_prepared(title, wikidata)

    if wikidata_errors:
        prepared = prepare_entity(
            title,
            all_qids,
            all_entities,
            wikidata_errors=wikidata_errors,
        )
    else:
        prepared = prepare_entity(title, all_qids, all_entities)

    def update_wikidata(entry):
        entry["wikidata"] = prepared_entity_to_canonical_wikidata(prepared)

    update_database_entry(
        database=database,
        title=title,
        update_callback=update_wikidata,
        filename=DATABASE_FILE,
        data_folder=data_folder,
        persistence_lock=persistence_lock,
    )

    with stats_lock:
        stats["prepared_entities"] += 1

    return prepared

def accept(
    title,
    philosopher_confidence,
    human_confidence,
    content_confidence,
    reasons
):

    entry = {
        "title": title,
        "status": "accepted",
        "philosopher_confidence": philosopher_confidence,
        "human_confidence": human_confidence,
        "content_confidence": content_confidence,
        "reasons": reasons,
        "last_processed": time.time()
    }

    return entry

def reject(
    title,
    philosopher_confidence,
    human_confidence,
    content_confidence,
    reasons
):
    entry = {
        "title": title,
        "status": "rejected",
        "philosopher_confidence": philosopher_confidence,
        "human_confidence": human_confidence,
        "content_confidence": content_confidence,
        "reasons": reasons,
        "last_processed": time.time()
    }

    return entry


def _validate_flat_evaluation_result(result):
    """Reject malformed worker results before canonical persistence."""
    if not isinstance(result, dict):
        raise ValueError("Evaluation result must be an object")

    title = result.get("title")
    if not isinstance(title, str) or not title:
        raise ValueError("Evaluation result title must be a non-empty string")

    status = result.get("status")
    if status not in ("accepted", "rejected"):
        raise ValueError("Unexpected evaluation status: {!r}".format(status))

    for field_name in (
        "human_confidence",
        "philosopher_confidence",
        "content_confidence",
    ):
        value = result.get(field_name)
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(
                "Evaluation result {} must be an integer".format(
                    field_name
                )
            )

    reasons = result.get("reasons")
    if (
        not isinstance(reasons, list)
        or not all(isinstance(reason, str) for reason in reasons)
    ):
        raise ValueError(
            "Evaluation result reasons must be a list of strings"
        )

    processed_at = result.get("last_processed")
    if (
        not isinstance(processed_at, (int, float))
        or isinstance(processed_at, bool)
    ):
        raise ValueError(
            "Evaluation result last_processed must be a number"
        )


def persist_canonical_evaluation(
    result,
    database,
    stats,
    stats_lock,
    persistence_lock,
    data_folder,
):
    """Persist one flat worker result into only its canonical evaluation."""
    _validate_flat_evaluation_result(result)

    title = result["title"]
    status = result["status"]

    def update_evaluation(entry):
        evaluation = entry["evaluation"]
        evaluation["status"] = status
        evaluation["algorithm_version"] = (
            CURRENT_EVALUATION_ALGORITHM_VERSION
        )
        evaluation["human_confidence"] = result["human_confidence"]
        evaluation["philosopher_confidence"] = result[
            "philosopher_confidence"
        ]
        evaluation["content_confidence"] = result["content_confidence"]
        evaluation["reasons"] = list(result["reasons"])
        evaluation["processed_at"] = result["last_processed"]

    final_hash = update_database_entry(
        database=database,
        title=title,
        update_callback=update_evaluation,
        filename=DATABASE_FILE,
        data_folder=data_folder,
        persistence_lock=persistence_lock,
    )

    with stats_lock:
        if status == "accepted":
            stats["new_accepted"] += 1
        else:
            stats["new_rejected"] += 1

    return final_hash

def title_filter(title):
    result = FilterResult()

    if _DISAMBIGUATION_TITLE_RE.search(title):
        result.hard_rejection = True
        result.reasons.append(
            "title hard rejection: disambiguation page"
        )
        return result

    exact_philosopher_disambiguator = bool(
        _EXACT_PHILOSOPHER_DISAMBIGUATOR_RE.search(title)
    )

    if _TITLE_NAMESPACE_RE.search(title) or ":" in title:
        result.nonhuman_penalty += 1
        result.reasons.append(
            "title nonhuman penalty (-1): namespace"
        )

    if _TITLE_OBJECT_PHRASE_RE.search(title):
        result.nonhuman_penalty += 1
        result.nonphilosopher_penalty += 1
        result.reasons.append(
            "title nonhuman + nonphilosopher penalty (-2): object phrase"
        )

    if exact_philosopher_disambiguator:
        result.human_bonus += 1
        result.philosopher_bonus += 2
        result.reasons.append(
            "title human bonus (+1): exact (philosopher)"
        )
        result.reasons.append(
            "title philosopher bonus (+2): exact (philosopher)"
        )
    elif _WEAK_PHILOSOPHER_DISAMBIGUATOR_RE.search(title):
        result.philosopher_bonus += 1
        result.human_bonus += 1
        result.reasons.append(
            "title human + philosopher bonus (+2): related philosopher disambiguator"
        )

    return result


def page_structure_filter(page):
    """Reject pageprops-confirmed disambiguation pages before content work."""
    result = FilterResult()
    if isinstance(page, dict) and page.get("is_disambiguation") is True:
        result.hard_rejection = True
        result.reasons.append(
            "page hard rejection: Wikipedia disambiguation page"
        )
    return result


def _name_tokens(value):
    """Return case-folded whole-word name tokens without punctuation aliases."""
    if not isinstance(value, str):
        return []

    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.findall(r"[^\W_]+", normalized, re.UNICODE)


def title_matches_lead_subject(title, lead_subject):
    """Return whether canonical title-name tokens occur in lead-name order."""
    if not isinstance(title, str) or not isinstance(lead_subject, str):
        return False

    title_without_disambiguator = re.sub(
        r"\s*\([^)]*\)\s*$",
        "",
        title,
    )
    title_tokens = _name_tokens(title_without_disambiguator)
    lead_tokens = _name_tokens(lead_subject)
    if not title_tokens or not lead_tokens:
        return False

    def tokens_match(title_token, lead_token):
        return (
            title_token == lead_token
            or (
                len(title_token) == 1
                and lead_token.startswith(title_token)
            )
        )

    lead_index = 0
    matched_full_token = False
    for title_token in title_tokens:
        while (
            lead_index < len(lead_tokens)
            and not tokens_match(title_token, lead_tokens[lead_index])
        ):
            lead_index += 1
        if lead_index == len(lead_tokens):
            break
        if len(title_token) > 1 and title_token == lead_tokens[lead_index]:
            matched_full_token = True
        lead_index += 1
    else:
        # A full token prevents two unrelated initials from becoming an
        # accidental identity match (for example, "A. B." / "Alice Brown").
        if matched_full_token:
            return True

    # Wikipedia sometimes shortens an article subject by omitting a title's
    # interior given name.  Accept that narrowly when its exact first and last
    # name tokens still identify the same person; never omit a first or last
    # token and never reorder identity tokens.
    if len(title_tokens) < 3:
        return False

    first_token = title_tokens[0]
    last_token = title_tokens[-1]
    if len(first_token) == 1 or len(last_token) == 1:
        return False

    try:
        first_index = lead_tokens.index(first_token)
        last_index = len(lead_tokens) - 1 - lead_tokens[::-1].index(last_token)
    except ValueError:
        return False

    return first_index < last_index


def _predicate_has_direct_philosopher_role(predicate):
    """Recognize a bounded role list ending in a direct philosopher role."""
    predicate = re.split(r"[.!?]", predicate, maxsplit=1)[0].strip()
    philosopher_match = re.search(
        r"\bphilosopher\b",
        predicate,
        re.IGNORECASE,
    )
    if philosopher_match is None:
        return False

    before = predicate[:philosopher_match.start()].strip()
    after = predicate[philosopher_match.end():].lstrip()
    if after.startswith(("'", "’")):
        return False
    if after and not re.match(
        r"^(?:[.,;:]|\b(?:who|whose|and|of|at|in|for|from|with)\b)",
        after,
        re.IGNORECASE,
    ):
        return False

    if before.casefold() in ("", "a", "an"):
        return True

    role_prefix = re.sub(r"^(?:a|an)\s+", "", before, flags=re.IGNORECASE)
    role_tokens = _name_tokens(role_prefix)
    if not role_tokens:
        return False
    if role_tokens[0] in ("the", "business"):
        return False
    if any(token in _NON_ROLE_PREDICATE_TOKENS for token in role_tokens):
        return False
    return True


def _summary_has_direct_subject_philosopher_statement(title, summary):
    """Return whether the lead directly defines this article subject as philosopher."""
    if not isinstance(summary, str):
        return False

    lead_match = _LEAD_COPULAR_RE.match(summary)
    if lead_match is None:
        return False

    return (
        title_matches_lead_subject(title, lead_match.group("subject"))
        and _predicate_has_direct_philosopher_role(
            lead_match.group("predicate")
        )
    )

def summary_filter(
    title,
    database,
    stats,
    stats_lock,
    persistence_lock,
    data_folder=None,
    limiter=None,
):
    result = FilterResult()

    summary = get_summary(
        title,
        database,
        stats,
        stats_lock,
        persistence_lock,
        data_folder,
        limiter=limiter,
    )

    if summary is None:
        return result

    summary_lower = summary.lower()
    # Do not split after initials such as "R. G. Collingwood".  The filters
    # only need a conservative lead sentence for object-type checks.
    first_sentence = re.split(
        r"(?<=[a-z0-9])[.!?]\s+",
        summary_lower,
        maxsplit=1,
    )[0]

    if _summary_has_direct_subject_philosopher_statement(
        title,
        summary,
    ):
        result.human_bonus += 1
        result.philosopher_bonus += 2
        result.reasons.append(
            "summary human bonus (+1): direct biographical philosopher statement"
        )
        result.reasons.append(
            "summary philosopher bonus (+2): direct biographical philosopher statement"
        )

    for pattern in NON_HUMAN_PATTERNS:
        if re.search(pattern, first_sentence):
            result.nonhuman_penalty += 1
            result.nonphilosopher_penalty += 1
            result.reasons.append(
                "summary nonhuman + nonphilosopher penalty (-2): {}".format(
                    pattern
                )
            )

    if "author" in first_sentence:
        result.human_bonus += 1
        result.reasons.append(
            "summary human bonus (+1): author"
        )

    if "known for his novels" in first_sentence:
        result.human_bonus += 1
        result.reasons.append(
            "summary human bonus (+1): known for his novels"
        )

    if "a book by" in first_sentence:
        result.nonhuman_penalty += 1
        result.reasons.append(
            "summary nonhuman penalty (-1): a book by"
        )

    if "was born" in summary_lower:
        result.human_bonus += 1
        result.reasons.append(
            "summary human bonus (+1): was born"
        )

    if "professor" in first_sentence:
        result.human_bonus += 1
        result.reasons.append(
            "summary human bonus (+1): professor"
        )

    if "academic" in first_sentence:
        result.human_bonus += 1
        result.reasons.append(
            "summary human bonus (+1): academic"
        )

    return result

def wikidata_filter(
    title,
    database,
    all_qids,
    all_entities,
    stats,
    stats_lock,
    persistence_lock,
    data_folder=None,
    wikidata_errors=None,
):
    result = FilterResult()

    prepared = prepare_entity_cached(
        title,
        database,
        all_qids,
        all_entities,
        stats,
        stats_lock,
        persistence_lock,
        data_folder=data_folder,
        wikidata_errors=wikidata_errors,
    )

    is_human = prepared.get("is_human")
    is_philosopher = prepared.get("is_philosopher")
    birth_w = prepared.get("birth")

    if is_human is True:
        result.human_bonus += 2
        result.reasons.append(
            "wikidata human bonus (+2): is_human = true"
        )
    elif is_human is False:
        result.nonhuman_penalty += 1
        result.reasons.append(
            "wikidata nonhuman penalty (-1): is_human = false"
        )

    if is_philosopher is True:
        result.philosopher_bonus += 2
        result.reasons.append(
            "wikidata philosopher bonus (+2): is_philosopher = true"
        )

    if birth_w is not None:
        result.human_bonus += 2
        result.reasons.append(
            "wikidata human bonus (+2): birth_w not None"
        )

    return result

def quote_filter(
    title,
    database,
    stats,
    stats_lock,
    persistence_lock,
    data_folder=None,
    max_quotes=MAX_QUOTES,
    limiter=None,
):
    result = FilterResult()

    quotes = get_quotes(
        title,
        database,
        stats,
        stats_lock,
        persistence_lock,
        data_folder,
        max_quotes=MAX_QUOTES,
        limiter=limiter,
    )

    if quotes:
        result.content_bonus += 1
        result.reasons.append(
            "quotes bonus (+1): quotes exist"
        )

        good_quotes = [
            q for q in quotes
            if 2 <= q["word_count"] <= 100
        ]

        if good_quotes:
            result.content_bonus += 1
            result.reasons.append(
                "quotes bonus (+1): good quotes exist"
            )
        else:
            result.noncontent_penalty += 1
            result.reasons.append(
                "quotes noncontent penalty (-1): good quotes do not exist"
            )
    else:
        result.noncontent_penalty += 1
        result.reasons.append(
            "quotes noncontent penalty (-1): quotes do not exist"
        )

    return result

def process_title(
        page,
        stats,
        database,
        all_qids,
        all_entities,
        stats_lock,
        persistence_lock,
        data_folder=None,
        limiter=None,
        wikidata_errors=None,
    ):
    title = page.get("title")

    if not title:
        return None

    entry = database.get(title)
    canonical_evaluation = (
        entry.get("evaluation")
        if isinstance(entry, dict)
        else None
    )

    if not evaluation_needs_processing(canonical_evaluation):
        with stats_lock:
            stats["cached_encountered"] += 1

        return None

    page_result = page_structure_filter(page)

    if page_result.hard_rejection:
        return reject(
            title,
            philosopher_confidence=0,
            human_confidence=0,
            content_confidence=0,
            reasons=page_result.reasons,
        )

    title_result = title_filter(title)

    if title_result.hard_rejection:
        return reject(
            title,
            philosopher_confidence=0,
            human_confidence=0,
            content_confidence=0,
            reasons=title_result.reasons,
        )

    summary_result = summary_filter(
        title,
        database,
        stats,
        stats_lock,
        persistence_lock,
        data_folder=data_folder,
        limiter=limiter,
    )

    wikidata_result = wikidata_filter(
        title,
        database,
        all_qids,
        all_entities,
        stats,
        stats_lock,
        persistence_lock,
        data_folder=data_folder,
        wikidata_errors=wikidata_errors,
    )

    prequote_combined = combine_filter_results(
        title_result,
        summary_result,
        wikidata_result,
    )

    human_confidence = (
        prequote_combined.human_bonus
        - prequote_combined.nonhuman_penalty
    )

    philosopher_confidence = (
        prequote_combined.philosopher_bonus
        - prequote_combined.nonphilosopher_penalty
    )

    content_confidence = (
        prequote_combined.content_bonus
        - prequote_combined.noncontent_penalty
    )

    if (
        human_confidence <= 0
        or philosopher_confidence <= 0
    ):
        return reject(
            title,
            philosopher_confidence,
            human_confidence,
            content_confidence,
            prequote_combined.reasons
        )

    quote_result = quote_filter(
        title,
        database,
        stats,
        stats_lock,
        persistence_lock,
        data_folder=data_folder,
        max_quotes=MAX_QUOTES,
        limiter=limiter,
    )

    combined = combine_filter_results(
        prequote_combined,
        quote_result,
    )

    human_confidence = (
        combined.human_bonus
        - combined.nonhuman_penalty
    )

    philosopher_confidence = (
        combined.philosopher_bonus
        - combined.nonphilosopher_penalty
    )

    content_confidence = (
        combined.content_bonus
        - combined.noncontent_penalty
    )

    if (
        human_confidence <= 0
        or philosopher_confidence <= 0
    ):
        return reject(
            title,
            philosopher_confidence,
            human_confidence,
            content_confidence,
            combined.reasons
        )

    return accept(
        title,
        philosopher_confidence,
        human_confidence,
        content_confidence,
        combined.reasons
    )
