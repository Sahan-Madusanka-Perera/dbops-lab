from dbops.model import Check, Status, grade, summarize, worst


def test_grade_boundaries_are_inclusive():
    assert grade(79.9, 80, 90) is Status.OK
    assert grade(80, 80, 90) is Status.WARN
    assert grade(90, 80, 90) is Status.CRIT


def test_worst_prefers_failures_over_unknown():
    assert worst([Status.OK, Status.UNKNOWN]) is Status.UNKNOWN
    assert worst([Status.UNKNOWN, Status.WARN]) is Status.WARN
    assert worst([Status.WARN, Status.CRIT, Status.UNKNOWN]) is Status.CRIT
    assert worst([]) is Status.OK


def test_exit_codes_follow_nagios():
    assert [int(s) for s in Status] == [0, 1, 2, 3]


def test_summarize_lists_every_status():
    checks = [Check("a", "x", Status.OK), Check("a", "y", Status.CRIT), Check("b", "x", Status.OK)]
    assert summarize(checks) == {"OK": 2, "WARN": 0, "CRIT": 1, "UNKNOWN": 0}
