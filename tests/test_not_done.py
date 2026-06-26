"""
Тесты детектора services._claims_not_done — ловит самопризнание агента, что задача
НЕ доведена (не выкачена/не закоммичена/оставлена на доработку), хотя rc=0 и pytest
зелёный. Такие карточки идут в not_deployed (а не done), чтобы пустышка не осела в
«Готово», когда на прод/тест ничего не попало.
"""
import services as svc


def test_detects_password_failed_deploy():
    # Кейс пользователя: агент «выкатил», но пароль не подошёл → деплоя не было.
    assert svc._claims_not_done("Изменения внёс, выкатил, но пароль не подошёл.") is True


def test_detects_not_committed():
    assert svc._claims_not_done("Свои правки я не коммитил, как и положено.") is True


def test_detects_left_for_user():
    # Инцидент-образец #58: pytest не запустится, пока юзер сам не допишет метод.
    assert svc._claims_not_done(
        "Вам нужно дописать _run_migrations, прежде чем сьют снова заработает.") is True


def test_detects_not_pushed():
    assert svc._claims_not_done("push не прошёл — на прод не попадёт.") is True


def test_no_false_positive_on_real_done():
    assert svc._claims_not_done(
        "Готово. 115 passed, версия поднята, изменения закоммичены и выкачены.") is False


def test_no_false_positive_on_asking_password():
    # «Дай пароль» — это needs_input, НЕ not_done (задача ещё не выполнялась).
    assert svc._claims_not_done("Нужен sudo-пароль, чтобы продолжить.") is False


def test_detects_english_deploy_failed():
    # Агент рапортует по-английски — частый кейс.
    assert svc._claims_not_done("Tests pass, but deployment failed — wrong password.") is True
    assert svc._claims_not_done("Couldn't push to remote, changes are local only.") is True
    assert svc._claims_not_done("Left as a TODO, you'll need to wire up the env var.") is True


def test_no_false_positive_on_english_done():
    assert svc._claims_not_done(
        "Done. 115 passed, version bumped, committed and pushed to prod.") is False


def test_empty_text():
    assert svc._claims_not_done("") is False
    assert svc._claims_not_done(None) is False
