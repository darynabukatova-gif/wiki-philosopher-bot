import time
from pathlib import Path
from typing import Dict
from dataclasses import dataclass
from evaluation import (
    persist_canonical_evaluation,
    process_title,
)
from telegram_bot import send_message
from wiki_philosopher_bot.runtime import stats_lock, persistence_lock
from concurrent.futures import ThreadPoolExecutor
from wiki_philosopher_bot.worker_runner import process_completed_futures
from presentation import format_philosopher_message
from wiki_philosopher_bot.utils import RateLimiter, get_random_philosopher
from wikipedia_api import (
    build_entity_cache,
    build_page_properties_cache,
    get_all_pages,
)
from cache import (
    load_database,
    update_database_entry,
)
from wiki_philosopher_bot.config import (
    DATABASE_FILE,
    CANONICAL_DATA_FOLDER,
    MAX_QUOTES, 
    MAX_WORKERS, 
    RATE_LIMIT, 
    SEARCH_TERM, 
    load_environment,
    RUN_REPORT_FOLDER,
)
from wiki_philosopher_bot.run_reporting import (
    build_run_report,
    capture_run_baseline,
    format_run_summary,
    save_run_report,
)


RUN_REPORTS_DIRECTORY = Path(RUN_REPORT_FOLDER)

@dataclass
class RuntimeState:
    database: Dict[str, dict]
    stats: Dict[str, int]

def make_initial_stats():
    return {
        "cached_summaries": 0,
        "downloaded_summaries": 0,
        "cached_entities": 0,
        "prepared_entities": 0,
        "cached_results": 0,
        "skipped_results": 0,
        "new_results": 0,
        "cached_quotes": 0,
        "downloaded_quotes": 0,
        "failed_quotes": 0,
        "cached_encountered": 0,
        "new_accepted": 0,
        "new_rejected": 0,
    }

def load_runtime_state(data_folder):
    return RuntimeState(
        database=load_database(
            DATABASE_FILE,
            data_folder,
        ),
        stats=make_initial_stats(),
    )

def discover_pages(search_term, limiter):
    all_pages = get_all_pages(
        search_term,
        limiter=limiter,
    )

    unique_pages = {}

    for page in all_pages:
        unique_pages[page["title"]] = page

    return list(unique_pages.values())

def build_entity_lookup(pages, database, limiter):
    page_titles = [page["title"] for page in pages]
    page_properties, pageprops_errors = build_page_properties_cache(
        page_titles,
        limiter=limiter,
    )

    for page in pages:
        properties = page_properties.get(page["title"])
        page["is_disambiguation"] = (
            properties.is_disambiguation
            if properties is not None
            else None
        )

    titles = [
        page["title"]
        for page in pages
        if (
            not isinstance(database.get(page["title"]), dict)
            or not isinstance(
                database[page["title"]].get("wikidata"),
                dict,
            )
            or database[page["title"]]["wikidata"].get("status")
            not in ("available", "unavailable")
        )
    ]

    return build_entity_cache(
        titles,
        limiter=limiter,
        page_properties=page_properties,
        pageprops_errors=pageprops_errors,
    )

def report_processing_error(title, error):
    print(
        "Failed to process {!r}: {}".format(
            title,
            error,
        )
    )

def evaluate_pages(
    pages,
    state,
    all_qids,
    all_entities,
    limiter,
    max_workers,
    data_folder,
    stats_lock,
    persistence_lock,
    wikidata_errors=None,
    report_error=None,
):
    if report_error is None:
        report_error = report_processing_error

    def persist_entry(entry):
        persist_canonical_evaluation(
            entry,
            state.database,
            state.stats,
            stats_lock,
            persistence_lock,
            data_folder,
        )

    future_to_page = {}

    with ThreadPoolExecutor(
        max_workers=max_workers
    ) as executor:
        for page in pages:
            future = executor.submit(
                process_title,
                page,
                state.stats,
                state.database,
                all_qids,
                all_entities,
                stats_lock=stats_lock,
                persistence_lock=persistence_lock,
                data_folder=data_folder,
                limiter=limiter,
                wikidata_errors=wikidata_errors,
            )

            future_to_page[future] = page

        process_completed_futures(
            future_to_page,
            persist_entry=persist_entry,
            report_error=report_error,
        )

def select_candidate(state):
    return get_random_philosopher(
        state.database,
    )


def persist_canonical_posting(
    title,
    database,
    persistence_lock,
    data_folder,
):
    """Record one confirmed posting without changing legacy posting state."""
    if not isinstance(title, str) or not title:
        raise ValueError("title must be a non-empty string")

    if title not in database:
        raise ValueError(
            "Posting title is absent from the canonical database: {}".format(
                title
            )
        )

    posted_timestamp = int(time.time())

    def update_posting(entry):
        posting = entry["posting"]
        posting["has_been_posted"] = True
        posting["posted_at"].append(posted_timestamp)

    return update_database_entry(
        database,
        title,
        update_posting,
        DATABASE_FILE,
        data_folder,
        persistence_lock,
    )

def send_and_record_post(
    title,
    message,
    database,
    persistence_lock,
    data_folder,
    send=send_message,
):
    telegram_result = send(message)

    if not telegram_result.ok:
        return telegram_result

    persist_canonical_posting(
        title,
        database,
        persistence_lock,
        data_folder,
    )

    return telegram_result

def main():
    started_at = time.time()
    state = None
    baseline = None
    selected_posting_title = None
    telegram_result = None
    processing_errors = []
    runtime_error = None

    def record_processing_error(title, error):
        report_processing_error(title, error)
        processing_errors.append({
            "title": title,
            "error": "{}: {}".format(type(error).__name__, error),
        })

    try:
        load_environment()
        state = load_runtime_state(CANONICAL_DATA_FOLDER)
        baseline = capture_run_baseline(state.database)
        limiter = RateLimiter(RATE_LIMIT)

        pages = discover_pages(SEARCH_TERM, limiter)
        all_qids, all_entities, wikidata_errors = build_entity_lookup(
            pages,
            state.database,
            limiter,
        )

        evaluate_pages(
            pages=pages,
            state=state,
            all_qids=all_qids,
            all_entities=all_entities,
            limiter=limiter,
            max_workers=MAX_WORKERS,
            data_folder=CANONICAL_DATA_FOLDER,
            stats_lock=stats_lock,
            persistence_lock=persistence_lock,
            wikidata_errors=wikidata_errors,
            report_error=record_processing_error,
        )

        philosopher = select_candidate(state)

        if philosopher is None:
            print("No philosopher available")
            return 0

        selected_posting_title = philosopher["title"]
        message = format_philosopher_message(
            philosopher,
            state.database,
            state.stats,
            stats_lock,
            persistence_lock,
            CANONICAL_DATA_FOLDER,
            max_quotes=MAX_QUOTES,
            limiter=limiter,
        )

        telegram_result = send_and_record_post(
            selected_posting_title,
            message,
            state.database,
            persistence_lock,
            CANONICAL_DATA_FOLDER,
            send=send_message,
        )

        if not telegram_result.ok:
            print(
                "Telegram message was not sent:",
                telegram_result.error_reason,
            )
            return 1

        print("\nSent message:\n", message)
        return 0
    except Exception as error:
        runtime_error = "{}: {}".format(type(error).__name__, error)
        raise
    finally:
        if state is not None and baseline is not None:
            finished_at = time.time()
            telegram_data = None
            if telegram_result is not None:
                telegram_data = {
                    "ok": telegram_result.ok,
                    "error_reason": telegram_result.error_reason,
                }
            report = build_run_report(
                baseline,
                state.database,
                state.stats,
                started_at,
                finished_at,
                selected_posting_title=selected_posting_title,
                telegram_result=telegram_data,
                processing_errors=processing_errors,
                runtime_error=runtime_error,
            )
            try:
                report_path, diagnostics = save_run_report(
                    report,
                    RUN_REPORTS_DIRECTORY,
                    started_at,
                )
            except Exception as error:
                print("Failed to save run report: {}".format(error))
            else:
                print(format_run_summary(report, report_path, diagnostics))

if __name__ == "__main__":
    raise SystemExit(main())
