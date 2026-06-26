#!/usr/bin/env bash
# prof: маркер «агент ждёт ввода/разрешения».
#
# Читает payload хука Claude Code из stdin, через jq берёт session_id и
# hook_event_name и создаёт/удаляет ~/prof/runs/waiting/<session_id>.json.
#
#   создать маркер: Notification, PreToolUse(AskUserQuestion|ExitPlanMode)
#   снять  маркер: PostToolUse, Stop
#
# Скрипт ВСЕГДА exit 0 и pass-through stdin (печатает payload обратно),
# чтобы не ломать цепочку других хуков на том же событии.
set -u

WAIT_DIR="$HOME/prof/runs/waiting"

payload="$(cat)"
printf '%s' "$payload"   # pass-through: вернуть stdin нетронутым

command -v jq >/dev/null 2>&1 || exit 0

sid="$(printf '%s' "$payload" | jq -r '.session_id // empty' 2>/dev/null)"
event="$(printf '%s' "$payload" | jq -r '.hook_event_name // empty' 2>/dev/null)"
tool="$(printf '%s' "$payload" | jq -r '.tool_name // empty' 2>/dev/null)"
[ -n "$sid" ] || exit 0

marker="$WAIT_DIR/$sid.json"

case "$event" in
  Notification)
    mkdir -p "$WAIT_DIR"
    printf '{"ts":%s,"event":"%s"}' "$(date +%s)" "$event" > "$marker"
    ;;
  PreToolUse)
    # ждать ввода = агент собирается спросить пользователя / показать план
    if [ "$tool" = "AskUserQuestion" ] || [ "$tool" = "ExitPlanMode" ]; then
      mkdir -p "$WAIT_DIR"
      printf '{"ts":%s,"event":"%s:%s"}' "$(date +%s)" "$event" "$tool" > "$marker"
    fi
    ;;
  PostToolUse|Stop)
    rm -f "$marker"
    ;;
esac

exit 0
