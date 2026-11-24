"""Actionable local storage errors that must never be retried as network errors."""

import errno


def storage_message(error):
    code = getattr(error, "errno", None)
    if code in {errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)}:
        return "Недостаточно места на диске. Освободи место и повтори операцию; уже проверенные файлы сохранены."
    if isinstance(error, PermissionError) or code in {errno.EACCES, errno.EPERM, errno.EROFS}:
        return "Нет доступа к файлам BlockHarbor. Проверь права на папку данных и не заблокирован ли файл другой программой."
    if code == errno.ENAMETOOLONG:
        return "Путь к файлу слишком длинный. Перемести данные BlockHarbor в папку с более коротким путём."
    if code in {errno.EMFILE, errno.ENFILE}:
        return "Система не может открыть ещё один файл. Закрой ненужные приложения и повтори операцию."
    return None
