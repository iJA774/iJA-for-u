import pytest
from pydantic import ValidationError

from config import AuthorizationSettings


def test_workspace_administrator_scope_requires_exact_principal_match() -> None:
    authorization = AuthorizationSettings.model_validate(
        {
            "workspace_administrators": [
                {
                    "platform": "onebot",
                    "account_id": "bot-1",
                    "actor_id": "user-1",
                    "scopes": ["workspace:skills:write"],
                }
            ]
        }
    )
    expected = frozenset({"workspace:skills:write"})
    assert authorization.scopes_for(
        platform="onebot",
        account_id="bot-1",
        actor_id="user-1",
    ) == expected
    assert authorization.scopes_for(
        platform="qq",
        account_id="bot-1",
        actor_id="user-1",
    ) == frozenset()
    assert authorization.scopes_for(
        platform="onebot",
        account_id="bot-2",
        actor_id="user-1",
    ) == frozenset()
    assert authorization.scopes_for(
        platform="onebot",
        account_id="bot-1",
        actor_id="same-looking-user-on-another-account",
    ) == frozenset()
    assert authorization.scopes_for(
        platform="onebot",
        account_id="bot-1",
        actor_id=None,
    ) == frozenset()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("platform", "*"),
        ("account_id", "*"),
        ("actor_id", "*"),
    ],
)
def test_workspace_administrator_rejects_wildcards(
    field: str,
    value: str,
) -> None:
    principal = {
        "platform": "onebot",
        "account_id": "bot-1",
        "actor_id": "user-1",
        "scopes": ["workspace:skills:write"],
    }
    principal[field] = value
    with pytest.raises(ValidationError, match="禁止 wildcard"):
        AuthorizationSettings.model_validate(
            {"workspace_administrators": [principal]}
        )
