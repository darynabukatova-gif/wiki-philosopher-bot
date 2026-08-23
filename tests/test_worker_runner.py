import pytest
from concurrent.futures import Future
from worker_runner import process_completed_futures

def completed_future(value):
    future = Future()
    future.set_result(value)
    return future

def failed_future(error):
    future = Future()
    future.set_exception(error)
    return future

def test_process_completed_futures_continues_after_worker_error():
    failed = failed_future(RuntimeError("bad page"))
    succeeded = completed_future(
        {
            "title": "Ada Lovelace",
            "status": "accepted",
        }
    )

    persisted = []
    errors = []

    process_completed_futures(
        {
            failed: {"title": "Broken title"},
            succeeded: {"title": "Ada Lovelace"},
        },
        persist_entry=persisted.append,
        report_error=lambda title, error: errors.append(
            (title, type(error), str(error))
        ),
    )

    assert persisted == [
        {
            "title": "Ada Lovelace",
            "status": "accepted",
        }
    ]
    assert errors == [
        ("Broken title", RuntimeError, "bad page")
    ]

def test_process_completed_futures_reports_persistence_error_and_keeps_other_success():
    first = completed_future(
        {
            "title": "Ada Lovelace",
            "status": "accepted",
        }
    )

    second = completed_future(
        {
            "title": "Plato",
            "status": "accepted",
        }
    )

    persisted = []
    errors = []

    def persist_entry(entry):
        if entry["title"] == "Ada Lovelace":
            raise OSError("disk full")

        persisted.append(entry)

    process_completed_futures(
        {
            first: {"title": "Ada Lovelace"},
            second: {"title": "Plato"},
        },
        persist_entry=persist_entry,
        report_error=lambda title, error: errors.append(
            (title, type(error), str(error))
        ),
    )

    assert persisted == [
        {
            "title": "Plato",
            "status": "accepted",
        }
    ]

    assert errors == [
        ("Ada Lovelace", OSError, "disk full")
    ]

def test_process_completed_futures_ignores_none_results():
    skipped = completed_future(None)

    persisted = []
    errors = []

    process_completed_futures(
        {
            skipped: {"title": "Already processed"},
        },
        persist_entry=persisted.append,
        report_error=lambda title, error: errors.append(
            (title, error)
        ),
    )

    assert persisted == []
    assert errors == []

def test_process_completed_futures_does_not_swallow_system_exit():
    future = failed_future(SystemExit(1))

    with pytest.raises(SystemExit):
        process_completed_futures(
            {
                future: {"title": "Ada Lovelace"},
            },
            persist_entry=lambda entry: None,
            report_error=lambda title, error: None,
        )


def test_process_completed_futures_propagates_value_error_from_persistence():
    future = completed_future(
        {"title": "Ada Lovelace", "status": "accepted"}
    )

    with pytest.raises(ValueError, match="contract bug"):
        process_completed_futures(
            {
                future: {"title": "Ada Lovelace"},
            },
            persist_entry=lambda entry: (_ for _ in ()).throw(
                ValueError("contract bug")
            ),
            report_error=lambda title, error: pytest.fail(
                "ValueError must not be reported as an operational error"
            ),
        )

def test_process_completed_futures_continues_with_rejected_entry_after_persistence_error():
    first = completed_future({"title": "Unsaved", "status": "accepted"})
    second = completed_future({"title": "Saved", "status": "rejected"})

    persisted = []
    errors = []

    def persist_entry(entry):
        if entry["title"] == "Unsaved":
            raise OSError("disk full")
        persisted.append(entry)

    process_completed_futures(
        {
            first: {"title": "Unsaved"},
            second: {"title": "Saved"},
        },
        persist_entry=persist_entry,
        report_error=lambda title, error: errors.append((title, str(error))),
    )

    assert persisted == [{"title": "Saved", "status": "rejected"}]
    assert errors == [("Unsaved", "disk full")]
