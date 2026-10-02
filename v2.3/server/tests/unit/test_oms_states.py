import pytest

from app.oms.states import IllegalTransition, OrderState as S, TERMINAL, classify_qmt, legacy_status, transition


@pytest.mark.parametrize("status,traded,ordered,expected", [
    (48, 0, 100, S.ACKED), (49, 0, 100, S.ACKED), (50, 0, 100, S.ACKED), (55, 40, 100, S.PARTIAL),
    (51, 0, 100, S.PENDING_CANCEL), (52, 40, 100, S.PENDING_CANCEL), (53, 40, 100, S.CANCELLED),
    (54, 0, 100, S.CANCELLED), (56, 100, 100, S.FILLED), (57, 0, 100, S.REJECTED), (255, 0, 100, S.UNKNOWN),
    (50, 100, 100, S.FILLED), (999, 0, 100, S.UNKNOWN)])
def test_classify(status, traded, ordered, expected):
    assert classify_qmt(status, traded, ordered) is expected


def test_terminal_is_sticky_and_idempotent():
    assert transition(S.FILLED, S.FILLED) is S.FILLED
    for terminal in TERMINAL:
        with pytest.raises(IllegalTransition):
            transition(terminal, S.ACKED)


def test_planned_can_only_start_submitting_or_be_dropped():
    assert transition(S.PLANNED, S.SUBMITTING) is S.SUBMITTING
    assert transition(S.PLANNED, S.NOT_SUBMITTED) is S.NOT_SUBMITTED
    with pytest.raises(IllegalTransition):
        transition(S.PLANNED, S.FILLED)


def test_unknown_never_goes_back_to_planned_and_can_be_resolved():
    assert transition(S.UNKNOWN, S.ACKED) is S.ACKED
    assert transition(S.UNKNOWN, S.FILLED) is S.FILLED
    assert transition(S.UNKNOWN, S.NOT_SUBMITTED) is S.NOT_SUBMITTED
    with pytest.raises(IllegalTransition):
        transition(S.UNKNOWN, S.PLANNED)


def test_pending_cancel_is_not_terminal():
    assert S.PENDING_CANCEL not in TERMINAL
    assert transition(S.PENDING_CANCEL, S.CANCELLED) is S.CANCELLED
    assert transition(S.PENDING_CANCEL, S.FILLED) is S.FILLED
    with pytest.raises(IllegalTransition):
        transition(S.PENDING_CANCEL, S.ACKED)


def test_partial_cannot_regress_to_acked():
    with pytest.raises(IllegalTransition):
        transition(S.PARTIAL, S.ACKED)
    assert transition(S.PARTIAL, S.EXPIRED_DAY) is S.EXPIRED_DAY


def test_legacy_mapping():
    assert legacy_status(S.FILLED, 100) == "FILLED"
    assert legacy_status(S.EXPIRED_DAY, 300) == "CANCELLED"
    assert legacy_status(S.EXPIRED_DAY, 0) == "CANCELLED"
    assert legacy_status(S.CANCELLED, 0) == "CANCELLED"
    assert legacy_status(S.REJECTED, 0) == "REJECTED"
    assert legacy_status(S.ACKED, 0) is None
    assert legacy_status(S.ACKED, 200) == "PARTIAL"
    assert legacy_status(S.UNKNOWN, 0) is None
    assert legacy_status(S.NOT_SUBMITTED, 0) == "NOT_SUBMITTED"
