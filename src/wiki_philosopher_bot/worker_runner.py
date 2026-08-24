from concurrent.futures import as_completed


def process_completed_futures(
    future_to_page,
    persist_entry,
    report_error,
):
    for future in as_completed(future_to_page):
        page = future_to_page[future]
        title = page.get("title", "<unknown title>")

        try:
            entry = future.result()
        except Exception as error:
            report_error(title, error)
            continue

        if entry is None:
            continue

        try:
            persist_entry(entry)
        except OSError as error:
            report_error(
                entry.get("title", title),
                error,
            )
            continue