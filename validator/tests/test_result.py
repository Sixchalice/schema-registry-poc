from schema_validator.models import ValidationResult, proto_import_path
from schema_validator.result import build_result


def test_no_messages_is_ok():
    assert build_result([], []) == ValidationResult(True, "Ok", "")


def test_warnings_only():
    assert build_result([], ["w1", "w2"]) == ValidationResult(True, "Warning", "w1\nw2")


def test_errors_win_and_hide_warnings():
    assert build_result(["e1", "e2"], ["w1"]) == ValidationResult(False, "Error", "e1\ne2")


def test_duplicate_errors_are_reported_once():
    assert build_result(["e1", "e1"], []).message == "e1"


def test_proto_import_path():
    assert proto_import_path("com.acme.Address") == "com/acme/Address.proto"
