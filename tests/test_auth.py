"""
Тесты токен-аутентификации prof.

(а) мутация без токена → 401
(б) мутация с токеном → 200
(в) GET без токена → 200

Мутация для теста — DELETE несуществующей карточки: проходит через middleware,
а в обработчике это безопасный no-op (DELETE ... WHERE id=<нет> ничего не меняет).
"""
from conftest import TOKEN

# заведомо несуществующий id — мутация не портит реальные данные
GHOST_CARD = 999_999_999


def test_mutation_without_token_401(client):
    r = client.delete(f"/api/cards/{GHOST_CARD}")
    assert r.status_code == 401


def test_mutation_with_cookie_token_200(client):
    client.cookies.set("prof_token", TOKEN)
    r = client.delete(f"/api/cards/{GHOST_CARD}")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_mutation_with_bearer_token_200(client):
    r = client.delete(
        f"/api/cards/{GHOST_CARD}",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_mutation_with_wrong_token_401(client):
    r = client.delete(
        f"/api/cards/{GHOST_CARD}",
        headers={"Authorization": "Bearer nope"},
    )
    assert r.status_code == 401


def test_get_without_token_200(client):
    r = client.get("/api/todos")
    assert r.status_code == 200


def test_index_sets_cookie_and_opens(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "prof_token" in r.cookies
    # токен подставлен в страницу вместо плейсхолдера
    assert "__PROF_TOKEN__" not in r.text
