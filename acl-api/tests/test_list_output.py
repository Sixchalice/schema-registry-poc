import pytest

from acl_api.cli.list_output import JavaMapParser, parse_list_output
from acl_api.errors import UnexpectedCliOutput

REAL_OUTPUT = """Current ACL's for Subject Operations are
{platform-admin={*=[SUBJECT_READ, SUBJECT_WRITE]}, c17.svc-a={orders-key=[SUBJECT_READ], orders-value=[]}}
Current ACL's for Global Operations are
{platform-admin=[GLOBAL_READ]}
"""


def test_parses_real_cli_output():
    snapshot = parse_list_output(REAL_OUTPUT)

    assert snapshot.subject_operations == {
        "platform-admin": {"*": ["SUBJECT_READ", "SUBJECT_WRITE"]},
        "c17.svc-a": {"orders-key": ["SUBJECT_READ"], "orders-value": []},
    }
    assert snapshot.global_operations == {"platform-admin": ["GLOBAL_READ"]}


def test_empty_maps():
    snapshot = parse_list_output(
        "Current ACL's for Subject Operations are \n{}\nCurrent ACL's for Global Operations are \n{}\n")

    assert snapshot.subject_operations == {}
    assert snapshot.global_operations == {}


def test_header_without_a_map_means_empty():
    snapshot = parse_list_output(
        "Current ACL's for Subject Operations are \nCurrent ACL's for Global Operations are \n")

    assert snapshot.subject_operations == {}


@pytest.mark.parametrize("text, expected", [
    ("{a, b={s=[X]}}", {"a, b": {"s": ["X"]}}),     # ', ' inside a key
    ("{p=q={s=t=[X, Y]}}", {"p=q": {"s=t": ["X", "Y"]}}),  # '=' inside keys
    ("{p={}}", {"p": {}}),
])
def test_keys_may_contain_separators(text, expected):
    assert JavaMapParser(text).parse() == expected


@pytest.mark.parametrize("output", [
    "",
    "something else entirely",
    "Current ACL's for Subject Operations are \n{p={s=[X]}\nCurrent ACL's for Global Operations are \n{}",
    "Current ACL's for Subject Operations are \n{p=[X]} trailing\nCurrent ACL's for Global Operations are \n{}",
])
def test_unparseable_output_raises(output):
    with pytest.raises(UnexpectedCliOutput):
        parse_list_output(output)
