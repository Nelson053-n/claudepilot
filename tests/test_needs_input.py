"""
Тесты детектора services._needs_user_input — распознаёт, что агент НЕ завершил
задачу, а ждёт ответа/доступа/РЕШЕНИЯ юзера. Такие карточки идут в needs_input
(колонка approved) с полем ответа в UI, а не в «Проверка».
"""
import services as svc


def test_detects_decision_fork_53():
    # Инцидент #53: агент-аналитик заканчивает РАЗВИЛКОЙ-вопросом, но добавляет
    # уверенную фразу-концовку → endswith('?') промахивался. Должно ловиться.
    text = (
        "Итог: на подписке оптимизируешь пропускную способность 5h-окна.\n\n"
        "Скажи, что делаем дальше: реализовать A (роутинг модели), добавить "
        "B (--max-turns), или сначала разложить в карточки prof? "
        "Без твоего слова код не трогаю.")
    assert svc._needs_user_input(text) is True


def test_detects_access_block():
    assert svc._needs_user_input("Не смог проверить — нет доступа к серверу.") is True


def test_detects_password_request():
    assert svc._needs_user_input("Нужен sudo-пароль из памяти, чтобы продолжить.") is True


def test_detects_trailing_question():
    assert svc._needs_user_input("Готово частично. Продолжить с остальными файлами?") is True


def test_no_false_positive_on_done_report():
    assert svc._needs_user_input("Готово. 64 passed, изменения закоммичены.") is False


def test_no_false_positive_on_code_with_paren():
    # Вопрос/скобки внутри кода-отчёта не должны триггерить.
    assert svc._needs_user_input("Исправил is_valid(x) и rerun(). Тесты зелёные.") is False


def test_empty_text():
    assert svc._needs_user_input("") is False
    assert svc._needs_user_input(None) is False
